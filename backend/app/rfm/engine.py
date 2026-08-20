"""Quantile-based RFM scoring.

Scores are 1-5. Recency is inverted (fewer days since the last order scores
higher). When the population is too small or too flat for meaningful
quantiles, the engine falls back to absolute thresholds so scores stay stable.
"""
from __future__ import annotations

from dataclasses import dataclass

MIN_POPULATION_FOR_QUANTILES = 20

# Absolute fallbacks (days / order count / NZD spend)
RECENCY_BANDS = [14, 30, 60, 120]  # <=14 -> 5, <=30 -> 4, <=60 -> 3, <=120 -> 2, else 1
FREQUENCY_BANDS = [1, 2, 4, 8]  # >8 -> 5
MONETARY_BANDS = [80, 200, 500, 1200]


@dataclass
class RfmInput:
    customer_id: int
    recency_days: int | None
    frequency: int
    monetary: float


@dataclass
class RfmResult:
    customer_id: int
    recency_score: int
    frequency_score: int
    monetary_score: int
    rfm_cell: str
    rfm_total: int
    rfm_segment: str
    recency_days: int | None
    frequency_value: int
    monetary_value: float


def _quantile_breaks(values: list[float]) -> list[float]:
    """Return the 20/40/60/80th percentile cut points of ``values``."""
    ordered = sorted(values)
    n = len(ordered)
    breaks = []
    for q in (0.2, 0.4, 0.6, 0.8):
        idx = min(int(q * n), n - 1)
        breaks.append(ordered[idx])
    return breaks


def _score_ascending(value: float, breaks: list[float]) -> int:
    """Higher value -> higher score (used for frequency and monetary)."""
    for i, b in enumerate(breaks):
        if value <= b:
            return i + 1
    return 5


def _score_descending(value: float, breaks: list[float]) -> int:
    """Lower value -> higher score (used for recency in days)."""
    for i, b in enumerate(breaks):
        if value <= b:
            return 5 - i
    return 1


def _usable(breaks: list[float]) -> bool:
    """Quantile breaks are only useful when they are strictly increasing."""
    return len(set(breaks)) == len(breaks)


def score_population(inputs: list[RfmInput]) -> list[RfmResult]:
    """Score an entire customer population in one pass."""
    scored_inputs = [i for i in inputs if i.frequency > 0 and i.recency_days is not None]

    use_quantiles = len(scored_inputs) >= MIN_POPULATION_FOR_QUANTILES
    r_breaks = f_breaks = m_breaks = None
    if use_quantiles:
        r_candidate = _quantile_breaks([float(i.recency_days) for i in scored_inputs])  # type: ignore[arg-type]
        f_candidate = _quantile_breaks([float(i.frequency) for i in scored_inputs])
        m_candidate = _quantile_breaks([i.monetary for i in scored_inputs])
        r_breaks = r_candidate if _usable(r_candidate) else None
        f_breaks = f_candidate if _usable(f_candidate) else None
        m_breaks = m_candidate if _usable(m_candidate) else None

    results: list[RfmResult] = []
    for item in inputs:
        if item.frequency <= 0 or item.recency_days is None:
            # A customer with no completed orders scores the floor in every
            # dimension rather than being excluded from the distribution.
            results.append(
                RfmResult(
                    customer_id=item.customer_id,
                    recency_score=1,
                    frequency_score=1,
                    monetary_score=1,
                    rfm_cell="111",
                    rfm_total=3,
                    rfm_segment="Prospects",
                    recency_days=item.recency_days,
                    frequency_value=item.frequency,
                    monetary_value=item.monetary,
                )
            )
            continue

        r = (
            _score_descending(float(item.recency_days), r_breaks)
            if r_breaks
            else _score_descending(float(item.recency_days), RECENCY_BANDS)
        )
        f = (
            _score_ascending(float(item.frequency), f_breaks)
            if f_breaks
            else _score_ascending(float(item.frequency), [float(b) for b in FREQUENCY_BANDS])
        )
        m = (
            _score_ascending(item.monetary, m_breaks)
            if m_breaks
            else _score_ascending(item.monetary, [float(b) for b in MONETARY_BANDS])
        )
        cell = f"{r}{f}{m}"
        results.append(
            RfmResult(
                customer_id=item.customer_id,
                recency_score=r,
                frequency_score=f,
                monetary_score=m,
                rfm_cell=cell,
                rfm_total=r + f + m,
                rfm_segment=classify_rfm_segment(r, f, m),
                recency_days=item.recency_days,
                frequency_value=item.frequency,
                monetary_value=item.monetary,
            )
        )
    return results


def classify_rfm_segment(r: int, f: int, m: int) -> str:
    """Map an R/F/M triple onto a named marketing segment."""
    fm = (f + m) / 2
    if r >= 4 and fm >= 4:
        return "Champions"
    if r >= 3 and fm >= 4:
        return "Loyal Customers"
    if r >= 4 and 2 <= fm < 4:
        return "Potential Loyalists"
    if r == 5 and f <= 1:
        return "New Customers"
    if r >= 4 and fm < 2:
        return "Promising"
    if r == 3 and fm >= 2:
        return "Needs Attention"
    if r == 3 and fm < 2:
        return "About To Sleep"
    if r == 2 and fm >= 4:
        return "Cannot Lose Them"
    if r == 2 and fm >= 2:
        return "At Risk"
    if r <= 2 and fm >= 4:
        return "Cannot Lose Them"
    if r <= 2 and fm >= 2:
        return "Hibernating"
    return "Lost"
