# API reference

Base URL: `http://127.0.0.1:8000`
Interactive docs: <http://127.0.0.1:8000/docs>

## Authentication

Two independent mechanisms, for two different callers.

**Dashboard users** authenticate with email and password and receive a JWT.
Send it as `Authorization: Bearer <token>`.

```bash
curl -X POST http://127.0.0.1:8000/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"admin@gimmedelivery.co.nz","password":"GimmeAdmin123!"}'
```

```json
{
  "access_token": "eyJhbGci...",
  "token_type": "bearer",
  "expires_in_minutes": 720,
  "user": { "id": 1, "email": "admin@gimmedelivery.co.nz", "role": "ADMIN" }
}
```

**Machine callers** (your storefront, an ETL job) use an API key created in
the UI under *Data & imports*, sent as `X-API-Key: gimme_sk_...`. The full key
is shown once at creation and only its hash is stored. A dashboard JWT is not
accepted as an API key, and vice versa.

---

## Ingestion

All ingestion endpoints accept an array and validate each row independently:
one bad row is reported and skipped, the rest are imported. A response is
always `200` with counts; a `422` means the payload was structurally wrong
(a type error), which is a client bug rather than a data problem.

| Method | Path                          | Auth    |
| ------ | ----------------------------- | ------- |
| POST   | `/api/v1/customers`           | API key |
| POST   | `/api/v1/orders`              | API key |
| POST   | `/api/v1/order-items`         | API key |
| POST   | `/api/v1/events`              | API key |
| POST   | `/api/v1/consent-events`      | API key |

```bash
curl -X POST http://127.0.0.1:8000/api/v1/orders \
  -H "X-API-Key: gimme_sk_..." \
  -H 'Content-Type: application/json' \
  -d '[{
        "external_id": "ORD-1001",
        "customer_external_id": "CUST-00042",
        "ordered_at": "2026-08-21T18:30:00",
        "status": "COMPLETED",
        "total_amount": 128.50
      }]'
```

```json
{
  "entity_type": "orders",
  "total_rows": 1,
  "accepted_rows": 1,
  "updated_rows": 0,
  "rejected_rows": 0,
  "duplicate_rows": 0,
  "errors": [],
  "affected_customers": 1
}
```

Posting the same `external_id` again **updates** rather than duplicating.
Ingesting a completed order triggers reactivation detection, campaign
attribution and a full intelligence refresh for that customer.

### CSV upload (session auth)

| Method | Path                                        | Purpose                          |
| ------ | ------------------------------------------- | -------------------------------- |
| POST   | `/api/v1/uploads/preview`                   | Parse and validate, write nothing |
| POST   | `/api/v1/uploads`                           | Import, returns an ingestion job  |
| GET    | `/api/v1/uploads`                           | Import history                    |
| GET    | `/api/v1/uploads/{id}/errors.csv`           | Downloadable error report         |
| GET    | `/api/v1/uploads/templates/{entity}.csv`    | Empty file with the right headers |

Both take `entity_type` as form data: `customers`, `orders`, `order_items`,
`events`, `consent_events`.

---

## Customers

| Method | Path                                     | Purpose                                    |
| ------ | ---------------------------------------- | ------------------------------------------ |
| GET    | `/api/v1/customers`                      | Paginated, filterable list                 |
| GET    | `/api/v1/customers/filters`              | Distinct values for the filter controls    |
| GET    | `/api/v1/customers/{id}`                 | Full Customer 360                          |
| POST   | `/api/v1/customers/{id}/recalculate`     | Recompute this customer's intelligence     |
| POST   | `/api/v1/customers/{id}/suppress`        | Add to the suppression list                |
| DELETE | `/api/v1/customers/{id}/suppress`        | Remove suppression                         |
| PATCH  | `/api/v1/customers/{id}/consent`         | Update consent, writing an audit trail     |

