"""Customer lookups, Customer 360, and "why didn't they get the message?"."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import and_, func, or_, select

from app.copilot.registry import Risk, ToolContext, ToolError, ToolResult, tool
from app.copilot.tools._common import (
    WINDOWS,
    customer_brief,
    find_customer,
    local_label,
    money,
    resolve_window,
    result_set,
)
from app.core.enums import AutomationKind, Channel, LifecycleStage, ChurnRiskBand
from app.models.entities import (
    Automation,
    AutomationEnrollment,
    AutomationSend,
    CampaignRecipient,
    Campaign,
    ChurnScore,
    CommunicationEvent,
    Customer,
    CustomerMetrics,
    Order,
    OrderPredictionRecord,
    ScheduledMessage,
    Segment,
    CustomerSegment,
    SuppressionList,
)

GROUP = "customers"
CUSTOMER_REF = {
    "type": "string",
    "description": "Customer id, external id, email, phone, or full name.",
}


@tool(
    "search_customers",
    group=GROUP,
    risk=Risk.READ,
    description=(
        "Find customers by value, recency, lifecycle and churn. Returns the top rows and the "
        "total count; the full matching list is remembered so a follow-up like 'create a "
        "campaign for them' can use it. 'Historically ordered more than $X' usually means "
        "average order value; say which interpretation you used."
    ),
    properties={
        "name": {"type": "string", "description": "Part of a first or last name."},
        "lifecycle_stages": {
            "type": "array",
            "items": {"type": "string", "enum": [s.value for s in LifecycleStage]},
        },
        "churn_risk_bands": {
            "type": "array",
            "items": {"type": "string", "enum": [b.value for b in ChurnRiskBand]},
        },
        "segment_id": {"type": "integer"},
        "min_lifetime_revenue": {"type": "number"},
        "min_average_order_value": {"type": "number"},
        "min_days_since_last_order": {"type": "integer"},
        "max_days_since_last_order": {"type": "integer"},
        "min_orders": {"type": "integer"},
        "preferred_category": {"type": "string", "description": "e.g. Beer, Wine, Spirits"},
        "contactable_only": {"type": "boolean", "description": "Marketing consent and not suppressed."},
        "sort_by": {
            "type": "string",
            "enum": ["lifetime_revenue", "average_order_value", "days_since_last_order", "total_orders"],
        },
        "limit": {"type": "integer", "description": "Rows to return (max 100). Default 20."},
    },
)
def search_customers(ctx: ToolContext, **args) -> ToolResult:
    query = select(Customer, CustomerMetrics).outerjoin(
        CustomerMetrics, CustomerMetrics.customer_id == Customer.id
    )
    conditions = []
    filters: list[str] = []
    if name := args.get("name"):
        like = f"%{name.strip().lower()}%"
        conditions.append(
            or_(func.lower(Customer.first_name).like(like), func.lower(Customer.last_name).like(like))
        )
        filters.append(f"name contains '{name}'")
    if stages := args.get("lifecycle_stages"):
        conditions.append(Customer.lifecycle_stage.in_(stages))
        filters.append(f"lifecycle in {'/'.join(stages)}")
    if bands := args.get("churn_risk_bands"):
        query = query.join(ChurnScore, ChurnScore.customer_id == Customer.id)
        conditions.append(ChurnScore.risk_band.in_(bands))
        filters.append(f"churn risk {'/'.join(bands)}")
    if segment_id := args.get("segment_id"):
        segment = ctx.db.get(Segment, segment_id)
        if segment is None:
            raise ToolError(f"Segment {segment_id} does not exist.")
        conditions.append(
            Customer.id.in_(select(CustomerSegment.customer_id).where(CustomerSegment.segment_id == segment_id))
        )
        filters.append(f"in segment '{segment.name}'")
    numeric = [
        ("min_lifetime_revenue", CustomerMetrics.lifetime_revenue, ">=", "lifetime spend ≥ ${:,.0f}"),
        ("min_average_order_value", CustomerMetrics.average_order_value, ">=", "average order ≥ ${:,.0f}"),
        ("min_days_since_last_order", CustomerMetrics.days_since_last_order, ">=", "no order for ≥ {} days"),
        ("max_days_since_last_order", CustomerMetrics.days_since_last_order, "<=", "ordered within {} days"),
        ("min_orders", CustomerMetrics.completed_orders, ">=", "≥ {} completed orders"),
    ]
    for key, column, op, label in numeric:
        value = args.get(key)
        if value is None:
            continue
        conditions.append(column >= value if op == ">=" else column <= value)
        filters.append(label.format(value))
    if category := args.get("preferred_category"):
        # A JSON list column; matched in Python below to stay database-neutral.
        filters.append(f"prefers {category}")
    if args.get("contactable_only"):
        conditions.append(and_(Customer.marketing_consent.is_(True), Customer.is_suppressed.is_(False)))
        filters.append("contactable")
    if conditions:
        query = query.where(and_(*conditions))

    sort_key = args.get("sort_by", "lifetime_revenue")
    sort_column = {
        "lifetime_revenue": CustomerMetrics.lifetime_revenue,
        "average_order_value": CustomerMetrics.average_order_value,
        "days_since_last_order": CustomerMetrics.days_since_last_order,
        "total_orders": CustomerMetrics.completed_orders,
    }[sort_key]
    query = query.order_by(func.coalesce(sort_column, 0).desc(), Customer.id)

    rows = ctx.db.execute(query).all()
    if category:
        wanted = category.strip().lower()
        rows = [
            (c, m) for c, m in rows
            if m and any(str(p).lower() == wanted for p in (m.preferred_categories or []))
        ]

    limit = max(1, min(int(args.get("limit", 20)), 100))
    description = "Customers " + (", ".join(filters) if filters else "(no filters)")
    ids = [c.id for c, _ in rows]
    return ToolResult.ok(
        [customer_brief(c, m) for c, m in rows[:limit]],
        metadata={
            "count": len(rows),
            "returned": min(limit, len(rows)),
            "filters": filters,
            "sorted_by": sort_key,
        },
        result_set=result_set("customers", ids, description),
    )


@tool(
    "get_customer",
    group=GROUP,
    risk=Risk.READ,
    description="One customer's profile and headline metrics. Contact details are masked.",
    properties={"customer": CUSTOMER_REF},
    required=["customer"],
)
def get_customer(ctx: ToolContext, customer: str) -> ToolResult:
    found = find_customer(ctx.db, customer)
    brief = customer_brief(found)
    brief.update(
        {
            "external_id": found.external_id,
            "phone": _mask(found.phone),
            "email": _mask(found.email),
            "sms_consent": found.sms_consent,
            "email_consent": found.email_consent,
            "whatsapp_consent": found.whatsapp_consent,
            "age_verified": found.age_verified,
        }
    )
    return ToolResult.ok(brief, focus=("customer", found.id))


@tool(
    "get_customer_360",
    group=GROUP,
    risk=Risk.READ,
    description=(
        "Everything the engine knows about one customer: metrics, churn, RFM, next best "
        "action and Smart Reorder prediction."
    ),
    properties={"customer": CUSTOMER_REF},
    required=["customer"],
)
def get_customer_360(ctx: ToolContext, customer: str) -> ToolResult:
    from app.services.intelligence import load_customer_views

    found = find_customer(ctx.db, customer)
    views = load_customer_views(ctx.db, [found.id])
    if not views:
        raise ToolError(f"No computed view exists yet for customer {found.id}.")
    view = dict(views[0])
    for private in ("email", "phone", "date_of_birth"):
        if view.get(private):
            view[private] = _mask(str(view[private]))
    return ToolResult.ok(view, focus=("customer", found.id))


@tool(
    "get_customer_orders",
    group=GROUP,
    risk=Risk.READ,
    description="A customer's most recent orders with their line items.",
    properties={"customer": CUSTOMER_REF, "limit": {"type": "integer"}},
    required=["customer"],
)
def get_customer_orders(ctx: ToolContext, customer: str, limit: int = 10) -> ToolResult:
    found = find_customer(ctx.db, customer)
    orders = ctx.db.execute(
        select(Order)
        .where(Order.customer_id == found.id)
        .order_by(Order.ordered_at.desc())
        .limit(max(1, min(limit, 50)))
    ).scalars().all()
    return ToolResult.ok(
        [
            {
                "id": o.id,
                "reference": o.external_id,
                "ordered_at": o.ordered_at.isoformat(),
                "ordered_at_local": local_label(o.ordered_at),
                "status": o.status,
                "total": money(o.total_amount),
                "coupon_code": o.coupon_code,
                "items": [
                    {"product": i.product_name, "category": i.category, "brand": i.brand, "quantity": i.quantity}
                    for i in o.items
                ],
            }
            for o in orders
        ],
        metadata={"customer_id": found.id, "customer": found.full_name},
        focus=("customer", found.id),
    )


@tool(
    "get_customer_predictions",
    group=GROUP,
    risk=Risk.READ,
    description=(
        "The Smart Reorder prediction for one customer: predicted next order, confidence, "
        "their Smart Reorder enrollments and upcoming reminders, and past prediction outcomes."
    ),
    properties={"customer": CUSTOMER_REF},
    required=["customer"],
)
def get_customer_predictions(ctx: ToolContext, customer: str) -> ToolResult:
    found = find_customer(ctx.db, customer)
    metrics = found.metrics
    enrollments = ctx.db.execute(
        select(AutomationEnrollment, Automation)
        .join(Automation, Automation.id == AutomationEnrollment.automation_id)
        .where(
            AutomationEnrollment.customer_id == found.id,
            Automation.kind == AutomationKind.NUDGE.value,
        )
    ).all()
    upcoming = ctx.db.execute(
        select(ScheduledMessage)
        .where(ScheduledMessage.customer_id == found.id)
        .order_by(ScheduledMessage.scheduled_at.desc())
        .limit(5)
    ).scalars().all()
    outcomes = ctx.db.execute(
        select(OrderPredictionRecord)
        .where(OrderPredictionRecord.customer_id == found.id)
        .order_by(OrderPredictionRecord.predicted_at.desc())
        .limit(5)
    ).scalars().all()
    return ToolResult.ok(
        {
            "customer": customer_brief(found),
            "predicted_next_order_at": (
                metrics.predicted_next_order_at.isoformat()
                if metrics and metrics.predicted_next_order_at
                else None
            ),
            "predicted_next_order_local": local_label(metrics.predicted_next_order_at) if metrics else None,
            "confidence": metrics.prediction_confidence if metrics else 0,
            "typical_order_weekday": metrics.typical_order_weekday if metrics else None,
            "typical_order_time": (
                f"{metrics.typical_order_hour:02d}:{(metrics.typical_order_minute or 0):02d}"
                if metrics and metrics.typical_order_hour is not None
                else None
            ),
            "median_days_between_orders": metrics.median_purchase_interval_days if metrics else None,
            "smart_reorder_enrollments": [
                {
                    "automation_id": a.id,
                    "automation": a.name,
                    "automation_status": a.status,
                    "enrollment_status": e.status,
                    "next_due_at_local": local_label(e.next_due_at),
                    "last_sent_at_local": local_label(e.last_sent_at),
                    "stop_reason": e.stop_reason,
                }
                for e, a in enrollments
            ],
            "recent_reminders": [_message_view(m) for m in upcoming],
            "past_prediction_outcomes": [
                {
                    "predicted_order_at_local": local_label(p.predicted_order_at),
                    "status": p.status,
                    "actual_order_at_local": local_label(p.actual_order_at),
                    "error_minutes": p.error_minutes,
                }
                for p in outcomes
            ],
        },
        focus=("customer", found.id),
    )


@tool(
    "get_customer_campaign_history",
    group=GROUP,
    risk=Risk.READ,
    description=(
        "Every campaign and automation message aimed at a customer — sent and withheld, "
        "with the recorded skip reason."
    ),
    properties={"customer": CUSTOMER_REF, "limit": {"type": "integer"}},
    required=["customer"],
)
def get_customer_campaign_history(ctx: ToolContext, customer: str, limit: int = 25) -> ToolResult:
    from app.automations.service import customer_history

    found = find_customer(ctx.db, customer)
    automated = customer_history(ctx.db, found.id, limit=max(1, min(limit, 100)))
    campaigns = ctx.db.execute(
        select(CampaignRecipient, Campaign.name)
        .join(Campaign, Campaign.id == CampaignRecipient.campaign_id)
        .where(CampaignRecipient.customer_id == found.id)
        .order_by(CampaignRecipient.id.desc())
        .limit(limit)
    ).all()
    return ToolResult.ok(
        {
            "automation_messages": automated,
            "campaign_messages": [
                {
                    "campaign": name,
                    "status": r.status,
                    "exclusion_reason": r.exclusion_reason,
                    "sent_at_local": local_label(r.sent_at),
                    "converted_at_local": local_label(r.converted_at),
                }
                for r, name in campaigns
            ],
        },
        focus=("customer", found.id),
    )


@tool(
    "diagnose_customer_delivery",
    group=GROUP,
    risk=Risk.READ,
    description=(
        "Explain whether and why a customer did or did not receive an automated message "
        "(Smart Reorder by default). Collects consent, suppression, prediction, enrollment, "
        "the scheduled message and its recorded cancellation reason, send records, and "
        "orders, and returns the documented reason. Use this for 'why didn't X get…'."
    ),
    properties={
        "customer": CUSTOMER_REF,
        "automation_id": {"type": "integer", "description": "Limit to one automation."},
        "period": {"type": "string", "enum": list(WINDOWS), "description": "Default last_7_days."},
    },
    required=["customer"],
)
def diagnose_customer_delivery(
    ctx: ToolContext, customer: str, automation_id: int | None = None, period: str = "last_7_days"
) -> ToolResult:
    from app.automations import nudge
    from app.automations.cohort import resolve_audience

    db = ctx.db
    found = find_customer(db, customer)
    start, end, label = resolve_window(period, ctx.now)
    # A reminder about yesterday was usually scheduled the day before.
    lookback_start = start - timedelta(days=1)

    if automation_id:
        automations = db.execute(select(Automation).where(Automation.id == automation_id)).scalars().all()
    else:
        automations = _relevant_automations(db, found.id)

    suppressions = db.execute(
        select(SuppressionList).where(
            SuppressionList.customer_id == found.id, SuppressionList.active.is_(True)
        )
    ).scalars().all()
    opt_outs = db.execute(
        select(CommunicationEvent).where(
            CommunicationEvent.customer_id == found.id,
            CommunicationEvent.event_type == "CUSTOMER_OPTED_OUT",
        )
    ).scalars().all()
    orders = db.execute(
        select(Order)
        .where(Order.customer_id == found.id, Order.ordered_at >= lookback_start, Order.ordered_at < end)
        .order_by(Order.ordered_at)
    ).scalars().all()
    metrics = found.metrics

    facts: dict = {
        "customer": {"id": found.id, "name": found.full_name},
        "period": label,
        "eligibility": {
            "marketing_consent": found.marketing_consent,
            "sms_consent": found.sms_consent,
            "whatsapp_consent": found.whatsapp_consent,
            "email_consent": found.email_consent,
            "is_suppressed": found.is_suppressed,
            "suppressed_channels": sorted({s.channel for s in suppressions}),
            "opted_out_events": len(opt_outs),
            "age_verified": found.age_verified,
            "has_phone": bool(found.phone),
        },
        "prediction": {
            "predicted_next_order_local": local_label(metrics.predicted_next_order_at) if metrics else None,
            "confidence": metrics.prediction_confidence if metrics else 0,
        },
        "orders_in_period": [
            {"ordered_at_local": local_label(o.ordered_at), "status": o.status, "total": money(o.total_amount)}
            for o in orders
        ],
        "automations": [],
    }

    findings: list[str] = []
    for automation in automations:
        entry: dict = {"automation_id": automation.id, "name": automation.name, "status": automation.status}
        enrollment = db.execute(
            select(AutomationEnrollment).where(
                AutomationEnrollment.automation_id == automation.id,
                AutomationEnrollment.customer_id == found.id,
            )
        ).scalars().first()
        messages = db.execute(
            select(ScheduledMessage)
            .where(
                ScheduledMessage.automation_id == automation.id,
                ScheduledMessage.customer_id == found.id,
                or_(
                    and_(ScheduledMessage.scheduled_at >= lookback_start, ScheduledMessage.scheduled_at < end),
                    and_(ScheduledMessage.cancelled_at >= lookback_start, ScheduledMessage.cancelled_at < end),
                ),
            )
            .order_by(ScheduledMessage.scheduled_at)
        ).scalars().all()
        sends = db.execute(
            select(AutomationSend)
            .where(
                AutomationSend.automation_id == automation.id,
                AutomationSend.customer_id == found.id,
                AutomationSend.is_dry_run.is_(False),
                AutomationSend.scheduled_for >= lookback_start,
                AutomationSend.scheduled_for < end,
            )
            .order_by(AutomationSend.scheduled_for)
        ).scalars().all()
        entry["enrollment"] = (
            {
                "status": enrollment.status,
                "next_due_at_local": local_label(enrollment.next_due_at),
                "last_sent_at_local": local_label(enrollment.last_sent_at),
                "stop_reason": enrollment.stop_reason,
            }
            if enrollment
            else None
        )
        entry["scheduled_messages"] = [_message_view(m) for m in messages]
        entry["send_records"] = [
            {
                "scheduled_for_local": local_label(s.scheduled_for),
                "status": s.status,
                "skip_reason": s.skip_reason,
                "skip_detail": s.skip_detail,
                "error": s.error_message,
            }
            for s in sends
        ]
        in_audience = found.id in set(resolve_audience(db, automation, now=ctx.now))
        entry["in_audience"] = in_audience
        if automation.kind == AutomationKind.NUDGE.value:
            plan = nudge.plan_for(db, found.id, nudge.config_of(automation), now=ctx.now)
            entry["current_plan"] = {
                "has_plan": plan.has_plan,
                "reason_if_none": plan.reason or None,
                "confidence": plan.prediction.overall_confidence if plan.prediction else None,
                "is_estimate": bool(plan.prediction and plan.prediction.is_estimate),
                "min_confidence": nudge.config_of(automation)["min_confidence"],
                "next_reminder_local": local_label(plan.scheduled_utc),
            }
        entry["conclusion"] = _conclude(found, automation, entry, facts)
        findings.append(f"{automation.name}: {entry['conclusion']}")
        facts["automations"].append(entry)

    if not automations:
        findings.append(
            "No Smart Reorder campaign exists, so no reminder could have been sent."
            if not automation_id
            else f"Automation {automation_id} does not exist."
        )
    facts["findings"] = findings
    return ToolResult.ok(facts, focus=("customer", found.id))


def _relevant_automations(db, customer_id: int) -> list[Automation]:
    """Smart Reorder campaigns that touched this customer, plus the live ones.

    Every draft ever made is not relevant to "why didn't they get it", and
    walking all of them buries the one answer that matters.
    """
    nudge_kind = AutomationKind.NUDGE.value
    involved = set()
    for model in (ScheduledMessage, AutomationSend, AutomationEnrollment):
        involved |= set(
            db.execute(select(model.automation_id).where(model.customer_id == customer_id).distinct()).scalars()
        )
    rows = db.execute(
        select(Automation)
        .where(
            Automation.kind == nudge_kind,
            or_(Automation.id.in_(involved or {0}), Automation.status.in_(["ACTIVE", "TESTING"])),
        )
        .order_by(Automation.id.desc())
        .limit(10)
    ).scalars().all()
    if rows:
        return list(rows)
    return list(
        db.execute(
            select(Automation).where(Automation.kind == nudge_kind).order_by(Automation.updated_at.desc()).limit(3)
        ).scalars()
    )


def _conclude(customer: Customer, automation: Automation, entry: dict, facts: dict) -> str:
    """The documented reason, in priority order. Only states recorded facts."""
    for message in reversed(entry["scheduled_messages"]):
        status = message["status"]
        if status in ("SENT", "DELIVERED", "CONVERTED"):
            return f"Sent — reminder {status.lower()} at {message['sent_at_local'] or message['scheduled_at_local']}."
        if status == "CANCELLED":
            reason = message["cancellation_reason"] or "no reason recorded"
            detail = f" {message['cancellation_detail']}" if message.get("cancellation_detail") else ""
            return (
                f"Not sent — the reminder scheduled for {message['scheduled_at_local']} was cancelled "
                f"({reason}).{detail}"
            )
        if status in ("FAILED", "SUPPRESSED", "EXPIRED"):
            why = message.get("error_message") or message.get("cancellation_detail") or message.get("cancellation_reason")
            return f"Not sent — the reminder ended {status}" + (f": {why}" if why else ".")
        if status in ("SCHEDULED", "PROCESSING", "DRAFT"):
            return f"Not sent yet — a reminder is {status.lower()} for {message['scheduled_at_local']}."
    for send in reversed(entry["send_records"]):
        if send["status"] in ("SENT", "DELIVERED"):
            return f"Sent at {send['scheduled_for_local']}."
        if send["status"] == "SKIPPED":
            return f"Not sent — skipped ({send['skip_reason']})." + (
                f" {send['skip_detail']}" if send.get("skip_detail") else ""
            )
        if send["status"] == "FAILED":
            return f"Not sent — the provider failed: {send.get('error') or 'no error recorded'}."

    eligibility = facts["eligibility"]
    if automation.status not in ("ACTIVE", "TESTING"):
        return f"Not sent — the campaign is {automation.status}, so it sends nothing."
    if not entry["in_audience"]:
        return "Not sent — the customer is not in this campaign's audience."
    if not eligibility["marketing_consent"]:
        return "Not sent — the customer has no marketing consent."
    if eligibility["is_suppressed"] or eligibility["suppressed_channels"]:
        return "Not sent — the customer is suppressed."
    channel_consent = {
        Channel.SMS.value: "sms_consent",
        Channel.WHATSAPP.value: "whatsapp_consent",
        Channel.EMAIL.value: "email_consent",
    }.get(automation.channel)
    if channel_consent and not eligibility[channel_consent]:
        return f"Not sent — no {automation.channel} consent."
    plan = entry.get("current_plan") or {}
    if plan and not plan.get("has_plan"):
        return f"Not sent — no usable ordering pattern ({plan.get('reason_if_none') or 'insufficient history'})."
    if plan and not plan.get("is_estimate") and plan.get("confidence") is not None and plan["confidence"] < plan.get("min_confidence", 0):
        return (
            f"Not sent — prediction confidence {plan['confidence']} is below this campaign's "
            f"minimum of {plan['min_confidence']}."
        )
    enrollment = entry.get("enrollment")
    if enrollment is None:
        return "Not sent — not enrolled yet; enrollment happens on the next queue build."
    if enrollment["status"] != "ACTIVE":
        return f"Not sent — enrollment is {enrollment['status']}" + (
            f" ({enrollment['stop_reason']})." if enrollment.get("stop_reason") else "."
        )
    if plan.get("next_reminder_local"):
        return f"Nothing was due in this period; the next reminder is planned for {plan['next_reminder_local']}."
    return "No message or skip was recorded for this period."


def _message_view(message: ScheduledMessage) -> dict:
    return {
        "id": message.id,
        "automation_id": message.automation_id,
        "status": message.status,
        "scheduled_at_local": local_label(message.scheduled_at),
        "predicted_order_at_local": local_label(message.predicted_order_at),
        "offset_minutes": message.offset_minutes,
        "confidence": message.confidence,
        "cancellation_reason": message.cancellation_reason,
        "cancellation_detail": message.cancellation_detail,
        "sent_at_local": local_label(message.sent_at),
        "error_message": message.error_message,
        "message": message.rendered_message,
    }


def _mask(value: str | None) -> str | None:
    if not value:
        return None
    if "@" in value:
        name, _, domain = value.partition("@")
        return f"{name[:2]}***@{domain}"
    return f"{value[:4]}***{value[-2:]}" if len(value) > 6 else "***"
