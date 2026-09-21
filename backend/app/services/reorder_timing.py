"""When a Smart Reorder reminder is actually scheduled to go out.

There were two answers to that question and they disagreed by two hours.

Customer 360 read :mod:`app.analytics.order_predictions`, which learns the
time of day to the minute and offers "30 minutes before" — so it showed a
7:09 PM reminder for a customer who orders around 7:39 PM. The automation that
does the sending read :mod:`app.analytics.order_patterns` instead, which
resolves the time only to a three-hour bucket, and subtracted a hard-coded two
hours. The same customer was scheduled for 5:00 PM. Nothing on screen said the
two numbers came from different models, so the page was describing a system
that did not exist.

This module is the single answer. Both the scheduler and every screen call it,
which is the only arrangement in which they cannot drift: a change to the
offset, the clamp, or the prediction moves the send and the display together
because they are the same call.

It owns the part that is genuinely policy rather than arithmetic — pulling a
reminder into the hours a business may text in — and returns that as a visible
fact (`moved_for_send_window`) rather than silently applying it, because a
reminder aimed at 7:09 PM and sent at 6:00 PM is a different promise and the
operator should be able to see that it happened.

Local naive datetimes throughout, in the business timezone, except
`scheduled_utc` which is what the database stores.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.analytics.order_predictions import (
    DEFAULT_REMINDER_OFFSET,
    MIN_ORDERS_FOR_PREDICTION,
    REMINDER_OFFSETS,
    OrderPrediction,
    predict_next_order,
    reminder_time_for,
)
from app.core.config import settings
from app.core.timezones import to_local, to_utc_naive
from app.models.base import utcnow


def clamp_to_window(local_due: datetime) -> datetime:
    """Pull a reminder into business hours **on the customer's own day**.

    A large share of drinks orders land after 7pm, and the generic quiet-hours
    deferral would push those reminders to 9am the following morning — past
    the moment the message was timed to catch, which defeats the point of
    timing it to a habit at all. So a late-evening slot is moved *earlier the
    same day* instead, arriving while they are still deciding. An overnight
    slot is the mirror image: it belongs to the evening before, not to a 9am
    the customer is asleep for.
    """
    start, end = settings.send_window
    last_slot = (datetime.combine(local_due.date(), end) - timedelta(hours=1)).time()

    if local_due.time() >= end:
        return local_due.replace(
            hour=last_slot.hour, minute=last_slot.minute, second=0, microsecond=0
        )
    if local_due.time() < start:
        return (local_due - timedelta(days=1)).replace(
            hour=last_slot.hour, minute=last_slot.minute, second=0, microsecond=0
        )
    return local_due


@dataclass
class ReminderPlan:
    """One customer's next reminder: when, from what, and what moved it."""

    has_plan: bool = False
    reason: str = ""

    prediction: OrderPrediction | None = None

    #: What the customer is predicted to do, untouched by send policy.
    predicted_local: datetime | None = None
    #: The predicted time minus the configured offset.
    reminder_local: datetime | None = None
    #: The reminder after the send window has had its say. This is the one
    #: that is stored, scheduled and displayed.
    scheduled_local: datetime | None = None
    scheduled_utc: datetime | None = None

    #: True when the send window moved the reminder off its aimed time. Shown
    #: rather than swallowed: it is the difference between "we will text them
    #: half an hour before they usually order" and "we will text them at 6pm".
    moved_for_send_window: bool = False

    offset: str = DEFAULT_REMINDER_OFFSET
    offset_minutes: int = REMINDER_OFFSETS[DEFAULT_REMINDER_OFFSET]

    def as_dict(self) -> dict:
        return {
            "has_plan": self.has_plan,
            "reason": self.reason,
            "predicted_local": _iso(self.predicted_local),
            "reminder_local": _iso(self.reminder_local),
            "scheduled_local": _iso(self.scheduled_local),
            "scheduled_utc": _iso(self.scheduled_utc),
            "moved_for_send_window": self.moved_for_send_window,
            "offset": self.offset,
            "offset_minutes": self.offset_minutes,
        }


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment is not None else None


