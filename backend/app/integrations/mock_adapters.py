"""Mock messaging adapters.

These behave like real providers: they accept sends, return provider message
IDs, deterministically produce a small rate of failures, and emit realistic
delivery/engagement event sequences. Everything they produce is tagged
``is_simulated=True`` so simulated activity is never mistaken for real
customer behaviour.
"""
from __future__ import annotations

import hashlib
import random
import uuid
from datetime import datetime, timedelta

from app.core.config import settings
from app.core.enums import Channel, EventType
from app.integrations.base import (
    ConnectionStatus,
    MessagingAdapter,
    NormalizedEvent,
    SendResult,
)

# Per-channel simulated behaviour rates. Loosely modelled on published NZ
# retail benchmarks so the demo analytics land in a believable range.
CHANNEL_BEHAVIOUR = {
    Channel.EMAIL: {
        "delivery_rate": 0.97,
        "open_rate": 0.42,
        "click_rate": 0.11,
        "reply_rate": 0.0,
        "unsubscribe_rate": 0.004,
    },
    Channel.SMS: {
        "delivery_rate": 0.98,
        "open_rate": 0.0,
        "click_rate": 0.09,
        "reply_rate": 0.02,
        "unsubscribe_rate": 0.006,
    },
    Channel.WHATSAPP: {
        "delivery_rate": 0.96,
        "open_rate": 0.68,
        "click_rate": 0.14,
        "reply_rate": 0.06,
        "unsubscribe_rate": 0.003,
    },
}

SENT_EVENT = {
    Channel.EMAIL: EventType.EMAIL_SENT,
    Channel.SMS: EventType.SMS_SENT,
    Channel.WHATSAPP: EventType.WHATSAPP_SENT,
}
DELIVERED_EVENT = {
    Channel.EMAIL: EventType.EMAIL_DELIVERED,
    Channel.SMS: EventType.SMS_DELIVERED,
    Channel.WHATSAPP: EventType.WHATSAPP_DELIVERED,
}
FAILED_EVENT = {
    Channel.EMAIL: EventType.EMAIL_BOUNCED,
    Channel.SMS: EventType.SMS_FAILED,
    Channel.WHATSAPP: EventType.MESSAGE_FAILED,
}
OPENED_EVENT = {
    Channel.EMAIL: EventType.EMAIL_OPENED,
    Channel.WHATSAPP: EventType.WHATSAPP_READ,
}


def seeded_random(*parts) -> random.Random:
    """A Random seeded from the inputs, so simulations are reproducible."""
    raw = "|".join(str(p) for p in parts)
    digest = hashlib.sha256(f"{settings.MOCK_SEED}:{raw}".encode()).hexdigest()[:16]
    return random.Random(int(digest, 16))


