from __future__ import annotations

import pytest

from app.analytics.metrics import compute_metrics
from app.churn.engine import score_churn
from app.core.enums import Channel, LifecycleStage, NextBestAction
from app.recommendations.engine import CustomerContext, choose_channel, recommend
from app.services.lifecycle import classify_lifecycle, expected_cycle_days
from tests.factories import NOW, cadence_history, order


def context(orders, *, signup_days_ago=200, **overrides) -> CustomerContext:
    from datetime import timedelta

    m = compute_metrics(orders, now=NOW)
    cycle, _ = expected_cycle_days(m)
    life = classify_lifecycle(
        m, signup_date=NOW - timedelta(days=signup_days_ago), now=NOW
    )
    churn = score_churn(m, expected_cycle_days=cycle, tenure_days=signup_days_ago)
    kwargs = {
        "lifecycle_stage": life.stage,
        "metrics": m,
        "churn": churn,
        "expected_cycle_days": cycle,
    }
    kwargs.update(overrides)
    return CustomerContext(**kwargs)


def test_suppressed_customer_blocks_all_messaging():
    ctx = context([order(5)], is_suppressed=True)
    r = recommend(ctx)
    assert r.action == NextBestAction.SUPPRESS_COMMUNICATION
    assert "SUPPRESSED" in r.reason_codes


def test_no_marketing_consent_blocks_messaging():
    ctx = context([order(5)], marketing_consent=False)
    r = recommend(ctx)
    assert r.action == NextBestAction.SUPPRESS_COMMUNICATION
    assert "NO_MARKETING_CONSENT" in r.reason_codes


def test_frequency_cap_returns_no_action():
    ctx = context([order(5)], messages_last_30d=4, frequency_cap_30d=4)
    r = recommend(ctx)
    assert r.action == NextBestAction.NO_ACTION
    assert "FREQUENCY_CAP_REACHED" in r.reason_codes


def test_blocking_rules_beat_lifecycle_rules():
    """A churned customer who is suppressed must not get a win-back."""
    orders = cadence_history(count=5, interval_days=30, last_order_days_ago=400)
    ctx = context(orders, signup_days_ago=700, is_suppressed=True)
    assert ctx.lifecycle_stage == LifecycleStage.CHURNED
    assert recommend(ctx).action == NextBestAction.SUPPRESS_COMMUNICATION


def test_new_customer_no_orders_gets_welcome():
    ctx = context([], signup_days_ago=5)
    r = recommend(ctx)
    assert r.action == NextBestAction.WELCOME


def test_new_customer_one_order_gets_second_order_push():
    ctx = context([order(5, 90.0)], signup_days_ago=10)
    r = recommend(ctx)
    assert r.action == NextBestAction.ENCOURAGE_SECOND_ORDER
    assert "SECOND_ORDER_WINDOW" in r.reason_codes


def test_activating_single_order_gets_second_order_push():
    ctx = context([order(40, 90.0)], signup_days_ago=60)
    assert recommend(ctx).action == NextBestAction.ENCOURAGE_SECOND_ORDER


def test_at_risk_gets_reorder_reminder():
    orders = cadence_history(count=5, interval_days=30, last_order_days_ago=55, amount=60.0)
    ctx = context(orders, signup_days_ago=300)
    assert ctx.lifecycle_stage == LifecycleStage.AT_RISK
    r = recommend(ctx)
    assert r.action == NextBestAction.REORDER_REMINDER
    assert "AT_RISK" in r.reason_codes


def test_dormant_gets_reactivation():
    orders = cadence_history(count=5, interval_days=30, last_order_days_ago=150, amount=60.0)
    ctx = context(orders, signup_days_ago=400)
    assert ctx.lifecycle_stage == LifecycleStage.DORMANT
    assert recommend(ctx).action == NextBestAction.REACTIVATION


def test_churned_gets_win_back():
    orders = cadence_history(count=5, interval_days=30, last_order_days_ago=400, amount=60.0)
    ctx = context(orders, signup_days_ago=700)
    assert ctx.lifecycle_stage == LifecycleStage.CHURNED
    assert recommend(ctx).action == NextBestAction.WIN_BACK


