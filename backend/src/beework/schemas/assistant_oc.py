from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from beework.runtime.oc import OCConfig
from beework.schemas.versions import ApprovalInfo


class OCAssistant(BaseModel):
    id: UUID
    name: str
    icon: str | None
    color: str | None
    status: str
    draft_version: str
    live_version: str | None
    can_edit: bool


class OCStageSummary(BaseModel):
    stage: str
    index: int
    group: str
    available: bool  # false → the stage task has not landed yet (“即将开放”)
    summary: str | None


class OCStateResponse(BaseModel):
    assistant: OCAssistant
    oc: OCConfig
    live_oc: OCConfig | None  # None while the assistant has never been published
    row_version: int
    updated_at: datetime
    has_changes: bool  # the draft OC differs from the live OC
    diff: list[dict[str, Any]]
    stages: list[OCStageSummary]
    pending_approval_id: UUID | None
    approval: ApprovalInfo


class OCSaveRequest(BaseModel):
    oc: OCConfig
    row_version: int = Field(ge=1)


class OCSubmitRequest(BaseModel):
    change_note: str = Field(default="", max_length=500)
    replace_pending: bool = False


class OCAssistantOption(BaseModel):
    id: UUID
    name: str
    icon: str | None
    color: str | None
    status: str
    version: str | None  # live version, if any


class OCAssistantOptions(BaseModel):
    items: list[OCAssistantOption]
