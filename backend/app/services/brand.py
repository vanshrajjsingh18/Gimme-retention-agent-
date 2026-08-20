"""Brand settings and compliance-rule bootstrap.

The brand settings row is the single source of truth for both LLM grounding
and the content compliance rules, so a change to allowed promotions
immediately narrows what the model may say and what the validator will accept.
"""
from __future__ import annotations

from datetime import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.compliance.engine import ComplianceConfig
from app.core.enums import ComplianceSeverity
from app.llm.prompts import GroundingContext
from app.models.entities import BrandSettings, ComplianceRule

BRAND_SETTINGS_ID = 1

GIMME_DEFAULTS: dict = {
    "company_name": "GIMME",
    "company_description": (
        "GIMME is a New Zealand on-demand beverage delivery service. We deliver beer, wine, "
        "spirits, mixers and non-alcoholic drinks to customers' doors, fast."
    ),
    "brand_voice": (
        "Friendly, direct and genuinely useful. We talk like a helpful local, not a "
        "marketing department. Short sentences. No hype, no pressure, no fake urgency."
    ),
    "tone": "Warm, casual and confident",
    "communication_principles": [
        "Be useful before being promotional.",
        "Say the thing plainly; never bury the point.",
        "Reference what the customer actually bought, never a guess.",
        "Never manufacture urgency or scarcity.",
        "Respect the customer's right to not hear from us.",
        "Always promote responsible consumption.",
    ],
    "preferred_vocabulary": [
        "your usual",
        "restock",
        "at your door",
        "whenever you're ready",
        "cheers",
    ],
    "words_to_avoid": [
        "cheap",
        "booze",
        "smash",
        "unmissable",
        "act now",
        "limited time only",
        "guilt-free",
    ],
    "emoji_usage": "sparing",
    "max_email_words": 140,
    "max_sms_characters": 320,
    "max_whatsapp_characters": 600,
    "email_signature": "Cheers,\nThe GIMME Team",
    "whatsapp_closing": "— The GIMME Team",
    "sms_style": "One sentence of context, one clear next step. No emoji, no links beyond ours.",
    "customer_service_phone": "0800 446 634",
    "customer_service_email": "help@gimmedelivery.co.nz",
    "website": "gimmedelivery.co.nz",
    "delivery_areas": [
        "Auckland Central",
        "Ponsonby",
        "Grey Lynn",
        "Mount Eden",
        "Newmarket",
        "Parnell",
        "Takapuna",
        "Wellington Central",
        "Christchurch Central",
    ],
    "delivery_promise": "Delivered in 60 minutes across our delivery areas",
    "mission_statement": (
        "Make getting the drinks you actually want as easy as deciding you want them."
    ),
    "responsible_drinking_statement": "Please enjoy responsibly.",
    "legal_disclaimer": (
        "GIMME is a licensed remote seller of alcohol. Licence number OFF-2024-0001."
    ),
    "age_restriction_statement": (
        "You must be 18 or over to purchase alcohol. We ID on delivery."
    ),
    "prohibited_claims": [
        "cheapest in town",
        "unlimited drinks",
        "drink more for less",
    ],
    "allowed_promotions": [],
    "active_coupon_codes": [],
    "verified_products": [],
    "minimum_age": 18,
}


