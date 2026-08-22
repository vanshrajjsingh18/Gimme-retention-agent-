"""Mapping provider delivery events onto the automation ledger.

TNZ (and the mock adapters) report what happened to a message after it left:
delivered, failed, or the customer replying STOP. Those outcomes have to land
on the ``AutomationSend`` row so a campaign can be audited from the ledger
alone rather than by joining three tables and hoping.
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import Channel, EventType, SendStatus
from app.models.base import utcnow
from app.models.entities import AutomationSend, Customer

logger = logging.getLogger(__name__)

#: Provider outcome -> ledger status. Only terminal states are mapped; an
#: engagement event (opened, read) says nothing about delivery.
STATUS_BY_EVENT: dict[EventType, SendStatus] = {
    EventType.SMS_SENT: SendStatus.SENT,
    EventType.SMS_DELIVERED: SendStatus.DELIVERED,
    EventType.SMS_FAILED: SendStatus.FAILED,
    EventType.EMAIL_SENT: SendStatus.SENT,
    EventType.EMAIL_DELIVERED: SendStatus.DELIVERED,
    EventType.EMAIL_BOUNCED: SendStatus.FAILED,
    EventType.WHATSAPP_SENT: SendStatus.SENT,
    EventType.WHATSAPP_DELIVERED: SendStatus.DELIVERED,
    EventType.MESSAGE_FAILED: SendStatus.FAILED,
}

#: A delivered message must not be walked back to "sent" by a late-arriving
#: event, so progress is one-way.
RANK = {
    SendStatus.SCHEDULED.value: 0,
    SendStatus.QUEUED.value: 1,
    SendStatus.SENT.value: 2,
    SendStatus.DELIVERED.value: 3,
    SendStatus.FAILED.value: 3,
}


def apply_delivery_event(
    db: Session,
    *,
    event_type: EventType,
    provider_message_id: str | None = None,
    message_id: int | None = None,
    occurred_at: datetime | None = None,
    error: str | None = None,
) -> AutomationSend | None:
    """Advance the ledger row for one message. Returns it, or None if unknown."""
    status = STATUS_BY_EVENT.get(event_type)
    if status is None:
        return None

    row = _find_send(db, provider_message_id=provider_message_id, message_id=message_id)
    if row is None:
        return None

    occurred_at = occurred_at or utcnow()
    if RANK.get(status.value, 0) < RANK.get(row.status, 0):
        # Out-of-order webhook: keep the further-along state.
        return row

    row.status = status.value
    if status == SendStatus.SENT and row.sent_at is None:
        row.sent_at = occurred_at
    elif status == SendStatus.DELIVERED:
        row.delivered_at = occurred_at
        row.sent_at = row.sent_at or occurred_at
    elif status == SendStatus.FAILED:
        row.error_message = error or row.error_message
    return row


def _find_send(
    db: Session, *, provider_message_id: str | None, message_id: int | None
) -> AutomationSend | None:
    if message_id is not None:
        row = db.execute(
            select(AutomationSend).where(AutomationSend.message_id == message_id)
        ).scalar_one_or_none()
        if row is not None:
            return row
    if provider_message_id:
        return db.execute(
            select(AutomationSend)
            .where(AutomationSend.provider_message_id == provider_message_id)
            .order_by(AutomationSend.id.desc())
            .limit(1)
        ).scalar_one_or_none()
    return None


def find_customer_by_contact(
    db: Session, contact: str | None, *, channel: Channel
) -> Customer | None:
    """Resolve an inbound reply to a customer by the address it came from.

    An opt-out that cannot be matched to a message must still be honoured, so
    the phone number is a valid fallback identity here.
    """
    if not contact:
        return None
    value = contact.strip()
    if not value:
        return None
    column = Customer.email if channel == Channel.EMAIL else Customer.phone
    row = db.execute(select(Customer).where(column == value)).scalars().first()
    if row is not None:
        return row
    if channel == Channel.EMAIL:
        return None
    # Phone numbers arrive in several shapes (+64…, 0064…, 021…), so fall back
    # to matching on the last nine digits, which are stable across all of them.
    digits = "".join(ch for ch in value if ch.isdigit())[-9:]
    if len(digits) < 8:
        return None
    candidates = (
        db.execute(select(Customer).where(Customer.phone.is_not(None))).scalars().all()
    )
    for candidate in candidates:
        if "".join(ch for ch in (candidate.phone or "") if ch.isdigit()).endswith(digits):
            return candidate
    return None
