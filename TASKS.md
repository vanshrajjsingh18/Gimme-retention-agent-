# Tasks

Nothing is ticked here that was not verified. Items left unticked are stated
with the reason.

## P0 — Required for a runnable MVP

- [x] Application starts reliably (`make backend`, `make frontend`)
- [x] Database persists data across restarts
- [x] Authentication works (JWT sessions, hashed API keys)
- [x] Customer and order ingestion (CSV upload and authenticated API)
- [x] Customer 360 profile
- [x] Behavioural metric computation
- [x] Lifecycle classification — all 9 stages proven reachable
- [x] RFM scoring
- [x] Churn scoring with per-factor attribution and explanations
- [x] Segmentation with a visual rule builder
- [x] Next best action
- [x] Brand settings
- [x] Mock LLM generation
- [x] Message validation rejecting ungrounded claims
- [x] Campaign creation
- [x] Consent and suppression enforcement
- [x] Compliance blocking
- [x] Campaign approval required before sending
- [x] Campaign copy is either written once with merge tags or drafted per
      recipient, chosen on the campaign and honoured by every send path

## Smart Reorder (personal order-time reminder)

- [x] Per-customer ordering routine: weekday, time of day to the minute
      (circular mean), median interval, 0–100 confidence per signal
- [x] Next-order prediction combining *when* they order with *how often*
- [x] Configurable reminder offset (15/30/60/120 min before, at, after, custom)
- [x] One timing engine shared by the scheduler and every screen
- [x] Minimum-confidence threshold for enrollment, counted in the dry run
- [x] Suppression when the customer has already ordered this cycle
- [x] Suppression when an order is in flight
- [x] Prediction stored on `customer_metrics` and refreshed with intelligence
- [x] Dynamic segments: Smart Reorder Eligible / Today / Next 24 Hours
- [x] Dashboard counts from stored predictions, before any campaign exists
- [x] Customers-likely-to-order-now view with routine, confidence and reminder
- [x] ORDERING PATTERN panel on Customer 360, in the customer's own timezone
- [x] Prediction outcomes recorded and scored (accuracy, median error, bands)
- [x] Dry run reporting who is excluded and why, including those who never
      became candidates
- [x] Idempotent scheduler — no duplicate reminder within the minimum gap
- [x] SAM-TEST-001 end-to-end scenario from the brief
- [ ] **A/B variants for reminder copy** — variants can be stored and
      attributed but not authored per Smart Reorder campaign
- [ ] **Per-customer channel fallback order surfaced in the builder** — the
      priority is applied, but not editable per campaign
- [x] Mock campaign sending
- [x] Event tracking
- [x] Reactivation detection
- [x] Attribution
- [x] Analytics computed from database data
- [x] End-to-end workflow (24-step automated test)

## P1 — Required product features

- [x] LLM provider abstraction supporting OpenAI-compatible endpoints
- [ ] **Real LLM provider exercised** — adapter written and unit-tested; no API
      key available in this environment to make a live call
- [x] Microsoft Outlook / Graph adapter implemented
- [ ] **Outlook live send exercised** — needs an Entra app registration with
      admin-consented `Mail.Send`
- [x] TNZ SMS adapter implemented
- [ ] **TNZ live send exercised** — needs a TNZ account with REST API access
- [x] WhatsApp adapter implemented (Meta Cloud, Twilio, 360dialog profiles)
- [ ] **WhatsApp live send exercised** — needs a WhatsApp Business account
- [x] Webhook endpoints with normalised events, idempotent and unit-tested
- [ ] **Webhooks exercised against a real provider** — no live provider available
- [x] Scheduled campaign dispatch implemented
- [ ] **Scheduled dispatch observed firing over a real interval**
- [x] Background jobs (intelligence refresh, dispatch, inbox ingestion)
- [x] Cohort analytics
- [x] A/B variant schema and attribution roll-up
- [ ] **A/B testing UI** — variants can be stored and attributed but not created
      from the interface

## P2 — Quality and reliability

- [x] Backend test suite (771 tests)
- [x] Frontend unit tests (65 tests)
- [x] Browser end-to-end tests (17 tests, failing on any console error)
- [x] Security review, codified as 29 tests
- [x] AST check proving all 129 routes carry an auth dependency
- [x] Fresh-install verification from an empty tree
- [x] Journey engine
- [x] Journey UI (builder and execution log)
- [x] Responsive layout verified at 390px and 1440px
- [x] Loading, empty, error and success states throughout
- [x] Documentation (README, architecture, API, compliance, integrations)
- [x] Docker Compose configuration, validated statically
- [ ] **Docker images built and run** — the `docker` CLI is present but no
      daemon is running in this environment

## P3 — Optional improvements

Not attempted; recorded so the gap is visible rather than implied.

- [ ] Frontend code-splitting (the bundle is 825KB / 230KB gzipped)
- [ ] Alembic migration for the initial schema (currently `create_all`)
- [ ] Segment rule evaluation pushed into SQL for very large customer bases
- [ ] Learned churn model to replace the hand-set factor weights
- [ ] Multi-user accounts and role management UI
- [ ] Rate limiting on the ingestion API
- [ ] Image moderation for message content
- [ ] Journey branching (currently a linear step list)
