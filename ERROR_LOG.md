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

---

## 2026-08-22 — Quiet hours were being checked against UTC, not New Zealand time

**Found by:** Reading `compliance/engine.py` before building the automation
send window, rather than by a failing test — nothing tested it, because every
test passed naive datetimes that were implicitly treated as local.

**Failure:** The database stores naive UTC throughout and `in_quiet_hours()`
compared that value directly against a wall-clock window. New Zealand is UTC+12
or UTC+13, so the check was wrong by half a day in the worst possible
direction: 09:00 UTC is 21:00 in Auckland and passed as "daytime", while 02:00
UTC is a perfectly reasonable 14:00 and was blocked as quiet hours. Any
automation built on this would have texted customers late at night while
refusing to send in the afternoon.

**Fix:** Added `app/core/timezones.py` as the single place anything reasons
about the customer's clock, made `in_quiet_hours()` convert through it, and
corrected the default window to 19:00–09:00 local (the complement of a
9am–7pm send window; it had been 21:00). Exclusion messages now quote the
customer's local time, so the reason an operator reads matches the clock the
customer was looking at.

**Preventive action:** The four existing quiet-hours tests were rewritten to
say which clock they mean: pure window logic runs with
`use_business_timezone=False`, and a new test asserts the conversion itself
(02:00 UTC allowed, 09:00 UTC blocked with "21:00 local" in the reason). A
test that passes a bare `datetime` to a time-of-day rule is not testing what it
appears to.

---

## 2026-08-22 — STOP suppressed one channel, not the customer

**Found by:** Tracing what a `CUSTOMER_OPTED_OUT` event actually did before
wiring automations into it.

**Failure:** `_apply_opt_out()` cleared the consent flag for the channel the
opt-out arrived on and wrote a per-channel suppression row. A customer who
replied STOP to an SMS therefore kept receiving email, stayed enrolled in every
sequence, and would have been re-enabled entirely by any data import that
restored a consent flag. For alcohol marketing that is not a rough edge, it is
a compliance failure.

**Fix:** `app/services/optout.py` now clears all four consent flags, writes a
`ConsentEvent` per type, sets `is_suppressed`, writes an **ALL**-channel
suppression record, stops every active automation enrollment, and writes an
audit entry. Eligibility reads the suppression row as well as the flags, so
restoring a flag alone cannot silently re-enable messaging. The campaign engine
now routes through the same function.

**Preventive action:** A test asserts that opting out of one automation stops
an unrelated one. Keyword matching is deliberately narrow — the keyword must be
the entire message — so "I couldn't stop drinking that IPA" is not an opt-out,
which is also tested.

---

## 2026-08-22 — TNZ webhook could not see a STOP reply at all

**Found by:** Working backwards from "what makes the opt-out fire?" after
building the opt-out service.

**Failure:** `TnzSmsAdapter.process_webhook()` only read delivery *status*
fields. An inbound reply carries no status, just the text the customer sent, so
every STOP arriving as a reply was dropped on the floor. The webhook endpoint
also ignored any event whose `provider_message_id` did not match a message we
had stored — meaning an opt-out could be lost because the provider echoed an id
back differently.

**Fix:** The adapter now reads reply bodies and classifies them as opt-out or
opt-in. The webhook resolves the customer by message id *or* by the address the
reply came from (matching on the last nine digits, since NZ numbers arrive as
`+64…`, `0064…` or `021…`), and applies the consent change even when no message
matches. Delivery receipts now also advance the automation ledger, and progress
is one-way so a late "sent" event cannot walk a delivery backwards.

**Preventive action:** Withdrawal of consent must never depend on an external
system getting our own identifier right on the way back.

---

## 2026-08-22 — Adding a model field broke every existing database at startup

**Found by:** Starting the server after adding two columns to `BrandSettings` —
the API had passed its whole test suite, because tests build the schema fresh.

**Failure:** `Base.metadata.create_all()` creates missing *tables* but never
missing *columns*. The new automation tables appeared; the new brand columns did
not, and the app died on `no such column: brand_settings.signatory_name`. For a
local-first tool the affected case is anyone with data they have accumulated —
i.e. the normal case, not an edge case.

**Fix:** Added `app/core/schema.py`. It compares model metadata against the live
schema and adds what is missing, additively only: it never drops, renames or
retypes, backfills Python-side defaults so existing rows are correct, and
**refuses** a NOT NULL column with no default rather than half-applying,
logging that it needs a real migration. Alembic stays in `requirements.txt` for
everything beyond adding a column.

**Preventive action:** Seven tests build a database from an older schema with a
row already in it and assert the row survives, the defaults backfill, a second
run is a no-op, and an unsafe column is refused with the table left untouched.
A green test suite says nothing about upgrade behaviour when the tests always
start from an empty database.

---

