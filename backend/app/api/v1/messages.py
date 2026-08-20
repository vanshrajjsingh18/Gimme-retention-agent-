"""Message Studio: generation, variation, editing, validation and approval."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_write
from app.core.database import get_db
from app.core.enums import Channel, MessageStatus
from app.llm.factory import get_llm_provider, provider_mode
from app.llm.prompts import TONE_INSTRUCTIONS
from app.models.base import utcnow
from app.models.entities import AuditLog, Customer, Message, User
from app.schemas.common import OperationResult
from app.schemas.models import (
    GenerateMessageRequest,
    MessageEditRequest,
    MessageOut,
    SendTestRequest,
)
from app.services.messaging import generate_message, revalidate_message

router = APIRouter()


@router.get("/messages/variations", tags=["messages"])
def list_variations(_: User = Depends(get_current_user)) -> dict:
    """Tone variations the Message Studio can request."""
    return {
        "variations": [
            {"key": key, "instruction": instruction}
            for key, instruction in TONE_INSTRUCTIONS.items()
        ]
    }


@router.get("/messages/llm-status", tags=["messages"])
def llm_status(_: User = Depends(get_current_user)) -> dict:
    provider = get_llm_provider()
    return {**provider.health(), "resolved_mode": provider_mode()}


@router.post("/messages/generate", response_model=MessageOut, tags=["messages"])
def generate(
    payload: GenerateMessageRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_write),
) -> MessageOut:
    """Generate a grounded message for one customer."""
    customer = db.get(Customer, payload.customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer not found.")

    message = generate_message(
        db,
        customer,
        channel=payload.channel,
        objective=payload.objective,
        variation=payload.variation,
        campaign_id=payload.campaign_id,
    )
    db.add(
        AuditLog(
            actor=user.email,
            action="MESSAGE_GENERATED",
            entity_type="message",
            entity_id=str(message.id),
            detail={
                "customer_id": customer.id,
                "channel": payload.channel.value,
                "variation": payload.variation,
                "valid": message.validation_result.get("valid"),
            },
        )
    )
    db.commit()
    return MessageOut.model_validate(message)


@router.get("/messages", response_model=list[MessageOut], tags=["messages"])
def list_messages(
    customer_id: int | None = None,
    campaign_id: int | None = None,
    status: str | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> list[MessageOut]:
    stmt = select(Message)
    if customer_id is not None:
        stmt = stmt.where(Message.customer_id == customer_id)
    if campaign_id is not None:
        stmt = stmt.where(Message.campaign_id == campaign_id)
    if status:
        stmt = stmt.where(Message.status == status.upper())
    # Filters before the limit, so a filtered page is not silently empty.
    stmt = stmt.order_by(Message.created_at.desc()).limit(limit)
    return [MessageOut.model_validate(m) for m in db.execute(stmt).scalars().all()]


@router.get("/messages/{message_id}", response_model=MessageOut, tags=["messages"])
def get_message(
    message_id: int, db: Session = Depends(get_db), _: User = Depends(get_current_user)
) -> MessageOut:
    message = db.get(Message, message_id)
    if message is None:
        raise HTTPException(status_code=404, detail="Message not found.")
    return MessageOut.model_validate(message)


@router.patch("/messages/{message_id}", response_model=MessageOut, tags=["messages"])
def edit_message(
    message_id: int,
    payload: MessageEditRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_write),
) -> MessageOut:
    """Edit a message. Editing always re-runs validation and clears approval."""
    message = db.get(Message, message_id)
    if message is None:
        raise HTTPException(status_code=404, detail="Message not found.")
    if message.status == MessageStatus.SENT.value:
        raise HTTPException(status_code=400, detail="A sent message cannot be edited.")

    if payload.subject is not None:
        message.subject = payload.subject
    if payload.body is not None:
        message.body = payload.body

    # An edit invalidates any prior approval; the edited copy must be
    # re-approved before it can be sent.
    message.approved_by_id = None
    message.approved_at = None
    db.commit()

    revalidate_message(db, message)
    db.add(
        AuditLog(
            actor=user.email,
            action="MESSAGE_EDITED",
            entity_type="message",
            entity_id=str(message_id),
            detail={"valid_after_edit": message.validation_result.get("valid")},
        )
    )
    db.commit()
    db.refresh(message)
    return MessageOut.model_validate(message)


@router.post("/messages/{message_id}/validate", response_model=MessageOut, tags=["messages"])
def validate(
    message_id: int, db: Session = Depends(get_db), _: User = Depends(require_write)
) -> MessageOut:
    message = db.get(Message, message_id)
    if message is None:
        raise HTTPException(status_code=404, detail="Message not found.")
    revalidate_message(db, message)
    db.refresh(message)
    return MessageOut.model_validate(message)


@router.post("/messages/{message_id}/approve", response_model=MessageOut, tags=["messages"])
def approve(
    message_id: int, db: Session = Depends(get_db), user: User = Depends(require_write)
) -> MessageOut:
    """Approve a message. Blocked while validation is failing."""
    message = db.get(Message, message_id)
    if message is None:
        raise HTTPException(status_code=404, detail="Message not found.")

    result = revalidate_message(db, message)
    if not result.valid:
        raise HTTPException(
            status_code=400,
            detail=(
                "This message cannot be approved while validation is failing: "
                + "; ".join(f.message for f in result.findings)
            ),
        )

    message.status = MessageStatus.APPROVED.value
    message.approved_by_id = user.id
    message.approved_at = utcnow()
    db.add(
        AuditLog(
            actor=user.email,
            action="MESSAGE_APPROVED",
            entity_type="message",
            entity_id=str(message_id),
        )
    )
    db.commit()
    db.refresh(message)
    return MessageOut.model_validate(message)


@router.post("/messages/{message_id}/reject", response_model=MessageOut, tags=["messages"])
def reject(
    message_id: int, db: Session = Depends(get_db), user: User = Depends(require_write)
) -> MessageOut:
    message = db.get(Message, message_id)
    if message is None:
        raise HTTPException(status_code=404, detail="Message not found.")
    message.status = MessageStatus.REJECTED.value
    message.approved_by_id = None
    message.approved_at = None
    db.add(
        AuditLog(
            actor=user.email,
            action="MESSAGE_REJECTED",
            entity_type="message",
            entity_id=str(message_id),
        )
    )
    db.commit()
    db.refresh(message)
    return MessageOut.model_validate(message)


@router.post("/messages/{message_id}/send-test", tags=["messages"])
def send_test(
    message_id: int,
    payload: SendTestRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_write),
) -> dict:
    """Send this message to a single test address."""
    from app.integrations.mock_adapters import BaseMockAdapter
    from app.integrations.registry import get_adapter

    message = db.get(Message, message_id)
    if message is None:
        raise HTTPException(status_code=404, detail="Message not found.")

    channel = Channel(message.channel)
    adapter = get_adapter(db, channel)
    result = adapter.send_message(
        to=payload.to, subject=message.subject, body=message.body, metadata={"is_test": True}
    )

    test_copy = Message(
        customer_id=message.customer_id,
        campaign_id=message.campaign_id,
        channel=message.channel,
        objective=message.objective,
        subject=message.subject,
        body=message.body,
        original_subject=message.subject,
        original_body=message.body,
        status=MessageStatus.SENT.value if result.success else MessageStatus.FAILED.value,
        provider=adapter.provider,
        provider_message_id=result.provider_message_id,
        is_test=True,
        sent_at=utcnow() if result.success else None,
        error_message=result.error,
    )
    db.add(test_copy)
    db.add(
        AuditLog(
            actor=user.email,
            action="TEST_MESSAGE_SENT",
            entity_type="message",
            entity_id=str(message_id),
            detail={"channel": message.channel, "success": result.success},
        )
    )
    db.commit()

    return {
        "success": result.success,
        "provider": adapter.provider,
        "is_simulated": isinstance(adapter, BaseMockAdapter),
        "provider_message_id": result.provider_message_id,
        "error": result.error,
    }
