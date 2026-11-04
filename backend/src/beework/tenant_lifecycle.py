"""Tenant lifecycle state machine (PRD 3.12.6, 5.1)."""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from beework.models import AuditEvent, AuthSession, Membership, Tenant
from beework.notifications import notify
from beework.worker.queue import enqueue, job_handler

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LifecycleAction:
    label: str
    to_status: str
    requires_review: bool = False


LIFECYCLE_ACTIONS: dict[str, dict[str, LifecycleAction]] = {
    "trial": {
        "convert_to_active": LifecycleAction("转为正式", "active"),
        "extend_trial": LifecycleAction("延长试用 14 天", "trial"),
        "suspend": LifecycleAction("停用", "suspended", requires_review=True),
    },
    "active": {
        "mark_grace": LifecycleAction("标记欠费（宽限）", "grace"),
        "suspend": LifecycleAction("停用", "suspended", requires_review=True),
    },
    "grace": {
        "record_payment": LifecycleAction("登记回款", "active"),
        "make_read_only": LifecycleAction("转为只读", "read_only"),
    },
    "read_only": {
        "record_payment": LifecycleAction("登记回款", "active"),
        "suspend": LifecycleAction("停用", "suspended", requires_review=True),
    },
    "suspended": {
        "restore": LifecycleAction("恢复", "active"),
        "schedule_deletion": LifecycleAction("进入待删除", "pending_deletion", requires_review=True),
    },
    "pending_deletion": {
        "restore_to_suspended": LifecycleAction("恢复为停用", "suspended"),
        "delete_permanently": LifecycleAction("永久删除", "deleted", requires_review=True),
    },
    "deleted": {},
}

TRIAL_EXTENSION = timedelta(days=14)
REVIEW_TIMEOUT = timedelta(hours=72)


def lifecycle_action(current_status: str, action: str) -> LifecycleAction | None:
    return LIFECYCLE_ACTIONS.get(current_status, {}).get(action)

GRACE_LIMIT = timedelta(days=7)  # PRD 5.1: grace longer than this turns the workspace read-only
DELETION_LIMIT = timedelta(days=30)  # pending deletion longer than this becomes deleted
DEFAULT_TRIAL_DAYS = 14
# Statuses in which the workspace must not consume models: its gateway keys are blocked (T-005).
NON_SERVING_STATUSES = frozenset({"read_only", "suspended", "pending_deletion", "deleted"})
DATA_NOT_PURGED_NOTE = "数据尚未清除：状态已变为已删除，真正的数据清除与删除证明由后续任务完成"


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def record_lifecycle_audit(
    db: Session,
    *,
    actor_id: UUID | None,
    tenant: Tenant,
    action: str,
    details: dict[str, str],
    operator: str = "platform",
) -> None:
    """Platform actions on a tenant are recorded in both chains (PRD 3.12, SET-113); ``operator`` is
    ``platform`` for a platform admin and ``system`` for the scheduled transitions (no actor)."""
    db.add(AuditEvent(tenant_id=tenant.id, actor_user_id=actor_id, action=action, details={**details, "operator": operator}))
    db.add(
        AuditEvent(
            tenant_id=None,
            actor_user_id=actor_id,
            action=action,
            details={**details, "operator": operator, "target_tenant_id": str(tenant.id), "tenant_name": tenant.name},
        )
    )


