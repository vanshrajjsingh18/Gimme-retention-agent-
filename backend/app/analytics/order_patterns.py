"""Per-customer ordering rhythm, derived from real order history.

Feature 2 fires a nudge at the day and time a customer usually orders, so it
needs a defensible answer to "when do they usually order?". Two failure modes
matter more than precision:

* firing on noise — a customer with two orders has no pattern, and guessing
  one produces a message timed by coincidence;
* firing on a stale pattern — habits drift, so a pattern computed once and
  frozen slowly stops matching.

Both are handled here: a minimum order count gates eligibility, and every
result carries a confidence score plus the window it was computed over so a
caller can decide whether to act on it.

Pure functions over ``OrderFact`` — no ORM, so the rules are cheap to test.
"""
from __future__ import annotations

import statistics
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, time, timedelta

from app.analytics.metrics import WEEKDAY_NAMES, OrderFact
from app.core.enums import OrderStatus

#: Orders to consider. Recent behaviour beats ancient behaviour, so the window
#: is bounded — a customer who used to order Fridays and now orders Sundays
#: should follow the change rather than average across it.
DEFAULT_WINDOW_ORDERS = 8

#: Below this, there is no pattern worth acting on. Three orders is the point
#: at which a repeated weekday stops being coincidence: with two orders any
#: weekday match is 1-in-7 luck.
MIN_ORDERS_FOR_PATTERN = 3

#: A pattern older than this should be recomputed before it is trusted.
PATTERN_STALE_AFTER_DAYS = 30

#: Time-of-day buckets, chosen to match how drinks delivery actually behaves:
#: a long evening peak, and everything before noon collapsed together.
TIME_BUCKETS: list[tuple[str, int, int]] = [
    ("morning", 6, 12),
    ("afternoon", 12, 17),
    ("early_evening", 17, 20),
    ("late_evening", 20, 24),
    ("overnight", 0, 6),
]

#: Where inside a bucket to aim. Sending at the very start of the peak reaches
#: the customer while they are still deciding.
BUCKET_SEND_HOUR: dict[str, int] = {
    "morning": 10,
    "afternoon": 14,
    "early_evening": 17,
    "late_evening": 18,
    "overnight": 18,
}


@dataclass
class OrderPattern:
    """A customer's ordering rhythm, with the evidence behind it."""

    has_pattern: bool = False
    reason: str = ""

    weekday: int | None = None
    weekday_name: str | None = None
    #: 0-1: share of orders falling on the modal weekday.
    weekday_confidence: float = 0.0

    time_bucket: str | None = None
    typical_hour: int | None = None
    time_confidence: float = 0.0

    #: Days between orders, used to decide how often to nudge.
    median_interval_days: float | None = None
    orders_considered: int = 0
    window_start: datetime | None = None
    window_end: datetime | None = None
    computed_at: datetime | None = None

    #: Combined 0-1 confidence, for ranking and for a UI to show honestly.
    confidence: float = 0.0

    def as_dict(self) -> dict:
        data = asdict(self)
        for key in ("window_start", "window_end", "computed_at"):
            if data[key] is not None:
                data[key] = data[key].isoformat()
        return data

    def is_stale(self, *, now: datetime, max_age_days: int = PATTERN_STALE_AFTER_DAYS) -> bool:
        if self.computed_at is None:
            return True
        return (now - self.computed_at).days >= max_age_days


def bucket_for_hour(hour: int) -> str:
    for name, start, end in TIME_BUCKETS:
        if start <= hour < end:
            return name
    return "overnight"


