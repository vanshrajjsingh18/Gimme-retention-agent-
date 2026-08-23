# PROJECT STATUS — GIMME Retention Engine

**Last updated:** 2026-08-22
**State:** MVP complete and verified from a clean install, plus campaign
automations (recurring sequences, behavioural nudges, cohort bulk sends).

## Product goal

A local-first, AI-assisted customer retention platform for GIMME Beverage
Delivery. It ingests customer and order data, builds Customer 360 profiles,
computes lifecycle stage, RFM, churn risk and next-best-action, generates
grounded personalised messages with an LLM, and runs compliance-gated
campaigns with full event tracking and revenue attribution.

## Current phase

Complete. All seven MVP milestones verified, including the closed retention
loop and a fresh-install run following the README from an empty tree.

The campaign-automation phase is also complete: three recurring campaign types
built on the existing TNZ integration, sharing one send pipeline so consent,
quiet hours, deduplication, delivery tracking and dry-run behave identically
across all three.

## Completed features

**Foundation** — FastAPI backend, React frontend, 33-table SQLite schema
(PostgreSQL-portable), JWT sessions plus hashed API keys, APScheduler jobs,
seed command producing 1,000 customers with 12 months of history.

**Intelligence** — five deterministic engines as pure functions: behavioural
metrics, 9-stage lifecycle classification with per-customer cadence, RFM with
quantile scoring and small-population fallback, transparent 0-100 churn
scoring with per-factor attribution, and next-best-action with reason codes.

**Data** — CSV upload with preview, per-row validation, duplicate detection
and downloadable error reports; authenticated ingestion APIs for customers,
orders, order items, events and consent events; watched inbox folder.

**Segmentation** — nested AND/OR rule engine over 30+ fields with six operator
families, live preview, 14 built-in segments, CSV export.

**Messaging** — LLM provider abstraction (mock + OpenAI-compatible), versioned
grounding prompts, and output validation rejecting invented coupons,
promotions, products, prices, delivery claims, stock claims and customer facts.

**Campaigns** — audience snapshots with per-recipient exclusion reasons,
compliance gating, human approval, mock sending with simulated delivery and
engagement, and full event tracking.

**Compliance** — alcohol-marketing rules enforced in code: age verification,
consent per channel, suppression, frequency caps, quiet hours, prohibited
claims, and vulnerability-targeting checks.

**Attribution** — configurable last-touch windows, reactivation detection,
idempotent per-order records, campaign revenue roll-up.

**Analytics** — overview, customer, churn, campaign and cohort dashboards, all
computed from the database at request time.

**Journeys** — step-based execution with triggers, delays, conditions and
actions; messages still pass the same grounding and compliance checks.

**Automations** — three recurring campaign types over one shared send pipeline
(`app/automations/`):

- *Cohort bulk sends* — one-off or recurring, with the audience re-evaluated
  from live segmentation at send time rather than a snapshot, and copy
  defaulting to the segment's own tone.
- *Recurring sequences* — steps timed by offset from each customer's own
  enrollment, rolling or fixed-cohort enrollment, stopping on opt-out, on an
  order, or at an end date. A skipped step is retried, not consumed.
- *Behavioural nudges* — a standing per-customer message at the day and time
  they usually order, derived from their order history (minimum three completed
  orders, eight-order window, daily staleness check), with an offer only where
  their discount dependency justifies it *and* an approved promotion exists.

Automations can be edited after creation. Changing the copy or the audience
withdraws approval and pauses the automation, because approval was given for
the message that was there — the editor says so before saving.

Shared across all three: consent re-checked at send time; NZ business hours
(09:00–19:00 `Pacific/Auckland`) with deferral rather than dropping; one message
per customer per *local* day resolved by priority; a delivery ledger recording
every attempt including skips and their reasons; and a dry run that takes the
identical code path and stops before the provider call.

**Timezone handling** — `app/core/timezones.py` is the single place anything
reasons about the customer's clock. The database stores naive UTC throughout.

**Global opt-out** — a STOP reply clears every consent flag, writes an
ALL-channel suppression record and stops every automation enrollment, across
all campaign types. Inbound reply bodies are read from the TNZ webhook and
resolved by phone number when the provider does not echo our message id back.

**Schema reconciliation** — `create_all` adds missing tables but never missing
columns, so `app/core/schema.py` adds declared columns additively and backfills
their defaults. An existing local database survives a model change.

**Frontend** — 18 pages, shared UI primitives with loading/empty/error states,
consistent colour semantics, responsive down to 390px. The automation detail
page's centrepiece is the dry run: who would receive what, in NZ local time,
and who would not with the reason in plain English.

## Test coverage

| Suite | Count | Command |
| --- | --- | --- |
| Backend | 473 | `make test-backend` |
| Frontend | 53 | `make test-frontend` |
| Browser (Playwright) | 13 | `make test-e2e` |
| **Total** | **539** | `make test` |

## Known bugs

None open. Twenty-two defects were found and fixed across both phases; all are
recorded in `ERROR_LOG.md` with what found them and what prevents a recurrence.

Five from the automation phase are worth singling out, because none was caught
by a test — each was found by running the system and looking at it:

- quiet hours were being compared against UTC, which in New Zealand is wrong by
  half a day in the direction that texts people at 9pm;
- adding a column to a model broke startup on every existing database, because
  `create_all` never adds columns;
- behavioural nudges for evening buyers were being deferred to the next morning
  — after the moment the message was timed to catch;
- a nudge could never be previewed before approval, because enrollment only
  happened on a live run, so the preview was always empty;
- timestamps labelled "NZ" were rendered in the viewer's timezone, showing
  06:00 for an 18:00 send.

The last two were found by screenshotting the page rather than reading the
JSON the endpoint returned.

## External blockers

Each has working code that could not be exercised here:

- **No LLM API key** — the OpenAI-compatible adapter is written and
  unit-tested, but no live call was made. Mock mode is the default and is
  fully functional.
- **No Microsoft Graph, TNZ or WhatsApp credentials** — all three live
  adapters are implemented; only the mock counterparts were exercised.
- **No Docker daemon** — `docker compose config` validates and every build
  path was checked, but the images were never built.

## Last successful verification

Both servers started against the seeded database, all three automation types
exercised end to end on real data (cohort send 35 sent / 73 skipped with
reasons; sequence advanced customers through Day 0 → Day 7; nudge enrolled 249
customers with per-customer order patterns), then 473 backend, 53 frontend and
13 Playwright tests run green with zero console errors.

## Open configuration item

`BrandSettings.signatory_name` and `signatory_title` are deliberately empty.
Until a real name is set, `{sign_off}` renders as nothing and messages go out
unsigned — attributing outbound customer SMS to an invented person would be
worse. Set these in Brand settings before using `{sign_off}` in any template.

## Next task

None outstanding. Recommended next steps are in `FINAL_REPORT.md` —
principally exercising the live adapters once credentials exist, and building
the images once a Docker daemon is available.
