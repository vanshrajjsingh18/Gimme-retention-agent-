"""Individual reorder reminders: written down, inspected, then sent one by one.

The engine that decides *when* each customer will next order already exists in
:mod:`app.services.reorder_timing`, and the gates that decide whether a message
may go out already exist in :mod:`app.automations.runtime`. What is here is the
thing between them: a row per customer per reminder, created ahead of time with
its text already rendered.

That intermediate record is the feature. Without it a Smart Reorder campaign is
a promise — one rule that will, at some point, produce thousands of different
messages at thousands of different minutes, and nobody can see any of them
until they have been sent. With it, an operator can read the exact string that
will reach a named customer at a named minute, change it, move it, or call it
off. "What is this about to send?" stops being a question about code.

Three rules hold the design together:

* **The queue never decides eligibility.** Dispatch hands its messages to
  ``execute_candidates``, which runs consent, suppression, age, frequency
  caps, quiet hours, dedup and content compliance exactly as it does for every
  other automation. A second implementation of those checks here would be a
  second thing to keep correct, and the one that drifted would be the one that
  texted somebody who had opted out.
* **A message is claimed before it is sent.** The move from SCHEDULED to
  PROCESSING is a conditional update, so two schedulers racing over the same
  row produce one send and one no-op rather than two messages.
* **Nothing is sent to somebody who has already ordered.** Checked again at
  dispatch, against orders placed since the message was written — which is the
  entire window in which the thing this reminder is for might have happened
  without us.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.automations import nudge
from app.automations.runtime import Candidate, execute_candidates
from app.core.enums import (
    AutomationKind,
    AutomationStatus,
    CancellationReason,
    Channel,
    OPEN_MESSAGE_STATUSES,
    ScheduledMessageStatus,
    SendStatus,
    SkipReason,
)
from app.core.timezones import to_local, to_utc_naive
from app.models.base import utcnow
from app.models.entities import (
    Automation,
    AutomationEnrollment,
    Customer,
    Order,
    ScheduledMessage,
)

logger = logging.getLogger(__name__)

#: How far ahead reminders are written down. Far enough that the queue is worth
#: looking at, short enough that it is not full of predictions that will be
#: recomputed before their moment arrives — a routine is refreshed as customers
#: order, and a message scheduled a month out is mostly a guess about a guess.
DEFAULT_HORIZON_DAYS = 14

#: A message whose moment passed by more than this is not sent late. "It's
#: about your usual Wednesday evening" delivered on Friday morning is worse
#: than silence: it says plainly that nothing was watching.
STALE_AFTER_MINUTES = 90

#: Retries per message. A provider refusing everything should not be retried
#: into the ground, and a reminder is not worth sending on the third attempt
#: anyway — its moment has gone.
MAX_ATTEMPTS = 3

#: Campaign states that may produce or dispatch messages. TESTING is live but
#: only for the named test recipients, which is enforced when queueing.
SENDING_STATUSES = (AutomationStatus.ACTIVE.value, AutomationStatus.TESTING.value)


@dataclass
class QueueReport:
    """What one pass of the queue builder did, and who it left out.

    The exclusion counts are not decoration. "426 eligible" is only meaningful
    beside the reasons the other 574 were not, and those reasons are the
    operator's to act on — insufficient history fixes itself, no consent does
    not.
    """

    automation_id: int = 0
    automation_name: str = ""
    dry_run: bool = False
    analysed: int = 0
    scheduled: int = 0
    refreshed: int = 0
    unchanged: int = 0
    cancelled: int = 0
    excluded: dict[str, int] = field(default_factory=dict)
    messages: list[dict] = field(default_factory=list)

    def exclude(self, reason: str) -> None:
        self.excluded[reason] = self.excluded.get(reason, 0) + 1

    def as_dict(self) -> dict:
        return {
            "automation_id": self.automation_id,
            "automation_name": self.automation_name,
            "dry_run": self.dry_run,
            "customers_analysed": self.analysed,
            "messages_scheduled": self.scheduled,
            "messages_refreshed": self.refreshed,
            "messages_unchanged": self.unchanged,
            "messages_cancelled": self.cancelled,
            "excluded_by_reason": self.excluded,
            "eligible": self.scheduled + self.refreshed + self.unchanged,
            "messages": self.messages,
        }


# --------------------------------------------------------------------------
# Reading the queue
# --------------------------------------------------------------------------
def open_message_for(
    db: Session, *, automation_id: int, customer_id: int
) -> ScheduledMessage | None:
    """This customer's pending reminder on this campaign, if they have one.

    At most one: a customer with two scheduled reminders would receive two,
    and the second would be about an order they had already been reminded to
    place. Enforced by everything that creates rows going through here first.
    """
    return (
        db.execute(
            select(ScheduledMessage)
            .where(
                ScheduledMessage.automation_id == automation_id,
                ScheduledMessage.customer_id == customer_id,
                ScheduledMessage.status.in_(OPEN_MESSAGE_STATUSES),
            )
            .order_by(ScheduledMessage.scheduled_at)
        )
        .scalars()
        .first()
    )


def upcoming(
    db: Session,
    *,
    automation_id: int | None = None,
    statuses: tuple[str, ...] = OPEN_MESSAGE_STATUSES,
    limit: int = 200,
    now: datetime | None = None,
) -> list[ScheduledMessage]:
    """The queue, soonest first — the screen an operator watches before going live."""
    query = select(ScheduledMessage).where(ScheduledMessage.status.in_(statuses))
    if automation_id is not None:
        query = query.where(ScheduledMessage.automation_id == automation_id)
    return list(
        db.execute(query.order_by(ScheduledMessage.scheduled_at).limit(limit)).scalars().all()
    )


def as_view(db: Session, message: ScheduledMessage) -> dict:
    """One queued message, as the operational screen shows it.

    Times are converted here rather than in the browser: somebody opening this
    from another country must see the customer's evening, not their own.
    """
    customer = message.customer or db.get(Customer, message.customer_id)
    return {
        "id": message.id,
        "customer_id": message.customer_id,
        "customer_name": (customer.full_name if customer else "") or f"Customer {message.customer_id}",
        "to": _masked_contact(customer, Channel(message.channel)) if customer else None,
        "automation_id": message.automation_id,
        "campaign_id": message.campaign_id,
        "channel": message.channel,
        "status": message.status,
        "scheduled_at": message.scheduled_at.isoformat(),
        "scheduled_at_local": to_local(message.scheduled_at).isoformat(),
        "predicted_order_at": (
            message.predicted_order_at.isoformat() if message.predicted_order_at else None
        ),
        "predicted_order_at_local": (
            to_local(message.predicted_order_at).isoformat()
            if message.predicted_order_at
            else None
        ),
        "offset_minutes": message.offset_minutes,
        "moved_for_send_window": message.moved_for_send_window,
        "confidence": message.confidence,
        "template": message.template,
        "message": message.rendered_message,
        "edited": message.edited_by_operator,
        "cancellation_reason": message.cancellation_reason,
        "cancellation_detail": message.cancellation_detail,
        "sent_at": message.sent_at.isoformat() if message.sent_at else None,
        "error_message": message.error_message,
        "product": message.context.get("product"),
        "usual_day": message.context.get("usual_day"),
        "usual_time": message.context.get("usual_time"),
        "is_test": bool(message.context.get("test_mode")),
    }


def _masked_contact(customer: Customer, channel: Channel) -> str | None:
    """Enough of the address to recognise, not enough to leak in a screenshot."""
    value = customer.email if channel == Channel.EMAIL else customer.phone
    if not value:
        return None
    if "@" in value:
        name, _, domain = value.partition("@")
        head = name[:2] if len(name) > 2 else name[:1]
        return f"{head}***@{domain}"
    return f"{value[:5]}***{value[-2:]}" if len(value) > 8 else value


# --------------------------------------------------------------------------
# Writing the queue
# --------------------------------------------------------------------------
def build_queue(
    db: Session,
    automation: Automation,
    *,
    now: datetime | None = None,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    limit: int | None = None,
    dry_run: bool = False,
    commit: bool = True,
) -> QueueReport:
    """Write down every eligible customer's next reminder.

    Runs over the automation's enrollments, each of which already carries this
    customer's own predicted slot. For each, the message is rendered now and
    stored, so that what the queue shows is the string the provider will get.

    A dry run takes the identical path and simply does not persist. That is
    what makes the preview worth trusting — it is not a second simulation of
    the rules, it is the rules with the writes turned off.

    ``limit`` is the production safeguard: a first activation can materialise
    five customers rather than five thousand, and the operator can read them.
    """
    now = now or utcnow()
    cfg = nudge.config_of(automation)
    report = QueueReport(
        automation_id=automation.id,
        automation_name=automation.name,
        dry_run=dry_run,
    )
    horizon = now + timedelta(days=horizon_days)
    test_only = automation.status == AutomationStatus.TESTING.value
    test_ids = set(_test_customer_ids(automation))

    # The audience, not just the people already enrolled. A dry run is run
    # *before* a campaign is switched on, which is precisely when nobody is
    # enrolled — walking enrollments meant the preview reported nothing at the
    # only moment somebody needed it to report something.
    from app.automations.cohort import resolve_audience

    if not dry_run:
        # Newly eligible customers join before the queue is written, so
        # somebody who reached their third order today is not left out until
        # the next enrollment pass.
        nudge.enroll(db, automation, now=now, commit=False)

    audience = list(resolve_audience(db, automation, now=now))
    enrollments = {
        e.customer_id: e
        for e in db.execute(
            select(AutomationEnrollment).where(
                AutomationEnrollment.automation_id == automation.id
            )
        )
        .scalars()
        .all()
    }
    customers = {
        c.id: c
        for c in db.execute(select(Customer).where(Customer.id.in_(audience))).scalars().all()
    }

    written = 0
    for customer_id in audience:
        report.analysed += 1
        enrollment = enrollments.get(customer_id)
        customer = customers.get(customer_id)
        if customer is None:
            report.exclude("CUSTOMER_MISSING")
            continue

        # In TESTING the campaign is live, but only for the people somebody
        # named. Everyone else is counted rather than queued, so the operator
        # can see the size of what they have not switched on yet.
        if test_only and customer.id not in test_ids:
            report.exclude("NOT_A_TEST_RECIPIENT")
            continue

        plan = nudge.plan_for(db, customer.id, cfg, now=now)
        if not plan.has_plan:
            report.exclude(plan.reason or "NO_PATTERN")
            continue
        if plan.prediction.overall_confidence < cfg["min_confidence"]:
            report.exclude("LOW_CONFIDENCE")
            continue
        if plan.scheduled_utc is None or plan.scheduled_utc > horizon:
            report.exclude("BEYOND_HORIZON")
            continue

        # Somebody who has already ordered in this cycle does not need
        # reminding — and writing the message down now, to cancel it later,
        # would put a reminder in the queue that was never going to be sent.
        if _ordered_since(db, customer.id, _cycle_start(plan, now=now)):
            report.exclude(SkipReason.ALREADY_ORDERED.value)
            continue

        existing = open_message_for(db, automation_id=automation.id, customer_id=customer.id)
        body, context = _render(db, automation, customer, plan, now=now)
        context["test_mode"] = test_only

        if dry_run:
            report.scheduled += 1
            report.messages.append(
                _preview(customer, plan, automation, body, context)
            )
            written += 1
            if limit is not None and written >= limit:
                break
            continue

        if existing is None:
            message = ScheduledMessage(
                customer_id=customer.id,
                automation_id=automation.id,
                campaign_id=automation.campaign_id,
                enrollment_id=enrollment.id if enrollment else None,
                status=ScheduledMessageStatus.SCHEDULED.value,
            )
            db.add(message)
            report.scheduled += 1
        elif _matches(existing, plan, body):
            report.unchanged += 1
            written += 1
            if limit is not None and written >= limit:
                break
            continue
        else:
            message = existing
            report.refreshed += 1

        _fill(message, plan=plan, automation=automation, body=body, context=context)
        written += 1
        if limit is not None and written >= limit:
            break

    if commit and not dry_run:
        db.commit()
    return report


def _fill(
    message: ScheduledMessage,
    *,
    plan,
    automation: Automation,
    body: str,
    context: dict,
) -> None:
    """Write the plan onto a row, leaving an operator's own words alone.

    A refresh that overwrote hand-edited copy would make the edit feature a
    lie — somebody would change a message, the pattern job would run, and
    their wording would vanish with nothing said.
    """
    message.scheduled_at = plan.scheduled_utc
    # The prediction is carried in the customer's local time — everything that
    # reasons about "when do they order" works on their clock. This column
    # holds naive UTC like every other timestamp, so it is converted here, at
    # the boundary, rather than stored on one clock beside a send time on
    # another.
    message.predicted_order_at = _predicted_utc(plan)
    message.offset_minutes = plan.offset_minutes
    message.moved_for_send_window = plan.moved_for_send_window
    message.confidence = plan.prediction.overall_confidence
    message.cycle_start_at = _cycle_start(plan, now=message.scheduled_at)
    message.channel = automation.channel
    message.template = automation.message_template or nudge.DEFAULT_NUDGE_TEMPLATE
    message.campaign_id = automation.campaign_id
    message.context = context
    if not message.edited_by_operator:
        message.rendered_message = body


def _predicted_utc(plan) -> datetime | None:
    """The predicted order moment, in the naive UTC the database stores."""
    return to_utc_naive(plan.predicted_local) if plan.predicted_local is not None else None


def _matches(message: ScheduledMessage, plan, body: str) -> bool:
    """Whether a stored row already says what this plan would say."""
    return (
        message.scheduled_at == plan.scheduled_utc
        and message.confidence == plan.prediction.overall_confidence
        and (message.edited_by_operator or message.rendered_message == body)
    )


def _render(
    db: Session, automation: Automation, customer: Customer, plan, *, now: datetime
) -> tuple[str, dict]:
    """This customer's message, and the reasoning the queue displays beside it."""
    body, offer = nudge.render_nudge(db, automation, customer, plan.prediction, now=now)
    metrics = customer.metrics
    return body, {
        "source": "smart_reorder",
        "usual_day": plan.prediction.preferred_weekday_name,
        "usual_time": plan.prediction.preferred_time_label,
        "interval_days": (
            plan.prediction.intervals.median_days if plan.prediction.intervals else None
        ),
        "product": (metrics.last_order_product if metrics else "") or None,
        "confidence": plan.prediction.overall_confidence,
        "aimed_at": plan.reminder_local.isoformat() if plan.reminder_local else None,
        "offer": offer.as_dict(),
    }


