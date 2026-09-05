# Campaign automations

Three recurring campaign types built on the existing TNZ SMS integration. They
share one send pipeline, so consent, quiet hours, deduplication, delivery
tracking and dry-run behave identically across all three.

- **Cohort bulk send** — one-off or recurring sends to whoever matches a
  segment *at send time*.
- **Recurring sequence** — an ordered series of steps timed from each
  customer's own enrollment.
- **Behavioural nudge** — a standing per-customer message at the day and time
  they usually order.

---

## Order history: the dependency all three rest on

Order history already exists and is complete. No import or sync is needed.

| Table | What it holds |
| --- | --- |
| `orders` | `ordered_at` (datetime), `status`, `total_amount`, `discount_amount`, `channel`, `coupon_code`, `delivery_city` |
| `order_items` | `sku`, `product_name`, `category`, `brand`, `quantity`, `unit_price`, `line_total` |

On the seeded dataset that is 5,713 orders across 10,553 line items, spanning
2025-06-17 to 2026-08-20, with a genuine time-of-day distribution (evening
peak: 19h = 1,255 orders, 18h = 1,115, 20h = 925). 729 customers have 3+
completed orders, 556 have 5+ — so the behavioural nudge has real signal to
work from.

`app/analytics/order_patterns.py` reads this through `OrderFact`, the same pure
dataclass the churn and RFM engines use. It never touches the ORM.

---

## The shared pipeline

Every candidate message, from any campaign type, passes through
`app/automations/runtime.py` in this order. A dry run takes the identical path
and stops before the provider call, which is what makes the preview
trustworthy — it is not a simulation of the rules, it *is* the rules.

### 1. Timing

The send time is resolved into the local business window first, so
deduplication is computed against the day a message would actually land on.

- Window: **09:00–19:00 `Pacific/Auckland`** (`SEND_WINDOW_START` /
  `SEND_WINDOW_END`).
- Outside it, the send is **deferred to the next open slot, never dropped** — a
  job running at 3am still reaches customers at 9am.
- The database stores naive UTC throughout; every local-time decision goes
  through `app/core/timezones.py`. NZ is UTC+12 (NZST) or UTC+13 (NZDT), so a
  quiet-hours check against naive UTC would be wrong by half a day.

### 2. Consent, at send time

`check_recipient()` is re-run for every recipient at dispatch, against the
customer's current state — never trusted from campaign creation or from the
audience preview. It covers marketing and per-channel consent, suppression,
age verification, missing contact details, and the 7/30-day frequency caps.

A customer who opted out an hour after the preview was taken is dropped here.

### 3. Deduplication

**One automated message per customer per local day.** The key is
`(customer_id, local_date)` on `automation_sends`, where `local_date` is the
customer's calendar date, not a UTC date that straddles their evening.

Contests are resolved by priority:

| Priority | Kind | Rationale |
| --- | --- | --- |
| 30 | `NUDGE` | Timed to this customer specifically; the most likely to convert |
| 20 | `SEQUENCE` | Part of a committed series |
| 10 | `COHORT_BULK` | The most substitutable — it can go tomorrow |

- A higher-priority candidate **displaces** a merely *scheduled* lower-priority
  send.
- A message that has already **sent** cannot be recalled, so priority is
  irrelevant against it — the new candidate is skipped regardless.
- The loser is written to the ledger as `SKIPPED` / `DEDUPED` with a detail
  line naming what beat it.

### 4. Content compliance

The rendered body runs through `check_content()`. Anything with a blocking
finding is skipped as `VALIDATION_FAILED` rather than sent.

### 5. Dispatch and the ledger

Every attempt writes an `automation_sends` row — sent, failed **or skipped** —
carrying the body, provider, provider message id, scheduled time, local date,
priority and, where applicable, the skip reason and detail. A campaign is
auditable from this table alone.

`idempotency_key` is `(automation, step, customer, local_date)` for a send, so
a re-run or a crash mid-batch cannot produce a duplicate message. Skips add
their reason to the key, since one customer can legitimately be skipped for
different reasons by different candidates on the same day.

Delivery receipts from TNZ advance the row through
`SENT → DELIVERED` / `FAILED` (`app/automations/delivery.py`). Progress is
one-way: a late-arriving "sent" event cannot walk a delivery backwards.

### Per-customer history

The same table read the other way — by customer rather than by automation — is
the answer to "what have we sent this person?". `customer_history()` returns it,
`GET /customers/{id}` carries it as `automation_history`, and the Automations
tab on a customer profile renders it: which automation, when in NZ time, the
delivery status, and the message body as it was actually sent.

