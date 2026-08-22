"""Example automations, so a fresh install shows the feature rather than an
empty screen.

Every one is seeded as a **draft**: nothing here sends until somebody approves
and activates it. That is deliberate — seed data that could start texting
customers on its own would be a trap, not a demo.
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.automations.service import create_automation
from app.automations.runtime import AutomationError
from app.core.enums import (
    AutomationKind,
    CampaignObjective,
    Channel,
    EnrollmentMode,
    RecurrenceKind,
)
from app.models.entities import Automation, Segment

logger = logging.getLogger(__name__)

#: One example per campaign type, each showing the feature that distinguishes
#: it: a live cohort re-evaluated weekly, a sequence on enrollment offsets, and
#: a standing per-customer nudge.
EXAMPLES: list[dict] = [
    {
        "name": "Weekly win-back",
        "description": (
            "Every Monday, message whoever is dormant that morning — the audience is "
            "re-evaluated at send time, so customers who have since ordered drop out."
        ),
        "kind": AutomationKind.COHORT_BULK.value,
        "segment": "Dormant",
        "objective": CampaignObjective.REACTIVATION.value,
        "recurrence": RecurrenceKind.WEEKLY.value,
        "recurrence_day": 0,
        "send_time_local": "10:00",
        # Left blank on purpose: the segment's own tone is the better default,
        # and showing that is more useful than hard-coding copy here.
        "message_template": "",
    },
    {
        "name": "Second-order series",
        "description": (
            "Three steps on Day 0, 7 and 14 counted from each customer's own "
            "enrollment. Stops the moment they place an order."
        ),
        "kind": AutomationKind.SEQUENCE.value,
        "segment": "Needs Second Order",
        "objective": CampaignObjective.SECOND_ORDER.value,
        "enrollment_mode": EnrollmentMode.ROLLING.value,
        "stop_on_order": True,
        "steps": [
            {
                "name": "Thanks for your first order",
                "offset_days": 0,
                "message_template": (
                    "Hi {first_name}, thanks for your first order with GIMME. "
                    "Whenever you're ready for the next one we're here: {website}. "
                    "Reply STOP to opt out."
                ),
            },
            {
                "name": "One week on",
                "offset_days": 7,
                "message_template": (
                    "Hi {first_name}, your favourites are still one tap away at "
                    "{website}. {delivery_promise}. Reply STOP to opt out."
                ),
            },
            {
                "name": "Two weeks on",
                "offset_days": 14,
                "message_template": (
                    "Hi {first_name}, no pressure — just so you know we deliver to "
                    "{city} whenever you need us. {website}. Reply STOP to opt out."
                ),
            },
        ],
    },
    {
        "name": "Reorder nudge",
        "description": (
            "A standing message at the day and time each regular usually orders, "
            "derived from their own order history. Runs until they opt out."
        ),
        "kind": AutomationKind.NUDGE.value,
        "segment": "Regulars",
        "objective": CampaignObjective.REORDER.value,
        "config": {"min_orders": 3, "min_gap_days": 7},
        "message_template": "",
    },
]


def seed_automations(db: Session) -> dict:
    """Create the example automations that do not already exist.

    Idempotent: re-running leaves existing automations untouched, so this is
    safe to call from the demo seeder on every run.
    """
    created: list[str] = []
    skipped: list[str] = []

    for spec in EXAMPLES:
        if db.execute(
            select(Automation.id).where(Automation.name == spec["name"])
        ).first():
            skipped.append(spec["name"])
            continue

        segment = db.execute(
            select(Segment).where(Segment.name == spec["segment"])
        ).scalar_one_or_none()
        if segment is None:
            logger.warning(
                "Skipping example automation '%s': segment '%s' does not exist.",
                spec["name"],
                spec["segment"],
            )
            skipped.append(spec["name"])
            continue

        try:
            create_automation(
                db,
                name=spec["name"],
                description=spec["description"],
                kind=spec["kind"],
                channel=Channel.SMS.value,
                objective=spec["objective"],
                segment_id=segment.id,
                enrollment_mode=spec.get("enrollment_mode"),
                recurrence=spec.get("recurrence", RecurrenceKind.ONCE.value),
                recurrence_day=spec.get("recurrence_day"),
                send_time_local=spec.get("send_time_local", "10:00"),
                message_template=spec.get("message_template", ""),
                config=spec.get("config", {}),
                stop_on_order=spec.get("stop_on_order", True),
                # Every example stays a draft needing approval. Seed data must
                # never be able to start messaging real customers by itself.
                require_approval=True,
                steps=spec.get("steps", []),
            )
        except AutomationError as exc:
            logger.warning("Could not seed automation '%s': %s", spec["name"], exc)
            skipped.append(spec["name"])
            continue
        created.append(spec["name"])

    logger.info("Seeded %d example automations (%d already existed).", len(created), len(skipped))
    return {"created": created, "skipped": skipped, "status": "DRAFT"}
