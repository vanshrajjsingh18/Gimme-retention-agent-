"""Ordering-rhythm detection and offer eligibility (Feature 2's foundations)."""
from __future__ import annotations

from datetime import datetime, timedelta

from app.analytics.order_patterns import (
    DEFAULT_WINDOW_ORDERS,
    MIN_ORDERS_FOR_PATTERN,
    OrderPattern,
    bucket_for_hour,
    compute_order_pattern,
    decide_offer,
    next_nudge_time,
    should_recompute,
)
from app.analytics.metrics import OrderFact
from app.core.enums import OrderStatus

NOW = datetime(2026, 8, 20, 12, 0)


def order(at: datetime, *, status: str = OrderStatus.COMPLETED.value, total: float = 60.0) -> OrderFact:
    return OrderFact(ordered_at=at, total_amount=total, status=status)


def fridays(count: int, *, hour: int = 18, end: datetime | None = None) -> list[OrderFact]:
    """`count` orders on consecutive Fridays, most recent last."""
    end = end or datetime(2026, 8, 14, hour, 0)  # a Friday
    return [order(end - timedelta(weeks=i)) for i in reversed(range(count))]


# ==========================================================================
# Eligibility
# ==========================================================================
def test_no_pattern_below_minimum_order_count():
    result = compute_order_pattern(fridays(2), now=NOW)
    assert result.has_pattern is False
    assert result.orders_considered == 2
    assert str(MIN_ORDERS_FOR_PATTERN) in result.reason


def test_single_order_reason_reads_naturally():
    assert "1 completed order;" in compute_order_pattern(fridays(1), now=NOW).reason


def test_no_orders_at_all():
    result = compute_order_pattern([], now=NOW)
    assert result.has_pattern is False
    assert result.confidence == 0.0


def test_cancelled_orders_do_not_count_toward_the_minimum():
    orders = fridays(2) + [order(datetime(2026, 8, 7, 18, 0), status=OrderStatus.CANCELLED.value)]
    assert compute_order_pattern(orders, now=NOW).has_pattern is False


def test_minimum_orders_is_enough():
    result = compute_order_pattern(fridays(MIN_ORDERS_FOR_PATTERN), now=NOW)
    assert result.has_pattern is True


# ==========================================================================
# Day and time detection
# ==========================================================================
def test_consistent_friday_evening_buyer():
    result = compute_order_pattern(fridays(6), now=NOW)
    assert result.weekday == 4
    assert result.weekday_name == "Friday"
    assert result.weekday_confidence == 1.0
    assert result.time_bucket == "early_evening"
    assert result.typical_hour == 18
    assert result.confidence == 1.0


def test_median_interval_of_a_weekly_buyer_is_seven_days():
    assert compute_order_pattern(fridays(5), now=NOW).median_interval_days == 7.0


def test_mixed_days_lower_confidence_but_still_pick_the_mode():
    orders = fridays(3) + [
        order(datetime(2026, 8, 11, 18, 0)),  # Tuesday
        order(datetime(2026, 8, 5, 18, 0)),  # Wednesday
    ]
    result = compute_order_pattern(orders, now=NOW)
    assert result.weekday == 4
    assert 0.0 < result.weekday_confidence < 1.0
    assert result.confidence < 1.0


def test_window_is_bounded_to_recent_orders():
    """A customer who moved from Fridays to Sundays follows the change."""
    old = [order(datetime(2026, 1, 2, 18, 0) - timedelta(weeks=i)) for i in range(6)]  # Fridays
    recent = [order(datetime(2026, 8, 16, 18, 0) - timedelta(weeks=i)) for i in range(8)]  # Sundays
    result = compute_order_pattern(old + recent, now=NOW)
    assert result.orders_considered == DEFAULT_WINDOW_ORDERS
    assert result.weekday_name == "Sunday"


def test_typical_hour_comes_from_the_modal_bucket_not_the_whole_window():
    """A lunchtime outlier must not drag the evening hour down to mid-afternoon."""
    orders = [
        order(datetime(2026, 8, 14, 12, 0)),  # afternoon outlier
        order(datetime(2026, 8, 7, 19, 0)),
        order(datetime(2026, 7, 31, 18, 0)),
        order(datetime(2026, 7, 24, 19, 0)),
    ]
    result = compute_order_pattern(orders, now=NOW)
    assert result.time_bucket == "early_evening"
    assert result.typical_hour == 19


