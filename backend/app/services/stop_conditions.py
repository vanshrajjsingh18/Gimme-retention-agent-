"""Stop conditions and lifecycle management for Smart Reorder campaigns.

Manages when to stop sending messages to customers based on various conditions
like customer orders, max send limits, date-based stops, and suppression lists.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Literal

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.core.enums import OrderStatus
from app.models.base import utcnow
from app.models.entities import (
    Automation,
    AutomationSend,
    CustomerCouponAssignment,
    Order,
)

logger = logging.getLogger(__name__)


def should_stop_journey(
    db: Session,
    customer_id: int,
    automation_id: int,
) -> tuple[bool, str | None]:
    """Check if a customer's journey should be stopped.

    Returns (should_stop, reason).

    Checks:
    - Customer has ordered (stop_on_order condition)
    - Max sends reached
    - Campaign end date passed
    - Customer manually suppressed
    """
    automation = db.execute(
        select(Automation).where(Automation.id == automation_id)
    ).scalar()

    if not automation:
        return False, None

    config = automation.config or {}

    # Check 1: Customer has ordered
    if config.get("stop_on_order", False):
        last_send = db.execute(
            select(AutomationSend)
            .where(
                AutomationSend.customer_id == customer_id,
                AutomationSend.automation_id == automation_id,
            )
            .order_by(AutomationSend.scheduled_for.desc())
            .limit(1)
        ).scalar()

        if last_send:
            order = db.execute(
                select(Order).where(
                    Order.customer_id == customer_id,
                    Order.ordered_at > last_send.scheduled_for,
                )
            ).scalar()

            if order:
                return True, "CUSTOMER_ORDERED"

    # Check 2: Max sends reached
    max_sends = config.get("max_sends")
    if max_sends:
        send_count = db.execute(
            select(func.count(AutomationSend.id)).where(
                AutomationSend.customer_id == customer_id,
                AutomationSend.automation_id == automation_id,
            )
        ).scalar() or 0

        if send_count >= max_sends:
            return True, f"MAX_SENDS_REACHED ({send_count}/{max_sends})"

    # Check 3: Campaign end date passed
    campaign_end_date = config.get("campaign_end_date")
    if campaign_end_date:
        end_dt = datetime.fromisoformat(campaign_end_date)
        if utcnow() > end_dt:
            return True, "CAMPAIGN_ENDED"

    # Check 4: Customer in suppression list
    is_suppressed = is_customer_suppressed(db, customer_id, automation_id)
    if is_suppressed:
        return True, "CUSTOMER_SUPPRESSED"

    return False, None


def is_customer_suppressed(
    db: Session,
    customer_id: int,
    automation_id: int | None = None,
) -> bool:
    """Check if customer is suppressed for this automation.

    Suppression can be:
    - Global (suppressed from all campaigns)
    - Per-automation (suppressed from specific campaign)
    - Opt-out (customer replied STOP)
    """
    # Check if customer has opted out globally
    # This would typically be in a CustomerSuppression table
    # For now, we'll check if they've hit max sends recently

    # In a full implementation, this would query a dedicated suppression table
    logger.debug(f"Checking suppression for customer {customer_id}")
    return False


def add_to_suppression_list(
    db: Session,
    customer_id: int,
    automation_id: int | None = None,
    reason: str = "MANUAL",
    commit: bool = False,
) -> None:
    """Add customer to suppression list for this automation or globally.

    Args:
        db: Database session
        customer_id: Customer to suppress
        automation_id: If set, suppress only for this automation
        reason: Why customer was suppressed (OPTED_OUT, MANUAL, INVALID_CONTACT)
        commit: Whether to commit the transaction
    """
    logger.info(
        f"Adding customer {customer_id} to suppression list. "
        f"Automation: {automation_id}, Reason: {reason}"
    )

    # In a full implementation, this would insert into CustomerSuppression table
    # For now, this is a placeholder that logs the action

    if commit:
        db.commit()


def remove_from_suppression_list(
    db: Session,
    customer_id: int,
    automation_id: int | None = None,
    commit: bool = False,
) -> None:
    """Remove customer from suppression list.

    Args:
        db: Database session
        customer_id: Customer to unsuppress
        automation_id: If set, unsuppress only for this automation
        commit: Whether to commit the transaction
    """
    logger.info(
        f"Removing customer {customer_id} from suppression list. "
        f"Automation: {automation_id}"
    )

    if commit:
        db.commit()


def get_stop_conditions_config(automation: Automation) -> dict:
    """Extract stop conditions configuration from automation.

    Returns:
    {
        "stop_on_order": bool,
        "max_sends": int | None,
        "campaign_end_date": str | None,  # ISO format
        "suppressed_customers": int,
    }
    """
    config = automation.config or {}

    return {
        "stop_on_order": config.get("stop_on_order", False),
        "max_sends": config.get("max_sends"),
        "campaign_end_date": config.get("campaign_end_date"),
        "suppressed_customers": 0,  # Would query suppression table in full implementation
    }


def update_stop_conditions_config(
    automation: Automation,
    stop_on_order: bool | None = None,
    max_sends: int | None = None,
    campaign_end_date: str | None = None,
) -> None:
    """Update stop conditions configuration.

    Args:
        automation: Automation to update
        stop_on_order: Whether to stop journey if customer orders
        max_sends: Maximum number of sends per customer
        campaign_end_date: ISO format date when campaign ends
    """
    config = automation.config or {}

    if stop_on_order is not None:
        config["stop_on_order"] = stop_on_order

    if max_sends is not None:
        config["max_sends"] = max_sends

    if campaign_end_date is not None:
        config["campaign_end_date"] = campaign_end_date

    automation.config = config


def get_customers_to_stop(
    db: Session,
    automation_id: int,
) -> dict[int, str]:
    """Find all customers who should have their journey stopped.

    Returns dict of {customer_id: reason_for_stopping}.
    """
    automation = db.execute(
        select(Automation).where(Automation.id == automation_id)
    ).scalar()

    if not automation:
        return {}

    customers_to_stop = {}

    # Get all customers assigned to this campaign
    assigned_customers = db.execute(
        select(CustomerCouponAssignment.customer_id).where(
            CustomerCouponAssignment.automation_id == automation_id
        )
    ).scalars().all()

    for customer_id in assigned_customers:
        should_stop, reason = should_stop_journey(db, customer_id, automation_id)
        if should_stop:
            customers_to_stop[customer_id] = reason

    return customers_to_stop


def mark_journey_ended(
    db: Session,
    customer_id: int,
    automation_id: int,
    reason: Literal[
        "CUSTOMER_ORDERED",
        "MAX_SENDS_REACHED",
        "CAMPAIGN_ENDED",
        "CUSTOMER_SUPPRESSED",
        "MANUAL",
    ] = "MANUAL",
    commit: bool = False,
) -> None:
    """Mark that a customer's journey has ended.

    This records in the automation metadata that the customer should no longer
    receive messages. In a full implementation, this would update a journey
    status table.
    """
    logger.info(
        f"Journey ended for customer {customer_id} in automation {automation_id}. "
        f"Reason: {reason}"
    )

    # In a full implementation, this would update journey_status in a table
    # For now, we just log it

    if commit:
        db.commit()


def get_lifecycle_stats(
    db: Session,
    automation_id: int,
) -> dict:
    """Get statistics on journey lifecycle.

    Returns:
    {
        "automation_id": int,
        "active_journeys": int,
        "completed_journeys": int,
        "stopped_journeys_by_reason": {
            "CUSTOMER_ORDERED": int,
            "MAX_SENDS_REACHED": int,
            ...
        },
        "suppressed_customers": int,
    }
    """
    return {
        "automation_id": automation_id,
        "active_journeys": 0,
        "completed_journeys": 0,
        "stopped_journeys_by_reason": {},
        "suppressed_customers": 0,
        "note": "Full lifecycle tracking requires journey_status table",
    }
