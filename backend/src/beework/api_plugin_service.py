"""API plugins (T-073, PRD 3.5.5, CAP-040–042): a REST or GraphQL API offered to agents as tools.

The description is parsed by :mod:`beework.api_spec`; every call goes through the controlled executor (T-071), which
validates the arguments, asks for the end user's confirmation on writes, scrubs credentials from everything that leaves, and
records only metadata. HTTP is done with ``safe_request`` and is limited to the plugin's own host: the credentials are only
ever sent where the administrator pointed the plugin (演进 PRD 15.3). An OAuth 2.0 client-credentials token is fetched at call
time, cached encrypted and refreshed shortly before it expires (CAP-040).
"""

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote, urlencode, urlsplit
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from beework.api_spec import ApiTool, SpecError, parse_spec
from beework.capabilities import CANDIDATE_LISTERS, MOUNT_RESOLVERS, MountItem
from beework.capability_service import _live_caps, decrypt, encrypt
from beework.models.capabilities import ApiPlugin, Capability
from beework.tools.executor import PROVIDERS, ToolContext, ToolError, ToolOutput, ToolSpec
from beework.tools.http import ToolHttpError, safe_request

# Test seams (like ``mcp_client``): a mock HTTP transport and a DNS resolver; production leaves both ``None``.
TRANSPORT = None
RESOLVER = None
TOKEN_REFRESH_MARGIN = timedelta(seconds=60)
# Headers the model may not set through a parameter: they would change who the request is for or how it is framed.
FORBIDDEN_HEADERS = {"host", "content-length", "transfer-encoding", "connection", "authorization", "cookie", "proxy-authorization", "te", "upgrade"}
DEFAULT_TOKEN_TTL = 3600


def host_of(url: str) -> str:
    return urlsplit(url).hostname or url


def mask_of(last4: str | None) -> str | None:
    return f"••••{last4}" if last4 else None


# ---------------------------------------------------------------------------
# The spec and its tools.
# ---------------------------------------------------------------------------


def apply_spec(plugin: ApiPlugin, text: str, filename: str = "") -> list[ApiTool]:
    """Parse and store the description. The ticked tools that still exist stay ticked; new ones start unticked — an
    administrator opts every tool in (a write is never offered by default). Raises :class:`SpecError`."""
    tools = parse_spec(text, plugin.api_type)
    names = [tool.name for tool in tools]
    plugin.spec_text, plugin.spec_filename = text, filename[:200]
    plugin.tools = [tool.as_dict() for tool in tools]
    plugin.selected_tools = [name for name in (plugin.selected_tools or []) if name in names]
    plugin.last_test_ok, plugin.last_test_at, plugin.last_test_error, plugin.last_test_tool = False, None, None, None
    return tools


def is_ready(plugin: ApiPlugin) -> bool:
    """CAP-041: a spec with tools, a successful test call and at least one ticked tool."""
    return bool(plugin.tools and plugin.last_test_ok and plugin.selected_tools)


def _tool(plugin: ApiPlugin, name: str) -> dict[str, Any] | None:
    return next((t for t in plugin.tools or [] if t.get("name") == name), None)


# ---------------------------------------------------------------------------
# Credentials.
# ---------------------------------------------------------------------------


def _client_credentials_token(plugin: ApiPlugin, ctx: ToolContext | None) -> str:
    secret = decrypt(plugin.oauth_client_secret_encrypted)
    form = {"grant_type": "client_credentials", "client_id": plugin.oauth_client_id}
    if secret:
        form["client_secret"] = secret
        if ctx is not None:
            ctx.register_secret(secret)
    if plugin.oauth_scope:
        form["scope"] = plugin.oauth_scope
    try:
        result = safe_request(
            "POST", plugin.oauth_token_url, headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            content=urlencode(form).encode(), resolver=RESOLVER, transport=TRANSPORT,
        )
    except ToolHttpError as exc:
        raise ToolError(exc.code, exc.message) from exc
    if result.status >= 400:
        raise ToolError("tool.auth_failed", "授权服务拒绝了请求，请检查 Token URL、客户端 ID 和密钥")
    try:
        data = json.loads(result.body.decode("utf-8"))
        token = str(data["access_token"])
    except (ValueError, KeyError, TypeError) as exc:
        raise ToolError("tool.auth_failed", "授权服务没有返回 access_token") from exc
    expires_in = data.get("expires_in")
    ttl = int(expires_in) if isinstance(expires_in, (int, float)) else DEFAULT_TOKEN_TTL
    plugin.oauth_access_encrypted = encrypt(token)
    plugin.oauth_expires_at = datetime.now(UTC) + timedelta(seconds=ttl)
    return token


