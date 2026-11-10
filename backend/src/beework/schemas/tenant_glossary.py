from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from beework.schemas.glossary import ImportPreviewRow, ImportRowError
from beework.schemas.versions import ApprovalInfo


class TenantTermWrite(BaseModel):
    term: str = Field(max_length=400)
    aliases: list[str] = Field(default_factory=list, max_length=100)
    definition: str = Field(default="", max_length=2000)


class TenantGlossarySettingsWrite(BaseModel):
    glossary_ids: list[UUID] = Field(default_factory=list, max_length=50)  # in inheritance order
    row_version: int = Field(ge=0)


class GlossaryTag(BaseModel):
    id: UUID
    name: str
    industry_name: str
    term_count: int
    selected: bool
    position: int | None  # 1-based order among the selected ones


class TenantTermResponse(BaseModel):
    key: UUID  # what to edit / restore / delete
    term: str
    aliases: list[str]
    definition: str
    label: Literal["industry_inherited", "tenant", "tenant_modified"]
    restorable: bool
    pending_approval_id: UUID | None = None


class TenantGlossaryChain(BaseModel):
    industries: list[str]
    industry_terms: int
    inherited: int
    tenant_level: int
    total: int
    hidden: int


class TenantGlossaryState(BaseModel):
    tenant_name: str
    chain: TenantGlossaryChain
    glossaries: list[GlossaryTag]
    settings_version: int
    items: list[TenantTermResponse]
    approval: ApprovalInfo


class TenantGlossaryMutation(BaseModel):
    outcome: Literal["applied", "pending"]
    kind: str
    approval_id: UUID | None = None


class TenantImportResponse(BaseModel):
    dry_run: bool
    recognized: int
    duplicates: int  # recognised terms the tenant already has (inherited or its own)
    errors: list[ImportRowError]
    preview: list[ImportPreviewRow]
    outcome: Literal["applied", "pending", "preview"]
    approval_id: UUID | None = None
