"""Helpers shared by the Copilot tools: finding things, describing them, time."""
from __future__ import annotations

from datetime import datetime, time, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.copilot.registry import ToolContext, ToolError
from app.core.timezones import combine_local, local_date, to_local
from app.models.entities import (
    Automation,
    Campaign,
    CouponVariant,
    Customer,
    CustomerMetrics,
    Segment,
)

#: "Tonight" in the business's own words: from 5pm local until midnight.
EVENING_START = time(17, 0)

WINDOWS = (
    "today",
    "tonight",
    "tomorrow",
    "yesterday",
    "next_24h",
    "next_7_days",
    "last_7_days",
    "last_week",
    "last_30_days",
    "last_90_days",
    "this_week",
)


def resolve_window(name: str, now: datetime) -> tuple[datetime, datetime, str]:
    """A named period as naive-UTC bounds plus a human label, on the business clock."""
    today = local_date(now)
    day_start = combine_local(today, time.min)
    if name == "today":
        return day_start, day_start + timedelta(days=1), f"today ({today:%a %d %b})"
    if name == "tonight":
        start = max(now, combine_local(today, EVENING_START))
        return start, day_start + timedelta(days=1), f"tonight ({today:%a %d %b}, 5pm–midnight)"
    if name == "tomorrow":
        start = day_start + timedelta(days=1)
        return start, start + timedelta(days=1), f"tomorrow ({today + timedelta(days=1):%a %d %b})"
    if name == "yesterday":
        return day_start - timedelta(days=1), day_start, f"yesterday ({today - timedelta(days=1):%a %d %b})"
    if name == "next_24h":
        return now, now + timedelta(hours=24), "the next 24 hours"
    if name == "next_7_days":
        return now, now + timedelta(days=7), "the next 7 days"
    if name == "last_7_days":
        return now - timedelta(days=7), now, "the last 7 days"
    if name == "last_week":
        monday = today - timedelta(days=today.weekday())
        start = combine_local(monday - timedelta(days=7), time.min)
        return start, combine_local(monday, time.min), (
            f"last week ({monday - timedelta(days=7):%d %b}–{monday - timedelta(days=1):%d %b})"
        )
    if name == "this_week":
        monday = today - timedelta(days=today.weekday())
        return combine_local(monday, time.min), now, f"this week (since Mon {monday:%d %b})"
    if name == "last_30_days":
        return now - timedelta(days=30), now, "the last 30 days"
    if name == "last_90_days":
        return now - timedelta(days=90), now, "the last 90 days"
    raise ToolError(f"Unknown period '{name}'. Use one of: {', '.join(WINDOWS)}.")


