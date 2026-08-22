"""Integration configuration, connection tests and provider webhooks."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_admin, require_write
from app.core.database import get_db
from app.automations.delivery import apply_delivery_event, find_customer_by_contact
from app.core.enums import Channel, EventType
from app.integrations.base import MessagingAdapter
from app.integrations.mock_adapters import BaseMockAdapter
from app.integrations.registry import (
    LIVE_ADAPTERS,
    MOCK_ADAPTERS,
    get_adapter,
    mask_credentials,
)
from app.integrations.whatsapp import PROVIDER_PROFILES
from app.llm.factory import get_llm_provider
from app.models.base import utcnow
from app.models.entities import (
    AuditLog,
    CommunicationEvent,
    Customer,
    Integration,
    Message,
    User,
)
from app.schemas.models import (
    IntegrationOut,
    IntegrationTestMessage,
    IntegrationUpdate,
)
from app.services.events import make_idempotency_key, record_communication_event
from app.services.optout import apply_global_opt_out, apply_opt_in

logger = logging.getLogger(__name__)

router = APIRouter()


def _required_for(integration: Integration) -> list[str]:
    adapter_cls = LIVE_ADAPTERS.get(Channel(integration.channel))
    if adapter_cls is None:
        return []
    adapter = adapter_cls(credentials=integration.credentials or {}, config=integration.config or {})
    return list(adapter.required_credentials)


def _out(integration: Integration) -> IntegrationOut:
    return IntegrationOut(
        id=integration.id,
        provider=integration.provider,
        channel=integration.channel,
        display_name=integration.display_name,
        mode=integration.mode,
        enabled=integration.enabled,
        status=integration.status,
        status_message=integration.status_message,
        last_checked_at=integration.last_checked_at,
        config=integration.config or {},
        credentials=mask_credentials(integration.credentials or {}),
        required_credentials=_required_for(integration),
    )


@router.get("/integrations", response_model=list[IntegrationOut], tags=["integrations"])
def list_integrations(
    db: Session = Depends(get_db), _: User = Depends(get_current_user)
) -> list[IntegrationOut]:
    integrations = db.execute(select(Integration).order_by(Integration.channel)).scalars().all()
    return [_out(i) for i in integrations]


@router.get("/integrations/whatsapp-profiles", tags=["integrations"])
def whatsapp_profiles(_: User = Depends(get_current_user)) -> dict:
    """Supported WhatsApp provider profiles and their required credentials."""
    return {
        "profiles": [
            {
                "key": key,
                "label": profile["label"],
                "base_url": profile["base_url"],
                "required_credentials": profile["required"],
            }
            for key, profile in PROVIDER_PROFILES.items()
        ]
    }


@router.get("/integrations/llm", tags=["integrations"])
def llm_integration(_: User = Depends(get_current_user)) -> dict:
    return get_llm_provider().health()


@router.patch(
    "/integrations/{integration_id}", response_model=IntegrationOut, tags=["integrations"]
)
def update_integration(
    integration_id: int,
    payload: IntegrationUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
) -> IntegrationOut:
    """Update an integration.

    Credentials are merged rather than replaced, so a partial update does not
    wipe secrets. An empty string clears a single credential.
    """
    integration = db.get(Integration, integration_id)
    if integration is None:
        raise HTTPException(status_code=404, detail="Integration not found.")

    changes = payload.model_dump(exclude_none=True)
    credentials = changes.pop("credentials", None)
    for key, value in changes.items():
        setattr(integration, key, value)

    if credentials is not None:
        merged = dict(integration.credentials or {})
        for key, value in credentials.items():
            if value == "":
                merged.pop(key, None)
            else:
                merged[key] = value
        integration.credentials = merged

    if integration.mode == "mock":
        integration.status = "MOCK"
        integration.status_message = (
            "Running in MOCK MODE. Messages are recorded locally and never sent."
        )
    else:
        integration.status = "NOT_CHECKED"
        integration.status_message = "Credentials updated. Run a connection test to verify."

    db.add(
        AuditLog(
            actor=user.email,
            action="INTEGRATION_UPDATED",
            entity_type="integration",
            entity_id=integration.provider,
            # Credential *keys* only; values are never written to the audit log.
            detail={
                "fields": sorted(changes),
                "credential_keys": sorted(credentials) if credentials else [],
            },
        )
    )
    db.commit()
    db.refresh(integration)
    return _out(integration)


@router.post("/integrations/{integration_id}/test-connection", tags=["integrations"])
def test_connection(
    integration_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_write),
) -> dict:
    integration = db.get(Integration, integration_id)
    if integration is None:
        raise HTTPException(status_code=404, detail="Integration not found.")

    adapter = get_adapter(db, Channel(integration.channel))
    status = adapter.validate_credentials()

    integration.status = status.status
    integration.status_message = status.message
    integration.last_checked_at = utcnow()
    db.commit()

    return {
        "provider": adapter.provider,
        "status": status.status,
        "message": status.message,
        "mode": status.mode,
        "is_mock": isinstance(adapter, BaseMockAdapter),
        "details": status.details,
    }


@router.post("/integrations/{integration_id}/test-message", tags=["integrations"])
def test_message(
    integration_id: int,
    payload: IntegrationTestMessage,
    db: Session = Depends(get_db),
    user: User = Depends(require_write),
) -> dict:
    integration = db.get(Integration, integration_id)
    if integration is None:
        raise HTTPException(status_code=404, detail="Integration not found.")

    channel = Channel(integration.channel)
    adapter = get_adapter(db, channel)
    result = adapter.send_test_message(
        to=payload.to, subject=payload.subject, body=payload.body
    )

    db.add(
        Message(
            channel=channel.value,
            objective="TEST",
            subject=payload.subject,
            body=payload.body,
            original_subject=payload.subject,
            original_body=payload.body,
            status="SENT" if result.success else "FAILED",
            provider=adapter.provider,
            provider_message_id=result.provider_message_id,
            is_test=True,
            sent_at=utcnow() if result.success else None,
            error_message=result.error,
        )
    )
    db.add(
        AuditLog(
            actor=user.email,
            action="INTEGRATION_TEST_MESSAGE",
            entity_type="integration",
            entity_id=integration.provider,
            detail={"success": result.success, "channel": channel.value},
        )
    )
    db.commit()

    return {
        "success": result.success,
        "provider": adapter.provider,
        "is_mock": isinstance(adapter, BaseMockAdapter),
        "provider_message_id": result.provider_message_id,
        "error": result.error,
    }


# --------------------------------------------------------------------------
# Webhooks
# --------------------------------------------------------------------------
CHANNEL_BY_WEBHOOK = {
    "outlook": Channel.EMAIL,
    "email": Channel.EMAIL,
    "tnz": Channel.SMS,
    "sms": Channel.SMS,
    "whatsapp": Channel.WHATSAPP,
}


@router.post("/webhooks/{provider}", tags=["integrations"])
async def receive_webhook(
    provider: str, request: Request, db: Session = Depends(get_db)
) -> dict:
    """Receive a provider webhook and record normalized events.

    Webhooks are unauthenticated by design (providers post from their own
    infrastructure), so this endpoint only ever *records* events for messages
    it already knows about — an unknown message ID is ignored rather than
    creating new records.
    """
    channel = CHANNEL_BY_WEBHOOK.get(provider.lower())
    if channel is None:
        raise HTTPException(
            status_code=404,
            detail=f"No webhook handler for provider '{provider}'.",
        )

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 - providers occasionally post form bodies
        form = await request.form()
        payload = dict(form)

    adapter = get_adapter(db, channel)
    events = adapter.process_webhook(payload)

    recorded = 0
    ignored = 0
    opt_outs = 0
    for event in events:
        message = None
        if event.provider_message_id:
            message = db.execute(
                select(Message).where(
                    Message.provider_message_id == event.provider_message_id
                )
            ).scalar_one_or_none()

        if event.event_type in (
            EventType.CUSTOMER_OPTED_OUT,
            EventType.CUSTOMER_REACTIVATED,
        ):
            # An opt-out is honoured even when the message it replies to
            # cannot be found: withdrawal of permission must never depend on
            # a provider echoing our own message id back correctly.
            customer = (
                db.get(Customer, message.customer_id)
                if message and message.customer_id
                else find_customer_by_contact(db, event.recipient, channel=channel)
            )
            if customer is not None:
                if event.event_type == EventType.CUSTOMER_OPTED_OUT:
                    apply_global_opt_out(
                        db,
                        customer,
                        source=f"{provider}_webhook",
                        channel=channel,
                        occurred_at=event.occurred_at or utcnow(),
                        commit=False,
                    )
                else:
                    apply_opt_in(
                        db,
                        customer,
                        source=f"{provider}_webhook",
                        occurred_at=event.occurred_at or utcnow(),
                        commit=False,
                    )
                opt_outs += 1

        if message is None:
            ignored += 1
            continue

        apply_delivery_event(
            db,
            event_type=event.event_type,
            provider_message_id=event.provider_message_id,
            message_id=message.id,
            occurred_at=event.occurred_at or utcnow(),
            error=(event.payload or {}).get("error"),
        )

        created = record_communication_event(
            db,
            event_type=event.event_type,
            customer_id=message.customer_id,
            campaign_id=message.campaign_id,
            message_id=message.id,
            channel=channel,
            provider=adapter.provider,
            occurred_at=event.occurred_at or utcnow(),
            is_simulated=isinstance(adapter, BaseMockAdapter),
            payload=event.payload,
            idempotency_key=make_idempotency_key(
                "webhook", event.provider_message_id, event.event_type.value,
                (event.occurred_at or utcnow()).isoformat(),
            ),
        )
        if created is not None:
            recorded += 1
    db.commit()

    return {
        "provider": provider,
        "events_received": len(events),
        "events_recorded": recorded,
        "events_ignored_unknown_message": ignored,
        "consent_changes_applied": opt_outs,
    }
