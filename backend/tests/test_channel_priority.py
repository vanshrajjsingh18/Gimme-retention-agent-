"""Which channel a message may use, and when the answer is "none".

The property that matters most is the last one: a customer who has consented
to nothing must produce no channel at all. A resolver that always returns
something makes every caller responsible for remembering the consent check,
and the one that forgets sends an unconsented message.
"""
from __future__ import annotations

import pytest

from app.core.enums import Channel
from app.services.channel_priority import consent_map, resolve_channel

ALL_CONSENTED = {Channel.EMAIL: True, Channel.SMS: True, Channel.WHATSAPP: True}
NONE_CONSENTED = {Channel.EMAIL: False, Channel.SMS: False, Channel.WHATSAPP: False}


def test_no_consent_means_no_channel():
    decision = resolve_channel(consents=NONE_CONSENTED)
    assert decision.channel is None
    assert decision.sendable is False
    assert decision.reason == "NO_CONSENTED_CHANNEL"


def test_the_customers_stated_preference_wins():
    decision = resolve_channel(consents=ALL_CONSENTED, preferred=Channel.EMAIL)
    assert decision.channel == Channel.EMAIL
    assert decision.used_preference is True
    assert decision.used_fallback is False


def test_fallback_order_is_whatsapp_then_sms_then_email():
    """The brief's order, not enum order."""
    assert resolve_channel(consents=ALL_CONSENTED).channel == Channel.WHATSAPP
    assert resolve_channel(
        consents={**ALL_CONSENTED, Channel.WHATSAPP: False}
    ).channel == Channel.SMS
    assert resolve_channel(
        consents={**ALL_CONSENTED, Channel.WHATSAPP: False, Channel.SMS: False}
    ).channel == Channel.EMAIL


def test_an_unusable_preference_falls_back_when_permitted():
    decision = resolve_channel(
        consents={Channel.EMAIL: False, Channel.SMS: True, Channel.WHATSAPP: False},
        preferred=Channel.EMAIL,
    )
    assert decision.channel == Channel.SMS
    assert decision.used_fallback is True


def test_fallback_can_be_refused_by_the_campaign():
    """Switching channels is a decision the operator declined to delegate."""
    decision = resolve_channel(
        consents={Channel.EMAIL: False, Channel.SMS: True, Channel.WHATSAPP: False},
        preferred=Channel.EMAIL,
        allow_fallback=False,
    )
    assert decision.channel is None
    assert decision.reason == "PREFERRED_CHANNEL_UNAVAILABLE"


def test_a_campaign_pinned_to_one_channel_does_not_substitute():
    """An operator who chose SMS did not ask us to email instead."""
    decision = resolve_channel(
        consents={Channel.EMAIL: True, Channel.SMS: False, Channel.WHATSAPP: True},
        requested=Channel.SMS,
    )
    assert decision.channel is None
    assert "SMS" in decision.reason


def test_channel_suppression_removes_that_channel_only():
    decision = resolve_channel(consents=ALL_CONSENTED, suppressed_channels={"WHATSAPP"})
    assert decision.channel == Channel.SMS


def test_global_suppression_removes_everything():
    decision = resolve_channel(consents=ALL_CONSENTED, suppressed_channels={"ALL"})
    assert decision.channel is None
    assert decision.reason == "SUPPRESSED_ALL_CHANNELS"


def test_consent_without_an_address_is_not_a_channel():
    """A send with no phone number fails at the provider days later."""
    decision = resolve_channel(
        consents=ALL_CONSENTED,
        reachable={Channel.WHATSAPP: False, Channel.SMS: False, Channel.EMAIL: True},
    )
    assert decision.channel == Channel.EMAIL


def test_withdrawn_marketing_consent_closes_every_channel():
    """A stale per-channel flag must not outlive the master switch."""
    consents = consent_map(
        email_consent=True,
        sms_consent=True,
        whatsapp_consent=True,
        marketing_consent=False,
    )
    assert not any(consents.values())
    assert resolve_channel(consents=consents).channel is None


@pytest.mark.parametrize("channel", list(Channel)[:3])
def test_a_resolved_channel_always_has_consent(channel):
    """The invariant, stated directly: never return an unconsented channel."""
    only_this = {c: (c == channel) for c in (Channel.EMAIL, Channel.SMS, Channel.WHATSAPP)}
    decision = resolve_channel(consents=only_this)
    if decision.channel is not None:
        assert only_this[decision.channel] is True