class BaseMockAdapter(MessagingAdapter):
    is_mock = True

    def validate_credentials(self) -> ConnectionStatus:
        return ConnectionStatus(
            status="OK",
            mode="mock",
            message=(
                f"{self.provider} is running in MOCK MODE. Messages are recorded locally "
                "and never leave this machine."
            ),
            details={"simulated": True},
        )

    def send_message(
        self, *, to: str, subject: str, body: str, metadata: dict | None = None
    ) -> SendResult:
        metadata = metadata or {}
        if not to:
            return SendResult(
                success=False,
                error="No recipient address supplied.",
                is_simulated=True,
            )

        # Deterministic simulated hard failures (bad address, unreachable
        # handset) so the failure path is exercised in every demo run.
        rng = seeded_random(self.provider, to, metadata.get("message_id", ""))
        if rng.random() > CHANNEL_BEHAVIOUR[self.channel]["delivery_rate"]:
            return SendResult(
                success=False,
                error=f"Simulated {self.channel.value} delivery failure for {_mask(to)}.",
                is_simulated=True,
                raw={"simulated": True, "reason": "delivery_failure"},
            )

        return SendResult(
            success=True,
            provider_message_id=f"mock-{self.provider}-{uuid.uuid4().hex[:16]}",
            is_simulated=True,
            # SMS and WhatsApp have no subject, so it is legitimately None.
            raw={"simulated": True, "to": _mask(to), "subject": (subject or "")[:80]},
        )

    def fetch_delivery_status(self, provider_message_id: str) -> dict:
        return {
            "provider_message_id": provider_message_id,
            "status": "delivered",
            "simulated": True,
            "checked_at": datetime.utcnow().isoformat(),
        }

    #: Provider-style status names, resolved against this adapter's channel.
    #: A mock integration must understand the same payloads a real provider
    #: sends, so a webhook can be exercised locally before going live.
    PROVIDER_STATUS_EVENTS: dict[str, dict[Channel, EventType]] = {
        "sent": SENT_EVENT,
        "delivered": DELIVERED_EVENT,
        "read": OPENED_EVENT,
        "opened": OPENED_EVENT,
        "failed": FAILED_EVENT,
        "undelivered": FAILED_EVENT,
        "bounced": FAILED_EVENT,
    }

    def process_webhook(self, payload: dict) -> list[NormalizedEvent]:
        """Accept both our event vocabulary and provider-style status names."""
        raw = str(payload.get("event") or payload.get("status") or "").strip()
        if not raw:
            return []

        event_type: EventType | None = None
        try:
            event_type = EventType(raw.upper())
        except ValueError:
            by_channel = self.PROVIDER_STATUS_EVENTS.get(raw.lower())
            if by_channel:
                event_type = by_channel.get(self.channel)
            elif raw.lower() in ("clicked", "click") and self.channel == Channel.EMAIL:
                event_type = EventType.EMAIL_CLICKED
            elif raw.lower() in ("replied", "reply") and self.channel == Channel.WHATSAPP:
                event_type = EventType.WHATSAPP_REPLIED
            elif raw.lower() in ("optout", "stop", "unsubscribed"):
                event_type = EventType.CUSTOMER_OPTED_OUT

        if event_type is None:
            return []

        return [
            NormalizedEvent(
                event_type=event_type,
                provider_message_id=payload.get("message_id"),
                occurred_at=_parse_ts(payload.get("timestamp")),
                channel=self.channel,
                recipient=payload.get("recipient"),
                payload={"simulated": True, **payload},
            )
        ]

    def simulate_engagement(
        self,
        *,
        provider_message_id: str,
        sent_at: datetime,
        engagement_bias: float = 1.0,
    ) -> list[NormalizedEvent]:
        """Produce a realistic post-send event sequence for one message.

        ``engagement_bias`` scales the open/click/reply probabilities so
        engaged customers behave differently from disengaged ones.
        """
        rates = CHANNEL_BEHAVIOUR[self.channel]
        rng = seeded_random("engagement", self.provider, provider_message_id)
        events: list[NormalizedEvent] = []

        delivered_at = sent_at + timedelta(seconds=rng.randint(5, 240))
        events.append(
            NormalizedEvent(
                event_type=DELIVERED_EVENT[self.channel],
                provider_message_id=provider_message_id,
                occurred_at=delivered_at,
                channel=self.channel,
                payload={"simulated": True},
            )
        )

        opened_at = None
        open_event = OPENED_EVENT.get(self.channel)
        if open_event and rng.random() < min(rates["open_rate"] * engagement_bias, 0.95):
            opened_at = delivered_at + timedelta(minutes=rng.randint(2, 60 * 20))
            events.append(
                NormalizedEvent(
                    event_type=open_event,
                    provider_message_id=provider_message_id,
                    occurred_at=opened_at,
                    channel=self.channel,
                    payload={"simulated": True},
                )
            )

        # A click implies the message was seen, so on channels that report opens
        # only allow clicks that follow one. Email is the only channel with a
        # distinct click event; SMS and WhatsApp link taps are not reported.
        can_click = opened_at is not None or open_event is None
        if (
            self.channel == Channel.EMAIL
            and can_click
            and rng.random() < min(rates["click_rate"] * engagement_bias, 0.6)
        ):
            base = opened_at or delivered_at
            events.append(
                NormalizedEvent(
                    event_type=EventType.EMAIL_CLICKED,
                    provider_message_id=provider_message_id,
                    occurred_at=base + timedelta(minutes=rng.randint(1, 90)),
                    channel=self.channel,
                    payload={"simulated": True},
                )
            )

        if (
            self.channel == Channel.WHATSAPP
            and rng.random() < rates["reply_rate"] * engagement_bias
        ):
            events.append(
                NormalizedEvent(
                    event_type=EventType.WHATSAPP_REPLIED,
                    provider_message_id=provider_message_id,
                    occurred_at=delivered_at + timedelta(minutes=rng.randint(5, 600)),
                    channel=self.channel,
                    payload={"simulated": True},
                )
            )

        if rng.random() < rates["unsubscribe_rate"]:
            events.append(
                NormalizedEvent(
                    event_type=EventType.CUSTOMER_OPTED_OUT,
                    provider_message_id=provider_message_id,
                    occurred_at=delivered_at + timedelta(minutes=rng.randint(1, 240)),
                    channel=self.channel,
                    payload={"simulated": True, "reason": "unsubscribed via message footer"},
                )
            )

        return events


class MockOutlookAdapter(BaseMockAdapter):
    provider = "outlook_mock"
    channel = Channel.EMAIL


class MockTnzAdapter(BaseMockAdapter):
    provider = "tnz_mock"
    channel = Channel.SMS


class MockWhatsAppAdapter(BaseMockAdapter):
    provider = "whatsapp_mock"
    channel = Channel.WHATSAPP


def _mask(value: str) -> str:
    """Mask a contact detail for logging. Never log a full address."""
    if not value:
        return ""
    if "@" in value:
        local, _, domain = value.partition("@")
        return f"{local[:2]}***@{domain}"
    return f"***{value[-4:]}" if len(value) > 4 else "***"


def _parse_ts(value) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", ""))
        except ValueError:
            return None
    return None
