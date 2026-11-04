from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from beework.schemas.assistant_checks import CheckIssue


class PublishRequest(BaseModel):
    change_note: str = Field(default="", max_length=500)
    # APR-002: withdraw the assistant's pending request and submit this one instead.
    replace_pending: bool = False


class RestoreRequest(BaseModel):
    row_version: int = Field(ge=1)


class RollbackRequest(BaseModel):
    change_note: str = Field(default="", max_length=500)
    replace_pending: bool = False


class VersionSummary(BaseModel):
    id: UUID
    version: str
    version_seq: int
    kind: Literal["publish", "rollback"]
    source_version_id: UUID | None
    change_note: str
    published_by: str | None
    published_at: datetime
    approval_id: UUID | None
    is_live: bool


class VersionDetail(VersionSummary):
    snapshot: dict[str, Any]
    change_diff: list[dict[str, Any]]


class VersionListResponse(BaseModel):
    items: list[VersionSummary]
    live_version_id: UUID | None
    draft_version: str


class PublishBlocker(BaseModel):
    code: str
    message: str


class ApprovalInfo(BaseModel):
    required: bool
    levels: int
    approver_role: str
    pending_approval_id: UUID | None


class PendingApproval(BaseModel):
    """Returned with 202 when the publish policy requires approval: nothing is live until it is approved."""

    pending_approval: bool = True
    approval_id: UUID
    next_version: str


class PublishPreview(BaseModel):
    next_version: str
    blockers: list[PublishBlocker]
    # High-impact and informational findings from the configuration check; they do not block publishing.
    warnings: list[CheckIssue] = []
    has_changes: bool
    diff: list[dict[str, Any]]
    can_publish: bool
    approval: ApprovalInfo


class CompareResponse(BaseModel):
    from_ref: str
    to_ref: str
    changes: list[dict[str, Any]]
