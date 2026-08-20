"""Shared FastAPI dependencies: authentication and authorization."""
from __future__ import annotations

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.enums import UserRole
from app.core.security import decode_access_token, hash_api_key
from app.models.base import utcnow
from app.models.entities import ApiKey, User

bearer_scheme = HTTPBearer(auto_error=False)


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    """Resolve the authenticated dashboard user from a bearer JWT."""
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    payload = decode_access_token(credentials.credentials)
    if payload is None or not payload.get("sub"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user = db.execute(
        select(User).where(User.email == payload["sub"])
    ).scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="User is inactive or unknown."
        )
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != UserRole.ADMIN.value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This action requires an administrator account.",
        )
    return user


def require_write(user: User = Depends(get_current_user)) -> User:
    """Any role except VIEWER may mutate data."""
    if user.role == UserRole.VIEWER.value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your account has read-only access.",
        )
    return user


def get_api_key(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    db: Session = Depends(get_db),
) -> ApiKey:
    """Authenticate a machine-to-machine ingestion request."""
    if not x_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-API-Key header.",
        )

    key = db.execute(
        select(ApiKey).where(ApiKey.key_hash == hash_api_key(x_api_key))
    ).scalar_one_or_none()
    if key is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key.")
    if not key.is_active or key.revoked_at is not None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="This API key has been revoked."
        )

    key.last_used_at = utcnow()
    db.commit()
    return key