## 2026-08-22 — Behavioural nudges were being deferred past the moment they aimed at

**Found by:** Running the nudge enrollment against the real seeded data and
reading the resulting schedule, rather than trusting that the shared
quiet-hours rule was right for every campaign type.

**Failure:** Feature 2 times a message to when a customer usually orders, and
drinks orders skew heavily to the evening — the seeded data peaks at 7pm. The
generic rule deferred anything outside 09:00–19:00 *forward*, so a customer who
reliably orders at 9pm on Saturday was scheduled for 9am Sunday: after they
would already have bought, which is precisely the message the feature exists to
avoid sending. The stored `next_due_at` was also untruthful, showing 21:00 for a
send that would actually go at 09:00 the next day.

**Fix:** Nudges clamp *backwards* into the customer's own local day — 21:00
becomes 18:00, and an overnight pattern moves to 18:00 the evening before, with
a guard that rolls to the next weekly occurrence if that lands in the past.
Other sends still defer forward, which remains right for them: a bulk send that
is a few hours late is fine, a nudge that is a day late is pointless.

**Preventive action:** A test enrolls customers whose patterns span 02:00 to
23:00 and asserts every resulting `next_due_at` falls inside business hours and
in the future. Sharing a pipeline is only correct where the shared behaviour is
actually right for every case — this one needed a documented exception, not a
uniform rule.

---

## 2026-08-22 — Mock SMS adapter crashed on a message with no subject

**Found by:** The first automation send test.

**Failure:** `BaseMockAdapter.send_message()` did `subject[:80]` when building
its simulated response. Every existing caller passed a string, because the
campaign engine always supplies a subject even for SMS. The automation runtime
passes `None`, which is what an SMS actually has, and the adapter raised
`TypeError: 'NoneType' object is not subscriptable` — 28 tests failing from one
line.

**Fix:** `(subject or "")[:80]`.

**Preventive action:** A mock has to accept everything its live counterpart
accepts. `subject` was already `str | None` in the interface; only the mock had
quietly assumed otherwise.

---

## 2026-08-22 — The send ledger's idempotency key could not record two outcomes on one day

**Found by:** A test asserting that one automation cannot double-send to the
same customer within a single batch.

**Failure:** `automation_sends.idempotency_key` was
`(automation, step, customer, local_date)` — correct as a replay guard for a
*send*, but it also had to cover *skips*. The second candidate for the same
customer was correctly skipped as `DEDUPED`, then failed to write its ledger row
on a UNIQUE violation, taking down the whole run. The safety mechanism was
destroying the audit trail that proves the safety mechanism worked.

**Fix:** A skip's reason is part of its key, so one customer can carry both a
send and a `DEDUPED` skip for the same day. The write is wrapped in a savepoint
that treats a collision as "already recorded" and returns the existing row, so a
crash mid-batch or two workers racing still cannot produce a duplicate message.

**Preventive action:** An idempotency key encodes what may happen at most once.
Sends and skips are different things and needed different keys.

---

## 2026-08-22 — End-to-end order timestamps depended on the wall clock

**Found by:** The full suite failing on a re-run, having passed an hour
earlier with no relevant change in between.

**Failure:** `test_e2e_retention_loop.py` built orders with
`(NOW - timedelta(days=160)).replace(hour=18)`. Pinning the hour without
adjusting the date means only 159 whole days have elapsed if the suite runs
before 18:00 UTC, so `days_since_last_order >= 160` failed — and took a second
test with it, which read state the first one was supposed to store. Confirmed
pre-existing by stashing the branch's changes and reproducing it on the original
code.

**Fix:** `iso()` steps back one further day when the requested hour has not yet
passed today, making the elapsed-day count exact whenever the suite runs.

**Preventive action:** A test whose result depends on the time of day it runs
will eventually fail for a reason unrelated to the change in front of you, and
send you looking in the wrong place.

---

## 2026-08-22 — A behavioural nudge could never be previewed before approval

**Found by:** Screenshotting the automation detail page after a dry run, rather
than reading the JSON the endpoint returned.

**Failure:** The page said "Nobody matches right now — 0 would receive" for a
nudge whose segment held 249 customers. Enrollment only happens on a live run,
and the nudge preview read the enrollment table, so a nudge nobody had joined
yet previewed as empty. Since a nudge cannot be approved without being
previewed and cannot be previewed meaningfully without being approved, the
safest campaign type had the least visible one. Feature 1 already solved this
with in-memory prospective enrollment; Feature 2 had simply not been given the
same treatment.

**Fix:** A nudge dry run now simulates enrollment in memory — computing each
customer's real order pattern and due time without writing anything — and
ignores the "is it due right now" filter, because a preview should show the
whole standing audience rather than the handful whose slot falls in this
minute. A live run still sends only what is due. The seeded nudge now previews
249 candidates: 91 would receive, 158 excluded with reasons.

