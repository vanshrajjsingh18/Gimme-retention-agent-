"""Smart Reorder: predictions, dry runs, and campaign configuration.

The prediction engine and the reminder queue remain the source of truth. These
tools read what they computed, and change campaign *configuration* through
the same automation service the UI uses — they never decide who is predicted
to order, or when.
"""
from __future__ import annotations

import re
from datetime import datetime

from sqlalchemy import func, select

from app.copilot.registry import Risk, ToolContext, ToolError, ToolResult, tool
from app.copilot.tools._common import (
    WINDOWS,
    automation_for,
    automation_snapshot,
    coupon_rows,
    customer_brief,
    local_label,
    resolve_window,
    result_set,
    segment_for,
)
from app.copilot.tools.messaging import validate_copy
from app.copilot.tools.segments import COHORT_PROPS
from app.core.enums import (
    AutomationKind,
    AutomationStatus,
    CampaignObjective,
    CancellationReason,
    Channel,
    RecipientStatus,
)
from app.models.entities import (
    AuditLog,
    Automation,
    CouponVariant,
    Customer,
    CustomerCouponAssignment,
    CustomerMetrics,
)

GROUP = "smart_reorder"
NUDGE = AutomationKind.NUDGE.value

#: Matches the "Smart Reorder Eligible" segment and the dashboard headline.
DEFAULT_MIN_CONFIDENCE = 70
DEFAULT_MINUTES_BEFORE = 30

EXCLUSION_LABELS = {
    "NO_PATTERN": "Not enough order history for a prediction",
    "LOW_CONFIDENCE": "Prediction confidence below the minimum",
    "BEYOND_HORIZON": "Next predicted order is more than 14 days away",
    "ALREADY_ORDERED": "Already ordered this cycle",
    "NOT_A_TEST_RECIPIENT": "Campaign is in testing; not a test recipient",
    "CUSTOMER_MISSING": "Customer record missing",
    RecipientStatus.EXCLUDED_NO_CONSENT.value: "No consent for this channel",
    RecipientStatus.EXCLUDED_SUPPRESSED.value: "Suppressed / opted out",
    RecipientStatus.EXCLUDED_FREQUENCY_CAP.value: "Frequency cap reached",
    RecipientStatus.EXCLUDED_AGE.value: "Age not verified",
    RecipientStatus.EXCLUDED_MISSING_CONTACT.value: "No contact details for this channel",
    RecipientStatus.EXCLUDED_QUIET_HOURS.value: "Inside quiet hours",
}

COUPONS_PROP = {
    "type": "array",
    "description": (
        "Coupon variants: [{\"code\": \"FIRST7\", \"allocation\": 40}, ...]. Allocations must sum "
        "to 100; omit allocation on every entry to split equally. Only use codes the operator "
        "gave you or that are already verified — never invent one."
    ),
    "items": {
        "type": "object",
        "properties": {"code": {"type": "string"}, "allocation": {"type": "number"}},
        "required": ["code"],
    },
}

AUDIENCE_PROPS = {
    "segment_id": {"type": "integer", "description": "Target an existing segment."},
    "segment_name": {"type": "string"},
    "cohort": {
        "type": "object",
        "description": "Purchase-history audience, saved as a cohort segment. Keys: "
        + ", ".join(COHORT_PROPS),
    },
    "use_last_result": {"type": "boolean", "description": "Target the customer list from the last search."},
}


