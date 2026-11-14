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


class TraceDetailResponse(TraceResponse):
    spans: list[SpanResponse]
    spans_source: Literal["langfuse", "unavailable"]
    notice: str | None
    timeline: list[TimelineEvent]  # BeeWork's own run events: always present, the only detail when Langfuse is down
