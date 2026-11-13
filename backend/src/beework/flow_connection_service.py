"""Flow connections (T-075, 演进 PRD 8): Chatflow / Workflow connections that agents mount and portal apps use.

A connection wraps an external Dify application. (Until T-169 it could also point at a published flow of the platform's
own canvas; the canvas was retired on 2026-11-12, and a ``native`` connection that still exists is *invalid* — it is never
silently ignored.) This module holds the rules both the API and the runtime share: the derived state, the Dify client (every
call goes through the controlled HTTP client of T-071 and the connection's API key never leaves the server), the executor's
provider and the mount resolver / candidate lister registered with the capability catalogue (T-070).
"""

import hashlib
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from beework.capabilities import CANDIDATE_LISTERS, MOUNT_RESOLVERS, MountItem
from beework.model_secrets import decrypt_model_api_key
from beework.models.flow_connections import DifyConversation, FlowConnection
from beework.models.runtime import Run
from beework.tools.executor import PROVIDERS, ToolContext, ToolError, ToolOutput, ToolSpec
from beework.tools.http import ToolHttpError, retry_after_of, safe_request

logger = logging.getLogger(__name__)

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


def _dify_error(status: int, body: bytes, retry_after: float | None = None) -> ToolError:
    try:
        message = str(json.loads(body.decode("utf-8", "replace")).get("message") or "")[:200]
    except (ValueError, AttributeError):
        message = ""
    if status in (401, 403):
        return ToolError("tool.auth_failed", "Dify 拒绝了凭据，请检查 API Key 是否属于该应用")
    if status == 404:
        return ToolError("tool.not_found", "Dify 找不到该应用，请检查 Base URL 与应用类型（Chatflow / Workflow）是否匹配")
    if status == 429:
        return ToolError("tool.rate_limited", "Dify 限流，请稍后重试", status=429, retry_after=retry_after)
    if status == 400:
        return ToolError("tool.invalid_arguments", f"Dify 拒绝了输入：{message or '参数不符合应用的输入定义'}")
    return ToolError("tool.upstream_error", f"Dify 返回 {status}" + (f"：{message}" if message else ""), status=status, retry_after=retry_after)


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
        raise _dify_error(result.status, result.body, retry_after_of(result.headers))
    try:
        data = json.loads(result.body.decode("utf-8"))
    except ValueError as exc:
        raise ToolError("tool.bad_response", "Dify 返回的不是有效的 JSON") from exc
    if not isinstance(data, dict):
        raise ToolError("tool.bad_response", "Dify 返回的格式不符合预期")
    return data


def user_hash(tenant_id: UUID, end_user: str | None) -> str:
    """Dify's ``user``: a stable hash of the end user (the same person is the same Dify user in every conversation), never an email
    or a phone number. Without an end user (a test, a debug run) the tenant alone identifies the caller."""
    return hashlib.sha256(f"{tenant_id}:{end_user or 'anonymous'}".encode()).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Application metadata and parameters (``/info``, ``/parameters``).
# ---------------------------------------------------------------------------

MODES = {"chatflow": ("advanced-chat", "chat"), "workflow": ("workflow",)}
FORM_TYPES = {"text-input": "string", "paragraph": "string", "select": "string", "number": "number"}
RESERVED = ("query",)  # tool arguments that are not Dify input variables
RETRY_CODES = {"tool.timeout", "tool.unreachable", "tool.rate_limited", "tool.upstream_error"}  # safe to repeat a read (T-165 owns the policy)


def normalize_parameters(raw: Any) -> list[dict[str, Any]]:
    """Dify's ``user_input_form`` ([{"text-input": {...}}, ...]) as a flat list: variable, label, type, required, default, options,
    max_length. File inputs are not supported (the agent cannot supply a file) and are left out."""
    fields: list[dict[str, Any]] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict) or len(item) != 1:
            continue
        form_type, body = next(iter(item.items()))
        if form_type not in FORM_TYPES or not isinstance(body, dict) or not isinstance(body.get("variable"), str):
            continue
        options = [str(o) for o in body.get("options") or []][:50] if form_type == "select" else []
        fields.append({
            "variable": body["variable"][:80], "label": str(body.get("label") or body["variable"])[:80], "type": FORM_TYPES[form_type],
            "required": bool(body.get("required")), "default": body.get("default") if isinstance(body.get("default"), str | int | float) else None,
            "options": options, "max_length": body.get("max_length") if isinstance(body.get("max_length"), int) else None,
        })
    return fields[:50]