def _preview(customer: Customer, plan, automation: Automation, body: str, context: dict) -> dict:
    """A dry run's row: the same numbers, with no database write behind them."""
    return {
        "customer_id": customer.id,
        "customer_name": customer.full_name or f"Customer {customer.id}",
        "channel": automation.channel,
        "scheduled_at": plan.scheduled_utc.isoformat(),
        "scheduled_at_local": to_local(plan.scheduled_utc).isoformat(),
        "predicted_order_at": (
            _predicted_utc(plan).isoformat() if plan.predicted_local else None
        ),
        "predicted_order_at_local": (
            plan.predicted_local.isoformat() if plan.predicted_local else None
        ),
        "confidence": plan.prediction.overall_confidence,
        "message": body,
        "usual_day": context.get("usual_day"),
        "usual_time": context.get("usual_time"),
        "product": context.get("product"),
        "status": "WOULD_SCHEDULE",
    }


def _test_customer_ids(automation: Automation) -> list[int]:
    raw = (automation.config or {}).get("test_customer_ids") or []
    return [int(v) for v in raw if str(v).strip().isdigit() or isinstance(v, int)]


def _cycle_start(plan, *, now: datetime) -> datetime:
    """The moment after which an order means "they have already done it".

    The order the prediction was built from, not an arbitrary window: anything
    placed since then is the purchase this reminder exists to prompt.
    """
    # Local, like everything else on the prediction, and compared against a
    # UTC column — so converted here rather than a day out.
    last = plan.prediction.last_order_at
    if last is None:
        return now - timedelta(days=1)
    return to_utc_naive(last) + timedelta(seconds=1)


