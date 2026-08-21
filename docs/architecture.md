# Architecture

## Shape

A modular monolith. One FastAPI backend, one React frontend, one SQLite file.
Module boundaries are enforced by structure rather than network hops, which
keeps the local setup to two processes while leaving each domain extractable
later.

```
┌────────────────┐        ┌──────────────────────────────────────────┐
│  React SPA     │  HTTP  │  FastAPI                                 │
│  (Vite, TS)    │ ─────► │                                          │
└────────────────┘  JWT   │  api/v1  ──►  services  ──►  engines     │
                          │                  │                       │
┌────────────────┐ X-API- │                  ▼                       │
│  Storefront /  │  Key   │            SQLAlchemy ORM                │
│  ETL           │ ─────► │                  │                       │
└────────────────┘        └──────────────────┼───────────────────────┘
                                             ▼
                                     SQLite (or PostgreSQL)
```

## The layering that matters

The single most important structural decision is that **the scoring engines
are pure functions**. They take plain dataclasses and return plain
dataclasses. They do not import the ORM, open a session, or know a database
exists.

```
app/analytics/metrics.py       compute_metrics(orders)      -> MetricResult
app/services/lifecycle.py      classify_lifecycle(metrics)  -> LifecycleResult
app/rfm/engine.py              score_population(inputs)     -> [RfmResult]
app/churn/engine.py            score_churn(metrics)         -> ChurnResult
app/recommendations/engine.py  recommend(context)           -> RecommendationResult
```

`app/services/intelligence.py` is the only place that bridges them to the
database: it reads orders into `OrderFact` objects, runs the engines, and
writes the results back.

This is why the engines have 94 unit tests that run in under a second, and why
it was practical to prove that all nine lifecycle stages are reachable and that
churn risk rises monotonically with lateness. Those properties would be
painful to assert against database fixtures.

## Request flow: what happens when an order arrives

```
POST /api/v1/orders  (X-API-Key)
   │
   ├─ schemas/models.py       shape validation → 422 on a client bug
   ├─ services/ingestion.py   per-row validation → row rejected, batch continues
   ├─                         Order row written
   │
   └─ services/attribution.py process_new_order()
        ├─ record ORDER_COMPLETED event (idempotent)
        ├─ detect reactivation (gap ≥ 90 days since previous order)
        ├─ attribute_order()
        │    ├─ find campaign touches inside the attribution window
        │    ├─ pick last touch, break ties by engagement strength
        │    ├─ write AttributionRecord (unique per order — never double-counts)
        │    └─ roll revenue onto the campaign and mark the recipient converted
        │
        └─ services/intelligence.refresh_customer()
             ├─ compute_metrics
             ├─ classify_lifecycle  → writes a transition row if the stage changed
             ├─ score_churn         → writes score, band, factors, explanation
             └─ recommend           → writes action, reason codes, explanation
```

The customer's lifecycle stage, churn score and recommended action are
therefore correct the moment the order lands, not on the next scheduled run.

## Grounding: how the LLM is constrained

The LLM is the only non-deterministic component, and it is boxed in on both
sides.

**Before generation**, `app/llm/prompts.py` assembles a `GroundingContext`
containing verified facts only: metrics computed from this customer's own
orders, the products they actually bought, the intelligence the engines
produced, and the brand's configured promotions, coupon codes, products and
delivery promise. The system prompt states that anything absent from that
context may not be stated.

**After generation**, `app/llm/validator.py` checks the output independently
of what the prompt said. It rejects unverified coupon codes, unapproved
promotions, product names outside the customer's history and the verified
catalogue, unbacked prices, delivery claims faster than the configured
promise, stock claims, unresolved placeholders, invented customer facts, and
every prohibited alcohol claim.

A message that fails validation cannot be approved, and a campaign will not
send it — `run_campaign` marks that recipient failed and moves on.

This ordering is deliberate: because validation happens outside the provider,
swapping the mock provider for a real model cannot change what the system is
willing to send.

## Compliance: two layers

`app/compliance/engine.py` separates two questions that are often conflated.

**Who may receive this?** `check_recipient` runs per customer, in severity
order, and returns a single reason: age verification, suppression, marketing
consent, channel consent, contactability, frequency caps, quiet hours. The
order is fixed so the reported reason is the most important one, not the first
one checked.

**May we say this?** `check_content` runs over the copy: prohibited claims,
unverified offers, delivery and stock claims, placeholders, and the mandatory
responsible-drinking statement.

Both run at approval time *and* again per recipient at send time, so consent
revoked between preview and send is honoured.

## Idempotency

Three tables carry uniqueness constraints that make replay safe:

| Table                   | Key                | Protects against                      |
| ----------------------- | ------------------ | ------------------------------------- |
| `customer_events`       | `idempotency_key`  | Re-importing the same event file      |
| `communication_events`  | `idempotency_key`  | A provider redelivering a webhook     |
| `attribution_records`   | `order_id`         | Double-counting campaign revenue      |

Event writes use a savepoint, so a duplicate insert does not poison the
surrounding transaction.

## Persistence

SQLAlchemy 2.0 with typed `Mapped[...]` columns. Enums are stored as strings
rather than database enum types, so adding a lifecycle stage or event type is
not a destructive migration.

The schema deliberately avoids SQLite-specific constructs. Moving to
PostgreSQL is a `DATABASE_URL` change; the one place that would need attention
is the `strftime` call in `analytics/dashboards.py`, which is isolated in a
`_month_key()` helper for exactly that reason.

`Base.metadata.create_all()` runs at startup, so a first run needs no separate
migration step. Alembic is installed for when the schema starts evolving
against data worth keeping.

## Background jobs

APScheduler runs three jobs (`app/jobs/scheduler.py`):

- **refresh_intelligence** (hourly) — recomputes everything so lifecycle stages
  age correctly even when no data is being imported. Without it, a customer
  would never become AT_RISK simply by the passage of time.
- **dispatch_campaigns** (every minute) — sends scheduled campaigns whose time
  has arrived.
- **ingest_inbox** (every 2 minutes) — imports CSVs dropped into `data/inbox/`,
  moving each to `processed/` or `failed/`.

Each job catches its own exceptions and logs to `system_logs`; a failure never
kills the scheduler.

## Frontend

React 19 with React Router. No global state library: server state lives in a
small `useQuery` hook that aborts in-flight requests on unmount and ignores
responses superseded by a newer one, which is the entirety of what this app
needed.

`utils/theme.ts` holds the colour meaning of every domain value in one place,
so a lifecycle stage or risk band looks identical in a badge, a table row and
a chart. `utils/charts.ts` wraps Recharts formatters once, because Recharts
hands formatters `ValueType | undefined` and a throw inside a formatter blanks
the entire chart.

## What is deliberately simple

- **Churn scoring is additive, not learned.** There is no labelled churn
  history to train on, and a retention team must be able to defend why a
  customer was flagged. The factor structure means a learned model can later
  replace the weights without changing the interface.
- **Segment rules evaluate in Python, not SQL.** This keeps every operator
  available on derived fields (churn score, RFM cell, days since last order)
  without a query builder. `FIELD_DEFINITIONS` is the single place a SQL
  translator would hook in when the customer base outgrows it. The customer
  *list* endpoint does filter in SQL, because that one is paginated and hot.
- **Journeys are a step list, not a branching graph.** Reliable execution and
  a readable audit trail matter more at this stage than visual sophistication.
