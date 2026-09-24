# Architecture Decisions

## 2026-08-20 — Modular monolith over microservices

**Decision:** One FastAPI backend with clear internal module boundaries
(`analytics`, `churn`, `rfm`, `recommendations`, `segmentation`, `campaigns`,
`compliance`, `llm`, `integrations`), plus one React frontend.

**Reason:** The MVP must run locally with two processes. Service boundaries are
enforced by module structure, not network hops.

**Alternatives considered:** Separate scoring service; serverless functions.

**Tradeoffs:** Single deployment unit; scaling is all-or-nothing. Acceptable
for an MVP, and the module boundaries make extraction possible later.

---

## 2026-08-20 — Pure functions for all scoring engines

**Decision:** Metrics, lifecycle, RFM, churn and NBA are pure functions over
plain dataclasses (`OrderFact`, `MetricResult`), with a separate persistence
layer that reads the ORM and writes results.

**Reason:** Scoring logic is the product's core value and must be exhaustively
testable. Pure functions test in milliseconds with no database fixtures, which
made it practical to prove all 9 lifecycle stages reachable and every churn
factor attributable.

**Alternatives considered:** Methods on ORM models; SQL-based scoring.

**Tradeoffs:** One extra mapping layer between ORM rows and dataclasses. Worth
it — the mapping is trivial and the test speed is not.

---

## 2026-08-20 — Churn scoring is deterministic and additive, never LLM-derived

**Decision:** The churn score is the sum of named, weighted factors whose
weights total 100. Each factor exposes `severity` (0-1) and `points`. The LLM
may only rewrite the explanation string for readability.

**Reason:** A retention team must be able to defend why a customer is flagged.
An opaque score is unusable for alcohol marketing where targeting decisions
carry regulatory weight.

**Alternatives considered:** Logistic regression / gradient boosting on
historical churn labels.

**Tradeoffs:** Lower theoretical accuracy than a trained model. There is also
no labelled churn history to train on in an MVP. The factor structure means a
learned model can later replace the weights without changing the interface.

---

## 2026-08-20 — Cadence saturation at 6x the expected cycle

**Decision:** The `cadence_overdue` churn factor reaches full severity at 6x
the customer's expected purchase cycle, not 3x.

**Reason:** Initial testing showed a customer 500 days past a 30-day cycle
scoring only 43.6/100 (MEDIUM) because the dominant factor saturated too early
and the quarter-over-quarter decline factors read "0 orders vs 0 orders" as
flat rather than as total stoppage. Both were fixed: cadence saturates later,
and zero activity in the recent window now registers full decline severity.

**Alternatives considered:** Raising the cadence weight alone (would have
distorted moderately-late customers).

**Tradeoffs:** Slightly lower scores for customers 2-3 cycles late. Verified by
`test_risk_rises_monotonically_with_lateness`.

---

## 2026-08-20 — Lapsed states outrank value tiers in lifecycle classification

**Decision:** A customer past their at-risk/dormant/churn thresholds is
classified by lapse, not by spend. A VIP who has vanished is CHURNED.

**Reason:** The lifecycle stage drives the retention action. Labelling an
absent big spender "VIP" hides exactly the customer the product exists to save.

**Tradeoffs:** Value tier is no longer visible from the stage alone, so
lifetime revenue and RFM are surfaced alongside it in Customer 360.

---

## 2026-08-20 — Quantile RFM with an absolute-threshold fallback

**Decision:** RFM uses population quantiles when there are >= 20 scorable
customers and the quantile breaks are strictly increasing; otherwise it falls
back to fixed bands. Customers with no completed orders are excluded from the
distribution and floored at 111.

**Reason:** Quantiles are meaningless on a tiny or flat population, and
including never-purchased customers would drag every real buyer's percentile.

**Tradeoffs:** Scores shift as the population changes — inherent to quantile
scoring, and mitigated by storing `calculated_at`.

---

## 2026-08-22 — One automation table for three campaign types

**Decision:** Recurring sequences, behavioural nudges and cohort bulk sends are
one `automations` table with a `kind` discriminator and one shared send
pipeline (`app/automations/runtime.py`), not three parallel implementations.

**Reason:** The three features differ only in *who* to message and *when*.
Everything after that — consent at send time, quiet hours, dedup, dispatch,
the delivery ledger, dry run — is identical, and it is exactly the part where a
bug is a compliance incident rather than a cosmetic defect. Three copies would
mean three places to get consent gating right and three chances to get it
wrong.