# --------------------------------------------------------------------------
# Shared pieces
# --------------------------------------------------------------------------
def normalise_coupons(ctx: ToolContext, coupons: list[dict], *, automation_id: int | None = None) -> list[dict]:
    """Validate codes and allocations. Codes must come from the operator or be verified."""
    from app.services.brand import get_brand_settings

    if not coupons:
        return []
    seen: set[str] = set()
    cleaned = []
    for entry in coupons:
        code = str(entry.get("code", "")).strip().upper()
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9_-]{1,49}", code):
            raise ToolError(f"'{entry.get('code')}' is not a valid coupon code.")
        if code in seen:
            raise ToolError(f"Coupon code {code} is listed twice.")
        seen.add(code)
        cleaned.append({"code": code, "allocation": entry.get("allocation")})

    given = [c["allocation"] for c in cleaned]
    if all(a is None for a in given):
        share = round(100.0 / len(cleaned), 2)
        for i, entry in enumerate(cleaned):
            entry["allocation"] = share if i < len(cleaned) - 1 else round(100.0 - share * (len(cleaned) - 1), 2)
    elif any(a is None for a in given):
        raise ToolError("Give an allocation for every coupon code, or for none (equal split).")
    total = round(sum(float(c["allocation"]) for c in cleaned), 2)
    if abs(total - 100.0) > 0.01:
        raise ToolError(f"Coupon allocations add up to {total}%, not 100%.")
    if any(float(c["allocation"]) <= 0 for c in cleaned):
        raise ToolError("Every coupon allocation must be above 0%.")

    verified = {c.upper() for c in (get_brand_settings(ctx.db).active_coupon_codes or [])}
    existing = {v.coupon_code.upper() for v in coupon_rows(ctx.db, automation_id)} if automation_id else set()
    said = ctx.user_text.upper()
    for entry in cleaned:
        code = entry["code"]
        entry["verified"] = code in verified
        if code in verified or code in existing:
            continue
        if not re.search(rf"(?<![A-Z0-9]){re.escape(code)}(?![A-Z0-9])", said):
            raise ToolError(
                f"Coupon code {code} was not given by the operator and is not a verified code. "
                "Ask which code to use — codes must never be invented."
            )
    return cleaned


def register_coupon_codes(ctx: ToolContext, codes: list[str]) -> list[str]:
    """Add operator-supplied codes to the verified list compliance checks against."""
    from app.services.brand import get_brand_settings

    brand = get_brand_settings(ctx.db)
    current = list(brand.active_coupon_codes or [])
    known = {c.upper() for c in current}
    added = [c for c in codes if c.upper() not in known]
    if added:
        brand.active_coupon_codes = current + added
        ctx.db.add(
            AuditLog(
                actor=ctx.user.email,
                action="BRAND_COUPON_CODES_ADDED",
                entity_type="brand",
                entity_id="1",
                detail={"codes": added, "via": "copilot"},
            )
        )
    return added


def replace_coupons(ctx: ToolContext, automation: Automation, coupons: list[dict]) -> None:
    for row in coupon_rows(ctx.db, automation.id):
        has_assignments = ctx.db.execute(
            select(CustomerCouponAssignment.id).where(CustomerCouponAssignment.variant_id == row.id).limit(1)
        ).first()
        if has_assignments:
            # Customers already promised this code keep it; it stops being
            # handed to anyone new.
            row.enabled = False
            row.allocation_percentage = 0.0
        else:
            ctx.db.delete(row)
    ctx.db.flush()
    for position, entry in enumerate(coupons):
        ctx.db.add(
            CouponVariant(
                automation_id=automation.id,
                position=position,
                coupon_code=entry["code"],
                allocation_percentage=float(entry["allocation"]),
                enabled=True,
            )
        )
    ctx.db.flush()


def resolve_audience_args(
    ctx: ToolContext, args: dict, *, name: str, current_segment_id: int | None = None
) -> tuple[int | None, list[int], str]:
    """(segment_id, manual_ids, description) from whichever audience form was given.

    A campaign's own cohort segment (``"<campaign> — audience"``) is edited in
    place rather than duplicated, so refining an audience does not leave a
    trail of near-identical segments behind.
    """
    from app.models.entities import Segment
    from app.services.purchase_cohorts import (
        CohortCriteria,
        CohortError,
        create_cohort_segment,
        is_cohort_segment,
        refresh_cohort_members,
    )

    forms = [k for k in ("segment_id", "segment_name", "cohort", "use_last_result") if args.get(k)]
    if len(forms) != 1:
        raise ToolError("Give exactly one audience: segment_id, segment_name, cohort, or use_last_result.")
    form = forms[0]
    if form in ("segment_id", "segment_name"):
        segment = segment_for(ctx, args.get("segment_id"), args.get("segment_name"))
        return segment.id, [], f"segment '{segment.name}'"
    if form == "cohort":
        try:
            criteria = CohortCriteria.from_dict(args["cohort"])
        except (CohortError, TypeError) as exc:
            raise ToolError(str(exc)) from exc
        current = ctx.db.get(Segment, current_segment_id) if current_segment_id else None
        if current is not None and is_cohort_segment(current) and current.name.endswith(" — audience"):
            current.rule_definition = {"cohort_query": criteria.as_dict()}
            current.description = criteria.describe()
            refresh_cohort_members(ctx.db, current, now=ctx.now)
            return current.id, [], criteria.describe()
        base_name, suffix = f"{name} — audience", 1
        segment_name = base_name
        while ctx.db.execute(select(Segment.id).where(Segment.name == segment_name)).first():
            suffix += 1
            segment_name = f"{base_name} ({suffix})"
        try:
            segment = create_cohort_segment(ctx.db, name=segment_name, criteria=criteria, now=ctx.now, commit=False)
        except CohortError as exc:
            raise ToolError(str(exc)) from exc
        return segment.id, [], criteria.describe()
    last = ctx.state.get("last_result_set") or {}
    if last.get("kind") != "customers" or not last.get("ids"):
        raise ToolError("There is no customer list in this conversation to target.")
    return None, list(last["ids"]), last.get("description", "the last customer list")


