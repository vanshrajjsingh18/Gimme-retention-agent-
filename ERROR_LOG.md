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

---

## 2026-09-05 — SMS could go out with no way to opt out

**Found by:** Implementing `use_llm`, then reading the first drafted messages
side by side with the templates they replaced.

**Failure:** The drafts said nothing about STOP. Every default template ends
with "Reply STOP to opt out.", so every SMS this system had ever produced
carried one — but only because the templates happened to say so. Nothing
enforced it. `check_content()` had no such rule, and the comment beside the
mandatory-statement block ("SMS has no room and is exempt") was about the
responsible-drinking statement, not the unsubscribe facility.

That is a legal requirement under the Unsolicited Electronic Messages Act 2007,
and it is also load-bearing for this product specifically: `optout.py` acts on
the keyword STOP, which is useless to a recipient who was never told it. The
convention held for exactly as long as every message came from a template, and
drafting is what broke that assumption.

**Fix:** `MISSING_SMS_OPT_OUT`, a blocking content rule on the SMS channel. It
is loose about wording — "Reply STOP", "Text STOP to unsubscribe", "Unsubscribe
any time" all satisfy it — because the requirement is that the recipient was
told, not that one particular sentence appears. `require_sms_opt_out` turns it
off for genuinely non-commercial messages such as a delivery notification.

Adding the rule immediately caught a second instance the same defect had been
hiding: the **mock LLM provider** generated SMS without an opt-out too, and
`test_mock_output_passes_validation_for_every_channel` had been asserting that
output was valid. The fix went into the provider and the SMS prompt, not the
rule — the test had been passing on a wrong premise.

**Preventive action:** Five tests: the rule blocks, an email is not asked for
SMS wording, four different phrasings all satisfy it, and the switch works. Two
test fixtures using placeholder bodies (`"First."`, `"Second."`) had to grow an
opt-out, which is the rule doing its job on the first day.

---

## 2026-09-05 — A UNIQUE violation from cleaning up a database by hand

**Found by:** Creating a sequence in the browser after clearing test data;
`UNIQUE constraint failed: automation_steps.automation_id, automation_steps.position`
came back as a bare 500, and the UI reported it as "could not reach the API".

**Failure:** Nothing to do with the product. Earlier in the session I had
cleared leftover test automations with a raw `sqlite3.connect()` and a
`DELETE FROM automations`. The application enables `PRAGMA foreign_keys=ON` on
every connection it opens, but my throwaway connection did not, so the delete
left `automation_steps` rows behind pointing at automations that no longer
existed. SQLite then reused the freed id, and the new automation's steps
collided with the orphans at positions 0 and 1.

**Fix:** Removed the orphaned rows through the ORM. No product change: the
cascade works correctly on every path the application actually uses, which is
the reason the tests never saw this.

**Preventive action:** Clean up through the ORM or the API, not raw SQL. The
misleading part was the diagnosis rather than the bug — the UI's "could not
reach the API" is what it says for any failed request, so a 500 from a corrupt
database reads identically to a backend that is not running. The first two
things I checked were both wrong because of it, and the traceback in the server
log named the real cause immediately.

---

## 2026-09-08 — The webhook could be used to restore consent somebody withdrew

**Found by:** Working out what actually stands between the current build and a
live TNZ integration, and reading the webhook endpoint's own justification for
being unauthenticated.

**Failure:** The endpoint's docstring said it was safe because it "only records
events for messages it already knows about". That is true of delivery receipts,
and false of the path that matters. An inbound *reply* is deliberately resolved
by phone number rather than by a message id we issued — so that an opt-out is
honoured even when a provider fails to echo our id back. With no authentication,
that same path was reachable by anyone.

Demonstrated against the live adapter before fixing anything: an unauthenticated
`POST {"Recipient": "+64…", "Reply": "STOP"}` suppressed a consenting customer,
and `"Reply": "START"` then cleared the suppression and turned marketing consent
back on. The second is the serious one. Suppressing somebody is at least
fail-safe; forging START silently resurrects consent a customer withdrew, and
this system would go on texting them.

It was invisible in mock mode, because the mock adapter does not parse TNZ's
payload shape — so it only becomes reachable at exactly the moment the
integration goes live and the URL is handed to a third party.

