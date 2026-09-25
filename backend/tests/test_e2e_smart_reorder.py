"""SAM-TEST-001 — Smart Reorder, end to end.

The scenario from the brief, run against the real automation pipeline rather
than against stubs: learn Sam's Wednesday-evening routine, dry-run it, send the
reminder once and only once, suppress it when he has already ordered, and
record the conversion and the prediction outcome when he orders afterwards.

The duplicate-suppression and already-ordered steps are the point. Those are
the two failures a customer would actually notice — the same reminder twice,
or "ready for your usual?" ten minutes after they ordered — and both are
asserted here against the pipeline that would really run.
"""
from __future__ import annotations

import time

from datetime import date, datetime, timedelta
from itertools import count

import pytest
from sqlalchemy import select

from app.analytics.order_predictions import predict_next_order, reminder_time_for
from app.automations.service import activate, approve, create_automation, run_automation
from app.automations import nudge
from app.core.enums import (
    AutomationKind,
    Channel,
    OrderStatus,
    PredictionStatus,
    SendStatus,
    SkipReason,
)
from app.core.timezones import to_local, to_utc_naive
from app.models.entities import (
    Automation,
    AutomationSend,
    Customer,
    Order,
    OrderPredictionRecord,
)
from app.services.intelligence import load_local_order_facts
from app.services.prediction_accuracy import (
    accuracy_report,
    record_prediction,
    resolve_predictions,
)

#: Five weekly Wednesday orders around 7:40 PM, in Sam's own local time.
SAM_ORDER_TIMES = ["19:32", "19:46", "19:39", "19:41", "19:35"]

#: Only the customer id is fixed by the brief; the automation name is not,
#: and this session does not roll back between tests.
#: Seeded from the clock rather than from 1. A counter that restarts every
#: process is unique within one run and collides on the next, which is
#: invisible on SQLite (a fresh file each time) and fails on a PostgreSQL test
#: database that keeps what the last run wrote.
_RUN = count(int(time.time() * 1000))

#: A Wednesday, far enough back that the fifth order is still in the past.
FIRST_ORDER_DATE = date(2026, 8, 5)


def _local(week: int, clock: str) -> datetime:
    hour, minute = (int(part) for part in clock.split(":"))
    day = FIRST_ORDER_DATE + timedelta(weeks=week)
    return datetime(day.year, day.month, day.day, hour, minute)


@pytest.fixture()
def sam(db) -> Customer:
    """SAM-TEST-001, with a Wednesday-evening habit and consent to be texted.

    The brief names this customer, so the identifier is fixed rather than
    generated — which means the fixture has to clear the previous run rather
    than rely on a rollback that this session does not do. Rebuilding from
    scratch each time is also the honest thing for an end-to-end test: it
    cannot pass on state some earlier test happened to leave behind.
    """
    previous = db.execute(
        select(Customer).where(Customer.external_id == "SAM-TEST-001")
    ).scalars().first()
    if previous is not None:
        db.delete(previous)
        db.commit()

    customer = Customer(
        external_id="SAM-TEST-001",
        email="sam-test-001@example.test",
        phone="+64210000777",
        first_name="Sam",
        last_name="Test",
        city="Auckland",
        date_of_birth=date(1990, 5, 1),
        age_verified=True,
        marketing_consent=True,
        sms_consent=True,
        email_consent=True,
        signup_date=to_utc_naive(_local(0, "19:32")) - timedelta(days=120),
    )
    db.add(customer)
    db.flush()

    for week, clock in enumerate(SAM_ORDER_TIMES):
        db.add(
            Order(
                external_id=f"SAM-ORD-{week}",
                customer_id=customer.id,
                # Stored as the naive UTC the database holds, exactly as a
                # real import would write it.
                ordered_at=to_utc_naive(_local(week, clock)),
                status=OrderStatus.COMPLETED.value,
                total_amount=68.50,
            )
        )
    db.commit()
    return customer


