"""Message generation: assemble grounding context, call the LLM, validate."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.compliance.engine import ComplianceConfig
from app.core.enums import Channel, MessageStatus
from app.llm.base import LLMError
from app.llm.factory import get_llm_provider
from app.llm.prompts import PROMPT_VERSION, GroundingContext, build_system_prompt, build_user_prompt
from app.llm.validator import ValidationResult, parse_llm_output, validate_message
from app.models.base import utcnow
from app.models.entities import Customer, Message
from app.services.brand import apply_brand_to_context, build_compliance_config, get_brand_settings
from app.services.intelligence import CustomerIntelligence, compute_intelligence
from app.services.lifecycle import expected_cycle_days

logger = logging.getLogger(__name__)


def build_grounding_context(
    db: Session,
    customer: Customer,
    *,
    channel: Channel,
    objective: str = "",
    campaign_name: str = "",
    intel: CustomerIntelligence | None = None,
) -> GroundingContext:
    """Assemble every verified fact the model is allowed to use."""
    intel = intel or compute_intelligence(db, customer)
    m = intel.metrics
    cycle, _ = expected_cycle_days(m)

    ctx = GroundingContext(
        customer_first_name=customer.first_name or "",
        city=customer.city,
        lifecycle_stage=intel.lifecycle.stage.value,
        total_completed_orders=m.completed_orders,
        lifetime_revenue=m.lifetime_revenue,
        average_order_value=m.average_order_value,
        days_since_last_order=m.days_since_last_order,
        expected_cycle_days=cycle,
        preferred_categories=list(m.preferred_categories),
        preferred_brands=list(m.preferred_brands),
        top_products=list(m.top_products),
        typical_order_weekday=m.typical_order_weekday,
        churn_score=intel.churn.score,
        churn_risk_band=intel.churn.risk_band.value,
        churn_explanation=intel.churn.explanation,
        recommended_action=intel.recommendation.action.value,
        recommendation_explanation=intel.recommendation.explanation,
        messages_sent_90d=intel.engagement["messages_sent_90d"],
        messages_opened_90d=intel.engagement["messages_opened_90d"],
        last_message_summary=_last_message_summary(db, customer.id),
        campaign_objective=objective or intel.recommendation.action.value,
        campaign_name=campaign_name,
        channel=channel.value,
    )
    return apply_brand_to_context(get_brand_settings(db), ctx)


def _last_message_summary(db: Session, customer_id: int) -> str:
    row = db.execute(
        select(Message)
        .where(Message.customer_id == customer_id, Message.sent_at.is_not(None))
        .order_by(Message.sent_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        return ""
    days = (utcnow() - row.sent_at).days if row.sent_at else 0
    label = row.subject or (row.body[:60] + "...") if row.body else "message"
    return f"{row.channel} '{label}' sent {days} days ago"


def generate_message(
    db: Session,
    customer: Customer,
    *,
    channel: Channel,
    objective: str = "",
    variation: str = "default",
    campaign_id: int | None = None,
    campaign_name: str = "",
    intel: CustomerIntelligence | None = None,
    config: ComplianceConfig | None = None,
    persist: bool = True,
) -> Message:
    """Generate one grounded message and validate it before persisting."""
    ctx = build_grounding_context(
        db,
        customer,
        channel=channel,
        objective=objective,
        campaign_name=campaign_name,
        intel=intel,
    )
    config = config or build_compliance_config(db)
    provider = get_llm_provider()

    system_prompt = build_system_prompt(ctx)
    user_prompt = build_user_prompt(ctx, variation=variation)

    try:
        response = provider.complete(system_prompt, user_prompt)
        subject, body = parse_llm_output(response.text)
        error: str | None = None
    except LLMError as exc:
        logger.warning("LLM generation failed for customer %s: %s", customer.id, exc)
        subject, body, error = "", "", str(exc)
        response = None

    if error:
        message = Message(
            customer_id=customer.id,
            campaign_id=campaign_id,
            channel=channel.value,
            objective=objective or ctx.recommended_action,
            status=MessageStatus.VALIDATION_FAILED.value,
            error_message=error,
            generated_at=utcnow(),
            generation_context=ctx.as_dict(),
            validation_result={"valid": False, "errors": [{"code": "LLM_ERROR", "message": error}]},
        )
        if persist:
            db.add(message)
            db.commit()
            db.refresh(message)
        return message

    validation = validate_message(
        subject=subject, body=body, channel=channel, context=ctx, config=config
    )

    message = Message(
        customer_id=customer.id,
        campaign_id=campaign_id,
        channel=channel.value,
        objective=objective or ctx.recommended_action,
        subject=subject,
        body=body,
        original_subject=subject,
        original_body=body,
        status=(
            MessageStatus.GENERATED.value
            if validation.valid
            else MessageStatus.VALIDATION_FAILED.value
        ),
        llm_provider=response.provider,
        llm_model=response.model,
        prompt_version=PROMPT_VERSION,
        generated_at=utcnow(),
        generation_context=ctx.as_dict(),
        validation_result=validation.as_dict(),
    )
    if persist:
        db.add(message)
        db.commit()
        db.refresh(message)
    return message


def revalidate_message(db: Session, message: Message) -> ValidationResult:
    """Re-run validation after an operator edit."""
    stored = message.generation_context or {}
    known = {f for f in GroundingContext.__dataclass_fields__}
    ctx = GroundingContext(**{k: v for k, v in stored.items() if k in known})
    config = build_compliance_config(db)
    result = validate_message(
        subject=message.subject,
        body=message.body,
        channel=Channel(message.channel),
        context=ctx,
        config=config,
    )
    message.validation_result = result.as_dict()
    was_edited = (
        message.subject != message.original_subject or message.body != message.original_body
    )
    message.was_edited = was_edited
    if result.valid:
        if message.status in (
            MessageStatus.VALIDATION_FAILED.value,
            MessageStatus.GENERATED.value,
            MessageStatus.DRAFT.value,
        ):
            message.status = (
                MessageStatus.EDITED.value if was_edited else MessageStatus.GENERATED.value
            )
    else:
        message.status = MessageStatus.VALIDATION_FAILED.value
    db.commit()
    return result
