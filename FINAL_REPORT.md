# Final report — GIMME Retention Engine

**Delivered:** 2026-08-21
**State:** Runnable, tested MVP. Verified from a clean install.

---

## Product summary

A local-first customer retention platform for GIMME, a New Zealand on-demand
beverage delivery business.

The product answers three questions and then acts on them. *Who is slipping
away?* — every customer is scored for churn risk against their own purchase
cadence, with the reasoning visible. *What should we say to them?* — a
next-best-action rule engine picks the intervention, and an LLM writes it using
only verified facts. *Did it work?* — a returning customer's order is detected
as a reactivation, attributed to the campaign that reached them, and the
revenue lands on the dashboard.

Because GIMME sells alcohol, the whole path is gated: age verification,
consent per channel, suppression, frequency caps and quiet hours decide who may
be contacted, and a content layer decides what may be said. Both are enforced
in application code, and both run again at send time rather than only at
preview.

It runs entirely on one machine. With no credentials it operates in **mock
mode**, generating messages locally and simulating realistic delivery and
engagement, so the complete workflow is demonstrable without an external
account.

---

## Architecture

A modular monolith: one FastAPI backend, one React frontend, one SQLite file.

```
React SPA ──JWT──►  FastAPI  ──►  services  ──►  pure scoring engines
Storefront ─X-API-Key─►             │
                                    ▼
                            SQLAlchemy → SQLite (PostgreSQL-portable)
```

The structural decision that matters most: **the five scoring engines are pure
functions.** They take dataclasses and return dataclasses; they never touch the
ORM. `services/intelligence.py` is the only bridge. That is why 94 engine tests
run in under a second, and why it was practical to prove all nine lifecycle
stages are reachable and that churn risk rises monotonically with lateness.

The second: **the LLM is boxed in on both sides.** A grounding context of
verified facts goes in; an independent validator checks what comes out. Because
validation lives outside the provider, swapping mock for a real model cannot
change what the system is willing to send.

Full detail in [`docs/architecture.md`](docs/architecture.md); the reasoning
and tradeoffs behind each choice are in [`DECISIONS.md`](DECISIONS.md).

---

## Implemented features

**Ingestion** — CSV upload with drag-and-drop, preview, per-row validation,
duplicate detection, accepted/rejected/updated counts and a downloadable error
report. Authenticated JSON APIs for customers, orders, order items, events and
consent events. A watched inbox folder. One bad row never fails a file.

**Customer intelligence** — behavioural metrics (revenue, AOV, cadence,
discount dependency, category and brand affinity, ordering habits, engagement,
projected LTV); 9-stage lifecycle classification using each customer's own
purchase cadence with configurable global fallbacks; quantile RFM with an
absolute fallback for small or flat populations; transparent 0-100 churn
scoring where every point comes from a named weighted factor; next-best-action
across 12 actions with reason codes.

**Customer 360** — identity, consent, suppression, order history,
communication history, campaign history, every computed metric, lifecycle
timeline, churn factors with their point contributions, RFM breakdown and the
recommended action with its explanation.

**Segmentation** — nested AND/OR rules over 30+ fields with number, string,
enum, boolean, date and list operators; live preview counts and samples; 14
built-in segments; duplicate, archive and CSV export.

**Message Studio** — grounded generation across email, SMS, WhatsApp and push;
seven tone variations; inline editing that always revalidates; approval blocked
while validation fails; test sends; full provenance stored (provider, model,
prompt version, context, validation result, edit state).

**Campaigns** — nine objectives, four channels, audience snapshots with
per-recipient exclusion reasons, compliance gating, human approval, scheduling,
test sends, mock execution with simulated delivery and engagement, and live
metric roll-up.

**Compliance** — eight prohibited alcohol claim categories, eight grounding
rules, mandatory statements, vulnerability-targeting checks, and per-recipient
eligibility across age, consent, suppression, frequency and quiet hours. Rules
are individually toggleable, and disabling one is audited.

**Attribution** — configurable last-touch windows (24h/48h/72h/7d), tie-breaks
by engagement strength, reactivation detection, idempotent per-order records,
campaign and variant revenue roll-up.

**Analytics** — overview, customer, churn, campaign and cohort dashboards,
every figure computed from the database at request time.

**Journeys** — nine triggers, four delay types, seven conditions and eight
actions, executed as an ordered step list with a per-customer execution log.

**Operations** — background jobs, audit log, system log, API key management,
integration configuration with masked secrets, and demo-data regeneration.

---

## Mocked features

Everything below has a complete live implementation that could not be exercised
here. Mock mode is not a stub — it produces realistic behaviour and is what
makes the product demonstrable.