List query parameters: `search`, `lifecycle_stage` (repeatable),
`churn_risk_band` (repeatable), `rfm_segment`, `segment_id`,
`recommended_action`, `city`, `marketing_consent`, `is_suppressed`,
`min_revenue`, `max_revenue`, `min_days_since_order`, `max_days_since_order`,
`page`, `page_size`, `sort_by`, `sort_dir`.

```bash
curl "http://127.0.0.1:8000/api/v1/customers?lifecycle_stage=AT_RISK&min_revenue=500&sort_by=churn_score&sort_dir=desc" \
  -H "Authorization: Bearer $TOKEN"
```

`GET /customers/{id}` returns `profile`, `orders`, `lifecycle_history`,
`communication_events`, `messages`, `campaigns`, `segments` and `attribution`.

---

## Segments

| Method | Path                                          | Purpose                             |
| ------ | --------------------------------------------- | ----------------------------------- |
| GET    | `/api/v1/segments/fields`                     | Field catalogue for the rule builder |
| POST   | `/api/v1/segments/preview`                    | Count and sample a candidate rule   |
| GET    | `/api/v1/segments`                            | List segments                       |
| POST   | `/api/v1/segments`                            | Create                              |
| PATCH  | `/api/v1/segments/{id}`                       | Update                              |
| POST   | `/api/v1/segments/{id}/duplicate`             | Copy a built-in into an editable one |
| POST   | `/api/v1/segments/{id}/archive`               | Archive                             |
| POST   | `/api/v1/segments/{id}/refresh`               | Re-evaluate membership              |
| POST   | `/api/v1/segments/refresh-all`                | Re-evaluate every segment           |
| GET    | `/api/v1/segments/{id}/members`               | Matching customers                  |
| GET    | `/api/v1/segments/{id}/export.csv`            | Export members                      |

A rule is a nested group:

```json
{
  "op": "AND",
  "conditions": [
    { "field": "lifecycle_stage", "operator": "in", "value": ["AT_RISK", "DORMANT"] },
    { "field": "lifetime_revenue", "operator": "gte", "value": 500 },
    { "op": "OR", "conditions": [
        { "field": "churn_score", "operator": "gte", "value": 70 },
        { "field": "days_since_last_order", "operator": "gt", "value": 90 }
    ]}
  ]
}
```

Operators are per field type — `number`, `string`, `enum`, `boolean`, `date`,
`list`. `GET /segments/fields` returns which apply to each field. An invalid
rule returns `400` naming the problem.

---

## Personalisation (merge tags)

| Method | Path                                    | Purpose                              |
| ------ | --------------------------------------- | ------------------------------------ |
| GET    | `/api/v1/message-fields`                | Every merge tag, and customers to preview against |
| POST   | `/api/v1/message-fields/preview`        | Render a template as one customer would receive it |

A merge tag is written `#field_name#` (the older `{field_name}` spelling still
resolves). It is filled per recipient at send time; the stored template is
never overwritten.

```bash
curl -X POST http://127.0.0.1:8000/api/v1/message-fields/preview \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"template": "Kia ora #first_name#, your #brand#?", "customer_id": 42}'
```

```json
{
  "text": "Kia ora Aroha, your Steinlager?",
  "template": "Kia ora #first_name#, your #brand#?",
  "missing_fields": [],
  "fallbacks_used": [],
  "unknown_tags": [],
  "customer_id": 42,
  "customer_name": "Aroha Ngata",
  "characters": 31
}
```

Every tag names the column in the [upload format](data-import.md) it reads,
so a spreadsheet can be mapped without reading any code. The composer shows
it too — the Personalize menu lists each field as `Label — #tag# (column)`.

