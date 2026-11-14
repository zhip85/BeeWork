"""Login failure lock-out, per-IP throttling and session idle rules (T-004).

Counters live in PostgreSQL (``login_throttles``) behind row locks, so any number of API instances agree.
``now`` is the single clock for this module and session validation; tests replace it.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from beework.errors import ApiError
from beework.models import AuditEvent, LoginThrottle, Membership, User
from beework.notifications import notify

ACCOUNT_MAX_FAILURES = 5
ACCOUNT_LOCK = timedelta(minutes=15)
IP_MAX_FAILURES = 20
IP_WINDOW = timedelta(minutes=1)
SESSION_ACTIVITY_WRITE_INTERVAL = timedelta(seconds=30)  # touching the row on every request would be wasteful


def now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _row(db: Session, scope: str, key: str, *, create: bool) -> LoginThrottle | None:
    statement = select(LoginThrottle).where(LoginThrottle.scope == scope, LoginThrottle.key == key).with_for_update()
    row = db.scalar(statement)
    if row is None and create:
        try:
            with db.begin_nested():
                db.add(LoginThrottle(scope=scope, key=key, window_started_at=now()))
                db.flush()
        except IntegrityError:  # another request created it first
            pass
        row = db.scalar(statement)
    return row


def _minutes(delta: timedelta) -> int:
    return max(1, int(delta.total_seconds() // 60) + (1 if delta.total_seconds() % 60 else 0))


# ---------------------------------------------------------------------------
# Before the password check.
# ---------------------------------------------------------------------------


def ensure_not_throttled(db: Session, *, ip: str | None, email: str) -> None:
    """Refuse a login attempt from a throttled IP (429) or for a locked account (423); no password work is done."""
    current = now()
    if ip:
        row = _row(db, "ip", ip, create=False)
        if row is not None:
            started = _aware(row.window_started_at)
            if current - started < IP_WINDOW and row.failed_count >= IP_MAX_FAILURES:
                wait = max(1, int((started + IP_WINDOW - current).total_seconds()))
                raise ApiError(429, "auth.rate_limited", "登录尝试过于频繁，请稍后再试", {"Retry-After": str(wait)})
    row = _row(db, "account", email, create=False)
    if row is not None and row.locked_until is not None:
        until = _aware(row.locked_until)
        if until > current:
            raise _locked(until - current)
        _release(db, row, email)  # the lock has run out: clear it and record the unlock
        db.commit()


def _locked(remaining: timedelta) -> ApiError:
    minutes = _minutes(remaining)
    return ApiError(
        423, "auth.locked", f"登录失败次数过多，账号已被锁定，请在 {minutes} 分钟后重试",
        {"Retry-After": str(max(1, int(remaining.total_seconds())))},
    )


def _release(db: Session, row: LoginThrottle, email: str) -> None:
    row.locked_until = None
    row.failed_count = 0
    user = db.scalar(select(User).where(User.email == email))
    if user is not None:
        _audit(db, user, "auth.account_unlocked", "success")


# ---------------------------------------------------------------------------
# After the password check.
# ---------------------------------------------------------------------------


def record_failure(db: Session, *, ip: str | None, email: str, user: User | None) -> ApiError | None:
    """Count a failed login. Returns the lock-out error when this failure locked the account."""
    current = now()
    if ip:
        row = _row(db, "ip", ip, create=True)
        if current - _aware(row.window_started_at) >= IP_WINDOW:
            row.window_started_at, row.failed_count = current, 0
        row.failed_count += 1
        row.last_failed_at = current
    account = _row(db, "account", email, create=True)
    # Failures older than the lock period no longer count as "consecutive".
    if account.last_failed_at is not None and current - _aware(account.last_failed_at) >= ACCOUNT_LOCK:
        account.failed_count = 0
    account.failed_count += 1
    account.last_failed_at = current
    locked: ApiError | None = None
    if account.failed_count >= ACCOUNT_MAX_FAILURES and account.locked_until is None:
        account.locked_until = current + ACCOUNT_LOCK
        locked = _locked(ACCOUNT_LOCK)
        if user is not None:
            _audit(db, user, "auth.account_locked", "warning")
            _notify_admins(db, user)
    return locked


def record_success(db: Session, *, email: str) -> None:
    db.execute(
        delete(LoginThrottle).where(LoginThrottle.scope == "account", LoginThrottle.key == email).execution_options(synchronize_session=False)
    )


def _audit(db: Session, user: User, action: str, result: str) -> None:
    tenant_ids: list[UUID | None] = list(
        db.scalars(select(Membership.tenant_id).where(Membership.user_id == user.id, Membership.status == "active"))
    ) or [None]
    for tenant_id in tenant_ids:
        db.add(AuditEvent(tenant_id=tenant_id, actor_user_id=None, target_user_id=user.id, action=action, result=result, details={"email": user.email}))


def _notify_admins(db: Session, user: User) -> None:
    from beework.events import emit_event

    tenant_ids = list(db.scalars(select(Membership.tenant_id).where(Membership.user_id == user.id, Membership.status == "active")))
    for tenant_id in tenant_ids:
        emit_event(db, tenant_id, "security.event", {"kind": "login_locked", "user_id": str(user.id)})  # who is told is up to the tenant's notification rules
        notify(
            db, tenant_id=tenant_id, role="tenant_admin", type="security.account_locked",
            title=f"账号 {user.email} 因连续登录失败被锁定 15 分钟", link="/members",
            params={"email": user.email},
        )


def release_expired_locks(db: Session) -> int:
    """Scheduled sweep: unlock accounts whose lock ran out (writing the audit) and drop stale counters."""
    current = now()
    released = 0
    for row in db.scalars(
        select(LoginThrottle).where(LoginThrottle.scope == "account", LoginThrottle.locked_until.is_not(None)).with_for_update()
    ):
        if _aware(row.locked_until) <= current:
            _release(db, row, row.key)
            released += 1
    cutoff = current - timedelta(days=1)
    db.execute(
        delete(LoginThrottle).where(
            LoginThrottle.locked_until.is_(None),
            (LoginThrottle.last_failed_at.is_(None)) | (LoginThrottle.last_failed_at < cutoff),
        ).execution_options(synchronize_session=False)
    )
    return released
