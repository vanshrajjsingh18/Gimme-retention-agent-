"""Common messaging-provider interface.

Every provider — live or mock — implements the same six operations so the
campaign engine never branches on provider identity.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime

from app.core.enums import Channel, EventType


@dataclass
class SendResult:
    success: bool
    provider_message_id: str | None = None
    error: str | None = None
    is_simulated: bool = False
    raw: dict = field(default_factory=dict)


@dataclass
class NormalizedEvent:
    """A provider webhook payload mapped onto our event vocabulary."""

    event_type: EventType
    provider_message_id: str | None = None
    occurred_at: datetime | None = None
    channel: Channel = Channel.EMAIL
    recipient: str | None = None
    payload: dict = field(default_factory=dict)


@dataclass
class ConnectionStatus:
    status: str  # OK | NOT_CONFIGURED | ERROR
    message: str
    mode: str = "mock"
    details: dict = field(default_factory=dict)


class MessagingAdapter(ABC):
    provider: str = "base"
    channel: Channel = Channel.EMAIL
    is_mock: bool = False

    #: Credential keys this adapter requires in live mode. Used by the
    #: integrations UI to render the right form and to mask stored values.
    required_credentials: tuple[str, ...] = ()

    def __init__(self, credentials: dict | None = None, config: dict | None = None) -> None:
        self.credentials = credentials or {}
        self.config = config or {}

    @abstractmethod
    def validate_credentials(self) -> ConnectionStatus:
        """Check that stored credentials are present and usable."""

    @abstractmethod
    def send_message(
        self, *, to: str, subject: str, body: str, metadata: dict | None = None
    ) -> SendResult:
        """Send one message."""

    def send_test_message(self, *, to: str, subject: str, body: str) -> SendResult:
        """Send a test message. Defaults to a normal send tagged as a test."""
        return self.send_message(
            to=to, subject=subject, body=body, metadata={"is_test": True}
        )

    @abstractmethod
    def fetch_delivery_status(self, provider_message_id: str) -> dict:
        """Poll the provider for the current status of one message."""

    @abstractmethod
    def process_webhook(self, payload: dict) -> list[NormalizedEvent]:
        """Turn a raw provider webhook body into normalized events."""

    def normalize_event(self, raw_event: dict) -> NormalizedEvent | None:
        """Map one raw provider event onto our vocabulary."""
        events = self.process_webhook(raw_event)
        return events[0] if events else None

    def missing_credentials(self) -> list[str]:
        return [k for k in self.required_credentials if not self.credentials.get(k)]
