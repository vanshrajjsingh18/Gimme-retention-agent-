"""Campaign automations: create, approve, preview, run and audit.

The dry-run endpoint is deliberately available on any automation in any state,
including a draft that has never been approved — previewing who *would* be
messaged is how an operator decides whether to approve at all.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_write
from app.automations import cohort, nudge, sequences
from app.automations.runtime import AutomationError
from app.automations.service import (
    activate,
    apply_update,
    approve,
    enroll_customers,
    automation_stats,
    create_automation,
    pause,
    preview,
    replace_steps,
    resume,
    run_automation,
    set_enrollment_paused,
)
from app.core.database import get_db
from app.core.enums import AutomationKind, AutomationStatus
from app.models.entities import (
    AuditLog,
    Automation,
    AutomationEnrollment,
    AutomationSend,
    AutomationStep,
    Campaign,
    Message,
    User,
)
from app.schemas.common import OperationResult
from app.schemas.models import (
    AutomationCreate,
    AutomationEnrollmentOut,
    AutomationOut,
    AutomationSendOut,
    AutomationStepIn,
    AutomationUpdate,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _get(db: Session, automation_id: int) -> Automation:
    automation = db.get(Automation, automation_id)
    if automation is None:
        raise HTTPException(status_code=404, detail="Automation not found.")
    return automation


def _out(db: Session, automation: Automation) -> AutomationOut:
    data = AutomationOut.model_validate(automation)
    data.segment_name = cohort.segment_name(db, automation)
    data.steps = [
        step
        for step in db.execute(
            select(AutomationStep)
            .where(AutomationStep.automation_id == automation.id)
            .order_by(AutomationStep.position)
        )
        .scalars()
        .all()
    ]
    return data


@router.get("/automations", response_model=list[AutomationOut], tags=["automations"])
def list_automations(
    kind: AutomationKind | None = None,
    status: AutomationStatus | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> list[AutomationOut]:
    query = select(Automation)
    if kind is not None:
        query = query.where(Automation.kind == kind.value)
    if status is not None:
        query = query.where(Automation.status == status.value)
    rows = (
        db.execute(query.order_by(Automation.id.desc()).offset(offset).limit(limit))
        .scalars()
        .all()
    )
    return [_out(db, row) for row in rows]


@router.post(
    "/automations", response_model=AutomationOut, status_code=201, tags=["automations"]
)
def create(
    payload: AutomationCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_write),
) -> AutomationOut:
    try:
        automation = create_automation(
            db,
            name=payload.name,
            kind=payload.kind.value,
            description=payload.description,
            channel=payload.channel.value,
            objective=payload.objective.value,
            segment_id=payload.segment_id,
            manual_customer_ids=payload.manual_customer_ids,
            enrollment_mode=payload.enrollment_mode.value if payload.enrollment_mode else None,
            recurrence=payload.recurrence.value,
            recurrence_day=payload.recurrence_day,
            send_time_local=payload.send_time_local,
            starts_at=payload.starts_at,
            ends_at=payload.ends_at,
            trigger_type=payload.trigger_type.value,
            message_template=payload.message_template,
            message_variants=payload.message_variants,
            template_overrides=payload.template_overrides,
            config=payload.config,
            stop_on_order=payload.stop_on_order,
            require_approval=payload.require_approval,
            steps=[step.model_dump() for step in payload.steps],
            created_by_id=user.id,
        )
    except AutomationError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _out(db, automation)


@router.get("/automations/{automation_id}", response_model=AutomationOut, tags=["automations"])
def get_automation(
    automation_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> AutomationOut:
    return _out(db, _get(db, automation_id))


@router.patch(
    "/automations/{automation_id}", response_model=AutomationOut, tags=["automations"]
)
def update_automation(
    automation_id: int,
    payload: AutomationUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(require_write),
) -> AutomationOut:
    automation = _get(db, automation_id)
    apply_update(
        db, automation, payload.model_dump(exclude_unset=True), actor=user.email
    )
    return _out(db, automation)


@router.put(
    "/automations/{automation_id}/steps",
    response_model=AutomationOut,
    tags=["automations"],
)
def set_steps(
    automation_id: int,
    steps: list[AutomationStepIn],
    db: Session = Depends(get_db),
    user: User = Depends(require_write),
) -> AutomationOut:
    automation = _get(db, automation_id)
    try:
        replace_steps(
            db, automation, [step.model_dump() for step in steps], actor=user.email
        )
    except AutomationError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _out(db, automation)


@router.delete("/automations/{automation_id}", response_model=OperationResult, tags=["automations"])
def delete_automation(
    automation_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_write),
) -> OperationResult:
    automation = _get(db, automation_id)
    if automation.status == AutomationStatus.ACTIVE.value:
        raise HTTPException(
            status_code=409,
            detail="Pause the automation before deleting it, so no send is interrupted mid-run.",
        )
    db.add(
        AuditLog(
            actor=user.email,
            action="AUTOMATION_DELETED",
            entity_type="automation",
            entity_id=str(automation.id),
            detail={"name": automation.name, "kind": automation.kind},
        )
    )
    # The backing campaign outlives the automation only when it holds a record
    # worth keeping. One that never sent anything is pure plumbing, and leaving
    # it behind accumulates rows nobody can explain.
    campaign = db.get(Campaign, automation.campaign_id) if automation.campaign_id else None
    if campaign is not None:
        sent = db.execute(
            select(func.count(Message.id)).where(Message.campaign_id == campaign.id)
        ).scalar_one()
        if sent == 0:
            db.delete(campaign)
    db.delete(automation)
    db.commit()
    return OperationResult(success=True, message=f"Automation '{automation.name}' deleted.")


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------
@router.post(
    "/automations/{automation_id}/approve", response_model=AutomationOut, tags=["automations"]
)
def approve_automation(
    automation_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_write),
) -> AutomationOut:
    return _out(db, approve(db, _get(db, automation_id), user_id=user.id))


@router.post(
    "/automations/{automation_id}/activate", response_model=AutomationOut, tags=["automations"]
)
def activate_automation(
    automation_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_write),
) -> AutomationOut:
    try:
        automation = activate(db, _get(db, automation_id))
    except AutomationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _out(db, automation)


@router.post(
    "/automations/{automation_id}/pause", response_model=AutomationOut, tags=["automations"]
)
def pause_automation(
    automation_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_write),
) -> AutomationOut:
    return _out(db, pause(db, _get(db, automation_id)))


@router.post(
    "/automations/{automation_id}/resume", response_model=AutomationOut, tags=["automations"]
)
def resume_automation(
    automation_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_write),
) -> AutomationOut:
    try:
        automation = resume(db, _get(db, automation_id))
    except AutomationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _out(db, automation)


# --------------------------------------------------------------------------
# Preview and run
# --------------------------------------------------------------------------
@router.post("/automations/{automation_id}/preview", tags=["automations"])
def preview_automation(
    automation_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict:
    """Dry run: exactly who would receive what, and when. Nothing is sent."""
    return preview(db, _get(db, automation_id))


@router.post("/automations/{automation_id}/run", tags=["automations"])
def run_now(
    automation_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_write),
) -> dict:
    """Run an automation immediately, outside its schedule."""
    automation = _get(db, automation_id)
    try:
        report = run_automation(db, automation)
    except AutomationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.add(
        AuditLog(
            actor=user.email,
            action="AUTOMATION_RUN",
            entity_type="automation",
            entity_id=str(automation.id),
            detail={"sent": report.sent, "skipped": report.skipped, "failed": report.failed},
        )
    )
    db.commit()
    return report.as_dict()


@router.post("/automations/{automation_id}/dry-run", tags=["automations"])
def dry_run(
    automation_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_write),
) -> dict:
    """Show who would be messaged, without messaging anybody.

    The same pipeline a live run uses, stopped short of the provider call, so
    the answer is produced by the code that would actually send rather than by
    a second implementation that could drift from it. Recipients are recorded
    as PREVIEW rows, and ``summary`` spells out every exclusion in words.

    Mandatory before a first live campaign, and safe to repeat.
    """
    automation = _get(db, automation_id)
    try:
        report = run_automation(db, automation, dry_run=True)
    except AutomationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.add(
        AuditLog(
            actor=user.email,
            action="AUTOMATION_DRY_RUN",
            entity_type="automation",
            entity_id=str(automation.id),
            detail={
                "evaluated": len(report.results),
                "would_send": report.previewed,
                "excluded": report.skipped,
            },
        )
    )
    db.commit()
    return report.as_dict()


@router.get("/automations/{automation_id}/audience", tags=["automations"])
def automation_audience(
    automation_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict:
    """The audience as it stands right now, re-evaluated live."""
    automation = _get(db, automation_id)
    customer_ids = cohort.resolve_audience(db, automation)
    return {
        "automation_id": automation.id,
        "segment_id": automation.segment_id,
        "segment_name": cohort.segment_name(db, automation),
        "audience_size": len(customer_ids),
        "customer_ids": customer_ids[:200],
        "truncated": len(customer_ids) > 200,
    }


# --------------------------------------------------------------------------
# Audit
# --------------------------------------------------------------------------
@router.get("/automations/{automation_id}/stats", tags=["automations"])
def stats(
    automation_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict:
    return automation_stats(db, _get(db, automation_id))


@router.get(
    "/automations/{automation_id}/sends",
    response_model=list[AutomationSendOut],
    tags=["automations"],
)
def list_sends(
    automation_id: int,
    status: str | None = None,
    include_dry_runs: bool = False,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> list[AutomationSend]:
    _get(db, automation_id)
    query = select(AutomationSend).where(AutomationSend.automation_id == automation_id)
    if not include_dry_runs:
        query = query.where(AutomationSend.is_dry_run.is_(False))
    if status:
        query = query.where(AutomationSend.status == status)
    # Filters before paging: a filtered page must not come back empty while
    # matches exist further down the table.
    return list(
        db.execute(query.order_by(AutomationSend.id.desc()).offset(offset).limit(limit))
        .scalars()
        .all()
    )


@router.get(
    "/automations/{automation_id}/enrollments",
    response_model=list[AutomationEnrollmentOut],
    tags=["automations"],
)
def list_enrollments(
    automation_id: int,
    status: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> list[AutomationEnrollment]:
    _get(db, automation_id)
    query = select(AutomationEnrollment).where(
        AutomationEnrollment.automation_id == automation_id
    )
    if status:
        query = query.where(AutomationEnrollment.status == status)
    return list(
        db.execute(
            query.order_by(AutomationEnrollment.id.desc()).offset(offset).limit(limit)
        )
        .scalars()
        .all()
    )


@router.post(
    "/automations/{automation_id}/enrollments/{enrollment_id}/pause",
    response_model=AutomationEnrollmentOut,
    tags=["automations"],
)
def pause_enrollment(
    automation_id: int,
    enrollment_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_write),
) -> AutomationEnrollment:
    """Hold one customer's place without losing their progress."""
    return _set_enrollment_paused(db, automation_id, enrollment_id, True, user)


