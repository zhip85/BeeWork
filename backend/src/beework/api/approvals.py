from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from beework.api.common import lock_tenant
from beework.api.conventions import ApiError, Page, Pagination, check_row_version, get_pagination
from beework.approvals import (
    POLICY_DEFAULTS,
    all_approvers,
    comment,
    current_approvers,
    decide,
    ensure_policies,
    load_for_update,
    multi_level_allowed,
    retry_apply,
    tenant_admins,
    withdraw,
)
from beework.auth import get_console_membership, require_csrf, require_tenant_roles
from beework.db import get_db
from beework.models import AuditEvent, Membership, Organization, User, utc_now
from beework.models.approvals import ApprovalComment, ApprovalRequest
from beework.rbac import require_tenant_permission
from beework.schemas.approvals import (
    ApprovalDetail,
    ApprovalItem,
    ApprovalSummary,
    CommentRequest,
    CommentView,
    DecisionRequest,
    LevelView,
    PolicyItem,
    PolicyListResponse,
    PolicyWriteRequest,
)

router = APIRouter()

# Approving is governed by the approval policy, not by module permissions (PER-001): any active member may call the
# approval endpoints, and what they can see or decide is limited by involvement. ``approvals:view`` only decides
# whether the navigation entry is shown to people who are not involved in any request.
# Tenant administrators and auditors see every request; everyone else sees their own and those awaiting them (6.1).
SEES_ALL_ROLES = ("tenant_admin", "auditor")


def _sees_all(membership: Membership) -> bool:
    return membership.role in SEES_ALL_ROLES


def _visible(membership: Membership, request: ApprovalRequest) -> bool:
    return (
        _sees_all(membership)
        or request.requester_id == membership.user_id
        or membership.user_id in all_approvers(request)
    )


def _names(db: Session, ids: set[UUID]) -> dict[UUID, str]:
    if not ids:
        return {}
    return {u.id: u.display_name for u in db.scalars(select(User).where(User.id.in_(ids)))}


def _org_names(db: Session, tenant_id: UUID, ids: set[UUID]) -> dict[UUID, str]:
    if not ids:
        return {}
    rows = db.execute(select(Organization.id, Organization.name).where(Organization.tenant_id == tenant_id, Organization.id.in_(ids)))
    return {row.id: row.name for row in rows}


def _items(db: Session, membership: Membership, requests: list[ApprovalRequest]) -> list[ApprovalItem]:
    user_ids: set[UUID] = set()
    for request in requests:
        if request.requester_id:
            user_ids.add(request.requester_id)
        user_ids |= all_approvers(request)
    names = _names(db, user_ids)
    orgs = _org_names(db, membership.tenant_id, {r.organization_id for r in requests if r.organization_id})
    admins = set(tenant_admins(db, membership.tenant_id))
    out = []
    for request in requests:
        approvers = current_approvers(request)
        out.append(
            ApprovalItem(
                id=request.id, title=request.title, type=request.type, policy_event=request.policy_event,
                object_type=request.object_type, object_id=request.object_id,
                organization_id=request.organization_id,
                organization_name=orgs.get(request.organization_id, "") if request.organization_id else "",
                requester_id=request.requester_id, requester_name=names.get(request.requester_id, ""),
                status=request.status, apply_status=request.apply_status,
                current_level=request.current_level, level_count=len(request.levels),
                approver_names=[names.get(u, "") for u in approvers], checks=request.checks,
                submitted_at=request.submitted_at, decided_at=request.decided_at,
                can_decide=membership.user_id in approvers and membership.user_id != request.requester_id,
                can_withdraw=request.status == "pending" and request.requester_id == membership.user_id,
                can_retry=request.status == "approved" and request.apply_status == "failed"
                and (request.requester_id == membership.user_id or membership.user_id in admins),
            )
        )
    return out


def _detail(db: Session, membership: Membership, request: ApprovalRequest) -> ApprovalDetail:
    item = _items(db, membership, [request])[0]
    names = _names(db, {UUID(lv["decided_by"]) for lv in request.levels if lv.get("decided_by")} | all_approvers(request))
    levels = [
        LevelView(
            level=lv["level"], approver_role=lv["approver_role"],
            approvers=[names.get(UUID(u), "") for u in lv["approvers"]], status=lv["status"],
            decided_by=names.get(UUID(lv["decided_by"])) if lv.get("decided_by") else None,
            comment=lv.get("comment") or "",
            decided_at=datetime.fromisoformat(lv["decided_at"]) if lv.get("decided_at") else None,
        )
        for lv in request.levels
    ]
    comments = db.scalars(
        select(ApprovalComment).where(ApprovalComment.approval_id == request.id).order_by(ApprovalComment.created_at)
    )
    return ApprovalDetail(
        **item.model_dump(), description=request.description, change_diff=request.change_diff, levels=levels,
        comments=[CommentView(id=c.id, author_name=c.author_name, body=c.body, created_at=c.created_at) for c in comments],
        apply_error=request.apply_error,
    )


