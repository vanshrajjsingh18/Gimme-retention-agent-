"""Bridges the pure scoring engines to the database.

Reads orders and communication history, runs metrics -> lifecycle -> churn ->
NBA, and persists the results. RFM is population-scoped so it runs as a
separate whole-population pass.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.analytics.metrics import MetricResult, OrderFact, compute_metrics
from app.churn.engine import ChurnResult, score_churn
from app.core.enums import (
    Channel,
    ChurnRiskBand,
    EventType,
    LifecycleStage,
    NextBestAction,
    OrderStatus,
)
from app.models.base import utcnow
from app.models.entities import (
    ChurnScore,
    CommunicationEvent,
    Customer,
    CustomerLifecycleHistory,
    CustomerMetrics,
    Message,
    Order,
    Recommendation,
    RfmScore,
    SuppressionList,
)
from app.recommendations.engine import CustomerContext, RecommendationResult, recommend
from app.rfm.engine import RfmInput, score_population
from app.services.lifecycle import (
    DEFAULT_THRESHOLDS,
    LifecycleResult,
    classify_lifecycle,
    detect_reactivation_from_history,
    expected_cycle_days,
)

logger = logging.getLogger(__name__)

DEFAULT_FREQUENCY_CAP_30D = 4


class CustomerIntelligence:
    """The full computed picture for one customer."""

    def __init__(
        self,
        customer: Customer,
        metrics: MetricResult,
        lifecycle: LifecycleResult,
        churn: ChurnResult,
        recommendation: RecommendationResult,
        engagement: dict,
    ) -> None:
        self.customer = customer
        self.metrics = metrics
        self.lifecycle = lifecycle
        self.churn = churn
        self.recommendation = recommendation
        self.engagement = engagement


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------
def load_order_facts(db: Session, customer_id: int) -> list[OrderFact]:
    orders = (
        db.execute(
            select(Order)
            .options(selectinload(Order.items))
            .where(Order.customer_id == customer_id)
            .order_by(Order.ordered_at)
        )
        .scalars()
        .all()
    )
    return [
        OrderFact(
            ordered_at=o.ordered_at,
            total_amount=o.total_amount,
            discount_amount=o.discount_amount,
            status=o.status,
            items=[
                {
                    "category": i.category,
                    "brand": i.brand,
                    "product_name": i.product_name,
                    "quantity": i.quantity,
                }
                for i in o.items
            ],
        )
        for o in orders
    ]


def load_engagement(db: Session, customer_id: int, *, now: datetime | None = None) -> dict:
    """Message send / interaction counts used by engagement scoring and caps."""
    now = now or utcnow()
    cutoff_90 = now - timedelta(days=90)
    cutoff_30 = now - timedelta(days=30)
    cutoff_7 = now - timedelta(days=7)

    rows = db.execute(
        select(CommunicationEvent.event_type, CommunicationEvent.occurred_at).where(
            CommunicationEvent.customer_id == customer_id,
            CommunicationEvent.occurred_at >= cutoff_90,
        )
    ).all()

    sent_types = {
        EventType.EMAIL_SENT.value,
        EventType.SMS_SENT.value,
        EventType.WHATSAPP_SENT.value,
    }
    counts = {
        "messages_sent_90d": 0,
        "messages_opened_90d": 0,
        "messages_clicked_90d": 0,
        "messages_replied_90d": 0,
        "messages_last_30d": 0,
        "messages_last_7d": 0,
    }
    for event_type, occurred_at in rows:
        if event_type in sent_types:
            counts["messages_sent_90d"] += 1
            if occurred_at >= cutoff_30:
                counts["messages_last_30d"] += 1
            if occurred_at >= cutoff_7:
                counts["messages_last_7d"] += 1
        elif event_type in (EventType.EMAIL_OPENED.value, EventType.WHATSAPP_READ.value):
            counts["messages_opened_90d"] += 1
        elif event_type == EventType.EMAIL_CLICKED.value:
            counts["messages_clicked_90d"] += 1
        elif event_type == EventType.WHATSAPP_REPLIED.value:
            counts["messages_replied_90d"] += 1
    return counts


def load_suppressed_channels(db: Session, customer_id: int) -> set[str]:
    rows = db.execute(
        select(SuppressionList.channel).where(
            SuppressionList.customer_id == customer_id, SuppressionList.active.is_(True)
        )
    ).all()
    return {r[0] for r in rows}


# --------------------------------------------------------------------------
# Computing
# --------------------------------------------------------------------------
def compute_intelligence(
    db: Session, customer: Customer, *, now: datetime | None = None
) -> CustomerIntelligence:
    """Run every engine for one customer without writing anything."""
    now = now or utcnow()
    orders = load_order_facts(db, customer.id)
    engagement = load_engagement(db, customer.id, now=now)
    metrics = compute_metrics(orders, now=now, engagement=engagement)

    completed_dates = [
        o.ordered_at for o in orders if o.status == OrderStatus.COMPLETED.value
    ]
    had_lapse = detect_reactivation_from_history(completed_dates) and (
        metrics.days_since_last_order is not None
        and metrics.days_since_last_order <= DEFAULT_THRESHOLDS.reactivation_recent_days
    )

    lifecycle = classify_lifecycle(
        metrics, signup_date=customer.signup_date, now=now, had_lapse=had_lapse
    )
    cycle, _ = expected_cycle_days(metrics)

    tenure_days = (now - customer.signup_date).days if customer.signup_date else None
    is_new = lifecycle.stage == LifecycleStage.NEW
    churn = score_churn(
        metrics,
        expected_cycle_days=cycle,
        is_new_customer=is_new,
        tenure_days=tenure_days,
        messages_sent_90d=engagement["messages_sent_90d"],
    )

    suppressed_channels = load_suppressed_channels(db, customer.id)
    recommendation = recommend(
        CustomerContext(
            lifecycle_stage=lifecycle.stage,
            metrics=metrics,
            churn=churn,
            expected_cycle_days=cycle,
            is_suppressed=customer.is_suppressed or "ALL" in suppressed_channels,
            marketing_consent=customer.marketing_consent,
            email_consent=customer.email_consent,
            sms_consent=customer.sms_consent,
            whatsapp_consent=customer.whatsapp_consent,
            preferred_channel=_safe_channel(customer.preferred_channel),
            messages_last_30d=engagement["messages_last_30d"],
            frequency_cap_30d=DEFAULT_FREQUENCY_CAP_30D,
        )
    )
    return CustomerIntelligence(customer, metrics, lifecycle, churn, recommendation, engagement)


def _safe_channel(value: str | None) -> Channel:
    try:
        return Channel(value)
    except (ValueError, TypeError):
        return Channel.EMAIL


# --------------------------------------------------------------------------
# Persisting
# --------------------------------------------------------------------------
def persist_intelligence(
    db: Session, intel: CustomerIntelligence, *, now: datetime | None = None
) -> None:
    """Write metrics, lifecycle transition, churn and recommendation rows."""
    now = now or utcnow()
    customer = intel.customer
    m = intel.metrics

    row = db.execute(
        select(CustomerMetrics).where(CustomerMetrics.customer_id == customer.id)
    ).scalar_one_or_none()
    if row is None:
        row = CustomerMetrics(customer_id=customer.id)
        db.add(row)

    for field_name in (
        "total_orders",
        "completed_orders",
        "cancelled_orders",
        "lifetime_revenue",
        "average_order_value",
        "total_units",
        "first_order_at",
        "last_order_at",
        "days_since_last_order",
        "days_since_first_order",
        "average_purchase_interval_days",
        "median_purchase_interval_days",
        "purchase_frequency_per_month",
        "discount_dependency",
        "orders_last_30d",
        "orders_last_90d",
        "orders_prev_90d",
        "orders_last_365d",
        "revenue_last_90d",
        "revenue_prev_90d",
        "spend_trend",
        "frequency_trend",
        "preferred_categories",
        "preferred_brands",
        "top_products",
        "typical_order_weekday",
        "typical_order_hour",
        "estimated_ltv",
        "engagement_score",
    ):
        setattr(row, field_name, getattr(m, field_name))
    row.messages_received_30d = intel.engagement["messages_last_30d"]
    row.messages_opened_90d = intel.engagement["messages_opened_90d"]
    row.messages_sent_90d = intel.engagement["messages_sent_90d"]
    row.calculated_at = now

    # Lifecycle transition (only recorded when the stage actually changes).
    new_stage = intel.lifecycle.stage.value
    if customer.lifecycle_stage != new_stage:
        db.add(
            CustomerLifecycleHistory(
                customer_id=customer.id,
                from_stage=customer.lifecycle_stage,
                to_stage=new_stage,
                reason=intel.lifecycle.reason,
                changed_at=now,
            )
        )
        customer.lifecycle_stage = new_stage
    customer.lifecycle_updated_at = now

    # Churn
    churn_row = db.execute(
        select(ChurnScore).where(ChurnScore.customer_id == customer.id)
    ).scalar_one_or_none()
    previous = churn_row.score if churn_row else None
    if churn_row is None:
        churn_row = ChurnScore(customer_id=customer.id)
        db.add(churn_row)
    churn_row.previous_score = previous
    churn_row.score = intel.churn.score
    churn_row.risk_band = intel.churn.risk_band.value
    churn_row.factors = intel.churn.factors_as_dicts()
    churn_row.explanation = intel.churn.explanation
    churn_row.revenue_at_risk = intel.churn.revenue_at_risk
    churn_row.calculated_at = now

    # Recommendation
    rec_row = db.execute(
        select(Recommendation).where(Recommendation.customer_id == customer.id)
    ).scalar_one_or_none()
    if rec_row is None:
        rec_row = Recommendation(customer_id=customer.id)
        db.add(rec_row)
    rec_row.action = intel.recommendation.action.value
    rec_row.priority = intel.recommendation.priority
    rec_row.reason_codes = intel.recommendation.reason_codes
    rec_row.explanation = intel.recommendation.explanation
    rec_row.recommended_channel = intel.recommendation.recommended_channel.value
    rec_row.suggested_products = intel.recommendation.suggested_products
    rec_row.calculated_at = now


def refresh_customer(
    db: Session, customer: Customer, *, now: datetime | None = None, commit: bool = True
) -> CustomerIntelligence:
    """Recompute and persist intelligence for a single customer."""
    intel = compute_intelligence(db, customer, now=now)
    persist_intelligence(db, intel, now=now)
    if commit:
        db.commit()
    return intel


def refresh_all_customers(
    db: Session, *, now: datetime | None = None, batch_size: int = 200
) -> dict:
    """Recompute intelligence for every customer, then RFM for the population."""
    now = now or utcnow()
    customer_ids = db.execute(select(Customer.id).order_by(Customer.id)).scalars().all()
    processed = 0
    for start in range(0, len(customer_ids), batch_size):
        chunk = customer_ids[start : start + batch_size]
        customers = (
            db.execute(select(Customer).where(Customer.id.in_(chunk))).scalars().all()
        )
        for customer in customers:
            intel = compute_intelligence(db, customer, now=now)
            persist_intelligence(db, intel, now=now)
            processed += 1
        db.commit()

    rfm_count = refresh_rfm(db, now=now)
    return {"customers_processed": processed, "rfm_scored": rfm_count, "calculated_at": now}


def refresh_rfm(db: Session, *, now: datetime | None = None) -> int:
    """Score the whole population; quantiles require every customer at once."""
    now = now or utcnow()
    rows = db.execute(
        select(
            CustomerMetrics.customer_id,
            CustomerMetrics.days_since_last_order,
            CustomerMetrics.completed_orders,
            CustomerMetrics.lifetime_revenue,
        )
    ).all()
    if not rows:
        return 0

    inputs = [
        RfmInput(
            customer_id=cid,
            recency_days=days,
            frequency=orders or 0,
            monetary=float(revenue or 0.0),
        )
        for cid, days, orders, revenue in rows
    ]
    results = score_population(inputs)

    existing = {
        r.customer_id: r for r in db.execute(select(RfmScore)).scalars().all()
    }
    for res in results:
        row = existing.get(res.customer_id)
        if row is None:
            row = RfmScore(customer_id=res.customer_id)
            db.add(row)
        row.recency_score = res.recency_score
        row.frequency_score = res.frequency_score
        row.monetary_score = res.monetary_score
        row.rfm_cell = res.rfm_cell
        row.rfm_total = res.rfm_total
        row.rfm_segment = res.rfm_segment
        row.recency_days = res.recency_days
        row.frequency_value = res.frequency_value
        row.monetary_value = res.monetary_value
        row.calculated_at = now
    db.commit()
    return len(results)


# --------------------------------------------------------------------------
# Flat customer view (used by segmentation and the customer list API)
# --------------------------------------------------------------------------
def build_customer_view(
    customer: Customer,
    metrics: CustomerMetrics | None,
    churn: ChurnScore | None,
    rfm: RfmScore | None,
    recommendation: Recommendation | None,
) -> dict:
    """Flatten a customer and their computed rows into one dict.

    This is the shape segment rules evaluate against and the shape the
    customer list endpoint returns.
    """
    view: dict = {
        "id": customer.id,
        "external_id": customer.external_id,
        "first_name": customer.first_name,
        "last_name": customer.last_name,
        "full_name": customer.full_name,
        "email": customer.email,
        "phone": customer.phone,
        "city": customer.city,
        "region": customer.region,
        "postcode": customer.postcode,
        "country": customer.country,
        "signup_date": customer.signup_date,
        "acquisition_source": customer.acquisition_source,
        "preferred_channel": customer.preferred_channel,
        "age_verified": customer.age_verified,
        "date_of_birth": customer.date_of_birth,
        "marketing_consent": customer.marketing_consent,
        "email_consent": customer.email_consent,
        "sms_consent": customer.sms_consent,
        "whatsapp_consent": customer.whatsapp_consent,
        "is_suppressed": customer.is_suppressed,
        "lifecycle_stage": customer.lifecycle_stage,
        "lifecycle_updated_at": customer.lifecycle_updated_at,
    }

    if metrics:
        view.update(
            {
                "total_orders": metrics.total_orders,
                "completed_orders": metrics.completed_orders,
                "cancelled_orders": metrics.cancelled_orders,
                "lifetime_revenue": metrics.lifetime_revenue,
                "average_order_value": metrics.average_order_value,
                "total_units": metrics.total_units,
                "first_order_at": metrics.first_order_at,
                "last_order_at": metrics.last_order_at,
                "days_since_last_order": metrics.days_since_last_order,
                "days_since_first_order": metrics.days_since_first_order,
                "average_purchase_interval_days": metrics.average_purchase_interval_days,
                "median_purchase_interval_days": metrics.median_purchase_interval_days,
                "purchase_frequency_per_month": metrics.purchase_frequency_per_month,
                "discount_dependency": metrics.discount_dependency,
                "orders_last_30d": metrics.orders_last_30d,
                "orders_last_90d": metrics.orders_last_90d,
                "revenue_last_90d": metrics.revenue_last_90d,
                "spend_trend": metrics.spend_trend,
                "frequency_trend": metrics.frequency_trend,
                "preferred_categories": metrics.preferred_categories,
                "preferred_brands": metrics.preferred_brands,
                "top_products": metrics.top_products,
                "typical_order_weekday": metrics.typical_order_weekday,
                "typical_order_hour": metrics.typical_order_hour,
                "estimated_ltv": metrics.estimated_ltv,
                "engagement_score": metrics.engagement_score,
            }
        )
    else:
        view.update(
            {
                "total_orders": 0,
                "completed_orders": 0,
                "cancelled_orders": 0,
                "lifetime_revenue": 0.0,
                "average_order_value": 0.0,
                "days_since_last_order": None,
                "preferred_categories": [],
                "preferred_brands": [],
                "top_products": [],
                "estimated_ltv": 0.0,
                "engagement_score": 0.0,
            }
        )

    view["churn_score"] = churn.score if churn else 0.0
    view["churn_risk_band"] = churn.risk_band if churn else ChurnRiskBand.LOW.value
    view["churn_explanation"] = churn.explanation if churn else ""
    view["churn_factors"] = churn.factors if churn else []
    view["revenue_at_risk"] = churn.revenue_at_risk if churn else 0.0

    view["rfm_cell"] = rfm.rfm_cell if rfm else None
    view["rfm_segment"] = rfm.rfm_segment if rfm else None
    view["rfm_total"] = rfm.rfm_total if rfm else None
    view["recency_score"] = rfm.recency_score if rfm else None
    view["frequency_score"] = rfm.frequency_score if rfm else None
    view["monetary_score"] = rfm.monetary_score if rfm else None

    view["recommended_action"] = (
        recommendation.action if recommendation else NextBestAction.NO_ACTION.value
    )
    view["recommendation_explanation"] = recommendation.explanation if recommendation else ""
    view["recommendation_reason_codes"] = recommendation.reason_codes if recommendation else []
    view["recommended_channel"] = (
        recommendation.recommended_channel if recommendation else Channel.EMAIL.value
    )
    view["suggested_products"] = recommendation.suggested_products if recommendation else []
    return view


def load_customer_views(
    db: Session, customer_ids: list[int] | None = None
) -> list[dict]:
    """Load flattened views, joined in bulk to avoid per-customer queries."""
    stmt = (
        select(Customer, CustomerMetrics, ChurnScore, RfmScore, Recommendation)
        .outerjoin(CustomerMetrics, CustomerMetrics.customer_id == Customer.id)
        .outerjoin(ChurnScore, ChurnScore.customer_id == Customer.id)
        .outerjoin(RfmScore, RfmScore.customer_id == Customer.id)
        .outerjoin(Recommendation, Recommendation.customer_id == Customer.id)
    )
    if customer_ids is not None:
        if not customer_ids:
            return []
        stmt = stmt.where(Customer.id.in_(customer_ids))
    rows = db.execute(stmt).all()
    return [build_customer_view(c, m, ch, r, rec) for c, m, ch, r, rec in rows]


def customer_count(db: Session) -> int:
    return db.execute(select(func.count(Customer.id))).scalar_one()
