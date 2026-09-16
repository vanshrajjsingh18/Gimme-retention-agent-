"""Holding the prediction engine to account.

A confidence score nobody checks is a guess with a percentage beside it. This
module records what the engine predicted, matches it against what the customer
actually did, and reports the gap — including the direction of the gap, which
is the part that says how to fix it. Consistently early means the reminder is
arriving after the decision was made; consistently late means the interval is
too short.

Resolution is deliberately separate from sending. A prediction is worth
recording whether or not a message follows it, and the suppressed ones matter
most: a customer who ordered on time with no reminder is evidence the
prediction was right and the message would have been wasted.
"""
from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.enums import OrderStatus, PredictionStatus
from app.models.base import utcnow
from app.models.entities import Order, OrderPredictionRecord

logger = logging.getLogger(__name__)

#: Ordering within this many minutes of the prediction counts as a hit. Three
#: hours sounds generous for a claim about a "7:39 PM habit", and is: the
#: figure people actually act on is the median error reported alongside it,
#: not this threshold. It is set where it is because a customer who orders at
#: 9pm instead of 7:40pm has kept their evening routine, and calling that a
#: miss would make the accuracy number describe punctuality rather than
#: whether the routine was real.
DEFAULT_HIT_WINDOW_MINUTES = 180

#: How long after the predicted moment to keep waiting before calling it a
#: miss. Shorter than the shortest sensible reorder interval, so a resolved
#: prediction cannot be overwritten by the *next* cycle's order.
DEFAULT_RESOLVE_AFTER_HOURS = 72


def record_prediction(
    db: Session,
    *,
    customer_id: int,
    predicted_order_at: datetime,
    confidence: int,
    automation_id: int | None = None,
    message_id: int | None = None,
    reminder_sent: bool = False,
    now: datetime | None = None,
) -> OrderPredictionRecord:
    """Store a prediction so it can be checked later.

    One open prediction per customer at a time. Re-predicting before the last
    one resolved updates it in place rather than stacking a second row, so a
    scheduler that runs every five minutes cannot manufacture a hundred
    predictions for the same window and flatter its own accuracy.
    """
    now = now or utcnow()
    existing = db.execute(
        select(OrderPredictionRecord).where(
            OrderPredictionRecord.customer_id == customer_id,
            OrderPredictionRecord.status == PredictionStatus.PENDING.value,
        )
    ).scalars().first()

    if existing is not None:
        existing.predicted_at = now
        existing.predicted_order_at = predicted_order_at
        existing.confidence = confidence
        if automation_id is not None:
            existing.automation_id = automation_id
        if message_id is not None:
            existing.message_id = message_id
        # Latches on: a reminder that went out stays recorded as sent even if
        # a later re-prediction is made without one.
        existing.reminder_sent = existing.reminder_sent or reminder_sent
        return existing

    record = OrderPredictionRecord(
        customer_id=customer_id,
        automation_id=automation_id,
        message_id=message_id,
        predicted_at=now,
        predicted_order_at=predicted_order_at,
        confidence=confidence,
        reminder_sent=reminder_sent,
        status=PredictionStatus.PENDING.value,
    )
    db.add(record)
    # Flushed for the same reason as in resolve_predictions: until it reaches
    # the database this row is invisible to the duplicate check above, so a
    # scheduler making several predictions in one transaction would create a
    # row per call rather than updating one.
    db.flush()
    return record


def _classify(error_minutes: float, *, hit_window: int) -> PredictionStatus:
    if abs(error_minutes) <= hit_window:
        return PredictionStatus.ORDERED_NEAR_PREDICTION
    return (
        PredictionStatus.ORDERED_LATE
        if error_minutes > 0
        else PredictionStatus.ORDERED_EARLY
    )


