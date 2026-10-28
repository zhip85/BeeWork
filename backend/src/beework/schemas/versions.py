from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field


class PublishRequest(BaseModel):
    change_note: str = Field(default="", max_length=500)


class RestoreRequest(BaseModel):
    row_version: int = Field(ge=1)


class RollbackRequest(BaseModel):
    change_note: str = Field(default="", max_length=500)


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


class PublishPreview(BaseModel):
    next_version: str
    blockers: list[PublishBlocker]
    has_changes: bool
    diff: list[dict[str, Any]]
    can_publish: bool


class CompareResponse(BaseModel):
    from_ref: str
    to_ref: str
    changes: list[dict[str, Any]]
