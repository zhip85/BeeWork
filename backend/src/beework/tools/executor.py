"""The controlled tool executor (T-071, CAP-042, CAP-054).

Every tool call of a run — an MCP tool, an API plugin operation, a Workflow offered as a tool, a flow connection — goes
through :func:`invoke_tool`. The order of checks is fixed:

1. the tool exists and its capability belongs to the tenant and is available (a provider's ``lookup``);
2. the tool is among those the current agent mounted and ticked (``ctx.allowed``);
3. the call depth is within the limit (RUL-014);
4. the arguments satisfy the tool's JSON schema;
5. a write needs the end user's confirmation — without it :class:`ToolConfirmationRequired` is raised so the graph can
   interrupt and ask, and a call that is resumed with ``confirmed=True`` carries a stable idempotency key;
6. the provider executes, using ``ctx.http`` (the safe client); credentials are decrypted by the provider and
   registered with ``ctx.register_secret`` so they are scrubbed from everything that leaves the executor;
7. the result passes the output guard (``ctx.guard_output``, the pipeline's ``pii_output`` hook) before the model
   sees it.

Only metadata is recorded (``tool_invocations``); arguments, results and credentials are never stored.
"""

import logging
import time
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from beework.models.tools import ToolInvocation
from beework.tools.http import ToolHttpError
from beework.tools.schema import validate

log = logging.getLogger(__name__)

MAX_CALL_DEPTH = 5  # RUL-014
MAX_RESULT_CHARS = 20_000  # what is handed back to the model
SOURCES = ("mcp", "api", "workflow", "skill", "connection", "agent")


@dataclass(frozen=True)
class ToolSpec:
    """A tool as the executor sees it. ``name`` is ``"{source}:{identifier}"`` and is what the model calls."""

    name: str
    source: str
    capability_id: UUID | None
    parameters: dict[str, Any] = field(default_factory=dict)  # JSON schema of the arguments
    writes: bool = False
    description: str = ""


@dataclass
class ToolOutput:
    """What a provider returns: text for the model plus the number of response bytes (for the record)."""

    content: str
    response_bytes: int = 0
    request_bytes: int = 0


class ToolProvider(Protocol):
    def lookup(self, db: Session, ctx: "ToolContext", name: str) -> ToolSpec | None:
        """The tool when it exists, its capability belongs to ``ctx.tenant_id`` and it is available; else ``None``."""

    def execute(self, db: Session, ctx: "ToolContext", spec: ToolSpec, args: dict[str, Any]) -> ToolOutput:
        """Run the tool. Raise :class:`ToolError` (or ``ToolHttpError``) for failures that are safe to show."""


# Providers register themselves by source (MCP T-072, API plugins T-073, skills T-074, flow connections T-075).
PROVIDERS: dict[str, ToolProvider] = {}


class ToolError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ToolConfirmationRequired(Exception):
    """A write needs the end user's confirmation first. The graph turns this into an ``interrupt`` carrying
    ``payload``; on resume it calls :func:`invoke_tool` again with ``confirmed`` and the same ``call_index``."""

    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__("tool.confirmation_required")
        self.payload = payload


@dataclass
class ToolContext:
    tenant_id: UUID
    # Names of the tools the agent mounted and ticked (演进 PRD 7.6, 15.3); anything else is refused.
    allowed: frozenset[str]
    run_id: UUID | None = None
    assistant_id: UUID | None = None
    end_user: str | None = None
    depth: int = 1
    environment: str = "production"  # which model environment a tool that calls a model (a leaf agent) uses
    check_stop: Callable[[], None] = lambda: None  # raises when the run was cancelled or timed out
    # A test call from the capability centre may use a plugin that is still a draft (T-073); a disabled one never.
    allow_unavailable: bool = False
    # The pipeline's pii_output hook (CAP-054): text -> text.
    guard_output: Callable[[str], str] = lambda text: text
    span: Callable[[str], AbstractContextManager] = lambda name: nullcontext()
    # Where a streaming capability (a Dify Chatflow whose answer is the reply) pushes tokens as they arrive; None when the caller wants the whole result.
    stream: Callable[[str], None] | None = None
    secrets: set[str] = field(default_factory=set)
    _calls: int = 0

    def register_secret(self, value: str | None) -> None:
        if value and len(value) >= 4:
            self.secrets.add(value)

    def next_index(self) -> int:
        self._calls += 1
        return self._calls

    def scrub(self, text: str) -> str:
        for secret in sorted(self.secrets, key=len, reverse=True):
            text = text.replace(secret, "***")
        return text


@dataclass
class ToolResult:
    status: str  # ok | error | cancelled
    content: str = ""
    code: str | None = None
    call_index: int = 0
    deduplicated: bool = False


