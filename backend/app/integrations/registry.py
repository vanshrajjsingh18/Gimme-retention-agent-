"""Integration registry: resolves a channel to a configured adapter."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.enums import Channel
from app.integrations.base import MessagingAdapter
from app.integrations.mock_adapters import (
    MockOutlookAdapter,
    MockTnzAdapter,
    MockWhatsAppAdapter,
)
from app.integrations.outlook import OutlookGraphAdapter
from app.integrations.tnz import TnzSmsAdapter
from app.integrations.whatsapp import WhatsAppAdapter
from app.models.entities import Integration

LIVE_ADAPTERS: dict[Channel, type[MessagingAdapter]] = {
    Channel.EMAIL: OutlookGraphAdapter,
    Channel.SMS: TnzSmsAdapter,
    Channel.WHATSAPP: WhatsAppAdapter,
}

MOCK_ADAPTERS: dict[Channel, type[MessagingAdapter]] = {
    Channel.EMAIL: MockOutlookAdapter,
    Channel.SMS: MockTnzAdapter,
    Channel.WHATSAPP: MockWhatsAppAdapter,
    Channel.PUSH: MockOutlookAdapter,
}

DEFAULT_INTEGRATIONS: list[dict] = [
    {
        "provider": "outlook",
        "channel": Channel.EMAIL.value,
        "display_name": "Microsoft Outlook (Microsoft Graph)",
        "mode": "mock",
        "config": {},
    },
    {
        "provider": "tnz",
        "channel": Channel.SMS.value,
        "display_name": "TNZ Group SMS",
        "mode": "mock",
        "config": {},
    },
    {
        "provider": "whatsapp",
        "channel": Channel.WHATSAPP.value,
        "display_name": "WhatsApp Business",
        "mode": "mock",
        "config": {"profile": "meta_cloud"},
    },
]

ENV_MODE_BY_CHANNEL = {
    Channel.EMAIL: settings.EMAIL_PROVIDER_MODE,
    Channel.SMS: settings.SMS_PROVIDER_MODE,
    Channel.WHATSAPP: settings.WHATSAPP_PROVIDER_MODE,
}


def ensure_default_integrations(db: Session) -> int:
    created = 0
    for spec in DEFAULT_INTEGRATIONS:
        exists = db.execute(
            select(Integration.id).where(Integration.provider == spec["provider"])
        ).first()
        if exists:
            continue
        db.add(
            Integration(
                **spec,
                credentials={},
                status="MOCK",
                status_message="Running in MOCK MODE. No external credentials configured.",
            )
        )
        created += 1
    db.commit()
    return created


def get_integration(db: Session, channel: Channel) -> Integration | None:
    return db.execute(
        select(Integration).where(Integration.channel == channel.value)
    ).scalar_one_or_none()


def get_adapter(db: Session, channel: Channel) -> MessagingAdapter:
    """Resolve the adapter for a channel.

    Live adapters are only used when the integration is explicitly set to
    ``live`` *and* every required credential is present. Anything else falls
    back to the mock adapter, so a half-configured integration can never
    silently drop messages.
    """
    integration = get_integration(db, channel)
    if integration is None:
        return MOCK_ADAPTERS.get(channel, MockOutlookAdapter)()

    mode = (integration.mode or "mock").lower()
    if mode != "live":
        return MOCK_ADAPTERS.get(channel, MockOutlookAdapter)(
            credentials={}, config=integration.config or {}
        )

    adapter_cls = LIVE_ADAPTERS.get(channel)
    if adapter_cls is None:
        return MOCK_ADAPTERS.get(channel, MockOutlookAdapter)()

    adapter = adapter_cls(
        credentials=integration.credentials or {}, config=integration.config or {}
    )
    if adapter.missing_credentials():
        return MOCK_ADAPTERS.get(channel, MockOutlookAdapter)(
            credentials={}, config=integration.config or {}
        )
    return adapter


def mask_credentials(credentials: dict) -> dict:
    """Return credentials safe to send to the frontend.

    Values are replaced with a presence flag and a short suffix; secrets never
    leave the backend.
    """
    masked = {}
    for key, value in (credentials or {}).items():
        text = str(value or "")
        if not text:
            masked[key] = {"configured": False, "hint": ""}
        elif len(text) <= 4:
            masked[key] = {"configured": True, "hint": "****"}
        else:
            masked[key] = {"configured": True, "hint": f"****{text[-4:]}"}
    return masked
