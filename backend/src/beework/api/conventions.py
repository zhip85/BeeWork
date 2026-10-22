"""Shared API conventions: error envelope, pagination, optimistic locking, authorization and validation."""

import ipaddress
import socket
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import DateTime, ForeignKey, Integer, String, func, select
from sqlalchemy.orm import Mapped, Session, mapped_column
from sqlalchemy.types import Uuid
from starlette.exceptions import HTTPException

from beework import rbac
from beework.auth import get_current_user
from beework.models import Membership, Organization, TenantRole, User, utc_now


class ApiError(Exception):
    """Business error rendered to clients as ``{"detail": ..., "code": ...}``."""

    def __init__(self, status_code: int, code: str, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.code = code
        self.detail = detail


def error_response(
    status_code: int, code: str, detail: str, headers: dict[str, str] | None = None
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code, content={"detail": detail, "code": code}, headers=headers
    )


async def api_error_handler(request: Request, exc: Exception) -> JSONResponse:
    if isinstance(exc, ApiError):
        return error_response(exc.status_code, exc.code, exc.detail)
    if isinstance(exc, HTTPException):
        return error_response(exc.status_code, "error", str(exc.detail), exc.headers)
    return error_response(500, "error.internal", "内部服务器错误")


# ---------------------------------------------------------------------------
# Generic columns / optimistic locking mixins for versioned tenant resources.
# ---------------------------------------------------------------------------


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)


class TenantOwnedMixin:
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )


class SoftDeleteMixin:
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=None)


class VersionedMixin:
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_by: Mapped[str | None] = mapped_column(String(64), nullable=True)


def check_row_version(obj: object, expected: int, db: Session) -> None:
    """Raise a 409 conflict when the client carries a stale ``row_version``."""
    current = getattr(obj, "row_version", None)
    if current == expected:
        return
    updated_by = getattr(obj, "updated_by", None)
    detail = (
        f"该对象已被 {updated_by} 修改，请刷新后重试"
        if updated_by
        else "该对象已被其他人修改，请刷新后重试"
    )
    raise ApiError(status_code=409, code="conflict.stale", detail=detail)


# ---------------------------------------------------------------------------
# Pagination: limit <= 100, offset >= 0, envelope ``Page[T] = {items, total, limit, offset}``.
# ---------------------------------------------------------------------------

DEFAULT_PAGE_LIMIT = 20
MAX_PAGE_LIMIT = 100


@dataclass(frozen=True)
class Pagination:
    limit: int = DEFAULT_PAGE_LIMIT
    offset: int = 0


