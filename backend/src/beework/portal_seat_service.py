"""AI portal seats: who holds one, capacity against the platform's grant, and every change to that (T-150).

A member holds a portal seat when an assignment says so (manual / auto), or when a department rule covers their
organization and they have not been individually excluded. The server computes this every time — nothing is cached —
and every change that could add holders is checked against the grant's assignable limit *after* being applied in the
transaction, so a refused batch is rolled back whole (整批拒绝、不做部分变更).
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from beework.entitlements import portal_grant, register_usage_counter
from beework.errors import ApiError
from beework.models import AuditEvent, Membership, Organization, User
from beework.models.portal_seats import PortalSeatAssignment, PortalSeatPolicy, PortalSeatRule
from beework.notifications import notify
from beework.rbac import membership_can, organization_subtree

IDLE_CHOICES = (30, 60, 90)
THRESHOLD_PERCENT = 90


def _aware(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value


def rule_scope(db: Session, tenant_id: UUID) -> set[UUID]:
    scope: set[UUID] = set()
    for organization_id in db.scalars(select(PortalSeatRule.organization_id).where(PortalSeatRule.tenant_id == tenant_id)):
        scope |= set(organization_subtree(db, tenant_id, organization_id))
    return scope


def assignment_rows(db: Session, tenant_id: UUID) -> dict[UUID, PortalSeatAssignment]:
    return {row.user_id: row for row in db.scalars(select(PortalSeatAssignment).where(PortalSeatAssignment.tenant_id == tenant_id))}


def active_members(db: Session, tenant_id: UUID) -> list[tuple[Membership, User]]:
    rows = db.execute(
        select(Membership, User).join(User, User.id == Membership.user_id).where(
            Membership.tenant_id == tenant_id, Membership.status == "active", User.is_active.is_(True)
        )
    )
    return [(membership, user) for membership, user in rows]


def seat_source(membership: Membership, row: PortalSeatAssignment | None, scope: set[UUID]) -> str | None:
    """``manual`` / ``auto`` / ``rule`` when the member holds a seat, ``None`` otherwise."""
    if row is not None and row.status == "assigned":
        return row.source or "manual"
    if membership.organization_id in scope and not (row is not None and row.excluded):
        return "rule"
    return None


def holders(db: Session, tenant_id: UUID) -> dict[UUID, str]:
    scope, rows = rule_scope(db, tenant_id), assignment_rows(db, tenant_id)
    result: dict[UUID, str] = {}
    for membership, _user in active_members(db, tenant_id):
        source = seat_source(membership, rows.get(membership.user_id), scope)
        if source is not None:
            result[membership.user_id] = source
    return result


def assigned_count(db: Session, tenant_id: UUID) -> int:
    return len(holders(db, tenant_id))


register_usage_counter("portal_seats", assigned_count)


def get_policy(db: Session, tenant_id: UUID) -> PortalSeatPolicy:
    policy = db.scalar(select(PortalSeatPolicy).where(PortalSeatPolicy.tenant_id == tenant_id))
    if policy is None:
        try:
            with db.begin_nested():
                policy = PortalSeatPolicy(tenant_id=tenant_id, auto_assign=False, idle_days=None)
                db.add(policy)
                db.flush()
        except IntegrityError:  # a concurrent request created the single row first (the page loads several endpoints at once)
            policy = db.scalar(select(PortalSeatPolicy).where(PortalSeatPolicy.tenant_id == tenant_id))
            if policy is None:
                raise
    return policy


def _audit(db: Session, tenant_id: UUID, actor: UUID | None, action: str, details: dict[str, str]) -> None:
    db.add(AuditEvent(tenant_id=tenant_id, actor_user_id=actor, action=action, details=details))


def ensure_capacity(db: Session, tenant_id: UUID, before: int) -> str | None:
    """Count the holders the pending (flushed) changes produce and refuse when over the assignable limit.

    Returns a notice when the result exceeds the contract number but stays within the grace allowance. Raising rolls the
    whole request back, so a batch is applied completely or not at all.
    """
    db.flush()
    grant = portal_grant(db, tenant_id)
    after = assigned_count(db, tenant_id)
    limit = grant.assignable_limit
    if limit is not None and after > limit and after > before:
        remaining = max(limit - before, 0)
        message = "合同席位已用尽，请联系客户成功经理扩容" if remaining == 0 else f"最多还能分配 {remaining} 个门户席位，已有 {before} 个"
        raise ApiError(409, "portal.seats_exhausted", message)
    _notify_threshold(db, tenant_id, grant.seats, before, after)
    if grant.seats is not None and after > grant.seats:
        return f"当前已超出合同 {grant.seats} 席位，处于宽限期"
    return None


def _notify_threshold(db: Session, tenant_id: UUID, seats: int | None, before: int, after: int) -> None:
    if seats and before * 100 < THRESHOLD_PERCENT * seats <= after * 100:
        notify(
            db, tenant_id=tenant_id, type="portal.seats_near_limit", title="AI 门户席位即将用尽",
            body=f"已分配 {after} / {seats}，达到合同席位的 {THRESHOLD_PERCENT}%。", link="/ai-portal",
            params={"assigned": after, "seats": seats}, role="tenant_admin",
        )


def _members_by_user(db: Session, tenant_id: UUID, user_ids: list[UUID]) -> dict[UUID, Membership]:
    unique = list(dict.fromkeys(user_ids))
    found = {
        membership.user_id: membership
        for membership, _user in active_members(db, tenant_id) if membership.user_id in set(unique)
    }
    if len(found) != len(unique):
        raise ApiError(404, "not_found", "未找到指定成员")  # also what another tenant's member looks like
    return found


def _row(db: Session, tenant_id: UUID, user_id: UUID) -> PortalSeatAssignment:
    row = db.scalar(
        select(PortalSeatAssignment).where(PortalSeatAssignment.tenant_id == tenant_id, PortalSeatAssignment.user_id == user_id)
    )
    if row is None:
        row = PortalSeatAssignment(tenant_id=tenant_id, user_id=user_id, status="none", excluded=False)
        db.add(row)
    return row


def assign(
    db: Session, tenant_id: UUID, user_ids: list[UUID], actor: UUID | None, *, source: str = "manual", approve: bool = False,
) -> tuple[int, str | None]:
    """Give each member a seat of their own. Members who already hold one are left alone."""
    members = _members_by_user(db, tenant_id, user_ids)
    before = assigned_count(db, tenant_id)
    held = holders(db, tenant_id)
    now, changed = datetime.now(UTC), 0
    for user_id in members:
        row = _row(db, tenant_id, user_id)
        was_requested = row.status == "requested"
        if user_id in held and not was_requested:
            continue
        row.status, row.source, row.excluded = "assigned", source, False
        row.assigned_by, row.assigned_at = (str(actor) if actor else None), now
        row.request_reason = None if approve else row.request_reason
        if approve or was_requested:
            row.decided_by, row.decided_at = (str(actor) if actor else None), now
        changed += 1
        if approve:
            notify(
                db, tenant_id=tenant_id, type="portal.seat_approved", title="门户席位申请已通过",
                body="你现在可以使用 AI 门户了。", link="/portal", recipient_ids=[user_id],
            )
    notice = ensure_capacity(db, tenant_id, before)
    if changed:
        _audit(db, tenant_id, actor, "portal_seat.approved" if approve else "portal_seat.assigned", {"count": str(changed), "source": source})
    return changed, notice


def revoke(db: Session, tenant_id: UUID, user_ids: list[UUID], actor: UUID | None, *, action: str = "portal_seat.revoked") -> int:
    """Take the seat back. Inside a rule department the member is marked excluded so the next sync does not re-grant it."""
    members = _members_by_user(db, tenant_id, user_ids)
    scope, held = rule_scope(db, tenant_id), holders(db, tenant_id)
    changed = 0
    for user_id, membership in members.items():
        if user_id not in held:
            continue
        row = _row(db, tenant_id, user_id)
        row.status, row.source = "none", None
        row.excluded = membership.organization_id in scope
        changed += 1
    db.flush()
    if changed:
        _audit(db, tenant_id, actor, action, {"count": str(changed)})
    return changed


def reject(db: Session, tenant_id: UUID, user_ids: list[UUID], actor: UUID | None) -> int:
    members = _members_by_user(db, tenant_id, user_ids)
    now, changed = datetime.now(UTC), 0
    for user_id in members:
        row = _row(db, tenant_id, user_id)
        if row.status != "requested":
            continue
        row.status, row.request_reason = "none", None
        row.decided_by, row.decided_at = (str(actor) if actor else None), now
        changed += 1
        notify(
            db, tenant_id=tenant_id, type="portal.seat_rejected", title="门户席位申请未通过",
            body="管理员暂未通过你的开通申请，如有需要可重新申请。", link="/portal", recipient_ids=[user_id],
        )
    db.flush()
    if changed:
        _audit(db, tenant_id, actor, "portal_seat.rejected", {"count": str(changed)})
    return changed


def approve(db: Session, tenant_id: UUID, user_ids: list[UUID], actor: UUID | None) -> tuple[int, str | None]:
    members = _members_by_user(db, tenant_id, user_ids)
    rows = assignment_rows(db, tenant_id)
    requested = [user_id for user_id in members if rows.get(user_id) is not None and rows[user_id].status == "requested"]
    if not requested:
        raise ApiError(409, "portal.no_request", "所选成员没有待审批的申请")
    return assign(db, tenant_id, requested, actor, source="manual", approve=True)


def set_rules(db: Session, tenant_id: UUID, organization_ids: list[UUID], actor: UUID | None) -> str | None:
    wanted = set(organization_ids)
    known = set(db.scalars(select(Organization.id).where(Organization.tenant_id == tenant_id, Organization.id.in_(wanted))))
    if known != wanted:
        raise ApiError(404, "not_found", "未找到指定部门")
    before = assigned_count(db, tenant_id)
    current = {rule.organization_id: rule for rule in db.scalars(select(PortalSeatRule).where(PortalSeatRule.tenant_id == tenant_id))}
    for organization_id, rule in current.items():
        if organization_id not in wanted:
            db.delete(rule)
    for organization_id in wanted - set(current):
        db.add(PortalSeatRule(tenant_id=tenant_id, organization_id=organization_id, created_by=str(actor) if actor else None))
    db.flush()
    # Exclusions only matter inside a rule's scope; drop the ones that no longer do.
    scope, rows = rule_scope(db, tenant_id), assignment_rows(db, tenant_id)
    for membership, _user in active_members(db, tenant_id):
        row = rows.get(membership.user_id)
        if row is not None and row.excluded and membership.organization_id not in scope:
            row.excluded = False
    notice = ensure_capacity(db, tenant_id, before)
    _audit(db, tenant_id, actor, "portal_seat_rule.updated", {"organizations": str(len(wanted)), "assigned": str(assigned_count(db, tenant_id))})
    return notice


def update_policy(db: Session, tenant_id: UUID, auto_assign: bool, idle_days: int | None, actor: UUID | None) -> PortalSeatPolicy:
    if idle_days is not None and idle_days not in IDLE_CHOICES:
        raise ApiError(422, "portal.policy_invalid", "闲置回收天数只能是 30、60、90 或不回收")
    policy = get_policy(db, tenant_id)
    changes = {}
    if policy.auto_assign != auto_assign:
        changes["auto_assign"] = f"{policy.auto_assign}→{auto_assign}"
    if policy.idle_days != idle_days:
        changes["idle_days"] = f"{policy.idle_days}→{idle_days}"
    policy.auto_assign, policy.idle_days = auto_assign, idle_days
    if changes:
        _audit(db, tenant_id, actor, "portal_seat_policy.updated", changes)
    db.flush()
    return policy


def idle_user_ids(db: Session, tenant_id: UUID, *, now: datetime | None = None) -> list[UUID]:
    """Holders whose last portal sign-in (or, never signed in, their seat / join date) is older than the idle policy."""
    policy = get_policy(db, tenant_id)
    if policy.idle_days is None:
        return []
    cutoff = (now or datetime.now(UTC)) - timedelta(days=policy.idle_days)
    scope, rows = rule_scope(db, tenant_id), assignment_rows(db, tenant_id)
    idle: list[UUID] = []
    for membership, _user in active_members(db, tenant_id):
        row = rows.get(membership.user_id)
        if seat_source(membership, row, scope) is None:
            continue
        reference = _aware(row.last_login_at) if row is not None else None
        reference = reference or (_aware(row.assigned_at) if row is not None and row.status == "assigned" else None) or _aware(membership.created_at)
        if reference is not None and reference < cutoff:
            idle.append(membership.user_id)
    return idle


def reclaim_idle(db: Session, tenant_id: UUID, actor: UUID | None, *, now: datetime | None = None) -> int:
    user_ids = idle_user_ids(db, tenant_id, now=now)
    if not user_ids:
        return 0
    return revoke(db, tenant_id, user_ids, actor, action="portal_seat.reclaimed")


def try_auto_assign(db: Session, membership: Membership) -> bool:
    """First sign-in auto-assignment (T-151): only when the tenant turned it on and a seat is still assignable."""
    if not get_policy(db, membership.tenant_id).auto_assign:
        return False
    savepoint = db.begin_nested()
    try:
        assign(db, membership.tenant_id, [membership.user_id], None, source="auto")
    except ApiError:
        savepoint.rollback()
        return False
    savepoint.commit()
    return True


def reclaim_idle_for_all_tenants(db: Session) -> None:
    """Daily worker job: apply each tenant's idle policy (tenants with “不回收” are skipped). One tenant failing must
    not stop the others."""
    from beework.api.common import lock_tenant

    tenant_ids = list(db.scalars(select(PortalSeatPolicy.tenant_id).where(PortalSeatPolicy.idle_days.is_not(None))))
    for tenant_id in tenant_ids:
        try:
            lock_tenant(db, tenant_id)
            reclaim_idle(db, tenant_id, None)
            db.commit()
        except Exception:  # noqa: BLE001 - isolate tenants from each other
            db.rollback()


# ---------------------------------------------------------------------------
# Employee side (T-151): the member's own seat, the portal session and requests.
# ---------------------------------------------------------------------------

LOGIN_TOUCH_INTERVAL = timedelta(minutes=5)
REASON_MAX = 200


def member_seat(db: Session, membership: Membership) -> tuple[str, str | None]:
    """(state, source) of one member: ``assigned`` with ``manual`` / ``auto`` / ``rule`` / ``unlimited``, else
    ``requested`` or ``unassigned``. A tenant whose grant is “全员不限” gives every active member access."""
    if portal_grant(db, membership.tenant_id).mode == "unlimited":
        return "assigned", "unlimited"
    row = db.scalar(
        select(PortalSeatAssignment).where(
            PortalSeatAssignment.tenant_id == membership.tenant_id, PortalSeatAssignment.user_id == membership.user_id
        )
    )
    source = seat_source(membership, row, rule_scope(db, membership.tenant_id))
    if source is not None:
        return "assigned", source
    return ("requested" if row is not None and row.status == "requested" else "unassigned"), None


def has_seat(db: Session, membership: Membership) -> bool:
    return membership.status == "active" and member_seat(db, membership)[0] == "assigned"


def open_session(db: Session, membership: Membership) -> tuple[str, str | None, bool]:
    """Called when the employee enters the portal: records the sign-in time for idle reclaim, and on the very first
    visit hands out a seat when the tenant enabled auto-assignment and one is still assignable. Returns
    ``(state, source, auto_assigned)``."""
    from beework.api.common import lock_tenant

    state, source = member_seat(db, membership)
    if state == "unassigned":
        row = db.scalar(
            select(PortalSeatAssignment).where(
                PortalSeatAssignment.tenant_id == membership.tenant_id, PortalSeatAssignment.user_id == membership.user_id
            )
        )
        if row is None:  # first visit; serialised so concurrent first sign-ins can never exceed the limit
            lock_tenant(db, membership.tenant_id)
            state, source = member_seat(db, membership)
            row = db.scalar(
                select(PortalSeatAssignment).where(
                    PortalSeatAssignment.tenant_id == membership.tenant_id, PortalSeatAssignment.user_id == membership.user_id
                )
            )
            if state == "unassigned" and row is None:
                db.add(PortalSeatAssignment(tenant_id=membership.tenant_id, user_id=membership.user_id, status="none", excluded=False))
                db.flush()
                if try_auto_assign(db, membership):
                    _touch_login(db, membership)
                    return "assigned", "auto", True
    if state == "assigned":
        _touch_login(db, membership)
    return state, source, False


def _touch_login(db: Session, membership: Membership) -> None:
    now = datetime.now(UTC)
    row = _row(db, membership.tenant_id, membership.user_id)
    last = _aware(row.last_login_at)
    if last is None or now - last > LOGIN_TOUCH_INTERVAL:
        row.last_login_at = now
        db.flush()


def request_seat(db: Session, membership: Membership, reason: str) -> str:
    """The employee asks for a seat. Idempotent: asking again while waiting changes nothing; a seat holder is told so."""
    from beework.api.common import lock_tenant

    reason = reason.strip()
    if len(reason) > REASON_MAX:
        raise ApiError(422, "portal.reason_too_long", f"申请原因不能超过 {REASON_MAX} 字")
    lock_tenant(db, membership.tenant_id)
    state, _source = member_seat(db, membership)
    if state != "unassigned":
        return state
    row = _row(db, membership.tenant_id, membership.user_id)
    row.status, row.request_reason = "requested", reason or None
    row.decided_by = row.decided_at = None
    db.flush()
    user = db.get(User, membership.user_id)
    approvers = [
        member.user_id for member, _user in active_members(db, membership.tenant_id)
        if member.user_id != membership.user_id and membership_can(db, member, "portal:publish")
    ]
    notify(
        db, tenant_id=membership.tenant_id, type="portal.seat_requested", title="有员工申请开通 AI 门户",
        body=f"{user.display_name if user else ''} 申请开通门户席位" + (f"：{reason}" if reason else ""), link="/ai-portal",
        params={"user_id": str(membership.user_id)}, recipient_ids=approvers,
    )
    return "requested"
