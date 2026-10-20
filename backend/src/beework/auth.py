import secrets
from uuid import UUID

import jwt
from fastapi import Cookie, Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from beework.config import settings
from beework.db import get_db
from beework.models import Membership, Tenant, User
from beework.security import decode_access_token

ACCESS_COOKIE = "beework_session"
CSRF_COOKIE = "beework_csrf"


def get_current_user(
    session_cookie: str | None = Cookie(default=None, alias=ACCESS_COOKIE),
    db: Session = Depends(get_db),
) -> User:
    if not session_cookie:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录")
    try:
        user_id = decode_access_token(session_cookie)
    except (jwt.InvalidTokenError, ValueError, KeyError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="登录已过期，请重新登录",
        ) from None

    user = db.get(User, user_id)
    if not user or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="账号不可用")
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
    if origin and origin.rstrip("/") != settings.app_origin.rstrip("/"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="请求来源不合法")


def get_tenant_membership(
    user: User = Depends(get_current_user),
    tenant_id_header: str | None = Header(default=None, alias="X-Tenant-ID"),
    db: Session = Depends(get_db),
) -> Membership:
    query = select(Membership).where(Membership.user_id == user.id)
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
    if not tenant or tenant.status != "active":
        raise HTTPException(status_code=403, detail="此企业工作区已停用")
    return membership


def require_tenant_roles(*roles: str):
    def dependency(
        membership: Membership = Depends(get_tenant_membership),
    ) -> Membership:
        if membership.role not in roles:
            raise HTTPException(status_code=403, detail="当前角色无权执行此操作")
        return membership

    return dependency
