"""Personalization and dynamic content generation for Smart Reorder campaigns.

Supports merge tags, customer attribute interpolation, dynamic offer selection,
behavior-based personalization, and product recommendations.
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.core.enums import LifecycleStage, OrderStatus
from app.models.base import utcnow
from app.models.entities import (
    ChurnScore,
    Customer,
    CustomerMetrics,
    Order,
    OrderItem,
)

logger = logging.getLogger(__name__)


def get_customer_attributes(db: Session, customer_id: int) -> dict:
    """Customer attributes for template previews, read from stored intelligence.

    Values come from ``CustomerMetrics`` (refreshed by the intelligence job)
    rather than being recomputed here, so this agrees with Customer 360.
    """
    customer = db.get(Customer, customer_id)
    if not customer:
        return {}

    metrics = db.execute(
        select(CustomerMetrics).where(CustomerMetrics.customer_id == customer_id)
    ).scalar_one_or_none()
    churn = db.execute(
        select(ChurnScore).where(ChurnScore.customer_id == customer_id)
    ).scalar_one_or_none()

    last_order_at = metrics.last_order_at if metrics else None
    return {
        "id": customer.id,
        "email": customer.email,
        "phone": customer.phone,
        "first_name": customer.first_name or "there",
        "last_name": customer.last_name or "",
        "city": customer.city or "",
        "total_orders": metrics.total_orders if metrics else 0,
        "total_spent": float(metrics.lifetime_revenue) if metrics else 0.0,
        "avg_order_value": float(metrics.average_order_value) if metrics else 0.0,
        "lifetime_value": float(metrics.lifetime_revenue) if metrics else 0.0,
        "last_order_date": last_order_at.isoformat() if last_order_at else None,
        "days_since_last_order": metrics.days_since_last_order if metrics else None,
        "preferred_category": (
            (metrics.preferred_categories or [None])[0] if metrics else None
        ),
        "preferred_time": (
            f"{metrics.typical_order_hour:02d}:{(metrics.typical_order_minute or 0):02d}"
            if metrics and metrics.typical_order_hour is not None
            else None
        ),
        "preferred_day": metrics.typical_order_weekday if metrics else None,
        "customer_segment": customer.lifecycle_stage,
        "is_vip": customer.lifecycle_stage == LifecycleStage.VIP.value,
        "is_at_risk": customer.lifecycle_stage == LifecycleStage.AT_RISK.value,
        "churn_risk_score": churn.score if churn else None,
    }


def get_product_recommendations(
    db: Session,
    customer_id: int,
    count: int = 3,
) -> list[dict]:
    """The best-selling products in the category this customer buys most.

    Ranked by units sold across completed orders. Products they already buy
    are included: for a reorder business, "your usual" is a recommendation.
    """
    top_category = db.execute(
        select(OrderItem.category)
        .join(Order, Order.id == OrderItem.order_id)
        .where(Order.customer_id == customer_id, Order.status == OrderStatus.COMPLETED.value)
        .group_by(OrderItem.category)
        .order_by(func.count(OrderItem.id).desc())
        .limit(1)
    ).scalar()
    if not top_category:
        return []

    rows = db.execute(
        select(
            OrderItem.product_name,
            OrderItem.brand,
            OrderItem.category,
            func.sum(OrderItem.quantity).label("units"),
        )
        .join(Order, Order.id == OrderItem.order_id)
        .where(OrderItem.category == top_category, Order.status == OrderStatus.COMPLETED.value)
        .group_by(OrderItem.product_name, OrderItem.brand, OrderItem.category)
        .order_by(func.sum(OrderItem.quantity).desc())
        .limit(count)
    ).all()
    return [
        {"name": name, "brand": brand, "category": category, "units_sold": int(units or 0)}
        for name, brand, category, units in rows
    ]


def select_dynamic_offer(
    db: Session,
    customer_id: int,
    automation_id: int,
) -> dict:
    """Select the most relevant offer for this customer.

    Considers customer attributes, order history, and offer performance.

    Returns:
    {
        "offer_type": "discount_percent" | "fixed_discount" | "free_delivery",
        "offer_value": float,
        "offer_code": str,
        "reason": str,  # Why this offer was selected
    }
    """
    customer_attrs = get_customer_attributes(db, customer_id)

    # VIP customers get better offers
    if customer_attrs.get("is_vip"):
        return {
            "offer_type": "discount_percent",
            "offer_value": 15.0,
            "offer_code": "VIP15",
            "reason": "VIP customer - premium offer",
        }

    # At-risk customers get incentive
    if customer_attrs.get("is_at_risk"):
        return {
            "offer_type": "discount_percent",
            "offer_value": 10.0,
            "offer_code": "COMEBACK10",
            "reason": "At-risk customer - reactivation offer",
        }

    # First-time buyers get welcome offer
    if customer_attrs.get("total_orders") == 1:
        return {
            "offer_type": "fixed_discount",
            "offer_value": 5.0,
            "offer_code": "WELCOME5",
            "reason": "First-time buyer - welcome offer",
        }

    # Regular customers get loyalty offer
    return {
        "offer_type": "discount_percent",
        "offer_value": 5.0,
        "offer_code": "LOYAL5",
        "reason": "Regular customer - loyalty offer",
    }


def render_personalized_template(
    template: str,
    customer_id: int,
    db: Session,
    extra_context: dict | None = None,
) -> str:
    """Render a message template with customer personalization.

    Replaces merge tags with customer attributes, handles missing values gracefully.

    Args:
        template: Template string with {merge_tags}
        customer_id: Customer to personalize for
        db: Database session
        extra_context: Additional context variables to use

    Returns rendered message string.
    """
    context = get_customer_attributes(db, customer_id)
    context.update(extra_context or {})

    # Replace merge tags
    rendered = template

    for key, value in context.items():
        placeholder = "{" + key + "}"
        if isinstance(value, (int, float)):
            value_str = str(value)
        elif value is None:
            value_str = ""
        else:
            value_str = str(value)

        rendered = rendered.replace(placeholder, value_str)

    # Replace any unresolved tags with empty string
    import re
    rendered = re.sub(r"\{[^}]+\}", "", rendered)

    return rendered


def get_personalization_tokens() -> list[str]:
    """Get list of available merge tokens for message templates.

    Returns list of token names that can be used in templates.
    """
    return [
        "id",
        "email",
        "phone",
        "first_name",
        "last_name",
        "city",
        "total_orders",
        "total_spent",
        "avg_order_value",
        "lifetime_value",
        "last_order_date",
        "days_since_last_order",
        "preferred_category",
        "preferred_time",
        "preferred_day",
        "customer_segment",
    ]


def validate_template(template: str) -> tuple[bool, list[str]]:
    """Validate that all merge tags in template are supported.

    Returns (is_valid, list_of_invalid_tags).
    """
    import re

    valid_tokens = get_personalization_tokens()
    pattern = r"\{([^}]+)\}"
    found_tags = re.findall(pattern, template)

    invalid_tags = [tag for tag in found_tags if tag not in valid_tokens]

    return len(invalid_tags) == 0, invalid_tags


def get_personalization_preview(
    db: Session,
    template: str,
    customer_id: int,
) -> dict:
    """Generate a preview of how a template will render for a customer.

    Returns:
    {
        "customer_id": int,
        "customer_name": str,
        "rendered_message": str,
        "tokens_used": [...],
        "missing_tokens": [...],
    }
    """
    customer = db.execute(select(Customer).where(Customer.id == customer_id)).scalar()

    if not customer:
        return {"error": "Customer not found"}

    import re

    customer_attrs = get_customer_attributes(db, customer_id)
    rendered = render_personalized_template(template, customer_id, db)

    # Find tokens used
    pattern = r"\{([^}]+)\}"
    tokens_found = set(re.findall(pattern, template))
    valid_tokens = set(get_personalization_tokens())
    missing = list(tokens_found - valid_tokens)

    return {
        "customer_id": customer_id,
        "customer_name": f"{customer.first_name} {customer.last_name}",
        "rendered_message": rendered,
        "tokens_used": list(tokens_found),
        "missing_or_invalid_tokens": missing,
    }
