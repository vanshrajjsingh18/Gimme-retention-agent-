"""Deterministic Next Best Action recommendation engine.

Rules are evaluated in strict priority order; the first match wins. Every
recommendation carries reason codes (machine-readable) and an explanation
(human-readable).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.analytics.metrics import MetricResult
from app.churn.engine import ChurnResult
from app.core.enums import Channel, ChurnRiskBand, LifecycleStage, NextBestAction


@dataclass
class CustomerContext:
    """Everything the recommender needs, independent of the ORM."""

    lifecycle_stage: LifecycleStage
    metrics: MetricResult
    churn: ChurnResult
    expected_cycle_days: float
    is_suppressed: bool = False
    marketing_consent: bool = True
    email_consent: bool = True
    sms_consent: bool = False
    whatsapp_consent: bool = False
    preferred_channel: Channel = Channel.EMAIL
    messages_last_30d: int = 0
    frequency_cap_30d: int = 4


@dataclass
class RecommendationResult:
    action: NextBestAction
    priority: int
    reason_codes: list[str] = field(default_factory=list)
    explanation: str = ""
    recommended_channel: Channel = Channel.EMAIL
    suggested_products: list[dict] = field(default_factory=list)


def choose_channel(ctx: CustomerContext) -> Channel:
    """Pick the highest-preference channel the customer has consented to."""
    consent_map = {
        Channel.EMAIL: ctx.email_consent,
        Channel.SMS: ctx.sms_consent,
        Channel.WHATSAPP: ctx.whatsapp_consent,
    }
    if consent_map.get(ctx.preferred_channel):
        return ctx.preferred_channel
    for channel in (Channel.EMAIL, Channel.WHATSAPP, Channel.SMS):
        if consent_map.get(channel):
            return channel
    return Channel.EMAIL


def recommend(ctx: CustomerContext) -> RecommendationResult:
    """Return the single next best action for a customer."""
    m = ctx.metrics
    channel = choose_channel(ctx)
    products = [p for p in m.top_products[:3]]

    # --- Blocking rules first -------------------------------------------------
    if ctx.is_suppressed:
        return RecommendationResult(
            NextBestAction.SUPPRESS_COMMUNICATION,
            priority=100,
            reason_codes=["SUPPRESSED"],
            explanation="Customer is on the suppression list; no outbound messaging.",
            recommended_channel=channel,
        )
    if not ctx.marketing_consent:
        return RecommendationResult(
            NextBestAction.SUPPRESS_COMMUNICATION,
            priority=100,
            reason_codes=["NO_MARKETING_CONSENT"],
            explanation="Customer has not given marketing consent; no outbound messaging.",
            recommended_channel=channel,
        )
    if ctx.messages_last_30d >= ctx.frequency_cap_30d:
        return RecommendationResult(
            NextBestAction.NO_ACTION,
            priority=90,
            reason_codes=["FREQUENCY_CAP_REACHED"],
            explanation=(
                f"Already received {ctx.messages_last_30d} messages in the last 30 days, "
                f"at the cap of {ctx.frequency_cap_30d}."
            ),
            recommended_channel=channel,
        )

    stage = ctx.lifecycle_stage

    # --- Lapsed customers -----------------------------------------------------
    if stage == LifecycleStage.CHURNED:
        return RecommendationResult(
            NextBestAction.WIN_BACK,
            priority=80,
            reason_codes=["CHURNED", f"CHURN_{ctx.churn.risk_band.value}"],
            explanation=(
                f"Has not ordered in {m.days_since_last_order} days and is classified as "
                "churned. A win-back offer is the only realistic path back."
            ),
            recommended_channel=channel,
            suggested_products=products,
        )

    if stage == LifecycleStage.DORMANT:
        return RecommendationResult(
            NextBestAction.REACTIVATION,
            priority=75,
            reason_codes=["DORMANT", f"CHURN_{ctx.churn.risk_band.value}"],
            explanation=(
                f"Dormant for {m.days_since_last_order} days against a "
                f"{int(ctx.expected_cycle_days)}-day cycle. Reactivation messaging is due."
            ),
            recommended_channel=channel,
            suggested_products=products,
        )

    if stage == LifecycleStage.AT_RISK:
        codes = ["AT_RISK", f"CHURN_{ctx.churn.risk_band.value}"]
        if m.lifetime_revenue >= 600:
            codes.append("HIGH_VALUE_AT_RISK")
        return RecommendationResult(
            NextBestAction.REORDER_REMINDER,
            priority=70,
            reason_codes=codes,
            explanation=(
                f"{m.days_since_last_order} days since their last order, "
                f"{int(m.days_since_last_order - ctx.expected_cycle_days)} days beyond their "
                "usual cycle. A timely reorder reminder can pull them back before they lapse."
            ),
            recommended_channel=channel,
            suggested_products=products,
        )

    # --- Active customers -----------------------------------------------------
    if stage == LifecycleStage.REACTIVATED:
        return RecommendationResult(
            NextBestAction.PERSONALIZED_RECOMMENDATION,
            priority=60,
            reason_codes=["REACTIVATED", "REBUILD_HABIT"],
            explanation=(
                "Recently returned after a long gap. A personalised recommendation helps "
                "convert the return into a repeat habit."
            ),
            recommended_channel=channel,
            suggested_products=products,
        )

    if stage == LifecycleStage.NEW:
        if m.completed_orders == 0:
            return RecommendationResult(
                NextBestAction.WELCOME,
                priority=55,
                reason_codes=["NEW_NO_ORDER"],
                explanation="Signed up recently but has not ordered yet; send the welcome message.",
                recommended_channel=channel,
            )
        return RecommendationResult(
            NextBestAction.ENCOURAGE_SECOND_ORDER,
            priority=58,
            reason_codes=["NEW_ONE_ORDER", "SECOND_ORDER_WINDOW"],
            explanation=(
                f"First order was {m.days_since_first_order} days ago. The second order is the "
                "strongest predictor of long-term retention."
            ),
            recommended_channel=channel,
            suggested_products=products,
        )

    if stage == LifecycleStage.ACTIVATING:
        if m.completed_orders <= 1:
            return RecommendationResult(
                NextBestAction.ENCOURAGE_SECOND_ORDER,
                priority=57,
                reason_codes=["ONE_ORDER", "SECOND_ORDER_WINDOW"],
                explanation=(
                    "Only one completed order so far; encouraging the second order is the "
                    "highest-leverage action."
                ),
                recommended_channel=channel,
                suggested_products=products,
            )
        return RecommendationResult(
            NextBestAction.PERSONALIZED_RECOMMENDATION,
            priority=50,
            reason_codes=["ACTIVATING", "HABIT_FORMING"],
            explanation=(
                f"{m.completed_orders} orders in and building a habit. Personalised product "
                "suggestions keep momentum."
            ),
            recommended_channel=channel,
            suggested_products=products,
        )

    if stage == LifecycleStage.VIP:
        # Only appreciate a VIP when they are not already due to reorder.
        if _due_to_reorder(m, ctx.expected_cycle_days):
            return RecommendationResult(
                NextBestAction.REORDER_REMINDER,
                priority=52,
                reason_codes=["VIP", "DUE_TO_REORDER"],
                explanation=(
                    f"VIP customer approaching their usual {int(ctx.expected_cycle_days)}-day "
                    "reorder point."
                ),
                recommended_channel=channel,
                suggested_products=products,
            )
        return RecommendationResult(
            NextBestAction.VIP_APPRECIATION,
            priority=48,
            reason_codes=["VIP", "HIGH_LTV"],
            explanation=(
                f"${m.lifetime_revenue:,.0f} lifetime revenue across {m.completed_orders} "
                "orders. Recognition protects the relationship."
            ),
            recommended_channel=channel,
            suggested_products=products,
        )

    if stage == LifecycleStage.HIGH_VALUE:
        if _due_to_reorder(m, ctx.expected_cycle_days):
            return RecommendationResult(
                NextBestAction.REORDER_REMINDER,
                priority=51,
                reason_codes=["HIGH_VALUE", "DUE_TO_REORDER"],
                explanation=(
                    f"High-value customer at {m.days_since_last_order} days into a "
                    f"{int(ctx.expected_cycle_days)}-day cycle."
                ),
                recommended_channel=channel,
                suggested_products=products,
            )
        return RecommendationResult(
            NextBestAction.LOYALTY_RECOGNITION,
            priority=45,
            reason_codes=["HIGH_VALUE"],
            explanation=(
                f"${m.lifetime_revenue:,.0f} lifetime revenue and ordering on schedule. "
                "Loyalty recognition reinforces the habit."
            ),
            recommended_channel=channel,
            suggested_products=products,
        )

    if stage == LifecycleStage.REGULAR:
        if _due_to_reorder(m, ctx.expected_cycle_days):
            return RecommendationResult(
                NextBestAction.REORDER_REMINDER,
                priority=50,
                reason_codes=["REGULAR", "DUE_TO_REORDER"],
                explanation=(
                    f"{m.days_since_last_order} days into their usual "
                    f"{int(ctx.expected_cycle_days)}-day cycle; a reorder nudge lands well now."
                ),
                recommended_channel=channel,
                suggested_products=products,
            )
        if m.preferred_categories:
            return RecommendationResult(
                NextBestAction.CATEGORY_MESSAGE,
                priority=40,
                reason_codes=["REGULAR", "CATEGORY_AFFINITY"],
                explanation=(
                    f"Consistently buys {m.preferred_categories[0]}; category-relevant content "
                    "is the most useful thing to send."
                ),
                recommended_channel=channel,
                suggested_products=products,
            )
        return RecommendationResult(
            NextBestAction.REQUEST_FEEDBACK,
            priority=30,
            reason_codes=["REGULAR", "NO_STRONG_SIGNAL"],
            explanation=(
                "Ordering steadily with no urgent signal. Asking for feedback keeps the "
                "relationship warm without selling."
            ),
            recommended_channel=channel,
        )

    return RecommendationResult(
        NextBestAction.NO_ACTION,
        priority=0,
        reason_codes=["NO_RULE_MATCH"],
        explanation="No action rule matched this customer's current state.",
        recommended_channel=channel,
    )


def _due_to_reorder(m: MetricResult, cycle: float) -> bool:
    """True when the customer is in the back half of their purchase cycle."""
    if m.days_since_last_order is None:
        return False
    return m.days_since_last_order >= cycle * 0.75