def dry_run_summary(ctx: ToolContext, automation: Automation, *, samples: int = 5) -> dict:
    """The engine's own dry run, plus the consent/suppression gate dispatch applies.

    Two passes because dispatch has two gates: the queue decides who has a
    prediction worth reminding about; the compliance recipient check decides
    who may be contacted on this channel at that moment.
    """
    from app.campaigns.service import build_recipient_view
    from app.compliance.engine import check_recipient
    from app.services.brand import build_compliance_config
    from app.services.coupon_assignment import get_or_assign_coupon
    from app.services.smart_reorder_queue import build_queue

    db = ctx.db
    report = build_queue(db, automation, now=ctx.now, dry_run=True)
    config = build_compliance_config(db)
    channel = Channel(automation.channel)
    excluded = {EXCLUSION_LABELS.get(k, k): v for k, v in report.excluded.items()}

    final: list[dict] = []
    for row in report.messages:
        customer = db.get(Customer, row["customer_id"])
        send_time = datetime.fromisoformat(row["scheduled_at"])
        status, _ = check_recipient(build_recipient_view(db, customer, now=send_time), channel, config, send_time=send_time)
        if status != RecipientStatus.ELIGIBLE:
            label = EXCLUSION_LABELS.get(status.value, status.value)
            excluded[label] = excluded.get(label, 0) + 1
            continue
        row = dict(row)
        row["coupon_code"] = row.get("coupon_code") or get_or_assign_coupon(
            db, customer.id, automation.id, persist=False
        )
        final.append(row)

    today_start, today_end, today_label = resolve_window("today", ctx.now)
    tonight_start, tonight_end, _ = resolve_window("tonight", ctx.now)

    def scheduled_between(start, end) -> int:
        return sum(1 for r in final if start <= datetime.fromisoformat(r["scheduled_at"]) < end)

    allocation: dict[str, int] = {}
    for row in final:
        if row.get("coupon_code"):
            allocation[row["coupon_code"]] = allocation.get(row["coupon_code"], 0) + 1

    snapshot = automation_snapshot(db, automation)
    touchpoints = len(snapshot.get("touchpoints") or []) or 1
    codes = [c["code"] for c in snapshot["coupons"] if c["enabled"]]
    check = validate_copy(ctx, automation.message_template or "", automation.channel, coupon_codes=codes)
    return {
        "campaign": {
            "id": automation.id,
            "name": automation.name,
            "status": automation.status,
            "channel": automation.channel,
            "audience": snapshot["audience"],
            "timing": snapshot.get("timing"),
        },
        "analysed": report.analysed,
        "eligible_before_consent": len(report.messages),
        "excluded": dict(sorted(excluded.items(), key=lambda kv: -kv[1])),
        "final_audience": len(final),
        "messages_today": scheduled_between(today_start, today_end),
        "messages_tonight": scheduled_between(tonight_start, tonight_end),
        "today_label": today_label,
        "horizon_days": 14,
        "estimated_touchpoints": len(final) * touchpoints,
        "coupon_allocation": allocation,
        "coupons_configured": snapshot["coupons"],
        "schedule_sample": [
            {
                "customer_id": r["customer_id"],
                "customer": r["customer_name"],
                "send_at_local": local_label(datetime.fromisoformat(r["scheduled_at"])),
                "predicted_order_local": local_label(datetime.fromisoformat(r["predicted_order_at"]))
                if r.get("predicted_order_at")
                else None,
                "minutes_before": (snapshot.get("timing") or {}).get("minutes_before_predicted_order"),
                "confidence": r.get("confidence"),
                "product": r.get("product"),
                "coupon_code": r.get("coupon_code"),
                "message": r.get("message"),
            }
            for r in sorted(final, key=lambda r: r["scheduled_at"])[:samples]
        ],
        "message_check": {
            "valid": check["valid"],
            "sendable_without_review": check["sendable_without_review"],
            "blocking": [f["message"] for f in check["blocking_findings"]],
            "needs_confirmation": [f["message"] for f in check["needs_confirmation_findings"]],
            "warnings": check["warnings"],
            "sms": check.get("sms"),
        },
        "_customer_ids": [r["customer_id"] for r in final],
    }


