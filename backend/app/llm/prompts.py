"""Grounded prompt construction for customer message generation.

The model only ever sees verified, system-computed facts. Anything it is not
given, it is explicitly told it may not invent. The prompt is versioned so a
generated message can be traced back to the instructions that produced it.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

from app.core.enums import Channel

PROMPT_VERSION = "v1"

CHANNEL_LIMITS = {
    Channel.EMAIL: {"subject_max_chars": 78, "body_max_words": 140},
    Channel.SMS: {"body_max_chars": 320},
    Channel.WHATSAPP: {"body_max_chars": 600},
    Channel.PUSH: {"title_max_chars": 48, "body_max_chars": 140},
}

TONE_INSTRUCTIONS = {
    "default": "Keep the brand's normal voice.",
    "shorter": "Make it noticeably shorter while keeping the same offer and call to action.",
    "warmer": "Make it warmer and more human, without becoming gushing or insincere.",
    "more_personal": (
        "Lean harder on the specific verified facts about this customer — their actual "
        "products and ordering pattern."
    ),
    "more_playful": "Add light personality and wit. Stay respectful and never silly about alcohol.",
    "more_premium": "Make the language more refined and understated. No exclamation marks.",
    "remove_sales_language": (
        "Strip out promotional and salesy language. Make it read as a useful, helpful note "
        "rather than an advert."
    ),
}


@dataclass
class GroundingContext:
    """Every fact the model is permitted to use, and nothing else."""

    # Customer facts (verified from the database)
    customer_first_name: str = ""
    lifecycle_stage: str = ""
    total_completed_orders: int = 0
    lifetime_revenue: float = 0.0
    average_order_value: float = 0.0
    days_since_last_order: int | None = None
    expected_cycle_days: float | None = None
    preferred_categories: list[str] = field(default_factory=list)
    preferred_brands: list[str] = field(default_factory=list)
    top_products: list[dict] = field(default_factory=list)
    typical_order_weekday: str | None = None
    city: str | None = None

    # Intelligence
    churn_score: float = 0.0
    churn_risk_band: str = ""
    churn_explanation: str = ""
    rfm_segment: str = ""
    recommended_action: str = ""
    recommendation_explanation: str = ""

    # Communication history
    messages_sent_90d: int = 0
    messages_opened_90d: int = 0
    last_message_summary: str = ""

    # Brand & verified marketing facts
    company_name: str = "GIMME"
    brand_voice: str = ""
    tone: str = ""
    communication_principles: list[str] = field(default_factory=list)
    preferred_vocabulary: list[str] = field(default_factory=list)
    words_to_avoid: list[str] = field(default_factory=list)
    emoji_usage: str = "sparing"
    email_signature: str = ""
    whatsapp_closing: str = ""
    sms_style: str = ""
    customer_service_email: str = ""
    customer_service_phone: str = ""
    website: str = ""
    delivery_areas: list[str] = field(default_factory=list)
    delivery_promise: str = ""
    mission_statement: str = ""
    responsible_drinking_statement: str = ""
    age_restriction_statement: str = ""
    legal_disclaimer: str = ""
    prohibited_claims: list[str] = field(default_factory=list)
    verified_promotions: list[str] = field(default_factory=list)
    verified_coupon_codes: list[str] = field(default_factory=list)
    verified_products: list[dict] = field(default_factory=list)

    # Campaign
    campaign_objective: str = ""
    campaign_name: str = ""
    channel: str = Channel.EMAIL.value

    def as_dict(self) -> dict:
        return asdict(self)


SYSTEM_PROMPT = """You are a retention copywriter for {company_name}, an alcohol delivery \
business in New Zealand. You write short, personal, useful messages to existing customers.

ABSOLUTE RULES — these override every other instruction:

1. You may ONLY state facts that appear in the VERIFIED CONTEXT below. If a fact is not \
in the context, you must not state it, imply it, or approximate it.
2. NEVER invent or mention: discounts, percentages off, dollar amounts off, coupon or \
promo codes, promotions, free delivery, prices, product names, stock levels, availability, \
delivery times, delivery areas, or loyalty points. Only the promotions, coupon codes and \
products listed under VERIFIED PROMOTIONS and VERIFIED PRODUCTS may be mentioned, and only \
exactly as written there. If those lists are empty, mention no offer at all.
3. NEVER invent facts about the customer. Do not guess their age, occupation, household, \
mood, plans, relationships, reason for not ordering, or anything about an occasion. Use \
only the customer facts given.
4. NEVER claim or imply that alcohol improves health, mood, emotional wellbeing, social \
standing, popularity, sexual or romantic success, or professional success.
5. NEVER encourage excessive, rapid or competitive drinking, and never reference drinking \
in connection with driving.
6. NEVER address or appeal to anyone under the legal purchase age.
7. NEVER output an unfilled placeholder such as {{{{name}}}} or [NAME]. Write the real value \
or omit the sentence.
8. Do not include an unsubscribe link, tracking pixel or footer — the sending system adds \
those.

Write in this brand voice: {brand_voice}
Tone: {tone}
Emoji usage: {emoji_usage}

