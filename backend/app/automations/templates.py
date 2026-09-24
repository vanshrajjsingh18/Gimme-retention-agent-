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
from app.services.merge_tags import (
    FALLBACKS,
    MESSAGE_FIELDS,
    PLACEHOLDER,
    customer_field_values,
    sample_values,
    tidy,
    token_of as _token,
)

__all__ = [
    "DEFAULT_SMS_TEMPLATES",
    "MERGE_TAGS",
    "PLACEHOLDER",
    "build_context",
    "default_template",
    "get_brand",
    "render",
    "sample_context",
    "sign_off",
    "tidy",
    "unresolved_tokens",
]

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
    # Raw, not already fallen back. The renderer applies the fallback, and it
    # can only report that it did so — "was this customer greeted by name or
    # by default?" — if what reaches it is the empty value.
    first_name = (customer.first_name or "").strip()
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
        # Every whitelisted customer field, read by one deterministic mapping
        # so the composer's Personalize menu and the sender cannot disagree
        # about what a tag means.
        **customer_field_values(customer),
        # The legacy spellings. The whitelist above owns first_name, last_name,
        # full_name and city; repeating them here is how they came to be
        # pre-filled with their own fallbacks in one place and not the other.
        "name": first_name,
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
#:
#: One table, shared with the resolver the campaign sender uses. Two of them
#: meant a token could fall back here and render a bare gap there, which is
#: exactly what happened to {favourite_brand} the first time this moved.
_EMPTY_FALLBACKS = FALLBACKS


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


def unresolved_tokens(text: str) -> list[str]:
    """Placeholders still present after rendering — never safe to send."""
    return [_token(m) for m in PLACEHOLDER.finditer(text)]


#: The tokens copy may use, for the composer to list. Read straight off the
#: whitelist, so a field added there appears in the menu without anybody
#: remembering to add it twice.
MERGE_TAGS: list[dict[str, str]] = [f.as_dict() for f in MESSAGE_FIELDS]


def sample_context() -> dict[str, str]:
    """The examples above as a context, for previewing copy with no customer.

    A test send with nobody attached would otherwise show the raw tokens and
    read as broken. Showing an obvious stand-in is what a preview is for.
    """
    return sample_values()


def get_brand(db: Session) -> BrandSettings:
    from app.services.brand import get_brand_settings

    return get_brand_settings(db)
