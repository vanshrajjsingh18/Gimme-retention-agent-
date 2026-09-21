"""Feature 2 — behavioural nudge-to-order.

A standing automation, not a campaign with an end date: each enrolled customer
is messaged at the day and time *they* usually order, with an offer only where
their own history justifies one, and it keeps running until they opt out.

The timing comes from :mod:`app.analytics.order_patterns`, which refuses to
produce a pattern from too few orders — a customer with two orders has no
rhythm, and inventing one produces a message timed by coincidence. Patterns
are recomputed on a schedule because habits drift.

Three safeguards keep it from becoming a nuisance:

* nothing is sent to a customer with an order already in flight — they do not
  need reminding to buy something they have just bought;
* the nudge is aimed slightly *ahead* of their usual slot, so it arrives while
  they are deciding rather than after they have ordered;
* the shared runtime's dedup gives the nudge the highest priority of the three
  automation types, so it displaces a bulk send rather than arriving alongside
  one.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.analytics.order_patterns import (
    MIN_ORDERS_FOR_PATTERN,
    OfferDecision,
    PATTERN_STALE_AFTER_DAYS,
    decide_offer,
    should_recompute,
)
from app.analytics.order_predictions import (
    DEFAULT_REMINDER_OFFSET,
    REMINDER_OFFSETS,
    OrderPrediction,
)
from app.services.reorder_timing import (
    ReminderPlan,
    clamp_to_window,  # noqa: F401 - re-exported; it lived here before
    plan_for_customer,
)
from app.automations.cohort import resolve_audience
from app.automations.runtime import Candidate, RunReport, execute_candidates
from app.automations.templates import build_context, get_brand, render
from app.core.config import settings
from app.core.enums import (
    AutomationKind,
    EnrollmentStatus,
    OrderStatus,
    SendStatus,
    SkipReason,
)
from app.core.timezones import local_date, to_local, to_utc_naive
from app.models.base import utcnow
from app.models.entities import (
    Automation,
    AutomationEnrollment,
    Customer,
    CustomerMetrics,
    Order,
)
from app.services.brand import get_brand_settings
from app.services.intelligence import load_local_order_facts

logger = logging.getLogger(__name__)

#: Never nudge the same customer more often than this, regardless of pattern.
#: A weekly buyer gets a weekly nudge; a monthly buyer does not get four.
DEFAULT_MIN_GAP_DAYS = 7

DEFAULT_NUDGE_TEMPLATE = (
    "Hi {first_name}, it's about your usual {usual_day} — want us to bring your "
    "{usual_category} round? {offer_line}Order at {website}. Reply STOP to opt out."
)

STOP_OPTED_OUT = "Customer opted out."


def config_of(automation: Automation) -> dict:
    cfg = dict(automation.config or {})

    # `lead_hours` was how far ahead of a bucketed hour to send, before the
    # schedule moved onto the minute-level prediction. An automation created
    # under the old model still means "send this far ahead", so it is carried
    # across as the equivalent offset rather than ignored — an old setting
    # silently doing nothing is how a campaign ends up sending at a time
    # nobody chose.
    legacy = {"lead_hours", "lead_days"} & set(cfg)
    if legacy and "reminder_offset" not in cfg and "custom_offset_minutes" not in cfg:
        # Mapped on the key being present, not on it being non-zero: a
        # deliberate `lead_hours: 0` means "at their usual time", and reading
        # that as "unset" would quietly move the send half an hour earlier
        # than the person who configured it asked for.
        cfg["custom_offset_minutes"] = (
            int(cfg.get("lead_hours", 0)) * 60 + int(cfg.get("lead_days", 0)) * 1440
        )

    cfg.setdefault("reminder_offset", DEFAULT_REMINDER_OFFSET)
    cfg.setdefault("custom_offset_minutes", None)
    cfg.setdefault("min_gap_days", DEFAULT_MIN_GAP_DAYS)
    cfg.setdefault("min_orders", MIN_ORDERS_FOR_PATTERN)
    cfg.setdefault("min_confidence", 0)
    cfg.setdefault("pattern_max_age_days", PATTERN_STALE_AFTER_DAYS)
    return cfg


def plan_for(
    db: Session, customer_id: int, cfg: dict, *, now: datetime, after: datetime | None = None
) -> ReminderPlan:
    """This customer's next reminder, under this automation's settings."""
    return plan_for_customer(
        db,
        customer_id,
        now=now,
        after=after or now,
        offset=cfg["reminder_offset"],
        custom_minutes=cfg["custom_offset_minutes"],
        min_orders=cfg["min_orders"],
    )


# --------------------------------------------------------------------------
# Enrollment and pattern maintenance
# --------------------------------------------------------------------------
def enroll(
    db: Session,
    automation: Automation,
    *,
    now: datetime | None = None,
    commit: bool = True,
) -> dict:
    """Enroll matching customers who have a usable ordering pattern.

    Customers without enough history are simply not enrolled; they are picked
    up automatically on a later run once they have ordered enough times.
    """
    now = now or utcnow()
    cfg = config_of(automation)
    existing = {
        row.customer_id: row
        for row in db.execute(
            select(AutomationEnrollment).where(
                AutomationEnrollment.automation_id == automation.id
            )
        )
        .scalars()
        .all()
    }

    enrolled = 0
    skipped_no_pattern = 0
    skipped_low_confidence = 0
    for customer_id in resolve_audience(db, automation, now=now):
        if customer_id in existing:
            continue
        plan = plan_for(db, customer_id, cfg, now=now)
        if not plan.has_plan:
            skipped_no_pattern += 1
            continue
        if plan.prediction.overall_confidence < cfg["min_confidence"]:
            # A routine the engine is not confident in produces a message
            # timed by coincidence. Counted rather than dropped silently, so
            # the threshold's cost is visible where it is being paid.
            skipped_low_confidence += 1
            continue
        db.add(
            AutomationEnrollment(
                automation_id=automation.id,
                customer_id=customer_id,
                status=EnrollmentStatus.ACTIVE.value,
                enrolled_at=now,
                pattern=_store_routine(plan, now=now),
                next_due_at=plan.scheduled_utc,
            )
        )
        enrolled += 1

    if commit:
        db.commit()
    return {
        "enrolled": enrolled,
        "already_enrolled": len(existing),
        "skipped_no_pattern": skipped_no_pattern,
        "skipped_low_confidence": skipped_low_confidence,
    }


#: Marks a stored routine as the minute-level prediction rather than the older
#: hour-bucket pattern, so a blob written by the previous model is recomputed
#: instead of being read with the wrong field names.
ROUTINE_KIND = "prediction"


def _store_routine(plan: ReminderPlan, *, now: datetime) -> dict:
    """What goes in the enrollment's ``pattern`` column.

    ``computed_at`` is written because staleness is read from it: without one,
    ``should_recompute`` treats every stored routine as expired and the whole
    audience is recomputed from order history on every five-minute run.
    """
    blob = plan.prediction.as_dict()
    blob["kind"] = ROUTINE_KIND
    blob["computed_at"] = now.isoformat()
    blob["reminder_offset"] = plan.offset
    blob["offset_minutes"] = plan.offset_minutes
    blob["moved_for_send_window"] = plan.moved_for_send_window
    return blob


def refresh_patterns(
    db: Session,
    automation: Automation,
    *,
    now: datetime | None = None,
    force: bool = False,
    commit: bool = True,
    skip_due: bool = False,
) -> dict:
    """Recompute stale patterns. Run monthly; habits drift.

    A customer whose history no longer supports a pattern is stopped rather
    than nudged on an old one.

    ``skip_due`` is set by a live run, which is about to act on the
    enrollments that have come due and must decide their fate before they are
    replanned. Left to replan them, a customer who had just ordered would have
    their slot moved out of the run's reach and vanish from it — no message,
    which is right, but no reason in the ledger either. An operator asking to
    refresh patterns on its own wants every stale routine recomputed, overdue
    ones included, so the default is off.
    """
    now = now or utcnow()
    cfg = config_of(automation)
    refreshed = 0
    dropped = 0

    for enrollment in _active(db, automation):
        if skip_due and enrollment.next_due_at is not None and enrollment.next_due_at <= now:
            continue
        stale = should_recompute(
            enrollment.pattern, now=now, max_age_days=cfg["pattern_max_age_days"]
        )
        # A routine stored by the previous, hour-bucket model is recomputed
        # whatever its age: its fields mean something different, and reading
        # it as a prediction would schedule from values that were never
        # measured the same way.
        outdated_shape = (enrollment.pattern or {}).get("kind") != ROUTINE_KIND
        if not force and not stale and not outdated_shape:
            continue
        plan = plan_for(
            db,
            enrollment.customer_id,
            cfg,
            now=now,
            after=max(now, enrollment.last_sent_at or now),
        )
        if not plan.has_plan:
            enrollment.pattern = {"kind": ROUTINE_KIND, "has_prediction": False, "reason": plan.reason}
            enrollment.status = EnrollmentStatus.STOPPED.value
            enrollment.stop_reason = plan.reason
            enrollment.stopped_at = now
            enrollment.next_due_at = None
            dropped += 1
            continue
        enrollment.pattern = _store_routine(plan, now=now)
        enrollment.next_due_at = plan.scheduled_utc
        refreshed += 1

    if commit:
        db.commit()
    return {"refreshed": refreshed, "dropped": dropped, "checked_at": now.isoformat()}


def _active(db: Session, automation: Automation) -> list[AutomationEnrollment]:
    return list(
        db.execute(
            select(AutomationEnrollment).where(
                AutomationEnrollment.automation_id == automation.id,
                AutomationEnrollment.status == EnrollmentStatus.ACTIVE.value,
            )
        )
        .scalars()
        .all()
    )


def routine_of(enrollment: AutomationEnrollment) -> OrderPrediction:
    """Rebuild the stored routine, ignoring stray and legacy keys."""
    return OrderPrediction(**_pattern_fields(enrollment.pattern))


# --------------------------------------------------------------------------
# Offer and copy
# --------------------------------------------------------------------------
def offer_for(db: Session, customer_id: int) -> OfferDecision:
    """Whether this customer's nudge carries a discount.

    Reuses the discount-dependency metric the retention engine already
    computes, and will only ever name a promotion that exists in brand
    settings.
    """
    brand = get_brand_settings(db)
    metrics = db.execute(
        select(CustomerMetrics).where(CustomerMetrics.customer_id == customer_id)
    ).scalar_one_or_none()
    return decide_offer(
        discount_dependency=metrics.discount_dependency if metrics else 0.0,
        verified_promotions=list(brand.allowed_promotions or []),
        verified_coupon_codes=list(brand.active_coupon_codes or []),
    )


def favourite_category(db: Session, customer_id: int) -> str:
    """The category this customer actually buys, or a neutral word.

    Drawn from their own order history, so naming it in a message is a
    statement of fact rather than a guess.
    """
    metrics = db.execute(
        select(CustomerMetrics).where(CustomerMetrics.customer_id == customer_id)
    ).scalar_one_or_none()
    categories = (metrics.preferred_categories if metrics else None) or []
    if not categories:
        return "usual"
    top = categories[0]
    # Stored either as plain names or as {"category": ..., "share": ...} rows.
    name = top.get("category") if isinstance(top, dict) else top
    return str(name).lower() if name else "usual"


def render_nudge(
    db: Session,
    automation: Automation,
    customer: Customer,
    routine: OrderPrediction,
    *,
    now: datetime,
) -> tuple[str, OfferDecision]:
    offer = offer_for(db, customer.id)
    brand = get_brand(db)
    offer_line = ""
    if offer.include_discount and offer.promotion:
        offer_line = f"{offer.promotion}"
        if offer.coupon_code:
            offer_line += f" with code {offer.coupon_code}"
        offer_line += ". "

    template = automation.message_template or DEFAULT_NUDGE_TEMPLATE
    context = build_context(
        customer,
        brand,
        extra={
            "usual_day": routine.preferred_weekday_name or "usual day",
            "usual_category": favourite_category(db, customer.id),
            "offer_line": offer_line,
            "promotion": offer.promotion or "",
            "coupon_code": offer.coupon_code or "",
        },
        now=now,
    )
    return render(template, context), offer


# --------------------------------------------------------------------------
# Safeguards
# --------------------------------------------------------------------------
def customers_with_pending_orders(db: Session, customer_ids: list[int]) -> set[int]:
    """Customers with an order in flight — nudging them would be nonsense."""
    if not customer_ids:
        return set()
    rows = db.execute(
        select(Order.customer_id).where(
            Order.customer_id.in_(customer_ids),
            Order.status == OrderStatus.PENDING.value,
        )
    ).all()
    return {row[0] for row in rows}


#: Order states that mean "they have bought". A cancelled order is not one:
#: somebody who cancelled has not been served and may well still want their
#: usual.
ORDERED_STATUSES = (OrderStatus.PENDING.value, OrderStatus.COMPLETED.value)


def customers_who_already_ordered(
    db: Session, cycle_start_by_customer: dict[int, datetime]
) -> dict[int, datetime]:
    """Customers who have ordered since the prediction was made.

    The one message this feature must never send is "ready for your usual
    order?" to somebody who ordered twenty minutes ago. Checking only for a
    PENDING order caught an order still in flight and missed the finished
    one — and a real order lands COMPLETED, so the common case was the one
    that got through.

    "Since the prediction was made" is the right boundary rather than "today"
    or "recently": the prediction was built from their last order, so any
    order after it is this cycle's, which is exactly the purchase the
    reminder was trying to prompt. It also means a weekly customer is not
    permanently suppressed by the order that taught us their routine.
    """
    if not cycle_start_by_customer:
        return {}

    rows = db.execute(
        select(Order.customer_id, func.max(Order.ordered_at))
        .where(
            Order.customer_id.in_(list(cycle_start_by_customer)),
            Order.status.in_(ORDERED_STATUSES),
        )
        .group_by(Order.customer_id)
    ).all()

    already: dict[int, datetime] = {}
    for customer_id, latest in rows:
        boundary = cycle_start_by_customer.get(customer_id)
        if latest is not None and boundary is not None and latest > boundary:
            already[customer_id] = latest
    return already


def _cycle_start(enrollment: AutomationEnrollment, routine: OrderPrediction) -> datetime | None:
    """The order this customer's current prediction was built from, in UTC."""
    if routine.last_order_at is None:
        return None
    # Stored local (the routine is learned on the customer's clock); the
    # orders it is compared against are the naive UTC the database holds.
    return to_utc_naive(routine.last_order_at)