| Upload column | Merge tag | What it says |
| --- | --- | --- |
| `first_name` | `#first_name#` | Their first name. Falls back to 'there', so a greeting never reads 'Hi ,'. |
| `last_name` | `#last_name#` | Their surname. |
| `first_name + last_name` | `#full_name#` | First and last name together. |
| `email` | `#email#` | Their email address. |
| `phone` | `#phone#` | Their mobile number. |
| `city` | `#city#` | The town or city on their record. |
| `region` | `#region#` | The region on their record. |
| `postcode` | `#postcode#` | Their postcode. |
| `country` | `#country#` | Their country. |
| `signup_date` | `#signup_date#` | When they joined, in New Zealand local time. |
| `product_name` | `#product#` `#product_name#` | What they last ordered, falling back to what they order most. |
| `category` | `#category#` | The category they buy from most. |
| `brand` | `#brand#` | The brand they buy most. |
| `ordered_at` | `#last_order_date#` | When they last ordered, in New Zealand local time. |
| `total_amount` | `#last_order_amount#` | What their last order came to. |
| `order_external_id` | `#last_order_id#` | The reference on their last order, as it appears in your own system. |
| `delivery_city` | `#delivery_city#` | Where their last order was delivered, which is not always their address on file. |

| Merge tag | What it says |
| --- | --- |
| `#order_count#` | How many completed orders they have placed. |
| `#preferred_category#` | The category they buy from most, across every order. |
| `#preferred_brand#` | The brand they buy most, across every order. |
| `#preferred_order_day#` | The day of the week they usually order, learned by Smart Reorder. |
| `#preferred_order_time#` | The time of day they usually order, learned by Smart Reorder. |
| `#average_order_value#` | What they typically spend per order. |

Plus the verified brand values `#link#` `#company#` `#delivery_promise#`
`#support_phone#` `#sign_off#`, which come from Brand settings rather than
from any file.

Your column heading works as a tag in its own right where the two names
differ: `#ordered_at#`, `#total_amount#` and `#order_external_id#` resolve
the same as `#last_order_date#`, `#last_order_amount#` and `#last_order_id#`.

The rest of the upload format deliberately has no tag, and says why:

| Column | Why it has no tag |
| --- | --- |
| `customer_external_id` | Your internal id for them. Not something to say to a customer. |
| `item_external_id` | An internal line id. |
| `sku` | An internal product code. #product# is the name a customer knows. |
| `date_of_birth` | Used to verify age, never repeated back in marketing copy. |
| `age_verified` | A compliance flag, not message content. |
| `marketing_consent` | A compliance flag, not message content. |
| `email_consent` | A compliance flag, not message content. |
| `sms_consent` | A compliance flag, not message content. |
| `whatsapp_consent` | A compliance flag, not message content. |
| `acquisition_source` | How you found them. Saying it back is unsettling. |
| `preferred_channel` | Decides how a message is sent, not what it says. |
| `status` | Order state. A marketing message is not an order update. |
| `channel` | Which of your channels the order came through. |
| `currency` | Every amount is already formatted with its symbol. |
| `discount_amount` | A component of the total. #last_order_amount# is the figure a customer recognises. |
| `delivery_fee` | As above. |
| `unit_price` | Line-level pricing. Quoting a price the checkout may no longer honour is a promise this system cannot keep. |
| `line_total` | As above. |
| `quantity` | Line-level detail that rarely reads well: 'fancy another 6?' |
| `coupon_code` | Codes come from the verified list in Brand settings, never from an uploaded file. |

A test asserts that every column in the upload template is either a tag or on
that list, so adding a column to the format forces a decision rather than
leaving a silent gap.

Three things this deliberately does not do:

* **A tag cannot name anything outside that list.** `#customer.password#`
  and `{{ 7 * 7 }}` are not tags that fail — nothing evaluates them, and they
  are carried through as the plain text they are.
* **A missing figure is not invented.** `#first_name#` falls back to
  "there"; `#last_order_amount#` and `#order_count#` fall back to nothing at
  all, because "$0.00" is a statement about a customer's account.
* **An unknown tag blocks the send.** It raises the blocking compliance
  finding `UNKNOWN_MERGE_TAG`, so a campaign cannot be approved and an
  automation cannot be activated while one is present.
