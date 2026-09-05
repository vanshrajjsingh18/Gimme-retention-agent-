"""Deterministic mock LLM provider.

Produces realistic, grounded copy from the same context the real provider
receives, so the whole product workflow is demonstrable without credentials.
Output is seeded by a hash of the context, making generations reproducible
while still varying between customers and between regenerations.
"""
from __future__ import annotations

import hashlib
import json
import random
import re

from app.core.enums import Channel, NextBestAction
from app.llm.base import LLMProvider, LLMResponse

#: What every generated SMS has to end with. Matches the wording the default
#: templates use, so drafted and templated copy read the same way.
SMS_OPT_OUT = "Reply STOP to opt out."

MODEL_NAME = "gimme-mock-writer-1"


class MockLLMProvider(LLMProvider):
    name = "mock"
    model = MODEL_NAME

    def complete(self, system_prompt: str, user_prompt: str, *, max_tokens: int = 900) -> LLMResponse:
        ctx = _parse_context(user_prompt)
        rng = random.Random(_seed(user_prompt))
        channel = Channel(ctx.get("channel", Channel.EMAIL.value))
        action = ctx.get("recommended_action") or NextBestAction.REORDER_REMINDER.value

        subject, body = _compose(ctx, channel, action, rng)
        payload = json.dumps({"subject": subject, "body": body})
        return LLMResponse(text=payload, provider=self.name, model=self.model, is_mock=True)

    def health(self) -> dict:
        return {
            "provider": self.name,
            "model": self.model,
            "status": "OK",
            "mode": "mock",
            "message": (
                "Mock LLM active. Messages are generated locally from verified customer "
                "context — no external API is called."
            ),
        }


# --------------------------------------------------------------------------
# Context recovery
# --------------------------------------------------------------------------
def _seed(text: str) -> int:
    return int(hashlib.sha256(text.encode()).hexdigest()[:12], 16)


