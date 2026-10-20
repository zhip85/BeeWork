import secrets
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

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
from beework.models import AuditEvent, Membership, Tenant, User
from beework.schemas import (
    ChangeRoleRequest,
    CreateTenantRequest,
    CurrentUserResponse,
    LoginRequest,
    MemberListResponse,
    MemberResponse,
    PlatformTenantResponse,
    TenantMembershipResponse,
)
from beework.security import TOKEN_LIFETIME_HOURS, create_access_token, verify_password

router = APIRouter(prefix="/api/v1")


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
        raise HTTPException(status_code=401, detail="邮箱或密码不正确")

    session_token = create_access_token(user.id)
    csrf_token = secrets.token_urlsafe(32)
    cookie_options = {
        "max_age": TOKEN_LIFETIME_HOURS * 60 * 60,
        "secure": settings.auth_cookie_secure,
        "samesite": "strict",
        "path": "/",
    }
    response.set_cookie(
        ACCESS_COOKIE, session_token, httponly=True, **cookie_options
    )
    response.set_cookie(CSRF_COOKIE, csrf_token, httponly=False, **cookie_options)
    return make_current_user_response(db, user)


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    response: Response,
    _: None = Depends(require_csrf),
) -> Response:
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
    rows = db.execute(
        select(Tenant, Membership.role)
        .join(Membership, Membership.tenant_id == Tenant.id)
        .where(Membership.user_id == user.id, Tenant.status == "active")
        .order_by(Tenant.name)
    )
    return [
        TenantMembershipResponse(
            id=tenant.id, name=tenant.name, slug=tenant.slug, role=role
        )
        for tenant, role in rows
    ]


@router.post(
    "/platform/tenants",
    response_model=TenantMembershipResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_tenant(
    payload: CreateTenantRequest,
    user: User = Depends(get_current_user),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> TenantMembershipResponse:
    if not user.is_platform_admin:
        raise HTTPException(status_code=403, detail="仅平台管理员可创建企业")
    existing = db.scalar(select(Tenant).where(Tenant.slug == payload.slug))
    if existing:
        raise HTTPException(status_code=409, detail="企业标识已被使用")
    tenant = Tenant(name=payload.name, slug=payload.slug)
    db.add(tenant)
    db.flush()
    db.add(Membership(tenant_id=tenant.id, user_id=user.id, role="tenant_admin"))
    db.add(
        AuditEvent(
            tenant_id=tenant.id,
            actor_user_id=user.id,
            action="tenant.created",
            details={"slug": tenant.slug},
        )
    )
    db.commit()
    db.refresh(tenant)
    return TenantMembershipResponse(
        id=tenant.id, name=tenant.name, slug=tenant.slug, role="tenant_admin"
    )


@router.get("/platform/tenants", response_model=list[PlatformTenantResponse])
def list_platform_tenants(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[PlatformTenantResponse]:
    if not user.is_platform_admin:
        raise HTTPException(status_code=403, detail="仅平台管理员可查看全部企业")
    tenants = db.scalars(select(Tenant).order_by(Tenant.created_at.desc()))
    return [
        PlatformTenantResponse(
            id=tenant.id,
            name=tenant.name,
            slug=tenant.slug,
            status=tenant.status,
        )
        for tenant in tenants
    ]


@router.get("/members", response_model=MemberListResponse)
def list_members(
    membership: Membership = Depends(require_tenant_roles("tenant_admin")),
    db: Session = Depends(get_db),
) -> MemberListResponse:
    members = db.execute(
        select(User, Membership.role, Membership.tenant_id)
        .join(Membership, Membership.user_id == User.id)
        .where(Membership.tenant_id == membership.tenant_id)
        .order_by(User.display_name, User.email)
    )
    return MemberListResponse(
        tenant_id=membership.tenant_id,
        members=[
            MemberResponse(
                id=user.id,
                email=user.email,
                display_name=user.display_name,
                role=role,
                is_active=user.is_active,
            )
            for user, role, _ in members
        ],
    )


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
    if (
        target_membership.role == "tenant_admin"
        and payload.role != "tenant_admin"
    ):
        admin_count = db.scalar(
            select(func.count())
            .select_from(Membership)
            .where(
                Membership.tenant_id == membership.tenant_id,
                Membership.role == "tenant_admin",
            )
        )
        if admin_count <= 1:
            raise HTTPException(
                status_code=409, detail="企业至少需要保留一名管理员"
            )

    previous_role = target_membership.role
    target_membership.role = payload.role
    db.add(
        AuditEvent(
            tenant_id=membership.tenant_id,
            actor_user_id=membership.user_id,
            target_user_id=target_user.id,
            action="membership.role_changed",
            details={"from": previous_role, "to": payload.role},
        )
    )
    db.commit()
    return MemberResponse(
        id=target_user.id,
        email=target_user.email,
        display_name=target_user.display_name,
        role=target_membership.role,
        is_active=target_user.is_active,
    )


def make_current_user_response(db: Session, user: User) -> CurrentUserResponse:
    memberships = list(
        db.execute(
            select(Tenant, Membership.role)
            .join(Membership, Membership.tenant_id == Tenant.id)
            .where(Membership.user_id == user.id, Tenant.status == "active")
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
                id=tenant.id, name=tenant.name, slug=tenant.slug, role=role
            )
            for tenant, role in memberships
        ],
    )
