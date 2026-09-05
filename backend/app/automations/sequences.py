"""Feature 1 — recurring campaign sequences.

A sequence is an ordered list of steps timed by **offset from the customer's
own enrollment**, not by calendar date. That is what makes a sequence reusable:
"Day 0, Day 7, Day 14" means seven days after *this* customer joined, so the
same sequence can run all year and every customer gets the same experience
regardless of when they entered it.

Two enrollment modes follow from that:

* ``ROLLING`` — the segment is re-evaluated on each run and newly matching
  customers start at Day 0 from the moment they join;
* ``FIXED_COHORT`` — the audience is locked at launch and nobody joins later.

A customer leaves the sequence when they opt out, when they place an order
(the goal is met — continuing to chase them would be an error), or when the
campaign's end date passes. A stopped enrollment never receives a later step,
which is checked at send time rather than trusted from a flag set earlier.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.automations.cohort import parse_local_time, resolve_audience, segment_name
from app.automations.runtime import Candidate, RunReport, execute_candidates
from app.automations.templates import build_context, default_template, get_brand, render
from app.core.enums import (
    AutomationKind,
    AutomationStatus,
    EnrollmentMode,
    EnrollmentStatus,
    OrderStatus,
    SequenceTrigger,
)
from app.core.timezones import combine_local, local_date, to_local
from app.models.base import utcnow
from app.models.entities import (
    Automation,
    AutomationEnrollment,
    AutomationStep,
    Customer,
    Order,
)

logger = logging.getLogger(__name__)

STOP_OPTED_OUT = "Customer opted out."
STOP_ORDERED = "Customer placed an order — sequence goal met."
STOP_ENDED = "Campaign end date passed."
STOP_LEFT_SEGMENT = "Customer no longer matches the audience."

#: How stale an already-due step may be and still be worth sending, for a
#: back-dated trigger. A Day 7 message whose moment passed yesterday is still
#: relevant; one from three weeks ago is not, and firing the whole backlog at
#: somebody who just joined would be worse than sending nothing.
DEFAULT_CATCH_UP_DAYS = 3


def resolve_trigger_at(
    automation: Automation, customer: Customer, *, now: datetime
) -> datetime | None:
    """When this customer's clock starts, per the sequence's trigger type.

    Returns None when the trigger has not happened for them — a signup-triggered
    sequence cannot enrol somebody with no signup date, and pretending it starts
    now would silently turn it into a different sequence.
    """
    trigger = automation.trigger_type or SequenceTrigger.SEGMENT_ENTRY.value

    if trigger == SequenceTrigger.SIGNUP.value:
        return customer.signup_date
    if trigger == SequenceTrigger.LAST_ORDER.value:
        return _last_completed_order_at(customer)
    # SEGMENT_ENTRY and MANUAL both start the clock when they join.
    return now


def _last_completed_order_at(customer: Customer) -> datetime | None:
    dates = [
        order.ordered_at
        for order in customer.orders
        if order.status == OrderStatus.COMPLETED.value
    ]
    return max(dates) if dates else None


# --------------------------------------------------------------------------
# Enrollment
# --------------------------------------------------------------------------
def enroll(
    db: Session,
    automation: Automation,
    *,
    now: datetime | None = None,
    commit: bool = True,
) -> dict:
    """Add newly matching customers to the sequence.

    Under ``FIXED_COHORT`` this only does anything on the first call: once the
    audience has been captured, later matches are ignored by design.
    """
    now = now or utcnow()
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

    if automation.enrollment_mode == EnrollmentMode.FIXED_COHORT.value and existing:
        return {"enrolled": 0, "already_enrolled": len(existing), "mode": "FIXED_COHORT"}

    # A manual sequence enrols nobody on its own; that is the whole point of
    # choosing it.
    if automation.trigger_type == SequenceTrigger.MANUAL.value:
        return {
            "enrolled": 0,
            "already_enrolled": len(existing),
            "mode": automation.enrollment_mode,
            "skipped_no_trigger": 0,
            "trigger": automation.trigger_type,
        }

    candidate_ids = [c for c in resolve_audience(db, automation, now=now) if c not in existing]
    customers = {
        c.id: c
        for c in db.execute(select(Customer).where(Customer.id.in_(candidate_ids)))
        .scalars()
        .all()
    } if candidate_ids else {}

    added = 0
    no_trigger = 0
    for customer_id in candidate_ids:
        customer = customers.get(customer_id)
        if customer is None:
            continue
        trigger_at = resolve_trigger_at(automation, customer, now=now)
        if trigger_at is None:
            # No signup date, or no completed order — the trigger this sequence
            # is defined by has not happened for them.
            no_trigger += 1
            continue
        db.add(
            AutomationEnrollment(
                automation_id=automation.id,
                customer_id=customer_id,
                status=EnrollmentStatus.ACTIVE.value,
                enrolled_at=now,
                # Step offsets count from here, which for a back-dated trigger
                # is not the same as when they joined.
                trigger_at=trigger_at,
                current_step=0,
            )
        )
        added += 1

    if commit:
        db.commit()
    return {
        "enrolled": added,
        "already_enrolled": len(existing),
        "mode": automation.enrollment_mode,
        "skipped_no_trigger": no_trigger,
        "trigger": automation.trigger_type or SequenceTrigger.SEGMENT_ENTRY.value,
    }


def active_enrollments(db: Session, automation: Automation) -> list[AutomationEnrollment]:
    """Enrolments the runner should act on. A paused customer is excluded."""
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


def stop_enrollment(
    enrollment: AutomationEnrollment, reason: str, *, at: datetime
) -> None:
    enrollment.status = EnrollmentStatus.STOPPED.value
    enrollment.stop_reason = reason
    enrollment.stopped_at = at


# --------------------------------------------------------------------------
# Stop conditions
# --------------------------------------------------------------------------
def apply_stop_conditions(
    db: Session,
    automation: Automation,
    enrollments: list[AutomationEnrollment],
    *,
    now: datetime,
) -> int:
    """Stop every enrollment that should no longer receive steps.

    Evaluated immediately before each run rather than on a schedule of its
    own, so a customer who ordered an hour ago does not get the next step.
    """
    if not enrollments:
        return 0

    stopped = 0
    ended = bool(automation.ends_at and now >= automation.ends_at)
    ordered_since = _ordered_since(
        db, [e.customer_id for e in enrollments], enrollments
    ) if automation.stop_on_order else {}
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

    for enrollment in enrollments:
        customer = customers.get(enrollment.customer_id)
        if customer is None:
            stop_enrollment(enrollment, "Customer record removed.", at=now)
            stopped += 1
        elif customer.is_suppressed or not customer.marketing_consent:
            stop_enrollment(enrollment, STOP_OPTED_OUT, at=now)
            stopped += 1
        elif ordered_since.get(enrollment.customer_id):
            stop_enrollment(enrollment, STOP_ORDERED, at=now)
            stopped += 1
        elif ended:
            stop_enrollment(enrollment, STOP_ENDED, at=now)
            stopped += 1

    return stopped


def _ordered_since(
    db: Session, customer_ids: list[int], enrollments: list[AutomationEnrollment]
) -> dict[int, bool]:
    """Which customers have placed an order since they enrolled.

    A cancelled order is not a goal met, so only real orders stop a sequence.
    """
    if not customer_ids:
        return {}
    enrolled_at = {e.customer_id: e.enrolled_at for e in enrollments}
    rows = db.execute(
        select(Order.customer_id, Order.ordered_at, Order.status).where(
            Order.customer_id.in_(customer_ids)
        )
    ).all()
    ordered: dict[int, bool] = {}
    for customer_id, ordered_at, status in rows:
        if status == OrderStatus.CANCELLED.value:
            continue
        since = enrolled_at.get(customer_id)
        if since is not None and ordered_at >= since:
            ordered[customer_id] = True
    return ordered


# --------------------------------------------------------------------------
# Step timing
# --------------------------------------------------------------------------
def steps_for(db: Session, automation: Automation) -> list[AutomationStep]:
    return list(
        db.execute(
            select(AutomationStep)
            .where(AutomationStep.automation_id == automation.id)
            .order_by(AutomationStep.position)
        )
        .scalars()
        .all()
    )


def step_due_at(
    automation: Automation, step: AutomationStep, enrollment: AutomationEnrollment
) -> datetime:
    """When a step is due for one customer, in naive UTC.

    The offset is applied to the enrollment's *local* date so that "Day 7 at
    10am" is 10am for the customer, not 10am UTC seven days later.
    """
    at = parse_local_time(step.send_time_local or automation.send_time_local)
    clock = enrollment.trigger_at or enrollment.enrolled_at
    day = local_date(clock) + timedelta(days=step.offset_days)
    return combine_local(day, at)


def due_steps(
    db: Session,
    automation: Automation,
    enrollments: list[AutomationEnrollment],
    *,
    now: datetime,
) -> list[tuple[AutomationEnrollment, AutomationStep, datetime]]:
    """The next unsent step for each enrollment, where it is now due.

    Only one step per customer per run: if a sequence was paused for a fortnight
    and three steps came due meanwhile, the customer gets the next one, not a
    burst of three. Late steps are still sent — the message is the point, and
    dropping it silently would be worse than sending it a day late.
    """
    steps = steps_for(db, automation)
    if not steps:
        return []

    catch_up_days = int(
        (automation.config or {}).get("catch_up_days", DEFAULT_CATCH_UP_DAYS)
    )
    due: list[tuple[AutomationEnrollment, AutomationStep, datetime]] = []
    for enrollment in enrollments:
        # A back-dated trigger (signup, last order) can leave early steps
        # already past on the day somebody joins. Skip over the ones that are
        # too stale to be worth sending rather than replaying the whole
        # backlog at them, but keep a short grace window so a step that came
        # due yesterday still lands.
        cutoff = enrollment.enrolled_at - timedelta(days=catch_up_days)
        while enrollment.current_step < len(steps):
            step = steps[enrollment.current_step]
            if step_due_at(automation, step, enrollment) >= cutoff:
                break
            enrollment.current_step += 1

        if enrollment.current_step >= len(steps):
            continue
        step = steps[enrollment.current_step]
        when = step_due_at(automation, step, enrollment)
        if when <= now:
            due.append((enrollment, step, now))
    return due


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
    enrollments = enrollments if enrollments is not None else active_enrollments(db, automation)
    brand = get_brand(db)
    name = segment_name(db, automation)

    pending = due_steps(db, automation, enrollments, now=now)
    if not pending:
        return [], {}

    customers = {
        c.id: c
        for c in db.execute(
            select(Customer).where(Customer.id.in_([e.customer_id for e, _, _ in pending]))
        )
        .scalars()
        .all()
    }

    candidates: list[Candidate] = []
    by_enrollment: dict[int, AutomationEnrollment] = {}
    for enrollment, step, when in pending:
        customer = customers.get(enrollment.customer_id)
        if customer is None:
            continue
        template = step.message_template or default_template(
            segment_name=name, objective=automation.objective
        )
        body = render(template, build_context(customer, brand, now=now))
        candidates.append(
            Candidate(
                customer_id=customer.id,
                scheduled_for=when,
                body=body,
                step_id=step.id,
                enrollment_id=enrollment.id,
                context={
                    "source": "sequence",
                    "step_position": step.position,
                    "step_name": step.name,
                    "offset_days": step.offset_days,
                    "enrolled_at": enrollment.enrolled_at.isoformat(),
                    "due_at_local": to_local(
                        step_due_at(automation, step, enrollment)
                    ).isoformat(),
                },
            )
        )
        by_enrollment[enrollment.id] = enrollment
    return candidates, by_enrollment


def run(
    db: Session,
    automation: Automation,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
) -> RunReport:
    """Advance a sequence by one step per eligible customer."""
    if automation.kind != AutomationKind.SEQUENCE.value:
        raise ValueError(f"Automation {automation.id} is not a sequence.")
    now = now or utcnow()

    if dry_run:
        # A preview of a sequence nobody has joined yet would otherwise be
        # empty and misleading, so enrollment is simulated in memory.
        enrollments = active_enrollments(db, automation) + _prospective_enrollments(
            db, automation, now=now
        )
    else:
        if automation.enrollment_mode == EnrollmentMode.ROLLING.value or not _has_enrollments(
            db, automation
        ):
            enroll(db, automation, now=now, commit=False)
            db.flush()
        enrollments = active_enrollments(db, automation)
    stopped = apply_stop_conditions(db, automation, enrollments, now=now)
    if not dry_run and stopped:
        db.commit()
    # Re-read: the stop pass above may have removed some of them.
    enrollments = [e for e in enrollments if e.status == EnrollmentStatus.ACTIVE.value]

    candidates, by_enrollment = build_candidates(
        db, automation, now=now, enrollments=enrollments
    )
    report = execute_candidates(db, automation, candidates, now=now, dry_run=dry_run)

    if not dry_run:
        _advance(db, report, by_enrollment, steps_total=len(steps_for(db, automation)), now=now)
        if automation.ends_at and now >= automation.ends_at:
            automation.status = AutomationStatus.COMPLETED.value
        elif (
            automation.enrollment_mode == EnrollmentMode.FIXED_COHORT.value
            and _has_enrollments(db, automation)
            and _all_enrollments_finished(db, automation)
        ):
            # Only a locked cohort can be "finished". A rolling sequence with
            # nobody currently enrolled is idle, not complete — tomorrow's
            # segment refresh may hand it a new customer.
            automation.status = AutomationStatus.COMPLETED.value
        db.commit()

    return report


def _prospective_enrollments(
    db: Session, automation: Automation, *, now: datetime
) -> list[AutomationEnrollment]:
    """Transient enrollments for customers who would join on a live run.

    Never added to the session — a dry run must not change state.
    """
    enrolled = set(
        db.execute(
            select(AutomationEnrollment.customer_id).where(
                AutomationEnrollment.automation_id == automation.id
            )
        )
        .scalars()
        .all()
    )
    if automation.enrollment_mode == EnrollmentMode.FIXED_COHORT.value and enrolled:
        return []
    return [
        AutomationEnrollment(
            automation_id=automation.id,
            customer_id=customer_id,
            status=EnrollmentStatus.ACTIVE.value,
            enrolled_at=now,
            current_step=0,
        )
        for customer_id in resolve_audience(db, automation, now=now)
        if customer_id not in enrolled
    ]


def _advance(
    db: Session,
    report: RunReport,
    by_enrollment: dict[int, AutomationEnrollment],
    *,
    steps_total: int,
    now: datetime,
) -> None:
    """Move a customer to the next step only when their message actually went.

    A skipped step is retried on the next run rather than being consumed —
    otherwise a quiet-hours deferral or a dedup loss would silently swallow a
    message the customer was meant to receive.
    """
    from app.core.enums import SendStatus

    sent_customers = {
        r.customer_id for r in report.results if r.status == SendStatus.SENT
    }
    for enrollment in by_enrollment.values():
        if enrollment.customer_id not in sent_customers:
            continue
        enrollment.current_step += 1
        enrollment.last_sent_at = now
        if enrollment.current_step >= steps_total:
            enrollment.status = EnrollmentStatus.COMPLETED.value
    db.flush()


def _has_enrollments(db: Session, automation: Automation) -> bool:
    return (
        db.execute(
            select(AutomationEnrollment.id)
            .where(AutomationEnrollment.automation_id == automation.id)
            .limit(1)
        ).first()
        is not None
    )


def _all_enrollments_finished(db: Session, automation: Automation) -> bool:
    return (
        db.execute(
            select(AutomationEnrollment.id)
            .where(
                AutomationEnrollment.automation_id == automation.id,
                AutomationEnrollment.status == EnrollmentStatus.ACTIVE.value,
            )
            .limit(1)
        ).first()
        is None
    )
