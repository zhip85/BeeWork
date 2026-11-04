from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

Event = Literal["publish", "channel", "byok", "kb", "skill", "rule", "export"]
ApproverRole = Literal["tenant_admin", "security_officer", "knowledge_owner", "assistant_owner"]


class PolicyItem(BaseModel):
    event: Event
    request_type: str
    approver_role: ApproverRole
    levels: Literal[1, 2]
    enabled: bool
    row_version: int


class PolicyListResponse(BaseModel):
    items: list[PolicyItem]
    pending_count: int
    multi_level_allowed: bool


class PolicyWriteItem(BaseModel):
    event: Event
    approver_role: ApproverRole
    levels: Literal[1, 2]
    enabled: bool
    row_version: int | None = None


class PolicyWriteRequest(BaseModel):
    items: list[PolicyWriteItem] = Field(min_length=1, max_length=20)


class ApprovalItem(BaseModel):
    id: UUID
    title: str
    type: str
    policy_event: str
    object_type: str
    object_id: UUID
    organization_id: UUID | None
    organization_name: str
    requester_id: UUID | None
    requester_name: str
    status: str
    apply_status: str
    current_level: int
    level_count: int
    approver_names: list[str]  # people who can decide the current level
    checks: list[Any]
    submitted_at: datetime
    decided_at: datetime | None
    can_decide: bool
    can_withdraw: bool
    can_retry: bool


class LevelView(BaseModel):
    level: int
    approver_role: str
    approvers: list[str]
    status: str
    decided_by: str | None
    comment: str
    decided_at: datetime | None


class CommentView(BaseModel):
    id: UUID
    author_name: str
    body: str
    created_at: datetime


class ApprovalDetail(ApprovalItem):
    description: str
    change_diff: list[Any]
    levels: list[LevelView]
    comments: list[CommentView]
    apply_error: str | None


class ApprovalSummary(BaseModel):
    pending_mine: int
    submitted: int
    visible_count: int  # requests this member may see; > 0 shows the entry even without approvals:view
    completed_this_month: int
    avg_duration_seconds: int | None


class DecisionRequest(BaseModel):
    comment: str = Field(default="", max_length=1000)


class CommentRequest(BaseModel):
    body: str = Field(min_length=1, max_length=2000)
