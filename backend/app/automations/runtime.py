"""The shared send pipeline for every automation type.

Each feature module decides *who* to message and *when*; everything after that
is identical, and lives here so that a safety property cannot be correct for
one campaign type and wrong for another:

  1. **Consent at send time.** Eligibility is re-evaluated for every recipient
     at dispatch, never trusted from campaign creation or audience preview.
     A customer who opted out an hour ago is dropped here.
  2. **Quiet hours.** A candidate outside the local send window is deferred to
     the next open slot, not dropped, so a job that happens to run at 3am does
     not silently lose the day's sends.
  3. **Dedup.** One automated message per customer per *local* day. Contests
     are resolved by automation priority, and the loser is recorded with the
     reason rather than vanishing.
  4. **Dispatch and ledger.** Every attempt writes an ``AutomationSend`` row —
     sent, failed or skipped — so a campaign is auditable after the fact.
  5. **Dry run.** The same pipeline, short of the provider call, producing
     PREVIEW rows: what a live run would do, decided by the same code.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.compliance.engine import ComplianceConfig, check_content, check_recipient
from app.core.config import settings
from app.core.enums import (
    AUTOMATION_PRIORITY,
    AutomationStatus,
    Channel,
    EventType,
    MessageStatus,
    RecipientStatus,
    SendStatus,
    SkipReason,
)
from app.core.timezones import in_send_window, local_date, next_send_slot, to_local
from app.campaigns.service import build_recipient_view
from app.models.base import utcnow
from app.models.entities import (
    Automation,
    AutomationSend,
    Customer,
    Message,
)
from app.services.brand import build_compliance_config
from app.services.events import record_communication_event
from app.services.messaging import generate_message
from app.integrations.registry import get_adapter

logger = logging.getLogger(__name__)

#: How a blocked recipient status maps onto a skip reason in the ledger.
SKIP_BY_RECIPIENT_STATUS: dict[str, SkipReason] = {
    RecipientStatus.EXCLUDED_NO_CONSENT.value: SkipReason.NO_CONSENT,
    RecipientStatus.EXCLUDED_SUPPRESSED.value: SkipReason.SUPPRESSED,
    RecipientStatus.EXCLUDED_AGE.value: SkipReason.AGE_NOT_VERIFIED,
    RecipientStatus.EXCLUDED_MISSING_CONTACT.value: SkipReason.MISSING_CONTACT,
    RecipientStatus.EXCLUDED_FREQUENCY_CAP.value: SkipReason.FREQUENCY_CAP,
    RecipientStatus.EXCLUDED_QUIET_HOURS.value: SkipReason.QUIET_HOURS,
}

SENT_EVENT_BY_CHANNEL = {
    Channel.EMAIL: EventType.EMAIL_SENT,
    Channel.SMS: EventType.SMS_SENT,
    Channel.WHATSAPP: EventType.WHATSAPP_SENT,
    Channel.PUSH: EventType.EMAIL_SENT,
}

#: Ledger states that occupy a customer's day. A SCHEDULED row is a claim that
#: can still be displaced; the rest have already left the building.
IMMOVABLE_STATUSES = (
    SendStatus.QUEUED.value,
    SendStatus.SENT.value,
    SendStatus.DELIVERED.value,
)


class AutomationError(RuntimeError):
    """Raised when an automation cannot run in its current state."""


@dataclass
class Candidate:
    """One proposed message, before any of the shared gates have run."""

    customer_id: int
    scheduled_for: datetime
    body: str
    step_id: int | None = None
    enrollment_id: int | None = None
    #: Which wording this recipient got, when the automation has variants.
    variant_index: int | None = None
    #: Draft this recipient's copy with the LLM instead of sending `body` as
    #: written. `body` stays as the fallback, so a failed generation still
    #: sends approved wording rather than nothing.
    generate: bool = False
    #: Why this candidate exists — surfaced in dry-run previews so an operator
    #: can see the reasoning, e.g. the order pattern or the offer decision.
    context: dict = field(default_factory=dict)


@dataclass
class SendDecision:
    """What the pipeline decided for one candidate."""

    customer_id: int
    status: SendStatus
    scheduled_for: datetime
    local_date: date
    body: str = ""
    skip_reason: SkipReason | None = None
    skip_detail: str | None = None
    customer_name: str = ""
    to: str | None = None
    context: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "customer_id": self.customer_id,
            "customer_name": self.customer_name,
            "to": _mask(self.to),
            "status": self.status.value,
            "scheduled_for": self.scheduled_for.isoformat(),
            "scheduled_for_local": to_local(self.scheduled_for).isoformat(),
            "local_date": self.local_date.isoformat(),
            "skip_reason": self.skip_reason.value if self.skip_reason else None,
            "skip_detail": self.skip_detail,
            "body": self.body,
            "context": self.context,
        }


@dataclass
class RunReport:
    """The outcome of one automation run."""

    automation_id: int
    automation_name: str
    kind: str
    dry_run: bool
    ran_at: datetime
    results: list[SendDecision] = field(default_factory=list)
    provider: str = ""
    is_mock: bool = True

    @property
    def sent(self) -> int:
        return sum(1 for r in self.results if r.status == SendStatus.SENT)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if r.status == SendStatus.FAILED)

    @property
    def skipped(self) -> int:
        return sum(1 for r in self.results if r.status == SendStatus.SKIPPED)

    @property
    def previewed(self) -> int:
        return sum(1 for r in self.results if r.status == SendStatus.PREVIEW)

    def skips_by_reason(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for r in self.results:
            if r.skip_reason is not None:
                counts[r.skip_reason.value] = counts.get(r.skip_reason.value, 0) + 1
        return counts

    def as_dict(self, *, sample_size: int = 50) -> dict:
        return {
            "automation_id": self.automation_id,
            "automation_name": self.automation_name,
            "kind": self.kind,
            "dry_run": self.dry_run,
            "ran_at": self.ran_at.isoformat(),
            "candidates": len(self.results),
            "sent": self.sent,
            "failed": self.failed,
            "skipped": self.skipped,
            "previewed": self.previewed,
            "skips_by_reason": self.skips_by_reason(),
            "provider": self.provider,
            "is_mock": self.is_mock,
            "recipients": [r.as_dict() for r in self.results[:sample_size]],
            "truncated": len(self.results) > sample_size,
        }


def _mask(contact: str | None) -> str | None:
    """Partially redact a phone/email so previews are safe to screenshot."""
    if not contact:
        return None
    if "@" in contact:
        name, _, domain = contact.partition("@")
        head = name[:2] if len(name) > 2 else name[:1]
        return f"{head}***@{domain}"
    return f"{contact[:-4].rstrip()[:4]}***{contact[-3:]}" if len(contact) > 7 else "***"


# --------------------------------------------------------------------------
# Gates
# --------------------------------------------------------------------------
def resolve_send_time(moment: datetime, *, defer: bool = True) -> tuple[datetime, bool]:
    """Move a send time inside the local business window.

    Returns ``(when, was_deferred)``. With ``defer=False`` the caller wants a
    hard skip instead — used where a late message would be pointless rather
    than merely late.
    """
    start, end = settings.send_window
    if in_send_window(moment, start, end):
        return moment, False
    if not defer:
        return moment, True
    return next_send_slot(moment, start, end), True


def claimed_days(
    db: Session, customer_ids: list[int], days: list[date]
) -> dict[tuple[int, date], AutomationSend]:
    """Existing ledger rows occupying (customer, local day), for dedup.

    Only live rows count: a previous run's SKIPPED or PREVIEW row does not
    reserve the day.
    """
    if not customer_ids or not days:
        return {}
    rows = (
        db.execute(
            select(AutomationSend).where(
                AutomationSend.customer_id.in_(customer_ids),
                AutomationSend.local_date.in_(days),
                AutomationSend.is_dry_run.is_(False),
                AutomationSend.status.in_(
                    (*IMMOVABLE_STATUSES, SendStatus.SCHEDULED.value)
                ),
            )
        )
        .scalars()
        .all()
    )
    claims: dict[tuple[int, date], AutomationSend] = {}
    for row in rows:
        key = (row.customer_id, row.local_date)
        current = claims.get(key)
        # Keep the strongest claim: an already-sent row beats a scheduled one,
        # and among equals the higher priority wins.
        if current is None or _claim_rank(row) > _claim_rank(current):
            claims[key] = row
    return claims


def _claim_rank(row: AutomationSend) -> tuple[int, int]:
    return (1 if row.status in IMMOVABLE_STATUSES else 0, row.priority)


def priority_for(automation: Automation) -> int:
    return AUTOMATION_PRIORITY.get(automation.kind, 0)


# --------------------------------------------------------------------------
# The pipeline
# --------------------------------------------------------------------------
def execute_candidates(
    db: Session,
    automation: Automation,
    candidates: list[Candidate],
    *,
    now: datetime | None = None,
    dry_run: bool = False,
    config: ComplianceConfig | None = None,
    defer_outside_window: bool = True,
) -> RunReport:
    """Run every shared gate over a feature's candidates, then dispatch.

    A dry run takes exactly the same path and stops before the provider call,
    which is what makes the preview trustworthy: it is not a separate
    simulation of the rules, it is the rules.
    """
    now = now or utcnow()
    config = config or build_compliance_config(db)
    channel = Channel(automation.channel)
    priority = priority_for(automation)

    if not dry_run:
        _assert_sendable(automation)

    adapter = get_adapter(db, channel)
    report = RunReport(
        automation_id=automation.id,
        automation_name=automation.name,
        kind=automation.kind,
        dry_run=dry_run,
        ran_at=now,
        provider=adapter.provider,
        is_mock=getattr(adapter, "is_mock", True),
    )

    # Resolve timing first, so dedup is computed against the day a message
    # would actually land on rather than the day it was proposed for.
    resolved: list[tuple[Candidate, datetime, date, bool]] = []
    for candidate in candidates:
        when, deferred = resolve_send_time(
            candidate.scheduled_for, defer=defer_outside_window
        )
        resolved.append((candidate, when, local_date(when), deferred))

    claims = claimed_days(
        db,
        [c.customer_id for c, _, _, _ in resolved],
        sorted({day for _, _, day, _ in resolved}),
    )
    # Claims taken during this run itself, so one automation cannot double-send
    # to the same customer on the same day within a single batch either.
    batch_claims: set[tuple[int, date]] = set()

    customers = _load_customers(db, [c.customer_id for c, _, _, _ in resolved])

    for candidate, when, day, deferred in resolved:
        customer = customers.get(candidate.customer_id)
        if customer is None:
            continue

        result = SendDecision(
            customer_id=candidate.customer_id,
            status=SendStatus.PREVIEW if dry_run else SendStatus.SCHEDULED,
            scheduled_for=when,
            local_date=day,
            body=candidate.body,
            customer_name=customer.full_name,
            to=customer.email if channel == Channel.EMAIL else customer.phone,
            context=dict(candidate.context),
        )
        if deferred and not defer_outside_window:
            _skip(
                result,
                SkipReason.QUIET_HOURS,
                f"{to_local(candidate.scheduled_for):%H:%M} local is outside the "
                f"{settings.SEND_WINDOW_START}-{settings.SEND_WINDOW_END} send window.",
                dry_run=dry_run,
            )
            _record(db, automation, candidate, result, priority=priority, dry_run=dry_run)
            report.results.append(result)
            continue
        if deferred:
            result.context["deferred_from"] = candidate.scheduled_for.isoformat()

        # 1. Consent, suppression, age, frequency cap — evaluated now, at send
        #    time, against the customer's current state.
        status, reason = check_recipient(
            build_recipient_view(db, customer, now=now), channel, config, send_time=when
        )
        if status != RecipientStatus.ELIGIBLE:
            _skip(
                result,
                SKIP_BY_RECIPIENT_STATUS.get(status.value, SkipReason.NO_CONSENT),
                reason,
                dry_run=dry_run,
            )
            _record(db, automation, candidate, result, priority=priority, dry_run=dry_run)
            report.results.append(result)
            continue

        # 2. Dedup: one automated message per customer per local day.
        key = (candidate.customer_id, day)
        blocker = claims.get(key)
        if key in batch_claims or blocker is not None:
            detail = _dedup_detail(blocker, priority, automation)
            if detail is not None:
                _skip(result, SkipReason.DEDUPED, detail, dry_run=dry_run)
                _record(
                    db, automation, candidate, result, priority=priority, dry_run=dry_run
                )
                report.results.append(result)
                continue
            # We outrank a merely-scheduled send: displace it.
            _displace(db, blocker, automation)
            claims.pop(key, None)

        # 3. Per-recipient copy, for a step that asked for it. Deliberately
        #    after the eligibility gate: somebody who withdrew consent should
        #    not have their history fed to a model to write a message that is
        #    never going to be sent.
        if candidate.generate:
            body, note = _generated_body(
                db,
                customer=customer,
                automation=automation,
                channel=channel,
                fallback=candidate.body,
                config=config,
            )
            candidate.body = body
            result.body = body
            result.context["llm"] = note

        # 4. Content compliance on the body as it will actually go out.
        findings = check_content(candidate.body, config, channel=channel)
        blocking = [f for f in findings if f.blocks_send]
        if blocking:
            _skip(
                result,
                SkipReason.VALIDATION_FAILED,
                "; ".join(f.message for f in blocking),
                dry_run=dry_run,
            )
            _record(db, automation, candidate, result, priority=priority, dry_run=dry_run)
            report.results.append(result)
            continue

        batch_claims.add(key)

        if dry_run:
            result.status = SendStatus.PREVIEW
            _record(db, automation, candidate, result, priority=priority, dry_run=True)
            report.results.append(result)
            continue

        _dispatch(
            db,
            automation=automation,
            candidate=candidate,
            customer=customer,
            result=result,
            channel=channel,
            adapter=adapter,
            priority=priority,
            now=now,
        )
        report.results.append(result)

    if not dry_run:
        automation.last_run_at = now
        automation.total_sent += report.sent
        automation.total_skipped += report.skipped
        automation.total_failed += report.failed
    db.commit()

    logger.info(
        "Automation %s (%s) %s: %d sent, %d skipped, %d failed",
        automation.name,
        automation.kind,
        "dry run" if dry_run else "run",
        report.sent or report.previewed,
        report.skipped,
        report.failed,
    )
    return report


def _assert_sendable(automation: Automation) -> None:
    if automation.status != AutomationStatus.ACTIVE.value:
        raise AutomationError(
            f"Automation '{automation.name}' must be ACTIVE to send "
            f"(current status: {automation.status}). Dry runs are always available."
        )
    if automation.require_approval and automation.approved_at is None:
        raise AutomationError(
            f"Automation '{automation.name}' requires human approval before it can send."
        )


def _load_customers(db: Session, ids: list[int]) -> dict[int, Customer]:
    if not ids:
        return {}
    rows = db.execute(select(Customer).where(Customer.id.in_(set(ids)))).scalars().all()
    return {c.id: c for c in rows}


def _skip(
    result: SendDecision, reason: SkipReason, detail: str | None, *, dry_run: bool
) -> None:
    # A dry run still reports SKIPPED, not PREVIEW: the point of the preview is
    # to show who would *not* receive the message, and why.
    result.status = SendStatus.SKIPPED
    result.skip_reason = reason
    result.skip_detail = detail


def _dedup_detail(
    blocker: AutomationSend | None, priority: int, automation: Automation
) -> str | None:
    """Explain a lost dedup contest, or None when this candidate should win."""
    if blocker is None:
        return (
            f"Another message from '{automation.name}' is already scheduled for this "
            "customer today."
        )
    if blocker.status in IMMOVABLE_STATUSES:
        # Already gone — priority is irrelevant, a sent message cannot be recalled.
        return (
            f"Customer already received an automated message today "
            f"(automation #{blocker.automation_id}, status {blocker.status})."
        )
    if blocker.priority >= priority:
        return (
            f"Higher-or-equal priority automation #{blocker.automation_id} "
            f"(priority {blocker.priority} vs {priority}) already holds this customer's day."
        )
    return None


def _displace(db: Session, blocker: AutomationSend, automation: Automation) -> None:
    """Stand down a lower-priority scheduled send in favour of this one."""
    blocker.status = SendStatus.SKIPPED.value
    blocker.skip_reason = SkipReason.DEDUPED.value
    blocker.skip_detail = (
        f"Displaced by higher-priority automation '{automation.name}' "
        f"({automation.kind}) on the same day."
    )
    db.add(blocker)


def _record(
    db: Session,
    automation: Automation,
    candidate: Candidate,
    result: SendDecision,
    *,
    priority: int,
    dry_run: bool,
    message_id: int | None = None,
    provider: str = "",
    provider_message_id: str | None = None,
) -> AutomationSend | None:
    """Write the ledger row for one candidate, whatever the outcome.

    The idempotency key makes a re-run of the same day a no-op rather than a
    second message: if the row already exists, the write is abandoned and the
    existing row returned. That is the backstop behind the dedup query — a
    crash mid-run, or two workers racing, cannot produce a duplicate send.
    """
    key = send_idempotency_key(automation, candidate, result, dry_run=dry_run)
    row = AutomationSend(
        automation_id=automation.id,
        step_id=candidate.step_id,
        enrollment_id=candidate.enrollment_id,
        customer_id=candidate.customer_id,
        message_id=message_id,
        campaign_id=automation.campaign_id,
        channel=automation.channel,
        status=result.status.value,
        skip_reason=result.skip_reason.value if result.skip_reason else None,
        skip_detail=result.skip_detail,
        scheduled_for=result.scheduled_for,
        local_date=result.local_date,
        sent_at=result.scheduled_for if result.status == SendStatus.SENT else None,
        body=result.body,
        generated=(result.context.get("llm") or {}).get("used") == "generated",
        provider=provider or "preview",
        provider_message_id=provider_message_id,
        is_dry_run=dry_run,
        priority=priority,
        variant_index=candidate.variant_index,
        idempotency_key=key,
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        logger.debug("Ledger row %s already exists; not writing a duplicate.", key)
        return db.execute(
            select(AutomationSend).where(AutomationSend.idempotency_key == key)
        ).scalar_one_or_none()
    return row


def send_idempotency_key(
    automation: Automation, candidate: Candidate, result: SendDecision, *, dry_run: bool
) -> str:
    """Identity of one ledger row.

    A *send* is keyed by (automation, step, customer, day) alone, which is what
    makes a replay safe: the second attempt collides and is dropped. A skip is
    keyed by its reason as well, because one customer can legitimately be
    skipped for different reasons by different candidates on the same day, and
    each of those is worth recording.
    """
    parts = [
        "dry" if dry_run else "live",
        f"a{automation.id}",
        f"s{candidate.step_id or 0}",
        f"c{candidate.customer_id}",
        result.local_date.isoformat(),
    ]
    if result.skip_reason is not None:
        parts.append(result.skip_reason.value)
    return ":".join(parts)


def _generated_body(
    db: Session,
    *,
    customer: Customer,
    automation: Automation,
    channel: Channel,
    fallback: str,
    config: ComplianceConfig,
) -> tuple[str, dict]:
    """Draft this recipient's copy with the LLM, falling back to the template.

    Generation is grounded — the model only sees verified facts about this
    customer — and the result is validated before it is accepted. Anything that
    fails, for any reason, falls back to the step's own wording rather than
    skipping the customer: the fallback is copy an operator already approved,
    so the worst case is a less personal message, not a missed one.

    Whatever comes back still goes through the same compliance gate as
    hand-written copy. Nothing here is a way around it.
    """
    note: dict = {"requested": True}
    try:
        message = generate_message(
            db,
            customer,
            channel=channel,
            objective=automation.objective,
            campaign_id=automation.campaign_id,
            campaign_name=automation.name,
            config=config,
            persist=False,
        )
    except Exception as exc:  # noqa: BLE001 - a bad draft must not stop the run
        logger.warning(
            "Generation failed for customer %s on automation %s: %s",
            customer.id,
            automation.id,
            exc,
        )
        return fallback, {**note, "used": "template", "reason": "generation_error"}

    if message.status != MessageStatus.GENERATED.value or not (message.body or "").strip():
        errors = (message.validation_result or {}).get("errors") or []
        reason = errors[0].get("code") if errors and isinstance(errors[0], dict) else "invalid"
        return fallback, {**note, "used": "template", "reason": reason}

    # Check the draft here, not only at the gate below, so a bad draft costs
    # the personalisation rather than the message. The gate still runs on
    # whichever body wins — this is an extra check, never a substitute.
    blocking = [f for f in check_content(message.body, config, channel=channel) if f.blocks_send]
    if blocking:
        logger.info(
            "Discarded a draft for customer %s on automation %s: %s",
            customer.id,
            automation.id,
            blocking[0].code,
        )
        return fallback, {**note, "used": "template", "reason": blocking[0].code}

    return message.body, {
        **note,
        "used": "generated",
        "provider": message.llm_provider,
        "model": message.llm_model,
    }


def _dispatch(
    db: Session,
    *,
    automation: Automation,
    candidate: Candidate,
    customer: Customer,
    result: SendDecision,
    channel: Channel,
    adapter,
    priority: int,
    now: datetime,
) -> None:
    """Hand one message to the provider and record what came back."""
    to = customer.email if channel == Channel.EMAIL else customer.phone
    message = Message(
        customer_id=customer.id,
        campaign_id=automation.campaign_id,
        channel=channel.value,
        objective=automation.objective,
        subject=None,
        body=candidate.body,
        original_subject=None,
        original_body=candidate.body,
        status=MessageStatus.APPROVED.value,
    )
    # A drafted message says who drafted it, so the history does not read as
    # though somebody wrote this wording by hand.
    llm = result.context.get("llm") or {}
    if llm.get("used") == "generated":
        message.llm_provider = llm.get("provider")
        message.llm_model = llm.get("model")
        message.generated_at = now
    db.add(message)
    db.flush()

    outcome = adapter.send_message(
        to=to or "",
        subject=None,
        body=candidate.body,
        metadata={
            "automation_id": automation.id,
            "automation_kind": automation.kind,
            "step_id": candidate.step_id,
        },
    )

    message.provider = adapter.provider
    message.provider_message_id = outcome.provider_message_id

    if outcome.success:
        result.status = SendStatus.SENT
        message.status = MessageStatus.SENT.value
        message.sent_at = now
    else:
        result.status = SendStatus.FAILED
        result.skip_detail = outcome.error
        message.status = MessageStatus.FAILED.value
        message.error_message = outcome.error

    row = _record(
        db,
        automation,
        candidate,
        result,
        priority=priority,
        dry_run=False,
        message_id=message.id,
        provider=adapter.provider,
        provider_message_id=outcome.provider_message_id,
    )
    if not outcome.success:
        row.error_message = outcome.error

    record_communication_event(
        db,
        event_type=SENT_EVENT_BY_CHANNEL[channel]
        if outcome.success
        else EventType.MESSAGE_FAILED,
        customer_id=customer.id,
        campaign_id=automation.campaign_id,
        message_id=message.id,
        channel=channel,
        provider=adapter.provider,
        occurred_at=now,
        is_simulated=outcome.is_simulated,
        payload={
            "automation_id": automation.id,
            "provider_message_id": outcome.provider_message_id,
            "error": outcome.error,
        },
    )
