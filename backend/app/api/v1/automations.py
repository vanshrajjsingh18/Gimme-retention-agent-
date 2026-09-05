"""Campaign automations: create, approve, preview, run and audit.

The dry-run endpoint is deliberately available on any automation in any state,
including a draft that has never been approved — previewing who *would* be
messaged is how an operator decides whether to approve at all.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
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