def _preview_after(ctx: ToolContext, args: dict, result: ToolResult) -> dict:
    """Run the dry run on the campaign the handler just created/changed in the sandbox."""
    target = result.target_id
    if target is None:
        return {}
    automation = ctx.db.get(Automation, int(target))
    if automation is None or automation.kind != NUDGE:
        return {}
    summary = dry_run_summary(ctx, automation)
    summary.pop("_customer_ids", None)
    return {"dry_run": summary}


# --------------------------------------------------------------------------
# Read tools
# --------------------------------------------------------------------------
@tool(
    "get_smart_reorder_overview",
    group=GROUP,
    risk=Risk.READ,
    description=(
        "Smart Reorder headline: customers with a usable prediction, predicted today / next "
        "24h, enrolled customers, active Smart Reorder campaigns, prediction accuracy, and the "
        "campaign settings Smart Reorder supports."
    ),
)
def get_smart_reorder_overview(ctx: ToolContext) -> ToolResult:
    from app.analytics.order_predictions import REMINDER_OFFSETS
    from app.api.v1.smart_reorder import overview

    data = overview(db=ctx.db, _=ctx.user)
    campaigns = ctx.db.execute(select(Automation).where(Automation.kind == NUDGE)).scalars().all()
    data["campaigns"] = [
        {"id": a.id, "name": a.name, "status": a.status, "channel": a.channel} for a in campaigns
    ]
    data["capabilities"] = {
        "timing": "Each customer's own predicted order time minus an offset in minutes "
        f"(presets: {', '.join(f'{v} min' for v in sorted(set(REMINDER_OFFSETS.values())))}; any value allowed).",
        "send_window": "Reminders falling outside the business send window are moved into it.",
        "channels": [Channel.SMS.value, Channel.WHATSAPP.value, Channel.EMAIL.value],
        "coupons": "Multiple coupon codes with percentage allocation; each customer keeps one code.",
        "stop_on_order": "A reminder is cancelled if the customer orders first.",
        "default_min_confidence": DEFAULT_MIN_CONFIDENCE,
    }
    return ToolResult.ok(data)


@tool(
    "get_smart_reorder_predictions",
    group=GROUP,
    risk=Risk.READ,
    description=(
        "Customers the prediction engine expects to order in a period (today, tonight, "
        "next_24h, …), contactable only, at or above a confidence. Remembered as a result set."
    ),
    properties={
        "period": {"type": "string", "enum": list(WINDOWS)},
        "min_confidence": {"type": "integer", "description": f"0-100. Default {DEFAULT_MIN_CONFIDENCE}."},
        "limit": {"type": "integer"},
    },
)
def get_smart_reorder_predictions(
    ctx: ToolContext, period: str = "today", min_confidence: int = DEFAULT_MIN_CONFIDENCE, limit: int = 20
) -> ToolResult:
    start, end, label = resolve_window(period, ctx.now)
    rows = ctx.db.execute(
        select(Customer, CustomerMetrics)
        .join(CustomerMetrics, CustomerMetrics.customer_id == Customer.id)
        .where(
            CustomerMetrics.predicted_next_order_at >= start,
            CustomerMetrics.predicted_next_order_at < end,
            CustomerMetrics.prediction_confidence >= min_confidence,
            Customer.marketing_consent.is_(True),
            Customer.is_suppressed.is_(False),
        )
        .order_by(CustomerMetrics.predicted_next_order_at)
    ).all()
    data = [
        {
            **customer_brief(c, m),
            "predicted_order_local": local_label(m.predicted_next_order_at),
            "confidence": m.prediction_confidence,
        }
        for c, m in rows[: max(1, min(limit, 100))]
    ]
    return ToolResult.ok(
        data,
        metadata={"count": len(rows), "period": label, "min_confidence": min_confidence},
        result_set=result_set("customers", [c.id for c, _ in rows], f"Predicted to reorder {label}"),
    )


@tool(
    "get_upcoming_reorders",
    group=GROUP,
    risk=Risk.READ,
    description="Enrolled Smart Reorder customers whose reminder window opens within N hours.",
    properties={"hours": {"type": "integer", "description": "1-168, default 24."}},
)
def get_upcoming_reorders(ctx: ToolContext, hours: int = 24) -> ToolResult:
    from app.api.v1.smart_reorder import upcoming

    data = upcoming(hours=max(1, min(hours, 168)), db=ctx.db, _=ctx.user)
    return ToolResult.ok(data, metadata={"count": data["total"]})


