"""Analytics for coupon performance in Smart Reorder campaigns.

Tracks coupon code effectiveness including message sends, order conversions,
revenue attribution, and coupon usage rates.
"""
from __future__ import annotations

import logging

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.core.enums import SendStatus
from app.models.entities import (
    Automation,
    AutomationSend,
    CustomerCouponAssignment,
    Order,
)

logger = logging.getLogger(__name__)


def get_coupon_performance(
    db: Session,
    automation_id: int,
) -> dict:
    """Get performance metrics for each coupon variant in an automation.

    Returns:
        {
            "automation_id": int,
            "coupons": [
                {
                    "coupon_code": str,
                    "assigned_customers": int,
                    "messages_sent": int,
                    "messages_delivered": int,
                    "orders": int,
                    "conversion_rate": float,
                    "revenue": float,
                    "coupon_used_count": int,
                    "coupon_usage_rate": float,
                    "revenue_per_customer": float,
                }
            ],
            "summary": {
                "total_assigned": int,
                "total_sent": int,
                "total_orders": int,
                "total_revenue": float,
                "overall_conversion_rate": float,
            }
        }
    """
    # Get customer->coupon assignments
    assignments = db.execute(
        select(
            CustomerCouponAssignment.coupon_code,
            func.count(CustomerCouponAssignment.customer_id).label("assigned_count"),
        )
        .where(CustomerCouponAssignment.automation_id == automation_id)
        .group_by(CustomerCouponAssignment.coupon_code)
    ).all()

    coupon_stats = {}
    for coupon_code, assigned_count in assignments:
        coupon_stats[coupon_code] = {
            "coupon_code": coupon_code,
            "assigned_customers": assigned_count,
            "messages_sent": 0,
            "messages_delivered": 0,
            "orders": 0,
            "revenue": 0.0,
            "coupon_used_count": 0,
        }

    # Get message sends by coupon (via customer assignment lookup)
    # Messages sent with each coupon
    send_stats = db.execute(
        select(
            CustomerCouponAssignment.coupon_code,
            func.count(AutomationSend.id).label("send_count"),
            func.sum(
                func.case(
                    (AutomationSend.status == SendStatus.DELIVERED.value, 1),
                    else_=0,
                )
            ).label("delivered_count"),
        )
        .join(
            CustomerCouponAssignment,
            and_(
                CustomerCouponAssignment.customer_id == AutomationSend.customer_id,
                CustomerCouponAssignment.automation_id == AutomationSend.automation_id,
            ),
        )
        .where(
            AutomationSend.automation_id == automation_id,
            AutomationSend.status.in_(
                [SendStatus.SENT.value, SendStatus.DELIVERED.value]
            ),
        )
        .group_by(CustomerCouponAssignment.coupon_code)
    ).all()

    for coupon_code, send_count, delivered_count in send_stats:
        if coupon_code in coupon_stats:
            coupon_stats[coupon_code]["messages_sent"] = send_count or 0
            coupon_stats[coupon_code]["messages_delivered"] = delivered_count or 0

    # Get orders by coupon (track which coupon was used)
    # For now, we'll count orders from customers who were sent a specific coupon
    # More sophisticated tracking would require recording which coupon was actually used in the order
    order_stats = db.execute(
        select(
            CustomerCouponAssignment.coupon_code,
            func.count(Order.id).label("order_count"),
            func.sum(Order.total_amount).label("total_revenue"),
        )
        .join(
            CustomerCouponAssignment,
            and_(
                CustomerCouponAssignment.customer_id == Order.customer_id,
                CustomerCouponAssignment.automation_id == automation_id,
            ),
        )
        .where(Order.created_at >= db.execute(
            select(func.min(CustomerCouponAssignment.assigned_at))
            .where(CustomerCouponAssignment.automation_id == automation_id)
        ).scalar()
        )
        .group_by(CustomerCouponAssignment.coupon_code)
    ).all()

    for coupon_code, order_count, total_revenue in order_stats:
        if coupon_code in coupon_stats:
            coupon_stats[coupon_code]["orders"] = order_count or 0
            coupon_stats[coupon_code]["revenue"] = float(total_revenue or 0.0)

    # Calculate derived metrics
    summary = {
        "total_assigned": 0,
        "total_sent": 0,
        "total_orders": 0,
        "total_revenue": 0.0,
    }

    for coupon_code, stats in coupon_stats.items():
        stats["conversion_rate"] = (
            round(stats["orders"] / stats["messages_sent"], 4)
            if stats["messages_sent"] > 0
            else 0.0
        )
        stats["revenue_per_customer"] = (
            round(stats["revenue"] / stats["assigned_customers"], 2)
            if stats["assigned_customers"] > 0
            else 0.0
        )
        stats["coupon_usage_rate"] = (
            round(stats["coupon_used_count"] / stats["assigned_customers"], 4)
            if stats["assigned_customers"] > 0
            else 0.0
        )

        summary["total_assigned"] += stats["assigned_customers"]
        summary["total_sent"] += stats["messages_sent"]
        summary["total_orders"] += stats["orders"]
        summary["total_revenue"] += stats["revenue"]

    summary["overall_conversion_rate"] = (
        round(summary["total_orders"] / summary["total_sent"], 4)
        if summary["total_sent"] > 0
        else 0.0
    )

    return {
        "automation_id": automation_id,
        "coupons": list(coupon_stats.values()),
        "summary": summary,
    }


def get_coupon_assignment_stats(
    db: Session,
    automation_id: int,
) -> dict:
    """Get basic stats about coupon assignments (without order tracking).

    Useful for checking that allocations matched expectations.
    """
    stats = db.execute(
        select(
            CustomerCouponAssignment.coupon_code,
            func.count(CustomerCouponAssignment.customer_id).label("count"),
        )
        .where(CustomerCouponAssignment.automation_id == automation_id)
        .group_by(CustomerCouponAssignment.coupon_code)
    ).all()

    total = sum(count for _, count in stats)

    return {
        "automation_id": automation_id,
        "assignments_by_coupon": {
            coupon_code: count for coupon_code, count in stats
        },
        "total_assignments": total,
        "allocation_percentages": {
            coupon_code: round((count / total * 100), 2)
            for coupon_code, count in stats
            if total > 0
        },
    }
