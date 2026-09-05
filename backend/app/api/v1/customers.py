"""Customer list, search and Customer 360 profile."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import get_current_user, require_write
from app.automations.service import customer_history
from app.core.database import get_db
from app.core.enums import Channel, ConsentType, LifecycleStage
from app.models.base import utcnow
from app.models.entities import (
    AttributionRecord,
    AuditLog,
    Campaign,
    CampaignRecipient,
    ChurnScore,
    CommunicationEvent,
    ConsentEvent,
    Customer,
    CustomerLifecycleHistory,
    CustomerMetrics,
    CustomerSegment,
    Message,
    Order,
    Recommendation,
    RfmScore,
    Segment,
    SuppressionList,
    User,
)
from app.schemas.common import OperationResult, Page
from app.schemas.models import (
    CommunicationEventOut,
    ConsentUpdateRequest,
    CustomerDetail,
    CustomerSummary,
    LifecycleHistoryOut,
    MessageOut,
    OrderOut,
    SuppressRequest,
)
from app.services.intelligence import build_customer_view, refresh_customer
from app.services.lifecycle import expected_cycle_days

router = APIRouter()

SORT_FIELDS = {
    "lifetime_revenue": CustomerMetrics.lifetime_revenue,
    "churn_score": ChurnScore.score,
    "days_since_last_order": CustomerMetrics.days_since_last_order,
    "total_orders": CustomerMetrics.total_orders,
    "estimated_ltv": CustomerMetrics.estimated_ltv,
    "engagement_score": CustomerMetrics.engagement_score,
    "created_at": Customer.created_at,
    "full_name": Customer.first_name,
}


@router.get("/customers", response_model=Page[CustomerSummary], tags=["customers"])
def list_customers(
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
    search: str | None = Query(default=None, description="Name, email, phone or external ID."),
    lifecycle_stage: list[str] | None = Query(default=None),
    churn_risk_band: list[str] | None = Query(default=None),
    rfm_segment: str | None = None,
    segment_id: int | None = None,
    recommended_action: str | None = None,
    city: str | None = None,
    marketing_consent: bool | None = None,
    is_suppressed: bool | None = None,
    min_revenue: float | None = None,
    max_revenue: float | None = None,
    min_days_since_order: int | None = None,
    max_days_since_order: int | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=200),
    sort_by: str = Query(default="lifetime_revenue"),
    sort_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
) -> Page[CustomerSummary]:
    """Paginated, filterable customer list.

    Filtering and sorting happen in SQL so a large customer base does not have
    to be loaded into memory to render one page.
    """
    stmt = (
        select(Customer, CustomerMetrics, ChurnScore, RfmScore, Recommendation)
        .outerjoin(CustomerMetrics, CustomerMetrics.customer_id == Customer.id)
        .outerjoin(ChurnScore, ChurnScore.customer_id == Customer.id)
        .outerjoin(RfmScore, RfmScore.customer_id == Customer.id)
        .outerjoin(Recommendation, Recommendation.customer_id == Customer.id)
    )

    if search:
        term = f"%{search.strip().lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(Customer.first_name).like(term),
                func.lower(Customer.last_name).like(term),
                func.lower(Customer.email).like(term),
                func.lower(Customer.external_id).like(term),
                Customer.phone.like(term),
                func.lower(Customer.first_name + " " + Customer.last_name).like(term),
            )
        )
    if lifecycle_stage:
        stmt = stmt.where(Customer.lifecycle_stage.in_(lifecycle_stage))
    if churn_risk_band:
        stmt = stmt.where(ChurnScore.risk_band.in_(churn_risk_band))
    if rfm_segment:
        stmt = stmt.where(RfmScore.rfm_segment == rfm_segment)
    if recommended_action:
        stmt = stmt.where(Recommendation.action == recommended_action)
    if city:
        stmt = stmt.where(func.lower(Customer.city) == city.strip().lower())
    if marketing_consent is not None:
        stmt = stmt.where(Customer.marketing_consent.is_(marketing_consent))
    if is_suppressed is not None:
        stmt = stmt.where(Customer.is_suppressed.is_(is_suppressed))
    if min_revenue is not None:
        stmt = stmt.where(CustomerMetrics.lifetime_revenue >= min_revenue)
    if max_revenue is not None:
        stmt = stmt.where(CustomerMetrics.lifetime_revenue <= max_revenue)
    if min_days_since_order is not None:
        stmt = stmt.where(CustomerMetrics.days_since_last_order >= min_days_since_order)
    if max_days_since_order is not None:
        stmt = stmt.where(CustomerMetrics.days_since_last_order <= max_days_since_order)
    if segment_id is not None:
        stmt = stmt.join(
            CustomerSegment,
            (CustomerSegment.customer_id == Customer.id)
            & (CustomerSegment.segment_id == segment_id),
        )

    total = db.execute(
        select(func.count()).select_from(stmt.subquery())
    ).scalar_one()

    column = SORT_FIELDS.get(sort_by, CustomerMetrics.lifetime_revenue)
    order = column.desc() if sort_dir == "desc" else column.asc()
    # Stable ordering: NULL metrics must not shuffle between pages.
    stmt = stmt.order_by(order, Customer.id.asc()).offset((page - 1) * page_size).limit(page_size)

    rows = db.execute(stmt).all()
    items = [
        CustomerSummary(**_summary_fields(build_customer_view(c, m, ch, r, rec)))
        for c, m, ch, r, rec in rows
    ]
    return Page[CustomerSummary](
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=max((total + page_size - 1) // page_size, 1),
    )


def _summary_fields(view: dict) -> dict:
    keys = set(CustomerSummary.model_fields)
    return {k: v for k, v in view.items() if k in keys}


@router.get("/customers/filters", tags=["customers"])
def customer_filter_options(
    db: Session = Depends(get_db), _: User = Depends(get_current_user)
) -> dict:
    """Distinct values available for the customer list filters."""
    cities = (
        db.execute(
            select(Customer.city)
            .where(Customer.city.is_not(None))
            .distinct()
            .order_by(Customer.city)
        )
        .scalars()
        .all()
    )
    rfm_segments = (
        db.execute(select(RfmScore.rfm_segment).distinct().order_by(RfmScore.rfm_segment))
        .scalars()
        .all()
    )
    actions = (
        db.execute(select(Recommendation.action).distinct().order_by(Recommendation.action))
        .scalars()
        .all()
    )
    return {
        "lifecycle_stages": [s.value for s in LifecycleStage],
        "churn_risk_bands": ["LOW", "MEDIUM", "HIGH", "CRITICAL"],
        "cities": cities,
        "rfm_segments": rfm_segments,
        "recommended_actions": actions,
        "sort_fields": sorted(SORT_FIELDS),
    }


@router.get("/customers/{customer_id}", response_model=CustomerDetail, tags=["customers"])
def get_customer(
    customer_id: int, db: Session = Depends(get_db), _: User = Depends(get_current_user)
) -> CustomerDetail:
    """The full Customer 360 profile."""
    customer = db.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer not found.")

    metrics = db.execute(
        select(CustomerMetrics).where(CustomerMetrics.customer_id == customer_id)
    ).scalar_one_or_none()
    churn = db.execute(
        select(ChurnScore).where(ChurnScore.customer_id == customer_id)
    ).scalar_one_or_none()
    rfm = db.execute(
        select(RfmScore).where(RfmScore.customer_id == customer_id)
    ).scalar_one_or_none()
    recommendation = db.execute(
        select(Recommendation).where(Recommendation.customer_id == customer_id)
    ).scalar_one_or_none()

    profile = build_customer_view(customer, metrics, churn, rfm, recommendation)

    # Derived fields the profile screen shows but that are not stored.
    from app.analytics.metrics import MetricResult

    if metrics is not None:
        proxy = MetricResult(
            completed_orders=metrics.completed_orders,
            median_purchase_interval_days=metrics.median_purchase_interval_days,
            average_purchase_interval_days=metrics.average_purchase_interval_days,
            days_since_last_order=metrics.days_since_last_order,
        )
        cycle, source = expected_cycle_days(proxy)
        profile["expected_cycle_days"] = round(cycle, 1)
        profile["cadence_source"] = source
        profile["days_overdue"] = (
            round((metrics.days_since_last_order or 0) - cycle, 1)
            if metrics.days_since_last_order is not None
            else None
        )
    else:
        profile["expected_cycle_days"] = None
        profile["cadence_source"] = None
        profile["days_overdue"] = None

    profile["suppressed_channels"] = (
        db.execute(
            select(SuppressionList.channel).where(
                SuppressionList.customer_id == customer_id,
                SuppressionList.active.is_(True),
            )
        )
        .scalars()
        .all()
    )
    profile["consent_history"] = [
        {
            "consent_type": e.consent_type,
            "granted": e.granted,
            "source": e.source,
            "occurred_at": e.occurred_at,
        }
        for e in db.execute(
            select(ConsentEvent)
            .where(ConsentEvent.customer_id == customer_id)
            .order_by(ConsentEvent.occurred_at.desc())
            .limit(30)
        )
        .scalars()
        .all()
    ]

    orders = (
        db.execute(
            select(Order)
            .options(selectinload(Order.items))
            .where(Order.customer_id == customer_id)
            .order_by(Order.ordered_at.desc())
        )
        .scalars()
        .all()
    )

    history = (
        db.execute(
            select(CustomerLifecycleHistory)
            .where(CustomerLifecycleHistory.customer_id == customer_id)
            .order_by(CustomerLifecycleHistory.changed_at.desc())
        )
        .scalars()
        .all()
    )

    events = (
        db.execute(
            select(CommunicationEvent)
            .where(CommunicationEvent.customer_id == customer_id)
            .order_by(CommunicationEvent.occurred_at.desc())
            .limit(100)
        )
        .scalars()
        .all()
    )

    messages = (
        db.execute(
            select(Message)
            .where(Message.customer_id == customer_id)
            .order_by(Message.created_at.desc())
            .limit(50)
        )
        .scalars()
        .all()
    )

    campaign_rows = db.execute(
        select(Campaign, CampaignRecipient)
        .join(CampaignRecipient, CampaignRecipient.campaign_id == Campaign.id)
        .where(CampaignRecipient.customer_id == customer_id)
        .order_by(Campaign.created_at.desc())
    ).all()
    campaigns = [
        {
            "campaign_id": c.id,
            "name": c.name,
            "objective": c.objective,
            "channel": c.channel,
            "status": r.status,
            "exclusion_reason": r.exclusion_reason,
            "sent_at": r.sent_at,
            "opened_at": r.opened_at,
            "clicked_at": r.clicked_at,
            "converted_at": r.converted_at,
        }
        for c, r in campaign_rows
    ]

    segment_rows = db.execute(
        select(Segment)
        .join(CustomerSegment, CustomerSegment.segment_id == Segment.id)
        .where(CustomerSegment.customer_id == customer_id)
        .order_by(Segment.name)
    ).scalars().all()
    segments = [
        {"id": s.id, "name": s.name, "segment_type": s.segment_type} for s in segment_rows
    ]

    attribution_rows = db.execute(
        select(AttributionRecord, Campaign, Order)
        .join(Campaign, Campaign.id == AttributionRecord.campaign_id)
        .join(Order, Order.id == AttributionRecord.order_id)
        .where(AttributionRecord.customer_id == customer_id)
        .order_by(AttributionRecord.created_at.desc())
    ).all()
    attribution = [
        {
            "order_external_id": o.external_id,
            "ordered_at": o.ordered_at,
            "campaign_id": c.id,
            "campaign_name": c.name,
            "revenue": a.revenue,
            "hours_since_touch": a.hours_since_touch,
            "is_reactivation": a.is_reactivation,
        }
        for a, c, o in attribution_rows
    ]

    return CustomerDetail(
        profile=profile,
        orders=[OrderOut.model_validate(o) for o in orders],
        lifecycle_history=[LifecycleHistoryOut.model_validate(h) for h in history],
        communication_events=[CommunicationEventOut.model_validate(e) for e in events],
        messages=[MessageOut.model_validate(m) for m in messages],
        automation_history=customer_history(db, customer_id),
        campaigns=campaigns,
        segments=segments,
        attribution=attribution,
    )


@router.post("/customers/{customer_id}/recalculate", tags=["customers"])
def recalculate_customer(
    customer_id: int, db: Session = Depends(get_db), user: User = Depends(require_write)
) -> dict:
    """Recompute metrics, lifecycle, churn and the recommendation."""
    customer = db.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer not found.")
    intel = refresh_customer(db, customer)
    return {
        "customer_id": customer_id,
        "lifecycle_stage": intel.lifecycle.stage.value,
        "lifecycle_reason": intel.lifecycle.reason,
        "churn_score": intel.churn.score,
        "churn_risk_band": intel.churn.risk_band.value,
        "recommended_action": intel.recommendation.action.value,
        "recommendation_explanation": intel.recommendation.explanation,
    }


@router.post(
    "/customers/{customer_id}/suppress", response_model=OperationResult, tags=["customers"]
)
def suppress_customer(
    customer_id: int,
    payload: SuppressRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_write),
) -> OperationResult:
    """Add a customer to the suppression list."""
    customer = db.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer not found.")

    channel = payload.channel.upper()
    valid = {"ALL"} | {c.value for c in Channel}
    if channel not in valid:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown channel '{channel}'. Expected one of: {', '.join(sorted(valid))}.",
        )

    existing = db.execute(
        select(SuppressionList).where(
            SuppressionList.customer_id == customer_id,
            SuppressionList.channel == channel,
        )
    ).scalar_one_or_none()
    if existing is None:
        db.add(
            SuppressionList(
                customer_id=customer_id,
                channel=channel,
                reason=payload.reason or "Suppressed by an operator.",
                created_by=user.email,
                active=True,
            )
        )
    else:
        existing.active = True
        existing.reason = payload.reason or existing.reason
        existing.created_by = user.email

    if channel == "ALL":
        customer.is_suppressed = True

    db.add(
        AuditLog(
            actor=user.email,
            action="CUSTOMER_SUPPRESSED",
            entity_type="customer",
            entity_id=str(customer_id),
            detail={"channel": channel, "reason": payload.reason},
        )
    )
    db.commit()
    refresh_customer(db, customer)
    return OperationResult(
        message=f"{customer.full_name} is now suppressed for {channel}.",
        detail={"channel": channel},
    )


@router.delete(
    "/customers/{customer_id}/suppress", response_model=OperationResult, tags=["customers"]
)
def unsuppress_customer(
    customer_id: int,
    channel: str = "ALL",
    db: Session = Depends(get_db),
    user: User = Depends(require_write),
) -> OperationResult:
    customer = db.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer not found.")

    rows = (
        db.execute(
            select(SuppressionList).where(
                SuppressionList.customer_id == customer_id,
                SuppressionList.channel == channel.upper(),
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        row.active = False
    if channel.upper() == "ALL":
        customer.is_suppressed = False

    db.add(
        AuditLog(
            actor=user.email,
            action="CUSTOMER_UNSUPPRESSED",
            entity_type="customer",
            entity_id=str(customer_id),
            detail={"channel": channel},
        )
    )
    db.commit()
    refresh_customer(db, customer)
    return OperationResult(message=f"Suppression removed for {channel.upper()}.")


@router.patch("/customers/{customer_id}/consent", response_model=OperationResult, tags=["customers"])
def update_consent(
    customer_id: int,
    payload: ConsentUpdateRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_write),
) -> OperationResult:
    """Update consent flags, writing an audit trail entry for each change."""
    customer = db.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer not found.")

    changes = payload.model_dump(exclude_none=True)
    if not changes:
        raise HTTPException(status_code=400, detail="No consent fields supplied.")

    type_by_field = {
        "marketing_consent": ConsentType.MARKETING,
        "email_consent": ConsentType.EMAIL,
        "sms_consent": ConsentType.SMS,
        "whatsapp_consent": ConsentType.WHATSAPP,
    }
    for field, granted in changes.items():
        setattr(customer, field, granted)
        db.add(
            ConsentEvent(
                customer_id=customer_id,
                consent_type=type_by_field[field].value,
                granted=granted,
                source=f"dashboard:{user.email}",
                occurred_at=utcnow(),
            )
        )

    # Revoking blanket marketing consent revokes every channel with it.
    if changes.get("marketing_consent") is False:
        for field in ("email_consent", "sms_consent", "whatsapp_consent"):
            setattr(customer, field, False)

    db.add(
        AuditLog(
            actor=user.email,
            action="CONSENT_UPDATED",
            entity_type="customer",
            entity_id=str(customer_id),
            detail=changes,
        )
    )
    db.commit()
    refresh_customer(db, customer)
    return OperationResult(message="Consent updated.", detail=changes)
