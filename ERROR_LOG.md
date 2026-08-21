# Error Log

Only failures with a real root cause are recorded here.

---

## 2026-08-20 — Index name collision on first `create_all`

**Command:** `python -c "Base.metadata.create_all(engine)"`

**Failure:** `sqlite3.OperationalError: index ix_campaign_recipients_status already exists`

**Root cause:** `CampaignRecipient.status` declares `index=True`, which makes
SQLAlchemy auto-generate an index named `ix_campaign_recipients_status`. An
explicit composite `Index("ix_campaign_recipients_status", "campaign_id", "status")`
in `__table_args__` claimed the same name.

**Fix:** Renamed the composite index to
`ix_campaign_recipients_campaign_status`.

**Result:** All 33 tables create cleanly.

**Preventive action:** Never name an explicit index `ix_<table>_<column>` when
that column also sets `index=True`.

---

## 2026-08-20 — Churn score under-reported fully-lapsed customers

**Command:** `pytest tests/test_churn.py`

**Failure:** 4 tests failed. A customer 500 days past a 30-day cycle scored
43.6/100 (MEDIUM) instead of CRITICAL; risk did not rise monotonically with
lateness.

**Root cause:** Two independent defects.
1. `cadence_overdue` saturated at 3x the expected cycle, so 120 days late and
   500 days late both produced identical maximum severity, and its 34-point
   weight capped the reachable score.
2. `frequency_decline` and `spend_decline` were driven by the ratio-based trend
   value. For a customer with zero orders in *both* the recent and prior
   90-day windows, `_trend(0, 0)` returns 0.0 — "flat" — so a customer who had
   completely stopped buying contributed nothing from either factor.

**Fix:** Saturation moved to 6x; weights rebalanced (cadence 40, frequency 18,
spend 16, engagement 8, single_order 10, discount 4, order_problems 4 = 100);
zero activity in the recent window with prior purchase history now registers
severity 1.0 directly rather than going through the trend ratio. Also gated
`engagement_decline` on having actually sent messages, since a zero engagement
score for a never-messaged customer says nothing about them, and replaced the
`_prev_orders` trend-reversal hack with a real `orders_prev_90d` metric field.

**Result:** 19/19 churn tests pass; lateness now scores monotonically
(0 → 14.6 → 33.8 → 56.7 → 74 across 5/40/80/150/400 days late).

**Preventive action:** `test_risk_rises_monotonically_with_lateness` and
`test_factor_weights_sum_to_100` guard both defects.

---

## 2026-08-21 — Filtered list endpoints could return an empty page

**Command:** `GET /api/v1/campaigns/{id}/recipients?status=SENT`

**Failure:** Returned zero recipients for a campaign that had sent 48 messages.

**Root cause:** Two separate issues. The status was legitimately `DELIVERED`
rather than `SENT` after simulated delivery — not a bug. But inspecting the
query exposed a real one: `.limit()` was applied to the statement *before* the
`.where()` filter was appended. SQLAlchemy accepts this, but the intent is
wrong, and on a large table a filtered page can come back empty while matching
rows exist beyond the limit. The same pattern appeared in five endpoints.

**Fix:** Filters are now applied before the limit in
`campaigns.campaign_recipients`, `campaigns.list_campaigns`,
`messages.list_messages`, `system.audit_log` and `system.system_logs`.

**Preventive action:** Build the statement, then apply `order_by().limit()` as
the final call.

---

## 2026-08-21 — EmailStr rejected valid customer addresses

**Command:** `pytest tests/test_api_ingestion.py`

**Failure:** `POST /api/v1/customers` returned 422 for
`api-cust-1@example.test`: "The part after the @-sign is a special-use or
reserved name."

**Root cause:** `CustomerIn.email` used Pydantic's `EmailStr`, which rejects
reserved TLDs. This is right for a signup form and wrong for an ingestion API:
it would reject reserved test domains *and* unusual-but-valid corporate ones,
dropping real customers at import, and a single bad address failed the whole
batch rather than one row.

**Fix:** The field is a plain string; format is checked per row in
`services/ingestion.py` with a structural regex, so a malformed address is
reported as a rejected row and the rest of the batch imports.