@tool(
    "preview_smart_reorder",
    group=GROUP,
    risk=Risk.READ,
    description=(
        "DRY RUN of a Smart Reorder campaign, writing nothing: audience analysed, exclusions by "
        "reason (history, confidence, already ordered, consent, opt-out, frequency cap), final "
        "audience, messages today/tonight, coupon allocation, sample schedule with each "
        "customer's send time and coupon, and a compliance check of the copy."
    ),
    properties={"automation_id": {"type": "integer"}, "samples": {"type": "integer"}},
)
def preview_smart_reorder(ctx: ToolContext, automation_id: int | None = None, samples: int = 5) -> ToolResult:
    automation = automation_for(ctx, automation_id, kind=NUDGE)
    summary = dry_run_summary(ctx, automation, samples=max(1, min(samples, 20)))
    ids = summary.pop("_customer_ids")
    return ToolResult.ok(
        summary,
        metadata={"count": summary["final_audience"], "dry_run": True},
        focus=("automation", automation.id),
        result_set=result_set("customers", ids, f"Final audience of '{automation.name}'"),
    )


@tool(
    "preview_coupon_assignment",
    group="coupons",
    risk=Risk.READ,
    description="How the campaign's coupon codes would be split across its current audience, without assigning any.",
    properties={"automation_id": {"type": "integer"}},
)
def preview_coupon_assignment(ctx: ToolContext, automation_id: int | None = None) -> ToolResult:
    from app.automations.cohort import resolve_audience
    from app.services.coupon_assignment import get_or_assign_coupon

    automation = automation_for(ctx, automation_id)
    counts: dict[str, int] = {}
    for customer_id in resolve_audience(ctx.db, automation, now=ctx.now):
        code = get_or_assign_coupon(ctx.db, customer_id, automation.id, persist=False)
        if code:
            counts[code] = counts.get(code, 0) + 1
    already = ctx.db.execute(
        select(func.count(CustomerCouponAssignment.id)).where(CustomerCouponAssignment.automation_id == automation.id)
    ).scalar_one()
    return ToolResult.ok(
        {
            "configured": automation_snapshot(ctx.db, automation)["coupons"],
            "audience_split": counts,
            "already_assigned": already,
        },
        focus=("automation", automation.id),
    )


@tool(
    "get_coupon_analytics",
    group="coupons",
    risk=Risk.READ,
    description="Per-coupon assignments, sends, deliveries, orders, conversion and revenue for a campaign.",
    properties={"automation_id": {"type": "integer"}},
)
def get_coupon_analytics(ctx: ToolContext, automation_id: int | None = None) -> ToolResult:
    from app.services.coupon_analytics import get_coupon_performance

    automation = automation_for(ctx, automation_id)
    data = get_coupon_performance(ctx.db, automation.id)
    data["note"] = "Orders are counted for customers assigned each code after assignment (not by code redeemed)."
    return ToolResult.ok(data, focus=("automation", automation.id))


