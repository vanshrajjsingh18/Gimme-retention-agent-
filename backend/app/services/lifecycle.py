"""Deterministic customer lifecycle classification.

The classifier prefers the customer's own purchase cadence when there is
enough order history; otherwise it falls back to configurable global
thresholds. Every decision returns a human-readable reason so the UI can show
why a customer sits in a stage.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.analytics.metrics import MetricResult
from app.core.enums import LifecycleStage
from app.models.base import utcnow


@dataclass(frozen=True)
class LifecycleThresholds:
    """Global fallbacks used when a customer has < 3 completed orders."""

    new_window_days: int = 30
    default_cycle_days: float = 45.0
    at_risk_multiplier: float = 1.5
    dormant_multiplier: float = 3.0
    churned_multiplier: float = 6.0
    dormant_floor_days: int = 120
    churned_floor_days: int = 240
    regular_min_orders: int = 3
    high_value_revenue: float = 600.0
    vip_revenue: float = 1500.0
    vip_min_orders: int = 10
    reactivation_gap_days: int = 90
    reactivation_recent_days: int = 30
    min_orders_for_personal_cadence: int = 3


DEFAULT_THRESHOLDS = LifecycleThresholds()


@dataclass
class LifecycleResult:
    stage: LifecycleStage
    reason: str
    expected_cycle_days: float
    days_late: float
    cadence_source: str  # "personal" | "global"


def expected_cycle_days(
    metrics: MetricResult, thresholds: LifecycleThresholds = DEFAULT_THRESHOLDS
) -> tuple[float, str]:
    """Return the customer's expected days-between-orders and its source."""
    if (
        metrics.completed_orders >= thresholds.min_orders_for_personal_cadence
        and metrics.median_purchase_interval_days
        and metrics.median_purchase_interval_days > 0
    ):
        return float(metrics.median_purchase_interval_days), "personal"
    if (
        metrics.completed_orders >= 2
        and metrics.average_purchase_interval_days
        and metrics.average_purchase_interval_days > 0
    ):
        # Blend a two-order sample with the global default to avoid overreacting
        # to a single observed gap.
        blended = (float(metrics.average_purchase_interval_days) + thresholds.default_cycle_days) / 2
        return blended, "blended"
    return thresholds.default_cycle_days, "global"