def _too_soon(
    enrollment: AutomationEnrollment, *, now: datetime, min_gap_days: int
) -> bool:
    if enrollment.last_sent_at is None:
        return False
    return (now - enrollment.last_sent_at) < timedelta(days=min_gap_days)


# --------------------------------------------------------------------------
# Run
# --------------------------------------------------------------------------
def build_candidates(
    db: Session,
    automation: Automation,
    *,
    now: datetime,
    enrollments: list[AutomationEnrollment] | None = None,
    ignore_due_time: bool = False,
) -> tuple[list[Candidate], dict[int, AutomationEnrollment]]:
    """Build the nudges that have come due.

    ``ignore_due_time`` is for previews: a live run should only send what is
    due right now, but an operator previewing the automation wants to see the
    whole standing audience and each customer's scheduled slot, not just the
    handful whose slot happens to fall in this minute.
    """
    cfg = config_of(automation)
    enrollments = enrollments if enrollments is not None else _active(db, automation)
    due = [
        e
        for e in enrollments
        if e.next_due_at is not None
        and (ignore_due_time or e.next_due_at <= now)
        and (ignore_due_time or not _too_soon(e, now=now, min_gap_days=cfg["min_gap_days"]))
    ]
    if not due:
        return [], {}

    customer_ids = [e.customer_id for e in due]
    routines = {e.customer_id: routine_of(e) for e in due}
    pending = customers_with_pending_orders(db, customer_ids)
    already = customers_who_already_ordered(
        db,
        {
            e.customer_id: start
            for e in due
            if (start := _cycle_start(e, routines[e.customer_id])) is not None
        },
    )
    customers = {
        c.id: c
        for c in db.execute(select(Customer).where(Customer.id.in_(customer_ids)))
        .scalars()
        .all()
    }

    candidates: list[Candidate] = []
    by_customer: dict[int, AutomationEnrollment] = {}
    for enrollment in due:
        customer = customers.get(enrollment.customer_id)
        if customer is None:
            continue
        routine = routines[enrollment.customer_id]

        suppression = _suppression_for(enrollment.customer_id, pending, already)
        if suppression is not None:
            # Recorded as a candidate so the skip is visible in the ledger and
            # the preview, rather than the customer quietly disappearing.
            reason, detail = suppression
            candidates.append(
                Candidate(
                    customer_id=customer.id,
                    scheduled_for=enrollment.next_due_at or now,
                    body="",
                    enrollment_id=enrollment.id,
                    context={
                        "source": "nudge",
                        "suppressed": reason.value,
                        "detail": detail,
                    },
                )
            )
            by_customer[customer.id] = enrollment
            continue

        body, offer = render_nudge(db, automation, customer, routine, now=now)
        candidates.append(
            Candidate(
                customer_id=customer.id,
                scheduled_for=enrollment.next_due_at or now,
                body=body,
                enrollment_id=enrollment.id,
                context={
                    "source": "nudge",
                    "usual_day": routine.preferred_weekday_name,
                    "usual_time": routine.preferred_time_label,
                    "predicted_order_at": (
                        routine.predicted_next_order_at.isoformat()
                        if routine.predicted_next_order_at
                        else None
                    ),
                    "pattern_confidence": routine.overall_confidence,
                    "offer": offer.as_dict(),
                },
            )
        )
        by_customer[customer.id] = enrollment
    return candidates, by_customer


