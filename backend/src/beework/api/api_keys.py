from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from beework.api.assistants import _actor_name
from beework.api.conventions import ApiError
from beework.api_keys import generate_key
from beework.auth import require_csrf
from beework.db import get_db
from beework.entitlements import environments_for
from beework.models import AuditEvent, Membership, Tenant, utc_now
from beework.models.channels import TenantApiKey
from beework.rbac import require_tenant_permission
from beework.schemas.open_api import ApiKeyCreate, ApiKeyCreated, ApiKeyList, ApiKeyResponse

router = APIRouter()


def _status(key: TenantApiKey) -> str:
    if key.revoked_at is not None:
        return "revoked"
    if key.expires_at is not None:
        expires = key.expires_at if key.expires_at.tzinfo else key.expires_at.replace(tzinfo=utc_now().tzinfo)
        if expires <= utc_now():
            return "expired"
    return "active"


def _response(key: TenantApiKey) -> ApiKeyResponse:
    return ApiKeyResponse(
        id=key.id, name=key.name, environment=key.environment, scopes=list(key.scopes or []), masked=key.masked,
        created_by_name=key.created_by_name, created_at=key.created_at, expires_at=key.expires_at,
        last_used_at=key.last_used_at, revoked_at=key.revoked_at, status=_status(key),
    )


@router.get("/tenant/api-keys", response_model=ApiKeyList)
def list_api_keys(
    membership: Membership = Depends(require_tenant_permission("settings:view")),
    db: Session = Depends(get_db),
) -> ApiKeyList:
    keys = db.scalars(select(TenantApiKey).where(TenantApiKey.tenant_id == membership.tenant_id).order_by(TenantApiKey.created_at.desc()))
    return ApiKeyList(items=[_response(key) for key in keys])


@router.post("/tenant/api-keys", response_model=ApiKeyCreated, status_code=status.HTTP_201_CREATED)
def create_api_key(
    payload: ApiKeyCreate,
    membership: Membership = Depends(require_tenant_permission("settings:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> ApiKeyCreated:
    tenant = db.get(Tenant, membership.tenant_id)
    if tenant is None or payload.environment not in environments_for(db, tenant.id):
        raise ApiError(403, "environment.unavailable", "当前套餐仅提供生产环境，开发与预发环境需企业版")
    if payload.expires_at is not None:
        expires = payload.expires_at if payload.expires_at.tzinfo else payload.expires_at.replace(tzinfo=utc_now().tzinfo)
        if expires <= utc_now():
            raise ApiError(422, "validation.invalid", "过期时间必须晚于当前时间")
    plaintext, digest, masked = generate_key(payload.environment)
    key = TenantApiKey(
        tenant_id=membership.tenant_id, name=payload.name, environment=payload.environment,
        scopes=sorted(set(payload.scopes)), key_hash=digest, masked=masked, created_by=membership.user_id,
        created_by_name=_actor_name(db, membership), expires_at=payload.expires_at,
    )
    db.add(key)
    db.flush()
    db.add(
        AuditEvent(
            tenant_id=membership.tenant_id, actor_user_id=membership.user_id, action="api_key.created",
            details={"api_key_id": str(key.id), "name": key.name, "environment": key.environment, "masked": masked, "scopes": ",".join(key.scopes)},
        )
    )
    db.commit()
    return ApiKeyCreated(**_response(key).model_dump(), key=plaintext)


@router.post("/tenant/api-keys/{key_id}/revoke", response_model=ApiKeyResponse)
def revoke_api_key(
    key_id: UUID,
    membership: Membership = Depends(require_tenant_permission("settings:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> ApiKeyResponse:
    """Takes effect immediately: the next request with this key gets 401."""
    key = db.scalar(select(TenantApiKey).where(TenantApiKey.id == key_id, TenantApiKey.tenant_id == membership.tenant_id).with_for_update())
    if key is None:
        raise ApiError(404, "not_found", "未找到指定 API 密钥")
    if key.revoked_at is not None:
        raise ApiError(409, "api_key.already_revoked", "该密钥已吊销")
    key.revoked_at = utc_now()
    key.revoked_reason = "revoked"
    db.add(
        AuditEvent(
            tenant_id=membership.tenant_id, actor_user_id=membership.user_id, action="api_key.revoked",
            details={"api_key_id": str(key.id), "name": key.name, "masked": key.masked},
        )
    )
    db.commit()
    return _response(key)


