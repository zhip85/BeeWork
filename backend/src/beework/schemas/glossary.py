from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class IndustryWrite(BaseModel):
    code: str | None = Field(default=None, pattern="^[a-z][a-z0-9_]{1,30}$")  # required on create, fixed afterwards
    name_zh: str = Field(min_length=1, max_length=60)
    name_en: str = Field(min_length=1, max_length=80)
    sort_order: int = Field(default=0, ge=0, le=10000)
    status: str = Field(default="active", pattern="^(active|disabled)$")


class IndustryResponse(BaseModel):
    code: str
    name_zh: str
    name_en: str
    sort_order: int
    status: str
    tenant_count: int
    glossary_count: int


class GlossaryWrite(BaseModel):
    name: str = Field(max_length=200)
    description: str = Field(default="", max_length=600)
    industry_code: str = Field(max_length=40)
    row_version: int | None = None


class GlossaryResponse(BaseModel):
    id: UUID
    name: str
    description: str
    industry_code: str
    industry_name: str
    term_count: int
    tenant_count: int  # tenants of this industry, i.e. those that can inherit it
    row_version: int
    updated_at: datetime


class TermWrite(BaseModel):
    term: str = Field(max_length=400)
    aliases: list[str] = Field(default_factory=list, max_length=100)
    definition: str = Field(default="", max_length=2000)
    row_version: int | None = None


class TermResponse(BaseModel):
    id: UUID
    glossary_id: UUID
    term: str
    aliases: list[str]
    definition: str
    row_version: int
    updated_by: str | None
    updated_at: datetime


class TermListResponse(BaseModel):
    items: list[TermResponse]
    total: int
    limit: int
    offset: int


class ImportRowError(BaseModel):
    line: int
    message: str


class ImportPreviewRow(BaseModel):
    line: int
    term: str
    aliases: list[str]
    definition: str
    exists: bool


class TermImportResponse(BaseModel):
    dry_run: bool
    recognized: int
    duplicates: int  # recognised terms that already exist in the glossary
    created: int
    updated: int
    skipped: int
    errors: list[ImportRowError]
    preview: list[ImportPreviewRow]