def _ordered_since(db: Session, customer_id: int, since: datetime) -> bool:
    return (
        db.execute(
            select(Order.id)
            .where(
                Order.customer_id == customer_id,
                Order.ordered_at > since,
                Order.status.in_(nudge.ORDERED_STATUSES),
            )
            .limit(1)
        ).first()
        is not None
    )


# --------------------------------------------------------------------------
# The dashboard
# --------------------------------------------------------------------------
def dashboard(db: Session, automation: Automation, *, now: datetime | None = None) -> dict:
    """What this campaign has done and is about to do.

    Counted off the message rows rather than recomputed from order history,
    because these are claims about what the engine actually did. "47 already
    ordered" means forty-seven reminders were called off, and it is only
    trustworthy if it comes from the forty-seven rows.
    """
    now = now or utcnow()
    counts = dict(
        db.execute(
            select(ScheduledMessage.status, func.count())
            .where(ScheduledMessage.automation_id == automation.id)
            .group_by(ScheduledMessage.status)
        ).all()
    )
    reasons = dict(
        db.execute(
            select(ScheduledMessage.cancellation_reason, func.count())
            .where(
                ScheduledMessage.automation_id == automation.id,
                ScheduledMessage.cancellation_reason.isnot(None),
            )
            .group_by(ScheduledMessage.cancellation_reason)
        ).all()
    )

    converted = counts.get(ScheduledMessageStatus.CONVERTED.value, 0)
    sent = (
        counts.get(ScheduledMessageStatus.SENT.value, 0)
        + counts.get(ScheduledMessageStatus.DELIVERED.value, 0)
        + converted
    )
    revenue = (
        db.execute(
            select(func.coalesce(func.sum(Order.total_amount), 0.0))
            .select_from(ScheduledMessage)
            .join(Order, Order.id == ScheduledMessage.converted_order_id)
            .where(ScheduledMessage.automation_id == automation.id)
        ).scalar()
        or 0.0
    )
    due_next_24h = (
        db.execute(
            select(func.count())
            .select_from(ScheduledMessage)
            .where(
                ScheduledMessage.automation_id == automation.id,
                ScheduledMessage.status == ScheduledMessageStatus.SCHEDULED.value,
                ScheduledMessage.scheduled_at <= now + timedelta(hours=24),
            )
        ).scalar()
        or 0
    )

    return {
        "automation_id": automation.id,
        "automation_name": automation.name,
        "status": automation.status,
        "messages_scheduled": counts.get(ScheduledMessageStatus.SCHEDULED.value, 0),
        "messages_processing": counts.get(ScheduledMessageStatus.PROCESSING.value, 0),
        "messages_sent": sent,
        "messages_delivered": counts.get(ScheduledMessageStatus.DELIVERED.value, 0),
        "messages_failed": counts.get(ScheduledMessageStatus.FAILED.value, 0),
        "messages_cancelled": counts.get(ScheduledMessageStatus.CANCELLED.value, 0),
        "messages_suppressed": counts.get(ScheduledMessageStatus.SUPPRESSED.value, 0),
        "messages_expired": counts.get(ScheduledMessageStatus.EXPIRED.value, 0),
        "due_next_24h": due_next_24h,
        # The number that says the feature is working: a reminder called off
        # because the customer got there first is a success, not a failure.
        "already_ordered": reasons.get(
            CancellationReason.CUSTOMER_ALREADY_ORDERED.value, 0
        ),
        "cancelled_by_reason": reasons,
        "conversions": converted,
        "revenue": round(float(revenue), 2),
        "conversion_rate": round(converted / sent * 100, 1) if sent else 0.0,
        "average_order_value": round(float(revenue) / converted, 2) if converted else 0.0,
    }