**Alternatives considered:** A table per campaign type; a generic workflow
engine driven by JSON config.

**Tradeoffs:** The single table carries columns only some kinds use
(`offset_days` lives on steps, `next_due_at` on enrollments, `recurrence` only
matters to cohort sends). A generic workflow engine would be more flexible and
far harder to reason about — the point here is that "one message per customer
per day" is provable, not configurable.

---

## 2026-08-22 — Every automation is backed by a Campaign row

**Decision:** Creating an automation creates a `Campaign` and stores its id on
`Automation.campaign_id`; every send is attributed to it.

**Reason:** Attribution, campaign analytics and the Customer 360 message
history all key off `campaign_id`. Without a backing campaign, automated sends
would be invisible to every existing report — a parallel reporting world that
has to be maintained separately and inevitably disagrees with the first one.

**Tradeoffs:** A Campaign row that is never sent through the campaign engine
itself, which is mildly surprising when reading the campaigns table directly.

---

## 2026-08-22 — Dedup keys on the customer's local calendar date

**Decision:** `AutomationSend.local_date` stores the date in
`Pacific/Auckland`, and "one automated message per customer per day" is a query
against `(customer_id, local_date)`.

**Reason:** New Zealand is UTC+12/+13, so a UTC day boundary falls at noon
local. Capping on the UTC date would let a customer receive one message at
11am and another at 1pm and call them different days, while treating a 9am and
a 9pm message on the same working day as the same day only by luck.

**Alternatives considered:** Rolling 24-hour window per customer.

**Tradeoffs:** A rolling window is arguably fairer but much harder to explain
to an operator looking at a ledger, and makes "did we message them today?"
depend on the exact minute of the previous send.

---

## 2026-08-22 — Quiet hours defer, they do not drop

**Decision:** A send falling outside 09:00–19:00 local is moved to the next
open slot rather than skipped. Behavioural nudges are the exception: a
late-evening pattern is pulled *back* to 18:00 the same day.

**Reason:** A job that happens to run at 3am must not silently lose the day's
sends. But for a nudge, deferring forward defeats the feature — a customer who
orders at 9pm on Saturday and is nudged at 9am Sunday is being reminded after
the moment has passed. Their nudge belongs earlier the same day, while they are
still deciding.

**Tradeoffs:** Two different behaviours for "outside the window", which has to
be understood rather than assumed. The alternative — one rule everywhere —
would make a third of the nudges useless, since drinks orders skew heavily to
the evening.

---

## 2026-08-22 — Opt-out is global, never per-channel

**Decision:** A STOP reply clears every consent flag, writes an ALL-channel
suppression record, sets `Customer.is_suppressed`, and stops every automation
enrollment the customer has.

**Reason:** A customer replying STOP is withdrawing permission to be contacted,
not expressing a preference about one campaign. The previous per-channel
handling would have let a customer who stopped SMS keep receiving email, and
would have left them enrolled in sequences that resume the moment a data import
restores a consent flag.

**Tradeoffs:** No way to opt out of one channel only. That is the right default
for alcohol marketing; a per-channel preference centre is a deliberate feature
to add later, not a default to fall into.

---

## 2026-08-22 — A behavioural nudge needs three orders, not one

**Decision:** `MIN_ORDERS_FOR_PATTERN = 3` completed orders, over a window of
the last 8, with a confidence score attached and a monthly recompute.

**Reason:** With two orders, a repeated weekday is a 1-in-7 coincidence.
Messaging on it produces a "we know when you usually order" claim that is not
true, which is worse than not messaging at all. On the seeded dataset this
gates the feature to 556 of 1,000 customers — the ones it can actually serve.

**Tradeoffs:** Excludes light buyers, who are exactly the ones a nudge might
convert. They are reachable by the other two campaign types instead.

---

## 2026-08-22 — An offer requires two independent gates

**Decision:** A nudge carries a discount only when the customer's
`discount_dependency` is at or above 0.4 **and** an approved promotion exists
in brand settings.

**Reason:** The first gate stops discount being spent on customers who buy at
full price anyway. The second means the system can never invent an offer: with
no approved promotion the nudge simply goes out without one, rather than
generating a discount the business has not agreed to honour.

**Tradeoffs:** Configuring promotions is now a prerequisite for offers to
appear at all, which is quiet if nobody has set them up. The stated reason on
every `OfferDecision` makes that visible rather than mysterious.

---

## 2026-08-22 — Additive schema reconciliation instead of migrations

