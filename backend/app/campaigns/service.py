"""Campaign audience building, compliance gating, approval and sending.

Send is gated three ways and all three must pass:
  1. The campaign must be in APPROVED or SCHEDULED state (a human action).
  2. The campaign-level compliance report must have no blocking findings.
  3. Each recipient is re-checked for eligibility at send time, not just at
     preview time, so consent revoked between preview and send is honoured.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.compliance.engine import (
    ComplianceConfig,
    ComplianceReport,
    RecipientView,
    check_campaign,
    check_recipient,
)
from app.core.enums import (
    CampaignStatus,
    Channel,
    EventType,
    MessageStatus,
    RecipientStatus,
)
from app.integrations.mock_adapters import BaseMockAdapter
from app.integrations.registry import get_adapter
from app.models.base import utcnow
from app.models.entities import (
    Campaign,
    CampaignRecipient,
    Customer,
    CustomerMetrics,
    Message,
    Segment,
    SuppressionList,
)
from app.services.brand import build_compliance_config
from app.services.events import make_idempotency_key, record_communication_event
from app.services.intelligence import load_engagement
from app.services.messaging import generate_message
from app.services.segments import evaluate_segment

logger = logging.getLogger(__name__)

SENDABLE_STATUSES = {CampaignStatus.APPROVED.value, CampaignStatus.SCHEDULED.value}

SENT_EVENT_BY_CHANNEL = {
    Channel.EMAIL: EventType.EMAIL_SENT,
    Channel.SMS: EventType.SMS_SENT,
    Channel.WHATSAPP: EventType.WHATSAPP_SENT,
    Channel.PUSH: EventType.EMAIL_SENT,
}


class CampaignError(RuntimeError):
    """Raised when a campaign operation is not permitted in the current state."""


# --------------------------------------------------------------------------
# Audience
# --------------------------------------------------------------------------
def build_recipient_view(
    db: Session, customer: Customer, *, now: datetime | None = None
) -> RecipientView:
    now = now or utcnow()
    engagement = load_engagement(db, customer.id, now=now)
    suppressed_channels = {
        row[0]
        for row in db.execute(
            select(SuppressionList.channel).where(
                SuppressionList.customer_id == customer.id,
                SuppressionList.active.is_(True),
            )
        ).all()
    }
    return RecipientView(
        customer_id=customer.id,
        age=_age(customer.date_of_birth, now),
        age_verified=customer.age_verified,
        is_suppressed=customer.is_suppressed,
        suppressed_channels=suppressed_channels,
        marketing_consent=customer.marketing_consent,
        email_consent=customer.email_consent,
        sms_consent=customer.sms_consent,
        whatsapp_consent=customer.whatsapp_consent,
        email=customer.email,
        phone=customer.phone,
        messages_last_7d=engagement["messages_last_7d"],
        messages_last_30d=engagement["messages_last_30d"],
        lifecycle_stage=customer.lifecycle_stage,
    )


def _age(dob, now: datetime) -> int | None:
    if dob is None:
        return None
    years = now.year - dob.year
    if (now.month, now.day) < (dob.month, dob.day):
        years -= 1
    return years


def preview_audience(
    db: Session,
    campaign: Campaign,
    *,
    config: ComplianceConfig | None = None,
    send_time: datetime | None = None,
    sample_size: int = 10,
) -> dict:
    """Compute the eligible/excluded breakdown for a campaign's audience."""
    config = config or build_compliance_config(db)
    channel = Channel(campaign.channel)
    send_time = send_time or campaign.scheduled_at or utcnow()

    if campaign.segment_id:
        segment = db.get(Segment, campaign.segment_id)
        if segment is None:
            raise CampaignError(f"Segment {campaign.segment_id} no longer exists.")
        views = evaluate_segment(db, segment)
        customer_ids = [v["id"] for v in views]
    else:
        customer_ids = db.execute(select(Customer.id)).scalars().all()

    if not customer_ids:
        return _empty_audience(send_time)

    customers = (
        db.execute(select(Customer).where(Customer.id.in_(customer_ids))).scalars().all()
    )

    eligible: list[dict] = []
    excluded_by_reason: dict[str, int] = {}
    exclusion_samples: dict[str, list[dict]] = {}
    decisions: list[tuple[int, RecipientStatus, str | None]] = []

    for customer in customers:
        view = build_recipient_view(db, customer, now=send_time)
        status, reason = check_recipient(view, channel, config, send_time=send_time)
        decisions.append((customer.id, status, reason))
        if status == RecipientStatus.ELIGIBLE:
            eligible.append(
                {
                    "id": customer.id,
                    "external_id": customer.external_id,
                    "full_name": customer.full_name,
                    "email": customer.email,
                    "phone": customer.phone,
                    "lifecycle_stage": customer.lifecycle_stage,
                }
            )
        else:
            key = status.value
            excluded_by_reason[key] = excluded_by_reason.get(key, 0) + 1
            exclusion_samples.setdefault(key, [])
            if len(exclusion_samples[key]) < 3:
                exclusion_samples[key].append(
                    {
                        "id": customer.id,
                        "full_name": customer.full_name,
                        "reason": reason,
                    }
                )

    return {
        "audience_size": len(customers),
        "eligible_count": len(eligible),
        "excluded_count": len(customers) - len(eligible),
        "excluded_by_reason": excluded_by_reason,
        "exclusion_samples": exclusion_samples,
        "sample_recipients": eligible[:sample_size],
        "channel": channel.value,
        "evaluated_at": send_time.isoformat(),
        "_decisions": decisions,
    }


