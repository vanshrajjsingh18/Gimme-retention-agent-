"""Last-touch campaign attribution and reactivation detection.

When an order arrives:
  1. Find the most recent qualifying campaign touch inside the attribution
     window for that customer.
  2. Create an attribution record and roll the revenue up onto the campaign.
  3. Detect whether this order is a reactivation (a return after a long gap).
  4. Record a CAMPAIGN_CONVERSION event and, when applicable, a
     CUSTOMER_REACTIVATED event.

Attribution is idempotent per order: ``attribution_records`` is uniquely keyed
on ``order_id``, so reprocessing an order never double-counts revenue.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import EventType, LifecycleStage, OrderStatus, RecipientStatus
from app.models.base import utcnow
from app.models.entities import (
    AttributionRecord,
    Campaign,
    CampaignRecipient,
    CampaignVariant,
    CommunicationEvent,
    Customer,
    Order,
)
from app.services.events import make_idempotency_key, record_communication_event, record_customer_event
from app.services.lifecycle import DEFAULT_THRESHOLDS

logger = logging.getLogger(__name__)

DEFAULT_WINDOW_HOURS = 72
SUPPORTED_WINDOWS = [24, 48, 72, 168]

# Which events count as an attributable "touch", strongest first. An engaged
# interaction outranks a mere delivery when both fall inside the window.
TOUCH_PRIORITY: list[str] = [
    EventType.EMAIL_CLICKED.value,
    EventType.WHATSAPP_REPLIED.value,
    EventType.EMAIL_OPENED.value,
    EventType.WHATSAPP_READ.value,
    EventType.EMAIL_DELIVERED.value,
    EventType.SMS_DELIVERED.value,
    EventType.WHATSAPP_DELIVERED.value,
    EventType.EMAIL_SENT.value,
    EventType.SMS_SENT.value,
    EventType.WHATSAPP_SENT.value,
]


def attribute_order(
    db: Session,
    order: Order,
    *,
    window_hours: int | None = None,
    commit: bool = True,
) -> AttributionRecord | None:
    """Attribute one order to its last-touch campaign, if any qualifies."""
    if order.status != OrderStatus.COMPLETED.value:
        return None

    existing = db.execute(
        select(AttributionRecord).where(AttributionRecord.order_id == order.id)
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    window = window_hours or DEFAULT_WINDOW_HOURS
    cutoff = order.ordered_at - timedelta(hours=window)

    touches = (
        db.execute(
            select(CommunicationEvent)
            .where(
                CommunicationEvent.customer_id == order.customer_id,
                CommunicationEvent.campaign_id.is_not(None),
                CommunicationEvent.event_type.in_(TOUCH_PRIORITY),
                CommunicationEvent.occurred_at <= order.ordered_at,
                CommunicationEvent.occurred_at >= cutoff,
            )
            .order_by(CommunicationEvent.occurred_at.desc())
        )
        .scalars()
        .all()
    )
    if not touches:
        return None

    # Per-campaign the attribution window can differ; honour the campaign's own
    # setting where it is narrower than the default.
    qualifying: list[tuple[CommunicationEvent, Campaign]] = []
    for touch in touches:
        campaign = db.get(Campaign, touch.campaign_id)
        if campaign is None:
            continue
        campaign_window = campaign.attribution_window_hours or window
        if (order.ordered_at - touch.occurred_at) <= timedelta(hours=campaign_window):
            qualifying.append((touch, campaign))

    if not qualifying:
        return None

    # Last touch = most recent; ties broken by engagement strength.
    def sort_key(pair: tuple[CommunicationEvent, Campaign]):
        event, _ = pair
        priority = TOUCH_PRIORITY.index(event.event_type)
        return (event.occurred_at, -priority)

    touch, campaign = max(qualifying, key=sort_key)

    hours_since = (order.ordered_at - touch.occurred_at).total_seconds() / 3600.0
    is_reactivation = detect_reactivation(db, order)

    record = AttributionRecord(
        order_id=order.id,
        customer_id=order.customer_id,
        campaign_id=campaign.id,
        message_id=touch.message_id,
        touch_event_id=touch.id,
        model="LAST_TOUCH",
        window_hours=campaign.attribution_window_hours or window,
        hours_since_touch=round(hours_since, 2),
        revenue=order.total_amount,
        is_reactivation=is_reactivation,
    )
    db.add(record)

    campaign.conversions = (campaign.conversions or 0) + 1
    campaign.attributed_revenue = round(
        (campaign.attributed_revenue or 0.0) + order.total_amount, 2
    )

    recipient = db.execute(
        select(CampaignRecipient).where(
            CampaignRecipient.campaign_id == campaign.id,
            CampaignRecipient.customer_id == order.customer_id,
        )
    ).scalar_one_or_none()
    if recipient is not None:
        recipient.status = RecipientStatus.CONVERTED.value
        recipient.converted_at = order.ordered_at
        if recipient.variant_id:
            variant = db.get(CampaignVariant, recipient.variant_id)
            if variant is not None:
                variant.conversions = (variant.conversions or 0) + 1
                variant.attributed_revenue = round(
                    (variant.attributed_revenue or 0.0) + order.total_amount, 2
                )

    record_communication_event(
        db,
        event_type=EventType.CAMPAIGN_CONVERSION,
        customer_id=order.customer_id,
        campaign_id=campaign.id,
        message_id=touch.message_id,
        channel=campaign.channel,
        provider=touch.provider,
        occurred_at=order.ordered_at,
        is_simulated=touch.is_simulated,
        payload={
            "order_id": order.id,
            "order_external_id": order.external_id,
            "revenue": order.total_amount,
            "hours_since_touch": round(hours_since, 2),
            "is_reactivation": is_reactivation,
        },
        idempotency_key=make_idempotency_key("conversion", order.id),
    )

    if commit:
        db.commit()
        db.refresh(record)
    return record


def detect_reactivation(
    db: Session, order: Order, *, gap_days: int = DEFAULT_THRESHOLDS.reactivation_gap_days
) -> bool:
    """True when this order follows a gap of at least ``gap_days``."""
    previous = db.execute(
        select(Order.ordered_at)
        .where(
            Order.customer_id == order.customer_id,
            Order.status == OrderStatus.COMPLETED.value,
            Order.ordered_at < order.ordered_at,
        )
        .order_by(Order.ordered_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if previous is None:
        return False
    return (order.ordered_at - previous).days >= gap_days


def process_new_order(
    db: Session, order: Order, *, commit: bool = True
) -> dict:
    """Everything that must happen when a completed order lands.

    Records the order event, detects reactivation, attributes the order to a
    campaign, and refreshes the customer's intelligence so their lifecycle,
    churn score and recommendation reflect the new purchase.
    """
    from app.services.intelligence import refresh_customer  # local: avoids a cycle

    customer = db.get(Customer, order.customer_id)
    result: dict = {
        "order_id": order.id,
        "attributed": False,
        "campaign_id": None,
        "attributed_revenue": 0.0,
        "reactivated": False,
        "previous_lifecycle_stage": customer.lifecycle_stage if customer else None,
        "lifecycle_stage": None,
    }
    if customer is None:
        return result

    record_customer_event(
        db,
        customer_id=customer.id,
        event_type=(
            EventType.ORDER_COMPLETED
            if order.status == OrderStatus.COMPLETED.value
            else EventType.ORDER_CREATED
        ),
        occurred_at=order.ordered_at,
        source="ingestion",
        payload={
            "order_external_id": order.external_id,
            "total_amount": order.total_amount,
            "status": order.status,
        },
        idempotency_key=make_idempotency_key("order", order.id, order.status),
    )

    was_reactivation = (
        order.status == OrderStatus.COMPLETED.value and detect_reactivation(db, order)
    )
    if was_reactivation:
        record_customer_event(
            db,
            customer_id=customer.id,
            event_type=EventType.CUSTOMER_REACTIVATED,
            occurred_at=order.ordered_at,
            source="attribution",
            payload={"order_external_id": order.external_id},
            idempotency_key=make_idempotency_key("reactivation", order.id),
        )
        result["reactivated"] = True

    attribution = attribute_order(db, order, commit=False)
    if attribution is not None:
        result.update(
            attributed=True,
            campaign_id=attribution.campaign_id,
            attributed_revenue=attribution.revenue,
        )

    db.commit()

    # Recompute lifecycle/churn/NBA now that the order is visible.
    intel = refresh_customer(db, customer)
    result["lifecycle_stage"] = intel.lifecycle.stage.value
    result["churn_score"] = intel.churn.score
    result["recommended_action"] = intel.recommendation.action.value
    return result


def backfill_attribution(db: Session, *, since: datetime | None = None) -> dict:
    """Attribute any completed orders that do not yet have a record."""
    stmt = select(Order).where(Order.status == OrderStatus.COMPLETED.value)
    if since is not None:
        stmt = stmt.where(Order.ordered_at >= since)
    orders = db.execute(stmt.order_by(Order.ordered_at)).scalars().all()

    attributed = 0
    revenue = 0.0
    reactivations = 0
    for order in orders:
        record = attribute_order(db, order, commit=False)
        if record is not None:
            attributed += 1
            revenue += record.revenue
            if record.is_reactivation:
                reactivations += 1
    db.commit()
    return {
        "orders_examined": len(orders),
        "orders_attributed": attributed,
        "attributed_revenue": round(revenue, 2),
        "reactivations": reactivations,
    }