def split_name(name: str) -> tuple[str, str]:
    source, _, rest = name.partition(":")
    return source, rest


def idempotency_key(ctx: ToolContext, call_index: int) -> str | None:
    return f"{ctx.run_id}:{call_index}" if ctx.run_id is not None else None


def _record(
    db: Session, ctx: ToolContext, name: str, source: str, spec: ToolSpec | None, status: str, *,
    call_index: int, started: float, code: str | None = None, request_bytes: int = 0, response_bytes: int = 0,
    key: str | None = None,
) -> None:
    db.add(
        ToolInvocation(
            tenant_id=ctx.tenant_id, run_id=ctx.run_id, assistant_id=ctx.assistant_id, call_index=call_index,
            tool_name=name[:200], source=source[:16], capability_id=spec.capability_id if spec else None,
            is_write=bool(spec and spec.writes), status=status, error_code=code,
            duration_ms=int((time.monotonic() - started) * 1000), request_bytes=request_bytes,
            response_bytes=response_bytes, idempotency_key=key,
        )
    )
    db.commit()


def _deny(db: Session, ctx: ToolContext, name: str, source: str, spec: ToolSpec | None, code: str, message: str,
          call_index: int, started: float) -> ToolResult:
    _record(db, ctx, name, source, spec, "denied", call_index=call_index, started=started, code=code)
    return ToolResult(status="error", content=message, code=code, call_index=call_index)


def invoke_tool(
    db: Session, ctx: ToolContext, name: str, args: dict[str, Any] | None = None, *,
    confirmed: bool | None = None, call_index: int | None = None,
) -> ToolResult:
    """Run one tool call. Refusals and failures come back as ``ToolResult(status="error")`` so the model can react;
    only a write that still needs confirmation raises :class:`ToolConfirmationRequired`."""
    started = time.monotonic()
    args = args or {}
    source, _ = split_name(name)
    index = call_index if call_index is not None else ctx.next_index()
    provider = PROVIDERS.get(source)
    if provider is None:
        return _deny(db, ctx, name, source or "?", None, "tool.not_found", "工具不存在", index, started)
    spec = provider.lookup(db, ctx, name)
    if spec is None:
        return _deny(db, ctx, name, source, None, "tool.not_found", "工具不存在或不可用", index, started)
    if name not in ctx.allowed:
        return _deny(db, ctx, name, source, spec, "tool.not_mounted", "该智能体没有挂载或没有勾选此工具", index, started)
    if ctx.depth > MAX_CALL_DEPTH:
        return _deny(db, ctx, name, source, spec, "tool.depth_exceeded", "调用嵌套层数超过上限", index, started)
    problems = validate(args, spec.parameters)
    if problems:
        return _deny(db, ctx, name, source, spec, "tool.invalid_arguments", "参数不正确：" + "；".join(problems[:5]), index, started)

    key = None
    if spec.writes:
        key = idempotency_key(ctx, index)
        if confirmed is None:
            raise ToolConfirmationRequired(
                {"tool": name, "description": spec.description, "call_index": index, "idempotency_key": key}
            )
        if not confirmed:
            _record(db, ctx, name, source, spec, "cancelled", call_index=index, started=started)
            return ToolResult(status="cancelled", content="用户取消", code="tool.cancelled", call_index=index)
        if key is not None and db.scalar(
            select(ToolInvocation.id).where(
                ToolInvocation.tenant_id == ctx.tenant_id, ToolInvocation.idempotency_key == key,
                ToolInvocation.status == "ok",
            )
        ):
            return ToolResult(status="ok", content="该操作已执行，未重复执行", call_index=index, deduplicated=True)

    request_bytes = response_bytes = 0
    try:
        with ctx.span(f"tool.{name}"):
            output = provider.execute(db, ctx, spec, args)
        request_bytes, response_bytes = output.request_bytes, output.response_bytes
        content = ctx.guard_output(ctx.scrub(output.content))[:MAX_RESULT_CHARS]
        _record(db, ctx, name, source, spec, "ok", call_index=index, started=started, request_bytes=request_bytes,
                response_bytes=response_bytes, key=key)
        return ToolResult(status="ok", content=content, call_index=index)
    except (ToolError, ToolHttpError) as exc:
        message = ctx.scrub(exc.message)
        _record(db, ctx, name, source, spec, "error", call_index=index, started=started, code=exc.code, key=None)
        return ToolResult(status="error", content=message, code=exc.code, call_index=index)
    except Exception:  # noqa: BLE001 — a provider bug must not take the run down or leak into the model's context
        log.warning("tool %s failed", name)  # no exception text: it may carry request data
        _record(db, ctx, name, source, spec, "error", call_index=index, started=started, code="tool.failed", key=None)
        return ToolResult(status="error", content="工具执行失败", code="tool.failed", call_index=index)