**Fix:** A shared `webhook_secret`, presented as an `X-Webhook-Secret` header or
a `?secret=` query parameter (some providers only let you configure a URL), and
compared with `secrets.compare_digest`. A **live** integration with no secret
configured refuses webhooks rather than trusting them — fail closed, because a
live integration without one is a publicly writable consent endpoint. Mock mode
stays open so local development needs no credentials.

**Preventive action:** Six tests, two of which are the attack itself rather than
the fix: the forged STOP and the forged START are asserted to change nothing.
The route is also still in the `INTENTIONALLY_PUBLIC` list in the route-auth
test, with its justification rewritten to say what actually makes it safe.

---

## 2026-09-08 — Two different answers to "when may we text somebody?"

**Found by:** The go-live readiness panel printing "09:00–21:00", one screen
away from an automations page printing "09:00–19:00 NZ time".

**Failure:** The send window was defined twice. `SEND_WINDOW_START/END` (09:00
to 19:00) is what the automation runtime defers candidates against; the
`QUIET_HOURS` compliance rule (21:00 to 09:00) is what `check_recipient` blocks
against. Automations obeyed both, so the tighter one won and nothing looked
wrong. Campaigns only go through `check_recipient` — so a campaign could text
somebody at 20:30 that an automation would have held until morning, while the
UI promised one window to both.

**Fix:** The seeded rule and its fallback now derive from `SEND_WINDOW`, so a
fresh install has one window. A *stored* rule is left alone — it is an
operator's configuration, and silently rewriting somebody's compliance settings
to match a constant would be worse than the inconsistency. Instead readiness
reports the mismatch, names both numbers, and says which knob to turn.

**Preventive action:** A test asserts quiet hours are the exact complement of
the send window on a fresh install, and another asserts the readiness check
notices when a stored rule disagrees. The general lesson is that the same
quantity defined in two places does not announce the disagreement — it only
shows up where the two paths differ, which here was the path with no test
comparing them.

---

## 2026-09-09 — Rejecting a whole customer over a landline

**Found by:** Re-reading yesterday's phone-normalisation change with a real
upload in mind.

**Failure:** Yesterday I made `ingest_customers` reject any row whose phone
number could not be resolved to an NZ mobile. The import's own rule is that a
customer needs an email address *or* a phone number — so a customer with a
perfectly good email and a landline, which is an ordinary thing to find in an
exported customer list, had their entire record thrown away. The change was
right about the number and wrong about the person.

**Fix:** An unusable number is dropped and the customer is kept, provided they
have an email. Only a row with no email *and* no usable number is rejected,
because that is genuinely nobody we can contact. The dropped number is reported
as a warning rather than absorbed silently: quietly discarding somebody's data
is worse than refusing it, and "why does this customer never get texted?" needs
an answer.

**Preventive action:** Two tests, one per branch — a landline plus an email
imports with `phone` cleared, and a landline with no email is rejected. The
general shape of the mistake is a validator written for one field deciding the
fate of the whole record.

---

## 2026-09-09 — A preview that would have imported the file

**Found by:** The test written for it, before the code was believed.

**Failure:** Making the CSV preview a real dry run meant running the actual
ingestor and discarding its writes. The first implementation ran it in a
session and rolled back afterwards. Every ingestor calls `db.commit()` when it
finishes — they are written to be called for real — so the rollback had nothing
left to undo. Clicking *Preview* would have imported the file.

The second attempt bound the session to a held-open connection with
`join_transaction_mode="create_savepoint"`, on the understanding that inner
commits would release savepoints inside an outer transaction that could still
be discarded. Measured rather than assumed, and it was wrong: with pysqlite the
write reached the database anyway. A count before and after said 1000 → 1001.

**Fix:** Remove the commit instead of trying to contain it. The dry run uses a
`Session` subclass whose `commit` flushes — the ingestor still sees its own
writes, so duplicate detection inside the file still works — and the caller
rolls back with `Session.rollback`. There is no code path in it that can
persist, so an ingestor that grows a new commit later is still contained.

**Preventive action:** A backend test asserts the customer count is unchanged
and that a previewed `external_id` is absent afterwards, and an end-to-end test
previews a file and then searches for one of its rows. Both were checked
against deliberately broken containment — restoring the real `commit` makes
them fail — so they are not passing by construction.

