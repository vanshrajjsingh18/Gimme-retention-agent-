"""Ordering patterns are read on the customer's clock, not the database's.

The rows are naive UTC and New Zealand runs twelve or thirteen hours ahead, so
reading a weekday or an hour straight off a stored timestamp answers for the
wrong moment. This is not a rounding concern: a 7:40 PM habit was being read as
7:40 AM, and an after-midnight order lands on the previous day in UTC, so the
weekday came out wrong too.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.analytics.order_predictions import predict_next_order
from app.core.timezones import to_utc_naive
from app.services.reorder_timing import plan_for_customer
from app.models.base import utcnow
from app.models.entities import Customer, Order
from app.services.intelligence import load_local_order_facts


def _sam_with_orders(db, local_times: list[datetime]) -> Customer:
    customer = Customer(
        external_id=f"TZ-{utcnow().timestamp()}",
        email=f"tz{utcnow().timestamp()}@example.test",
        first_name="Sam",
        last_name="Tz",
        age_verified=True,
        marketing_consent=True,
        sms_consent=True,
        signup_date=utcnow() - timedelta(days=365),
    )
    db.add(customer)
    db.flush()
    for i, local in enumerate(local_times):
        db.add(
            Order(
                external_id=f"TZORD-{customer.id}-{i}",
                customer_id=customer.id,
                # Stored the way a real import stores it: naive UTC.
                ordered_at=to_utc_naive(local),
                total_amount=55.0,
            )
        )
    db.flush()
    return customer


def test_an_evening_habit_is_not_read_as_a_morning_one(db):
    """Wednesday 7:40 PM NZ must not come back as 7:40 AM.

    Asserted through the planner the scheduler itself calls, because that is
    the layer that owns the conversion: the orders arrive as the naive UTC the
    database stores, and anything reading a weekday or an hour off those
    directly describes a habit nobody has.
    """
    local = [datetime(2026, 8, 5, 19, 40) + timedelta(weeks=w) for w in range(5)]
    customer = _sam_with_orders(db, local)

    routine = plan_for_customer(db, customer.id, now=utcnow()).prediction

    assert routine.has_prediction
    assert routine.preferred_weekday_name == "Wednesday"
    assert routine.preferred_hour == 19, (
        f"read as hour {routine.preferred_hour} — the UTC offset leaked in"
    )


def test_an_after_midnight_order_keeps_its_own_weekday(db):
    """Thursday 00:30 NZ is Wednesday 12:30 in UTC — a different day."""
    local = [datetime(2026, 8, 6, 0, 30) + timedelta(weeks=w) for w in range(5)]
    customer = _sam_with_orders(db, local)

    routine = plan_for_customer(db, customer.id, now=utcnow()).prediction

    assert routine.has_prediction
    assert routine.preferred_weekday_name == "Thursday", (
        f"landed on {routine.preferred_weekday_name} — the order was dated in UTC"
    )


def test_the_prediction_engine_agrees_once_facts_are_local(db):
    local = [datetime(2026, 8, 5, 19, 40) + timedelta(weeks=w) for w in range(5)]
    customer = _sam_with_orders(db, local)

    facts = load_local_order_facts(db, customer.id)
    prediction = predict_next_order(facts, now=local[-1] + timedelta(hours=1))

    assert prediction.preferred_weekday_name == "Wednesday"
    assert prediction.preferred_hour == 19
    assert prediction.preferred_time_label == "7:40 PM"


@pytest.mark.parametrize("local_dt", [
    datetime(2026, 1, 14, 19, 40),   # NZDT, UTC+13
    datetime(2026, 7, 15, 19, 40),   # NZST, UTC+12
])
def test_both_sides_of_daylight_saving_read_back_as_the_same_local_hour(db, local_dt):
    """The stored offset differs by an hour; the customer's clock does not.

    Handled by the zone database rather than by adding hours, so the summer
    and winter cases have to agree.
    """
    local = [local_dt + timedelta(weeks=w) for w in range(5)]
    customer = _sam_with_orders(db, local)

    facts = load_local_order_facts(db, customer.id)
    assert {f.ordered_at.hour for f in facts} == {19}