**Preventive action:** `test_bad_email_rejects_the_row_not_the_batch` and
`test_structurally_invalid_payload_returns_422` pin both behaviours.

---

## 2026-08-21 — Seeded SMS campaign excluded 100% of its recipients

**Command:** `make seed`, then inspecting the SMS campaign's recipients.

**Failure:** 191 recipients, 0 sent — 80 excluded by quiet hours.

**Root cause:** Not a compliance bug; quiet hours worked correctly. The seed
computed historical send times as `now - timedelta(days=N)`, which preserves
the current time of day. A seed run after 21:00 backdated every campaign to a
time inside quiet hours, so every SMS and WhatsApp recipient was excluded.

**Fix:** Historical campaigns are seeded at a mid-morning hour, which is also
more realistic.

**Preventive action:** Any generated timestamp meant to be plausible must set
its time of day explicitly rather than inheriting the wall clock.

---

## 2026-08-21 — Frontend depended on a webfont that cannot load offline

**Command:** `npx playwright test`

**Failure:** Seven tests failed on `net::ERR_CONNECTION_RESET` and a 404. The
pages rendered, but the console-error assertion caught them.

**Root cause:** `index.html` linked Google Fonts, unreachable from the browser
in this environment, and no favicon was defined so every page load 404ed.
Both are real defects for a local-first product that must work with no
outbound access.

**Fix:** The font stack is system-only and the favicon is an inlined data URI.

**Preventive action:** The browser tests fail on any console error or failed
API request, so a re-introduced external dependency fails the build.

---

## 2026-08-21 — Missing React key on expandable order rows

**Command:** `npx playwright test`

**Failure:** "Each child in a list should have a unique key. Check the render
method of `OrdersTab`."

**Root cause:** Each order rendered a bare `<>` fragment wrapping its row and
its expanded detail row. The key was on the inner `<tr>`, not on the fragment,
so React saw an unkeyed list.

**Fix:** `<Fragment key={order.id}>` carries the key for the pair.

---

## 2026-08-21 — Fresh install wrote its database outside the project

**Command:** `cp .env.example .env && make seed`

**Failure:** `make seed` reported success, but `data/gimme.db` did not exist —
the database had been written to `/app/data/gimme.db`.

**Root cause:** `.env.example` hard-coded `sqlite:////app/data/gimme.db`, the
path used *inside the Docker container*. Copying it for a local install
pointed the database at an absolute path outside the repository, which would
also fail outright on a machine where `/` is not writable.

**Fix:** `DATABASE_URL` and `INBOX_DIR` are commented out in `.env.example`, so
the code's repo-relative defaults apply. Docker Compose sets the container
paths explicitly, which it already did.

**Preventive action:** Container-specific paths belong in the compose file, not
in the example env shared by both.

---

## 2026-08-21 — Mock webhook parser rejected the documented payload

**Command:** `pytest tests/test_security.py::test_webhook_ignores_events_for_unknown_messages`

**Failure:** Posting `{"event": "read", "message_id": "..."}` recorded nothing
and reported zero events — not even as ignored.

**Root cause:** The mock adapters' `process_webhook` accepted only our internal
event vocabulary (`WHATSAPP_READ`), while the live adapters accept
provider-style names (`read`, `delivered`). `docs/integrations.md` documents
the provider-style payload for local testing, so following the documentation
produced silence.

**Fix:** Mock adapters resolve both vocabularies, mapping provider status names
through the adapter's own channel.

**Preventive action:** A mock adapter must accept every payload shape its live
counterpart accepts, or it cannot be used to rehearse a real integration.

---

## 2026-08-21 — Docker builds would have copied host artefacts into the images

**Found by:** Reviewing the Dockerfiles after `docker compose config` passed.

**Failure:** Not observed — no Docker daemon is available here — but
`COPY backend/ ./` would have copied the host `.venv` (whose scripts carry
absolute host paths) and `COPY frontend/ ./` would have copied `node_modules`,
overwriting the platform-correct dependencies installed inside the image.

**Fix:** Added `.dockerignore` excluding virtualenvs, `node_modules`, build
output, the database and `.env`.

**Preventive action:** A `.dockerignore` belongs in the same commit as the
first Dockerfile, not after the first slow or broken build.
