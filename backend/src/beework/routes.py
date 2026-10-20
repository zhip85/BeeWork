import hashlib
import csv
import io
import json
import secrets
from datetime import datetime, timedelta, timezone
from uuid import UUID

import jwt
from fastapi import APIRouter, Cookie, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy import cast, func, or_, select, String
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from beework.audit_maintenance import AUDIT_RETENTION_DAYS, verify_audit_chain
from beework.auth import (
    ACCESS_COOKIE,
    CSRF_COOKIE,
    get_current_user,
    get_tenant_membership,
    require_csrf,
    require_tenant_roles,
)
from beework.config import settings
from beework.db import get_db
from beework.models import (
    AUDIT_ACTION_DESCRIPTIONS,
    AuditEvent,
    AuthSession,
    MemberInvitation,
    Membership,
    Organization,
    PlatformModel,
    PlatformModelTenant,
    Tenant,
    TenantModelEnvironment,
    TenantModelSelection,
    TenantRole,
    User,
)
from beework.rbac import (
    BUILTIN_PERMISSIONS,
    PERMISSION_CATALOG,
    ROLE_LABELS,
    flatten_permissions,
    organization_is_within_scope,
    require_tenant_permission,
    validate_permissions,
)
from beework.schemas import (
    ChangeTenantStatusRequest,
    ChangeMemberOrganizationRequest,
    ChangeMemberStatusRequest,
    ChangeRoleRequest,
    CreateMemberInvitationsRequest,
    CreateMemberInvitationsResponse,
    CreatedMemberInvitationResponse,
    MemberInvitationResponse,
    CreateRoleRequest,
    CreateOrganizationRequest,
    CreateTenantRequest,
    CurrentUserResponse,
    LoginRequest,
    MemberListResponse,
    MemberResponse,
    OrganizationListResponse,
    OrganizationResponse,
    PermissionCatalogResponse,
    PlatformTenantResponse,
    PlatformOverviewResponse,
    RenameOrganizationRequest,
    RoleListResponse,
    RoleResponse,
    TenantMembershipResponse,
    UpdateRoleRequest,
    AcceptMemberInvitationRequest,
    AuditIntegrityResponse,
    AuditEventListResponse,
    AuditEventResponse,
    PlatformModelWriteRequest,
    PlatformModelListResponse,
    PlatformModelResponse,
    TenantModelEnvironmentResponse,
    TenantModelEnvironmentWriteRequest,
    TenantModelOption,
)
from beework.model_secrets import encrypt_model_api_key
from beework.security import (
    TOKEN_LIFETIME_HOURS,
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)

router = APIRouter(prefix="/api/v1")

AUDIT_CATEGORIES = {
    "assistants": "助手",
    "security": "安全",
    "members_permissions": "成员与权限",
    "billing": "计费",
    "developer": "开发者",
}

def _create_login_session(db: Session, user: User, response: Response) -> None:
    token_id = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=TOKEN_LIFETIME_HOURS)
    db.add(AuthSession(user_id=user.id, token_id=token_id, expires_at=expires_at))
    db.flush()
    session_token = create_access_token(user.id, token_id)
    csrf_token = secrets.token_urlsafe(32)
    cookie_options = {
        "max_age": TOKEN_LIFETIME_HOURS * 60 * 60,
        "secure": settings.auth_cookie_secure,
        "samesite": "strict",
        "path": "/",
    }
    response.set_cookie(ACCESS_COOKIE, session_token, httponly=True, **cookie_options)
    response.set_cookie(CSRF_COOKIE, csrf_token, httponly=False, **cookie_options)


def _record_user_activity(
    db: Session,
    user: User,
    action: str,
    *,
    result: str = "success",
    details: dict[str, str] | None = None,
) -> None:
    tenant_ids: list[UUID | None] = list(
        db.scalars(
            select(Membership.tenant_id).where(
                Membership.user_id == user.id,
                Membership.status == "active",
            )
        )
    )
    if not tenant_ids:
        tenant_ids = [None]
    for tenant_id in tenant_ids:
        db.add(
            AuditEvent(
                tenant_id=tenant_id,
                actor_user_id=user.id if result == "success" else None,
                action=action,
                result=result,
                details=details or {},
            )
        )


def _seat_usage(db: Session, tenant_id: UUID) -> int:
    now = datetime.now(timezone.utc)
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


def _invitation_response(
    db: Session, invitation: MemberInvitation
) -> MemberInvitationResponse:
    custom_role = (
        db.get(TenantRole, invitation.custom_role_id)
        if invitation.custom_role_id
        else None
    )
    organization = db.get(Organization, invitation.organization_id)
    if organization is None:
        raise RuntimeError("Invitation references a missing organization")
    return MemberInvitationResponse(
        id=invitation.id,
        email=invitation.email,
        role=invitation.role,
        custom_role_id=invitation.custom_role_id,
        custom_role_name=custom_role.name if custom_role else None,
        organization_id=invitation.organization_id,
        organization_name=organization.name,
        status=(
            "expired"
            if invitation.status == "pending"
            and (
                invitation.expires_at.replace(tzinfo=timezone.utc)
                if invitation.expires_at.tzinfo is None
                else invitation.expires_at
            ) <= datetime.now(timezone.utc)
            else invitation.status
        ),
        expires_at=invitation.expires_at,
    )


def _member_response(
    db: Session, user: User, membership: Membership
) -> MemberResponse:
    organization = db.get(Organization, membership.organization_id)
    custom_role = (
        db.get(TenantRole, membership.custom_role_id)
        if membership.custom_role_id
        else None
    )
    if organization is None:
        raise RuntimeError("Membership references a missing organization")
    return MemberResponse(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        role=membership.role,
        is_active=user.is_active,
        status=membership.status,
        organization_id=membership.organization_id,
        organization_name=organization.name,
        custom_role_id=membership.custom_role_id,
        custom_role_name=custom_role.name if custom_role else None,
    )


