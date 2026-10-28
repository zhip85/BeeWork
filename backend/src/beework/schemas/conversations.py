from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class MessageResponse(BaseModel):
    id: UUID
    seq: int
    role: str
    content: str
    feedback: str | None
    trace_id: str | None
    run_id: UUID | None
    created_at: datetime


class ConversationResponse(BaseModel):
    id: UUID
    assistant_id: UUID
    assistant_version: str | None
    source: str
    channel: str  # channel type of the end user ("console" for test conversations)
    environment: str
    end_user_id: UUID | None
    end_user_name: str | None
    turns: int
    csat: Decimal | None
    tokens: int
    status: Literal["in_progress", "handoff", "resolved", "abandoned"]
    started_at: datetime
    last_active_at: datetime
    ended_at: datetime | None


class ConversationDetailResponse(ConversationResponse):
    messages: list[MessageResponse]


class CompliancePolicyResponse(BaseModel):
    retention_days: int
    redact_pii: bool
    allow_export: bool
    row_version: int
    retention_choices: list[int]
    max_retention_days: int | None


class CompliancePolicyWriteRequest(BaseModel):
    retention_days: int = Field(ge=1, le=3650)
    redact_pii: bool
    allow_export: bool
    # 0 when no policy has been saved yet.
    row_version: int = Field(ge=0)
