# Architecture

GIMME Voice Commerce is one deterministic commerce engine with several ways in. Siri, Google and AI agents are interfaces to it. None of them contains ordering logic.

```
              Siri / Apple Intelligence        Google surfaces           AI agents (any MCP client)
                       |                              |                              |
            App Intents in the iOS app      Android app / account-linked       MCP client
            (adapters/apple/*.swift)         surface                           |
                       |                              |                              |
        POST /v1/voice/adapters/apple    POST /v1/voice/adapters/google       /mcp  (Streamable HTTP,
                       |                              |                       OAuth bearer; or stdio)
                       +---------------+--------------+                              |
                                       |                                             |
                          VoiceDialog (src/adapters/dialog.ts)            MCP server (src/mcp/)
                          turn-by-turn conversation state                 17 tools, strict schemas
                                       |                                             |
                                       +---------------- REST Voice API -------------+
                                                     /v1/voice/*  (src/http/)
                                                              |
                                          VoiceCommerceService  (src/core/)
                          sessions · resolution · order intents · confirmation ·
                          payment authorization · final validation · order creation
                                                              |
                 +--------------+--------------+--------------+--------------+--------------+
                 |              |              |              |              |              |
             Customers       Catalog       Inventory      Delivery       Pricing     Orders · Payments
                 +--------------+--------- GimmeBackend ports (src/backend/ports.ts) ------+
                                                              |
                                  GIMME core platform (sandbox: src/backend/sandbox/)
```

Cross-cutting pieces sit beside the service: the hash-chained **audit log**, the **event bus** and signed **webhooks**, **metrics** and the voice funnel, the **purchase authorizer** that signs confirmations, and the **state store** for sessions, intents and idempotency records.

## Layers and what each one may do

| Layer | Code | Holds rules? | Talks to |
| --- | --- | --- | --- |
| Platform code (Swift App Intents, Android) | `adapters/apple/` | No. It runs the platform's own confirmation and authentication UI. | Apple adapter endpoint |
| Platform adapters | `src/adapters/apple.ts`, `google.ts` | No. They translate request and response shapes. | `VoiceDialog` |
| Dialog | `src/adapters/dialog.ts` | No. It keeps conversational context (the open clarifying question, the intent awaiting a yes). | Service |
| MCP server | `src/mcp/` | No. Each tool calls one service method. | Service |
| REST Voice API | `src/http/` | No. It validates request bodies against the contract schemas. | Service |
| **Service** | `src/core/voice-commerce-service.ts` | **Yes. Every ordering rule is here.** | Backend ports, store, audit, events |
| Backend ports | `src/backend/ports.ts` | No. GIMME's systems own the data. | GIMME core |

A language model can decide which tool to call and with which arguments, but it cannot skip a check. `create_order_intent` re-runs product, stock, delivery, compliance, payment-method and pricing checks itself. `place_order` re-runs eligibility, delivery, stock (by reserving it) and price. It doesn't matter which earlier tools the model did or didn't call (§19, §33).

## Contracts are code

`src/contracts/schemas.ts` (zod) is the only definition of every input and output. The following are all derived from it:

- MCP tool `inputSchema` / `outputSchema` (validated by the MCP SDK on every call)
- REST request validation
- `docs/openapi.json` and `docs/mcp-tools.{json,md}` (`npm run docs:generate`; a test fails if they go stale)

## Object models

### Voice session (§28)

| Field | Notes |
| --- | --- |
| `session_id` | `VS-<uuid>` |
| `customer_id` | Always the OAuth token's subject. Never taken from an argument. |
| `platform`, `platform_user_id` | The platform user must be linked to the customer (identity confusion, §38). |
| `client_id` | The OAuth client that opened the session. Any other client is refused (session hijacking). |
| `conversation_id`, `platform_session_id` | Used in idempotency keys and for correlation. |
| `stage`, `stage_history` | Position in the §19 state machine, with timestamps. |
| `active_order_intent_id` | At most one live intent per session. A new basket supersedes the old one. |
| `expires_at` | 15 minutes idle, sliding, hard-capped at 1 hour. |

There is no payment information on a session.

### Order intent (§29)

`OI-<uuid>`. It holds priced lines from the authoritative quote, `delivery_address_id`, `payment_method_id` (a saved-method reference or `WALLET:APPLE_PAY`), the `quote_id`, subtotal, fees, discount, total and currency, the ETA, delivery requirements (e.g. `ID_CHECK_ON_DELIVERY`), `status`, `authorization_status`, `payment_status`, and `expires_at` (5 minutes). Amounts are integer cents internally and decimal NZD at the API edge.

### Purchase authorization

A signed JWT (HS256, server-only key, `typ: gimme-purchase-authorization+jwt`), valid for 2 minutes and usable once. Its claims carry a SHA-256 digest over the canonical JSON of {intent id, customer, session, platform, lines, total, currency, address, payment method, quote id}, plus the confirmation evidence (method, `device_authenticated`, platform confirmation id, time). See [security.md](security.md).

### Payment authorization record

`PAYAUTH-<uuid>`. It links the intent, the provider `payment_reference`, the amount and currency, and a status (`AUTHORIZED → CAPTURED | VOIDED`). The provider idempotency key is `payauth:<order_intent_id>`, so one intent can never produce two provider authorizations.

### Order

The order is owned by GIMME's OMS. Voice keeps only the link (`order.source = { channel: VOICE, platform, order_intent_id }`).

