"""Whether the engine's predictions actually came true.

The point of these is that the accuracy number must be capable of being bad.
A metric that cannot go down measures nothing, so several of these assert on
misses rather than hits.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.core.enums import OrderStatus, PredictionStatus
from app.models.base import utcnow
from app.models.entities import Customer, Order, OrderPredictionRecord
from app.services.prediction_accuracy import (
    accuracy_report,
    pending_count,
    record_prediction,
    resolve_predictions,
)


@pytest.fixture()
def customer(db):
    stamp = utcnow().timestamp()
    c = Customer(
        external_id=f"ACC-{stamp}",
        email=f"acc{stamp}@example.test",
        first_name="Acc",
        last_name="Test",
        age_verified=True,
        marketing_consent=True,
        signup_date=utcnow() - timedelta(days=200),
    )
    db.add(c)
    db.flush()
    return c


def place_order(db, customer, at, *, amount=70.0):
    o = Order(
        external_id=f"ACCORD-{customer.id}-{at.timestamp()}",
        customer_id=customer.id,
        ordered_at=at,
        status=OrderStatus.COMPLETED.value,
        total_amount=amount,
    )
    db.add(o)
    db.flush()
    return o


def test_an_order_near_the_prediction_is_a_hit(db, customer):
    now = utcnow()
    predicted = now + timedelta(hours=2)
    record_prediction(db, customer_id=customer.id, predicted_order_at=predicted, confidence=90, now=now)
    db.flush()

    place_order(db, customer, predicted + timedelta(minutes=20))
    resolve_predictions(db, now=predicted + timedelta(hours=1), commit=False)

    row = db.execute(
        __import__("sqlalchemy").select(OrderPredictionRecord).where(
            OrderPredictionRecord.customer_id == customer.id
        )
    ).scalars().one()
    assert row.status == PredictionStatus.ORDERED_NEAR_PREDICTION.value
    assert row.error_minutes == pytest.approx(20, abs=1)


def test_ordering_far_too_early_is_recorded_as_early_not_as_a_hit(db, customer):
    """The direction of the error is the actionable part."""
    now = utcnow()
    predicted = now + timedelta(days=1)
    record_prediction(db, customer_id=customer.id, predicted_order_at=predicted, confidence=80, now=now)
    db.flush()

    place_order(db, customer, predicted - timedelta(hours=10))
    resolve_predictions(db, now=predicted + timedelta(hours=1), commit=False)

    row = db.execute(
        __import__("sqlalchemy").select(OrderPredictionRecord).where(
            OrderPredictionRecord.customer_id == customer.id
        )
    ).scalars().one()
    assert row.status == PredictionStatus.ORDERED_EARLY.value
    assert row.error_minutes < 0, "the sign should say which way we were wrong"


def test_no_order_within_the_window_is_a_miss(db, customer):
    now = utcnow()
    predicted = now + timedelta(hours=1)
    record_prediction(db, customer_id=customer.id, predicted_order_at=predicted, confidence=95, now=now)
    db.flush()

    # Long past the resolution horizon, with no order at all.
    resolve_predictions(db, now=predicted + timedelta(days=5), commit=False)

    row = db.execute(
        __import__("sqlalchemy").select(OrderPredictionRecord).where(
            OrderPredictionRecord.customer_id == customer.id
        )
    ).scalars().one()
    assert row.status == PredictionStatus.NO_ORDER.value

    report = accuracy_report(db)
    assert report.hits == 0
    assert report.accuracy_pct == 0.0, "a metric that cannot go down measures nothing"


def test_an_order_placed_before_we_predicted_does_not_count_as_foresight(db, customer):
    """We cannot claim to have foreseen an order that had already happened."""
    now = utcnow()
    place_order(db, customer, now - timedelta(hours=3))

    predicted = now + timedelta(hours=1)
    record_prediction(db, customer_id=customer.id, predicted_order_at=predicted, confidence=90, now=now)
    db.flush()
    resolve_predictions(db, now=predicted + timedelta(days=5), commit=False)

    row = db.execute(
        __import__("sqlalchemy").select(OrderPredictionRecord).where(
            OrderPredictionRecord.customer_id == customer.id
        )
    ).scalars().one()
    assert row.status == PredictionStatus.NO_ORDER.value
    assert row.order_id is None


def test_repredicting_updates_in_place_rather_than_stacking(db, customer):
    """A five-minute scheduler must not manufacture its own sample size."""
    now = utcnow()
    for i in range(5):
        record_prediction(
            db,
            customer_id=customer.id,
            predicted_order_at=now + timedelta(hours=3),
            confidence=70 + i,
            now=now + timedelta(minutes=5 * i),
        )
        db.flush()

    assert pending_count(db) == 1
    rows = db.execute(
        __import__("sqlalchemy").select(OrderPredictionRecord).where(
            OrderPredictionRecord.customer_id == customer.id
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].confidence == 74, "the latest prediction should win"


def test_reminder_sent_latches_on(db, customer):
    now = utcnow()
    record_prediction(db, customer_id=customer.id, predicted_order_at=now + timedelta(hours=3),
                      confidence=80, reminder_sent=True, now=now)
    db.flush()
    record_prediction(db, customer_id=customer.id, predicted_order_at=now + timedelta(hours=4),
                      confidence=82, reminder_sent=False, now=now)
    db.flush()

    row = db.execute(
        __import__("sqlalchemy").select(OrderPredictionRecord).where(
            OrderPredictionRecord.customer_id == customer.id
        )
    ).scalars().one()
    assert row.reminder_sent is True, "a message that went out cannot be un-sent"


def test_accuracy_bands_are_cumulative(db, customer):
    """A 20-minute error is inside every band above it."""
    now = utcnow()
    predicted = now + timedelta(hours=2)
    record_prediction(db, customer_id=customer.id, predicted_order_at=predicted, confidence=90, now=now)
    db.flush()
    place_order(db, customer, predicted + timedelta(minutes=20))
    resolve_predictions(db, now=predicted + timedelta(hours=1), commit=False)

    report = accuracy_report(db)
    assert report.within_30_minutes == 1
    assert report.within_1_hour == 1
    assert report.within_3_hours == 1
    assert report.within_24_hours == 1
    assert report.accuracy_pct == 100.0
    assert report.median_error_minutes == pytest.approx(20, abs=1)


def test_report_separates_did_they_order_from_did_we_know_when(db, customer):
    now = utcnow()
    predicted = now + timedelta(hours=2)
    record_prediction(db, customer_id=customer.id, predicted_order_at=predicted, confidence=90, now=now)
    db.flush()
    # Ordered, but a long way off the predicted moment.
    place_order(db, customer, predicted + timedelta(hours=20))
    resolve_predictions(db, now=predicted + timedelta(days=4), commit=False)

    report = accuracy_report(db)
    assert report.accuracy_pct == 0.0, "20 hours out is not a hit"
    assert report.ordered_at_all_pct == 100.0, "but they did order"