**Decision:** `create_tables()` runs `reconcile_schema()`, which adds columns
the models declare but the database lacks. Additive only; a NOT NULL column
with no default is refused and reported.

**Reason:** `create_all` creates missing tables but never missing columns, so
adding a field to a model broke every existing local database at startup with
"no such column". For a local-first tool, an existing database with real data
in it is the normal case, not an edge case.

**Alternatives considered:** Full Alembic migrations (the dependency is already
present); telling developers to delete their database.

**Tradeoffs:** Only handles added columns. Type changes, constraints and
backfills still need a real migration — which is why Alembic stays in
`requirements.txt` rather than being removed.

---

## 2026-08-22 — Editing an automation's message withdraws its approval

**Decision:** Changing `message_template`, `template_overrides`, the audience,
the per-kind `config`, or a sequence's steps clears `approved_at` and pauses an
active automation. Renaming or editing the description does not.

**Reason:** Approval is a human vouching for a specific message going to a
specific group of people. Without this rule, an approved automation is a
standing permission to send *whatever it currently says* — someone could
approve innocuous copy and edit it afterwards, and the compliance gate would
have been bypassed without anyone bypassing anything.

Pausing as well as un-approving is the honest half: `require_approval` already
stops the send, so an un-approved active automation would sit there looking
live while sending nothing. Better that its status says what is true.

**Alternatives considered:** Versioning automations so an edit creates a new
draft; blocking edits on approved automations entirely.

**Tradeoffs:** Fixing a typo costs a re-approval. That is the right price —
the alternative is a gate that can be walked around by editing after the fact.
Versioning would be better still and is the natural next step if approvals
become frequent enough to be a nuisance.


---

## 2026-09-20 — Who writes a campaign's copy is the campaign's, not the send's

**Decision:** `campaigns.copy_mode` holds WRITTEN or DRAFTED. `run_campaign`
reads it; the `generate_per_customer` send-time parameter is removed, and a
request still carrying it is refused with a message saying where the setting
went. Changing the mode withdraws approval, exactly as changing the body does.

**Reason:** Approval is a person vouching for a specific message going to a
specific group of people. While drafting was a send-time flag defaulting to
true, the words that were approved and the words that were sent had no
necessary relationship: the dashboard passed `true` on every run and the
scheduler took the default, so approved copy was replaced by a generated
message on both paths, and nothing on screen said so.

Storing it also makes the two honest ways to write a campaign nameable, which
is what lets the UI stop contradicting itself — merge tags belong to written
copy and are offered only there, and the body is labelled "Fallback body" when
it is one.

**Alternatives considered:** Keeping the flag but defaulting it to false —
which fixes the accident and not the design, since anything calling the API
could still send something other than what was approved. Having drafted copy
require its own approval per recipient, which is unworkable for a send of any
size and is what the grounding validator and compliance re-check exist to
handle instead.

**Tradeoffs:** Choosing is now part of writing a campaign, where before there
was a default nobody saw. Existing campaigns are marked DRAFTED once, when the
column appears, so an upgrade cannot change what an approved campaign sends —
at the cost of a one-off backfill that a stricter reading would call a
migration.

---

## 2026-09-20 — Drafted copy is previewed against real recipients, not described

**Decision:** `GET /campaigns/{id}/copy-preview` renders the first few eligible
recipients' messages through the same calls the send makes — merge tags filled
for written copy, a real draft for drafted copy — and persists nothing. The
campaign page shows it before approval, and hides it once the campaign has
sent.

**Reason:** For a drafted campaign there is no text on the page that is the
message, so approving one is approving something unseen. A preview produced by
the same code path is the only kind that cannot drift from what will be sent;
anything else is a description of an intention.

**Tradeoffs:** Previewing a drafted campaign runs the model for each sample,
which costs real tokens against a live provider. Capped at five, generated with
`persist=False`, and only when somebody asks. A draft that fails grounding is
shown as "would not send" rather than as the fallback, because that is what the
send does with it.


---

## 2026-09-21 — One timing engine, called by the sender and by every screen

**Decision:** `app/services/reorder_timing.py` answers "when is this
customer's reminder scheduled for?" — prediction, configured offset, and the
send-window clamp in one call. The Smart Reorder scheduler and the customer
API both use it. The offset lives on the campaign.

**Reason:** There were two answers and they differed by two hours. A screen
showing a time the sender will not use is worse than a screen showing
nothing: it is a specific, checkable, wrong promise. The only structural fix
is that the display and the send are the same call, because any other
arrangement re-creates the drift the moment either side is changed.

