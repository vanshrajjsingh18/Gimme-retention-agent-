"""Integration registry: resolves a channel to a configured adapter."""
from __future__ import annotations

import secrets

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


def resolved_credentials(integration: Integration | None, channel: Channel) -> dict:
    """Credentials for a channel, with the environment taking precedence.

    On a deployed host, secrets belong in the environment rather than in a
    database row that a backup would carry off. Where both exist the
    environment wins, so redeploying with a rotated token takes effect without
    anyone editing a record.
    """
    stored = dict((integration.credentials if integration else None) or {})
    if channel == Channel.SMS:
        stored.update(settings.tnz_env_credentials)
    return stored


def credential_source(integration: Integration | None, channel: Channel) -> dict[str, str]:
    """Where each credential came from, for display. Never the values."""
    env = settings.tnz_env_credentials if channel == Channel.SMS else {}
    stored = (integration.credentials if integration else None) or {}
    sources = {key: "environment" for key in env}
    for key in stored:
        sources.setdefault(key, "database")
    return sources


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
        credentials=resolved_credentials(integration, channel),
        config=integration.config or {},
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


#: Credential holding the shared secret a provider must present on its webhook.
WEBHOOK_SECRET_KEY = "webhook_secret"


def webhook_secret(integration: Integration | None) -> str:
    """The configured webhook secret, from the environment or the database."""
    if integration is not None and Channel(integration.channel) == Channel.SMS:
        from_env = settings.tnz_env_credentials.get(WEBHOOK_SECRET_KEY, "")
        if from_env:
            return from_env
    if integration is None:
        return ""
    return str((integration.credentials or {}).get(WEBHOOK_SECRET_KEY) or "").strip()


def check_webhook_auth(integration: Integration | None, presented: str | None) -> str | None:
    """Decide whether an inbound webhook may be processed.

    Returns ``None`` to allow, or a reason to reject.

    A webhook is not a read-only ping: an inbound reply of STOP suppresses a
    customer, and START restores the marketing consent they withdrew. Both are
    resolved by phone number rather than by a message id we issued, which is
    what makes an unauthenticated endpoint dangerous rather than merely noisy —
    anyone who knows a customer's number could withdraw or, far worse, restore
    their consent.

    So a live integration must carry a secret and the caller must present it.
    Mock mode stays open, because nothing there reaches a real person and local
    development would otherwise need credentials to test a webhook.
    """
    mode = (integration.mode if integration else "mock") or "mock"
    if mode.lower() != "live":
        return None

    expected = webhook_secret(integration)
    if not expected:
        # Fail closed. A live integration with no secret configured is a
        # publicly writable consent endpoint.
        return (
            "This integration is live but has no webhook_secret configured, so "
            "inbound webhooks are refused. Set one here and in the provider's "
            "webhook settings."
        )
    if not presented or not secrets.compare_digest(presented, expected):
        return "Webhook secret missing or incorrect."
    return None
