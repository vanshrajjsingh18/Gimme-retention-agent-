"""Password hashing, JWT issuing and API-key hashing helpers."""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.core.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

API_KEY_PREFIX = "gimme_sk_"


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return pwd_context.verify(plain, hashed)
    except ValueError:
        return False


def create_access_token(subject: str, extra: dict[str, Any] | None = None) -> str:
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": subject,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)).timestamp()),
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def decode_access_token(token: str) -> dict[str, Any] | None:
    try:
        return jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    except JWTError:
        return None


def generate_api_key() -> tuple[str, str, str]:
    """Return (full_key, key_prefix, key_hash).

    Only the hash and a short display prefix are persisted; the full key is
    shown to the operator exactly once.
    """
    raw = secrets.token_urlsafe(32)
    full_key = f"{API_KEY_PREFIX}{raw}"
    return full_key, full_key[: len(API_KEY_PREFIX) + 6], hash_api_key(full_key)


def hash_api_key(full_key: str) -> str:
    return hashlib.sha256(f"{settings.SECRET_KEY}:{full_key}".encode()).hexdigest()


def api_keys_match(full_key: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_api_key(full_key), stored_hash)
