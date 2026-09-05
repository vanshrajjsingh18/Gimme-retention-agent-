# GIMME Retention Engine

A local-first customer retention platform for GIMME, a New Zealand on-demand
beverage delivery business. It ingests customer and order data, builds Customer
360 profiles, computes lifecycle stage, RFM, churn risk and next-best-action,
generates grounded personalised messages with an LLM, and runs
compliance-gated campaigns with full event tracking and revenue attribution.

Everything runs on your machine. With no credentials configured it operates in
**mock mode**: messages are generated locally, recorded, and given realistic
simulated delivery and engagement — the entire product workflow is
demonstrable end to end without an external account.

---

## Quick start

```bash
git clone <this repo> && cd Gimme-retention-agent-
cp .env.example .env          # every value has a working default

make setup                    # install backend + frontend dependencies
make seed                     # create the database, generate 1,000 customers
```

Then run the two servers in separate terminals:

```bash
make backend                  # http://127.0.0.1:8000  (API + /docs)
make frontend                 # http://127.0.0.1:5173  (UI)
```

Open <http://127.0.0.1:5173> and sign in:

| Field    | Value                        |
| -------- | ---------------------------- |
| Email    | `admin@gimmedelivery.co.nz`  |
| Password | `GimmeAdmin123!`             |

These are **local development credentials only**. Change `ADMIN_PASSWORD` and
`SECRET_KEY` in `.env` before running this anywhere shared.

### With Docker instead

```bash
docker compose up --build -d
docker compose exec backend python -m scripts.seed_demo --customers 1000 --reset
```

Same URLs. `docker compose down` stops it; the database lives in a named
volume and survives restarts.

---

## What it does

**Ingest** — CSV upload with drag-and-drop, preview, per-row validation,
duplicate detection and a downloadable error report; plus authenticated
JSON APIs for customers, orders, order items, events and consent events. A
watched inbox folder imports files dropped into `data/inbox/`.

**Understand** — five deterministic engines compute, for every customer:
behavioural metrics, a 9-stage lifecycle classification, RFM scores, a
transparent 0-100 churn risk score with per-factor attribution, and a next
best action with reason codes. None of this involves the LLM.

**Segment** — a visual rule builder over 30+ fields with nested AND/OR
groups, live preview counts, and 14 built-in segments that re-evaluate as
data changes.

**Write** — an LLM writes personalised messages from a grounding context of
verified facts only. Output is validated before it can be approved: invented
coupon codes, discounts, products, prices, delivery claims, stock claims and
customer facts are all rejected.

**Send** — campaigns pass a compliance check and explicit human approval
before sending. Each recipient is re-checked at send time for age
verification, marketing and channel consent, suppression, frequency caps and
quiet hours.

**Automate** — three recurring campaign types over one shared send pipeline:
*cohort sends* to whoever matches a segment at send time; *sequences* whose
steps are timed from each customer's own enrollment; and *behavioural nudges*
that message each customer at the day and time they usually order, derived
from their order history. All three re-check consent at dispatch, respect NZ
business hours, and never let two automations reach the same customer on the
same day. Every one can be dry-run first: exactly who would receive what, and
who would not, with the reason.

**Close the loop** — delivery and engagement events are recorded, a returning
customer's order is detected as a reactivation, attributed to the campaign
that touched them, and the revenue appears on the dashboards.

---

## Project layout

```
backend/          FastAPI application
  app/
    analytics/      metric computation and dashboard queries
    api/v1/         HTTP routes
    campaigns/      audience, compliance gating, sending
    churn/          churn scoring engine
    compliance/     alcohol-marketing rules and eligibility
    core/           config, database, security, enums
    integrations/   Outlook, TNZ, WhatsApp adapters (+ mocks)
    journeys/       step-based journey execution
    llm/            provider abstraction, prompts, output validation
    models/         SQLAlchemy entities
    recommendations/ next-best-action engine
    rfm/            RFM scoring
    schemas/        Pydantic request/response models
    segmentation/   segment rule evaluation
    services/       persistence layer bridging engines and the database
  scripts/        init_db, seed_demo
  tests/          513 backend tests
frontend/         React + TypeScript + Vite + Tailwind + Recharts
  src/
    api/ components/ features/ hooks/ layouts/ pages/ types/ utils/
  e2e/            Playwright browser tests
docs/             architecture, API, automations, compliance, integrations
sample-data/      example CSVs matching the import formats
docker/           Dockerfiles and nginx config
```

