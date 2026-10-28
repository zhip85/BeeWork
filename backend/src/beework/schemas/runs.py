from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class CreateRunRequest(BaseModel):
    message: str = Field(min_length=1, max_length=20000)
    # Continue an existing console conversation; omitted: a new conversation is started.
    conversation_id: UUID | None = None
    # "draft" runs the editable draft; "live" runs the published version's frozen snapshot.
    target: Literal["draft", "live"] = "draft"


class CreateRunResponse(BaseModel):
    run_id: UUID
    conversation_id: UUID
    status: Literal["queued"] = "queued"


class RunResponse(BaseModel):
    id: UUID
    assistant_id: UUID
    conversation_id: UUID | None
    source: str
    environment: str
    status: str
    input: dict
    output: dict | None
    error_code: str | None
    error: str | None
    retrieval_mode: str | None
    draft_row_version: int | None
    latency_ms: int | None
    pipeline_ms: int | None
    stage_ms: dict | None
    error_stage: str | None
    tokens_in: int | None
    tokens_out: int | None
    trace_url: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