# --------------------------------------------------------------------------
# Operator actions
# --------------------------------------------------------------------------
class QueueError(Exception):
    """A message cannot be acted on — usually because it has already gone."""


def _open_or_raise(db: Session, message_id: int) -> ScheduledMessage:
    message = db.get(ScheduledMessage, message_id)
    if message is None:
        raise QueueError("That scheduled message no longer exists.")
    if message.status not in OPEN_MESSAGE_STATUSES:
        raise QueueError(
            f"This message is {message.status.lower()} and can no longer be changed."
        )
    return message


def cancel(
    db: Session,
    message: ScheduledMessage,
    reason: CancellationReason,
    *,
    detail: str = "",
    now: datetime | None = None,
    commit: bool = True,
) -> ScheduledMessage:
    """Call a reminder off, recording which of the several reasons it was.

    Counted separately on the dashboard because they mean opposite things: a
    message cancelled because the customer ordered first is the feature
    working, and one cancelled because they opted out is not.
    """
    message.status = ScheduledMessageStatus.CANCELLED.value
    message.cancellation_reason = reason.value
    message.cancellation_detail = detail or None
    message.cancelled_at = now or utcnow()
    if commit:
        db.commit()
    return message


def cancel_by_id(db: Session, message_id: int, *, detail: str = "") -> ScheduledMessage:
    return cancel(
        db,
        _open_or_raise(db, message_id),
        CancellationReason.OPERATOR_CANCELLED,
        detail=detail,
    )