| Feature | Mock behaviour |
| --- | --- |
| LLM generation | Deterministic local writer using the same grounding context and the same output validation as a real provider |
| Email (Outlook) | Accepts sends, returns a provider message ID, simulates delivery, opens, clicks and opt-outs |
| SMS (TNZ) | Same, with SMS-appropriate rates and no open tracking |
| WhatsApp | Same, with read receipts and replies |
| Delivery events | Generated per message, scaled by the customer's own engagement score |
| Provider webhooks | Endpoints accept both provider-style and internal payloads |

All simulated records carry `is_simulated = true` and are labelled in the UI.
Randomness is seeded from `MOCK_SEED`, so a given customer and message always
behave identically.

---

## External credentials required

To move any of these to live:

| Integration | Needs |
| --- | --- |
| **LLM** | An API key for any OpenAI-compatible endpoint. Set `LLM_PROVIDER=openai` and `LLM_API_KEY`. |
| **Microsoft Outlook** | An Entra app registration with the application permission `Mail.Send` and admin consent; tenant ID, client ID, client secret, sender mailbox. Note that Graph's `sendMail` reports no bounce or open events — those need Exchange message trace or a mail gateway. |
| **TNZ SMS** | A TNZ Group account with REST API access; auth token and registered sender. |
| **WhatsApp** | An account with Meta Cloud API, Twilio or 360dialog; credentials vary by profile. |

Setup steps for each are in [`docs/integrations.md`](docs/integrations.md).

---

## How to start

```bash
cp .env.example .env
make setup      # install backend and frontend dependencies
make seed       # create the database, generate 1,000 customers
```

Then, in two terminals:

```bash
make backend    # http://127.0.0.1:8000  (API + /docs)
make frontend   # http://127.0.0.1:5173  (UI)
```

With Docker instead:

```bash
docker compose up --build -d
docker compose exec backend python -m scripts.seed_demo --customers 1000 --reset
```

## Demo credentials

| Field | Value |
| --- | --- |
| Email | `admin@gimmedelivery.co.nz` |
| Password | `GimmeAdmin123!` |

**Local development only.** Change `ADMIN_PASSWORD` and `SECRET_KEY` in `.env`
before running this anywhere shared.

## How to run tests

```bash
make test           # backend + frontend
make test-backend   # 306 tests
make test-frontend  # 35 tests
make test-e2e       # 10 browser tests (needs both servers running)
```

## How to execute the complete demo workflow

1. Sign in and read the **Overview** — 1,000 customers, revenue, at-risk
   counts and lifecycle distribution, all from the database.
2. **Customers** → filter to *At Risk* → open a profile. Every metric,
   the churn score with its contributing factors, RFM and the recommended
   action are computed, not stored placeholders.
3. **Brand** → confirm the responsible drinking statement and note that
   *Approved promotions* is empty — nothing may be offered.
4. **Message Studio** → pick a customer → **Generate**. The message names
   products they actually bought and their real order gap. Now paste in
   `Take 40% off with code MEGA50! Only 2 left in stock.` and save: four
   blocking findings appear and **Approve** is disabled.
5. **Segments** → **New segment** → add conditions and watch the match count
   update live.
6. **Campaigns** → **New campaign** → target *High Value At Risk*.
7. On the campaign page, read the **Audience** panel: eligible versus excluded,
   broken down by no consent, unverified age and suppression.
8. Try **Run campaign** before approving — refused. Run the **compliance
   check**, submit, approve, then send a **test message**.
9. **Run campaign**. Messages are generated per customer, validated, sent in
   mock mode, and delivery and engagement events are simulated.
10. Post a new order for a lapsed recipient through the API:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/orders \
  -H "X-API-Key: <create one under Data & imports>" \
  -H 'Content-Type: application/json' \
  -d '[{"external_id":"ORD-DEMO-1","customer_external_id":"<their ID>",
        "ordered_at":"'"$(date -u +%Y-%m-%dT%H:%M:%S)"'",
        "status":"COMPLETED","total_amount":128.50}]'