def resolve_predictions(
    db: Session,
    *,
    now: datetime | None = None,
    hit_window_minutes: int = DEFAULT_HIT_WINDOW_MINUTES,
    resolve_after_hours: int = DEFAULT_RESOLVE_AFTER_HOURS,
    commit: bool = True,
) -> dict:
    """Match pending predictions to real orders; retire the ones that missed.

    An order counts against a prediction when it falls after the prediction
    was *made* and within the resolution window either side of the predicted
    moment. Orders before ``predicted_at`` are excluded deliberately — an
    order the customer had already placed cannot be evidence that we foresaw
    it.
    """
    now = now or utcnow()
    pending = db.execute(
        select(OrderPredictionRecord).where(
            OrderPredictionRecord.status == PredictionStatus.PENDING.value
        )
    ).scalars().all()

    resolved = {"matched": 0, "missed": 0, "still_pending": 0}
    horizon = timedelta(hours=resolve_after_hours)

    for record in pending:
        order = db.execute(
            select(Order)
            .where(
                Order.customer_id == record.customer_id,
                Order.status == OrderStatus.COMPLETED.value,
                Order.ordered_at >= record.predicted_at,
                Order.ordered_at <= record.predicted_order_at + horizon,
            )
            .order_by(Order.ordered_at)
        ).scalars().first()

        if order is not None:
            delta = (order.ordered_at - record.predicted_order_at).total_seconds() / 60.0
            record.actual_order_at = order.ordered_at
            record.order_id = order.id
            record.error_minutes = round(delta, 1)
            record.status = _classify(delta, hit_window=hit_window_minutes).value
            record.resolved_at = now
            resolved["matched"] += 1
            continue

        if now > record.predicted_order_at + horizon:
            record.status = PredictionStatus.NO_ORDER.value
            record.resolved_at = now
            resolved["missed"] += 1
            continue

        resolved["still_pending"] += 1

    if commit:
        db.commit()
    else:
        # Sessions here are autoflush=False, in production as well as in tests.
        # Without this the rows keep their old status in the database while
        # the in-memory objects look updated, so a SELECT that filters on
        # status — every one of the reports below — silently returns nothing
        # and the dashboard shows zeros. Flushing keeps the caller's
        # transaction open while making the changes queryable.
        db.flush()
    return {**resolved, "checked_at": now.isoformat()}


@dataclass
class AccuracyReport:
    """Aggregate accuracy, phrased so a non-engineer can act on it."""

    total_resolved: int = 0
    hits: int = 0
    early: int = 0
    late: int = 0
    no_order: int = 0
    median_error_minutes: float | None = None
    mean_error_minutes: float | None = None
    within_30_minutes: int = 0
    within_1_hour: int = 0
    within_3_hours: int = 0
    within_24_hours: int = 0

    @property
    def accuracy_pct(self) -> float:
        """Share of resolved predictions where the customer ordered near it."""
        if not self.total_resolved:
            return 0.0
        return round(100.0 * self.hits / self.total_resolved, 1)

    @property
    def ordered_at_all_pct(self) -> float:
        """Share where they ordered at all, however far from the prediction.

        Reported next to accuracy because the two answer different questions:
        whether the customer was going to order, and whether we knew when.
        """
        if not self.total_resolved:
            return 0.0
        ordered = self.hits + self.early + self.late
        return round(100.0 * ordered / self.total_resolved, 1)

    def as_dict(self) -> dict:
        return {
            "total_resolved": self.total_resolved,
            "hits": self.hits,
            "early": self.early,
            "late": self.late,
            "no_order": self.no_order,
            "accuracy_pct": self.accuracy_pct,
            "ordered_at_all_pct": self.ordered_at_all_pct,
            "median_error_minutes": self.median_error_minutes,
            "mean_error_minutes": self.mean_error_minutes,
            "within_30_minutes": self.within_30_minutes,
            "within_1_hour": self.within_1_hour,
            "within_3_hours": self.within_3_hours,
            "within_24_hours": self.within_24_hours,
        }


def accuracy_report(
    db: Session, *, automation_id: int | None = None, since: datetime | None = None
) -> AccuracyReport:
    """Aggregate how the engine has been doing."""
    query = select(OrderPredictionRecord).where(
        OrderPredictionRecord.status != PredictionStatus.PENDING.value
    )
    if automation_id is not None:
        query = query.where(OrderPredictionRecord.automation_id == automation_id)
    if since is not None:
        query = query.where(OrderPredictionRecord.predicted_at >= since)

    rows = db.execute(query).scalars().all()
    report = AccuracyReport(total_resolved=len(rows))
    if not rows:
        return report

    errors: list[float] = []
    for row in rows:
        if row.status == PredictionStatus.ORDERED_NEAR_PREDICTION.value:
            report.hits += 1
        elif row.status == PredictionStatus.ORDERED_EARLY.value:
            report.early += 1
        elif row.status == PredictionStatus.ORDERED_LATE.value:
            report.late += 1
        elif row.status == PredictionStatus.NO_ORDER.value:
            report.no_order += 1

        if row.error_minutes is None:
            continue
        gap = abs(row.error_minutes)
        errors.append(gap)
        # Cumulative buckets: a 20-minute error is inside every band above it.
        if gap <= 30:
            report.within_30_minutes += 1
        if gap <= 60:
            report.within_1_hour += 1
        if gap <= 180:
            report.within_3_hours += 1
        if gap <= 1440:
            report.within_24_hours += 1

    if errors:
        report.median_error_minutes = round(statistics.median(errors), 1)
        report.mean_error_minutes = round(statistics.fmean(errors), 1)
    return report


def pending_count(db: Session, *, automation_id: int | None = None) -> int:
    query = select(func.count(OrderPredictionRecord.id)).where(
        OrderPredictionRecord.status == PredictionStatus.PENDING.value
    )
    if automation_id is not None:
        query = query.where(OrderPredictionRecord.automation_id == automation_id)
    return db.execute(query).scalar_one()
