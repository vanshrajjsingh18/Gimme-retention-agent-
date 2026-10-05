# GIMME Voice API v1

The stable handshake point for every adapter (§27, §44). Full schemas are in [`openapi.json`](openapi.json). The MCP tools in [`mcp-tools.md`](mcp-tools.md) map one-to-one onto the same service methods.

## Conventions

| Header | Direction | Purpose |
| --- | --- | --- |
| `Authorization: Bearer <token>` | request | An OAuth access token from GIMME's authorization server. Required on every `/v1/voice/*` route except the hosted confirmation page. |
| `GIMME-API-Version: 1.0` | both | Optional on requests. An unsupported version returns `400`. Always set on responses. |
| `X-Request-Id` | both | Echoed back. Generated if absent. |
| `X-Correlation-Id` | both | Ties together a multi-turn conversation (e.g. the Siri interaction id). Echoed back and written to logs and the audit trail. |
| `Idempotency-Key` | request | Accepted by `POST /order-intents`, `/orders` and `/orders/reorder`. **Required** for `/orders` (in the header or body). |

**Amounts** are decimal NZD with two places. **Errors** always return this body:

```json
{
  "success": false,
  "error_code": "PRICE_CHANGED",
  "message": "The price has changed to $36.47. Do you want me to continue?",
  "recoverable": true,
  "suggested_action": "RECONFIRM_PRICE",
  "details": { "previous_total": 28.97, "new_total": 36.47, "replacement_order_intent_id": "OI-…" },
  "request_id": "req_…"
}
```

