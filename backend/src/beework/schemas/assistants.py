from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

CATEGORIES = ("customer_service", "sales", "internal", "insurance")
LANGUAGES = ("zh-CN", "en")
MOUNT_KINDS = ("kb", "agent", "mcp", "skill", "chatflow", "flow", "api")

Category = Literal["customer_service", "sales", "internal", "insurance"]
Visibility = Literal["own_org", "specified", "all"]
AssistantStatus = Literal["draft", "published", "disabled"]


class AssistantMounts(BaseModel):
    kb: list[UUID] = Field(default_factory=list)
    agent: list[UUID] = Field(default_factory=list)
    mcp: list[UUID] = Field(default_factory=list)
    skill: list[UUID] = Field(default_factory=list)
    chatflow: list[UUID] = Field(default_factory=list)
    flow: list[UUID] = Field(default_factory=list)
    api: list[UUID] = Field(default_factory=list)


class AssistantDraftConfig(BaseModel):
    """Editable assistant configuration stored in ``assistants.draft_config`` (read by the T-025 compiler)."""

    dsl_version: Literal[1] = 1
    prompt: str = Field(default="", max_length=8000)
    model_id: UUID | None = None
    driven_by: UUID | None = None
    default_language: Literal["zh-CN", "en"] = "zh-CN"
    greeting: str = Field(default="", max_length=500)
    suggested_questions: bool = True
    feedback_enabled: bool = True
    mounts: AssistantMounts = Field(default_factory=AssistantMounts)
    # Placeholder for the OC configuration owned by T-054.
    oc: dict = Field(default_factory=dict)


class AssistantWriteRequest(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    description: str = Field(default="", max_length=500)
    organization_id: UUID | None = None
    category: Category = "customer_service"
    icon: str | None = Field(default=None, max_length=40)
    color: str | None = Field(default=None, max_length=16)
    visibility: Visibility = "own_org"
    visible_org_ids: list[UUID] = Field(default_factory=list, max_length=200)
    config: AssistantDraftConfig = Field(default_factory=AssistantDraftConfig)
    # Wizard "save draft": required-field checks are relaxed (AST-024).
    draft: bool = False
    row_version: int | None = None

    @model_validator(mode="after")
    def check_visibility(self) -> "AssistantWriteRequest":
        if self.visibility == "specified" and not self.visible_org_ids:
            raise ValueError("请选择可见的组织")
        if self.visibility != "specified" and self.visible_org_ids:
            raise ValueError("只有“指定组织”可见范围可以选择组织")
        return self


class AssistantResponse(BaseModel):
    id: UUID
    name: str
    description: str
    organization_id: UUID
    organization_name: str
    category: str
    icon: str | None
    color: str | None
    status: AssistantStatus
    visibility: str
    visible_org_ids: list[UUID]
    config: AssistantDraftConfig
    draft_version: str
    live_version_id: UUID | None
    owner_id: UUID | None
    created_by: str | None
    updated_by: str | None
    row_version: int
    created_at: datetime
    updated_at: datetime
    # The caller may edit / delete it (organization or assistant scope can see more than it owns).
    can_edit: bool


class AssistantQuotaResponse(BaseModel):
    used: int
    limit: int | None
    policy: str


class AssistantSummaryResponse(BaseModel):
    total: int
    published: int
    draft: int
    disabled: int
    monthly_conversations: int | None  # filled in once conversations exist (T-026)
    quota: AssistantQuotaResponse


class AssistantModelOption(BaseModel):
    id: UUID
    display_name: str
    provider: str
    is_default: bool


class AssistantModelOptionsResponse(BaseModel):
    environment: str
    models: list[AssistantModelOption]
    default_model_id: UUID | None
