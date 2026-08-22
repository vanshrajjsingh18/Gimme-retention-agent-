"""Feature 3 — cohort-based bulk campaigns.

A cohort send targets whoever matches a segment **at send time**, not a list
captured when the campaign was written. That distinction is the whole feature:
"every Monday, message whoever is currently At Risk" has to mean *currently*,
or a recurring campaign slowly turns into a stale mailing list — messaging
people who have since ordered and missing the ones who have since lapsed.

So the audience is re-evaluated on every occurrence, and the customers that
matched last time are never reused.
"""
from __future__ import annotations

import logging
from datetime import datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.automations.runtime import Candidate, RunReport, execute_candidates
from app.automations.templates import build_context, default_template, get_brand, render
from app.core.enums import AutomationKind, RecurrenceKind
from app.core.timezones import combine_local, to_local
from app.models.base import utcnow
from app.models.entities import Automation, Customer, Segment
from app.services.segments import evaluate_segment

logger = logging.getLogger(__name__)


def resolve_audience(
    db: Session, automation: Automation, *, now: datetime | None = None
) -> list[int]:
    """Customer ids matching this automation's audience right now.

    A manual list is honoured as given; a segment is re-run against live data,
    which is what makes a recurring cohort campaign track reality.
    """
    if automation.segment_id:
        segment = db.get(Segment, automation.segment_id)
        if segment is None:
            logger.warning(
                "Automation %s references segment %s, which no longer exists.",
                automation.id,
                automation.segment_id,
            )
            return []
        return [row["id"] for row in evaluate_segment(db, segment)]

    ids = [int(cid) for cid in (automation.manual_customer_ids or [])]
    if not ids:
        return []
    # Filter to ids that still exist, so a deleted customer is not carried
    # around in the config forever.
    return list(
        db.execute(select(Customer.id).where(Customer.id.in_(ids))).scalars().all()
    )


def segment_name(db: Session, automation: Automation) -> str | None:
    if not automation.segment_id:
        return None
    segment = db.get(Segment, automation.segment_id)
    return segment.name if segment else None


def template_for(db: Session, automation: Automation) -> str:
    """The copy this cohort send will use.

    Explicit copy wins; otherwise the segment's default tone is used, so a
    lapsed-repeat-buyer cohort reads as a reorder reminder and a one-time-buyer
    cohort reads as second-order encouragement without anyone configuring it.
    """
    if automation.message_template:
        return automation.message_template
    name = segment_name(db, automation)
    overrides = automation.template_overrides or {}
    if name and name in overrides:
        return overrides[name]
    return default_template(segment_name=name, objective=automation.objective)


def build_candidates(
    db: Session, automation: Automation, *, now: datetime | None = None
) -> list[Candidate]:
    now = now or utcnow()
    when = occurrence_time(automation, now=now)
    template = template_for(db, automation)
    brand = get_brand(db)
    name = segment_name(db, automation)

    customer_ids = resolve_audience(db, automation, now=now)
    if not customer_ids:
        return []

    customers = (
        db.execute(select(Customer).where(Customer.id.in_(customer_ids))).scalars().all()
    )
    candidates = []
    for customer in customers:
        body = render(template, build_context(customer, brand, now=now))
        candidates.append(
            Candidate(
                customer_id=customer.id,
                scheduled_for=when,
                body=body,
                context={
                    "source": "cohort",
                    "segment": name,
                    "matched_at": now.isoformat(),
                },
            )
        )
    return candidates


def occurrence_time(automation: Automation, *, now: datetime) -> datetime:
    """When this occurrence lands, in naive UTC.

    A cohort send goes out when its run fires. The automation's local send
    time is honoured by the *schedule* (see :func:`next_occurrence`), which is
    what the scheduler wakes on — so by the time a run starts the clock has
    already been respected, and a manual run should go now rather than being
    pushed to tomorrow's slot. Quiet hours are still enforced downstream.
    """
    return now


def parse_local_time(value: str | None, fallback: time = time(10, 0)) -> time:
    if not value:
        return fallback
    try:
        hour, _, minute = value.partition(":")
        return time(int(hour), int(minute or 0))
    except ValueError:
        return fallback


def next_occurrence(automation: Automation, *, after: datetime) -> datetime | None:
    """The next scheduled run, or None when the campaign is finished.

    Recurrence is expressed in local time so a weekly Monday send does not
    drift onto Sunday when the offset changes.
    """
    if automation.recurrence == RecurrenceKind.ONCE.value:
        return None

    at = parse_local_time(automation.send_time_local)
    local_after = to_local(after)
    day = local_after.date()

    if automation.recurrence == RecurrenceKind.DAILY.value:
        candidate = combine_local(day, at)
        if candidate <= after:
            candidate = combine_local(day + timedelta(days=1), at)
    elif automation.recurrence == RecurrenceKind.WEEKLY.value:
        target = automation.recurrence_day if automation.recurrence_day is not None else 0
        delta = (target - day.weekday()) % 7
        candidate = combine_local(day + timedelta(days=delta), at)
        if candidate <= after:
            candidate = combine_local(day + timedelta(days=delta + 7), at)
    elif automation.recurrence == RecurrenceKind.MONTHLY.value:
        target_day = automation.recurrence_day or 1
        candidate = combine_local(_clamp_day(day.year, day.month, target_day), at)
        if candidate <= after:
            year, month = (day.year + 1, 1) if day.month == 12 else (day.year, day.month + 1)
            candidate = combine_local(_clamp_day(year, month, target_day), at)
    else:
        return None

    if automation.ends_at and candidate > automation.ends_at:
        return None
    return candidate


def _clamp_day(year: int, month: int, day: int):
    """Month-end safe: a 31st schedule lands on the 30th in a 30-day month."""
    from calendar import monthrange
    from datetime import date

    return date(year, month, min(day, monthrange(year, month)[1]))


def run(
    db: Session,
    automation: Automation,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
) -> RunReport:
    """Execute one occurrence of a cohort campaign."""
    if automation.kind != AutomationKind.COHORT_BULK.value:
        raise ValueError(f"Automation {automation.id} is not a cohort campaign.")
    now = now or utcnow()
    candidates = build_candidates(db, automation, now=now)
    report = execute_candidates(db, automation, candidates, now=now, dry_run=dry_run)
    if not dry_run:
        automation.next_run_at = next_occurrence(automation, after=now)
        if automation.next_run_at is None:
            from app.core.enums import AutomationStatus

            automation.status = AutomationStatus.COMPLETED.value
        db.commit()
    return report
