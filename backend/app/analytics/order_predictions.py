"""When a customer is next likely to order, to the minute.

``order_patterns`` answers "which day and roughly which part of the day?" — a
weekday plus a time bucket, which is enough to schedule a nudge to the hour.
Smart Reorder needs to arrive a set number of minutes before somebody orders,
and an hour-resolution answer cannot support that: aiming thirty minutes ahead
of "the early evening" is not aiming at anything.

So this module adds the two things the bucket model cannot give:

* a representative time of day **to the minute**, derived in a way that
  survives times either side of midnight;
* a prediction that combines *when* they order with *how often*, rather than
  using one and ignoring the other.

Both are pure functions over ``OrderFact``. Nothing here imports the ORM, and
nothing here decides whether a message may be sent — eligibility, consent and
suppression stay in the automation layer where they can be audited in one
place.

All datetimes in and out are **customer-local naive**. Converting to and from
UTC is the caller's job, because only the caller knows the customer's zone,
and doing it here would bury a timezone assumption in arithmetic.
"""
from __future__ import annotations

import math
import statistics
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

from app.analytics.metrics import WEEKDAY_NAMES, OrderFact
from app.core.enums import OrderStatus

MINUTES_PER_DAY = 24 * 60

#: Orders considered. Matches order_patterns: recent behaviour beats ancient
#: behaviour, and a customer who has moved from Friday to Sunday should be
#: followed rather than averaged across.
DEFAULT_WINDOW_ORDERS = 8

#: Fewer completed orders than this and there is no routine to learn. Named
#: separately from the automation's own threshold so the two can diverge
#: without one silently redefining the other.
MIN_ORDERS_FOR_PREDICTION = 3

#: Confidence bands, on the 0-100 scale the dashboard shows. Defaults only —
#: every caller takes them as arguments.
CONFIDENCE_HIGH = 70
CONFIDENCE_MEDIUM = 50

#: How far from the interval-implied date we will move to land on the
#: customer's usual weekday. Beyond this the weekday is no longer a refinement
#: of the interval, it is overriding it, and the interval is the stronger
#: signal for *when the next order is due*.
MAX_WEEKDAY_SNAP_DAYS = 3


def _minute_of_day(moment: datetime) -> int:
    return moment.hour * 60 + moment.minute


def describe_interval(days: float | None) -> str:
    """A gap in days, phrased the way somebody would say it out loud.

    Order times drift by a few minutes week to week, so a weekly customer's
    median gap is 6.999 days rather than 7. Printing that verbatim in the
    dashboard reads as false precision about a habit nobody keeps to the
    second, so near-whole numbers are said as whole numbers.
    """
    if days is None:
        return "an unknown interval"
    if abs(days - round(days)) < 0.05:
        whole = int(round(days))
        return "1 day" if whole == 1 else f"{whole} days"
    return f"{days:.1f} days"


@dataclass
class CircularTime:
    """A representative time of day, with how tightly the samples cluster."""

    minute_of_day: int
    #: 0-1 resultant length. 1.0 is every order at the same minute; near 0 is
    #: times scattered around the clock with no centre worth naming.
    concentration: float

    @property
    def hour(self) -> int:
        return self.minute_of_day // 60

    @property
    def minute(self) -> int:
        return self.minute_of_day % 60

    def label(self) -> str:
        """12-hour clock, as the dashboards and messages show it."""
        hour24, minute = self.hour, self.minute
        suffix = "AM" if hour24 < 12 else "PM"
        hour12 = hour24 % 12 or 12
        return f"{hour12}:{minute:02d} {suffix}"


def circular_time_of_day(moments: list[datetime]) -> CircularTime | None:
    """The centre of a set of clock times, treating the clock as a circle.

    A plain average is wrong here and wrong in the direction that matters.
    Orders at 23:50 and 00:10 are twenty minutes apart, but their arithmetic
    mean is 12:00 — midday, the one time of day neither customer was awake
    for. Late-evening ordering is exactly the behaviour this product is built
    around, so the naive version would fail on the most common case.

    Each time becomes a point on the unit circle; averaging the points and
    taking the angle back gives a centre that wraps correctly. The length of
    that mean vector falls out for free as a clustering measure, which is a
    more honest time-confidence than "share of orders in the same bucket":
    four orders spread across a three-hour bucket score the same as four in
    the same minute under bucketing, and very differently here.
    """
    if not moments:
        return None

    angles = [2 * math.pi * _minute_of_day(m) / MINUTES_PER_DAY for m in moments]
    mean_sin = sum(math.sin(a) for a in angles) / len(angles)
    mean_cos = sum(math.cos(a) for a in angles) / len(angles)

    concentration = math.hypot(mean_sin, mean_cos)
    if concentration < 1e-9:
        # Times are spread so evenly that the centre is undefined rather than
        # merely uncertain — any answer here would be an artefact of rounding.
        return None

    angle = math.atan2(mean_sin, mean_cos) % (2 * math.pi)
    minute = int(round(angle * MINUTES_PER_DAY / (2 * math.pi))) % MINUTES_PER_DAY
    return CircularTime(minute_of_day=minute, concentration=round(concentration, 4))