@pytest.fixture()
def smart_reorder(db, bootstrapped, sam) -> Automation:
    automation = create_automation(
        db,
        name=f"Smart Reorder Reminder (SAM-TEST-001) #{next(_RUN)}",
        kind=AutomationKind.NUDGE.value,
        channel=Channel.SMS.value,
        manual_customer_ids=[sam.id],
        message_template=(
            "Hi {first_name}, your usual {usual_day} is coming up — GIMME's "
            "ready when you are. Order at {website}. Reply STOP to opt out."
        ),
    )
    approve(db, automation, user_id=1)
    activate(db, automation, now=to_utc_naive(_local(4, "20:30")))
    return automation


def _sends(db, automation) -> list[AutomationSend]:
    return list(
        db.execute(
            select(AutomationSend)
            .where(AutomationSend.automation_id == automation.id)
            .order_by(AutomationSend.id)
        ).scalars().all()
    )


def _real_sends(db, automation) -> list[AutomationSend]:
    return [s for s in _sends(db, automation) if not s.is_dry_run]


# ==========================================================================
# 1. The routine is learned from the orders
# ==========================================================================
def test_step_1_sams_routine_is_wednesday_evening(db, sam):
    facts = load_local_order_facts(db, sam.id)
    last_local = _local(4, SAM_ORDER_TIMES[4])
    prediction = predict_next_order(facts, now=last_local + timedelta(hours=1))

    assert prediction.has_prediction
    assert prediction.preferred_weekday_name == "Wednesday"
    assert prediction.preferred_hour == 19
    assert 35 <= prediction.preferred_minute <= 43, prediction.preferred_time_label
    assert prediction.overall_confidence >= 70, prediction.overall_confidence
    assert prediction.band() == "HIGH"

    nxt = prediction.predicted_next_order_at
    assert nxt.weekday() == 2
    assert (nxt.date() - last_local.date()).days == 7

    # 30 minutes before, per the campaign default.
    assert reminder_time_for(prediction) == nxt - timedelta(minutes=30)


# ==========================================================================
# 2. Dry run — Sam is eligible, and nothing is sent
# ==========================================================================
def test_step_2_dry_run_finds_sam_eligible_and_sends_nothing(db, sam, smart_reorder):
    nudge.enroll(db, smart_reorder, now=to_utc_naive(_local(4, "20:30")))
    report = run_automation(db, smart_reorder, dry_run=True)

    assert report.dry_run is True
    assert sam.id in {r.customer_id for r in report.results}
    assert report.previewed >= 1, report.dry_run_summary()["lines"]
    assert report.sent == 0, "a dry run sent a real message"
    assert not _real_sends(db, smart_reorder)

    summary = report.dry_run_summary()
    assert summary["headline"] == "DRY RUN RESULTS"
    assert "No messages were actually sent." in summary["lines"]


# ==========================================================================
# 3 & 4. The reminder sends once, and only once
# ==========================================================================
def test_step_3_and_4_the_reminder_is_sent_once_and_not_repeated(db, sam, smart_reorder):
    """Running the scheduler twice must not text Sam twice.

    The scheduler runs every five minutes, so a reminder window is crossed by
    many runs. Idempotence here is not a nicety — without it Sam receives the
    same message a dozen times in an hour.
    """
    enroll_at = to_utc_naive(_local(4, "20:30"))
    nudge.enroll(db, smart_reorder, now=enroll_at)

    enrollment = nudge._active(db, smart_reorder)[0]
    assert enrollment.next_due_at is not None
    due = enrollment.next_due_at

    first = run_automation(db, smart_reorder, now=due)
    assert first.sent == 1, first.as_dict()["summary"]["lines"]
    after_first = len(_real_sends(db, smart_reorder))

    # The very next scheduler tick, five minutes later.
    second = run_automation(db, smart_reorder, now=due + timedelta(minutes=5))
    assert second.sent == 0, "Sam was messaged twice"
    assert len(_real_sends(db, smart_reorder)) == after_first


