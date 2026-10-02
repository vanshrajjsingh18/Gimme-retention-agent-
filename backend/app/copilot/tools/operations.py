"""The scheduled-message queue, background jobs, and system health."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select

from app.copilot.registry import Risk, ToolContext, ToolError, ToolResult, tool
from app.copilot.tools._common import WINDOWS, local_label, resolve_window
from app.core.config import settings
from app.core.enums import OPEN_MESSAGE_STATUSES
from app.core.timezones import to_utc_naive
from app.models.entities import Automation, Integration, ScheduledMessage, SystemLog

GROUP = "scheduler"


def _view(db, message: ScheduledMessage) -> dict:
    from app.services.smart_reorder_queue import as_view

    view = as_view(db, message)
    view["scheduled_at_local_label"] = local_label(message.scheduled_at)
    return view


@tool(
    "get_scheduled_messages",
    group=GROUP,
    risk=Risk.READ,
    description=(
        "Smart Reorder messages scheduled (or already resolved) in a period — 'what is scheduled "
        "tonight', 'today's Smart Reorder messages'. Status counts plus the rows, soonest first."
    ),
    properties={
        "period": {"type": "string", "enum": list(WINDOWS), "description": "Default today."},
        "automation_id": {"type": "integer"},
        "status": {"type": "string", "description": "e.g. SCHEDULED, SENT, CANCELLED. Default: all."},
        "limit": {"type": "integer"},
    },
)
def get_scheduled_messages(ctx: ToolContext, period: str = "today", automation_id: int | None = None,
                           status: str | None = None, limit: int = 25) -> ToolResult:
    start, end, label = resolve_window(period, ctx.now)
    conditions = [ScheduledMessage.scheduled_at >= start, ScheduledMessage.scheduled_at < end]
    if automation_id:
        conditions.append(ScheduledMessage.automation_id == automation_id)
    counts = dict(
        ctx.db.execute(
            select(ScheduledMessage.status, func.count()).where(*conditions).group_by(ScheduledMessage.status)
        ).all()
    )
    if status:
        conditions.append(ScheduledMessage.status == status.upper())
    base = select(ScheduledMessage).where(*conditions)
    rows = ctx.db.execute(base.order_by(ScheduledMessage.scheduled_at).limit(max(1, min(limit, 200)))).scalars().all()
    return ToolResult.ok(
        {"by_status": counts, "total": sum(counts.values()), "messages": [_view(ctx.db, m) for m in rows]},
        metadata={"period": label, "count": sum(counts.values())},
    )


@tool(
    "reschedule_message",
    group=GROUP,
    risk=Risk.WRITE,
    summarize=lambda a: f"Reschedule message #{a.get('message_id')} to {a.get('send_at_local')}",
    description="Move one customer's pending reminder to a new local time. Nobody else's message changes.",
    properties={
        "message_id": {"type": "integer"},
        "send_at_local": {"type": "string", "description": "Business local time 'YYYY-MM-DD HH:MM'."},
    },
    required=["message_id", "send_at_local"],
)
def reschedule_message(ctx: ToolContext, message_id: int, send_at_local: str) -> ToolResult:
    from app.services.smart_reorder_queue import QueueError, reschedule

    try:
        when = to_utc_naive(datetime.strptime(send_at_local, "%Y-%m-%d %H:%M"))
    except ValueError as exc:
        raise ToolError("send_at_local must look like 2026-10-09 18:30.") from exc
    if when <= ctx.now:
        raise ToolError("That time has already passed.")
    message = ctx.db.get(ScheduledMessage, message_id)
    if message is None:
        raise ToolError(f"Scheduled message {message_id} does not exist.")
    before = _view(ctx.db, message)
    try:
        message = reschedule(ctx.db, message_id, when)
    except QueueError as exc:
        raise ToolError(str(exc)) from exc
    after = _view(ctx.db, message)
    return ToolResult.ok(after, target_type="scheduled_message", target_id=message_id, before=before, after=after)


@tool(
    "cancel_scheduled_message",
    group=GROUP,
    risk=Risk.WRITE,
    summarize=lambda a: f"Cancel scheduled message #{a.get('message_id')}",
    description="Call off one customer's pending reminder, recording the reason.",
    properties={"message_id": {"type": "integer"}, "reason": {"type": "string"}},
    required=["message_id"],
)
def cancel_scheduled_message(ctx: ToolContext, message_id: int, reason: str = "") -> ToolResult:
    from app.services.smart_reorder_queue import QueueError, cancel_by_id

    message = ctx.db.get(ScheduledMessage, message_id)
    if message is None:
        raise ToolError(f"Scheduled message {message_id} does not exist.")
    if message.status not in OPEN_MESSAGE_STATUSES:
        raise ToolError(f"Message {message_id} is {message.status}; only pending messages can be cancelled.")
    before = _view(ctx.db, message)
    try:
        message = cancel_by_id(ctx.db, message_id, detail=reason or f"Cancelled by {ctx.user.email} via the AI Copilot.")
    except QueueError as exc:
        raise ToolError(str(exc)) from exc
    after = _view(ctx.db, message)
    return ToolResult.ok(after, target_type="scheduled_message", target_id=message_id, before=before, after=after)


@tool(
    "get_scheduler_status",
    group="system",
    risk=Risk.READ,
    description="Whether the background scheduler is running and its jobs (with next run times).",
)
def get_scheduler_status(ctx: ToolContext) -> ToolResult:
    from app.jobs.scheduler import scheduler_status

    return ToolResult.ok(scheduler_status())


@tool(
    "get_active_jobs",
    group="system",
    risk=Risk.READ,
    description="Automations that are live, with their last and next run times.",
)
def get_active_jobs(ctx: ToolContext) -> ToolResult:
    rows = ctx.db.execute(
        select(Automation).where(Automation.status.in_(["ACTIVE", "TESTING"])).order_by(Automation.next_run_at)
    ).scalars().all()
    return ToolResult.ok(
        [
            {
                "automation_id": a.id,
                "name": a.name,
                "kind": a.kind,
                "status": a.status,
                "last_run_local": local_label(a.last_run_at),
                "next_run_local": local_label(a.next_run_at),
                "sent": a.total_sent,
                "skipped": a.total_skipped,
                "failed": a.total_failed,
            }
            for a in rows
        ]
    )


@tool(
    "get_system_status",
    group="system",
    risk=Risk.READ,
    description="Environment, messaging integrations and their modes (mock/live), scheduler, AI provider, and data volume.",
)
def get_system_status(ctx: ToolContext) -> ToolResult:
    from app.copilot.providers import provider_info
    from app.jobs.scheduler import scheduler_status
    from app.services.seed import summary

    integrations = ctx.db.execute(select(Integration)).scalars().all()
    return ToolResult.ok(
        {
            "environment": settings.ENVIRONMENT,
            "business_timezone": settings.BUSINESS_TIMEZONE,
            "send_window": f"{settings.SEND_WINDOW_START}–{settings.SEND_WINDOW_END}",
            "integrations": [
                {"provider": i.provider, "channel": i.channel, "mode": i.mode, "status": i.status, "enabled": i.enabled}
                for i in integrations
            ],
            "scheduler": scheduler_status(),
            "ai_copilot": provider_info(),
            "data": summary(ctx.db),
        }
    )


@tool(
    "get_recent_errors",
    group="system",
    risk=Risk.READ,
    description="Recent ERROR/WARNING system log entries, and recently failed Smart Reorder messages.",
    properties={"limit": {"type": "integer"}},
)
def get_recent_errors(ctx: ToolContext, limit: int = 20) -> ToolResult:
    logs = ctx.db.execute(
        select(SystemLog)
        .where(SystemLog.level.in_(["ERROR", "WARNING"]))
        .order_by(SystemLog.created_at.desc())
        .limit(max(1, min(limit, 100)))
    ).scalars().all()
    failed = ctx.db.execute(
        select(ScheduledMessage)
        .where(ScheduledMessage.status == "FAILED")
        .order_by(ScheduledMessage.updated_at.desc())
        .limit(10)
    ).scalars().all()
    return ToolResult.ok(
        {
            "log_entries": [
                {"at_local": local_label(l.created_at), "level": l.level, "source": l.source, "message": l.message}
                for l in logs
            ],
            "failed_messages": [
                {"id": m.id, "customer_id": m.customer_id, "error": m.error_message or m.error_code,
                 "scheduled_at_local": local_label(m.scheduled_at)}
                for m in failed
            ],
        }
    )
