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
from beework.rbac import PORTAL_ONLY_ROLES

QUOTA_KEYS = (
    "assistants",
    "seats",
    "monthly_points",
    "storage_gb",
    "monthly_conversations",
    "api_qps",
    "portal_seats",
)

# Seed definitions of the three plans, version 1 (PRD 3.14.1, PLT-080–082). The migration carries
# its own copy; this one lets tests and fresh databases without migrations bootstrap the same data.
PLAN_DEFAULTS: dict[str, dict] = {
    "standard": {
        "name": "标准版",
        "description": "智能体、知识库、FAQ、组件；网页挂件与 API 渠道。",
        "price_text": "$499 / 月",
        "entitlements": {
            "external_flows": False, "mcp": False, "skills": False,
            "memory": "short", "channels": "widget_api", "handoff": False,
            "evaluations": False, "multi_agent": False, "sso": False,
            "approval_levels": 1, "environments": "production_only",
            "custom_domain": False, "byok": False, "log_retention_days": 30,
            "portal_mode": "named", "portal_overage": "hard",
        },
        "quotas": {
            "assistants": 5, "seats": 10, "monthly_points": 200_000, "storage_gb": 5,
            "monthly_conversations": 20_000, "api_qps": 10, "portal_seats": 200,
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
            "portal_mode": "named", "portal_overage": "grace_5",
        },
        "quotas": {
            "assistants": 30, "seats": 50, "monthly_points": 1_500_000, "storage_gb": 50,
            "monthly_conversations": 200_000, "api_qps": 50, "portal_seats": 1000,
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
            "portal_mode": "unlimited", "portal_overage": None,
        },
        "quotas": {
            "assistants": None, "seats": 500, "monthly_points": 10_000_000, "storage_gb": 1024,
            "monthly_conversations": 2_000_000, "api_qps": 300, "portal_seats": None,
        },
    },
}
# Product has not fixed per-quota overage rules yet; every quota is a hard limit until T-011/T-102.
DEFAULT_OVERAGE_POLICY = {key: "hard" for key in QUOTA_KEYS}


def default_overage_policy(entitlements: dict) -> dict:
    """The portal seat quota's overage policy follows the plan's ``portal_overage`` (grace → soft limit)."""
    grace = str(entitlements.get("portal_overage") or "").startswith("grace")
    return {**DEFAULT_OVERAGE_POLICY, "portal_seats": "soft" if grace else "hard"}


# ---------------------------------------------------------------------------
# AI portal seats granted by the platform per contract (T-149, 演进 PRD 13.1–13.3). Console seats are separate.
# ---------------------------------------------------------------------------

PORTAL_MODES = ("named", "active", "unlimited")
PORTAL_OVERAGES = ("hard", "grace_5", "grace_10")
GRACE_PERCENT = {"hard": 0, "grace_5": 5, "grace_10": 10}
PORTAL_MODE_LABELS = {"named": "指定用户", "active": "按月活跃", "unlimited": "全员不限"}
PORTAL_OVERAGE_LABELS = {"hard": "硬限制", "grace_5": "宽限 5%", "grace_10": "宽限 10%"}


@dataclass(frozen=True)
class PortalGrant:
    mode: str
    seats: int | None  # None only for ``unlimited``
    overage: str | None  # None only for ``unlimited``

    @property
    def assignable_limit(self) -> int | None:
        """The most seats that may be assigned: the contract number plus the grace allowance (rounded down)."""
        if self.mode == "unlimited" or self.seats is None:
            return None
        return self.seats * (100 + GRACE_PERCENT.get(self.overage or "hard", 0)) // 100

    def label(self) -> str:
        if self.mode == "unlimited":
            return PORTAL_MODE_LABELS["unlimited"]
        return f"{PORTAL_MODE_LABELS[self.mode]} · {self.seats:,} · {PORTAL_OVERAGE_LABELS[self.overage or 'hard']}"


def validate_portal_grant(mode: str, seats: int | None, overage: str | None) -> PortalGrant:
    if mode not in PORTAL_MODES:
        raise ApiError(422, "portal.grant_invalid", "AI 门户计费模式无效")
    if mode == "unlimited":
        return PortalGrant("unlimited", None, None)
    if seats is None or isinstance(seats, bool) or seats <= 0:
        raise ApiError(422, "portal.grant_invalid", "门户席位数需大于 0")
    overage = overage or "hard"
    if overage not in PORTAL_OVERAGES:
        raise ApiError(422, "portal.grant_invalid", "席位用尽时的策略无效")
    return PortalGrant(mode, seats, overage)