def _suppression_for(
    customer_id: int, pending: set[int], already: dict[int, datetime]
) -> tuple[SkipReason, str] | None:
    """Why this customer must not be reminded, if they must not be.

    An order in flight is checked first. Both facts stop the send, but a
    pending order is the narrower statement — "their order is on its way"
    tells an operator more than "they ordered at some point since we
    predicted", and the ledger should carry the more specific of two true
    reasons.
    """
    if customer_id in pending:
        return (SkipReason.PENDING_ORDER, "Customer has an order in flight.")
    if customer_id in already:
        when = already[customer_id]
        return (
            SkipReason.ALREADY_ORDERED,
            f"Customer ordered on {to_local(when):%-d %b at %-I:%M %p} — after this "
            "reminder was predicted, so there is nothing to remind them about.",
        )
    return None


def _prospective_enrollments(
    db: Session, automation: Automation, *, now: datetime
) -> list[AutomationEnrollment]:
    """Transient enrollments for customers who would join on a live run.

    Never added to the session — a dry run must not change state. Customers
    without a usable order pattern are left out here exactly as they would be
    by :func:`enroll`, so the preview count matches what a live run produces.
    """
    cfg = config_of(automation)
    enrolled = set(
        db.execute(
            select(AutomationEnrollment.customer_id).where(
                AutomationEnrollment.automation_id == automation.id
            )
        )
        .scalars()
        .all()
    )

    prospective: list[AutomationEnrollment] = []
    for customer_id in resolve_audience(db, automation, now=now):
        if customer_id in enrolled:
            continue
        plan = plan_for(db, customer_id, cfg, now=now)
        if not plan.has_plan or plan.prediction.overall_confidence < cfg["min_confidence"]:
            continue
        prospective.append(
            AutomationEnrollment(
                automation_id=automation.id,
                customer_id=customer_id,
                status=EnrollmentStatus.ACTIVE.value,
                enrolled_at=now,
                pattern=_store_routine(plan, now=now),
                next_due_at=plan.scheduled_utc,
            )
        )
    return prospective


