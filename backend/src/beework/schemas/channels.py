from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

from beework.channel_publish import COLOR_PATTERN, MAX_ORIGINS, normalize_origins
from beework.schemas.open_api import ComponentEvent

Environment = Literal["development", "staging", "production"]


class ExperienceOverrides(BaseModel):
    """Channel-level overrides; an empty value inherits from the assistant / platform defaults."""

    primary_color: str = Field(default="", max_length=7)
    assistant_name: str = Field(default="", max_length=50)
    welcome: str = Field(default="", max_length=500)

    @field_validator("primary_color")
    @classmethod
    def color(cls, value: str) -> str:
        if value and not COLOR_PATTERN.match(value):
            raise ValueError("主色应为 #RRGGBB 格式")
        return value.lower()

    @field_validator("assistant_name", "welcome")
    @classmethod
    def strip(cls, value: str) -> str:
        return value.strip()


class ChannelWrite(BaseModel):
    type: str = Field(default="website", min_length=1, max_length=16)
    name: str = Field(min_length=1, max_length=50)
    assistant_id: UUID
    environment: Environment = "production"
    allowed_origins: list[str] = Field(default_factory=list, max_length=MAX_ORIGINS)
    experience: ExperienceOverrides = Field(default_factory=ExperienceOverrides)
    # Connection parameters of the type (see ``GET /channel-types``). Secrets are write-only: omitted or blank keeps the stored value.
    component_ids: list[UUID] = Field(default_factory=list, max_length=30)  # tenant components shown on this channel (T-043)
    config: dict[str, str] = Field(default_factory=dict, max_length=20)
    secrets: dict[str, str] = Field(default_factory=dict, max_length=20)
    row_version: int | None = None

    @field_validator("config", "secrets")
    @classmethod
    def short_values(cls, value: dict[str, str]) -> dict[str, str]:
        if any(len(k) > 40 or len(v) > 500 for k, v in value.items()):
            raise ValueError("连接参数过长")
        return {k: v.strip() for k, v in value.items()}

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("渠道名称不能为空")
        return value

    @field_validator("allowed_origins")
    @classmethod
    def origins(cls, value: list[str]) -> list[str]:
        return normalize_origins(value)


class PublishChannelRequest(BaseModel):
    replace_pending: bool = False


class ChannelResponse(BaseModel):
    id: UUID
    type: str
    name: str
    assistant_id: UUID
    assistant_name: str | None  # None: the assistant was deleted (“智能体不可用”)
    environment: str
    allowed_origins: list[str]
    experience: ExperienceOverrides
    effective_experience: dict[str, str]
    status: Literal["draft", "active", "error", "disabled"]
    type_name: str
    type_enabled: bool
    config: dict[str, str]
    component_ids: list[UUID]
    published_components: list[dict]  # [{id, name, trigger_tag, version, mode}] frozen in the live snapshot
    secret_masks: dict[str, str]  # key -> masked value, only for secrets that are set
    health_error: str | None
    health_checked_at: datetime | None
    site_key: str
    published_version: str  # "—" until first published
    published_at: datetime | None
    has_unpublished_changes: bool
    pending_approval_id: UUID | None
    row_version: int
    created_at: datetime
    updated_at: datetime
    embed_code: str | None  # only once published (website: the script tag)
    integration: dict[str, str] | None  # only once published: {kind, code} for the type (script / link / webhook / curl)


class ChannelTypeResponse(BaseModel):
    code: str
    name: str
    version: str
    enabled: bool
    available: bool  # enabled by the platform and included in the tenant's plan
    unavailable_reason: Literal["disabled", "plan"] | None
    connection_params: list[dict]
    capabilities: list[str]
    default_experience: dict


class ChannelTypeList(BaseModel):
    items: list[ChannelTypeResponse]


class ChannelTypeWrite(BaseModel):
    enabled: bool | None = None
    version: str | None = Field(default=None, min_length=1, max_length=16, pattern=r"^[0-9][0-9A-Za-z.\-]*$")
    capabilities: list[str] | None = Field(default=None, max_length=12)
    default_experience: dict | None = None


class PublishPreview(BaseModel):
    version_from: str
    version_to: str
    first_publish: bool
    changes: list[dict]
    problems: list[str]  # why publishing would be refused right now
    components: list[dict]  # the selection a publish would freeze: {id, name_zh, trigger_tag, version, mode}
    components_diff: dict  # {added, removed, changed} against the live version
    components_note: str
    injection_prompt: str | None
    trial: list[dict]
    trial_note: str


class ChannelList(BaseModel):
    items: list[ChannelResponse]


class PendingChannelApproval(BaseModel):
    pending_approval: bool = True
    approval_id: UUID


# -- widget (public) ------------------------------------------------------------------------------------


class WidgetSessionRequest(BaseModel):
    site_key: str = Field(min_length=1, max_length=64)
    visitor_id: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9_-]{8,64}$")


class WidgetConfig(BaseModel):
    assistant_name: str
    welcome: str
    primary_color: str


class WidgetSession(BaseModel):
    token: str
    expires_in: int
    visitor_id: str
    config: WidgetConfig


class WidgetChatRequest(BaseModel):
    message: str = Field(default="", max_length=4000)
    event: ComponentEvent | None = None  # what the visitor did on a component; replaces the message (T-059)
    conversation_id: UUID | None = None
    stream: bool = False

    @model_validator(mode="after")
    def message_or_event(self) -> "WidgetChatRequest":
        if self.event is not None:
            self.message = self.event.as_message()
        elif not self.message.strip():
            raise ValueError("message 与 event 至少填写一个")
        return self