def classify_lifecycle(
    metrics: MetricResult,
    *,
    signup_date: datetime | None = None,
    now: datetime | None = None,
    thresholds: LifecycleThresholds = DEFAULT_THRESHOLDS,
    had_lapse: bool | None = None,
) -> LifecycleResult:
    """Classify a customer into exactly one lifecycle stage.

    ``had_lapse`` marks a customer who previously went quiet past the
    reactivation gap and has since ordered again; when omitted it is inferred
    from the metric window fields.
    """
    now = now or utcnow()
    cycle, cadence_source = expected_cycle_days(metrics, thresholds)
    days_since = metrics.days_since_last_order
    days_late = float(days_since - cycle) if days_since is not None else 0.0

    # No purchase history at all.
    if metrics.completed_orders == 0:
        if signup_date is not None and (now - signup_date).days <= thresholds.new_window_days:
            return LifecycleResult(
                LifecycleStage.NEW,
                "Signed up within the last "
                f"{thresholds.new_window_days} days and has not ordered yet.",
                cycle,
                0.0,
                cadence_source,
            )
        if metrics.cancelled_orders > 0:
            return LifecycleResult(
                LifecycleStage.CHURNED,
                "Has only cancelled or refunded orders and no completed purchase.",
                cycle,
                0.0,
                cadence_source,
            )
        return LifecycleResult(
            LifecycleStage.DORMANT,
            "Registered but has never completed an order.",
            cycle,
            0.0,
            cadence_source,
        )

    assert days_since is not None  # completed_orders > 0 guarantees a last order

    # Lapsed states take precedence over value tiers: a VIP who has vanished
    # needs to show up as CHURNED, not VIP.
    churn_cut = max(cycle * thresholds.churned_multiplier, thresholds.churned_floor_days)
    dormant_cut = max(cycle * thresholds.dormant_multiplier, thresholds.dormant_floor_days)
    at_risk_cut = cycle * thresholds.at_risk_multiplier

    if days_since >= churn_cut:
        return LifecycleResult(
            LifecycleStage.CHURNED,
            f"Last ordered {days_since} days ago, beyond the "
            f"{int(churn_cut)}-day churn threshold for their {int(cycle)}-day cycle.",
            cycle,
            days_late,
            cadence_source,
        )
    if days_since >= dormant_cut:
        return LifecycleResult(
            LifecycleStage.DORMANT,
            f"Last ordered {days_since} days ago, beyond the "
            f"{int(dormant_cut)}-day dormancy threshold.",
            cycle,
            days_late,
            cadence_source,
        )
    if days_since >= at_risk_cut:
        return LifecycleResult(
            LifecycleStage.AT_RISK,
            f"Last ordered {days_since} days ago vs an expected {int(cycle)}-day cycle "
            f"({int(days_late)} days overdue).",
            cycle,
            days_late,
            cadence_source,
        )

    # Active customer: did they come back from a lapse?
    if had_lapse is None:
        had_lapse = _infer_lapse(metrics, thresholds)
    if had_lapse and days_since <= thresholds.reactivation_recent_days:
        return LifecycleResult(
            LifecycleStage.REACTIVATED,
            "Returned to ordering after a gap of at least "
            f"{thresholds.reactivation_gap_days} days.",
            cycle,
            days_late,
            cadence_source,
        )

    # Value tiers for active customers.
    if (
        metrics.lifetime_revenue >= thresholds.vip_revenue
        and metrics.completed_orders >= thresholds.vip_min_orders
    ):
        return LifecycleResult(
            LifecycleStage.VIP,
            f"${metrics.lifetime_revenue:,.0f} lifetime revenue across "
            f"{metrics.completed_orders} orders and still ordering on cadence.",
            cycle,
            days_late,
            cadence_source,
        )
    if metrics.lifetime_revenue >= thresholds.high_value_revenue:
        return LifecycleResult(
            LifecycleStage.HIGH_VALUE,
            f"${metrics.lifetime_revenue:,.0f} lifetime revenue, above the "
            f"${thresholds.high_value_revenue:,.0f} high-value threshold.",
            cycle,
            days_late,
            cadence_source,
        )
    if metrics.completed_orders >= thresholds.regular_min_orders:
        return LifecycleResult(
            LifecycleStage.REGULAR,
            f"{metrics.completed_orders} completed orders with a steady "
            f"{int(cycle)}-day cycle.",
            cycle,
            days_late,
            cadence_source,
        )
    if metrics.completed_orders >= 2:
        return LifecycleResult(
            LifecycleStage.ACTIVATING,
            "Placed a repeat order and is building a habit "
            f"({metrics.completed_orders} orders so far).",
            cycle,
            days_late,
            cadence_source,
        )

    # Exactly one completed order.
    if metrics.days_since_first_order is not None and (
        metrics.days_since_first_order <= thresholds.new_window_days
    ):
        return LifecycleResult(
            LifecycleStage.NEW,
            f"First order was {metrics.days_since_first_order} days ago; still in the "
            f"{thresholds.new_window_days}-day new-customer window.",
            cycle,
            days_late,
            cadence_source,
        )
    return LifecycleResult(
        LifecycleStage.ACTIVATING,
        "One completed order; needs a second order to establish a habit.",
        cycle,
        days_late,
        cadence_source,
    )


def _infer_lapse(metrics: MetricResult, thresholds: LifecycleThresholds) -> bool:
    """Detect a return-from-quiet purely from aggregate metrics.

    A customer who bought recently but had no orders in the preceding 90-day
    window, while having a longer history, is treated as reactivated.
    """
    if metrics.completed_orders < 2:
        return False
    if metrics.orders_last_30d == 0:
        return False
    if metrics.revenue_prev_90d > 0:
        return False
    if metrics.days_since_first_order is None:
        return False
    return metrics.days_since_first_order > thresholds.reactivation_gap_days


def detect_reactivation_from_history(
    order_dates: list[datetime],
    *,
    gap_days: int = DEFAULT_THRESHOLDS.reactivation_gap_days,
) -> bool:
    """True when the most recent order followed a gap of >= ``gap_days``."""
    if len(order_dates) < 2:
        return False
    ordered = sorted(order_dates)
    gap = (ordered[-1] - ordered[-2]).days
    return gap >= gap_days
