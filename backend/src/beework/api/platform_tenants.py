from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from beework.api.conventions import require_platform_admin
from beework.auth import (
    require_csrf,
)
from beework.db import get_db
from beework.models import (
    AuditEvent,
    AuthSession,
    Membership,
    Organization,
    PlatformOperationRequest,
    Tenant,
    User,
)
from beework.schemas import (
    CreateTenantRequest,
    PlatformOperationResponse,
    PlatformOverviewResponse,
    PlatformTenantResponse,
    ReviewPlatformOperationRequest,
    TenantLifecycleActionOption,
    TenantLifecycleRequest,
    TenantLifecycleResponse,
)
from beework.security import (
    hash_password,
)
from beework.tenant_lifecycle import (
    LIFECYCLE_ACTIONS,
    REVIEW_TIMEOUT,
    TRIAL_EXTENSION,
    lifecycle_action,
)

router = APIRouter()


@router.post(
    "/platform/tenants",
    response_model=PlatformTenantResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_tenant(
    payload: CreateTenantRequest,
    user: User = Depends(require_platform_admin),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> PlatformTenantResponse:
    existing = db.scalar(select(Tenant).where(Tenant.slug == payload.slug))
    if existing:
        raise HTTPException(status_code=409, detail="企业标识已被使用")
    admin_email = str(payload.admin_email).strip().lower()
    tenant_admin = db.scalar(select(User).where(User.email == admin_email))
    if tenant_admin:
        raise HTTPException(status_code=409, detail="企业管理员邮箱已注册，请使用其他邮箱")
    tenant = Tenant(
        name=payload.name,
        slug=payload.slug,
        seat_limit=payload.seat_limit,
        plan=payload.plan,
        region=payload.region,
    )
    db.add(tenant)
    db.flush()
    root_organization = Organization(tenant_id=tenant.id, name=tenant.name)
    db.add(root_organization)
    db.flush()
    tenant_admin = User(
        email=admin_email,
        display_name=payload.admin_name,
        password_hash=hash_password(payload.admin_password),
        is_platform_admin=False,
    )
    db.add(tenant_admin)
    db.flush()
    db.add(
        Membership(
            tenant_id=tenant.id,
            user_id=tenant_admin.id,
            organization_id=root_organization.id,
            role="tenant_admin",
        )
    )
    db.add(
        AuditEvent(
            tenant_id=tenant.id,
            actor_user_id=user.id,
            action="tenant.created",
            details={
                "slug": tenant.slug,
                "tenant_admin_user_id": str(tenant_admin.id),
                "tenant_admin_email": tenant_admin.email,
            },
        )
    )
    db.add(
        AuditEvent(
            tenant_id=None,
            actor_user_id=user.id,
            action="tenant.created",
            details={
                "target_tenant_id": str(tenant.id),
                "tenant_name": tenant.name,
                "slug": tenant.slug,
                "plan": tenant.plan,
                "region": tenant.region,
            },
        )
    )
    db.commit()
    db.refresh(tenant)
    return _tenant_response(db, tenant)


@router.get("/platform/tenants", response_model=list[PlatformTenantResponse])
def list_platform_tenants(
    user: User = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> list[PlatformTenantResponse]:
    tenants = db.scalars(select(Tenant).order_by(Tenant.created_at.desc()))
    return [_tenant_response(db, tenant) for tenant in tenants]


@router.get("/platform/overview", response_model=PlatformOverviewResponse)
def get_platform_overview(
    user: User = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> PlatformOverviewResponse:
    return PlatformOverviewResponse(
        tenant_count=db.scalar(select(func.count()).select_from(Tenant)) or 0,
        active_tenant_count=db.scalar(
            select(func.count()).select_from(Tenant).where(Tenant.status == "active")
        ) or 0,
        suspended_tenant_count=db.scalar(
            select(func.count()).select_from(Tenant).where(Tenant.status == "suspended")
        ) or 0,
        member_count=db.scalar(select(func.count()).select_from(Membership)) or 0,
        active_member_count=db.scalar(
            select(func.count())
            .select_from(Membership)
            .join(User, User.id == Membership.user_id)
            .join(Tenant, Tenant.id == Membership.tenant_id)
            .where(
                User.is_active.is_(True),
                Tenant.status == "active",
            )
        ) or 0,
        platform_admin_count=db.scalar(
            select(func.count()).select_from(User).where(
                User.is_platform_admin.is_(True),
                User.is_active.is_(True),
            )
        ) or 0,
    )


def _tenant_response(db: Session, tenant: Tenant) -> PlatformTenantResponse:
    member_count = db.scalar(
        select(func.count()).select_from(Membership).where(Membership.tenant_id == tenant.id)
    ) or 0
    admin = db.execute(
        select(User.email, User.display_name)
        .join(Membership, Membership.user_id == User.id)
        .where(
            Membership.tenant_id == tenant.id,
            Membership.role == "tenant_admin",
            User.is_active.is_(True),
        )
        .order_by(Membership.created_at)
        .limit(1)
    ).first()
    return PlatformTenantResponse(
        id=tenant.id,
        name=tenant.name,
        slug=tenant.slug,
        status=tenant.status,
        seat_limit=tenant.seat_limit,
        plan=tenant.plan,
        region=tenant.region,
        member_count=member_count,
        admin_email=admin.email if admin else None,
        admin_name=admin.display_name if admin else None,
        trial_ends_at=tenant.trial_ends_at,
        available_actions=[
            TenantLifecycleActionOption(
                action=key,
                label=action.label,
                to_status=action.to_status,
                requires_review=action.requires_review,
            )
            for key, action in LIFECYCLE_ACTIONS.get(tenant.status, {}).items()
        ],
    )


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _operation_response(db: Session, request: PlatformOperationRequest) -> PlatformOperationResponse:
    tenant = db.get(Tenant, request.target_tenant_id)
    requester = db.get(User, request.requested_by)
    reviewer = db.get(User, request.reviewed_by) if request.reviewed_by else None
    action = LIFECYCLE_ACTIONS.get(request.from_status, {}).get(request.operation)
    return PlatformOperationResponse(
        id=request.id,
        operation=request.operation,
        operation_label=action.label if action else request.operation,
        target_tenant_id=request.target_tenant_id,
        target_tenant_name=tenant.name if tenant else "",
        from_status=request.from_status,
        to_status=request.to_status,
        reason=request.reason,
        status=request.status,
        requested_by_id=request.requested_by,
        requested_by_name=requester.display_name if requester else "",
        reviewed_by_name=reviewer.display_name if reviewer else None,
        review_comment=request.review_comment,
        created_at=request.created_at,
        expires_at=request.expires_at,
        reviewed_at=request.reviewed_at,
    )


def _record_lifecycle_audit(
    db: Session,
    *,
    actor_id: UUID,
    tenant: Tenant,
    action: str,
    details: dict[str, str],
) -> None:
    # Platform actions on a tenant are recorded in both chains (PRD 3.12, SET-113).
    db.add(AuditEvent(tenant_id=tenant.id, actor_user_id=actor_id, action=action, details={**details, "operator": "platform"}))
    db.add(
        AuditEvent(
            tenant_id=None,
            actor_user_id=actor_id,
            action=action,
            details={**details, "target_tenant_id": str(tenant.id), "tenant_name": tenant.name},
        )
    )


def _apply_lifecycle(db: Session, tenant: Tenant, operation: str, to_status: str) -> None:
    now = datetime.now(UTC)
    if operation == "extend_trial":
        base = _as_utc(tenant.trial_ends_at) if tenant.trial_ends_at else now
        tenant.trial_ends_at = max(base, now) + TRIAL_EXTENSION
    if to_status != tenant.status:
        tenant.status = to_status
        tenant.status_changed_at = now
    if to_status in {"pending_deletion", "deleted"}:
        db.query(AuthSession).filter(
            AuthSession.user_id.in_(
                select(Membership.user_id).where(Membership.tenant_id == tenant.id)
            ),
            AuthSession.revoked_at.is_(None),
        ).update({"revoked_at": now}, synchronize_session=False)


def _expire_stale_operations(db: Session) -> None:
    now = datetime.now(UTC)
    for request in db.scalars(
        select(PlatformOperationRequest).where(PlatformOperationRequest.status == "pending")
    ):
        if _as_utc(request.expires_at) <= now:
            request.status = "rejected"
            request.review_comment = "72 小时内未复核，已自动驳回"
            request.reviewed_at = now


@router.post(
    "/platform/tenants/{tenant_id}/lifecycle",
    response_model=TenantLifecycleResponse,
)
def run_tenant_lifecycle_action(
    tenant_id: UUID,
    payload: TenantLifecycleRequest,
    response: Response,
    user: User = Depends(require_platform_admin),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> TenantLifecycleResponse:
    tenant = db.scalar(select(Tenant).where(Tenant.id == tenant_id).with_for_update(key_share=True))
    if tenant is None:
        raise HTTPException(status_code=404, detail="未找到企业工作区")
    action = lifecycle_action(tenant.status, payload.action)
    if action is None:
        raise HTTPException(status_code=409, detail="当前租户状态不支持该操作")
    details = {
        "operation": payload.action,
        "from": tenant.status,
        "to": action.to_status,
        "reason": payload.reason,
    }
    if not action.requires_review:
        _record_lifecycle_audit(
            db, actor_id=user.id, tenant=tenant, action="tenant.status_changed", details=details
        )
        _apply_lifecycle(db, tenant, payload.action, action.to_status)
        db.commit()
        db.refresh(tenant)
        return TenantLifecycleResponse(tenant=_tenant_response(db, tenant))

    if not payload.reason:
        raise HTTPException(status_code=422, detail="停用与删除需填写原因")
    _expire_stale_operations(db)
    if db.scalar(
        select(PlatformOperationRequest.id).where(
            PlatformOperationRequest.target_tenant_id == tenant.id,
            PlatformOperationRequest.status == "pending",
        )
    ):
        raise HTTPException(status_code=409, detail="该租户已有待复核的操作")
    request = PlatformOperationRequest(
        operation=payload.action,
        target_tenant_id=tenant.id,
        from_status=tenant.status,
        to_status=action.to_status,
        reason=payload.reason,
        status="pending",
        requested_by=user.id,
        expires_at=datetime.now(UTC) + REVIEW_TIMEOUT,
    )
    db.add(request)
    db.flush()
    _record_lifecycle_audit(
        db,
        actor_id=user.id,
        tenant=tenant,
        action="tenant.lifecycle_requested",
        details={**details, "operation_id": str(request.id)},
    )
    db.commit()
    db.refresh(tenant)
    response.status_code = status.HTTP_202_ACCEPTED
    return TenantLifecycleResponse(
        tenant=_tenant_response(db, tenant),
        operation=_operation_response(db, request),
    )


@router.get("/platform/operations", response_model=list[PlatformOperationResponse])
def list_platform_operations(
    status_filter: str | None = Query(
        default=None, alias="status", pattern="^(pending|succeeded|rejected)$"
    ),
    user: User = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> list[PlatformOperationResponse]:
    _expire_stale_operations(db)
    db.commit()
    statement = select(PlatformOperationRequest)
    if status_filter:
        statement = statement.where(PlatformOperationRequest.status == status_filter)
    requests = db.scalars(statement.order_by(PlatformOperationRequest.created_at.desc()).limit(200))
    return [_operation_response(db, request) for request in requests]


def _pending_operation_for_review(
    db: Session, operation_id: UUID, reviewer: User
) -> PlatformOperationRequest:
    _expire_stale_operations(db)
    request = db.scalar(
        select(PlatformOperationRequest)
        .where(PlatformOperationRequest.id == operation_id)
        .with_for_update()
    )
    if request is None:
        raise HTTPException(status_code=404, detail="未找到待复核操作")
    if request.status != "pending":
        db.commit()
        raise HTTPException(status_code=409, detail="该操作已处理或已超时")
    if request.requested_by == reviewer.id:
        raise HTTPException(status_code=403, detail="复核人不能是发起人")
    return request


@router.post(
    "/platform/operations/{operation_id}/approve",
    response_model=PlatformOperationResponse,
)
def approve_platform_operation(
    operation_id: UUID,
    payload: ReviewPlatformOperationRequest,
    user: User = Depends(require_platform_admin),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> PlatformOperationResponse:
    request = _pending_operation_for_review(db, operation_id, user)
    tenant = db.scalar(
        select(Tenant).where(Tenant.id == request.target_tenant_id).with_for_update(key_share=True)
    )
    if tenant is None or tenant.status != request.from_status:
        raise HTTPException(status_code=409, detail="租户状态已变化，请驳回后重新发起")
    request.status = "succeeded"
    request.reviewed_by = user.id
    request.review_comment = payload.comment.strip() or None
    request.reviewed_at = datetime.now(UTC)
    _record_lifecycle_audit(
        db,
        actor_id=user.id,
        tenant=tenant,
        action="tenant.status_changed",
        details={
            "operation": request.operation,
            "operation_id": str(request.id),
            "from": request.from_status,
            "to": request.to_status,
            "reason": request.reason,
            "requested_by": str(request.requested_by),
        },
    )
    _apply_lifecycle(db, tenant, request.operation, request.to_status)
    db.commit()
    return _operation_response(db, request)


@router.post(
    "/platform/operations/{operation_id}/reject",
    response_model=PlatformOperationResponse,
)
def reject_platform_operation(
    operation_id: UUID,
    payload: ReviewPlatformOperationRequest,
    user: User = Depends(require_platform_admin),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> PlatformOperationResponse:
    request = _pending_operation_for_review(db, operation_id, user)
    tenant = db.get(Tenant, request.target_tenant_id)
    request.status = "rejected"
    request.reviewed_by = user.id
    request.review_comment = payload.comment.strip() or None
    request.reviewed_at = datetime.now(UTC)
    if tenant is not None:
        _record_lifecycle_audit(
            db,
            actor_id=user.id,
            tenant=tenant,
            action="tenant.lifecycle_rejected",
            details={"operation": request.operation, "operation_id": str(request.id)},
        )
    db.commit()
    return _operation_response(db, request)
