"""Transparent, deterministic churn risk scoring.

Every point of the 0-100 score comes from a named, weighted factor computed in
application code. The LLM is never involved in producing a number; it may only
rephrase the explanation string this module produces.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.analytics.metrics import MetricResult
from app.core.enums import ChurnRiskBand

# Factor weights sum to 100. Each factor contributes weight x severity(0..1).
FACTOR_WEIGHTS: dict[str, float] = {
    "cadence_overdue": 40.0,
    "frequency_decline": 18.0,
    "spend_decline": 16.0,
    "engagement_decline": 8.0,
    "single_order": 10.0,
    "discount_dependency": 4.0,
    "order_problems": 4.0,
}

# How many multiples of the expected cycle a customer must be overdue before
# the cadence factor saturates. Keeps 3-months-late and 2-years-late distinct.
CADENCE_SATURATION_MULTIPLE = 6.0

BAND_THRESHOLDS = [
    (70.0, ChurnRiskBand.CRITICAL),
    (45.0, ChurnRiskBand.HIGH),
    (25.0, ChurnRiskBand.MEDIUM),
]

FACTOR_LABELS: dict[str, str] = {
    "cadence_overdue": "Overdue against their usual order cycle",
    "frequency_decline": "Ordering less often than the previous quarter",
    "spend_decline": "Spending less than the previous quarter",
    "engagement_decline": "Low interaction with recent messages",
    "single_order": "Has not established a repeat-purchase habit",
    "discount_dependency": "Only orders when discounted",
    "order_problems": "Recent cancellations or refunds",
}


@dataclass
class ChurnFactor:
    code: str
    label: str
    severity: float  # 0..1
    points: float
    detail: str

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "label": self.label,
            "severity": round(self.severity, 3),
            "points": round(self.points, 1),
            "detail": self.detail,
        }


@dataclass
class ChurnResult:
    score: float
    risk_band: ChurnRiskBand
    factors: list[ChurnFactor] = field(default_factory=list)
    explanation: str = ""
    revenue_at_risk: float = 0.0

    def top_factors(self, n: int = 3) -> list[ChurnFactor]:
        return sorted(self.factors, key=lambda f: f.points, reverse=True)[:n]

    def factors_as_dicts(self) -> list[dict]:
        return [f.as_dict() for f in sorted(self.factors, key=lambda f: f.points, reverse=True)]


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def score_churn(
    metrics: MetricResult,
    *,
    expected_cycle_days: float,
    is_new_customer: bool = False,
    tenure_days: int | None = None,
    messages_sent_90d: int = 0,
) -> ChurnResult:
    """Compute a 0-100 churn risk score with per-factor attribution.

    ``tenure_days`` (days since signup) is only used for customers who have
    never completed an order, where there is no purchase cadence to measure.
    """
    factors: list[ChurnFactor] = []

    def add(code: str, severity: float, detail: str) -> None:
        severity = _clamp01(severity)
        if severity <= 0:
            return
        points = FACTOR_WEIGHTS[code] * severity
        factors.append(ChurnFactor(code, FACTOR_LABELS[code], severity, points, detail))

    days_since = metrics.days_since_last_order
    cycle = max(expected_cycle_days, 1.0)

    # 1. Cadence overdue - the dominant signal.
    if metrics.completed_orders == 0:
        # Never purchased: treat time since signup as overdue against the
        # expected time-to-first-order (one cycle).
        tenure = tenure_days or 0
        add(
            "cadence_overdue",
            _clamp01(tenure / (cycle * 3.0)),
            f"Registered {tenure} days ago and has still not placed a first order."
            if tenure
            else "Has never placed an order.",
        )
        add(
            "single_order",
            1.0,
            "No completed orders, so there is no purchase habit to retain.",
        )
    elif days_since is not None:
        overdue_ratio = (days_since - cycle) / cycle
        if overdue_ratio > 0:
            severity = _clamp01(overdue_ratio / CADENCE_SATURATION_MULTIPLE)
            add(
                "cadence_overdue",
                severity,
                f"{days_since} days since their last order against an expected "
                f"{int(cycle)}-day cycle ({int(days_since - cycle)} days overdue).",
            )

    # 2. Frequency decline quarter over quarter. A customer with purchase
    #    history and *no* orders in either recent window has stopped entirely,
    #    which the ratio-based trend cannot express (0 vs 0 reads as flat).
    if metrics.completed_orders > 0:
        if metrics.orders_last_90d == 0:
            add(
                "frequency_decline",
                1.0,
                "No orders at all in the last 90 days despite prior purchase history.",
            )
        elif metrics.frequency_trend < 0:
            add(
                "frequency_decline",
                abs(metrics.frequency_trend),
                f"{metrics.orders_last_90d} orders in the last 90 days vs "
                f"{metrics.orders_prev_90d} in the 90 days before.",
            )

        # 3. Spend decline.
        if metrics.revenue_last_90d == 0:
            add(
                "spend_decline",
                1.0,
                "No revenue in the last 90 days despite prior purchase history.",
            )
        elif metrics.spend_trend < 0:
            add(
                "spend_decline",
                abs(metrics.spend_trend),
                f"${metrics.revenue_last_90d:,.0f} spent in the last 90 days vs "
                f"${metrics.revenue_prev_90d:,.0f} in the 90 days before.",
            )

    # 4. Engagement decline. Only meaningful once we have actually messaged
    #    them; otherwise a zero engagement score says nothing about the customer.
    if messages_sent_90d > 0 and metrics.engagement_score < 40:
        severity = _clamp01((40 - metrics.engagement_score) / 40)
        add(
            "engagement_decline",
            severity,
            f"Engagement score of {metrics.engagement_score:.0f}/100 across "
            f"{messages_sent_90d} messages sent in the last 90 days.",
        )

    # 5. Single-order customers churn far more often than repeat buyers.
    if metrics.completed_orders == 1:
        severity = 0.5 if is_new_customer else 0.9
        add(
            "single_order",
            severity,
            "Only one completed order, so no repeat-purchase habit is established.",
        )

    # 6. Discount dependency.
    if metrics.discount_dependency > 0.5:
        add(
            "discount_dependency",
            _clamp01((metrics.discount_dependency - 0.5) / 0.5),
            f"{metrics.discount_dependency * 100:.0f}% of their order value relies on "
            "discounting.",
        )

    # 7. Cancellations / refunds.
    if metrics.cancelled_orders > 0 and metrics.total_orders > 0:
        ratio = metrics.cancelled_orders / metrics.total_orders
        add(
            "order_problems",
            _clamp01(ratio * 2),
            f"{metrics.cancelled_orders} of {metrics.total_orders} orders were "
            "cancelled or refunded.",
        )

    score = round(min(100.0, sum(f.points for f in factors)), 1)

    # Brand-new customers inside their first cycle should not look risky.
    if is_new_customer and days_since is not None and days_since <= cycle:
        score = round(min(score, 20.0), 1)

    band = ChurnRiskBand.LOW
    for threshold, candidate in BAND_THRESHOLDS:
        if score >= threshold:
            band = candidate
            break

    result = ChurnResult(score=score, risk_band=band, factors=factors)
    result.revenue_at_risk = round(_revenue_at_risk(metrics, score), 2)
    result.explanation = build_explanation(result, metrics)
    return result


def _revenue_at_risk(metrics: MetricResult, score: float) -> float:
    """Annualised revenue exposed by this customer leaving."""
    annual_run_rate = metrics.average_order_value * metrics.purchase_frequency_per_month * 12
    if annual_run_rate <= 0:
        annual_run_rate = metrics.lifetime_revenue
    return annual_run_rate * (score / 100.0)


def build_explanation(result: ChurnResult, metrics: MetricResult) -> str:
    """Plain-English summary derived only from computed factors."""
    if not result.factors:
        if metrics.completed_orders == 0:
            return "No completed orders yet, so there is no purchase pattern to assess."
        return (
            f"Ordering on schedule with {metrics.completed_orders} completed orders and "
            f"{metrics.days_since_last_order} days since the last one. No risk signals detected."
        )

    top = result.top_factors(3)
    lead = (
        f"Churn risk is {result.risk_band.value.lower()} at {result.score:.0f}/100. "
        if result.score > 0
        else ""
    )
    bullets = " ".join(f"{f.detail}" for f in top)
    return f"{lead}{bullets}".strip()