Withheld rows appear alongside sent ones, with the reason. That is the point of
the tab: without them, a customer who was in the audience and deliberately
skipped for consent looks identical to one who was never a candidate at all,
and "why didn't they get it?" has no answer.

---

## Opt-out

A `STOP` reply is a withdrawal of permission to contact, not a preference about
one campaign. `app/services/optout.py` therefore:

1. clears **all four** consent flags;
2. writes a `ConsentEvent` per type;
3. sets `Customer.is_suppressed`;
4. writes an **ALL-channel** `SuppressionList` record;
5. **stops every active automation enrollment** the customer has;
6. writes an `AuditLog` entry.

Eligibility reads the suppression record as well as the consent flags, so a
later data import restoring a flag cannot silently re-enable messaging.

Recognised as opt-out (case-insensitive, punctuation stripped, and the keyword
must be the **entire** message): `stop`, `stopall`, `stop all`, `unsubscribe`,
`unsub`, `cancel`, `end`, `quit`, `optout`, `opt out`, `opt-out`, `remove`,
`no`, `nomore`, `no more`. Opt back in with `start`, `unstop`, `yes`,
`subscribe`, `optin`.

"I couldn't stop drinking that IPA" is not an opt-out — the keyword has to be
the whole message.

The TNZ webhook reads inbound reply bodies, and resolves the customer by phone
number when the provider does not echo our message id back. An opt-out is never
dropped for want of a matching message.

---

## Feature 3 — Cohort bulk sends

The audience is **re-evaluated on every occurrence** from live segmentation.
"Every Monday, message whoever is currently At Risk" means *currently* — a
customer who has since ordered drops out, and one who has since lapsed is
picked up, without anyone touching the campaign.

Copy defaults to the segment's tone unless overridden:

| Segment | Default objective |
| --- | --- |
| Needs Second Order | `SECOND_ORDER` — encouragement toward a second order |
| Dormant | `REACTIVATION` — "it's been a while" |
| Churned | `WIN_BACK` |
| At Risk / High Value At Risk / Critical Churn Risk | `RETENTION` |
| Regulars / Recently Reactivated | `REORDER` — reorder reminder |
| VIP / High Value | `VIP_APPRECIATION` |
| New Customers | `ACTIVATION` |

Explicit `message_template` wins; then `template_overrides[segment_name]`; then
the mapping above.

**Variants.** `message_variants` holds up to ten alternative wordings for one
send. A customer is assigned by `customer_id % len(variants)` rather than at
random, so the same customer always gets the same variant and **a preview shows
exactly what the live run will send** — random assignment would make the
preview a lie. The chosen variant is recorded on the ledger row
(`variant_index`), so which wording someone received is auditable afterwards.

Recurrence is `ONCE`, `DAILY`, `WEEKLY` (with `recurrence_day` 0=Monday) or
`MONTHLY` (with `recurrence_day` as day-of-month, clamped safely — a 31st
schedule lands on the 30th in a 30-day month). All recurrence is computed in
local time, so a Monday send does not drift onto Sunday across a DST change.

---

## Feature 1 — Recurring sequences

Steps are timed by **`offset_days` from the customer's own enrollment**, not by
calendar date. That is what makes a sequence reusable: Day 0 / Day 7 / Day 14
means seven days after *this* customer joined, so the same sequence runs all
year and everyone gets the same experience.

**The trigger** decides what each customer's clock counts from. Step offsets
are measured from this moment, which is not always when they joined:

| Trigger | Clock starts at |
| --- | --- |
| `SEGMENT_ENTRY` (default) | The moment they join the audience |
| `SIGNUP` | Their signup date, however long ago |
| `LAST_ORDER` | Their most recent completed order |
| `MANUAL` | Nobody is enrolled automatically; an operator adds them by hand |

A customer for whom the trigger has not happened — no signup date, no completed
order — is **not enrolled**, and the count is reported. Starting their clock
"now" instead would quietly turn the campaign into a different one.

A `MANUAL` sequence needs a way to add people, so the Enrollments card on a
sequence has an **Add customers** button (`POST /automations/{id}/enrollments`
with customer ids). It reports what happened to each id — enrolled, already in,
or not found — rather than silently succeeding, and re-submitting the same ids
enrolls nobody twice.

