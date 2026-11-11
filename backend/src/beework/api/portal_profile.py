"""AI portal — the employee's own profile, personal memory, notification switch and password (T-145, 演进 PRD 9.3、11.6).

Everything is the caller's own: there is no way to name another member. Every endpoint needs a portal seat (T-151);
the administrator's switches (profile editing, personal memory, task notifications) are enforced here (T-141).
"""

import jwt
from fastapi import APIRouter, Cookie, Depends, Request, Response, status
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from beework import login_security, portal_profile_service, portal_service
from beework.auth import ACCESS_COOKIE, require_csrf
from beework.db import get_db
from beework.errors import ApiError
from beework.memory import delete_end_user_memories
from beework.models import AuditEvent, AuthSession, Membership, Organization, User
from beework.models.conversations import EndUser
from beework.models.memory import EndUserMemory
from beework.models.portal import PortalApp
from beework.schemas.portal_profile import (
    MemoryEntry,
    MemoryResponse,
    PasswordChange,
    ProfileResponse,
    ProfileUpdate,
    SwitchBody,
)
from beework.security import decode_access_token, hash_password, verify_password

router = APIRouter()
seat = Depends(portal_service.require_portal_seat)
MEMORY_LIMIT = 200


def _gate(feature: str):
    """Seat first, then the administrator's switch for ``feature`` (403 ``portal.feature_disabled``)."""

    def dependency(membership: Membership = seat, db: Session = Depends(get_db)) -> Membership:
        if not portal_service.feature_enabled(db, membership.tenant_id, feature):
            raise ApiError(403, "portal.feature_disabled", "管理员已关闭此功能")
        return membership

    return dependency


def _audit(db: Session, membership: Membership, action: str, details: dict[str, str]) -> None:
    db.add(AuditEvent(tenant_id=membership.tenant_id, actor_user_id=membership.user_id, action=action, details=details))


def _profile_response(db: Session, membership: Membership) -> ProfileResponse:
    user = db.get(User, membership.user_id)
    organization = db.get(Organization, membership.organization_id)
    profile = portal_profile_service.get_profile(db, membership.tenant_id, membership.user_id)
    app = db.get(PortalApp, profile.default_app_id) if profile is not None and profile.default_app_id else None
    visible = {a.id for a in portal_service.visible_apps(db, membership)}
    usable = app is not None and app.id in visible  # a default that is no longer visible reads as none
    tenant_id = membership.tenant_id
    return ProfileResponse(
        display_name=user.display_name, email=user.email, department=organization.name if organization else "",
        job_title=profile.job_title if profile else "", default_app_id=app.id if usable else None,
        default_app_name=app.name if usable else None,
        can_edit=portal_service.feature_enabled(db, tenant_id, "profile_edit"),
        login_method="邮箱与密码（本地账号）",
        memory_available=portal_service.feature_enabled(db, tenant_id, "personal_memory"),
        memory_enabled=profile.personal_memory if profile else True,
        notifications_available=portal_service.feature_enabled(db, tenant_id, "task_notifications"),
        notifications_enabled=profile.task_notifications if profile else True,
    )


@router.get("/portal/me/profile", response_model=ProfileResponse)
def get_my_profile(membership: Membership = seat, db: Session = Depends(get_db)) -> ProfileResponse:
    return _profile_response(db, membership)


@router.put("/portal/me/profile", response_model=ProfileResponse)
def update_my_profile(
    payload: ProfileUpdate, membership: Membership = Depends(_gate("profile_edit")),
    _: None = Depends(require_csrf), db: Session = Depends(get_db),
) -> ProfileResponse:
    if payload.default_app_id is not None and payload.default_app_id not in {a.id for a in portal_service.visible_apps(db, membership)}:
        raise ApiError(404, "not_found", "未找到指定应用")  # an app the employee cannot use looks missing
    user = db.get(User, membership.user_id)
    profile = portal_profile_service.get_profile(db, membership.tenant_id, membership.user_id, create=True)
    changed = [
        name for name, before, after in (
            ("display_name", user.display_name, payload.display_name), ("job_title", profile.job_title, payload.job_title.strip()),
            ("default_app_id", profile.default_app_id, payload.default_app_id),
        ) if before != after
    ]
    user.display_name, profile.job_title, profile.default_app_id = payload.display_name, payload.job_title.strip(), payload.default_app_id
    if changed:
        _audit(db, membership, "portal_profile.updated", {"fields": ", ".join(changed)})
    db.flush()
    result = _profile_response(db, membership)
    db.commit()
    return result


# -- personal memory ------------------------------------------------------------------------------------------------