def apply_transition(db: Session, tenant: Tenant, operation: str, to_status: str, *, now: datetime | None = None) -> None:
    """Change the tenant's status and everything that follows from it: trial extension, session revocation for
    deletion, the status notice for tenant admins and the gateway key block / unblock."""
    now = now or datetime.now(UTC)
    if operation == "extend_trial":
        base = as_utc(tenant.trial_ends_at) if tenant.trial_ends_at else now
        tenant.trial_ends_at = max(base, now) + TRIAL_EXTENSION
    if to_status == tenant.status:
        return
    previous = tenant.status
    tenant.status = to_status
    tenant.status_changed_at = now
    notify(
        db, tenant_id=tenant.id, role="tenant_admin", type="tenant.status_changed",
        title="企业工作区状态已变更", body=f"当前状态：{to_status}", params={"status": to_status},
    )
    if to_status in {"pending_deletion", "deleted"}:
        db.query(AuthSession).filter(
            AuthSession.user_id.in_(select(Membership.user_id).where(Membership.tenant_id == tenant.id)),
            AuthSession.revoked_at.is_(None),
        ).update({"revoked_at": now}, synchronize_session=False)
    if (previous in NON_SERVING_STATUSES) != (to_status in NON_SERVING_STATUSES):
        # A key that can still call models would defeat suspension, so blocking is queued with the transition
        # (same transaction) and the job converges on whatever the status is when it runs.
        enqueue(
            db, "gateway.sync_tenant_access", {"tenant_id": str(tenant.id)}, tenant_id=tenant.id,
            idempotency_key=f"gateway.access:{tenant.id}:{to_status}:{int(now.timestamp())}",
            max_attempts=30,  # a suspended workspace must not keep a working key through a long gateway outage
        )


# ---------------------------------------------------------------------------
# Scheduled transitions (hourly).
# ---------------------------------------------------------------------------


def _auto_transition(db: Session, tenant: Tenant, to_status: str, reason: str, now: datetime) -> None:
    details = {"operation": f"auto_{tenant.status}_to_{to_status}", "from": tenant.status, "to": to_status, "reason": reason}
    if to_status == "deleted":
        details["data_purged"] = "false"
        details["note"] = DATA_NOT_PURGED_NOTE
    record_lifecycle_audit(db, actor_id=None, tenant=tenant, action="tenant.status_changed", details=details, operator="system")
    apply_transition(db, tenant, "auto", to_status, now=now)


def run_auto_lifecycle(db: Session, *, now: datetime | None = None) -> dict[str, int]:
    """Trial expired → suspended; grace > 7 days → read-only; pending deletion > 30 days → deleted.

    Idempotent: a tenant only matches while it is still in the source status, so a second run finds nothing.
    Deleting here only changes the status and revokes sessions; data purging belongs to T-105.
    """
    current = now or datetime.now(UTC)
    counts = {"trial_expired": 0, "grace_to_read_only": 0, "pending_deletion_deleted": 0}

    def due(status: str, *extra):
        # Filtered in SQL so only tenants that are actually due get row-locked; the Python check below is exact.
        return list(db.scalars(select(Tenant).where(Tenant.status == status, *extra).with_for_update()))

    for tenant in due("trial", Tenant.trial_ends_at.is_not(None), Tenant.trial_ends_at <= current):
        if as_utc(tenant.trial_ends_at) <= current:
            _auto_transition(db, tenant, "suspended", "试用期已到期", current)
            counts["trial_expired"] += 1
    for tenant in due("grace", func.coalesce(Tenant.status_changed_at, Tenant.created_at) <= current - GRACE_LIMIT):
        since = as_utc(tenant.status_changed_at or tenant.created_at)
        if current - since > GRACE_LIMIT:
            _auto_transition(db, tenant, "read_only", "欠费宽限期超过 7 天", current)
            counts["grace_to_read_only"] += 1
    for tenant in due("pending_deletion", func.coalesce(Tenant.status_changed_at, Tenant.created_at) <= current - DELETION_LIMIT):
        since = as_utc(tenant.status_changed_at or tenant.created_at)
        if current - since > DELETION_LIMIT:
            _auto_transition(db, tenant, "deleted", "待删除超过 30 天", current)
            counts["pending_deletion_deleted"] += 1
    return counts


@job_handler("tenants.auto_lifecycle")
def auto_lifecycle_job(db: Session, _payload: dict[str, Any]) -> None:
    counts = run_auto_lifecycle(db)
    if any(counts.values()):
        logger.info("Automatic tenant lifecycle transitions: %s", counts)