**Preventive action:** Four tests cover it, including that previewing enrolls
nobody and that a customer with too few orders is absent from the preview
exactly as they would be from a live run — the preview count has to match what
running it would actually do.

---

## 2026-08-22 — Times labelled "NZ" were rendered in the viewer's timezone

**Found by:** The same screenshot. The row read "23 Aug 2026, 06:00 am NZ"
while the API had scheduled that send for 18:00 NZ.

**Failure:** `formatDateTime()` does `new Date(value).toLocaleString('en-NZ')`,
which picks the locale's *formatting* but the *browser's* timezone. Given an
already-local `2026-08-23T18:00:00+12:00`, a browser running in UTC rendered
06:00 and the UI labelled it "NZ". Every send window, quiet-hours and per-day
decision in this system is made in New Zealand time, so a screen that says NZ
and shows something else is worse than showing nothing.

**Fix:** Added `formatBusinessTime()`, which passes
`timeZone: 'Pacific/Auckland'` explicitly, and used it for every automation
timestamp — scheduled sends, next run, and per-customer nudge due times. It is
now correct whether the operator is in Auckland, in London, or a CI container
running in UTC.

**Preventive action:** Five tests, including one asserting that the same
instant written three ways (`+12:00`, `Z`, `-04:00`) renders identically.
`toLocaleString('en-NZ')` selects a language, not a place; the timezone has to
be named separately or it silently follows the machine.

---

## 2026-08-22 — Automation plumbing filled up the campaigns list

**Found by:** Querying `/api/v1/campaigns` while checking something else, and
noticing 20 of the 26 rows were named "… (automation)".

**Failure:** Every automation carries a backing `Campaign` so its sends flow
through the existing attribution and analytics rather than a parallel reporting
world. That decision is right, but those campaigns were also being listed on
the Campaigns screen — showing an operator a pile of drafts they never created,
cannot meaningfully edit, and whose status reads DRAFT forever because they are
never sent through the campaign engine.

**Fix:** The campaigns list now excludes any campaign referenced by
`automations.campaign_id`, with `include_automations=true` to see them.
Derived from the automation table rather than a flag on the campaign, so the
two cannot drift apart — a flag would need maintaining in two places and would
be wrong the first time somebody forgot.

**Preventive action:** Two tests: a backing campaign is absent from the default
listing and present with the flag, and a campaign somebody actually created is
still listed — hiding plumbing must not hide real work.

---

## 2026-08-22 — A frequency-cap test asserted the mock provider's mood

**Found by:** The test passing alone and failing as part of its class.

**Failure:** A new test asserted `report.sent == 1` after the 7-day window had
cleared. The mock adapter deliberately fails a deterministic share of
recipients so the failure path is exercised in every demo run, and this
customer's phone number happened to draw one. The test was asserting that the
simulated provider cooperated, not that the cap had lifted.

**Fix:** Assert the actual property — that nothing was *skipped* — rather than
that the send succeeded. Writing it correctly then exposed a rule worth pinning
down: only a successful send counts toward the cap, because a message the
provider rejected never reached the customer and should not consume their
allowance. That now has its own tests, including that a failed send is still
recorded in the ledger — not counting is not the same as not happening.

**Preventive action:** When a test involves a deliberately non-deterministic
collaborator, assert the property under test, not the collaborator's output.

---

## 2026-09-05 — The dashboard said nobody had returned, next to a count of 24 who had

**Found by:** Screenshotting the Overview page for a demo and reading the
Retention health panel.

**Failure:** Two adjacent rows read "Reactivation rate 0.0% — 0 customers
returned after a lapse" and "Currently reactivated 24 — Returned within the
last 30 days". They measure genuinely different things: the first counts
`AttributionRecord.is_reactivation`, which is a return we can *attribute to a
campaign*, while the second counts customers currently in the REACTIVATED
lifecycle stage. But the first hint states a plain falsehood — 24 customers did
return after a lapse; what was zero is the number we could credit to a
campaign. A dashboard contradicting itself is worse than one omitting the
metric, because a reader has to decide which half to disbelieve.

