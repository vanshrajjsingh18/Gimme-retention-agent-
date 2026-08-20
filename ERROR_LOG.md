# Error Log

Only failures with a real root cause are recorded here.

---

## 2026-08-20 — Index name collision on first `create_all`

**Command:** `python -c "Base.metadata.create_all(engine)"`

**Failure:** `sqlite3.OperationalError: index ix_campaign_recipients_status already exists`

**Root cause:** `CampaignRecipient.status` declares `index=True`, which makes
SQLAlchemy auto-generate an index named `ix_campaign_recipients_status`. An
explicit composite `Index("ix_campaign_recipients_status", "campaign_id", "status")`
in `__table_args__` claimed the same name.

**Fix:** Renamed the composite index to
`ix_campaign_recipients_campaign_status`.

**Result:** All 33 tables create cleanly.

**Preventive action:** Never name an explicit index `ix_<table>_<column>` when
that column also sets `index=True`.

---

## 2026-08-20 — Churn score under-reported fully-lapsed customers

**Command:** `pytest tests/test_churn.py`

**Failure:** 4 tests failed. A customer 500 days past a 30-day cycle scored
43.6/100 (MEDIUM) instead of CRITICAL; risk did not rise monotonically with
lateness.

**Root cause:** Two independent defects.
1. `cadence_overdue` saturated at 3x the expected cycle, so 120 days late and
   500 days late both produced identical maximum severity, and its 34-point
   weight capped the reachable score.
2. `frequency_decline` and `spend_decline` were driven by the ratio-based trend
   value. For a customer with zero orders in *both* the recent and prior
   90-day windows, `_trend(0, 0)` returns 0.0 — "flat" — so a customer who had
   completely stopped buying contributed nothing from either factor.

**Fix:** Saturation moved to 6x; weights rebalanced (cadence 40, frequency 18,
spend 16, engagement 8, single_order 10, discount 4, order_problems 4 = 100);
zero activity in the recent window with prior purchase history now registers
severity 1.0 directly rather than going through the trend ratio. Also gated
`engagement_decline` on having actually sent messages, since a zero engagement
score for a never-messaged customer says nothing about them, and replaced the
`_prev_orders` trend-reversal hack with a real `orders_prev_90d` metric field.

**Result:** 19/19 churn tests pass; lateness now scores monotonically
(0 → 14.6 → 33.8 → 56.7 → 74 across 5/40/80/150/400 days late).

**Preventive action:** `test_risk_rises_monotonically_with_lateness` and
`test_factor_weights_sum_to_100` guard both defects.
