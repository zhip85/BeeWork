from datetime import datetime

from pydantic import BaseModel


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