def auth_headers(plugin: ApiPlugin, ctx: ToolContext | None = None) -> dict[str, str]:
    """The credential header of the plugin; every secret is registered so it is scrubbed from results and errors."""
    secret = decrypt(plugin.secret_encrypted)
    if plugin.auth_type == "api_key":
        if not secret or not plugin.api_key_header:
            raise ToolError("tool.auth_required", "尚未配置 API Key")
        headers = {plugin.api_key_header: secret}
    elif plugin.auth_type == "bearer":
        if not secret:
            raise ToolError("tool.auth_required", "尚未配置访问令牌")
        headers = {"Authorization": f"Bearer {secret}"}
    elif plugin.auth_type == "oauth_cc":
        token = decrypt(plugin.oauth_access_encrypted)
        expires = plugin.oauth_expires_at
        stale = expires is None or expires.replace(tzinfo=expires.tzinfo or UTC) - datetime.now(UTC) < TOKEN_REFRESH_MARGIN
        if token is None or stale:
            token = _client_credentials_token(plugin, ctx)
        headers = {"Authorization": f"Bearer {token}"}
        secret = token
    else:
        return {}
    if ctx is not None:
        ctx.register_secret(secret)
    return headers


# ---------------------------------------------------------------------------
# Building and sending the request.
# ---------------------------------------------------------------------------


def _text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ",".join(_text(v) for v in value)
    return str(value)


