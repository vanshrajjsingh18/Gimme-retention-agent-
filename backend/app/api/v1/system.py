"""System status, audit log and demo-data management."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_admin
from app.core.config import settings
from app.core.database import get_db
from app.jobs.scheduler import scheduler_status
from app.llm.factory import get_llm_provider, provider_mode
from app.models.entities import AuditLog, Integration, SystemLog, User
from app.services.seed import summary

router = APIRouter()


@router.get("/system/status", tags=["system"])
def system_status(db: Session = Depends(get_db), _: User = Depends(get_current_user)) -> dict:
    """Operating mode and data volume, for the header and settings screens."""
    integrations = db.execute(select(Integration)).scalars().all()
    return {
        "app_name": settings.APP_NAME,
        "environment": settings.ENVIRONMENT,
        "llm": {**get_llm_provider().health(), "mode": provider_mode()},
        "integrations": [
            {
                "provider": i.provider,
                "channel": i.channel,
                "mode": i.mode,
                "status": i.status,
            }
            for i in integrations
        ],
        "mock_mode": provider_mode() == "mock"
        or all(i.mode == "mock" for i in integrations),
        "scheduler": scheduler_status(),
        "data": summary(db),
    }


@router.get("/system/audit-log", tags=["system"])
def audit_log(
    limit: int = Query(default=100, ge=1, le=500),
    action: str | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> dict:
    stmt = select(AuditLog)
    if action:
        stmt = stmt.where(AuditLog.action == action.upper())
    rows = db.execute(
        stmt.order_by(AuditLog.created_at.desc()).limit(limit)
    ).scalars().all()
    return {
        "entries": [
            {
                "id": r.id,
                "actor": r.actor,
                "action": r.action,
                "entity_type": r.entity_type,
                "entity_id": r.entity_id,
                "detail": r.detail,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ]
    }


@router.get("/system/logs", tags=["system"])
def system_logs(
    limit: int = Query(default=100, ge=1, le=500),
    level: str | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> dict:
    stmt = select(SystemLog)
    if level:
        stmt = stmt.where(SystemLog.level == level.upper())
    rows = db.execute(
        stmt.order_by(SystemLog.created_at.desc()).limit(limit)
    ).scalars().all()
    return {
        "entries": [
            {
                "id": r.id,
                "level": r.level,
                "source": r.source,
                "message": r.message,
                "context": r.context,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ]
    }


@router.post("/system/seed-demo-data", tags=["system"])
def seed_demo_data(
    customers: int = Query(default=1000, ge=10, le=5000),
    reset: bool = Query(default=True),
    include_campaigns: bool = Query(default=True),
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
) -> dict:
    """Generate the synthetic demo dataset from the dashboard.

    This is a destructive operation when ``reset`` is true: it deletes all
    customer and transactional data (configuration is preserved).
    """
    from app.services.intelligence import refresh_all_customers
    from app.services.seed import clear_demo_data, generate_customers
    from app.services.seed_campaigns import seed_campaigns
    from app.services.segments import refresh_all_segments

    if reset:
        clear_demo_data(db)

    counts = generate_customers(db, count=customers)
    refresh_all_customers(db)
    refresh_all_segments(db)

    campaign_result = {}
    if include_campaigns:
        campaign_result = seed_campaigns(db)
        refresh_all_customers(db)
        refresh_all_segments(db)

    db.add(
        AuditLog(
            actor=user.email,
            action="DEMO_DATA_SEEDED",
            entity_type="system",
            entity_id="seed",
            detail={"customers": customers, "reset": reset},
        )
    )
    db.commit()

    return {"generated": counts, "campaigns": campaign_result, "totals": summary(db)}
