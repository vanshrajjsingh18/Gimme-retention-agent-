# PROJECT STATUS — GIMME Retention Engine

**Last updated:** 2026-09-24
**State:** MVP and campaign automations complete and verified from a clean
install, plus deployment, the live TNZ path, Smart Reorder, GIMME data import,
and campaign copy that is personalised and chosen rather than assumed.

## Product goal

A local-first, AI-assisted customer retention platform for GIMME Beverage
Delivery. It ingests customer and order data, builds Customer 360 profiles,
computes lifecycle stage, RFM, churn risk and next-best-action, generates
grounded personalised messages with an LLM, and runs compliance-gated
campaigns with full event tracking and revenue attribution.

## Current phase

Getting GIMME's own data and copy through the system, with the live TNZ path
ready for credentials.

Earlier phases are complete: all seven MVP milestones verified including the
closed retention loop and a fresh-install run from an empty tree, and three
recurring campaign types sharing one send pipeline so consent, quiet hours,
deduplication, delivery tracking and dry-run behave identically across all
three.

Since then, and in commit order:

- **Deployment** — one Railway service on Postgres serving the built dashboard
  from the API, with the traps closed (no silent SQLite in production, no
  compiler needed at build). `docs/deploy-railway.md` records what the deploy
  proved and what it did not.
- **The live TNZ path** — credentials enterable before going live, a mock
  connection test that cannot pass for a provider, TNZ's documented request
  shape, its refusal reasons carried into the error shown, and a go-live
  readiness panel computed at runtime (`docs/tnz-go-live.md`).
- **Ordering habits and Smart Reorder** — each customer's order time learned to
  the minute on a circular clock and read on their own clock, exposed on the
  customer API and given a page of its own, with a ledger recording what the
  prediction engine promised and whether it happened.
- **Importing GIMME's export** — one file for customers, orders and lines; brand
  and category read out of it so preferences compute; segment membership
  recomputed when a file lands; and an import that will not silently load a
  list nobody can contact.
- **Copy** — merge tags in campaign sends, age verification assumable by
  configuration where a file omits the column, and `copy_mode` on the campaign
  deciding whether the approved body is the message or a per-recipient draft.
- **Smart Reorder** — the per-customer reorder reminder finished and made
  coherent: one timing engine behind both the sender and the screens, the
  reminder offset configurable per campaign, suppression when the customer has
  already ordered this cycle, the prediction stored on `customer_metrics` so
  segments and the dashboard can read it, and three dynamic segments over it.
- **Personalisation** — one whitelist of customer fields behind a Personalize
  menu that inserts at the cursor and a live preview rendered by the server,
  shared by the campaign composer, the automation composer and Smart Reorder.
  A tag resolves against that list or not at all, an unknown one blocks
  approval and activation, and every send records what was filled in and what
  fell back. Spreadsheet column names are mapped to those fields on import, so
  an export calling it "First Name" or "Item Name" still feeds `#first_name#`
  and `#product#`. Each tag names the column it reads, and a test holds the
  two lists together: a column in the upload format either has a tag or has a
  recorded reason for not having one.

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

**Campaign copy** — a campaign says who writes it. `copy_mode` is `WRITTEN`,
where the approved body is the message and merge tags fill in each recipient's
own details, or `DRAFTED`, where every recipient's message is written at send
time and the body is the fallback. It is stored on the campaign, not passed at
send time, because it decides what the approver is approving; changing it
withdraws approval as editing the body does. Either way, the copy preview shows
three real recipients' messages, produced by the same calls the send makes.

**Frontend** — 18 pages, shared UI primitives with loading/empty/error states,
consistent colour semantics, responsive down to 390px. The automation detail
page's centrepiece is the dry run: who would receive what, in NZ local time,
and who would not with the reason in plain English.

## Test coverage

| Suite | Count | Command |
| --- | --- | --- |
| Backend | 771 | `make test-backend` |
| Frontend | 65 | `make test-frontend` |
| Browser (Playwright) | 17 | `make test-e2e` |
| **Total** | **853** | `make test` |

## Known bugs

None open. Every defect found so far is recorded in `ERROR_LOG.md` with what found them and what prevents a
recurrence, along with one entry that turned out not to be a product defect at
all — a database corrupted by my own hand-written cleanup — kept because the
misdiagnosis it caused is the useful part.

Six from the automation phase are worth singling out, because none was caught
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
  06:00 for an 18:00 send;
- nothing required an SMS to say how to opt out. Every template happened to end
  with "Reply STOP to opt out.", so the gap was invisible until copy started
  being drafted per customer and the drafts left it off.

The third and fourth were found by screenshotting the page rather than reading
the JSON the endpoint returned; the last by reading a generated message beside
the template it replaced.

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

2026-09-21, Smart Reorder end to end on seeded data: the campaign dry-run
reported 27 evaluated / 12 eligible with every exclusion named, activation
enrolled 27 customers, and the upcoming view showed each customer's learned
routine with the reminder that follows from it — a 6:48 PM customer scheduled
at 6:18 PM, exactly the configured 30 minutes ahead, and a 7:47 PM customer
visibly pulled back to 6:00 PM because 7:17 PM is past the send window's
close. 771 backend, 65 frontend and 17 Playwright tests green.

2026-09-20, from an empty tree: `make setup`, `make seed-small`, both servers
started, then a written campaign created, approved and sent in mock mode — the
five recipients received the approved copy with their own name and usual
product in it. An existing database was upgraded in place: the new column was
added and the ten campaigns already in it marked `DRAFTED`.

Earlier, on the automation phase: all three automation types exercised end to
end on real data (cohort send 35 sent / 73 skipped with reasons; sequence
advanced customers through Day 0 → Day 7; nudge enrolled 249 customers with
per-customer order patterns).

## Open configuration item

`BrandSettings.signatory_name` and `signatory_title` are deliberately empty.
Until a real name is set, `{sign_off}` renders as nothing and messages go out
unsigned — attributing outbound customer SMS to an invented person would be
worse. Set these in Brand settings before using `{sign_off}` in any template.

## Next task

Nothing is half-built. The largest remaining gap is not code: the TNZ go-live
blockers are all credential-shaped (auth token, sender, webhook secret, and a
connection test that actually reaches TNZ), and the readiness panel lists them
in the order they have to be done. Beyond that, `FINAL_REPORT.md` holds the
standing recommendations — exercising the live adapters once credentials exist,
and building the Docker images once a daemon is available.