def _portal_end_user(db: Session, membership: Membership) -> EndUser | None:
    return db.scalar(
        select(EndUser).where(
            EndUser.tenant_id == membership.tenant_id, EndUser.channel_type == "portal", EndUser.external_id == str(membership.user_id)
        )
    )


@router.get("/portal/me/memory", response_model=MemoryResponse)
def my_memory(membership: Membership = Depends(_gate("personal_memory")), db: Session = Depends(get_db)) -> MemoryResponse:
    end_user = _portal_end_user(db, membership)
    rows = [] if end_user is None else list(
        db.scalars(
            select(EndUserMemory).where(EndUserMemory.tenant_id == membership.tenant_id, EndUserMemory.end_user_id == end_user.id)
            .order_by(EndUserMemory.created_at.desc(), EndUserMemory.id).limit(MEMORY_LIMIT)
        )
    )
    profile = portal_profile_service.get_profile(db, membership.tenant_id, membership.user_id)
    return MemoryResponse(
        enabled=profile.personal_memory if profile else True,
        items=[MemoryEntry(id=r.id, kind=r.file, content=r.content, created_at=r.created_at) for r in rows],
    )


@router.put("/portal/me/memory/settings", response_model=MemoryResponse)
def switch_my_memory(
    payload: SwitchBody, membership: Membership = Depends(_gate("personal_memory")),
    _: None = Depends(require_csrf), db: Session = Depends(get_db),
) -> MemoryResponse:
    profile = portal_profile_service.get_profile(db, membership.tenant_id, membership.user_id, create=True)
    if profile.personal_memory != payload.enabled:
        profile.personal_memory = payload.enabled
        _audit(db, membership, "portal_profile.memory_switched", {"enabled": str(payload.enabled).lower()})
    db.commit()
    return my_memory(membership, db)


@router.delete("/portal/me/memory", status_code=status.HTTP_204_NO_CONTENT)
def delete_my_memory(
    membership: Membership = Depends(_gate("personal_memory")), _: None = Depends(require_csrf), db: Session = Depends(get_db),
) -> Response:
    """Right to erasure for the employee's own memory (T-056a's deletion service); audited by it."""
    end_user = _portal_end_user(db, membership)
    if end_user is not None:
        delete_end_user_memories(db, membership.tenant_id, end_user.id, actor_user_id=membership.user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# -- notifications and password -------------------------------------------------------------------------------------


@router.put("/portal/me/notifications", response_model=ProfileResponse)
def switch_my_notifications(
    payload: SwitchBody, membership: Membership = Depends(_gate("task_notifications")),
    _: None = Depends(require_csrf), db: Session = Depends(get_db),
) -> ProfileResponse:
    profile = portal_profile_service.get_profile(db, membership.tenant_id, membership.user_id, create=True)
    if profile.task_notifications != payload.enabled:
        profile.task_notifications = payload.enabled
        _audit(db, membership, "portal_profile.notifications_switched", {"enabled": str(payload.enabled).lower()})
    db.flush()
    result = _profile_response(db, membership)
    db.commit()
    return result


@router.post("/portal/me/password", status_code=status.HTTP_204_NO_CONTENT)
def change_my_password(
    payload: PasswordChange,
    request: Request,
    membership: Membership = seat,
    _: None = Depends(require_csrf),
    session_cookie: str | None = Cookie(default=None, alias=ACCESS_COOKIE),
    db: Session = Depends(get_db),
) -> Response:
    """Local accounts only. Wrong attempts count like failed sign-ins (so this cannot be used to guess the password), and
    a new password ends every other session of the account while keeping this one."""
    user = db.get(User, membership.user_id)
    ip = request.client.host if request.client else None
    login_security.ensure_not_throttled(db, ip=ip, email=user.email)
    if not verify_password(payload.current_password, user.password_hash):
        locked = login_security.record_failure(db, ip=ip, email=user.email, user=user)
        db.commit()
        if locked is not None:
            raise locked
        raise ApiError(422, "auth.password_incorrect", "当前密码不正确")
    if verify_password(payload.new_password, user.password_hash):
        raise ApiError(422, "auth.password_unchanged", "新密码不能与当前密码相同")
    login_security.record_success(db, email=user.email)
    user.password_hash = hash_password(payload.new_password)
    try:
        _user_id, current_token = decode_access_token(session_cookie or "")
    except (jwt.InvalidTokenError, ValueError, KeyError):
        current_token = None
    db.execute(
        update(AuthSession)
        .where(AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None), AuthSession.token_id != (current_token or ""))
        .values(revoked_at=login_security.now())
    )
    _audit(db, membership, "user.password_changed", {"scope": "portal"})
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)

