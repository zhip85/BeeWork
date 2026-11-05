from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class QuotaUsageResponse(BaseModel):
    key: str
    limit: int | None
    used: int | None  # None: no usage counter for this quota yet
    policy: str


class TenantSubscriptionResponse(BaseModel):
    plan_code: str
    plan_name: str
    plan_version: int
    entitlements: dict
    quotas: list[QuotaUsageResponse]
    billing_cycle: str
    renews_at: datetime | None
    addons: dict


class PlanVersionResponse(BaseModel):
    version: int
    entitlements: dict
    quotas: dict
    overage_policy: dict
    published_at: datetime
    tenant_count: int  # tenants whose current subscription is this version


class PlanTenantRef(BaseModel):
    id: str
    name: str


class PlanResponse(BaseModel):
    code: str
    name: str
    description: str
    price_text: str
    tenant_count: int
    tenants: list[PlanTenantRef]  # up to three, for the plan card (PLT-080)
    latest_version: int
    versions: list[PlanVersionResponse]


class PublishPlanVersionRequest(BaseModel):
    """Fields left out keep the latest version's value (so the edit form can send just what changed)."""

    name: str | None = Field(default=None, min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=500)
    price_text: str | None = Field(default=None, max_length=80)
    entitlements: dict | None = None
    quotas: dict | None = None
    overage_policy: dict | None = None


class ChangeSubscriptionRequest(BaseModel):
    plan_code: str = Field(min_length=1, max_length=24)
    version: int | None = Field(default=None, ge=1)  # latest when omitted
    seats: int | None = Field(default=None, ge=1, le=100000)  # the plan's seat quota when omitted
    billing_cycle: Literal["monthly", "yearly"] = "monthly"
    renews_at: datetime | None = None


class PlatformSubscriptionResponse(BaseModel):
    tenant_id: str
    plan_code: str
    plan_version: int
    quotas: dict
    entitlements: dict
    billing_cycle: str
    renews_at: datetime | None
    created_at: datetime
