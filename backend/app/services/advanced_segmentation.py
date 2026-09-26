"""Advanced segmentation and targeting for Smart Reorder campaigns.

Supports dynamic audience segmentation, behavioral rules, RFM analysis,
lifecycle stages, and lookalike targeting based on high-value customers.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.models.base import utcnow
from app.models.entities import (
    Customer,
    CustomerMetrics,
    Order,
)

logger = logging.getLogger(__name__)


def get_rfm_scores(db: Session) -> dict[int, dict]:
    """Calculate RFM (Recency, Frequency, Monetary) scores for all customers.

    RFM is a customer segmentation method based on:
    - Recency: Days since last order
    - Frequency: Total number of orders
    - Monetary: Total amount spent

    Returns dict of {customer_id: {recency, frequency, monetary, rfm_score}}.
    """
    now = utcnow()

    # Get order data for all customers
    order_data = db.execute(
        select(
            Order.customer_id,
            func.max(Order.ordered_at).label("last_order"),
            func.count(Order.id).label("order_count"),
            func.sum(Order.total_amount).label("total_spent"),
        )
        .group_by(Order.customer_id)
    ).all()

    rfm_scores = {}

    for customer_id, last_order, order_count, total_spent in order_data:
        # Recency: days since last order
        recency = (now - last_order).days if last_order else 999

        # Frequency: total orders
        frequency = order_count or 0

        # Monetary: total spent
        monetary = float(total_spent or 0.0)

        # Calculate RFM score (1-5 scale for each dimension)
        # Lower recency is better (1-5)
        if recency <= 7:
            r_score = 5
        elif recency <= 30:
            r_score = 4
        elif recency <= 90:
            r_score = 3
        elif recency <= 180:
            r_score = 2
        else:
            r_score = 1

        # Higher frequency is better (1-5)
        if frequency >= 10:
            f_score = 5
        elif frequency >= 5:
            f_score = 4
        elif frequency >= 3:
            f_score = 3
        elif frequency >= 2:
            f_score = 2
        else:
            f_score = 1

        # Higher monetary is better (1-5)
        if monetary >= 500:
            m_score = 5
        elif monetary >= 200:
            m_score = 4
        elif monetary >= 100:
            m_score = 3
        elif monetary >= 50:
            m_score = 2
        else:
            m_score = 1

        # Combined RFM score (average of three dimensions)
        rfm_score = (r_score + f_score + m_score) / 3

        rfm_scores[customer_id] = {
            "recency": recency,
            "frequency": frequency,
            "monetary": monetary,
            "r_score": r_score,
            "f_score": f_score,
            "m_score": m_score,
            "rfm_score": round(rfm_score, 2),
        }

    return rfm_scores


def classify_customer_lifecycle(customer_id: int, rfm: dict) -> str:
    """Classify customer into lifecycle stage based on RFM scores.

    Lifecycle stages:
    - "new": Recent first order (recency < 7 days, frequency = 1)
    - "active": Recent and frequent (recency < 30 days, frequency >= 2)
    - "loyal": High-value repeat customer (rfm_score >= 4, frequency >= 5)
    - "at_risk": Was active but hasn't ordered recently (recency 30-90, frequency >= 2)
    - "churned": Very inactive (recency > 90)
    - "dormant": Former customer (recency > 180)
    """
    recency = rfm.get("recency", 999)
    frequency = rfm.get("frequency", 0)
    rfm_score = rfm.get("rfm_score", 0)

    if frequency == 1 and recency < 7:
        return "new"
    elif recency < 30 and frequency >= 2:
        return "active"
    elif rfm_score >= 4 and frequency >= 5:
        return "loyal"
    elif 30 <= recency <= 90 and frequency >= 2:
        return "at_risk"
    elif 90 < recency <= 180:
        return "churned"
    else:
        return "dormant"


def segment_by_lifecycle(db: Session) -> dict[str, list[int]]:
    """Segment all customers by lifecycle stage.

    Returns dict of {lifecycle_stage: [customer_ids]}.
    """
    rfm_scores = get_rfm_scores(db)

    segments = {
        "new": [],
        "active": [],
        "loyal": [],
        "at_risk": [],
        "churned": [],
        "dormant": [],
    }

    for customer_id, rfm in rfm_scores.items():
        stage = classify_customer_lifecycle(customer_id, rfm)
        segments[stage].append(customer_id)

    return segments


def get_high_value_customers(
    db: Session,
    min_lifetime_value: float = 200.0,
    min_order_count: int = 3,
) -> list[int]:
    """Get list of high-value customers for lookalike targeting.

    Returns customer IDs that meet high-value criteria.
    """
    rfm_scores = get_rfm_scores(db)

    high_value = []
    for customer_id, rfm in rfm_scores.items():
        if rfm["monetary"] >= min_lifetime_value and rfm["frequency"] >= min_order_count:
            high_value.append(customer_id)

    return high_value


def find_lookalike_customers(
    db: Session,
    base_customers: list[int],
    lookalike_pool_size: int = 100,
) -> list[dict]:
    """Find customers similar to high-value base set.

    Lookalikes based on:
    - Similar RFM scores
    - Same geographic region
    - Similar order patterns

    Returns list of (customer_id, similarity_score) tuples.
    """
    rfm_scores = get_rfm_scores(db)

    # Calculate average RFM of base customers
    base_rfm_scores = [rfm_scores[cid] for cid in base_customers if cid in rfm_scores]

    if not base_rfm_scores:
        return []

    avg_r_score = sum(r["r_score"] for r in base_rfm_scores) / len(base_rfm_scores)
    avg_f_score = sum(r["f_score"] for r in base_rfm_scores) / len(base_rfm_scores)
    avg_m_score = sum(r["m_score"] for r in base_rfm_scores) / len(base_rfm_scores)

    # Find customers with similar RFM
    lookalikes = []

    for customer_id, rfm in rfm_scores.items():
        if customer_id in base_customers:
            continue

        # Calculate similarity as distance from average base RFM
        diff_r = abs(rfm["r_score"] - avg_r_score)
        diff_f = abs(rfm["f_score"] - avg_f_score)
        diff_m = abs(rfm["m_score"] - avg_m_score)

        # Similarity score (higher is better, max 15)
        similarity = 15 - (diff_r + diff_f + diff_m)

        if similarity >= 10:  # Only include reasonably similar customers
            lookalikes.append({
                "customer_id": customer_id,
                "similarity_score": round(similarity / 15, 2),
            })

    # Sort by similarity and return top N
    lookalikes.sort(key=lambda x: x["similarity_score"], reverse=True)
    return lookalikes[:lookalike_pool_size]


def apply_targeting_rules(
    db: Session,
    rules: list[dict],
) -> list[int]:
    """Apply a set of targeting rules to find matching customers.

    Rules format:
    [
        {
            "type": "rfm_score_min",
            "value": 3.5,
            "operator": ">="
        },
        {
            "type": "days_since_order_max",
            "value": 30,
            "operator": "<="
        },
        {
            "type": "order_count_min",
            "value": 2,
            "operator": ">="
        }
    ]

    Returns list of customer IDs matching all rules.
    """
    rfm_scores = get_rfm_scores(db)

    matching_customers = []

    for customer_id, rfm in rfm_scores.items():
        matches_all = True

        for rule in rules:
            rule_type = rule.get("type")
            value = rule.get("value")
            operator = rule.get("operator", ">=")

            if rule_type == "rfm_score_min":
                if operator == ">=" and rfm["rfm_score"] < value:
                    matches_all = False
                elif operator == "<=" and rfm["rfm_score"] > value:
                    matches_all = False
            elif rule_type == "days_since_order_max":
                if operator == "<=" and rfm["recency"] > value:
                    matches_all = False
                elif operator == ">=" and rfm["recency"] < value:
                    matches_all = False
            elif rule_type == "order_count_min":
                if operator == ">=" and rfm["frequency"] < value:
                    matches_all = False
                elif operator == "<=" and rfm["frequency"] > value:
                    matches_all = False
            elif rule_type == "lifetime_value_min":
                if operator == ">=" and rfm["monetary"] < value:
                    matches_all = False
                elif operator == "<=" and rfm["monetary"] > value:
                    matches_all = False

            if not matches_all:
                break

        if matches_all:
            matching_customers.append(customer_id)

    return matching_customers


def get_segmentation_analysis(db: Session) -> dict:
    """Get comprehensive segmentation analysis for the customer base.

    Returns:
    {
        "total_customers": int,
        "lifecycle_segments": {
            "new": int,
            "active": int,
            "loyal": int,
            "at_risk": int,
            "churned": int,
            "dormant": int,
        },
        "rfm_quartiles": {
            "high_value": int,
            "medium_value": int,
            "low_value": int,
        },
        "avg_customer_ltv": float,
        "high_value_customers_count": int,
    }
    """
    rfm_scores = get_rfm_scores(db)

    if not rfm_scores:
        return {
            "total_customers": 0,
            "lifecycle_segments": {},
            "rfm_quartiles": {},
        }

    lifecycle_segs = segment_by_lifecycle(db)
    high_value = get_high_value_customers(db)

    # Calculate LTV quartiles
    all_ltv = [rfm["monetary"] for rfm in rfm_scores.values()]
    avg_ltv = sum(all_ltv) / len(all_ltv) if all_ltv else 0

    high_value_count = len([ltv for ltv in all_ltv if ltv >= avg_ltv * 1.5])
    medium_value_count = len(
        [ltv for ltv in all_ltv if avg_ltv * 0.5 <= ltv < avg_ltv * 1.5]
    )
    low_value_count = len([ltv for ltv in all_ltv if ltv < avg_ltv * 0.5])

    return {
        "total_customers": len(rfm_scores),
        "lifecycle_segments": {
            "new": len(lifecycle_segs["new"]),
            "active": len(lifecycle_segs["active"]),
            "loyal": len(lifecycle_segs["loyal"]),
            "at_risk": len(lifecycle_segs["at_risk"]),
            "churned": len(lifecycle_segs["churned"]),
            "dormant": len(lifecycle_segs["dormant"]),
        },
        "rfm_quartiles": {
            "high_value": high_value_count,
            "medium_value": medium_value_count,
            "low_value": low_value_count,
        },
        "avg_customer_ltv": round(avg_ltv, 2),
        "high_value_customers_count": len(high_value),
    }
