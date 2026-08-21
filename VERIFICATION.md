# Verification

Status of every requirement, and how each was verified.

**Legend** — `VERIFIED` observed working through the running application;
`TESTED` covered by automated tests; `IMPLEMENTED` code exists but is not
exercised end to end; `BLOCKED` needs something unavailable.

Last full run: 306 backend tests, 35 frontend tests, 10 browser tests — all
passing from a clean install.

---

## Milestone 1 — Application foundation

| Requirement | Status | How verified |
| --- | --- | --- |
| Backend starts | VERIFIED | `make backend` → `GET /health` returns 200 |
| Frontend starts | VERIFIED | `make frontend` → 200 at `127.0.0.1:5173`; Playwright drives it |
| Database initialises | VERIFIED | `make init` creates 33 tables on a clean checkout |
| Authentication works | VERIFIED | Login through the UI; `tests/test_api_auth.py` (12 tests) |
| Seed command works | VERIFIED | `make seed` → 1,000 customers, 5,713 orders, in 23s |
| Docker Compose | IMPLEMENTED | `docker compose config` validates; every COPY path and referenced file checked to resolve. **Not built** — no Docker daemon in this environment |
| Runs without Docker | VERIFIED | The whole build and verification was done this way |

## Milestone 2 — Data foundation

| Requirement | Status | How verified |
| --- | --- | --- |
| Customer CSV upload | VERIFIED | UI upload; `test_api_ingestion.py`; e2e step 01 |
| Order CSV upload | TESTED | `test_csv_upload_imports_and_reports_partial_failures` |
| Order-item upload | TESTED | Same ingestion path, covered by `test_order_and_items_ingest_and_drive_metrics` |
| API ingestion | VERIFIED | Live `curl` with an API key; e2e step 02 |
| Validation | TESTED | 22 ingestion tests covering bad dates, missing columns, orphan orders, duplicates |
| Error handling | VERIFIED | Rejected rows reported per row with a downloadable CSV report |
| Data persists | TESTED | `test_24_data_persists_across_a_new_session` |
| Duplicate detection | TESTED | `test_duplicate_rows_within_a_file_are_reported` |
| Downloadable error report | VERIFIED | `GET /uploads/{id}/errors.csv` returns a populated CSV |
| Scheduled folder ingestion | IMPLEMENTED | `ingest_inbox_job` runs every 2 minutes; not exercised end to end |

## Milestone 3 — Customer intelligence

| Requirement | Status | How verified |
| --- | --- | --- |
| Customer 360 | VERIFIED | Browser test asserts computed metrics, factors and explanation render |
| Behavioural metrics | TESTED | 14 tests in `test_metrics.py` |
| Lifecycle classification | TESTED | 21 tests; `test_every_stage_reachable` proves all 9 stages occur |
| RFM | TESTED | 11 tests including small-population and flat-population fallbacks |
| Churn scoring | TESTED | 19 tests; monotonic with lateness; weights sum to 100 |
| Churn explanation | VERIFIED | Rendered per customer; asserted in the e2e and browser suites |
| Segmentation | TESTED | 23 rule tests; live preview verified in the browser |
| Next best action | TESTED | 29 tests; every lifecycle stage maps to an action |
| Search, filter, paginate | VERIFIED | Browser test filters to AT_RISK and asserts the list narrows |

## Milestone 4 — AI message system

| Requirement | Status | How verified |
| --- | --- | --- |
| Brand settings | VERIFIED | Edited through the UI; e2e step 04 |
| Mock LLM generation | VERIFIED | Generated messages reference real products and the real order gap |
| Real provider architecture | IMPLEMENTED | OpenAI-compatible adapter written; **no API key available to exercise it** |
| Grounding | TESTED | Prompt contains verified facts only; 45 tests in `test_llm_generation.py` |
| Message validation | VERIFIED | Invented coupon, discount, stock and delivery claims all blocked, in tests and in the browser |
| Editing | VERIFIED | Edit revalidates and clears approval |
| Approval | VERIFIED | Approval refused while validation fails (e2e step 06, browser test) |
| Prompt/model/version stored | TESTED | Asserted in `test_05_generate_a_grounded_message` |

## Milestone 5 — Campaign engine

| Requirement | Status | How verified |
| --- | --- | --- |
| Campaign creation | VERIFIED | Created through the UI and the API |
| Audience preview | VERIFIED | Live: 65 audience → 48 eligible, 12 no consent, 5 unverified age |
| Consent enforcement | TESTED | e2e step 12; `test_consent_withdrawn_after_approval_is_still_honoured` |
| Suppression enforcement | TESTED | e2e step 12 and 18; `test_suppression_after_approval_is_still_honoured` |
| Frequency caps | TESTED | 3 compliance tests, both windows |
| Quiet hours | TESTED | 4 tests including the midnight wrap |
| Compliance blocking | VERIFIED | Health claim and excessive-consumption copy blocked submission (e2e step 14) |
| Test send | VERIFIED | Returned a simulated provider message ID |
| Human approval required | TESTED | e2e step 13; `test_campaign_cannot_send_without_approval` |
| Mock sending | VERIFIED | 48 sent, 69 simulated events recorded |
| Event tracking | VERIFIED | Sends, deliveries, opens and clicks persisted and visible per customer |
| Scheduled campaigns | IMPLEMENTED | `dispatch_scheduled_campaigns_job` runs every minute; not exercised end to end |
| A/B variants | IMPLEMENTED | `campaign_variants` table and attribution roll-up exist; no UI |

## Milestone 6 — Closed retention loop

Verified twice: once by hand against the running stack, once as an automated
test (`tests/test_e2e_retention_loop.py`, 24 steps).