def compute_order_pattern(
    orders: list[OrderFact],
    *,
    now: datetime,
    window_orders: int = DEFAULT_WINDOW_ORDERS,
    min_orders: int = MIN_ORDERS_FOR_PATTERN,
) -> OrderPattern:
    """Derive a customer's usual order day and time from their last N orders.

    Only completed orders count: a cancelled order says nothing about when
    somebody likes to buy.
    """
    completed = sorted(
        (o for o in orders if o.status == OrderStatus.COMPLETED.value),
        key=lambda o: o.ordered_at,
    )

    if len(completed) < min_orders:
        return OrderPattern(
            has_pattern=False,
            reason=(
                f"Only {len(completed)} completed "
                f"{'order' if len(completed) == 1 else 'orders'}; at least {min_orders} "
                "are needed before a day-and-time pattern is meaningful."
            ),
            orders_considered=len(completed),
            computed_at=now,
        )

    window = completed[-window_orders:]

    weekday_counts = Counter(o.ordered_at.weekday() for o in window)
    modal_weekday, weekday_hits = weekday_counts.most_common(1)[0]
    weekday_confidence = weekday_hits / len(window)

    bucket_counts = Counter(bucket_for_hour(o.ordered_at.hour) for o in window)
    modal_bucket, bucket_hits = bucket_counts.most_common(1)[0]
    time_confidence = bucket_hits / len(window)

    # The representative hour is the median of the orders actually in the
    # modal bucket, not of every order — averaging a lunchtime and a late
    # night order would land at neither.
    in_bucket = [o.ordered_at.hour for o in window if bucket_for_hour(o.ordered_at.hour) == modal_bucket]
    typical_hour = int(statistics.median(in_bucket)) if in_bucket else BUCKET_SEND_HOUR[modal_bucket]

    intervals = [
        (window[i].ordered_at - window[i - 1].ordered_at).total_seconds() / 86400.0
        for i in range(1, len(window))
    ]
    median_interval = round(statistics.median(intervals), 2) if intervals else None

    # A weekday match is 1-in-7 by chance and a bucket match roughly 1-in-5, so
    # weight the weekday signal higher — it is the harder one to hit by luck.
    confidence = round(0.6 * weekday_confidence + 0.4 * time_confidence, 3)

    return OrderPattern(
        has_pattern=True,
        reason=(
            f"{weekday_hits} of the last {len(window)} orders fell on "
            f"{WEEKDAY_NAMES[modal_weekday]}, {bucket_hits} in the {modal_bucket.replace('_', ' ')}."
        ),
        weekday=modal_weekday,
        weekday_name=WEEKDAY_NAMES[modal_weekday],
        weekday_confidence=round(weekday_confidence, 3),
        time_bucket=modal_bucket,
        typical_hour=typical_hour,
        time_confidence=round(time_confidence, 3),
        median_interval_days=median_interval,
        orders_considered=len(window),
        window_start=window[0].ordered_at,
        window_end=window[-1].ordered_at,
        computed_at=now,
        confidence=confidence,
    )


def next_nudge_time(
    pattern: OrderPattern,
    *,
    after: datetime,
    lead_days: int = 0,
) -> datetime | None:
    """The next local datetime matching the customer's pattern.

    Returns a *local* naive datetime; the caller converts to UTC and applies
    the send window. ``lead_days`` shifts the nudge earlier, to arrive before
    the customer would have ordered anyway rather than after.
    """
    if not pattern.has_pattern or pattern.weekday is None:
        return None

    hour = pattern.typical_hour if pattern.typical_hour is not None else 17
    target_weekday = (pattern.weekday - lead_days) % 7

    days_ahead = (target_weekday - after.weekday()) % 7
    candidate = (after + timedelta(days=days_ahead)).replace(
        hour=hour, minute=0, second=0, microsecond=0
    )
    # If today is the day but the hour has passed, go to next week.
    if candidate <= after:
        candidate += timedelta(days=7)
    return candidate


def should_recompute(pattern: dict | None, *, now: datetime, max_age_days: int = PATTERN_STALE_AFTER_DAYS) -> bool:
    """True when a stored pattern blob is missing or past its refresh age."""
    if not pattern:
        return True
    computed = pattern.get("computed_at")
    if not computed:
        return True
    try:
        parsed = datetime.fromisoformat(str(computed))
    except ValueError:
        return True
    return (now - parsed).days >= max_age_days


# --------------------------------------------------------------------------
# Offer eligibility
# --------------------------------------------------------------------------
#: Above this share of discounted spend, a customer is treated as
#: discount-responsive and a nudge may carry an offer.
DISCOUNT_RESPONSIVE_THRESHOLD = 0.4


@dataclass
class OfferDecision:
    """Whether a nudge should carry a discount, and why."""

    include_discount: bool
    reason: str
    discount_dependency: float = 0.0
    promotion: str | None = None
    coupon_code: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def decide_offer(
    *,
    discount_dependency: float,
    verified_promotions: list[str],
    verified_coupon_codes: list[str],
    threshold: float = DISCOUNT_RESPONSIVE_THRESHOLD,
) -> OfferDecision:
    """Include a discount only where the customer's history justifies it.

    Two independent gates, and both must pass:

    * the customer has historically responded to discounting, so the offer is
      not spent on someone who would have bought anyway;
    * an approved promotion exists in brand settings — the system will not
      invent one, and with no approved promotion the nudge simply carries no
      offer rather than fabricating a discount.
    """
    if discount_dependency < threshold:
        return OfferDecision(
            include_discount=False,
            reason=(
                f"Discount dependency {discount_dependency:.0%} is below the "
                f"{threshold:.0%} threshold — this customer buys without an incentive."
            ),
            discount_dependency=discount_dependency,
        )

    if not verified_promotions:
        return OfferDecision(
            include_discount=False,
            reason=(
                "Customer is discount-responsive, but no approved promotion is "
                "configured in Brand settings, so no offer can be made."
            ),
            discount_dependency=discount_dependency,
        )

    return OfferDecision(
        include_discount=True,
        reason=(
            f"Discount dependency {discount_dependency:.0%} is at or above the "
            f"{threshold:.0%} threshold; using an approved promotion."
        ),
        discount_dependency=discount_dependency,
        promotion=verified_promotions[0],
        coupon_code=verified_coupon_codes[0] if verified_coupon_codes else None,
    )