def test_bucket_boundaries():
    assert bucket_for_hour(0) == "overnight"
    assert bucket_for_hour(6) == "morning"
    assert bucket_for_hour(12) == "afternoon"
    assert bucket_for_hour(17) == "early_evening"
    assert bucket_for_hour(20) == "late_evening"
    assert bucket_for_hour(23) == "late_evening"


def test_result_serialises_for_storage():
    data = compute_order_pattern(fridays(4), now=NOW).as_dict()
    assert data["weekday_name"] == "Friday"
    assert isinstance(data["computed_at"], str)


# ==========================================================================
# Scheduling the nudge
# ==========================================================================
def test_next_nudge_lands_on_the_pattern_day_and_hour():
    pattern = compute_order_pattern(fridays(6), now=NOW)
    nudge = next_nudge_time(pattern, after=datetime(2026, 8, 17, 9, 0))  # Monday
    assert nudge.weekday() == 4
    assert nudge.hour == 18
    assert nudge == datetime(2026, 8, 21, 18, 0)


def test_nudge_rolls_to_next_week_when_the_hour_has_passed():
    pattern = compute_order_pattern(fridays(6), now=NOW)
    nudge = next_nudge_time(pattern, after=datetime(2026, 8, 21, 19, 0))  # Friday, late
    assert nudge == datetime(2026, 8, 28, 18, 0)


def test_lead_days_shifts_the_nudge_earlier():
    pattern = compute_order_pattern(fridays(6), now=NOW)
    nudge = next_nudge_time(pattern, after=datetime(2026, 8, 17, 9, 0), lead_days=1)
    assert nudge.weekday() == 3  # Thursday, ahead of their Friday order


def test_no_nudge_without_a_pattern():
    assert next_nudge_time(OrderPattern(), after=NOW) is None


# ==========================================================================
# Recompute schedule
# ==========================================================================
def test_missing_pattern_needs_recompute():
    assert should_recompute(None, now=NOW) is True
    assert should_recompute({}, now=NOW) is True
    assert should_recompute({"computed_at": None}, now=NOW) is True
    assert should_recompute({"computed_at": "not a date"}, now=NOW) is True


def test_fresh_pattern_is_left_alone_and_stale_one_is_not():
    fresh = {"computed_at": (NOW - timedelta(days=5)).isoformat()}
    stale = {"computed_at": (NOW - timedelta(days=31)).isoformat()}
    assert should_recompute(fresh, now=NOW) is False
    assert should_recompute(stale, now=NOW) is True


def test_is_stale_matches_should_recompute():
    pattern = compute_order_pattern(fridays(4), now=NOW - timedelta(days=40))
    assert pattern.is_stale(now=NOW) is True
    assert compute_order_pattern(fridays(4), now=NOW).is_stale(now=NOW) is False


# ==========================================================================
# Offer eligibility
# ==========================================================================
PROMOS = ["10% off your next order"]
CODES = ["GIMME10"]


def test_discount_withheld_from_a_full_price_buyer():
    decision = decide_offer(
        discount_dependency=0.1, verified_promotions=PROMOS, verified_coupon_codes=CODES
    )
    assert decision.include_discount is False
    assert "below" in decision.reason
    assert decision.promotion is None


def test_discount_offered_to_a_discount_responsive_buyer():
    decision = decide_offer(
        discount_dependency=0.8, verified_promotions=PROMOS, verified_coupon_codes=CODES
    )
    assert decision.include_discount is True
    assert decision.promotion == PROMOS[0]
    assert decision.coupon_code == "GIMME10"


def test_no_offer_is_invented_when_none_is_approved():
    """The second gate: responsive customer, but nothing approved to offer."""
    decision = decide_offer(
        discount_dependency=0.9, verified_promotions=[], verified_coupon_codes=[]
    )
    assert decision.include_discount is False
    assert "no approved promotion" in decision.reason.lower()


def test_promotion_without_a_coupon_code_is_still_usable():
    decision = decide_offer(
        discount_dependency=0.9, verified_promotions=PROMOS, verified_coupon_codes=[]
    )
    assert decision.include_discount is True
    assert decision.coupon_code is None


def test_threshold_is_inclusive():
    decision = decide_offer(
        discount_dependency=0.4, verified_promotions=PROMOS, verified_coupon_codes=CODES
    )
    assert decision.include_discount is True