def offset_minutes_for(offset: str, custom_minutes: int | None = None) -> int:
    """Minutes before the predicted order to send. Negative sends after it."""
    if custom_minutes is not None:
        return int(custom_minutes)
    return REMINDER_OFFSETS.get(offset, REMINDER_OFFSETS[DEFAULT_REMINDER_OFFSET])


def plan_from_prediction(
    prediction: OrderPrediction,
    *,
    after_local: datetime,
    offset: str = DEFAULT_REMINDER_OFFSET,
    custom_minutes: int | None = None,
) -> ReminderPlan:
    """Turn a prediction into the reminder that will actually be scheduled.

    ``after_local`` is the moment to schedule from — normally now, or the last
    send. A slot already behind that moment is rolled forward a whole cycle
    rather than fired late: a reminder that arrives after the customer has
    ordered is the failure this feature exists to avoid.
    """
    minutes = offset_minutes_for(offset, custom_minutes)

    if not prediction.has_prediction or prediction.predicted_next_order_at is None:
        return ReminderPlan(
            has_plan=False,
            reason=prediction.reason or "No ordering routine to aim at.",
            prediction=prediction,
            offset=offset,
            offset_minutes=minutes,
        )

    predicted = prediction.predicted_next_order_at
    interval = (prediction.intervals.median_days if prediction.intervals else None) or 7.0

    # Roll whole cycles until the reminder is in the future. Stepping by the
    # customer's own interval keeps the slot on their routine; nudging it to
    # "now plus a bit" would invent a habit they do not have.
    guard = 0
    while True:
        aimed = reminder_time_for(
            _with_predicted(prediction, predicted), offset=offset, custom_minutes=custom_minutes
        )
        scheduled = clamp_to_window(aimed)
        if scheduled > after_local or guard >= 60:
            break
        predicted = predicted + timedelta(days=interval)
        guard += 1

    return ReminderPlan(
        has_plan=True,
        reason=prediction.reason,
        prediction=prediction,
        predicted_local=predicted,
        reminder_local=aimed,
        scheduled_local=scheduled,
        scheduled_utc=to_utc_naive(scheduled),
        moved_for_send_window=scheduled != aimed,
        offset=offset,
        offset_minutes=minutes,
    )


def _with_predicted(prediction: OrderPrediction, predicted: datetime) -> OrderPrediction:
    """A copy of the prediction aimed at a different cycle's order."""
    if predicted == prediction.predicted_next_order_at:
        return prediction
    from dataclasses import replace

    return replace(prediction, predicted_next_order_at=predicted)


def plan_for_customer(
    db: Session,
    customer_id: int,
    *,
    now: datetime | None = None,
    after: datetime | None = None,
    offset: str = DEFAULT_REMINDER_OFFSET,
    custom_minutes: int | None = None,
    min_orders: int = MIN_ORDERS_FOR_PREDICTION,
) -> ReminderPlan:
    """Learn one customer's routine and plan their next reminder from it.

    ``now`` and ``after`` are naive UTC as the database stores them; the
    conversion to the customer's clock happens here so no caller has to
    remember to do it. Reading a weekday or an hour straight off a UTC
    timestamp describes a habit nobody has — for New Zealand it is out by
    half a day, in the direction that turns a 7:40 PM habit into 7:40 AM.
    """
    from app.services.intelligence import load_local_order_facts

    now = now or utcnow()
    local_now = to_local(now).replace(tzinfo=None)
    local_after = to_local(after or now).replace(tzinfo=None)

    prediction = predict_next_order(
        load_local_order_facts(db, customer_id), now=local_now, min_orders=min_orders
    )
    return plan_from_prediction(
        prediction,
        after_local=local_after,
        offset=offset,
        custom_minutes=custom_minutes,
    )
