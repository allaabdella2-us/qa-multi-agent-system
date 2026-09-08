"""Password hashing, token issue/verify and the request-scoped identity."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db
from .models import User

PBKDF2_ALGORITHM = "pbkdf2_sha256"
PBKDF2_ITERATIONS = 260_000

bearer_scheme = HTTPBearer(auto_error=False)


def hash_password(password: str, salt: bytes | None = None) -> str:
    """Encode a password as ``pbkdf2_sha256$<iterations>$<salt>$<hash>``."""
    if salt is None:
        salt = secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return f"{PBKDF2_ALGORITHM}${PBKDF2_ITERATIONS}${salt.hex()}${derived.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_hex, hash_hex = encoded.split("$")
        salt = bytes.fromhex(salt_hex)
        rounds = int(iterations)
    except ValueError:
        return False
    if algorithm != PBKDF2_ALGORITHM:
        return False
    derived = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, rounds)
    return hmac.compare_digest(derived.hex(), hash_hex)


def create_access_token(user: User) -> str:
    now = datetime.now(UTC)
    claims = {
        "sub": str(user.id),
        "org_id": user.org_id,
        "role": user.role,
        "iat": now,
        "exp": now + timedelta(minutes=settings.access_token_ttl_minutes),
    }
    return jwt.encode(claims, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict[str, Any]:
    """Decode a bearer token, raising :class:`jwt.PyJWTError` if it is not valid."""
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])


def unauthorized(message: str = "Not authenticated") -> HTTPException:
    return HTTPException(
        status_code=401,
        detail=message,
        headers={"WWW-Authenticate": "Bearer"},
    )


def current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    db: Annotated[Session, Depends(get_db)],
) -> User:
    if credentials is None or not credentials.credentials:
        raise unauthorized()
    try:
        claims = decode_access_token(credentials.credentials)
    except jwt.PyJWTError:
        raise unauthorized("Invalid or expired token") from None

    try:
        user_id = int(claims["sub"])
    except (KeyError, TypeError, ValueError):
        raise unauthorized("Invalid or expired token") from None

    user = db.scalars(select(User).where(User.id == user_id)).one_or_none()
    if user is None:
        raise unauthorized("Invalid or expired token")
    return user


CurrentUser = Annotated[User, Depends(current_user)]


def require_role(*roles: str):
    """Build a dependency that admits only the given roles."""

    def dependency(user: CurrentUser) -> User:
        if user.role not in roles:
            raise HTTPException(
                status_code=403,
                detail=f"Role '{user.role}' is not permitted to perform this action",
            )
        return user

    return dependency


require_admin = require_role("admin")
AdminUser = Annotated[User, Depends(require_admin)]
