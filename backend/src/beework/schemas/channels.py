from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from beework.channel_publish import COLOR_PATTERN, MAX_ORIGINS, normalize_origins

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
    name: str = Field(min_length=1, max_length=50)
    assistant_id: UUID
    environment: Environment = "production"
    allowed_origins: list[str] = Field(default_factory=list, max_length=MAX_ORIGINS)
    experience: ExperienceOverrides = Field(default_factory=ExperienceOverrides)
    row_version: int | None = None

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
    status: Literal["draft", "active", "disabled"]
    site_key: str
    published_version: str  # "—" until first published
    published_at: datetime | None
    has_unpublished_changes: bool
    pending_approval_id: UUID | None
    row_version: int
    created_at: datetime
    updated_at: datetime
    embed_code: str | None  # only once published


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
    message: str = Field(min_length=1, max_length=4000)
    conversation_id: UUID | None = None
    stream: bool = False
