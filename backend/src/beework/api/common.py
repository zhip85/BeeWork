import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import HTTPException, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from beework.auth import (
    ACCESS_COOKIE,
    CSRF_COOKIE,
)
from beework.config import settings
from beework.models import (
    AuditEvent,
    AuthSession,
    Membership,
    Organization,
    Tenant,
    TenantRole,
    User,
)
from beework.rbac import (
    BUILTIN_PERMISSIONS,
    flatten_permissions,
)
from beework.schemas import (
    CurrentUserResponse,
    TenantMembershipResponse,
)
from beework.security import (
    TOKEN_LIFETIME_HOURS,
    create_access_token,
)
from beework.tenant_access import tenant_access_mode


def create_login_session(db: Session, user: User, response: Response) -> None:
    token_id = secrets.token_urlsafe(32)
    expires_at = datetime.now(UTC) + timedelta(hours=TOKEN_LIFETIME_HOURS)
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


def record_user_activity(
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


def lock_tenant(db: Session, tenant_id: UUID) -> Tenant:
    # FOR NO KEY UPDATE serialises tenant-level checks (seats, lifecycle) without
    # blocking FK key-share locks taken when other requests insert audit events.
    tenant = db.scalar(
        select(Tenant).where(Tenant.id == tenant_id).with_for_update(key_share=True)
    )
    if tenant is None:
        raise HTTPException(status_code=404, detail="未找到企业工作区")
    return tenant


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
            )
            .order_by(Tenant.name)
        )
    )
    memberships = [
        row for row in memberships
        if tenant_access_mode(row[0].status, row[1].role) is not None
    ]
    return CurrentUserResponse(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        is_platform_admin=user.is_platform_admin,
        locale=user.locale,
        memberships=[
            TenantMembershipResponse(
                id=tenant.id,
                name=tenant.name,
                slug=tenant.slug,
                role=member.role,
                custom_role_id=member.custom_role_id,
                custom_role_name=custom_role.name if custom_role else None,
                organization_id=member.organization_id,
                tenant_status=tenant.status,
                access_mode=tenant_access_mode(tenant.status, member.role),
                permissions=sorted(
                    flatten_permissions(custom_role.permissions)
                    if custom_role and custom_role.is_active
                    else BUILTIN_PERMISSIONS.get(member.role, frozenset())
                ),
            )
            for tenant, member, _, custom_role in memberships
        ],
    )
