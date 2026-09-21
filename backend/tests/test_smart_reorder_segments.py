"""The prediction is a stored fact, so it can be segmented and scheduled on.

Computed inside a function call it could only ever be shown one customer at a
time: a segment cannot filter on it, the scheduler cannot ask "whose window is
approaching?" without replaying every customer's order history, and "how many
people are due today?" has no cheap answer. The intelligence refresh writes it
down; these tests say it is written, kept current, and honest when there is
nothing to predict.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.enums import OrderStatus
from app.core.timezones import to_local, to_utc_naive
from app.models.base import utcnow
from app.models.entities import Customer, CustomerMetrics, Order, Segment
from app.services.intelligence import build_customer_view, refresh_customer
from app.services.segments import evaluate_segment

_SEQ = iter(range(1, 10_000))


def _customer_with_orders(db, local_times: list[datetime], **overrides) -> Customer:
    n = next(_SEQ)
    customer = Customer(
        external_id=f"PRED-{n}",
        email=f"pred{n}@example.test",
        phone=f"+6421000{n:04d}",
        first_name="Pat",
        last_name="Predict",
        age_verified=True,
        marketing_consent=True,
        sms_consent=True,
        signup_date=utcnow() - timedelta(days=365),
        **overrides,
    )
    db.add(customer)
    db.flush()
    for i, local in enumerate(local_times):
        db.add(
            Order(
                external_id=f"PREDORD-{n}-{i}",
                customer_id=customer.id,
                ordered_at=to_utc_naive(local),
                status=OrderStatus.COMPLETED.value,
                total_amount=60.0,
            )
        )
    db.commit()
    refresh_customer(db, customer)
    return customer


def _metrics(db, customer) -> CustomerMetrics:
    return db.execute(
        select(CustomerMetrics).where(CustomerMetrics.customer_id == customer.id)
    ).scalar_one()


def _weekly_evenings(weeks: int = 6, *, hour: int = 19, minute: int = 40) -> list[datetime]:
    """Weekly orders ending a few days ago, so the next one is still ahead."""
    last = to_local(utcnow()).replace(
        hour=hour, minute=minute, second=0, microsecond=0, tzinfo=None
    ) - timedelta(days=3)
    return [last - timedelta(weeks=w) for w in reversed(range(weeks))]


# ==========================================================================
# The prediction is stored
# ==========================================================================
def test_the_refresh_stores_when_they_are_next_likely_to_order(db, bootstrapped):
    customer = _customer_with_orders(db, _weekly_evenings())
    metrics = _metrics(db, customer)

    assert metrics.predicted_next_order_at is not None
    assert metrics.predicted_next_order_at > utcnow(), "predicted a moment already past"
    assert metrics.prediction_confidence >= 70
    assert to_local(metrics.predicted_next_order_at).hour == 19
    assert metrics.typical_order_minute == 40


def test_a_customer_without_a_routine_gets_no_prediction_rather_than_a_guess(db, bootstrapped):
    """Two orders is not a habit, and inventing one is worse than saying so."""
    customer = _customer_with_orders(db, _weekly_evenings(weeks=2))
    metrics = _metrics(db, customer)

    assert metrics.predicted_next_order_at is None
    assert metrics.prediction_confidence == 0


def test_the_prediction_advances_once_the_predicted_order_arrives(db, bootstrapped):
    """A stored prediction that never updates is a stale guess with an index on it.

    Once the customer does the thing that was predicted, the stored answer has
    to move on to the next cycle — otherwise the scheduler keeps aiming at a
    moment that has already happened.
    """
    customer = _customer_with_orders(db, _weekly_evenings())
    before = _metrics(db, customer).predicted_next_order_at

    db.add(
        Order(
            external_id=f"PREDORD-NEW-{customer.id}",
            customer_id=customer.id,
            ordered_at=before,
            status=OrderStatus.COMPLETED.value,
            total_amount=60.0,
        )
    )
    db.commit()
    refresh_customer(db, customer)

    after = _metrics(db, customer).predicted_next_order_at
    assert after > before, "still aiming at an order the customer has already placed"


def test_an_off_routine_order_does_not_move_a_weekly_customers_slot(db, bootstrapped):
    """One Tuesday order does not make a Wednesday customer a Tuesday one.

    The interval says the next order is about a week away and the weekday says
    which day that lands on; a single order off the routine should not drag
    the whole schedule to a new day, or a customer who orders once out of
    habit would be chased on the wrong evening for a month.
    """
    customer = _customer_with_orders(db, _weekly_evenings())
    before = _metrics(db, customer).predicted_next_order_at

    db.add(
        Order(
            external_id=f"PREDORD-ODD-{customer.id}",
            customer_id=customer.id,
            ordered_at=utcnow(),
            status=OrderStatus.COMPLETED.value,
            total_amount=60.0,
        )
    )
    db.commit()
    refresh_customer(db, customer)

    assert to_local(_metrics(db, customer).predicted_next_order_at).weekday() == (
        to_local(before).weekday()
    )


# ==========================================================================
# The flat view a segment rule reads
# ==========================================================================
def test_the_view_says_how_long_until_the_predicted_order(db, bootstrapped):
    customer = _customer_with_orders(db, _weekly_evenings())
    view = build_customer_view(customer, _metrics(db, customer), None, None, None)

    assert view["prediction_confidence"] >= 70
    assert view["hours_until_predicted_order"] is not None
    assert view["hours_until_predicted_order"] > 0
    assert isinstance(view["predicted_order_today"], bool)


def test_today_means_the_customers_local_day(db, bootstrapped):
    """UTC's day and a New Zealand evening are different days.

    Deciding "today" on the stored timestamp would put every customer whose
    routine falls after 12pm NZ into tomorrow, which is most of them.
    """
    local_today_evening = to_local(utcnow()).replace(
        hour=21, minute=0, second=0, microsecond=0, tzinfo=None
    )
    customer = _customer_with_orders(db, _weekly_evenings())
    metrics = _metrics(db, customer)
    metrics.predicted_next_order_at = to_utc_naive(local_today_evening)
    db.commit()

    view = build_customer_view(customer, metrics, None, None, None)
    assert view["predicted_order_today"] is True


# ==========================================================================
# The segments
# ==========================================================================
@pytest.mark.parametrize(
    "name",
    ["Smart Reorder Eligible", "Smart Reorder Today", "Smart Reorder Next 24 Hours"],
)
def test_the_smart_reorder_segments_exist(db, bootstrapped, name):
    assert db.execute(select(Segment).where(Segment.name == name)).scalar_one_or_none()


def test_a_customer_with_a_confident_routine_is_smart_reorder_eligible(db, bootstrapped):
    customer = _customer_with_orders(db, _weekly_evenings())
    segment = db.execute(
        select(Segment).where(Segment.name == "Smart Reorder Eligible")
    ).scalar_one()

    assert customer.id in {v["id"] for v in evaluate_segment(db, segment)}


def test_a_customer_with_too_little_history_is_not_eligible(db, bootstrapped):
    customer = _customer_with_orders(db, _weekly_evenings(weeks=2))
    segment = db.execute(
        select(Segment).where(Segment.name == "Smart Reorder Eligible")
    ).scalar_one()

    assert customer.id not in {v["id"] for v in evaluate_segment(db, segment)}


def test_withdrawing_consent_removes_them_from_the_eligible_segment(db, bootstrapped):
    """The segment is a view of who is reachable, not a record of who once was."""
    customer = _customer_with_orders(db, _weekly_evenings())
    segment = db.execute(
        select(Segment).where(Segment.name == "Smart Reorder Eligible")
    ).scalar_one()
    assert customer.id in {v["id"] for v in evaluate_segment(db, segment)}

    customer.marketing_consent = False
    db.commit()

    assert customer.id not in {v["id"] for v in evaluate_segment(db, segment)}


def test_the_next_24_hours_segment_only_holds_customers_due_within_a_day(db, bootstrapped):
    customer = _customer_with_orders(db, _weekly_evenings())
    metrics = _metrics(db, customer)
    segment = db.execute(
        select(Segment).where(Segment.name == "Smart Reorder Next 24 Hours")
    ).scalar_one()

    metrics.predicted_next_order_at = utcnow() + timedelta(hours=6)
    db.commit()
    assert customer.id in {v["id"] for v in evaluate_segment(db, segment)}

    metrics.predicted_next_order_at = utcnow() + timedelta(days=4)
    db.commit()
    assert customer.id not in {v["id"] for v in evaluate_segment(db, segment)}
