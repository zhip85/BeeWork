"""Generic approval engine (T-022): policies, approver resolution, multi-level flow, apply and expiry.

Any controlled change calls ``submit_change``. When the tenant's policy for the event is off, the registered
action runs immediately; otherwise an approval request is created and the action runs from the worker once
every level approved (APR-023). Action executors register with ``@approval_action("assistant.publish")``.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from beework.entitlements import entitlement_value
from beework.errors import ApiError
from beework.models import AuditEvent, Membership, TenantRole, User, utc_now
from beework.models.approvals import ApprovalComment, ApprovalPolicy, ApprovalRequest
from beework.worker.queue import enqueue, job_handler

logger = logging.getLogger(__name__)

# event -> (request type, default approver role, default levels, default enabled); PRD 3.9.1 and SET-071.
POLICY_DEFAULTS: dict[str, tuple[str, str, int, bool]] = {
    "publish": ("publish", "tenant_admin", 1, True),
    "channel": ("publish", "security_officer", 1, True),
    "byok": ("rule_change", "security_officer", 2, True),
    "kb": ("publish", "knowledge_owner", 1, False),
    "skill": ("capability_access", "tenant_admin", 1, True),
    "rule": ("rule_change", "security_officer", 1, True),
    "export": ("data_export", "tenant_admin", 1, False),
}
APPROVER_ROLES = ("tenant_admin", "security_officer", "knowledge_owner", "assistant_owner")
APPROVER_TAGS = ("security_officer", "knowledge_owner")
REQUEST_TYPES = ("publish", "capability_access", "data_export", "rule_change")
EXPIRE_AFTER = timedelta(days=7)  # APR-024
REMIND_AFTER = timedelta(hours=24)


@dataclass
class ApplyContext:
    tenant_id: UUID
    object_type: str
    object_id: UUID
    payload: dict[str, Any]
    requester_id: UUID | None
    approval_id: UUID | None  # None when the policy is off and the change is applied directly


ActionExecutor = Callable[[Session, ApplyContext], None]
ACTIONS: dict[str, ActionExecutor] = {}
# Notification hooks (T-023 registers the in-app notifier): (db, event, request, recipient user ids).
Notifier = Callable[[Session, str, ApprovalRequest, list[UUID]], None]
NOTIFIERS: list[Notifier] = []


def approval_action(name: str) -> Callable[[ActionExecutor], ActionExecutor]:
    def register(executor: ActionExecutor) -> ActionExecutor:
        if name in ACTIONS:
            raise RuntimeError(f"Approval action already registered: {name}")
        ACTIONS[name] = executor
        return executor

    return register


@dataclass
class SubmitResult:
    approval: ApprovalRequest | None
    applied_directly: bool


# ---------------------------------------------------------------------------
# Policies.
# ---------------------------------------------------------------------------


def ensure_policies(db: Session, tenant_id: UUID) -> dict[str, ApprovalPolicy]:
    """The tenant's seven policies, created with the PRD defaults the first time they are needed."""
    rows = {p.event: p for p in db.scalars(select(ApprovalPolicy).where(ApprovalPolicy.tenant_id == tenant_id))}
    for event, (_, role, levels, enabled) in POLICY_DEFAULTS.items():
        if event in rows:
            continue
        policy = ApprovalPolicy(tenant_id=tenant_id, event=event, approver_role=role, levels=levels, enabled=enabled)
        try:
            with db.begin_nested():
                db.add(policy)
                db.flush()
        except IntegrityError:  # another transaction created it first
            policy = db.scalar(
                select(ApprovalPolicy).where(ApprovalPolicy.tenant_id == tenant_id, ApprovalPolicy.event == event)
            )
        rows[event] = policy
    return rows


def multi_level_allowed(db: Session, tenant_id: UUID) -> bool:
    return entitlement_value(db, tenant_id, "approval_levels") == "multi"


# ---------------------------------------------------------------------------
# Approver resolution (APR-003, PER-001).
# ---------------------------------------------------------------------------


def _active_members(db: Session, tenant_id: UUID):
    return select(Membership.user_id).where(Membership.tenant_id == tenant_id, Membership.status == "active")


def tenant_admins(db: Session, tenant_id: UUID) -> list[UUID]:
    return list(db.scalars(_active_members(db, tenant_id).where(Membership.role == "tenant_admin")))