def _empty_audience(send_time: datetime) -> dict:
    return {
        "audience_size": 0,
        "eligible_count": 0,
        "excluded_count": 0,
        "excluded_by_reason": {},
        "exclusion_samples": {},
        "sample_recipients": [],
        "channel": "",
        "evaluated_at": send_time.isoformat(),
        "_decisions": [],
    }


def snapshot_audience(
    db: Session, campaign: Campaign, *, config: ComplianceConfig | None = None
) -> dict:
    """Materialise the audience into ``campaign_recipients`` and persist a summary."""
    audience = preview_audience(db, campaign, config=config)
    decisions = audience.pop("_decisions")

    existing = {
        r.customer_id: r
        for r in db.execute(
            select(CampaignRecipient).where(CampaignRecipient.campaign_id == campaign.id)
        )
        .scalars()
        .all()
    }
    # Recipients already sent to are frozen; re-snapshotting must not rewrite
    # the delivery record of a running campaign.
    frozen = {
        RecipientStatus.SENT.value,
        RecipientStatus.DELIVERED.value,
        RecipientStatus.CONVERTED.value,
        RecipientStatus.FAILED.value,
    }

    for customer_id, status, reason in decisions:
        row = existing.get(customer_id)
        if row is None:
            row = CampaignRecipient(campaign_id=campaign.id, customer_id=customer_id)
            db.add(row)
        elif row.status in frozen:
            continue
        row.status = status.value
        row.exclusion_reason = reason

    # Drop recipients no longer in the audience, unless already sent to.
    current_ids = {cid for cid, _, _ in decisions}
    for customer_id, row in existing.items():
        if customer_id not in current_ids and row.status not in frozen:
            db.delete(row)

    campaign.total_recipients = audience["eligible_count"]
    campaign.audience_snapshot = audience
    db.commit()
    return audience


# --------------------------------------------------------------------------
# Compliance gate
# --------------------------------------------------------------------------
def run_compliance_check(
    db: Session, campaign: Campaign, *, config: ComplianceConfig | None = None
) -> ComplianceReport:
    """Run and persist the campaign compliance report."""
    config = config or build_compliance_config(db)
    segment = db.get(Segment, campaign.segment_id) if campaign.segment_id else None
    report = check_campaign(
        subject=campaign.subject,
        body=campaign.body,
        channel=Channel(campaign.channel),
        objective=campaign.objective,
        segment_rule=segment.rule_definition if segment else None,
        config=config,
        approved_by_human=campaign.status
        in (CampaignStatus.APPROVED.value, CampaignStatus.SCHEDULED.value),
    )
    campaign.compliance_result = report.as_dict()
    if campaign.status in (
        CampaignStatus.DRAFT.value,
        CampaignStatus.AI_GENERATED.value,
        CampaignStatus.VALIDATED.value,
        CampaignStatus.COMPLIANCE_CHECKED.value,
    ):
        campaign.status = CampaignStatus.COMPLIANCE_CHECKED.value
    db.commit()
    return report


