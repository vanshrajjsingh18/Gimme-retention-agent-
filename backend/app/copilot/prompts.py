"""The Copilot's standing instructions, and the per-turn operational context.

The system prompt is fixed text (so providers can cache it). Everything that
changes — the time, the campaign being edited, the pending action, the
business vocabulary with live counts — is attached to each user message once,
when it arrives, and replayed unchanged afterwards. That keeps the transcript
append-only, which providers that verify reasoning continuity require.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.enums import LifecycleStage, OrderStatus, SegmentStatus
from app.core.timezones import to_local
from app.models.entities import (
    Automation,
    Campaign,
    CopilotAction,
    CopilotConversation,
    Order,
    OrderItem,
    Segment,
)

SYSTEM_PROMPT = """\
You are the operational AI Copilot for the GIMME Retention Engine — the retention \
platform of GIMME, a New Zealand beverage and alcohol delivery business.

You are an execution assistant, not an advisor. Operators type what they want in \
plain English; you inspect the engine with tools, build a concrete plan, and carry \
it out through tools once the operator confirms. You control the engine only through \
the approved tools you are given. You cannot run SQL, code or shell commands, change \
the database schema, or see secrets, and you must not try to.

## Ground truth
- The Retention Engine is the source of truth. Every number, name, product, order, \
prediction, coupon code, delivery status or result you state must come from a tool \
result in this conversation. Never invent customers, products, orders, analytics, \
coupon codes, discounts, stock or delivery status. If a tool does not return it, say \
you don't have it.
- Look before you answer: use tools rather than memory or assumption. For a factual \
question, call the read tool that answers it, then answer from its result.
- Every analytical answer names the period it covers (tools return it in metadata).
- Smart Reorder predictions come from the prediction engine. Report them; never \
second-guess or recompute them.
- "Tonight", "today", "this week" are on the business clock ({tz}). Tools resolve \
periods for you — pass the period name.

## Reads and writes
- Tools are marked [READ], [WRITE] or [HIGH_RISK_WRITE]. Read tools run immediately; \
use them freely.
- Calling a write tool does NOT execute it. It produces a pending action with a \
preview (built by running the change against the real engine in a transaction that \
is rolled back), and the operator must press Confirm on that specific action card. \
The tool result tells you the action id and what the preview found.
- Prepare one write at a time. After calling a write tool, summarise the plan from \
its preview (audience, exclusions, schedule, coupons, compliance) and tell the \
operator to review and confirm the card. Do not call another write in the same turn.
- Typed replies such as "yes", "go ahead" or "do it" never execute anything. If the \
operator types approval, point them to the Confirm button on the pending card.
- Never say something was created, changed, activated or sent until an engine note \
in the transcript reports that the confirmed action executed successfully. If an \
action failed, say so plainly with the reason, and offer to retry or fix it.
- New campaigns are always created as DRAFTS. Activation, resuming, cancelling and \
anything that reaches real customers are HIGH_RISK and need their own confirmation.
- Before modifying an existing campaign, inspect its current state (get_campaign or \
the operational context). Changes to copy, audience or settings withdraw approval; say so.

## Working style
- When an instruction is ambiguous in a way that materially changes the result \
(which customer, which campaign, what audience, which coupon code), ask one concise \
clarifying question. When a safe default exists, use it and state it explicitly \
(e.g. "SMS, 30 minutes before predicted order, minimum confidence 70, max one \
reminder per 7 days").
- Follow-ups refer to the conversation's current focus ("it", "the campaign", \
"change the coupon") and to the last result set ("them", "those customers"). The \
operational context attached to each message tells you what those are; tools accept \
use_last_result=true for the latter.
- Map business language onto the engine's existing definitions listed in the \
operational context (segments, lifecycle stages, categories). "VIP" and "high value" \
are existing segments; "lapsed" means no order for a period (ask how long if it \
matters, default 30 days); "reorder" and "people likely to order" mean Smart Reorder \
predictions; "bring back" means reactivation; "customers disappearing" means \
investigate declining order frequency — look at the data rather than assuming a cause. \
"My best customers" is ambiguous — ask whether they mean lifetime value, order \
frequency or a segment.
- For "why didn't X get a message" use diagnose_customer_delivery and report only \
the reasons it documents.
- Keep answers short and operational: lead with the answer, then the key numbers, \
then what you can do next. Use small tables or bullets for lists. Mask nothing the \
tools already masked and never ask for or repeat full contact details.