DEFAULT_COMPLIANCE_RULES: list[dict] = [
    {
        "code": "AGE_VERIFICATION",
        "name": "Verified age required",
        "description": (
            "Alcohol marketing may only be sent to customers whose age has been verified."
        ),
        "severity": ComplianceSeverity.CRITICAL.value,
        "blocks_send": True,
        "config": {"minimum_age": 18, "require_age_verification": True},
    },
    {
        "code": "MARKETING_CONSENT",
        "name": "Marketing consent required",
        "description": "Customers must have granted marketing consent before being contacted.",
        "severity": ComplianceSeverity.CRITICAL.value,
        "blocks_send": True,
        "config": {},
    },
    {
        "code": "CHANNEL_CONSENT",
        "name": "Channel-specific consent required",
        "description": "Consent is per channel; email consent does not imply SMS consent.",
        "severity": ComplianceSeverity.CRITICAL.value,
        "blocks_send": True,
        "config": {},
    },
    {
        "code": "SUPPRESSION",
        "name": "Suppression list enforced",
        "description": "Suppressed customers are excluded from every campaign.",
        "severity": ComplianceSeverity.CRITICAL.value,
        "blocks_send": True,
        "config": {},
    },
    {
        "code": "FREQUENCY_CAP",
        "name": "Message frequency caps",
        "description": "Limits how many marketing messages a customer can receive.",
        "severity": ComplianceSeverity.CRITICAL.value,
        "blocks_send": True,
        "config": {"cap_30d": 4, "cap_7d": 2},
    },
    {
        "code": "QUIET_HOURS",
        "name": "Quiet hours for SMS and WhatsApp",
        "description": "No SMS or WhatsApp messages between 21:00 and 09:00.",
        "severity": ComplianceSeverity.CRITICAL.value,
        "blocks_send": True,
        "config": {"start": "21:00", "end": "09:00", "enabled": True},
    },
    {
        "code": "RESPONSIBLE_DRINKING",
        "name": "Responsible drinking statement on email",
        "description": "Every marketing email must carry the responsible drinking statement.",
        "severity": ComplianceSeverity.CRITICAL.value,
        "blocks_send": True,
        "config": {"required": True},
    },
    {
        "code": "AGE_STATEMENT",
        "name": "Age restriction statement on email",
        "description": "Marketing emails should carry the R18 statement.",
        "severity": ComplianceSeverity.WARNING.value,
        "blocks_send": False,
        "config": {"required": True},
    },
    {
        "code": "PROHIBITED_CLAIMS",
        "name": "Prohibited alcohol claims",
        "description": (
            "Blocks claims that alcohol improves health, mood, social standing, sexual or "
            "professional success, encourages excessive consumption, appeals to minors, or "
            "associates drinking with driving."
        ),
        "severity": ComplianceSeverity.CRITICAL.value,
        "blocks_send": True,
        "config": {},
    },
    {
        "code": "GROUNDED_CLAIMS",
        "name": "No invented offers, products, prices or facts",
        "description": (
            "Blocks unverified coupon codes, discounts, promotions, product names, prices, "
            "delivery times, stock claims and invented customer facts."
        ),
        "severity": ComplianceSeverity.CRITICAL.value,
        "blocks_send": True,
        "config": {},
    },
    {
        "code": "VULNERABILITY_TARGETING",
        "name": "No targeting of inferred vulnerability",
        "description": (
            "Flags audiences selected on discount dependency or very high consumption "
            "frequency."
        ),
        "severity": ComplianceSeverity.CRITICAL.value,
        "blocks_send": True,
        "config": {},
    },
    {
        "code": "HUMAN_APPROVAL",
        "name": "Human approval required before sending",
        "description": "No campaign can send without an explicit human approval action.",
        "severity": ComplianceSeverity.CRITICAL.value,
        "blocks_send": True,
        "config": {},
    },
]


def get_brand_settings(db: Session) -> BrandSettings:
    """Return the singleton brand settings row, creating it on first access."""
    settings_row = db.get(BrandSettings, BRAND_SETTINGS_ID)
    if settings_row is None:
        settings_row = BrandSettings(id=BRAND_SETTINGS_ID, **GIMME_DEFAULTS)
        db.add(settings_row)
        db.commit()
        db.refresh(settings_row)
    return settings_row


def ensure_compliance_rules(db: Session) -> int:
    created = 0
    for spec in DEFAULT_COMPLIANCE_RULES:
        exists = db.execute(
            select(ComplianceRule.id).where(ComplianceRule.code == spec["code"])
        ).first()
        if exists:
            continue
        db.add(ComplianceRule(**spec))
        created += 1
    db.commit()
    return created


def _parse_time(value: str, fallback: time) -> time:
    try:
        hour, minute = value.split(":")
        return time(int(hour), int(minute))
    except (ValueError, AttributeError):
        return fallback


