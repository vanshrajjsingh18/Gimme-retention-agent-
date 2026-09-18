"""Message templates for automations, and the segment-to-copy defaults.

Templates use ``{placeholder}`` tokens rather than an LLM, because most
automation copy is fixed and reviewed once. Two rules matter:

* an unresolved placeholder must never reach a customer — it is rendered as a
  sensible fallback, and an unknown token is flagged rather than sent;
* nothing is invented. Product names, promotions and the sign-off all come
  from verified brand settings, so a template that references something the
  business has not approved renders without it instead of making it up.
"""
from __future__ import annotations

import re
from datetime import datetime

from sqlalchemy.orm import Session

from app.core.enums import CampaignObjective
from app.models.entities import BrandSettings, Customer

#: Two spellings of the same thing. ``{token}`` is what the seeded templates
#: use; ``#token#`` is the convention in every other SMS tool, and so the one
#: somebody writing copy in the composer reaches for first. Both resolve
#: against the same context, and neither is the second-class spelling.
PLACEHOLDER = re.compile(r"\{([a-z_]+)\}|#([a-z_]+)#")


def _token(match: re.Match) -> str:
    """The token name, whichever of the two spellings matched."""
    return match.group(1) or match.group(2)

#: Default copy per campaign objective, reused as the segment-specific split
#: for cohort sends: a lapsed repeat buyer gets a reorder reminder, a one-time
#: buyer gets encouragement toward a second order.
DEFAULT_SMS_TEMPLATES: dict[str, str] = {
    CampaignObjective.REORDER.value: (
        "Hi {first_name}, it's about the time you usually restock. "
        "Your {usual_category} order is a tap away at {website}. Reply STOP to opt out."
    ),
    CampaignObjective.SECOND_ORDER.value: (
        "Hi {first_name}, thanks for your first order with GIMME. "
        "Whenever you're ready for round two we're here — {website}. Reply STOP to opt out."
    ),
    CampaignObjective.RETENTION.value: (
        "Hi {first_name}, it's been longer than usual since your last order. "
        "Nothing's changed on our end — {website} whenever you need us. Reply STOP to opt out."
    ),
    CampaignObjective.REACTIVATION.value: (
        "Hi {first_name}, it's been a while. We're still delivering to {city} and your "
        "order history is where you left it: {website}. Reply STOP to opt out."
    ),
    CampaignObjective.WIN_BACK.value: (
        "Hi {first_name}, we'd love to have you back. Everything you used to order is "
        "still one tap away at {website}. Reply STOP to opt out."
    ),
    CampaignObjective.VIP_APPRECIATION.value: (
        "Hi {first_name}, you're one of our most loyal customers and we wanted to say "
        "thanks properly. {website}. Reply STOP to opt out."
    ),
    CampaignObjective.ACTIVATION.value: (
        "Hi {first_name}, welcome to GIMME. {delivery_promise}. Reply STOP to opt out."
    ),
}

#: Which objective a default segment's copy should use. Overridable per
#: campaign; this is only the starting point when nothing else is specified.
SEGMENT_OBJECTIVE_DEFAULTS: dict[str, str] = {
    "New Customers": CampaignObjective.ACTIVATION.value,
    "Needs Second Order": CampaignObjective.SECOND_ORDER.value,
    "Regulars": CampaignObjective.REORDER.value,
    "VIP Customers": CampaignObjective.VIP_APPRECIATION.value,
    "High Value Customers": CampaignObjective.VIP_APPRECIATION.value,
    "At Risk": CampaignObjective.RETENTION.value,
    "High Value At Risk": CampaignObjective.RETENTION.value,
    "Critical Churn Risk": CampaignObjective.RETENTION.value,
    "Dormant": CampaignObjective.REACTIVATION.value,
    "Churned": CampaignObjective.WIN_BACK.value,
    "Recently Reactivated": CampaignObjective.REORDER.value,
}


def default_template(*, segment_name: str | None, objective: str) -> str:
    """The starting copy for a cohort send.

    Segment mapping wins when the segment is one we recognise, because the
    audience is a better description of what to say than the objective label
    somebody happened to pick.
    """
    if segment_name and segment_name in SEGMENT_OBJECTIVE_DEFAULTS:
        objective = SEGMENT_OBJECTIVE_DEFAULTS[segment_name]
    return DEFAULT_SMS_TEMPLATES.get(
        objective, DEFAULT_SMS_TEMPLATES[CampaignObjective.RETENTION.value]
    )


def sign_off(brand: BrandSettings) -> str:
    """A named sign-off, or nothing at all.

    Left blank until a real signatory is configured: attributing a message to
    an invented person is worse than sending it unsigned.
    """
    if not brand.signatory_name:
        return ""
    if brand.signatory_title:
        return f"{brand.signatory_name}, {brand.signatory_title}"
    return brand.signatory_name


