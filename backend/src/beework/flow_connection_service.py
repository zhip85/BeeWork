"""Flow connections (T-075, 演进 PRD 8): Chatflow / Workflow connections that agents mount and portal apps use.

A connection wraps an external Dify application. (Until T-169 it could also point at a published flow of the platform's
own canvas; the canvas was retired on 2026-11-12, and a ``native`` connection that still exists is *invalid* — it is never
silently ignored.) This module holds the rules both the API and the runtime share: the derived state, the Dify client (every
call goes through the controlled HTTP client of T-071 and the connection's API key never leaves the server), the executor's
provider and the mount resolver / candidate lister registered with the capability catalogue (T-070).
"""

import json
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from beework.capabilities import CANDIDATE_LISTERS, MOUNT_RESOLVERS, MountItem
from beework.model_secrets import decrypt_model_api_key
from beework.models.flow_connections import FlowConnection
from beework.tools.executor import PROVIDERS, ToolContext, ToolError, ToolOutput, ToolSpec
from beework.tools.http import ToolHttpError, safe_request

# Test seams (like ``mcp_client``): a mock HTTP transport and a DNS resolver; production leaves both ``None``.
TRANSPORT = None
RESOLVER = None
KINDS = ("chatflow", "workflow")
MOUNT_KIND = {"chatflow": "chatflow", "workflow": "flow"}  # the assistant's ``mounts`` key of each connection kind
REFERENCE_TARGET = {"chatflow": "chatflow_connection", "workflow": "workflow_connection"}  # resource_references targets
DIFY_USER = "beework"
STATE_TEXT = {"native_retired": "本平台画布已取消，请改用 Dify 应用"}
# Why a call failed, as a stable category (演进 PRD 8.4): shown next to the last test and recorded with it.
ERROR_CATEGORY = {
    "tool.timeout": "timeout", "tool.unreachable": "network", "tool.unresolvable": "network",
    "tool.forbidden_address": "forbidden", "tool.host_not_allowed": "forbidden", "tool.invalid_url": "config",
    "tool.redirect_blocked": "config", "tool.response_too_large": "response",
}


def host_of(url: str) -> str:
    return urlsplit(url).hostname or ""


def mask_of(conn: FlowConnection) -> str | None:
    return f"****{conn.credential_last4}" if conn.credential_encrypted and conn.credential_last4 else None


# ---------------------------------------------------------------------------
# State.
# ---------------------------------------------------------------------------


def state_of(db: Session, conn: FlowConnection) -> tuple[str, str | None]:
    """(``available`` | ``disabled`` | ``invalid``, reason). A disabled connection stays *disabled*; a leftover ``native`` one
    (its canvas is gone) is *invalid*."""
    if conn.status == "disabled":
        return "disabled", None
    if conn.source_type != "dify":
        return "invalid", STATE_TEXT["native_retired"]
    return "available", None


# ---------------------------------------------------------------------------
# Dify client (every call goes through the controlled HTTP client).
# ---------------------------------------------------------------------------


def _headers(conn: FlowConnection, ctx: ToolContext | None) -> dict[str, str]:
    key = decrypt_model_api_key(conn.credential_encrypted) if conn.credential_encrypted else ""
    if not key:
        raise ToolError("tool.auth_required", "尚未配置 Dify API Key")
    if ctx is not None:
        ctx.register_secret(key)
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def _dify_error(status: int, body: bytes) -> ToolError:
    try:
        message = str(json.loads(body.decode("utf-8", "replace")).get("message") or "")[:200]
    except (ValueError, AttributeError):
        message = ""
    if status in (401, 403):
        return ToolError("tool.auth_failed", "Dify 拒绝了凭据，请检查 API Key 是否属于该应用")
    if status == 404:
        return ToolError("tool.not_found", "Dify 找不到该应用，请检查 Base URL 与应用类型（Chatflow / Workflow）是否匹配")
    if status == 429:
        return ToolError("tool.rate_limited", "Dify 限流，请稍后重试")
    if status == 400:
        return ToolError("tool.invalid_arguments", f"Dify 拒绝了输入：{message or '参数不符合应用的输入定义'}")
    return ToolError("tool.upstream_error", f"Dify 返回 {status}" + (f"：{message}" if message else ""))