Return ONLY a JSON object, with no surrounding prose or code fences, in this exact shape:
{{"subject": "...", "body": "..."}}
For non-email channels set "subject" to an empty string."""


def build_system_prompt(ctx: GroundingContext) -> str:
    return SYSTEM_PROMPT.format(
        company_name=ctx.company_name or "GIMME",
        brand_voice=ctx.brand_voice or "Friendly, direct, and genuinely useful. Never pushy.",
        tone=ctx.tone or "Warm and conversational",
        emoji_usage=ctx.emoji_usage or "sparing",
    )


def build_user_prompt(ctx: GroundingContext, *, variation: str = "default") -> str:
    """Assemble the verified-context block and the writing task."""
    channel = Channel(ctx.channel)
    limits = CHANNEL_LIMITS[channel]

    customer_facts = {
        "first_name": ctx.customer_first_name,
        "city": ctx.city,
        "lifecycle_stage": ctx.lifecycle_stage,
        "completed_orders": ctx.total_completed_orders,
        "lifetime_revenue_nzd": round(ctx.lifetime_revenue, 2),
        "average_order_value_nzd": round(ctx.average_order_value, 2),
        "days_since_last_order": ctx.days_since_last_order,
        "usual_days_between_orders": (
            round(ctx.expected_cycle_days) if ctx.expected_cycle_days else None
        ),
        "preferred_categories": ctx.preferred_categories,
        "preferred_brands": ctx.preferred_brands,
        "products_they_actually_bought": [p.get("product_name") for p in ctx.top_products],
        "typical_order_day": ctx.typical_order_weekday,
    }
    intelligence = {
        "churn_risk_band": ctx.churn_risk_band,
        "churn_score_out_of_100": ctx.churn_score,
        "why_they_are_at_risk": ctx.churn_explanation,
        "rfm_segment": ctx.rfm_segment,
        "recommended_action": ctx.recommended_action,
        "why_this_action": ctx.recommendation_explanation,
    }
    history = {
        "messages_sent_last_90_days": ctx.messages_sent_90d,
        "messages_opened_last_90_days": ctx.messages_opened_90d,
        "most_recent_message": ctx.last_message_summary or "none",
    }
    verified_marketing = {
        "promotions": ctx.verified_promotions,
        "coupon_codes": ctx.verified_coupon_codes,
        "products": ctx.verified_products,
        "delivery_promise": ctx.delivery_promise,
        "delivery_areas": ctx.delivery_areas,
        "website": ctx.website,
        "customer_service_email": ctx.customer_service_email,
        "customer_service_phone": ctx.customer_service_phone,
    }

    sections = [
        "=== VERIFIED CONTEXT ===",
        "",
        "CUSTOMER FACTS (the only customer facts you may use):",
        json.dumps(customer_facts, indent=2, default=str),
        "",
        "CUSTOMER INTELLIGENCE (computed by the retention system, do not recalculate):",
        json.dumps(intelligence, indent=2, default=str),
        "",
        "COMMUNICATION HISTORY:",
        json.dumps(history, indent=2, default=str),
        "",
        "VERIFIED PROMOTIONS, PRODUCTS AND CONTACT DETAILS "
        "(the ONLY offers and products you may mention):",
        json.dumps(verified_marketing, indent=2, default=str),
        "",
    ]

    if ctx.communication_principles:
        sections += ["COMMUNICATION PRINCIPLES:", _bullets(ctx.communication_principles), ""]
    if ctx.preferred_vocabulary:
        sections += ["PREFERRED VOCABULARY:", ", ".join(ctx.preferred_vocabulary), ""]
    if ctx.words_to_avoid:
        sections += ["WORDS TO AVOID:", ", ".join(ctx.words_to_avoid), ""]
    if ctx.prohibited_claims:
        sections += ["ADDITIONAL PROHIBITED CLAIMS:", _bullets(ctx.prohibited_claims), ""]

    sections += [
        "=== WRITING TASK ===",
        "",
        f"Channel: {channel.value}",
        f"Campaign objective: {ctx.campaign_objective or ctx.recommended_action}",
        f"Constraints: {json.dumps(limits)}",
        "",
        _channel_instructions(ctx, channel),
        "",
        f"Variation instruction: {TONE_INSTRUCTIONS.get(variation, TONE_INSTRUCTIONS['default'])}",
        "",
        "Remember: state nothing that is not in the verified context above. "
        "Return only the JSON object.",
    ]
    return "\n".join(sections)


def _bullets(items: list[str]) -> str:
    return "\n".join(f"- {item}" for item in items)


def _channel_instructions(ctx: GroundingContext, channel: Channel) -> str:
    if channel == Channel.EMAIL:
        required = []
        if ctx.responsible_drinking_statement:
            required.append(
                "End the body with this exact responsible drinking statement on its own line: "
                f'"{ctx.responsible_drinking_statement}"'
            )
        if ctx.age_restriction_statement:
            required.append(
                "Immediately after it, include this exact age statement on its own line: "
                f'"{ctx.age_restriction_statement}"'
            )
        if ctx.email_signature:
            required.append(f'Sign off with: "{ctx.email_signature}"')
        return "\n".join(
            [
                "Write an email. The subject must be under 78 characters and must not be "
                "clickbait. The body must be under 140 words, in short paragraphs.",
                *required,
            ]
        )
    if channel == Channel.SMS:
        style = f" Style guidance: {ctx.sms_style}" if ctx.sms_style else ""
        return (
            "Write a single SMS under 320 characters. No subject line. Be direct — one "
            f"sentence of context and one clear next step. Do not use emoji.{style}"
        )
    if channel == Channel.WHATSAPP:
        closing = (
            f' Close with: "{ctx.whatsapp_closing}"' if ctx.whatsapp_closing else ""
        )
        return (
            "Write a WhatsApp message under 600 characters. Conversational, like a message "
            f"from a person rather than a brand broadcast.{closing}"
        )
    return (
        "Write push notification copy: a title under 48 characters and a body under 140 "
        "characters. Put the title in the 'subject' field."
    )
