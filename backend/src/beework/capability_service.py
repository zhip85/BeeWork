"""Capabilities (T-072): the pieces the API, the tool provider, the mount registry and the health job share."""

import json
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode, urlsplit
from uuid import UUID

import jwt
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from beework.capabilities import CANDIDATE_LISTERS, MOUNT_RESOLVERS, MountItem
from beework.config import settings
from beework.model_secrets import decrypt_model_api_key, encrypt_model_api_key
from beework.models.capabilities import Capability, McpConnection
from beework.tools import mcp_client
from beework.tools.executor import PROVIDERS, ToolContext, ToolError, ToolOutput, ToolSpec
from beework.tools.http import ToolHttpError, safe_request
from beework.tools.mcp_client import McpClient, McpError

OAUTH_STATE_MINUTES = 10
TOKEN_REFRESH_MARGIN = timedelta(seconds=60)
UNHEALTHY_AFTER = 3  # consecutive failed health checks before the connection is flagged


def encrypt(value: str) -> str:
    return encrypt_model_api_key(value)


def decrypt(value: str | None) -> str | None:
    return decrypt_model_api_key(value) if value else None


def mask(last4: str | None) -> str | None:
    return f"••••{last4}" if last4 else None


def host_of(url: str) -> str:
    return urlsplit(url).hostname or url


# ---------------------------------------------------------------------------
# Credentials and OAuth 2.1 (authorization code + PKCE).
# ---------------------------------------------------------------------------


def redirect_uri() -> str:
    base = (settings.public_api_url or settings.app_origin).rstrip("/")
    return f"{base}/api/v1/capabilities/mcp/oauth/callback"


def make_state(tenant_id: UUID, user_id: UUID, capability_id: UUID, verifier: str) -> str:
    payload = {
        "t": str(tenant_id), "u": str(user_id), "c": str(capability_id), "v": verifier,
        "exp": datetime.now(UTC) + timedelta(minutes=OAUTH_STATE_MINUTES),
    }
    return jwt.encode(payload, settings.auth_secret, algorithm="HS256")


