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
- [x] Individual scheduled message per customer, rendered and stored ahead
      of the send, in `scheduled_messages`
- [x] Upcoming Smart Reorder Messages queue with open / edit / reschedule /
      cancel per customer
- [x] Smart Reorder Reminder as a type in Campaigns → Create Campaign
- [x] Dry run over the whole audience before activation, writing nothing
- [x] Batch cap on the first build, so activation does not materialise
      thousands of messages at once
- [x] Idempotent dispatch — SCHEDULED → PROCESSING claim, one send per message
- [x] Final already-ordered check at send time, anchored to the order the
      prediction was built from
- [x] Per-message conversion with signed prediction error
- [x] Re-plan on every order: obsolete reminder cancelled, next one written
- [x] Message statuses distinct from campaign statuses; TESTING and ARCHIVED
      added to campaigns
- [x] Grounding findings confirmable by a named reviewer, recorded in the
      audit log, with prohibited claims still hard-blocking
- [ ] **Approved promotions and coupon codes maintained in Brand settings**
      so the engine can verify them itself rather than asking a reviewer
- [ ] **A/B variants for reminder copy** — variants can be stored and
      attributed but not authored per Smart Reorder campaign
- [ ] **Per-customer channel fallback order surfaced in the builder** — the
      priority is applied, but not editable per campaign

## Personalisation (merge tags)

- [x] One whitelist of fields, resolved by deterministic application code
- [x] `#field_name#` in campaign and automation copy, filled per recipient
- [x] Personalize menu inserting at the cursor, in both composers
- [x] Live preview against a chosen customer or a sample one
- [x] Fallbacks for missing values; no invented figures
- [x] Unknown tags block campaign approval, sending, and automation activation
- [x] `GET /api/v1/message-fields`, `POST /api/v1/message-fields/preview`
- [x] Per-send audit: template, resolved text, missing fields, fallbacks used
- [x] Works with Smart Reorder, through the same resolver
- [x] Excel/CSV column spellings mapped to canonical fields on import
- [x] A gap no fallback can close is skipped rather than sent broken
- [x] Copy preview works outside the send window
- [x] Every tag names the upload column it reads; column headings work as tags
- [x] Every upload column has a tag or a recorded reason for not having one
- [x] `IMPORT_TIMESTAMPS_ARE_LOCAL` for exports that record local order times
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

## Exports

- [x] Segment export includes a `phone` column in NZ international form
- [x] Reuses the canonical `normalize_nz_phone`; no second implementation
- [x] Blank rather than invented for unresolvable numbers; stored value untouched
- [ ] **An .xlsx export** so a `+`-prefixed cell is explicitly typed as text
      rather than relying on how a spreadsheet guesses (needs `openpyxl`)

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
