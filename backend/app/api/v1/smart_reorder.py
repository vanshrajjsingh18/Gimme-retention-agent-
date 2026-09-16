"""Smart Reorder: who is approaching their usual window, and how we are doing.

Two views. ``/upcoming`` is the operational one — the customers whose reminder
is about to fire, so somebody can look before it does. ``/accuracy`` is the
retrospective one, and is the reason the confidence scores elsewhere can be
believed at all.

Reads precomputed enrollments rather than recomputing a pattern per customer.
The scheduler already maintains ``next_due_at`` and a stored pattern for every
enrolled customer, so answering this question is an indexed range scan rather
than a walk over everybody's order history — which is what keeps the page
usable at a hundred thousand customers instead of a thousand.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.analytics.order_predictions import describe_interval
from app.core.database import get_db
from app.core.enums import AutomationKind, AutomationStatus, EnrollmentStatus
from app.core.timezones import to_local
from app.models.base import utcnow
from app.models.entities import (
    Automation,
    AutomationEnrollment,
    Customer,
    CustomerMetrics,
    User,
)
from app.services.prediction_accuracy import accuracy_report, pending_count

router = APIRouter()

#: Cap on rows returned, so a large audience cannot render a page that never
#: finishes. The counts above the table are computed separately and stay true.
MAX_ROWS = 200


def _minutes_until(moment: datetime, *, now: datetime) -> int:
    return int(round((moment - now).total_seconds() / 60.0))


@router.get("/smart-reorder/upcoming", tags=["smart-reorder"])
def upcoming(
    hours: int = Query(24, ge=1, le=168),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict:
    """Customers entering their predicted reorder window within ``hours``.

    Backs the CUSTOMERS LIKELY TO ORDER NOW view. Times come back in both UTC
    and the customer's local clock: the local one is what an operator is
    reasoning about, and asking the browser to convert would put a second
    timezone implementation in the product.
    """
    now = utcnow()
    horizon = now + timedelta(hours=hours)

    rows = db.execute(
        select(AutomationEnrollment, Customer, Automation)
        .join(Customer, Customer.id == AutomationEnrollment.customer_id)
        .join(Automation, Automation.id == AutomationEnrollment.automation_id)
        .where(
            Automation.kind == AutomationKind.NUDGE.value,
            AutomationEnrollment.status == EnrollmentStatus.ACTIVE.value,
            AutomationEnrollment.next_due_at.is_not(None),
            AutomationEnrollment.next_due_at <= horizon,
        )
        .order_by(AutomationEnrollment.next_due_at)
        .limit(MAX_ROWS)
    ).all()

    metrics = {
        m.customer_id: m
        for m in db.execute(
            select(CustomerMetrics).where(
                CustomerMetrics.customer_id.in_([c.id for _, c, _ in rows] or [0])
            )
        ).scalars().all()
    }

    customers = []
    for enrollment, customer, automation in rows:
        pattern = enrollment.pattern or {}
        due = enrollment.next_due_at
        row_metrics = metrics.get(customer.id)
        customers.append(
            {
                "customer_id": customer.id,
                "name": f"{customer.first_name} {customer.last_name}".strip(),
                "automation_id": automation.id,
                "automation_name": automation.name,
                "automation_status": automation.status,
                "predicted_at": due.isoformat(),
                "predicted_at_local": to_local(due).isoformat(),
                "minutes_away": _minutes_until(due, now=now),
                # Already overdue is a real state, not a bug: the scheduler
                # runs on an interval and a window can open between ticks.
                "is_due_now": due <= now,
                "usual_day": pattern.get("weekday_name"),
                "usual_hour": pattern.get("typical_hour"),
                "confidence": int(round((pattern.get("confidence") or 0) * 100)),
                "interval_label": describe_interval(pattern.get("median_interval_days")),
                "last_order_at": (
                    row_metrics.last_order_at.isoformat()
                    if row_metrics and row_metrics.last_order_at
                    else None
                ),
                "channel": automation.channel,
                "last_sent_at": (
                    enrollment.last_sent_at.isoformat()
                    if enrollment.last_sent_at
                    else None
                ),
            }
        )

    due_now = sum(1 for c in customers if c["is_due_now"])
    return {
        "generated_at": now.isoformat(),
        "horizon_hours": hours,
        "due_now": due_now,
        "upcoming": len(customers) - due_now,
        "total": len(customers),
        "truncated": len(customers) == MAX_ROWS,
        "customers": customers,
    }


@router.get("/smart-reorder/accuracy", tags=["smart-reorder"])
def accuracy(
    automation_id: int | None = Query(None),
    days: int = Query(90, ge=1, le=730),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict:
    """How the predictions have actually turned out.

    ``total_resolved`` of zero is the honest answer for a young campaign, and
    the UI is expected to say "not enough data yet" rather than render 0% as
    though the engine had been tested and failed.
    """
    since = utcnow() - timedelta(days=days)
    report = accuracy_report(db, automation_id=automation_id, since=since)
    return {
        **report.as_dict(),
        "window_days": days,
        "automation_id": automation_id,
        "pending": pending_count(db, automation_id=automation_id),
    }


@router.get("/smart-reorder/overview", tags=["smart-reorder"])
def overview(
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict:
    """Headline counts for the Smart Reorder dashboard."""
    now = utcnow()
    today_end = now + timedelta(hours=24)

    enrolled = db.execute(
        select(AutomationEnrollment.id)
        .join(Automation, Automation.id == AutomationEnrollment.automation_id)
        .where(
            Automation.kind == AutomationKind.NUDGE.value,
            AutomationEnrollment.status == EnrollmentStatus.ACTIVE.value,
        )
    ).all()

    due_24h = db.execute(
        select(AutomationEnrollment.id)
        .join(Automation, Automation.id == AutomationEnrollment.automation_id)
        .where(
            Automation.kind == AutomationKind.NUDGE.value,
            AutomationEnrollment.status == EnrollmentStatus.ACTIVE.value,
            AutomationEnrollment.next_due_at.is_not(None),
            AutomationEnrollment.next_due_at <= today_end,
        )
    ).all()

    active_campaigns = db.execute(
        select(Automation.id).where(
            Automation.kind == AutomationKind.NUDGE.value,
            Automation.status == AutomationStatus.ACTIVE.value,
        )
    ).all()

    report = accuracy_report(db)
    return {
        "generated_at": now.isoformat(),
        "eligible_customers": len(enrolled),
        "predicted_next_24h": len(due_24h),
        "active_campaigns": len(active_campaigns),
        "predictions_pending": pending_count(db),
        "accuracy": report.as_dict(),
    }