**Fix:** Renamed to "Campaign-driven win-backs", with the hint naming the
denominator it is actually a rate of ("0 of 199 lapsed customers returned via a
campaign"), and the neighbouring row to "Reactivated customers — came back
after lapsing, however they found us". Same relabelling on the churn page's
"Reactivations" tile.

**Preventive action:** Two metrics sharing a word need labels that say which
one they are, especially when they sit in the same panel. The number was right
in both places; only the English was wrong, which is exactly the kind of defect
a test suite will never catch and a screenshot catches immediately.

---

## 2026-09-05 — The schema reconciler crashed on a JSON column instead of refusing

**Found by:** Adding `message_variants: Mapped[list] = mapped_column(JSON,
nullable=False, default=list)` and starting the app.

**Failure:** `sqlite3.OperationalError: Cannot add a NOT NULL column with
default value NULL`. The reconciler's safety guard checked
`column.default is None` before adding a NOT NULL column — but `default=list`
is a *callable* default. It exists, so the guard passed it as safe, and then
the generated ALTER carried no DEFAULT clause because a Python callable cannot
be written into DDL. So the guard let through exactly the case it was meant to
catch, and the tool I wrote to make schema changes painless crashed on the
first schema change after writing it.

**Fix:** The guard now asks whether a *literal* can be derived, not whether a
default exists. Callable defaults are evaluated — `list` → `[]`, `dict` → `{}`
— and serialised as JSON, which is what makes JSON columns addable at all;
anything that cannot be evaluated is refused and reported as needing a real
migration, as before.

**Preventive action:** A test adds two JSON columns with callable defaults to a
table containing a row and asserts both land with `[]` and `{}` backfilled.
"Has a default" and "has a default the DDL can express" are different
questions, and only the second one matters here.

---

## 2026-09-05 — Skipping back-dated sequence steps was too aggressive by a day

**Found by:** A test written from the spec — enrol somebody who signed up eight
days ago into a Day 0 / 7 / 14 sequence and expect the Day 7 message.

**Failure:** Nothing was sent. The rule was "skip any step whose due date falls
before the customer joined", which is right for a Day 0 welcome to somebody who
signed up three months ago, but also silently swallowed a Day 7 message whose
moment passed *yesterday*. A step one day stale is still worth sending; the
rule made no distinction between one day and one year.

**Fix:** A `catch_up_days` grace window, default 3. Steps that came due within
it are sent; older ones are skipped and the customer resumes at the first step
still worth sending. Signed up eight days ago → Day 7 arrives; signed up thirty
days ago → nothing, and the enrollment completes.

**Preventive action:** Both cases are tested by name. The first version of the
rule was defensible in isolation and wrong at the boundary, which is the usual
shape of a rule written without a concrete example either side of it.

---

## 2026-09-05 — Deleting an automation resurrected its plumbing as a campaign

**Found by:** Clearing three throwaway automations out of the dev database, then
looking at the campaign list.

**Failure:** Three campaigns nobody had written — `E2E cohort … (automation)`
and friends — were sitting in the operator's campaign list. Every automation
creates a backing campaign so its sends flow through the existing attribution,
and the list hid those by asking which campaigns were referenced by
`automations.campaign_id`. Deriving the fact rather than storing a flag was a
deliberate choice: a flag can drift, a join cannot. But deleting the automation
deletes the reference, so the derivation lost its source and the plumbing
surfaced as a draft campaign — with a name ending in "(automation)" and no way
for an operator to make sense of it.

**Fix:** Two parts. A write-once `campaigns.is_automation_backing`, set when the
automation creates the campaign and never cleared, keeps it hidden after the
automation is gone; the join still covers live automations, so the two cannot
disagree. And on delete, a backing campaign that never sent anything is deleted
with the automation — there is no record in it to preserve. One that *did* send
survives, because deleting it would orphan the attribution for messages that
really went out.

**Preventive action:** Two tests, one per branch: a backing campaign is absent
from the list before and after its automation is deleted, and one that sent is
kept but still hidden. The original test only checked the live case, which is
the half that worked.

---

## 2026-09-05 — Every timestamp would have been 12 hours wrong in Auckland

**Found by:** Reading an enrollment table that said a customer joined at
`03:29 am` on a page whose send window is labelled "09:00–19:00 NZ time".

**Failure:** The API returns naive UTC (`2026-09-05T03:29:52`, no `Z`), and
`new Date()` reads a string with no zone as the *viewer's* local time. In this
UTC container that coincidence produces the right instant, so everything looked
correct. On an operator's machine in Auckland the same string would be read as
NZ time — 12 or 13 hours off the actual moment — and `formatBusinessTime`,
written specifically to stop timezone confusion, would have converted from the
wrong instant and confidently displayed a wrong NZ time.

The tests already asserted the right thing ("06:00 UTC on 23 June is 6pm in
Auckland"). They passed because the test runner was also UTC, where the bug is
invisible. A test that cannot fail is not coverage.

**Fix:** A single `parseTimestamp()` that appends `Z` to a zone-less timestamp
and leaves anything with an explicit offset alone; all five formatters go
through it. The enrollment column also moved from viewer-local `formatDateTime`
to `formatBusinessTime`, and its header now says "Enrolled (NZ)".

**Preventive action:** Vitest now runs under `TZ=America/New_York` — neither UTC
nor New Zealand. Reverting the parse makes the two existing assertions fail,
which was verified rather than assumed. The lesson is that the environment a
test runs in is part of the test: these ones had been passing for the wrong
reason since they were written.
