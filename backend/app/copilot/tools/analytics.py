"""Analytics: every figure comes from a query, and says which period it covers."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import case, func, select

from app.copilot.registry import Risk, ToolContext, ToolResult, tool
from app.copilot.tools._common import WINDOWS, automation_for, money, resolve_window, segment_for
from app.core.enums import AutomationKind, OrderStatus
from app.models.entities import (
    AttributionRecord,
    Automation,
    Campaign,
    ChurnScore,
    CommunicationEvent,
    Customer,
    CustomerMetrics,
    CustomerSegment,
    Order,
    ScheduledMessage,
)

GROUP = "analytics"
PERIOD = {"type": "string", "enum": list(WINDOWS), "description": "Default last_30_days."}


def _period(ctx: ToolContext, period: str | None, default: str = "last_30_days"):
    start, end, label = resolve_window(period or default, ctx.now)
    return start, end, {"period": label, "from": start.isoformat(), "to": end.isoformat()}


def _smart_reorder_campaign_ids(ctx: ToolContext) -> list[int]:
    return [
        cid
        for cid in ctx.db.execute(
            select(Automation.campaign_id).where(
                Automation.kind == AutomationKind.NUDGE.value, Automation.campaign_id.is_not(None)
            )
        ).scalars()
    ]


@tool(
    "get_retention_overview",
    group=GROUP,
    risk=Risk.READ,
    description="Headline retention dashboard: customers by lifecycle, active/repeat rates, revenue, churn risk.",
)
def get_retention_overview(ctx: ToolContext) -> ToolResult:
    from app.analytics.dashboards import overview

    return ToolResult.ok(overview(ctx.db, now=ctx.now), metadata={"period": "as of now (30/90-day windows inside)"})


@tool(
    "get_campaign_analytics",
    group=GROUP,
    risk=Risk.READ,
    description=(
        "Campaign performance. With an automation_id: that campaign's sends, cancellations, "
        "conversions and revenue. Without: all campaigns compared (sends, conversion, revenue)."
    ),
    properties={"automation_id": {"type": "integer"}},
)
def get_campaign_analytics(ctx: ToolContext, automation_id: int | None = None) -> ToolResult:
    from app.analytics.dashboards import campaign_analytics

    if automation_id is not None:
        from app.automations.service import automation_stats

        automation = automation_for(ctx, automation_id)
        data = {"campaign": automation.name, "stats": automation_stats(ctx.db, automation)}
        if automation.kind == AutomationKind.NUDGE.value:
            from app.services.smart_reorder_queue import dashboard

            data["smart_reorder"] = dashboard(ctx.db, automation, now=ctx.now)
        return ToolResult.ok(data, metadata={"period": "campaign lifetime"}, focus=("automation", automation.id))
    return ToolResult.ok(campaign_analytics(ctx.db, now=ctx.now), metadata={"period": "all time"})


@tool(
    "get_revenue_analytics",
    group=GROUP,
    risk=Risk.READ,
    description=(
        "Revenue in a period: total order revenue, revenue attributed to campaigns (last-touch "
        "attribution), Smart Reorder's share, reactivations, and revenue on orders using each "
        "coupon code. source=smart_reorder restricts attribution to Smart Reorder."
    ),
    properties={"period": PERIOD, "source": {"type": "string", "enum": ["all", "smart_reorder", "campaigns"]}},
)
def get_revenue_analytics(ctx: ToolContext, period: str | None = None, source: str = "all") -> ToolResult:
    db = ctx.db
    start, end, meta = _period(ctx, period)
    orders = db.execute(
        select(func.count(Order.id), func.coalesce(func.sum(Order.total_amount), 0.0)).where(
            Order.status == OrderStatus.COMPLETED.value, Order.ordered_at >= start, Order.ordered_at < end
        )
    ).one()
    attribution = (
        select(
            AttributionRecord.campaign_id,
            func.count(AttributionRecord.id),
            func.coalesce(func.sum(AttributionRecord.revenue), 0.0),
            func.sum(case((AttributionRecord.is_reactivation.is_(True), 1), else_=0)),
        )
        .join(Order, Order.id == AttributionRecord.order_id)
        .where(Order.ordered_at >= start, Order.ordered_at < end)
        .group_by(AttributionRecord.campaign_id)
    )
    smart_ids = set(_smart_reorder_campaign_ids(ctx))
    names = dict(db.execute(select(Campaign.id, Campaign.name)).all())
    by_campaign = []
    for campaign_id, count, revenue, reactivations in db.execute(attribution).all():
        is_smart = campaign_id in smart_ids
        if source == "smart_reorder" and not is_smart:
            continue
        if source == "campaigns" and is_smart:
            continue
        by_campaign.append(
            {
                "campaign_id": campaign_id,
                "campaign": names.get(campaign_id, f"Campaign {campaign_id}"),
                "smart_reorder": is_smart,
                "attributed_orders": count,
                "attributed_revenue": money(revenue),
                "reactivations": int(reactivations or 0),
            }
        )
    by_campaign.sort(key=lambda r: -r["attributed_revenue"])
    coupons = db.execute(
        select(Order.coupon_code, func.count(Order.id), func.coalesce(func.sum(Order.total_amount), 0.0))
        .where(
            Order.coupon_code.is_not(None),
            Order.coupon_code != "",
            Order.status == OrderStatus.COMPLETED.value,
            Order.ordered_at >= start,
            Order.ordered_at < end,
        )
        .group_by(Order.coupon_code)
        .order_by(func.sum(Order.total_amount).desc())
    ).all()
    attributed_total = sum(r["attributed_revenue"] for r in by_campaign)
    return ToolResult.ok(
        {
            "total_orders": orders[0],
            "total_revenue": money(orders[1]),
            "attributed_revenue": money(attributed_total),
            "attributed_orders": sum(r["attributed_orders"] for r in by_campaign),
            "reactivations": sum(r["reactivations"] for r in by_campaign),
            "attribution_model": "last touch within each campaign's attribution window",
            "by_campaign": by_campaign[:20],
            "coupon_revenue": [
                {"coupon_code": code, "orders": n, "revenue": money(rev)} for code, n, rev in coupons[:20]
            ],
            "source": source,
        },
        metadata=meta,
    )


@tool(
    "get_conversion_analytics",
    group=GROUP,
    risk=Risk.READ,
    description="Messages sent vs attributed conversions in a period, by channel.",
    properties={"period": PERIOD},
)
def get_conversion_analytics(ctx: ToolContext, period: str | None = None) -> ToolResult:
    db = ctx.db
    start, end, meta = _period(ctx, period)
    sent = dict(
        db.execute(
            select(CommunicationEvent.channel, func.count())
            .where(
                CommunicationEvent.event_type.like("%_SENT"),
                CommunicationEvent.occurred_at >= start,
                CommunicationEvent.occurred_at < end,
            )
            .group_by(CommunicationEvent.channel)
        ).all()
    )
    conversions = db.execute(
        select(func.count(AttributionRecord.id), func.coalesce(func.sum(AttributionRecord.revenue), 0.0))
        .join(Order, Order.id == AttributionRecord.order_id)
        .where(Order.ordered_at >= start, Order.ordered_at < end)
    ).one()
    total_sent = sum(sent.values())
    return ToolResult.ok(
        {
            "messages_sent_by_channel": sent,
            "messages_sent": total_sent,
            "attributed_conversions": conversions[0],
            "attributed_revenue": money(conversions[1]),
            "conversion_rate": round(conversions[0] / total_sent, 4) if total_sent else None,
        },
        metadata=meta,
    )


@tool(
    "get_segment_analytics",
    group=GROUP,
    risk=Risk.READ,
    description="A segment's size, value, recency, lifecycle mix and churn-risk mix, plus its orders in a period.",
    properties={"segment_id": {"type": "integer"}, "name": {"type": "string"}, "period": PERIOD},
)
def get_segment_analytics(ctx: ToolContext, segment_id: int | None = None, name: str | None = None,
                          period: str | None = None) -> ToolResult:
    db = ctx.db
    segment = segment_for(ctx, segment_id, name)
    start, end, meta = _period(ctx, period)
    members = select(CustomerSegment.customer_id).where(CustomerSegment.segment_id == segment.id)
    stats = db.execute(
        select(
            func.count(CustomerMetrics.id),
            func.coalesce(func.sum(CustomerMetrics.lifetime_revenue), 0.0),
            func.avg(CustomerMetrics.average_order_value),
            func.avg(CustomerMetrics.days_since_last_order),
        ).where(CustomerMetrics.customer_id.in_(members))
    ).one()
    stages = dict(
        db.execute(
            select(Customer.lifecycle_stage, func.count()).where(Customer.id.in_(members)).group_by(Customer.lifecycle_stage)
        ).all()
    )
    churn = dict(
        db.execute(
            select(ChurnScore.risk_band, func.count()).where(ChurnScore.customer_id.in_(members)).group_by(ChurnScore.risk_band)
        ).all()
    )
    period_orders = db.execute(
        select(func.count(Order.id), func.coalesce(func.sum(Order.total_amount), 0.0)).where(
            Order.customer_id.in_(members),
            Order.status == OrderStatus.COMPLETED.value,
            Order.ordered_at >= start,
            Order.ordered_at < end,
        )
    ).one()
    return ToolResult.ok(
        {
            "segment": segment.name,
            "members": segment.member_count,
            "lifetime_revenue": money(stats[1]),
            "average_order_value": money(stats[2]),
            "average_days_since_last_order": round(float(stats[3]), 1) if stats[3] is not None else None,
            "lifecycle_mix": stages,
            "churn_risk_mix": churn,
            "orders_in_period": period_orders[0],
            "revenue_in_period": money(period_orders[1]),
        },
        metadata=meta,
        focus=("segment", segment.id),
    )


@tool(
    "get_prediction_accuracy",
    group=GROUP,
    risk=Risk.READ,
    description="How Smart Reorder predictions turned out: hit rate, ordered-at-all rate, median error, pending.",
    properties={"days": {"type": "integer", "description": "Look-back window, default 90."}, "automation_id": {"type": "integer"}},
)
def get_prediction_accuracy(ctx: ToolContext, days: int = 90, automation_id: int | None = None) -> ToolResult:
    from app.services.prediction_accuracy import accuracy_report, pending_count

    report = accuracy_report(ctx.db, automation_id=automation_id, since=ctx.now - timedelta(days=days))
    data = report.as_dict()
    data["pending"] = pending_count(ctx.db, automation_id=automation_id)
    return ToolResult.ok(data, metadata={"period": f"the last {days} days"})


@tool(
    "get_message_delivery_analytics",
    group=GROUP,
    risk=Risk.READ,
    description="Message events (sent, delivered, failed, opted out…) by channel, and Smart Reorder queue outcomes, in a period.",
    properties={"period": PERIOD},
)
def get_message_delivery_analytics(ctx: ToolContext, period: str | None = None) -> ToolResult:
    db = ctx.db
    start, end, meta = _period(ctx, period)
    events = db.execute(
        select(CommunicationEvent.channel, CommunicationEvent.event_type, func.count())
        .where(CommunicationEvent.occurred_at >= start, CommunicationEvent.occurred_at < end)
        .group_by(CommunicationEvent.channel, CommunicationEvent.event_type)
    ).all()
    by_channel: dict[str, dict[str, int]] = {}
    for channel, event_type, count in events:
        by_channel.setdefault(channel, {})[event_type] = count
    queue = dict(
        db.execute(
            select(ScheduledMessage.status, func.count())
            .where(ScheduledMessage.scheduled_at >= start, ScheduledMessage.scheduled_at < end)
            .group_by(ScheduledMessage.status)
        ).all()
    )
    cancellations = dict(
        db.execute(
            select(ScheduledMessage.cancellation_reason, func.count())
            .where(
                ScheduledMessage.scheduled_at >= start,
                ScheduledMessage.scheduled_at < end,
                ScheduledMessage.cancellation_reason.is_not(None),
            )
            .group_by(ScheduledMessage.cancellation_reason)
        ).all()
    )
    return ToolResult.ok(
        {"events_by_channel": by_channel, "smart_reorder_queue": queue, "smart_reorder_cancellations": cancellations},
        metadata=meta,
    )