**Back-dated triggers** need care. A signup-triggered Day 0 / 7 / 14 sequence
enrolling somebody who signed up three months ago would otherwise fire all
three steps in three consecutive runs. Steps whose moment passed more than
`catch_up_days` (default **3**) before they joined are skipped, and the
customer resumes at the first step still worth sending. Signed up eight days
ago, they get Day 7; signed up thirty days ago, they get nothing and the
enrollment completes.

**Enrollment mode** is a per-campaign toggle:

- `ROLLING` — the segment is re-evaluated each run; new matching customers
  start at Day 0 from the moment they join.
- `FIXED_COHORT` — the audience is locked at launch; nobody joins later.

**Stop conditions**, all evaluated immediately before each run so they cannot
go stale:

| Condition | Behaviour |
| --- | --- |
| Customer opts out | Enrollment stopped, no later step is sent |
| Customer places an order | Goal met — stopped (`stop_on_order`, default on). A *cancelled* order does not count |
| Campaign `ends_at` passes | Remaining steps are not sent; the automation completes |

One customer can also be **paused** on their own, without touching the rest of
the campaign — `POST /automations/{id}/enrollments/{enrollment_id}/pause`, and
`/resume` to release them. A pause is a person saying "not this one, not now"
and keeps their progress; `STOPPED` is the system deciding they are finished.
Conflating the two would lose the difference between resumable and done.

One step per customer per run. If a sequence was paused for a fortnight and
three steps came due, the customer gets the next one, not a burst of three.

A step that was **skipped** (quiet hours, a lost dedup contest) is **retried on
the next run, not consumed** — otherwise the deferral would silently swallow a
message the customer was meant to receive.

Steps cannot be edited once customers are enrolled: changing an offset would
re-time messages for people already partway through. Pause and create a new
version instead.

---

## Feature 2 — Behavioural nudges

A standing automation with no end date. Each customer is messaged at the day
and time *they* usually order, and it continues until they opt out.

### "Usual order day and time"

| Setting | Default | Why |
| --- | --- | --- |
| `MIN_ORDERS_FOR_PATTERN` | 3 completed orders | With two orders, a repeated weekday is 1-in-7 luck. Guessing produces a message timed by coincidence |
| `DEFAULT_WINDOW_ORDERS` | last 8 orders | Recent behaviour beats ancient behaviour — a customer who moved from Fridays to Sundays should follow the change, not average across it |
| `PATTERN_STALE_AFTER_DAYS` | 30 days | Habits drift; a pattern computed once and frozen slowly stops matching |

Only **completed** orders count — a cancelled order says nothing about when
somebody likes to buy.

The weekday is the mode of the window. The time bucket is the mode of five
buckets (`morning` 6–12, `afternoon` 12–17, `early_evening` 17–20,
`late_evening` 20–24, `overnight` 0–6), and the representative hour is the
median of the orders **inside** the modal bucket — averaging a lunchtime and a
late-night order would land at neither.

Confidence is `0.6 × weekday_confidence + 0.4 × time_confidence`: a weekday
match is 1-in-7 by chance and a bucket match roughly 1-in-5, so the weekday
signal is weighted higher.

Patterns are recomputed daily by `refresh_order_patterns_job`, which only
touches patterns past their age. A customer whose history no longer supports a
pattern is **stopped**, not nudged on a stale one.

### Arriving before the window

`lead_hours` (default **2**) sends the nudge that far ahead of the customer's
usual slot: the point is to reach them while they are still deciding, and a
message arriving at the exact hour they normally buy is often too late to
change anything. `lead_days` shifts whole days for a weekly rhythm.

The lead never pushes a nudge outside business hours — a 10am buyer minus two
hours would be 08:00, so the window clamp below brings it back to 09:00.

### Timing into business hours

A nudge is pulled into 09:00–19:00 **on the customer's own day**, not deferred
forward like other sends:

| Pattern | Nudge sent |
| --- | --- |
| Saturday 21:00 | Saturday 18:00 |
| Saturday 03:00 | Friday 18:00 |
| Saturday 17:00 | Saturday 17:00 (unchanged) |

Deferring a 9pm buyer to 9am Sunday would reach them after the moment had
passed, which defeats the point of timing the message to their habit.

### Offers

`decide_offer()` requires **two independent gates**, and both must pass:

1. `discount_dependency >= 0.4` — the customer has historically responded to
   discounting, so the offer is not spent on someone who would have bought
   anyway;
2. an approved promotion exists in Brand settings — **the system never invents
   an offer**. With none configured, the nudge goes out without one.

Every decision carries its reason, visible in the dry-run preview.

### Safeguards