def portal_grant_of(entitlements: dict, quotas: dict, plan_code: str | None = None) -> PortalGrant:
    """Read the grant from a snapshot; snapshots written before T-149 fall back to their plan's defaults."""
    if "portal_mode" not in entitlements:
        default = PLAN_DEFAULTS.get(plan_code or "", PLAN_DEFAULTS["standard"])
        return PortalGrant(
            default["entitlements"]["portal_mode"], default["quotas"]["portal_seats"], default["entitlements"]["portal_overage"]
        )
    mode = entitlements.get("portal_mode")
    if mode == "unlimited":
        return PortalGrant("unlimited", None, None)
    return PortalGrant(mode, quotas.get("portal_seats"), entitlements.get("portal_overage") or "hard")


def _write_portal_grant(entitlements: dict, quotas: dict, overage_policy: dict, grant: PortalGrant) -> None:
    entitlements["portal_mode"] = grant.mode
    entitlements["portal_overage"] = grant.overage
    quotas["portal_seats"] = grant.seats
    overage_policy["portal_seats"] = "soft" if (grant.overage or "").startswith("grace") else "hard"


def _resolve_portal_grant(
    version: PlanVersion, current: TenantSubscription | None, keep_current: bool,
    mode: str | None, seats: int | None, overage: str | None, plan_code: str,
) -> PortalGrant:
    """Explicit values win over the kept current grant (same plan) which wins over the plan version's own."""
    if keep_current and current is not None:
        base = portal_grant_of(current.entitlements, current.quotas, plan_code)
    else:
        base = portal_grant_of(version.entitlements, version.quotas, plan_code)
    if mode is None and seats is None and overage is None:
        return base
    new_mode = mode or base.mode
    return validate_portal_grant(
        new_mode, seats if seats is not None else base.seats, overage if overage is not None else base.overage
    )


def portal_grant(db: Session, tenant_id: UUID) -> PortalGrant:
    subscription = get_subscription(db, tenant_id)
    tenant = db.get(Tenant, tenant_id)
    return portal_grant_of(subscription.entitlements, subscription.quotas, tenant.plan if tenant else None)


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


CONSOLE_FREE_ROLES = PORTAL_ONLY_ROLES  # portal-only members use no console seat (演进 PRD 13.2); their AI-portal seats are T-150


def seat_usage(db: Session, tenant_id: UUID) -> int:
    """Console seats: active members plus unexpired pending invitations, portal-only ``employee`` members excluded."""
    now = datetime.now(UTC)
    active_members = db.scalar(
        select(func.count())
        .select_from(Membership)
        .join(User, User.id == Membership.user_id)
        .where(
            Membership.tenant_id == tenant_id,
            Membership.status == "active",
            User.is_active.is_(True),
            Membership.role.not_in(CONSOLE_FREE_ROLES),
        )
    ) or 0
    pending_invitations = db.scalar(
        select(func.count())
        .select_from(MemberInvitation)
        .where(
            MemberInvitation.tenant_id == tenant_id,
            MemberInvitation.status == "pending",
            MemberInvitation.expires_at > now,
            MemberInvitation.role.not_in(CONSOLE_FREE_ROLES),
        )
    ) or 0
    return active_members + pending_invitations


register_usage_counter("seats", seat_usage)


def _points_used(db: Session, tenant_id: UUID) -> int:
    from beework.usage import month_points

    return month_points(db, tenant_id)


def _conversations_used(db: Session, tenant_id: UUID) -> int:
    from beework.usage import month_conversations

    return month_conversations(db, tenant_id)


# Points and conversations are metered by T-011; the counters import it lazily because usage depends on this module.
register_usage_counter("monthly_points", _points_used)
register_usage_counter("monthly_conversations", _conversations_used)

