"""Normalized event ingestion.

Every event carries an idempotency key so replaying a webhook, re-running a
simulation, or re-importing a file can never double-count. Writes use a
savepoint so a duplicate does not poison the surrounding transaction.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.enums import Channel, EventType
from app.models.base import utcnow
from app.models.entities import (
    Campaign,
    CampaignRecipient,
    CommunicationEvent,
    CustomerEvent,
    Message,
)

logger = logging.getLogger(__name__)


def make_idempotency_key(*parts) -> str:
    raw = "|".join(str(p) for p in parts if p is not None)
    return hashlib.sha256(raw.encode()).hexdigest()[:48]


def record_customer_event(
    db: Session,
    *,
    customer_id: int | None,
    event_type: EventType | str,
    occurred_at: datetime | None = None,
    source: str = "system",
    payload: dict | None = None,
    idempotency_key: str | None = None,
) -> CustomerEvent | None:
    """Persist a behavioural event. Returns None when it is a duplicate."""
    event_type_value = event_type.value if isinstance(event_type, EventType) else str(event_type)
    occurred_at = occurred_at or utcnow()
    key = idempotency_key or make_idempotency_key(
        customer_id, event_type_value, occurred_at.isoformat(), source
    )

    existing = db.execute(
        select(CustomerEvent.id).where(CustomerEvent.idempotency_key == key)
    ).first()
    if existing:
        return None

    event = CustomerEvent(
        customer_id=customer_id,
        event_type=event_type_value,
        occurred_at=occurred_at,
        source=source,
        payload=payload or {},
        idempotency_key=key,
    )
    try:
        with db.begin_nested():
            db.add(event)
        return event
    except IntegrityError:
        # Lost a race with a concurrent insert of the same key.
        return None


def record_communication_event(
    db: Session,
    *,
    event_type: EventType | str,
    customer_id: int | None = None,
    campaign_id: int | None = None,
    message_id: int | None = None,
    channel: Channel | str = Channel.EMAIL,
    provider: str = "mock",
    occurred_at: datetime | None = None,
    is_simulated: bool = False,
    payload: dict | None = None,
    idempotency_key: str | None = None,
) -> CommunicationEvent | None:
    """Persist a delivery/engagement event and update rollups.

    Returns None when the event is a duplicate, so callers can distinguish a
    genuinely new event from a replay.
    """
    event_type_value = event_type.value if isinstance(event_type, EventType) else str(event_type)
    channel_value = channel.value if isinstance(channel, Channel) else str(channel)
    occurred_at = occurred_at or utcnow()
    key = idempotency_key or make_idempotency_key(
        customer_id, campaign_id, message_id, event_type_value, occurred_at.isoformat()
    )

    existing = db.execute(
        select(CommunicationEvent.id).where(CommunicationEvent.idempotency_key == key)
    ).first()
    if existing:
        return None

    event = CommunicationEvent(
        customer_id=customer_id,
        campaign_id=campaign_id,
        message_id=message_id,
        event_type=event_type_value,
        channel=channel_value,
        provider=provider,
        occurred_at=occurred_at,
        is_simulated=is_simulated,
        payload=payload or {},
        idempotency_key=key,
    )
    try:
        with db.begin_nested():
            db.add(event)
            db.flush()
    except IntegrityError:
        return None

    _apply_event_side_effects(db, event)
    return event


# Event type -> (recipient timestamp field, campaign counter field)
_SIDE_EFFECTS: dict[str, tuple[str | None, str | None]] = {
    EventType.EMAIL_SENT.value: ("sent_at", "messages_sent"),
    EventType.SMS_SENT.value: ("sent_at", "messages_sent"),
    EventType.WHATSAPP_SENT.value: ("sent_at", "messages_sent"),
    EventType.EMAIL_DELIVERED.value: ("delivered_at", "messages_delivered"),
    EventType.SMS_DELIVERED.value: ("delivered_at", "messages_delivered"),
    EventType.WHATSAPP_DELIVERED.value: ("delivered_at", "messages_delivered"),
    EventType.EMAIL_OPENED.value: ("opened_at", "messages_opened"),
    EventType.WHATSAPP_READ.value: ("opened_at", "messages_opened"),
    EventType.EMAIL_CLICKED.value: ("clicked_at", "messages_clicked"),
    EventType.WHATSAPP_REPLIED.value: (None, "messages_replied"),
    EventType.EMAIL_BOUNCED.value: (None, "messages_failed"),
    EventType.SMS_FAILED.value: (None, "messages_failed"),
    EventType.MESSAGE_FAILED.value: (None, "messages_failed"),
    EventType.CUSTOMER_OPTED_OUT.value: (None, "unsubscribes"),
}


def _apply_event_side_effects(db: Session, event: CommunicationEvent) -> None:
    """Update recipient timestamps and campaign counters for an event."""
    recipient_field, campaign_field = _SIDE_EFFECTS.get(event.event_type, (None, None))
    if recipient_field is None and campaign_field is None:
        return

    recipient = None
    if event.message_id:
        message = db.get(Message, event.message_id)
        if message and message.recipient_id:
            recipient = db.get(CampaignRecipient, message.recipient_id)
    if recipient is None and event.campaign_id and event.customer_id:
        recipient = db.execute(
            select(CampaignRecipient).where(
                CampaignRecipient.campaign_id == event.campaign_id,
                CampaignRecipient.customer_id == event.customer_id,
            )
        ).scalar_one_or_none()

    if recipient is not None and recipient_field:
        # First occurrence wins: an open is "when they first opened".
        if getattr(recipient, recipient_field) is None:
            setattr(recipient, recipient_field, event.occurred_at)
        if recipient_field == "delivered_at":
            recipient.status = "DELIVERED"
        elif recipient_field == "sent_at" and recipient.status not in ("DELIVERED", "CONVERTED"):
            recipient.status = "SENT"

    if event.campaign_id and campaign_field:
        campaign = db.get(Campaign, event.campaign_id)
        if campaign is not None:
            setattr(campaign, campaign_field, (getattr(campaign, campaign_field) or 0) + 1)


def event_counts_for_campaign(db: Session, campaign_id: int) -> dict[str, int]:
    rows = db.execute(
        select(CommunicationEvent.event_type, CommunicationEvent.id).where(
            CommunicationEvent.campaign_id == campaign_id
        )
    ).all()
    counts: dict[str, int] = {}
    for event_type, _ in rows:
        counts[event_type] = counts.get(event_type, 0) + 1
    return counts
