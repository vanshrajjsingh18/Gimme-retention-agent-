"""Automation lifecycle: create, approve, activate, run, report.

One entry point (:func:`run_automation`) dispatches on ``kind``, so callers —
the API, the scheduler, the CLI — never branch on campaign type themselves.
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.automations import cohort, nudge, sequences
from app.automations.runtime import AutomationError, RunReport
from app.core.enums import (
    AutomationKind,
    AutomationStatus,
    CampaignObjective,
    CampaignStatus,
    Channel,
    EnrollmentStatus,
    RecurrenceKind,
    SendStatus,
)
from app.models.base import utcnow
from app.models.entities import (
    AuditLog,
    Automation,
    AutomationEnrollment,
    AutomationSend,
    AutomationStep,
    Campaign,
)

logger = logging.getLogger(__name__)

RUNNERS = {
    AutomationKind.COHORT_BULK.value: cohort.run,
    AutomationKind.SEQUENCE.value: sequences.run,
    AutomationKind.NUDGE.value: nudge.run,
}


# --------------------------------------------------------------------------
# Creation
# --------------------------------------------------------------------------
def create_automation(
    db: Session,
    *,
    name: str,
    kind: str,
    description: str = "",
    channel: str = Channel.SMS.value,
    objective: str = CampaignObjective.RETENTION.value,
    segment_id: int | None = None,
    manual_customer_ids: list[int] | None = None,
    enrollment_mode: str | None = None,
    recurrence: str = RecurrenceKind.ONCE.value,
    recurrence_day: int | None = None,
    send_time_local: str = "10:00",
    starts_at: datetime | None = None,
    ends_at: datetime | None = None,
    message_template: str = "",
    template_overrides: dict | None = None,
    config: dict | None = None,
    stop_on_order: bool = True,
    require_approval: bool = True,
    steps: list[dict] | None = None,
    created_by_id: int | None = None,
) -> Automation:
    """Create an automation and its backing campaign.

    Every automation gets a Campaign row so its sends appear in the existing
    campaign analytics, attribution and Customer 360 history rather than in a
    parallel reporting world of their own.
    """
    if kind not in RUNNERS:
        raise AutomationError(f"Unknown automation kind: {kind}")
    if segment_id is None and not manual_customer_ids:
        raise AutomationError(
            "An automation needs an audience: either a segment or a manual customer list."
        )

    campaign = Campaign(
        name=f"{name} (automation)",
        description=description or f"Backing campaign for automation '{name}'.",
        objective=objective,
        channel=channel,
        segment_id=segment_id,
        status=CampaignStatus.DRAFT.value,
        subject=None,
        body=message_template,
    )
    db.add(campaign)
    db.flush()

    automation = Automation(
        name=name,
        description=description,
        kind=kind,
        status=AutomationStatus.DRAFT.value,
        channel=channel,
        objective=objective,
        segment_id=segment_id,
        manual_customer_ids=list(manual_customer_ids or []),
        enrollment_mode=enrollment_mode or _default_enrollment_mode(kind),
        recurrence=recurrence,
        recurrence_day=recurrence_day,
        send_time_local=send_time_local,
        starts_at=starts_at,
        ends_at=ends_at,
        config=config or {},
        message_template=message_template,
        template_overrides=template_overrides or {},
        campaign_id=campaign.id,
        stop_on_order=stop_on_order,
        require_approval=require_approval,
        created_by_id=created_by_id,
    )
    db.add(automation)
    db.flush()

    for position, step in enumerate(steps or []):
        db.add(
            AutomationStep(
                automation_id=automation.id,
                position=step.get("position", position),
                name=step.get("name", f"Step {position + 1}"),
                offset_days=int(step.get("offset_days", 0)),
                send_time_local=step.get("send_time_local"),
                message_template=step.get("message_template", ""),
                use_llm=bool(step.get("use_llm", False)),
            )
        )

    if kind == AutomationKind.SEQUENCE.value and not steps:
        raise AutomationError("A sequence needs at least one step.")

    db.commit()
    logger.info("Created %s automation '%s' (id=%s)", kind, name, automation.id)
    return automation


def _default_enrollment_mode(kind: str) -> str:
    from app.core.enums import EnrollmentMode

    # A nudge is a standing automation, so new matching customers must be able
    # to join it; a sequence defaults to rolling for the same reason but is
    # commonly locked, which is why it is a per-campaign toggle.
    return EnrollmentMode.ROLLING.value


def replace_steps(db: Session, automation: Automation, steps: list[dict]) -> Automation:
    """Rewrite a sequence's steps. Refused once customers are enrolled.

    Changing offsets mid-flight would silently re-time messages for people
    already partway through, so the sequence has to be drafted again instead.
    """
    if _enrollment_count(db, automation):
        raise AutomationError(
            "Steps cannot be changed once customers are enrolled — pause the "
            "automation and create a new version instead."
        )
    for row in db.execute(
        select(AutomationStep).where(AutomationStep.automation_id == automation.id)
    ).scalars().all():
        db.delete(row)
    db.flush()
    for position, step in enumerate(steps):
        db.add(
            AutomationStep(
                automation_id=automation.id,
                position=step.get("position", position),
                name=step.get("name", f"Step {position + 1}"),
                offset_days=int(step.get("offset_days", 0)),
                send_time_local=step.get("send_time_local"),
                message_template=step.get("message_template", ""),
                use_llm=bool(step.get("use_llm", False)),
            )
        )
    db.commit()
    return automation


def _enrollment_count(db: Session, automation: Automation) -> int:
    return (
        db.execute(
            select(func.count(AutomationEnrollment.id)).where(
                AutomationEnrollment.automation_id == automation.id
            )
        ).scalar_one()
        or 0
    )


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------
def approve(db: Session, automation: Automation, *, user_id: int) -> Automation:
    """Record human approval. Required before an automation can send."""
    automation.approved_by_id = user_id
    automation.approved_at = utcnow()
    db.add(
        AuditLog(
            actor=str(user_id),
            action="AUTOMATION_APPROVED",
            entity_type="automation",
            entity_id=str(automation.id),
            detail={"name": automation.name, "kind": automation.kind},
        )
    )
    db.commit()
    return automation


def activate(db: Session, automation: Automation, *, now: datetime | None = None) -> Automation:
    """Switch an automation on.

    Approval is checked here as well as at send time — failing at activation
    is a better experience than an automation that looks live and silently
    sends nothing.
    """
    now = now or utcnow()
    if automation.require_approval and automation.approved_at is None:
        raise AutomationError(
            f"Automation '{automation.name}' must be approved before it is activated."
        )
    automation.status = AutomationStatus.ACTIVE.value
    automation.starts_at = automation.starts_at or now
    if automation.kind == AutomationKind.COHORT_BULK.value:
        automation.next_run_at = (
            cohort.next_occurrence(automation, after=now)
            if automation.recurrence != RecurrenceKind.ONCE.value
            else (automation.starts_at or now)
        )
    else:
        automation.next_run_at = now
    db.commit()
    return automation


def pause(db: Session, automation: Automation) -> Automation:
    automation.status = AutomationStatus.PAUSED.value
    db.commit()
    return automation


def resume(db: Session, automation: Automation, *, now: datetime | None = None) -> Automation:
    if automation.status != AutomationStatus.PAUSED.value:
        raise AutomationError("Only a paused automation can be resumed.")
    return activate(db, automation, now=now)


# --------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------
def run_automation(
    db: Session,
    automation: Automation,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
) -> RunReport:
    runner = RUNNERS.get(automation.kind)
    if runner is None:
        raise AutomationError(f"Unknown automation kind: {automation.kind}")
    return runner(db, automation, now=now, dry_run=dry_run)


def preview(db: Session, automation: Automation, *, now: datetime | None = None) -> dict:
    """Dry run: exactly who would receive what, and when. Nothing is sent."""
    report = run_automation(db, automation, now=now, dry_run=True)
    return report.as_dict()


def due_automations(db: Session, *, now: datetime | None = None) -> list[Automation]:
    """Active automations whose next run has come due."""
    now = now or utcnow()
    return list(
        db.execute(
            select(Automation).where(
                Automation.status == AutomationStatus.ACTIVE.value,
                Automation.next_run_at.is_not(None),
                Automation.next_run_at <= now,
            )
        )
        .scalars()
        .all()
    )


def run_due(db: Session, *, now: datetime | None = None) -> list[dict]:
    """Run every automation that is due. Used by the scheduler.

    One automation failing must not stop the rest, so each is isolated: a
    broken template in a cohort campaign should not block the nudges.
    """
    now = now or utcnow()
    reports = []
    for automation in due_automations(db, now=now):
        try:
            report = run_automation(db, automation, now=now)
            reports.append(report.as_dict(sample_size=0))
        except Exception:  # noqa: BLE001 - one bad automation must not stop the batch
            logger.exception("Automation %s failed to run", automation.id)
            db.rollback()
            reports.append(
                {"automation_id": automation.id, "error": "run failed — see logs"}
            )
    return reports


def refresh_nudge_patterns(db: Session, *, now: datetime | None = None) -> dict:
    """Monthly maintenance: recompute order patterns for every nudge.

    Habits drift, so a pattern computed once and frozen slowly stops matching
    the customer it describes.
    """
    now = now or utcnow()
    totals = {"automations": 0, "refreshed": 0, "dropped": 0}
    for automation in (
        db.execute(
            select(Automation).where(
                Automation.kind == AutomationKind.NUDGE.value,
                Automation.status == AutomationStatus.ACTIVE.value,
            )
        )
        .scalars()
        .all()
    ):
        result = nudge.refresh_patterns(db, automation, now=now)
        totals["automations"] += 1
        totals["refreshed"] += result["refreshed"]
        totals["dropped"] += result["dropped"]
    return totals


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------
def automation_stats(db: Session, automation: Automation) -> dict:
    """Delivery and enrollment counts for one automation."""
    by_status = dict(
        db.execute(
            select(AutomationSend.status, func.count(AutomationSend.id))
            .where(
                AutomationSend.automation_id == automation.id,
                AutomationSend.is_dry_run.is_(False),
            )
            .group_by(AutomationSend.status)
        ).all()
    )
    by_skip = dict(
        db.execute(
            select(AutomationSend.skip_reason, func.count(AutomationSend.id))
            .where(
                AutomationSend.automation_id == automation.id,
                AutomationSend.is_dry_run.is_(False),
                AutomationSend.skip_reason.is_not(None),
            )
            .group_by(AutomationSend.skip_reason)
        ).all()
    )
    enrollments = dict(
        db.execute(
            select(AutomationEnrollment.status, func.count(AutomationEnrollment.id))
            .where(AutomationEnrollment.automation_id == automation.id)
            .group_by(AutomationEnrollment.status)
        ).all()
    )
    return {
        "automation_id": automation.id,
        "name": automation.name,
        "kind": automation.kind,
        "status": automation.status,
        "sends_by_status": by_status,
        "skips_by_reason": by_skip,
        "enrollments": enrollments,
        "active_enrollments": enrollments.get(EnrollmentStatus.ACTIVE.value, 0),
        "total_sent": by_status.get(SendStatus.SENT.value, 0)
        + by_status.get(SendStatus.DELIVERED.value, 0),
        "total_failed": by_status.get(SendStatus.FAILED.value, 0),
        "total_skipped": by_status.get(SendStatus.SKIPPED.value, 0),
        "last_run_at": automation.last_run_at.isoformat() if automation.last_run_at else None,
        "next_run_at": automation.next_run_at.isoformat() if automation.next_run_at else None,
    }