def _json_block(prompt: str, header: str) -> dict:
    """Pull one labelled JSON block back out of the assembled user prompt."""
    idx = prompt.find(header)
    if idx == -1:
        return {}
    start = prompt.find("{", idx)
    if start == -1:
        return {}
    depth = 0
    for i in range(start, len(prompt)):
        if prompt[i] == "{":
            depth += 1
        elif prompt[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(prompt[start : i + 1])
                except json.JSONDecodeError:
                    return {}
    return {}


def _parse_context(prompt: str) -> dict:
    facts = _json_block(prompt, "CUSTOMER FACTS")
    intel = _json_block(prompt, "CUSTOMER INTELLIGENCE")
    marketing = _json_block(prompt, "VERIFIED PROMOTIONS")

    channel_match = re.search(r"^Channel:\s*(\w+)", prompt, re.MULTILINE)
    objective_match = re.search(r"^Campaign objective:\s*(.+)$", prompt, re.MULTILINE)
    responsible = re.search(
        r"responsible drinking statement on its own line:\s*\"(.+?)\"", prompt, re.DOTALL
    )
    age = re.search(r"age statement on its own line:\s*\"(.+?)\"", prompt, re.DOTALL)
    signature = re.search(r"Sign off with:\s*\"(.+?)\"", prompt, re.DOTALL)
    closing = re.search(r"Close with:\s*\"(.+?)\"", prompt, re.DOTALL)
    variation = re.search(r"^Variation instruction:\s*(.+)$", prompt, re.MULTILINE)

    return {
        "first_name": facts.get("first_name") or "there",
        "city": facts.get("city"),
        "lifecycle_stage": facts.get("lifecycle_stage", ""),
        "completed_orders": facts.get("completed_orders", 0),
        "days_since_last_order": facts.get("days_since_last_order"),
        "usual_days_between_orders": facts.get("usual_days_between_orders"),
        "preferred_categories": facts.get("preferred_categories") or [],
        "preferred_brands": facts.get("preferred_brands") or [],
        "products": [p for p in (facts.get("products_they_actually_bought") or []) if p],
        "typical_order_day": facts.get("typical_order_day"),
        "recommended_action": intel.get("recommended_action", ""),
        "churn_risk_band": intel.get("churn_risk_band", ""),
        "promotions": marketing.get("promotions") or [],
        "coupon_codes": marketing.get("coupon_codes") or [],
        "delivery_promise": marketing.get("delivery_promise") or "",
        "website": marketing.get("website") or "",
        "customer_service_email": marketing.get("customer_service_email") or "",
        "channel": (channel_match.group(1) if channel_match else Channel.EMAIL.value),
        "objective": (objective_match.group(1).strip() if objective_match else ""),
        "responsible_statement": (responsible.group(1).strip() if responsible else ""),
        "age_statement": (age.group(1).strip() if age else ""),
        "signature": (signature.group(1).strip() if signature else ""),
        "whatsapp_closing": (closing.group(1).strip() if closing else ""),
        "variation": (variation.group(1).strip() if variation else ""),
    }


# --------------------------------------------------------------------------
# Copy composition
# --------------------------------------------------------------------------
SUBJECTS: dict[str, list[str]] = {
    NextBestAction.WELCOME.value: [
        "Welcome to GIMME, {name}",
        "{name}, you're all set with GIMME",
    ],
    NextBestAction.ENCOURAGE_SECOND_ORDER.value: [
        "{name}, ready for round two?",
        "Your next GIMME order, {name}",
    ],
    NextBestAction.REORDER_REMINDER.value: [
        "{name}, time to restock?",
        "Your usual, whenever you're ready",
    ],
    NextBestAction.PERSONALIZED_RECOMMENDATION.value: [
        "Picked for you, {name}",
        "{name}, something you might like",
    ],
    NextBestAction.CATEGORY_MESSAGE.value: [
        "New in {category}, {name}",
        "{name}, worth a look in {category}",
    ],
    NextBestAction.VIP_APPRECIATION.value: [
        "Thank you, {name}",
        "{name}, you're one of our best",
    ],
    NextBestAction.LOYALTY_RECOGNITION.value: [
        "{name}, thanks for sticking with us",
        "We appreciate you, {name}",
    ],
    NextBestAction.REACTIVATION.value: [
        "{name}, we've missed you",
        "It's been a while, {name}",
    ],
    NextBestAction.WIN_BACK.value: [
        "{name}, still here whenever you need us",
        "One from us, {name}",
    ],
    NextBestAction.REQUEST_FEEDBACK.value: [
        "{name}, how are we doing?",
        "Two minutes, {name}?",
    ],
}

OPENINGS: dict[str, str] = {
    NextBestAction.WELCOME.value: (
        "Welcome aboard. GIMME delivers drinks to your door, and we've kept things simple: "
        "browse, order, done."
    ),
    NextBestAction.ENCOURAGE_SECOND_ORDER.value: (
        "You ordered with us {days} days ago and we hope it landed well. If you're planning "
        "anything this week, we're here."
    ),
    NextBestAction.REORDER_REMINDER.value: (
        "It's been {days} days since your last order — right about when you usually restock."
    ),
    NextBestAction.PERSONALIZED_RECOMMENDATION.value: (
        "Based on what you've ordered before, we thought a couple of things might be worth "
        "your time."
    ),
    NextBestAction.CATEGORY_MESSAGE.value: (
        "You know your way around {category}, so we thought you'd want to know what's on the "
        "shelf."
    ),
    NextBestAction.VIP_APPRECIATION.value: (
        "You've ordered with us {orders} times now. That means a lot to a small team, and we "
        "wanted to say so properly."
    ),
    NextBestAction.LOYALTY_RECOGNITION.value: (
        "{orders} orders in and still going. Thanks for making GIMME part of your routine."
    ),
    NextBestAction.REACTIVATION.value: (
        "It's been {days} days since we last saw an order from you. No pressure at all — just "
        "letting you know we're still here."
    ),
    NextBestAction.WIN_BACK.value: (
        "It's been {days} days, so we'll keep this short. If GIMME stopped being useful, "
        "we'd genuinely like to know."
    ),
    NextBestAction.REQUEST_FEEDBACK.value: (
        "You've ordered with us a few times now, so your opinion is worth more than most. "
        "How have we been doing?"
    ),
    NextBestAction.NO_ACTION.value: "Just checking in from the GIMME team.",
    NextBestAction.SUPPRESS_COMMUNICATION.value: "Just checking in from the GIMME team.",
}

CLOSERS = [
    "Order whenever suits — we'll take it from there.",
    "Everything's in your account when you're ready.",
    "No rush. We're here when you need us.",
]


def _compose(ctx: dict, channel: Channel, action: str, rng: random.Random) -> tuple[str, str]:
    name = str(ctx["first_name"]).split(" ")[0] or "there"
    days = ctx.get("days_since_last_order")
    orders = ctx.get("completed_orders", 0)
    category = (ctx["preferred_categories"] or ["drinks"])[0]
    products = ctx["products"][:2]

    subject_pool = SUBJECTS.get(action, SUBJECTS[NextBestAction.REORDER_REMINDER.value])
    subject = rng.choice(subject_pool).format(name=name, category=category)

    opening = OPENINGS.get(action, OPENINGS[NextBestAction.NO_ACTION.value]).format(
        days=days if days is not None else "a few",
        orders=orders,
        category=category,
    )

    lines: list[str] = [f"Hi {name},", "", opening]

    # Only ever name products the customer actually bought.
    if products and action != NextBestAction.WELCOME.value:
        if len(products) == 1:
            lines += ["", f"Your {products[0]} is one tap away in your order history."]
        else:
            lines += [
                "",
                f"Your regulars — {products[0]} and {products[1]} — are still in your order "
                "history, ready to reorder.",
            ]

    # Only ever mention a promotion that was supplied as verified.
    promotions = ctx.get("promotions") or []
    coupons = ctx.get("coupon_codes") or []
    if promotions and action in {
        NextBestAction.WIN_BACK.value,
        NextBestAction.REACTIVATION.value,
        NextBestAction.ENCOURAGE_SECOND_ORDER.value,
    }:
        offer = promotions[0]
        if coupons:
            lines += ["", f"{offer} — use code {coupons[0]} at checkout."]
        else:
            lines += ["", offer]

    if ctx.get("delivery_promise") and action != NextBestAction.REQUEST_FEEDBACK.value:
        lines += ["", ctx["delivery_promise"] + "."]

    if action == NextBestAction.REQUEST_FEEDBACK.value:
        lines += ["", "Just hit reply — a sentence is plenty."]
    else:
        lines += ["", rng.choice(CLOSERS)]

    body = "\n".join(lines)

    if channel == Channel.EMAIL:
        if ctx.get("signature"):
            body += f"\n\n{ctx['signature']}"
        if ctx.get("responsible_statement"):
            body += f"\n\n{ctx['responsible_statement']}"
        if ctx.get("age_statement"):
            body += f"\n{ctx['age_statement']}"
        body = _apply_variation(body, ctx.get("variation", ""))
        return subject, body

    if channel == Channel.SMS:
        # The opt-out is not decoration: a commercial SMS without one is
        # blocked at the compliance gate, so the mock has to produce copy that
        # could really be sent. Reserve its room before trimming to the limit.
        sms = _short_form(name, opening, products, ctx, limit=300 - len(SMS_OPT_OUT) - 1)
        return "", f"{_apply_variation(sms, ctx.get('variation', ''))} {SMS_OPT_OUT}"

    if channel == Channel.WHATSAPP:
        wa = _short_form(name, opening, products, ctx, limit=520)
        if ctx.get("whatsapp_closing"):
            wa += f"\n\n{ctx['whatsapp_closing']}"
        return "", _apply_variation(wa, ctx.get("variation", ""))

    # PUSH
    title = subject[:44]
    push_body = opening[:130]
    return title, push_body


def _short_form(name: str, opening: str, products: list[str], ctx: dict, *, limit: int) -> str:
    parts = [f"Hi {name}, {opening[0].lower()}{opening[1:]}"]
    if products:
        parts.append(f"Your {products[0]} is ready to reorder.")
    promotions = ctx.get("promotions") or []
    coupons = ctx.get("coupon_codes") or []
    if promotions and coupons:
        parts.append(f"{promotions[0]} with code {coupons[0]}.")
    elif promotions:
        parts.append(promotions[0] + ".")
    text = " ".join(parts)
    if len(text) > limit:
        text = text[: limit - 1].rsplit(" ", 1)[0] + "."
    return text


def _apply_variation(body: str, variation: str) -> str:
    """Apply the requested tone adjustment deterministically."""
    v = variation.lower()
    if "shorter" in v:
        blocks = [b for b in body.split("\n\n") if b.strip()]
        if len(blocks) > 3:
            # Keep the greeting, the first substantive paragraph, and the tail
            # (signature / statements), dropping the middle.
            body = "\n\n".join(blocks[:2] + blocks[-2:])
    if "premium" in v:
        body = body.replace("!", ".")
    if "salesy" in v or "sales language" in v:
        for phrase in (" — use code", "use code"):
            if phrase in body:
                body = re.sub(r"\n\n[^\n]*use code[^\n]*", "", body)
                break
    if "playful" in v and not body.rstrip().endswith("Cheers!"):
        body = body.replace("No rush. We're here when you need us.", "No rush — we're not going anywhere.")
    return body