```

11. Reopen their profile: the stage has moved to **Reactivated**, the churn
    score has collapsed, the transition is recorded with its reason, and the
    order is attributed to the campaign.
12. **Campaign analytics** shows the conversion and the attributed revenue.
13. Restart the backend and confirm everything persists.

---

## Test results

| Suite | Tests | Result |
| --- | --- | --- |
| Backend unit — metrics, lifecycle, RFM, churn, NBA, segmentation, compliance, LLM | 219 | pass |
| Backend integration — auth, ingestion, API | 34 | pass |
| Backend end-to-end — 24-step retention loop | 24 | pass |
| Backend security | 29 | pass |
| Frontend unit — formatters, chart helpers, rule builder | 35 | pass |
| Browser — Playwright against the live stack | 10 | pass |
| **Total** | **351** | **all passing** |

Notable properties asserted rather than assumed:

- All nine lifecycle stages are reachable (`test_every_stage_reachable`).
- Churn risk rises monotonically with lateness, and the factor weights sum
  to exactly 100.
- All 99 API routes carry an authentication dependency — checked by walking
  the AST, so a new unguarded endpoint fails the build.
- Consent, suppression or age verification withdrawn *after* approval still
  blocks the send.
- A message failing grounding validation is never delivered, even mid-campaign.
- Re-ingesting an order does not double-count campaign revenue.
- The UI raises no console error and makes no failing API request.

---

## Known limitations

Stated plainly, because an assumed capability is worse than a known gap.

**Not exercised here** (code complete, no credentials or daemon available):
live LLM calls, live sends on all three channels, webhooks against a real
provider, and the Docker image build. `docker compose config` validates and
every build path was checked, but no image was built.

**Deliberately simple**

- **Churn scoring is additive, not learned.** There is no labelled churn
  history to train on, and a retention team must be able to defend a flag. The
  factor structure means a model can later replace the weights without changing
  the interface.
- **Segment rules evaluate in Python.** This keeps every operator available on
  derived fields without a query builder. It loads the candidate set into
  memory, so it will need pushing into SQL well before a million customers.
  The customer *list* endpoint already filters in SQL.
- **Journeys are a linear step list**, not a branching graph. A failed
  condition exits the customer.
- **Attribution is last-touch only.** Multi-touch models would need a
  different record shape.

**Rough edges**

- The frontend bundle is 825KB (230KB gzipped) with no code splitting.
- The schema is created with `create_all` rather than an initial Alembic
  migration; Alembic is installed for when the schema evolves against data
  worth keeping.
- A/B variants can be stored and attributed but not created from the UI.
- Scheduled campaign dispatch and inbox ingestion run on a timer but were not
  observed firing over a real interval.
- One SQLite-specific call (`strftime` for month bucketing) sits behind a
  `_month_key()` helper and would need attention on PostgreSQL.

---

## Security review

**Verified** — passwords are bcrypt-hashed and salted; API keys are stored only
as salted hashes and shown once; integration secrets never reach the browser
(only a presence flag and a four-character hint); the audit log records
credential *names*, never values; request logging never stores request bodies;
CORS is an explicit origin list, not a wildcard; uploads are size-limited and
entity types are allowlisted; all database access goes through the ORM with no
string-built SQL; every route except login, health and the webhook endpoint
requires authentication; a viewer role cannot mutate; expired, tampered and
deleted-user tokens are all rejected.

**Accepted risks for a local-first MVP**

| Risk | Rationale |
| --- | --- |
| No rate limiting on ingestion | Trusted local callers; add a reverse-proxy limit before exposing it. |
| Webhook endpoints unauthenticated | Providers post from their own infrastructure. Mitigated: events are only recorded for a message ID the system already sent; anything else is counted and discarded. Add signature verification per provider before exposing publicly. |
| No CSRF tokens | The API is token-authenticated with no cookie session, so there is no ambient authority to forge. |
| Dev credentials in `.env.example` | Clearly labelled, and the README says to change them. `.env` is gitignored. |
| Single shared admin account | Role support exists (`ADMIN`/`MARKETER`/`VIEWER`) but there is no user management UI. |
| SQLite in production | Fine for one operator; move to PostgreSQL for concurrent writers. |

---

## Recommended next steps

**Before real customer data**

1. Change `SECRET_KEY` and `ADMIN_PASSWORD`; move both to a secret manager.
2. Add webhook signature verification per provider.
3. Add rate limiting to the ingestion endpoints.
4. Have someone qualified review [`docs/compliance.md`](docs/compliance.md)
   against your actual obligations. The rules are a reasonable reading of
   common requirements, not a legal opinion.
5. Move to PostgreSQL and generate the initial Alembic migration.

**Before a real send**

6. Configure and test one channel end to end — Outlook is the most likely
   first, and needs a delivery-event source wired in since Graph provides none.
7. Run a small campaign to an internal list before any customer send.
8. Confirm the audit log captures what your compliance process needs.

**Product**

9. Build the A/B testing UI — the schema and attribution already support it.
10. Add multi-touch attribution alongside last-touch.
11. Once enough labelled churn outcomes exist, fit a model and compare it
    against the current weights; keep the factor interface so explanations
    survive.
12. Code-split the frontend bundle.
