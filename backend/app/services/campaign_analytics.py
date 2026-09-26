"""Advanced analytics and reporting for Smart Reorder campaign automation.

Provides detailed insights into campaign performance, customer journey tracking,
revenue attribution, conversion funnels, and cohort analysis.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.core.enums import SendStatus
from app.models.base import utcnow
from app.models.entities import (
    Automation,
    AutomationSend,
    Customer,
    CustomerCouponAssignment,
    Order,
)

logger = logging.getLogger(__name__)


def get_campaign_performance_summary(
    db: Session,
    automation_id: int,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
) -> dict:
    """Get high-level performance metrics for a campaign.

    Returns:
    {
        "automation_id": int,
        "period": {"start": date, "end": date},
        "audience_size": int,
        "messages_sent": int,
        "messages_delivered": int,
        "delivery_rate": float,
        "unique_customers_contacted": int,
        "orders": int,
        "unique_customers_ordered": int,
        "conversion_rate": float,
        "revenue": float,
        "revenue_per_customer": float,
        "avg_order_value": float,
        "messages_per_customer": float,
        "cost_per_acquisition": float,  # if campaign cost is tracked
    }
    """
    if start_date is None:
        # Default to last 30 days
        end_date = end_date or utcnow()
        start_date = end_date - timedelta(days=30)
    if end_date is None:
        end_date = utcnow()

    # Get total assigned customers
    assigned = db.execute(
        select(func.count(func.distinct(CustomerCouponAssignment.customer_id))).where(
            CustomerCouponAssignment.automation_id == automation_id
        )
    ).scalar() or 0

    # Get message stats
    sends = db.execute(
        select(
            func.count(AutomationSend.id),
            func.sum(
                func.case(
                    (AutomationSend.status == SendStatus.DELIVERED.value, 1),
                    else_=0,
                )
            ),
            func.count(func.distinct(AutomationSend.customer_id)),
        ).where(
            AutomationSend.automation_id == automation_id,
            AutomationSend.created_at >= start_date,
            AutomationSend.created_at <= end_date,
        )
    ).first()

    total_sends, delivered_count, unique_contacted = sends or (0, 0, 0)
    total_sends = total_sends or 0
    delivered_count = delivered_count or 0
    unique_contacted = unique_contacted or 0

    # Get order stats
    orders = db.execute(
        select(
            func.count(Order.id),
            func.count(func.distinct(Order.customer_id)),
            func.sum(Order.total_amount),
            func.avg(Order.total_amount),
        ).where(
            Order.created_at >= start_date,
            Order.created_at <= end_date,
            # Join through customer assignment to campaign
            Order.customer_id.in_(
                select(CustomerCouponAssignment.customer_id).where(
                    CustomerCouponAssignment.automation_id == automation_id
                )
            ),
        )
    ).first()

    total_orders, unique_ordered, total_revenue, avg_order_value = orders or (0, 0, 0, 0)
    total_orders = total_orders or 0
    unique_ordered = unique_ordered or 0
    total_revenue = float(total_revenue or 0.0)
    avg_order_value = float(avg_order_value or 0.0)

    return {
        "automation_id": automation_id,
        "period": {"start": start_date.isoformat(), "end": end_date.isoformat()},
        "audience_size": assigned,
        "messages_sent": total_sends,
        "messages_delivered": delivered_count,
        "delivery_rate": round(delivered_count / total_sends, 4) if total_sends > 0 else 0.0,
        "unique_customers_contacted": unique_contacted,
        "orders": total_orders,
        "unique_customers_ordered": unique_ordered,
        "conversion_rate": (
            round(unique_ordered / unique_contacted, 4)
            if unique_contacted > 0
            else 0.0
        ),
        "revenue": total_revenue,
        "revenue_per_customer": (
            round(total_revenue / assigned, 2) if assigned > 0 else 0.0
        ),
        "avg_order_value": avg_order_value,
        "messages_per_customer": (
            round(total_sends / assigned, 2) if assigned > 0 else 0.0
        ),
    }


def get_customer_journey_details(
    db: Session,
    automation_id: int,
    customer_id: int,
) -> dict:
    """Get detailed journey tracking for a specific customer.

    Returns:
    {
        "customer_id": int,
        "automation_id": int,
        "customer_info": {...},
        "journey_status": "active" | "completed" | "stopped",
        "messages": [
            {
                "send_id": int,
                "position": int,
                "scheduled_for": datetime,
                "sent_at": datetime,
                "status": str,
                "channel": str,
                "body_preview": str,
            }
        ],
        "orders_after_first_message": [...],
        "total_revenue": float,
        "time_to_conversion": int | None,  # days
    }
    """
    # Get customer info
    customer = db.execute(select(Customer).where(Customer.id == customer_id)).scalar()

    if not customer:
        return {"error": "Customer not found"}

    # Get all sends for this customer in this automation
    sends = db.execute(
        select(AutomationSend)
        .where(
            AutomationSend.customer_id == customer_id,
            AutomationSend.automation_id == automation_id,
        )
        .order_by(AutomationSend.scheduled_for)
    ).scalars().all()

    messages_data = []
    first_send_time = None
    for send in sends:
        if first_send_time is None:
            first_send_time = send.scheduled_for

        touchpoint_pos = send.context.get("touchpoint_position", 1) if send.context else 1
        messages_data.append({
            "send_id": send.id,
            "position": touchpoint_pos,
            "scheduled_for": send.scheduled_for.isoformat(),
            "sent_at": send.sent_at.isoformat() if send.sent_at else None,
            "status": send.status,
            "channel": send.channel,
            "body_preview": send.body[:100] + "..." if len(send.body) > 100 else send.body,
        })

    # Get orders after first message
    orders_after = []
    if first_send_time:
        orders = db.execute(
            select(Order)
            .where(
                Order.customer_id == customer_id,
                Order.ordered_at >= first_send_time,
            )
            .order_by(Order.ordered_at)
        ).scalars().all()

        orders_after = [
            {
                "order_id": o.id,
                "ordered_at": o.ordered_at.isoformat(),
                "total_amount": float(o.total_amount),
                "status": o.status,
            }
            for o in orders
        ]

    # Calculate total revenue
    total_revenue = sum(o["total_amount"] for o in orders_after)

    # Calculate time to conversion
    time_to_conversion = None
    if orders_after and first_send_time:
        first_order_time = orders_after[0]["ordered_at"]
        time_to_conversion = (
            datetime.fromisoformat(first_order_time) - first_send_time
        ).days

    return {
        "customer_id": customer_id,
        "automation_id": automation_id,
        "customer_info": {
            "email": customer.email,
            "phone": customer.phone_number,
            "first_name": customer.first_name,
        },
        "journey_status": "active" if len(sends) < 3 else "completed_or_stopped",
        "messages": messages_data,
        "orders_after_first_message": orders_after,
        "total_revenue": float(total_revenue),
        "time_to_conversion_days": time_to_conversion,
    }


def get_conversion_funnel(
    db: Session,
    automation_id: int,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
) -> dict:
    """Get conversion funnel from audience to orders.

    Returns:
    {
        "automation_id": int,
        "funnel": [
            {"stage": "audience", "count": int, "pct_of_prev": 100.0},
            {"stage": "contacted", "count": int, "pct_of_prev": float},
            {"stage": "delivered", "count": int, "pct_of_prev": float},
            {"stage": "ordered", "count": int, "pct_of_prev": float},
        ],
    }
    """
    if start_date is None:
        end_date = end_date or utcnow()
        start_date = end_date - timedelta(days=30)
    if end_date is None:
        end_date = utcnow()

    # Stage 1: Audience (assigned)
    audience = db.execute(
        select(func.count(func.distinct(CustomerCouponAssignment.customer_id))).where(
            CustomerCouponAssignment.automation_id == automation_id
        )
    ).scalar() or 0

    # Stage 2: Contacted (sent at least one message)
    contacted = db.execute(
        select(func.count(func.distinct(AutomationSend.customer_id))).where(
            AutomationSend.automation_id == automation_id,
            AutomationSend.created_at >= start_date,
            AutomationSend.created_at <= end_date,
        )
    ).scalar() or 0

    # Stage 3: Delivered (received at least one message)
    delivered = db.execute(
        select(func.count(func.distinct(AutomationSend.customer_id))).where(
            AutomationSend.automation_id == automation_id,
            AutomationSend.status == SendStatus.DELIVERED.value,
            AutomationSend.created_at >= start_date,
            AutomationSend.created_at <= end_date,
        )
    ).scalar() or 0

    # Stage 4: Ordered (placed an order)
    ordered = db.execute(
        select(func.count(func.distinct(Order.customer_id))).where(
            Order.created_at >= start_date,
            Order.created_at <= end_date,
            Order.customer_id.in_(
                select(CustomerCouponAssignment.customer_id).where(
                    CustomerCouponAssignment.automation_id == automation_id
                )
            ),
        )
    ).scalar() or 0

    funnel = []
    stages = [
        ("audience", audience),
        ("contacted", contacted),
        ("delivered", delivered),
        ("ordered", ordered),
    ]

    for i, (stage_name, count) in enumerate(stages):
        if i == 0:
            pct_of_prev = 100.0
        else:
            prev_count = stages[i - 1][1]
            pct_of_prev = round((count / prev_count * 100), 2) if prev_count > 0 else 0.0

        funnel.append({
            "stage": stage_name,
            "count": count,
            "pct_of_prev": pct_of_prev,
        })

    return {
        "automation_id": automation_id,
        "period": {"start": start_date.isoformat(), "end": end_date.isoformat()},
        "funnel": funnel,
    }


def get_cohort_analysis(
    db: Session,
    automation_id: int,
    cohort_by: Literal["enrollment_date", "first_message_date"] = "enrollment_date",
) -> dict:
    """Analyze performance by cohort (e.g., week of enrollment).

    Returns cohorts with their performance metrics.
    """
    # This is a simplified version; a full implementation would have
    # more sophisticated cohort bucketing (daily, weekly, monthly)
    logger.info(f"Cohort analysis for automation {automation_id} by {cohort_by}")

    return {
        "automation_id": automation_id,
        "cohort_by": cohort_by,
        "cohorts": [],
        "note": "Cohort analysis implementation depends on specific bucketing strategy",
    }


def get_touchpoint_performance(
    db: Session,
    automation_id: int,
) -> dict:
    """Analyze performance at each touchpoint position.

    Useful for multi-touch campaigns to see engagement drop-off.

    Returns:
    {
        "automation_id": int,
        "touchpoints": [
            {
                "position": 1,
                "messages_sent": int,
                "messages_delivered": int,
                "delivery_rate": float,
                "customers_reached": int,
                "orders_from_position": int,
                "conversion_from_position": float,
            },
            ...
        ],
    }
    """
    # Get performance data grouped by touchpoint position
    touchpoint_stats = db.execute(
        select(
            func.cast(
                func.json_extract(AutomationSend.context, "$.touchpoint_position"),
                type_=int,
            ).label("position"),
            func.count(AutomationSend.id).label("total_sent"),
            func.sum(
                func.case(
                    (AutomationSend.status == SendStatus.DELIVERED.value, 1),
                    else_=0,
                )
            ).label("delivered"),
            func.count(func.distinct(AutomationSend.customer_id)).label("customers"),
        )
        .where(AutomationSend.automation_id == automation_id)
        .group_by("position")
        .order_by("position")
    ).all()

    touchpoints = []
    for position, sent, delivered, customers in touchpoint_stats or []:
        delivered = delivered or 0
        delivery_rate = round(delivered / sent, 4) if sent > 0 else 0.0

        touchpoints.append({
            "position": position or 1,
            "messages_sent": sent or 0,
            "messages_delivered": delivered,
            "delivery_rate": delivery_rate,
            "customers_reached": customers or 0,
            "note": "Order tracking per touchpoint requires additional data structure",
        })

    return {
        "automation_id": automation_id,
        "touchpoints": touchpoints,
    }
