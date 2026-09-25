"""Does Smart Reorder send when it says it will, and stay quiet when it should?

Two promises the product makes on screen, asserted against the pipeline that
actually sends:

* the Smart Reorder page says every send re-checks "whether they have already
  ordered";
* Customer 360 shows a predicted order time and the reminder time that follows
  from it, to the minute.

Both are claims about behaviour, so they belong in tests rather than in a
paragraph nobody can run.
"""
from __future__ import annotations

import time

from datetime import date, datetime, timedelta
from itertools import count

import pytest
from sqlalchemy import select

from app.analytics.order_predictions import predict_next_order, reminder_time_for
from app.automations import nudge
from app.automations.service import activate, approve, create_automation, run_automation
from app.core.enums import AutomationKind, Channel, OrderStatus, SkipReason
from app.core.timezones import to_local, to_utc_naive
from app.models.entities import Automation, Customer, Order
from app.services.intelligence import load_local_order_facts

#: Five weekly Wednesday orders around 7:40 PM, in the customer's local time.
ORDER_TIMES = ["19:32", "19:46", "19:39", "19:41", "19:35"]
FIRST_ORDER_DATE = date(2026, 8, 5)  # a Wednesday

#: Seeded from the clock rather than from 1. A counter that restarts every
#: process is unique within one run and collides on the next, which is
#: invisible on SQLite (a fresh file each time) and fails on a PostgreSQL test
#: database that keeps what the last run wrote.
_RUN = count(int(time.time() * 1000))


def _local(week: int, clock: str) -> datetime:
    hour, minute = (int(part) for part in clock.split(":"))
    day = FIRST_ORDER_DATE + timedelta(weeks=week)
    return datetime(day.year, day.month, day.day, hour, minute)


@pytest.fixture()
def sam(db) -> Customer:
    """A customer with an unmistakable Wednesday-evening routine."""
    previous = db.execute(
        select(Customer).where(Customer.external_id == "SAM-TIMING-001")
    ).scalars().first()
    if previous is not None:
        db.delete(previous)
        db.commit()

    customer = Customer(
        external_id="SAM-TIMING-001",
        email="sam-timing@example.test",
        phone="+64210000778",
        first_name="Sam",
        last_name="Timing",
        city="Auckland",
        date_of_birth=date(1990, 5, 1),
        age_verified=True,
        marketing_consent=True,
        sms_consent=True,
        signup_date=to_utc_naive(_local(0, "19:32")) - timedelta(days=120),
    )
    db.add(customer)
    db.flush()
    for week, clock in enumerate(ORDER_TIMES):
        db.add(
            Order(
                external_id=f"SAM-TIMING-ORD-{week}",
                customer_id=customer.id,
                ordered_at=to_utc_naive(_local(week, clock)),
                status=OrderStatus.COMPLETED.value,
                total_amount=68.50,
            )
        )
    db.commit()
    return customer


