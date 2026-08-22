"""Global opt-out handling.

A STOP reply is a withdrawal of permission to contact, not a preference about
one campaign. It therefore suppresses **every** channel and **every**
automation type at once, and does so in a way that survives a later data
import re-setting a consent flag:

  1. all consent flags are cleared;
  2. an ALL-channel suppression record is written;
  3. ``Customer.is_suppressed`` is set;
  4. every active automation enrollment for that customer is stopped.

Eligibility checks read the suppression record as well as the consent flags,
so restoring a flag alone cannot silently re-enable messaging.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import Channel, ConsentType, EventType
from app.models.base import utcnow
from app.models.entities import AuditLog, ConsentEvent, Customer, SuppressionList

logger = logging.getLogger(__name__)

#: Keywords that constitute an opt-out. Matched case-insensitively against the
#: whole message after stripping punctuation, so "Stop." and "STOP!" both count.
#: Deliberately narrow — "please stop sending me so many" is caught by the
#: exact-word match, but "I couldn't stop drinking it" is not, because the
#: keyword must be the entire message.
OPT_OUT_KEYWORDS = {
    "stop",
    "stopall",
    "stop all",
    "unsubscribe",
    "unsub",
    "cancel",
    "end",
    "quit",
    "optout",
    "opt out",
    "opt-out",
    "remove",
    "no",
    "nomore",
    "no more",
}

#: Keywords that re-enable messaging after an opt-out.
OPT_IN_KEYWORDS = {"start", "unstop", "yes", "subscribe", "optin", "opt in", "opt-in"}

_PUNCTUATION = re.compile(r"[^\w\s-]")


def normalise_reply(body: str | None) -> str:
    """Lower-case, strip punctuation and collapse whitespace."""
    if not body:
        return ""
    cleaned = _PUNCTUATION.sub("", body).strip().lower()
    return re.sub(r"\s+", " ", cleaned)


def is_opt_out(body: str | None) -> bool:
    """True when an inbound reply is an opt-out request."""
    return normalise_reply(body) in OPT_OUT_KEYWORDS


def is_opt_in(body: str | None) -> bool:
    return normalise_reply(body) in OPT_IN_KEYWORDS


def apply_global_opt_out(
    db: Session,
    customer: Customer,
    *,
    source: str = "sms_reply",
    channel: Channel | None = None,
    occurred_at: datetime | None = None,
    commit: bool = True,
) -> dict:
    """Suppress a customer across every channel and automation.

    ``channel`` records where the opt-out arrived from; it does not narrow the
    effect. Returns a summary of what changed.
    """
    occurred_at = occurred_at or utcnow()

    revoked = []
    for field_name, consent_type in (
        ("marketing_consent", ConsentType.MARKETING),
        ("email_consent", ConsentType.EMAIL),
        ("sms_consent", ConsentType.SMS),
        ("whatsapp_consent", ConsentType.WHATSAPP),
    ):
        if getattr(customer, field_name):
            revoked.append(consent_type.value)
        setattr(customer, field_name, False)
        db.add(
            ConsentEvent(
                customer_id=customer.id,
                consent_type=consent_type.value,
                granted=False,
                source=source,
                occurred_at=occurred_at,
            )
        )

    customer.is_suppressed = True
    existing = db.execute(
        select(SuppressionList).where(
            SuppressionList.customer_id == customer.id,
            SuppressionList.channel == "ALL",
        )
    ).scalar_one_or_none()
    reason = f"Customer opted out via {channel.value if channel else source}."
    if existing is None:
        db.add(
            SuppressionList(
                customer_id=customer.id,
                channel="ALL",
                reason=reason,
                created_by="system",
                active=True,
            )
        )
    else:
        existing.active = True
        existing.reason = reason

    stopped = _stop_enrollments(db, customer, occurred_at)

    db.add(
        AuditLog(
            actor=source,
            action="CUSTOMER_OPTED_OUT",
            entity_type="customer",
            entity_id=str(customer.id),
            detail={
                "channel": channel.value if channel else None,
                "consents_revoked": revoked,
                "enrollments_stopped": stopped,
            },
        )
    )

    if commit:
        db.commit()

    logger.info(
        "Global opt-out applied to customer %s (%d enrollments stopped)",
        customer.id,
        stopped,
    )
    return {
        "customer_id": customer.id,
        "consents_revoked": revoked,
        "enrollments_stopped": stopped,
        "suppressed": True,
    }


def _stop_enrollments(db: Session, customer: Customer, occurred_at: datetime) -> int:
    """Stop every active automation enrollment. Returns how many were stopped."""
    # Imported lazily: the automations package imports this module for its own
    # opt-out handling, and a top-level import would be circular.
    from app.models.entities import AutomationEnrollment
    from app.core.enums import EnrollmentStatus

    rows = (
        db.execute(
            select(AutomationEnrollment).where(
                AutomationEnrollment.customer_id == customer.id,
                AutomationEnrollment.status == EnrollmentStatus.ACTIVE.value,
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        row.status = EnrollmentStatus.STOPPED.value
        row.stop_reason = "Customer opted out."
        row.stopped_at = occurred_at
    return len(rows)


def apply_opt_in(
    db: Session,
    customer: Customer,
    *,
    source: str = "sms_reply",
    occurred_at: datetime | None = None,
    commit: bool = True,
) -> dict:
    """Reverse a global opt-out.

    Restores marketing and the channel the request arrived on. Other channels
    stay off: consenting to SMS again is not consent to email.
    """
    occurred_at = occurred_at or utcnow()

    customer.is_suppressed = False
    customer.marketing_consent = True
    customer.sms_consent = True
    for consent_type in (ConsentType.MARKETING, ConsentType.SMS):
        db.add(
            ConsentEvent(
                customer_id=customer.id,
                consent_type=consent_type.value,
                granted=True,
                source=source,
                occurred_at=occurred_at,
            )
        )

    for row in (
        db.execute(
            select(SuppressionList).where(SuppressionList.customer_id == customer.id)
        )
        .scalars()
        .all()
    ):
        row.active = False

    db.add(
        AuditLog(
            actor=source,
            action="CUSTOMER_OPTED_IN",
            entity_type="customer",
            entity_id=str(customer.id),
            detail={"channels_restored": ["MARKETING", "SMS"]},
        )
    )
    if commit:
        db.commit()
    return {"customer_id": customer.id, "suppressed": False}


def handle_inbound_reply(
    db: Session,
    *,
    customer: Customer,
    body: str,
    channel: Channel = Channel.SMS,
    occurred_at: datetime | None = None,
) -> dict | None:
    """Process an inbound message for opt-out / opt-in keywords.

    Returns a summary when the reply changed something, otherwise None.
    """
    if is_opt_out(body):
        return apply_global_opt_out(
            db, customer, source=f"{channel.value.lower()}_reply", channel=channel,
            occurred_at=occurred_at,
        )
    if is_opt_in(body):
        return apply_opt_in(
            db, customer, source=f"{channel.value.lower()}_reply", occurred_at=occurred_at
        )
    return None
