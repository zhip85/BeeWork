from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

FaqStatus = Literal["draft", "published", "expired"]


class FaqWriteRequest(BaseModel):
    question: str = Field(max_length=400)
    similar_questions: list[str] = Field(default_factory=list, max_length=30)
    answer: str = Field(max_length=4000)
    # "draft" (default) or "published"; publishing needs the faq:publish permission.
    status: Literal["draft", "published"] = "draft"
    expires_at: date | None = None
    row_version: int | None = None


class FaqResponse(BaseModel):
    id: UUID
    kb_id: UUID
    question: str
    similar_questions: list[str]
    answer: str
    status: FaqStatus
    expires_at: date | None
    hits_30d: int
    sync_status: str
    sync_error: str | None
    published_at: datetime | None
    created_by: str | None
    row_version: int
    created_at: datetime
    updated_at: datetime


class FaqListResponse(BaseModel):
    items: list[FaqResponse]
    total: int
    limit: int
    offset: int
    counts: dict[str, int]  # draft / published / expired / all


class ImportRowError(BaseModel):
    line: int
    message: str


class ImportPreviewRow(BaseModel):
    line: int
    question: str
    similar_questions: list[str]
    answer: str


class FaqImportResponse(BaseModel):
    dry_run: bool
    recognized: int
    created: int
    errors: list[ImportRowError]
    preview: list[ImportPreviewRow]  # the first few recognised rows