- Never fires against a customer with a **pending order** — they do not need
  reminding to buy what they have just bought. Recorded as a `PENDING_ORDER`
  skip so it is visible, not a silent disappearance.
- `DEFAULT_MIN_GAP_DAYS = 7` — a weekly buyer gets a weekly nudge; a monthly
  buyer does not get four.
- Highest dedup priority, so a nudge displaces a bulk send rather than arriving
  alongside one.

---

## Frequency caps

Deduplication stops two messages on one day. The 7-day and 30-day caps stop too
many over weeks — independent rules, both needed: without the cap, a customer
enrolled in several automations could receive one message every single day and
never trip the per-day rule once.

Only a **successful send** counts toward a customer's allowance. A skip never
reached the provider, and a provider failure never reached the customer, so
neither consumes it. Both are still written to the ledger — not counting is not
the same as not happening.

---

## The backing campaign

Every automation owns a `Campaign` row, so its sends carry a `campaign_id` and
flow through the existing attribution, campaign analytics and Customer 360
message history unchanged. An order placed inside the attribution window after
an automation send is credited to that automation's campaign exactly as it
would be for a hand-built one.

Those campaigns are plumbing rather than campaigns anybody created, so the
Campaigns screen hides them — pass `include_automations=true` to see them. The
exclusion is derived from `automations.campaign_id`, so a live automation and
its plumbing cannot drift apart, *and* from a write-once
`campaigns.is_automation_backing` flag, which is what keeps the campaign hidden
once the automation is deleted and the join has nothing left to match.

Deleting an automation deletes its backing campaign only when nothing was ever
sent through it. Once messages have gone out, the campaign is the record their
attribution hangs off, so it outlives the automation — hidden, but intact.

---

## Scheduling

Two APScheduler jobs, no queue broker. Per-customer timing lives in
`next_run_at` and `next_due_at` columns, so a poll is enough resolution for a
business that only sends between 9am and 7pm.

| Job | Interval | Does |
| --- | --- | --- |
| `run_automations` | `AUTOMATION_TICK_MINUTES` (5) | Runs every automation whose `next_run_at` has passed |
| `refresh_order_patterns` | 24 hours | Recomputes stale nudge patterns |

One automation failing does not stop the rest — each is isolated, and a broken
template in a cohort campaign will not block the nudges.

---

## Dry run

`POST /api/v1/automations/{id}/preview` returns exactly who would receive what,
when, in local time — and who would not, with the reason. It works on **any**
automation in any state, including an unapproved draft, because previewing is
how an operator decides whether to approve at all.

Nothing is sent, no provider is called, and a dry run does not reserve a
customer's day. Preview rows are written to the ledger flagged `is_dry_run` and
hidden from the send list unless `include_dry_runs=true`.

Contact details are partially redacted in previews, so a preview is safe to
screenshot.

---

## Approval

A live run requires **both** `status = ACTIVE` **and**, when
`require_approval` is set (the default), a recorded human approval. Approval is
checked at activation as well as at send time — failing at activation is a
better experience than an automation that looks live and silently sends
nothing.

Viewers can read everything, and approve, run or delete nothing.

### Approval follows the message

Approval attaches to the *copy and audience*, not merely to the automation's
existence. Somebody signed off on a specific message going to a specific group;
editing either and carrying on sending would mean sending something nobody
approved.

So changing any of these withdraws approval:

`message_template` · `template_overrides` · `segment_id` ·
`manual_customer_ids` · `config` · a sequence's steps

When that happens the automation is also **paused**, because an
approved-looking campaign that has quietly stopped sending is worse than one
that visibly needs attention. An audit entry records what was changed and
whether it had been running. The editor warns before saving rather than
letting an operator discover it afterwards.

Renaming, or editing the description, changes nothing a customer would receive
and leaves approval intact. An automation created with `require_approval:
false` is left alone entirely — turning that gate off is a deliberate choice.

Sequence steps additionally **cannot be changed once anyone is enrolled**:
re-timing a sequence under customers already partway through it would change
what they receive and when. Create a new version instead.