* **A gap no fallback can close is not sent.** A recipient whose message
  would read "You have spent over orders" is recorded as `FAILED` with the
  missing field named, and counted in the send's `missing_personalisation`.

Column names in an uploaded file are mapped to these fields on import, so a
spreadsheet with "First Name", "FirstName" or "Customer First Name" all feed
`#first_name#`, and "Product", "Item Name" or "Product Name" all feed
`#product_name#`. A CSV preview returns `column_mapping` saying how each of
your columns was read.

## Messages

| Method | Path                                    | Purpose                              |
| ------ | --------------------------------------- | ------------------------------------ |
| GET    | `/api/v1/messages/llm-status`           | Provider, model and mode             |
| GET    | `/api/v1/messages/variations`           | Available tone variations            |
| POST   | `/api/v1/messages/generate`             | Generate a grounded message          |
| GET    | `/api/v1/messages`                      | List messages                        |
| PATCH  | `/api/v1/messages/{id}`                 | Edit — always revalidates            |
| POST   | `/api/v1/messages/{id}/validate`        | Re-run validation                    |
| POST   | `/api/v1/messages/{id}/approve`         | Approve — refused if validation fails |
| POST   | `/api/v1/messages/{id}/reject`          | Reject                               |
| POST   | `/api/v1/messages/{id}/send-test`       | Send to one address                  |

```bash
curl -X POST http://127.0.0.1:8000/api/v1/messages/generate \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"customer_id": 42, "channel": "EMAIL", "objective": "REACTIVATION"}'
```

Variations: `default`, `shorter`, `warmer`, `more_personal`, `more_playful`,
`more_premium`, `remove_sales_language`.

Every message carries a `validation_result`:

```json
{
  "valid": false,
  "errors": [
    { "code": "UNVERIFIED_COUPON_CODE",
      "message": "Mentions coupon code 'MEGA50', which is not in the verified list.",
      "severity": "CRITICAL", "blocks_send": true, "excerpt": "MEGA50" }
  ],
  "warnings": []
}
```

Editing a message clears any prior approval — the edited copy must be
re-approved.

---

### Segment export

`GET /api/v1/segments/{id}/export.csv` returns the segment's current members
as CSV, in this column order:

```
external_id, first_name, last_name, email, phone, city, lifecycle_stage,
completed_orders, lifetime_revenue, days_since_last_order, churn_score,
churn_risk_band, rfm_segment, recommended_action, marketing_consent
```

`phone` is written in international form — `+642902076762`, not
`02902076762` — using the same `normalize_nz_phone` the send path uses, so
one idea of what a valid number is serves imports, sends and exports alike.
A number already international is passed through rather than converted twice,
and a number that cannot be resolved to an NZ mobile is left **blank** rather
than guessed at: a fabricated digit in a phone column is a call to a stranger.
The count of blanked numbers goes to the log; which customers they were does
not.

Exporting never rewrites the stored value. `02902076762` stays `02902076762`
on the customer record — the international form is how the number is written
down, not a correction to it.

The file carries a UTF-8 BOM so Excel opens it as UTF-8 rather than guessing
at a codepage. Note that Excel may still display a `+`-prefixed cell as a
number; the value in the file is correct, and importing the column as Text
(Data → From Text/CSV) preserves it on screen.

---

## Smart Reorder — individual reminders

