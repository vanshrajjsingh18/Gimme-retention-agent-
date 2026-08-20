"""Authentication and API key management."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_admin
from app.core.config import settings
from app.core.database import get_db
from app.core.security import (
    api_keys_match,
    create_access_token,
    generate_api_key,
    verify_password,
)
from app.models.base import utcnow
from app.models.entities import ApiKey, AuditLog, User
from app.schemas.models import (
    ApiKeyCreate,
    ApiKeyCreated,
    ApiKeyOut,
    LoginRequest,
    TokenResponse,
    UserOut,
)

router = APIRouter()


@router.post("/auth/login", response_model=TokenResponse, tags=["auth"])
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> TokenResponse:
    user = db.execute(
        select(User).where(User.email == payload.email.lower())
    ).scalar_one_or_none()

    # Same message for unknown email and wrong password: do not leak which
    # accounts exist.
    if user is None or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password.",
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="This account is disabled."
        )

    user.last_login_at = utcnow()
    db.add(AuditLog(actor=user.email, action="LOGIN", entity_type="user", entity_id=str(user.id)))
    db.commit()

    return TokenResponse(
        access_token=create_access_token(user.email, {"role": user.role, "uid": user.id}),
        expires_in_minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES,
        user=UserOut.model_validate(user),
    )


@router.get("/auth/me", response_model=UserOut, tags=["auth"])
def me(user: User = Depends(get_current_user)) -> UserOut:
    return UserOut.model_validate(user)


@router.get("/api-keys", response_model=list[ApiKeyOut], tags=["auth"])
def list_api_keys(
    db: Session = Depends(get_db), _: User = Depends(require_admin)
) -> list[ApiKeyOut]:
    keys = db.execute(select(ApiKey).order_by(ApiKey.created_at.desc())).scalars().all()
    return [ApiKeyOut.model_validate(k) for k in keys]


@router.post(
    "/api-keys",
    response_model=ApiKeyCreated,
    status_code=status.HTTP_201_CREATED,
    tags=["auth"],
)
def create_api_key(
    payload: ApiKeyCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
) -> ApiKeyCreated:
    """Create an ingestion API key.

    The full key is returned once here and never again — only its hash is
    stored.
    """
    full_key, prefix, key_hash = generate_api_key()
    key = ApiKey(
        name=payload.name,
        key_prefix=prefix,
        key_hash=key_hash,
        scopes=payload.scopes,
        created_by_id=user.id,
    )
    db.add(key)
    db.add(
        AuditLog(
            actor=user.email,
            action="API_KEY_CREATED",
            entity_type="api_key",
            entity_id=payload.name,
            detail={"scopes": payload.scopes},
        )
    )
    db.commit()
    db.refresh(key)

    return ApiKeyCreated(**ApiKeyOut.model_validate(key).model_dump(), api_key=full_key)


@router.delete("/api-keys/{key_id}", response_model=ApiKeyOut, tags=["auth"])
def revoke_api_key(
    key_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
) -> ApiKeyOut:
    key = db.get(ApiKey, key_id)
    if key is None:
        raise HTTPException(status_code=404, detail="API key not found.")
    key.is_active = False
    key.revoked_at = utcnow()
    db.add(
        AuditLog(
            actor=user.email,
            action="API_KEY_REVOKED",
            entity_type="api_key",
            entity_id=str(key_id),
        )
    )
    db.commit()
    db.refresh(key)
    return ApiKeyOut.model_validate(key)
