"""Segment evaluation and membership maintenance."""
from __future__ import annotations

import logging

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.enums import SegmentStatus, SegmentType
from app.models.base import utcnow
from app.models.entities import CustomerSegment, Segment
from app.segmentation.rules import evaluate, validate_rule
from app.services.intelligence import load_customer_views

logger = logging.getLogger(__name__)


def evaluate_segment(db: Session, segment: Segment, *, views: list[dict] | None = None) -> list[dict]:
    """Return the customer views currently matching a segment's rule."""
    if segment.segment_type == SegmentType.MANUAL.value:
        member_ids = (
            db.execute(
                select(CustomerSegment.customer_id).where(
                    CustomerSegment.segment_id == segment.id
                )
            )
            .scalars()
            .all()
        )
        return load_customer_views(db, list(member_ids))

    views = views if views is not None else load_customer_views(db)
    now = utcnow()
    rule = segment.rule_definition or {}
    return [v for v in views if evaluate(rule, v, now=now)]


def preview_rule(db: Session, rule: dict, *, limit: int = 10) -> dict:
    """Count and sample the customers a candidate rule would match."""
    validate_rule(rule)
    views = load_customer_views(db)
    now = utcnow()
    matches = [v for v in views if evaluate(rule, v, now=now)]
    return {
        "total_customers": len(views),
        "matched_customers": len(matches),
        "match_rate": round(len(matches) / len(views), 4) if views else 0.0,
        "sample": [_sample_row(v) for v in matches[:limit]],
    }


def _sample_row(view: dict) -> dict:
    return {
        "id": view["id"],
        "external_id": view["external_id"],
        "full_name": view["full_name"],
        "email": view.get("email"),
        "lifecycle_stage": view.get("lifecycle_stage"),
        "lifetime_revenue": view.get("lifetime_revenue", 0.0),
        "days_since_last_order": view.get("days_since_last_order"),
        "churn_score": view.get("churn_score", 0.0),
        "churn_risk_band": view.get("churn_risk_band"),
    }


def refresh_segment_membership(
    db: Session, segment: Segment, *, views: list[dict] | None = None, commit: bool = True
) -> int:
    """Recompute a dynamic segment's membership table. Returns member count."""
    if segment.segment_type == SegmentType.MANUAL.value:
        count = db.execute(
            select(CustomerSegment.customer_id).where(CustomerSegment.segment_id == segment.id)
        ).all()
        segment.member_count = len(count)
        segment.last_evaluated_at = utcnow()
        if commit:
            db.commit()
        return segment.member_count

    matches = evaluate_segment(db, segment, views=views)
    matched_ids = {v["id"] for v in matches}

    existing_ids = set(
        db.execute(
            select(CustomerSegment.customer_id).where(CustomerSegment.segment_id == segment.id)
        )
        .scalars()
        .all()
    )

    to_add = matched_ids - existing_ids
    to_remove = existing_ids - matched_ids

    if to_remove:
        db.execute(
            delete(CustomerSegment).where(
                CustomerSegment.segment_id == segment.id,
                CustomerSegment.customer_id.in_(to_remove),
            )
        )
    for customer_id in to_add:
        db.add(
            CustomerSegment(
                segment_id=segment.id, customer_id=customer_id, source="dynamic"
            )
        )

    segment.member_count = len(matched_ids)
    segment.last_evaluated_at = utcnow()
    if commit:
        db.commit()
    return segment.member_count


def refresh_all_segments(db: Session) -> dict:
    """Recompute every active dynamic segment from one shared customer snapshot."""
    segments = (
        db.execute(select(Segment).where(Segment.status == SegmentStatus.ACTIVE.value))
        .scalars()
        .all()
    )
    views = load_customer_views(db)
    results = {}
    for segment in segments:
        results[segment.name] = refresh_segment_membership(
            db, segment, views=views, commit=False
        )
    db.commit()
    return results


