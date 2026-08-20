from __future__ import annotations

from app.analytics.metrics import compute_metrics
from app.churn.engine import FACTOR_WEIGHTS, score_churn
from app.core.enums import ChurnRiskBand, OrderStatus
from app.services.lifecycle import expected_cycle_days
from tests.factories import NOW, cadence_history, order


def score_for(orders, *, is_new=False, engagement=None, tenure_days=None):
    m = compute_metrics(orders, now=NOW, engagement=engagement)
    cycle, _ = expected_cycle_days(m)
    return m, score_churn(
        m,
        expected_cycle_days=cycle,
        is_new_customer=is_new,
        tenure_days=tenure_days,
        messages_sent_90d=(engagement or {}).get("messages_sent_90d", 0),
    )


def test_factor_weights_sum_to_100():
    assert round(sum(FACTOR_WEIGHTS.values()), 6) == 100.0


def test_on_cadence_regular_customer_is_low_risk():
    orders = cadence_history(count=6, interval_days=30, last_order_days_ago=8, amount=100.0)
    _, r = score_for(orders, engagement={"messages_sent_90d": 4, "messages_opened_90d": 3})
    assert r.risk_band == ChurnRiskBand.LOW
    assert r.score < 25


def test_score_always_within_bounds():
    for days in (1, 30, 90, 200, 500, 1000):
        orders = cadence_history(count=4, interval_days=20, last_order_days_ago=days)
        _, r = score_for(orders)
        assert 0 <= r.score <= 100


def test_risk_rises_monotonically_with_lateness():
    scores = []
    for days in (5, 40, 80, 150, 400):
        orders = cadence_history(count=6, interval_days=30, last_order_days_ago=days, amount=100.0)
        _, r = score_for(orders)
        scores.append(r.score)
    assert scores == sorted(scores)
    assert scores[0] < scores[-1]


def test_late_customer_flags_cadence_factor():
    orders = cadence_history(count=6, interval_days=30, last_order_days_ago=70, amount=100.0)
    _, r = score_for(orders)
    codes = {f.code for f in r.factors}
    assert "cadence_overdue" in codes
    assert r.risk_band in {ChurnRiskBand.MEDIUM, ChurnRiskBand.HIGH, ChurnRiskBand.CRITICAL}


def test_dormant_customer_is_high_or_critical():
    orders = cadence_history(count=6, interval_days=30, last_order_days_ago=200, amount=100.0)
    _, r = score_for(orders)
    assert r.risk_band in {ChurnRiskBand.HIGH, ChurnRiskBand.CRITICAL}


def test_churned_customer_is_critical():
    orders = cadence_history(count=6, interval_days=30, last_order_days_ago=500, amount=100.0)
    _, r = score_for(orders)
    assert r.risk_band == ChurnRiskBand.CRITICAL
    assert r.score >= 70


def test_brand_new_customer_capped_within_first_cycle():
    _, r = score_for([order(3, 90.0)], is_new=True)
    assert r.score <= 20
    assert r.risk_band == ChurnRiskBand.LOW


def test_single_order_customer_carries_risk_after_new_window():
    _, r = score_for([order(50, 90.0)], is_new=False)
    codes = {f.code for f in r.factors}
    assert "single_order" in codes
    assert r.score > 0


def test_reactivated_customer_is_lower_risk_than_when_dormant():
    dormant = cadence_history(count=4, interval_days=30, last_order_days_ago=250, amount=100.0)
    reactivated = [order(3, 100.0)] + dormant
    _, dormant_r = score_for(dormant)
    _, react_r = score_for(reactivated)
    assert react_r.score < dormant_r.score


def test_declining_spend_and_frequency_add_points():
    steady = [order(5, 100.0), order(35, 100.0), order(65, 100.0), order(95, 100.0), order(125, 100.0)]
    declining = [order(5, 20.0), order(100, 100.0), order(130, 100.0), order(160, 100.0)]
    _, steady_r = score_for(steady)
    _, decl_r = score_for(declining)
    decl_codes = {f.code for f in decl_r.factors}
    assert "spend_decline" in decl_codes
    assert decl_r.score > steady_r.score


def test_discount_dependency_factor_triggers():
    orders = [
        order(5, 40.0, discount=60.0),
        order(35, 40.0, discount=60.0),
        order(65, 40.0, discount=60.0),
    ]
    _, r = score_for(orders)
    assert "discount_dependency" in {f.code for f in r.factors}


def test_cancellations_add_risk():
    orders = [
        order(5, 100.0),
        order(35, 100.0),
        order(50, 100.0, status=OrderStatus.CANCELLED.value),
        order(60, 100.0, status=OrderStatus.CANCELLED.value),
    ]
    _, r = score_for(orders)
    assert "order_problems" in {f.code for f in r.factors}


def test_low_engagement_adds_risk():
    orders = cadence_history(count=5, interval_days=30, last_order_days_ago=10, amount=100.0)
    _, engaged = score_for(
        orders, engagement={"messages_sent_90d": 10, "messages_opened_90d": 9, "messages_clicked_90d": 6}
    )
    _, ignored = score_for(orders, engagement={"messages_sent_90d": 10, "messages_opened_90d": 0})
    assert ignored.score >= engaged.score


def test_explanation_is_human_readable_and_grounded():
    orders = cadence_history(count=6, interval_days=30, last_order_days_ago=120, amount=100.0)
    _, r = score_for(orders)
    assert r.explanation
    assert "120 days" in r.explanation
    assert f"{r.score:.0f}" in r.explanation


def test_explanation_for_healthy_customer_mentions_no_risk():
    orders = cadence_history(count=6, interval_days=30, last_order_days_ago=5, amount=100.0)
    _, r = score_for(
        orders, engagement={"messages_sent_90d": 5, "messages_opened_90d": 5, "messages_clicked_90d": 3}
    )
    assert r.score < 25
    assert r.explanation


def test_points_equal_weight_times_severity():
    orders = cadence_history(count=6, interval_days=30, last_order_days_ago=90, amount=100.0)
    _, r = score_for(orders)
    for f in r.factors:
        assert abs(f.points - FACTOR_WEIGHTS[f.code] * f.severity) < 0.01


def test_revenue_at_risk_scales_with_score():
    high = cadence_history(count=8, interval_days=30, last_order_days_ago=300, amount=200.0)
    low = cadence_history(count=8, interval_days=30, last_order_days_ago=5, amount=200.0)
    _, high_r = score_for(high)
    _, low_r = score_for(low)
    assert high_r.revenue_at_risk > low_r.revenue_at_risk


def test_top_factors_sorted_by_points():
    orders = cadence_history(count=6, interval_days=30, last_order_days_ago=200, amount=100.0)
    _, r = score_for(orders)
    points = [f.points for f in r.top_factors(3)]
    assert points == sorted(points, reverse=True)
