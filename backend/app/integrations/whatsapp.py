"""Configurable WhatsApp provider adapter.

WhatsApp Business messaging is offered by several providers with near-identical
request shapes (Meta Cloud API, Twilio, 360dialog, Infobip). Rather than
hard-coding one, this adapter is driven by a small provider profile stored in
the integration's ``config``, so switching provider is a settings change.

Defaults target the Meta WhatsApp Cloud API.
"""
from __future__ import annotations

import logging
from datetime import datetime

import httpx

from app.core.enums import Channel, EventType
from app.integrations.base import (
    ConnectionStatus,
    MessagingAdapter,
    NormalizedEvent,
    SendResult,
)

logger = logging.getLogger(__name__)

PROVIDER_PROFILES: dict[str, dict] = {
    "meta_cloud": {
        "label": "Meta WhatsApp Cloud API",
        "base_url": "https://graph.facebook.com/v21.0",
        "send_path": "/{phone_number_id}/messages",
        "auth": "bearer",
        "required": ["access_token", "phone_number_id"],
    },
    "twilio": {
        "label": "Twilio WhatsApp",
        "base_url": "https://api.twilio.com/2010-04-01",
        "send_path": "/Accounts/{account_sid}/Messages.json",
        "auth": "basic",
        "required": ["account_sid", "auth_token", "from_number"],
    },
    "360dialog": {
        "label": "360dialog WhatsApp",
        "base_url": "https://waba-v2.360dialog.io",
        "send_path": "/messages",
        "auth": "d360",
        "required": ["api_key"],
    },
}

DEFAULT_PROFILE = "meta_cloud"


