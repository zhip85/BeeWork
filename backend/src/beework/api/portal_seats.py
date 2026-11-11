"""AI portal seats — tenant-side assignment (T-150, 演进 PRD 5.2、13.4)."""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from beework import portal_seat_service as seats
from beework.api.subscription import portal_response
from beework.auth import require_csrf
from beework.db import get_db
from beework.entitlements import get_subscription, portal_grant, seat_usage
from beework.models import Membership, Organization
from beework.models.portal_seats import PortalSeatRule
from beework.rbac import organization_subtree, require_tenant_permission
from beework.schemas.portal_seats import (
    SeatBatchRequest,
    SeatChangeResponse,
    SeatMember,
    SeatMemberPage,
    SeatPolicyBody,
    SeatReclaimResponse,
    SeatRuleOrganization,
    SeatRulesResponse,
    SeatRulesUpdate,
    SeatSummary,
)

router = APIRouter()


def _summary(db: Session, tenant_id: UUID) -> SeatSummary:
    grant = portal_grant(db, tenant_id)
    assigned = seats.assigned_count(db, tenant_id)
    limit = grant.assignable_limit
    rows = seats.assignment_rows(db, tenant_id)
    subscription = get_subscription(db, tenant_id)
    return SeatSummary(
        grant=portal_response(grant),
        assigned=assigned,
        remaining=None if limit is None else max(limit - assigned, 0),
        requested=sum(1 for row in rows.values() if row.status == "requested"),
        idle=len(seats.idle_user_ids(db, tenant_id)),
        over_contract=grant.seats is not None and assigned > grant.seats,
        console_seats_used=seat_usage(db, tenant_id),
        console_seats_limit=subscription.quotas.get("seats"),
    )


@router.get("/portal/seats/summary", response_model=SeatSummary)
def seat_summary(
    membership: Membership = Depends(require_tenant_permission("portal:view")), db: Session = Depends(get_db),
) -> SeatSummary:
    result = _summary(db, membership.tenant_id)
    db.commit()  # persists a lazily created policy row / subscription snapshot
    return result