def _get(conn: FlowConnection, path: str) -> dict[str, Any]:
    """A read of the application: retried on a network failure, a timeout, a 5xx or a rate limit (idempotent, so repeating is safe)."""
    last: ToolError | None = None
    for attempt in range(3):
        try:
            result = safe_request(
                "GET", f"{conn.base_url.rstrip('/')}{path}", headers=_headers(conn, None), allowed_host=host_of(conn.base_url),
                transport=TRANSPORT, resolver=RESOLVER,
            )
            if not 200 <= result.status < 300:
                raise _dify_error(result.status, result.body)
            data = json.loads(result.body.decode("utf-8"))
            if not isinstance(data, dict):
                raise ToolError("tool.bad_response", "Dify 返回的格式不符合预期")
            return data
        except ToolHttpError as exc:
            last = ToolError(exc.code, exc.message)
        except ToolError as exc:
            last = exc
        except ValueError as exc:
            raise ToolError("tool.bad_response", "Dify 返回的不是有效的 JSON") from exc
        if last.code not in RETRY_CODES or attempt == 2:
            break
        time.sleep(0.2 * (attempt + 1))
    assert last is not None
    raise last


def sync_app(conn: FlowConnection) -> None:
    """Read the application's mode and input variables into the connection (T-163). A mode that does not fit the connection's kind is
    refused with the reason (``flow.kind_mismatch``); any other failure leaves the connection as it was and is recorded."""
    info = _get(conn, "/info")
    mode = str(info.get("mode") or "")
    if not mode:
        raise ToolError("tool.bad_response", "Dify 没有返回应用类型（/info）")
    if mode not in MODES[conn.kind]:
        names = {"advanced-chat": "Chatflow", "chat": "聊天助手", "workflow": "Workflow", "agent-chat": "Agent", "completion": "文本生成"}
        raise ToolError("flow.kind_mismatch", f"该 Dify 应用是“{names.get(mode, mode or '未知类型')}”，与所选的“{'Chatflow' if conn.kind == 'chatflow' else 'Workflow'}”不一致")
    parameters = _get(conn, "/parameters")
    conn.app_mode = mode[:20]
    conn.parameters = normalize_parameters(parameters.get("user_input_form"))
    conn.parameters_synced_at = datetime.now(UTC)
    conn.parameters_error = None


# ---------------------------------------------------------------------------
# Calls
# ---------------------------------------------------------------------------


def _inputs_for(conn: FlowConnection, args: dict[str, Any]) -> dict[str, Any]:
    """Assemble the Dify ``inputs`` from the tool arguments: by ``input_mapping`` ({Dify variable: argument name}) when the
    administrator set one (it overrides), otherwise by the application's own variable names."""
    mapping = conn.input_mapping or {}
    if mapping:
        return {variable: args[argument] for variable, argument in mapping.items() if argument in args}
    if conn.parameters:
        return {p["variable"]: args[p["variable"]] for p in conn.parameters if p["variable"] in args and not (conn.kind == "chatflow" and p["variable"] in RESERVED)}
    raw = args.get("inputs")
    return raw if isinstance(raw, dict) else {}


def _output_text(conn: FlowConnection, outputs: Any) -> str:
    field = (conn.output_mapping or {}).get("field")
    if field and isinstance(outputs, dict) and field in outputs:
        value = outputs[field]
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    if isinstance(outputs, str):
        return outputs
    return json.dumps(outputs, ensure_ascii=False)


@dataclass
class DifyReply:
    text: str
    dify_conversation_id: str | None = None