def resolve_approvers(
    db: Session, tenant_id: UUID, role: str, *, requester_id: UUID | None, owner_user_id: UUID | None = None
) -> list[UUID]:
    """Users who may decide a level. The requester never approves their own request; an empty set escalates
    to the tenant administrators, and a request nobody else could approve is refused."""
    found: list[UUID] = []
    if role == "tenant_admin":
        found = tenant_admins(db, tenant_id)
    elif role in APPROVER_TAGS:
        roles = db.scalars(select(TenantRole).where(TenantRole.tenant_id == tenant_id, TenantRole.is_active.is_(True)))
        role_ids = [r.id for r in roles if role in (r.approver_tags or [])]
        if role_ids:
            found = list(db.scalars(_active_members(db, tenant_id).where(Membership.custom_role_id.in_(role_ids))))
    elif role == "assistant_owner" and owner_user_id is not None:
        found = list(db.scalars(_active_members(db, tenant_id).where(Membership.user_id == owner_user_id)))
    approvers = [user_id for user_id in dict.fromkeys(found) if user_id != requester_id]
    if not approvers:
        approvers = [user_id for user_id in tenant_admins(db, tenant_id) if user_id != requester_id]
    if not approvers:
        raise ApiError(409, "approval.no_approver", "没有其他可审批的人，请邀请另一位租户管理员，或在审批策略中关闭该事项")
    return approvers


# ---------------------------------------------------------------------------
# Audit and notification helpers.
# ---------------------------------------------------------------------------


def _audit(db: Session, request: ApprovalRequest, action: str, actor_id: UUID | None, **extra: str) -> None:
    db.add(
        AuditEvent(
            tenant_id=request.tenant_id,
            actor_user_id=actor_id,
            action=action,
            details={"approval_id": str(request.id), "title": request.title, "type": request.type, **extra},
        )
    )


def _notify(db: Session, event: str, request: ApprovalRequest, recipients: list[UUID]) -> None:
    for notifier in NOTIFIERS:
        notifier(db, event, request, [r for r in recipients if r is not None])


def current_approvers(request: ApprovalRequest) -> list[UUID]:
    if request.status != "pending" or not 1 <= request.current_level <= len(request.levels):
        return []
    return [UUID(item) for item in request.levels[request.current_level - 1]["approvers"]]


def all_approvers(request: ApprovalRequest) -> set[UUID]:
    return {UUID(item) for level in request.levels for item in level["approvers"]}


# ---------------------------------------------------------------------------
# Submit.
# ---------------------------------------------------------------------------


def submit_change(
    db: Session,
    *,
    tenant_id: UUID,
    event: str,
    action: str,
    object_type: str,
    object_id: UUID,
    title: str,
    payload: dict[str, Any] | None = None,
    change_diff: list | None = None,
    checks: list | None = None,
    description: str = "",
    requester_id: UUID | None,
    organization_id: UUID | None = None,
    owner_user_id: UUID | None = None,
    replace_pending: bool = False,
) -> SubmitResult:
    """Apply the change now when the policy is off (APR-001), otherwise open an approval request."""
    if event not in POLICY_DEFAULTS:
        raise ValueError(f"Unknown approval event: {event}")
    payload = payload or {}
    policy = ensure_policies(db, tenant_id)[event]
    request_type = POLICY_DEFAULTS[event][0]
    if not policy.enabled:
        executor = ACTIONS.get(action)
        if executor is None:
            raise ApiError(500, "approval.unknown_action", f"未注册的审批动作：{action}")
        executor(db, ApplyContext(tenant_id, object_type, object_id, payload, requester_id, None))
        # A request left over from before the policy was switched off describes an older state of the object;
        # approving it later would overwrite this change, so it is withdrawn as superseded.
        superseded = _pending_for(db, tenant_id, object_type, object_id, request_type)
        if superseded is not None:
            _withdraw(db, superseded, requester_id)
            _notify(db, "approval.withdrawn", superseded, [superseded.requester_id])
        return SubmitResult(None, True)

    existing = _pending_for(db, tenant_id, object_type, object_id, request_type)
    if existing is not None:
        if not replace_pending:
            raise ApiError(409, "approval.pending_exists", "已有待审批的申请，是否撤回并重新提交？")
        _withdraw(db, existing, requester_id)
        db.flush()

    levels = [{"level": 1, "approver_role": policy.approver_role}]
    if policy.levels >= 2:
        levels.append({"level": 2, "approver_role": "tenant_admin"})  # the second level is always the tenant admin
    for level in levels:
        approvers = resolve_approvers(
            db, tenant_id, level["approver_role"], requester_id=requester_id, owner_user_id=owner_user_id
        )
        level.update(
            approvers=[str(user_id) for user_id in approvers],
            status="pending", decided_by=None, comment="", decided_at=None,
        )
    request = ApprovalRequest(
        tenant_id=tenant_id,
        title=title[:200],
        type=request_type,
        policy_event=event,
        object_type=object_type,
        object_id=object_id,
        organization_id=organization_id,
        description=description[:500],
        change_diff=change_diff or [],
        checks=checks or [],
        action=action,
        payload=payload,
        requester_id=requester_id,
        levels=levels,
        current_level=1,
    )
    try:
        with db.begin_nested():
            db.add(request)
            db.flush()
    except IntegrityError:  # lost the race against another submission of the same object
        raise ApiError(409, "approval.pending_exists", "已有待审批的申请，是否撤回并重新提交？") from None
    _audit(db, request, "approval.submitted", requester_id)
    _notify(db, "approval.submitted", request, current_approvers(request))
    return SubmitResult(request, False)