def reschedule(db: Session, message_id: int, when: datetime) -> ScheduledMessage:
    """Move one customer's reminder, without touching anybody else's."""
    message = _open_or_raise(db, message_id)
    message.scheduled_at = when
    message.context = {**(message.context or {}), "rescheduled_by_operator": True}
    db.commit()
    return message


def edit(db: Session, message_id: int, body: str) -> ScheduledMessage:
    """Rewrite one customer's copy. The campaign's template is untouched."""
    message = _open_or_raise(db, message_id)
    message.rendered_message = body
    message.edited_by_operator = True
    db.commit()
    return message


def cancel_open_for_campaign(
    db: Session,
    automation: Automation,
    reason: CancellationReason,
    *,
    now: datetime | None = None,
) -> int:
    """Call off everything still pending — used when a campaign is archived."""
    now = now or utcnow()
    result = db.execute(
        update(ScheduledMessage)
        .where(
            ScheduledMessage.automation_id == automation.id,
            ScheduledMessage.status.in_(OPEN_MESSAGE_STATUSES),
        )
        .values(
            status=ScheduledMessageStatus.CANCELLED.value,
            cancellation_reason=reason.value,
            cancelled_at=now,
        )
    )
    db.commit()
    return result.rowcount or 0


# --------------------------------------------------------------------------
# Conversion
# --------------------------------------------------------------------------
#: How long after a reminder an order still counts as answering it. Overridden
#: per campaign by the backing campaign's own window.
DEFAULT_ATTRIBUTION_HOURS = 72


