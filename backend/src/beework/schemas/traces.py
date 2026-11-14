from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel


class TraceResponse(BaseModel):
    """One row of the call-trace list: a run (OPS-040)."""

    run_id: UUID
    trace_id: str
    assistant_id: UUID
    assistant_name: str
    conversation_id: UUID | None
    origin: Literal["online", "debug", "trial", "eval"]
    source: str
    channel: str | None
    environment: str
    status: Literal["success", "error", "slow", "running"]
    latency_ms: int | None
    tokens_in: int | None
    tokens_out: int | None
    error_code: str | None
    trace_available: bool  # False once retention removed it from the tracing service
    created_at: datetime
    # T-168: what the router decided (``runs.output.route``); all None for runs that never reached it
    path: Literal["A", "B"] | None = None
    route_target: str | None = None
    handoff: str | None = None  # why path A turned into B
    capability_error: str | None = None  # the capability that was tried failed with this code
    confidence: float | None = None


class SpanResponse(BaseModel):
    id: str
    parent_id: str | None
    name: str
    kind: str  # span | generation | event
    start_ms: int  # offset from the start of the trace
    duration_ms: int | None
    status: Literal["ok", "error"]
    message: str | None
    model: str | None
    tokens_in: int | None
    tokens_out: int | None
    cost: float | None
    input: str | None
    output: str | None


class TimelineEvent(BaseModel):
    seq: int
    type: str
    offset_ms: int



class ToolCallResponse(BaseModel):
    """One call of a capability during the run: metadata only, never arguments or results (T-071, T-165)."""

    call_index: int
    tool_name: str
    source: str
    status: str
    error_code: str | None
    attempts: int
    breaker_state: str | None
    duration_ms: int
    attempt_ms: list[int]
    is_write: bool


class RouteCount(BaseModel):
    key: str
    count: int


class PathCost(BaseModel):
    """Spend of the runs that took a path (or reached one capability): model tokens and tool calls."""

    key: str
    runs: int
    tokens_in: int
    tokens_out: int
    tool_calls: int
    avg_latency_ms: int | None


class RouteSummary(BaseModel):
    days: int
    runs: int  # runs that went through the router
    path_a: int
    path_b: int
    avg_confidence: float | None
    handoff_reasons: list[RouteCount]
    failed_capabilities: list[RouteCount]
    by_path: list[PathCost]
    by_capability: list[PathCost]
    truncated: bool


class TraceDetailResponse(TraceResponse):
    spans: list[SpanResponse]
    spans_source: Literal["langfuse", "unavailable"]
    notice: str | None
    timeline: list[TimelineEvent]  # BeeWork's own run events: always present, the only detail when Langfuse is down
    route: dict | None = None  # the router's record, without user text or argument values
    tool_calls: list[ToolCallResponse] = []
