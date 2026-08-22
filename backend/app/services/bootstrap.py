"""Database initialisation and idempotent configuration bootstrap."""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import Base, engine
from app.core.enums import UserRole
from app.core.schema import reconcile_schema
from app.core.security import hash_password
from app.integrations.registry import ensure_default_integrations
from app.models.entities import User
from app.services.brand import ensure_compliance_rules, get_brand_settings
from app.services.segments import ensure_default_segments

logger = logging.getLogger(__name__)


def create_tables() -> None:
    import app.models  # noqa: F401 - registers every table

    Base.metadata.create_all(engine)
    # create_all adds missing tables but never missing columns, which would
    # leave an existing local database failing at startup after a model gains
    # a field. Reconcile additively so nobody has to delete their data.
    added = reconcile_schema(engine)
    if added:
        logger.info(
            "Schema reconciled: added %s",
            ", ".join(f"{table}.{column}" for table, columns in added.items() for column in columns),
        )


def ensure_admin_user(db: Session) -> User:
    """Create the local development admin account if it does not exist."""
    user = db.execute(
        select(User).where(User.email == settings.ADMIN_EMAIL)
    ).scalar_one_or_none()
    if user is not None:
        return user
    user = User(
        email=settings.ADMIN_EMAIL,
        full_name="GIMME Admin",
        hashed_password=hash_password(settings.ADMIN_PASSWORD),
        role=UserRole.ADMIN.value,
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    logger.info("Created admin user %s", settings.ADMIN_EMAIL)
    return user


def bootstrap(db: Session) -> dict:
    """Idempotently ensure every piece of baseline configuration exists."""
    user = ensure_admin_user(db)
    brand = get_brand_settings(db)
    rules = ensure_compliance_rules(db)
    segments = ensure_default_segments(db)
    integrations = ensure_default_integrations(db)
    return {
        "admin_email": user.email,
        "brand": brand.company_name,
        "compliance_rules_created": rules,
        "segments_created": segments,
        "integrations_created": integrations,
    }