**Alternatives considered:** Teaching the automation to call the prediction
engine directly and leaving the API as it was — which fixes today's
mismatch and not the class of it; both would still have owned their own
clamping and offset.

**Tradeoffs:** The clamp is policy living in a service rather than in the
automation layer, which is a slightly odd home for it. The alternative was
duplicating it, which is how it drifts.

---

## 2026-09-21 — A reminder moved by the send window says so

**Decision:** The plan carries `moved_for_send_window` and both the aimed
and the scheduled time. The API returns all three; the composer and the
customer panel say when they differ.

**Reason:** A customer who orders at 7:39 PM has their reminder aimed at
7:09 PM, which is past the 7 PM close of the send window, so it goes at 6:00
PM instead. That is the right call — the alternative deferral rule would
push it to 9am the following morning, after the moment it was timed to
catch. But "half an hour before they usually order" and "at 6" are
different promises, and an operator reading a page that only shows the
second has no way to know the first was not kept.

**Tradeoffs:** More fields on the response and more words on the screen, for
a case that only arises for late-evening customers — who are a large share
of this particular business.

---

## 2026-09-21 — The next-order prediction is stored, not computed on demand

**Decision:** `customer_metrics` gains `predicted_next_order_at` (indexed),
`prediction_confidence` and `typical_order_minute`, written by the
intelligence refresh.

**Reason:** Three things needed it at once. A segment cannot filter on a
number that only exists inside a function call, so "Smart Reorder Eligible"
was unbuildable. The scheduler was replaying every customer's order history
on every five-minute run. And the dashboard could only count enrolled
customers, so it read zero until somebody activated a campaign — the
opposite of what a page for deciding whether to run one is for.

**Alternatives considered:** Computing on read with a cache. That is a
cache, with its invalidation, plus a second definition of "current".

**Tradeoffs:** A stored prediction can be stale between refreshes, which is
why the send path re-plans from live order history at send time rather than
trusting the column. The column is for finding candidates; the send is for
deciding.

---

## 2026-09-24 — A merge tag is a field lookup, never an expression

**Decision:** `app/services/merge_tags.py` holds one explicit whitelist of
fields. A tag resolves against that table or it does not resolve at all.
There is no attribute path, no expression, and no way to reach a column that
is not listed — `#customer.password#` is not a tag that fails, it is text.
Substitution is a single pass, and resolved values are stripped of control
characters and of the tag delimiters themselves, so a value can never be read
back as another tag.

**Reason:** The obvious implementation — walk the attribute path on the ORM
object — is four lines shorter and turns every column in the database into
something a person writing marketing copy can put in a text message. The
whitelist is the feature.

**Alternatives considered:** A template engine (Jinja). It is a language, and
a language in a field an operator types into is a much larger surface than
this needs. Nothing here wants a loop.

**Tradeoffs:** Adding a field is a code change rather than configuration.
That is the intended cost.

---

## 2026-09-24 — An unknown merge tag blocks approval, not just the preview

**Decision:** `UNKNOWN_MERGE_TAG` is a blocking compliance finding, checked
again at the top of `run_campaign` and at automation activation. Each
offending tag is named.

**Reason:** A tag that cannot be filled is not a value that comes back empty
— it is delivered verbatim to the whole audience. It is also the one failure
a person reading the copy could have caught, so it belongs in the gate where
somebody is still looking, rather than in a sent message. Automations matter
more than campaigns here: they send unattended for as long as they are on, so
activation is the last moment anybody is watching.

**Tradeoffs:** The check is a regex pass over the copy at approve, send and
activate. Cheap, and worth paying three times rather than trusting a
compliance snapshot taken before the copy was last edited.

---

## 2026-09-24 — Personalisation happens on the way out, and is recorded

**Decision:** The campaign row keeps the template. The resolved text goes on
the `Message` row, with the template in `original_body` and a
`personalisation` block in `generation_context` naming the missing fields and
the fallbacks that stood in for them.

**Reason:** Two different questions — "what was the copy?" and "what did this
person receive?" — were being answered with one string. And "Hi there" is
indistinguishable from a customer actually called There unless the fallback
was recorded at the moment it was used, which is the first thing asked when
copy reads oddly for one recipient and not another.

**Tradeoffs:** A JSON blob per sent message. It sits in a column that already
existed for the drafted-copy path.

---

## 2026-09-24 — Column names are canonicalised on import, once