def build_context(
    customer: Customer,
    brand: BrandSettings,
    *,
    extra: dict | None = None,
    now: datetime | None = None,
) -> dict[str, str]:
    """Verified values a template may reference.

    Everything here is either the customer's own record or an approved brand
    setting — there is no free text, so a rendered message cannot claim
    anything the business has not signed off.
    """
    first_name = (customer.first_name or "there").strip() or "there"
    # The favourites live on the metrics row the intelligence pass writes.
    # Absent for a customer with no order history, which the fallbacks cover.
    metrics = customer.metrics
    brands = [b for b in (metrics.preferred_brands or []) if b] if metrics else []
    categories = [c for c in (metrics.preferred_categories or []) if c] if metrics else []
    products = (
        [
            p.get("product_name")
            for p in (metrics.top_products or [])
            if isinstance(p, dict) and p.get("product_name")
        ]
        if metrics
        else []
    )

    context = {
        "name": first_name,
        "first_name": first_name,
        "last_name": (customer.last_name or "").strip(),
        "full_name": customer.full_name or "there",
        "city": customer.city or "your area",
        "company": brand.company_name or "GIMME",
        "website": brand.website or "",
        "link": brand.website or "",
        "delivery_promise": brand.delivery_promise or "",
        "support_phone": brand.customer_service_phone or "",
        "support_email": brand.customer_service_email or "",
        "sign_off": sign_off(brand),
        "favourite_brand": brands[0] if brands else "",
        "favourite_category": categories[0] if categories else "",
        "favourite_product": products[0] if products else "",
        "usual_category": categories[0] if categories else "usual",
        "promotion": "",
        "coupon_code": "",
        "usual_day": "",
    }
    if extra:
        context.update({k: ("" if v is None else str(v)) for k, v in extra.items()})
    return context


#: Fallbacks for tokens that would otherwise render as an empty gap mid-sentence.
#: The favourites are the reason this matters most: a customer with no order
#: history has none, and "$10 off " is a worse message than "$10 off your
#: favourites".
_EMPTY_FALLBACKS = {
    "website": "gimme",
    "link": "gimme",
    "usual_category": "usual",
    "city": "your area",
    "name": "there",
    "first_name": "there",
    "full_name": "there",
    "favourite_brand": "your favourites",
    "favourite_category": "your usual",
    "favourite_product": "your usual order",
}


def render(template: str, context: dict[str, str]) -> str:
    """Fill a template, then tidy up what the values left behind.

    An unknown token is left visible rather than silently deleted so it fails
    the compliance placeholder check instead of shipping a broken sentence.
    """
    def substitute(match: re.Match) -> str:
        key = _token(match)
        if key not in context:
            return match.group(0)
        value = context[key]
        return value if value else _EMPTY_FALLBACKS.get(key, "")

    rendered = PLACEHOLDER.sub(substitute, template)
    return tidy(rendered)


def tidy(text: str) -> str:
    """Clean up the punctuation an empty substitution leaves behind."""
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\s+([,.!?])", r"\1", text)
    text = re.sub(r"([,.]) *\1+", r"\1", text)
    text = re.sub(r" *\n *", "\n", text)
    return text.strip()


def unresolved_tokens(text: str) -> list[str]:
    """Placeholders still present after rendering — never safe to send."""
    return [_token(m) for m in PLACEHOLDER.finditer(text)]


#: The tokens copy may use, for the composer to list. Ordered as a person
#: writing a message would reach for them.
MERGE_TAGS: list[dict[str, str]] = [
    {"token": "name", "label": "First name", "example": "Sarah"},
    {"token": "full_name", "label": "Full name", "example": "Sarah Patel"},
    {"token": "city", "label": "City", "example": "Auckland"},
    {"token": "favourite_brand", "label": "Favourite brand", "example": "Steinlager"},
    {"token": "favourite_category", "label": "Favourite category", "example": "Beer"},
    {
        "token": "favourite_product",
        "label": "Most ordered product",
        "example": "Steinlager Classic 12pk",
    },
    {"token": "link", "label": "Order link", "example": "gimmedelivery.co.nz"},
    {"token": "company", "label": "Company name", "example": "GIMME"},
    {"token": "delivery_promise", "label": "Delivery promise", "example": "in 45 minutes"},
    {"token": "support_phone", "label": "Support phone", "example": "09 123 4567"},
    {"token": "sign_off", "label": "Sign-off", "example": "Alex, Customer Care"},
]


def sample_context() -> dict[str, str]:
    """The examples above as a context, for previewing copy with no customer.

    A test send with nobody attached would otherwise show the raw tokens and
    read as broken. Showing an obvious stand-in is what a preview is for.
    """
    return {tag["token"]: tag["example"] for tag in MERGE_TAGS}


def get_brand(db: Session) -> BrandSettings:
    from app.services.brand import get_brand_settings

    return get_brand_settings(db)