`message` is customer-safe and short enough to speak. All 34 error codes, with their HTTP status and suggested action, are listed in [`mcp-tools.md#error-codes`](mcp-tools.md#error-codes).

## Handshake (§44)

`POST /v1/voice/sessions` with `{ platform, platform_user_id, platform_session_id?, conversation_id? }` establishes the session. The response:

```json
{
  "session_id": "VS-…",
  "customer_id": "GIMME12345",
  "authenticated": true,
  "customer_name": "Sam",
  "default_address": { "id": "ADDR_HOME", "summary": "Home, Ponsonby" },
  "eligible_to_order": true,
  "eligibility": "ELIGIBLE",
  "expires_at": "…",
  "handshake": {
    "protocol_version": "1.0", "mcp_version": "1.0", "voice_api_version": "1.0", "capability_version": "1.0",
    "platform": "APPLE", "session_id": "VS-…", "customer_id": "GIMME12345", "client_id": "gimme-ios-app",
    "scopes": ["gimme.customer.read", "…"], "request_id": "req_…", "conversation_id": "…"
  }
}
```

This establishes, in order:

1. platform identity (`platform`, `platform_user_id`)
2. customer identity (from the token; the platform user must be linked)
3. session identity
4. the OAuth client and its scopes
5. conversation id, request id, and the protocol, MCP, API and capability versions

## Endpoints

| Method and path | Purpose | Scope |
| --- | --- | --- |
| `POST /v1/voice/sessions` | Handshake / identify customer | `customer.read` |
| `GET /v1/voice/sessions/{id}` | Read a session | — |
| `POST /v1/voice/resolve` | `{type: USUAL \| LAST_ORDER \| ITEMS}` → SKUs or one clarifying question | `products.read`, `customer.read` |
| `POST /v1/voice/products/search` | Catalogue search | `products.read` |
| `GET /v1/voice/preferences?session_id=` | Usual order, recent orders, address and payment labels | `customer.read` |
| `POST /v1/voice/inventory/check` | Live stock, with alternatives | `inventory.read` |
| `POST /v1/voice/delivery/validate` | Delivery eligibility | `inventory.read` |
| `POST /v1/voice/customer/validate` | Purchase eligibility | `customer.read` |
| `POST /v1/voice/cart/calculate` | Authoritative quote (informational) | `cart.create` |
| `POST /v1/voice/order-intents` | Validate and hold for confirmation | `cart.create` |
| `GET /v1/voice/order-intents/{id}` | Intent state | `cart.create` |
| `POST /v1/voice/order-intents/{id}/confirmation-request` | Single-use hosted confirmation link | `cart.create` |
| `POST /v1/voice/order-intents/{id}/confirm` | Record the customer's decision (first-party only) | `order.confirm` |
| `POST /v1/voice/order-intents/{id}/authorize-payment` | Tokenised payment authorization | `payment.authorize` |
| `POST /v1/voice/orders` | Place the order (idempotent) | `order.create` |
| `POST /v1/voice/orders/reorder` | Rebuild a past order as a new intent | `order.read`, `cart.create` |
| `GET /v1/voice/orders/latest`, `GET /v1/voice/orders/{id}` | Status and ETA | `order.read` |
| `GET /v1/voice/orders/{id}/receipt` | Safe receipt | `order.read` |
| `POST /v1/voice/orders/{id}/cancel` | Request cancellation | `order.cancel` |
| `POST /v1/voice/dialog/turn` | Platform-neutral dialog (below) | per the operation it runs |
| `POST /v1/voice/adapters/apple/intents` | Apple adapter ([apple-integration.md](apple-integration.md)) | per operation |
| `POST /v1/voice/adapters/google/fulfillment` | Google adapter ([google-integration.md](google-integration.md)) | per operation |
| `GET/POST /v1/voice/confirm/{id}?nonce=` | GIMME-hosted confirmation page | nonce |
| `POST /v1/internal/order-events` | OMS → voice status events (HMAC) | internal secret |
| `/mcp` | MCP Streamable HTTP | bearer |
| `GET /.well-known/oauth-protected-resource[/mcp]` | RFC 9728 metadata | — |
| `GET /health`, `GET /metrics` | Operations | — (keep on an internal network) |

## Idempotency (§21)

- **`POST /orders`.** The key is required. Recommended form: `<platform>:<customer_id>:<conversation_id>:<order_intent_id>`.
  - A repeat with the same key and same body returns the original response with `idempotent_replay: true`.
  - The same key with a different body returns `409 IDEMPOTENCY_CONFLICT`.
  - A different key for an intent that has already been placed returns `409 ORDER_ALREADY_CREATED`, with the existing `order_id` in `details`.
  - Records are kept for 24 hours.
- **`POST /order-intents` and `/orders/reorder`.** The key is optional. A repeat returns the same intent.
- **Payment authorization** is idempotent per intent, without a key: a retry returns the same authorization. After a `PAYMENT_TIMEOUT`, retry with the same request; the provider is asked the same idempotent question.

Outcomes that can be retried (timeouts, unknown errors) are not stored against the key, so a retry with the same key re-runs. Final outcomes are stored.

## Dialog turn (`POST /v1/voice/dialog/turn`)

This is for thin voice clients that would rather not drive the state machine themselves.

```json
{ "platform": "GOOGLE", "platform_user_id": "…", "conversation_id": "…", "session_id": "…?",
  "intent": "ORDER_USUAL | ORDER_ITEMS | REORDER_LAST | SELECT_OPTION | CONFIRM | DECLINE | ORDER_STATUS | CANCEL_ORDER | RECEIPT",
  "items": [{ "query": "Heineken", "quantity": 12, "unit": "UNIT" }],
  "option_sku": "HEI6PK",
  "confirmation": { "confirmed_total": 28.96, "device_authenticated": true, "method": "ANDROID_APP_CONFIRMATION" },
  "wallet_token": "…", "order_id": "…" }
```

The response is `{ session_id, speech, expect, end_conversation, confirmation?, order_intent_id?, options?, order?, error? }`. `expect` tells the platform what to do next:

| `expect` | Platform should |
| --- | --- |
| `NONE` | Speak and finish. |
| `SELECTION` | Offer `options` and send `SELECT_OPTION`. |
| `CONFIRMATION` | Run the native purchase confirmation for `confirmation.amount`, then send `CONFIRM` (or `DECLINE`). |
| `DEVICE_CONFIRMATION` | Ask for unlock or biometric. |
| `APP_HANDOFF` | Open the GIMME app (age verification, add a card, link the account). |

A `CONFIRM` turn confirms, authorizes payment and places the order. If the price changed, the response is `expect: CONFIRMATION` with the new amount and the replacement intent.

## Webhooks (§30)

External subscribers, normally the adapter backends, are configured with `WEBHOOK_SUBSCRIBERS='[{"url":"https://…","secret":"…","events":["order.*"]}]'`.

**Events:**

- `order_intent.created`, `order_intent.confirmed`, `order_intent.superseded`
- `payment.authorized`, `payment.failed`
- `order.created`, `order.confirmed`, `order.accepted`, `order.preparing`, `order.dispatched`, `order.out_for_delivery`, `order.delivered`, `order.cancelled`, `order.refunded`, `order.failed`

```
POST <subscriber url>
Content-Type: application/json
GIMME-Event-Id: EVT-…
GIMME-Event-Type: order.out_for_delivery
GIMME-Signature: t=1791234567,v1=<hex HMAC-SHA256(secret, "1791234567.<raw body>")>

{ "id": "EVT-…", "type": "order.out_for_delivery", "created_at": "…", "api_version": "1.0",
  "data": { "customer_id": "…", "order_id": "GIMME-123456", "order_intent_id": "OI-…", "platform": "APPLE",
            "status": "OUT_FOR_DELIVERY", "total": 96.5, "currency": "NZD", "estimated_minutes": 12 } }
```

**Delivery** is at least once:

- Retried with exponential backoff (1s, 2s, 4s, … capped at 5 minutes, 6 attempts by default) on network errors, 5xx, 408 and 429.
- Any other 4xx is not retried.

**Receivers must:**

- verify the HMAC over the raw body
- compare it in constant time
- reject timestamps more than 5 minutes old
- de-duplicate on `GIMME-Event-Id`

**Inbound from the OMS.** GIMME's OMS reports status changes to `POST /v1/internal/order-events` as `{ order, previous_status }`, signed the same way with `INTERNAL_EVENTS_SECRET`.
