"""Merge tags: one customer's own details, filled into copy somebody wrote.

A campaign body is written once and sent to thousands of people, so the only
thing that makes the message theirs is what the tags resolve to. Three rules
decide everything in here:

* **A tag resolves against the whitelist below and nothing else.** There is no
  expression to evaluate, no attribute path to walk, and no way for a tag to
  name a column that is not listed. ``#customer.password#`` is not a tag that
  fails — it is not a tag at all, and the same goes for ``{{ anything }}``.
* **The stored template is never overwritten.** Resolution happens on the way
  out, per recipient, so the words that were approved stay on the campaign.
  Editing the message and editing what a customer receives remain the same
  act.
* **It is ordinary application code.** Given the same customer and the same
  template it produces the same message every time. Nothing here asks a model
  where a value should come from; the mapping is the table below and a reader
  can check it.

Values are cleaned before they land in a message: control characters and the
tag delimiters themselves are stripped, so a value can never be mistaken for
markup or for another tag. Substitution is a single pass, so a resolved value
is never itself scanned for tags.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from sqlalchemy.orm import Session

from app.core.timezones import to_local
from app.models.entities import BrandSettings, Customer

# --------------------------------------------------------------------------
# What a tag looks like
# --------------------------------------------------------------------------
#: Two spellings of the same thing. ``{token}`` is what the seeded templates
#: use; ``#token#`` is the convention in every other SMS tool, and so the one
#: somebody writing copy in the composer reaches for first. Both resolve
#: against the same whitelist, and neither is the second-class spelling.
#: Case is forgiving — ``#First_Name#`` is what a person types.
PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}|#([A-Za-z_][A-Za-z0-9_]*)#")

#: Anything hash-delimited that *looks* like somebody reaching for a field.
#: ``#customer.password#`` does not match :data:`PLACEHOLDER`, so without this
#: it would sail through validation and be delivered as literal text. It has
#: to start with a letter so that "rated #1 #2" is not read as a tag.
SUSPECT_TAG = re.compile(r"#([A-Za-z][A-Za-z0-9_. -]{0,40})#")


def token_of(match: re.Match) -> str:
    """The token name, whichever of the two spellings matched."""
    return (match.group(1) or match.group(2)).lower()


# --------------------------------------------------------------------------
# The whitelist
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class MessageField:
    """One thing a message is allowed to say about a customer.

    ``fallback`` is what appears when the customer's record has nothing —
    never a placeholder-looking string, and never an invented number.
    """

    token: str
    label: str
    group: str
    example: str
    fallback: str = ""
    description: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "token": self.token,
            "label": self.label,
            "group": self.group,
            "example": self.example,
            "fallback": self.fallback,
            "description": self.description,
            # What a person types to use it. Spelled out so the UI never has
            # to build the syntax itself and get it subtly wrong.
            "tag": f"#{self.token}#",
        }


#: Everything a message may say about the person receiving it. Ordered the way
#: somebody writing copy reaches for them, because this list is also the
#: Personalize menu.
CUSTOMER_FIELDS: list[MessageField] = [
    MessageField(
        "first_name", "First name", "Customer", "Sarah", "there",
        "Their first name. Falls back to 'there', so a greeting never reads 'Hi ,'.",
    ),
    MessageField("last_name", "Last name", "Customer", "Patel", "", "Their surname."),
    MessageField("full_name", "Full name", "Customer", "Sarah Patel", "there", "First and last name."),
    MessageField("email", "Email address", "Customer", "sarah@example.co.nz", "", "Their email address."),
    MessageField("phone", "Mobile number", "Customer", "021 555 0134", "", "Their mobile number."),
    MessageField(
        "product", "Most ordered product", "Ordering", "Steinlager Classic 12pk",
        "your usual order", "The product they order most often.",
    ),
    MessageField(
        "product_name", "Most ordered product", "Ordering", "Steinlager Classic 12pk",
        "your usual order", "Same as #product#, for copy that reads better with the longer name.",
    ),
    MessageField(
        "category", "Favourite category", "Ordering", "Beer", "your usual",
        "The category they buy from most.",
    ),
    MessageField(
        "brand", "Favourite brand", "Ordering", "Steinlager", "your favourites",
        "The brand they buy most.",
    ),
    MessageField(
        "preferred_category", "Favourite category", "Ordering", "Beer", "your usual",
        "Same as #category#.",
    ),
    MessageField(
        "preferred_brand", "Favourite brand", "Ordering", "Steinlager", "your favourites",
        "Same as #brand#.",
    ),
    MessageField(
        "last_order_date", "Last order date", "Ordering", "14 Sep", "recently",
        "When they last ordered, in New Zealand local time.",
    ),
    # No fallback on any of the money or count fields. A missing number is not
    # an excuse to make one up: "$0.00" and "0 orders" are statements about a
    # customer, and a wrong one is worse than a gap the tidy-up closes.
    MessageField(
        "last_order_amount", "Last order amount", "Ordering", "$68.50", "",
        "What their last order came to.",
    ),
    MessageField(
        "average_order_value", "Average order value", "Ordering", "$62.40", "",
        "What they typically spend per order.",
    ),
    MessageField(
        "order_count", "Number of orders", "Ordering", "12", "",
        "How many completed orders they have placed.",
    ),
    MessageField(
        "preferred_order_day", "Usual order day", "Ordering", "Wednesday", "your usual day",
        "The day of the week they usually order, learned by Smart Reorder.",
    ),
    MessageField(
        "preferred_order_time", "Usual order time", "Ordering", "7:39 pm", "your usual time",
        "The time of day they usually order, learned by Smart Reorder.",
    ),
    MessageField("city", "City", "Customer", "Auckland", "your area", "The city we deliver to."),
]

#: Verified brand settings. Not about the customer, but copy reaches for them
#: in the same breath, and they are on the same whitelist for the same reason.
BRAND_FIELDS: list[MessageField] = [
    MessageField("link", "Order link", "GIMME", "gimmedelivery.co.nz", "gimme", "Your store link."),
    MessageField("company", "Company name", "GIMME", "GIMME", "GIMME", "Your business name."),
    MessageField(
        "delivery_promise", "Delivery promise", "GIMME", "in 45 minutes", "",
        "The delivery promise set in Brand Settings.",
    ),
    MessageField("support_phone", "Support phone", "GIMME", "09 123 4567", "", "Your support number."),
    MessageField("sign_off", "Sign-off", "GIMME", "Alex, Customer Care", "", "Your configured signatory."),
]

MESSAGE_FIELDS: list[MessageField] = CUSTOMER_FIELDS + BRAND_FIELDS

#: Tokens the seeded copy and the automation templates already use. They are
#: allowed but not offered in the menu: ``{name}`` is a second spelling of
#: first_name, and ``{usual_day}`` is filled by Smart Reorder from the
#: routine, not from a customer column. Leaving them off the whitelist would
#: have made every existing template fail validation the day this shipped.
LEGACY_TOKENS: frozenset[str] = frozenset(
    {
        "name",
        "website",
        "support_email",
        "usual_category",
        "usual_day",
        "favourite_brand",
        "favourite_category",
        "favourite_product",
        "promotion",
        "coupon_code",
    }
)

ALLOWED_TOKENS: frozenset[str] = frozenset(f.token for f in MESSAGE_FIELDS) | LEGACY_TOKENS

#: Per-token fallbacks, keyed for the renderer. The legacy spellings carry the
#: same fallback as the field they duplicate: "your favourites" has to mean
#: the same thing whether the copy says #brand# or {favourite_brand}, and
#: keeping a second table is how one of them ends up rendering a bare gap.
FALLBACKS: dict[str, str] = {
    **{f.token: f.fallback for f in MESSAGE_FIELDS if f.fallback},
    "name": "there",
    "website": "gimme",
    "usual_category": "usual",
    "favourite_brand": "your favourites",
    "favourite_category": "your usual",
    "favourite_product": "your usual order",
}


def field_catalog() -> list[dict[str, str]]:
    """The whitelist, as the Personalize menu and the API serve it."""
    return [f.as_dict() for f in MESSAGE_FIELDS]


def sample_values() -> dict[str, str]:
    """The examples above as a context, for previewing with no customer.

    The legacy spellings are filled from the same examples rather than left
    out: a test send of copy written last year should read as a message, not
    as a message with ``{website}`` still in it.
    """
    values = {f.token: f.example for f in MESSAGE_FIELDS}
    values.update(
        {
            "name": values["first_name"],
            "website": values["link"],
            "support_email": "hello@gimmedelivery.co.nz",
            "usual_category": values["category"],
            "usual_day": "Wednesday",
            "favourite_brand": values["brand"],
            "favourite_category": values["category"],
            "favourite_product": values["product"],
            "promotion": "",
            "coupon_code": "",
        }
    )
    return values


def unknown_tags(text: str | None) -> list[str]:
    """Tags in this copy that name something not on the whitelist.

    Returned so the operator is told which tag is wrong rather than that
    "something" is. Used to block approval — a campaign that would deliver a
    literal ``#discont_code#`` should never reach a customer.
    """
    if not text:
        return []
    found: list[str] = []
    for match in PLACEHOLDER.finditer(text):
        token = token_of(match)
        if token not in ALLOWED_TOKENS and token not in found:
            found.append(token)
    for match in SUSPECT_TAG.finditer(text):
        token = match.group(1)
        if token.lower() in ALLOWED_TOKENS or token in found:
            continue
        # Already reported under its lowercase spelling by the loop above.
        if PLACEHOLDER.fullmatch(match.group(0)):
            continue
        found.append(token)
    return found


# --------------------------------------------------------------------------
# Reading a customer's values
# --------------------------------------------------------------------------
def _money(value: float | None) -> str:
    return "" if value is None or value <= 0 else f"${value:,.2f}"


def _first(values) -> str:
    if not values:
        return ""
    for entry in values:
        if isinstance(entry, dict):
            entry = entry.get("product_name")
        if entry:
            return str(entry)
    return ""


def _clock(hour: int | None, minute: int | None) -> str:
    """"7:39 pm" — the routine as a person would say it."""
    if hour is None:
        return ""
    moment = datetime(2000, 1, 1, hour, minute or 0)
    return moment.strftime("%-I:%M %p").lower()


def customer_field_values(customer: Customer | None) -> dict[str, str]:
    """Every customer token's raw value. Empty string where there is none.

    Raw on purpose: fallbacks are applied by the renderer, which needs to know
    the difference between "nothing was there" and "this is what was there" to
    report it afterwards.
    """
    if customer is None:
        return {f.token: "" for f in CUSTOMER_FIELDS}

    metrics = customer.metrics
    brands = _first(metrics.preferred_brands if metrics else None)
    categories = _first(metrics.preferred_categories if metrics else None)
    product = _first(metrics.top_products if metrics else None)
    last_order_at = metrics.last_order_at if metrics else None

    return {
        "first_name": (customer.first_name or "").strip(),
        "last_name": (customer.last_name or "").strip(),
        "full_name": (customer.full_name or "").strip(),
        "email": (customer.email or "").strip(),
        "phone": (customer.phone or "").strip(),
        "city": (customer.city or "").strip(),
        "product": product,
        "product_name": product,
        "category": categories,
        "preferred_category": categories,
        "brand": brands,
        "preferred_brand": brands,
        # Stored UTC, read in the customer's own day — the same rule the rest
        # of the product follows. A NZ evening order is not yesterday.
        "last_order_date": f"{to_local(last_order_at):%-d %b}" if last_order_at else "",
        "last_order_amount": _money(metrics.last_order_amount if metrics else None),
        "average_order_value": _money(metrics.average_order_value if metrics else None),
        "order_count": str(metrics.completed_orders) if metrics and metrics.completed_orders else "",
        "preferred_order_day": (metrics.typical_order_weekday or "") if metrics else "",
        "preferred_order_time": _clock(
            metrics.typical_order_hour if metrics else None,
            metrics.typical_order_minute if metrics else None,
        ),
    }


def brand_field_values(brand: BrandSettings | None) -> dict[str, str]:
    """The brand tokens. Nothing invented — an unset setting resolves empty."""
    if brand is None:
        return {f.token: "" for f in BRAND_FIELDS}
    from app.automations.templates import sign_off

    return {
        "link": brand.website or "",
        "company": brand.company_name or "",
        "delivery_promise": brand.delivery_promise or "",
        "support_phone": brand.customer_service_phone or "",
        "sign_off": sign_off(brand),
    }


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------
#: Characters a resolved value may never carry into a message: control codes,
#: and the tag delimiters themselves. Substitution is single-pass so a value
#: containing "#first_name#" would not be re-resolved anyway — this is so it
#: cannot be mistaken for one by the validator, or by a person reading a log.
_UNSAFE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f{}#]")


def clean_value(value: str) -> str:
    """A field value, made safe to drop into plain-text copy.

    Every channel this sends on is plain text — there is no HTML body — so the
    job is removing what could be read as structure rather than escaping it.
    Newlines collapse to a space because a value with one in it would break an
    email subject line and an SMS segment alike.
    """
    text = _UNSAFE.sub("", str(value))
    return re.sub(r"\s+", " ", text).strip()


def tidy(text: str) -> str:
    """Clean up the punctuation an empty substitution leaves behind."""
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\s+([,.!?])", r"\1", text)
    text = re.sub(r"([,.]) *\1+", r"\1", text)
    text = re.sub(r" *\n *", "\n", text)
    return text.strip()


@dataclass
class ResolvedMessage:
    """One message, and what it took to produce it.

    The extra fields are not decoration: a send log that records only the text
    cannot answer "did this customer get the fallback or their real name?",
    which is the first question asked when copy reads oddly.
    """

    text: str = ""
    template: str = ""
    #: Tokens used by the template whose value was empty for this customer.
    missing_fields: list[str] = field(default_factory=list)
    #: Of those, the ones a fallback covered.
    fallbacks_used: list[str] = field(default_factory=list)
    #: Tags naming something not on the whitelist. Left visible in the text.
    unknown_tags: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.unknown_tags

    def as_dict(self) -> dict:
        return {
            "text": self.text,
            "template": self.template,
            "missing_fields": self.missing_fields,
            "fallbacks_used": self.fallbacks_used,
            "unknown_tags": self.unknown_tags,
        }


def render_template(template: str | None, values: dict[str, str]) -> ResolvedMessage:
    """Fill a template from a context, recording what happened.

    A token the context does not know is left exactly as written. Deleting it
    would ship a broken sentence quietly; leaving it visible means the
    validator catches it and, if it somehow got that far, a person reading the
    message can see what went wrong.
    """
    result = ResolvedMessage(text=template or "", template=template or "")
    if not template:
        return result

    # Copy with no tags in it is returned exactly as written. Running it
    # through the tidy-up would silently reflow whitespace somebody chose, in
    # a message nobody asked to have templated.
    if not PLACEHOLDER.search(template):
        result.unknown_tags = unknown_tags(template)
        return result

    missing: list[str] = []
    fell_back: list[str] = []

    def substitute(match: re.Match) -> str:
        token = token_of(match)
        if token not in values:
            return match.group(0)
        value = clean_value(values[token] or "")
        if value:
            return value
        if token not in missing:
            missing.append(token)
        fallback = FALLBACKS.get(token, "")
        if fallback and token not in fell_back:
            fell_back.append(token)
        return fallback

    result.text = tidy(PLACEHOLDER.sub(substitute, template))
    result.missing_fields = missing
    result.fallbacks_used = fell_back
    result.unknown_tags = unknown_tags(template)
    return result


def context_for(
    db: Session, customer: Customer | None, *, extra: dict | None = None
) -> dict[str, str]:
    """The full whitelist of values for one recipient.

    With nobody attached this is the sample context, so a preview or a test
    send shows the shape of the real message rather than raw tokens.
    """
    from app.automations.templates import build_context, get_brand

    brand = get_brand(db)
    if customer is None:
        values = sample_values()
    else:
        # build_context is what the automations already render against, so
        # going through it keeps one context rather than two that drift.
        values = dict(build_context(customer, brand))
    if extra:
        values.update({k: ("" if v is None else str(v)) for k, v in extra.items()})
    return values


def resolve_message_template(
    db: Session,
    template: str | None,
    customer_id: int | None = None,
    context: dict | None = None,
) -> ResolvedMessage:
    """Fill a template for one customer. The reusable entry point.

    Deliberately independent of any screen: the campaign sender, the test
    send, the preview endpoint and Smart Reorder all call this, so what an
    operator previews is produced by the code that will do the sending.

    ``context`` supplies values a customer record cannot — Smart Reorder's
    ``usual_day``, for instance, which comes from the learned routine.
    """
    customer = db.get(Customer, customer_id) if customer_id else None
    return render_template(template, context_for(db, customer, extra=context))