@router.post(
    "/automations/{automation_id}/enrollments/{enrollment_id}/resume",
    response_model=AutomationEnrollmentOut,
    tags=["automations"],
)
def resume_enrollment(
    automation_id: int,
    enrollment_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_write),
) -> AutomationEnrollment:
    return _set_enrollment_paused(db, automation_id, enrollment_id, False, user)


def _set_enrollment_paused(
    db: Session, automation_id: int, enrollment_id: int, paused: bool, user: User
) -> AutomationEnrollment:
    _get(db, automation_id)
    enrollment = db.get(AutomationEnrollment, enrollment_id)
    if enrollment is None or enrollment.automation_id != automation_id:
        raise HTTPException(status_code=404, detail="Enrollment not found.")
    try:
        return set_enrollment_paused(db, enrollment, paused=paused, actor=user.email)
    except AutomationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/automations/{automation_id}/enrollments", tags=["automations"])
def add_enrollments(
    automation_id: int,
    customer_ids: list[int],
    db: Session = Depends(get_db),
    user: User = Depends(require_write),
) -> dict:
    """Enrol named customers by hand — the MANUAL sequence trigger."""
    automation = _get(db, automation_id)
    try:
        return enroll_customers(db, automation, customer_ids, actor=user.email)
    except AutomationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/automations/{automation_id}/refresh-patterns", tags=["automations"])
