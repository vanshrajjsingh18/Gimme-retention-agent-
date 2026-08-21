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

## Campaigns

| Method | Path                                          | Purpose                               |
| ------ | --------------------------------------------- | ------------------------------------- |
| GET    | `/api/v1/campaigns/options`                   | Objectives, channels, statuses        |
| GET    | `/api/v1/campaigns`                           | List                                  |
| POST   | `/api/v1/campaigns`                           | Create a draft                        |
| GET    | `/api/v1/campaigns/{id}`                      | Detail with rolled-up metrics         |
| PATCH  | `/api/v1/campaigns/{id}`                      | Edit — resets approval                |
| GET    | `/api/v1/campaigns/{id}/audience`             | Eligible / excluded breakdown         |
| POST   | `/api/v1/campaigns/{id}/audience/snapshot`    | Materialise recipients                |
| GET    | `/api/v1/campaigns/{id}/recipients`           | Recipient list with exclusion reasons |
| POST   | `/api/v1/campaigns/{id}/compliance-check`     | Run and store the compliance report   |
| POST   | `/api/v1/campaigns/{id}/submit`               | Submit for approval                   |
| POST   | `/api/v1/campaigns/{id}/approve`              | Human approval                        |
| POST   | `/api/v1/campaigns/{id}/schedule`             | Schedule a send                       |
| POST   | `/api/v1/campaigns/{id}/send-test`            | Test send                             |
| POST   | `/api/v1/campaigns/{id}/run`                  | Execute                               |
| POST   | `/api/v1/campaigns/{id}/pause`                | Pause                                 |
| POST   | `/api/v1/campaigns/{id}/cancel`               | Cancel                                |

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