def _dify_request(conn: FlowConnection, ctx: ToolContext | None, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    base = conn.base_url.rstrip("/")
    try:
        result = safe_request(
            "POST", f"{base}{path}", headers=_headers(conn, ctx), json=payload,
            allowed_host=host_of(conn.base_url),  # the key is only ever sent to the host the administrator entered
            transport=TRANSPORT, resolver=RESOLVER,
        )
    except ToolHttpError as exc:
        raise ToolError(exc.code, exc.message) from exc
    if not 200 <= result.status < 300:
        raise _dify_error(result.status, result.body)
    try:
        data = json.loads(result.body.decode("utf-8"))
    except ValueError as exc:
        raise ToolError("tool.bad_response", "Dify 返回的不是有效的 JSON") from exc
    if not isinstance(data, dict):
        raise ToolError("tool.bad_response", "Dify 返回的格式不符合预期")
    return data


def _inputs_for(conn: FlowConnection, args: dict[str, Any]) -> dict[str, Any]:
    """Assemble the Dify ``inputs`` from the tool arguments by ``input_mapping`` ({Dify variable: argument name})."""
    mapping = conn.input_mapping or {}
    if not mapping:
        raw = args.get("inputs")
        return raw if isinstance(raw, dict) else {}
    return {variable: args[argument] for variable, argument in mapping.items() if argument in args}


def _output_text(conn: FlowConnection, outputs: Any) -> str:
    field = (conn.output_mapping or {}).get("field")
    if field and isinstance(outputs, dict) and field in outputs:
        value = outputs[field]
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    if isinstance(outputs, str):
        return outputs
    return json.dumps(outputs, ensure_ascii=False)


def call_dify(conn: FlowConnection, ctx: ToolContext | None, args: dict[str, Any]) -> str:
    """Run the Dify application once, blocking, and return the text the model should see."""
    if conn.kind == "chatflow":
        data = _dify_request(
            conn, ctx, "/chat-messages",
            {"inputs": _inputs_for(conn, args), "query": str(args.get("query") or ""), "response_mode": "blocking", "user": DIFY_USER},
        )
        answer = data.get("answer")
        if not isinstance(answer, str):
            raise ToolError("tool.bad_response", "Dify 没有返回回答（answer）")
        return answer
    data = _dify_request(
        conn, ctx, "/workflows/run", {"inputs": _inputs_for(conn, args), "response_mode": "blocking", "user": DIFY_USER}
    )
    body = data.get("data") if isinstance(data.get("data"), dict) else {}
    if body.get("status") not in (None, "succeeded"):
        raise ToolError("tool.workflow_failed", f"Dify 工作流未成功：{str(body.get('error') or body.get('status'))[:200]}")
    return _output_text(conn, body.get("outputs"))


# ---------------------------------------------------------------------------
# Testing (演进 PRD 8.4): one real call; the result, the time and the error category are kept.
# ---------------------------------------------------------------------------


def run_test(db: Session, conn: FlowConnection) -> tuple[bool, str | None, str | None, int | None]:
    """(ok, error message, error category, duration ms): one real call to the Dify application. A leftover ``native``
    connection (the canvas was retired, T-169) fails with its reason."""
    import time

    started = time.monotonic()
    conn.last_test_at = datetime.now(UTC)
    error: str | None = None
    category: str | None = None
    if conn.source_type != "dify":
        error, category = STATE_TEXT["native_retired"], "invalid"
    else:
        try:
            call_dify(conn, None, {"query": "连接测试", "inputs": {}})
        except ToolError as exc:
            error, category = exc.message, ERROR_CATEGORY.get(exc.code, exc.code.removeprefix("tool."))
    ms = int((time.monotonic() - started) * 1000)
    conn.last_test_ok, conn.last_test_error, conn.last_test_ms = error is None, error, ms
    return error is None, error, category, ms


# ---------------------------------------------------------------------------
# Catalogue integration: mounting, candidates and the executor's provider.
# ---------------------------------------------------------------------------


def _live(db: Session, tenant_id: UUID, kind: str, ids: list[UUID]) -> dict[UUID, FlowConnection]:
    if not ids:
        return {}
    return {
        c.id: c for c in db.scalars(
            select(FlowConnection).where(
                FlowConnection.tenant_id == tenant_id, FlowConnection.kind == kind, FlowConnection.id.in_(ids),
                FlowConnection.deleted_at.is_(None),
            )
        )
    }


def _resolver(kind: str):
    def resolve(db: Session, tenant_id: UUID, ids: list[UUID]) -> dict[UUID, MountItem]:
        """CAP-051: available → available; disabled / invalid → disabled (reason in ``meta``); a deleted one is not returned."""
        items: dict[UUID, MountItem] = {}
        for conn_id, conn in _live(db, tenant_id, kind, ids).items():
            state, reason = state_of(db, conn)
            items[conn_id] = MountItem(
                id=conn_id, kind=MOUNT_KIND[kind], name=conn.name, state="available" if state == "available" else "disabled",
                meta={"source": conn.source_type, "state": state, "reason": reason},
            )
        return items

    return resolve


def _lister(kind: str):
    def candidates(db: Session, membership, q: str | None, limit: int) -> list[dict]:
        statement = select(FlowConnection).where(
            FlowConnection.tenant_id == membership.tenant_id, FlowConnection.kind == kind,
            FlowConnection.status == "enabled", FlowConnection.deleted_at.is_(None),
        )
        if q and q.strip():
            statement = statement.where(func.lower(FlowConnection.name).like(f"%{q.strip().lower()}%"))
        items: list[dict] = []
        for conn in db.scalars(statement.order_by(func.lower(FlowConnection.name), FlowConnection.id)):
            if state_of(db, conn)[0] != "available":
                continue  # RUL-010: only what can be called now
            items.append({"id": conn.id, "kind": MOUNT_KIND[kind], "name": conn.name, "meta": {"summary": f"Dify · {host_of(conn.base_url)}", "source": conn.source_type}})
            if len(items) >= limit:
                break
        return items

    return candidates


def tool_name(connection_id: UUID) -> str:
    return f"connection:{connection_id}"


def _spec(conn: FlowConnection) -> ToolSpec:
    mapping = conn.input_mapping or {}
    properties: dict[str, Any] = {}
    required: list[str] = []
    if conn.kind == "chatflow":
        properties["query"] = {"type": "string", "description": "发给对话流程的问题"}
        required.append("query")
    for argument in dict.fromkeys(mapping.values()):
        properties[argument] = {"type": "string"}
    if conn.kind == "workflow" and not mapping:
        properties["inputs"] = {"type": "object", "description": "工作流的输入变量"}
    return ToolSpec(
        name=tool_name(conn.id), source="connection", capability_id=conn.id,
        parameters={"type": "object", "properties": properties, "required": required},
        writes=False,  # chosen and mounted by an administrator; the answer is text for the model (see T-075 完成记录)
        description=(conn.description or conn.name)[:300],
    )


def connection_tool_specs(db: Session, tenant_id: UUID, kind: str, ids: list[UUID]) -> list[ToolSpec]:
    return [_spec(c) for c in _live(db, tenant_id, kind, ids).values() if state_of(db, c)[0] == "available"]


class FlowConnectionProvider:
    """``connection:{connection id}`` — a Chatflow / Workflow connection offered to an agent as one tool."""

    def lookup(self, db: Session, ctx: ToolContext, name: str) -> ToolSpec | None:
        try:
            conn_id = UUID(name.split(":", 1)[1])
        except (IndexError, ValueError):
            return None
        conn = db.scalar(
            select(FlowConnection).where(
                FlowConnection.id == conn_id, FlowConnection.tenant_id == ctx.tenant_id, FlowConnection.deleted_at.is_(None)
            )
        )
        if conn is None or state_of(db, conn)[0] != "available":
            return None
        return _spec(conn)

    def execute(self, db: Session, ctx: ToolContext, spec: ToolSpec, args: dict[str, Any]) -> ToolOutput:
        conn = db.get(FlowConnection, spec.capability_id)
        if conn is None:
            raise ToolError("tool.unavailable", "该流程连接当前不可用")
        if conn.source_type != "dify":
            raise ToolError("tool.unavailable", STATE_TEXT["native_retired"])
        text = call_dify(conn, ctx, args)
        return ToolOutput(content=text, response_bytes=len(text.encode("utf-8")), request_bytes=len(json.dumps(args, ensure_ascii=False)))


MOUNT_RESOLVERS["chatflow"] = _resolver("chatflow")
MOUNT_RESOLVERS["flow"] = _resolver("workflow")
CANDIDATE_LISTERS["chatflow"] = _lister("chatflow")
CANDIDATE_LISTERS["flow"] = _lister("workflow")
PROVIDERS["connection"] = FlowConnectionProvider()
