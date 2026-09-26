"""A/B testing and experimentation framework for Smart Reorder campaigns.

Supports message variants, coupon variants, and statistical analysis to
optimize campaign performance through controlled experimentation.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.core.enums import SendStatus
from app.models.base import utcnow
from app.models.entities import (
    Automation,
    AutomationSend,
    CustomerCouponAssignment,
    CouponVariant,
    Order,
)

logger = logging.getLogger(__name__)


@dataclass
class VariantStats:
    """Statistics for an A/B test variant."""

    variant_id: str | int
    variant_name: str
    total_sends: int
    delivered: int
    delivery_rate: float
    orders: int
    conversion_rate: float
    revenue: float
    revenue_per_send: float
    customers_reached: int
    avg_order_value: float | None = None
    confidence_interval_95: tuple[float, float] | None = None


def get_coupon_variant_stats(
    db: Session,
    automation_id: int,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
) -> list[VariantStats]:
    """Get statistics for each coupon variant in the campaign.

    Enables comparison of coupon performance for A/B testing.

    Returns list of VariantStats, one per coupon variant.
    """
    if start_date is None:
        end_date = end_date or utcnow()
        start_date = end_date - timedelta(days=30)
    if end_date is None:
        end_date = utcnow()

    # Get all coupon variants for this automation
    variants = db.execute(
        select(CouponVariant).where(CouponVariant.automation_id == automation_id)
    ).scalars().all()

    stats_list = []

    for variant in variants:
        # Count sends for this coupon
        sends = db.execute(
            select(func.count(AutomationSend.id))
            .join(
                CustomerCouponAssignment,
                and_(
                    CustomerCouponAssignment.customer_id == AutomationSend.customer_id,
                    CustomerCouponAssignment.automation_id == AutomationSend.automation_id,
                ),
            )
            .where(
                CustomerCouponAssignment.coupon_code == variant.coupon_code,
                AutomationSend.created_at >= start_date,
                AutomationSend.created_at <= end_date,
            )
        ).scalar() or 0

        # Count delivered
        delivered = db.execute(
            select(func.count(AutomationSend.id))
            .join(
                CustomerCouponAssignment,
                and_(
                    CustomerCouponAssignment.customer_id == AutomationSend.customer_id,
                    CustomerCouponAssignment.automation_id == AutomationSend.automation_id,
                ),
            )
            .where(
                CustomerCouponAssignment.coupon_code == variant.coupon_code,
                AutomationSend.status == SendStatus.DELIVERED.value,
                AutomationSend.created_at >= start_date,
                AutomationSend.created_at <= end_date,
            )
        ).scalar() or 0

        # Count unique customers reached
        customers_reached = db.execute(
            select(func.count(func.distinct(AutomationSend.customer_id)))
            .join(
                CustomerCouponAssignment,
                and_(
                    CustomerCouponAssignment.customer_id == AutomationSend.customer_id,
                    CustomerCouponAssignment.automation_id == AutomationSend.automation_id,
                ),
            )
            .where(
                CustomerCouponAssignment.coupon_code == variant.coupon_code,
                AutomationSend.created_at >= start_date,
                AutomationSend.created_at <= end_date,
            )
        ).scalar() or 0

        # Count orders and revenue
        orders_data = db.execute(
            select(
                func.count(Order.id),
                func.sum(Order.total_amount),
            )
            .join(
                CustomerCouponAssignment,
                CustomerCouponAssignment.customer_id == Order.customer_id,
            )
            .where(
                CustomerCouponAssignment.coupon_code == variant.coupon_code,
                CustomerCouponAssignment.automation_id == automation_id,
                Order.created_at >= start_date,
                Order.created_at <= end_date,
            )
        ).first()

        orders, revenue = orders_data or (0, 0)
        orders = orders or 0
        revenue = float(revenue or 0.0)

        # Calculate metrics
        delivery_rate = round(delivered / sends, 4) if sends > 0 else 0.0
        conversion_rate = round(orders / customers_reached, 4) if customers_reached > 0 else 0.0
        revenue_per_send = round(revenue / sends, 2) if sends > 0 else 0.0

        stats = VariantStats(
            variant_id=variant.id,
            variant_name=variant.coupon_code,
            total_sends=sends,
            delivered=delivered,
            delivery_rate=delivery_rate,
            orders=orders,
            conversion_rate=conversion_rate,
            revenue=revenue,
            revenue_per_send=revenue_per_send,
            customers_reached=customers_reached,
            avg_order_value=round(revenue / orders, 2) if orders > 0 else None,
        )
        stats_list.append(stats)

    return stats_list


def get_message_variant_performance(
    db: Session,
    automation_id: int,
) -> dict:
    """Get performance comparison for message template variants.

    If automation supports multiple message templates, compare their
    performance metrics.
    """
    # This is a placeholder for message template A/B testing
    # Full implementation would require tracking which template was sent
    # in AutomationSend.context

    return {
        "automation_id": automation_id,
        "variants": [],
        "note": "Message template tracking requires storing template_id in send context",
    }


def calculate_statistical_significance(
    variant_a_orders: int,
    variant_a_sends: int,
    variant_b_orders: int,
    variant_b_sends: int,
    confidence_level: float = 0.95,
) -> tuple[float, bool]:
    """Calculate p-value and significance for two-proportion z-test.

    Returns (p_value, is_significant_at_95_percent).

    Uses a two-tailed z-test to compare conversion rates between two variants.
    """
    if variant_a_sends == 0 or variant_b_sends == 0:
        return 1.0, False

    rate_a = variant_a_orders / variant_a_sends
    rate_b = variant_b_orders / variant_b_sends

    pooled_rate = (variant_a_orders + variant_b_orders) / (
        variant_a_sends + variant_b_sends
    )
    std_error = math.sqrt(
        pooled_rate * (1 - pooled_rate) * (1 / variant_a_sends + 1 / variant_b_sends)
    )

    if std_error == 0:
        return 1.0, False

    z_score = (rate_a - rate_b) / std_error
    # Approximate p-value using standard normal distribution
    p_value = 2 * (1 - _normal_cdf(abs(z_score)))

    is_significant = p_value < (1 - confidence_level)

    return p_value, is_significant


def _normal_cdf(x: float) -> float:
    """Approximate cumulative normal distribution function."""
    return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0


def recommend_winner(
    variants: list[VariantStats],
    metric: str = "revenue_per_send",
) -> VariantStats | None:
    """Recommend the winning variant based on specified metric.

    Args:
        variants: List of VariantStats
        metric: Which metric to optimize for
                (revenue_per_send, conversion_rate, delivery_rate)

    Returns the variant with highest metric value, or None if no variants.
    """
    if not variants:
        return None

    if metric == "revenue_per_send":
        return max(variants, key=lambda v: v.revenue_per_send)
    elif metric == "conversion_rate":
        return max(variants, key=lambda v: v.conversion_rate)
    elif metric == "delivery_rate":
        return max(variants, key=lambda v: v.delivery_rate)
    else:
        return variants[0]


def setup_holdout_group(
    automation: Automation,
    holdout_percentage: int = 10,
) -> None:
    """Configure a holdout group for A/B testing.

    The holdout group receives no messages and serves as a control
    for measuring campaign lift.

    Args:
        automation: Automation to configure
        holdout_percentage: Percentage of audience to hold out (1-50)
    """
    if not (1 <= holdout_percentage <= 50):
        raise ValueError("Holdout percentage must be between 1 and 50")

    config = automation.config or {}
    config["holdout_percentage"] = holdout_percentage
    config["holdout_enabled"] = True
    automation.config = config

    logger.info(
        f"Holdout group enabled for automation {automation.id}: {holdout_percentage}%"
    )


def is_customer_in_holdout(
    customer_id: int,
    automation_id: int,
    holdout_percentage: int,
) -> bool:
    """Determine if a customer is in the holdout group using deterministic hashing.

    Uses customer_id hash to ensure same customer always in same group.

    Args:
        customer_id: Customer ID
        automation_id: Automation ID
        holdout_percentage: Holdout percentage configured

    Returns True if customer should be held out from campaign.
    """
    # Use deterministic hash to assign to holdout
    combined = customer_id * 1103515245 + automation_id * 12345
    hash_value = (combined >> 16) % 100

    return hash_value < holdout_percentage


def get_ab_test_summary(
    db: Session,
    automation_id: int,
) -> dict:
    """Get summary of A/B testing setup and results for a campaign.

    Returns:
    {
        "automation_id": int,
        "coupon_variants": [VariantStats],
        "message_variants": [...],
        "holdout_enabled": bool,
        "holdout_percentage": int | None,
        "winner": VariantStats | None,
    }
    """
    automation = db.execute(
        select(Automation).where(Automation.id == automation_id)
    ).scalar()

    if not automation:
        return {}

    config = automation.config or {}
    coupon_stats = get_coupon_variant_stats(db, automation_id)
    winner = recommend_winner(coupon_stats) if coupon_stats else None

    return {
        "automation_id": automation_id,
        "coupon_variants": [
            {
                "variant_id": v.variant_id,
                "variant_name": v.variant_name,
                "total_sends": v.total_sends,
                "delivered": v.delivered,
                "delivery_rate": v.delivery_rate,
                "orders": v.orders,
                "conversion_rate": v.conversion_rate,
                "revenue": v.revenue,
                "revenue_per_send": v.revenue_per_send,
                "customers_reached": v.customers_reached,
            }
            for v in coupon_stats
        ],
        "holdout_enabled": config.get("holdout_enabled", False),
        "holdout_percentage": config.get("holdout_percentage"),
        "winner": {
            "variant_name": winner.variant_name,
            "revenue_per_send": winner.revenue_per_send,
            "conversion_rate": winner.conversion_rate,
        } if winner else None,
    }