def _pending_for(db: Session, tenant_id: UUID, object_type: str, object_id: UUID, request_type: str):
    return db.scalar(
        select(ApprovalRequest).where(
            ApprovalRequest.tenant_id == tenant_id,
            ApprovalRequest.object_type == object_type,
            ApprovalRequest.object_id == object_id,
            ApprovalRequest.type == request_type,
            ApprovalRequest.status == "pending",
        )
    )


def pending_request(db: Session, tenant_id: UUID, object_type: str, object_id: UUID) -> ApprovalRequest | None:
    return db.scalar(
        select(ApprovalRequest).where(
            ApprovalRequest.tenant_id == tenant_id,
            ApprovalRequest.object_type == object_type,
            ApprovalRequest.object_id == object_id,
            ApprovalRequest.status == "pending",
        )
    )


def has_pending(db: Session, tenant_id: UUID, object_type: str, object_id: UUID) -> bool:
    """APR-002: whether the object shows as “审批中”."""
    return (
        db.scalar(
            select(ApprovalRequest.id).where(
                ApprovalRequest.tenant_id == tenant_id,
                ApprovalRequest.object_type == object_type,
                ApprovalRequest.object_id == object_id,
                ApprovalRequest.status == "pending",
            )
        )
        is not None
    )


# ---------------------------------------------------------------------------
# Decisions.
# ---------------------------------------------------------------------------


def load_for_update(db: Session, tenant_id: UUID, approval_id: UUID) -> ApprovalRequest:
    request = db.scalar(
        select(ApprovalRequest)
        .where(ApprovalRequest.id == approval_id, ApprovalRequest.tenant_id == tenant_id)
        .with_for_update()
    )
    if request is None:
        raise ApiError(404, "not_found", "未找到指定审批单")
    return request


def decide(db: Session, request: ApprovalRequest, actor_id: UUID, *, approve: bool, comment: str = "") -> ApprovalRequest:
    """Record the current level's decision. The caller holds the row lock (``load_for_update``)."""
    if request.status != "pending":
        raise ApiError(409, "approval.not_pending", "该审批单已处理，请刷新")
    if actor_id == request.requester_id or actor_id not in current_approvers(request):
        raise ApiError(403, "approval.not_approver", "你不是当前级别的审批人")
    comment = comment.strip()
    if not approve and not comment:
        raise ApiError(422, "validation.invalid", "驳回必须填写意见")
    now = utc_now()
    levels = [dict(level) for level in request.levels]
    current = levels[request.current_level - 1]
    current.update(status="approved" if approve else "rejected", decided_by=str(actor_id), comment=comment, decided_at=now.isoformat())
    request.levels = levels
    if comment:
        _add_comment(db, request, actor_id, comment)
    if not approve:
        request.status = "rejected"
        request.decided_at = now
        _audit(db, request, "approval.rejected", actor_id, level=str(request.current_level))
        _notify(db, "approval.rejected", request, [request.requester_id])
    elif request.current_level < len(levels):
        _audit(db, request, "approval.level_approved", actor_id, level=str(request.current_level))
        request.current_level += 1
        _notify(db, "approval.submitted", request, current_approvers(request))
    else:
        request.status = "approved"
        request.decided_at = now
        request.apply_status = "pending"
        _audit(db, request, "approval.approved", actor_id)
        _enqueue_apply(db, request)
        _notify(db, "approval.approved", request, [request.requester_id])
    return request