def _get_visible(db: Session, membership: Membership, approval_id: UUID, *, lock: bool = False) -> ApprovalRequest:
    request = (
        load_for_update(db, membership.tenant_id, approval_id)
        if lock
        else db.scalar(select(ApprovalRequest).where(ApprovalRequest.id == approval_id, ApprovalRequest.tenant_id == membership.tenant_id))
    )
    if request is None or not _visible(membership, request):
        raise ApiError(404, "not_found", "未找到指定审批单")
    return request


def _tenant_requests(db: Session, tenant_id: UUID) -> list[ApprovalRequest]:
    return list(db.scalars(select(ApprovalRequest).where(ApprovalRequest.tenant_id == tenant_id)))


# ---------------------------------------------------------------------------
# Approval center (APR-010–013).
# ---------------------------------------------------------------------------


@router.get("/approvals/summary", response_model=ApprovalSummary)
def approvals_summary(
    membership: Membership = Depends(get_console_membership),
    db: Session = Depends(get_db),
) -> ApprovalSummary:
    visible = [r for r in _tenant_requests(db, membership.tenant_id) if _visible(membership, r)]
    now = utc_now()
    done = [
        r for r in visible
        if r.status in ("approved", "rejected") and r.decided_at
        and (r.decided_at.year, r.decided_at.month) == (now.year, now.month)
    ]
    durations = [(r.decided_at - r.submitted_at).total_seconds() for r in done]
    return ApprovalSummary(
        pending_mine=sum(membership.user_id in current_approvers(r) for r in visible),
        submitted=sum(r.requester_id == membership.user_id for r in visible),
        visible_count=len(visible),
        completed_this_month=len(done),
        avg_duration_seconds=int(sum(durations) / len(durations)) if durations else None,
    )


@router.get("/approvals", response_model=Page[ApprovalItem])
def list_approvals(
    scope: Literal["pending_mine", "submitted", "all"] = "pending_mine",
    status: Literal["pending", "approved", "rejected", "withdrawn", "expired"] | None = None,
    type: Literal["publish", "capability_access", "data_export", "rule_change"] | None = None,
    organization_id: UUID | None = None,
    q: str | None = Query(default=None, max_length=100),
    sort: Literal["oldest", "newest"] = "oldest",
    pagination: Pagination = Depends(get_pagination),
    membership: Membership = Depends(get_console_membership),
    db: Session = Depends(get_db),
) -> Page[ApprovalItem]:
    rows = [r for r in _tenant_requests(db, membership.tenant_id) if _visible(membership, r)]
    if scope == "pending_mine":
        rows = [r for r in rows if membership.user_id in current_approvers(r)]
    elif scope == "submitted":
        rows = [r for r in rows if r.requester_id == membership.user_id]
    if status:
        rows = [r for r in rows if r.status == status]
    if type:
        rows = [r for r in rows if r.type == type]
    if organization_id:
        rows = [r for r in rows if r.organization_id == organization_id]
    if q and q.strip():
        needle = q.strip().lower()
        requester_names = _names(db, {r.requester_id for r in rows if r.requester_id})
        rows = [r for r in rows if needle in r.title.lower() or needle in requester_names.get(r.requester_id, "").lower()]
    rows.sort(key=lambda r: r.submitted_at, reverse=sort == "newest")
    page = rows[pagination.offset : pagination.offset + pagination.limit]
    return Page[ApprovalItem](items=_items(db, membership, page), total=len(rows), limit=pagination.limit, offset=pagination.offset)


@router.get("/approvals/{approval_id}", response_model=ApprovalDetail)
def get_approval(
    approval_id: UUID,
    membership: Membership = Depends(get_console_membership),
    db: Session = Depends(get_db),
) -> ApprovalDetail:
    return _detail(db, membership, _get_visible(db, membership, approval_id))


def _act(db: Session, membership: Membership, approval_id: UUID, action) -> ApprovalDetail:
    request = _get_visible(db, membership, approval_id, lock=True)
    action(request)
    db.commit()
    db.refresh(request)
    return _detail(db, membership, request)


