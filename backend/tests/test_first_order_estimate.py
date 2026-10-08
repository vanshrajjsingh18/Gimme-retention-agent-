"""Smart Reorder for customers who have ordered once (minimum completed orders = 1).

One order is not a routine, so the time is an estimate: the weekday and time
of that order, after the store's typical wait before a second order.
"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta, time as clock
from itertools import count


from app.analytics.order_predictions import predict_next_order
from app.automations.service import create_automation
from app.core.timezones import combine_local, local_date
from app.models.base import utcnow
from app.models.entities import Customer, Order, OrderItem
from app.services.reorder_timing import typical_first_reorder_days
from app.services.smart_reorder_queue import build_queue
from tests.factories import order

_RUN = count(int(time.time() * 1000))
NOW = datetime(2026, 10, 8, 12, 0)  # a Thursday


def test_one_order_gets_an_estimate_only_when_the_campaign_allows_it():
    first = order(10, now=NOW.replace(hour=19, minute=40))  # Monday 28 Sep, 7:40 PM

    assert predict_next_order([first], now=NOW, min_orders=3).has_prediction is False
    assert predict_next_order([first], now=NOW, min_orders=2).has_prediction is False

    estimate = predict_next_order([first], now=NOW, min_orders=1, first_reorder_days=14)
    assert estimate.has_prediction and estimate.is_estimate
    assert estimate.overall_confidence == 0
    assert estimate.predicted_next_order_at > NOW
    assert estimate.predicted_next_order_at.weekday() == first.ordered_at.weekday()
    assert (estimate.predicted_next_order_at.hour, estimate.predicted_next_order_at.minute) == (19, 40)
    assert "estimate" in estimate.reason


def test_old_single_order_rolls_forward_to_the_next_cycle():
    first = order(200, now=NOW.replace(hour=18, minute=5))
    estimate = predict_next_order([first], now=NOW, min_orders=1, first_reorder_days=14)
    assert NOW < estimate.predicted_next_order_at <= NOW + timedelta(days=14)


def test_two_orders_still_learn_rather_than_estimate():
    history = [order(30, now=NOW), order(16, now=NOW)]
    prediction = predict_next_order(history, now=NOW, min_orders=1, first_reorder_days=14)
    assert prediction.has_prediction and not prediction.is_estimate


def test_store_gap_is_measured_from_repeat_customers(db, seeded):
    days = typical_first_reorder_days(db)
    assert 2.0 <= days <= 90.0


def test_campaign_with_minimum_one_reaches_a_one_order_customer(db, seeded):
    now = utcnow()
    tag = next(_RUN)
    customer = Customer(
        external_id=f"ONE-ORDER-{tag}", first_name="Mere", last_name=f"Once{tag}", phone="+64211112233",
        city="Napier", date_of_birth=date(1990, 1, 1), age_verified=True,
        marketing_consent=True, sms_consent=True, signup_date=now - timedelta(days=30),
    )
    db.add(customer)
    db.flush()
    placed = Order(
        external_id=f"{customer.external_id}-1", customer_id=customer.id, status="COMPLETED", total_amount=40.0,
        ordered_at=combine_local(local_date(now) - timedelta(days=9), clock(18, 15)),
    )
    db.add(placed)
    db.flush()
    db.add(OrderItem(external_id=f"{placed.external_id}-1", order_id=placed.id, sku="X", product_name="Epic Pale Ale 6pk",
                     category="Beer", brand="Epic", quantity=1, unit_price=40.0, line_total=40.0))
    db.commit()

    def campaign(min_orders: int):
        return create_automation(
            db, name=f"One order {tag} min {min_orders}", kind="NUDGE",
            manual_customer_ids=[customer.id],
            message_template="Hi #first_name#, fancy another #product#? Reply STOP to opt out.",
            config={"min_orders": min_orders, "min_confidence": 70, "custom_offset_minutes": 30},
        )

    excluded = build_queue(db, campaign(2), now=now, dry_run=True, horizon_days=60)
    assert excluded.scheduled == 0

    included = build_queue(db, campaign(1), now=now, dry_run=True, horizon_days=60)
    assert included.scheduled == 1, included.excluded
    assert included.messages[0]["customer_id"] == customer.id
    db.rollback()
