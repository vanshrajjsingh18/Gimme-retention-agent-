"""Campaign lifecycle API."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_write
from app.campaigns.service import (
    CampaignError,
    approve_campaign,
    cancel_campaign,
    pause_campaign,
    preview_audience,
    run_campaign,
    run_compliance_check,
    schedule_campaign,
    send_test_message,
    snapshot_audience,
    submit_for_approval,
)
from app.core.database import get_db
from app.core.enums import CampaignObjective, CampaignStatus, Channel
from app.models.entities import (
    AuditLog,
    Campaign,
    CampaignRecipient,
    Customer,
    Segment,
    User,
)
from app.schemas.models import (
    CampaignCreate,
    CampaignOut,
    CampaignUpdate,
    RunCampaignRequest,
    ScheduleRequest,
    SendTestRequest,
)

router = APIRouter()

EDITABLE_STATUSES = {
    CampaignStatus.DRAFT.value,
    CampaignStatus.AI_GENERATED.value,
    CampaignStatus.VALIDATED.value,
    CampaignStatus.COMPLIANCE_CHECKED.value,
    CampaignStatus.AWAITING_APPROVAL.value,
}


def _out(db: Session, campaign: Campaign) -> CampaignOut:
    data = CampaignOut.model_validate(campaign)
    if campaign.segment_id:
        segment = db.get(Segment, campaign.segment_id)
        data.segment_name = segment.name if segment else None
    return data


def _get(db: Session, campaign_id: int) -> Campaign:
    campaign = db.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail="Campaign not found.")
    return campaign


@router.get("/campaigns/options", tags=["campaigns"])
def campaign_options(_: User = Depends(get_current_user)) -> dict:
    return {
        "objectives": [o.value for o in CampaignObjective],
        "channels": [c.value for c in Channel],
        "statuses": [s.value for s in CampaignStatus],
        "attribution_windows": [
            {"hours": 24, "label": "24 hours"},
            {"hours": 48, "label": "48 hours"},
            {"hours": 72, "label": "72 hours"},
            {"hours": 168, "label": "7 days"},
        ],
    }


@router.get("/campaigns", response_model=list[CampaignOut], tags=["campaigns"])
def list_campaigns(
    status: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> list[CampaignOut]:
    stmt = select(Campaign)
    if status:
        stmt = stmt.where(Campaign.status == status.upper())
    stmt = stmt.order_by(Campaign.created_at.desc()).limit(limit)
    return [_out(db, c) for c in db.execute(stmt).scalars().all()]


@router.post("/campaigns", response_model=CampaignOut, status_code=201, tags=["campaigns"])
def create_campaign(
    payload: CampaignCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_write),
) -> CampaignOut:
    if payload.segment_id is not None and db.get(Segment, payload.segment_id) is None:
        raise HTTPException(status_code=400, detail="The selected segment does not exist.")

    campaign = Campaign(
        **payload.model_dump(exclude={"objective", "channel"}),
        objective=payload.objective.value,
        channel=payload.channel.value,
        status=CampaignStatus.DRAFT.value,
        created_by_id=user.id,
    )
    db.add(campaign)
    db.add(
        AuditLog(
            actor=user.email,
            action="CAMPAIGN_CREATED",
            entity_type="campaign",
            entity_id=payload.name,
        )
    )
    db.commit()
    db.refresh(campaign)
    return _out(db, campaign)


@router.get("/campaigns/{campaign_id}", response_model=CampaignOut, tags=["campaigns"])
def get_campaign(
    campaign_id: int, db: Session = Depends(get_db), _: User = Depends(get_current_user)
) -> CampaignOut:
    return _out(db, _get(db, campaign_id))


@router.patch("/campaigns/{campaign_id}", response_model=CampaignOut, tags=["campaigns"])
def update_campaign(
    campaign_id: int,
    payload: CampaignUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(require_write),
) -> CampaignOut:
    campaign = _get(db, campaign_id)
    if campaign.status not in EDITABLE_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"A campaign in status {campaign.status} cannot be edited. Cancel it and "
                "create a new one instead."
            ),
        )

    changes = payload.model_dump(exclude_none=True)
    for key, value in changes.items():
        setattr(campaign, key, value.value if hasattr(value, "value") else value)

    # Any content or audience change invalidates the previous approval and
    # compliance result.
    if changes.keys() & {"subject", "body", "segment_id", "channel", "objective"}:
        campaign.status = CampaignStatus.DRAFT.value
        campaign.compliance_result = {}
        campaign.approved_by_id = None
        campaign.approved_at = None

    db.add(
        AuditLog(
            actor=user.email,
            action="CAMPAIGN_UPDATED",
            entity_type="campaign",
            entity_id=str(campaign_id),
            detail={"fields": sorted(changes)},
        )
    )
    db.commit()
    db.refresh(campaign)
    return _out(db, campaign)


@router.get("/campaigns/{campaign_id}/audience", tags=["campaigns"])
def campaign_audience(
    campaign_id: int, db: Session = Depends(get_db), _: User = Depends(get_current_user)
) -> dict:
    """Preview the audience breakdown without persisting a snapshot."""
    campaign = _get(db, campaign_id)
    try:
        audience = preview_audience(db, campaign)
    except CampaignError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    audience.pop("_decisions", None)
    return audience


@router.post("/campaigns/{campaign_id}/audience/snapshot", tags=["campaigns"])
def snapshot(
    campaign_id: int, db: Session = Depends(get_db), _: User = Depends(require_write)
) -> dict:
    """Materialise the audience into campaign recipients."""
    campaign = _get(db, campaign_id)
    try:
        return snapshot_audience(db, campaign)
    except CampaignError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/campaigns/{campaign_id}/recipients", tags=["campaigns"])
def campaign_recipients(
    campaign_id: int,
    status: str | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict:
    _get(db, campaign_id)
    stmt = (
        select(CampaignRecipient, Customer)
        .join(Customer, Customer.id == CampaignRecipient.customer_id)
        .where(CampaignRecipient.campaign_id == campaign_id)
    )
    if status:
        stmt = stmt.where(CampaignRecipient.status == status.upper())
    # Filters must be applied before the limit, or a page can come back empty
    # while matching rows exist beyond the cut.
    rows = db.execute(stmt.order_by(CampaignRecipient.id).limit(limit)).all()
    return {
        "recipients": [
            {
                "customer_id": c.id,
                "external_id": c.external_id,
                "full_name": c.full_name,
                "email": c.email,
                "phone": c.phone,
                "lifecycle_stage": c.lifecycle_stage,
                "status": r.status,
                "exclusion_reason": r.exclusion_reason,
                "sent_at": r.sent_at,
                "delivered_at": r.delivered_at,
                "opened_at": r.opened_at,
                "clicked_at": r.clicked_at,
                "converted_at": r.converted_at,
            }
            for r, c in rows
        ]
    }


@router.post("/campaigns/{campaign_id}/compliance-check", tags=["campaigns"])
def compliance_check(
    campaign_id: int, db: Session = Depends(get_db), _: User = Depends(require_write)
) -> dict:
    campaign = _get(db, campaign_id)
    report = run_compliance_check(db, campaign)
    return report.as_dict()


@router.post("/campaigns/{campaign_id}/submit", response_model=CampaignOut, tags=["campaigns"])
def submit(
    campaign_id: int, db: Session = Depends(get_db), user: User = Depends(require_write)
) -> CampaignOut:
    campaign = _get(db, campaign_id)
    try:
        submit_for_approval(db, campaign)
    except CampaignError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.add(
        AuditLog(
            actor=user.email,
            action="CAMPAIGN_SUBMITTED",
            entity_type="campaign",
            entity_id=str(campaign_id),
        )
    )
    db.commit()
    db.refresh(campaign)
    return _out(db, campaign)


@router.post("/campaigns/{campaign_id}/approve", response_model=CampaignOut, tags=["campaigns"])
def approve(
    campaign_id: int, db: Session = Depends(get_db), user: User = Depends(require_write)
) -> CampaignOut:
    """Human approval. Required before any campaign can send."""
    campaign = _get(db, campaign_id)
    try:
        approve_campaign(db, campaign, user_id=user.id)
    except CampaignError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.add(
        AuditLog(
            actor=user.email,
            action="CAMPAIGN_APPROVED",
            entity_type="campaign",
            entity_id=str(campaign_id),
            detail={"name": campaign.name},
        )
    )
    db.commit()
    db.refresh(campaign)
    return _out(db, campaign)


@router.post("/campaigns/{campaign_id}/schedule", response_model=CampaignOut, tags=["campaigns"])
def schedule(
    campaign_id: int,
    payload: ScheduleRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_write),
) -> CampaignOut:
    campaign = _get(db, campaign_id)
    try:
        schedule_campaign(db, campaign, payload.scheduled_at)
    except CampaignError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.refresh(campaign)
    return _out(db, campaign)


@router.post("/campaigns/{campaign_id}/send-test", tags=["campaigns"])
def test_send(
    campaign_id: int,
    payload: SendTestRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_write),
) -> dict:
    campaign = _get(db, campaign_id)
    result = send_test_message(
        db, campaign, to=payload.to, customer_id=payload.customer_id
    )
    db.add(
        AuditLog(
            actor=user.email,
            action="CAMPAIGN_TEST_SENT",
            entity_type="campaign",
            entity_id=str(campaign_id),
            detail={"success": result["success"]},
        )
    )
    db.commit()
    return result


@router.post("/campaigns/{campaign_id}/run", tags=["campaigns"])
def run(
    campaign_id: int,
    payload: RunCampaignRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_write),
) -> dict:
    """Execute an approved campaign."""
    campaign = _get(db, campaign_id)
    try:
        stats = run_campaign(
            db,
            campaign,
            generate_per_customer=payload.generate_per_customer,
            simulate_engagement=payload.simulate_engagement,
            limit=payload.limit,
        )
    except CampaignError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    db.add(
        AuditLog(
            actor=user.email,
            action="CAMPAIGN_RUN",
            entity_type="campaign",
            entity_id=str(campaign_id),
            detail=stats,
        )
    )
    db.commit()
    return stats


@router.post("/campaigns/{campaign_id}/pause", response_model=CampaignOut, tags=["campaigns"])
def pause(
    campaign_id: int, db: Session = Depends(get_db), _: User = Depends(require_write)
) -> CampaignOut:
    campaign = _get(db, campaign_id)
    try:
        pause_campaign(db, campaign)
    except CampaignError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.refresh(campaign)
    return _out(db, campaign)


@router.post("/campaigns/{campaign_id}/cancel", response_model=CampaignOut, tags=["campaigns"])
def cancel(
    campaign_id: int, db: Session = Depends(get_db), user: User = Depends(require_write)
) -> CampaignOut:
    campaign = _get(db, campaign_id)
    try:
        cancel_campaign(db, campaign)
    except CampaignError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.add(
        AuditLog(
            actor=user.email,
            action="CAMPAIGN_CANCELLED",
            entity_type="campaign",
            entity_id=str(campaign_id),
        )
    )
    db.commit()
    db.refresh(campaign)
    return _out(db, campaign)