# ==========================================================================
# 5. Already ordered — the reminder is suppressed
# ==========================================================================
def test_step_5_no_reminder_once_sam_has_already_ordered(db, sam, smart_reorder):
    """"Ready for your usual?" must not arrive after he has ordered."""
    enroll_at = to_utc_naive(_local(4, "20:30"))
    nudge.enroll(db, smart_reorder, now=enroll_at)
    enrollment = nudge._active(db, smart_reorder)[0]
    due = enrollment.next_due_at

    # Sam orders before the reminder would have gone out.
    db.add(
        Order(
            external_id="SAM-ORD-EARLY",
            customer_id=sam.id,
            ordered_at=due - timedelta(minutes=20),
            status=OrderStatus.PENDING.value,
            total_amount=71.0,
        )
    )
    db.commit()

    report = run_automation(db, smart_reorder, now=due)

    assert report.sent == 0, "texted a customer who had just ordered"
    reasons = report.skips_by_reason()
    assert (
        SkipReason.PENDING_ORDER.value in reasons
        or SkipReason.ALREADY_ORDERED.value in reasons
    ), reasons


# ==========================================================================
# 6. Conversion, revenue and the prediction outcome
# ==========================================================================
def test_step_6_an_order_after_the_reminder_is_recorded_against_the_prediction(db, sam):
    """The loop closes: predicted, reminded, ordered, scored."""
    last_local = _local(4, SAM_ORDER_TIMES[4])
    prediction = predict_next_order(
        load_local_order_facts(db, sam.id), now=last_local + timedelta(hours=1)
    )
    predicted_utc = to_utc_naive(prediction.predicted_next_order_at)
    made_at = predicted_utc - timedelta(hours=1)

    record_prediction(
        db,
        customer_id=sam.id,
        predicted_order_at=predicted_utc,
        confidence=prediction.overall_confidence,
        reminder_sent=True,
        now=made_at,
    )
    db.flush()

    # Sam orders shortly after his usual moment.
    db.add(
        Order(
            external_id="SAM-ORD-CONVERTED",
            customer_id=sam.id,
            ordered_at=predicted_utc + timedelta(minutes=18),
            status=OrderStatus.COMPLETED.value,
            total_amount=74.25,
        )
    )
    db.flush()

    resolve_predictions(db, now=predicted_utc + timedelta(hours=2), commit=False)

    record = db.execute(
        select(OrderPredictionRecord).where(
            OrderPredictionRecord.customer_id == sam.id
        )
    ).scalars().one()

    assert record.status == PredictionStatus.ORDERED_NEAR_PREDICTION.value
    assert record.error_minutes == pytest.approx(18, abs=1)
    assert record.reminder_sent is True
    assert record.order_id is not None
    assert record.actual_order_at is not None

    report = accuracy_report(db)
    assert report.hits == 1
    assert report.accuracy_pct == 100.0
    assert report.within_30_minutes == 1


# ==========================================================================
# 7. The prediction is expressed in Sam's time, not the database's
# ==========================================================================
def test_the_reminder_lands_in_sams_evening_not_his_morning(db, sam, smart_reorder):
    """The whole feature is worthless if this is twelve hours out."""
    nudge.enroll(db, smart_reorder, now=to_utc_naive(_local(4, "20:30")))
    enrollment = nudge._active(db, smart_reorder)[0]

    assert enrollment.pattern["preferred_weekday_name"] == "Wednesday"
    assert enrollment.pattern["preferred_hour"] == 19, enrollment.pattern
    # To the minute, not to the hour: the reminder is aimed a set number of
    # minutes ahead of this, so an hour-resolution answer cannot support it.
    assert enrollment.pattern["preferred_time_label"] == "7:39 PM"

    local_due = to_local(enrollment.next_due_at)
    assert 9 <= local_due.hour <= 21, f"reminder scheduled for {local_due:%A %H:%M} local"
