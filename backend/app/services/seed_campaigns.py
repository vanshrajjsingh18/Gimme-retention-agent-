"""Seed historical campaigns with realistic engagement and attribution.

Runs after customer/order seeding. Campaigns are backdated so the analytics
screens have a populated history on first launch, and every generated message
goes through the same grounding validation as a live send.
"""
from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.enums import (
    CampaignObjective,
    CampaignStatus,
    Channel,
    EventType,
    LifecycleStage,
    MessageStatus,
    RecipientStatus,
)
from app.integrations.mock_adapters import MockOutlookAdapter, MockTnzAdapter, MockWhatsAppAdapter
from app.models.base import utcnow
from app.models.entities import (
    Campaign,
    CampaignRecipient,
    Customer,
    CustomerMetrics,
    Message,
    Segment,
    User,
)
from app.services.attribution import backfill_attribution
from app.services.brand import build_compliance_config, get_brand_settings
from app.services.events import make_idempotency_key, record_communication_event
from app.services.messaging import generate_message

logger = logging.getLogger(__name__)

ADAPTERS = {
    Channel.EMAIL: MockOutlookAdapter,
    Channel.SMS: MockTnzAdapter,
    Channel.WHATSAPP: MockWhatsAppAdapter,
}

HISTORICAL_CAMPAIGNS: list[dict] = [
    {
        "name": "March Reorder Reminder",
        "objective": CampaignObjective.REORDER.value,
        "channel": Channel.EMAIL,
        "segment": "Regulars",
        "days_ago": 96,
        "max_recipients": 90,
    },
    {
        "name": "Second Order Nudge",
        "objective": CampaignObjective.SECOND_ORDER.value,
        "channel": Channel.EMAIL,
        "segment": "Needs Second Order",
        "days_ago": 74,
        "max_recipients": 70,
    },
    {
        "name": "VIP Thank You",
        "objective": CampaignObjective.VIP_APPRECIATION.value,
        "channel": Channel.EMAIL,
        "segment": "VIP Customers",
        "days_ago": 58,
        "max_recipients": 40,
    },
    {
        "name": "At Risk Save",
        "objective": CampaignObjective.RETENTION.value,
        "channel": Channel.EMAIL,
        "segment": "At Risk",
        "days_ago": 41,
        "max_recipients": 80,
    },
    {
        "name": "Dormant Reactivation",
        "objective": CampaignObjective.REACTIVATION.value,
        "channel": Channel.EMAIL,
        "segment": "Dormant",
        "days_ago": 27,
        "max_recipients": 70,
    },
    {
        "name": "Restock SMS",
        "objective": CampaignObjective.REORDER.value,
        "channel": Channel.SMS,
        "segment": "High Value Customers",
        "days_ago": 15,
        "max_recipients": 45,
    },
]

SUBJECTS = {
    CampaignObjective.REORDER.value: "Time to restock?",
    CampaignObjective.SECOND_ORDER.value: "Ready for round two?",
    CampaignObjective.VIP_APPRECIATION.value: "Thank you from the GIMME team",
    CampaignObjective.RETENTION.value: "We're still here whenever you need us",
    CampaignObjective.REACTIVATION.value: "It's been a while",
}


def _campaign_body(db: Session, objective: str, channel: Channel) -> str:
    brand = get_brand_settings(db)
    openings = {
        CampaignObjective.REORDER.value: (
            "It's about the time you usually restock, so we've kept your order history one "
            "tap away."
        ),
        CampaignObjective.SECOND_ORDER.value: (
            "You ordered with us recently and we hope it landed well. Whenever you need "
            "another round, we're here."
        ),
        CampaignObjective.VIP_APPRECIATION.value: (
            "You're one of our most loyal customers, and we wanted to say thanks properly."
        ),
        CampaignObjective.RETENTION.value: (
            "We noticed it's been longer than usual since your last order. No pressure — "
            "just letting you know nothing has changed on our end."
        ),
        CampaignObjective.REACTIVATION.value: (
            "It's been a while since we last saw an order from you. We're still delivering, "
            "and your order history is right where you left it."
        ),
    }
    body = f"Hi there,\n\n{openings.get(objective, openings[CampaignObjective.REORDER.value])}"
    if brand.delivery_promise:
        body += f"\n\n{brand.delivery_promise}."
    body += "\n\nOrder whenever suits — we'll take it from there."

    if channel == Channel.EMAIL:
        if brand.email_signature:
            body += f"\n\n{brand.email_signature}"
        if brand.responsible_drinking_statement:
            body += f"\n\n{brand.responsible_drinking_statement}"
        if brand.age_restriction_statement:
            body += f"\n{brand.age_restriction_statement}"
    return body


