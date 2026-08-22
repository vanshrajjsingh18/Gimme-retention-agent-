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

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analytics.order_patterns import (
    MIN_ORDERS_FOR_PATTERN,
    OrderPattern,
    OfferDecision,
    PATTERN_STALE_AFTER_DAYS,
    compute_order_pattern,
    decide_offer,
    next_nudge_time,
    should_recompute,
)
from app.automations.cohort import resolve_audience
from app.automations.runtime import Candidate, RunReport, execute_candidates
from app.automations.templates import build_context, get_brand, render
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
from app.services.intelligence import load_order_facts

logger = logging.getLogger(__name__)

#: Days ahead of the customer's usual slot to send. One day means the message
#: lands the evening before they would typically order.
DEFAULT_LEAD_DAYS = 0

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
    cfg.setdefault("lead_days", DEFAULT_LEAD_DAYS)
    cfg.setdefault("min_gap_days", DEFAULT_MIN_GAP_DAYS)
    cfg.setdefault("min_orders", MIN_ORDERS_FOR_PATTERN)
    cfg.setdefault("pattern_max_age_days", PATTERN_STALE_AFTER_DAYS)
    return cfg


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
    for customer_id in resolve_audience(db, automation, now=now):
        if customer_id in existing:
            continue
        pattern = compute_pattern(db, customer_id, now=now, min_orders=cfg["min_orders"])
        if not pattern.has_pattern:
            skipped_no_pattern += 1
            continue
        db.add(
            AutomationEnrollment(
                automation_id=automation.id,
                customer_id=customer_id,
                status=EnrollmentStatus.ACTIVE.value,
                enrolled_at=now,
                pattern=pattern.as_dict(),
                next_due_at=_due_from_pattern(pattern, after=now, lead_days=cfg["lead_days"]),
            )
        )
        enrolled += 1

    if commit:
        db.commit()
    return {
        "enrolled": enrolled,
        "already_enrolled": len(existing),
        "skipped_no_pattern": skipped_no_pattern,
    }


def compute_pattern(
    db: Session, customer_id: int, *, now: datetime, min_orders: int = MIN_ORDERS_FOR_PATTERN
) -> OrderPattern:
    """Derive one customer's ordering rhythm from their order history."""
    return compute_order_pattern(
        load_order_facts(db, customer_id), now=now, min_orders=min_orders
    )


def refresh_patterns(
    db: Session,
    automation: Automation,
    *,
    now: datetime | None = None,
    force: bool = False,
    commit: bool = True,
) -> dict:
    """Recompute stale patterns. Run monthly; habits drift.

    A customer whose history no longer supports a pattern is stopped rather
    than nudged on an old one.
    """
    now = now or utcnow()
    cfg = config_of(automation)
    refreshed = 0
    dropped = 0

    for enrollment in _active(db, automation):
        if not force and not should_recompute(
            enrollment.pattern, now=now, max_age_days=cfg["pattern_max_age_days"]
        ):
            continue
        pattern = compute_pattern(
            db, enrollment.customer_id, now=now, min_orders=cfg["min_orders"]
        )
        enrollment.pattern = pattern.as_dict()
        if not pattern.has_pattern:
            enrollment.status = EnrollmentStatus.STOPPED.value
            enrollment.stop_reason = pattern.reason
            enrollment.stopped_at = now
            enrollment.next_due_at = None
            dropped += 1
            continue
        enrollment.next_due_at = _due_from_pattern(
            pattern, after=max(now, enrollment.last_sent_at or now), lead_days=cfg["lead_days"]
        )
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