**Decision:** `parse_csv` maps every header to an internal field name before
any ingestor sees it: word breaks and case normalised, then an explicit table
for genuine renames. Unrecognised headers keep their slug and are ignored.

**Reason:** The merge tags are only as good as the data behind them, and the
join between the two is a name. Doing this per-ingestor is how one of them
ends up understanding "Order Date" and the others quietly not.

**Alternatives considered:** A fuzzy match on header similarity. It would
handle spellings nobody has sent us, at the cost of occasionally mapping
"Delivery Notes" onto a customer's name — and a wrong mapping is worse than a
missing column, because a missing column is reported.

**Tradeoffs:** A spelling not in the table is not imported. The preview now
returns `column_mapping`, so that is visible before committing rather than
discovered in a campaign.

---

## 2026-09-24 — #product# follows the latest order, not the longest habit

**Decision:** `#product#` resolves to the product on the customer's most
recent completed order, falling back to their most-ordered product. The
most-ordered one keeps its own tags, `#preferred_brand#` and
`#preferred_category#`.

**Reason:** "Fancy another Corona Extra?" is a question about what they last
bought. A customer who has just switched should be asked about the new thing,
not told what they used to drink — which is what a most-ordered lookup says to
anybody mid-switch.

**Tradeoffs:** One more stored column, `customer_metrics.last_order_product`,
written by the metrics pass. The alternative was a per-recipient query into
the order lines on every send.

---

## 2026-09-24 — The merge tags are pinned to the upload format

**Decision:** Every field names the upload column it reads
(`MessageField.source_column`), the column heading works as a tag in its own
right where the names differ (`#ordered_at#` = `#last_order_date#`), and every
column in the upload template must either have a tag or appear in
`NOT_FOR_MESSAGING` with a reason. A test enforces the last part.

**Reason:** The two halves of personalisation are joined at a name, and
nothing was holding them together — the upload format had carried `region`,
`signup_date` and `delivery_city` all along while the tag list had never heard
of them. The exclusion list is the more useful half: without it, a column with
no tag is indistinguishable from a column somebody forgot.

**Tradeoffs:** Adding a column to the upload format now fails a test until
somebody decides whether it is message content. That is the intended cost — it
is a decision, and it was previously being made by omission.

---

## 2026-09-24 — How a bare timestamp is read is configuration

**Decision:** `IMPORT_TIMESTAMPS_ARE_LOCAL`, defaulting to false.

**Reason:** `2026-09-16 19:40:00` in a CSV says nothing about which clock
wrote it, and the column it lands in holds naive UTC. Most order exports
record local time, in which case reading it as UTC puts every order half a day
out — and the hour is what the learned routine, Smart Reorder's send time and
`#preferred_order_time#` are all built on.

**Alternatives considered:** Assuming local, since this is a New Zealand
business and 5am alcohol deliveries are implausible. The inference is probably
right and is still not ours to make silently: it changes what every
already-imported timestamp means.

**Tradeoffs:** Answering the question is a setting plus a re-import rather
than a click. The default leaves existing data exactly as it is.

---

## 2026-09-24 — A Smart Reorder reminder is a row before it is a message

**Decision:** `scheduled_messages` holds one row per customer per reminder,
written ahead of time with its text already rendered, and the scheduler
dispatches from it.

**Reason:** The engine that decides *when* each customer will next order
already existed, and so did the gates that decide whether a message may go
out. What was missing was the thing between them. Without a row per message a
Smart Reorder campaign is a promise — one rule that will, at some point,
produce thousands of different messages at thousands of different minutes,
and nobody can see any of them until they have been sent. With it, an
operator can read the exact string that will reach a named customer at a named
minute, change it, move it, or call it off.

Rendering at scheduling time rather than at send time is the part that makes
it worth having: a queue that shows a template is a queue nobody can check.

**Alternatives considered:** Computing the queue on read, from the existing
enrollments. That shows what *would* be sent, which is not the same claim —
it cannot be edited, cannot record that a person cancelled one, and changes
under you between looking and sending.

**Tradeoffs:** A row that can go stale. Handled by re-planning on every
order rather than on a schedule, and by expiring a message whose moment has
passed rather than sending it late.

---

## 2026-09-24 — The queue never decides eligibility

**Decision:** Dispatch hands each message to `execute_candidates`, the same
path every other automation takes. The only check the queue makes itself is
whether the customer has already ordered.