def seed_campaigns(
    db: Session, *, seed: int | None = None, now: datetime | None = None
) -> dict:
    """Create, send and simulate engagement for a set of historical campaigns."""
    from app.campaigns.service import build_recipient_view
    from app.compliance.engine import check_recipient

    rng = random.Random(seed if seed is not None else settings.MOCK_SEED + 7)
    now = now or utcnow()
    config = build_compliance_config(db)
    approver = db.execute(select(User).order_by(User.id)).scalars().first()

    created = 0
    sent = 0
    events = 0

    for spec in HISTORICAL_CAMPAIGNS:
        segment = db.execute(
            select(Segment).where(Segment.name == spec["segment"])
        ).scalar_one_or_none()
        channel: Channel = spec["channel"]
        # Send historical campaigns at a plausible mid-morning hour. Inheriting
        # the current wall-clock time would put a seed run after 21:00 inside
        # quiet hours, excluding every SMS and WhatsApp recipient.
        sent_at = (now - timedelta(days=spec["days_ago"])).replace(
            hour=10, minute=rng.randint(0, 59), second=0, microsecond=0
        )

        campaign = Campaign(
            name=spec["name"],
            description=f"Seeded historical campaign ({spec['objective']}).",
            objective=spec["objective"],
            channel=channel.value,
            status=CampaignStatus.COMPLETED.value,
            segment_id=segment.id if segment else None,
            sending_strategy="IMMEDIATE",
            attribution_window_hours=72,
            subject=SUBJECTS.get(spec["objective"], "A note from GIMME")
            if channel == Channel.EMAIL
            else "",
            body=_campaign_body(db, spec["objective"], channel),
            created_by_id=approver.id if approver else None,
            approved_by_id=approver.id if approver else None,
            approved_at=sent_at - timedelta(hours=2),
            started_at=sent_at,
            completed_at=sent_at + timedelta(hours=1),
            compliance_result={"passed": True, "blocking_count": 0, "findings": [], "seeded": True},
        )
        db.add(campaign)
        db.flush()
        created += 1

        # Choose recipients from the segment where possible, otherwise anyone
        # contactable on this channel.
        if segment is not None:
            from app.services.segments import evaluate_segment

            candidate_ids = [v["id"] for v in evaluate_segment(db, segment)]
        else:
            candidate_ids = db.execute(select(Customer.id)).scalars().all()

        rng.shuffle(candidate_ids)
        adapter = ADAPTERS[channel]()
        recipients_added = 0

        for customer_id in candidate_ids:
            if recipients_added >= spec["max_recipients"]:
                break
            customer = db.get(Customer, customer_id)
            if customer is None:
                continue

            view = build_recipient_view(db, customer, now=sent_at)
            status, reason = check_recipient(view, channel, config, send_time=sent_at)

            recipient = CampaignRecipient(
                campaign_id=campaign.id,
                customer_id=customer.id,
                status=status.value,
                exclusion_reason=reason,
            )
            db.add(recipient)
            db.flush()

            if status != RecipientStatus.ELIGIBLE:
                continue
            recipients_added += 1

            message = generate_message(
                db,
                customer,
                channel=channel,
                objective=spec["objective"],
                campaign_id=campaign.id,
                campaign_name=campaign.name,
                config=config,
                persist=True,
            )
            if message.status == MessageStatus.VALIDATION_FAILED.value:
                recipient.status = RecipientStatus.FAILED.value
                recipient.exclusion_reason = "Generated message failed validation."
                campaign.messages_failed += 1
                continue

            to = customer.email if channel == Channel.EMAIL else customer.phone
            result = adapter.send_message(
                to=to or "",
                subject=message.subject,
                body=message.body,
                metadata={"campaign_id": campaign.id, "message_id": recipient.id},
            )
            message.recipient_id = recipient.id
            message.provider = adapter.provider
            message.provider_message_id = result.provider_message_id
            message.generated_at = sent_at

            if not result.success:
                message.status = MessageStatus.FAILED.value
                message.error_message = result.error
                recipient.status = RecipientStatus.FAILED.value
                campaign.messages_failed += 1
                continue

            message.status = MessageStatus.SENT.value
            message.sent_at = sent_at
            message.approved_by_id = approver.id if approver else None
            message.approved_at = sent_at - timedelta(hours=2)
            recipient.status = RecipientStatus.SENT.value
            recipient.sent_at = sent_at
            sent += 1

            record_communication_event(
                db,
                event_type={
                    Channel.EMAIL: EventType.EMAIL_SENT,
                    Channel.SMS: EventType.SMS_SENT,
                    Channel.WHATSAPP: EventType.WHATSAPP_SENT,
                }[channel],
                customer_id=customer.id,
                campaign_id=campaign.id,
                message_id=message.id,
                channel=channel,
                provider=adapter.provider,
                occurred_at=sent_at,
                is_simulated=True,
                payload={"seeded": True},
                idempotency_key=make_idempotency_key("seed-sent", message.id),
            )
            events += 1

            metrics = db.execute(
                select(CustomerMetrics).where(CustomerMetrics.customer_id == customer.id)
            ).scalar_one_or_none()
            bias = 0.4 + ((metrics.engagement_score if metrics else 50.0) / 100.0) * 1.2
            for event in adapter.simulate_engagement(
                provider_message_id=message.provider_message_id or f"msg-{message.id}",
                sent_at=sent_at,
                engagement_bias=bias,
            ):
                created_event = record_communication_event(
                    db,
                    event_type=event.event_type,
                    customer_id=customer.id,
                    campaign_id=campaign.id,
                    message_id=message.id,
                    channel=channel,
                    provider=adapter.provider,
                    occurred_at=event.occurred_at or sent_at,
                    is_simulated=True,
                    payload=event.payload,
                    idempotency_key=make_idempotency_key(
                        "seed-engagement", message.id, event.event_type.value
                    ),
                )
                if created_event is not None:
                    events += 1

            campaign.total_recipients = recipients_added
        db.commit()

    attribution = backfill_attribution(db)
    return {
        "campaigns": created,
        "messages_sent": sent,
        "communication_events": events,
        "attribution": attribution,
    }