def _due_from_pattern(
    pattern: OrderPattern, *, after: datetime, lead_days: int
) -> datetime | None:
    """Next due time in naive UTC, from a pattern expressed in local time."""
    local_after = to_local(after).replace(tzinfo=None)
    local_due = next_nudge_time(pattern, after=local_after, lead_days=lead_days)
    return to_utc_naive(local_due) if local_due else None


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
    pattern: OrderPattern,
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
            "usual_day": pattern.weekday_name or "usual day",
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
) -> tuple[list[Candidate], dict[int, AutomationEnrollment]]:
    cfg = config_of(automation)
    enrollments = enrollments if enrollments is not None else _active(db, automation)
    due = [
        e
        for e in enrollments
        if e.next_due_at is not None
        and e.next_due_at <= now
        and not _too_soon(e, now=now, min_gap_days=cfg["min_gap_days"])
    ]
    if not due:
        return [], {}

    customer_ids = [e.customer_id for e in due]
    pending = customers_with_pending_orders(db, customer_ids)
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
        pattern = OrderPattern(**_pattern_fields(enrollment.pattern))
        if enrollment.customer_id in pending:
            # Recorded as a candidate so the skip is visible in the ledger and
            # the preview, rather than the customer quietly disappearing.
            candidates.append(
                Candidate(
                    customer_id=customer.id,
                    scheduled_for=enrollment.next_due_at or now,
                    body="",
                    enrollment_id=enrollment.id,
                    context={
                        "source": "nudge",
                        "suppressed": SkipReason.PENDING_ORDER.value,
                        "detail": "Customer has an order in flight.",
                    },
                )
            )
            by_customer[customer.id] = enrollment
            continue

        body, offer = render_nudge(db, automation, customer, pattern, now=now)
        candidates.append(
            Candidate(
                customer_id=customer.id,
                scheduled_for=enrollment.next_due_at or now,
                body=body,
                enrollment_id=enrollment.id,
                context={
                    "source": "nudge",
                    "usual_day": pattern.weekday_name,
                    "usual_hour": pattern.typical_hour,
                    "pattern_confidence": pattern.confidence,
                    "offer": offer.as_dict(),
                },
            )
        )
        by_customer[customer.id] = enrollment
    return candidates, by_customer


def _pattern_fields(blob: dict | None) -> dict:
    """Rebuild an OrderPattern from its stored JSON, ignoring stray keys."""
    from datetime import datetime as _dt

    known = set(OrderPattern.__dataclass_fields__)
    data = {k: v for k, v in (blob or {}).items() if k in known}
    for key in ("window_start", "window_end", "computed_at"):
        if isinstance(data.get(key), str):
            try:
                data[key] = _dt.fromisoformat(data[key])
            except ValueError:
                data[key] = None
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

    if not dry_run:
        enroll(db, automation, now=now, commit=False)
        db.flush()
        refresh_patterns(db, automation, now=now, commit=False)
        _stop_opted_out(db, automation, now=now)
        db.commit()

    enrollments = _active(db, automation)
    candidates, by_customer = build_candidates(
        db, automation, now=now, enrollments=enrollments
    )

    # Pending-order candidates carry no body; short-circuit them here so they
    # are logged as skips rather than sent as empty messages.
    sendable = [c for c in candidates if not c.context.get("suppressed")]
    report = execute_candidates(db, automation, sendable, now=now, dry_run=dry_run)
    for candidate in candidates:
        if candidate.context.get("suppressed"):
            report.results.append(
                _pending_skip(db, automation, candidate, now=now, dry_run=dry_run)
            )

    if not dry_run:
        _reschedule(db, report, by_customer, lead_days=cfg["lead_days"], now=now)
        db.commit()
    return report


def _pending_skip(
    db: Session, automation: Automation, candidate: Candidate, *, now: datetime, dry_run: bool
):
    from app.automations.runtime import SendDecision, _record, priority_for

    when = candidate.scheduled_for
    decision = SendDecision(
        customer_id=candidate.customer_id,
        status=SendStatus.SKIPPED,
        scheduled_for=when,
        local_date=local_date(when),
        skip_reason=SkipReason.PENDING_ORDER,
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
    lead_days: int,
    now: datetime,
) -> None:
    """Set each customer's next due time after a run.

    A sent nudge advances from the send; a skipped one is pushed to the next
    matching slot rather than retried immediately, so a customer who lost a
    dedup contest is not chased the following morning.
    """
    for result in report.results:
        enrollment = by_customer.get(result.customer_id)
        if enrollment is None:
            continue
        pattern = OrderPattern(**_pattern_fields(enrollment.pattern))
        if result.status == SendStatus.SENT:
            enrollment.last_sent_at = now
        enrollment.next_due_at = _due_from_pattern(
            pattern, after=now, lead_days=lead_days
        )
    db.flush()