def local_label(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    return to_local(moment).strftime("%a %d %b %-I:%M %p")


def money(value: float | None) -> float:
    return round(float(value or 0.0), 2)


# --------------------------------------------------------------------------
# Finding things
# --------------------------------------------------------------------------
def find_customer(db: Session, ref: str | int) -> Customer:
    """A customer by id, external id, email, phone or name. Never a guess.

    An ambiguous name is an error that lists the candidates, so the model
    asks which one rather than picking the first Sarah it finds.
    """
    if isinstance(ref, int) or (isinstance(ref, str) and ref.strip().isdigit()):
        customer = db.get(Customer, int(ref))
        if customer:
            return customer
    text = str(ref).strip()
    if not text:
        raise ToolError("Which customer? Give a name, email, phone or id.")
    exact = db.execute(
        select(Customer).where(
            or_(
                Customer.external_id == text,
                func.lower(Customer.email) == text.lower(),
                Customer.phone == text,
            )
        )
    ).scalars().first()
    if exact:
        return exact

    parts = text.lower().split()
    query = select(Customer)
    if len(parts) >= 2:
        query = query.where(
            func.lower(Customer.first_name) == parts[0],
            func.lower(Customer.last_name).startswith(" ".join(parts[1:])),
        )
    else:
        query = query.where(
            or_(func.lower(Customer.first_name) == parts[0], func.lower(Customer.last_name) == parts[0])
        )
    matches = db.execute(query.order_by(Customer.id).limit(6)).scalars().all()
    if not matches:
        raise ToolError(f"No customer matches '{text}'.")
    if len(matches) > 1:
        names = "; ".join(f"#{c.id} {c.full_name} ({c.city or 'no city'})" for c in matches[:5])
        raise ToolError(
            f"'{text}' matches {len(matches)}{'+' if len(matches) == 6 else ''} customers: {names}. "
            "Ask the operator which one, or pass the customer id."
        )
    return matches[0]


def automation_for(ctx: ToolContext, automation_id: int | None, *, kind: str | None = None) -> Automation:
    """The named automation, or the one this conversation is working on."""
    if automation_id is None:
        entity_type, entity_id = ctx.active
        if entity_type == "automation" and entity_id:
            automation_id = entity_id
    if automation_id is None:
        raise ToolError(
            "No campaign is selected in this conversation. Name one, or list campaigns first."
        )
    automation = ctx.db.get(Automation, int(automation_id))
    if automation is None:
        raise ToolError(f"Automation {automation_id} does not exist.")
    if kind and automation.kind != kind:
        raise ToolError(
            f"'{automation.name}' is a {automation.kind} automation, not a "
            f"{'Smart Reorder' if kind == 'NUDGE' else kind} campaign."
        )
    return automation


def campaign_for(ctx: ToolContext, campaign_id: int | None) -> Campaign:
    if campaign_id is None:
        entity_type, entity_id = ctx.active
        if entity_type == "campaign" and entity_id:
            campaign_id = entity_id
    if campaign_id is None:
        raise ToolError("No one-off campaign is selected. Name one, or list campaigns first.")
    campaign = ctx.db.get(Campaign, int(campaign_id))
    if campaign is None:
        raise ToolError(f"Campaign {campaign_id} does not exist.")
    return campaign


def segment_for(ctx: ToolContext, segment_id: int | None, name: str | None = None) -> Segment:
    if segment_id is None and name:
        segment = ctx.db.execute(
            select(Segment).where(func.lower(Segment.name) == name.strip().lower())
        ).scalars().first()
        if segment is None:
            candidates = ctx.db.execute(
                select(Segment.name).where(Segment.name.ilike(f"%{name.strip()}%")).limit(5)
            ).scalars().all()
            hint = f" Similar: {', '.join(candidates)}." if candidates else ""
            raise ToolError(f"No segment is named '{name}'.{hint}")
        return segment
    if segment_id is None:
        entity_type, entity_id = ctx.active
        if entity_type == "segment" and entity_id:
            segment_id = entity_id
    if segment_id is None:
        raise ToolError("No segment is selected. Name one, or list segments first.")
    segment = ctx.db.get(Segment, int(segment_id))
    if segment is None:
        raise ToolError(f"Segment {segment_id} does not exist.")
    return segment


# --------------------------------------------------------------------------
# Describing things — also the before/after states on execution receipts
# --------------------------------------------------------------------------
def coupon_rows(db: Session, automation_id: int) -> list[CouponVariant]:
    return list(
        db.execute(
            select(CouponVariant)
            .where(CouponVariant.automation_id == automation_id)
            .order_by(CouponVariant.position, CouponVariant.id)
        ).scalars()
    )


def automation_snapshot(db: Session, automation: Automation) -> dict:
    from app.automations import nudge

    segment = db.get(Segment, automation.segment_id) if automation.segment_id else None
    cfg = nudge.config_of(automation) if automation.kind == "NUDGE" else dict(automation.config or {})
    snapshot = {
        "id": automation.id,
        "type": "automation",
        "name": automation.name,
        "kind": automation.kind,
        "kind_label": {"NUDGE": "Smart Reorder", "SEQUENCE": "Sequence", "COHORT_BULK": "Bulk"}.get(
            automation.kind, automation.kind
        ),
        "status": automation.status,
        "channel": automation.channel,
        "objective": automation.objective,
        "audience": (
            {
                "segment_id": segment.id,
                "segment": segment.name,
                "members": segment.member_count,
                **(
                    {"cohort_query": segment.rule_definition["cohort_query"]}
                    if (segment.rule_definition or {}).get("cohort_query")
                    else {}
                ),
            }
            if segment
            else {"manual_customers": len(automation.manual_customer_ids or [])}
        ),
        "message_template": automation.message_template,
        "approved": automation.approved_at is not None,
        "require_approval": automation.require_approval,
        "stop_on_order": automation.stop_on_order,
        "starts_at": automation.starts_at.isoformat() if automation.starts_at else None,
        "ends_at": automation.ends_at.isoformat() if automation.ends_at else None,
        "coupons": [
            {"code": v.coupon_code, "allocation": v.allocation_percentage, "enabled": v.enabled}
            for v in coupon_rows(db, automation.id)
        ],
    }
    if automation.kind == "NUDGE":
        from app.services.reorder_timing import offset_minutes_for

        snapshot["timing"] = {
            "minutes_before_predicted_order": offset_minutes_for(
                cfg["reminder_offset"], cfg["custom_offset_minutes"]
            ),
            "min_confidence": cfg["min_confidence"],
            "min_gap_days": cfg["min_gap_days"],
        }
        if cfg.get("touchpoints"):
            snapshot["touchpoints"] = cfg["touchpoints"]
        if cfg.get("frequency_rules"):
            snapshot["frequency_rules"] = cfg["frequency_rules"]
    return snapshot


def campaign_snapshot(db: Session, campaign: Campaign) -> dict:
    segment = db.get(Segment, campaign.segment_id) if campaign.segment_id else None
    return {
        "id": campaign.id,
        "type": "campaign",
        "name": campaign.name,
        "status": campaign.status,
        "channel": campaign.channel,
        "objective": campaign.objective,
        "segment": segment.name if segment else None,
        "segment_id": campaign.segment_id,
        "body": campaign.body,
        "subject": campaign.subject,
        "scheduled_at": campaign.scheduled_at.isoformat() if campaign.scheduled_at else None,
        "approved": campaign.approved_at is not None,
        "messages_sent": campaign.messages_sent,
        "conversions": campaign.conversions,
        "attributed_revenue": money(campaign.attributed_revenue),
    }


def segment_snapshot(segment: Segment) -> dict:
    from app.services.purchase_cohorts import is_cohort_segment

    return {
        "id": segment.id,
        "type": "segment",
        "name": segment.name,
        "description": segment.description,
        "segment_type": "COHORT" if is_cohort_segment(segment) else segment.segment_type,
        "status": segment.status,
        "is_system": segment.is_system,
        "members": segment.member_count,
        "rule": segment.rule_definition,
        "last_evaluated_at": segment.last_evaluated_at.isoformat() if segment.last_evaluated_at else None,
    }


def customer_brief(customer: Customer, metrics: CustomerMetrics | None = None) -> dict:
    metrics = metrics if metrics is not None else customer.metrics
    return {
        "id": customer.id,
        "name": customer.full_name or f"Customer {customer.id}",
        "city": customer.city,
        "lifecycle_stage": customer.lifecycle_stage,
        "total_orders": metrics.completed_orders if metrics else 0,
        "lifetime_revenue": money(metrics.lifetime_revenue if metrics else 0),
        "average_order_value": money(metrics.average_order_value if metrics else 0),
        "days_since_last_order": metrics.days_since_last_order if metrics else None,
        "last_order_product": (metrics.last_order_product if metrics else "") or None,
        "preferred_categories": (metrics.preferred_categories or [])[:3] if metrics else [],
        "marketing_consent": customer.marketing_consent,
        "is_suppressed": customer.is_suppressed,
    }


def result_set(kind: str, ids: list[int], description: str) -> dict:
    """What "them" refers to in the next message. Capped, and says so."""
    cap = 5000
    return {
        "kind": kind,
        "ids": ids[:cap],
        "count": len(ids),
        "truncated": len(ids) > cap,
        "description": description,
    }
