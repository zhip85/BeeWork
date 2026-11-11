import secrets
from datetime import UTC, timedelta
from uuid import UUID

import jwt
from fastapi import Cookie, Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from beework import login_security
from beework.config import settings
from beework.db import get_db
from beework.models import AuthSession, Membership, Tenant, User
from beework.rbac import PORTAL_ONLY_ROLES
from beework.security import decode_access_token
from beework.tenant_access import READ_METHODS, tenant_access_mode

ACCESS_COOKIE = "beework_session"
CSRF_COOKIE = "beework_csrf"


def get_current_user(
    request: Request,
    session_cookie: str | None = Cookie(default=None, alias=ACCESS_COOKIE),
    db: Session = Depends(get_db),
) -> User:
    if not session_cookie:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录")
    try:
        user_id, token_id = decode_access_token(session_cookie)
    except (jwt.InvalidTokenError, ValueError, KeyError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="登录已过期，请重新登录",
        ) from None

    user = db.get(User, user_id)
    if not user or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="账号不可用")
    current = login_security.now()
    auth_session = db.scalar(
        select(AuthSession).where(
            AuthSession.token_id == token_id,
            AuthSession.user_id == user.id,
            AuthSession.revoked_at.is_(None),
            AuthSession.expires_at > current,
        )
    )
    if auth_session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="登录已失效，请重新登录",
        )
    last_active = auth_session.last_active_at or auth_session.created_at
    last_active = last_active if last_active.tzinfo else last_active.replace(tzinfo=UTC)
    idle = current - last_active
    if idle > timedelta(minutes=settings.session_idle_minutes):
        auth_session.revoked_at = current
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="登录已因长时间无操作而失效，请重新登录",
        )
    # Every request slides the idle window, except background polling (the bell, badges), which would otherwise
    # keep an abandoned tab signed in for ever.
    if request.headers.get("X-Background-Request") != "1" and idle >= login_security.SESSION_ACTIVITY_WRITE_INTERVAL:
        auth_session.last_active_at = current
        db.commit()
    return user


def require_csrf(
    request: Request,
    csrf_cookie: str | None = Cookie(default=None, alias=CSRF_COOKIE),
    csrf_header: str | None = Header(default=None, alias="X-CSRF-Token"),
) -> None:
    if not csrf_cookie or not csrf_header or not secrets.compare_digest(
        csrf_cookie, csrf_header
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="安全校验失败，请刷新页面后重试",
        )
    origin = request.headers.get("origin")
    if not settings.origin_allowed(origin):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="请求来源不合法")


def _resolve_tenant_membership(
    request: Request, user: User, tenant_id_header: str | None, db: Session, *, writable_when_read_only: bool
) -> Membership:
    query = select(Membership).where(
        Membership.user_id == user.id,
        Membership.status == "active",
    )
    if tenant_id_header:
        try:
            tenant_id = UUID(tenant_id_header)
        except ValueError:
            raise HTTPException(status_code=400, detail="租户 ID 格式无效") from None
        query = query.where(Membership.tenant_id == tenant_id)
    memberships = list(db.scalars(query))
    if not memberships:
        raise HTTPException(status_code=403, detail="你没有此企业的访问权限")
    if len(memberships) > 1:
        raise HTTPException(status_code=400, detail="请先选择一个企业工作区")
    membership = memberships[0]
    tenant = db.get(Tenant, membership.tenant_id)
    access_mode = tenant_access_mode(tenant.status, membership.role) if tenant else None
    if access_mode is None:
        raise HTTPException(status_code=403, detail="此企业工作区已停用")
    if access_mode == "read_only" and request.method not in READ_METHODS and not writable_when_read_only:
        raise HTTPException(status_code=403, detail="企业工作区当前为只读状态，仅可查看与导出")
    return membership


def get_tenant_membership(
    request: Request,
    user: User = Depends(get_current_user),
    tenant_id_header: str | None = Header(default=None, alias="X-Tenant-ID"),
    db: Session = Depends(get_db),
) -> Membership:
    return _resolve_tenant_membership(request, user, tenant_id_header, db, writable_when_read_only=False)


def get_console_membership(membership: Membership = Depends(get_tenant_membership)) -> Membership:
    """A tenant membership that may use the management console — portal-only ``employee`` members may not."""
    if membership.role in PORTAL_ONLY_ROLES:
        raise HTTPException(status_code=403, detail="当前角色无权访问管理控制台")
    return membership


def get_tenant_membership_for_own_state(
    request: Request,
    user: User = Depends(get_current_user),
    tenant_id_header: str | None = Header(default=None, alias="X-Tenant-ID"),
    db: Session = Depends(get_db),
) -> Membership:
    """Like ``get_tenant_membership`` but still writable in a read-only workspace.

    Only for endpoints that change the caller's own per-user state (e.g. marking notifications read); the
    status-change notice itself is delivered exactly when a workspace turns read-only or suspended.
    """
    return _resolve_tenant_membership(request, user, tenant_id_header, db, writable_when_read_only=True)


def require_tenant_roles(*roles: str):
    def dependency(
        membership: Membership = Depends(get_tenant_membership),
    ) -> Membership:
        if membership.role not in roles:
            raise HTTPException(status_code=403, detail="当前角色无权执行此操作")
        return membership

    return dependency
