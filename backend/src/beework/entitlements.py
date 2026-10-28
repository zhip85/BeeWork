"""Plan entitlements, subscription snapshots and quota decisions (T-010a).

The subscription snapshot on ``tenant_subscriptions`` is the source of truth for what a tenant may
use. Quota usage is counted from business tables through counters registered with
``register_usage_counter``; domains that add a quota (assistants, storage, ...) register their own.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from fastapi import Depends
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from beework.errors import ApiError
from beework.models import MemberInvitation, Membership, Tenant, User
from beework.models.plans import Plan, PlanVersion, TenantSubscription

QUOTA_KEYS = (
    "assistants",
    "seats",
    "monthly_points",
    "storage_gb",
    "monthly_conversations",
    "api_qps",
)

# Seed definitions of the three plans, version 1 (PRD 3.14.1, PLT-080–082). The migration carries
# its own copy; this one lets tests and fresh databases without migrations bootstrap the same data.
PLAN_DEFAULTS: dict[str, dict] = {
    "standard": {
        "name": "标准版",
        "description": "助理、知识库、FAQ、组件；网页挂件与 API 渠道。",
        "price_text": "$499 / 月",
        "entitlements": {
            "external_flows": False, "mcp": False, "skills": False,
            "memory": "short", "channels": "widget_api", "handoff": False,
            "evaluations": False, "multi_agent": False, "sso": False,
            "approval_levels": 1, "environments": "production_only",
            "custom_domain": False, "byok": False, "log_retention_days": 30,
        },
        "quotas": {
            "assistants": 5, "seats": 10, "monthly_points": 200_000, "storage_gb": 5,
            "monthly_conversations": 20_000, "api_qps": 10,
        },
    },
    "professional": {
        "name": "专业版",
        "description": "+ 外部流程、MCP、技能、IM 渠道、人工转接、评测、SSO。",
        "price_text": "$2,400 / 月",
        "entitlements": {
            "external_flows": True, "mcp": True, "skills": True,
            "memory": "short_long", "channels": "im", "handoff": True,
            "evaluations": True, "multi_agent": "delegate_handoff_parallel", "sso": "sso",
            "approval_levels": 1, "environments": "production_only",
            "custom_domain": "custom_domain", "byok": "model_keys", "log_retention_days": 90,
        },
        "quotas": {
            "assistants": 30, "seats": 50, "monthly_points": 1_500_000, "storage_gb": 50,
            "monthly_conversations": 200_000, "api_qps": 50,
        },
    },
    "enterprise": {
        "name": "企业版",
        "description": "+ 全部多智能体模式、多环境、白标、SCIM、自带密钥。",
        "price_text": "定制合同",
        "entitlements": {
            "external_flows": True, "mcp": True, "skills": True,
            "memory": "all_cross_assistant", "channels": "all", "handoff": True,
            "evaluations": True, "multi_agent": "all_long_tasks", "sso": "sso_scim",
            "approval_levels": "multi", "environments": "all",
            "custom_domain": "white_label", "byok": "model_and_encryption_keys",
            "log_retention_days": "custom",
        },
        "quotas": {
            "assistants": None, "seats": 500, "monthly_points": 10_000_000, "storage_gb": 1024,
            "monthly_conversations": 2_000_000, "api_qps": 300,
        },
    },
}
# Product has not fixed per-quota overage rules yet; every quota is a hard limit until T-011/T-102.
DEFAULT_OVERAGE_POLICY = {key: "hard" for key in QUOTA_KEYS}


@dataclass(frozen=True)
class QuotaDecision:
    allowed: bool
    used: int
    limit: int | None
    policy: str


UsageCounter = Callable[[Session, UUID], int]
_usage_counters: dict[str, UsageCounter] = {}


def register_usage_counter(key: str, counter: UsageCounter) -> None:
    _usage_counters[key] = counter


def get_usage_counter(key: str) -> UsageCounter | None:
    return _usage_counters.get(key)


def seat_usage(db: Session, tenant_id: UUID) -> int:
    """Active members plus unexpired pending invitations."""
    now = datetime.now(UTC)
    active_members = db.scalar(
        select(func.count())
        .select_from(Membership)
        .join(User, User.id == Membership.user_id)
        .where(
            Membership.tenant_id == tenant_id,
            Membership.status == "active",
            User.is_active.is_(True),
        )
    ) or 0
    pending_invitations = db.scalar(
        select(func.count())
        .select_from(MemberInvitation)
        .where(
            MemberInvitation.tenant_id == tenant_id,
            MemberInvitation.status == "pending",
            MemberInvitation.expires_at > now,
        )
    ) or 0
    return active_members + pending_invitations


register_usage_counter("seats", seat_usage)


def ensure_default_plans(db: Session) -> None:
    """Insert any missing default plan / version 1 (idempotent; no-op after the migration seed)."""
    for code, definition in PLAN_DEFAULTS.items():
        plan = db.scalar(select(Plan).where(Plan.code == code))
        if plan is None:
            try:
                with db.begin_nested():
                    plan = Plan(
                        code=code,
                        name=definition["name"],
                        description=definition["description"],
                        price_text=definition["price_text"],
                    )
                    db.add(plan)
                    db.flush()
            except IntegrityError:
                plan = db.scalar(select(Plan).where(Plan.code == code))
        if plan is None:
            continue
        has_version = db.scalar(select(PlanVersion.id).where(PlanVersion.plan_id == plan.id).limit(1))
        if has_version is None:
            try:
                with db.begin_nested():
                    db.add(
                        PlanVersion(
                            plan_id=plan.id,
                            version=1,
                            entitlements=definition["entitlements"],
                            quotas=definition["quotas"],
                            overage_policy=DEFAULT_OVERAGE_POLICY,
                        )
                    )
                    db.flush()
            except IntegrityError:
                pass


def _latest_plan_version(db: Session, plan_code: str) -> PlanVersion:
    ensure_default_plans(db)
    version = db.scalar(
        select(PlanVersion)
        .join(Plan, Plan.id == PlanVersion.plan_id)
        .where(Plan.code == plan_code)
        .order_by(PlanVersion.version.desc())
        .limit(1)
    )
    if version is None:
        raise ApiError(409, "plan.unknown", f"未找到套餐：{plan_code}")
    return version


def create_subscription(
    db: Session,
    tenant: Tenant,
    *,
    seats: int | None = None,
    billing_cycle: str = "monthly",
) -> TenantSubscription:
    """Snapshot the latest version of the tenant's plan as its current subscription.

    ``seats`` overrides the plan's seat quota (platform admins pick a seat count at tenant creation).
    Any previous current subscription is kept as history.
    """
    version = _latest_plan_version(db, tenant.plan)
    quotas = dict(version.quotas)
    if seats is not None:
        quotas["seats"] = seats
    db.execute(
        update(TenantSubscription)
        .where(TenantSubscription.tenant_id == tenant.id, TenantSubscription.is_current.is_(True))
        .values(is_current=False)
    )
    subscription = TenantSubscription(
        tenant_id=tenant.id,
        plan_version_id=version.id,
        entitlements=dict(version.entitlements),
        quotas=quotas,
        overage_policy=dict(version.overage_policy),
        billing_cycle=billing_cycle,
        addons={},
    )
    db.add(subscription)
    db.flush()
    return subscription


def get_subscription(db: Session, tenant_id: UUID) -> TenantSubscription:
    """Current subscription; tenants created before a snapshot existed get one on first use."""
    subscription = db.scalar(
        select(TenantSubscription).where(
            TenantSubscription.tenant_id == tenant_id, TenantSubscription.is_current.is_(True)
        )
    )
    if subscription is not None:
        return subscription
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise ApiError(404, "tenant.not_found", "未找到企业工作区")
    try:
        with db.begin_nested():
            return create_subscription(db, tenant, seats=tenant.seat_limit)
    except IntegrityError:
        # A concurrent request created the current subscription first.
        existing = db.scalar(
            select(TenantSubscription).where(
                TenantSubscription.tenant_id == tenant_id, TenantSubscription.is_current.is_(True)
            )
        )
        if existing is None:
            raise
        return existing


def _has_value(value: object) -> bool:
    return value not in (False, None, 0, "", "none")


def has_entitlement(db: Session, tenant_id: UUID, key: str) -> bool:
    return _has_value(get_subscription(db, tenant_id).entitlements.get(key))


def entitlement_value(db: Session, tenant_id: UUID, key: str) -> object:
    return get_subscription(db, tenant_id).entitlements.get(key)


def ensure_entitlement(db: Session, tenant_id: UUID, key: str) -> None:
    if not has_entitlement(db, tenant_id, key):
        raise ApiError(403, "plan.feature_locked", "当前套餐未包含，请联系平台升级")


def require_entitlement(key: str):
    """Route dependency: 403 ``plan.feature_locked`` when the tenant's plan lacks the feature."""
    from beework.auth import get_tenant_membership
    from beework.db import get_db

    def dependency(
        membership: Membership = Depends(get_tenant_membership),
        db: Session = Depends(get_db),
    ) -> Membership:
        ensure_entitlement(db, membership.tenant_id, key)
        return membership

    return dependency


