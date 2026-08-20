"""Customer behavioural metric computation.

Pure functions operate on plain order dicts so they can be unit-tested without
a database, and a thin persistence layer writes the results to
``customer_metrics``.
"""
from __future__ import annotations

import statistics
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

from app.core.enums import OrderStatus
from app.models.base import utcnow

WEEKDAY_NAMES = [
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
]


@dataclass
class OrderFact:
    """Minimal order projection used by the metric calculators."""

    ordered_at: datetime
    total_amount: float
    discount_amount: float = 0.0
    status: str = OrderStatus.COMPLETED.value
    items: list[dict] = field(default_factory=list)


@dataclass
class MetricResult:
    total_orders: int = 0
    completed_orders: int = 0
    cancelled_orders: int = 0
    lifetime_revenue: float = 0.0
    average_order_value: float = 0.0
    total_units: int = 0
    first_order_at: datetime | None = None
    last_order_at: datetime | None = None
    days_since_last_order: int | None = None
    days_since_first_order: int | None = None
    average_purchase_interval_days: float | None = None
    median_purchase_interval_days: float | None = None
    purchase_frequency_per_month: float = 0.0
    discount_dependency: float = 0.0
    orders_last_30d: int = 0
    orders_last_90d: int = 0
    orders_prev_90d: int = 0
    orders_last_365d: int = 0
    revenue_last_90d: float = 0.0
    revenue_prev_90d: float = 0.0
    spend_trend: float = 0.0
    frequency_trend: float = 0.0
    preferred_categories: list = field(default_factory=list)
    preferred_brands: list = field(default_factory=list)
    top_products: list = field(default_factory=list)
    typical_order_weekday: str | None = None
    typical_order_hour: int | None = None
    estimated_ltv: float = 0.0
    engagement_score: float = 0.0

    def as_dict(self) -> dict:
        return asdict(self)


def _round(value: float, digits: int = 2) -> float:
    return float(round(value, digits))


def compute_metrics(
    orders: list[OrderFact],
    *,
    now: datetime | None = None,
    engagement: dict | None = None,
) -> MetricResult:
    """Compute behavioural metrics from a customer's full order history.

    Only COMPLETED orders count toward revenue and cadence; cancelled and
    refunded orders are counted separately so churn scoring can use them.
    """
    now = now or utcnow()
    result = MetricResult()
    result.total_orders = len(orders)

    completed = sorted(
        [o for o in orders if o.status == OrderStatus.COMPLETED.value],
        key=lambda o: o.ordered_at,
    )
    result.cancelled_orders = sum(
        1 for o in orders if o.status in (OrderStatus.CANCELLED.value, OrderStatus.REFUNDED.value)
    )
    result.completed_orders = len(completed)

    engagement = engagement or {}

    if not completed:
        result.engagement_score = _engagement_score(engagement, orders_last_90d=0)
        return result

    result.lifetime_revenue = _round(sum(o.total_amount for o in completed))
    result.average_order_value = _round(result.lifetime_revenue / len(completed))
    result.first_order_at = completed[0].ordered_at
    result.last_order_at = completed[-1].ordered_at
    result.days_since_last_order = max((now - completed[-1].ordered_at).days, 0)
    result.days_since_first_order = max((now - completed[0].ordered_at).days, 0)

    # Purchase cadence
    if len(completed) >= 2:
        gaps = [
            (completed[i].ordered_at - completed[i - 1].ordered_at).total_seconds() / 86400.0
            for i in range(1, len(completed))
        ]
        gaps = [g for g in gaps if g >= 0]
        if gaps:
            result.average_purchase_interval_days = _round(sum(gaps) / len(gaps))
            result.median_purchase_interval_days = _round(statistics.median(gaps))

    tenure_days = max((now - completed[0].ordered_at).days, 1)
    result.purchase_frequency_per_month = _round(len(completed) / (tenure_days / 30.44), 3)

    # Windows
    def _window(days: int, offset_days: int = 0) -> list[OrderFact]:
        end = now - timedelta(days=offset_days)
        start = end - timedelta(days=days)
        return [o for o in completed if start < o.ordered_at <= end]

    result.orders_last_30d = len(_window(30))
    last90 = _window(90)
    prev90 = _window(90, offset_days=90)
    result.orders_last_90d = len(last90)
    result.orders_prev_90d = len(prev90)
    result.orders_last_365d = len(_window(365))
    result.revenue_last_90d = _round(sum(o.total_amount for o in last90))
    result.revenue_prev_90d = _round(sum(o.total_amount for o in prev90))

    # Trends: -1.0 (total decline) .. +1.0 (strong growth); 0 when flat/no base.
    result.spend_trend = _trend(result.revenue_last_90d, result.revenue_prev_90d)
    result.frequency_trend = _trend(float(result.orders_last_90d), float(len(prev90)))

    # Discount dependency = share of orders that used a discount, weighted by depth.
    discounted = [o for o in completed if o.discount_amount > 0]
    if completed:
        share = len(discounted) / len(completed)
        gross = sum(o.total_amount + o.discount_amount for o in completed)
        depth = (sum(o.discount_amount for o in completed) / gross) if gross > 0 else 0.0
        result.discount_dependency = _round(min(1.0, 0.7 * share + 0.3 * min(depth * 3, 1.0)), 3)

    # Product preferences
    category_counter: Counter[str] = Counter()
    brand_counter: Counter[str] = Counter()
    product_counter: Counter[str] = Counter()
    units = 0
    for order in completed:
        for item in order.items:
            qty = int(item.get("quantity", 1) or 1)
            units += qty
            if item.get("category"):
                category_counter[item["category"]] += qty
            if item.get("brand"):
                brand_counter[item["brand"]] += qty
            if item.get("product_name"):
                product_counter[item["product_name"]] += qty
    result.total_units = units
    result.preferred_categories = [c for c, _ in category_counter.most_common(3)]
    result.preferred_brands = [b for b, _ in brand_counter.most_common(3)]
    result.top_products = [
        {"product_name": name, "quantity": qty} for name, qty in product_counter.most_common(5)
    ]

    # Ordering habits
    weekday_counter = Counter(o.ordered_at.weekday() for o in completed)
    hour_counter = Counter(o.ordered_at.hour for o in completed)
    if weekday_counter:
        result.typical_order_weekday = WEEKDAY_NAMES[weekday_counter.most_common(1)[0][0]]
    if hour_counter:
        result.typical_order_hour = hour_counter.most_common(1)[0][0]

    result.engagement_score = _engagement_score(engagement, orders_last_90d=result.orders_last_90d)
    result.estimated_ltv = estimate_ltv(result)
    return result