# --------------------------------------------------------------------------
# Write tools
# --------------------------------------------------------------------------
@tool(
    "create_smart_reorder_campaign",
    group=GROUP,
    risk=Risk.WRITE,
    creates=True,
    previewer=_preview_after,
    summarize=lambda a: f"Create Smart Reorder campaign '{a.get('name')}' as a DRAFT",
    description=(
        "Create a Smart Reorder campaign as a DRAFT (never activated). Each customer is "
        "messaged N minutes before their own predicted reorder time. Audience is an existing "
        "segment, a purchase cohort, or the last customer list. The confirmation card shows "
        "the engine's dry run of the result. Operator-supplied coupon codes not yet verified "
        "are added to Brand Settings' verified list as part of this action."
    ),
    properties={
        "name": {"type": "string"},
        "description": {"type": "string"},
        **AUDIENCE_PROPS,
        "channel": {"type": "string", "enum": [Channel.SMS.value, Channel.WHATSAPP.value, Channel.EMAIL.value]},
        "minutes_before": {"type": "integer", "description": f"Default {DEFAULT_MINUTES_BEFORE}. Negative = after."},
        "min_confidence": {"type": "integer", "description": f"0-100. Default {DEFAULT_MIN_CONFIDENCE}."},
        "min_gap_days": {"type": "integer", "description": "Never remind the same customer more often. Default 7."},
        "message_template": {"type": "string"},
        "coupons": COUPONS_PROP,
    },
    required=["name", "message_template"],
)
def create_smart_reorder_campaign(ctx: ToolContext, **args) -> ToolResult:
    from app.automations.runtime import AutomationError
    from app.automations.service import create_automation

    db = ctx.db
    name = args["name"].strip()
    if db.execute(select(Automation.id).where(Automation.name == name)).first():
        raise ToolError(f"A campaign named '{name}' already exists.")
    channel = args.get("channel", Channel.SMS.value)
    coupons = normalise_coupons(ctx, args.get("coupons") or [])
    template = args["message_template"]
    check = validate_copy(ctx, template, channel, coupon_codes=[c["code"] for c in coupons])
    if check["unknown_tags"] or check["blocking_findings"]:
        problems = check["unknown_tags"] + [f["message"] for f in check["blocking_findings"]]
        raise ToolError("The message cannot be used: " + "; ".join(problems))

    segment_id, manual_ids, audience_label = resolve_audience_args(ctx, args, name=name)
    config = {
        "custom_offset_minutes": int(args.get("minutes_before", DEFAULT_MINUTES_BEFORE)),
        "min_confidence": int(args.get("min_confidence", DEFAULT_MIN_CONFIDENCE)),
        "min_gap_days": int(args.get("min_gap_days", 7)),
    }
    try:
        automation = create_automation(
            db,
            name=name,
            kind=NUDGE,
            description=args.get("description") or f"Smart Reorder for {audience_label}. Created by the AI Copilot.",
            channel=channel,
            objective=CampaignObjective.REORDER.value,
            segment_id=segment_id,
            manual_customer_ids=manual_ids,
            message_template=template,
            config=config,
            stop_on_order=True,
            require_approval=True,
            created_by_id=ctx.user.id,
        )
    except AutomationError as exc:
        raise ToolError(str(exc)) from exc

    registered = register_coupon_codes(ctx, [c["code"] for c in coupons if not c["verified"]])
    replace_coupons(ctx, automation, coupons)
    db.add(
        AuditLog(
            actor=ctx.user.email,
            action="AUTOMATION_CREATED",
            entity_type="automation",
            entity_id=str(automation.id),
            detail={"via": "copilot", "kind": NUDGE, "coupon_codes_registered": registered},
        )
    )
    db.commit()
    snapshot = automation_snapshot(db, automation)
    snapshot["audience_definition"] = audience_label
    snapshot["coupon_codes_added_to_brand_settings"] = registered
    return ToolResult.ok(
        snapshot,
        focus=("automation", automation.id),
        target_type="automation",
        target_id=automation.id,
        after=snapshot,
    )


@tool(
    "update_smart_reorder_campaign",
    group=GROUP,
    risk=Risk.WRITE,
    previewer=_preview_after,
    summarize=lambda a: "Update Smart Reorder campaign settings",
    description=(
        "Change a Smart Reorder campaign's timing, confidence threshold, frequency, channel, "
        "name, audience, or multi-touch touchpoints. Changes to what would be sent withdraw approval."
    ),
    properties={
        "automation_id": {"type": "integer"},
        "name": {"type": "string"},
        "channel": {"type": "string", "enum": [Channel.SMS.value, Channel.WHATSAPP.value, Channel.EMAIL.value]},
        "minutes_before": {"type": "integer"},
        "min_confidence": {"type": "integer"},
        "min_gap_days": {"type": "integer"},
        **AUDIENCE_PROPS,
        "touchpoints": {
            "type": "array",
            "description": "[{position, timing_minutes_before | timing_days_after, condition?, stop_on_order?, message_template?}]",
            "items": {"type": "object"},
        },
    },
)
def update_smart_reorder_campaign(ctx: ToolContext, automation_id: int | None = None, **args) -> ToolResult:
    from app.automations.service import apply_update
    from app.services.automation_config_validation import validate_touchpoint_rules

    db = ctx.db
    automation = automation_for(ctx, automation_id, kind=NUDGE)
    before = automation_snapshot(db, automation)
    changes: dict = {}
    config = dict(automation.config or {})
    if "minutes_before" in args:
        config["custom_offset_minutes"] = int(args["minutes_before"])
        config.pop("reminder_offset", None)
    if "min_confidence" in args:
        if not 0 <= int(args["min_confidence"]) <= 100:
            raise ToolError("min_confidence must be between 0 and 100.")
        config["min_confidence"] = int(args["min_confidence"])
    if "min_gap_days" in args:
        if int(args["min_gap_days"]) < 1:
            raise ToolError("min_gap_days must be at least 1.")
        config["min_gap_days"] = int(args["min_gap_days"])
    if "touchpoints" in args:
        errors = validate_touchpoint_rules(Automation(config={"touchpoints": args["touchpoints"]}))
        if errors:
            raise ToolError("Invalid touchpoints: " + "; ".join(errors))
        config["touchpoints"] = args["touchpoints"]
    if config != (automation.config or {}):
        changes["config"] = config
    if args.get("name"):
        changes["name"] = args["name"].strip()
    if args.get("channel"):
        changes["channel"] = args["channel"]
    if any(args.get(k) for k in AUDIENCE_PROPS):
        segment_id, manual_ids, _ = resolve_audience_args(
            ctx, args, name=changes.get("name", automation.name), current_segment_id=automation.segment_id
        )
        changes["segment_id"] = segment_id
        changes["manual_customer_ids"] = manual_ids
    if not changes:
        raise ToolError("Nothing to change — no new settings were given.")
    summary = apply_update(db, automation, changes, actor=ctx.user.email)
    after = automation_snapshot(db, automation)
    return ToolResult.ok(
        {"campaign": after, "changed": summary["fields"], "approval_withdrawn": summary["approval_revoked"]},
        focus=("automation", automation.id),
        target_type="automation",
        target_id=automation.id,
        before=before,
        after=after,
    )


