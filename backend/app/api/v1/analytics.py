"""Analytics endpoints. Every figure is derived from the database."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.analytics import dashboards
from app.api.deps import get_current_user, require_write
from app.core.database import get_db
from app.models.entities import User

router = APIRouter()


@router.get("/analytics/overview", tags=["analytics"])
def overview(db: Session = Depends(get_db), _: User = Depends(get_current_user)) -> dict:
    return dashboards.overview(db)


@router.get("/analytics/customers", tags=["analytics"])
def customers(
    months: int = Query(default=12, ge=1, le=36),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict:
    return dashboards.customer_analytics(db, months=months)


@router.get("/analytics/churn", tags=["analytics"])
def churn(db: Session = Depends(get_db), _: User = Depends(get_current_user)) -> dict:
    return dashboards.churn_analytics(db)


@router.get("/analytics/campaigns", tags=["analytics"])
def campaigns(db: Session = Depends(get_db), _: User = Depends(get_current_user)) -> dict:
    return dashboards.campaign_analytics(db)


@router.get("/analytics/cohorts", tags=["analytics"])
def cohorts(
    months: int = Query(default=12, ge=1, le=36),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict:
    return dashboards.cohort_analytics(db, months=months)


@router.get("/analytics/activity", tags=["analytics"])
def activity(
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict:
    return {"activity": dashboards.recent_activity(db, limit=limit)}


@router.post("/analytics/recalculate", tags=["analytics"])
def recalculate(db: Session = Depends(get_db), _: User = Depends(require_write)) -> dict:
    """Recompute intelligence for every customer, then refresh segments."""
    from app.services.intelligence import refresh_all_customers
    from app.services.segments import refresh_all_segments

    result = refresh_all_customers(db)
    segments = refresh_all_segments(db)
    return {
        "customers_processed": result["customers_processed"],
        "rfm_scored": result["rfm_scored"],
        "segments": segments,
    }
