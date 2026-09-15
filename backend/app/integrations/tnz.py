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

#: Enough of TNZ's reply to diagnose the failure, not so much that an HTML
#: error page fills the message log or the dashboard toast.
MAX_PROVIDER_ERROR = 300

#: Keys TNZ has been observed to put its human-readable reason under. The
#: casing varies between endpoints, so all the plausible spellings are tried.
#: ErrorMessage first: it is the key TNZ's v2.04 messaging endpoints actually
#: use, and it holds the sentence worth reading. The rest are fallbacks for the
#: other endpoints, whose casing is not consistent with it.
_ERROR_KEYS = (
    "ErrorMessage", "errorMessage", "Error", "error", "Message", "message",
    "Reason", "reason", "Detail", "detail", "Status", "status",
)


def _join_error_message(value: object) -> str:
    """TNZ's ErrorMessage is a list of sentences, not a string.

    Treating it as a string meant it failed the isinstance check and fell
    through to dumping the whole JSON object at the operator, so the one
    readable sentence in the reply arrived wrapped in Python dict syntax.
    """
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        parts = [str(v).strip() for v in value if str(v).strip()]
        return " ".join(parts)
    return ""


def describe_http_error(response: httpx.Response) -> str:
    """TNZ's own words for why it refused, prefixed with the status code.

    A bare "TNZ returned HTTP 400" is almost useless: 400 is what TNZ returns
    for every malformed or incomplete payload, and it names the offending
    field in the body. Discarding that turned a one-line fix into guesswork,
    so the body is now carried through to whoever is reading the log.
    """
    prefix = f"TNZ returned HTTP {response.status_code}"
    try:
        payload = response.json()
    except ValueError:
        payload = None

    detail = ""
    if isinstance(payload, dict):
        for key in _ERROR_KEYS:
            joined = _join_error_message(payload.get(key))
            if joined:
                detail = joined
                break
        else:
            # No recognised key: the whole object is more use than nothing.
            detail = str(payload)
    elif isinstance(payload, str) and payload.strip():
        detail = payload.strip()
    elif payload is None:
        detail = (response.text or "").strip()

    if not detail:
        return f"{prefix}."
    detail = " ".join(detail.split())
    if len(detail) > MAX_PROVIDER_ERROR:
        detail = detail[:MAX_PROVIDER_ERROR].rstrip() + "…"
    return f"{prefix}: {detail}"


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
                status="ERROR",
                mode="live",
                message="TNZ rejected the supplied credentials.",
            )
        # Anything else in the 4xx/5xx range is not a working connection either.
        # Reporting OK here told the go-live checklist that TNZ was reachable
        # and happy when it had in fact refused the request, which is exactly
        # the false confidence this checklist exists to prevent.
        if response.status_code >= 400:
            return ConnectionStatus(
                status="ERROR", mode="live", message=describe_http_error(response)
            )
        return ConnectionStatus(
            status="OK", mode="live", message=f"Connected to TNZ at {self.base_url}."
        )

    def _message_data(self, *, to: str, body: str, metadata: dict | None) -> dict:
        """The one object TNZ's v2.04 send endpoint actually wants.

        Authenticated with an auth token, the whole request body is
        ``{"MessageData": {...}}`` and every field lives inside it — the
        message, the recipients and the sender alike. An earlier version of
        this adapter spread those across the top level and passed MessageData
        as a bare string, which TNZ answered with "Failed to process your API
        request. Check your syntax." and nothing more specific, because it
        could not parse far enough to say which field was wrong.

        Shape taken from TNZ's own Python client rather than inferred:
        tnzapi/api/v204/messaging/{requests/sms_api.py,dtos/*.py}.
        """
        data: dict = {
            "Message": body,
            "Destinations": [{"Recipient": to}],
        }
        # Optional fields are omitted rather than sent empty. An empty string
        # is a value, and a provider is entitled to reject it as one.
        sender = str(self.credentials.get("sender") or "").strip()
        if sender:
            data["FromNumber"] = sender
        reference = str((metadata or {}).get("reference") or "").strip()
        if reference:
            data["Reference"] = reference
        return data

    def send_message(
        self, *, to: str, subject: str, body: str, metadata: dict | None = None
    ) -> SendResult:
        missing = self.missing_credentials()
        if missing:
            return SendResult(
                success=False, error=f"Missing TNZ credentials: {', '.join(missing)}."
            )

        payload = {"MessageData": self._message_data(to=to, body=body, metadata=metadata)}
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
            return SendResult(success=False, error=describe_http_error(response))
        try:
            data = response.json()
        except ValueError:
            data = {}

        # TNZ carries its own verdict in the body: {"Result": "Success"|"Failed",
        # "MessageID": ..., "ErrorMessage": [...]}. A 2xx is the HTTP layer
        # saying it understood the request, not TNZ saying it accepted the
        # message, so trusting the status code alone would file a refusal as a
        # delivered send — invisible until somebody asks why a customer never
        # heard from us.
        if isinstance(data, dict):
            result = str(data.get("Result") or "").strip()
            if result and result.lower() != "success":
                reason = _join_error_message(data.get("ErrorMessage")) or result
                return SendResult(success=False, error=f"TNZ refused the message: {reason}")

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
                    "message": describe_http_error(response),
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