@tool(
    "update_coupon_allocation",
    group="coupons",
    risk=Risk.WRITE,
    previewer=_preview_after,
    summarize=lambda a: "Change coupon codes / allocation",
    description=(
        "Replace a campaign's coupon codes and their split. Pass the full list; allocations "
        "must total 100 (omit all for an equal split). To change only the split, repeat the "
        "existing codes in order with new allocations. Customers already assigned a code keep it."
    ),
    properties={"automation_id": {"type": "integer"}, "coupons": COUPONS_PROP},
    required=["coupons"],
)
def update_coupon_allocation(ctx: ToolContext, coupons: list, automation_id: int | None = None) -> ToolResult:
    from app.automations.service import revoke_approval

    db = ctx.db
    automation = automation_for(ctx, automation_id)
    cleaned = normalise_coupons(ctx, coupons, automation_id=automation.id)
    if not cleaned:
        raise ToolError("Give at least one coupon code.")
    before = automation_snapshot(db, automation)
    registered = register_coupon_codes(ctx, [c["code"] for c in cleaned if not c["verified"]])
    replace_coupons(ctx, automation, cleaned)
    revoked = revoke_approval(db, automation, reason="Coupon codes changed.", actor=ctx.user.email)
    db.add(
        AuditLog(
            actor=ctx.user.email,
            action="AUTOMATION_COUPONS_UPDATED",
            entity_type="automation",
            entity_id=str(automation.id),
            detail={"coupons": [{k: c[k] for k in ("code", "allocation")} for c in cleaned], "via": "copilot"},
        )
    )
    db.commit()
    after = automation_snapshot(db, automation)
    kept = db.execute(
        select(func.count(CustomerCouponAssignment.id)).where(CustomerCouponAssignment.automation_id == automation.id)
    ).scalar_one()
    return ToolResult.ok(
        {
            "campaign": after,
            "customers_keeping_existing_code": kept,
            "coupon_codes_added_to_brand_settings": registered,
            "approval_withdrawn": revoked,
        },
        focus=("automation", automation.id),
        target_type="automation",
        target_id=automation.id,
        before=before,
        after=after,
    )


@tool(
    "create_coupon_variant",
    group="coupons",
    risk=Risk.WRITE,
    previewer=_preview_after,
    summarize=lambda a: f"Add coupon code {str(a.get('code', '')).upper()}",
    description="Add one coupon code to a campaign at a given allocation; the others are scaled down proportionally.",
    properties={
        "automation_id": {"type": "integer"},
        "code": {"type": "string"},
        "allocation": {"type": "number", "description": "Percent for the new code (0-100)."},
    },
    required=["code", "allocation"],
)
def create_coupon_variant(ctx: ToolContext, code: str, allocation: float, automation_id: int | None = None) -> ToolResult:
    automation = automation_for(ctx, automation_id)
    current = [v for v in coupon_rows(ctx.db, automation.id) if v.enabled]
    if not 0 < allocation <= 100:
        raise ToolError("Allocation must be above 0 and at most 100.")
    remaining = 100.0 - allocation
    total = sum(v.allocation_percentage for v in current) or 1.0
    coupons = [
        {"code": v.coupon_code, "allocation": round(v.allocation_percentage / total * remaining, 2)} for v in current
    ]
    coupons.append({"code": code, "allocation": allocation})
    drift = round(100.0 - sum(c["allocation"] for c in coupons), 2)
    coupons[0]["allocation"] = round(coupons[0]["allocation"] + drift, 2)
    return update_coupon_allocation(ctx, coupons=coupons, automation_id=automation.id)


