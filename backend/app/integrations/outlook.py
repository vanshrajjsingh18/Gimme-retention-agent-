"""Microsoft Outlook / Microsoft Graph email adapter.

Uses the client-credentials flow against Microsoft Entra ID and sends via
``POST /v1.0/users/{sender}/sendMail``. Requires an app registration with the
application permission ``Mail.Send`` granted admin consent.

Delivery and engagement signals: Graph does not expose bounce or open events
through ``sendMail``. Production deployments wire those through Exchange
message trace or a mail gateway; ``process_webhook`` accepts the normalized
shape those systems produce.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import httpx

from app.core.enums import Channel, EventType
from app.integrations.base import (
    ConnectionStatus,
    MessagingAdapter,
    NormalizedEvent,
    SendResult,
)

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
LOGIN_BASE = "https://login.microsoftonline.com"


class OutlookGraphAdapter(MessagingAdapter):
    provider = "outlook"
    channel = Channel.EMAIL
    required_credentials = ("tenant_id", "client_id", "client_secret", "sender_address")

    def __init__(self, credentials: dict | None = None, config: dict | None = None) -> None:
        super().__init__(credentials, config)
        self._token: str | None = None
        self._token_expires_at: datetime | None = None

    # ----------------------------------------------------------------------
    def _get_token(self) -> str:
        now = datetime.now(timezone.utc)
        if self._token and self._token_expires_at and now < self._token_expires_at:
            return self._token

        missing = self.missing_credentials()
        if missing:
            raise RuntimeError(f"Missing Outlook credentials: {', '.join(missing)}")

        url = f"{LOGIN_BASE}/{self.credentials['tenant_id']}/oauth2/v2.0/token"
        data = {
            "client_id": self.credentials["client_id"],
            "client_secret": self.credentials["client_secret"],
            "scope": "https://graph.microsoft.com/.default",
            "grant_type": "client_credentials",
        }
        with httpx.Client(timeout=30) as client:
            response = client.post(url, data=data)
        if response.status_code >= 400:
            raise RuntimeError(
                f"Microsoft Entra token request failed with HTTP {response.status_code}."
            )
        payload = response.json()
        self._token = payload["access_token"]
        self._token_expires_at = now + timedelta(seconds=int(payload.get("expires_in", 3600)) - 60)
        return self._token

    # ----------------------------------------------------------------------
    def validate_credentials(self) -> ConnectionStatus:
        missing = self.missing_credentials()
        if missing:
            return ConnectionStatus(
                status="NOT_CONFIGURED",
                mode="live",
                message=f"Missing credentials: {', '.join(missing)}.",
            )
        try:
            self._get_token()
        except (RuntimeError, httpx.HTTPError) as exc:
            return ConnectionStatus(
                status="ERROR", mode="live", message=f"Could not authenticate: {exc}"
            )
        return ConnectionStatus(
            status="OK",
            mode="live",
            message=f"Authenticated with Microsoft Graph as {self.credentials['sender_address']}.",
        )

    def send_message(
        self, *, to: str, subject: str, body: str, metadata: dict | None = None
    ) -> SendResult:
        try:
            token = self._get_token()
        except (RuntimeError, httpx.HTTPError) as exc:
            return SendResult(success=False, error=str(exc))

        sender = self.credentials["sender_address"]
        payload = {
            "message": {
                "subject": subject,
                "body": {"contentType": "Text", "content": body},
                "toRecipients": [{"emailAddress": {"address": to}}],
            },
            "saveToSentItems": True,
        }
        try:
            with httpx.Client(timeout=30) as client:
                response = client.post(
                    f"{GRAPH_BASE}/users/{sender}/sendMail",
                    json=payload,
                    headers={"Authorization": f"Bearer {token}"},
                )
        except httpx.HTTPError as exc:
            return SendResult(success=False, error=f"Could not reach Microsoft Graph: {exc}")

        if response.status_code not in (200, 202):
            return SendResult(
                success=False,
                error=f"Microsoft Graph returned HTTP {response.status_code}.",
            )
        # sendMail returns 202 with no body, so there is no provider message id
        # to correlate against; use the Graph request id where present.
        request_id = response.headers.get("request-id")
        return SendResult(success=True, provider_message_id=request_id or "graph-accepted")

    def fetch_delivery_status(self, provider_message_id: str) -> dict:
        return {
            "provider_message_id": provider_message_id,
            "status": "unknown",
            "message": (
                "Microsoft Graph sendMail does not report per-message delivery status. "
                "Connect Exchange message trace or a mail gateway for delivery events."
            ),
        }

    def process_webhook(self, payload: dict) -> list[NormalizedEvent]:
        """Map a mail-gateway event onto our vocabulary."""
        mapping = {
            "delivered": EventType.EMAIL_DELIVERED,
            "opened": EventType.EMAIL_OPENED,
            "open": EventType.EMAIL_OPENED,
            "clicked": EventType.EMAIL_CLICKED,
            "click": EventType.EMAIL_CLICKED,
            "bounced": EventType.EMAIL_BOUNCED,
            "bounce": EventType.EMAIL_BOUNCED,
            "failed": EventType.MESSAGE_FAILED,
            "unsubscribed": EventType.CUSTOMER_OPTED_OUT,
        }
        raw_events = payload.get("value") or payload.get("events") or [payload]
        results: list[NormalizedEvent] = []
        for raw in raw_events:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("event") or raw.get("eventType") or "").lower()
            event_type = mapping.get(name)
            if event_type is None:
                continue
            results.append(
                NormalizedEvent(
                    event_type=event_type,
                    provider_message_id=raw.get("messageId") or raw.get("message_id"),
                    occurred_at=_parse_ts(raw.get("timestamp") or raw.get("dateTime")),
                    channel=Channel.EMAIL,
                    recipient=raw.get("recipient"),
                    payload=raw,
                )
            )
        return results


def _parse_ts(value) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", ""))
        except ValueError:
            return None
    return None