def build_compliance_config(db: Session) -> ComplianceConfig:
    """Assemble the live compliance configuration from brand + rule rows."""
    brand = get_brand_settings(db)
    rules = {r.code: r for r in db.execute(select(ComplianceRule)).scalars().all()}

    def enabled(code: str, default: bool = True) -> bool:
        rule = rules.get(code)
        return default if rule is None else bool(rule.enabled)

    def config_of(code: str) -> dict:
        rule = rules.get(code)
        return (rule.config or {}) if rule else {}

    freq = config_of("FREQUENCY_CAP")
    quiet = config_of("QUIET_HOURS")
    age_cfg = config_of("AGE_VERIFICATION")

    disabled = {code for code, rule in rules.items() if not rule.enabled}
    # Content rule codes are finer-grained than the rule rows, so disabling the
    # umbrella rule disables the codes it owns.
    if "PROHIBITED_CLAIMS" in disabled:
        disabled |= {
            "HEALTH_CLAIM",
            "EMOTIONAL_WELLBEING_CLAIM",
            "SOCIAL_SUCCESS_CLAIM",
            "SEXUAL_SUCCESS_CLAIM",
            "PROFESSIONAL_SUCCESS_CLAIM",
            "EXCESSIVE_CONSUMPTION",
            "UNDERAGE_APPEAL",
            "DRINK_DRIVING",
            "BRAND_PROHIBITED_CLAIM",
        }
    if "GROUNDED_CLAIMS" in disabled:
        disabled |= {
            "UNVERIFIED_COUPON_CODE",
            "UNVERIFIED_PROMOTION",
            "UNVERIFIED_DELIVERY_CLAIM",
            "UNVERIFIED_STOCK_CLAIM",
            "UNVERIFIED_PRODUCT",
            "UNVERIFIED_PRICE",
            "INVENTED_CUSTOMER_FACT",
        }
    if "RESPONSIBLE_DRINKING" in disabled:
        disabled.add("MISSING_RESPONSIBLE_DRINKING")
    if "AGE_STATEMENT" in disabled:
        disabled.add("MISSING_AGE_STATEMENT")

    return ComplianceConfig(
        minimum_age=int(age_cfg.get("minimum_age", brand.minimum_age or 18)),
        require_age_verification=(
            enabled("AGE_VERIFICATION")
            and bool(age_cfg.get("require_age_verification", True))
        ),
        frequency_cap_30d=int(freq.get("cap_30d", 4)) if enabled("FREQUENCY_CAP") else 10**6,
        frequency_cap_7d=int(freq.get("cap_7d", 2)) if enabled("FREQUENCY_CAP") else 10**6,
        quiet_hours_start=_parse_time(quiet.get("start", "21:00"), time(21, 0)),
        quiet_hours_end=_parse_time(quiet.get("end", "09:00"), time(9, 0)),
        enforce_quiet_hours=enabled("QUIET_HOURS") and bool(quiet.get("enabled", True)),
        require_responsible_drinking_statement=enabled("RESPONSIBLE_DRINKING"),
        require_age_statement_on_email=enabled("AGE_STATEMENT"),
        allowed_coupon_codes=list(brand.active_coupon_codes or []),
        allowed_promotions=list(brand.allowed_promotions or []),
        verified_products=list(brand.verified_products or []),
        delivery_promise=brand.delivery_promise or "",
        extra_prohibited_claims=list(brand.prohibited_claims or []),
        responsible_drinking_statement=brand.responsible_drinking_statement or "",
        age_restriction_statement=brand.age_restriction_statement or "",
        disabled_rules=disabled,
    )


def apply_brand_to_context(brand: BrandSettings, ctx: GroundingContext) -> GroundingContext:
    """Populate the brand half of an LLM grounding context."""
    ctx.company_name = brand.company_name
    ctx.brand_voice = brand.brand_voice
    ctx.tone = brand.tone
    ctx.communication_principles = list(brand.communication_principles or [])
    ctx.preferred_vocabulary = list(brand.preferred_vocabulary or [])
    ctx.words_to_avoid = list(brand.words_to_avoid or [])
    ctx.emoji_usage = brand.emoji_usage
    ctx.email_signature = brand.email_signature
    ctx.whatsapp_closing = brand.whatsapp_closing
    ctx.sms_style = brand.sms_style
    ctx.customer_service_email = brand.customer_service_email
    ctx.customer_service_phone = brand.customer_service_phone
    ctx.website = brand.website
    ctx.delivery_areas = list(brand.delivery_areas or [])
    ctx.delivery_promise = brand.delivery_promise
    ctx.mission_statement = brand.mission_statement
    ctx.responsible_drinking_statement = brand.responsible_drinking_statement
    ctx.age_restriction_statement = brand.age_restriction_statement
    ctx.legal_disclaimer = brand.legal_disclaimer
    ctx.prohibited_claims = list(brand.prohibited_claims or [])
    ctx.verified_promotions = list(brand.allowed_promotions or [])
    ctx.verified_coupon_codes = list(brand.active_coupon_codes or [])
    ctx.verified_products = list(brand.verified_products or [])
    return ctx