@router.post("/auth/login", response_model=CurrentUserResponse)
def login(
    payload: LoginRequest,
    response: Response,
    db: Session = Depends(get_db),
) -> CurrentUserResponse:
    email = str(payload.email).strip().lower()
    user = db.scalar(select(User).where(User.email == email))
    if not user or not user.is_active or not verify_password(
        payload.password, user.password_hash
    ):
        if user and user.is_active:
            _record_user_activity(
                db,
                user,
                "auth.login_failed",
                result="failure",
                details={"email": email},
            )
            db.commit()
        raise HTTPException(status_code=401, detail="邮箱或密码不正确")

    if not user.is_platform_admin:
        active_membership = db.scalar(
            select(Membership.id)
            .join(Tenant, Tenant.id == Membership.tenant_id)
            .where(
                Membership.user_id == user.id,
                Membership.status == "active",
                Tenant.status == "active",
            )
        )
        if active_membership is None:
            raise HTTPException(status_code=403, detail="账号当前没有可用的企业工作区")
    _create_login_session(db, user, response)
    _record_user_activity(db, user, "auth.login")
    db.commit()
    return make_current_user_response(db, user)


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    response: Response,
    _: None = Depends(require_csrf),
    session_cookie: str | None = Cookie(default=None, alias=ACCESS_COOKIE),
    db: Session = Depends(get_db),
) -> Response:
    if session_cookie:
        try:
            _, token_id = decode_access_token(session_cookie)
        except (jwt.InvalidTokenError, ValueError, KeyError):
            token_id = ""
        if token_id:
            auth_session = db.scalar(
                select(AuthSession).where(AuthSession.token_id == token_id)
            )
            if auth_session and auth_session.revoked_at is None:
                auth_session.revoked_at = datetime.now(timezone.utc)
                user = db.get(User, auth_session.user_id)
                if user:
                    _record_user_activity(db, user, "auth.logout")
                db.commit()
    response.delete_cookie(ACCESS_COOKIE, path="/", samesite="strict")
    response.delete_cookie(CSRF_COOKIE, path="/", samesite="strict")
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.get("/auth/me", response_model=CurrentUserResponse)
def current_user(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CurrentUserResponse:
    return make_current_user_response(db, user)


@router.get("/tenants", response_model=list[TenantMembershipResponse])
def list_tenants(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[TenantMembershipResponse]:
    return make_current_user_response(db, user).memberships


def _audit_query(
    tenant_id: UUID | None,
    *,
    scope: str,
    category: str | None,
    search: str | None,
    result: str | None,
    start: datetime | None,
    end: datetime | None,
):
    statement = (
        select(AuditEvent, User)
        .outerjoin(User, User.id == AuditEvent.actor_user_id)
        .where(
            AuditEvent.scope == scope,
            AuditEvent.tenant_id == tenant_id
            if tenant_id is not None
            else AuditEvent.tenant_id.is_(None),
            AuditEvent.created_at
            >= datetime.now(timezone.utc) - timedelta(days=AUDIT_RETENTION_DAYS),
        )
    )
    if category:
        statement = statement.where(AuditEvent.category == category)
    if result:
        statement = statement.where(AuditEvent.result == result)
    if start:
        statement = statement.where(AuditEvent.created_at >= start)
    if end:
        statement = statement.where(AuditEvent.created_at <= end)
    if search:
        pattern = f"%{search.strip()}%"
        statement = statement.where(
            or_(
                User.display_name.ilike(pattern),
                User.email.ilike(pattern),
                AuditEvent.action.ilike(pattern),
                AuditEvent.description.ilike(pattern),
                cast(AuditEvent.details, String).ilike(pattern),
            )
        )
    return statement.order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())


def _csv_safe_cell(value: str) -> str:
    if value.lstrip()[:1] in {"=", "+", "-", "@", "\t", "\r"}:
        return f"'{value}"
    return value


def _audit_response(
    rows,
    *,
    total: int,
    limit: int,
    offset: int,
) -> AuditEventListResponse:
    return AuditEventListResponse(
        events=[
            AuditEventResponse(
                id=audit.id,
                category=audit.category,
                result=audit.result,
                created_at=audit.created_at,
                actor_name=actor.display_name if actor else "系统",
                actor_email=actor.email if actor else None,
                description=(
                    AUDIT_ACTION_DESCRIPTIONS.get(audit.action, audit.action)
                    if audit.description == audit.action
                    else audit.description
                ),
                source_ip=audit.source_ip,
                details=audit.details,
            )
            for audit, actor in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/audit/events", response_model=AuditEventListResponse)
def list_audit_events(
    category: str | None = Query(default=None, pattern="^(assistants|security|members_permissions|billing|developer)$"),
    search: str | None = Query(default=None, max_length=200),
    result: str | None = Query(default=None, pattern="^(success|warning|failure)$"),
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=100000),
    membership: Membership = Depends(require_tenant_permission("audit:view")),
    db: Session = Depends(get_db),
) -> AuditEventListResponse:
    if start and end and start > end:
        raise HTTPException(status_code=422, detail="开始时间不能晚于结束时间")
    statement = _audit_query(
        membership.tenant_id,
        scope="tenant",
        category=category,
        search=search,
        result=result,
        start=start,
        end=end,
    )
    total = db.scalar(
        select(func.count())
        .select_from(AuditEvent)
        .where(
            AuditEvent.scope == "tenant",
            AuditEvent.tenant_id == membership.tenant_id,
            AuditEvent.created_at
            >= datetime.now(timezone.utc) - timedelta(days=AUDIT_RETENTION_DAYS),
        )
    ) or 0
    if category or search or result or start or end:
        total = db.scalar(
            select(func.count()).select_from(
                statement.with_only_columns(AuditEvent.id).order_by(None).subquery()
            )
        ) or 0
    rows = db.execute(statement.limit(limit).offset(offset)).all()
    return _audit_response(rows, total=total, limit=limit, offset=offset)


@router.get("/audit/events/export")
def export_audit_events(
    category: str | None = Query(default=None, pattern="^(assistants|security|members_permissions|billing|developer)$"),
    search: str | None = Query(default=None, max_length=200),
    result: str | None = Query(default=None, pattern="^(success|warning|failure)$"),
    start: datetime | None = None,
    end: datetime | None = None,
    membership: Membership = Depends(require_tenant_permission("audit:export")),
    db: Session = Depends(get_db),
) -> Response:
    if start and end and start > end:
        raise HTTPException(status_code=422, detail="开始时间不能晚于结束时间")
    statement = _audit_query(
        membership.tenant_id,
        scope="tenant",
        category=category,
        search=search,
        result=result,
        start=start,
        end=end,
    )
    rows = db.execute(statement).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["结果", "时间", "分类", "操作人", "描述", "来源 IP", "变更详情"])
    for audit, actor in rows:
        values = [
            audit.result,
            audit.created_at.isoformat(),
            AUDIT_CATEGORIES.get(audit.category, audit.category),
            actor.display_name if actor else "系统",
            (
                AUDIT_ACTION_DESCRIPTIONS.get(audit.action, audit.action)
                if audit.description == audit.action
                else audit.description
            ),
            audit.source_ip or "",
            json.dumps(audit.details, ensure_ascii=False, sort_keys=True),
        ]
        writer.writerow([_csv_safe_cell(value) for value in values])
    content = output.getvalue()
    output.close()
    db.add(
        AuditEvent(
            tenant_id=membership.tenant_id,
            actor_user_id=membership.user_id,
            action="audit.exported",
            details={
                "row_count": str(len(rows)),
                "category": category or "",
                "search": search or "",
                "result": result or "",
            },
        )
    )
    db.commit()
    return Response(
        content=content.encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="beework-audit.csv"'},
    )


@router.get("/audit/integrity", response_model=AuditIntegrityResponse)
def check_tenant_audit_integrity(
    membership: Membership = Depends(require_tenant_permission("audit:view")),
    db: Session = Depends(get_db),
) -> AuditIntegrityResponse:
    result = verify_audit_chain(db, f"tenant:{membership.tenant_id}")
    return AuditIntegrityResponse(**result)


@router.get("/platform/audit/events", response_model=AuditEventListResponse)
def list_platform_audit_events(
    category: str | None = Query(default=None, pattern="^(assistants|security|members_permissions|billing|developer)$"),
    search: str | None = Query(default=None, max_length=200),
    result: str | None = Query(default=None, pattern="^(success|warning|failure)$"),
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=100000),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> AuditEventListResponse:
    if not user.is_platform_admin:
        raise HTTPException(status_code=403, detail="仅平台管理员可查看平台操作审计")
    if start and end and start > end:
        raise HTTPException(status_code=422, detail="开始时间不能晚于结束时间")
    statement = _audit_query(
        None,
        scope="platform",
        category=category,
        search=search,
        result=result,
        start=start,
        end=end,
    )
    total = db.scalar(
        select(func.count())
        .select_from(AuditEvent)
        .where(
            AuditEvent.scope == "platform",
            AuditEvent.tenant_id.is_(None),
            AuditEvent.created_at
            >= datetime.now(timezone.utc) - timedelta(days=AUDIT_RETENTION_DAYS),
        )
    ) or 0
    if category or search or result or start or end:
        total = db.scalar(
            select(func.count()).select_from(
                statement.with_only_columns(AuditEvent.id).order_by(None).subquery()
            )
        ) or 0
    rows = db.execute(statement.limit(limit).offset(offset)).all()
    return _audit_response(rows, total=total, limit=limit, offset=offset)


@router.get("/platform/audit/events/export")
def export_platform_audit_events(
    category: str | None = Query(default=None, pattern="^(assistants|security|members_permissions|billing|developer)$"),
    search: str | None = Query(default=None, max_length=200),
    result: str | None = Query(default=None, pattern="^(success|warning|failure)$"),
    start: datetime | None = None,
    end: datetime | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    if not user.is_platform_admin:
        raise HTTPException(status_code=403, detail="仅平台管理员可导出平台操作审计")
    if start and end and start > end:
        raise HTTPException(status_code=422, detail="开始时间不能晚于结束时间")
    statement = _audit_query(
        None,
        scope="platform",
        category=category,
        search=search,
        result=result,
        start=start,
        end=end,
    )
    rows = db.execute(statement).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["结果", "时间", "分类", "操作人", "描述", "来源 IP", "操作详情"])
    for audit, actor in rows:
        writer.writerow(
            [
                _csv_safe_cell(value)
                for value in (
                    audit.result,
                    audit.created_at.isoformat(),
                    AUDIT_CATEGORIES.get(audit.category, audit.category),
                    actor.display_name if actor else "系统",
                    audit.description,
                    audit.source_ip or "",
                    json.dumps(audit.details, ensure_ascii=False, sort_keys=True),
                )
            ]
        )
    content = output.getvalue().encode("utf-8-sig")
    output.close()
    db.add(
        AuditEvent(
            tenant_id=None,
            actor_user_id=user.id,
            action="audit.exported",
            details={"scope": "platform", "row_count": str(len(rows))},
        )
    )
    db.commit()
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="beework-platform-audit.csv"'},
    )


