# PROJECT STATUS — GIMME Retention Engine

**Last updated:** 2026-08-21
**State:** MVP complete and verified from a clean install.

## Product goal

A local-first, AI-assisted customer retention platform for GIMME Beverage
Delivery. It ingests customer and order data, builds Customer 360 profiles,
computes lifecycle stage, RFM, churn risk and next-best-action, generates
grounded personalised messages with an LLM, and runs compliance-gated
campaigns with full event tracking and revenue attribution.

## Current phase

Complete. All seven milestones verified, including the closed retention loop
and a fresh-install run following the README from an empty tree.

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

**Frontend** — 16 pages, shared UI primitives with loading/empty/error states,
consistent colour semantics, responsive down to 390px.

## Test coverage

| Suite | Count | Command |
| --- | --- | --- |
| Backend | 306 | `make test-backend` |
| Frontend | 35 | `make test-frontend` |
| Browser (Playwright) | 10 | `make test-e2e` |
| **Total** | **351** | `make test` |

## Known bugs

None open. Ten defects were found and fixed during verification; they are
listed in `VERIFICATION.md` and `ERROR_LOG.md`.

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

Fresh install from an empty working tree following the README:
`make setup` → `make seed` → `make test` → both servers started →
`npx playwright test`. 351 tests passing, zero console errors.

## Next task

None for the MVP. Recommended next steps are in `FINAL_REPORT.md` —
principally exercising the live adapters once credentials exist, and building
the images once a Docker daemon is available.
