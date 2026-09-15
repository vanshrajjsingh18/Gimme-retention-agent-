"""The ordering routine a customer's history actually implies.

Smart Reorder aims at a minute, so these pin the arithmetic that produces one:
the clock-wrap case a plain average gets backwards, the interval statistics,
and the rule that a prediction uses both *when* and *how often* rather than
whichever is easier.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.analytics.metrics import OrderFact
from app.analytics.order_predictions import (
    CONFIDENCE_HIGH,
    DEFAULT_REMINDER_OFFSET,
    MIN_ORDERS_FOR_PREDICTION,
    circular_time_of_day,
    interval_stats,
    predict_next_order,
    reminder_time_for,
)
from app.core.enums import OrderStatus


def order(at: datetime, *, amount: float = 60.0, status: str = OrderStatus.COMPLETED.value) -> OrderFact:
    return OrderFact(ordered_at=at, total_amount=amount, status=status)


def wednesdays(times: list[str], *, start: datetime = datetime(2026, 8, 5)) -> list[OrderFact]:
    """Weekly Wednesday orders at the given local clock times."""
    out = []
    for week, clock in enumerate(times):
        hour, minute = (int(p) for p in clock.split(":"))
        out.append(order((start + timedelta(weeks=week)).replace(hour=hour, minute=minute)))
    return out


# ==========================================================================
# Time of day, on a circle
# ==========================================================================
def test_clustered_evening_times_average_to_the_cluster():
    centre = circular_time_of_day([
        datetime(2026, 8, 5, 19, 32),
        datetime(2026, 8, 12, 19, 48),
        datetime(2026, 8, 19, 19, 41),
        datetime(2026, 8, 26, 19, 35),
    ])
    assert centre is not None
    assert centre.hour == 19
    assert 32 <= centre.minute <= 48
    assert centre.concentration > 0.99  # tight cluster
    assert centre.label().endswith("PM")


def test_times_either_side_of_midnight_do_not_average_to_midday():
    """The case a plain mean gets exactly backwards.

    23:50 and 00:10 are twenty minutes apart. Averaging the raw minute-of-day
    gives 12:00 — midday, when neither order happened, and the one answer that
    is maximally wrong. Late-evening ordering is this product's core
    behaviour, so this is not an edge case.
    """
    centre = circular_time_of_day([
        datetime(2026, 8, 5, 23, 50),
        datetime(2026, 8, 12, 0, 10),
    ])
    assert centre is not None
    assert centre.minute_of_day in (0, 1439, 1)  # midnight, give or take a minute
    assert centre.hour != 12, "fell back to the arithmetic mean"


def test_evenly_scattered_times_have_no_centre():
    """Six-hourly orders have no usual time; saying they do would be invention."""
    assert circular_time_of_day([
        datetime(2026, 8, 5, 0, 0),
        datetime(2026, 8, 5, 6, 0),
        datetime(2026, 8, 5, 12, 0),
        datetime(2026, 8, 5, 18, 0),
    ]) is None


def test_concentration_separates_tight_from_loose():
    tight = circular_time_of_day([datetime(2026, 8, 5, 19, 30), datetime(2026, 8, 12, 19, 34)])
    loose = circular_time_of_day([datetime(2026, 8, 5, 11, 0), datetime(2026, 8, 12, 22, 0)])
    assert tight is not None and loose is not None
    assert tight.concentration > loose.concentration


# ==========================================================================
# Intervals
# ==========================================================================
def test_interval_statistics():
    base = datetime(2026, 8, 5, 19, 0)
    stats = interval_stats([
        order(base),
        order(base + timedelta(days=7)),
        order(base + timedelta(days=15)),   # 8
        order(base + timedelta(days=21)),   # 6
    ])
    assert stats.count == 3
    assert stats.median_days == 7.0
    assert stats.min_days == 6.0
    assert stats.max_days == 8.0
    assert stats.mean_days == 7.0
    assert stats.stdev_days == 1.0


def test_regular_intervals_beat_erratic_ones_on_confidence():
    base = datetime(2026, 8, 5, 19, 0)
    regular = interval_stats([order(base + timedelta(days=7 * i)) for i in range(5)])
    erratic = interval_stats([
        order(base), order(base + timedelta(days=1)),
        order(base + timedelta(days=30)), order(base + timedelta(days=32)),
    ])
    assert regular.confidence > erratic.confidence
    assert regular.confidence > 0.9


def test_median_is_used_so_one_holiday_does_not_move_the_estimate():
    """The spec asks for the median precisely because of outliers."""
    base = datetime(2026, 8, 5, 19, 0)
    gaps = [7, 7, 7, 60, 7]  # one long break
    moments, cursor = [], base
    for g in gaps:
        moments.append(order(cursor))
        cursor += timedelta(days=g)
    moments.append(order(cursor))

    stats = interval_stats(moments)
    assert stats.median_days == 7.0
    assert stats.mean_days > 12, "sanity: the mean really is dragged"


# ==========================================================================
# The worked example from the brief
# ==========================================================================
def test_sam_wednesday_evening_routine():
    """Sam: five weekly Wednesday orders around 7:40 PM."""
    orders = wednesdays(["19:32", "19:46", "19:39", "19:41", "19:35"])
    now = orders[-1].ordered_at + timedelta(hours=1)

    p = predict_next_order(orders, now=now)

    assert p.has_prediction
    assert p.preferred_weekday_name == "Wednesday"
    assert p.preferred_hour == 19
    assert 35 <= p.preferred_minute <= 43, f"expected ~7:39 PM, got {p.preferred_time_label}"
    # Not exactly 7.0: the clock time drifts by a few minutes each week, so
    # consecutive orders are 7 days give or take a quarter of an hour.
    assert p.intervals is not None
    assert p.intervals.median_days == pytest.approx(7.0, abs=0.05)
    assert p.overall_confidence >= CONFIDENCE_HIGH, p.overall_confidence
    assert p.band() == "HIGH"

    nxt = p.predicted_next_order_at
    assert nxt is not None
    assert nxt.weekday() == 2, "predicted onto a Wednesday"
    assert (nxt.date() - orders[-1].ordered_at.date()).days == 7
    assert nxt.hour == 19


def test_reminder_lands_thirty_minutes_before_the_prediction():
    p = predict_next_order(
        wednesdays(["19:32", "19:46", "19:39", "19:41", "19:35"]),
        now=datetime(2026, 9, 2, 20, 0),
    )
    send_at = reminder_time_for(p, offset=DEFAULT_REMINDER_OFFSET)
    assert send_at == p.predicted_next_order_at - timedelta(minutes=30)

    assert reminder_time_for(p, offset="2_HOURS_BEFORE") == p.predicted_next_order_at - timedelta(hours=2)
    assert reminder_time_for(p, offset="AT_PREDICTED_TIME") == p.predicted_next_order_at
    assert reminder_time_for(p, custom_minutes=5) == p.predicted_next_order_at - timedelta(minutes=5)


# ==========================================================================
# Refusing to predict
# ==========================================================================
def test_two_orders_are_not_a_routine():
    p = predict_next_order(wednesdays(["19:30", "19:40"]), now=datetime(2026, 8, 20))
    assert p.has_prediction is False
    assert str(MIN_ORDERS_FOR_PREDICTION) in p.reason
    assert p.predicted_next_order_at is None


def test_cancelled_orders_do_not_count_towards_the_minimum():
    base = datetime(2026, 8, 5, 19, 30)
    orders = [
        order(base),
        order(base + timedelta(days=7)),
        order(base + timedelta(days=14), status=OrderStatus.CANCELLED.value),
    ]
    p = predict_next_order(orders, now=base + timedelta(days=20))
    assert p.has_prediction is False, "a cancelled order was treated as evidence of a habit"


def test_a_scattered_customer_scores_low_not_high():
    """No weekly routine, so the campaign threshold should exclude them."""
    base = datetime(2026, 8, 3, 12, 0)
    orders = [
        order(base),
        order(base + timedelta(days=2, hours=7)),
        order(base + timedelta(days=9, hours=-5)),
        order(base + timedelta(days=25, hours=3)),
        order(base + timedelta(days=27, hours=9)),
    ]
    p = predict_next_order(orders, now=base + timedelta(days=30))
    assert p.overall_confidence < CONFIDENCE_HIGH
    assert p.band() in ("LOW", "MEDIUM")


# ==========================================================================
# The prediction uses both signals
# ==========================================================================
def test_prediction_is_not_just_last_order_plus_seven_days():
    """A fortnightly customer must not be predicted weekly.

    The brief is explicit that adding a fixed week to the last order is the
    wrong answer; the interval has to carry through.
    """
    base = datetime(2026, 8, 5, 19, 30)  # Wednesday
    orders = [order(base + timedelta(days=14 * i)) for i in range(5)]
    p = predict_next_order(orders, now=orders[-1].ordered_at + timedelta(hours=1))

    assert p.intervals is not None and p.intervals.median_days == 14.0
    gap = (p.predicted_next_order_at - orders[-1].ordered_at).days
    assert gap == 14, f"used the weekday and dropped the interval: {gap} days"


def test_interval_wins_when_the_weekday_is_far_away():
    """A snap must refine the interval, not overrule it.

    A customer ordering every 30 days should not be dragged back a week to
    land on their modal weekday — at that distance the weekday is the weaker
    signal about when the next order is actually due.
    """
    base = datetime(2026, 8, 5, 19, 30)
    orders = [order(base + timedelta(days=30 * i)) for i in range(4)]
    p = predict_next_order(orders, now=orders[-1].ordered_at + timedelta(hours=1))

    gap = (p.predicted_next_order_at - orders[-1].ordered_at).days
    assert 27 <= gap <= 33, f"snapped too far from the interval: {gap} days"


def test_prediction_is_always_in_the_future():
    """A stale history must still yield a schedulable time, not a past one."""
    orders = wednesdays(["19:30", "19:35", "19:40", "19:38"])
    much_later = orders[-1].ordered_at + timedelta(days=90)
    p = predict_next_order(orders, now=much_later)

    assert p.predicted_next_order_at > much_later
    assert p.predicted_next_order_at.weekday() == 2, "drifted off the usual weekday"
    assert p.predicted_next_order_at.hour == 19


@pytest.mark.parametrize("weeks", [3, 5, 8])
def test_more_consistent_history_raises_confidence(weeks):
    p = predict_next_order(
        wednesdays(["19:35"] * weeks), now=datetime(2026, 12, 1)
    )
    assert p.has_prediction
    assert p.day_confidence == 100
    assert p.time_confidence == 100


@pytest.mark.parametrize(
    "days,expected",
    [
        (6.999, "7 days"),      # a weekly customer, whose clock time drifts
        (7.0, "7 days"),
        (1.0, "1 day"),
        (13.98, "14 days"),
        (10.4, "10.4 days"),    # genuinely not a round number
        (None, "an unknown interval"),
    ],
)
def test_interval_is_described_without_false_precision(days, expected):
    """"About every 6.999 days" reads as a machine talking."""
    from app.analytics.order_predictions import describe_interval

    assert describe_interval(days) == expected


def test_sam_reason_reads_naturally():
    p = predict_next_order(
        wednesdays(["19:32", "19:46", "19:39", "19:41", "19:35"]),
        now=datetime(2026, 9, 2, 20, 30),
    )
    assert "about every 7 days" in p.reason
    assert "6.99" not in p.reason