class WhatsAppAdapter(MessagingAdapter):
    provider = "whatsapp"
    channel = Channel.WHATSAPP

    @property
    def profile_key(self) -> str:
        return str(self.config.get("profile") or DEFAULT_PROFILE)

    @property
    def profile(self) -> dict:
        return PROVIDER_PROFILES.get(self.profile_key, PROVIDER_PROFILES[DEFAULT_PROFILE])

    @property
    def required_credentials(self) -> tuple[str, ...]:  # type: ignore[override]
        return tuple(self.profile["required"])

    @property
    def base_url(self) -> str:
        return str(self.config.get("base_url") or self.profile["base_url"]).rstrip("/")

    def _send_url(self) -> str:
        path = self.profile["send_path"].format(**self.credentials)
        return f"{self.base_url}{path}"

    def _auth(self) -> tuple[dict, tuple | None]:
        """Return ``(headers, basic_auth)`` for the configured profile."""
        auth_style = self.profile["auth"]
        if auth_style == "bearer":
            return (
                {
                    "Authorization": f"Bearer {self.credentials.get('access_token', '')}",
                    "Content-Type": "application/json",
                },
                None,
            )
        if auth_style == "d360":
            return (
                {
                    "D360-API-KEY": self.credentials.get("api_key", ""),
                    "Content-Type": "application/json",
                },
                None,
            )
        return (
            {"Content-Type": "application/x-www-form-urlencoded"},
            (self.credentials.get("account_sid", ""), self.credentials.get("auth_token", "")),
        )

    # ----------------------------------------------------------------------
    def validate_credentials(self) -> ConnectionStatus:
        missing = self.missing_credentials()
        if missing:
            return ConnectionStatus(
                status="NOT_CONFIGURED",
                mode="live",
                message=f"Missing credentials for {self.profile['label']}: {', '.join(missing)}.",
                details={"profile": self.profile_key},
            )
        return ConnectionStatus(
            status="OK",
            mode="live",
            message=(
                f"{self.profile['label']} credentials are present. Sends will go to "
                f"{self.base_url}."
            ),
            details={"profile": self.profile_key},
        )

    def send_message(
        self, *, to: str, subject: str, body: str, metadata: dict | None = None
    ) -> SendResult:
        missing = self.missing_credentials()
        if missing:
            return SendResult(
                success=False,
                error=f"Missing WhatsApp credentials: {', '.join(missing)}.",
            )

        headers, basic = self._auth()
        try:
            with httpx.Client(timeout=30) as client:
                if self.profile_key == "twilio":
                    response = client.post(
                        self._send_url(),
                        data={
                            "From": f"whatsapp:{self.credentials['from_number']}",
                            "To": f"whatsapp:{to}",
                            "Body": body,
                        },
                        headers=headers,
                        auth=basic,
                    )
                else:
                    response = client.post(
                        self._send_url(),
                        json={
                            "messaging_product": "whatsapp",
                            "recipient_type": "individual",
                            "to": to,
                            "type": "text",
                            "text": {"body": body},
                        },
                        headers=headers,
                    )
        except httpx.HTTPError as exc:
            return SendResult(success=False, error=f"Could not reach the WhatsApp provider: {exc}")

        if response.status_code >= 400:
            return SendResult(
                success=False,
                error=f"WhatsApp provider returned HTTP {response.status_code}.",
            )
        try:
            data = response.json()
        except ValueError:
            data = {}
        message_id = (
            (data.get("messages") or [{}])[0].get("id")
            if isinstance(data.get("messages"), list)
            else data.get("sid") or data.get("id")
        )
        return SendResult(success=True, provider_message_id=message_id, raw={"status": "accepted"})

    def fetch_delivery_status(self, provider_message_id: str) -> dict:
        return {
            "provider_message_id": provider_message_id,
            "status": "unknown",
            "message": (
                "WhatsApp providers report delivery and read receipts by webhook rather "
                "than polling. Point the provider at /api/v1/webhooks/whatsapp."
            ),
        }

    def process_webhook(self, payload: dict) -> list[NormalizedEvent]:
        """Normalize both Meta Cloud and Twilio-shaped webhook bodies."""
        mapping = {
            "sent": EventType.WHATSAPP_SENT,
            "delivered": EventType.WHATSAPP_DELIVERED,
            "read": EventType.WHATSAPP_READ,
            "failed": EventType.MESSAGE_FAILED,
            "undelivered": EventType.MESSAGE_FAILED,
        }
        results: list[NormalizedEvent] = []

        # Meta Cloud API shape
        for entry in payload.get("entry", []) or []:
            for change in entry.get("changes", []) or []:
                value = change.get("value", {}) or {}
                for status in value.get("statuses", []) or []:
                    event_type = mapping.get(str(status.get("status", "")).lower())
                    if event_type:
                        results.append(
                            NormalizedEvent(
                                event_type=event_type,
                                provider_message_id=status.get("id"),
                                occurred_at=_parse_epoch(status.get("timestamp")),
                                channel=Channel.WHATSAPP,
                                recipient=status.get("recipient_id"),
                                payload=status,
                            )
                        )
                for message in value.get("messages", []) or []:
                    results.append(
                        NormalizedEvent(
                            event_type=EventType.WHATSAPP_REPLIED,
                            provider_message_id=message.get("context", {}).get("id"),
                            occurred_at=_parse_epoch(message.get("timestamp")),
                            channel=Channel.WHATSAPP,
                            recipient=message.get("from"),
                            payload=message,
                        )
                    )

        # Twilio shape
        twilio_status = str(payload.get("MessageStatus", "")).lower()
        if twilio_status and twilio_status in mapping:
            results.append(
                NormalizedEvent(
                    event_type=mapping[twilio_status],
                    provider_message_id=payload.get("MessageSid"),
                    occurred_at=None,
                    channel=Channel.WHATSAPP,
                    recipient=payload.get("To"),
                    payload=payload,
                )
            )

        # Generic shape (also used by the mock webhook endpoint)
        generic = str(payload.get("event", "")).lower()
        if not results and generic in mapping:
            results.append(
                NormalizedEvent(
                    event_type=mapping[generic],
                    provider_message_id=payload.get("message_id"),
                    occurred_at=None,
                    channel=Channel.WHATSAPP,
                    recipient=payload.get("recipient"),
                    payload=payload,
                )
            )
        return results


def _parse_epoch(value) -> datetime | None:
    try:
        return datetime.utcfromtimestamp(int(value))
    except (TypeError, ValueError):
        return None