def _pattern_fields(blob: dict | None) -> dict:
    """Rebuild an OrderPrediction from its stored JSON, ignoring stray keys."""
    from datetime import datetime as _dt

    from app.analytics.order_predictions import IntervalStats

    known = set(OrderPrediction.__dataclass_fields__)
    data = {k: v for k, v in (blob or {}).items() if k in known}
    for key in ("last_order_at", "predicted_next_order_at"):
        if isinstance(data.get(key), str):
            try:
                data[key] = _dt.fromisoformat(data[key])
            except ValueError:
                data[key] = None
    if isinstance(data.get("intervals"), dict):
        fields = set(IntervalStats.__dataclass_fields__)
        data["intervals"] = IntervalStats(
            **{k: v for k, v in data["intervals"].items() if k in fields}
        )
    return data


def run(
    db: Session,
    automation: Automation,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
) -> RunReport:
    """Send every nudge that has come due."""
    if automation.kind != AutomationKind.NUDGE.value:
        raise ValueError(f"Automation {automation.id} is not a nudge automation.")
    now = now or utcnow()
    cfg = config_of(automation)

    if dry_run:
        # Enrollment only happens on a live run, so a preview of a nudge
        # nobody has joined yet would be empty and would tell an operator
        # nothing — which defeats the point of previewing before approving.
        # Simulate it in memory instead.
        enrollments = _active(db, automation) + _prospective_enrollments(
            db, automation, now=now
        )
    else:
        enroll(db, automation, now=now, commit=False)
        db.flush()
        refresh_patterns(db, automation, now=now, commit=False, skip_due=True)
        _stop_opted_out(db, automation, now=now)
        db.commit()
        enrollments = _active(db, automation)

    candidates, by_customer = build_candidates(
        db, automation, now=now, enrollments=enrollments, ignore_due_time=dry_run
    )

    # Suppressed candidates carry no body; short-circuit them here so they
    # are logged as skips rather than sent as empty messages.
    sendable = [c for c in candidates if not c.context.get("suppressed")]
    report = execute_candidates(db, automation, sendable, now=now, dry_run=dry_run)
    for candidate in candidates:
        if candidate.context.get("suppressed"):
            report.results.append(
                _order_skip(db, automation, candidate, now=now, dry_run=dry_run)
            )

    if not dry_run:
        _reschedule(db, report, by_customer, cfg=cfg, now=now)
        db.commit()
    return report


