"""Which channel to reach a customer on, and whether we may at all.

Deterministic by design. The brief is explicit that channel choice, consent and
suppression are application decisions and never the model's, and this is where
that line is drawn for Smart Reorder: the LLM writes the words, this decides
whether there is anywhere to send them.

The single most important property is that :func:`resolve_channel` can answer
"nowhere". A resolver that always returns a channel forces every caller to
remember the consent check separately, and the one that forgets sends an
unconsented message. Returning ``None`` with a reason makes that impossible to
overlook — there is nothing to send *to*.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.core.enums import Channel

#: Order to fall back through when the customer has no stated preference, or
#: their preference is unusable. WhatsApp first because it is the richest and
#: cheapest of the three, then SMS, then email — this is the brief's order and
#: it is deliberately not alphabetical or enum order, so it lives in one named
#: constant rather than being spelled out wherever a fallback happens.
FALLBACK_ORDER: tuple[Channel, ...] = (Channel.WHATSAPP, Channel.SMS, Channel.EMAIL)


@dataclass(frozen=True)
class ChannelDecision:
    """Where a message may go, or why it may not go anywhere."""

    channel: Channel | None
    #: Short machine-readable reason, for the skip ledger and the dry run.
    reason: str
    #: True when the customer's own stated preference was honoured.
    used_preference: bool = False
    #: True when we fell back off their preference onto something else.
    used_fallback: bool = False

    @property
    def sendable(self) -> bool:
        return self.channel is not None

    def as_dict(self) -> dict:
        return {
            "channel": self.channel.value if self.channel else None,
            "reason": self.reason,
            "used_preference": self.used_preference,
            "used_fallback": self.used_fallback,
        }


def consent_map(
    *,
    email_consent: bool,
    sms_consent: bool,
    whatsapp_consent: bool,
    marketing_consent: bool = True,
) -> dict[Channel, bool]:
    """Per-channel consent, with the marketing flag as a master switch.

    Marketing consent gates all three rather than sitting alongside them: a
    customer who withdrew marketing consent has withdrawn it everywhere, and
    treating the channel flags as independent would let a stale SMS flag keep
    messaging somebody who opted out of marketing entirely.
    """
    if not marketing_consent:
        return {Channel.EMAIL: False, Channel.SMS: False, Channel.WHATSAPP: False}
    return {
        Channel.EMAIL: bool(email_consent),
        Channel.SMS: bool(sms_consent),
        Channel.WHATSAPP: bool(whatsapp_consent),
    }


def resolve_channel(
    *,
    consents: dict[Channel, bool],
    preferred: Channel | None = None,
    suppressed_channels: set[str] | frozenset[str] | None = None,
    reachable: dict[Channel, bool] | None = None,
    requested: Channel | None = None,
    allow_fallback: bool = True,
) -> ChannelDecision:
    """Pick a channel, or refuse.

    ``requested`` is the campaign's fixed channel — when set, it is the only
    candidate, because an operator who chose SMS did not ask us to email
    instead. ``allow_fallback`` governs only the *automatic* case.

    ``reachable`` carries whether the customer has a usable address for each
    channel. Consent without a phone number is not a channel: the send would
    fail at the provider, and counting it as available hides the real reason
    somebody was never contacted behind a delivery error days later.
    """
    suppressed = {s.upper() for s in (suppressed_channels or set())}
    reachable = reachable or {}

    def usable(channel: Channel) -> bool:
        if not consents.get(channel):
            return False
        if channel.value in suppressed or "ALL" in suppressed:
            return False
        # Absent from `reachable` means "not asserted", which is treated as
        # reachable — callers that cannot tell should not be forced to lie.
        return reachable.get(channel, True)

    if "ALL" in suppressed:
        return ChannelDecision(None, "SUPPRESSED_ALL_CHANNELS")

    # A campaign pinned to one channel: that channel or nothing.
    if requested is not None:
        if usable(requested):
            return ChannelDecision(requested, "CAMPAIGN_CHANNEL")
        return ChannelDecision(None, f"NO_CONSENT_FOR_{requested.value}")

    if preferred is not None and usable(preferred):
        return ChannelDecision(preferred, "CUSTOMER_PREFERENCE", used_preference=True)

    if preferred is not None and not allow_fallback:
        # Their preference is unusable and the campaign forbids substituting.
        # Silently switching channels would be a decision the operator
        # explicitly declined to delegate.
        return ChannelDecision(None, "PREFERRED_CHANNEL_UNAVAILABLE")

    for channel in FALLBACK_ORDER:
        if usable(channel):
            return ChannelDecision(
                channel,
                "FALLBACK" if preferred is not None else "DEFAULT_PRIORITY",
                used_fallback=preferred is not None,
            )

    return ChannelDecision(None, "NO_CONSENTED_CHANNEL")
