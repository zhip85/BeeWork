from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

SourceType = Literal["agent", "chatflow_connection", "workflow_connection"]
Audience = Literal["all", "orgs", "admins"]
Mode = Literal["chat", "form"]
AppStatus = Literal["draft", "listed", "delisted"]


class PortalAppWriteRequest(BaseModel):
    source_type: SourceType | None = None  # required on create; on update it may only repeat the current value
    source_id: UUID | None = None
    name: str = Field(max_length=200)
    description: str = Field(default="", max_length=1000)
    category: str = Field(default="", max_length=100)
    audience: Audience = "all"
    organization_ids: list[UUID] = Field(default_factory=list, max_length=200)
    mode: Mode = "chat"
    icon: str = Field(default="sparkles", max_length=40)
    featured: bool = False
    row_version: int | None = None  # required on update


class DelistRequest(BaseModel):
    reason: str = Field(default="", max_length=200)
    row_version: int | None = None


class ListRequest(BaseModel):
    row_version: int | None = None


class PortalAppResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    source_type: SourceType
    source_id: UUID | None
    source_name: str | None = None
    source_state: Literal["available", "unavailable", "unset"] = "unset"  # 来源未设置 / 来源不可用
    name: str
    description: str
    category: str
    audience: Audience
    organization_ids: list[UUID] = Field(default_factory=list)
    organization_names: list[str] = Field(default_factory=list)
    mode: Mode
    icon: str
    featured: bool
    status: AppStatus
    listed_at: datetime | None = None
    delisted_reason: str | None = None
    updated_at: datetime
    updated_by: str | None = None
    row_version: int
    can_edit: bool = False
    can_publish: bool = False


class SourceEntry(BaseModel):
    source_type: SourceType
    source_id: UUID
    name: str
    description: str = ""
    modes: list[Mode] = Field(default_factory=lambda: ["chat"])
    in_use: bool = False  # another live (non-deleted) app already points at it


class SourcesResponse(BaseModel):
    items: list[SourceEntry]


class PortalSummary(BaseModel):
    listed: int
    draft: int
    delisted: int
    # Filled by the usage analytics (T-147); null until then so the page never shows invented numbers.
    monthly_active_employees: int | None = None
    monthly_calls: int | None = None


class ListCheckIssue(BaseModel):
    code: str
    message: str


AnswerStyle = Literal["concise", "balanced", "detailed"]
DelistedHistory = Literal["readonly", "hidden"]


class PortalSettingsBody(BaseModel):
    portal_name: str = Field(default="", max_length=50)
    input_placeholder: str = Field(default="", max_length=100)
    default_audience: Audience = "all"
    answer_style: AnswerStyle = "balanced"
    nav_search: bool = True
    nav_projects: bool = True
    nav_app_library: bool = True
    nav_recent: bool = True
    nav_pinned: bool = True
    file_upload: bool = True
    voice_input: bool = False
    profile_edit: bool = True
    personal_memory: bool = True
    task_notifications: bool = True
    delisted_history: DelistedHistory = "readonly"


class PortalSettingsUpdate(PortalSettingsBody):
    row_version: int | None = None  # 0 / absent only while the tenant has never saved settings


class PortalSettingsResponse(PortalSettingsBody):
    row_version: int = 0
    updated_at: datetime | None = None
    updated_by: str | None = None
    can_edit: bool = False


class PortalMeConfig(BaseModel):
    """What the employee side may read: switches and brand, nothing administrative."""

    portal_name: str
    input_placeholder: str
    answer_style: AnswerStyle
    nav_search: bool
    nav_projects: bool
    nav_app_library: bool
    nav_recent: bool
    nav_pinned: bool
    file_upload: bool
    voice_input: bool
    profile_edit: bool
    personal_memory: bool
    task_notifications: bool
    delisted_history: DelistedHistory
    has_console: bool  # whether “返回管理端” is offered


class PortalSeatState(BaseModel):
    """The member's own portal seat (T-151). ``state`` decides whether the portal opens or the no-seat page shows."""

    state: Literal["assigned", "requested", "unassigned"]
    source: Literal["rule", "manual", "auto", "unlimited"] | None = None
    auto_assigned: bool = False  # true only on the response that just handed out the seat
    can_request: bool
    portal_name: str


class PortalSeatRequest(BaseModel):
    reason: str = Field(default="", max_length=200)