def _rest_request(plugin: ApiPlugin, binding: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    path = str(binding["path"])
    for entry in binding.get("path_params") or []:
        value = _text(args.get(entry["arg"], ""))
        if value in ("", ".", "..") or "\x00" in value:
            raise ToolError("tool.invalid_arguments", f"路径参数 {entry['name']} 的值不合法")
        path = path.replace("{" + entry["name"] + "}", quote(value, safe=""))
    query = {e["name"]: _text(args[e["arg"]]) for e in binding.get("query_params") or [] if args.get(e["arg"]) is not None}
    headers: dict[str, str] = {"Accept": "application/json"}
    for entry in binding.get("header_params") or []:
        if args.get(entry["arg"]) is None:
            continue
        if entry["name"].lower() in FORBIDDEN_HEADERS:
            raise ToolError("tool.invalid_arguments", f"不允许设置请求头 {entry['name']}")
        headers[entry["name"]] = _text(args[entry["arg"]])
    request: dict[str, Any] = {"method": binding["method"], "url": plugin.base_url.rstrip("/") + path, "params": query, "headers": headers}
    if binding.get("body") and args.get("body") is not None:
        request["json"] = args["body"]
    return request


def _graphql_request(plugin: ApiPlugin, binding: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    declared = {name: type_text for name, type_text in (binding.get("args") or {}).items() if name in args}
    variables = ", ".join(f"${name}: {type_text}" for name, type_text in declared.items())
    call = ", ".join(f"{name}: ${name}" for name in declared)
    document = (
        f"{binding['operation']}{'(' + variables + ')' if variables else ''} "
        f"{{ {binding['field']}{'(' + call + ')' if call else ''}{binding.get('selection') or ''} }}"
    )
    return {
        "method": "POST", "url": plugin.base_url, "params": {}, "headers": {"Accept": "application/json"},
        "json": {"query": document, "variables": {name: args[name] for name in declared}},
    }


def _error_text(body: bytes) -> str:
    text = body.decode("utf-8", "replace").strip()
    try:
        parsed = json.loads(text)
        for key in ("message", "error", "detail", "error_description"):
            if isinstance(parsed, dict) and isinstance(parsed.get(key), str):
                return parsed[key][:200]
    except ValueError:
        pass
    return text[:200]


def call_tool(plugin: ApiPlugin, tool: dict[str, Any], ctx: ToolContext | None, args: dict[str, Any]) -> tuple[str, int]:
    """Send one call and return (text for the model, request bytes)."""
    binding = tool.get("binding") or {}
    request = _graphql_request(plugin, binding, args) if binding.get("type") == "graphql" else _rest_request(plugin, binding, args)
    headers = {**request["headers"], **auth_headers(plugin, ctx)}
    sent = len(json.dumps(request.get("json"), ensure_ascii=False)) if "json" in request else 0
    try:
        result = safe_request(
            request["method"], request["url"], headers=headers, json=request.get("json"), params=request["params"],
            allowed_host=host_of(plugin.base_url), resolver=RESOLVER, transport=TRANSPORT,
        )
    except ToolHttpError as exc:
        raise ToolError(exc.code, exc.message) from exc
    if result.status in (401, 403):
        raise ToolError("tool.auth_failed", "接口拒绝了凭据，请检查认证配置")
    if not 200 <= result.status < 300:
        raise ToolError("tool.api_error", f"接口返回 {result.status}：{_error_text(result.body)}".rstrip("："))
    text = result.body.decode("utf-8", "replace")
    if binding.get("type") == "graphql":
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise ToolError("tool.bad_response", "接口返回的不是有效的 JSON") from exc
        errors = data.get("errors") if isinstance(data, dict) else None
        if errors and not (isinstance(data.get("data"), dict) and data["data"].get(binding["field"]) is not None):
            first = errors[0].get("message") if isinstance(errors[0], dict) else str(errors[0])
            raise ToolError("tool.api_error", f"GraphQL 错误：{str(first)[:200]}")
        return json.dumps((data.get("data") or {}).get(binding["field"]), ensure_ascii=False), sent
    try:  # compact JSON is cheaper for the model; anything else is passed through as text
        return json.dumps(json.loads(text), ensure_ascii=False), sent
    except ValueError:
        return text, sent


# ---------------------------------------------------------------------------
# Catalogue integration: mounting, candidates and the executor's provider.
# ---------------------------------------------------------------------------


def tool_name(capability_id: UUID, tool: str) -> str:
    return f"api:{capability_id}:{tool}"


def _spec(cap: Capability, tool: dict[str, Any]) -> ToolSpec:
    return ToolSpec(
        name=tool_name(cap.id, tool["name"]), source="api", capability_id=cap.id, parameters=tool.get("input_schema") or {},
        writes=bool(tool.get("writes")), description=(tool.get("description") or tool["name"])[:300],
    )


def api_tool_specs(db: Session, tenant_id: UUID, capability_ids: list[UUID]) -> list[ToolSpec]:
    """The ticked tools of the given available plugins — the agent's tool list."""
    specs: list[ToolSpec] = []
    for cap_id, cap in _live_caps(db, tenant_id, "api", capability_ids).items():
        plugin = db.get(ApiPlugin, cap_id)
        if cap.status != "available" or plugin is None:
            continue
        ticked = set(plugin.selected_tools or [])
        specs.extend(_spec(cap, t) for t in plugin.tools or [] if t.get("name") in ticked)
    return specs


def resolve_api_mounts(db: Session, tenant_id: UUID, ids: list[UUID]) -> dict[UUID, MountItem]:
    items: dict[UUID, MountItem] = {}
    for cap_id, cap in _live_caps(db, tenant_id, "api", ids).items():
        plugin = db.get(ApiPlugin, cap_id)
        items[cap_id] = MountItem(
            id=cap_id, kind="api", name=cap.name, state="available" if cap.status == "available" else "disabled",
            meta={"status": cap.status, "tools": len(plugin.selected_tools or []) if plugin else 0},
        )
    return items


def list_api_candidates(db: Session, membership, q: str | None, limit: int) -> list[dict]:
    statement = select(Capability).where(
        Capability.tenant_id == membership.tenant_id, Capability.kind == "api", Capability.status == "available", Capability.deleted_at.is_(None),
    )
    if q and q.strip():
        statement = statement.where(func.lower(Capability.name).like(f"%{q.strip().lower()}%"))
    items: list[dict] = []
    for cap in db.scalars(statement.order_by(func.lower(Capability.name), Capability.id).limit(limit)):
        plugin = db.get(ApiPlugin, cap.id)
        count = len(plugin.selected_tools or []) if plugin else 0
        kind = "GraphQL" if plugin is not None and plugin.api_type == "graphql" else "REST"
        items.append({"id": cap.id, "kind": "api", "name": cap.name, "meta": {"summary": f"{count} 个工具 · {kind} · {host_of(plugin.base_url) if plugin else ''}", "tools": count}})
    return items


class ApiToolProvider:
    """Registers API plugin tools with the controlled executor (T-071): ``api:{capability id}:{tool}``."""

    def lookup(self, db: Session, ctx: ToolContext, name: str) -> ToolSpec | None:
        parts = name.split(":", 2)
        if len(parts) != 3:
            return None
        try:
            cap_id = UUID(parts[1])
        except ValueError:
            return None
        cap = _live_caps(db, ctx.tenant_id, "api", [cap_id]).get(cap_id)
        plugin = db.get(ApiPlugin, cap_id) if cap else None
        if cap is None or plugin is None:
            return None
        if cap.status != "available" and not (ctx.allow_unavailable and cap.status == "draft"):  # a draft may be tested, a disabled one may not
            return None
        if parts[2] not in (plugin.selected_tools or []):  # only ticked tools are visible
            return None
        tool = _tool(plugin, parts[2])
        return _spec(cap, tool) if tool else None

    def execute(self, db: Session, ctx: ToolContext, spec: ToolSpec, args: dict[str, Any]) -> ToolOutput:
        plugin = db.get(ApiPlugin, spec.capability_id)
        tool = _tool(plugin, spec.name.split(":", 2)[2]) if plugin else None
        if plugin is None or tool is None:
            raise ToolError("tool.unavailable", "该接口当前不可用")
        text, sent = call_tool(plugin, tool, ctx, args)
        db.commit()  # a refreshed OAuth token is kept
        return ToolOutput(content=text, response_bytes=len(text.encode("utf-8")), request_bytes=sent)


MOUNT_RESOLVERS["api"] = resolve_api_mounts
CANDIDATE_LISTERS["api"] = list_api_candidates
PROVIDERS["api"] = ApiToolProvider()

__all__ = ["SpecError", "apply_spec", "api_tool_specs", "is_ready", "tool_name"]
