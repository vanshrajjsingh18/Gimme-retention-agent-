# Architecture Decisions

## 2026-08-20 — Modular monolith over microservices

**Decision:** One FastAPI backend with clear internal module boundaries
(`analytics`, `churn`, `rfm`, `recommendations`, `segmentation`, `campaigns`,
`compliance`, `llm`, `integrations`), plus one React frontend.

**Reason:** The MVP must run locally with two processes. Service boundaries are
enforced by module structure, not network hops.

**Alternatives considered:** Separate scoring service; serverless functions.

**Tradeoffs:** Single deployment unit; scaling is all-or-nothing. Acceptable
for an MVP, and the module boundaries make extraction possible later.

---

## 2026-08-20 — Pure functions for all scoring engines

**Decision:** Metrics, lifecycle, RFM, churn and NBA are pure functions over
plain dataclasses (`OrderFact`, `MetricResult`), with a separate persistence
layer that reads the ORM and writes results.

**Reason:** Scoring logic is the product's core value and must be exhaustively
testable. Pure functions test in milliseconds with no database fixtures, which
made it practical to prove all 9 lifecycle stages reachable and every churn
factor attributable.

**Alternatives considered:** Methods on ORM models; SQL-based scoring.

**Tradeoffs:** One extra mapping layer between ORM rows and dataclasses. Worth
it — the mapping is trivial and the test speed is not.

---

## 2026-08-20 — Churn scoring is deterministic and additive, never LLM-derived

**Decision:** The churn score is the sum of named, weighted factors whose
weights total 100. Each factor exposes `severity` (0-1) and `points`. The LLM
may only rewrite the explanation string for readability.

**Reason:** A retention team must be able to defend why a customer is flagged.
An opaque score is unusable for alcohol marketing where targeting decisions
carry regulatory weight.

**Alternatives considered:** Logistic regression / gradient boosting on
historical churn labels.

**Tradeoffs:** Lower theoretical accuracy than a trained model. There is also
no labelled churn history to train on in an MVP. The factor structure means a
learned model can later replace the weights without changing the interface.

---

## 2026-08-20 — Cadence saturation at 6x the expected cycle

**Decision:** The `cadence_overdue` churn factor reaches full severity at 6x
the customer's expected purchase cycle, not 3x.

**Reason:** Initial testing showed a customer 500 days past a 30-day cycle
scoring only 43.6/100 (MEDIUM) because the dominant factor saturated too early
and the quarter-over-quarter decline factors read "0 orders vs 0 orders" as
flat rather than as total stoppage. Both were fixed: cadence saturates later,
and zero activity in the recent window now registers full decline severity.

**Alternatives considered:** Raising the cadence weight alone (would have
distorted moderately-late customers).

**Tradeoffs:** Slightly lower scores for customers 2-3 cycles late. Verified by
`test_risk_rises_monotonically_with_lateness`.

---

## 2026-08-20 — Lapsed states outrank value tiers in lifecycle classification

**Decision:** A customer past their at-risk/dormant/churn thresholds is
classified by lapse, not by spend. A VIP who has vanished is CHURNED.

**Reason:** The lifecycle stage drives the retention action. Labelling an
absent big spender "VIP" hides exactly the customer the product exists to save.

**Tradeoffs:** Value tier is no longer visible from the stage alone, so
lifetime revenue and RFM are surfaced alongside it in Customer 360.

---

## 2026-08-20 — Quantile RFM with an absolute-threshold fallback

**Decision:** RFM uses population quantiles when there are >= 20 scorable
customers and the quantile breaks are strictly increasing; otherwise it falls
back to fixed bands. Customers with no completed orders are excluded from the
distribution and floored at 111.

**Reason:** Quantiles are meaningless on a tiny or flat population, and
including never-purchased customers would drag every real buyer's percentile.

**Tradeoffs:** Scores shift as the population changes — inherent to quantile
scoring, and mitigated by storing `calculated_at`.