def _enqueue_apply(db: Session, request: ApprovalRequest) -> None:
    enqueue(
        db, "approvals.apply", {"approval_id": str(request.id)},
        tenant_id=request.tenant_id, idempotency_key=f"approvals.apply:{request.id}:{request.apply_attempts}",
    )


def _withdraw(db: Session, request: ApprovalRequest, actor_id: UUID | None) -> None:
    request.status = "withdrawn"
    request.decided_at = utc_now()
    _audit(db, request, "approval.withdrawn", actor_id)


def withdraw(db: Session, request: ApprovalRequest, actor_id: UUID) -> ApprovalRequest:
    if request.status != "pending":
        raise ApiError(409, "approval.not_pending", "该审批单已处理，请刷新")
    if actor_id != request.requester_id:
        raise ApiError(403, "auth.forbidden", "只有申请人可以撤回")
    _withdraw(db, request, actor_id)
    return request


def _add_comment(db: Session, request: ApprovalRequest, author_id: UUID | None, body: str) -> None:
    user = db.get(User, author_id) if author_id else None
    db.add(
        ApprovalComment(
            tenant_id=request.tenant_id, approval_id=request.id, author_id=author_id,
            author_name=user.display_name if user else "", body=body.strip(),
        )
    )


def comment(db: Session, request: ApprovalRequest, author_id: UUID, body: str) -> None:
    if not body.strip():
        raise ApiError(422, "validation.invalid", "评论不能为空")
    _add_comment(db, request, author_id, body)


def retry_apply(db: Session, request: ApprovalRequest, actor_id: UUID) -> ApprovalRequest:
    if request.status != "approved" or request.apply_status != "failed":
        raise ApiError(409, "approval.not_retryable", "只有生效失败的审批单可以重试")
    request.apply_status = "pending"
    request.apply_error = None
    request.apply_attempts += 1
    _audit(db, request, "approval.apply_retried", actor_id)
    _enqueue_apply(db, request)
    return request


# ---------------------------------------------------------------------------
# Worker jobs.
# ---------------------------------------------------------------------------


@job_handler("approvals.apply")
def apply_approved(db: Session, payload: dict[str, Any]) -> None:
    request = db.scalar(
        select(ApprovalRequest).where(ApprovalRequest.id == UUID(payload["approval_id"])).with_for_update()
    )
    if request is None or request.status != "approved" or request.apply_status != "pending":
        return
    executor = ACTIONS.get(request.action)
    try:
        if executor is None:
            raise RuntimeError(f"未注册的审批动作：{request.action}")
        with db.begin_nested():
            executor(
                db,
                ApplyContext(request.tenant_id, request.object_type, request.object_id, request.payload,
                             request.requester_id, request.id),
            )
    except Exception as exc:  # noqa: BLE001 - recorded on the request so it can be retried (APR-023)
        logger.exception("Approval %s failed to apply", request.id)
        request.apply_status = "failed"
        request.apply_error = str(exc)[:2000]
        _audit(db, request, "approval.apply_failed", None, error=request.apply_error[:300])
        _notify(db, "approval.apply_failed", request, [request.requester_id, *tenant_admins(db, request.tenant_id)])
        return
    request.apply_status = "succeeded"
    request.apply_error = None
    _audit(db, request, "approval.applied", None)


def sweep(db: Session, *, now: datetime | None = None) -> None:
    """Expire requests pending past 7 days and remind approvers once after 24 hours (APR-024)."""
    current = now or datetime.now(UTC)
    pending = list(db.scalars(select(ApprovalRequest).where(ApprovalRequest.status == "pending").with_for_update()))
    for request in pending:
        submitted = request.submitted_at if request.submitted_at.tzinfo else request.submitted_at.replace(tzinfo=UTC)
        age = current - submitted
        if age >= EXPIRE_AFTER:
            request.status = "expired"
            request.decided_at = current
            _audit(db, request, "approval.expired", None)
            _notify(db, "approval.expired", request, [request.requester_id])
        elif age >= REMIND_AFTER and request.reminded_at is None:
            request.reminded_at = current
            _notify(db, "approval.reminder", request, current_approvers(request))


@job_handler("approvals.sweep")
def sweep_job(db: Session, _payload: dict[str, Any]) -> None:
    sweep(db)