ENVIRONMENTS_ENTITLEMENT = {"production_only": ("production",), "all": ("development", "staging", "production")}


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
                            overage_policy=default_overage_policy(definition["entitlements"]),
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
    portal_mode: str | None = None,
    portal_seats: int | None = None,
    portal_overage: str | None = None,
) -> TenantSubscription:
    """Snapshot the latest version of the tenant's plan as its current subscription.

    ``seats`` overrides the plan's seat quota (platform admins pick a seat count at tenant creation).
    Any previous current subscription is kept as history.
    """
    version = _latest_plan_version(db, tenant.plan)
    quotas = dict(version.quotas)
    if seats is not None:
        quotas["seats"] = seats
    entitlements, overage_policy = dict(version.entitlements), dict(version.overage_policy)
    _write_portal_grant(
        entitlements, quotas, overage_policy,
        _resolve_portal_grant(version, None, False, portal_mode, portal_seats, portal_overage, tenant.plan),
    )
    db.execute(
        update(TenantSubscription)
        .where(TenantSubscription.tenant_id == tenant.id, TenantSubscription.is_current.is_(True))
        .values(is_current=False)
    )
    subscription = TenantSubscription(
        tenant_id=tenant.id,
        plan_version_id=version.id,
        entitlements=entitlements,
        quotas=quotas,
        overage_policy=overage_policy,
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


def environments_for(db: Session, tenant_id: UUID) -> tuple[str, ...]:
    """Environments the tenant's subscription offers (RUL-004); the subscription, not ``tenants.plan``, decides."""
    value = get_subscription(db, tenant_id).entitlements.get("environments")
    return ENVIRONMENTS_ENTITLEMENT.get(value, ("production",))


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


# ---------------------------------------------------------------------------
# Plan maintenance (T-010, PLT-085) and applying a plan to a tenant.
# ---------------------------------------------------------------------------

ENTITLEMENT_KEYS = tuple(PLAN_DEFAULTS["standard"]["entitlements"])
OVERAGE_POLICIES = ("hard", "soft", "degrade")


def validate_plan_definition(
    entitlements: dict | None, quotas: dict | None, overage_policy: dict | None
) -> None:
    for key, value in (entitlements or {}).items():
        if key not in ENTITLEMENT_KEYS:
            raise ApiError(422, "plan.invalid", f"未知权益：{key}")
        if value is not None and not isinstance(value, bool | str | int):
            raise ApiError(422, "plan.invalid", f"权益 {key} 的取值无效")
    environments = (entitlements or {}).get("environments")
    if environments is not None and environments not in ENVIRONMENTS_ENTITLEMENT:
        raise ApiError(422, "plan.invalid", "环境权益必须是 production_only 或 all")
    levels = (entitlements or {}).get("approval_levels")
    if levels is not None and levels not in (1, "multi"):
        raise ApiError(422, "plan.invalid", "审批层级必须是 1 或 multi")
    portal_mode = (entitlements or {}).get("portal_mode")
    if portal_mode is not None and portal_mode not in PORTAL_MODES:
        raise ApiError(422, "plan.invalid", "AI 门户计费模式无效")
    portal_overage = (entitlements or {}).get("portal_overage")
    if portal_overage is not None and portal_overage not in PORTAL_OVERAGES:
        raise ApiError(422, "plan.invalid", "AI 门户席位超额策略无效")
    for key, value in (quotas or {}).items():
        if key not in QUOTA_KEYS:
            raise ApiError(422, "plan.invalid", f"未知配额：{key}")
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            raise ApiError(422, "plan.invalid", f"配额 {key} 必须是非负整数，或留空表示不限")
    for key, value in (overage_policy or {}).items():
        if key not in QUOTA_KEYS or value not in OVERAGE_POLICIES:
            raise ApiError(422, "plan.invalid", f"超额策略 {key} 无效")


def latest_version(db: Session, plan: Plan) -> PlanVersion:
    return db.scalars(
        select(PlanVersion).where(PlanVersion.plan_id == plan.id).order_by(PlanVersion.version.desc()).limit(1)
    ).one()


def publish_plan_version(
    db: Session,
    plan: Plan,
    *,
    entitlements: dict | None = None,
    quotas: dict | None = None,
    overage_policy: dict | None = None,
    name: str | None = None,
    description: str | None = None,
    price_text: str | None = None,
) -> PlanVersion:
    """Save the plan with the given changes applied as a new immutable version (PLT-085).

    Existing subscriptions keep their snapshot; only tenants subscribed afterwards get the new version.
    """
    validate_plan_definition(entitlements, quotas, overage_policy)
    db.execute(select(Plan.id).where(Plan.id == plan.id).with_for_update())  # serialise concurrent publishes
    current = latest_version(db, plan)
    if name is not None:
        plan.name = name
    if description is not None:
        plan.description = description
    if price_text is not None:
        plan.price_text = price_text
    version = PlanVersion(
        plan_id=plan.id,
        version=current.version + 1,
        entitlements={**current.entitlements, **(entitlements or {})},
        quotas={**current.quotas, **(quotas or {})},
        overage_policy={**current.overage_policy, **(overage_policy or {})},
    )
    db.add(version)
    db.flush()
    return version


def change_subscription(
    db: Session,
    tenant: Tenant,
    plan_code: str,
    *,
    version_number: int | None = None,
    seats: int | None = None,
    billing_cycle: str = "monthly",
    renews_at: datetime | None = None,
    portal_mode: str | None = None,
    portal_seats: int | None = None,
    portal_overage: str | None = None,
) -> TenantSubscription:
    """Apply a plan version to a tenant as its new current subscription snapshot (history is kept).

    Objects the tenant created beyond the new quotas stay (PLT-083); they just cannot be added to. Without ``seats`` the
    plan's own seat quota applies, except when re-applying the tenant's current plan, which keeps its seats. ``tenants.plan`` / ``seat_limit`` are denormalised copies kept in step.
    """
    ensure_default_plans(db)
    plan = db.scalar(select(Plan).where(Plan.code == plan_code))
    if plan is None:
        raise ApiError(404, "plan.not_found", f"未找到套餐：{plan_code}")
    version = (
        db.scalar(select(PlanVersion).where(PlanVersion.plan_id == plan.id, PlanVersion.version == version_number))
        if version_number is not None
        else latest_version(db, plan)
    )
    if version is None:
        raise ApiError(404, "plan.version_not_found", "未找到指定的套餐版本")
    quotas = dict(version.quotas)
    current = db.scalar(
        select(TenantSubscription).where(TenantSubscription.tenant_id == tenant.id, TenantSubscription.is_current.is_(True))
    )
    if seats is None and current is not None and tenant.plan == plan.code and current.quotas.get("seats") is not None:
        seats = current.quotas["seats"]  # moving to a newer version of the same plan must not reset negotiated seats
    if seats is not None:
        quotas["seats"] = seats
    entitlements, overage_policy = dict(version.entitlements), dict(version.overage_policy)
    grant = _resolve_portal_grant(
        version, current, current is not None and tenant.plan == plan.code, portal_mode, portal_seats, portal_overage, plan.code
    )
    _write_portal_grant(entitlements, quotas, overage_policy, grant)
    db.execute(
        update(TenantSubscription)
        .where(TenantSubscription.tenant_id == tenant.id, TenantSubscription.is_current.is_(True))
        .values(is_current=False)
    )
    previous = db.scalar(
        select(TenantSubscription).where(TenantSubscription.tenant_id == tenant.id).order_by(TenantSubscription.created_at.desc()).limit(1)
    )
    subscription = TenantSubscription(
        tenant_id=tenant.id,
        plan_version_id=version.id,
        entitlements=entitlements,
        quotas=quotas,
        overage_policy=overage_policy,
        billing_cycle=billing_cycle,
        renews_at=renews_at,
        addons=dict(previous.addons) if previous is not None else {},
    )
    db.add(subscription)
    tenant.plan = plan.code
    if quotas.get("seats") is not None:
        tenant.seat_limit = quotas["seats"]
    db.flush()
    return subscription


def plan_in_use(db: Session, plan: Plan) -> bool:
    """RUL-013: a plan any tenant is on, or any subscription (including history) points at, cannot be deleted."""
    on_tenant = db.scalar(select(Tenant.id).where(Tenant.plan == plan.code).limit(1))
    on_subscription = db.scalar(
        select(TenantSubscription.id)
        .join(PlanVersion, PlanVersion.id == TenantSubscription.plan_version_id)
        .where(PlanVersion.plan_id == plan.id)
        .limit(1)
    )
    return on_tenant is not None or on_subscription is not None