def quota_limit(db: Session, tenant_id: UUID, key: str) -> int | None:
    return get_subscription(db, tenant_id).quotas.get(key)


def quota_usage(db: Session, tenant_id: UUID, key: str) -> int:
    counter = _usage_counters.get(key)
    if counter is None:
        raise ValueError(f"配额 {key} 尚未注册用量计数器")
    return counter(db, tenant_id)


def check_quota(db: Session, tenant_id: UUID, key: str, increment: int = 1) -> QuotaDecision:
    """Would adding ``increment`` stay within the quota? Soft / degrade policies never block."""
    subscription = get_subscription(db, tenant_id)
    limit = subscription.quotas.get(key)
    policy = subscription.overage_policy.get(key, "hard")
    used = quota_usage(db, tenant_id, key)
    within = limit is None or used + increment <= limit
    return QuotaDecision(allowed=within or policy != "hard", used=used, limit=limit, policy=policy)


def enforce_quota(
    db: Session, tenant_id: UUID, key: str, increment: int = 1, message: str | None = None
) -> QuotaDecision:
    """``check_quota`` that raises 409 ``plan.quota_exceeded`` on a hard limit."""
    decision = check_quota(db, tenant_id, key, increment)
    if not decision.allowed:
        raise ApiError(
            409,
            "plan.quota_exceeded",
            message or f"已达套餐配额上限（{decision.used}/{decision.limit}），请联系平台升级",
        )
    return decision
