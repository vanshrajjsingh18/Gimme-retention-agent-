"""Regression tests for the campaign-configuration services.

Each of these services shipped referencing fields that do not exist on the
models (``customer.timezone``, ``customer.phone_number``, a ``Product`` model,
``AutomationSend.context``) and was only ever compiled, never called. These
tests call every one of them against the seeded database so that cannot
happen silently again.
"""
from __future__ import annotations

import time
from itertools import count

from sqlalchemy import func, select

from app.automations.service import create_automation
from app.core.enums import AutomationKind
from app.models.entities import (
    CouponVariant,
    Customer,
    CustomerCouponAssignment,
    Segment,
)

_RUN = count(int(time.time() * 1000))


def _nudge(db, **config):
    segment = db.execute(select(Segment).order_by(Segment.id)).scalars().first()
    automation = create_automation(
        db,
        name=f"Config services {next(_RUN)}",
        kind=AutomationKind.NUDGE.value,
        segment_id=segment.id,
        message_template="Hi #first_name#, code #coupon_code#. Reply STOP to opt out.",
        config=config,
    )
    for position, (code, pct) in enumerate([("FIRST7", 50.0), ("LUCKY7", 50.0)]):
        db.add(
            CouponVariant(
                automation_id=automation.id,
                position=position,
                coupon_code=code,
                allocation_percentage=pct,
            )
        )
    db.commit()
    return automation


def test_every_campaign_service_runs_against_real_models(db, seeded):
    from app.services import (
        ab_testing,
        advanced_segmentation,
        audience_preview,
        campaign_analytics,
        coupon_analytics,
        multi_touch_orchestration,
        personalization,
        stop_conditions,
    )

    automation = _nudge(db)
    customer = db.execute(select(Customer).order_by(Customer.id)).scalars().first()

    assert audience_preview.preview_audience(db, automation)["automation_id"] == automation.id
    assert campaign_analytics.get_campaign_performance_summary(db, automation.id)
    assert campaign_analytics.get_conversion_funnel(db, automation.id)["funnel"]
    assert campaign_analytics.get_touchpoint_performance(db, automation.id) == {
        "automation_id": automation.id,
        "touchpoints": [],
    }
    assert campaign_analytics.get_customer_journey_details(db, automation.id, customer.id)
    assert coupon_analytics.get_coupon_performance(db, automation.id)["summary"]
    assert multi_touch_orchestration.get_journey_stats(db, automation.id)
    assert ab_testing.get_ab_test_summary(db, automation.id)["automation_id"] == automation.id
    assert stop_conditions.should_stop_journey(db, customer.id, automation.id) == (False, None)
    assert advanced_segmentation.get_segmentation_analysis(db)["total_customers"] > 0

    attributes = personalization.get_customer_attributes(db, customer.id)
    assert attributes["phone"] == customer.phone
    assert isinstance(personalization.get_product_recommendations(db, customer.id), list)


def test_quiet_hours_use_the_business_clock(db, seeded):
    """Customers carry no timezone; this crashed on ``customer.timezone``."""
    from app.services.frequency_gating import should_send_message

    automation = _nudge(db, frequency_rules={"quiet_hours": {"start": "00:00", "end": "23:59"}})
    customer = db.execute(
        select(Customer).where(
            Customer.marketing_consent.is_(True), Customer.is_suppressed.is_(False)
        )
    ).scalars().first()
    assert should_send_message(db, customer.id, automation.id) == (False, "QUIET_HOURS")


def test_dry_run_does_not_persist_coupon_assignments(db, seeded):
    from app.services.coupon_assignment import get_or_assign_coupon
    from app.services.smart_reorder_queue import build_queue

    automation = _nudge(db)
    customer = db.execute(select(Customer).order_by(Customer.id)).scalars().first()

    would_get = get_or_assign_coupon(db, customer.id, automation.id, persist=False)
    assert would_get in {"FIRST7", "LUCKY7"}

    build_queue(db, automation, dry_run=True)
    db.commit()
    stored = db.execute(
        select(func.count(CustomerCouponAssignment.id)).where(
            CustomerCouponAssignment.automation_id == automation.id
        )
    ).scalar_one()
    assert stored == 0

    assert get_or_assign_coupon(db, customer.id, automation.id) == would_get
    db.commit()
