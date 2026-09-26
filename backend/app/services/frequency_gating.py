"""Frequency gating and eligibility checking for Smart Reorder messages.

Ensures customers are not over-messaged by enforcing frequency rules,
quiet hours, and maximum touchpoint limits.
"""
from __future__ import annotations

import logging
from datetime import datetime, time

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.core.timezones import to_local, to_utc_naive
from app.models.base import utcnow
from app.models.entities import (
    Automation,
    AutomationSend,
    Customer,
)
from app.core.enums import SendStatus, SkipReason

logger = logging.getLogger(__name__)


def should_send_message(
    db: Session,
    customer_id: int,
    automation_id: int,
    touchpoint_position: int = 0,
    now: datetime | None = None,
) -> tuple[bool, str | None]:
    """Check if customer is eligible to receive a message.

    Returns:
        (should_send, reason_if_blocked)
            - (True, None) if customer should receive message
            - (False, "reason_code") if message should be skipped
    """
    now = now or utcnow()

    # Get customer and automation
    customer = db.get(Customer, customer_id)
    if not customer:
        return False, "CUSTOMER_NOT_FOUND"

    automation = db.get(Automation, automation_id)
    if not automation:
        return False, "AUTOMATION_NOT_FOUND"

    # Check fundamental eligibility
    if not customer.marketing_consent:
        return False, "NO_MARKETING_CONSENT"

    if customer.is_suppressed:
        return False, "SUPPRESSED"

    # Extract frequency rules from automation config
    config = automation.config or {}
    frequency = config.get("frequency_rules", {})

    # Check quiet hours if configured
    if "quiet_hours" in frequency:
        quiet = frequency["quiet_hours"]
        if _in_quiet_hours(customer, now, quiet):
            return False, "QUIET_HOURS"

    # Check max messages in time window
    max_freq_days = frequency.get("max_frequency_days")
    if max_freq_days and max_freq_days > 0:
        can_send, reason = _check_message_frequency(
            db, customer_id, automation_id, max_freq_days, now
        )
        if not can_send:
            return False, reason

    # Check max total touchpoints
    max_touchpoints = frequency.get("max_total_touchpoints")
    if max_touchpoints and max_touchpoints > 0:
        sent_count = _count_sent_messages(db, customer_id, automation_id, now)
        if sent_count >= max_touchpoints:
            return False, "MAX_TOUCHPOINTS_REACHED"

    return True, None


def _in_quiet_hours(
    customer: "Customer",
    now: datetime,
    quiet_hours: dict,
) -> bool:
    """Check if current time is within customer's quiet hours.

    Quiet hours are specified as:
    {
        "start": "19:00",  # 7 PM
        "end": "09:00"     # 9 AM next day
    }
    """
    if not quiet_hours or not quiet_hours.get("start") or not quiet_hours.get("end"):
        return False

    # Convert now to customer's local time
    local_now = to_local(now, customer.timezone or "UTC")
    current_time = local_now.time()

    start_str = quiet_hours["start"]
    end_str = quiet_hours["end"]

    try:
        start_time = datetime.strptime(start_str, "%H:%M").time()
        end_time = datetime.strptime(end_str, "%H:%M").time()
    except ValueError:
        logger.warning(f"Invalid quiet hours format: {start_str}, {end_str}")
        return False

    # Handle overnight quiet hours (e.g., 19:00 to 09:00)
    if start_time <= end_time:
        # Same day
        return start_time <= current_time <= end_time
    else:
        # Crosses midnight
        return current_time >= start_time or current_time <= end_time


def _check_message_frequency(
    db: Session,
    customer_id: int,
    automation_id: int,
    max_frequency_days: int,
    now: datetime,
) -> tuple[bool, str | None]:
    """Check if customer has space for another message within frequency window."""
    from datetime import timedelta

    cutoff_date = now - timedelta(days=max_frequency_days)

    # Count messages sent in the frequency window
    recent_count = db.execute(
        select(func.count(AutomationSend.id))
        .where(
            AutomationSend.customer_id == customer_id,
            AutomationSend.automation_id == automation_id,
            AutomationSend.status.in_(
                [SendStatus.SENT.value, SendStatus.DELIVERED.value]
            ),
            AutomationSend.sent_at >= cutoff_date,
        )
    ).scalar()

    if recent_count and recent_count > 0:
        return False, "FREQUENCY_LIMIT_REACHED"

    return True, None


def _count_sent_messages(
    db: Session,
    customer_id: int,
    automation_id: int,
    now: datetime,
) -> int:
    """Count how many messages have been sent to customer in this campaign."""
    return (
        db.execute(
            select(func.count(AutomationSend.id))
            .where(
                AutomationSend.customer_id == customer_id,
                AutomationSend.automation_id == automation_id,
                AutomationSend.status.in_(
                    [SendStatus.SENT.value, SendStatus.DELIVERED.value]
                ),
            )
        ).scalar()
        or 0
    )


def has_customer_ordered_since(
    db: Session,
    customer_id: int,
    since: datetime,
) -> bool:
    """Check if customer has placed an order since the given time."""
    from app.models.entities import Order

    count = db.execute(
        select(func.count(Order.id))
        .where(
            Order.customer_id == customer_id,
            Order.created_at >= since,
        )
    ).scalar()

    return count and count > 0