def _order_skip(
    db: Session, automation: Automation, candidate: Candidate, *, now: datetime, dry_run: bool
):
    """Record a skip for a customer whose orders rule the reminder out.

    The reason comes from the candidate rather than being assumed here. It
    was hard-coded to PENDING_ORDER, so even once an already-ordered customer
    was detected the ledger would have said their order was still on its way —
    which is a different fact, and the wrong one to show somebody asking why
    no reminder went out.
    """
    from app.automations.runtime import SendDecision, _record, priority_for

    when = candidate.scheduled_for
    reason = SkipReason(candidate.context.get("suppressed", SkipReason.PENDING_ORDER.value))
    decision = SendDecision(
        customer_id=candidate.customer_id,
        status=SendStatus.SKIPPED,
        scheduled_for=when,
        local_date=local_date(when),
        skip_reason=reason,
        skip_detail=candidate.context.get("detail"),
        context=candidate.context,
    )
    _record(
        db,
        automation,
        candidate,
        decision,
        priority=priority_for(automation),
        dry_run=dry_run,
    )
    return decision


def _stop_opted_out(db: Session, automation: Automation, *, now: datetime) -> int:
    """Drop enrollments for customers who have since opted out."""
    enrollments = _active(db, automation)
    if not enrollments:
        return 0
    customers = {
        c.id: c
        for c in db.execute(
            select(Customer).where(
                Customer.id.in_([e.customer_id for e in enrollments])
            )
        )
        .scalars()
        .all()
    }
    stopped = 0
    for enrollment in enrollments:
        customer = customers.get(enrollment.customer_id)
        if customer is None or customer.is_suppressed or not customer.marketing_consent:
            enrollment.status = EnrollmentStatus.STOPPED.value
            enrollment.stop_reason = STOP_OPTED_OUT
            enrollment.stopped_at = now
            enrollment.next_due_at = None
            stopped += 1
    return stopped


def _reschedule(
    db: Session,
    report: RunReport,
    by_customer: dict[int, AutomationEnrollment],
    *,
    cfg: dict,
    now: datetime,
) -> None:
    """Set each customer's next due time after a run.

    A sent nudge advances from the send; a skipped one is pushed to the next
    matching slot rather than retried immediately, so a customer who lost a
    dedup contest is not chased the following morning.

    Replanned from order history rather than from the stored routine, so a
    customer who was skipped *because they just ordered* is rescheduled
    around that new order — their cycle has restarted, and aiming at the old
    prediction would chase them again next run.
    """
    for result in report.results:
        enrollment = by_customer.get(result.customer_id)
        if enrollment is None:
            continue
        if result.status == SendStatus.SENT:
            enrollment.last_sent_at = now
        plan = plan_for(db, enrollment.customer_id, cfg, now=now)
        if plan.has_plan:
            enrollment.pattern = _store_routine(plan, now=now)
            enrollment.next_due_at = plan.scheduled_utc
        else:
            enrollment.next_due_at = None
    db.flush()