@router.get("/portal/seats/members", response_model=SeatMemberPage)
def seat_members(
    q: str | None = Query(default=None, max_length=100),
    filter: str | None = Query(default=None, pattern="^(assigned|unassigned|requested|idle)$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    membership: Membership = Depends(require_tenant_permission("portal:view")),
    db: Session = Depends(get_db),
) -> SeatMemberPage:
    tenant_id = membership.tenant_id
    scope, rows = seats.rule_scope(db, tenant_id), seats.assignment_rows(db, tenant_id)
    idle = set(seats.idle_user_ids(db, tenant_id))
    organizations = {org.id: org.name for org in db.scalars(select(Organization).where(Organization.tenant_id == tenant_id))}
    items: list[SeatMember] = []
    needle = (q or "").strip().lower()
    for member, user in seats.active_members(db, tenant_id):
        row = rows.get(member.user_id)
        source = seats.seat_source(member, row, scope)
        state = "assigned" if source else ("requested" if row is not None and row.status == "requested" else "unassigned")
        if needle and needle not in user.display_name.lower() and needle not in user.email.lower():
            continue
        if filter == "idle" and member.user_id not in idle:
            continue
        if filter in ("assigned", "unassigned", "requested") and state != filter:
            continue
        items.append(
            SeatMember(
                user_id=member.user_id, display_name=user.display_name, email=user.email,
                organization_id=member.organization_id, organization_name=organizations.get(member.organization_id, ""),
                role=member.role, state=state, source=source,
                reason=row.request_reason if row is not None and state == "requested" else None,
                last_login_at=row.last_login_at if row is not None else None,
                assigned_at=row.assigned_at if row is not None and source in ("manual", "auto") else None,
                idle=member.user_id in idle,
            )
        )
    order = {"requested": 0, "assigned": 1, "unassigned": 2}
    items.sort(key=lambda item: (order[item.state], item.display_name.lower(), str(item.user_id)))
    start = (page - 1) * page_size
    return SeatMemberPage(items=items[start:start + page_size], total=len(items), page=page, page_size=page_size)


def _change(db: Session, tenant_id: UUID, changed: int, notice: str | None = None) -> SeatChangeResponse:
    db.commit()
    return SeatChangeResponse(changed=changed, notice=notice, summary=_summary(db, tenant_id))


@router.post("/portal/seats/assign", response_model=SeatChangeResponse)
def assign_seats(
    payload: SeatBatchRequest,
    membership: Membership = Depends(require_tenant_permission("portal:publish")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> SeatChangeResponse:
    from beework.api.common import lock_tenant

    lock_tenant(db, membership.tenant_id)
    changed, notice = seats.assign(db, membership.tenant_id, payload.user_ids, membership.user_id)
    return _change(db, membership.tenant_id, changed, notice)


@router.post("/portal/seats/revoke", response_model=SeatChangeResponse)
def revoke_seats(
    payload: SeatBatchRequest,
    membership: Membership = Depends(require_tenant_permission("portal:publish")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> SeatChangeResponse:
    from beework.api.common import lock_tenant

    lock_tenant(db, membership.tenant_id)
    return _change(db, membership.tenant_id, seats.revoke(db, membership.tenant_id, payload.user_ids, membership.user_id))


@router.post("/portal/seats/approve", response_model=SeatChangeResponse)
def approve_seats(
    payload: SeatBatchRequest,
    membership: Membership = Depends(require_tenant_permission("portal:publish")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> SeatChangeResponse:
    from beework.api.common import lock_tenant

    lock_tenant(db, membership.tenant_id)
    changed, notice = seats.approve(db, membership.tenant_id, payload.user_ids, membership.user_id)
    return _change(db, membership.tenant_id, changed, notice)


@router.post("/portal/seats/reject", response_model=SeatChangeResponse)
def reject_seats(
    payload: SeatBatchRequest,
    membership: Membership = Depends(require_tenant_permission("portal:publish")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> SeatChangeResponse:
    from beework.api.common import lock_tenant

    lock_tenant(db, membership.tenant_id)
    return _change(db, membership.tenant_id, seats.reject(db, membership.tenant_id, payload.user_ids, membership.user_id))


@router.get("/portal/seats/rules", response_model=SeatRulesResponse)
def get_rules(
    membership: Membership = Depends(require_tenant_permission("portal:view")), db: Session = Depends(get_db),
) -> SeatRulesResponse:
    tenant_id = membership.tenant_id
    enabled = set(db.scalars(select(PortalSeatRule.organization_id).where(PortalSeatRule.tenant_id == tenant_id)))
    members = seats.active_members(db, tenant_id)
    result = []
    for org in db.scalars(select(Organization).where(Organization.tenant_id == tenant_id).order_by(Organization.name, Organization.id)):
        subtree = set(organization_subtree(db, tenant_id, org.id))
        result.append(
            SeatRuleOrganization(
                organization_id=org.id, name=org.name, parent_id=org.parent_id,
                member_count=sum(1 for member, _user in members if member.organization_id in subtree), enabled=org.id in enabled,
            )
        )
    return SeatRulesResponse(organizations=result)


@router.put("/portal/seats/rules", response_model=SeatChangeResponse)
def put_rules(
    payload: SeatRulesUpdate,
    membership: Membership = Depends(require_tenant_permission("portal:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> SeatChangeResponse:
    from beework.api.common import lock_tenant

    lock_tenant(db, membership.tenant_id)
    notice = seats.set_rules(db, membership.tenant_id, payload.organization_ids, membership.user_id)
    return _change(db, membership.tenant_id, len(payload.organization_ids), notice)


@router.get("/portal/seats/policy", response_model=SeatPolicyBody)
def get_policy(
    membership: Membership = Depends(require_tenant_permission("portal:view")), db: Session = Depends(get_db),
) -> SeatPolicyBody:
    policy = seats.get_policy(db, membership.tenant_id)
    result = SeatPolicyBody(auto_assign=policy.auto_assign, idle_days=policy.idle_days)  # type: ignore[arg-type]
    db.commit()
    return result


@router.put("/portal/seats/policy", response_model=SeatPolicyBody)
def put_policy(
    payload: SeatPolicyBody,
    membership: Membership = Depends(require_tenant_permission("portal:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> SeatPolicyBody:
    policy = seats.update_policy(db, membership.tenant_id, payload.auto_assign, payload.idle_days, membership.user_id)
    result = SeatPolicyBody(auto_assign=policy.auto_assign, idle_days=policy.idle_days)  # type: ignore[arg-type]
    db.commit()
    return result


@router.post("/portal/seats/reclaim-idle", response_model=SeatReclaimResponse)
def reclaim_idle(
    membership: Membership = Depends(require_tenant_permission("portal:publish")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> SeatReclaimResponse:
    from beework.api.common import lock_tenant

    lock_tenant(db, membership.tenant_id)
    reclaimed = seats.reclaim_idle(db, membership.tenant_id, membership.user_id, now=datetime.now(UTC))
    db.commit()
    return SeatReclaimResponse(reclaimed=reclaimed, summary=_summary(db, membership.tenant_id))