---

## Testing

```bash
make test          # backend (pytest) + frontend (vitest)
make test-backend  # 513 tests
make test-frontend # 54 tests
make test-e2e      # 13 Playwright tests — needs both servers running
```

The backend suite includes a 24-step end-to-end scenario
(`tests/test_e2e_retention_loop.py`) that drives the entire product workflow
through the HTTP API: upload data, inspect computed intelligence, generate and
approve a message, build a segment, create a compliance-gated campaign, send it
in mock mode, ingest a returning order, and verify the reactivation,
attribution and analytics that follow.

The Playwright suite drives the real UI against a live backend and fails on any
uncaught console error or failed API request.

---

## Configuration

All settings live in `.env` — see [`.env.example`](.env.example) for the full
annotated list. The ones that matter most:

| Variable                                        | Default  | Effect                                                        |
| ----------------------------------------------- | -------- | ------------------------------------------------------------- |
| `SECRET_KEY`                                    | dev value| Signs tokens and derives API-key hashes. **Change it.**        |
| `ADMIN_EMAIL` / `ADMIN_PASSWORD`                | dev login| The account created on first start.                            |
| `DATABASE_URL`                                  | SQLite   | Point at PostgreSQL to move off SQLite; no code change needed. |
| `LLM_PROVIDER` / `LLM_API_KEY`                  | `mock`   | `openai` plus a key uses a real model; empty key falls back to mock. |
| `EMAIL_PROVIDER_MODE` etc.                      | `mock`   | Per-channel send mode; also switchable in the UI.              |
| `ENABLE_SCHEDULER`                              | `true`   | Background refresh, scheduled sends, inbox ingestion.          |

---

## Using a real LLM

Mock mode is the default and produces grounded, validated copy with no
external call. To use a real model, set in `.env`:

```bash
LLM_PROVIDER=openai
LLM_API_KEY=sk-...
LLM_BASE_URL=https://api.openai.com/v1   # any OpenAI-compatible endpoint
LLM_MODEL=gpt-4o-mini
```

Restart the backend. The same grounding prompt and the same output validation
apply either way — swapping providers cannot change the safety properties,
because validation happens outside the provider.

---

## Connecting real message providers

Go to **Integrations**, switch a provider to *live* and enter its credentials.
Secrets are stored server-side and never returned to the browser — only a
masked hint. If a required credential is missing the system falls back to mock
rather than silently dropping messages.

See [`docs/integrations.md`](docs/integrations.md) for what each provider needs.

---

## Documentation

- [Architecture](docs/architecture.md) — how the pieces fit and why
- [API reference](docs/api.md) — every endpoint, with examples
- [Automations](docs/automations.md) — the three recurring campaign types, and the consent/dedup/quiet-hours rules they share
- [Compliance](docs/compliance.md) — the alcohol-marketing rules and how they are enforced
- [Integrations](docs/integrations.md) — provider setup and webhooks
- [DECISIONS.md](DECISIONS.md) — architecture decisions and their tradeoffs
- [FINAL_REPORT.md](FINAL_REPORT.md) — build summary, test results, known limitations

Interactive API documentation is served at <http://127.0.0.1:8000/docs>.

---

## A note on the demo data

`make seed` generates 1,000 synthetic customers with 12 months of order history
from a fixed random seed, so the dataset is identical on every machine. It
spans thirteen behavioural personas, which is what makes all nine lifecycle
stages, all four churn bands and twelve RFM segments appear in the dashboards.

**No real customer data is used anywhere in this project.** All names,
addresses, emails and phone numbers are generated; email addresses use the
reserved `.test` TLD and cannot receive mail.