**Reason:** Consent, suppression, age, frequency caps, quiet hours, dedup and
content compliance are one implementation or they are two, and the one that
drifts is the one that texts somebody who opted out. The already-ordered
check is the exception because it is the only one whose answer means "this
reminder was a good idea and is no longer needed" rather than "this customer
may not be messaged".

**Tradeoffs:** The dispatcher is a translation layer — it maps the pipeline's
decision back onto the message's own status. Worth it: a second copy of the
compliance rules is a second thing to keep correct.

---

## 2026-09-24 — Claiming a message is a conditional update

**Decision:** SCHEDULED → PROCESSING is an `UPDATE ... WHERE status =
'SCHEDULED'`, and only a dispatcher whose update matched a row may send.

**Reason:** Duplicate protection cannot rest on "the job never overlaps".
That is not a property anybody can promise about a background worker — a
retry, a second instance, a slow tick and an overlapping one all break it.
The conditional update makes two racing schedulers produce one send and one
no-op regardless.

**Tradeoffs:** A message whose dispatcher dies mid-flight is left PROCESSING
rather than retried immediately. Bounded by the attempt counter and the
staleness window, both of which prefer silence to a reminder sent late.

---

## 2026-09-24 — A reviewer can vouch for what the engine cannot check

**Decision:** Compliance findings split in two. Those the engine can judge —
prohibited claims, targeting inferred vulnerability, unfillable merge tags —
keep blocking. Those that report *an absence of data on our side* —
`UNVERIFIED_COUPON_CODE`, `UNVERIFIED_PROMOTION`, `UNVERIFIED_PRICE_CLAIM`,
`UNVERIFIED_DELIVERY_CLAIM`, `MISSING_SMS_OPT_OUT` — can be cleared by a named
human confirming them at approval. The finding stays in the report at full
severity, now carrying who vouched for it, and the confirmation is written to
the audit log with the copy as it stood.

**Reason:** This system holds no pricing data, no catalogue and no promotions
beyond the few configured in Brand settings. "Mentions the promotion '$10
off', which is not on the approved list" is not the engine detecting a lie —
it is the engine reporting that it has nothing to check against. Treating that
as a veto makes our ignorance outrank the knowledge of the person who typed
it, and the only way past it is to stop writing real offers. A compliance
engine people route around is worse than one that asks.

Sign-off is per finding, not a blanket override: confirming the coupon code
says nothing about the price.

**Alternatives considered:** Disabling the rules. That loses the check for
LLM-drafted copy, which is what they were built for — nobody reads each of
those, and an invented promotion is exactly what they catch. Also considered
maintaining a promotions list so the engine could verify properly; that is the
better long-term answer and is what Brand settings is for, but it cannot be
the precondition for sending today's campaign.

**Tradeoffs:** `MISSING_SMS_OPT_OUT` is on the vouchable list at the business's
request, and it is the one carrying real legal weight — a commercial
electronic message must carry an unsubscribe facility. It is defensible here
because GIMME sends via a TNZ short code that handles STOP at the carrier
level, so a reviewer confirming "the facility exists" is saying something
true. The recommendation remains that the copy says "Reply STOP to opt out",
because the facility should be stated, not merely available.

---

## 2026-09-24 — The segment export writes phone numbers in international form

**Decision:** `GET /segments/{id}/export.csv` gains a `phone` column beside
`email`, written as `+642902076762` via the existing `normalize_nz_phone`. A
number that cannot be resolved exports blank. The stored value is never
touched.

**Reason:** An exported list exists to be used somewhere else — uploaded to a
provider, handed to an agency, dialled. `02902076762` is fine on a screen and
wrong in most of those places.

Blank rather than the original value for the unresolvable ones, because a
column that is international for most rows and whatever-was-typed for the
rest is worse than one with visible gaps: the gaps get chased, while a stray
`09 555 1234` among the `+64`s looks like data somebody has checked.

**Alternatives considered:** A second, more generous formatter that also
accepts landlines, so no row exports blank. Rejected because it would mean two
answers in this codebase to "is this a valid phone number" — and the one that
drifts is the one that decides who gets texted. Against real data the question
is moot: all 1,010 customers export cleanly.

**Tradeoffs:** A customer whose only number is a landline exports blank. That
is the cost of one canonical normaliser, and it is visible rather than silent.

Excel may display a `+`-prefixed cell as a number. Fixing that inside a CSV
means writing `="+64…"`, which breaks every non-spreadsheet reader including
this system's own importer, so the file keeps the correct value.
