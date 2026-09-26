"""Validation for Smart Reorder campaign configuration.

Ensures frequency rules, touchpoint rules, and coupon allocations are
properly configured before campaign activation.
"""
from __future__ import annotations

import logging
from typing import Literal

from sqlalchemy.orm import Session

from app.models.entities import Automation, CouponVariant

logger = logging.getLogger(__name__)

ValidationError = Literal[
    "ALLOCATION_PERCENTAGE_MISMATCH",
    "NO_COUPONS_CONFIGURED",
    "INVALID_FREQUENCY_WINDOW",
    "INVALID_ALLOCATION_PERCENTAGE",
]


def validate_coupon_allocation(db: Session, automation: Automation) -> list[str]:
    """Validate that coupon allocations sum to 100%.

    Returns empty list if valid, list of error messages if not.
    """
    errors = []

    variants = db.execute(
        CouponVariant.__table__.select().where(
            CouponVariant.automation_id == automation.id
        )
    ).fetchall()

    if not variants:
        return errors  # No coupons configured is fine

    total_allocation = sum(v.allocation_percentage for v in variants)

    if total_allocation != 100.0:
        errors.append(
            f"Coupon allocation percentages sum to {total_allocation}%, "
            f"but must equal 100%"
        )

    # Check for invalid percentages
    for variant in variants:
        if variant.allocation_percentage < 0 or variant.allocation_percentage > 100:
            errors.append(
                f"Coupon '{variant.coupon_code}' has invalid "
                f"allocation percentage: {variant.allocation_percentage}%"
            )

    return errors


def validate_frequency_rules(automation: Automation) -> list[str]:
    """Validate frequency rule configuration.

    Returns empty list if valid, list of error messages if not.
    """
    errors = []
    config = automation.config or {}
    frequency = config.get("frequency_rules", {})

    if not frequency:
        return errors  # No frequency rules is fine

    # Validate quiet hours if present
    if "quiet_hours" in frequency:
        quiet_hours = frequency["quiet_hours"]
        if "start" in quiet_hours or "end" in quiet_hours:
            try:
                if "start" in quiet_hours:
                    _validate_time_format(quiet_hours["start"])
                if "end" in quiet_hours:
                    _validate_time_format(quiet_hours["end"])
            except ValueError as e:
                errors.append(f"Invalid quiet hours format: {e}")

    # Validate max frequency
    max_freq = frequency.get("max_frequency_days")
    if max_freq is not None:
        if max_freq < 1:
            errors.append("max_frequency_days must be >= 1")

    # Validate max touchpoints
    max_touch = frequency.get("max_total_touchpoints")
    if max_touch is not None:
        if max_touch < 1:
            errors.append("max_total_touchpoints must be >= 1")

    return errors


def validate_touchpoint_rules(automation: Automation) -> list[str]:
    """Validate multi-touch automation configuration.

    Returns empty list if valid, list of error messages if not.
    """
    errors = []
    config = automation.config or {}
    touchpoints = config.get("touchpoints", [])

    if not touchpoints:
        return errors  # No touchpoints is fine (single message)

    if not isinstance(touchpoints, list):
        errors.append("touchpoints must be a list")
        return errors

    for i, tp in enumerate(touchpoints):
        if not isinstance(tp, dict):
            errors.append(f"Touchpoint {i} is not a dict")
            continue

        # Each touchpoint must have position
        if "position" not in tp:
            errors.append(f"Touchpoint {i} missing 'position'")

        # Must have either timing_minutes_before or timing_days_after
        if "timing_minutes_before" not in tp and "timing_days_after" not in tp:
            errors.append(
                f"Touchpoint {i} must have either "
                f"'timing_minutes_before' or 'timing_days_after'"
            )

    return errors


def validate_automation_config(
    db: Session,
    automation: Automation,
) -> dict[str, list[str]]:
    """Run all validation checks on automation configuration.

    Returns dict of validation_type -> list of error messages.
    Empty list means no errors for that category.
    """
    return {
        "coupon_allocation": validate_coupon_allocation(db, automation),
        "frequency_rules": validate_frequency_rules(automation),
        "touchpoint_rules": validate_touchpoint_rules(automation),
    }


def is_configuration_valid(db: Session, automation: Automation) -> bool:
    """Check if configuration is valid for activation.

    Returns True only if all validation checks pass.
    """
    errors = validate_automation_config(db, automation)
    return all(not errors[key] for key in errors)


def _validate_time_format(time_str: str) -> None:
    """Validate that time string is in HH:MM format."""
    try:
        parts = time_str.split(":")
        if len(parts) != 2:
            raise ValueError(f"Invalid time format: {time_str}")
        hour = int(parts[0])
        minute = int(parts[1])
        if not (0 <= hour < 24 and 0 <= minute < 60):
            raise ValueError(f"Invalid time values: {time_str}")
    except (ValueError, IndexError) as e:
        raise ValueError(f"Invalid time format: {time_str}") from e