def refresh_patterns(
    automation_id: int,
    force: bool = False,
    db: Session = Depends(get_db),
    _: User = Depends(require_write),
) -> dict:
    """Recompute order patterns for a behavioural nudge."""
    automation = _get(db, automation_id)
    if automation.kind != AutomationKind.NUDGE.value:
        raise HTTPException(
            status_code=400,
            detail="Order patterns only apply to behavioural nudge automations.",
        )
    return nudge.refresh_patterns(db, automation, force=force)


@router.post("/automations/{automation_id}/enroll", tags=["automations"])
def enroll_now(
    automation_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_write),
) -> dict:
    """Bring the enrollment list up to date without sending anything."""
    automation = _get(db, automation_id)
    if automation.kind == AutomationKind.NUDGE.value:
        return nudge.enroll(db, automation)
    if automation.kind == AutomationKind.SEQUENCE.value:
        return sequences.enroll(db, automation)
    raise HTTPException(
        status_code=400,
        detail="Cohort campaigns resolve their audience at send time and have no enrollments.",
    )


# --------------------------------------------------------------------------
# Smart Reorder Campaign Extension: Audience Preview
# --------------------------------------------------------------------------
@router.get("/automations/{automation_id}/audience-preview", tags=["automations"])
def preview_audience(
    automation_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict:
    """Show detailed breakdown of eligible audience for this automation.

    Returns counts at each filtering step to help campaign creators understand
    who will receive messages and why some customers are excluded.
    """
    from app.services.audience_preview import preview_audience

    automation = _get(db, automation_id)
    return preview_audience(db, automation)


# --------------------------------------------------------------------------
# Smart Reorder Campaign Extension: Coupon Variants
# --------------------------------------------------------------------------
@router.get("/automations/{automation_id}/coupon-variants", tags=["automations"])
def list_coupon_variants(
    automation_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict:
    """List all coupon variants for an automation."""
    from app.models.entities import CouponVariant

    automation = _get(db, automation_id)
    variants = db.execute(
        select(CouponVariant)
        .where(CouponVariant.automation_id == automation_id)
        .order_by(CouponVariant.position)
    ).scalars().all()

    return {
        "automation_id": automation_id,
        "variants": [
            {
                "id": v.id,
                "position": v.position,
                "coupon_code": v.coupon_code,
                "allocation_percentage": v.allocation_percentage,
                "enabled": v.enabled,
                "created_at": v.created_at.isoformat(),
            }
            for v in variants
        ],
    }


@router.post("/automations/{automation_id}/coupon-variants", tags=["automations"])
def create_coupon_variant(
    automation_id: int,
    coupon_code: str = Query(...),
    allocation_percentage: float = Query(...),
    enabled: bool = Query(default=True),
    db: Session = Depends(get_db),
    user: User = Depends(require_write),
) -> dict:
    """Add a new coupon variant to an automation."""
    from app.models.entities import CouponVariant

    automation = _get(db, automation_id)

    # Get next position
    max_position = db.execute(
        select(func.max(CouponVariant.position)).where(
            CouponVariant.automation_id == automation_id
        )
    ).scalar() or -1

    variant = CouponVariant(
        automation_id=automation_id,
        position=max_position + 1,
        coupon_code=coupon_code,
        allocation_percentage=allocation_percentage,
        enabled=enabled,
    )
    db.add(variant)
    db.commit()

    db.add(
        AuditLog(
            actor=user.email,
            action="COUPON_VARIANT_CREATED",
            entity_type="automation",
            entity_id=str(automation_id),
            detail={
                "coupon_code": coupon_code,
                "allocation_percentage": allocation_percentage,
            },
        )
    )
    db.commit()

    return {
        "id": variant.id,
        "coupon_code": variant.coupon_code,
        "allocation_percentage": variant.allocation_percentage,
        "enabled": variant.enabled,
    }


@router.delete("/automations/{automation_id}/coupon-variants/{variant_id}", tags=["automations"])
def delete_coupon_variant(
    automation_id: int,
    variant_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_write),
) -> dict:
    """Remove a coupon variant from an automation."""
    from app.models.entities import CouponVariant

    automation = _get(db, automation_id)
    variant = db.get(CouponVariant, variant_id)

    if not variant or variant.automation_id != automation_id:
        raise HTTPException(status_code=404, detail="Variant not found.")

    coupon_code = variant.coupon_code
    db.delete(variant)
    db.add(
        AuditLog(
            actor=user.email,
            action="COUPON_VARIANT_DELETED",
            entity_type="automation",
            entity_id=str(automation_id),
            detail={"coupon_code": coupon_code},
        )
    )
    db.commit()

    return {"deleted": True, "variant_id": variant_id}


@router.get("/automations/{automation_id}/coupon-analytics", tags=["automations"])
def coupon_analytics(
    automation_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict:
    """Get performance analytics for coupon variants.

    Shows coupon assignment counts, message sends, conversions, and revenue
    to help evaluate which coupons drive orders.
    """
    from app.services.coupon_analytics import get_coupon_performance

    automation = _get(db, automation_id)
    return get_coupon_performance(db, automation_id)


@router.get("/automations/{automation_id}/touchpoint-config", tags=["automations"])
def get_touchpoint_config(
    automation_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict:
    """Get multi-touch touchpoint configuration for an automation.

    Returns the touchpoint array from automation.config if configured,
    or empty list if automation uses single-message mode.
    """
    from app.services.multi_touch_orchestration import get_touchpoints_config

    automation = _get(db, automation_id)
    touchpoints = get_touchpoints_config(automation)
    return {
        "automation_id": automation_id,
        "touchpoints": touchpoints,
        "mode": "multi_touch" if touchpoints else "single_message",
    }


@router.put("/automations/{automation_id}/touchpoint-config", tags=["automations"])
def update_touchpoint_config(
    automation_id: int,
    touchpoints: list[dict],
    db: Session = Depends(get_db),
    _: User = Depends(require_write),
) -> dict:
    """Update multi-touch touchpoint configuration for an automation.

    Replaces the touchpoints array in automation.config.
    """
    from app.services.automation_config_validation import validate_touchpoint_rules

    automation = _get(db, automation_id)

    # Validate touchpoints
    errors = validate_touchpoint_rules(automation)
    if errors:
        raise HTTPException(status_code=422, detail={"validation_errors": errors})

    # Update config
    config = automation.config or {}
    config["touchpoints"] = touchpoints
    automation.config = config

    db.commit()
    db.refresh(automation)

    return {
        "automation_id": automation_id,
        "touchpoints": touchpoints,
        "mode": "multi_touch" if touchpoints else "single_message",
    }


@router.get("/automations/{automation_id}/journey-stats", tags=["automations"])
def journey_stats(
    automation_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict:
    """Get statistics on multi-touch journey progress.

    Shows how many customers are at each touchpoint position,
    useful for understanding campaign flow and engagement.
    """
    from app.services.multi_touch_orchestration import get_journey_stats

    automation = _get(db, automation_id)
    return get_journey_stats(db, automation_id)


@router.get("/automations/{automation_id}/performance", tags=["automations"])
def campaign_performance(
    automation_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict:
    """Get campaign performance summary with key metrics.

    Shows messages sent/delivered, orders, conversion rates, revenue metrics.
    """
    from app.services.campaign_analytics import get_campaign_performance_summary

    automation = _get(db, automation_id)
    return get_campaign_performance_summary(db, automation_id)


@router.get("/automations/{automation_id}/funnel", tags=["automations"])
def conversion_funnel(
    automation_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict:
    """Get conversion funnel from audience through to orders.

    Shows drop-off at each stage: assigned → contacted → delivered → ordered.
    """
    from app.services.campaign_analytics import get_conversion_funnel

    automation = _get(db, automation_id)
    return get_conversion_funnel(db, automation_id)


@router.get("/automations/{automation_id}/customer-journey/{customer_id}", tags=["automations"])
def customer_journey(
    automation_id: int,
    customer_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict:
    """Get detailed journey tracking for a specific customer.

    Shows all messages sent, delivery status, orders placed after first message,
    time to conversion, and total revenue attributed.
    """
    from app.services.campaign_analytics import get_customer_journey_details

    automation = _get(db, automation_id)
    return get_customer_journey_details(db, automation_id, customer_id)


@router.get("/automations/{automation_id}/touchpoint-performance", tags=["automations"])
def touchpoint_performance(
    automation_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict:
    """Get performance metrics for each touchpoint in a multi-touch campaign.

    Shows message delivery and engagement metrics per touchpoint position.
    """
    from app.services.campaign_analytics import get_touchpoint_performance

    automation = _get(db, automation_id)
    return get_touchpoint_performance(db, automation_id)


@router.get("/automations/{automation_id}/stop-conditions", tags=["automations"])
def get_stop_conditions(
    automation_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict:
    """Get stop conditions configuration for an automation.

    Shows which conditions trigger journey termination (stop_on_order,
    max_sends, campaign_end_date).
    """
    from app.services.stop_conditions import get_stop_conditions_config

    automation = _get(db, automation_id)
    return get_stop_conditions_config(automation)


@router.put("/automations/{automation_id}/stop-conditions", tags=["automations"])
def update_stop_conditions(
    automation_id: int,
    stop_on_order: bool | None = None,
    max_sends: int | None = None,
    campaign_end_date: str | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(require_write),
) -> dict:
    """Update stop conditions configuration for an automation.

    Configure when to stop sending to customers.
    """
    from app.services.stop_conditions import update_stop_conditions_config

    automation = _get(db, automation_id)

    update_stop_conditions_config(
        automation,
        stop_on_order=stop_on_order,
        max_sends=max_sends,
        campaign_end_date=campaign_end_date,
    )

    db.commit()
    db.refresh(automation)

    return get_stop_conditions(automation_id, db, _)


@router.get("/automations/{automation_id}/lifecycle-stats", tags=["automations"])
def lifecycle_stats(
    automation_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict:
    """Get lifecycle statistics for an automation.

    Shows how many journeys are active, completed, or stopped,
    and the reasons for stopping.
    """
    from app.services.stop_conditions import get_lifecycle_stats

    automation = _get(db, automation_id)
    return get_lifecycle_stats(db, automation_id)


@router.get("/automations/{automation_id}/ab-test-summary", tags=["automations"])
def ab_test_summary(
    automation_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict:
    """Get A/B testing summary and performance comparison.

    Shows coupon variant performance, message variant performance,
    holdout group configuration, and recommended winner.
    """
    from app.services.ab_testing import get_ab_test_summary

    automation = _get(db, automation_id)
    return get_ab_test_summary(db, automation_id)


@router.post("/automations/{automation_id}/setup-holdout", tags=["automations"])
def setup_holdout(
    automation_id: int,
    holdout_percentage: int = 10,
    db: Session = Depends(get_db),
    _: User = Depends(require_write),
) -> dict:
    """Setup a control/holdout group for measuring campaign lift.

    Customers in the holdout group will not receive messages, allowing
    measurement of campaign impact.
    """
    from app.services.ab_testing import setup_holdout_group

    automation = _get(db, automation_id)
    setup_holdout_group(automation, holdout_percentage)

    db.commit()
    db.refresh(automation)

    return {
        "automation_id": automation_id,
        "holdout_enabled": True,
        "holdout_percentage": holdout_percentage,
    }