**Also worth recording:** the same full e2e run quietly imported that file into
the development database, because a throwaway screenshot spec I had left in
`e2e/` performed a real import and `playwright test` runs everything in the
directory. Removed. Temporary specs in a watched directory are not temporary.

---

## 2026-09-12 — You had to go live in order to enter the credentials

**Found by:** Opening the TNZ card to enter API keys, and finding no fields.

**Failure:** The credentials section in the integration dialog was wrapped in
`{mode === 'live' && …}`. So the only way to reach the auth token and sender
fields was to first switch the integration to Live — declaring yourself ready
in order to find out whether you are. The readiness panel compounded it by
saying "Add them under this integration's settings", which pointed at fields
that were not rendered.

**Fix:** The credentials are always shown. Saving them while in Mock changes
nothing about sending, which the dialog now says, so an operator can enter
keys, test, and switch over in that order rather than the reverse.

**Preventive action:** An end-to-end test opens the dialog in Mock and asserts
the fields are present.

---

## 2026-09-12 — Testing the mock counted as testing TNZ

**Found by:** Clicking *Test connection* while in mock mode and watching the
go-live checklist move "Connection test" into the Passing list.

**Failure:** The mock adapter's `validate_credentials` returns `status="OK"`,
which is right for what it is — it has nothing to fail against. The endpoint
stored that verbatim, and the readiness check asks whether the stored status is
`OK`. So a test of the mock satisfied the go-live check for a *provider*
connection, and the panel would say a blocker had cleared having never once
spoken to TNZ. That is the specific failure this panel exists to prevent: false
assurance is worse than no assurance.

**Fix:** The stored status now says what was actually tested — `MOCK` when the
adapter was a mock, the real result otherwise. The readiness check treats MOCK
as an unmet blocker and says why, rather than silently failing a string
comparison. A failed live test now also reports what the provider said instead
of "Last result: ERROR", and the remedy no longer tells you to run the test you
just ran.

**Preventive action:** A test asserts a mock connection test leaves the
connection check failing, and an end-to-end test asserts the same through the
dialog. The general shape: a stand-in that always succeeds will satisfy any
check that only looks at whether something succeeded.

---

## 2026-09-12 — The analytics pages were broken on PostgreSQL

**Found by:** Making the test suite able to run against PostgreSQL, then
running it, because a Railway deployment uses Postgres and "it passes on
SQLite" is not evidence about an engine it never touched.

**Failure:** `UndefinedFunction: function strftime(unknown, timestamp without
time zone) does not exist`. The analytics month-grouping helper called
`strftime()`, which only SQLite has. Every query through it — customer growth,
new-vs-repeat, the whole Customer analytics page — would have raised a 500 on
a deployed host.

The helper's own docstring read *"Portable YYYY-MM extraction (SQLite
strftime, PostgreSQL to_char)"*. The portability was described and never
implemented; the body only ever called `strftime`. A comment asserting a
property is not the property, and on SQLite nothing ever contradicted it.

**Fix:** A real dialect-aware expression via SQLAlchemy's `@compiles` —
`strftime` on SQLite, `to_char` on PostgreSQL, chosen by whichever dialect is
connected.

**Preventive action:** `TEST_DATABASE_URL` now points the whole suite at any
database, and all 579 tests pass against PostgreSQL 16 as well as SQLite. Plus
a unit test compiling the expression for both dialects and asserting
`strftime` does not appear in the PostgreSQL output.

The wider lesson: the test suite ran on an engine the product would never be
deployed on, so an entire class of defect was structurally invisible. Nothing
about the code looked wrong — the bug lived in the gap between the two.

---

## 2026-09-12 — Serving the dashboard swallowed the API

**Found by:** Checking the routes immediately after adding static file serving,
rather than assuming a catch-all route behaves.

**Failure:** Serving the built dashboard from the API needs a catch-all on
`/{path}` so client-side routes like `/customers/42` resolve to `index.html`.
Registered naively it also caught `/api/...`: a `GET` on a `POST`-only endpoint
returned **`200` with an HTML page** instead of an error, as did any mistyped
API path. A caller then parses HTML as JSON and gets `Unexpected token '<'` —
an error that says nothing about the real cause. (I hit exactly that error
earlier in this session from a different cause, which is why it was worth
checking.)

**Fix:** The catch-all refuses `api/`, `health`, `docs`, `redoc` and
`openapi.json`, returning a JSON 404 naming the path.