class _Sse:
    """Reads Dify's server-sent events as the bytes arrive: the answer so far, the ids needed to continue or stop, the final outputs."""

    def __init__(self, conn: FlowConnection, sink: Callable[[str], None] | None, check_stop: Callable[[], None]) -> None:
        self.conn, self.sink, self.check_stop = conn, sink, check_stop
        self.buffer = b""
        self.answer: list[str] = []
        self.conversation_id: str | None = None
        self.task_id: str | None = None
        self.outputs: Any = None
        self.workflow_status: str | None = None
        self.workflow_error: str | None = None
        self.error: ToolError | None = None
        self.ended = False

    def feed(self, chunk: bytes) -> None:
        self.buffer += chunk.replace(b"\r\n", b"\n")  # a proxy may normalise line endings
        while b"\n\n" in self.buffer:
            raw, self.buffer = self.buffer.split(b"\n\n", 1)
            for line in raw.split(b"\n"):
                if line.startswith(b"data:"):
                    self.event(line[5:].strip())

    def finish(self) -> None:
        """The last frame may arrive without its closing blank line."""
        rest, self.buffer = self.buffer, b""
        for line in rest.split(b"\n"):
            if line.startswith(b"data:"):
                self.event(line[5:].strip())

    def event(self, payload: bytes) -> None:
        self.check_stop()  # between events: a cancelled or timed-out run stops reading
        try:
            data = json.loads(payload.decode("utf-8"))
        except ValueError:
            return
        if not isinstance(data, dict):
            return
        self.conversation_id = str(data["conversation_id"]) if data.get("conversation_id") else self.conversation_id
        self.task_id = str(data["task_id"]) if data.get("task_id") else self.task_id
        kind = data.get("event")
        if kind in ("message", "agent_message") and isinstance(data.get("answer"), str):
            self.answer.append(data["answer"])
            if self.sink is not None and data["answer"]:
                self.sink(data["answer"])
        elif kind == "message_end":
            self.ended = True
        elif kind == "workflow_finished" and isinstance(data.get("data"), dict):
            self.outputs, self.workflow_status, self.workflow_error = data["data"].get("outputs"), data["data"].get("status"), data["data"].get("error")
            self.ended = True
        elif kind == "error":
            status = data.get("status")
            self.error = _dify_error(status if isinstance(status, int) else 500, json.dumps({"message": data.get("message")}).encode())


def _stop(conn: FlowConnection, task_id: str | None, user: str) -> None:
    """Tell Dify to stop a run the user cancelled (best effort: the run is cancelled on our side either way)."""
    if not task_id:
        return
    path = f"/chat-messages/{task_id}/stop" if conn.kind == "chatflow" else f"/workflows/tasks/{task_id}/stop"
    try:
        _dify_request(conn, None, path, {"user": user})
    except Exception:  # noqa: BLE001 — nothing to do about a failed stop
        logger.info("Dify stop request failed", exc_info=True)


