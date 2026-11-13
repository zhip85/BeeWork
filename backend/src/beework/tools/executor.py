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
from beework.tools.http import CALL_TIMEOUT, ToolHttpError
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
    idempotent: bool = False  # a write that is safe to repeat (T-165): the only kind of write that is ever retried


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
    def __init__(self, code: str, message: str, *, status: int | None = None, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status  # the HTTP status of the upstream answer, when there was one (T-165 decides what is worth retrying)
        self.retry_after = retry_after  # seconds the upstream asked us to wait


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
    # The run's time budget (T-165): the monotonic time after which no attempt may start and no wait may reach; None = unlimited.
    deadline: float | None = None
    sleep: Callable[[float], None] = time.sleep  # a seam for the retry waits
    secrets: set[str] = field(default_factory=set)
    _calls: int = 0

    def remaining(self) -> float | None:
        return None if self.deadline is None else self.deadline - time.monotonic()

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
    key: str | None = None, attempts: int = 1, policy: str | None = None, breaker_state: str | None = None,
    attempt_ms: list[int] | None = None,
) -> None:
    db.add(
        ToolInvocation(
            tenant_id=ctx.tenant_id, run_id=ctx.run_id, assistant_id=ctx.assistant_id, call_index=call_index,
            tool_name=name[:200], source=source[:16], capability_id=spec.capability_id if spec else None,
            is_write=bool(spec and spec.writes), status=status, error_code=code,
            duration_ms=int((time.monotonic() - started) * 1000), request_bytes=request_bytes,
            response_bytes=response_bytes, idempotency_key=key, attempts=attempts, policy=policy, breaker_state=breaker_state,
            attempt_ms=attempt_ms or [],
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

    return _execute(db, ctx, provider, spec, name, source, args, index, started, key)


def _execute(
    db: Session, ctx: ToolContext, provider: Any, spec: ToolSpec, name: str, source: str, args: dict[str, Any], index: int,
    started: float, key: str | None,
) -> ToolResult:
    """The call itself under its policy (T-165): the circuit, the timeout of every attempt, the repeats of a failure that is worth
    repeating, and the record of all of it."""
    from beework.tools import policy as policy_mod  # late: the policy reads the capability registry

    entry = policy_mod.entry_for_tool(db, ctx.tenant_id, name)
    policy = policy_mod.resolve(entry)
    breaker_name = policy_mod.breaker_key(ctx.tenant_id, name)
    cfg = policy_mod.breaker_config()
    allowed, state, wait = policy_mod.BREAKER.admit(breaker_name, cfg)
    if not allowed:
        _record(db, ctx, name, source, spec, "denied", call_index=index, started=started, code="tool.circuit_open", policy=policy.source, breaker_state="open")
        return ToolResult(status="error", content=f"该工具近期连续失败，已暂停调用，约 {wait} 秒后再试", code="tool.circuit_open", call_index=index)
    attempt_ms: list[int] = []
    attempts = 0
    error: ToolError | ToolHttpError | None = None
    try:
        while True:
            ctx.check_stop()  # a cancelled or timed-out run stops here, not after the next attempt
            remaining = ctx.remaining()
            if attempts and remaining is not None and remaining <= 0:
                break  # the budget ran out while waiting: the last failure is the outcome
            timeout = policy.timeout_ms / 1000 if remaining is None else max(0.1, min(policy.timeout_ms / 1000, remaining))
            error = None  # only now: until an attempt really starts, the failure of the previous one stays the outcome
            attempts += 1
            attempt_started = time.monotonic()
            token = CALL_TIMEOUT.set(timeout)
            try:
                with ctx.span(f"tool.{name}"):
                    output = provider.execute(db, ctx, spec, args)
            except (ToolError, ToolHttpError) as exc:
                error = exc
            except Exception:  # noqa: BLE001 — a provider bug must not take the run down or leak into the model's context
                log.warning("tool %s failed", name)  # no exception text: it may carry request data
                attempt_ms.append(int((time.monotonic() - attempt_started) * 1000))
                if state == "half_open":
                    policy_mod.BREAKER.release(breaker_name)  # the probe was not judged; the next call may probe
                _record(db, ctx, name, source, spec, "error", call_index=index, started=started, code="tool.failed", key=None, attempts=attempts, policy=policy.source, breaker_state=state, attempt_ms=attempt_ms)
                return ToolResult(status="error", content="工具执行失败", code="tool.failed", call_index=index)
            finally:
                CALL_TIMEOUT.reset(token)
            attempt_ms.append(int((time.monotonic() - attempt_started) * 1000))
            if error is None:
                policy_mod.BREAKER.success(breaker_name)
                content = ctx.guard_output(ctx.scrub(output.content))[:MAX_RESULT_CHARS]
                _record(db, ctx, name, source, spec, "ok", call_index=index, started=started, request_bytes=output.request_bytes,
                        response_bytes=output.response_bytes, key=key, attempts=attempts, policy=policy.source, breaker_state=state, attempt_ms=attempt_ms)
                return ToolResult(status="ok", content=content, call_index=index)
            category = policy_mod.category_of(error)
            if category is None or state == "half_open":
                break
            if attempts >= policy.max_attempts or category not in policy.retry_on or not policy_mod.retry_allowed(spec, key, error):
                break
            wait_for = policy_mod.delay_seconds(policy, attempts, error)
            remaining = ctx.remaining()
            if remaining is not None and wait_for >= remaining:
                break  # the wait alone would use up the run's budget
            _wait(ctx, wait_for)
    except BaseException:  # the run was cancelled or timed out: a probe in flight is released, not judged
        if state == "half_open":
            policy_mod.BREAKER.release(breaker_name)
        raise
    assert error is not None or attempts > 0
    category = policy_mod.category_of(error) if error is not None else None
    breaker_state = state
    if category is not None:
        if policy_mod.BREAKER.failure(breaker_name, cfg):
            breaker_state = "opened"
            _circuit_opened(db, ctx, spec, entry, name)
    elif state == "half_open":
        policy_mod.BREAKER.success(breaker_name)  # the probe got a definite (non-transient) answer: the tool is back
    code = getattr(error, "code", "tool.failed")
    message = ctx.scrub(getattr(error, "message", "工具执行失败"))
    _record(db, ctx, name, source, spec, "error", call_index=index, started=started, code=code, key=None, attempts=attempts, policy=policy.source, breaker_state=breaker_state, attempt_ms=attempt_ms)
    return ToolResult(status="error", content=message, code=code, call_index=index)


def _wait(ctx: ToolContext, seconds: float) -> None:
    """Sleep in small slices so a cancelled run is noticed within a fraction of a second."""
    end = time.monotonic() + seconds
    while (left := end - time.monotonic()) > 0:
        ctx.check_stop()
        ctx.sleep(min(0.2, left))


def _circuit_opened(db: Session, ctx: ToolContext, spec: ToolSpec, entry: Any, name: str) -> None:
    """The circuit of a tool just opened: tell the capability administrators and flag an MCP connection (the T-072 health mark)."""
    try:
        from beework.notifications import notify

        label = entry.name if entry is not None else name
        notify(
            db, tenant_id=ctx.tenant_id, type="tool.circuit_open", title=f"能力不稳定：{label}",
            body="连续多次调用失败，已暂时停止调用；稍后会自动试探恢复。", link="/capabilities?type=registry", params={"tool": name}, role="tenant_admin",
        )
        if spec.source == "mcp" and spec.capability_id is not None:
            from beework.models.capabilities import McpConnection

            conn = db.get(McpConnection, spec.capability_id)
            if conn is not None:
                conn.health = "error"
        db.commit()
    except Exception:  # noqa: BLE001 — a notification problem must not turn a tool failure into a crash
        log.warning("could not report the open circuit of %s", name, exc_info=False)
        db.rollback()
