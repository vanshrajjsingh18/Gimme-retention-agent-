"""Smart Reorder coupon assignment and allocation logic.

Handles deterministic coupon variant assignment for customers based on
configured allocation percentages. Assignments are persistent and stored
in the database to ensure the same customer always receives the same coupon
for a given campaign.
"""
from __future__ import annotations

import logging
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.entities import (
    Automation,
    CouponVariant,
    CustomerCouponAssignment,
)

logger = logging.getLogger(__name__)

AllocationMethod = Literal["random", "equal", "manual"]


def get_or_assign_coupon(
    db: Session,
    customer_id: int,
    automation_id: int,
) -> str | None:
    """Get existing coupon assignment or assign a new one.

    Returns the coupon code assigned to this customer for this automation,
    or None if no variants are configured or customer is not eligible.

    Assignments are deterministic and persistent — a customer always gets
    the same coupon for a given automation.
    """
    # Check for existing assignment
    existing = db.execute(
        select(CustomerCouponAssignment).where(
            CustomerCouponAssignment.customer_id == customer_id,
            CustomerCouponAssignment.automation_id == automation_id,
        )
    ).scalar_one_or_none()

    if existing:
        return existing.coupon_code

    # Get automation and its variants
    automation = db.get(Automation, automation_id)
    if not automation:
        return None

    # Get active variants sorted by position
    variants = db.execute(
        select(CouponVariant).where(
            CouponVariant.automation_id == automation_id,
            CouponVariant.enabled.is_(True),
        )
        .order_by(CouponVariant.position)
    ).scalars().all()

    if not variants:
        return None

    # Assign based on allocation percentages
    selected_variant = _select_variant_by_allocation(customer_id, variants)

    if not selected_variant:
        logger.warning(
            "Could not select variant for customer %s, automation %s",
            customer_id,
            automation_id,
        )
        return None

    # Store assignment
    assignment = CustomerCouponAssignment(
        customer_id=customer_id,
        automation_id=automation_id,
        coupon_code=selected_variant.coupon_code,
        variant_id=selected_variant.id,
    )
    db.add(assignment)
    db.flush()

    return assignment.coupon_code


def _select_variant_by_allocation(
    customer_id: int,
    variants: list[CouponVariant],
) -> CouponVariant | None:
    """Select variant based on allocation percentages using customer_id as seed.

    Uses deterministic assignment: the same customer_id always gets the same
    variant, but distribution across variants matches configured percentages.

    This is done by mapping customer_id to a position in [0, 100) and finding
    which variant that position falls into.
    """
    if not variants:
        return None

    # Use customer_id as deterministic seed (deterministic hash)
    # Map customer_id to position in [0, 100)
    position = (customer_id * 1103515245 + 12345) % 100

    cumulative = 0.0
    for variant in variants:
        cumulative += variant.allocation_percentage
        if position < cumulative:
            return variant

    # Fallback to last variant (handles rounding issues)
    return variants[-1]


def get_coupon_assignment(
    db: Session,
    customer_id: int,
    automation_id: int,
) -> CustomerCouponAssignment | None:
    """Get the stored coupon assignment for a customer."""
    return db.execute(
        select(CustomerCouponAssignment).where(
            CustomerCouponAssignment.customer_id == customer_id,
            CustomerCouponAssignment.automation_id == automation_id,
        )
    ).scalar_one_or_none()


def get_coupon_stats(
    db: Session,
    automation_id: int,
) -> dict:
    """Get coupon assignment and performance statistics."""
    # Count assignments by coupon code
    assignment_counts = dict(
        db.execute(
            select(
                CustomerCouponAssignment.coupon_code,
                func.count(CustomerCouponAssignment.id),
            )
            .where(CustomerCouponAssignment.automation_id == automation_id)
            .group_by(CustomerCouponAssignment.coupon_code)
        ).all()
    )

    return {
        "automation_id": automation_id,
        "assignments_by_coupon": assignment_counts,
        "total_assignments": sum(assignment_counts.values()),
    }