| Method | Path                                             | Purpose                              |
| ------ | ------------------------------------------------ | ------------------------------------ |
| GET    | `/api/v1/smart-reorder/overview`                 | Eligible customers, accuracy, enrolled |
| GET    | `/api/v1/smart-reorder/upcoming`                 | Customers entering their ordering window |
| GET    | `/api/v1/smart-reorder/queue`                    | The individual messages waiting to send |
| GET    | `/api/v1/smart-reorder/queue/{id}`               | One customer's reminder, with its reasoning |
| POST   | `/api/v1/smart-reorder/queue/{id}/edit`          | Rewrite one customer's copy          |
| POST   | `/api/v1/smart-reorder/queue/{id}/reschedule`    | Move one customer's send time        |
| POST   | `/api/v1/smart-reorder/queue/{id}/cancel`        | Call off one reminder                |
| POST   | `/api/v1/smart-reorder/{id}/dry-run`             | What would be scheduled, writing nothing |
| POST   | `/api/v1/smart-reorder/{id}/build-queue`         | Write the messages down (`?limit=` to batch) |
| GET    | `/api/v1/smart-reorder/{id}/dashboard`           | Sent, cancelled, converted, revenue  |

The unit of scheduling is **one customer, one predicted order time, one
message** — not a campaign with a global send time. A campaign is the rule;
`scheduled_messages` holds what it produced:

```
CUSTOMER → ORDER HISTORY → ROUTINE → PREDICTED NEXT ORDER
        → INDIVIDUAL REMINDER TIME → INDIVIDUAL MESSAGE (stored, rendered)
        → FINAL ELIGIBILITY CHECK → SENT → ORDER → CONVERSION → RE-PLAN
```

A dry run before activation:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/smart-reorder/17/dry-run \
  -H "Authorization: Bearer $TOKEN"
```

```json
{
  "customers_analysed": 8,
  "messages_scheduled": 6,
  "excluded_by_reason": { "LOW_CONFIDENCE": 1, "BEYOND_HORIZON": 1 },
  "messages": [
    { "customer_name": "Sam Rangi",
      "predicted_order_at_local": "2026-09-30T19:40:00+13:00",
      "scheduled_at_local": "2026-09-30T18:00:00+13:00",
      "confidence": 100,
      "message": "Hi Sam, ready for another Corona Extra 12pk? GIMME's ready when you are." }
  ]
}
```

Nothing is written by a dry run — it is the same code path with the writes
turned off, so the counts are the rules executed rather than a second
simulation of them.

**Message statuses** are separate from the campaign's own: `DRAFT`,
`SCHEDULED`, `PROCESSING`, `SENT`, `DELIVERED`, `FAILED`, `CANCELLED`,
`SUPPRESSED`, `CONVERTED`, `EXPIRED`. A paused campaign still has thousands of
scheduled messages; "the campaign is active" says nothing about whether any
particular customer's message went out.

Three behaviours worth knowing:

* **The final check happens at send time, not at scheduling time.** A message
  written on Monday is re-checked on Wednesday against orders placed in
  between, then run through the same consent, suppression, frequency-cap and
  quiet-hours pipeline every other automation uses.
* **A customer who orders first is not messaged.** Their reminder is
  `CANCELLED` with `CUSTOMER_ALREADY_ORDERED`, which is the feature working
  rather than a failure, and is counted separately on the dashboard.
* **An order re-plans the next reminder.** The pending message is cancelled
  as `PREDICTION_SUPERSEDED` and a fresh one is written from the new history,
  on the order itself rather than on a schedule — the gap between the two is
  exactly the window in which the wrong message goes out.

---

## Campaigns

| Method | Path                                          | Purpose                               |
| ------ | --------------------------------------------- | ------------------------------------- |
| GET    | `/api/v1/campaigns/options`                   | Objectives, channels, statuses, merge tags, copy modes |
| GET    | `/api/v1/campaigns`                           | List                                  |
| POST   | `/api/v1/campaigns`                           | Create a draft                        |
| GET    | `/api/v1/campaigns/{id}`                      | Detail with rolled-up metrics         |
| PATCH  | `/api/v1/campaigns/{id}`                      | Edit — resets approval                |
| GET    | `/api/v1/campaigns/{id}/audience`             | Eligible / excluded breakdown         |
| POST   | `/api/v1/campaigns/{id}/audience/snapshot`    | Materialise recipients                |
| GET    | `/api/v1/campaigns/{id}/recipients`           | Recipient list with exclusion reasons |
| GET    | `/api/v1/campaigns/{id}/copy-preview`         | What real recipients would receive, with any unknown merge tags |
| POST   | `/api/v1/campaigns/{id}/compliance-check`     | Run and store the compliance report   |
| POST   | `/api/v1/campaigns/{id}/submit`               | Submit for approval                   |
| POST   | `/api/v1/campaigns/{id}/approve`              | Human approval                        |
| POST   | `/api/v1/campaigns/{id}/schedule`             | Schedule a send                       |
| POST   | `/api/v1/campaigns/{id}/send-test`            | Test send                             |
| POST   | `/api/v1/campaigns/{id}/run`                  | Execute                               |
| POST   | `/api/v1/campaigns/{id}/pause`                | Pause                                 |
| POST   | `/api/v1/campaigns/{id}/cancel`               | Cancel                                |

### Who writes the copy

A campaign carries `copy_mode`, and it decides what is sent:

- **`WRITTEN`** (the default) — the body is the message. Merge tags such as
  `#name#` and `#favourite_product#` are filled from each recipient's own
  record, so what was approved is what goes out.
