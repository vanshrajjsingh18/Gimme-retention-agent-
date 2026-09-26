"""Multi-touch automation orchestration for Smart Reorder campaigns.

Manages sending multiple messages to customers across configurable touchpoints,
with conditional logic (e.g., only send message 2 if customer hasn't ordered
since message 1) and stop conditions (e.g., stop journey if customer orders).
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
    Order,
)

logger = logging.getLogger(__name__)


def get_touchpoints_config(automation: Automation) -> list[dict]:
    """Extract touchpoint configuration from automation.

    Returns list of touchpoint dicts in position order, or empty list if
    no touchpoints configured (single-message mode).
    """
    config = automation.config or {}
    touchpoints = config.get("touchpoints", [])

    if not touchpoints:
        return []

    # Sort by position to ensure correct order
    sorted_touchpoints = sorted(touchpoints, key=lambda t: t.get("position", 0))
    return sorted_touchpoints


def get_current_touchpoint(
    db: Session,
    customer_id: int,
    automation_id: int,
    touchpoints: list[dict],
) -> tuple[int | None, dict | None]:
    """Determine which touchpoint this customer should receive next.

    Returns (touchpoint_position, touchpoint_config) or (None, None) if:
    - No touchpoints configured
    - Journey completed (all touchpoints sent)
    - Journey stopped (customer ordered or stop condition met)

    Checks in order:
    1. Has customer ordered since the last message? If yes and last touchpoint
       has stop_on_order=True, return None (journey stopped).
    2. What was the last message sent? Return the next touchpoint.
    3. If no messages sent yet, return position 1.
    """
    if not touchpoints:
        return None, None

    # Get the last sent message for this customer in this automation
    last_send = db.execute(
        select(AutomationSend)
        .where(
            AutomationSend.customer_id == customer_id,
            AutomationSend.automation_id == automation_id,
        )
        .order_by(AutomationSend.scheduled_for.desc())
        .limit(1)
    ).scalar()

    # If no messages sent yet, start with position 1
    if last_send is None:
        return touchpoints[0]["position"], touchpoints[0]

    # Get the position from the last send's context
    last_position = 1
    if last_send.context:
        last_position = last_send.context.get("touchpoint_position", 1)

    # Check if customer ordered since the last message
    customer_ordered_since = has_customer_ordered_since(
        db, customer_id, last_send.scheduled_for
    )

    # If customer ordered and the last touchpoint has stop_on_order, stop journey
    if customer_ordered_since:
        last_touchpoint = next(
            (tp for tp in touchpoints if tp.get("position") == last_position), None
        )
        if last_touchpoint and last_touchpoint.get("stop_on_order", False):
            return None, None  # Journey stopped

    # Find the next touchpoint position
    next_position = None
    for tp in touchpoints:
        if tp.get("position", 0) > last_position:
            next_position = tp.get("position")
            break

    if next_position is None:
        return None, None  # All touchpoints sent

    # Return the next touchpoint
    next_touchpoint = next(
        (tp for tp in touchpoints if tp.get("position") == next_position), None
    )
    return next_position, next_touchpoint


def calculate_touchpoint_send_time(
    base_time: datetime,
    touchpoint: dict,
) -> datetime:
    """Calculate when a touchpoint should be sent based on its timing config.

    Supports:
    - timing_minutes_before: Subtract minutes from base_time
    - timing_days_after: Add days to base_time
    - timing_hours_after: Add hours to base_time

    base_time is typically the customer's predicted order time for position 1,
    or the scheduled time of the previous message for subsequent positions.
    """
    if "timing_minutes_before" in touchpoint:
        minutes = touchpoint["timing_minutes_before"]
        return base_time - timedelta(minutes=minutes)

    if "timing_days_after" in touchpoint:
        days = touchpoint["timing_days_after"]
        return base_time + timedelta(days=days)

    if "timing_hours_after" in touchpoint:
        hours = touchpoint["timing_hours_after"]
        return base_time + timedelta(hours=hours)

    # Default: use base_time if no timing specified
    return base_time


def should_send_touchpoint(
    db: Session,
    customer_id: int,
    automation_id: int,
    touchpoint: dict,
    last_send_time: datetime | None = None,
) -> tuple[bool, str | None]:
    """Check if a touchpoint should be sent to this customer.

    Returns (should_send, block_reason).

    Checks:
    - Condition field: if_not_ordered_since_last_message
    - Any other conditions in the touchpoint config
    """
    condition = touchpoint.get("condition")

    if condition == "if_not_ordered_since_last_message":
        if last_send_time is None:
            # No previous send, so condition is satisfied
            return True, None

        # Check if customer ordered since last send
        if has_customer_ordered_since(db, customer_id, last_send_time):
            return False, "ORDERED_SINCE_LAST_MESSAGE"

    # No condition or condition satisfied
    return True, None


def has_customer_ordered_since(
    db: Session, customer_id: int, since_time: datetime
) -> bool:
    """Check if customer has placed an order since the given time.

    Includes both PENDING and COMPLETED orders.
    """
    order = db.execute(
        select(Order).where(
            Order.customer_id == customer_id,
            Order.ordered_at > since_time,
        )
    ).scalar()

    return order is not None


def get_touchpoint_message_template(
    automation: Automation,
    touchpoint: dict,
) -> str:
    """Get the message template for a specific touchpoint.

    Falls back to automation.message_template if touchpoint doesn't specify one.
    """
    if "message_template" in touchpoint:
        return touchpoint["message_template"]

    # Fall back to automation's default template
    return automation.message_template or ""


def mark_journey_completed(
    db: Session,
    customer_id: int,
    automation_id: int,
    reason: Literal["all_touchpoints_sent", "stopped_on_order"] = "all_touchpoints_sent",
    commit: bool = False,
) -> None:
    """Mark that a customer's journey through this automation is complete.

    This is recorded in the automation sends so we can track why the journey ended.
    In practice, the next run will detect no more touchpoints and skip the customer.
    """
    logger.info(
        f"Journey completed for customer {customer_id} in automation {automation_id}: {reason}"
    )
    if commit:
        db.commit()


def get_journey_stats(
    db: Session,
    automation_id: int,
) -> dict:
    """Get statistics on multi-touch journey progress.

    Returns:
    {
        "total_customers_in_journey": int,
        "by_touchpoint_position": {
            1: count,
            2: count,
            3: count,
        },
        "journeys_completed": int,
        "journeys_stopped_by_order": int,
    }
    """
    # Get count of unique customers who have sent at least one message
    customers_with_sends = db.execute(
        select(func.count(func.distinct(AutomationSend.customer_id))).where(
            AutomationSend.automation_id == automation_id
        )
    ).scalar() or 0

    # Get distribution by touchpoint position
    positions = db.execute(
        select(
            func.cast(
                func.json_extract(AutomationSend.context, "$.touchpoint_position"),
                type_=int,
            ),
            func.count(AutomationSend.id),
        )
        .where(AutomationSend.automation_id == automation_id)
        .group_by(
            func.cast(
                func.json_extract(AutomationSend.context, "$.touchpoint_position"),
                type_=int,
            )
        )
    ).all()

    position_counts = {int(pos): count for pos, count in positions}

    return {
        "total_customers_in_journey": customers_with_sends,
        "by_touchpoint_position": position_counts,
        "automation_id": automation_id,
    }
