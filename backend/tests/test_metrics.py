from __future__ import annotations

from app.analytics.metrics import compute_metrics, estimate_ltv
from app.core.enums import OrderStatus
from tests.factories import NOW, cadence_history, order


def test_no_orders_returns_empty_metrics():
    m = compute_metrics([], now=NOW)
    assert m.total_orders == 0
    assert m.completed_orders == 0
    assert m.lifetime_revenue == 0.0
    assert m.last_order_at is None
    assert m.days_since_last_order is None
    assert m.estimated_ltv == 0.0


def test_basic_revenue_and_aov():
    orders = [order(10, 120.0), order(40, 80.0), order(70, 100.0)]
    m = compute_metrics(orders, now=NOW)
    assert m.completed_orders == 3
    assert m.lifetime_revenue == 300.0
    assert m.average_order_value == 100.0
    assert m.days_since_last_order == 10
    assert m.days_since_first_order == 70


def test_cancelled_orders_excluded_from_revenue_but_counted():
    orders = [
        order(10, 120.0),
        order(20, 500.0, status=OrderStatus.CANCELLED.value),
        order(30, 80.0, status=OrderStatus.REFUNDED.value),
    ]
    m = compute_metrics(orders, now=NOW)
    assert m.total_orders == 3
    assert m.completed_orders == 1
    assert m.cancelled_orders == 2
    assert m.lifetime_revenue == 120.0


def test_purchase_intervals():
    orders = cadence_history(count=5, interval_days=30, last_order_days_ago=5)
    m = compute_metrics(orders, now=NOW)
    assert m.average_purchase_interval_days == 30.0
    assert m.median_purchase_interval_days == 30.0
    assert m.days_since_last_order == 5


def test_median_interval_resists_one_outlier_gap():
    orders = [order(5), order(35), order(65), order(400)]
    m = compute_metrics(orders, now=NOW)
    # Gaps: 335, 30, 30 -> median 30, mean ~131
    assert m.median_purchase_interval_days == 30.0
    assert m.average_purchase_interval_days > 100


def test_windows_and_trends():
    # 3 orders in last 90 days, 1 in the previous 90.
    orders = [order(5, 100.0), order(30, 100.0), order(60, 100.0), order(120, 100.0)]
    m = compute_metrics(orders, now=NOW)
    assert m.orders_last_90d == 3
    assert m.revenue_last_90d == 300.0
    assert m.revenue_prev_90d == 100.0
    assert m.spend_trend == 1.0  # capped growth
    assert m.frequency_trend == 1.0


def test_declining_trend_is_negative():
    orders = [order(10, 50.0), order(100, 100.0), order(130, 100.0), order(160, 100.0)]
    m = compute_metrics(orders, now=NOW)
    assert m.revenue_last_90d == 50.0
    assert m.revenue_prev_90d == 300.0
    assert m.spend_trend < 0
    assert m.frequency_trend < 0


def test_discount_dependency_zero_without_discounts():
    m = compute_metrics([order(5, 100.0), order(35, 100.0)], now=NOW)
    assert m.discount_dependency == 0.0


def test_discount_dependency_high_when_every_order_discounted():
    orders = [order(5, 80.0, discount=20.0), order(35, 80.0, discount=20.0)]
    m = compute_metrics(orders, now=NOW)
    assert m.discount_dependency > 0.7


def test_preferences_ranked_by_quantity():
    orders = [
        order(5, category="Wine", brand="Villa Maria", product="Villa Maria Sauv Blanc", quantity=6),
        order(20, category="Beer", brand="Steinlager", product="Steinlager 12pk", quantity=1),
        order(40, category="Wine", brand="Villa Maria", product="Villa Maria Sauv Blanc", quantity=3),
    ]
    m = compute_metrics(orders, now=NOW)
    assert m.preferred_categories[0] == "Wine"
    assert m.preferred_brands[0] == "Villa Maria"
    assert m.top_products[0]["product_name"] == "Villa Maria Sauv Blanc"
    assert m.top_products[0]["quantity"] == 9
    assert m.total_units == 10


def test_typical_order_day_and_hour_recorded():
    m = compute_metrics(cadence_history(count=4, interval_days=7, last_order_days_ago=3), now=NOW)
    # All orders 7 days apart land on the same weekday and hour.
    assert m.typical_order_weekday is not None
    assert m.typical_order_hour is not None


def test_engagement_score_uses_message_interaction():
    orders = [order(5)]
    low = compute_metrics(orders, now=NOW, engagement={"messages_sent_90d": 10, "messages_opened_90d": 0})
    high = compute_metrics(
        orders,
        now=NOW,
        engagement={
            "messages_sent_90d": 10,
            "messages_opened_90d": 9,
            "messages_clicked_90d": 5,
            "messages_replied_90d": 2,
        },
    )
    assert high.engagement_score > low.engagement_score
    assert 0 <= low.engagement_score <= 100
    assert 0 <= high.engagement_score <= 100


def test_ltv_never_below_lifetime_revenue():
    orders = cadence_history(count=8, interval_days=30, last_order_days_ago=400, amount=100.0)
    m = compute_metrics(orders, now=NOW)
    assert m.estimated_ltv >= m.lifetime_revenue
    assert estimate_ltv(m) == m.estimated_ltv


def test_ltv_higher_for_on_cadence_customer():
    active = compute_metrics(
        cadence_history(count=6, interval_days=30, last_order_days_ago=10, amount=100.0), now=NOW
    )
    lapsed = compute_metrics(
        cadence_history(count=6, interval_days=30, last_order_days_ago=300, amount=100.0), now=NOW
    )
    assert active.estimated_ltv > lapsed.estimated_ltv