**Preventive action:** A test asserting an unknown API path returns JSON rather
than HTML, that a POST-only route does not answer a GET with a page, and that
`/health` still returns its real payload.

**Also caught, by an existing test:** the new static route has no authentication,
and the route-auth test failed until it was added to the intentionally-public
list with a justification. It is legitimately public — the login page has to
load before anyone can authenticate — but the safeguard made that a decision
rather than an oversight.

---

## 2026-09-20 — The campaign send ignored the copy somebody approved

**Found by:** Writing a campaign the way the composer invites you to — "Kia ora
#name#, your usual #favourite_product# is one tap away" — approving it, and
reading what the five recipients actually received.

**Failure:** None of them got it. Every message was a model-drafted one about
days since their last order. The merge tags shipped the commit before and were
unreachable from the dashboard, and, worse, approval was meaningless: a person
vouched for one message and the system sent another.

Drafting was a parameter on the send call, `generate_per_customer`, defaulting
to `True`. The dashboard hardcoded `true` in its run request, and the scheduler
called `run_campaign(db, campaign)` and took the default. So both routes into a
send replaced the approved body with generated copy, and the only way to get
the written copy sent was to call the API by hand with the flag off — which
nothing did.

The same page said both things at once. The Message card read "Per-customer
personalisation is generated at send time", and the panel directly beneath it
said each merge tag "is replaced with that customer's own detail when the
message goes out". Both were describing the same body. Only one could be true.

A test send hid it rather than exposing it: with a customer attached it always
generated, whatever the campaign would do. The one screen for checking your own
copy before sending showed a message the campaign would never send.

**Fix:** `campaigns.copy_mode` — WRITTEN or DRAFTED — decided on the campaign,
where the approver can see it. `run_campaign` reads it instead of taking a
flag; the send-time parameter is gone, and a caller still passing it gets a 400
saying where the setting moved rather than having it ignored. The test send
follows the same value, changing the mode withdraws approval as any other
content change does, and the compliance report records
`COPY_DRAFTED_PER_RECIPIENT` so the approval record cannot imply somebody
approved words a model had not yet written. A copy preview renders three real
recipients through the same calls the send makes, so drafted copy can be read
before it is approved rather than after it is sent.

Campaigns that already existed are marked DRAFTED once, when the column
appears, because that is what they have been doing; the alternative is an
approved campaign quietly changing what it sends on an upgrade.

**Preventive action:** Tests assert the written copy is what lands, that the
scheduler path (which has no caller to pass a flag) honours the campaign, that
a test send shows what the send will do in both modes, that the old flag is
refused rather than ignored, and that previewing writes nothing. The general
shape, twice now in this codebase: a setting that decides what customers
receive must live on the thing being approved, not on the call that sends it.

---

## 2026-09-21 — Smart Reorder was two systems that disagreed about the time

**Found by:** Writing the two assertions the screens already imply — "the
reminder goes out when Customer 360 says it will" and "a customer who has
ordered is not reminded" — and watching both fail.

**Failure:** Two separate defects behind one feature.

The timing came from two engines. Customer 360 read the minute-level
prediction and showed a 7:09 PM reminder for a customer who orders around
7:39 PM. The automation that actually sends read the older hour-bucket
pattern, subtracted a hard-coded two hours, and scheduled 5:00 PM. Neither
number was wrong on its own terms, and nothing on the page said they came
from different models — so the screen was describing a system that did not
exist, and the configurable "30 minutes before" the API advertised was not
reachable from anywhere.

The already-ordered check looked only for a PENDING order. A real order
lands COMPLETED, so the common case — ordered at 7:15, reminded at 7:09 —
went straight through. `SkipReason.ALREADY_ORDERED` was defined, had a
phrase written for the ledger, and was set by nothing; the dry-run test
that covered it accepted either reason and passed on the pending one. The
Smart Reorder page stated in as many words that every send re-checks
"whether they have already ordered".

**Fix:** One engine, `app/services/reorder_timing.py`, called by the
scheduler and by every screen — the only arrangement in which they cannot
drift. The offset is the campaign's setting rather than a constant. The
already-ordered check compares against the order the prediction was built
from, so this cycle's purchase suppresses the reminder while the order that
taught us the routine does not.

