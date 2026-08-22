"""TNZ Group SMS adapter.

TNZ's REST API (https://www.tnz.co.nz) accepts a JSON send payload
authenticated with an auth token or basic credentials. Delivery receipts are
returned via a status endpoint and/or a configured webhook.
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

DEFAULT_BASE_URL = "https://api.tnz.co.nz"


class TnzSmsAdapter(MessagingAdapter):
    provider = "tnz"
    channel = Channel.SMS
    required_credentials = ("auth_token", "sender")

    @property
    def base_url(self) -> str:
        return str(self.config.get("base_url") or DEFAULT_BASE_URL).rstrip("/")

    def _headers(self) -> dict:
        return {
            "Authorization": f"Basic {self.credentials.get('auth_token', '')}",
            "Content-Type": "application/json",
        }

    def validate_credentials(self) -> ConnectionStatus:
        missing = self.missing_credentials()
        if missing:
            return ConnectionStatus(
                status="NOT_CONFIGURED",
                mode="live",
                message=f"Missing credentials: {', '.join(missing)}.",
            )
        try:
            with httpx.Client(timeout=20) as client:
                response = client.get(f"{self.base_url}/api/v2.04/get/sms/status", headers=self._headers())
        except httpx.HTTPError as exc:
            return ConnectionStatus(
                status="ERROR", mode="live", message=f"Could not reach TNZ: {exc}"
            )
        if response.status_code in (401, 403):
            return ConnectionStatus(
                status="ERROR", mode="live", message="TNZ rejected the supplied credentials."
            )
        return ConnectionStatus(
            status="OK", mode="live", message=f"Connected to TNZ at {self.base_url}."
        )

    def send_message(
        self, *, to: str, subject: str, body: str, metadata: dict | None = None
    ) -> SendResult:
        missing = self.missing_credentials()
        if missing:
            return SendResult(
                success=False, error=f"Missing TNZ credentials: {', '.join(missing)}."
            )

        payload = {
            "MessageType": "SMS",
            "Reference": (metadata or {}).get("reference", ""),
            "SendMode": "Immediate",
            "MessageData": body,
            "Destinations": [{"Recipient": to}],
            "FromNumber": self.credentials.get("sender", ""),
        }
        try:
            with httpx.Client(timeout=30) as client:
                response = client.post(
                    f"{self.base_url}/api/v2.04/send/sms",
                    json=payload,
                    headers=self._headers(),
                )
        except httpx.HTTPError as exc:
            return SendResult(success=False, error=f"Could not reach TNZ: {exc}")

        if response.status_code >= 400:
            return SendResult(
                success=False, error=f"TNZ returned HTTP {response.status_code}."
            )
        try:
            data = response.json()
        except ValueError:
            data = {}
        message_id = str(
            data.get("MessageID") or data.get("JobNum") or data.get("Reference") or ""
        )
        return SendResult(
            success=True, provider_message_id=message_id or None, raw={"status": "accepted"}
        )

    def fetch_delivery_status(self, provider_message_id: str) -> dict:
        try:
            with httpx.Client(timeout=20) as client:
                response = client.get(
                    f"{self.base_url}/api/v2.04/get/sms/status",
                    params={"MessageID": provider_message_id},
                    headers=self._headers(),
                )
            if response.status_code >= 400:
                return {
                    "provider_message_id": provider_message_id,
                    "status": "error",
                    "message": f"TNZ returned HTTP {response.status_code}.",
                }
            return {"provider_message_id": provider_message_id, **response.json()}
        except (httpx.HTTPError, ValueError) as exc:
            return {
                "provider_message_id": provider_message_id,
                "status": "error",
                "message": str(exc),
            }

    def process_webhook(self, payload: dict) -> list[NormalizedEvent]:
        mapping = {
            "delivered": EventType.SMS_DELIVERED,
            "sent": EventType.SMS_SENT,
            "failed": EventType.SMS_FAILED,
            "undelivered": EventType.SMS_FAILED,
            "rejected": EventType.SMS_FAILED,
            "optout": EventType.CUSTOMER_OPTED_OUT,
            "stop": EventType.CUSTOMER_OPTED_OUT,
        }
        raw_events = payload.get("Results") or payload.get("events") or [payload]
        results: list[NormalizedEvent] = []
        for raw in raw_events:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("Status") or raw.get("status") or raw.get("event") or "").lower()
            event_type = mapping.get(name)
            if event_type is None:
                # An inbound reply carries no status, just the text the
                # customer sent. A STOP arrives this way, so it must be read
                # here or opt-outs would be silently dropped.
                event_type = _reply_event(raw)
            if event_type is None:
                continue
            results.append(
                NormalizedEvent(
                    event_type=event_type,
                    provider_message_id=str(
                        raw.get("MessageID") or raw.get("message_id") or ""
                    )
                    or None,
                    occurred_at=_parse_ts(raw.get("Timestamp") or raw.get("timestamp")),
                    channel=Channel.SMS,
                    recipient=raw.get("Recipient") or raw.get("recipient"),
                    payload=raw,
                )
            )
        return results


#: Fields TNZ may carry an inbound reply body in.
REPLY_FIELDS = ("Reply", "reply", "MessageText", "message_text", "Body", "body", "Text", "text")


def reply_body(raw: dict) -> str | None:
    """The customer's reply text, if this payload is an inbound message."""
    for field in REPLY_FIELDS:
        value = raw.get(field)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _reply_event(raw: dict) -> EventType | None:
    """Classify an inbound reply as an opt-out, an opt-in, or neither."""
    body = reply_body(raw)
    if body is None:
        return None
    # Imported here: the opt-out service imports model code, and adapters are
    # deliberately model-free.
    from app.services.optout import is_opt_in, is_opt_out

    if is_opt_out(body):
        return EventType.CUSTOMER_OPTED_OUT
    if is_opt_in(body):
        return EventType.CUSTOMER_REACTIVATED
    return None


def _parse_ts(value) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", ""))
        except ValueError:
            return None
    return None