@router.post("/approvals/{approval_id}/approve", response_model=ApprovalDetail)
def approve(
    approval_id: UUID,
    payload: DecisionRequest | None = None,
    membership: Membership = Depends(get_console_membership),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> ApprovalDetail:
    return _act(db, membership, approval_id, lambda r: decide(db, r, membership.user_id, approve=True, comment=(payload.comment if payload else "")))


@router.post("/approvals/{approval_id}/reject", response_model=ApprovalDetail)
def reject(
    approval_id: UUID,
    payload: DecisionRequest,
    membership: Membership = Depends(get_console_membership),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> ApprovalDetail:
    return _act(db, membership, approval_id, lambda r: decide(db, r, membership.user_id, approve=False, comment=payload.comment))


@router.post("/approvals/{approval_id}/withdraw", response_model=ApprovalDetail)
def withdraw_approval(
    approval_id: UUID,
    membership: Membership = Depends(get_console_membership),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> ApprovalDetail:
    return _act(db, membership, approval_id, lambda r: withdraw(db, r, membership.user_id))


@router.post("/approvals/{approval_id}/comment", response_model=ApprovalDetail)
def comment_approval(
    approval_id: UUID,
    payload: CommentRequest,
    membership: Membership = Depends(get_console_membership),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> ApprovalDetail:
    return _act(db, membership, approval_id, lambda r: comment(db, r, membership.user_id, payload.body))


@router.post("/approvals/{approval_id}/retry", response_model=ApprovalDetail)
def retry_approval(
    approval_id: UUID,
    membership: Membership = Depends(get_console_membership),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> ApprovalDetail:
    def action(request: ApprovalRequest) -> None:
        if request.requester_id != membership.user_id and membership.role != "tenant_admin":
            raise ApiError(403, "auth.forbidden", "只有申请人或租户管理员可以重试")
        retry_apply(db, request, membership.user_id)

    return _act(db, membership, approval_id, action)


# ---------------------------------------------------------------------------
# Approval policies (SET-071).
# ---------------------------------------------------------------------------


def _policy_response(db: Session, membership: Membership) -> PolicyListResponse:
    policies = ensure_policies(db, membership.tenant_id)
    pending = sum(r.status == "pending" for r in _tenant_requests(db, membership.tenant_id))
    return PolicyListResponse(
        items=[
            PolicyItem(
                event=event, request_type=POLICY_DEFAULTS[event][0], approver_role=policies[event].approver_role,
                levels=policies[event].levels, enabled=policies[event].enabled, row_version=policies[event].row_version,
            )
            for event in POLICY_DEFAULTS
        ],
        pending_count=pending,
        multi_level_allowed=multi_level_allowed(db, membership.tenant_id),
    )


@router.get("/tenant/approval-policies", response_model=PolicyListResponse)
def get_policies(
    membership: Membership = Depends(require_tenant_permission("settings:view")),
    db: Session = Depends(get_db),
) -> PolicyListResponse:
    response = _policy_response(db, membership)
    db.commit()  # persists lazily created default rows
    return response


@router.put("/tenant/approval-policies", response_model=PolicyListResponse)
def update_policies(
    payload: PolicyWriteRequest,
    membership: Membership = Depends(require_tenant_roles("tenant_admin")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> PolicyListResponse:
    lock_tenant(db, membership.tenant_id)
    policies = ensure_policies(db, membership.tenant_id)
    allow_multi = multi_level_allowed(db, membership.tenant_id)
    events = [item.event for item in payload.items]
    if len(set(events)) != len(events):
        raise ApiError(422, "validation.invalid", "同一事项不能重复提交")
    for item in payload.items:
        policy = policies[item.event]
        if item.row_version is not None:
            check_row_version(policy, item.row_version, db)
        if item.levels == 2 and not allow_multi and policy.levels != 2:
            raise ApiError(403, "plan.feature_locked", "两级审批仅企业版可用，请联系平台升级")
        before = (policy.approver_role, policy.levels, policy.enabled)
        after = (item.approver_role, item.levels, item.enabled)
        if before == after:
            continue
        policy.approver_role, policy.levels, policy.enabled = after
        policy.row_version += 1
        policy.updated_by = str(membership.user_id)
        db.add(
            AuditEvent(
                tenant_id=membership.tenant_id, actor_user_id=membership.user_id, action="approval.policy_updated",
                details={
                    "event": item.event,
                    "before": f"{before[0]}/{before[1]}/{'on' if before[2] else 'off'}",
                    "after": f"{after[0]}/{after[1]}/{'on' if after[2] else 'off'}",
                },
            )
        )
    db.flush()
    response = _policy_response(db, membership)
    db.commit()
    return response
