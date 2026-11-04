"""Tenant API keys for the open API (T-028, SET-090)."""

import hashlib
import secrets
import string
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from beework.errors import ApiError
from beework.models import Tenant, utc_now
from beework.models.channels import TenantApiKey

SCOPES = ("chat", "knowledge.read", "knowledge.write", "analytics.read")
ALPHABET = string.ascii_letters + string.digits
LAST_USED_WRITE_INTERVAL = timedelta(minutes=1)  # one write per minute, not one per request
SERVING_STATUSES = frozenset({"trial", "active", "grace"})


def hash_key(plaintext: str) -> str:
    # The key is 24 random alphanumerics (~143 bits), so an unsalted SHA-256 is enough and lets us look it up directly.
    return hashlib.sha256(plaintext.encode()).hexdigest()


def generate_key(environment: str) -> tuple[str, str, str]:
    """``(plaintext, sha256, mask)``: ``bw_live_`` for production, ``bw_test_`` for the others, plus 24 random characters."""
    prefix = "bw_live_" if environment == "production" else "bw_test_"
    plaintext = prefix + "".join(secrets.choice(ALPHABET) for _ in range(24))
    return plaintext, hash_key(plaintext), f"{plaintext[:8]}••••{plaintext[-4:]}"


def revoke_keys_of_user(db: Session, tenant_id: UUID, user_id: UUID, reason: str) -> int:
    """SET-013: a suspended or removed member's personal keys stop working immediately."""
    result = db.execute(
        update(TenantApiKey)
        .where(TenantApiKey.tenant_id == tenant_id, TenantApiKey.created_by == user_id, TenantApiKey.revoked_at.is_(None))
        .values(revoked_at=utc_now(), revoked_reason=reason)
    )
    return result.rowcount or 0


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def authenticate(db: Session, authorization: str | None) -> tuple[TenantApiKey, Tenant]:
    """Resolve ``Authorization: Bearer bw_...`` to a live key and its serving tenant, or raise the 401/403 to return."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise ApiError(401, "missing_api_key", "请在 Authorization 头中提供 Bearer API 密钥", {"WWW-Authenticate": "Bearer"})
    plaintext = authorization[7:].strip()
    key = db.scalar(select(TenantApiKey).where(TenantApiKey.key_hash == hash_key(plaintext))) if plaintext else None
    now = utc_now()
    if key is None or key.revoked_at is not None:
        raise ApiError(401, "invalid_api_key", "API 密钥无效或已吊销", {"WWW-Authenticate": "Bearer"})
    if key.expires_at is not None and _aware(key.expires_at) <= now:
        raise ApiError(401, "expired_api_key", "API 密钥已过期", {"WWW-Authenticate": "Bearer"})
    tenant = db.get(Tenant, key.tenant_id)
    if tenant is None or tenant.status not in SERVING_STATUSES:
        raise ApiError(403, "tenant_unavailable", "企业工作区当前不可用")
    if key.last_used_at is None or now - _aware(key.last_used_at) >= LAST_USED_WRITE_INTERVAL:
        key.last_used_at = now
        db.commit()
    return key, tenant


def require_scope(key: TenantApiKey, scope: str) -> None:
    if scope not in (key.scopes or []):
        raise ApiError(403, "insufficient_scope", f"该 API 密钥没有“{scope}”权限范围")
