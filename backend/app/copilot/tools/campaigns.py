"""Campaigns: one-off sends (Campaign) and automations (Smart Reorder, sequences, bulk).

"Campaign" in conversation means either. Tools take ``automation_id`` or
``campaign_id``; with neither, they act on whatever the conversation is focused on.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select

from app.copilot.registry import Risk, ToolContext, ToolError, ToolResult, tool
from app.copilot.tools._common import (
    automation_for,
    automation_snapshot,
    campaign_for,
    campaign_snapshot,
    money,
)
from app.copilot.tools.messaging import validate_copy
from app.copilot.tools.smart_reorder import AUDIENCE_PROPS, resolve_audience_args
from app.core.enums import (
    AutomationKind,
    AutomationStatus,
    CampaignObjective,
    CampaignStatus,
    CancellationReason,
    Channel,
)
from app.core.timezones import to_utc_naive
from app.models.entities import AuditLog, Automation, Campaign

GROUP = "campaigns"
KIND_FILTER = {
    "smart_reorder": AutomationKind.NUDGE.value,
    "sequence": AutomationKind.SEQUENCE.value,
    "bulk": AutomationKind.COHORT_BULK.value,
}
TARGET_PROPS = {
    "automation_id": {"type": "integer", "description": "An automation (Smart Reorder, sequence, bulk)."},
    "campaign_id": {"type": "integer", "description": "A one-off campaign."},
}


def _target(ctx: ToolContext, automation_id: int | None, campaign_id: int | None):
    if automation_id is not None:
        return automation_for(ctx, automation_id)
    if campaign_id is not None:
        return campaign_for(ctx, campaign_id)
    entity_type, _ = ctx.active
    if entity_type == "campaign":
        return campaign_for(ctx, None)
    return automation_for(ctx, None)


def _automation_row(db, a: Automation) -> dict:
    backing = db.get(Campaign, a.campaign_id) if a.campaign_id else None
    return {
        "type": "automation",
        "id": a.id,
        "name": a.name,
        "kind": {"NUDGE": "Smart Reorder", "SEQUENCE": "Sequence", "COHORT_BULK": "Bulk"}.get(a.kind, a.kind),
        "status": a.status,
        "channel": a.channel,
        "ends_at": a.ends_at.isoformat() if a.ends_at else None,
        "messages_sent": a.total_sent,
        "conversions": backing.conversions if backing else 0,
        "attributed_revenue": money(backing.attributed_revenue if backing else 0),
    }


def _campaign_row(c: Campaign) -> dict:
    return {
        "type": "campaign",
        "id": c.id,
        "name": c.name,
        "kind": "One-off",
        "status": c.status,
        "channel": c.channel,
        "scheduled_at": c.scheduled_at.isoformat() if c.scheduled_at else None,
        "messages_sent": c.messages_sent,
        "conversions": c.conversions,
        "attributed_revenue": money(c.attributed_revenue),
    }


def _matching(ctx: ToolContext, *, kind=None, status=None, channel=None, name_contains=None,
              ending_within_days=None, zero_conversions=None) -> list[tuple[str, object, dict]]:
    db = ctx.db
    out: list[tuple[str, object, dict]] = []
    if kind in (None, "all", *KIND_FILTER):
        query = select(Automation).where(Automation.status != AutomationStatus.ARCHIVED.value)
        if kind in KIND_FILTER:
            query = query.where(Automation.kind == KIND_FILTER[kind])
        for a in db.execute(query.order_by(Automation.id)).scalars():
            out.append(("automation", a, _automation_row(db, a)))
    if kind in (None, "all", "one_off"):
        backing_ids = select(Automation.campaign_id).where(Automation.campaign_id.is_not(None))
        query = select(Campaign).where(
            Campaign.is_automation_backing.is_(False), Campaign.id.not_in(backing_ids)
        )
        for c in db.execute(query.order_by(Campaign.id)).scalars():
            out.append(("campaign", c, _campaign_row(c)))

    def keep(row: dict) -> bool:
        if status and row["status"] != status.upper():
            return False
        if channel and row["channel"] != channel.upper():
            return False
        if name_contains and name_contains.lower() not in row["name"].lower():
            return False
        if zero_conversions and row["conversions"]:
            return False
        if ending_within_days is not None:
            if not row.get("ends_at"):
                return False
            ends = datetime.fromisoformat(row["ends_at"])
            if not ctx.now <= ends <= ctx.now + timedelta(days=ending_within_days):
                return False
        return True

    return [item for item in out if keep(item[2])]


FILTER_PROPS = {
    "kind": {"type": "string", "enum": ["all", "smart_reorder", "sequence", "bulk", "one_off"]},
    "status": {"type": "string", "description": "e.g. DRAFT, ACTIVE, PAUSED, SCHEDULED"},
    "channel": {"type": "string", "enum": [c.value for c in Channel]},
    "name_contains": {"type": "string"},
    "ending_within_days": {"type": "integer"},
    "zero_conversions": {"type": "boolean"},
}


@tool(
    "list_campaigns",
    group=GROUP,
    risk=Risk.READ,
    description="Campaigns and automations with status, channel, sends, conversions and attributed revenue. Filterable.",
    properties=FILTER_PROPS,
)
def list_campaigns(ctx: ToolContext, **filters) -> ToolResult:
    rows = [row for _, _, row in _matching(ctx, **filters)]
    return ToolResult.ok(rows, metadata={"filters": filters})


@tool(
    "get_campaign",
    group=GROUP,
    risk=Risk.READ,
    description="Full configuration and performance of one campaign or automation. Focuses the conversation on it.",
    properties=TARGET_PROPS,
)
def get_campaign(ctx: ToolContext, automation_id: int | None = None, campaign_id: int | None = None) -> ToolResult:
    from app.automations.service import automation_stats

    target = _target(ctx, automation_id, campaign_id)
    if isinstance(target, Automation):
        data = automation_snapshot(ctx.db, target)
        data["stats"] = automation_stats(ctx.db, target)
        if target.kind == AutomationKind.NUDGE.value:
            from app.services.smart_reorder_queue import dashboard

            data["queue"] = dashboard(ctx.db, target, now=ctx.now)
        return ToolResult.ok(data, focus=("automation", target.id))
    return ToolResult.ok(campaign_snapshot(ctx.db, target), focus=("campaign", target.id))


@tool(
    "preview_campaign",
    group=GROUP,
    risk=Risk.READ,
    description=(
        "Preview a campaign before activating it, writing nothing. Smart Reorder → full dry run; "
        "one-off → eligible/excluded audience and rendered copy samples; other automations → the "
        "engine's dry-run report."
    ),
    properties=TARGET_PROPS,
)
def preview_campaign(ctx: ToolContext, automation_id: int | None = None, campaign_id: int | None = None) -> ToolResult:
    from app.automations.service import preview
    from app.campaigns.service import preview_audience, preview_copy

    target = _target(ctx, automation_id, campaign_id)
    if isinstance(target, Automation):
        if target.kind == AutomationKind.NUDGE.value:
            from app.copilot.tools.smart_reorder import preview_smart_reorder

            return preview_smart_reorder(ctx, automation_id=target.id)
        report = preview(ctx.db, target, now=ctx.now)
        return ToolResult.ok(report, metadata={"dry_run": True}, focus=("automation", target.id))
    audience = preview_audience(ctx.db, target)
    audience.pop("_decisions", None)
    for row in audience.get("sample_recipients", []):
        row.pop("email", None)
        row.pop("phone", None)
    return ToolResult.ok(
        {"audience": audience, "copy": preview_copy(ctx.db, target, count=3)},
        metadata={"dry_run": True},
        focus=("campaign", target.id),
    )


@tool(
    "create_campaign",
    group=GROUP,
    risk=Risk.WRITE,
    creates=True,
    summarize=lambda a: f"Create one-off campaign '{a.get('name')}' as a DRAFT",
    description=(
        "Create a one-off campaign as a DRAFT (nothing is sent or scheduled). For reminders "
        "timed to each customer's predicted reorder, use create_smart_reorder_campaign instead."
    ),
    properties={
        "name": {"type": "string"},
        "description": {"type": "string"},
        "objective": {"type": "string", "enum": [o.value for o in CampaignObjective]},
        "channel": {"type": "string", "enum": [Channel.SMS.value, Channel.WHATSAPP.value, Channel.EMAIL.value]},
        **AUDIENCE_PROPS,
        "subject": {"type": "string", "description": "Email only."},
        "body": {"type": "string"},
        "planned_send_local": {"type": "string", "description": "Intended start, local 'YYYY-MM-DD HH:MM'. Recorded, not scheduled."},
    },
    required=["name", "body"],
)
def create_campaign(ctx: ToolContext, **args) -> ToolResult:
    db = ctx.db
    name = args["name"].strip()
    if db.execute(select(Campaign.id).where(Campaign.name == name)).first():
        raise ToolError(f"A campaign named '{name}' already exists.")
    channel = args.get("channel", Channel.SMS.value)
    check = validate_copy(ctx, args["body"], channel)
    if check["unknown_tags"] or check["blocking_findings"]:
        raise ToolError(
            "The copy cannot be used: "
            + "; ".join(check["unknown_tags"] + [f["message"] for f in check["blocking_findings"]])
        )
    audience_args = {k: args.get(k) for k in AUDIENCE_PROPS}
    segment_id = None
    if any(audience_args.values()):
        if audience_args.get("use_last_result"):
            from app.copilot.tools.segments import create_segment

            created = create_segment(ctx, name=f"{name} — audience", use_last_result=True)
            segment_id = created.target_id
        else:
            segment_id, _, _ = resolve_audience_args(ctx, audience_args, name=name)
    planned = None
    if args.get("planned_send_local"):
        try:
            planned = to_utc_naive(datetime.strptime(args["planned_send_local"], "%Y-%m-%d %H:%M"))
        except ValueError as exc:
            raise ToolError("planned_send_local must look like 2026-10-09 18:00.") from exc
    campaign = Campaign(
        name=name,
        description=args.get("description", "Created by the AI Copilot."),
        objective=args.get("objective", CampaignObjective.RETENTION.value),
        channel=channel,
        segment_id=segment_id,
        status=CampaignStatus.DRAFT.value,
        subject=args.get("subject"),
        body=args["body"],
        scheduled_at=planned,
        created_by_id=ctx.user.id,
    )
    db.add(campaign)
    db.flush()
    db.add(
        AuditLog(actor=ctx.user.email, action="CAMPAIGN_CREATED", entity_type="campaign",
                 entity_id=str(campaign.id), detail={"via": "copilot"})
    )
    db.commit()
    snapshot = campaign_snapshot(db, campaign)
    return ToolResult.ok(snapshot, focus=("campaign", campaign.id), target_type="campaign",
                         target_id=campaign.id, after=snapshot)


@tool(
    "update_campaign",
    group=GROUP,
    risk=Risk.WRITE,
    summarize=lambda a: "Update campaign settings",
    description=(
        "Change a campaign's name, channel, objective or audience. Copy changes go through "
        "update_message_template. Any change to what would be sent withdraws approval."
    ),
    properties={
        **TARGET_PROPS,
        "name": {"type": "string"},
        "channel": {"type": "string", "enum": [Channel.SMS.value, Channel.WHATSAPP.value, Channel.EMAIL.value]},
        "objective": {"type": "string", "enum": [o.value for o in CampaignObjective]},
        **AUDIENCE_PROPS,
    },
)
def update_campaign(ctx: ToolContext, automation_id: int | None = None, campaign_id: int | None = None, **args) -> ToolResult:
    from app.automations.service import apply_update

    db = ctx.db
    target = _target(ctx, automation_id, campaign_id)
    changes = {k: args[k] for k in ("name", "channel", "objective") if args.get(k)}
    if any(args.get(k) for k in AUDIENCE_PROPS):
        segment_id, manual_ids, _ = resolve_audience_args(ctx, args, name=changes.get("name", target.name))
        changes["segment_id"] = segment_id
        if isinstance(target, Automation):
            changes["manual_customer_ids"] = manual_ids
        elif segment_id is None:
            raise ToolError("A one-off campaign needs a segment; save the list as a segment first.")
    if not changes:
        raise ToolError("Nothing to change — no new settings were given.")
    if isinstance(target, Automation):
        before = automation_snapshot(db, target)
        summary = apply_update(db, target, changes, actor=ctx.user.email)
        after = automation_snapshot(db, target)
        return ToolResult.ok({"campaign": after, **summary}, focus=("automation", target.id),
                             target_type="automation", target_id=target.id, before=before, after=after)
    editable = {CampaignStatus.DRAFT.value, CampaignStatus.AI_GENERATED.value, CampaignStatus.VALIDATED.value,
                CampaignStatus.COMPLIANCE_CHECKED.value, CampaignStatus.AWAITING_APPROVAL.value,
                CampaignStatus.APPROVED.value}
    if target.status not in editable:
        raise ToolError(f"A campaign in status {target.status} cannot be edited.")
    before = campaign_snapshot(db, target)
    for key, value in changes.items():
        setattr(target, key, value)
    target.status = CampaignStatus.DRAFT.value
    target.approved_at = None
    target.approved_by_id = None
    target.compliance_result = {}
    db.add(AuditLog(actor=ctx.user.email, action="CAMPAIGN_UPDATED", entity_type="campaign",
                    entity_id=str(target.id), detail={"fields": sorted(changes), "via": "copilot"}))
    db.commit()
    after = campaign_snapshot(db, target)
    return ToolResult.ok(after, focus=("campaign", target.id), target_type="campaign",
                         target_id=target.id, before=before, after=after)


def pause_automation(ctx: ToolContext, automation: Automation) -> ToolResult:
    from app.automations.service import pause

    if automation.status not in (AutomationStatus.ACTIVE.value, AutomationStatus.TESTING.value):
        raise ToolError(f"'{automation.name}' is {automation.status}, not running, so there is nothing to pause.")
    before = automation_snapshot(ctx.db, automation)
    pause(ctx.db, automation)
    ctx.db.add(AuditLog(actor=ctx.user.email, action="AUTOMATION_PAUSED", entity_type="automation",
                        entity_id=str(automation.id), detail={"via": "copilot"}))
    ctx.db.commit()
    after = automation_snapshot(ctx.db, automation)
    return ToolResult.ok(after, focus=("automation", automation.id), target_type="automation",
                         target_id=automation.id, before=before, after=after)


def archive_automation(ctx: ToolContext, automation: Automation) -> ToolResult:
    from app.services.smart_reorder_queue import cancel_open_for_campaign

    if automation.status == AutomationStatus.ARCHIVED.value:
        raise ToolError(f"'{automation.name}' is already archived.")
    before = automation_snapshot(ctx.db, automation)
    automation.status = AutomationStatus.ARCHIVED.value
    automation.next_run_at = None
    cancelled = cancel_open_for_campaign(ctx.db, automation, CancellationReason.CAMPAIGN_ARCHIVED, now=ctx.now)
    ctx.db.add(AuditLog(actor=ctx.user.email, action="AUTOMATION_ARCHIVED", entity_type="automation",
                        entity_id=str(automation.id), detail={"via": "copilot", "messages_cancelled": cancelled}))
    ctx.db.commit()
    after = automation_snapshot(ctx.db, automation)
    return ToolResult.ok({"campaign": after, "pending_messages_cancelled": cancelled},
                         focus=("automation", automation.id), target_type="automation",
                         target_id=automation.id, before=before, after=after)


@tool(
    "pause_campaign",
    group=GROUP,
    risk=Risk.WRITE,
    summarize=lambda a: "Pause campaign",
    description="Pause a running campaign or automation. Nothing further is sent until it is resumed.",
    properties=TARGET_PROPS,
)
def pause_campaign(ctx: ToolContext, automation_id: int | None = None, campaign_id: int | None = None) -> ToolResult:
    from app.campaigns.service import pause_campaign as pause_one_off

    target = _target(ctx, automation_id, campaign_id)
    if isinstance(target, Automation):
        return pause_automation(ctx, target)
    before = campaign_snapshot(ctx.db, target)
    pause_one_off(ctx.db, target)
    after = campaign_snapshot(ctx.db, target)
    return ToolResult.ok(after, focus=("campaign", target.id), target_type="campaign",
                         target_id=target.id, before=before, after=after)


@tool(
    "resume_campaign",
    group=GROUP,
    risk=Risk.HIGH_RISK_WRITE,
    summarize=lambda a: "RESUME campaign — sending restarts",
    description="Resume a paused automation; it starts sending again on its schedule.",
    properties={"automation_id": {"type": "integer"}},
)
def resume_campaign(ctx: ToolContext, automation_id: int | None = None) -> ToolResult:
    from app.automations.runtime import AutomationError
    from app.automations.service import resume

    automation = automation_for(ctx, automation_id)
    before = automation_snapshot(ctx.db, automation)
    try:
        resume(ctx.db, automation, now=ctx.now)
    except AutomationError as exc:
        raise ToolError(str(exc)) from exc
    after = automation_snapshot(ctx.db, automation)
    return ToolResult.ok(after, focus=("automation", automation.id), target_type="automation",
                         target_id=automation.id, before=before, after=after)


@tool(
    "cancel_campaign",
    group=GROUP,
    risk=Risk.HIGH_RISK_WRITE,
    summarize=lambda a: "CANCEL campaign",
    description="Cancel a one-off campaign, or archive an automation and call off its pending messages.",
    properties=TARGET_PROPS,
)
def cancel_campaign(ctx: ToolContext, automation_id: int | None = None, campaign_id: int | None = None) -> ToolResult:
    from app.campaigns.service import cancel_campaign as cancel_one_off

    target = _target(ctx, automation_id, campaign_id)
    if isinstance(target, Automation):
        return archive_automation(ctx, target)
    before = campaign_snapshot(ctx.db, target)
    cancel_one_off(ctx.db, target)
    after = campaign_snapshot(ctx.db, target)
    return ToolResult.ok(after, focus=("campaign", target.id), target_type="campaign",
                         target_id=target.id, before=before, after=after)


def _activate_preview(ctx: ToolContext, args: dict, result: ToolResult) -> dict:
    from app.copilot.tools.smart_reorder import _preview_after

    return _preview_after(ctx, args, result)


@tool(
    "activate_campaign",
    group=GROUP,
    risk=Risk.HIGH_RISK_WRITE,
    previewer=_activate_preview,
    summarize=lambda a: "ACTIVATE campaign — real customers will be messaged",
    description=(
        "Activate an automation (recording the confirming person's approval), or approve and "
        "schedule a one-off campaign for planned_send_local (or its recorded planned time). "
        "Refused when compliance would block the messages."
    ),
    properties={**TARGET_PROPS, "planned_send_local": {"type": "string", "description": "One-off only: 'YYYY-MM-DD HH:MM' local."}},
)
def activate_campaign(ctx: ToolContext, automation_id: int | None = None, campaign_id: int | None = None,
                      planned_send_local: str | None = None) -> ToolResult:
    from app.campaigns.service import CampaignError, approve_campaign, schedule_campaign, snapshot_audience, submit_for_approval
    from app.copilot.tools.smart_reorder import _activate

    target = _target(ctx, automation_id, campaign_id)
    if isinstance(target, Automation):
        return _activate(ctx, target)
    when = target.scheduled_at
    if planned_send_local:
        try:
            when = to_utc_naive(datetime.strptime(planned_send_local, "%Y-%m-%d %H:%M"))
        except ValueError as exc:
            raise ToolError("planned_send_local must look like 2026-10-09 18:00.") from exc
    if when is None:
        raise ToolError("When should it send? Give planned_send_local.")
    if when < ctx.now:
        raise ToolError("That send time is in the past.")
    before = campaign_snapshot(ctx.db, target)
    try:
        snapshot_audience(ctx.db, target)
        if target.status not in (CampaignStatus.AWAITING_APPROVAL.value, CampaignStatus.APPROVED.value):
            submit_for_approval(ctx.db, target, user_id=ctx.user.id)
        if target.status != CampaignStatus.APPROVED.value:
            approve_campaign(ctx.db, target, user_id=ctx.user.id)
        schedule_campaign(ctx.db, target, when)
    except CampaignError as exc:
        raise ToolError(str(exc)) from exc
    after = campaign_snapshot(ctx.db, target)
    return ToolResult.ok({"campaign": after, "eligible_recipients": target.total_recipients},
                         focus=("campaign", target.id), target_type="campaign",
                         target_id=target.id, before=before, after=after)


@tool(
    "bulk_update_campaigns",
    group=GROUP,
    risk=Risk.HIGH_RISK_WRITE,
    summarize=lambda a: f"Bulk {a.get('action')} campaigns",
    description=(
        "Apply one action to every campaign matching the filters: pause, resume, cancel, or "
        "set_channel. The confirmation card lists every affected campaign before anything changes."
    ),
    properties={
        "action": {"type": "string", "enum": ["pause", "resume", "cancel", "set_channel"]},
        "new_channel": {"type": "string", "enum": [Channel.SMS.value, Channel.WHATSAPP.value, Channel.EMAIL.value]},
        **FILTER_PROPS,
    },
    required=["action"],
)
def bulk_update_campaigns(ctx: ToolContext, action: str, new_channel: str | None = None, **filters) -> ToolResult:
    from app.automations.service import apply_update

    if action == "set_channel" and not new_channel:
        raise ToolError("set_channel needs new_channel.")
    matched = _matching(ctx, **filters)
    if not matched:
        raise ToolError("No campaigns match those filters.")
    results = []
    for kind, target, row in matched:
        try:
            if action == "pause":
                if kind == "automation":
                    pause_automation(ctx, target)
                else:
                    pause_campaign(ctx, campaign_id=target.id)
            elif action == "resume":
                if kind != "automation":
                    raise ToolError("one-off campaigns are resumed from the Campaigns page")
                resume_campaign(ctx, automation_id=target.id)
            elif action == "cancel":
                target_arg = {"automation_id": target.id} if kind == "automation" else {"campaign_id": target.id}
                cancel_campaign(ctx, **target_arg)
            elif kind == "automation":
                apply_update(ctx.db, target, {"channel": new_channel}, actor=ctx.user.email)
            else:
                update_campaign(ctx, campaign_id=target.id, channel=new_channel)
            results.append({"type": kind, "id": target.id, "name": row["name"], "ok": True,
                            "status_after": target.status, "channel_after": target.channel})
        except ToolError as exc:
            ctx.db.rollback()
            results.append({"type": kind, "id": target.id, "name": row["name"], "ok": False, "error": str(exc)})
    changed = [r for r in results if r["ok"]]
    return ToolResult.ok(
        {"action": action, "affected": len(changed), "skipped": len(results) - len(changed), "results": results},
        target_type="bulk",
        target_id=f"{len(changed)} campaigns",
        before={"matched": [row for _, _, row in matched]},
        after={"results": results},
    )