---

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/v1/automations` | List, filterable by `kind` and `status` |
| `POST` | `/api/v1/automations` | Create (always as a draft) |
| `GET` | `/api/v1/automations/{id}` | One automation with its steps |
| `PATCH` | `/api/v1/automations/{id}` | Update |
| `PUT` | `/api/v1/automations/{id}/steps` | Replace a sequence's steps (refused once anyone is enrolled) |
| `DELETE` | `/api/v1/automations/{id}` | Delete (refused while active) |
| `POST` | `/api/v1/automations/{id}/approve` | Record human approval |
| `POST` | `/api/v1/automations/{id}/activate` | Switch on |
| `POST` | `/api/v1/automations/{id}/pause` | Pause |
| `POST` | `/api/v1/automations/{id}/resume` | Resume |
| `POST` | `/api/v1/automations/{id}/preview` | Dry run |
| `POST` | `/api/v1/automations/{id}/run` | Run now, outside the schedule |
| `GET` | `/api/v1/automations/{id}/audience` | Audience resolved live |
| `GET` | `/api/v1/automations/{id}/stats` | Delivery and enrollment counts |
| `GET` | `/api/v1/automations/{id}/sends` | The delivery ledger |
| `GET` | `/api/v1/automations/{id}/enrollments` | Per-customer enrollment state |
| `POST` | `/api/v1/automations/{id}/enroll` | Bring enrollments up to date without sending |
| `POST` | `/api/v1/automations/{id}/refresh-patterns` | Recompute nudge order patterns |

---

## Configuration

| Setting | Default | Meaning |
| --- | --- | --- |
| `BUSINESS_TIMEZONE` | `Pacific/Auckland` | The clock every local decision uses |
| `SEND_WINDOW_START` | `09:00` | Earliest local send time |
| `SEND_WINDOW_END` | `19:00` | Latest local send time (exclusive) |
| `AUTOMATION_TICK_MINUTES` | `5` | How often due automations are checked |

---

## Message templates

Templates use `{placeholder}` tokens filled from the customer's own record and
approved brand settings — there is no free text, so a rendered message cannot
claim anything the business has not signed off.

`{first_name}` `{full_name}` `{city}` `{company}` `{website}`
`{delivery_promise}` `{support_phone}` `{support_email}` `{sign_off}`

Nudges add `{usual_day}`, `{usual_category}`, `{offer_line}`, `{promotion}`,
`{coupon_code}`.

An empty value renders a sensible fallback (`{first_name}` → "there"); an
*unknown* token is left visible so it fails the compliance placeholder check
rather than shipping a broken sentence.

### Sign-off

`BrandSettings.signatory_name` and `signatory_title` are **deliberately empty**.
Until a real name is configured, `{sign_off}` renders as nothing and the
message goes out unsigned. Attributing outbound customer SMS to an invented
person is worse than sending it unsigned — set these in Brand settings before
using `{sign_off}`.

---

## Copy written per customer

A sequence step can set `use_llm`, and its copy is then drafted for each
recipient instead of rendered from the template. The step's own wording stays
on file as the fallback — a step that asks for a draft still needs message text,
because the fallback is what goes out when a draft cannot be used.

**Where it happens matters.** The draft is generated *after* the eligibility
gate, not while candidates are being built. Somebody who withdrew consent, is
suppressed, or is over their frequency cap has already been ruled out by then,
and their history is never handed to a model to write a message that was never
going to be sent.

**What can go wrong, and what happens then.** In every failure the step's
approved wording is sent instead, and the ledger records `generated = false`:

| Outcome | Result |
| --- | --- |
| Provider error or timeout | Template sent, `reason: generation_error` |
| Draft fails grounding validation | Template sent, `reason` is the validation code |
| Draft fails a compliance rule | Template sent, `reason` is the rule code |
| Draft is accepted | Draft sent, `generated = true`, provider and model recorded |

Losing the personalisation is the right price for a bad draft. Dropping the
message would punish the customer for a problem on our side.

**It is not a way around approval.** A dry run generates exactly as a live run
does, so the preview an operator approves on is the copy that will actually be
sent — approving on template text while drafted text goes out would make the
approval meaningless. Whatever wins still passes the same compliance gate as
hand-written copy, and `AutomationSend.generated` says which is which, so the
history never implies a person wrote something a model did.

Every draft costs one model call per recipient, in a dry run as well as a live
one. On a large audience that is the thing to think about before ticking the
box.

### The opt-out is enforced, not assumed

Every commercial SMS must tell the recipient how to stop, and until drafting
existed that was a convention inside the default templates rather than a rule.
`MISSING_SMS_OPT_OUT` now blocks any SMS without one, whether it was drafted or
typed by hand. The check is deliberately loose about wording — "Reply STOP",
"Text STOP to unsubscribe", "Unsubscribe any time" all pass — because the rule
is that the recipient was told, not that one sentence appears. Set
`require_sms_opt_out = false` for a genuinely non-commercial SMS such as a
delivery notification.