def submit_for_approval(db: Session, campaign: Campaign) -> Campaign:
    report = run_compliance_check(db, campaign)
    if not report.passed:
        raise CampaignError(
            "Campaign cannot be submitted for approval while compliance checks are failing: "
            + "; ".join(f.message for f in report.blocking_findings)
        )
    campaign.status = CampaignStatus.AWAITING_APPROVAL.value
    db.commit()
    return campaign


def approve_campaign(db: Session, campaign: Campaign, *, user_id: int) -> Campaign:
    """Approve a campaign. Requires a passing compliance report."""
    if campaign.status not in (
        CampaignStatus.AWAITING_APPROVAL.value,
        CampaignStatus.COMPLIANCE_CHECKED.value,
    ):
        raise CampaignError(
            f"Only a campaign awaiting approval can be approved (current status: "
            f"{campaign.status})."
        )

    config = build_compliance_config(db)
    segment = db.get(Segment, campaign.segment_id) if campaign.segment_id else None
    report = check_campaign(
        subject=campaign.subject,
        body=campaign.body,
        channel=Channel(campaign.channel),
        objective=campaign.objective,
        segment_rule=segment.rule_definition if segment else None,
        config=config,
        approved_by_human=True,
    )
    campaign.compliance_result = report.as_dict()
    if not report.passed:
        db.commit()
        raise CampaignError(
            "Campaign has blocking compliance findings and cannot be approved: "
            + "; ".join(f.message for f in report.blocking_findings)
        )

    campaign.status = CampaignStatus.APPROVED.value
    campaign.approved_by_id = user_id
    campaign.approved_at = utcnow()
    db.commit()
    return campaign


def schedule_campaign(db: Session, campaign: Campaign, when: datetime) -> Campaign:
    if campaign.status != CampaignStatus.APPROVED.value:
        raise CampaignError("Only an approved campaign can be scheduled.")
    campaign.scheduled_at = when
    campaign.status = CampaignStatus.SCHEDULED.value
    db.commit()
    return campaign


# --------------------------------------------------------------------------
# Sending
# --------------------------------------------------------------------------
def send_test_message(
    db: Session,
    campaign: Campaign,
    *,
    to: str,
    customer_id: int | None = None,
) -> dict:
    """Send a single test message. Never touches campaign metrics."""
    channel = Channel(campaign.channel)
    adapter = get_adapter(db, channel)
    subject, body = campaign.subject, campaign.body

    if customer_id:
        customer = db.get(Customer, customer_id)
        if customer is not None:
            generated = generate_message(
                db,
                customer,
                channel=channel,
                objective=campaign.objective,
                campaign_name=campaign.name,
                persist=False,
            )
            subject, body = generated.subject or subject, generated.body or body

    result = adapter.send_message(
        to=to, subject=subject, body=body, metadata={"is_test": True}
    )

    message = Message(
        customer_id=customer_id,
        campaign_id=campaign.id,
        channel=channel.value,
        objective=campaign.objective,
        subject=subject,
        body=body,
        original_subject=subject,
        original_body=body,
        status=MessageStatus.SENT.value if result.success else MessageStatus.FAILED.value,
        provider=adapter.provider,
        provider_message_id=result.provider_message_id,
        is_test=True,
        sent_at=utcnow() if result.success else None,
        error_message=result.error,
    )
    db.add(message)
    db.commit()

    return {
        "success": result.success,
        "provider": adapter.provider,
        "is_simulated": result.is_simulated,
        "provider_message_id": result.provider_message_id,
        "error": result.error,
        "subject": subject,
        "body": body,
    }


