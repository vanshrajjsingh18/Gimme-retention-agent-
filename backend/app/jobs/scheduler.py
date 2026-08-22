"""Background jobs.

Three recurring jobs:
  * refresh intelligence and segments so lifecycle stages age correctly even
    when nobody is importing data;
  * dispatch scheduled campaigns whose send time has arrived;
  * ingest CSVs dropped into the local inbox folder.
"""
from __future__ import annotations

import logging
import os
import shutil
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.core.config import settings
from app.core.database import session_scope
from app.models.base import utcnow
from app.models.entities import SystemLog

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None

INBOX_ENTITY_PREFIXES = {
    "customers": "customers",
    "orders": "orders",
    "order_items": "order_items",
    "order-items": "order_items",
    "events": "events",
    "consent_events": "consent_events",
    "consent-events": "consent_events",
}


def _log(db, level: str, source: str, message: str, context: dict | None = None) -> None:
    db.add(SystemLog(level=level, source=source, message=message, context=context or {}))


def refresh_intelligence_job() -> None:
    """Recompute metrics, lifecycle, churn, RFM and segment membership."""
    from app.services.intelligence import refresh_all_customers
    from app.services.segments import refresh_all_segments

    try:
        with session_scope() as db:
            result = refresh_all_customers(db)
            segments = refresh_all_segments(db)
            _log(
                db,
                "INFO",
                "scheduler",
                f"Refreshed intelligence for {result['customers_processed']} customers.",
                {"rfm_scored": result["rfm_scored"], "segments": len(segments)},
            )
    except Exception as exc:  # noqa: BLE001 - a job failure must not kill the scheduler
        logger.exception("Scheduled intelligence refresh failed")
        with session_scope() as db:
            _log(db, "ERROR", "scheduler", f"Intelligence refresh failed: {exc}")


def dispatch_scheduled_campaigns_job() -> None:
    """Run any approved campaign whose scheduled send time has passed."""
    from app.campaigns.service import CampaignError, due_scheduled_campaigns, run_campaign

    try:
        with session_scope() as db:
            due = due_scheduled_campaigns(db)
            for campaign in due:
                try:
                    stats = run_campaign(db, campaign)
                    _log(
                        db,
                        "INFO",
                        "scheduler",
                        f"Dispatched scheduled campaign '{campaign.name}'.",
                        stats,
                    )
                except CampaignError as exc:
                    _log(
                        db,
                        "WARNING",
                        "scheduler",
                        f"Scheduled campaign '{campaign.name}' could not run: {exc}",
                    )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Scheduled campaign dispatch failed")
        with session_scope() as db:
            _log(db, "ERROR", "scheduler", f"Campaign dispatch failed: {exc}")


def run_automations_job() -> None:
    """Run every automation whose next run is due.

    Deliberately the same simple interval poll the campaign dispatcher uses,
    rather than a queue: the per-customer timing lives in ``next_run_at`` and
    ``next_due_at`` columns, so a five-minute tick is enough resolution for a
    business that only sends between 9am and 7pm, and it needs no broker to
    operate.
    """
    from app.automations.service import run_due

    try:
        with session_scope() as db:
            reports = run_due(db)
            for report in reports:
                if "error" in report:
                    _log(
                        db,
                        "ERROR",
                        "automations",
                        f"Automation {report['automation_id']} failed to run.",
                        report,
                    )
                elif report["sent"] or report["skipped"] or report["failed"]:
                    _log(
                        db,
                        "INFO",
                        "automations",
                        f"Ran automation '{report['automation_name']}': "
                        f"{report['sent']} sent, {report['skipped']} skipped, "
                        f"{report['failed']} failed.",
                        report,
                    )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Automation run failed")
        with session_scope() as db:
            _log(db, "ERROR", "automations", f"Automation run failed: {exc}")


def refresh_order_patterns_job() -> None:
    """Recompute behavioural-nudge order patterns. Habits drift."""
    from app.automations.service import refresh_nudge_patterns

    try:
        with session_scope() as db:
            totals = refresh_nudge_patterns(db)
            if totals["refreshed"] or totals["dropped"]:
                _log(
                    db,
                    "INFO",
                    "automations",
                    f"Refreshed order patterns: {totals['refreshed']} updated, "
                    f"{totals['dropped']} customers dropped for lack of a pattern.",
                    totals,
                )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Order pattern refresh failed")
        with session_scope() as db:
            _log(db, "ERROR", "automations", f"Order pattern refresh failed: {exc}")