Two consequences worth naming. The send window can still move a reminder,
and now says so (`moved_for_send_window`, `aimed_at`) rather than quietly
producing a different promise. And a due enrollment is no longer replanned
before the run inspects it — it was, so a customer who had just ordered had
their slot moved out of reach and vanished from the run: no message, which
was right, and no reason in the ledger, which was not.

**Preventive action:** A test asserts the scheduled slot and the slot on
screen are the same weekday and clock time, which is what differed. Others
cover a completed order suppressing the send, last week's order not
suppressing it, and the preview accounting for customers who never became
candidates at all.

---

## 2026-09-21 — predict_next_order hung forever on orders placed minutes apart

**Found by:** Seeding the demo database after wiring the prediction into the
intelligence refresh. A four-second job did not finish in ten minutes.

**Failure:** An infinite loop, not a slow one. The roll-forward advances a
prediction by the customer's median interval until it is in the future, then
snaps it back to their usual weekday and overwrites the clock fields with
their usual time. For an interval under a day the snap returns the same
date and the overwrite restores the same time, so the value never changes
and the `while predicted <= now` test never becomes false.

Three orders fifteen minutes apart produce exactly that — somebody who
forgets the mixers and orders again. Before the prediction moved into the
refresh this was reachable from Customer 360 and the Smart Reorder page for
any such customer: an ordinary page load that never returned.

**Fix:** `_advance_past` checks that each step actually advances and falls
back to a whole week when it does not, which is what anchoring to a weekday
means anyway, with an iteration cap as a backstop. The previously hanging
case now returns in under a millisecond.

**Preventive action:** A test builds that history and asserts the call
returns in under two seconds, so the failure reads as "the roll-forward is
not advancing" rather than as a suite that hangs.

The wider lesson: the loop was correct for every history anyone had tried
it on, and its termination depended on a property of the data — an interval
longer than a day — that nothing enforced or checked.

---

## 2026-09-21 — Every seeded customer was a breakfast buyer

**Found by:** Reading the new dashboard's output rather than only its
counts. The soonest predicted orders were 5:18 AM, 7:29 AM, 7:58 AM. For a
drinks delivery business that is not a plausible reorder window.

**Failure:** The seeder picks an order hour "skewed to evenings" — 5pm to
9pm — and wrote it straight into `ordered_at`, which holds naive UTC. New
Zealand runs twelve or thirteen hours ahead, so a 7pm order was stored as
19:00 and read back as 7am the next morning. 90% of the demo data described
a customer base that orders at dawn, and Smart Reorder faithfully learned
to remind them then.

This is the same defect the read side was fixed for in September, in the
same codebase, on the other side of the boundary: the readers were taught
to convert and the writer never was.

**Fix:** The hour is chosen in local time and converted with `to_utc_naive`
before it is stored. 678 of 749 seeded orders now fall between 4pm and 10pm
on the customer's own clock, where the comment always said they were.

**Preventive action:** A test asserts most seeded orders land in a local
evening and that none lands between 1am and 8am. It fails loudly on the old
behaviour, which is the point — the previous version was invisible for as
long as nothing read the hour back.

---

## 2026-09-21 — Changing when a campaign sends did nothing to the people in it

**Found by:** Changing the reminder offset on the running campaign to check a
claim before writing it into VERIFICATION.md. The claim was wrong: the
reminders did not move.

**Failure:** Routines are recomputed only when they go stale, which is thirty
days. The offset is applied when a slot is planned, so an operator moving
"30 minutes before" to "2 hours before" changed the stored setting, returned
200, and left every enrolled customer scheduled at the old time until their
routine happened to expire. Nothing on screen said the change had not taken
effect.

**Fix:** The offset a slot was planned with is stored beside it, so the
refresh can see that the campaign's setting no longer matches and replan.
Verified on the running system: a 6:48 PM customer moved from a 6:18 PM
reminder to 4:48 PM.

**Preventive action:** A test changes the offset on an enrolled campaign and
asserts the scheduled slot moves.

The near-miss is the part worth keeping. This was found only because a line
of documentation was checked against the product instead of against the code
that was supposed to implement it.

---

## 2026-09-24 — The older merge tags stopped falling back, and only Postgres said so

**Found by:** Running the suite against PostgreSQL after it passed on SQLite.
One campaign test failed: "Kia ora, your is one tap away at gimmedelivery.co.nz".