def credit_conversion(
    db: Session, order: Order, *, now: datetime | None = None, commit: bool = False
) -> dict | None:
    """Mark the reminder this order answered, if one did.

    Campaign-level attribution already happens in :mod:`app.services.attribution`
    and is not repeated here. What this adds is the individual record: which
    message, how long the customer took, and how far out the prediction was.
    That last number is the only way to find out whether the timing is any
    good — a conversion says the message worked, and the error says whether
    the hour it was sent at had anything to do with it.
    """
    now = now or utcnow()
    if order.status not in nudge.ORDERED_STATUSES:
        return None

    candidates = (
        db.execute(
            select(ScheduledMessage)
            .where(
                ScheduledMessage.customer_id == order.customer_id,
                ScheduledMessage.status.in_(
                    (
                        ScheduledMessageStatus.SENT.value,
                        ScheduledMessageStatus.DELIVERED.value,
                    )
                ),
                ScheduledMessage.sent_at.isnot(None),
                ScheduledMessage.sent_at <= order.ordered_at,
            )
            .order_by(ScheduledMessage.sent_at.desc())
            .limit(5)
        )
        .scalars()
        .all()
    )

    for message in candidates:
        window = _attribution_hours(db, message)
        elapsed = (order.ordered_at - message.sent_at).total_seconds() / 3600.0
        if elapsed > window:
            continue
        message.status = ScheduledMessageStatus.CONVERTED.value
        message.converted_order_id = order.id
        message.converted_at = order.ordered_at
        message.context = {
            **(message.context or {}),
            "conversion": {
                "order_id": order.id,
                "order_external_id": order.external_id,
                "order_amount": order.total_amount,
                "sent_at": message.sent_at.isoformat(),
                "ordered_at": order.ordered_at.isoformat(),
                "hours_to_conversion": round(elapsed, 2),
                "attribution_window_hours": window,
                # Signed on purpose: consistently early means the reminder is
                # arriving after the decision is already made, and
                # consistently late means the interval is too short. They
                # call for opposite fixes, so an absolute value hides which.
                "prediction_error_minutes": (
                    round(
                        (order.ordered_at - message.predicted_order_at).total_seconds() / 60.0
                    )
                    if message.predicted_order_at
                    else None
                ),
            },
        }
        if commit:
            db.commit()
        return {
            "scheduled_message_id": message.id,
            "hours_to_conversion": round(elapsed, 2),
        }
    return None


def _attribution_hours(db: Session, message: ScheduledMessage) -> float:
    from app.models.entities import Campaign

    if message.campaign_id:
        campaign = db.get(Campaign, message.campaign_id)
        if campaign is not None and campaign.attribution_window_hours:
            return float(campaign.attribution_window_hours)
    return float(DEFAULT_ATTRIBUTION_HOURS)


