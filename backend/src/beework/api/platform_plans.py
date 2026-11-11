"""Platform-side plan maintenance and subscription changes (T-010: PLT-080–085, RUL-013)."""

from uuid import UUID

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from beework.api.conventions import require_platform_admin
from beework.api.subscription import portal_response
from beework.auth import require_csrf
from beework.db import get_db
from beework.entitlements import (
    PortalGrant,
    change_subscription,
    ensure_default_plans,
    get_subscription,
    plan_in_use,
    portal_grant_of,
    publish_plan_version,
)
from beework.errors import ApiError
from beework.models import AuditEvent, Tenant, User
from beework.models.plans import Plan, PlanVersion, TenantSubscription
from beework.schemas.plans import (
    ChangeSubscriptionRequest,
    PlanResponse,
    PlanTenantRef,
    PlanVersionResponse,
    PlatformSubscriptionResponse,
    PublishPlanVersionRequest,
)
from beework.tenant_lifecycle import record_lifecycle_audit

router = APIRouter()


def _plan_response(db: Session, plan: Plan) -> PlanResponse:
    versions = list(
        db.scalars(select(PlanVersion).where(PlanVersion.plan_id == plan.id).order_by(PlanVersion.version.desc()))
    )
    counts = dict(
        db.execute(
            select(TenantSubscription.plan_version_id, func.count())
            .where(TenantSubscription.is_current.is_(True), TenantSubscription.plan_version_id.in_([v.id for v in versions]))
            .group_by(TenantSubscription.plan_version_id)
        ).all()
    )
    tenants = list(
        db.execute(
            select(Tenant.id, Tenant.name)
            .join(TenantSubscription, TenantSubscription.tenant_id == Tenant.id)
            .where(TenantSubscription.is_current.is_(True), TenantSubscription.plan_version_id.in_([v.id for v in versions]))
            .order_by(Tenant.name)
        ).all()
    )
    return PlanResponse(
        code=plan.code,
        name=plan.name,
        description=plan.description,
        price_text=plan.price_text,
        tenant_count=len(tenants),
        tenants=[PlanTenantRef(id=str(tenant_id), name=name) for tenant_id, name in tenants[:3]],
        latest_version=versions[0].version,
        versions=[
            PlanVersionResponse(
                version=v.version,
                entitlements=v.entitlements,
                quotas=v.quotas,
                overage_policy=v.overage_policy,
                published_at=v.published_at,
                tenant_count=counts.get(v.id, 0),
            )
            for v in versions
        ],
    )


def _plan_or_404(db: Session, code: str) -> Plan:
    ensure_default_plans(db)
    plan = db.scalar(select(Plan).where(Plan.code == code))
    if plan is None:
        raise ApiError(404, "plan.not_found", f"未找到套餐：{code}")
    return plan


@router.get("/platform/plans", response_model=list[PlanResponse])
def list_plans(
    _: User = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> list[PlanResponse]:
    ensure_default_plans(db)
    db.commit()
    order = {"standard": 0, "professional": 1, "enterprise": 2}
    plans = sorted(db.scalars(select(Plan)), key=lambda plan: (order.get(plan.code, 9), plan.code))
    return [_plan_response(db, plan) for plan in plans]


@router.post("/platform/plans/{code}/versions", response_model=PlanResponse, status_code=status.HTTP_201_CREATED)
def publish_version(
    code: str,
    payload: PublishPlanVersionRequest,
    user: User = Depends(require_platform_admin),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> PlanResponse:
    plan = _plan_or_404(db, code)
    version = publish_plan_version(
        db, plan, entitlements=payload.entitlements, quotas=payload.quotas, overage_policy=payload.overage_policy,
        name=payload.name, description=payload.description, price_text=payload.price_text,
    )
    db.add(
        AuditEvent(
            tenant_id=None, actor_user_id=user.id, action="plan.version_published",
            details={"plan": plan.code, "version": str(version.version)},
        )
    )
    db.commit()
    return _plan_response(db, plan)


@router.delete("/platform/plans/{code}", status_code=status.HTTP_204_NO_CONTENT)
def delete_plan(
    code: str,
    user: User = Depends(require_platform_admin),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> Response:
    plan = _plan_or_404(db, code)
    if plan_in_use(db, plan):
        raise ApiError(409, "plan.in_use", "有租户正在使用该套餐，不能删除")
    for version in db.scalars(select(PlanVersion).where(PlanVersion.plan_id == plan.id)):
        db.delete(version)
    db.delete(plan)
    db.add(AuditEvent(tenant_id=None, actor_user_id=user.id, action="plan.deleted", details={"plan": code}))
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _subscription_response(db: Session, tenant_id: UUID) -> PlatformSubscriptionResponse:
    subscription = get_subscription(db, tenant_id)
    version = db.get(PlanVersion, subscription.plan_version_id)
    plan = db.get(Plan, version.plan_id)
    return PlatformSubscriptionResponse(
        tenant_id=str(tenant_id), plan_code=plan.code, plan_version=version.version, quotas=subscription.quotas,
        entitlements=subscription.entitlements, billing_cycle=subscription.billing_cycle,
        renews_at=subscription.renews_at, created_at=subscription.created_at,
        portal=portal_response(portal_grant_of(subscription.entitlements, subscription.quotas, plan.code)),
    )


@router.get("/platform/tenants/{tenant_id}/subscription", response_model=PlatformSubscriptionResponse)
def get_tenant_subscription_for_platform(
    tenant_id: UUID,
    _: User = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> PlatformSubscriptionResponse:
    if db.get(Tenant, tenant_id) is None:
        raise ApiError(404, "tenant.not_found", "未找到企业工作区")
    response = _subscription_response(db, tenant_id)
    db.commit()
    return response


@router.post("/platform/tenants/{tenant_id}/subscription", response_model=PlatformSubscriptionResponse)
def apply_subscription_change(
    tenant_id: UUID,
    payload: ChangeSubscriptionRequest,
    user: User = Depends(require_platform_admin),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> PlatformSubscriptionResponse:
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise ApiError(404, "tenant.not_found", "未找到企业工作区")
    before = _subscription_response(db, tenant_id)
    subscription = change_subscription(
        db, tenant, payload.plan_code, version_number=payload.version, seats=payload.seats,
        billing_cycle=payload.billing_cycle, renews_at=payload.renews_at,
        portal_mode=payload.portal_mode, portal_seats=payload.portal_seats, portal_overage=payload.portal_overage,
    )
    after = _subscription_response(db, tenant_id)
    record_lifecycle_audit(
        db, actor_id=user.id, tenant=tenant, action="tenant.subscription_changed",
        details={
            "from_plan": f"{before.plan_code}@v{before.plan_version}",
            "to_plan": f"{after.plan_code}@v{after.plan_version}",
            "seats": str(subscription.quotas.get("seats")),
            **_portal_change(before.portal, after.portal),
            "billing_cycle": subscription.billing_cycle,
        },
    )
    db.commit()
    return after



def _portal_change(before, after) -> dict[str, str]:
    """“合同变更 · AI 门户：指定用户 · 1,500 → 按月活跃 · 1,800”, only when the grant actually changed."""
    old = PortalGrant(before.mode, before.seats, before.overage)
    new = PortalGrant(after.mode, after.seats, after.overage)
    return {"portal": f"{old.label()} → {new.label()}"} if old != new else {}
