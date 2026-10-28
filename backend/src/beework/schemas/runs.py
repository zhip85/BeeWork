from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class CreateRunRequest(BaseModel):
    message: str = Field(min_length=1, max_length=20000)


class CreateRunResponse(BaseModel):
    run_id: UUID
    status: Literal["queued"] = "queued"


class RunResponse(BaseModel):
    id: UUID
    assistant_id: UUID
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
    tokens_in: int | None
    tokens_out: int | None
    trace_url: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