def call_dify(
    conn: FlowConnection, ctx: ToolContext | None, args: dict[str, Any], *, dify_conversation_id: str | None = None,
    user: str | None = None, sink: Callable[[str], None] | None = None,
) -> DifyReply:
    """Run the Dify application once and return the text the model should see. With ``streaming`` on (or a ``sink`` that wants the
    tokens) the response is read as server-sent events as it arrives; a cancelled run stops the Dify run too."""
    user = user or DIFY_USER
    stream = conn.streaming or sink is not None
    mode = "streaming" if stream else "blocking"
    if conn.kind == "chatflow":
        path = "/chat-messages"
        payload: dict[str, Any] = {"inputs": _inputs_for(conn, args), "query": str(args.get("query") or ""), "response_mode": mode, "user": user}
        if dify_conversation_id:
            payload["conversation_id"] = dify_conversation_id
    else:
        path, payload = "/workflows/run", {"inputs": _inputs_for(conn, args), "response_mode": mode, "user": user}
    if not stream:
        data = _dify_request(conn, ctx, path, payload)
        if conn.kind == "chatflow":
            answer = data.get("answer")
            if not isinstance(answer, str):
                raise ToolError("tool.bad_response", "Dify 没有返回回答（answer）")
            return DifyReply(answer, str(data["conversation_id"]) if data.get("conversation_id") else None)
        body = data.get("data") if isinstance(data.get("data"), dict) else {}
        if body.get("status") not in (None, "succeeded"):
            raise ToolError("tool.workflow_failed", f"Dify 工作流未成功：{str(body.get('error') or body.get('status'))[:200]}")
        return DifyReply(_output_text(conn, body.get("outputs")))
    reader = _Sse(conn, sink if conn.kind == "chatflow" else None, ctx.check_stop if ctx is not None else (lambda: None))
    try:
        result = safe_request(
            "POST", f"{conn.base_url.rstrip('/')}{path}", headers=_headers(conn, ctx), json=payload,
            allowed_host=host_of(conn.base_url), transport=TRANSPORT, resolver=RESOLVER, on_chunk=reader.feed,
        )
    except ToolHttpError as exc:
        _stop(conn, reader.task_id, user)  # a timeout or a response over the size limit: the Dify run must not carry on
        raise ToolError(exc.code, exc.message) from exc
    except Exception:  # the run was cancelled or timed out while reading
        _stop(conn, reader.task_id, user)
        raise
    if not 200 <= result.status < 300:
        raise _dify_error(result.status, result.body)
    reader.finish()
    if reader.error is not None:
        raise reader.error
    if conn.kind == "chatflow":
        if not reader.ended:  # also when part of the answer arrived: a cut-off reply is not a reply
            raise ToolError("tool.stream_interrupted", "Dify 的流式回答中断了")
        return DifyReply("".join(reader.answer), reader.conversation_id)
    if not reader.ended:
        raise ToolError("tool.stream_interrupted", "Dify 的流式回答中断了")
    if reader.workflow_status not in (None, "succeeded"):
        raise ToolError("tool.workflow_failed", f"Dify 工作流未成功：{str(reader.workflow_error or reader.workflow_status)[:200]}")
    return DifyReply(_output_text(conn, reader.outputs))


# ---------------------------------------------------------------------------
# Testing (演进 PRD 8.4): one real call; the result, the time and the error category are kept.
# ---------------------------------------------------------------------------


def run_test(db: Session, conn: FlowConnection) -> tuple[bool, str | None, str | None, int | None]:
    """(ok, error message, error category, duration ms): one real call to the Dify application. A leftover ``native``
    connection (the canvas was retired, T-169) fails with its reason."""
    started = time.monotonic()
    conn.last_test_at = datetime.now(UTC)
    error: str | None = None
    category: str | None = None
    if conn.source_type != "dify":
        error, category = STATE_TEXT["native_retired"], "invalid"
    else:
        try:
            call_dify(conn, None, {"query": "连接测试", "inputs": {}}, user=user_hash(conn.tenant_id, None))
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
    """The argument schema of the tool: the application's own input variables (read from Dify), with the administrator's
    ``input_mapping`` overriding them (its argument names replace the variable names)."""
    mapping = conn.input_mapping or {}
    properties: dict[str, Any] = {}
    required: list[str] = []
    if conn.kind == "chatflow":
        properties["query"] = {"type": "string", "description": "发给对话流程的问题"}
        required.append("query")
    if mapping:
        for argument in dict.fromkeys(mapping.values()):
            properties[argument] = {"type": "string"}
    else:
        for field_ in conn.parameters or []:
            if conn.kind == "chatflow" and field_["variable"] in RESERVED:
                continue  # the question itself: a variable of that name cannot be told apart from it
            prop: dict[str, Any] = {"type": field_["type"], "description": field_["label"]}
            if field_.get("options"):
                prop["enum"] = field_["options"]
            if field_.get("max_length"):
                prop["maxLength"] = field_["max_length"]
            properties[field_["variable"]] = prop
            if field_.get("required"):
                required.append(field_["variable"])
    if conn.kind == "workflow" and not mapping and not conn.parameters:
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
        conversation_id = _conversation_of(db, ctx)
        bound = binding_for(db, conn, conversation_id) if conn.kind == "chatflow" and conversation_id else None
        reply = call_dify(
            conn, ctx, args, dify_conversation_id=bound.dify_conversation_id if bound else None,
            user=user_hash(ctx.tenant_id, ctx.end_user), sink=ctx.stream if conn.kind == "chatflow" else None,
        )
        if conn.kind == "chatflow" and conversation_id and reply.dify_conversation_id:
            bind(db, conn, conversation_id, reply.dify_conversation_id)
        text = reply.text
        return ToolOutput(content=text, response_bytes=len(text.encode("utf-8")), request_bytes=len(json.dumps(args, ensure_ascii=False)))