@router.get("/platform/audit/integrity", response_model=AuditIntegrityResponse)
def check_platform_audit_integrity(
    tenant_id: UUID | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> AuditIntegrityResponse:
    if not user.is_platform_admin:
        raise HTTPException(status_code=403, detail="仅平台管理员可验证平台操作审计")
    if tenant_id is None:
        chain_scope = "platform"
    else:
        if db.get(Tenant, tenant_id) is None:
            raise HTTPException(status_code=404, detail="未找到企业工作区")
        chain_scope = f"tenant:{tenant_id}"
    return AuditIntegrityResponse(**verify_audit_chain(db, chain_scope))


@router.post(
    "/platform/tenants",
    response_model=PlatformTenantResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_tenant(
    payload: CreateTenantRequest,
    user: User = Depends(get_current_user),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> PlatformTenantResponse:
    if not user.is_platform_admin:
        raise HTTPException(status_code=403, detail="仅平台管理员可创建企业")
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
    return PlatformTenantResponse(
        id=tenant.id,
        name=tenant.name,
        slug=tenant.slug,
        status=tenant.status,
        seat_limit=tenant.seat_limit,
        plan=tenant.plan,
        region=tenant.region,
        member_count=1,
        admin_email=tenant_admin.email,
        admin_name=tenant_admin.display_name,
    )


@router.get("/platform/tenants", response_model=list[PlatformTenantResponse])
def list_platform_tenants(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[PlatformTenantResponse]:
    if not user.is_platform_admin:
        raise HTTPException(status_code=403, detail="仅平台管理员可查看全部企业")
    tenants = list(db.scalars(select(Tenant).order_by(Tenant.created_at.desc())))
    results: list[PlatformTenantResponse] = []
    for tenant in tenants:
        member_count = db.scalar(
            select(func.count()).select_from(Membership).where(
                Membership.tenant_id == tenant.id
            )
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
        results.append(
            PlatformTenantResponse(
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
            )
        )
    return results


@router.get("/platform/overview", response_model=PlatformOverviewResponse)
def get_platform_overview(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PlatformOverviewResponse:
    if not user.is_platform_admin:
        raise HTTPException(status_code=403, detail="仅平台管理员可查看平台总览")
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


def _platform_model_response(model: PlatformModel) -> PlatformModelResponse:
    return PlatformModelResponse(
        id=model.id,
        display_name=model.display_name,
        provider=model.provider,
        model_type=model.model_type,
        availability=model.availability,
        input_price_per_million=model.input_price_per_million,
        output_price_per_million=model.output_price_per_million,
        input_points_per_thousand=model.input_points_per_thousand,
        output_points_per_thousand=model.output_points_per_thousand,
        status=model.status,
        provider_model_id=model.provider_model_id,
        endpoint_url=model.endpoint_url,
        credential_configured=bool(model.encrypted_api_key),
        api_key_masked=f"••••••{model.api_key_last_four}",
        api_version=model.api_version,
        region=model.region,
        context_window=model.context_window,
        max_output_tokens=model.max_output_tokens,
        default_temperature=model.default_temperature,
        rpm_limit=model.rpm_limit,
        fallback_model_id=model.fallback_model_id,
        allowlist_tenant_ids=[entry.tenant_id for entry in model.allowlist],
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def _validate_platform_model_references(
    db: Session,
    payload: PlatformModelWriteRequest,
    *,
    model_id: UUID | None = None,
) -> list[Tenant]:
    tenants = list(
        db.scalars(
            select(Tenant).where(Tenant.id.in_(payload.allowlist_tenant_ids))
        )
    ) if payload.allowlist_tenant_ids else []
    if len(tenants) != len(set(payload.allowlist_tenant_ids)):
        raise HTTPException(status_code=422, detail="白名单包含不存在的租户")

    if payload.fallback_model_id is None:
        return tenants
    if payload.fallback_model_id == model_id:
        raise HTTPException(status_code=422, detail="模型不能降级到自身")
    fallback = db.get(PlatformModel, payload.fallback_model_id)
    if fallback is None:
        raise HTTPException(status_code=422, detail="降级模型不存在")
    if fallback.model_type != payload.model_type or fallback.region != payload.region:
        raise HTTPException(status_code=422, detail="降级模型必须与当前模型类型和区域一致")

    visited: set[UUID] = set()
    current: PlatformModel | None = fallback
    while current is not None:
        if current.id == model_id:
            raise HTTPException(status_code=422, detail="降级模型不能形成循环引用")
        if current.id in visited:
            raise HTTPException(status_code=409, detail="现有模型降级关系已存在循环")
        visited.add(current.id)
        current = (
            db.get(PlatformModel, current.fallback_model_id)
            if current.fallback_model_id
            else None
        )
    return tenants


def _write_platform_model(
    db: Session,
    model: PlatformModel,
    payload: PlatformModelWriteRequest,
    *,
    create: bool,
) -> None:
    tenant_rows = _validate_platform_model_references(
        db,
        payload,
        model_id=None if create else model.id,
    )
    if create and not payload.api_key:
        raise HTTPException(status_code=422, detail="新增模型必须填写 API Key")
    if payload.api_key:
        model.encrypted_api_key = encrypt_model_api_key(payload.api_key)
        model.api_key_last_four = payload.api_key[-4:]

    for field in (
        "display_name",
        "provider",
        "model_type",
        "availability",
        "input_price_per_million",
        "output_price_per_million",
        "input_points_per_thousand",
        "output_points_per_thousand",
        "status",
        "provider_model_id",
        "endpoint_url",
        "api_version",
        "region",
        "context_window",
        "max_output_tokens",
        "default_temperature",
        "rpm_limit",
        "fallback_model_id",
    ):
        setattr(model, field, getattr(payload, field))
    model.allowlist.clear()
    model.allowlist.extend(
        PlatformModelTenant(tenant_id=tenant.id) for tenant in tenant_rows
    )


def _eligible_tenant_models(db: Session, tenant: Tenant) -> list[PlatformModel]:
    models = list(db.scalars(
        select(PlatformModel)
        .where(
            PlatformModel.status == "healthy",
            PlatformModel.region == tenant.region,
        )
        .order_by(PlatformModel.provider, PlatformModel.display_name)
    ))
    eligible = []
    for model in models:
        if model.availability == "all_plans":
            eligible.append(model)
        elif model.availability == "professional_plus" and tenant.plan in {
            "professional", "enterprise"
        }:
            eligible.append(model)
        elif model.availability == "enterprise" and tenant.plan == "enterprise":
            eligible.append(model)
        elif model.availability == "allowlist" and db.scalar(
            select(PlatformModelTenant.model_id).where(
                PlatformModelTenant.model_id == model.id,
                PlatformModelTenant.tenant_id == tenant.id,
            )
        ):
            eligible.append(model)
    return eligible


def _tenant_model_environment_response(
    db: Session, tenant: Tenant, environment: str
) -> TenantModelEnvironmentResponse:
    config = db.scalar(
        select(TenantModelEnvironment).where(
            TenantModelEnvironment.tenant_id == tenant.id,
            TenantModelEnvironment.environment == environment,
        )
    )
    selections = list(
        db.scalars(
            select(TenantModelSelection).where(
                TenantModelSelection.environment_id == config.id
            )
        )
    ) if config else []
    eligible_models = _eligible_tenant_models(db, tenant)
    eligible_by_id = {model.id: model for model in eligible_models}
    enabled_ids = [selection.model_id for selection in selections]
    default_ids = {
        model_type: next(
            (
                selection.model_id
                for selection in selections
                if selection.model_type == model_type and selection.is_default
            ),
            None,
        )
        for model_type in ("chat", "embedding", "rerank")
    }
    return TenantModelEnvironmentResponse(
        tenant_id=tenant.id,
        environment=environment,
        plan=tenant.plan,
        region=tenant.region,
        version=config.version if config else 0,
        updated_at=config.updated_at if config else None,
        models=[
            TenantModelOption(
                id=model.id,
                display_name=model.display_name,
                provider=model.provider,
                model_type=model.model_type,
                region=model.region,
                input_price_per_million=model.input_price_per_million,
            )
            for model in eligible_models
        ],
        enabled_model_ids=enabled_ids,
        default_model_ids=default_ids,
        unavailable_enabled_model_ids=[
            model_id for model_id in enabled_ids if model_id not in eligible_by_id
        ],
    )


@router.get(
    "/tenant/model-environments/{environment}",
    response_model=TenantModelEnvironmentResponse,
)
def get_tenant_model_environment(
    environment: str,
    membership: Membership = Depends(require_tenant_permission("settings:view")),
    db: Session = Depends(get_db),
) -> TenantModelEnvironmentResponse:
    if environment not in {"development", "testing", "production"}:
        raise HTTPException(status_code=422, detail="环境必须是开发、测试或生产")
    tenant = db.get(Tenant, membership.tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="未找到企业工作区")
    return _tenant_model_environment_response(db, tenant, environment)


@router.put(
    "/tenant/model-environments/{environment}",
    response_model=TenantModelEnvironmentResponse,
)
def update_tenant_model_environment(
    environment: str,
    payload: TenantModelEnvironmentWriteRequest,
    membership: Membership = Depends(require_tenant_permission("settings:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> TenantModelEnvironmentResponse:
    if environment not in {"development", "testing", "production"}:
        raise HTTPException(status_code=422, detail="环境必须是开发、测试或生产")
    tenant = db.scalar(
        select(Tenant).where(Tenant.id == membership.tenant_id).with_for_update()
    )
    if tenant is None:
        raise HTTPException(status_code=404, detail="未找到企业工作区")
    config = db.scalar(
        select(TenantModelEnvironment)
        .where(
            TenantModelEnvironment.tenant_id == tenant.id,
            TenantModelEnvironment.environment == environment,
        )
        .with_for_update()
    )
    current_version = config.version if config else 0
    if payload.expected_version != current_version:
        raise HTTPException(status_code=409, detail="模型环境配置已被其他人修改，请刷新后重试")
    eligible = {
        model.id: model for model in _eligible_tenant_models(db, tenant)
    }
    requested_ids = set(payload.enabled_model_ids)
    if requested_ids - eligible.keys():
        raise HTTPException(
            status_code=422,
            detail="所选模型已停用，或不符合当前套餐、区域和租户开放范围",
        )
    for model_type, model_id in payload.default_model_ids.items():
        if model_id is not None and eligible[model_id].model_type != model_type:
            raise HTTPException(status_code=422, detail="默认模型类型与配置项不匹配")
    if config is None:
        config = TenantModelEnvironment(
            tenant_id=tenant.id,
            environment=environment,
            version=0,
        )
        db.add(config)
        db.flush()
    db.query(TenantModelSelection).filter(
        TenantModelSelection.environment_id == config.id
    ).delete(synchronize_session=False)
    for model_id in payload.enabled_model_ids:
        model = eligible[model_id]
        db.add(
            TenantModelSelection(
                environment_id=config.id,
                model_id=model_id,
                model_type=model.model_type,
                is_default=payload.default_model_ids.get(model.model_type) == model_id,
            )
        )
    config.version += 1
    db.add(
        AuditEvent(
            tenant_id=tenant.id,
            actor_user_id=membership.user_id,
            action="model_environment.updated",
            details={
                "environment": environment,
                "version": str(config.version),
                "enabled_model_count": str(len(payload.enabled_model_ids)),
            },
        )
    )
    db.commit()
    db.refresh(config)
    return _tenant_model_environment_response(db, tenant, environment)


@router.get("/platform/models", response_model=PlatformModelListResponse)
def list_platform_models(
    search: str | None = Query(default=None, max_length=120),
    status_filter: str | None = Query(
        default=None, alias="status", pattern="^(healthy|canary|disabled)$"
    ),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PlatformModelListResponse:
    if not user.is_platform_admin:
        raise HTTPException(status_code=403, detail="仅平台管理员可查看模型目录")
    statement = select(PlatformModel)
    if search and search.strip():
        statement = statement.where(
            or_(
                PlatformModel.display_name.ilike(f"%{search.strip()}%"),
                PlatformModel.provider_model_id.ilike(f"%{search.strip()}%"),
            )
        )
    if status_filter:
        statement = statement.where(PlatformModel.status == status_filter)
    models = db.scalars(
        statement.order_by(PlatformModel.updated_at.desc(), PlatformModel.display_name)
    )
    return PlatformModelListResponse(
        models=[_platform_model_response(model) for model in models]
    )


@router.post(
    "/platform/models",
    response_model=PlatformModelResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_platform_model(
    payload: PlatformModelWriteRequest,
    user: User = Depends(get_current_user),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> PlatformModelResponse:
    if not user.is_platform_admin:
        raise HTTPException(status_code=403, detail="仅平台管理员可新增模型")
    if db.scalar(
        select(PlatformModel.id).where(
            PlatformModel.display_name == payload.display_name
        )
    ):
        raise HTTPException(status_code=409, detail="模型显示名称已存在")
    model = PlatformModel()
    _write_platform_model(db, model, payload, create=True)
    db.add(model)
    db.add(
        AuditEvent(
            tenant_id=None,
            actor_user_id=user.id,
            action="model_catalog.created",
            details={
                "display_name": payload.display_name,
                "provider": payload.provider,
                "model_type": payload.model_type,
                "region": payload.region,
                "status": payload.status,
            },
        )
    )
    try:
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="模型显示名称已存在") from error
    db.refresh(model)
    return _platform_model_response(model)


@router.put("/platform/models/{model_id}", response_model=PlatformModelResponse)
def update_platform_model(
    model_id: UUID,
    payload: PlatformModelWriteRequest,
    user: User = Depends(get_current_user),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> PlatformModelResponse:
    if not user.is_platform_admin:
        raise HTTPException(status_code=403, detail="仅平台管理员可更新模型")
    model = db.get(PlatformModel, model_id)
    if model is None:
        raise HTTPException(status_code=404, detail="未找到模型")
    duplicate = db.scalar(
        select(PlatformModel.id).where(
            PlatformModel.display_name == payload.display_name,
            PlatformModel.id != model.id,
        )
    )
    if duplicate:
        raise HTTPException(status_code=409, detail="模型显示名称已存在")
    previous_status = model.status
    _write_platform_model(db, model, payload, create=False)
    db.add(
        AuditEvent(
            tenant_id=None,
            actor_user_id=user.id,
            action=(
                "model_catalog.disabled"
                if payload.status == "disabled" and previous_status != "disabled"
                else "model_catalog.updated"
            ),
            details={
                "model_id": str(model.id),
                "display_name": payload.display_name,
                "status": payload.status,
            },
        )
    )
    try:
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="模型目录更新冲突") from error
    db.refresh(model)
    return _platform_model_response(model)


@router.patch(
    "/platform/tenants/{tenant_id}/status",
    response_model=PlatformTenantResponse,
)
def change_tenant_status(
    tenant_id: UUID,
    payload: ChangeTenantStatusRequest,
    user: User = Depends(get_current_user),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> PlatformTenantResponse:
    if not user.is_platform_admin:
        raise HTTPException(status_code=403, detail="仅平台管理员可调整企业状态")
    tenant = db.scalar(
        select(Tenant).where(Tenant.id == tenant_id).with_for_update()
    )
    if tenant is None:
        raise HTTPException(status_code=404, detail="未找到企业工作区")
    previous_status = tenant.status
    tenant.status = payload.status
    db.add(
        AuditEvent(
            tenant_id=tenant.id,
            actor_user_id=user.id,
            action="tenant.status_changed",
            details={"from": previous_status, "to": tenant.status},
        )
    )
    db.add(
        AuditEvent(
            tenant_id=None,
            actor_user_id=user.id,
            action="tenant.status_changed",
            details={
                "target_tenant_id": str(tenant.id),
                "tenant_name": tenant.name,
                "from": previous_status,
                "to": tenant.status,
            },
        )
    )
    db.commit()
    db.refresh(tenant)
    return PlatformTenantResponse(
        id=tenant.id,
        name=tenant.name,
        slug=tenant.slug,
        status=tenant.status,
        seat_limit=tenant.seat_limit,
        plan=tenant.plan,
        region=tenant.region,
        member_count=db.scalar(
            select(func.count()).select_from(Membership).where(
                Membership.tenant_id == tenant.id
            )
        ) or 0,
    )


@router.get("/members", response_model=MemberListResponse)
def list_members(
    membership: Membership = Depends(require_tenant_roles("tenant_admin")),
    db: Session = Depends(get_db),
) -> MemberListResponse:
    members = db.execute(
        select(User, Membership, Organization.name, TenantRole.name)
        .join(Membership, Membership.user_id == User.id)
        .join(Organization, Organization.id == Membership.organization_id)
        .outerjoin(TenantRole, TenantRole.id == Membership.custom_role_id)
        .where(Membership.tenant_id == membership.tenant_id)
        .order_by(User.display_name, User.email)
    )
    return MemberListResponse(
        tenant_id=membership.tenant_id,
        seat_limit=db.get(Tenant, membership.tenant_id).seat_limit,
        seats_used=_seat_usage(db, membership.tenant_id),
        members=[
            MemberResponse(
                id=user.id,
                email=user.email,
                display_name=user.display_name,
                role=member.role,
                is_active=user.is_active,
                status=member.status,
                organization_id=member.organization_id,
                organization_name=organization_name,
                custom_role_id=member.custom_role_id,
                custom_role_name=custom_role_name,
            )
            for user, member, organization_name, custom_role_name in members
        ],
        invitations=[
            _invitation_response(db, invitation)
            for invitation in db.scalars(
                select(MemberInvitation)
                .where(MemberInvitation.tenant_id == membership.tenant_id)
                .order_by(MemberInvitation.created_at.desc())
            )
        ],
    )


@router.post(
    "/members/invitations",
    response_model=CreateMemberInvitationsResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_member_invitations(
    payload: CreateMemberInvitationsRequest,
    membership: Membership = Depends(require_tenant_roles("tenant_admin")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> CreateMemberInvitationsResponse:
    tenant = _lock_tenant(db, membership.tenant_id)
    organization = db.scalar(
        select(Organization).where(
            Organization.id == payload.organization_id,
            Organization.tenant_id == membership.tenant_id,
        )
    )
    if organization is None:
        raise HTTPException(status_code=404, detail="邀请部门不属于当前企业")
    custom_role = None
    if payload.role == "custom":
        custom_role = db.scalar(
            select(TenantRole).where(
                TenantRole.id == payload.custom_role_id,
                TenantRole.tenant_id == membership.tenant_id,
                TenantRole.is_active.is_(True),
            )
        )
        if custom_role is None:
            raise HTTPException(status_code=404, detail="未找到可用的自定义角色")
        if custom_role.scope == "organization" and not organization_is_within_scope(
            db, membership.tenant_id, organization.id, custom_role.scope_organization_id
        ):
            raise HTTPException(status_code=409, detail="所选部门不属于该角色的数据范围")

    emails = [str(email).strip().lower() for email in payload.emails]
    for email in emails:
        user = db.scalar(select(User).where(func.lower(User.email) == email))
        if user:
            existing_membership = db.scalar(
                select(Membership).where(
                    Membership.tenant_id == membership.tenant_id,
                    Membership.user_id == user.id,
                )
            )
            if existing_membership:
                raise HTTPException(status_code=409, detail=f"{email} 已是本企业成员")
        pending_invitation = db.scalar(
            select(MemberInvitation).where(
                MemberInvitation.tenant_id == membership.tenant_id,
                MemberInvitation.email == email,
                MemberInvitation.status == "pending",
                MemberInvitation.expires_at > datetime.now(timezone.utc),
            )
        )
        if pending_invitation:
            raise HTTPException(status_code=409, detail=f"{email} 已有待处理邀请")

    seats_used = _seat_usage(db, membership.tenant_id)
    if seats_used + len(emails) > tenant.seat_limit:
        raise HTTPException(
            status_code=409,
            detail=f"席位已满（{seats_used}/{tenant.seat_limit}），请联系平台扩容",
        )

    created: list[CreatedMemberInvitationResponse] = []
    now = datetime.now(timezone.utc)
    for email in emails:
        token = secrets.token_urlsafe(32)
        invitation = MemberInvitation(
            tenant_id=membership.tenant_id,
            email=email,
            role=payload.role,
            custom_role_id=custom_role.id if custom_role else None,
            organization_id=organization.id,
            token_hash=hashlib.sha256(token.encode()).hexdigest(),
            status="pending",
            invited_by=membership.user_id,
            expires_at=now + timedelta(days=7),
        )
        db.add(invitation)
        db.flush()
        base = _invitation_response(db, invitation)
        created.append(
            CreatedMemberInvitationResponse(
                **base.model_dump(),
                invitation_url=f"{settings.app_origin}/invite?token={token}",
            )
        )
        db.add(
            AuditEvent(
                tenant_id=membership.tenant_id,
                actor_user_id=membership.user_id,
                action="membership.invited",
                details={"invitation_id": str(invitation.id), "email": email},
            )
        )
    db.commit()
    return CreateMemberInvitationsResponse(
        invitations=created,
        seat_limit=tenant.seat_limit,
        seats_used=seats_used + len(created),
    )


@router.post(
    "/members/invitations/accept",
    response_model=CurrentUserResponse,
)
def accept_member_invitation(
    payload: AcceptMemberInvitationRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> CurrentUserResponse:
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") != settings.app_origin.rstrip("/"):
        raise HTTPException(status_code=403, detail="请求来源不合法")
    token_hash = hashlib.sha256(payload.token.encode()).hexdigest()
    invitation = db.scalar(
        select(MemberInvitation).where(MemberInvitation.token_hash == token_hash)
    )
    if invitation is None:
        raise HTTPException(status_code=410, detail="邀请链接无效或已使用")
    tenant = _lock_tenant(db, invitation.tenant_id)
    invitation = db.scalar(
        select(MemberInvitation)
        .where(
            MemberInvitation.id == invitation.id,
            MemberInvitation.tenant_id == tenant.id,
            MemberInvitation.token_hash == token_hash,
        )
        .with_for_update()
    )
    if invitation is None or invitation.status != "pending":
        raise HTTPException(status_code=410, detail="邀请链接无效或已使用")
    expires_at = invitation.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= datetime.now(timezone.utc):
        invitation.status = "expired"
        db.commit()
        raise HTTPException(status_code=410, detail="邀请链接已过期")

    organization = db.scalar(
        select(Organization).where(
            Organization.id == invitation.organization_id,
            Organization.tenant_id == invitation.tenant_id,
        )
    )
    if organization is None:
        raise HTTPException(status_code=409, detail="邀请所属部门已不可用")
    if invitation.custom_role_id:
        invitation_role = db.scalar(
            select(TenantRole).where(
                TenantRole.id == invitation.custom_role_id,
                TenantRole.tenant_id == invitation.tenant_id,
                TenantRole.is_active.is_(True),
            )
        )
        if invitation_role is None or (
            invitation_role.scope == "organization"
            and not organization_is_within_scope(
                db,
                invitation.tenant_id,
                invitation.organization_id,
                invitation_role.scope_organization_id,
            )
        ):
            raise HTTPException(status_code=409, detail="邀请角色已不可用，请联系企业管理员")
    user = db.scalar(select(User).where(func.lower(User.email) == invitation.email))
    if user:
        if not user.is_active or not verify_password(payload.password, user.password_hash):
            raise HTTPException(status_code=401, detail="邮箱或密码不正确")
        if db.scalar(
            select(Membership.id).where(
                Membership.tenant_id == invitation.tenant_id,
                Membership.user_id == user.id,
            )
        ):
            raise HTTPException(status_code=409, detail="该账号已是企业成员")
    else:
        if not payload.display_name:
            raise HTTPException(status_code=422, detail="请填写姓名")
        user = User(
            email=invitation.email,
            display_name=payload.display_name,
            password_hash=hash_password(payload.password),
        )
        db.add(user)
        db.flush()

    if _seat_usage(db, invitation.tenant_id) > tenant.seat_limit:
        raise HTTPException(status_code=409, detail="企业席位已调整，请联系企业管理员")
    member = Membership(
        tenant_id=invitation.tenant_id,
        user_id=user.id,
        organization_id=invitation.organization_id,
        role=invitation.role,
        custom_role_id=invitation.custom_role_id,
        status="active",
    )
    invitation.status = "accepted"
    db.add(member)
    db.add(
        AuditEvent(
            tenant_id=invitation.tenant_id,
            actor_user_id=user.id,
            target_user_id=user.id,
            action="membership.invitation_accepted",
            details={"invitation_id": str(invitation.id)},
        )
    )
    _create_login_session(db, user, response)
    db.commit()
    return make_current_user_response(db, user)


@router.post(
    "/members/invitations/{invitation_id}/resend",
    response_model=CreatedMemberInvitationResponse,
)
def resend_member_invitation(
    invitation_id: UUID,
    membership: Membership = Depends(require_tenant_roles("tenant_admin")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> CreatedMemberInvitationResponse:
    _lock_tenant(db, membership.tenant_id)
    invitation = db.scalar(
        select(MemberInvitation)
        .where(
            MemberInvitation.id == invitation_id,
            MemberInvitation.tenant_id == membership.tenant_id,
        )
        .with_for_update()
    )
    if invitation is None or invitation.status not in {"pending", "expired"}:
        raise HTTPException(status_code=404, detail="未找到可重新发送的邀请")
    previous_expiry = invitation.expires_at
    if previous_expiry.tzinfo is None:
        previous_expiry = previous_expiry.replace(tzinfo=timezone.utc)
    if (
        previous_expiry <= datetime.now(timezone.utc)
        and _seat_usage(db, membership.tenant_id)
        >= db.get(Tenant, membership.tenant_id).seat_limit
    ):
        raise HTTPException(
            status_code=409,
            detail="席位已满，请联系平台扩容后再重新发送邀请",
        )
    token = secrets.token_urlsafe(32)
    invitation.token_hash = hashlib.sha256(token.encode()).hexdigest()
    invitation.status = "pending"
    invitation.expires_at = datetime.now(timezone.utc) + timedelta(days=7)
    db.add(
        AuditEvent(
            tenant_id=membership.tenant_id,
            actor_user_id=membership.user_id,
            action="membership.invitation_resent",
            details={"invitation_id": str(invitation.id), "email": invitation.email},
        )
    )
    db.commit()
    return CreatedMemberInvitationResponse(
        **_invitation_response(db, invitation).model_dump(),
        invitation_url=f"{settings.app_origin}/invite?token={token}",
    )


@router.delete(
    "/members/invitations/{invitation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def revoke_member_invitation(
    invitation_id: UUID,
    membership: Membership = Depends(require_tenant_roles("tenant_admin")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> Response:
    _lock_tenant(db, membership.tenant_id)
    invitation = db.scalar(
        select(MemberInvitation).where(
            MemberInvitation.id == invitation_id,
            MemberInvitation.tenant_id == membership.tenant_id,
            MemberInvitation.status == "pending",
        )
    )
    if invitation is None:
        raise HTTPException(status_code=404, detail="未找到待撤销的邀请")
    invitation.status = "revoked"
    db.add(
        AuditEvent(
            tenant_id=membership.tenant_id,
            actor_user_id=membership.user_id,
            action="membership.invitation_revoked",
            details={"invitation_id": str(invitation.id), "email": invitation.email},
        )
    )
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch(
    "/members/{member_user_id}/status",
    response_model=MemberResponse,
)
def change_member_status(
    member_user_id: UUID,
    payload: ChangeMemberStatusRequest,
    membership: Membership = Depends(require_tenant_roles("tenant_admin")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> MemberResponse:
    _lock_tenant(db, membership.tenant_id)
    target_membership = db.scalar(
        select(Membership)
        .where(
            Membership.tenant_id == membership.tenant_id,
            Membership.user_id == member_user_id,
        )
        .with_for_update()
    )
    target_user = db.get(User, member_user_id)
    if target_membership is None or target_user is None:
        raise HTTPException(status_code=404, detail="未找到该企业成员")
    if target_membership.status == payload.status:
        return _member_response(db, target_user, target_membership)
    if payload.status == "active":
        if _seat_usage(db, membership.tenant_id) >= db.get(
            Tenant, membership.tenant_id
        ).seat_limit:
            raise HTTPException(
                status_code=409,
                detail="席位已满，请联系平台扩容后再恢复成员",
            )
    if (
        payload.status == "suspended"
        and target_membership.role == "tenant_admin"
        and target_user.is_active
    ):
        admin_count = db.scalar(
            select(func.count())
            .select_from(Membership)
            .join(User, User.id == Membership.user_id)
            .where(
                Membership.tenant_id == membership.tenant_id,
                Membership.role == "tenant_admin",
                Membership.status == "active",
                User.is_active.is_(True),
            )
        ) or 0
        if admin_count <= 1:
            raise HTTPException(status_code=409, detail="企业至少需要保留一名启用的管理员")

    previous_status = target_membership.status
    target_membership.status = payload.status
    if payload.status == "suspended":
        now = datetime.now(timezone.utc)
        db.query(AuthSession).filter(
            AuthSession.user_id == target_user.id,
            AuthSession.revoked_at.is_(None),
        ).update({"revoked_at": now}, synchronize_session=False)
    db.add(
        AuditEvent(
            tenant_id=membership.tenant_id,
            actor_user_id=membership.user_id,
            target_user_id=target_user.id,
            action=f"membership.{payload.status}",
            details={"from": previous_status, "to": payload.status},
        )
    )
    db.commit()
    return _member_response(db, target_user, target_membership)


@router.delete(
    "/members/{member_user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def remove_member(
    member_user_id: UUID,
    membership: Membership = Depends(require_tenant_roles("tenant_admin")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> Response:
    _lock_tenant(db, membership.tenant_id)
    target_membership = db.scalar(
        select(Membership)
        .where(
            Membership.tenant_id == membership.tenant_id,
            Membership.user_id == member_user_id,
        )
        .with_for_update()
    )
    target_user = db.get(User, member_user_id)
    if target_membership is None or target_user is None:
        raise HTTPException(status_code=404, detail="未找到该企业成员")
    if (
        target_membership.status == "active"
        and target_membership.role == "tenant_admin"
        and target_user.is_active
    ):
        admin_count = db.scalar(
            select(func.count())
            .select_from(Membership)
            .join(User, User.id == Membership.user_id)
            .where(
                Membership.tenant_id == membership.tenant_id,
                Membership.role == "tenant_admin",
                Membership.status == "active",
                User.is_active.is_(True),
            )
        ) or 0
        if admin_count <= 1:
            raise HTTPException(status_code=409, detail="企业至少需要保留一名启用的管理员")

    db.query(AuthSession).filter(
        AuthSession.user_id == target_user.id,
        AuthSession.revoked_at.is_(None),
    ).update({"revoked_at": datetime.now(timezone.utc)}, synchronize_session=False)
    db.add(
        AuditEvent(
            tenant_id=membership.tenant_id,
            actor_user_id=membership.user_id,
            target_user_id=target_user.id,
            action="membership.removed",
            details={"email": target_user.email},
        )
    )
    db.delete(target_membership)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch("/members/{member_user_id}/role", response_model=MemberResponse)
def change_member_role(
    member_user_id: UUID,
    payload: ChangeRoleRequest,
    membership: Membership = Depends(require_tenant_roles("tenant_admin")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> MemberResponse:
    tenant = db.scalar(
        select(Tenant)
        .where(Tenant.id == membership.tenant_id)
        .with_for_update()
    )
    if tenant is None:
        raise HTTPException(status_code=404, detail="未找到企业工作区")
    target_membership = db.scalar(
        select(Membership).where(
            Membership.tenant_id == membership.tenant_id,
            Membership.user_id == member_user_id,
        )
    )
    target_user = db.get(User, member_user_id)
    if not target_membership or not target_user:
        raise HTTPException(status_code=404, detail="未找到该企业成员")
    if member_user_id == membership.user_id:
        raise HTTPException(status_code=409, detail="不能修改自己的角色")
    custom_role: TenantRole | None = None
    if payload.role == "custom":
        custom_role = db.scalar(
            select(TenantRole).where(
                TenantRole.id == payload.custom_role_id,
                TenantRole.tenant_id == membership.tenant_id,
                TenantRole.is_active.is_(True),
            )
        )
        if custom_role is None:
            raise HTTPException(status_code=404, detail="未找到可用的自定义角色")
        if custom_role.scope == "organization" and not organization_is_within_scope(
            db,
            membership.tenant_id,
            target_membership.organization_id,
            custom_role.scope_organization_id,
        ):
            raise HTTPException(status_code=409, detail="该成员不属于此角色的数据范围")
    if (
        target_user.is_active
        and target_membership.status == "active"
        and target_membership.role == "tenant_admin"
        and payload.role != "tenant_admin"
    ):
        admin_count = db.scalar(
            select(func.count())
            .select_from(Membership)
            .join(User, User.id == Membership.user_id)
            .where(
                Membership.tenant_id == membership.tenant_id,
                Membership.role == "tenant_admin",
                Membership.status == "active",
                User.is_active.is_(True),
            )
        )
        if admin_count <= 1:
            raise HTTPException(
                status_code=409, detail="企业至少需要保留一名管理员"
            )

    previous_role = target_membership.role
    previous_custom_role_id = target_membership.custom_role_id
    target_membership.role = payload.role
    target_membership.custom_role_id = custom_role.id if custom_role else None
    db.add(
        AuditEvent(
            tenant_id=membership.tenant_id,
            actor_user_id=membership.user_id,
            target_user_id=target_user.id,
            action="membership.role_changed",
            details={
                "from": previous_role,
                "to": payload.role,
                "from_custom_role_id": (
                    str(previous_custom_role_id) if previous_custom_role_id else ""
                ),
                "custom_role_id": str(custom_role.id) if custom_role else "",
            },
        )
    )
    db.commit()
    return MemberResponse(
        id=target_user.id,
        email=target_user.email,
        display_name=target_user.display_name,
        role=target_membership.role,
        is_active=target_user.is_active,
        status=target_membership.status,
        organization_id=target_membership.organization_id,
        organization_name=db.get(
            Organization, target_membership.organization_id
        ).name,
        custom_role_id=target_membership.custom_role_id,
        custom_role_name=custom_role.name if custom_role else None,
    )


@router.get("/roles/catalog", response_model=PermissionCatalogResponse)
def get_permission_catalog(
    _: Membership = Depends(require_tenant_roles("tenant_admin")),
) -> PermissionCatalogResponse:
    return PermissionCatalogResponse(
        modules=[
            {"key": key, "label": module["label"], "actions": module["actions"]}
            for key, module in PERMISSION_CATALOG.items()
        ],
        action_labels={
            "view": "查看",
            "edit": "编辑",
            "publish": "发布",
            "delete": "删除",
            "test": "测试",
            "intervene": "介入",
            "export": "导出",
            "approve": "审批",
        },
    )


@router.get("/roles", response_model=RoleListResponse)
def list_roles(
    membership: Membership = Depends(require_tenant_roles("tenant_admin")),
    db: Session = Depends(get_db),
) -> RoleListResponse:
    roles: list[RoleResponse] = []
    for role_key, role_name in ROLE_LABELS.items():
        member_count = db.scalar(
            select(func.count())
            .select_from(Membership)
            .where(
                Membership.tenant_id == membership.tenant_id,
                Membership.role == role_key,
                Membership.custom_role_id.is_(None),
            )
        ) or 0
        permissions: dict[str, list[str]] = {}
        for permission in BUILTIN_PERMISSIONS[role_key]:
            module, action = permission.split(":", 1)
            permissions.setdefault(module, []).append(action)
        roles.append(
            RoleResponse(
                key=role_key,
                id=None,
                name=role_name,
                description="系统内置角色",
                scope="tenant",
                scope_organization_id=None,
                permissions=permissions,
                is_active=True,
                is_builtin=True,
                member_count=member_count,
            )
        )

    custom_roles = list(
        db.scalars(
            select(TenantRole)
            .where(TenantRole.tenant_id == membership.tenant_id)
            .order_by(TenantRole.created_at, TenantRole.name)
        )
    )
    for role in custom_roles:
        member_count = db.scalar(
            select(func.count())
            .select_from(Membership)
            .where(
                Membership.tenant_id == membership.tenant_id,
                Membership.custom_role_id == role.id,
            )
        ) or 0
        roles.append(
            RoleResponse(
                key=f"custom:{role.id}",
                id=role.id,
                name=role.name,
                description=role.description,
                scope=role.scope,
                scope_organization_id=role.scope_organization_id,
                permissions=role.permissions,
                is_active=role.is_active,
                is_builtin=False,
                member_count=member_count,
            )
        )
    return RoleListResponse(roles=roles)


@router.post(
    "/roles",
    response_model=RoleResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_role(
    payload: CreateRoleRequest,
    membership: Membership = Depends(require_tenant_roles("tenant_admin")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> RoleResponse:
    _lock_tenant(db, membership.tenant_id)
    _validate_role_scope(db, membership.tenant_id, payload.scope, payload.scope_organization_id)
    if db.scalar(
        select(TenantRole.id).where(
            TenantRole.tenant_id == membership.tenant_id,
            TenantRole.name == payload.name,
        )
    ):
        raise HTTPException(status_code=409, detail="角色名称已存在")
    role = TenantRole(
        tenant_id=membership.tenant_id,
        name=payload.name,
        description=payload.description.strip(),
        scope=payload.scope,
        scope_organization_id=payload.scope_organization_id,
        permissions=validate_permissions(payload.permissions),
        is_active=payload.is_active,
        created_by=membership.user_id,
    )
    db.add(role)
    db.flush()
    db.add(
        AuditEvent(
            tenant_id=membership.tenant_id,
            actor_user_id=membership.user_id,
            action="role.created",
            details={
                "role_id": str(role.id),
                "name": role.name,
                "scope": role.scope,
                "permissions": json.dumps(role.permissions, ensure_ascii=False, sort_keys=True),
            },
        )
    )
    db.commit()
    return RoleResponse(
        key=f"custom:{role.id}",
        id=role.id,
        name=role.name,
        description=role.description,
        scope=role.scope,
        scope_organization_id=role.scope_organization_id,
        permissions=role.permissions,
        is_active=role.is_active,
        is_builtin=False,
        member_count=0,
    )


@router.put("/roles/{role_id}", response_model=RoleResponse)
def update_role(
    role_id: UUID,
    payload: UpdateRoleRequest,
    membership: Membership = Depends(require_tenant_roles("tenant_admin")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> RoleResponse:
    _lock_tenant(db, membership.tenant_id)
    role = db.scalar(
        select(TenantRole).where(
            TenantRole.id == role_id,
            TenantRole.tenant_id == membership.tenant_id,
        )
    )
    if role is None:
        raise HTTPException(status_code=404, detail="未找到自定义角色")
    _validate_role_scope(db, membership.tenant_id, payload.scope, payload.scope_organization_id)
    duplicate = db.scalar(
        select(TenantRole.id).where(
            TenantRole.tenant_id == membership.tenant_id,
            TenantRole.name == payload.name,
            TenantRole.id != role.id,
        )
    )
    if duplicate:
        raise HTTPException(status_code=409, detail="角色名称已存在")
    member_ids = list(
        db.scalars(
            select(Membership.organization_id).where(
                Membership.tenant_id == membership.tenant_id,
                Membership.custom_role_id == role.id,
            )
        )
    )
    if payload.scope == "organization" and any(
        not organization_is_within_scope(
            db,
            membership.tenant_id,
            organization_id,
            payload.scope_organization_id,
        )
        for organization_id in member_ids
    ):
        raise HTTPException(status_code=409, detail="角色范围不能排除已分配成员的所属部门")
    if not payload.is_active and member_ids:
        raise HTTPException(status_code=409, detail="请先将成员改派到其他角色，再停用此角色")

    previous_name = role.name
    previous_scope = role.scope
    previous_permissions = role.permissions
    previous_is_active = role.is_active
    role.name = payload.name
    role.description = payload.description.strip()
    role.scope = payload.scope
    role.scope_organization_id = payload.scope_organization_id
    role.permissions = validate_permissions(payload.permissions)
    role.is_active = payload.is_active
    db.add(
        AuditEvent(
            tenant_id=membership.tenant_id,
            actor_user_id=membership.user_id,
            action="role.updated",
            details={
                "role_id": str(role.id),
                "from": previous_name,
                "to": role.name,
                "scope_from": previous_scope,
                "scope_to": role.scope,
                "active_from": str(previous_is_active),
                "active_to": str(role.is_active),
                "permissions_from": json.dumps(
                    previous_permissions, ensure_ascii=False, sort_keys=True
                ),
                "permissions_to": json.dumps(
                    role.permissions, ensure_ascii=False, sort_keys=True
                ),
            },
        )
    )
    db.commit()
    return RoleResponse(
        key=f"custom:{role.id}",
        id=role.id,
        name=role.name,
        description=role.description,
        scope=role.scope,
        scope_organization_id=role.scope_organization_id,
        permissions=role.permissions,
        is_active=role.is_active,
        is_builtin=False,
        member_count=0,
    )


@router.delete("/roles/{role_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_role(
    role_id: UUID,
    membership: Membership = Depends(require_tenant_roles("tenant_admin")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> Response:
    _lock_tenant(db, membership.tenant_id)
    role = db.scalar(
        select(TenantRole).where(
            TenantRole.id == role_id,
            TenantRole.tenant_id == membership.tenant_id,
        )
    )
    if role is None:
        raise HTTPException(status_code=404, detail="未找到自定义角色")
    assigned = db.scalar(
        select(func.count())
        .select_from(Membership)
        .where(
            Membership.tenant_id == membership.tenant_id,
            Membership.custom_role_id == role.id,
        )
    ) or 0
    if assigned:
        raise HTTPException(status_code=409, detail="请先将成员改派到其他角色，再删除此角色")
    db.add(
        AuditEvent(
            tenant_id=membership.tenant_id,
            actor_user_id=membership.user_id,
            action="role.deleted",
            details={"role_id": str(role.id), "name": role.name},
        )
    )
    db.delete(role)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/organizations", response_model=OrganizationListResponse)
def list_organizations(
    membership: Membership = Depends(require_tenant_permission("organizations:view")),
    db: Session = Depends(get_db),
) -> OrganizationListResponse:
    organizations = list(
        db.scalars(
            select(Organization)
            .where(Organization.tenant_id == membership.tenant_id)
            .order_by(Organization.parent_id, Organization.name)
        )
    )
    scoped_role = (
        db.scalar(
            select(TenantRole).where(
                TenantRole.id == membership.custom_role_id,
                TenantRole.tenant_id == membership.tenant_id,
                TenantRole.is_active.is_(True),
            )
        )
        if membership.custom_role_id
        else None
    )
    scope_organization_id = (
        scoped_role.scope_organization_id
        if scoped_role and scoped_role.scope == "organization"
        else None
    )
    if scope_organization_id is not None:
        organizations = [
            organization
            for organization in organizations
            if organization_is_within_scope(
                db,
                membership.tenant_id,
                organization.id,
                scope_organization_id,
            )
        ]
    organization_ids = [organization.id for organization in organizations]
    member_counts = dict(
        db.execute(
            select(Membership.organization_id, func.count(Membership.id))
            .where(Membership.organization_id.in_(organization_ids))
            .group_by(Membership.organization_id)
        ).all()
    ) if organization_ids else {}
    child_counts = dict(
        db.execute(
            select(Organization.parent_id, func.count(Organization.id))
            .where(Organization.parent_id.in_(organization_ids))
            .group_by(Organization.parent_id)
        ).all()
    ) if organization_ids else {}
    by_id = {organization.id: organization for organization in organizations}
    children: dict[UUID, list[Organization]] = {}
    roots: list[Organization] = []
    for organization in sorted(organizations, key=lambda item: item.name):
        if organization.parent_id is None:
            roots.append(organization)
        else:
            children.setdefault(organization.parent_id, []).append(organization)
    for siblings in children.values():
        siblings.sort(key=lambda item: item.name)

    def depth_of(organization: Organization) -> int:
        depth = 0
        parent_id = organization.parent_id
        while parent_id is not None:
            parent = by_id.get(parent_id)
            if parent is None:
                if scope_organization_id is not None:
                    break
                raise RuntimeError("组织树存在不属于当前租户的父节点")
            depth += 1
            parent_id = parent.parent_id
        return depth

    ordered_organizations: list[Organization] = []

    def append_subtree(organization: Organization) -> None:
        ordered_organizations.append(organization)
        for child in children.get(organization.id, []):
            append_subtree(child)

    if scope_organization_id is not None:
        for organization in organizations:
            if organization.id == scope_organization_id:
                append_subtree(organization)
    else:
        for root in roots:
            append_subtree(root)
    if scope_organization_id is None and len(roots) != 1:
        raise RuntimeError("企业必须且只能有一个根组织")

    return OrganizationListResponse(
        tenant_id=membership.tenant_id,
        organizations=[
            OrganizationResponse(
                id=organization.id,
                tenant_id=organization.tenant_id,
                parent_id=organization.parent_id,
                name=organization.name,
                depth=depth_of(organization),
                member_count=member_counts.get(organization.id, 0),
                child_count=child_counts.get(organization.id, 0),
                is_root=organization.parent_id is None,
            )
            for organization in ordered_organizations
        ],
    )


@router.post(
    "/organizations",
    response_model=OrganizationResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_organization(
    payload: CreateOrganizationRequest,
    membership: Membership = Depends(require_tenant_roles("tenant_admin")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> OrganizationResponse:
    tenant = db.scalar(
        select(Tenant)
        .where(Tenant.id == membership.tenant_id)
        .with_for_update()
    )
    if tenant is None:
        raise HTTPException(status_code=404, detail="未找到企业工作区")

    roots = list(
        db.scalars(
            select(Organization).where(
                Organization.tenant_id == membership.tenant_id,
                Organization.parent_id.is_(None),
            )
        )
    )
    if len(roots) != 1:
        raise RuntimeError("企业必须且只能有一个根组织")
    parent_id = payload.parent_id or roots[0].id
    parent = db.scalar(
        select(Organization).where(
            Organization.id == parent_id,
            Organization.tenant_id == membership.tenant_id,
        )
    )
    if parent is None:
        raise HTTPException(status_code=404, detail="未找到所属部门")
    duplicate = db.scalar(
        select(Organization.id).where(
            Organization.tenant_id == membership.tenant_id,
            Organization.parent_id == parent.id,
            Organization.name == payload.name,
        )
    )
    if duplicate:
        raise HTTPException(status_code=409, detail="同一部门下不能有重名部门")

    organization = Organization(
        tenant_id=membership.tenant_id,
        parent_id=parent.id,
        name=payload.name,
    )
    db.add(organization)
    db.flush()
    db.add(
        AuditEvent(
            tenant_id=membership.tenant_id,
            actor_user_id=membership.user_id,
            action="organization.created",
            details={"organization_id": str(organization.id), "name": organization.name},
        )
    )
    db.commit()
    return OrganizationResponse(
        id=organization.id,
        tenant_id=organization.tenant_id,
        parent_id=organization.parent_id,
        name=organization.name,
        depth=depth_of_new_organization(db, organization),
        member_count=0,
        child_count=0,
        is_root=False,
    )


@router.patch("/organizations/{organization_id}", response_model=OrganizationResponse)
def rename_organization(
    organization_id: UUID,
    payload: RenameOrganizationRequest,
    membership: Membership = Depends(require_tenant_roles("tenant_admin")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> OrganizationResponse:
    tenant = db.scalar(
        select(Tenant)
        .where(Tenant.id == membership.tenant_id)
        .with_for_update()
    )
    if tenant is None:
        raise HTTPException(status_code=404, detail="未找到企业工作区")
    organization = db.scalar(
        select(Organization).where(
            Organization.id == organization_id,
            Organization.tenant_id == membership.tenant_id,
        )
    )
    if organization is None:
        raise HTTPException(status_code=404, detail="未找到部门")
    if organization.parent_id is None:
        raise HTTPException(
            status_code=409,
            detail="根组织名称需在企业信息中修改",
        )
    duplicate = db.scalar(
        select(Organization.id).where(
            Organization.tenant_id == membership.tenant_id,
            Organization.parent_id == organization.parent_id,
            Organization.name == payload.name,
            Organization.id != organization.id,
        )
    )
    if duplicate:
        raise HTTPException(status_code=409, detail="同一部门下不能有重名部门")

    previous_name = organization.name
    organization.name = payload.name
    db.add(
        AuditEvent(
            tenant_id=membership.tenant_id,
            actor_user_id=membership.user_id,
            action="organization.renamed",
            details={
                "organization_id": str(organization.id),
                "from": previous_name,
                "to": organization.name,
            },
        )
    )
    db.commit()
    return organization_response(db, organization)


@router.delete("/organizations/{organization_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_organization(
    organization_id: UUID,
    membership: Membership = Depends(require_tenant_roles("tenant_admin")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> Response:
    tenant = db.scalar(
        select(Tenant)
        .where(Tenant.id == membership.tenant_id)
        .with_for_update()
    )
    if tenant is None:
        raise HTTPException(status_code=404, detail="未找到企业工作区")
    organization = db.scalar(
        select(Organization).where(
            Organization.id == organization_id,
            Organization.tenant_id == membership.tenant_id,
        )
    )
    if organization is None:
        raise HTTPException(status_code=404, detail="未找到部门")
    if organization.parent_id is None:
        raise HTTPException(status_code=409, detail="根组织不能删除")
    child_count = db.scalar(
        select(func.count()).select_from(Organization).where(
            Organization.parent_id == organization.id
        )
    )
    member_count = db.scalar(
        select(func.count()).select_from(Membership).where(
            Membership.organization_id == organization.id
        )
    )
    if child_count or member_count:
        raise HTTPException(
            status_code=409,
            detail="该部门仍有子部门或成员，请先迁移后再删除",
        )
    db.add(
        AuditEvent(
            tenant_id=membership.tenant_id,
            actor_user_id=membership.user_id,
            action="organization.deleted",
            details={"organization_id": str(organization.id), "name": organization.name},
        )
    )
    db.delete(organization)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch(
    "/members/{member_user_id}/organization",
    response_model=MemberResponse,
)
def change_member_organization(
    member_user_id: UUID,
    payload: ChangeMemberOrganizationRequest,
    membership: Membership = Depends(require_tenant_roles("tenant_admin")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> MemberResponse:
    tenant = db.scalar(
        select(Tenant)
        .where(Tenant.id == membership.tenant_id)
        .with_for_update()
    )
    if tenant is None:
        raise HTTPException(status_code=404, detail="未找到企业工作区")
    target_membership = db.scalar(
        select(Membership).where(
            Membership.tenant_id == membership.tenant_id,
            Membership.user_id == member_user_id,
        )
    )
    target_user = db.get(User, member_user_id)
    if target_membership is None or target_user is None:
        raise HTTPException(status_code=404, detail="未找到该企业成员")
    organization = db.scalar(
        select(Organization).where(
            Organization.id == payload.organization_id,
            Organization.tenant_id == membership.tenant_id,
        )
    )
    if organization is None:
        raise HTTPException(status_code=404, detail="未找到所属部门")
    if target_membership.custom_role_id:
        assigned_role = db.scalar(
            select(TenantRole).where(
                TenantRole.id == target_membership.custom_role_id,
                TenantRole.tenant_id == membership.tenant_id,
            )
        )
        if (
            assigned_role
            and assigned_role.scope == "organization"
            and not organization_is_within_scope(
                db,
                membership.tenant_id,
                organization.id,
                assigned_role.scope_organization_id,
            )
        ):
            raise HTTPException(
                status_code=409,
                detail="目标部门超出成员当前角色的数据范围，请先调整角色",
            )

    previous_organization_id = target_membership.organization_id
    target_membership.organization_id = organization.id
    db.add(
        AuditEvent(
            tenant_id=membership.tenant_id,
            actor_user_id=membership.user_id,
            target_user_id=target_user.id,
            action="membership.organization_changed",
            details={
                "from": str(previous_organization_id),
                "to": str(organization.id),
            },
        )
    )
    db.commit()
    return MemberResponse(
        id=target_user.id,
        email=target_user.email,
        display_name=target_user.display_name,
        role=target_membership.role,
        is_active=target_user.is_active,
        status=target_membership.status,
        organization_id=organization.id,
        organization_name=organization.name,
        custom_role_id=target_membership.custom_role_id,
        custom_role_name=(
            assigned_role.name
            if target_membership.custom_role_id and assigned_role
            else None
        ),
    )


def organization_response(
    db: Session, organization: Organization
) -> OrganizationResponse:
    member_count = db.scalar(
        select(func.count()).select_from(Membership).where(
            Membership.organization_id == organization.id
        )
    ) or 0
    child_count = db.scalar(
        select(func.count()).select_from(Organization).where(
            Organization.parent_id == organization.id
        )
    ) or 0
    depth = depth_of_new_organization(db, organization)
    return OrganizationResponse(
        id=organization.id,
        tenant_id=organization.tenant_id,
        parent_id=organization.parent_id,
        name=organization.name,
        depth=depth,
        member_count=member_count,
        child_count=child_count,
        is_root=organization.parent_id is None,
    )


def depth_of_new_organization(db: Session, organization: Organization) -> int:
    depth = 0
    parent_id = organization.parent_id
    while parent_id is not None:
        parent = db.get(Organization, parent_id)
        if parent is None or parent.tenant_id != organization.tenant_id:
            raise RuntimeError("组织树存在不属于当前租户的父节点")
        depth += 1
        parent_id = parent.parent_id
    return depth


def _lock_tenant(db: Session, tenant_id: UUID) -> Tenant:
    tenant = db.scalar(
        select(Tenant).where(Tenant.id == tenant_id).with_for_update()
    )
    if tenant is None:
        raise HTTPException(status_code=404, detail="未找到企业工作区")
    return tenant


def _validate_role_scope(
    db: Session,
    tenant_id: UUID,
    scope: str,
    scope_organization_id: UUID | None,
) -> None:
    if scope == "tenant":
        if scope_organization_id is not None:
            raise HTTPException(status_code=422, detail="租户范围角色不能指定组织")
        return
    if scope != "organization" or scope_organization_id is None:
        raise HTTPException(status_code=422, detail="组织范围角色必须指定组织")
    organization = db.scalar(
        select(Organization.id).where(
            Organization.id == scope_organization_id,
            Organization.tenant_id == tenant_id,
        )
    )
    if organization is None:
        raise HTTPException(status_code=404, detail="角色范围组织不属于当前企业")


def make_current_user_response(db: Session, user: User) -> CurrentUserResponse:
    memberships = list(
        db.execute(
            select(Tenant, Membership, Organization.name, TenantRole)
            .join(Membership, Membership.tenant_id == Tenant.id)
            .join(Organization, Organization.id == Membership.organization_id)
            .outerjoin(TenantRole, TenantRole.id == Membership.custom_role_id)
            .where(
                Membership.user_id == user.id,
                Membership.status == "active",
                Tenant.status == "active",
            )
            .order_by(Tenant.name)
        )
    )
    return CurrentUserResponse(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        is_platform_admin=user.is_platform_admin,
        memberships=[
            TenantMembershipResponse(
                id=tenant.id,
                name=tenant.name,
                slug=tenant.slug,
                role=member.role,
                custom_role_id=member.custom_role_id,
                custom_role_name=custom_role.name if custom_role else None,
                organization_id=member.organization_id,
                permissions=sorted(
                    flatten_permissions(custom_role.permissions)
                    if custom_role and custom_role.is_active
                    else BUILTIN_PERMISSIONS.get(member.role, frozenset())
                ),
            )
            for tenant, member, _, custom_role in memberships
        ],
    )