## State machines

All three are transition tables in `src/domain/state-machine.ts`. An illegal transition throws.

**Voice conversation (§19).** Each stage advances only to the next one. Two non-linear moves are allowed once the customer is identified: starting a new basket (back to `PRODUCT_RESOLUTION`) and asking about an order (`DELIVERY_TRACKING`).

```
DISCOVERY → CUSTOMER_IDENTIFICATION → PRODUCT_RESOLUTION → INVENTORY_VALIDATION → DELIVERY_VALIDATION
→ COMPLIANCE_VALIDATION → PRICE_CALCULATION → ORDER_INTENT_CREATED → CUSTOMER_CONFIRMATION
→ PAYMENT_AUTHORIZATION → FINAL_VALIDATION → ORDER_CREATION → ORDER_CONFIRMED → DELIVERY_TRACKING
```

**Order intent.**

```
AWAITING_CONFIRMATION ──confirm──▶ CONFIRMED ──authorize payment──▶ PAYMENT_AUTHORIZED ──place──▶ ORDER_PLACED
        │                              │                                   │
        └──────────────┬───────────────┴───────────────────────────────────┘
                       ▼
     EXPIRED (5 min) · CANCELLED (customer said no) · SUPERSEDED (new basket, or price changed) · FAILED
```

There is no path to `PAYMENT_AUTHORIZED` that skips `CONFIRMED`, and no path to `ORDER_PLACED` that skips `PAYMENT_AUTHORIZED`. All five terminal states are final.

**Order (OMS, §15).** `CREATED → PAYMENT_AUTHORIZED → CONFIRMED → ACCEPTED → PREPARING → DISPATCHED → OUT_FOR_DELIVERY → DELIVERED`. An order can go to `CANCELLED` up to `PREPARING`, to `FAILED` from any pre-delivery state, and to `REFUNDED` after `DELIVERED`, `CANCELLED` or `FAILED`. Whether a cancellation is allowed is decided by the OMS, not by this service.

## The order path, end to end

```
identify_customer          token sub → customer; platform user must be linked; session pinned to OAuth client
resolve_items              "my usual" / "a dozen Heinekens" → SKUs, or one clarifying question
create_order_intent        products → compliance → stock → delivery → payment method → authoritative quote
                           → intent AWAITING_CONFIRMATION + exact summary/amount to put to the customer
request_authorization /    customer confirms (Siri requestConfirmation, MCP elicitation, hosted page);
  POST …/confirm           confirmed total must equal intent total to the cent; device auth where required
                           → signed single-use purchase authorization bound to the intent digest
authorize_payment          verify signature + digest + unused → provider authorize (idempotent per intent)
place_order                idempotency key → per-intent lock → re-check eligibility, delivery
                           → reserve stock atomically → re-quote (price changed? void, release, replacement intent)
                           → create order in OMS → capture → ORDER_PLACED
get_order_status           OMS status + ETA, spoken
```

## Versioning (§45)

| Contract | Version | Where |
| --- | --- | --- |
| Handshake protocol | 1.0 | `handshake.protocol_version` in the session response |
| MCP tool contracts | 1.0 per tool | `_meta["gimme/contract_version"]` on each tool |
| Voice API | 1.0 | `/v1/` path; `GIMME-API-Version` header (an unsupported value returns 400) |
| GIMME core APIs | any | Hidden behind `src/backend/ports.ts`. Adapters never see them. |

A breaking change to a tool ships as a new tool name or a contract version bump. A breaking change to the API ships as `/v2`. A change in GIMME's internal order API changes one port implementation and nothing above it.

## Observability (§40)

- **Logs.** One JSON line per HTTP request and per error, each with `request_id` and `correlation_id`. Clients may send `X-Request-Id` and `X-Correlation-Id`; both are echoed back on every response.
- **Metrics** at `/metrics` (Prometheus):
  - `gimme_operations_total{op,outcome,channel}`
  - `gimme_operation_latency_ms{op}`
  - `gimme_mcp_tool_calls_total{tool,outcome}`
  - `gimme_mcp_tool_latency_ms{tool}`
  - `gimme_http_latency_ms{method,route,status}`
  - `gimme_payment_latency_ms{op}`
  - `gimme_payment_failures_total{reason}`
  - `gimme_duplicate_requests_prevented_total{op}`
  - `gimme_orders_total{platform,source}`
  - `gimme_events_total{type}`
- **Funnel.** `gimme_voice_funnel_total{stage,platform}` is counted once per session per stage. The stages are `session_started → product_resolved → cart_priced → intent_created → confirmation_requested → confirmed → payment_authorized → order_placed`, plus `confirmation_declined` and `abandoned`.

Suggested dashboard panels:

| Panel | Query |
| --- | --- |
| Conversion by stage | `sum by (stage) (increase(gimme_voice_funnel_total[1d]))`, shown as a funnel |
| Voice abandonment | `1 - order_placed / intent_created` |
| Failed authorization rate | `gimme_operations_total{op="confirm_order_intent",outcome!="success"}` over all confirmations |
| Failed order rate | `gimme_operations_total{op="place_order",outcome!="success"}` |
| Duplicate prevention rate | `gimme_duplicate_requests_prevented_total` over `place_order` calls |
| Latency | p95 of `gimme_mcp_tool_latency_ms` and `gimme_payment_latency_ms` |