# --------------------------------------------------------------------------
# Reacting to a new order
# --------------------------------------------------------------------------
def refresh_for_customer(
    db: Session, customer_id: int, *, now: datetime | None = None, commit: bool = True
) -> dict:
    """Recompute this customer's reminder after they have ordered.

    The order that just landed is both the thing a pending reminder was for
    and the newest evidence about when they will order next. So the pending
    message is cancelled — sending it would be asking somebody to buy what
    they have just bought — and a fresh one is written from the new history.

    Called on every order rather than left to the pattern refresh, because the
    gap between the two is exactly the window in which the wrong message goes
    out.
    """
    now = now or utcnow()
    cancelled = 0
    rescheduled = 0

    campaigns = (
        db.execute(
            select(Automation).where(
                Automation.kind == AutomationKind.NUDGE.value,
                Automation.status.in_(SENDING_STATUSES),
            )
        )
        .scalars()
        .all()
    )
    for automation in campaigns:
        existing = open_message_for(
            db, automation_id=automation.id, customer_id=customer_id
        )
        if existing is not None:
            cancel(
                db,
                existing,
                CancellationReason.PREDICTION_SUPERSEDED,
                detail="The customer ordered, so this reminder was aimed at a moment that has passed.",
                now=now,
                commit=False,
            )
            cancelled += 1

        enrollment = (
            db.execute(
                select(AutomationEnrollment).where(
                    AutomationEnrollment.automation_id == automation.id,
                    AutomationEnrollment.customer_id == customer_id,
                )
            )
            .scalars()
            .first()
        )
        if enrollment is None:
            continue

        cfg = nudge.config_of(automation)
        plan = nudge.plan_for(db, customer_id, cfg, now=now)
        if not plan.has_plan or plan.prediction.overall_confidence < cfg["min_confidence"]:
            continue
        enrollment.next_due_at = plan.scheduled_utc
        enrollment.pattern = nudge._store_routine(plan, now=now)

        customer = db.get(Customer, customer_id)
        if customer is None:
            continue
        body, context = _render(db, automation, customer, plan, now=now)
        message = ScheduledMessage(
            customer_id=customer_id,
            automation_id=automation.id,
            campaign_id=automation.campaign_id,
            enrollment_id=enrollment.id,
            status=ScheduledMessageStatus.SCHEDULED.value,
        )
        db.add(message)
        _fill(message, plan=plan, automation=automation, body=body, context=context)
        rescheduled += 1

    if commit:
        db.commit()
    return {"cancelled": cancelled, "rescheduled": rescheduled}


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------
def due_messages(db: Session, *, now: datetime | None = None, limit: int = 200) -> list[ScheduledMessage]:
    """Messages whose minute has arrived, on a campaign that may send."""
    now = now or utcnow()
    return list(
        db.execute(
            select(ScheduledMessage)
            .join(Automation, Automation.id == ScheduledMessage.automation_id)
            .where(
                ScheduledMessage.status == ScheduledMessageStatus.SCHEDULED.value,
                ScheduledMessage.scheduled_at <= now,
                Automation.status.in_(SENDING_STATUSES),
            )
            .order_by(ScheduledMessage.scheduled_at)
            .limit(limit)
        )
        .scalars()
        .all()
    )


def claim(db: Session, message: ScheduledMessage, *, now: datetime | None = None) -> bool:
    """Take exclusive ownership of a message, or report that somebody else has.

    A conditional update rather than a read-then-write: two schedulers running
    the same second both see a SCHEDULED row, and only the one whose UPDATE
    matches gets to send. Without this the duplicate protection would depend
    on the two processes never overlapping, which is not a property anybody
    can promise about a background job.
    """
    now = now or utcnow()
    result = db.execute(
        update(ScheduledMessage)
        .where(
            ScheduledMessage.id == message.id,
            ScheduledMessage.status == ScheduledMessageStatus.SCHEDULED.value,
        )
        .values(
            status=ScheduledMessageStatus.PROCESSING.value,
            attempts=ScheduledMessage.attempts + 1,
            updated_at=now,
        )
    )
    db.commit()
    if not result.rowcount:
        return False
    db.refresh(message)
    return True


def dispatch_due(
    db: Session, *, now: datetime | None = None, limit: int = 200
) -> dict:
    """Send every reminder that has come due, one customer at a time."""
    now = now or utcnow()
    stats = {
        "considered": 0,
        "sent": 0,
        "cancelled": 0,
        "suppressed": 0,
        "failed": 0,
        "expired": 0,
        "claim_lost": 0,
    }
    for message in due_messages(db, now=now, limit=limit):
        stats["considered"] += 1
        outcome = dispatch_one(db, message, now=now)
        stats[outcome] = stats.get(outcome, 0) + 1
    return stats


