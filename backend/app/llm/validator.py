"""Validation of LLM-generated message content.

Runs *after* generation and *before* a message can be approved or sent. It
combines the compliance content rules with grounding checks specific to
generated text: invented customer facts, unverified product names, and channel
length limits.

A message that fails validation cannot be approved. An operator may edit it and
revalidate.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from app.compliance.engine import ComplianceConfig, ComplianceFinding, check_content
from app.core.enums import Channel, ComplianceSeverity
from app.llm.prompts import CHANNEL_LIMITS, GroundingContext

# Claims about the customer the model cannot possibly know from the context.
INVENTED_FACT_PATTERNS: list[tuple[str, str]] = [
    (
        "INVENTED_CUSTOMER_FACT",
        r"\byour\s+(?:birthday|anniversary|wedding|graduation|promotion|new\s+job|"
        r"new\s+home|housewarming|baby|engagement)\b"
        r"|\bhappy\s+(?:birthday|anniversary|retirement|graduation)\b"
        r"|\bcongratulations\s+on\b",
    ),
    (
        "INVENTED_CUSTOMER_FACT",
        r"\b(?:we\s+(?:know|hear|noticed)\s+you(?:'re|\s+are)\s+"
        r"(?:stressed|sad|lonely|celebrating|struggling|going\s+through))\b",
    ),
    (
        "INVENTED_CUSTOMER_FACT",
        r"\byou(?:'ve|\s+have)\s+(?:been\s+)?(?:working\s+hard|had\s+a\s+(?:tough|rough|long)\s+"
        r"(?:day|week|month|year))\b",
    ),
    (
        "INVENTED_CUSTOMER_FACT",
        r"\byour\s+(?:family|kids|children|partner|husband|wife|flatmates)\b",
    ),
]

PRICE_PATTERN = re.compile(r"\$\s?\d+(?:\.\d{2})?")


@dataclass
class ValidationResult:
    valid: bool = True
    findings: list[ComplianceFinding] = field(default_factory=list)
    warnings: list[ComplianceFinding] = field(default_factory=list)
    subject_length: int = 0
    body_length: int = 0
    body_word_count: int = 0

    def as_dict(self) -> dict:
        return {
            "valid": self.valid,
            "errors": [f.as_dict() for f in self.findings],
            "warnings": [f.as_dict() for f in self.warnings],
            "subject_length": self.subject_length,
            "body_length": self.body_length,
            "body_word_count": self.body_word_count,
        }


def parse_llm_output(raw: str) -> tuple[str, str]:
    """Extract ``(subject, body)`` from a model response.

    Tolerates code fences and leading prose, and falls back to treating the
    whole response as the body so a malformed response still surfaces in the
    UI for editing rather than vanishing.
    """
    text = (raw or "").strip()
    if not text:
        return "", ""

    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            data = json.loads(text[start : end + 1])
            if isinstance(data, dict):
                return str(data.get("subject", "") or ""), str(data.get("body", "") or "")
        except json.JSONDecodeError:
            pass

    return "", text


def validate_message(
    *,
    subject: str,
    body: str,
    channel: Channel,
    context: GroundingContext,
    config: ComplianceConfig,
) -> ValidationResult:
    """Validate generated content against grounding, compliance and limits."""
    result = ValidationResult()
    combined = f"{subject}\n{body}" if channel == Channel.EMAIL else body
    lowered = combined.lower()

    errors: list[ComplianceFinding] = []
    warnings: list[ComplianceFinding] = []

    # 1. All compliance content rules (prohibited claims, unverified coupons,
    #    promotions, delivery and stock claims, placeholders, required statements).
    for finding in check_content(combined, config, channel=channel):
        (errors if finding.blocks_send else warnings).append(finding)

    # 2. Invented customer facts.
    for code, pattern in INVENTED_FACT_PATTERNS:
        match = re.search(pattern, lowered, re.IGNORECASE)
        if match:
            errors.append(
                ComplianceFinding(
                    code,
                    "States a fact about the customer that is not in the verified context: "
                    f"'{match.group(0).strip()}'.",
                    ComplianceSeverity.CRITICAL,
                    True,
                    match.group(0).strip(),
                )
            )

    # 3. Product names must come from the customer's own purchase history or
    #    the verified product catalogue.
    allowed_products = _allowed_product_tokens(context)
    for phrase in _candidate_product_mentions(combined):
        if not _product_is_verified(phrase, allowed_products):
            errors.append(
                ComplianceFinding(
                    "UNVERIFIED_PRODUCT",
                    f"Mentions '{phrase}', which is not in this customer's purchase history "
                    "or the verified product list.",
                    ComplianceSeverity.CRITICAL,
                    True,
                    phrase,
                )
            )

    # 4. Prices. The system holds historical order totals, not a live price
    #    list, so any specific price in outbound copy is unverifiable.
    price = PRICE_PATTERN.search(combined)
    if price and not _price_is_verified(price.group(0), context):
        errors.append(
            ComplianceFinding(
                "UNVERIFIED_PRICE",
                f"States the price '{price.group(0)}', which is not backed by verified "
                "product or promotion data.",
                ComplianceSeverity.CRITICAL,
                True,
                price.group(0),
            )
        )

    # 5. Words the brand has asked to avoid.
    for word in context.words_to_avoid:
        token = word.strip().lower()
        if token and re.search(rf"\b{re.escape(token)}\b", lowered):
            warnings.append(
                ComplianceFinding(
                    "BRAND_WORD_TO_AVOID",
                    f"Uses '{word}', which is on the brand's words-to-avoid list.",
                    ComplianceSeverity.WARNING,
                    False,
                    word,
                )
            )

    # 6. Channel limits.
    result.subject_length = len(subject or "")
    result.body_length = len(body or "")
    result.body_word_count = len((body or "").split())
    limits = CHANNEL_LIMITS[channel]

    if "subject_max_chars" in limits and result.subject_length > limits["subject_max_chars"]:
        warnings.append(
            ComplianceFinding(
                "SUBJECT_TOO_LONG",
                f"Subject is {result.subject_length} characters; the limit is "
                f"{limits['subject_max_chars']}.",
                ComplianceSeverity.WARNING,
                False,
            )
        )
    if "body_max_words" in limits and result.body_word_count > limits["body_max_words"]:
        warnings.append(
            ComplianceFinding(
                "BODY_TOO_LONG",
                f"Body is {result.body_word_count} words; the guideline is "
                f"{limits['body_max_words']}.",
                ComplianceSeverity.WARNING,
                False,
            )
        )
    if "body_max_chars" in limits and result.body_length > limits["body_max_chars"]:
        # A hard limit for SMS/WhatsApp: over-length messages are truncated or
        # split by the provider, so this blocks rather than warns.
        errors.append(
            ComplianceFinding(
                "BODY_EXCEEDS_CHANNEL_LIMIT",
                f"Body is {result.body_length} characters; the {channel.value} limit is "
                f"{limits['body_max_chars']}.",
                ComplianceSeverity.CRITICAL,
                True,
            )
        )

    # 7. Structural sanity.
    if not (body or "").strip():
        errors.append(
            ComplianceFinding(
                "EMPTY_BODY", "Generated message has no body.", ComplianceSeverity.CRITICAL, True
            )
        )
    if channel == Channel.EMAIL and not (subject or "").strip():
        errors.append(
            ComplianceFinding(
                "EMPTY_SUBJECT",
                "Email message has no subject line.",
                ComplianceSeverity.CRITICAL,
                True,
            )
        )

    result.findings = errors
    result.warnings = warnings
    result.valid = not errors
    return result


# --------------------------------------------------------------------------
def _allowed_product_tokens(context: GroundingContext) -> set[str]:
    names: set[str] = set()
    for product in context.top_products:
        name = str(product.get("product_name", "")).lower().strip()
        if name:
            names.add(name)
    for product in context.verified_products:
        name = str(
            product.get("product_name") or product.get("name") or ""
        ).lower().strip()
        if name:
            names.add(name)
    for brand in context.preferred_brands:
        if brand:
            names.add(str(brand).lower().strip())
    return names


# Product-shaped mentions: a capitalised multi-word phrase followed by a
# pack/format token, which is how drinks products are named.
PRODUCT_MENTION = re.compile(
    r"\b((?:[A-Z][\w'&-]+\s+){1,4}(?:\d+\s*(?:pk|pack)|IPA|Lager|Pilsner|Ale|Stout|"
    r"Sauvignon\s+Blanc|Pinot\s+Noir|Chardonnay|Merlot|Ros[ée]|Gin|Vodka|Whisky|Whiskey|"
    r"Rum|Tequila|Cider|Seltzer))\b"
)


def _candidate_product_mentions(text: str) -> list[str]:
    return list({m.group(1).strip() for m in PRODUCT_MENTION.finditer(text or "")})


def _product_is_verified(phrase: str, allowed: set[str]) -> bool:
    p = phrase.lower().strip()
    for name in allowed:
        if p in name or name in p:
            return True
    return False


def _price_is_verified(price_text: str, context: GroundingContext) -> bool:
    """A price is acceptable only if it appears verbatim in verified data."""
    normalized = price_text.replace(" ", "")
    haystacks = list(context.verified_promotions) + [
        str(p.get("price", "")) for p in context.verified_products
    ] + [str(p.get("product_name", "")) for p in context.verified_products]
    return any(normalized in h.replace(" ", "") for h in haystacks if h)