- **`DRAFTED`** — each recipient's message is written at send time from their
  verified facts, and the body is the fallback used when a draft fails. Every
  draft passes the same grounding and compliance checks as hand-written copy.

It lives on the campaign rather than on the send call because it decides what
the person approving is approving. Changing it resets the campaign to draft and
clears the approval, the same as editing the body. `POST /run` no longer accepts
`generate_per_customer`; a request carrying it is refused with a `400` naming
`copy_mode`, rather than the flag being ignored.

`GET /campaigns/{id}/copy-preview?count=3` returns what real recipients would
receive, produced by the same calls the send makes and persisting nothing:

```json
{
  "copy_mode": "DRAFTED",
  "eligible_count": 48,
  "samples": [
    { "customer_id": 91, "full_name": "Kiri Zhang", "subject": "",
      "body": "Hi Kiri, it's been 142 days since we last saw an order…",
      "validation_failed": false }
  ]
}
```

`validation_failed` marks a draft that fails grounding. At send time that
recipient is skipped rather than sent the fallback, which is why the preview
shows it as such rather than showing the fallback.

### Sending is gated three ways

All three must pass, and they are independent:

1. **Status** — the campaign must be `APPROVED` or `SCHEDULED`, which only a
   human action produces.
2. **Compliance** — the stored report must have no blocking findings.
3. **Per-recipient eligibility** — re-checked at send time, so consent revoked
   after the preview is honoured.

Attempting to skip a step returns `400` with the reason:

```json
{ "detail": "Campaign must be approved before sending (current status: DRAFT)." }
```

Audience response:

```json
{
  "audience_size": 65,
  "eligible_count": 48,
  "excluded_count": 17,
  "excluded_by_reason": { "EXCLUDED_NO_CONSENT": 12, "EXCLUDED_AGE": 5 },
  "exclusion_samples": { "EXCLUDED_AGE": [{ "id": 91, "full_name": "…",
      "reason": "Age has not been verified; alcohol marketing requires verified age." }] },
  "sample_recipients": [ … ]
}
```

---

## Automations

Recurring campaign types built on the existing TNZ integration. See
[`automations.md`](automations.md) for the rules behind them.