def dispatch_one(db: Session, message: ScheduledMessage, *, now: datetime | None = None) -> str:
    """Run the final checks on one message and, if they pass, send it.

    The order of the checks is the point. Whether the customer has already
    ordered is asked first and separately, because it is the only one whose
    answer means "this reminder was a good idea and is no longer needed"
    rather than "this customer may not be messaged". Everything after it is
    the shared eligibility pipeline, unchanged.
    """
    now = now or utcnow()
    automation = db.get(Automation, message.automation_id)
    if automation is None or automation.status not in SENDING_STATUSES:
        cancel(
            db,
            message,
            CancellationReason.CAMPAIGN_PAUSED,
            detail="The campaign was not active when this message came due.",
            now=now,
        )
        return "cancelled"

    # Too late to be useful. A reminder aimed at somebody's Wednesday evening
    # is not worth sending on Friday morning.
    if message.scheduled_at < now - timedelta(minutes=STALE_AFTER_MINUTES):
        message.status = ScheduledMessageStatus.EXPIRED.value
        message.cancellation_reason = CancellationReason.WINDOW_MISSED.value
        message.cancellation_detail = (
            f"Due {to_local(message.scheduled_at):%-d %b %-I:%M %p} and not sent in time."
        )
        message.cancelled_at = now
        db.commit()
        return "expired"

    if message.attempts >= MAX_ATTEMPTS:
        message.status = ScheduledMessageStatus.FAILED.value
        message.error_message = f"Gave up after {message.attempts} attempts."
        db.commit()
        return "failed"

    if not claim(db, message, now=now):
        return "claim_lost"

    # THE check. Asked against the order the prediction was built from, so an
    # order placed at any point between writing this message and sending it
    # counts — that gap is the whole window in which they might have beaten
    # us to it. The anchor is stored rather than worked back from the
    # prediction: "predicted minus the usual interval" lands an hour or two
    # early and catches the customer's own last order, which cancelled
    # perfectly good reminders.
    since = message.cycle_start_at or message.created_at
    if _ordered_since(db, message.customer_id, since):
        cancel(
            db,
            message,
            CancellationReason.CUSTOMER_ALREADY_ORDERED,
            detail="They ordered before the reminder was due.",
            now=now,
        )
        return "cancelled"

    customer = db.get(Customer, message.customer_id)
    if customer is None:
        cancel(db, message, CancellationReason.NO_LONGER_ELIGIBLE, now=now)
        return "cancelled"

    # Everything else — consent, channel consent, suppression, age, frequency
    # cap, quiet hours, contactability, dedup, content compliance — is the
    # shared pipeline. Deliberately not reimplemented here: a second copy of
    # the compliance rules is a second thing to keep right, and the one that
    # drifts is the one that texts somebody who opted out.
    candidate = Candidate(
        customer_id=customer.id,
        scheduled_for=message.scheduled_at,
        body=message.rendered_message,
        enrollment_id=message.enrollment_id,
        context={**(message.context or {}), "scheduled_message_id": message.id},
    )
    report = execute_candidates(
        db,
        automation,
        [candidate],
        now=now,
        # Its time has come; deferring it to tomorrow morning would send a
        # reminder about a moment that has passed. Quiet hours still block it
        # — the gate refuses rather than postpones.
        defer_outside_window=False,
    )
    return _record_outcome(db, message, report, now=now)


def _record_outcome(db: Session, message: ScheduledMessage, report, *, now: datetime) -> str:
    """Map what the send pipeline decided back onto the queued message."""
    result = report.results[0] if report.results else None
    if result is None:
        message.status = ScheduledMessageStatus.SCHEDULED.value
        db.commit()
        return "claim_lost"

    if result.status in (SendStatus.SENT, SendStatus.DELIVERED):
        message.status = ScheduledMessageStatus.SENT.value
        message.sent_at = now
        message.provider = report.provider
        message.provider_message_id = result.context.get("provider_message_id")
        message.rendered_message = result.body or message.rendered_message
        db.commit()
        return "sent"

    if result.status == SendStatus.FAILED:
        message.status = ScheduledMessageStatus.FAILED.value
        message.error_message = result.skip_detail or "The provider refused the message."
        message.error_code = result.skip_reason.value if result.skip_reason else None
        db.commit()
        return "failed"

    # Skipped. Which of the two it is matters: already-ordered is the engine
    # getting it right, and a consent skip is a customer we may not contact.
    reason = result.skip_reason
    if reason in (SkipReason.ALREADY_ORDERED, SkipReason.PENDING_ORDER):
        cancel(
            db,
            message,
            CancellationReason.CUSTOMER_ALREADY_ORDERED,
            detail=result.skip_detail or "",
            now=now,
        )
        return "cancelled"

    message.status = ScheduledMessageStatus.SUPPRESSED.value
    message.cancellation_reason = (reason.value if reason else None)
    message.cancellation_detail = result.skip_detail
    message.cancelled_at = now
    db.commit()
    return "suppressed"
