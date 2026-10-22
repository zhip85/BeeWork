from datetime import UTC, datetime

import jwt
from fastapi import APIRouter, Cookie, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from beework.api.common import (
    create_login_session,
    make_current_user_response,
    record_user_activity,
)
from beework.auth import (
    ACCESS_COOKIE,
    CSRF_COOKIE,
    get_current_user,
    require_csrf,
)
from beework.db import get_db
from beework.models import (
    AuthSession,
    User,
)
from beework.schemas import (
    CurrentUserResponse,
    LoginRequest,
    TenantMembershipResponse,
)
from beework.security import (
    decode_access_token,
    verify_password,
)

router = APIRouter()


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
            record_user_activity(
                db,
                user,
                "auth.login_failed",
                result="failure",
                details={"email": email},
            )
            db.commit()
        raise HTTPException(status_code=401, detail="邮箱或密码不正确")

    current = make_current_user_response(db, user)
    if not user.is_platform_admin and not current.memberships:
        raise HTTPException(status_code=403, detail="账号当前没有可用的企业工作区")
    create_login_session(db, user, response)
    record_user_activity(db, user, "auth.login")
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
                auth_session.revoked_at = datetime.now(UTC)
                user = db.get(User, auth_session.user_id)
                if user:
                    record_user_activity(db, user, "auth.logout")
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
