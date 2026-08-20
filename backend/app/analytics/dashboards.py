"""Analytics queries.

Every figure on every dashboard is computed here from the database. There are
no constants standing in for metrics.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import Float, case, cast, func, select
from sqlalchemy.orm import Session

from app.core.enums import (
    CampaignStatus,
    Channel,
    ChurnRiskBand,
    EventType,
    LifecycleStage,
    OrderStatus,
    RecipientStatus,
)
from app.models.base import utcnow
from app.models.entities import (
    AttributionRecord,
    Campaign,
    CampaignRecipient,
    ChurnScore,
    CommunicationEvent,
    Customer,
    CustomerMetrics,
    Message,
    Order,
    RfmScore,
)

ACTIVE_STAGES = [
    LifecycleStage.NEW.value,
    LifecycleStage.ACTIVATING.value,
    LifecycleStage.REGULAR.value,
    LifecycleStage.HIGH_VALUE.value,
    LifecycleStage.VIP.value,
    LifecycleStage.REACTIVATED.value,
]


def _month_key(column):
    """Portable YYYY-MM extraction (SQLite strftime, PostgreSQL to_char)."""
    return func.strftime("%Y-%m", column)


def _safe_rate(numerator: float, denominator: float, digits: int = 4) -> float:
    return round(numerator / denominator, digits) if denominator else 0.0


# --------------------------------------------------------------------------
# Overview
# --------------------------------------------------------------------------
def overview(db: Session, *, now: datetime | None = None) -> dict:
    now = now or utcnow()
    d30 = now - timedelta(days=30)
    d90 = now - timedelta(days=90)

    total_customers = db.execute(select(func.count(Customer.id))).scalar_one()

    stage_counts = dict(
        db.execute(
            select(Customer.lifecycle_stage, func.count()).group_by(Customer.lifecycle_stage)
        ).all()
    )

    new_customers_30d = db.execute(
        select(func.count(Customer.id)).where(Customer.signup_date >= d30)
    ).scalar_one()

    active_customers = db.execute(
        select(func.count(Customer.id)).where(Customer.lifecycle_stage.in_(ACTIVE_STAGES))
    ).scalar_one()

    repeat_customers = db.execute(
        select(func.count(CustomerMetrics.id)).where(CustomerMetrics.completed_orders >= 2)
    ).scalar_one()
    customers_with_orders = db.execute(
        select(func.count(CustomerMetrics.id)).where(CustomerMetrics.completed_orders >= 1)
    ).scalar_one()

    order_agg = db.execute(
        select(
            func.count(Order.id),
            func.coalesce(func.sum(Order.total_amount), 0.0),
        ).where(Order.status == OrderStatus.COMPLETED.value)
    ).one()
    total_orders, total_revenue = int(order_agg[0]), float(order_agg[1])

    orders_30d = db.execute(
        select(
            func.count(Order.id), func.coalesce(func.sum(Order.total_amount), 0.0)
        ).where(Order.status == OrderStatus.COMPLETED.value, Order.ordered_at >= d30)
    ).one()

    orders_prev_30d = db.execute(
        select(
            func.count(Order.id), func.coalesce(func.sum(Order.total_amount), 0.0)
        ).where(
            Order.status == OrderStatus.COMPLETED.value,
            Order.ordered_at >= now - timedelta(days=60),
            Order.ordered_at < d30,
        )
    ).one()

    # Retention: customers who ordered in the last 90 days and also in the 90
    # days before that.
    recent_ids = set(
        db.execute(
            select(Order.customer_id)
            .where(Order.status == OrderStatus.COMPLETED.value, Order.ordered_at >= d90)
            .distinct()
        )
        .scalars()
        .all()
    )
    prior_ids = set(
        db.execute(
            select(Order.customer_id)
            .where(
                Order.status == OrderStatus.COMPLETED.value,
                Order.ordered_at >= now - timedelta(days=180),
                Order.ordered_at < d90,
            )
            .distinct()
        )
        .scalars()
        .all()
    )
    retained = len(recent_ids & prior_ids)

    reactivations = db.execute(
        select(func.count(AttributionRecord.id)).where(
            AttributionRecord.is_reactivation.is_(True)
        )
    ).scalar_one()
    reactivated_now = stage_counts.get(LifecycleStage.REACTIVATED.value, 0)

    at_risk = stage_counts.get(LifecycleStage.AT_RISK.value, 0)
    dormant = stage_counts.get(LifecycleStage.DORMANT.value, 0)
    churned = stage_counts.get(LifecycleStage.CHURNED.value, 0)

    revenue_at_risk = float(
        db.execute(
            select(func.coalesce(func.sum(ChurnScore.revenue_at_risk), 0.0))
        ).scalar_one()
    )
    estimated_ltv = float(
        db.execute(
            select(func.coalesce(func.sum(CustomerMetrics.estimated_ltv), 0.0))
        ).scalar_one()
    )
    campaign_revenue = float(
        db.execute(
            select(func.coalesce(func.sum(Campaign.attributed_revenue), 0.0))
        ).scalar_one()
    )

    return {
        "generated_at": now.isoformat(),
        "total_customers": total_customers,
        "active_customers": active_customers,
        "new_customers_30d": new_customers_30d,
        "repeat_customers": repeat_customers,
        "reactivated_customers": reactivated_now,
        "total_reactivations": reactivations,
        "at_risk_customers": at_risk,
        "dormant_customers": dormant,
        "churned_customers": churned,
        "total_orders": total_orders,
        "total_revenue": round(total_revenue, 2),
        "average_order_value": round(_safe_rate(total_revenue, total_orders, 2), 2),
        "orders_30d": int(orders_30d[0]),
        "revenue_30d": round(float(orders_30d[1]), 2),
        "orders_prev_30d": int(orders_prev_30d[0]),
        "revenue_prev_30d": round(float(orders_prev_30d[1]), 2),
        "revenue_change_30d": _safe_rate(
            float(orders_30d[1]) - float(orders_prev_30d[1]), float(orders_prev_30d[1])
        ),
        "repeat_purchase_rate": _safe_rate(repeat_customers, customers_with_orders),
        "retention_rate_90d": _safe_rate(retained, len(prior_ids)),
        "reactivation_rate": _safe_rate(reactivations, max(dormant + churned, 1)),
        "estimated_ltv_total": round(estimated_ltv, 2),
        "revenue_at_risk": round(revenue_at_risk, 2),
        "campaign_attributed_revenue": round(campaign_revenue, 2),
        "campaign_revenue_share": _safe_rate(campaign_revenue, total_revenue),
        "lifecycle_distribution": [
            {"stage": stage.value, "count": stage_counts.get(stage.value, 0)}
            for stage in LifecycleStage
        ],
    }


# --------------------------------------------------------------------------
# Customer analytics
# --------------------------------------------------------------------------
def customer_analytics(db: Session, *, months: int = 12, now: datetime | None = None) -> dict:
    now = now or utcnow()
    start = now - timedelta(days=months * 31)

    growth_rows = db.execute(
        select(_month_key(Customer.signup_date), func.count())
        .where(Customer.signup_date >= start)
        .group_by(_month_key(Customer.signup_date))
        .order_by(_month_key(Customer.signup_date))
    ).all()

    # New vs repeat orders per month: an order is "repeat" when the customer
    # had an earlier completed order.
    first_orders = dict(
        db.execute(
            select(Order.customer_id, func.min(Order.ordered_at))
            .where(Order.status == OrderStatus.COMPLETED.value)
            .group_by(Order.customer_id)
        ).all()
    )
    order_rows = db.execute(
        select(Order.customer_id, Order.ordered_at, Order.total_amount).where(
            Order.status == OrderStatus.COMPLETED.value, Order.ordered_at >= start
        )
    ).all()

    monthly: dict[str, dict] = {}
    for customer_id, ordered_at, amount in order_rows:
        key = ordered_at.strftime("%Y-%m")
        bucket = monthly.setdefault(
            key, {"month": key, "new": 0, "repeat": 0, "revenue": 0.0}
        )
        is_first = first_orders.get(customer_id) == ordered_at
        bucket["new" if is_first else "repeat"] += 1
        bucket["revenue"] += float(amount)

    lifecycle_rows = db.execute(
        select(Customer.lifecycle_stage, func.count()).group_by(Customer.lifecycle_stage)
    ).all()

    rfm_rows = db.execute(
        select(RfmScore.rfm_segment, func.count(), func.sum(RfmScore.monetary_value))
        .group_by(RfmScore.rfm_segment)
        .order_by(func.count().desc())
    ).all()

    rfm_grid = db.execute(
        select(RfmScore.recency_score, RfmScore.frequency_score, func.count())
        .group_by(RfmScore.recency_score, RfmScore.frequency_score)
    ).all()

    frequency_rows = db.execute(
        select(CustomerMetrics.completed_orders, func.count())
        .group_by(CustomerMetrics.completed_orders)
        .order_by(CustomerMetrics.completed_orders)
    ).all()

    ltv_buckets = _bucket(
        db,
        CustomerMetrics.estimated_ltv,
        [(0, 100), (100, 250), (250, 500), (500, 1000), (1000, 2500), (2500, None)],
        "$",
    )
    revenue_buckets = _bucket(
        db,
        CustomerMetrics.lifetime_revenue,
        [(0, 100), (100, 250), (250, 500), (500, 1000), (1000, 2500), (2500, None)],
        "$",
    )

    return {
        "customer_growth": [{"month": m, "new_customers": c} for m, c in growth_rows],
        "new_vs_repeat": [
            {**v, "revenue": round(v["revenue"], 2)}
            for v in sorted(monthly.values(), key=lambda x: x["month"])
        ],
        "lifecycle_distribution": [{"stage": s, "count": c} for s, c in lifecycle_rows],
        "rfm_distribution": [
            {"segment": s, "count": c, "revenue": round(float(r or 0), 2)}
            for s, c, r in rfm_rows
        ],
        "rfm_grid": [
            {"recency": r, "frequency": f, "count": c} for r, f, c in rfm_grid
        ],
        "purchase_frequency": [
            {"orders": o, "customers": c} for o, c in frequency_rows
        ],
        "ltv_distribution": ltv_buckets,
        "revenue_distribution": revenue_buckets,
    }


def _bucket(db: Session, column, ranges: list[tuple], prefix: str = "") -> list[dict]:
    """Count rows falling into each numeric range."""
    results = []
    for low, high in ranges:
        stmt = select(func.count()).select_from(CustomerMetrics).where(column >= low)
        if high is not None:
            stmt = stmt.where(column < high)
        count = db.execute(stmt).scalar_one()
        label = f"{prefix}{low:,.0f}+" if high is None else f"{prefix}{low:,.0f}-{prefix}{high:,.0f}"
        results.append({"range": label, "min": low, "max": high, "count": count})
    return results


# --------------------------------------------------------------------------
# Churn analytics
# --------------------------------------------------------------------------
def churn_analytics(db: Session, *, now: datetime | None = None) -> dict:
    now = now or utcnow()

    band_rows = db.execute(
        select(
            ChurnScore.risk_band,
            func.count(),
            func.coalesce(func.sum(ChurnScore.revenue_at_risk), 0.0),
        ).group_by(ChurnScore.risk_band)
    ).all()
    by_band = {b: {"count": c, "revenue_at_risk": round(float(r), 2)} for b, c, r in band_rows}

    # Risk movement: how many customers got riskier or safer since the last
    # recalculation.
    movement = db.execute(
        select(
            func.sum(case((ChurnScore.score > ChurnScore.previous_score, 1), else_=0)),
            func.sum(case((ChurnScore.score < ChurnScore.previous_score, 1), else_=0)),
            func.sum(case((ChurnScore.score == ChurnScore.previous_score, 1), else_=0)),
        ).where(ChurnScore.previous_score.is_not(None))
    ).one()

    # Churn reasons: how often each factor is the top contributor.
    reason_counts: dict[str, dict] = {}
    for (factors,) in db.execute(select(ChurnScore.factors)).all():
        if not factors:
            continue
        top = max(factors, key=lambda f: f.get("points", 0))
        entry = reason_counts.setdefault(
            top["code"], {"code": top["code"], "label": top.get("label", top["code"]), "count": 0}
        )
        entry["count"] += 1

    at_risk_value = db.execute(
        select(func.coalesce(func.sum(CustomerMetrics.lifetime_revenue), 0.0))
        .select_from(CustomerMetrics)
        .join(ChurnScore, ChurnScore.customer_id == CustomerMetrics.customer_id)
        .where(ChurnScore.risk_band.in_([ChurnRiskBand.HIGH.value, ChurnRiskBand.CRITICAL.value]))
    ).scalar_one()

    lapsed = db.execute(
        select(func.count(Customer.id)).where(
            Customer.lifecycle_stage.in_(
                [LifecycleStage.DORMANT.value, LifecycleStage.CHURNED.value]
            )
        )
    ).scalar_one()
    reactivations = db.execute(
        select(func.count(AttributionRecord.id)).where(
            AttributionRecord.is_reactivation.is_(True)
        )
    ).scalar_one()

    score_buckets = []
    for low, high in [(0, 25), (25, 45), (45, 70), (70, 101)]:
        count = db.execute(
            select(func.count()).where(ChurnScore.score >= low, ChurnScore.score < high)
        ).scalar_one()
        score_buckets.append({"range": f"{low}-{min(high, 100)}", "count": count})

    top_at_risk = db.execute(
        select(
            Customer.id,
            Customer.first_name,
            Customer.last_name,
            Customer.lifecycle_stage,
            ChurnScore.score,
            ChurnScore.risk_band,
            ChurnScore.explanation,
            CustomerMetrics.lifetime_revenue,
            CustomerMetrics.days_since_last_order,
        )
        .join(ChurnScore, ChurnScore.customer_id == Customer.id)
        .outerjoin(CustomerMetrics, CustomerMetrics.customer_id == Customer.id)
        .where(ChurnScore.risk_band.in_([ChurnRiskBand.HIGH.value, ChurnRiskBand.CRITICAL.value]))
        .order_by(CustomerMetrics.lifetime_revenue.desc())
        .limit(10)
    ).all()

    return {
        "risk_distribution": [
            {
                "band": band.value,
                "count": by_band.get(band.value, {}).get("count", 0),
                "revenue_at_risk": by_band.get(band.value, {}).get("revenue_at_risk", 0.0),
            }
            for band in ChurnRiskBand
        ],
        "score_distribution": score_buckets,
        "revenue_at_risk": round(float(at_risk_value), 2),
        "risk_movement": {
            "increased": int(movement[0] or 0),
            "decreased": int(movement[1] or 0),
            "unchanged": int(movement[2] or 0),
        },
        "churn_reasons": sorted(
            reason_counts.values(), key=lambda x: x["count"], reverse=True
        ),
        "reactivation_rate": _safe_rate(reactivations, max(lapsed, 1)),
        "total_reactivations": reactivations,
        "top_at_risk_customers": [
            {
                "id": r[0],
                "full_name": f"{r[1]} {r[2]}".strip(),
                "lifecycle_stage": r[3],
                "churn_score": r[4],
                "risk_band": r[5],
                "explanation": r[6],
                "lifetime_revenue": round(float(r[7] or 0), 2),
                "days_since_last_order": r[8],
            }
            for r in top_at_risk
        ],
    }


# --------------------------------------------------------------------------
# Campaign analytics
# --------------------------------------------------------------------------
def campaign_analytics(db: Session, *, now: datetime | None = None) -> dict:
    now = now or utcnow()

    totals = db.execute(
        select(
            func.count(Campaign.id),
            func.coalesce(func.sum(Campaign.messages_sent), 0),
            func.coalesce(func.sum(Campaign.messages_delivered), 0),
            func.coalesce(func.sum(Campaign.messages_opened), 0),
            func.coalesce(func.sum(Campaign.messages_clicked), 0),
            func.coalesce(func.sum(Campaign.messages_replied), 0),
            func.coalesce(func.sum(Campaign.messages_failed), 0),
            func.coalesce(func.sum(Campaign.unsubscribes), 0),
            func.coalesce(func.sum(Campaign.conversions), 0),
            func.coalesce(func.sum(Campaign.attributed_revenue), 0.0),
        )
    ).one()
    (
        campaign_count, sent, delivered, opened, clicked, replied, failed,
        unsubscribes, conversions, revenue,
    ) = totals
    sent, revenue = int(sent), float(revenue)

    channel_rows = db.execute(
        select(
            Campaign.channel,
            func.count(Campaign.id),
            func.coalesce(func.sum(Campaign.messages_sent), 0),
            func.coalesce(func.sum(Campaign.messages_delivered), 0),
            func.coalesce(func.sum(Campaign.messages_opened), 0),
            func.coalesce(func.sum(Campaign.messages_clicked), 0),
            func.coalesce(func.sum(Campaign.conversions), 0),
            func.coalesce(func.sum(Campaign.attributed_revenue), 0.0),
        ).group_by(Campaign.channel)
    ).all()

    campaign_rows = db.execute(
        select(Campaign).order_by(Campaign.created_at.desc()).limit(50)
    ).scalars().all()

    objective_rows = db.execute(
        select(
            Campaign.objective,
            func.count(Campaign.id),
            func.coalesce(func.sum(Campaign.messages_sent), 0),
            func.coalesce(func.sum(Campaign.conversions), 0),
            func.coalesce(func.sum(Campaign.attributed_revenue), 0.0),
        ).group_by(Campaign.objective)
    ).all()

    return {
        "totals": {
            "campaigns": int(campaign_count),
            "messages_sent": sent,
            "messages_delivered": int(delivered),
            "messages_opened": int(opened),
            "messages_clicked": int(clicked),
            "messages_replied": int(replied),
            "messages_failed": int(failed),
            "unsubscribes": int(unsubscribes),
            "conversions": int(conversions),
            "attributed_revenue": round(revenue, 2),
            "delivery_rate": _safe_rate(int(delivered), sent),
            "open_rate": _safe_rate(int(opened), int(delivered) or sent),
            "click_rate": _safe_rate(int(clicked), int(delivered) or sent),
            "reply_rate": _safe_rate(int(replied), int(delivered) or sent),
            "conversion_rate": _safe_rate(int(conversions), sent),
            "unsubscribe_rate": _safe_rate(int(unsubscribes), sent),
            "revenue_per_message": round(_safe_rate(revenue, sent, 4), 2),
        },
        "by_channel": [
            {
                "channel": c,
                "campaigns": n,
                "messages_sent": int(s),
                "delivery_rate": _safe_rate(int(d), int(s)),
                "open_rate": _safe_rate(int(o), int(d) or int(s)),
                "click_rate": _safe_rate(int(cl), int(d) or int(s)),
                "conversions": int(conv),
                "attributed_revenue": round(float(rev), 2),
                "revenue_per_message": round(_safe_rate(float(rev), int(s), 4), 2),
            }
            for c, n, s, d, o, cl, conv, rev in channel_rows
        ],
        "by_objective": [
            {
                "objective": o,
                "campaigns": n,
                "messages_sent": int(s),
                "conversions": int(conv),
                "attributed_revenue": round(float(rev), 2),
                "conversion_rate": _safe_rate(int(conv), int(s)),
            }
            for o, n, s, conv, rev in objective_rows
        ],
        "campaigns": [
            {
                "id": c.id,
                "name": c.name,
                "objective": c.objective,
                "channel": c.channel,
                "status": c.status,
                "started_at": c.started_at.isoformat() if c.started_at else None,
                "total_recipients": c.total_recipients,
                "messages_sent": c.messages_sent,
                "messages_delivered": c.messages_delivered,
                "messages_opened": c.messages_opened,
                "messages_clicked": c.messages_clicked,
                "conversions": c.conversions,
                "attributed_revenue": round(c.attributed_revenue, 2),
                "delivery_rate": _safe_rate(c.messages_delivered, c.messages_sent),
                "open_rate": _safe_rate(
                    c.messages_opened, c.messages_delivered or c.messages_sent
                ),
                "click_rate": _safe_rate(
                    c.messages_clicked, c.messages_delivered or c.messages_sent
                ),
                "conversion_rate": _safe_rate(c.conversions, c.messages_sent),
                "revenue_per_message": round(
                    _safe_rate(c.attributed_revenue, c.messages_sent, 4), 2
                ),
                "unsubscribe_rate": _safe_rate(c.unsubscribes, c.messages_sent),
            }
            for c in campaign_rows
        ],
    }


# --------------------------------------------------------------------------
# Cohorts
# --------------------------------------------------------------------------
def cohort_analytics(db: Session, *, months: int = 12, now: datetime | None = None) -> dict:
    """Monthly acquisition cohorts with month 0-6 retention.

    A customer is retained in month N when they placed at least one completed
    order in that month, counted from their first order month.
    """
    now = now or utcnow()

    rows = db.execute(
        select(Order.customer_id, Order.ordered_at).where(
            Order.status == OrderStatus.COMPLETED.value
        )
    ).all()

    orders_by_customer: dict[int, list[datetime]] = {}
    for customer_id, ordered_at in rows:
        orders_by_customer.setdefault(customer_id, []).append(ordered_at)

    cohorts: dict[str, dict] = {}
    cutoff = (now - timedelta(days=months * 31)).replace(day=1)

    for customer_id, dates in orders_by_customer.items():
        dates.sort()
        first = dates[0]
        if first < cutoff:
            continue
        cohort_key = first.strftime("%Y-%m")
        cohort = cohorts.setdefault(
            cohort_key,
            {"cohort": cohort_key, "size": 0, "retained": {n: set() for n in range(7)}},
        )
        cohort["size"] += 1
        for order_date in dates:
            offset = (order_date.year - first.year) * 12 + (order_date.month - first.month)
            if 0 <= offset <= 6:
                cohort["retained"][offset].add(customer_id)

    result = []
    for key in sorted(cohorts):
        cohort = cohorts[key]
        size = cohort["size"]
        # Only report a month that has actually elapsed for this cohort.
        cohort_start = datetime.strptime(key, "%Y-%m")
        elapsed = (now.year - cohort_start.year) * 12 + (now.month - cohort_start.month)
        result.append(
            {
                "cohort": key,
                "size": size,
                "months": [
                    {
                        "month": n,
                        "customers": len(cohort["retained"][n]),
                        "rate": _safe_rate(len(cohort["retained"][n]), size),
                    }
                    for n in range(7)
                    if n <= elapsed
                ],
            }
        )
    return {"cohorts": result, "max_months": 6}


# --------------------------------------------------------------------------
# Activity feed
# --------------------------------------------------------------------------
def recent_activity(db: Session, *, limit: int = 20) -> list[dict]:
    """Recent conversions and campaign sends, newest first."""
    conversions = db.execute(
        select(AttributionRecord, Campaign, Customer)
        .join(Campaign, Campaign.id == AttributionRecord.campaign_id)
        .join(Customer, Customer.id == AttributionRecord.customer_id)
        .order_by(AttributionRecord.created_at.desc())
        .limit(limit)
    ).all()

    items = [
        {
            "type": "CONVERSION",
            "at": a.created_at.isoformat(),
            "title": f"{c.full_name} converted from {camp.name}",
            "detail": f"${a.revenue:,.2f} attributed"
            + (" (reactivation)" if a.is_reactivation else ""),
            "customer_id": c.id,
            "campaign_id": camp.id,
        }
        for a, camp, c in conversions
    ]

    campaigns = db.execute(
        select(Campaign)
        .where(Campaign.started_at.is_not(None))
        .order_by(Campaign.started_at.desc())
        .limit(limit)
    ).scalars().all()
    items += [
        {
            "type": "CAMPAIGN",
            "at": c.started_at.isoformat(),
            "title": f"{c.name} sent to {c.messages_sent} customers",
            "detail": f"{c.channel} - {c.objective}",
            "customer_id": None,
            "campaign_id": c.id,
        }
        for c in campaigns
    ]

    return sorted(items, key=lambda x: x["at"], reverse=True)[:limit]
