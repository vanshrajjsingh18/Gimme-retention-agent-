from __future__ import annotations

from datetime import timedelta

from app.analytics.metrics import compute_metrics
from app.core.enums import LifecycleStage, OrderStatus
from app.services.lifecycle import (
    DEFAULT_THRESHOLDS,
    classify_lifecycle,
    detect_reactivation_from_history,
    expected_cycle_days,
)
from tests.factories import NOW, cadence_history, order


def stage_for(orders, *, signup_days_ago=None, had_lapse=None):
    m = compute_metrics(orders, now=NOW)
    signup = NOW - timedelta(days=signup_days_ago) if signup_days_ago is not None else None
    return classify_lifecycle(m, signup_date=signup, now=NOW, had_lapse=had_lapse)


def test_new_customer_no_orders_recent_signup():
    r = stage_for([], signup_days_ago=5)
    assert r.stage == LifecycleStage.NEW
    assert "has not ordered yet" in r.reason


def test_registered_long_ago_never_ordered_is_dormant():
    r = stage_for([], signup_days_ago=400)
    assert r.stage == LifecycleStage.DORMANT


def test_only_cancelled_orders_is_churned():
    r = stage_for(
        [order(10, 100.0, status=OrderStatus.CANCELLED.value)], signup_days_ago=400
    )
    assert r.stage == LifecycleStage.CHURNED


def test_new_customer_single_recent_order():
    r = stage_for([order(5, 90.0)], signup_days_ago=10)
    assert r.stage == LifecycleStage.NEW


def test_single_order_past_new_window_is_activating():
    r = stage_for([order(40, 90.0)], signup_days_ago=60)
    assert r.stage == LifecycleStage.ACTIVATING


def test_two_orders_is_activating():
    r = stage_for([order(5, 90.0), order(35, 90.0)], signup_days_ago=60)
    assert r.stage == LifecycleStage.ACTIVATING


def test_regular_customer():
    orders = cadence_history(count=5, interval_days=30, last_order_days_ago=10, amount=60.0)
    r = stage_for(orders, signup_days_ago=200)
    assert r.stage == LifecycleStage.REGULAR


def test_high_value_customer():
    orders = cadence_history(count=6, interval_days=30, last_order_days_ago=10, amount=150.0)
    r = stage_for(orders, signup_days_ago=250)
    assert r.stage == LifecycleStage.HIGH_VALUE


def test_vip_customer():
    orders = cadence_history(count=12, interval_days=25, last_order_days_ago=10, amount=180.0)
    r = stage_for(orders, signup_days_ago=400)
    assert r.stage == LifecycleStage.VIP


def test_high_value_needs_revenue_not_just_orders():
    # Many small orders: regular, not high value.
    orders = cadence_history(count=12, interval_days=25, last_order_days_ago=10, amount=20.0)
    r = stage_for(orders, signup_days_ago=400)
    assert r.stage == LifecycleStage.REGULAR


def test_at_risk_when_past_cycle():
    # 30-day cycle, last order 55 days ago -> 1.5x = 45 day cut, dormant cut is 120.
    orders = cadence_history(count=5, interval_days=30, last_order_days_ago=55, amount=60.0)
    r = stage_for(orders, signup_days_ago=300)
    assert r.stage == LifecycleStage.AT_RISK
    assert "overdue" in r.reason


def test_dormant_when_far_past_cycle():
    orders = cadence_history(count=5, interval_days=30, last_order_days_ago=150, amount=60.0)
    r = stage_for(orders, signup_days_ago=400)
    assert r.stage == LifecycleStage.DORMANT


def test_churned_when_beyond_churn_threshold():
    orders = cadence_history(count=5, interval_days=30, last_order_days_ago=300, amount=60.0)
    r = stage_for(orders, signup_days_ago=600)
    assert r.stage == LifecycleStage.CHURNED


def test_vip_who_vanished_is_churned_not_vip():
    orders = cadence_history(count=15, interval_days=20, last_order_days_ago=400, amount=200.0)
    r = stage_for(orders, signup_days_ago=900)
    assert r.stage == LifecycleStage.CHURNED


def test_reactivated_when_flagged():
    orders = [order(3, 90.0), order(300, 90.0), order(330, 90.0)]
    r = stage_for(orders, signup_days_ago=400, had_lapse=True)
    assert r.stage == LifecycleStage.REACTIVATED


def test_reactivation_inferred_from_metrics():
    # Long history, nothing in the previous 90-day window, ordered 3 days ago.
    orders = [order(3, 90.0), order(300, 90.0), order(330, 90.0)]
    r = stage_for(orders, signup_days_ago=400)
    assert r.stage == LifecycleStage.REACTIVATED


def test_personal_cadence_used_with_enough_history():
    m = compute_metrics(
        cadence_history(count=6, interval_days=14, last_order_days_ago=5), now=NOW
    )
    cycle, source = expected_cycle_days(m)
    assert source == "personal"
    assert cycle == 14.0


def test_global_cadence_for_single_order():
    m = compute_metrics([order(5)], now=NOW)
    cycle, source = expected_cycle_days(m)
    assert source == "global"
    assert cycle == DEFAULT_THRESHOLDS.default_cycle_days


def test_fast_cadence_customer_flagged_at_risk_sooner_than_slow_one():
    fast = cadence_history(count=6, interval_days=7, last_order_days_ago=20, amount=50.0)
    slow = cadence_history(count=6, interval_days=60, last_order_days_ago=20, amount=50.0)
    assert stage_for(fast, signup_days_ago=300).stage == LifecycleStage.AT_RISK
    assert stage_for(slow, signup_days_ago=500).stage in {
        LifecycleStage.REGULAR,
        LifecycleStage.HIGH_VALUE,
    }


def test_detect_reactivation_from_history():
    from datetime import datetime

    dates = [datetime(2024, 1, 1), datetime(2024, 2, 1), datetime(2024, 8, 1)]
    assert detect_reactivation_from_history(dates) is True
    assert detect_reactivation_from_history(dates[:2]) is False
    assert detect_reactivation_from_history([datetime(2024, 1, 1)]) is False


def test_every_stage_reachable():
    """Guards against a rule ordering change silently orphaning a stage."""
    seen = {
        stage_for([], signup_days_ago=5).stage,
        stage_for([order(40, 90.0)], signup_days_ago=60).stage,
        stage_for(
            cadence_history(count=5, interval_days=30, last_order_days_ago=10, amount=60.0),
            signup_days_ago=200,
        ).stage,
        stage_for(
            cadence_history(count=6, interval_days=30, last_order_days_ago=10, amount=150.0),
            signup_days_ago=250,
        ).stage,
        stage_for(
            cadence_history(count=12, interval_days=25, last_order_days_ago=10, amount=180.0),
            signup_days_ago=400,
        ).stage,
        stage_for(
            cadence_history(count=5, interval_days=30, last_order_days_ago=55, amount=60.0),
            signup_days_ago=300,
        ).stage,
        stage_for(
            cadence_history(count=5, interval_days=30, last_order_days_ago=150, amount=60.0),
            signup_days_ago=400,
        ).stage,
        stage_for(
            cadence_history(count=5, interval_days=30, last_order_days_ago=300, amount=60.0),
            signup_days_ago=600,
        ).stage,
        stage_for([order(3, 90.0), order(300, 90.0)], signup_days_ago=400, had_lapse=True).stage,
    }
    assert seen == set(LifecycleStage)