@dataclass
class IntervalStats:
    """How often a customer orders, and how regular they are about it."""

    count: int = 0
    median_days: float | None = None
    mean_days: float | None = None
    min_days: float | None = None
    max_days: float | None = None
    stdev_days: float | None = None
    #: 0-1. How predictable the spacing is, from the spread relative to the
    #: typical gap — a customer who orders every 7 days give or take a few
    #: hours is far more actionable than one who orders every 7 days on
    #: average but anywhere from 1 to 30.
    confidence: float = 0.0

    def as_dict(self) -> dict:
        return asdict(self)


def interval_stats(orders: list[OrderFact]) -> IntervalStats:
    """Gaps between consecutive completed orders, in days."""
    moments = sorted(o.ordered_at for o in orders)
    gaps = [
        (moments[i] - moments[i - 1]).total_seconds() / 86400.0
        for i in range(1, len(moments))
    ]
    if not gaps:
        return IntervalStats()

    median = statistics.median(gaps)
    stdev = statistics.stdev(gaps) if len(gaps) >= 2 else 0.0

    # Coefficient of variation, inverted. Scale-free on purpose: a two-day
    # swing means something different for a weekly customer than a monthly
    # one, and an absolute threshold would treat them the same.
    if median > 0:
        confidence = max(0.0, 1.0 - (stdev / median))
    else:
        confidence = 0.0

    return IntervalStats(
        count=len(gaps),
        median_days=round(median, 3),
        mean_days=round(statistics.fmean(gaps), 3),
        min_days=round(min(gaps), 3),
        max_days=round(max(gaps), 3),
        stdev_days=round(stdev, 3),
        confidence=round(min(1.0, confidence), 4),
    )


@dataclass
class OrderPrediction:
    """A customer's ordering routine and the next order it implies."""

    has_prediction: bool = False
    reason: str = ""

    preferred_weekday: int | None = None
    preferred_weekday_name: str | None = None
    preferred_time_label: str | None = None
    preferred_hour: int | None = None
    preferred_minute: int | None = None

    orders_considered: int = 0
    last_order_at: datetime | None = None
    predicted_next_order_at: datetime | None = None

    intervals: IntervalStats | None = None

    #: All 0-100, so the dashboard and the campaign threshold speak one scale.
    day_confidence: int = 0
    time_confidence: int = 0
    interval_confidence: int = 0
    overall_confidence: int = 0

    def band(
        self, *, high: int = CONFIDENCE_HIGH, medium: int = CONFIDENCE_MEDIUM
    ) -> str:
        if self.overall_confidence >= high:
            return "HIGH"
        if self.overall_confidence >= medium:
            return "MEDIUM"
        return "LOW"

    def as_dict(self) -> dict:
        data = asdict(self)
        for key in ("last_order_at", "predicted_next_order_at"):
            if data[key] is not None:
                data[key] = data[key].isoformat()
        return data


def _snap_to_weekday(base: datetime, weekday: int, *, limit: int = MAX_WEEKDAY_SNAP_DAYS) -> datetime:
    """Move ``base`` to the nearest date with ``weekday``, within ``limit`` days.

    The interval says roughly *when* the next order is due; the weekday says
    which day of the week it lands on. Snapping to the nearest matching day
    uses both, which is the point — adding the interval alone ignores a
    customer who only ever orders on Wednesdays, and jumping to "next
    Wednesday" alone ignores a customer who orders every three weeks.

    Past ``limit`` the snap would drag the date further than the interval's
    own uncertainty, so the interval wins and the date is left alone.
    """
    offset = (weekday - base.weekday()) % 7
    forward, backward = offset, offset - 7  # e.g. +2 days or -5 days
    nearest = forward if abs(forward) <= abs(backward) else backward
    if abs(nearest) > limit:
        return base
    return base + timedelta(days=nearest)