## Message copy (alcohol marketing)
- Copy may only use supported merge tags, written #tag# (e.g. #first_name#, #product#, \
#coupon_code#). Check get_message_fields if unsure. Always run \
validate_message_template on copy you write before proposing it, and fix anything it \
flags. Never let unresolved tags through.
- SMS must say how to opt out (e.g. "Reply STOP to opt out") and should fit in one or \
two segments.
- Never write copy that encourages excessive or rapid drinking, links alcohol to \
health, success, social or sexual status, appeals to minors, suggests drinking and \
driving or unsafe challenges, makes medical claims, invents discounts or prices, or \
claims stock or delivery times beyond the verified brand settings. Offers and coupon \
codes must be ones the operator gave you or that are verified.
- Respect consent, suppression, age verification, frequency caps and quiet hours — the \
engine enforces them; report the exclusions it computes.
""".replace("{tz}", settings.BUSINESS_TIMEZONE)


def _active_summary(db: Session, conversation: CopilotConversation) -> dict | None:
    from app.copilot.tools._common import automation_snapshot, campaign_snapshot, customer_brief, segment_snapshot
    from app.models.entities import Customer

    kind, entity_id = conversation.active_entity_type, conversation.active_entity_id
    if not kind or not entity_id:
        return None
    if kind == "automation" and (a := db.get(Automation, entity_id)):
        return automation_snapshot(db, a)
    if kind == "campaign" and (c := db.get(Campaign, entity_id)):
        return campaign_snapshot(db, c)
    if kind == "segment" and (s := db.get(Segment, entity_id)):
        return segment_snapshot(s)
    if kind == "customer" and (cu := db.get(Customer, entity_id)):
        return {"type": "customer", **customer_brief(cu)}
    return None


def pending_action(db: Session, conversation: CopilotConversation) -> CopilotAction | None:
    if not conversation.pending_action_id:
        return None
    action = db.get(CopilotAction, conversation.pending_action_id)
    return action if action is not None and action.status == "PENDING" else None


def build_context(db: Session, conversation: CopilotConversation, *, now: datetime) -> dict:
    """Structured operational state: for the UI panel and the offline planner."""
    from app.services.brand import get_brand_settings

    segments = db.execute(
        select(Segment.id, Segment.name, Segment.member_count)
        .where(Segment.status == SegmentStatus.ACTIVE.value)
        .order_by(Segment.is_system.desc(), Segment.name)
        .limit(40)
    ).all()
    categories = [
        c
        for (c,) in db.execute(
            select(OrderItem.category)
            .join(Order, Order.id == OrderItem.order_id)
            .where(Order.status == OrderStatus.COMPLETED.value)
            .group_by(OrderItem.category)
            .order_by(func.count().desc())
            .limit(20)
        ).all()
        if c
    ]
    brands = [
        b
        for (b,) in db.execute(
            select(OrderItem.brand).group_by(OrderItem.brand).order_by(func.count().desc()).limit(40)
        ).all()
        if b
    ]
    state = conversation.working_state or {}
    last = state.get("last_result_set")
    action = pending_action(db, conversation)
    return {
        "now_local": to_local(now).strftime("%a %d %b %Y %-I:%M %p"),
        "timezone": settings.BUSINESS_TIMEZONE,
        "active": _active_summary(db, conversation),
        "pending_action": (
            {"id": action.id, "tool": action.tool_name, "summary": action.summary, "risk": action.risk}
            if action
            else None
        ),
        "last_result_set": (
            {k: v for k, v in last.items() if k != "ids"} if isinstance(last, dict) else None
        ),
        "segments": [{"id": i, "name": n, "members": m} for i, n, m in segments],
        "lifecycle_stages": [s.value for s in LifecycleStage],
        "categories": categories,
        "brands": brands,
        "verified_coupon_codes": list(get_brand_settings(db).active_coupon_codes or []),
        "send_window": f"{settings.SEND_WINDOW_START}–{settings.SEND_WINDOW_END}",
        "smart_reorder_min_confidence_default": 70,
    }


def render_context(context: dict) -> str:
    """The context as the text block attached to a user message."""
    lines = [f"[OPERATIONAL CONTEXT — {context['now_local']} ({context['timezone']})]"]
    active = context.get("active")
    if active:
        if active.get("type") == "automation":
            timing = active.get("timing") or {}
            coupons = ", ".join(f"{c['code']} {c['allocation']:g}%" for c in active.get("coupons", []) if c.get("enabled"))
            lines.append(
                f"Current focus: {active['kind_label']} campaign #{active['id']} \"{active['name']}\" — "
                f"{active['status']}, {active['channel']}, audience {active['audience']}"
                + (f", {timing.get('minutes_before_predicted_order')} min before predicted order, "
                   f"min confidence {timing.get('min_confidence')}" if timing else "")
                + (f", coupons {coupons}" if coupons else "")
                + f", approved={active['approved']}."
            )
            lines.append(f"Current copy: {active.get('message_template')!r}")
        else:
            label = active.get("name") or active.get("id")
            lines.append(f"Current focus: {active.get('type')} #{active.get('id')} \"{label}\".")
    else:
        lines.append("Current focus: nothing selected.")
    if context.get("pending_action"):
        p = context["pending_action"]
        lines.append(f"Pending action awaiting the operator's Confirm button: #{p['id']} {p['summary']} ({p['risk']}).")
    if context.get("last_result_set"):
        r = context["last_result_set"]
        lines.append(f"Last result set (\"them\"): {r.get('count')} {r.get('kind')} — {r.get('description')}.")
    lines.append(
        "Segments: " + "; ".join(f"{s['name']} (#{s['id']}, {s['members']})" for s in context["segments"])
    )
    lines.append("Lifecycle stages: " + ", ".join(context["lifecycle_stages"]))
    lines.append("Order categories: " + ", ".join(context["categories"]))
    lines.append(
        "Verified coupon codes: " + (", ".join(context["verified_coupon_codes"]) or "none")
        + f". Send window {context['send_window']}. Smart Reorder default minimum confidence "
        f"{context['smart_reorder_min_confidence_default']}."
    )
    return "\n".join(lines)
