from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from beework.events import EVENT_TYPES


class WebhookWrite(BaseModel):
    url: str = Field(min_length=1, max_length=500)
    events: list[str] = Field(min_length=1, max_length=len(EVENT_TYPES))
    enabled: bool = True
    row_version: int | None = None

    @field_validator("url")
    @classmethod
    def strip_url(cls, value: str) -> str:
        return value.strip()

    @field_validator("events")
    @classmethod
    def known_events(cls, value: list[str]) -> list[str]:
        unknown = [e for e in value if e not in EVENT_TYPES]
        if unknown:
            raise ValueError(f"不支持的事件：{', '.join(unknown)}")
        return list(dict.fromkeys(value))


class DeliverySummary(BaseModel):
    status: str
    status_code: int | None
    at: datetime | None


class WebhookResponse(BaseModel):
    id: UUID
    url: str
    events: list[str]
    enabled: bool
    secret_mask: str  # the secret is never returned again after it was shown
    disabled_reason: Literal["failing"] | None
    last_delivery: DeliverySummary | None
    failing_since: datetime | None
    row_version: int
    created_at: datetime


class WebhookCreated(WebhookResponse):
    signing_secret: str  # shown once


class WebhookList(BaseModel):
    items: list[WebhookResponse]
    events: list[str]  # what can be subscribed to


class DeliveryResponse(BaseModel):
    id: UUID
    event: str
    status: Literal["pending", "retrying", "delivered", "dead", "cancelled"]
    attempts: int
    status_code: int | None
    duration_ms: int | None
    error: str | None
    is_test: bool
    created_at: datetime
    last_attempt_at: datetime | None
    next_retry_at: datetime | None


class DeliveryList(BaseModel):
    items: list[DeliveryResponse]
