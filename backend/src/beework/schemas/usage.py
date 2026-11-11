from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from beework.schemas.plans import PortalGrantResponse


class BudgetResponse(BaseModel):
    monthly_points: int
    thresholds: list[int]
    recipients: list[str]
    pause_non_production: bool
    percent_used: float
    paused: bool  # non-production traffic is paused for the rest of this month


class BudgetUpdate(BaseModel):
    monthly_points: int = Field(ge=1, le=1_000_000_000)
    thresholds: list[Literal[50, 80, 100]] = Field(max_length=3)
    recipients: list[str] = Field(default_factory=list, max_length=10)
    pause_non_production: bool = False


class MonthUsage(BaseModel):
    month: str  # YYYY-MM
    points: int


class AssistantUsage(BaseModel):
    assistant_id: str | None  # None: the "other" bucket
    name: str | None  # None: deleted or unattributed; the client labels it
    points: int
    share: float  # of this month's points, 0..1
    other: bool = False


class UsageSummary(BaseModel):
    month: str
    points_used: int
    points_quota: int | None  # the plan's monthly points; None means unlimited
    projected_points: int
    seats_used: int
    seats_limit: int | None
    portal: PortalGrantResponse
    environment_count: int
    plan_name: str
    billing_cycle: str
    renews_at: datetime | None
    budget: BudgetResponse
    history: list[MonthUsage]
    by_assistant: list[AssistantUsage]
    synced_at: datetime | None
    sync_error: str | None
