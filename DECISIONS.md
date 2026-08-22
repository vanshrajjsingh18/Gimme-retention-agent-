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

---

## 2026-08-22 — One automation table for three campaign types

**Decision:** Recurring sequences, behavioural nudges and cohort bulk sends are
one `automations` table with a `kind` discriminator and one shared send
pipeline (`app/automations/runtime.py`), not three parallel implementations.

**Reason:** The three features differ only in *who* to message and *when*.
Everything after that — consent at send time, quiet hours, dedup, dispatch,
the delivery ledger, dry run — is identical, and it is exactly the part where a
bug is a compliance incident rather than a cosmetic defect. Three copies would
mean three places to get consent gating right and three chances to get it
wrong.

**Alternatives considered:** A table per campaign type; a generic workflow
engine driven by JSON config.

**Tradeoffs:** The single table carries columns only some kinds use
(`offset_days` lives on steps, `next_due_at` on enrollments, `recurrence` only
matters to cohort sends). A generic workflow engine would be more flexible and
far harder to reason about — the point here is that "one message per customer
per day" is provable, not configurable.

---

## 2026-08-22 — Every automation is backed by a Campaign row

**Decision:** Creating an automation creates a `Campaign` and stores its id on
`Automation.campaign_id`; every send is attributed to it.

**Reason:** Attribution, campaign analytics and the Customer 360 message
history all key off `campaign_id`. Without a backing campaign, automated sends
would be invisible to every existing report — a parallel reporting world that
has to be maintained separately and inevitably disagrees with the first one.

**Tradeoffs:** A Campaign row that is never sent through the campaign engine
itself, which is mildly surprising when reading the campaigns table directly.

---

## 2026-08-22 — Dedup keys on the customer's local calendar date

**Decision:** `AutomationSend.local_date` stores the date in
`Pacific/Auckland`, and "one automated message per customer per day" is a query
against `(customer_id, local_date)`.

**Reason:** New Zealand is UTC+12/+13, so a UTC day boundary falls at noon
local. Capping on the UTC date would let a customer receive one message at
11am and another at 1pm and call them different days, while treating a 9am and
a 9pm message on the same working day as the same day only by luck.

**Alternatives considered:** Rolling 24-hour window per customer.

**Tradeoffs:** A rolling window is arguably fairer but much harder to explain
to an operator looking at a ledger, and makes "did we message them today?"
depend on the exact minute of the previous send.

---

## 2026-08-22 — Quiet hours defer, they do not drop

**Decision:** A send falling outside 09:00–19:00 local is moved to the next
open slot rather than skipped. Behavioural nudges are the exception: a
late-evening pattern is pulled *back* to 18:00 the same day.

**Reason:** A job that happens to run at 3am must not silently lose the day's
sends. But for a nudge, deferring forward defeats the feature — a customer who
orders at 9pm on Saturday and is nudged at 9am Sunday is being reminded after
the moment has passed. Their nudge belongs earlier the same day, while they are
still deciding.

**Tradeoffs:** Two different behaviours for "outside the window", which has to
be understood rather than assumed. The alternative — one rule everywhere —
would make a third of the nudges useless, since drinks orders skew heavily to
the evening.

---

## 2026-08-22 — Opt-out is global, never per-channel

**Decision:** A STOP reply clears every consent flag, writes an ALL-channel
suppression record, sets `Customer.is_suppressed`, and stops every automation
enrollment the customer has.

**Reason:** A customer replying STOP is withdrawing permission to be contacted,
not expressing a preference about one campaign. The previous per-channel
handling would have let a customer who stopped SMS keep receiving email, and
would have left them enrolled in sequences that resume the moment a data import
restores a consent flag.

**Tradeoffs:** No way to opt out of one channel only. That is the right default
for alcohol marketing; a per-channel preference centre is a deliberate feature
to add later, not a default to fall into.

---

## 2026-08-22 — A behavioural nudge needs three orders, not one

**Decision:** `MIN_ORDERS_FOR_PATTERN = 3` completed orders, over a window of
the last 8, with a confidence score attached and a monthly recompute.

**Reason:** With two orders, a repeated weekday is a 1-in-7 coincidence.
Messaging on it produces a "we know when you usually order" claim that is not
true, which is worse than not messaging at all. On the seeded dataset this
gates the feature to 556 of 1,000 customers — the ones it can actually serve.

**Tradeoffs:** Excludes light buyers, who are exactly the ones a nudge might
convert. They are reachable by the other two campaign types instead.

---

## 2026-08-22 — An offer requires two independent gates

**Decision:** A nudge carries a discount only when the customer's
`discount_dependency` is at or above 0.4 **and** an approved promotion exists
in brand settings.

**Reason:** The first gate stops discount being spent on customers who buy at
full price anyway. The second means the system can never invent an offer: with
no approved promotion the nudge simply goes out without one, rather than
generating a discount the business has not agreed to honour.

**Tradeoffs:** Configuring promotions is now a prerequisite for offers to
appear at all, which is quiet if nobody has set them up. The stated reason on
every `OfferDecision` makes that visible rather than mysterious.

---

## 2026-08-22 — Additive schema reconciliation instead of migrations

**Decision:** `create_tables()` runs `reconcile_schema()`, which adds columns
the models declare but the database lacks. Additive only; a NOT NULL column
with no default is refused and reported.

**Reason:** `create_all` creates missing tables but never missing columns, so
adding a field to a model broke every existing local database at startup with
"no such column". For a local-first tool, an existing database with real data
in it is the normal case, not an edge case.

**Alternatives considered:** Full Alembic migrations (the dependency is already
present); telling developers to delete their database.

**Tradeoffs:** Only handles added columns. Type changes, constraints and
backfills still need a real migration — which is why Alembic stays in
`requirements.txt` rather than being removed.

---

## 2026-08-22 — Editing an automation's message withdraws its approval

**Decision:** Changing `message_template`, `template_overrides`, the audience,
the per-kind `config`, or a sequence's steps clears `approved_at` and pauses an
active automation. Renaming or editing the description does not.

**Reason:** Approval is a human vouching for a specific message going to a
specific group of people. Without this rule, an approved automation is a
standing permission to send *whatever it currently says* — someone could
approve innocuous copy and edit it afterwards, and the compliance gate would
have been bypassed without anyone bypassing anything.

Pausing as well as un-approving is the honest half: `require_approval` already
stops the send, so an un-approved active automation would sit there looking
live while sending nothing. Better that its status says what is true.

**Alternatives considered:** Versioning automations so an edit creates a new
draft; blocking edits on approved automations entirely.

**Tradeoffs:** Fixing a typo costs a re-approval. That is the right price —
the alternative is a gate that can be walked around by editing after the fact.
Versioning would be better still and is the natural next step if approvals
become frequent enough to be a nuisance.