def read_state(token: str) -> dict[str, str]:
    try:
        return jwt.decode(token, settings.auth_secret, algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise ToolError("mcp.oauth_state", "授权请求无效或已过期，请重新发起授权") from exc


def authorize_url(conn: McpConnection, state: str, challenge: str) -> str:
    query = {
        "response_type": "code", "client_id": conn.oauth_client_id, "redirect_uri": redirect_uri(), "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256",
    }
    if conn.oauth_scopes.strip():
        query["scope"] = conn.oauth_scopes.strip()
    separator = "&" if "?" in conn.oauth_authorize_url else "?"
    return f"{conn.oauth_authorize_url}{separator}{urlencode(query)}"


def _token_request(conn: McpConnection, form: dict[str, str]) -> dict:
    secret = decrypt(conn.oauth_client_secret_encrypted)
    body = {**form, "client_id": conn.oauth_client_id, **({"client_secret": secret} if secret else {})}
    try:
        result = safe_request(
            "POST", conn.oauth_token_url, headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            content=urlencode(body).encode(), resolver=mcp_client.RESOLVER, transport=mcp_client.TRANSPORT,
        )
    except ToolHttpError as exc:
        raise McpError(exc.code, exc.message) from exc
    if result.status >= 400:
        raise McpError("mcp.oauth_failed", "授权服务拒绝了请求，请检查客户端配置后重新授权")
    try:
        data = json.loads(result.body)
    except ValueError as exc:
        raise McpError("mcp.oauth_failed", "授权服务返回了无法解析的内容") from exc
    if not isinstance(data, dict) or not data.get("access_token"):
        raise McpError("mcp.oauth_failed", "授权服务没有返回访问令牌")
    return data


def _store_tokens(conn: McpConnection, data: dict) -> None:
    conn.oauth_access_encrypted = encrypt(str(data["access_token"]))
    if data.get("refresh_token"):
        conn.oauth_refresh_encrypted = encrypt(str(data["refresh_token"]))
    expires_in = data.get("expires_in")
    conn.oauth_expires_at = datetime.now(UTC) + timedelta(seconds=int(expires_in)) if isinstance(expires_in, (int, float)) else None


def exchange_code(conn: McpConnection, code: str, verifier: str) -> None:
    data = _token_request(
        conn, {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri(), "code_verifier": verifier}
    )
    _store_tokens(conn, data)


def access_token(conn: McpConnection) -> str:
    """A usable access token; one that expires within a minute is refreshed first (the refresh token is kept)."""
    token = decrypt(conn.oauth_access_encrypted)
    if token is None:
        raise McpError("mcp.auth_required", "尚未授权，请先完成 OAuth 授权")
    expires = conn.oauth_expires_at
    if expires is not None and expires.replace(tzinfo=expires.tzinfo or UTC) - datetime.now(UTC) < TOKEN_REFRESH_MARGIN:
        refresh = decrypt(conn.oauth_refresh_encrypted)
        if refresh is None:
            raise McpError("mcp.auth_required", "授权已过期，请重新授权")
        data = _token_request(conn, {"grant_type": "refresh_token", "refresh_token": refresh})
        _store_tokens(conn, data)
        token = str(data["access_token"])
    return token


def auth_headers(conn: McpConnection, ctx: ToolContext | None = None) -> dict[str, str]:
    if conn.auth_type == "bearer":
        token = decrypt(conn.secret_encrypted)
        if not token:
            raise McpError("mcp.auth_required", "尚未配置访问令牌")
    elif conn.auth_type == "oauth":
        token = access_token(conn)
    else:
        return {}
    if ctx is not None:
        ctx.register_secret(token)
    return {"Authorization": f"Bearer {token}"}


def client_for(conn: McpConnection, ctx: ToolContext | None = None) -> McpClient:
    return McpClient(conn.url, auth_headers(conn, ctx))


# ---------------------------------------------------------------------------
# Testing and readiness.
# ---------------------------------------------------------------------------


def run_test(conn: McpConnection) -> tuple[bool, str | None]:
    """CAP-030 / CAP-031: connect, list the tools and reconcile them with the selection.

    First test: every tool is ticked. Later tests keep the ticks, new tools start unticked, tools that vanished leave."""
    now = datetime.now(UTC)
    conn.last_test_at = now
    try:
        tools = client_for(conn).list_tools()
    except (McpError, ToolHttpError) as exc:
        conn.last_test_ok, conn.last_test_error = False, exc.message[:300]
        return False, exc.message
    first = not conn.tools
    names = [tool.name for tool in tools]
    conn.selected_tools = list(names) if first else [name for name in (conn.selected_tools or []) if name in names]
    conn.tools = [tool.as_dict() for tool in tools]
    conn.last_test_ok, conn.last_test_error = True, None
    conn.health, conn.health_failures, conn.health_checked_at = "ok", 0, now
    return True, None


def is_ready(conn: McpConnection) -> bool:
    """CAP-032: tested successfully and at least one tool ticked."""
    return bool(conn.last_test_ok and conn.selected_tools)


# ---------------------------------------------------------------------------
# Catalogue integration: mounting, candidates, the executor's provider.
# ---------------------------------------------------------------------------


def _live_caps(db: Session, tenant_id: UUID, kind: str, ids: list[UUID]) -> dict[UUID, Capability]:
    if not ids:
        return {}
    return {
        c.id: c for c in db.scalars(
            select(Capability).where(
                Capability.tenant_id == tenant_id, Capability.kind == kind, Capability.id.in_(ids), Capability.deleted_at.is_(None)
            )
        )
    }


def resolve_mcp_mounts(db: Session, tenant_id: UUID, ids: list[UUID]) -> dict[UUID, MountItem]:
    """CAP-051: available → available; draft / disabled → disabled; a deleted one is not returned (reported deleted)."""
    items: dict[UUID, MountItem] = {}
    for cap_id, cap in _live_caps(db, tenant_id, "mcp", ids).items():
        conn = db.get(McpConnection, cap_id)
        items[cap_id] = MountItem(
            id=cap_id, kind="mcp", name=cap.name, state="available" if cap.status == "available" else "disabled",
            meta={"status": cap.status, "tools": len(conn.selected_tools or []) if conn else 0, "health": conn.health if conn else "unknown"},
        )
    return items


def list_mcp_candidates(db: Session, membership, q: str | None, limit: int) -> list[dict]:
    statement = select(Capability).where(
        Capability.tenant_id == membership.tenant_id, Capability.kind == "mcp", Capability.status == "available",
        Capability.deleted_at.is_(None),
    )
    if q and q.strip():
        statement = statement.where(func.lower(Capability.name).like(f"%{q.strip().lower()}%"))
    items: list[dict] = []
    for cap in db.scalars(statement.order_by(func.lower(Capability.name), Capability.id).limit(limit)):
        conn = db.get(McpConnection, cap.id)
        count = len(conn.selected_tools or []) if conn else 0
        items.append({"id": cap.id, "kind": "mcp", "name": cap.name, "meta": {"summary": f"{count} 个工具 · {host_of(conn.url) if conn else ''}", "tools": count}})
    return items


MOUNT_RESOLVERS["mcp"] = resolve_mcp_mounts
CANDIDATE_LISTERS["mcp"] = list_mcp_candidates


def tool_name(capability_id: UUID, tool: str) -> str:
    return f"mcp:{capability_id}:{tool}"


def mcp_tool_specs(db: Session, tenant_id: UUID, capability_ids: list[UUID]) -> list[ToolSpec]:
    """The tools of the given (available) MCP connections that the administrator ticked — for the agent's tool list."""
    specs: list[ToolSpec] = []
    for cap_id, cap in _live_caps(db, tenant_id, "mcp", capability_ids).items():
        conn = db.get(McpConnection, cap_id)
        if cap.status != "available" or conn is None:
            continue
        ticked = set(conn.selected_tools or [])
        for tool in conn.tools or []:
            if tool.get("name") in ticked:
                specs.append(_spec(cap, tool))
    return specs


def _spec(cap: Capability, tool: dict) -> ToolSpec:
    return ToolSpec(
        name=tool_name(cap.id, tool["name"]), source="mcp", capability_id=cap.id, parameters=tool.get("input_schema") or {},
        writes=not tool.get("read_only", False), description=(tool.get("description") or tool["name"])[:300],
    )


class McpToolProvider:
    """Registers MCP tools with the controlled executor (T-071): ``mcp:{capability id}:{tool}``."""

    def lookup(self, db: Session, ctx: ToolContext, name: str) -> ToolSpec | None:
        parts = name.split(":", 2)
        if len(parts) != 3:
            return None
        try:
            cap_id = UUID(parts[1])
        except ValueError:
            return None
        cap = _live_caps(db, ctx.tenant_id, "mcp", [cap_id]).get(cap_id)
        conn = db.get(McpConnection, cap_id) if cap else None
        if cap is None or conn is None or cap.status != "available":
            return None
        if parts[2] not in (conn.selected_tools or []):  # only ticked tools are visible (CAP-031)
            return None
        tool = next((t for t in conn.tools or [] if t.get("name") == parts[2]), None)
        return _spec(cap, tool) if tool else None

    def execute(self, db: Session, ctx: ToolContext, spec: ToolSpec, args: dict) -> ToolOutput:
        conn = db.get(McpConnection, spec.capability_id)
        assert conn is not None
        client = client_for(conn, ctx)
        text, is_error = client.call_tool(spec.name.split(":", 2)[2], args)
        db.commit()  # a refreshed OAuth token is kept
        if is_error:
            raise ToolError("tool.mcp_error", (text or "工具返回错误")[:300])
        return ToolOutput(content=text, response_bytes=len(text.encode("utf-8")), request_bytes=len(str(args)))


PROVIDERS["mcp"] = McpToolProvider()


# ---------------------------------------------------------------------------
# Health check (CAP-032): every five minutes, by the worker.
# ---------------------------------------------------------------------------


def check_health(db: Session, conn: McpConnection, cap: Capability) -> None:
    """One probe. After ``UNHEALTHY_AFTER`` failures in a row the connection is flagged “连接异常” and the tenant's
    administrators are told once; the capability itself is never disabled automatically."""
    from beework.events import emit_event
    from beework.notifications import notify  # late: notifications imports the approvals module

    now = datetime.now(UTC)
    conn.health_checked_at = now
    try:
        client_for(conn).list_tools()
    except (McpError, ToolHttpError) as exc:
        conn.health_failures += 1
        if conn.health_failures >= UNHEALTHY_AFTER and conn.health != "error":
            conn.health = "error"
            emit_event(db, cap.tenant_id, "mcp.unhealthy", {"capability_id": str(cap.id), "name": cap.name, "failures": conn.health_failures})
            notify(
                db, tenant_id=cap.tenant_id, type="mcp.unhealthy", title=f"MCP 连接异常：{cap.name}",
                body=f"最近 {conn.health_failures} 次健康检查失败：{exc.message[:200]}", link=f"/capabilities/mcp/{cap.id}",
                params={"capability_id": str(cap.id)}, role="tenant_admin",
            )
        return
    conn.health, conn.health_failures = "ok", 0


def mcp_health_job(db: Session, _payload: dict) -> None:
    rows = db.execute(
        select(McpConnection, Capability)
        .join(Capability, Capability.id == McpConnection.capability_id)
        .where(Capability.kind == "mcp", Capability.status == "available", Capability.deleted_at.is_(None))
    ).all()
    for conn, cap in rows:
        check_health(db, conn, cap)
        db.commit()