def run_campaign(
    db: Session,
    campaign: Campaign,
    *,
    generate_per_customer: bool = True,
    simulate_engagement: bool = True,
    limit: int | None = None,
    now: datetime | None = None,
) -> dict:
    """Execute an approved campaign.

    In MOCK MODE nothing leaves the machine: messages and events are recorded
    locally and tagged as simulated.
    """
    now = now or utcnow()
    if campaign.status not in SENDABLE_STATUSES:
        raise CampaignError(
            f"Campaign must be approved before sending (current status: {campaign.status})."
        )

    report_data = campaign.compliance_result or {}
    if not report_data.get("passed", False):
        raise CampaignError(
            "Campaign cannot send: the compliance report has blocking findings. "
            "Re-run the compliance check after fixing them."
        )

    config = build_compliance_config(db)
    channel = Channel(campaign.channel)
    adapter = get_adapter(db, channel)

    campaign.status = CampaignStatus.RUNNING.value
    campaign.started_at = campaign.started_at or now
    db.commit()

    recipients = (
        db.execute(
            select(CampaignRecipient).where(
                CampaignRecipient.campaign_id == campaign.id,
                CampaignRecipient.status.in_(
                    [RecipientStatus.ELIGIBLE.value, RecipientStatus.QUEUED.value]
                ),
            )
        )
        .scalars()
        .all()
    )
    if limit is not None:
        recipients = recipients[:limit]

    stats = {
        "attempted": 0,
        "sent": 0,
        "failed": 0,
        "skipped_ineligible": 0,
        "generation_failed": 0,
        "simulated_events": 0,
    }

    for recipient in recipients:
        customer = db.get(Customer, recipient.customer_id)
        if customer is None:
            continue

        # Re-check eligibility at send time: consent may have changed since
        # the audience snapshot was taken.
        view = build_recipient_view(db, customer, now=now)
        status, reason = check_recipient(view, channel, config, send_time=now)
        if status != RecipientStatus.ELIGIBLE:
            recipient.status = status.value
            recipient.exclusion_reason = reason
            stats["skipped_ineligible"] += 1
            continue

        stats["attempted"] += 1

        subject, body = campaign.subject, campaign.body
        message_row: Message | None = None
        if generate_per_customer:
            message_row = generate_message(
                db,
                customer,
                channel=channel,
                objective=campaign.objective,
                campaign_id=campaign.id,
                campaign_name=campaign.name,
                config=config,
                persist=True,
            )
            if message_row.status == MessageStatus.VALIDATION_FAILED.value:
                # A message that fails grounding validation is never sent.
                recipient.status = RecipientStatus.FAILED.value
                recipient.exclusion_reason = (
                    "Generated message failed content validation and was not sent."
                )
                message_row.recipient_id = recipient.id
                stats["generation_failed"] += 1
                stats["failed"] += 1
                campaign.messages_failed += 1
                db.commit()
                continue
            subject, body = message_row.subject, message_row.body

        to = customer.email if channel == Channel.EMAIL else customer.phone
        result = adapter.send_message(
            to=to or "",
            subject=subject,
            body=body,
            metadata={"campaign_id": campaign.id, "message_id": recipient.id},
        )

        if message_row is None:
            message_row = Message(
                customer_id=customer.id,
                campaign_id=campaign.id,
                channel=channel.value,
                objective=campaign.objective,
                subject=subject,
                body=body,
                original_subject=subject,
                original_body=body,
            )
            db.add(message_row)
            db.flush()

        message_row.recipient_id = recipient.id
        message_row.provider = adapter.provider
        message_row.provider_message_id = result.provider_message_id

        if not result.success:
            message_row.status = MessageStatus.FAILED.value
            message_row.error_message = result.error
            recipient.status = RecipientStatus.FAILED.value
            recipient.exclusion_reason = result.error
            campaign.messages_failed += 1
            stats["failed"] += 1
            record_communication_event(
                db,
                event_type=EventType.MESSAGE_FAILED,
                customer_id=customer.id,
                campaign_id=campaign.id,
                message_id=message_row.id,
                channel=channel,
                provider=adapter.provider,
                occurred_at=now,
                is_simulated=result.is_simulated,
                payload={"error": result.error},
            )
            db.commit()
            continue

        message_row.status = MessageStatus.SENT.value
        message_row.sent_at = now
        recipient.status = RecipientStatus.SENT.value
        recipient.sent_at = now
        stats["sent"] += 1

        record_communication_event(
            db,
            event_type=SENT_EVENT_BY_CHANNEL[channel],
            customer_id=customer.id,
            campaign_id=campaign.id,
            message_id=message_row.id,
            channel=channel,
            provider=adapter.provider,
            occurred_at=now,
            is_simulated=result.is_simulated,
            payload={"provider_message_id": result.provider_message_id},
        )

        if simulate_engagement and isinstance(adapter, BaseMockAdapter):
            stats["simulated_events"] += _simulate_recipient_engagement(
                db,
                adapter=adapter,
                campaign=campaign,
                customer=customer,
                message=message_row,
                sent_at=now,
            )
        db.commit()

    campaign.status = CampaignStatus.COMPLETED.value
    campaign.completed_at = utcnow()
    db.commit()

    stats["campaign_status"] = campaign.status
    stats["is_mock"] = isinstance(adapter, BaseMockAdapter)
    stats["provider"] = adapter.provider
    return stats