def ingest_inbox_job() -> None:
    """Import any CSV dropped into the inbox folder.

    Files are named ``<entity_type>-*.csv`` (for example
    ``orders-2026-08-20.csv``) and are moved to ``processed/`` or ``failed/``
    after the run so the same file is never imported twice.
    """
    from app.services.ingestion import ingest_csv

    inbox = settings.INBOX_DIR
    if not os.path.isdir(inbox):
        return

    processed_dir = os.path.join(inbox, "processed")
    failed_dir = os.path.join(inbox, "failed")
    os.makedirs(processed_dir, exist_ok=True)
    os.makedirs(failed_dir, exist_ok=True)

    for filename in sorted(os.listdir(inbox)):
        path = os.path.join(inbox, filename)
        if not os.path.isfile(path) or not filename.lower().endswith(".csv"):
            continue

        stem = filename.rsplit(".", 1)[0].lower()
        entity_type = next(
            (
                value
                for prefix, value in INBOX_ENTITY_PREFIXES.items()
                if stem.startswith(prefix)
            ),
            None,
        )
        if entity_type is None:
            shutil.move(path, os.path.join(failed_dir, filename))
            with session_scope() as db:
                _log(
                    db,
                    "WARNING",
                    "inbox",
                    f"Could not determine the entity type for '{filename}'. Name files "
                    "like customers-*.csv, orders-*.csv or order_items-*.csv.",
                )
            continue

        try:
            with open(path, "rb") as handle:
                content = handle.read()
            with session_scope() as db:
                job = ingest_csv(
                    db, entity_type, content, filename=filename, source="inbox"
                )
                succeeded = job.status == "COMPLETED"
                _log(
                    db,
                    "INFO" if succeeded else "ERROR",
                    "inbox",
                    f"Imported '{filename}': {job.accepted_rows} accepted, "
                    f"{job.rejected_rows} rejected.",
                    {"job_id": job.id, "status": job.status},
                )
            shutil.move(path, os.path.join(processed_dir if succeeded else failed_dir, filename))
        except Exception as exc:  # noqa: BLE001
            logger.exception("Inbox ingestion failed for %s", filename)
            shutil.move(path, os.path.join(failed_dir, filename))
            with session_scope() as db:
                _log(db, "ERROR", "inbox", f"Failed to import '{filename}': {exc}")


def start_scheduler() -> BackgroundScheduler | None:
    global _scheduler
    if not settings.ENABLE_SCHEDULER:
        logger.info("Scheduler disabled (ENABLE_SCHEDULER=false).")
        return None
    if _scheduler is not None:
        return _scheduler

    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        refresh_intelligence_job,
        IntervalTrigger(minutes=settings.SCHEDULER_METRICS_INTERVAL_MINUTES),
        id="refresh_intelligence",
        replace_existing=True,
        max_instances=1,
    )
    scheduler.add_job(
        dispatch_scheduled_campaigns_job,
        IntervalTrigger(minutes=1),
        id="dispatch_campaigns",
        replace_existing=True,
        max_instances=1,
    )
    scheduler.add_job(
        ingest_inbox_job,
        IntervalTrigger(minutes=2),
        id="ingest_inbox",
        replace_existing=True,
        max_instances=1,
    )
    scheduler.add_job(
        run_automations_job,
        IntervalTrigger(minutes=settings.AUTOMATION_TICK_MINUTES),
        id="run_automations",
        replace_existing=True,
        max_instances=1,
    )
    scheduler.add_job(
        refresh_order_patterns_job,
        # Daily, so a customer whose pattern went stale overnight is picked up
        # promptly; the recompute itself only touches patterns past their age.
        IntervalTrigger(hours=24),
        id="refresh_order_patterns",
        replace_existing=True,
        max_instances=1,
    )
    scheduler.start()
    _scheduler = scheduler
    logger.info("Background scheduler started with %d jobs.", len(scheduler.get_jobs()))
    return scheduler


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def scheduler_status() -> dict:
    if _scheduler is None:
        return {"running": False, "jobs": []}
    return {
        "running": _scheduler.running,
        "jobs": [
            {
                "id": job.id,
                "next_run_at": job.next_run_time.isoformat() if job.next_run_time else None,
            }
            for job in _scheduler.get_jobs()
        ],
    }