# --------------------------------------------------------------------------
# Default system segments
# --------------------------------------------------------------------------
DEFAULT_SEGMENTS: list[dict] = [
    {
        "name": "New Customers",
        "description": "Signed up or first ordered within the last 30 days.",
        "rule": {"field": "lifecycle_stage", "operator": "eq", "value": "NEW"},
    },
    {
        "name": "Needs Second Order",
        "description": "Exactly one completed order — the highest-leverage retention moment.",
        "rule": {
            "op": "AND",
            "conditions": [
                {"field": "completed_orders", "operator": "eq", "value": 1},
                {"field": "is_suppressed", "operator": "is_false"},
            ],
        },
    },
    {
        "name": "Regulars",
        "description": "Established repeat customers ordering on cadence.",
        "rule": {"field": "lifecycle_stage", "operator": "eq", "value": "REGULAR"},
    },
    {
        "name": "VIP Customers",
        "description": "Highest lifetime value customers still ordering.",
        "rule": {"field": "lifecycle_stage", "operator": "eq", "value": "VIP"},
    },
    {
        "name": "High Value Customers",
        "description": "Above the high-value revenue threshold and still active.",
        "rule": {
            "field": "lifecycle_stage",
            "operator": "in",
            "value": ["HIGH_VALUE", "VIP"],
        },
    },
    {
        "name": "At Risk",
        "description": "Overdue against their own purchase cadence.",
        "rule": {"field": "lifecycle_stage", "operator": "eq", "value": "AT_RISK"},
    },
    {
        "name": "High Value At Risk",
        "description": "Customers worth $500+ who are lapsing. The priority save list.",
        "rule": {
            "op": "AND",
            "conditions": [
                {"field": "lifecycle_stage", "operator": "in", "value": ["AT_RISK", "DORMANT"]},
                {"field": "lifetime_revenue", "operator": "gte", "value": 500},
                {"field": "is_suppressed", "operator": "is_false"},
            ],
        },
    },
    {
        "name": "Dormant",
        "description": "Well past their expected reorder point but not yet written off.",
        "rule": {"field": "lifecycle_stage", "operator": "eq", "value": "DORMANT"},
    },
    {
        "name": "Churned",
        "description": "Beyond the churn threshold; win-back only.",
        "rule": {"field": "lifecycle_stage", "operator": "eq", "value": "CHURNED"},
    },
    {
        "name": "Recently Reactivated",
        "description": "Returned to ordering after a long gap.",
        "rule": {"field": "lifecycle_stage", "operator": "eq", "value": "REACTIVATED"},
    },
    {
        "name": "Critical Churn Risk",
        "description": "Churn score of 70 or above.",
        "rule": {"field": "churn_score", "operator": "gte", "value": 70},
    },
    {
        "name": "Email Contactable",
        "description": "Consented to marketing and email, not suppressed.",
        "rule": {
            "op": "AND",
            "conditions": [
                {"field": "marketing_consent", "operator": "is_true"},
                {"field": "email_consent", "operator": "is_true"},
                {"field": "is_suppressed", "operator": "is_false"},
            ],
        },
    },
    {
        "name": "Wine Drinkers",
        "description": "Wine is among their top purchased categories.",
        "rule": {
            "field": "preferred_categories",
            "operator": "contains_any",
            "value": ["Wine"],
        },
    },
    {
        "name": "Beer Drinkers",
        "description": "Beer is among their top purchased categories.",
        "rule": {
            "field": "preferred_categories",
            "operator": "contains_any",
            "value": ["Beer"],
        },
    },
]


def ensure_default_segments(db: Session) -> int:
    """Create the built-in system segments if they do not already exist."""
    created = 0
    for spec in DEFAULT_SEGMENTS:
        exists = db.execute(
            select(Segment.id).where(Segment.name == spec["name"])
        ).first()
        if exists:
            continue
        validate_rule(spec["rule"])
        db.add(
            Segment(
                name=spec["name"],
                description=spec["description"],
                segment_type=SegmentType.DYNAMIC.value,
                status=SegmentStatus.ACTIVE.value,
                is_system=True,
                rule_definition=spec["rule"],
            )
        )
        created += 1
    db.commit()
    return created