# ---------------------------------------------------------------------------
# Conversation binding and stickiness (T-163): a multi-turn Chatflow keeps its Dify conversation, and while it waits for the
# user's next message that message goes straight back to it.
# ---------------------------------------------------------------------------


def _conversation_of(db: Session, ctx: ToolContext) -> UUID | None:
    if ctx.run_id is None:
        return None
    run = db.get(Run, ctx.run_id)
    return run.conversation_id if run is not None else None


def _fresh(conn: FlowConnection, row: DifyConversation) -> bool:
    return row.status == "active" and datetime.now(UTC) - _aware(row.last_active_at) <= timedelta(minutes=max(1, conn.idle_ttl_minutes))


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def binding_for(db: Session, conn: FlowConnection, conversation_id: UUID) -> DifyConversation | None:
    row = db.scalar(select(DifyConversation).where(DifyConversation.connection_id == conn.id, DifyConversation.conversation_id == conversation_id))
    return row if row is not None and _fresh(conn, row) else None


def bind(db: Session, conn: FlowConnection, conversation_id: UUID, dify_conversation_id: str) -> None:
    row = db.scalar(select(DifyConversation).where(DifyConversation.connection_id == conn.id, DifyConversation.conversation_id == conversation_id))
    if row is None:
        db.add(DifyConversation(
            tenant_id=conn.tenant_id, connection_id=conn.id, conversation_id=conversation_id, dify_conversation_id=dify_conversation_id,
            last_active_at=datetime.now(UTC), status="active",
        ))
    else:
        row.dify_conversation_id, row.last_active_at, row.status = dify_conversation_id, datetime.now(UTC), "active"
    db.flush()


def is_exit(conn: FlowConnection, text: str) -> bool:
    cleaned = re.sub(r"[\s。！!，,.？?]+", "", text or "")
    return bool(cleaned) and cleaned in {re.sub(r"[\s。！!，,.？?]+", "", str(w)) for w in conn.exit_words or []}


def sticky_connection(db: Session, tenant_id: UUID, connection_id: UUID, conversation_id: UUID, text: str) -> FlowConnection | None:
    """The Chatflow the conversation's next message must go back to, or ``None``: the connection is sticky, still usable, the binding is
    active and not idle for too long, and the message is not an exit word (which closes the binding)."""
    conn = db.scalar(select(FlowConnection).where(FlowConnection.id == connection_id, FlowConnection.tenant_id == tenant_id, FlowConnection.deleted_at.is_(None)))
    if conn is None or conn.kind != "chatflow" or not conn.sticky or state_of(db, conn)[0] != "available":
        return None
    row = db.scalar(select(DifyConversation).where(DifyConversation.connection_id == conn.id, DifyConversation.conversation_id == conversation_id))
    if row is None or not _fresh(conn, row):
        return None
    if is_exit(conn, text):
        row.status = "closed"
        db.flush()
        return None
    return conn


MOUNT_RESOLVERS["chatflow"] = _resolver("chatflow")
MOUNT_RESOLVERS["flow"] = _resolver("workflow")
CANDIDATE_LISTERS["chatflow"] = _lister("chatflow")
CANDIDATE_LISTERS["flow"] = _lister("workflow")
PROVIDERS["connection"] = FlowConnectionProvider()