def predict_next_order(
    orders: list[OrderFact],
    *,
    now: datetime,
    window_orders: int = DEFAULT_WINDOW_ORDERS,
    min_orders: int = MIN_ORDERS_FOR_PREDICTION,
) -> OrderPrediction:
    """Learn a customer's routine and project the next order from it.

    ``orders`` and ``now`` are customer-local naive datetimes. Only completed
    orders count: a cancelled order says nothing about when somebody likes to
    buy, and counting it would drag both the weekday and the interval.
    """
    completed = sorted(
        (o for o in orders if o.status == OrderStatus.COMPLETED.value),
        key=lambda o: o.ordered_at,
    )

    if len(completed) < min_orders:
        noun = "order" if len(completed) == 1 else "orders"
        return OrderPrediction(
            has_prediction=False,
            reason=(
                f"Only {len(completed)} completed {noun}; at least {min_orders} are "
                "needed before an ordering routine is more than coincidence."
            ),
            orders_considered=len(completed),
            last_order_at=completed[-1].ordered_at if completed else None,
        )

    window = completed[-window_orders:]
    stats = interval_stats(window)
    last_order_at = window[-1].ordered_at

    weekday_counts = Counter(o.ordered_at.weekday() for o in window)
    weekday, weekday_hits = weekday_counts.most_common(1)[0]
    day_confidence = weekday_hits / len(window)

    # Time is taken from the orders on the customer's usual day only. Mixing in
    # the odd Saturday afternoon order would pull the centre away from the
    # weeknight time we are actually aiming at.
    on_preferred_day = [o.ordered_at for o in window if o.ordered_at.weekday() == weekday]
    centre = circular_time_of_day(on_preferred_day) or circular_time_of_day(
        [o.ordered_at for o in window]
    )

    if centre is None:
        return OrderPrediction(
            has_prediction=False,
            reason="Order times are spread evenly around the clock, so there is no usual time to aim at.",
            orders_considered=len(window),
            last_order_at=last_order_at,
            intervals=stats,
        )

    if stats.median_days is None or stats.median_days <= 0:
        return OrderPrediction(
            has_prediction=False,
            reason="Orders do not have a measurable gap between them, so the next one cannot be projected.",
            orders_considered=len(window),
            last_order_at=last_order_at,
            intervals=stats,
        )

    # How often, then which day, then what time — in that order, so each
    # signal refines the one before rather than replacing it.
    due = last_order_at + timedelta(days=stats.median_days)
    due = _snap_to_weekday(due, weekday)
    predicted = due.replace(
        hour=centre.hour, minute=centre.minute, second=0, microsecond=0
    )

    # A prediction already in the past is no use to a scheduler. Advance whole
    # cycles rather than jumping to "now plus something", which would invent a
    # routine the customer does not have.
    while predicted <= now:
        predicted = _snap_to_weekday(
            predicted + timedelta(days=stats.median_days), weekday
        ).replace(hour=centre.hour, minute=centre.minute, second=0, microsecond=0)

    day_pct = int(round(day_confidence * 100))
    time_pct = int(round(centre.concentration * 100))
    interval_pct = int(round(stats.confidence * 100))

    # Weighted towards the day: a weekday match is 1-in-7 by chance, while a
    # tight clock time is common even in customers with no weekly routine at
    # all, because most people order in the evening.
    overall = int(round(0.45 * day_pct + 0.25 * time_pct + 0.30 * interval_pct))

    return OrderPrediction(
        has_prediction=True,
        reason=(
            f"{weekday_hits} of the last {len(window)} orders fell on "
            f"{WEEKDAY_NAMES[weekday]}, clustered around {centre.label()}, "
            f"about every {describe_interval(stats.median_days)}."
        ),
        preferred_weekday=weekday,
        preferred_weekday_name=WEEKDAY_NAMES[weekday],
        preferred_time_label=centre.label(),
        preferred_hour=centre.hour,
        preferred_minute=centre.minute,
        orders_considered=len(window),
        last_order_at=last_order_at,
        predicted_next_order_at=predicted,
        intervals=stats,
        day_confidence=day_pct,
        time_confidence=time_pct,
        interval_confidence=interval_pct,
        overall_confidence=overall,
    )


#: Offsets the campaign UI offers, in minutes before the predicted order.
#: Negative values send after it, which is a legitimate choice for a customer
#: who tends to order late rather than early.
REMINDER_OFFSETS: dict[str, int] = {
    "15_MIN_BEFORE": 15,
    "30_MIN_BEFORE": 30,
    "60_MIN_BEFORE": 60,
    "2_HOURS_BEFORE": 120,
    "AT_PREDICTED_TIME": 0,
    "30_MIN_AFTER": -30,
}

DEFAULT_REMINDER_OFFSET = "30_MIN_BEFORE"


def reminder_time_for(
    prediction: OrderPrediction,
    *,
    offset: str = DEFAULT_REMINDER_OFFSET,
    custom_minutes: int | None = None,
) -> datetime | None:
    """When to send, given a prediction and how far ahead of it to aim.

    ``custom_minutes`` overrides the named offset, for the campaign builder's
    "Custom" option. Still customer-local and naive: the caller converts.
    """
    if not prediction.has_prediction or prediction.predicted_next_order_at is None:
        return None
    minutes = (
        custom_minutes
        if custom_minutes is not None
        else REMINDER_OFFSETS.get(offset, REMINDER_OFFSETS[DEFAULT_REMINDER_OFFSET])
    )
    return prediction.predicted_next_order_at - timedelta(minutes=minutes)
