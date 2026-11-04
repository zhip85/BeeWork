from datetime import UTC, datetime, timedelta
from uuid import UUID

import jwt
from pwdlib import PasswordHash

from beework.config import settings

password_hash = PasswordHash.recommended()
# Verified against when the account does not exist, so unknown emails cost the same time as known ones.
DUMMY_PASSWORD_HASH = password_hash.hash("not-a-real-password-for-timing-only")
TOKEN_ALGORITHM = "HS256"


def hash_password(password: str) -> str:
    return password_hash.hash(password)


def verify_password(password: str, hashed_password: str) -> bool:
    return password_hash.verify(password, hashed_password)


def create_access_token(user_id: UUID, session_id: str) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": str(user_id),
            "sid": session_id,
            "iat": now,
            "exp": now + timedelta(minutes=settings.session_max_minutes),
        },
        settings.auth_secret,
        algorithm=TOKEN_ALGORITHM,
    )


def decode_access_token(token: str) -> tuple[UUID, str]:
    payload = jwt.decode(token, settings.auth_secret, algorithms=[TOKEN_ALGORITHM])
    return UUID(payload["sub"]), str(payload["sid"])
