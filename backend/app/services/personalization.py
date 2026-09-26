"""Personalization and dynamic content generation for Smart Reorder campaigns.

Supports merge tags, customer attribute interpolation, dynamic offer selection,
behavior-based personalization, and product recommendations.
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.models.entities import (
    Customer,
    CustomerMetrics,
    Order,
    Product,
)

logger = logging.getLogger(__name__)


def get_customer_attributes(db: Session, customer_id: int) -> dict:
    """Get comprehensive customer attribute dictionary for template rendering.

    Returns:
    {
        "id": int,
        "email": str,
        "phone": str,
        "first_name": str,
        "last_name": str,
        "city": str,
        "total_orders": int,
        "total_spent": float,
        "avg_order_value": float,
        "lifetime_value": float,
        "last_order_date": str | None,
        "days_since_last_order": int | None,
        "preferred_category": str | None,
        "preferred_time": str | None,
        "preferred_day": str | None,
        "customer_segment": str | None,
        "is_vip": bool,
        "is_at_risk": bool,
        "churn_risk_score": float | None,
    }
    """
    customer = db.execute(select(Customer).where(Customer.id == customer_id)).scalar()

    if not customer:
        return {}

    # Get metrics
    metrics = db.execute(
        select(CustomerMetrics).where(CustomerMetrics.customer_id == customer_id)
    ).scalar()

    # Get order history
    orders = db.execute(
        select(Order)
        .where(Order.customer_id == customer_id)
        .order_by(Order.ordered_at.desc())
    ).scalars().all()

    total_orders = len(orders)
    total_spent = sum(o.total_amount for o in orders)
    avg_order_value = total_spent / total_orders if total_orders > 0 else 0.0

    last_order = orders[0] if orders else None
    last_order_date = None
    days_since_last = None

    if last_order:
        last_order_date = last_order.ordered_at.isoformat()
        days_since_last = (datetime.utcnow() - last_order.ordered_at).days

    return {
        "id": customer.id,
        "email": customer.email,
        "phone": customer.phone_number,
        "first_name": customer.first_name or "there",
        "last_name": customer.last_name or "",
        "city": customer.city or "",
        "total_orders": total_orders,
        "total_spent": float(total_spent),
        "avg_order_value": float(avg_order_value),
        "lifetime_value": float(total_spent),
        "last_order_date": last_order_date,
        "days_since_last_order": days_since_last,
        "preferred_category": metrics.preferred_category if metrics else None,
        "preferred_time": metrics.preferred_time if metrics else None,
        "preferred_day": metrics.preferred_day if metrics else None,
        "customer_segment": customer.segment or None,
        "is_vip": (total_spent > 500) if total_spent else False,
        "is_at_risk": days_since_last is not None and days_since_last > 60,
        "churn_risk_score": metrics.churn_risk_score if metrics else None,
    }


def get_product_recommendations(
    db: Session,
    customer_id: int,
    count: int = 3,
) -> list[dict]:
    """Get personalized product recommendations for a customer.

    Based on purchase history and category preferences.

    Returns list of product dicts with name, description, price.
    """
    # Get customer's most purchased category
    top_category = db.execute(
        select(Product.category)
        .join(Order, Order.id == Product.order_id)
        .where(Order.customer_id == customer_id)
        .group_by(Product.category)
        .order_by(func.count(Product.id).desc())
        .limit(1)
    ).scalar()

    if not top_category:
        return []

    # Get popular products from that category
    products = db.execute(
        select(Product)
        .where(Product.category == top_category)
        .order_by(Product.popularity_score.desc())
        .limit(count)
    ).scalars().all()

    return [
        {
            "name": p.name,
            "description": p.description or "",
            "price": float(p.price),
            "category": p.category,
        }
        for p in products
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
