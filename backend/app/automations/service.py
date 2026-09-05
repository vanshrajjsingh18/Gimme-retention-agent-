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
    SequenceTrigger,
)
from app.models.base import utcnow
from app.models.entities import (
    AuditLog,
    Automation,
    Customer,
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
    trigger_type: str | None = None,
    message_template: str = "",
    message_variants: list[str] | None = None,
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
    if kind == AutomationKind.SEQUENCE.value and not steps:
        # Checked before anything is written, so a rejected sequence does not
        # leave an orphaned backing campaign behind.
        raise AutomationError("A sequence needs at least one step.")
    if db.execute(select(Automation.id).where(Automation.name == name)).first():
        raise AutomationError(f"An automation named '{name}' already exists.")

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
        trigger_type=trigger_type or SequenceTrigger.SEGMENT_ENTRY.value,
        message_template=message_template,
        message_variants=list(message_variants or []),
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

    db.commit()
    logger.info("Created %s automation '%s' (id=%s)", kind, name, automation.id)
    return automation


def _default_enrollment_mode(kind: str) -> str:
    from app.core.enums import EnrollmentMode

    # A nudge is a standing automation, so new matching customers must be able
    # to join it; a sequence defaults to rolling for the same reason but is
    # commonly locked, which is why it is a per-campaign toggle.
    return EnrollmentMode.ROLLING.value


#: Fields whose change means the approved thing is no longer what would be
#: sent. Approval attaches to the message, not merely to the automation's
#: existence, so editing any of these withdraws it.
APPROVAL_SENSITIVE_FIELDS = {
    "message_template",
    "template_overrides",
    "segment_id",
    "manual_customer_ids",
    "config",
}


def revoke_approval(
    db: Session, automation: Automation, *, reason: str, actor: str = "system"
) -> bool:
    """Withdraw approval after a change to what would be sent.

    Returns whether anything changed. An automation that never required
    approval is left alone: the operator turned that gate off deliberately.

    An active automation is also paused, because an approved-looking campaign
    that silently stops sending is worse than one that visibly needs attention.
    """
    if not automation.require_approval or automation.approved_at is None:
        return False

    automation.approved_at = None
    automation.approved_by_id = None
    was_active = automation.status == AutomationStatus.ACTIVE.value
    if was_active:
        automation.status = AutomationStatus.PAUSED.value
        automation.next_run_at = None

    db.add(
        AuditLog(
            actor=actor,
            action="AUTOMATION_APPROVAL_REVOKED",
            entity_type="automation",
            entity_id=str(automation.id),
            detail={"reason": reason, "was_active": was_active},
        )
    )
    logger.info(
        "Approval revoked for automation %s (%s); %s",
        automation.id,
        reason,
        "paused" if was_active else "still a draft",
    )
    return True


def apply_update(
    db: Session, automation: Automation, changes: dict, *, actor: str = "system"
) -> dict:
    """Apply an update, withdrawing approval if it changes what would be sent.

    Returns a summary including whether re-approval is now needed, so the
    caller can tell the operator rather than letting them discover it when
    nothing sends.
    """
    for key, value in changes.items():
        setattr(automation, key, value.value if hasattr(value, "value") else value)

    touched = sorted(set(changes) & APPROVAL_SENSITIVE_FIELDS)
    revoked = False
    if touched:
        revoked = revoke_approval(
            db,
            automation,
            reason=f"Changed {', '.join(touched)}.",
            actor=actor,
        )

    db.add(
        AuditLog(
            actor=actor,
            action="AUTOMATION_UPDATED",
            entity_type="automation",
            entity_id=str(automation.id),
            detail={"fields": sorted(changes), "approval_revoked": revoked},
        )
    )
    db.commit()
    return {"fields": sorted(changes), "approval_revoked": revoked}


def replace_steps(
    db: Session, automation: Automation, steps: list[dict], *, actor: str = "system"
) -> Automation:
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
    # New steps are new copy, and approval was given for the old copy.
    revoke_approval(db, automation, reason="Sequence steps rewritten.", actor=actor)
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


def set_enrollment_paused(
    db: Session, enrollment: AutomationEnrollment, *, paused: bool, actor: str = "system"
) -> AutomationEnrollment:
    """Hold or release one customer's place in an automation.

    Distinct from STOPPED, which is the system deciding they are done (opted
    out, ordered, campaign ended). A pause is a human saying "not this one, not
    now", and it is reversible without losing their progress.
    """
    if paused and enrollment.status != EnrollmentStatus.ACTIVE.value:
        raise AutomationError(
            f"Only an active enrollment can be paused (currently {enrollment.status})."
        )
    if not paused and enrollment.status != EnrollmentStatus.PAUSED.value:
        raise AutomationError(
            f"Only a paused enrollment can be resumed (currently {enrollment.status})."
        )

    enrollment.status = (
        EnrollmentStatus.PAUSED.value if paused else EnrollmentStatus.ACTIVE.value
    )
    enrollment.stop_reason = "Paused by an operator." if paused else None
    db.add(
        AuditLog(
            actor=actor,
            action="ENROLLMENT_PAUSED" if paused else "ENROLLMENT_RESUMED",
            entity_type="automation_enrollment",
            entity_id=str(enrollment.id),
            detail={
                "automation_id": enrollment.automation_id,
                "customer_id": enrollment.customer_id,
                "current_step": enrollment.current_step,
            },
        )
    )
    db.commit()
    return enrollment


def enroll_customers(
    db: Session,
    automation: Automation,
    customer_ids: list[int],
    *,
    now: datetime | None = None,
    actor: str = "system",
) -> dict:
    """Enrol named customers by hand — the MANUAL sequence trigger.

    Also usable to add somebody to any sequence out of band. Already-enrolled
    customers are left exactly as they are rather than being reset to step one.
    """
    from app.automations.sequences import resolve_trigger_at

    if automation.kind != AutomationKind.SEQUENCE.value:
        raise AutomationError("Only a sequence takes manual enrollments.")

    now = now or utcnow()
    existing = set(
        db.execute(
            select(AutomationEnrollment.customer_id).where(
                AutomationEnrollment.automation_id == automation.id
            )
        )
        .scalars()
        .all()
    )
    wanted = [cid for cid in customer_ids if cid not in existing]
    customers = {
        c.id: c
        for c in db.execute(select(Customer).where(Customer.id.in_(wanted))).scalars().all()
    } if wanted else {}

    enrolled, missing, no_trigger = 0, 0, 0
    for customer_id in wanted:
        customer = customers.get(customer_id)
        if customer is None:
            missing += 1
            continue
        trigger_at = resolve_trigger_at(automation, customer, now=now)
        if trigger_at is None:
            no_trigger += 1
            continue
        db.add(
            AutomationEnrollment(
                automation_id=automation.id,
                customer_id=customer_id,
                status=EnrollmentStatus.ACTIVE.value,
                enrolled_at=now,
                trigger_at=trigger_at,
                current_step=0,
            )
        )
        enrolled += 1

    db.add(
        AuditLog(
            actor=actor,
            action="ENROLLMENTS_ADDED",
            entity_type="automation",
            entity_id=str(automation.id),
            detail={"requested": len(customer_ids), "enrolled": enrolled},
        )
    )
    db.commit()
    return {
        "enrolled": enrolled,
        "already_enrolled": len(customer_ids) - len(wanted),
        "unknown_customers": missing,
        "skipped_no_trigger": no_trigger,
    }


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