def _activation_preview(ctx: ToolContext, args: dict, result: ToolResult) -> dict:
    return _preview_after(ctx, args, result)


@tool(
    "activate_smart_reorder_campaign",
    group=GROUP,
    risk=Risk.HIGH_RISK_WRITE,
    previewer=_activation_preview,
    summarize=lambda a: "ACTIVATE Smart Reorder campaign — real customers will be messaged",
    description=(
        "Switch a Smart Reorder campaign on. Confirming records the confirming person's approval "
        "and activates it; reminders then go out at each customer's scheduled time. Refused if "
        "the copy has blocking compliance findings."
    ),
    properties={"automation_id": {"type": "integer"}},
)
def activate_smart_reorder_campaign(ctx: ToolContext, automation_id: int | None = None) -> ToolResult:
    return _activate(ctx, automation_for(ctx, automation_id, kind=NUDGE))


def _activate(ctx: ToolContext, automation: Automation) -> ToolResult:
    from app.automations.runtime import AutomationError
    from app.automations.service import activate, approve

    db = ctx.db
    if automation.status == AutomationStatus.ACTIVE.value:
        raise ToolError(f"'{automation.name}' is already active.")
    if automation.status == AutomationStatus.ARCHIVED.value:
        raise ToolError(f"'{automation.name}' is archived and cannot be activated.")
    codes = [v.coupon_code for v in coupon_rows(db, automation.id) if v.enabled]
    check = validate_copy(ctx, automation.message_template or "", automation.channel, coupon_codes=codes)
    blockers = check["unknown_tags"] + [f["message"] for f in check["blocking_findings"]]
    blockers += [f["message"] for f in check["needs_confirmation_findings"]]
    if blockers:
        raise ToolError(
            "Activation refused — every message would be blocked at send time: " + "; ".join(blockers)
        )
    before = automation_snapshot(db, automation)
    try:
        if automation.require_approval and automation.approved_at is None:
            approve(db, automation, user_id=ctx.user.id)
        activate(db, automation, now=ctx.now)
    except AutomationError as exc:
        raise ToolError(str(exc)) from exc
    db.add(
        AuditLog(
            actor=ctx.user.email,
            action="AUTOMATION_ACTIVATED",
            entity_type="automation",
            entity_id=str(automation.id),
            detail={"via": "copilot"},
        )
    )
    db.commit()
    after = automation_snapshot(db, automation)
    return ToolResult.ok(
        {"campaign": after, "approved_by": ctx.user.email},
        focus=("automation", automation.id),
        target_type="automation",
        target_id=automation.id,
        before=before,
        after=after,
    )


@tool(
    "pause_smart_reorder_campaign",
    group=GROUP,
    risk=Risk.WRITE,
    summarize=lambda a: "Pause Smart Reorder campaign",
    description="Pause a Smart Reorder campaign. Queued reminders are held, not sent, while paused.",
    properties={"automation_id": {"type": "integer"}},
)
def pause_smart_reorder_campaign(ctx: ToolContext, automation_id: int | None = None) -> ToolResult:
    from app.copilot.tools.campaigns import pause_automation

    return pause_automation(ctx, automation_for(ctx, automation_id, kind=NUDGE))


@tool(
    "cancel_smart_reorder_campaign",
    group=GROUP,
    risk=Risk.HIGH_RISK_WRITE,
    summarize=lambda a: "CANCEL Smart Reorder campaign and call off its pending reminders",
    description="Archive a Smart Reorder campaign and cancel every reminder still pending. Cannot be undone.",
    properties={"automation_id": {"type": "integer"}},
)
def cancel_smart_reorder_campaign(ctx: ToolContext, automation_id: int | None = None) -> ToolResult:
    from app.copilot.tools.campaigns import archive_automation

    return archive_automation(ctx, automation_for(ctx, automation_id, kind=NUDGE))


__all__ = ["dry_run_summary", "_activate", "CancellationReason"]
