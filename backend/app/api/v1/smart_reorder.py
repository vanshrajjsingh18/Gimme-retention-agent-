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

from datetime import datetime, time, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_write
from app.analytics.order_predictions import describe_interval
from app.core.database import get_db
from app.core.enums import (
    OPEN_MESSAGE_STATUSES,
    AutomationKind,
    AutomationStatus,
    EnrollmentStatus,
)
from app.core.timezones import local_date, to_local, to_utc_naive
from app.models.base import utcnow
from app.models.entities import (
    Automation,
    AutomationEnrollment,
    Customer,
    CustomerMetrics,
    ScheduledMessage,
    User,
)
from app.schemas.models import EditMessageRequest, RescheduleMessageRequest
from app.services import smart_reorder_queue as queue
from app.services.merge_tags import unknown_tags
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
                "usual_day": pattern.get("preferred_weekday_name"),
                "usual_hour": pattern.get("preferred_hour"),
                "usual_time": pattern.get("preferred_time_label"),
                # Already on the 0-100 scale. It was stored 0-1 by the previous
                # model and multiplied here; reading the new value the same way
                # would report a confident customer as 9,000%.
                "confidence": int(pattern.get("overall_confidence") or 0),
                "interval_label": describe_interval(
                    (pattern.get("intervals") or {}).get("median_days")
                ),
                "predicted_order_at_local": (
                    pattern.get("predicted_next_order_at") or None
                ),
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
    population = _predicted_population(db, now=now)
    return {
        "generated_at": now.isoformat(),
        # Who could be reached, from the stored predictions — true whether or
        # not a campaign happens to be running. Reporting only the enrolled
        # made the whole dashboard read zero until somebody activated
        # something, which is the opposite of what it is for: deciding
        # whether activating anything is worthwhile.
        "eligible_customers": population["eligible"],
        "predicted_today": population["today"],
        "predicted_next_24h": population["next_24h"],
        "min_confidence": ELIGIBLE_CONFIDENCE,
        # Who a live campaign is actually watching.
        "enrolled_customers": len(enrolled),
        "enrolled_due_next_24h": len(due_24h),
        "active_campaigns": len(active_campaigns),
        "predictions_pending": pending_count(db),
        "accuracy": report.as_dict(),
    }


#: The confidence at or above which a routine is worth acting on. Matches the
#: "Smart Reorder Eligible" segment and the campaign builder's default, so the
#: dashboard headline and the audience a campaign would actually reach are the
#: same number.
ELIGIBLE_CONFIDENCE = 70


def _predicted_population(db: Session, *, now: datetime) -> dict:
    """Counts over the stored predictions, not over campaign enrollments.

    Answers "is there anything here worth sending?" before a campaign exists.
    Consent and suppression are part of the count because a customer who
    cannot be contacted is not an opportunity, however predictable they are.
    """
    contactable = (
        Customer.marketing_consent.is_(True),
        Customer.is_suppressed.is_(False),
    )
    base = (
        select(func.count())
        .select_from(CustomerMetrics)
        .join(Customer, Customer.id == CustomerMetrics.customer_id)
        .where(
            CustomerMetrics.prediction_confidence >= ELIGIBLE_CONFIDENCE,
            CustomerMetrics.predicted_next_order_at.is_not(None),
            *contactable,
        )
    )

    # "Today" is the customer's local day. Converting the day's bounds once
    # here keeps the comparison on an indexed UTC column instead of forcing a
    # per-row conversion the database cannot use an index for.
    day_start = to_utc_naive(datetime.combine(local_date(now), time.min))
    day_end = day_start + timedelta(days=1)

    return {
        "eligible": db.execute(base).scalar_one(),
        "today": db.execute(
            base.where(
                CustomerMetrics.predicted_next_order_at >= day_start,
                CustomerMetrics.predicted_next_order_at < day_end,
            )
        ).scalar_one(),
        "next_24h": db.execute(
            base.where(
                CustomerMetrics.predicted_next_order_at >= now,
                CustomerMetrics.predicted_next_order_at <= now + timedelta(hours=24),
            )
        ).scalar_one(),
    }