| Step | Status | Observed |
| --- | --- | --- |
| Campaign sent | VERIFIED | 48 recipients, mock mode |
| Interaction recorded | VERIFIED | 19 opens, 2 clicks persisted as communication events |
| New order ingested | VERIFIED | Posted through the authenticated API |
| Reactivation detected | VERIFIED | CUST-00775, dormant 193 days, flagged on the returning order |
| Lifecycle updated | VERIFIED | DORMANT → REACTIVATED, with the transition and reason stored |
| Churn recalculated | VERIFIED | 69.9 → 3.8, band CRITICAL → LOW |
| Conversion attributed | VERIFIED | $128.50 attributed to the campaign that touched them |
| Revenue updated | VERIFIED | Campaign attributed revenue and conversion count both rose |
| Analytics reflect it | VERIFIED | Overview reactivations and campaign revenue both moved |
| Attribution idempotent | TESTED | Re-ingesting the same order does not double-count |

## Milestone 7 — Final quality

| Requirement | Status | How verified |
| --- | --- | --- |
| Full backend tests | VERIFIED | 306 passing |
| Full frontend tests | VERIFIED | 35 passing |
| Browser end-to-end | VERIFIED | 10 passing, zero console errors, zero failed requests |
| Security review | VERIFIED | 29 tests; AST check proves all 99 routes are guarded |
| Documentation | VERIFIED | README, architecture, API, compliance, integrations |
| Clean startup | VERIFIED | Fresh install from an empty tree, following the README |
| Fresh database setup | VERIFIED | `make seed` on a clean checkout |

---

## Final acceptance workflow

All 45 steps of the specified acceptance test were executed. Grouped by area:

| Steps | Area | Status |
| --- | --- | --- |
| 1-5 | Clone, configure, start, log in, load data | VERIFIED |
| 6-14 | Overview, search, Customer 360, metrics, lifecycle, RFM, churn, explanation, NBA | VERIFIED |
| 15-19 | Brand config, generate, validate, edit, approve | VERIFIED |
| 20-21 | Create segment, preview matches | VERIFIED |
| 22-29 | Campaign, audience, channel, recipients, consent/suppression/cap exclusions, compliance | VERIFIED |
| 30-34 | Test send, approve, run in mock mode, store events, simulate engagement | VERIFIED |
| 35-41 | Import order, recalculate, detect reactivation, update lifecycle, attribute, show revenue, update analytics | VERIFIED |
| 42-43 | Restart, verify persistence | VERIFIED |
| 44 | Run the complete test suite | VERIFIED — 351 tests |
| 45 | Fresh-install verification | VERIFIED — found and fixed a real bug (see below) |

---

## Requirements not fully met

Stated plainly rather than marked green:

| Requirement | Status | Why |
| --- | --- | --- |
| Real LLM provider | BLOCKED | Adapter written and unit-tested; no API key available in this environment to exercise a live call |
| Microsoft Outlook live send | BLOCKED | Needs an Entra app registration with admin-consented `Mail.Send` |
| TNZ live send | BLOCKED | Needs a TNZ account with REST API access |
| WhatsApp live send | BLOCKED | Needs a WhatsApp Business account |
| Provider webhooks against a real provider | BLOCKED | Endpoints and normalisation implemented and unit-tested; no live provider to post to them |
| Docker Compose build | BLOCKED | The `docker` CLI is present but no daemon is running here, so the images were never built. Config validated statically and a `.dockerignore` added so host `.venv`/`node_modules` cannot leak into the images |
| A/B testing | IMPLEMENTED | Schema and attribution support variants; no UI to create them |
| Scheduled campaign dispatch | IMPLEMENTED | Job runs; not verified end to end over a real interval |
| Inbox folder ingestion | IMPLEMENTED | Job runs; not verified end to end |
| Journey execution over real time | IMPLEMENTED | Runs on demand and is unit-covered; multi-day delays not observed elapsing |

---

## Defects found by verification

The verification passes were worth running — each of these was a real bug
caught by a check rather than by reading code.

| Found by | Defect | Fix |
| --- | --- | --- |
| Unit tests | A customer 500 days past a 30-day cycle scored 43.6/100 (MEDIUM). Cadence saturated too early, and "0 orders vs 0 orders" read as flat rather than as total stoppage | Saturation moved to 6x, weights rebalanced, zero-activity handled directly |
| Unit tests | Delivery-claim check compared phrasing, so "we deliver in 60 minutes" failed against a promise of "delivered in 60 minutes" | Compare the duration, not the wording |
| Unit tests | Prohibited-claim patterns missed "happy birthday" and "we'll be there in 10 minutes" | Patterns widened |
| API testing | `.limit()` applied before `.where()` in 5 list endpoints, so a filtered page could come back empty while matches existed | Filters applied before the limit |
| API testing | `EmailStr` rejected reserved TLDs, which would drop real customers on import | Format checked per row in the service layer |
| Browser tests | No favicon, and a Google Fonts dependency that cannot load offline | Inlined favicon, system font stack |
| Browser tests | Missing React key on expandable order rows | `Fragment` with a key |
| Fresh install | `.env.example` shipped the Docker database path, so a local install wrote its database to `/app/data` outside the project | Path left unset so the repo-relative default applies |
| Security review | Mock webhook parser rejected the provider-style payload the docs tell users to post | Mock adapters accept both vocabularies |
| Seed review | Seeded SMS campaign excluded 100% of recipients — seed timestamps inherited the current wall-clock time, landing inside quiet hours | Historical campaigns send at a plausible mid-morning hour |
