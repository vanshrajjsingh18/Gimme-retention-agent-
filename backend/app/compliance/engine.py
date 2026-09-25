"""Deterministic compliance enforcement for alcohol marketing.

Two layers:

1. **Per-recipient eligibility** (``check_recipient``) — age, consent,
   suppression, frequency caps, quiet hours, contactability. Decides who is
   allowed to receive a message.
2. **Per-campaign content checks** (``check_campaign``) — prohibited claims,
   invented promotions, missing responsible-drinking statements, targeting of
   inferred vulnerability. A CRITICAL finding blocks sending outright.

Nothing here consults the LLM. The LLM's output is an *input* to these checks.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, time

from app.core.phone import is_sendable, normalize_nz_phone
from app.core.enums import Channel, ComplianceSeverity, LifecycleStage, RecipientStatus
from app.services.merge_tags import unknown_tags as unknown_merge_tags

# --------------------------------------------------------------------------
# Prohibited claim patterns
# --------------------------------------------------------------------------
# Each entry: (rule code, human label, regex). Patterns are deliberately
# narrow so that ordinary marketing copy does not trip them.
PROHIBITED_CLAIM_PATTERNS: list[tuple[str, str, str]] = [
    (
        "HEALTH_CLAIM",
        "Implies alcohol improves health or wellbeing",
        r"\b(?:good|great|better|healthy|healthier|beneficial)\s+for\s+(?:your\s+)?"
        r"(?:health|heart|body|immune|wellbeing|well-being)\b"
        r"|\b(?:boosts?|improves?|cures?|heals?|detox(?:es|ifies)?)\s+(?:your\s+)?"
        r"(?:health|immunity|immune\s+system|metabolism)\b"
        r"|\bhealth\s+benefits?\b",
    ),
    (
        "EMOTIONAL_WELLBEING_CLAIM",
        "Implies alcohol improves emotional wellbeing or relieves distress",
        r"\b(?:cure|cures|fix|fixes|beat|beats|kill|kills|drown|drowns?|wash(?:es)?\s+away)"
        r"\s+(?:your\s+)?(?:sadness|depression|anxiety|stress|sorrows?|worries|loneliness|grief)\b"
        r"|\b(?:feel\s+better|cheer\s+(?:you|yourself)\s+up|lift\s+your\s+mood)\s+"
        r"(?:with|after)\s+(?:a\s+)?(?:drink|beer|wine|glass|bottle)\b"
        r"|\bdrink\s+(?:away|your\s+(?:troubles|problems|sorrows|stress))\b",
    ),
    (
        "SOCIAL_SUCCESS_CLAIM",
        "Implies alcohol improves social standing or popularity",
        r"\b(?:be|become|makes?\s+you)\s+(?:the\s+)?(?:more\s+)?"
        r"(?:popular|cool|cooler|admired|respected|life\s+of\s+the\s+party)\b"
        r"|\b(?:impress|win\s+over)\s+(?:your\s+)?(?:friends|mates|guests|everyone)\s+with\s+"
        r"(?:a\s+)?(?:drink|beer|wine|bottle)\b",
    ),
    (
        "SEXUAL_SUCCESS_CLAIM",
        "Implies alcohol improves sexual or romantic success",
        r"\b(?:irresistible|seductive|sexy|get\s+lucky|pull\s+(?:anyone|someone)|"
        r"attract\s+(?:women|men|anyone))\b",
    ),
    (
        "PROFESSIONAL_SUCCESS_CLAIM",
        "Implies alcohol improves professional success",
        r"\b(?:close\s+(?:the\s+)?deal|land\s+(?:the\s+)?(?:job|client|promotion)|"
        r"get\s+(?:that\s+)?promotion)\s+(?:with|over)\s+(?:a\s+)?(?:drink|beer|wine|bottle)\b"
        r"|\b(?:drink|bottle)\s+your\s+way\s+to\s+(?:success|the\s+top)\b",
    ),
    (
        "EXCESSIVE_CONSUMPTION",
        "Encourages excessive or rapid consumption",
        r"\b(?:get\s+(?:wasted|hammered|smashed|plastered|trashed)|"
        r"drink\s+(?:till|until)\s+you\s+drop|binge|chug|skull\s+(?:it|this)|"
        r"bottoms\s+up|down\s+it\s+in\s+one|drink\s+as\s+much\s+as\s+you\s+can)\b"
        r"|\bno\s+limits?\b",
    ),
    (
        "UNDERAGE_APPEAL",
        "Language or imagery that appeals to minors",
        r"\b(?:kids?|children|teens?|teenagers?|school\s+(?:kids|leavers)|under-?18s?)\b",
    ),
    (
        "DRINK_DRIVING",
        "References driving in connection with drinking",
        r"\b(?:one\s+for\s+the\s+road|drink\s+and\s+drive|drive\s+(?:home\s+)?after\s+"
        r"(?:a\s+few|drinks?))\b",
    ),
]

# Placeholder patterns that must never survive into a sent message.
UNRESOLVED_PLACEHOLDER = re.compile(r"\{\{?\s*[a-z_][a-z0-9_. ]*\s*\}?\}|\[[A-Z_]{3,}\]")

# Discount / promotion mentions that must match a verified promotion.
DISCOUNT_PATTERN = re.compile(
    r"(\d{1,3})\s*%\s*(?:off|discount)|"
    r"\$\s*(\d+(?:\.\d{1,2})?)\s*(?:off|discount)|"
    r"\b(free\s+delivery|free\s+shipping|buy\s+one\s+get\s+one|bogo|half\s+price)\b",
    re.IGNORECASE,
)

# A specific amount of money presented as a price. This system holds no
# pricing data at all — not a catalogue, not a feed, nothing — so every figure
# of this shape is one the copy invented. Offers are handled by
# DISCOUNT_PATTERN above; this is the plain "$24.99 a bottle" case, which that
# pattern does not match because there is no "off" or "discount" after it.
#
# `(?!\d)(?!\.\d)` is load-bearing, not tidiness. Without it the engine
# backtracks out of the "off" guard: "$10 off" fails as a whole, so the number
# is retried a digit shorter, "$1" passes the guard, and the report names a
# price the copy never stated. Requiring the figure to be whole leaves the
# match to fail as intended and the phrase to be judged as the discount it is.
#
# It rejects a digit or a decimal point followed by a digit — not any full
# stop, because "Delivery from $1." ends a sentence and is a real price claim.
_WHOLE_NUMBER = r"(?!\d)(?!\.\d)"
PRICE_CLAIM_PATTERN = re.compile(
    rf"(?<![\w])(?:NZ)?\$\s*\d+(?:\.\d{{1,2}})?{_WHOLE_NUMBER}(?!\s*(?:off|discount))"
    rf"|\b\d+(?:\.\d{{1,2}})?{_WHOLE_NUMBER}\s*(?:dollars|bucks)\b",
    re.IGNORECASE,
)

# Coupon-code shaped tokens: 4+ chars, uppercase alphanumeric, containing a
# digit. Avoids matching ordinary capitalised words.
#
# The digit lookahead is bounded to the token — `(?=.*\d)` scans the whole rest
# of the message, so "GIMME" matched whenever a digit appeared anywhere after
# it. The company's own name was reported as an unverified coupon code in
# every message that also mentioned a number, which is most of them.
COUPON_PATTERN = re.compile(r"\b(?=[A-Z0-9]{4,20}\b)(?=[A-Z0-9]*\d)[A-Z][A-Z0-9]{3,19}\b")

DELIVERY_CLAIM_PATTERN = re.compile(
    r"\b(?:deliver(?:y|ed|s|ing)?|arrives?|arrive|get\s+it|be\s+there|at\s+your\s+door|"
    r"on\s+your\s+doorstep|with\s+you)\s+(?:to\s+you\s+)?(?:in|within|under|inside)\s+"
    r"(?:just\s+|as\s+little\s+as\s+)?(\d{1,3})\s*(minutes?|mins?|hours?|hrs?)\b",
    re.IGNORECASE,
)

STOCK_CLAIM_PATTERN = re.compile(
    r"\b(?:in\s+stock|back\s+in\s+stock|only\s+\d+\s+left|last\s+\d+|"
    r"limited\s+stock|while\s+stocks\s+last|selling\s+out\s+fast)\b",
    re.IGNORECASE,
)

# How a message can tell somebody to stop. Deliberately generous about the
# wording — STOP is the keyword the opt-out handler acts on, and the point of
# the rule is that the recipient was told it, not that they were told it in one
# particular sentence.
OPT_OUT_PATTERN = re.compile(
    r"\b(?:reply|text|txt|send)\s+(?:with\s+)?[\"']?stop\b"
    r"|\bstop\s+to\s+(?:opt\s*out|unsubscribe|cancel|end)\b"
    r"|\bunsubscribe\b",
    re.IGNORECASE,
)

# Quiet hours are the complement of the allowed business window: sends are
# blocked from 19:00 until 09:00 the following morning, in BUSINESS LOCAL
# TIME. Evaluated via app.core.timezones, never against naive UTC.
DEFAULT_QUIET_HOURS_START = time(19, 0)
DEFAULT_QUIET_HOURS_END = time(9, 0)


#: Findings that say "this system cannot verify this", not "this is wrong".
#:
#: The distinction matters more than it looks. A health claim is something the
#: engine can read and judge; a coupon code is not. This system holds no
#: pricing data, no catalogue and no promotions beyond the handful configured
#: in Brand settings, so a figure or a code in hand-written copy is unverified
#: *by us* — which is not the same as untrue. The person who typed "FIRST10"
#: knows whether it is a real code, and blocking them substitutes the engine's
#: ignorance for their knowledge.
#:
#: So these stop the LLM inventing offers, which is what they were built for,
#: and a human reviewer can vouch for them instead of being vetoed by them.
#: Everything not on this list still blocks: a prohibited claim, a message
#: targeting inferred vulnerability, or an unfillable merge tag is not
#: something a reviewer can make true by agreeing with it.
VOUCHABLE_RULES: frozenset[str] = frozenset(
    {
        "UNVERIFIED_COUPON_CODE",
        "UNVERIFIED_PROMOTION",
        "UNVERIFIED_PRICE_CLAIM",
        "UNVERIFIED_DELIVERY_CLAIM",
        "UNVERIFIED_PRODUCT_CLAIM",
        "MISSING_SMS_OPT_OUT",
    }
)


@dataclass
class ComplianceFinding:
    code: str
    message: str
    severity: ComplianceSeverity
    blocks_send: bool
    excerpt: str = ""
    #: True once a named human has taken responsibility for this claim.
    vouched_by: str = ""

    @property
    def vouchable(self) -> bool:
        """Whether a human reviewer can take responsibility for this."""
        return self.code in VOUCHABLE_RULES

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "severity": self.severity.value,
            "blocks_send": self.blocks_send,
            "excerpt": self.excerpt,
            "vouchable": self.vouchable,
            "vouched_by": self.vouched_by,
        }


@dataclass
class ComplianceReport:
    findings: list[ComplianceFinding] = field(default_factory=list)
    checked_at: datetime | None = None

    @property
    def passed(self) -> bool:
        """True when nothing blocks sending."""
        return not any(f.blocks_send for f in self.findings)

    @property
    def blocking_findings(self) -> list[ComplianceFinding]:
        return [f for f in self.findings if f.blocks_send]

    @property
    def needs_vouching(self) -> list[ComplianceFinding]:
        """Blocking findings a reviewer could take responsibility for.

        The difference between "you cannot send this" and "confirm this is
        right and you can" — which is the difference between a compliance
        engine that helps and one people work around.
        """
        return [f for f in self.findings if f.blocks_send and f.vouchable]

    @property
    def hard_blocking(self) -> list[ComplianceFinding]:
        """Findings no reviewer can sign away."""
        return [f for f in self.findings if f.blocks_send and not f.vouchable]

    def as_dict(self) -> dict:
        return {
            "passed": self.passed,
            "blocking_count": len(self.blocking_findings),
            "vouchable_codes": sorted({f.code for f in self.needs_vouching}),
            "hard_blocking_count": len(self.hard_blocking),
            "findings": [f.as_dict() for f in self.findings],
            "checked_at": (self.checked_at or datetime.utcnow()).isoformat(),
        }


@dataclass
class ComplianceConfig:
    """Tunable enforcement settings, sourced from brand + compliance rules."""

    minimum_age: int = 18
    require_age_verification: bool = True
    frequency_cap_30d: int = 4
    frequency_cap_7d: int = 2
    quiet_hours_start: time = DEFAULT_QUIET_HOURS_START
    quiet_hours_end: time = DEFAULT_QUIET_HOURS_END
    enforce_quiet_hours: bool = True
    #: Resolve quiet hours in business local time rather than raw UTC.
    use_business_timezone: bool = True
    require_responsible_drinking_statement: bool = True
    require_age_statement_on_email: bool = True
    #: Every commercial SMS must tell the recipient how to stop receiving them.
    #: The Unsolicited Electronic Messages Act 2007 requires a functional
    #: unsubscribe facility, and GIMME's own STOP handling only works if people
    #: are told the word.
    require_sms_opt_out: bool = True
    allowed_coupon_codes: list[str] = field(default_factory=list)
    allowed_promotions: list[str] = field(default_factory=list)
    verified_products: list[str] = field(default_factory=list)
    delivery_promise: str = ""
    extra_prohibited_claims: list[str] = field(default_factory=list)
    responsible_drinking_statement: str = ""
    age_restriction_statement: str = ""
    disabled_rules: set[str] = field(default_factory=set)


# --------------------------------------------------------------------------
# Recipient eligibility
# --------------------------------------------------------------------------
@dataclass
class RecipientView:
    """Flat projection of everything eligibility depends on."""

    customer_id: int
    age: int | None = None
    age_verified: bool = False
    is_suppressed: bool = False
    suppressed_channels: set[str] = field(default_factory=set)
    marketing_consent: bool = False
    email_consent: bool = False
    sms_consent: bool = False
    whatsapp_consent: bool = False
    email: str | None = None
    phone: str | None = None
    messages_last_7d: int = 0
    messages_last_30d: int = 0
    lifecycle_stage: str = LifecycleStage.NEW.value


CHANNEL_CONSENT_FIELD = {
    Channel.EMAIL: "email_consent",
    Channel.SMS: "sms_consent",
    Channel.WHATSAPP: "whatsapp_consent",
    Channel.PUSH: "marketing_consent",
}

CHANNEL_CONTACT_FIELD = {
    Channel.EMAIL: "email",
    Channel.SMS: "phone",
    Channel.WHATSAPP: "phone",
}


def check_recipient(
    recipient: RecipientView,
    channel: Channel,
    config: ComplianceConfig,
    *,
    send_time: datetime | None = None,
) -> tuple[RecipientStatus, str | None]:
    """Return ``(status, exclusion_reason)`` for one prospective recipient.

    Checks run in severity order so the reported reason is the most important
    one, and the outcome is stable regardless of how many rules a customer
    trips.
    """
    # 1. Age — a hard legal gate for alcohol marketing.
    if config.require_age_verification and not recipient.age_verified:
        return (
            RecipientStatus.EXCLUDED_AGE,
            "Age has not been verified; alcohol marketing requires verified age.",
        )
    if recipient.age is not None and recipient.age < config.minimum_age:
        return (
            RecipientStatus.EXCLUDED_AGE,
            f"Customer is under the minimum age of {config.minimum_age}.",
        )

    # 2. Suppression — an explicit operator or customer decision.
    if recipient.is_suppressed:
        return (RecipientStatus.EXCLUDED_SUPPRESSED, "Customer is on the suppression list.")
    if channel.value in recipient.suppressed_channels or "ALL" in recipient.suppressed_channels:
        return (
            RecipientStatus.EXCLUDED_SUPPRESSED,
            f"Customer is suppressed for the {channel.value} channel.",
        )

    # 3. Consent — general marketing consent, then channel-specific consent.
    if not recipient.marketing_consent:
        return (RecipientStatus.EXCLUDED_NO_CONSENT, "No marketing consent on record.")
    if not getattr(recipient, CHANNEL_CONSENT_FIELD[channel], False):
        return (
            RecipientStatus.EXCLUDED_NO_CONSENT,
            f"No {channel.value} channel consent on record.",
        )

    # 4. Contactability.
    contact_field = CHANNEL_CONTACT_FIELD.get(channel)
    if contact_field:
        contact = getattr(recipient, contact_field, None)
        if not contact:
            return (
                RecipientStatus.EXCLUDED_MISSING_CONTACT,
                f"No {'email address' if contact_field == 'email' else 'phone number'} on record.",
            )
        # Holding a phone number is not the same as being reachable on it. A
        # landline or a malformed import would otherwise be handed to the SMS
        # provider, which fails silently — or delivers to somebody else.
        if contact_field == "phone" and not is_sendable(contact):
            return (
                RecipientStatus.EXCLUDED_MISSING_CONTACT,
                "Phone number on record is not a mobile number an SMS can reach.",
            )

    # 5. Frequency caps.
    if recipient.messages_last_30d >= config.frequency_cap_30d:
        return (
            RecipientStatus.EXCLUDED_FREQUENCY_CAP,
            f"Received {recipient.messages_last_30d} messages in 30 days "
            f"(cap {config.frequency_cap_30d}).",
        )
    if recipient.messages_last_7d >= config.frequency_cap_7d:
        return (
            RecipientStatus.EXCLUDED_FREQUENCY_CAP,
            f"Received {recipient.messages_last_7d} messages in 7 days "
            f"(cap {config.frequency_cap_7d}).",
        )

    # 6. Quiet hours (SMS and WhatsApp only; email is not intrusive at night).
    if (
        config.enforce_quiet_hours
        and send_time is not None
        and channel in (Channel.SMS, Channel.WHATSAPP)
        and in_quiet_hours(send_time, config)
    ):
        from app.core.timezones import to_local

        local = to_local(send_time) if config.use_business_timezone else send_time
        return (
            RecipientStatus.EXCLUDED_QUIET_HOURS,
            f"Send time {local:%H:%M} local falls inside quiet hours "
            f"({config.quiet_hours_start:%H:%M}-{config.quiet_hours_end:%H:%M}).",
        )

    return (RecipientStatus.ELIGIBLE, None)


def in_quiet_hours(moment: datetime, config: ComplianceConfig) -> bool:
    """True when the customer's LOCAL time falls inside quiet hours.

    ``moment`` is the naive-UTC instant of the send. It is converted to
    business local time first: checking a New Zealand quiet-hours window
    against UTC would be wrong by twelve or thirteen hours.
    """
    from app.core.timezones import to_local

    t = to_local(moment).time() if config.use_business_timezone else moment.time()
    start, end = config.quiet_hours_start, config.quiet_hours_end
    if start <= end:
        return start <= t < end
    return t >= start or t < end


# --------------------------------------------------------------------------
# Campaign content checks
# --------------------------------------------------------------------------
def check_content(
    text: str,
    config: ComplianceConfig,
    *,
    channel: Channel = Channel.EMAIL,
    is_full_message: bool = True,
) -> list[ComplianceFinding]:
    """Run every content rule over a message body (plus subject if combined)."""
    findings: list[ComplianceFinding] = []
    haystack = text or ""
    lowered = haystack.lower()

    def add(code: str, message: str, severity: ComplianceSeverity, blocks: bool, excerpt: str = ""):
        if code in config.disabled_rules:
            return
        findings.append(ComplianceFinding(code, message, severity, blocks, excerpt))

    # 1. Prohibited claims.
    for code, label, pattern in PROHIBITED_CLAIM_PATTERNS:
        match = re.search(pattern, lowered, re.IGNORECASE)
        if match:
            add(
                code,
                f"{label}. Alcohol marketing may not make this claim.",
                ComplianceSeverity.CRITICAL,
                True,
                match.group(0).strip(),
            )

    # 2. Brand-configured prohibited claims.
    for phrase in config.extra_prohibited_claims:
        phrase_l = phrase.strip().lower()
        if phrase_l and phrase_l in lowered:
            add(
                "BRAND_PROHIBITED_CLAIM",
                f"Contains the brand-prohibited phrase '{phrase}'.",
                ComplianceSeverity.CRITICAL,
                True,
                phrase,
            )

    # 3. Unverified coupon codes.
    allowed_codes = {c.strip().upper() for c in config.allowed_coupon_codes if c.strip()}
    for token in set(COUPON_PATTERN.findall(haystack)):
        if token.upper() not in allowed_codes:
            add(
                "UNVERIFIED_COUPON_CODE",
                f"Mentions coupon code '{token}', which is not in the verified list.",
                ComplianceSeverity.CRITICAL,
                True,
                token,
            )

    # 4. Discounts / promotions that are not on the approved list.
    if DISCOUNT_PATTERN.search(haystack):
        approved = [p.strip().lower() for p in config.allowed_promotions if p.strip()]
        for match in DISCOUNT_PATTERN.finditer(haystack):
            phrase = match.group(0).strip()
            if not any(_promotion_matches(phrase, p) for p in approved):
                add(
                    "UNVERIFIED_PROMOTION",
                    f"Mentions the promotion '{phrase}', which is not on the approved "
                    "promotions list.",
                    ComplianceSeverity.CRITICAL,
                    True,
                    phrase,
                )

    # 5. Delivery-time claims must be no faster than the configured promise.
    #    Compare the substantive claim (a duration) rather than the wording, so
    #    "we deliver in 60 minutes" is accepted against a promise of
    #    "delivered in 60 minutes" but "in 20 minutes" is not.
    promised_minutes = _promised_delivery_minutes(config.delivery_promise)
    for match in DELIVERY_CLAIM_PATTERN.finditer(haystack):
        phrase = match.group(0).strip()
        claimed = _duration_to_minutes(match.group(1), match.group(2))
        if promised_minutes is None:
            reason = "no delivery promise is configured"
        elif claimed is not None and claimed < promised_minutes:
            reason = (
                f"the configured delivery promise is {int(promised_minutes)} minutes, "
                "so this claims faster delivery than GIMME commits to"
            )
        else:
            continue
        add(
            "UNVERIFIED_DELIVERY_CLAIM",
            f"Makes the delivery claim '{phrase}', which is not backed by the "
            f"configured delivery promise: {reason}.",
            ComplianceSeverity.CRITICAL,
            True,
            phrase,
        )

    # 6. Stock / availability claims cannot be verified from the data we hold.
    match = STOCK_CLAIM_PATTERN.search(haystack)
    if match:
        add(
            "UNVERIFIED_STOCK_CLAIM",
            f"Makes the stock claim '{match.group(0).strip()}'. Live stock levels are not "
            "available to this system, so the claim cannot be verified.",
            ComplianceSeverity.CRITICAL,
            True,
            match.group(0).strip(),
        )

    # 6b. Specific prices. Nothing in this system knows what anything costs,
    # so a figure in the copy is an invention — and a wrong price is a promise
    # to a customer that the checkout will break.
    # An approved promotion is allowed to contain a figure — the brand's own
    # "Free delivery on orders over $80" is verified copy, and blocking it
    # would make the approved list unusable. Only amounts outside any approved
    # promotion present in the message are inventions.
    approved_spans = [
        p.strip().lower() for p in config.allowed_promotions if p.strip()
    ]
    lowered_haystack = haystack.lower()
    for match in PRICE_CLAIM_PATTERN.finditer(haystack):
        phrase = match.group(0).strip()
        inside_approved = any(
            promo in lowered_haystack
            and phrase.lower() in promo
            and lowered_haystack.index(promo)
            <= match.start()
            < lowered_haystack.index(promo) + len(promo)
            for promo in approved_spans
        )
        if inside_approved:
            continue
        add(
            "UNVERIFIED_PRICE_CLAIM",
            f"States the price '{phrase}'. This system holds no pricing data, so the "
            "figure cannot be verified against anything.",
            ComplianceSeverity.CRITICAL,
            True,
            phrase,
        )
        break

    # 7a. Merge tags naming something that does not exist.
    #
    #     A tag resolves against a whitelist, so "#discont_code#" is not a
    #     value that comes back empty — it is text, and it would be delivered
    #     verbatim to everyone in the audience. Blocking is the point: the
    #     typo has to be caught while somebody is still looking at the copy,
    #     not read back off a sent message. Each offending tag is named,
    #     because "a merge tag is wrong" in a 400-character body is not
    #     something an operator can act on.
    for tag in unknown_merge_tags(haystack):
        add(
            "UNKNOWN_MERGE_TAG",
            f"Uses the merge tag '#{tag}#', which is not a field this system can "
            "fill. It would be sent to the customer exactly as written.",
            ComplianceSeverity.CRITICAL,
            True,
            f"#{tag}#",
        )

    # 7. Unresolved template placeholders.
    match = UNRESOLVED_PLACEHOLDER.search(haystack)
    if match:
        add(
            "UNRESOLVED_PLACEHOLDER",
            f"Contains the unresolved placeholder '{match.group(0)}'.",
            ComplianceSeverity.CRITICAL,
            True,
            match.group(0),
        )

    if not is_full_message:
        return findings

    # 8. An SMS has to say how to stop receiving them.
    #
    #    Until now this was a convention inside the default templates rather
    #    than a rule, which held only for as long as every message came from
    #    one of those templates. It is a legal requirement, and the STOP
    #    handling elsewhere in this system is useless to somebody who was never
    #    told the word.
    if channel == Channel.SMS and config.require_sms_opt_out:
        if not OPT_OUT_PATTERN.search(haystack):
            add(
                "MISSING_SMS_OPT_OUT",
                "SMS does not tell the recipient how to opt out. A commercial "
                'message must carry an unsubscribe facility, e.g. "Reply STOP '
                'to opt out."',
                ComplianceSeverity.CRITICAL,
                True,
            )

    # 9. Mandatory statements (email only — SMS has no room and is exempt).
    if channel == Channel.EMAIL:
        if config.require_responsible_drinking_statement and config.responsible_drinking_statement:
            if not _contains_statement(lowered, config.responsible_drinking_statement):
                add(
                    "MISSING_RESPONSIBLE_DRINKING",
                    "Email is missing the configured responsible drinking statement.",
                    ComplianceSeverity.CRITICAL,
                    True,
                )
        if config.require_age_statement_on_email and config.age_restriction_statement:
            if not _contains_statement(lowered, config.age_restriction_statement):
                add(
                    "MISSING_AGE_STATEMENT",
                    "Email is missing the configured age restriction statement.",
                    ComplianceSeverity.WARNING,
                    False,
                )

    return findings


def _duration_to_minutes(amount: str | None, unit: str | None) -> float | None:
    if not amount or not unit:
        return None
    try:
        value = float(amount)
    except ValueError:
        return None
    return value * 60 if unit.lower().startswith(("hour", "hr")) else value


def _promised_delivery_minutes(promise: str) -> float | None:
    """Extract the delivery window (in minutes) from the brand promise text."""
    if not promise:
        return None
    match = DELIVERY_CLAIM_PATTERN.search(promise)
    if match:
        return _duration_to_minutes(match.group(1), match.group(2))
    # Fall back to any "<number> <unit>" mention in the promise.
    generic = re.search(r"(\d{1,3})\s*(minutes?|mins?|hours?|hrs?)", promise, re.IGNORECASE)
    return _duration_to_minutes(generic.group(1), generic.group(2)) if generic else None


def _contains_statement(haystack_lower: str, statement: str) -> bool:
    """Loose containment check tolerant of whitespace and light rewording."""
    normalized = re.sub(r"\s+", " ", statement.strip().lower())
    if not normalized:
        return True
    if normalized in re.sub(r"\s+", " ", haystack_lower):
        return True
    # Accept the statement if a strong majority of its distinctive words appear.
    words = [w for w in re.findall(r"[a-z]{4,}", normalized)]
    if not words:
        return True
    present = sum(1 for w in words if w in haystack_lower)
    return present / len(words) >= 0.8


def _promotion_matches(phrase: str, approved: str) -> bool:
    """A mentioned promotion is allowed if the approved entry covers it."""
    phrase_l = re.sub(r"\s+", " ", phrase.lower().strip())
    approved_l = re.sub(r"\s+", " ", approved.lower().strip())
    if not approved_l:
        return False
    return phrase_l in approved_l or approved_l in phrase_l


def check_targeting(
    *,
    segment_rule: dict | None,
    objective: str,
    config: ComplianceConfig,
) -> list[ComplianceFinding]:
    """Reject audience definitions that target inferred vulnerability.

    Targeting people *because* they drink heavily, are discount-dependent in a
    way that implies financial vulnerability, or are inferred to be in distress
    is prohibited. Targeting on lifecycle and recency is not.
    """
    findings: list[ComplianceFinding] = []
    if not segment_rule:
        return findings

    flagged_fields = {
        "discount_dependency": (
            "Targeting customers by discount dependency can select for financial "
            "vulnerability."
        ),
    }

    def walk(node: dict) -> None:
        if not isinstance(node, dict):
            return
        if "conditions" in node:
            for child in node.get("conditions") or []:
                walk(child)
            return
        field_name = node.get("field")
        operator = node.get("operator")
        value = node.get("value")
        if field_name == "discount_dependency" and operator in ("gt", "gte", "between"):
            threshold = value[0] if isinstance(value, (list, tuple)) and value else value
            try:
                numeric = float(threshold)
            except (TypeError, ValueError):
                numeric = 0.0
            if numeric >= 0.7:
                findings.append(
                    ComplianceFinding(
                        "VULNERABILITY_TARGETING",
                        flagged_fields["discount_dependency"],
                        ComplianceSeverity.WARNING,
                        False,
                        f"discount_dependency {operator} {threshold}",
                    )
                )
        # Very high purchase frequency combined with a push objective reads as
        # encouraging heavier consumption.
        if (
            field_name == "purchase_frequency_per_month"
            and operator in ("gt", "gte")
            and objective in ("REORDER", "RETENTION")
        ):
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                numeric = 0.0
            if numeric >= 12:
                findings.append(
                    ComplianceFinding(
                        "HEAVY_CONSUMPTION_TARGETING",
                        "Targeting customers who order more than 12 times a month with a "
                        "reorder push may encourage excessive consumption.",
                        ComplianceSeverity.CRITICAL,
                        True,
                        f"purchase_frequency_per_month {operator} {value}",
                    )
                )

    walk(segment_rule)
    return [f for f in findings if f.code not in config.disabled_rules]


def check_campaign(
    *,
    subject: str,
    body: str,
    channel: Channel,
    objective: str,
    segment_rule: dict | None,
    config: ComplianceConfig,
    approved_by_human: bool = False,
    drafted: bool = False,
    vouched_for: dict[str, str] | None = None,
) -> ComplianceReport:
    """Full campaign-level compliance report.

    `drafted` says the body below is a fallback rather than the message: each
    recipient's copy is written at send time. It does not change what is
    checked — the fallback has to be compliant too, and every draft is checked
    again as it is written — but the report is the record of what a person
    approved, and it would be a misleading record if it did not say that the
    text in it is not the text that will be sent.
    """
    findings: list[ComplianceFinding] = []
    combined = f"{subject}\n{body}" if channel == Channel.EMAIL else body

    if not (body or "").strip():
        findings.append(
            ComplianceFinding(
                "EMPTY_MESSAGE",
                "Campaign has no message body.",
                ComplianceSeverity.CRITICAL,
                True,
            )
        )
    if channel == Channel.EMAIL and not (subject or "").strip():
        findings.append(
            ComplianceFinding(
                "EMPTY_SUBJECT",
                "Email campaign has no subject line.",
                ComplianceSeverity.CRITICAL,
                True,
            )
        )

    findings.extend(check_content(combined, config, channel=channel))
    findings.extend(check_targeting(segment_rule=segment_rule, objective=objective, config=config))

    # A named person has read this and taken responsibility for the claims the
    # engine cannot check. The finding stays in the report at full severity —
    # it is the record of what was vouched for and by whom — but it no longer
    # stops the send, because the verification it was asking for has happened.
    for finding in findings:
        who = (vouched_for or {}).get(finding.code)
        if who and finding.vouchable:
            finding.blocks_send = False
            finding.vouched_by = who
            finding.message = f"{finding.message} Confirmed by {who}."

    if not approved_by_human:
        findings.append(
            ComplianceFinding(
                "REQUIRES_HUMAN_APPROVAL",
                "Campaign has not been approved by a human reviewer.",
                ComplianceSeverity.INFO,
                False,
            )
        )

    if drafted:
        findings.append(
            ComplianceFinding(
                "COPY_DRAFTED_PER_RECIPIENT",
                "Each recipient's message is written at send time and is not the copy "
                "checked here, which is used only if a draft fails. Every draft is "
                "checked against these same rules before it sends.",
                ComplianceSeverity.INFO,
                False,
            )
        )

    return ComplianceReport(findings=findings, checked_at=datetime.utcnow())
