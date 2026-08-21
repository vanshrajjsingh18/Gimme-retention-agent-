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

- [x] Backend test suite (306 tests)
- [x] Frontend unit tests (35 tests)
- [x] Browser end-to-end tests (10 tests, failing on any console error)
- [x] Security review, codified as 29 tests
- [x] AST check proving all 99 routes carry an auth dependency
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
