from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field


class RuleWrite(BaseModel):
    event: str = Field(min_length=1, max_length=32)
    channel: Literal["email", "slack", "sms", "webhook"]
    # email {members, roles, emails}; slack {url}; sms {phones}; webhook {webhook_ids}. Slack's url may be left out on an edit to keep the stored one.
    recipients: dict[str, Any] = Field(default_factory=dict)
    frequency: Literal["realtime", "hourly", "daily"] = "realtime"
    enabled: bool = True
    row_version: int | None = None


class RuleResponse(BaseModel):
    id: UUID
    event: str
    channel: str
    recipients: dict[str, Any]  # a Slack address is shown masked
    summary: str  # "3 名成员 · 1 个角色 · 2 个邮箱" style line for the list
    frequency: str
    enabled: bool
    row_version: int
    created_at: datetime


class RuleList(BaseModel):
    items: list[RuleResponse]


class NamedItem(BaseModel):
    id: str
    name: str


class Catalog(BaseModel):
    events: list[dict[str, str]]  # key, zh, en
    channels: list[dict[str, Any]]  # key, available, reason
    frequencies: list[str]
    roles: list[NamedItem]
    members: list[NamedItem]
    webhooks: list[NamedItem]