**Failure:** Moving the campaign sender onto the shared resolver gave it a
fallback table covering only the fields on the new whitelist. The older
spellings — `{name}`, `{favourite_brand}`, `{website}` — kept a second table
inside `templates.py`, which the new path never consulted. Copy written
against them rendered a bare gap mid-sentence.

The reason SQLite missed it is the part worth keeping: the test previews
against the first customer in the audience, and the two engines return that
audience in a different order. On SQLite it happened to be somebody with a
favourite brand and a first name, so every tag resolved and the missing
fallbacks were invisible. The test was not weaker on SQLite; it was luckier.

**Fix:** One table. `FALLBACKS` in `merge_tags.py` carries the legacy
spellings alongside the whitelisted fields, and `templates.py` points at it
rather than keeping its own.

**Preventive action:** A test renders the legacy spellings for a customer
with no name and no order history and asserts the fallbacks appear. It fails
on either engine, regardless of who the audience happens to start with.

---

## 2026-09-24 — The importer only understood its own column names

**Found by:** Checking the merge-tag feature against the brief's section 4,
which asks that "First Name", "FirstName" and "Customer First Name" all reach
`first_name`. None of them did.

**Failure:** `parse_csv` keyed every row by the header exactly as written, and
each ingestor read `row.get("first_name")`. A GIMME export whose first column
is called "First Name" therefore imported every customer with a blank name —
and reported every row accepted, because a blank name is not an error. The
same held for "Item Name" against `product_name`, "Order Date" against
`ordered_at` and "Customer ID" against `customer_external_id`.

Quiet in the worst way: the import looks clean, the customers are all there,
and it only surfaces later as a campaign that greets the entire audience as
"there" and offers them all "your usual order".

**Fix:** One canonicalisation pass in `parse_csv`, so every ingestor and the
header check see the same field names. Word breaks and case are normalised
("First Name", "FirstName", " FIRST_NAME " → `first_name`); genuine renames
are an explicit table. A header matching neither keeps its own slug and is
ignored rather than guessed at — mapping "Delivery Notes" onto the nearest
field is how somebody's address ends up in their name. The preview now
returns `column_mapping` so an operator can see how each column was read
before committing to the import.

**Preventive action:** Tests take a file using none of the internal column
names and assert a rendered message comes out the other end, rather than
checking the mapping at the seam where it is easy to get right in isolation.

---

## 2026-09-24 — You could not read your own campaign copy after 7pm

**Found by:** The browser suite, run at 19:20 NZ instead of the afternoon.
A campaign preview test that had passed all day failed.

**Failure:** The copy preview drew its samples from the eligible audience,
and eligibility includes quiet hours evaluated at the current moment. Between
7pm and 9am every SMS recipient is correctly excluded, so the preview came
back empty — an operator sitting down after dinner to write tomorrow's
campaign saw no message, no recipient and no reason, and would reasonably
conclude the composer was broken.

Worth separating: whether somebody may be *messaged now* and whether an
operator may *read the copy* are different questions, and the preview was
answering the first when it was asked the second.

**Fix:** When the audience is empty and the clock is why, the samples fall
back to the people quiet hours is holding — contactable customers who would
receive this in the morning, not anyone excluded for any reason, which would
have rendered the copy as an opted-out customer. The eligible count stays
honest and the screen says which it is showing.

**Preventive action:** A test pins a campaign's send time inside quiet hours
and asserts the preview still produces a message. The send tests added with
this feature also now pass an explicit `now`, because several of them were
passing by virtue of the hour the suite happened to run.

---

## 2026-09-24 — Half of "you usually order Wednesday evening" was in UTC

**Found by:** Uploading a file in the exact template format and rendering
every merge tag from it. `#preferred_order_time#` read "7:00 pm" for an order
placed at 19:40 — right by accident — while `#preferred_order_day#` and the
minute beside them disagreed.

**Failure:** `compute_metrics` counted `typical_order_weekday` and
`typical_order_hour` straight off the naive-UTC column, while
`typical_order_minute` is written by the Smart Reorder engine, which converts
to local. So one time was assembled from two clocks: the hour from UTC, the
minute from New Zealand. A 7:39pm customer read back as either 7:39 am or
7:00 pm depending on which half you looked at.

It reached further than the tag. The same fields go into the LLM prompt
context as `typical_order_day`, so generated copy had been quoting the wrong
day, and Customer 360 showed it.

