"""Audience preview and eligibility analysis for Smart Reorder campaigns.

Shows detailed breakdown of which customers match the automation's audience
and why some customers might be excluded.
"""
from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.automations.cohort import resolve_audience
from app.automations.nudge import config_of, plan_for
from app.models.base import utcnow
from app.models.entities import (
    Automation,
    AutomationEnrollment,
    Customer,
)

logger = logging.getLogger(__name__)


def preview_audience(
    db: Session,
    automation: Automation,
) -> dict:
    """Generate detailed audience preview for an automation.

    Returns a breakdown of:
    - Total customers matching the segment/cohort
    - Customers eligible for Smart Reorder (order history, confidence)
    - Various exclusion reasons and counts
    """
    now = utcnow()

    # Get audience from segment/cohort
    audience_customer_ids = resolve_audience(db, automation, now=now)
    total_in_audience = len(audience_customer_ids)

    if not audience_customer_ids:
        return {
            "automation_id": automation.id,
            "audience_size": 0,
            "eligible_customers": 0,
            "exclusions": {
                "no_audience_match": total_in_audience,
            },
            "final_eligible": 0,
        }

    # Load customer data for audience
    customers = {
        c.id: c
        for c in db.execute(
            select(Customer).where(Customer.id.in_(audience_customer_ids))
        )
        .scalars()
        .all()
    }

    # Get already enrolled customers
    enrolled = set(
        db.execute(
            select(AutomationEnrollment.customer_id).where(
                AutomationEnrollment.automation_id == automation.id
            )
        )
        .scalars()
        .all()
    )

    # Check Smart Reorder eligibility for each customer
    config = config_of(automation)
    exclusions = {
        "no_marketing_consent": 0,
        "suppressed": 0,
        "no_phone_number": 0,
        "insufficient_order_history": 0,
        "low_confidence": 0,
        "already_enrolled": 0,
    }

    eligible_count = 0

    for customer_id in audience_customer_ids:
        customer = customers.get(customer_id)

        if customer_id in enrolled:
            exclusions["already_enrolled"] += 1
            continue

        if not customer:
            exclusions["no_marketing_consent"] += 1
            continue

        if not customer.marketing_consent:
            exclusions["no_marketing_consent"] += 1
            continue

        if customer.is_suppressed:
            exclusions["suppressed"] += 1
            continue

        if not customer.phone_number:
            exclusions["no_phone_number"] += 1
            continue

        # Check order history and confidence
        plan = plan_for(db, customer_id, config, now=now)
        if not plan.has_plan:
            exclusions["insufficient_order_history"] += 1
            continue

        if plan.prediction.overall_confidence < config["min_confidence"]:
            exclusions["low_confidence"] += 1
            continue

        eligible_count += 1

    return {
        "automation_id": automation.id,
        "audience_size": total_in_audience,
        "eligible_customers": eligible_count,
        "exclusions": exclusions,
        "final_eligible": eligible_count,
        "configuration": {
            "min_confidence": config["min_confidence"],
            "min_gap_days": config["min_gap_days"],
        },
    }


def get_audience_breakdown(
    db: Session,
    automation: Automation,
) -> dict:
    """Get a high-level breakdown of audience eligibility.

    Useful for displaying in the UI campaign builder.
    """
    preview = preview_audience(db, automation)

    return {
        "automation_id": automation.id,
        "total_matched": preview["audience_size"],
        "smart_reorder_eligible": preview["final_eligible"],
        "exclusion_summary": preview["exclusions"],
        "eligibility_rate": (
            round(preview["final_eligible"] / preview["audience_size"], 4)
            if preview["audience_size"] > 0
            else 0.0
        ),
    }