@pytest.fixture()
def campaign(db, bootstrapped, sam) -> Automation:
    automation = create_automation(
        db,
        name=f"Smart Reorder timing #{next(_RUN)}",
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


def _shown_reminder_utc(client, auth_headers, customer) -> datetime:
    """The reminder time Customer 360 puts on the screen."""
    payload = client.get(
        f"/api/v1/customers/{customer.id}/order-pattern", headers=auth_headers
    ).json()
    assert payload["has_prediction"], payload["reason"]
    return to_utc_naive(datetime.fromisoformat(payload["reminder_at"]))


# ==========================================================================
# Already ordered
# ==========================================================================
def test_a_completed_order_just_before_the_reminder_suppresses_it(db, sam, campaign):
    """The message this feature must never send.

    "Ready for your usual order?" twenty minutes after they ordered. A
    customer who has just bought does not need reminding to buy, and being
    reminded anyway says plainly that nothing was checked.

    A real order lands COMPLETED — an imported one always does. Checking only
    for PENDING catches an order still in flight and misses the finished one,
    which is the far more common case.
    """
    enroll_at = to_utc_naive(_local(4, "20:30"))
    nudge.enroll(db, campaign, now=enroll_at)
    enrollment = nudge._active(db, campaign)[0]
    due = enrollment.next_due_at

    db.add(
        Order(
            external_id="SAM-TIMING-ALREADY",
            customer_id=sam.id,
            ordered_at=due - timedelta(minutes=20),
            status=OrderStatus.COMPLETED.value,
            total_amount=71.0,
        )
    )
    db.commit()

    report = run_automation(db, campaign, now=due)

    assert report.sent == 0, "texted a customer who had already ordered"
    assert SkipReason.ALREADY_ORDERED.value in report.skips_by_reason()


def test_an_order_from_last_week_does_not_suppress_this_week(db, sam, campaign):
    """The check must look at the current cycle, not at any order ever placed.

    Suppressing on the previous order would mean a customer who orders every
    week is never reminded again after their first.
    """
    enroll_at = to_utc_naive(_local(4, "20:30"))
    nudge.enroll(db, campaign, now=enroll_at)
    enrollment = nudge._active(db, campaign)[0]
    due = enrollment.next_due_at

    # The fifth order in the fixture is already ~7 days before the reminder.
    report = run_automation(db, campaign, now=due)

    assert SkipReason.ALREADY_ORDERED.value not in report.skips_by_reason()


# ==========================================================================
# Sending when the dashboard says it will
# ==========================================================================
def test_the_reminder_is_scheduled_for_the_time_the_screen_shows(
    db, sam, campaign, client, auth_headers
):
    """One timing engine, or the screen is describing a system that does not exist.

    Customer 360 used to read the minute-level prediction and show a 7:09 PM
    reminder, while the sender scheduled from a three-hour bucket minus a
    hard-coded two hours and fired at 5:00 PM. Neither number was wrong on its
    own terms; they were answers from two different models, and nothing said
    so. The invariant is not "the reminder lands exactly 30 minutes before" —
    the send window can legitimately move it — it is that the number on the
    screen is the number the sender will use.
    """
    enroll_at = to_utc_naive(_local(4, "20:30"))
    nudge.enroll(db, campaign, now=enroll_at)
    enrollment = nudge._active(db, campaign)[0]

    # The two are computed at different moments — the enrollment from the
    # fixture's clock, the endpoint from the real one — so they land on
    # different weeks. What must match is the slot itself: same weekday, same
    # local clock time. That is exactly what differed before, by two hours.
    scheduled = to_local(enrollment.next_due_at)
    shown = to_local(_shown_reminder_utc(client, auth_headers, sam))

    assert (scheduled.weekday(), scheduled.hour, scheduled.minute) == (
        shown.weekday(),
        shown.hour,
        shown.minute,
    ), f"sender schedules {scheduled:%A %-I:%M %p} but the screen shows {shown:%A %-I:%M %p}"


def test_changing_when_to_send_moves_the_already_scheduled_reminders(db, sam, campaign):
    """A timing change has to reach the customers already enrolled.

    Routines are only recomputed once they go stale, so an operator moving
    "30 minutes before" to "2 hours before" changed nothing for up to thirty
    days: the setting was accepted, stored, and did nothing to the slots it
    was about. The offset each slot was planned with is stored beside it, so
    the mismatch is visible and the refresh picks it up.
    """
    enroll_at = to_utc_naive(_local(4, "20:30"))
    nudge.enroll(db, campaign, now=enroll_at)
    before = to_local(nudge._active(db, campaign)[0].next_due_at)

    campaign.config = {**(campaign.config or {}), "reminder_offset": "2_HOURS_BEFORE"}
    db.commit()
    nudge.refresh_patterns(db, campaign, now=enroll_at)

    after = to_local(nudge._active(db, campaign)[0].next_due_at)
    assert after < before, f"reminder stayed at {before:%H:%M} after the offset changed"
    # 7:39 PM less two hours is 5:39 PM, which the send window leaves alone.
    assert (after.hour, after.minute) == (17, 39), after


def test_the_dry_run_accounts_for_customers_who_never_became_candidates(
    db, bootstrapped, sam, campaign
):
    """A preview that quietly drops people explains nothing.

    Somebody with too few orders, or a routine too loose to time a message
    by, produces no candidate at all — so no skip is recorded, and the
    audience simply comes back smaller with nothing to account for the
    difference. That is the one question a dry run exists to answer.
    """
    from app.automations.service import run_automation

    # Cleared first, the way the `sam` fixture does: on SQLite the database is
    # new each run, on PostgreSQL it is the one the last run left behind.
    previous = db.execute(
        select(Customer).where(Customer.external_id == "SAM-TIMING-THIN")
    ).scalars().first()
    if previous is not None:
        db.delete(previous)
        db.commit()

    thin = Customer(
        external_id="SAM-TIMING-THIN",
        email="thin@example.test",
        phone="+64210000779",
        first_name="Thin",
        last_name="History",
        age_verified=True,
        marketing_consent=True,
        sms_consent=True,
        signup_date=to_utc_naive(_local(0, "19:32")),
    )
    db.add(thin)
    db.flush()
    db.add(
        Order(
            external_id="THIN-ORD-0",
            customer_id=thin.id,
            ordered_at=to_utc_naive(_local(0, "19:32")),
            status=OrderStatus.COMPLETED.value,
            total_amount=40.0,
        )
    )
    campaign.manual_customer_ids = [sam.id, thin.id]
    db.commit()

    report = run_automation(db, campaign, now=to_utc_naive(_local(5, "10:00")), dry_run=True)
    summary = report.dry_run_summary()

    assert report.not_enrolled.get("INSUFFICIENT_HISTORY") == 1
    assert summary["evaluated"] == 2, summary["lines"]
    assert any("too few orders" in line for line in summary["lines"]), summary["lines"]


def test_the_screen_says_when_the_send_window_moved_the_reminder(
    db, sam, client, auth_headers
):
    """A reminder moved off its aimed time is a different promise.

    Sam orders around 7:39 PM, so 30 minutes before is 7:09 PM — past the 7 PM
    close of the send window. Pulling it back to 6 PM is the right call, and
    the wrong thing to do quietly: "we text them half an hour before they
    usually order" and "we text them at 6" are not the same claim.
    """
    payload = client.get(
        f"/api/v1/customers/{sam.id}/order-pattern", headers=auth_headers
    ).json()

    assert payload["moved_for_send_window"] is True
    assert payload["aimed_at"] != payload["reminder_at"]
    # And the aim itself honours the configured offset from the prediction.
    aimed = datetime.fromisoformat(payload["aimed_at"])
    predicted = datetime.fromisoformat(payload["predicted_next_order_at"])
    assert (predicted - aimed).total_seconds() / 60 == 30