Same defect class as the seeder one fixed three days earlier — a local
concept computed on the UTC side of the boundary — now found on a third
code path.

**Fix:** The weekday and hour are counted off `to_local(o.ordered_at)`, like
every other piece of code in this system that reasons about the customer's
clock.

**What it exposed:** with all three fields agreeing, an uploaded `19:40`
Wednesday now reads back as 7:00 am Thursday — which is the *correct*
consequence of the still-open question about whether the export's `ordered_at`
is local or UTC. The bug had been hiding it by being wrong in the opposite
direction. `IMPORT_TIMESTAMPS_ARE_LOCAL` now makes that a setting rather than
a code change, defaulted off so no existing data is silently reinterpreted.

**Preventive action:** A test builds four Wednesday-evening orders as local
moments and asserts `#preferred_order_day#` is Wednesday and
`#preferred_order_time#` ends in "pm".

---

## 2026-09-24 — Every reminder cancelled itself using the order it was built from

**Found by:** Running the campaign against real imported customers rather than
the test fixture. Every message came back CANCELLED / CUSTOMER_ALREADY_ORDERED
before it could send — including for customers who had not ordered since the
prediction was made.

**Failure:** The final already-ordered check needs an instant after which an
order counts as "they have already done it". At dispatch the only fields to
hand were the predicted moment and the typical interval, so the window was
worked back as *predicted − interval*. That lands an hour or two earlier than
the customer's own last order, because the predicted time is the routine's
average clock time and the last order is one particular evening. So the order
the prediction was **derived from** fell inside the window, read as a purchase
that had beaten us to it, and the reminder was called off.

The shape of it is worth keeping: a derived value used as an anchor, where the
exact value was known at a different moment and thrown away.

**Fix:** The anchor is computed when the message is written — the one place
the exact last-order time is in hand — and stored on the row as
`cycle_start_at`. Dispatch reads it rather than re-deriving it.

**Preventive action:** A test asserts the stored anchor is strictly after the
customer's own last completed order, and that dispatching a fresh reminder
cancels nothing. It fails on the old behaviour for every customer.

The near-miss is that the unit tests passed throughout. Sam's fixture happens
to order at almost exactly his average time, so *predicted − interval* landed
within seconds of his last order and the window was right by coincidence. It
took customers with ordinary variation to show it.

---

## 2026-09-24 — A dry run over an audience nobody had enrolled yet

**Found by:** Walking section 45's acceptance criteria against the running
app. The dry run reported "0 analysed, 0 would be scheduled" on a campaign
with 47 matching customers.

**Failure:** The queue builder iterated the automation's *enrollments*, which
are created when a campaign is activated. A dry run is run precisely before
that — its whole purpose is deciding whether to activate — so the preview was
empty at the only moment anybody needed it to say something.

**Fix:** The builder resolves the audience directly and looks the enrollment
up if there is one. A live build still enrolls first, so newly eligible
customers are picked up.

**Preventive action:** The acceptance walk-through is scripted, and the dry
run is asserted to return individual customers before the campaign is ever
activated.

---

## 2026-09-24 — The company's own name was blocking its own campaigns

**Found by:** A screenshot of the compliance panel: five critical findings on
an ordinary GIMME offer, the first of which reported `GIMME` as an unverified
coupon code.

**Failure:** The coupon pattern requires an uppercase token containing a
digit, written `\b(?=[A-Z0-9]{4,20}\b)(?=.*\d)[A-Z][A-Z0-9]{3,19}\b`. The
digit lookahead `(?=.*\d)` is unanchored — it scans the whole rest of the
message rather than the token — so `GIMME` matched whenever any digit appeared
anywhere after it. Which is most messages: a price, a percentage, a pack size,
a phone number.

So the brand's own name was reported as an unverified coupon code in nearly
every campaign, and every one of those was blocked from sending.

**Fix:** The lookahead is bounded to the token's own character class:
`(?=[A-Z0-9]*\d)`. `GIMME` no longer matches; `FIRST10` and `SUMMER24` still
do.

**Preventive action:** A test asserts the company name is not a coupon code
with and without a digit elsewhere in the message, and that two real codes
still are.

The lesson is about lookaheads rather than about coupons: `(?=.*\d)` inside a
token pattern reads as "this token contains a digit" and means "a digit occurs
somewhere later in the subject". The two are the same only in a test string
that ends at the token.
