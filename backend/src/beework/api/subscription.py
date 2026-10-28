from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from beework.db import get_db
from beework.entitlements import QUOTA_KEYS, get_subscription, get_usage_counter
from beework.models import Membership
from beework.models.plans import Plan, PlanVersion
from beework.rbac import require_tenant_permission
from beework.schemas.plans import QuotaUsageResponse, TenantSubscriptionResponse

router = APIRouter()


@router.get("/tenant/subscription", response_model=TenantSubscriptionResponse)
def get_tenant_subscription(
    membership: Membership = Depends(require_tenant_permission("settings:view")),
    db: Session = Depends(get_db),
) -> TenantSubscriptionResponse:
    subscription = get_subscription(db, membership.tenant_id)
    version = db.get(PlanVersion, subscription.plan_version_id)
    plan = db.get(Plan, version.plan_id)
    quotas = []
    for key in QUOTA_KEYS:
        counter = get_usage_counter(key)
        quotas.append(
            QuotaUsageResponse(
                key=key,
                limit=subscription.quotas.get(key),
                used=counter(db, membership.tenant_id) if counter else None,
                policy=subscription.overage_policy.get(key, "hard"),
            )
        )
    db.commit()  # persists a lazily created snapshot
    return TenantSubscriptionResponse(
        plan_code=plan.code,
        plan_name=plan.name,
        plan_version=version.version,
        entitlements=subscription.entitlements,
        quotas=quotas,
        billing_cycle=subscription.billing_cycle,
        renews_at=subscription.renews_at,
        addons=subscription.addons,
    )