# --------------------------------------------------------------------------
# The individual message queue
# --------------------------------------------------------------------------
# Everything below is about one customer's one reminder. The endpoints above
# answer "how is Smart Reorder doing"; these answer "what is about to be sent
# to this person, and can I change it" — which is the question somebody has
# before they switch a campaign on for thousands of people.
@router.get("/smart-reorder/queue", tags=["smart-reorder"])
def message_queue(
    automation_id: int | None = None,
    status: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict:
    """The individual messages this engine is preparing to send.

    The operational screen. Each row is one customer, one predicted order
    time, one send time and the exact text they will receive.
    """
    statuses = (status,) if status else OPEN_MESSAGE_STATUSES
    messages = queue.upcoming(
        db, automation_id=automation_id, statuses=statuses, limit=limit
    )
    now = utcnow()
    rows = []
    for message in messages:
        view = queue.as_view(db, message)
        view["minutes_away"] = _minutes_until(message.scheduled_at, now=now)
        rows.append(view)
    return {
        "generated_at": now.isoformat(),
        "count": len(rows),
        "messages": rows,
    }


@router.get("/smart-reorder/queue/{message_id}", tags=["smart-reorder"])
def message_detail(
    message_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict:
    """One customer's reminder, with the reasoning that produced it."""
    message = db.get(ScheduledMessage, message_id)
    if message is None:
        raise HTTPException(status_code=404, detail="That scheduled message does not exist.")
    view = queue.as_view(db, message)
    view["context"] = message.context
    view["attempts"] = message.attempts
    return view


@router.post("/smart-reorder/queue/{message_id}/cancel", tags=["smart-reorder"])
def cancel_message(
    message_id: int,
    payload: dict | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(require_write),
) -> dict:
    try:
        message = queue.cancel_by_id(
            db, message_id, detail=(payload or {}).get("reason", "")
        )
    except queue.QueueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return queue.as_view(db, message)


@router.post("/smart-reorder/queue/{message_id}/reschedule", tags=["smart-reorder"])
def reschedule_message(
    message_id: int,
    payload: RescheduleMessageRequest,
    db: Session = Depends(get_db),
    _: User = Depends(require_write),
) -> dict:
    """Move one customer's reminder. Nobody else's slot changes."""
    try:
        message = queue.reschedule(db, message_id, to_utc_naive(payload.scheduled_at))
    except queue.QueueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return queue.as_view(db, message)


@router.post("/smart-reorder/queue/{message_id}/edit", tags=["smart-reorder"])
def edit_message(
    message_id: int,
    payload: EditMessageRequest,
    db: Session = Depends(get_db),
    _: User = Depends(require_write),
) -> dict:
    """Rewrite one customer's copy, leaving the campaign's template alone."""
    unknown = unknown_tags(payload.body)
    if unknown:
        raise HTTPException(
            status_code=400,
            detail="This message uses merge tags that cannot be filled: "
            + ", ".join(f"#{tag}#" for tag in unknown),
        )
    try:
        message = queue.edit(db, message_id, payload.body)
    except queue.QueueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return queue.as_view(db, message)


@router.post("/smart-reorder/{automation_id}/dry-run", tags=["smart-reorder"])
def dry_run(
    automation_id: int,
    limit: int | None = Query(default=None, ge=1, le=5000),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict:
    """Exactly what would be scheduled, with nothing written.

    The same code path the real build takes, with the writes turned off — so
    the counts are the rules executed rather than a second simulation of them.
    """
    automation = _nudge_or_404(db, automation_id)
    return queue.build_queue(db, automation, dry_run=True, limit=limit).as_dict()


@router.post("/smart-reorder/{automation_id}/build-queue", tags=["smart-reorder"])
def build_queue_now(
    automation_id: int,
    limit: int | None = Query(default=None, ge=1, le=5000),
    db: Session = Depends(get_db),
    _: User = Depends(require_write),
) -> dict:
    """Write the individual messages down.

    ``limit`` is the production safeguard: a first activation can materialise
    five customers rather than five thousand, so somebody can read what the
    engine produced before letting it loose on the whole audience.
    """
    automation = _nudge_or_404(db, automation_id)
    return queue.build_queue(db, automation, limit=limit).as_dict()


@router.get("/smart-reorder/{automation_id}/dashboard", tags=["smart-reorder"])
def campaign_dashboard(
    automation_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict:
    """What this campaign has sent, cancelled and earned."""
    return queue.dashboard(db, _nudge_or_404(db, automation_id))


def _nudge_or_404(db: Session, automation_id: int) -> Automation:
    automation = db.get(Automation, automation_id)
    if automation is None or automation.kind != AutomationKind.NUDGE.value:
        raise HTTPException(
            status_code=404, detail="That Smart Reorder campaign does not exist."
        )
    return automation