| Method | Path                                            | Purpose                                            |
| ------ | ----------------------------------------------- | -------------------------------------------------- |
| GET    | `/api/v1/automations`                           | List; filter by `kind` and `status`                |
| POST   | `/api/v1/automations`                           | Create — always as a draft                         |
| GET    | `/api/v1/automations/{id}`                      | One automation with its steps                      |
| PATCH  | `/api/v1/automations/{id}`                      | Update                                             |
| PUT    | `/api/v1/automations/{id}/steps`                | Replace a sequence's steps                         |
| DELETE | `/api/v1/automations/{id}`                      | Delete (409 while active)                          |
| POST   | `/api/v1/automations/{id}/approve`              | Record human approval                              |
| POST   | `/api/v1/automations/{id}/activate`             | Switch on (409 without approval)                   |
| POST   | `/api/v1/automations/{id}/pause`                | Pause                                              |
| POST   | `/api/v1/automations/{id}/resume`               | Resume                                             |
| POST   | `/api/v1/automations/{id}/preview`              | **Dry run** — nothing is sent                      |
| POST   | `/api/v1/automations/{id}/run`                  | Run now, outside the schedule                      |
| GET    | `/api/v1/automations/{id}/audience`             | Audience resolved live                             |
| GET    | `/api/v1/automations/{id}/stats`                | Delivery and enrollment counts                     |
| GET    | `/api/v1/automations/{id}/sends`                | The delivery ledger                                |
| GET    | `/api/v1/automations/{id}/enrollments`          | Per-customer enrollment state                      |
| POST   | `/api/v1/automations/{id}/enroll`               | Update enrollments without sending                 |
| POST   | `/api/v1/automations/{id}/refresh-patterns`     | Recompute nudge order patterns                     |

Create a weekly cohort send:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/automations \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{
        "name": "Monday win-back",
        "kind": "COHORT_BULK",
        "channel": "SMS",
        "segment_id": 9,
        "recurrence": "WEEKLY",
        "recurrence_day": 0,
        "send_time_local": "10:00"
      }'
```

Create a three-step sequence. Offsets are days from each customer's own
enrollment, not calendar dates:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/automations \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{
        "name": "Second-order series",
        "kind": "SEQUENCE",
        "segment_id": 2,
        "enrollment_mode": "ROLLING",
        "stop_on_order": true,
        "steps": [
          {"name": "Day 0",  "offset_days": 0,
           "message_template": "Hi {first_name}, thanks for your first order. Reply STOP to opt out."},
          {"name": "Day 7",  "offset_days": 7,
           "message_template": "Hi {first_name}, ready for round two? Reply STOP to opt out."},
          {"name": "Day 14", "offset_days": 14,
           "message_template": "Hi {first_name}, we are here whenever you need us. Reply STOP to opt out."}
        ]
      }'
```

Dry run — works on a draft, sends nothing:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/automations/3/preview \
  -H "Authorization: Bearer $TOKEN"