def _simulate_recipient_engagement(
    db: Session,
    *,
    adapter: BaseMockAdapter,
    campaign: Campaign,
    customer: Customer,
    message: Message,
    sent_at: datetime,
) -> int:
    """Generate and record simulated delivery/engagement for one message."""
    metrics = db.execute(
        select(CustomerMetrics).where(CustomerMetrics.customer_id == customer.id)
    ).scalar_one_or_none()
    # Engaged customers open more; disengaged ones open less. Scales the
    # baseline rates between 0.4x and 1.6x.
    engagement_score = metrics.engagement_score if metrics else 50.0
    bias = 0.4 + (engagement_score / 100.0) * 1.2

    events = adapter.simulate_engagement(
        provider_message_id=message.provider_message_id or f"msg-{message.id}",
        sent_at=sent_at,
        engagement_bias=bias,
    )
    recorded = 0
    for event in events:
        created = record_communication_event(
            db,
            event_type=event.event_type,
            customer_id=customer.id,
            campaign_id=campaign.id,
            message_id=message.id,
            channel=Channel(campaign.channel),
            provider=adapter.provider,
            occurred_at=event.occurred_at or sent_at,
            is_simulated=True,
            payload=event.payload,
            idempotency_key=make_idempotency_key(
                "sim", message.id, event.event_type.value
            ),
        )
        if created is not None:
            recorded += 1
            if event.event_type == EventType.CUSTOMER_OPTED_OUT:
                _apply_opt_out(db, customer, Channel(campaign.channel))
    return recorded


def _apply_opt_out(db: Session, customer: Customer, channel: Channel) -> None:
    """Honour an opt-out immediately: revoke consent and suppress the channel."""
    if channel == Channel.EMAIL:
        customer.email_consent = False
    elif channel == Channel.SMS:
        customer.sms_consent = False
    elif channel == Channel.WHATSAPP:
        customer.whatsapp_consent = False

    existing = db.execute(
        select(SuppressionList).where(
            SuppressionList.customer_id == customer.id,
            SuppressionList.channel == channel.value,
        )
    ).scalar_one_or_none()
    if existing is None:
        db.add(
            SuppressionList(
                customer_id=customer.id,
                channel=channel.value,
                reason="Customer opted out via message footer.",
                created_by="system",
                active=True,
            )
        )
    else:
        existing.active = True


def pause_campaign(db: Session, campaign: Campaign) -> Campaign:
    if campaign.status not in (CampaignStatus.RUNNING.value, CampaignStatus.SCHEDULED.value):
        raise CampaignError("Only a running or scheduled campaign can be paused.")
    campaign.status = CampaignStatus.PAUSED.value
    db.commit()
    return campaign


def cancel_campaign(db: Session, campaign: Campaign) -> Campaign:
    if campaign.status in (CampaignStatus.COMPLETED.value, CampaignStatus.CANCELLED.value):
        raise CampaignError(f"Campaign is already {campaign.status.lower()}.")
    campaign.status = CampaignStatus.CANCELLED.value
    db.commit()
    return campaign


def due_scheduled_campaigns(db: Session, *, now: datetime | None = None) -> list[Campaign]:
    now = now or utcnow()
    return (
        db.execute(
            select(Campaign).where(
                Campaign.status == CampaignStatus.SCHEDULED.value,
                Campaign.scheduled_at.is_not(None),
                Campaign.scheduled_at <= now,
            )
        )
        .scalars()
        .all()
    )