def test_vip_recently_ordered_gets_appreciation():
    orders = cadence_history(count=12, interval_days=25, last_order_days_ago=3, amount=180.0)
    ctx = context(orders, signup_days_ago=400)
    assert ctx.lifecycle_stage == LifecycleStage.VIP
    assert recommend(ctx).action == NextBestAction.VIP_APPRECIATION


def test_vip_due_to_reorder_gets_reminder_instead():
    orders = cadence_history(count=12, interval_days=25, last_order_days_ago=22, amount=180.0)
    ctx = context(orders, signup_days_ago=400)
    assert ctx.lifecycle_stage == LifecycleStage.VIP
    r = recommend(ctx)
    assert r.action == NextBestAction.REORDER_REMINDER
    assert "DUE_TO_REORDER" in r.reason_codes


def test_high_value_recently_ordered_gets_loyalty_recognition():
    orders = cadence_history(count=6, interval_days=30, last_order_days_ago=3, amount=150.0)
    ctx = context(orders, signup_days_ago=250)
    assert ctx.lifecycle_stage == LifecycleStage.HIGH_VALUE
    assert recommend(ctx).action == NextBestAction.LOYALTY_RECOGNITION


def test_regular_due_to_reorder_gets_reminder():
    orders = cadence_history(count=6, interval_days=30, last_order_days_ago=26, amount=60.0)
    ctx = context(orders, signup_days_ago=300)
    assert ctx.lifecycle_stage == LifecycleStage.REGULAR
    assert recommend(ctx).action == NextBestAction.REORDER_REMINDER


def test_regular_mid_cycle_gets_category_message():
    orders = cadence_history(count=6, interval_days=30, last_order_days_ago=3, amount=60.0)
    ctx = context(orders, signup_days_ago=300)
    assert ctx.lifecycle_stage == LifecycleStage.REGULAR
    r = recommend(ctx)
    assert r.action == NextBestAction.CATEGORY_MESSAGE
    assert "CATEGORY_AFFINITY" in r.reason_codes


def test_reactivated_gets_personalized_recommendation():
    orders = [order(3, 90.0), order(300, 90.0), order(330, 90.0)]
    ctx = context(orders, signup_days_ago=400)
    assert ctx.lifecycle_stage == LifecycleStage.REACTIVATED
    assert recommend(ctx).action == NextBestAction.PERSONALIZED_RECOMMENDATION


def test_every_recommendation_has_reason_codes_and_explanation():
    scenarios = [
        context([], signup_days_ago=5),
        context([order(5, 90.0)], signup_days_ago=10),
        context(cadence_history(count=6, interval_days=30, last_order_days_ago=3, amount=60.0)),
        context(
            cadence_history(count=5, interval_days=30, last_order_days_ago=55, amount=60.0),
            signup_days_ago=300,
        ),
        context([order(5)], is_suppressed=True),
    ]
    for ctx in scenarios:
        r = recommend(ctx)
        assert r.reason_codes, f"missing reason codes for {ctx.lifecycle_stage}"
        assert r.explanation, f"missing explanation for {ctx.lifecycle_stage}"
        assert isinstance(r.recommended_channel, Channel)


def test_channel_falls_back_to_a_consented_channel():
    ctx = context(
        [order(5)],
        preferred_channel=Channel.SMS,
        sms_consent=False,
        email_consent=False,
        whatsapp_consent=True,
    )
    assert choose_channel(ctx) == Channel.WHATSAPP


def test_channel_uses_preference_when_consented():
    ctx = context([order(5)], preferred_channel=Channel.SMS, sms_consent=True)
    assert choose_channel(ctx) == Channel.SMS


def test_channel_defaults_to_email_when_nothing_consented():
    ctx = context(
        [order(5)], email_consent=False, sms_consent=False, whatsapp_consent=False
    )
    assert choose_channel(ctx) == Channel.EMAIL


@pytest.mark.parametrize(
    "stage",
    [s for s in LifecycleStage],
)
def test_recommender_handles_every_lifecycle_stage(stage):
    """No stage may fall through to an unmatched NO_ACTION."""
    ctx = context(cadence_history(count=4, interval_days=30, last_order_days_ago=10))
    ctx.lifecycle_stage = stage
    r = recommend(ctx)
    assert "NO_RULE_MATCH" not in r.reason_codes