```

```json
{
  "dry_run": true,
  "candidates": 108,
  "previewed": 35,
  "skipped": 73,
  "sent": 0,
  "skips_by_reason": { "NO_CONSENT": 65, "AGE_NOT_VERIFIED": 8 },
  "is_mock": true,
  "recipients": [
    {
      "customer_id": 412,
      "customer_name": "Dylan Ngata",
      "to": "+642***021",
      "status": "PREVIEW",
      "scheduled_for_local": "2026-08-24T10:00:00+12:00",
      "local_date": "2026-08-24",
      "skip_reason": null,
      "body": "Hi Dylan, it's been a while. We're still delivering to Christchurch…"
    },
    {
      "customer_id": 418,
      "customer_name": "Mere Tahana",
      "status": "SKIPPED",
      "skip_reason": "NO_CONSENT",
      "skip_detail": "Customer has not given marketing consent."
    }
  ]
}
```

A live run needs `status = ACTIVE` and, when `require_approval` is set,
a recorded approval — otherwise it returns `409`. Viewers get `403` on
approve, run and delete.

---

## Analytics

| Method | Path                              | Returns                                       |
| ------ | --------------------------------- | --------------------------------------------- |
| GET    | `/api/v1/analytics/overview`      | Headline retention and revenue figures        |
| GET    | `/api/v1/analytics/customers`     | Growth, mix, RFM and value distributions      |
| GET    | `/api/v1/analytics/churn`         | Risk bands, reasons, movement, save list      |
| GET    | `/api/v1/analytics/campaigns`     | Delivery, engagement, conversion, revenue     |
| GET    | `/api/v1/analytics/cohorts`       | Monthly cohorts, months 0-6                   |
| GET    | `/api/v1/analytics/activity`      | Recent conversions and sends                  |
| POST   | `/api/v1/analytics/recalculate`   | Recompute all intelligence and segments       |

Every figure is computed from the database at request time.

---

## Brand and compliance

| Method | Path                                       | Purpose                              |
| ------ | ------------------------------------------ | ------------------------------------ |
| GET    | `/api/v1/brand`                            | Brand settings                       |
| PUT    | `/api/v1/brand`                            | Update — changes message grounding   |
| GET    | `/api/v1/compliance/rules`                 | Rules and their enabled state        |
| PATCH  | `/api/v1/compliance/rules/{id}`            | Enable or disable a rule (audited)   |
| GET    | `/api/v1/compliance/config`                | Live enforcement configuration       |
| GET    | `/api/v1/compliance/prohibited-claims`     | Built-in claim categories            |
| POST   | `/api/v1/compliance/check-content`         | Test arbitrary copy against the rules |

---

## Integrations and webhooks

| Method | Path                                              | Purpose                      |
| ------ | ------------------------------------------------- | ---------------------------- |
| GET    | `/api/v1/integrations`                            | Providers, masked credentials |
| PATCH  | `/api/v1/integrations/{id}`                       | Update mode and credentials  |
| POST   | `/api/v1/integrations/{id}/test-connection`       | Validate credentials         |
| POST   | `/api/v1/integrations/{id}/test-message`          | Send a test                  |
| GET    | `/api/v1/integrations/whatsapp-profiles`          | Supported WhatsApp providers |
| POST   | `/api/v1/webhooks/{provider}`                     | Receive delivery events      |

Webhook endpoints are unauthenticated by design — providers post from their own
infrastructure — so they only ever record events for a message they already
know. An unrecognised provider message ID is counted and ignored rather than
creating a stray record.

---

## Journeys

| Method | Path                                     | Purpose                     |
| ------ | ---------------------------------------- | --------------------------- |
| GET    | `/api/v1/journeys/catalog`               | Available triggers and steps |
| GET/POST | `/api/v1/journeys`                     | List / create               |
| PATCH  | `/api/v1/journeys/{id}`                  | Update                      |
| POST   | `/api/v1/journeys/{id}/activate`         | Activate                    |
| POST   | `/api/v1/journeys/{id}/pause`            | Pause                       |
| POST   | `/api/v1/journeys/{id}/enrol`            | Enrol eligible customers    |
| POST   | `/api/v1/journeys/{id}/run`              | Advance active customers    |
| GET    | `/api/v1/journeys/{id}/executions`       | Execution log               |

---

## System

| Method | Path                                  | Purpose                              |
| ------ | ------------------------------------- | ------------------------------------ |
| GET    | `/health`                             | Liveness — the only public endpoint  |
| GET    | `/api/v1/system/status`               | Mode, providers, scheduler, volumes  |
| GET    | `/api/v1/system/audit-log`            | Audit trail (admin)                  |
| GET    | `/api/v1/system/logs`                 | System log (admin)                   |
| POST   | `/api/v1/system/seed-demo-data`        | Regenerate demo data (admin, destructive) |

---

## Errors

| Status | Meaning                                                             |
| ------ | ------------------------------------------------------------------- |
| 400    | The request is valid but the operation is not allowed in this state  |
| 401    | Missing, invalid or expired credentials                              |
| 403    | Authenticated but not permitted (role, or read-only account)         |
| 404    | No such resource                                                     |
| 413    | Upload exceeds `MAX_UPLOAD_BYTES`                                    |
| 422    | Request body failed schema validation                                |

`422` responses carry a per-field breakdown alongside the summary:

```json
{
  "detail": "0.total_amount: Input should be greater than or equal to 0",
  "errors": [{ "field": "0.total_amount", "message": "Input should be greater than or equal to 0" }]
}
```