def get_pagination(
    limit: int = Query(default=DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> Pagination:
    """FastAPI dependency: ``pagination: Pagination = Depends(get_pagination)``."""
    return Pagination(limit=limit, offset=offset)


class Page[T](BaseModel):
    items: list[T]
    total: int
    limit: int
    offset: int


def paginate(db: Session, statement, pagination: Pagination | None = None) -> tuple[list, int]:
    """Return ``(items, total)``; the statement must be ordered by the caller."""
    page = pagination or Pagination()
    total = db.scalar(select(func.count()).select_from(statement.order_by(None).subquery())) or 0
    items = list(db.scalars(statement.offset(page.offset).limit(page.limit)).all())
    return items, total


# ---------------------------------------------------------------------------
# Authorization helpers.
# ---------------------------------------------------------------------------


def require_platform_admin(user: User = Depends(get_current_user)) -> User:
    if not user.is_platform_admin:
        raise ApiError(status_code=403, code="auth.forbidden", detail="仅平台管理员可执行此操作")
    return user


def ensure_resource_in_scope(membership: Membership, resource, db: Session) -> None:
    if getattr(resource, "tenant_id", None) != membership.tenant_id:
        raise ApiError(status_code=404, code="not_found", detail="未找到指定资源")
    organization_id = getattr(resource, "organization_id", None)
    if organization_id is None:
        return
    try:
        rbac.ensure_organization_scope(membership, organization_id, db)
    except HTTPException as exc:
        raise ApiError(status_code=404, code="not_found", detail="未找到指定资源") from exc


def scope_filter(statement, model, membership: Membership, db: Session):
    if membership.custom_role_id is None or membership.role == "tenant_admin":
        return statement
    custom_role = db.scalar(
        select(TenantRole).where(
            TenantRole.id == membership.custom_role_id,
            TenantRole.tenant_id == membership.tenant_id,
            TenantRole.is_active.is_(True),
        )
    )
    if (
        custom_role is None
        or custom_role.scope != "organization"
        or custom_role.scope_organization_id is None
    ):
        return statement
    allowed_ids = _organization_subtree(
        db, membership.tenant_id, custom_role.scope_organization_id
    )
    return statement.where(model.organization_id.in_(allowed_ids))


def _organization_subtree(db: Session, tenant_id: UUID, root_id: UUID) -> list[UUID]:
    allowed_ids = [root_id]
    frontier = [root_id]
    while frontier:
        children = db.execute(
            select(Organization.id).where(
                Organization.tenant_id == tenant_id,
                Organization.parent_id.in_(frontier),
            )
        ).scalars().all()
        next_frontier = [org_id for org_id in children if org_id not in allowed_ids]
        allowed_ids.extend(next_frontier)
        frontier = next_frontier
    return allowed_ids


# ---------------------------------------------------------------------------
# Name / URL validation helpers.
# ---------------------------------------------------------------------------


def normalize_name(value: str | None, max_len: int = 100) -> str:
    normalized = (value or "").strip()
    if not normalized:
        raise ApiError(status_code=400, code="validation.invalid_name", detail="名称不能为空")
    if len(normalized) > max_len:
        raise ApiError(status_code=400, code="validation.invalid_name", detail=f"名称长度不能超过 {max_len} 个字符")
    return normalized


def ensure_unique_name(db: Session, model, tenant_id: UUID, name: str, exclude_id: UUID | None = None) -> None:
    normalized = normalize_name(name)
    statement = (
        select(model)
        .where(model.tenant_id == tenant_id)
        .where(func.lower(model.name) == normalized.lower())
    )
    if exclude_id is not None:
        statement = statement.where(model.id != exclude_id)
    if db.scalars(statement.limit(1)).first() is not None:
        raise ApiError(status_code=409, code="validation.duplicate", detail=f"同租户下已存在同名对象：{normalized}")


def default_public_url_resolver(url: str) -> list[str]:
    hostname = urlsplit(url).hostname or ""
    if not hostname:
        return []
    addresses: list[str] = []
    for info in socket.getaddrinfo(hostname, None):
        host = info[4][0]
        addresses.append(str(host))
    return addresses


def validate_public_https_url(url: str, resolver: Callable[[str], list[str]] | None = None) -> str:
    resolve = resolver or default_public_url_resolver
    parsed = urlsplit(url)
    hostname = parsed.hostname or ""
    if parsed.scheme != "https" or not hostname or "." not in hostname:
        raise ApiError(status_code=400, code="security.invalid_url", detail="地址必须是公网 https 链接")
    try:
        # An IP literal is its own resolution; never trust a resolver for it.
        addresses = [str(ipaddress.ip_address(hostname))]
    except ValueError:
        try:
            addresses = resolve(url)
        except OSError as exc:
            raise ApiError(status_code=400, code="security.invalid_url", detail="地址无法解析，请检查域名") from exc
    if not addresses:
        raise ApiError(status_code=400, code="security.invalid_url", detail="地址无法解析，请检查域名")
    for raw_address in addresses:
        address = ipaddress.ip_address(raw_address.split("%")[0])
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
        ):
            raise ApiError(status_code=400, code="security.invalid_url", detail="地址不得指向内网、回环或保留地址")
    return url