def _trend(current: float, previous: float) -> float:
    if previous <= 0:
        return 0.0 if current <= 0 else 1.0
    return _round(max(-1.0, min(1.0, (current - previous) / previous)), 3)


def _engagement_score(engagement: dict, *, orders_last_90d: int) -> float:
    """0-100 blend of message interaction and recent purchasing."""
    sent = engagement.get("messages_sent_90d", 0)
    opened = engagement.get("messages_opened_90d", 0)
    clicked = engagement.get("messages_clicked_90d", 0)
    replied = engagement.get("messages_replied_90d", 0)

    if sent > 0:
        open_rate = min(opened / sent, 1.0)
        click_rate = min(clicked / sent, 1.0)
        reply_rate = min(replied / sent, 1.0)
        message_component = 100 * (0.5 * open_rate + 0.3 * click_rate + 0.2 * reply_rate)
    else:
        message_component = 0.0

    purchase_component = min(orders_last_90d, 6) / 6 * 100

    if sent > 0:
        score = 0.55 * message_component + 0.45 * purchase_component
    else:
        score = purchase_component
    return _round(max(0.0, min(100.0, score)), 1)


def estimate_ltv(metrics: MetricResult) -> float:
    """Simple, transparent predicted LTV.

    LTV = AOV x expected orders per year x expected remaining years, where the
    expected lifespan shrinks as the customer goes quiet. This is a heuristic
    for prioritisation, not a financial forecast.
    """
    if metrics.completed_orders == 0 or metrics.average_order_value <= 0:
        return 0.0

    orders_per_year = max(metrics.purchase_frequency_per_month * 12, 0.5)
    orders_per_year = min(orders_per_year, 60.0)

    expected_interval = (
        metrics.median_purchase_interval_days
        or metrics.average_purchase_interval_days
        or 60.0
    )
    days_late = (metrics.days_since_last_order or 0) - expected_interval
    if days_late <= 0:
        retention_years = 2.0
    elif days_late < expected_interval:
        retention_years = 1.25
    elif days_late < expected_interval * 3:
        retention_years = 0.6
    else:
        retention_years = 0.2

    projected = metrics.average_order_value * orders_per_year * retention_years
    # Never project below what the customer already spent.
    return _round(max(projected, metrics.lifetime_revenue))
