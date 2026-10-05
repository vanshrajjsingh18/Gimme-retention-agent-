# Sandbox (§41)

`src/backend/sandbox/` is a complete in-memory GIMME behind the same ports production will use. Fixtures are named after the scenario they exercise. Prices are illustrative, not GIMME's price list.

## Customers

| Customer | Eligibility | Payment | Linked platform users | Usual order |
| --- | --- | --- | --- | --- |
| `TEST_CUSTOMER` (Sam) | `ELIGIBLE` | `PM_SUCCESS` (default), `PM_DECLINED`, `PM_TIMEOUT` | `apple-user-test`, `google-user-test` | 2 × Heineken 12-pack, 1 × Cloudy Bay SB |
| `TEST_CUSTOMER_NO_PAYMENT` (Alex) | `ELIGIBLE` | none → `PAYMENT_REQUIRED` | `apple-user-nopay` | Steinlager 12-pack |
| `TEST_CUSTOMER_AGE_REVIEW` (Jordan) | `AGE_VERIFICATION_REQUIRED` (soft drinks only) | `PM_AR` | `apple-user-agereview` | Corona 12-pack |
| `TEST_CUSTOMER_RESTRICTED` (Riley) | `ACCOUNT_RESTRICTED` | `PM_RS` | `apple-user-restricted` | Heineken 12-pack |

Agent platforms (`MCP_AGENT`, `WEB_AGENT`, `SANDBOX`) treat the OAuth grant itself as the link.

## Products

| SKU | Scenario |
| --- | --- |
| `AVAILABLE_PRODUCT` | Always in stock |
| `OUT_OF_STOCK_PRODUCT` | Stock 0 → `PRODUCT_UNAVAILABLE` with alternatives |
| `PRICE_CHANGED_PRODUCT` | $20.00 on its first quote, then $27.50 → `PRICE_CHANGED` at placement |
| `AGE_RESTRICTED_PRODUCT` | A spirit, blocked for `TEST_CUSTOMER_AGE_REVIEW` |
| `UNKNOWN_PRODUCT` | Doesn't exist → `PRODUCT_NOT_FOUND` |

There is also a realistic catalogue: Heineken 6, 12 and 24-packs, Heineken 0.0, Corona, Steinlager, Beck's, Warsteiner, two NZ wines, Coca-Cola and ice.

## Payments

| Method / token | Scenario |
| --- | --- |
| `PM_SUCCESS` | `PAYMENT_SUCCESS` |
| `PM_DECLINED`, wallet `tok_declined` | `PAYMENT_DECLINED` |
| `PM_TIMEOUT` | `PAYMENT_TIMEOUT`. The authorization happened at the provider, and a retry with the same idempotency key learns it (`PAYMENT_RETRY`) |
| any `place_order` repeat | `PAYMENT_DUPLICATE`, answered from the idempotency store |

`SandboxBackend.paymentLedger()` lists every provider-side authorization, so tests can assert "charged exactly once".

## Delivery

| Address | Scenario |
| --- | --- |
| `ADDR_HOME`, `ADDR_WORK` | `DELIVERY_AVAILABLE` (45 and 35 minutes) |
| `ADDR_OUTSIDE_ZONE` | `OUTSIDE_ZONE` → `OUTSIDE_SERVICE_AREA` |
| `ADDR_NO_CAPACITY` | `DELIVERY_UNAVAILABLE` |
| any address, clock outside 07:00–23:00 Auckland | `STORE_CLOSED` |

## Promotions

`VOICE10` takes $10 off a subtotal of $50 or more.

## Controls for tests

```ts
backend.setStock(sku, n);        backend.setPrice(sku, cents);
backend.setOrderStatus(id, "OUT_FOR_DELIVERY");   // emits order.* events
backend.now = () => new Date(…); // trading hours
```

## Test suites

| File | Covers |
| --- | --- |
| `test/mvp-flow.test.ts` | §47 MVP end to end: usual → confirm → pay → place → status; funnel; audit |
| `test/security.test.ts` | Every §38 threat |
| `test/sandbox-scenarios.test.ts` | Every §41 customer, product, payment and delivery scenario; reorder, cancel, receipt, idempotency |
| `test/resolver.test.ts` | "A dozen Heinekens", clarification, alcohol-free handling, nationality matching |
| `test/mcp.test.ts` | MCP client: schemas, metadata, elicitation confirm and decline, hosted confirmation, errors |
| `test/mcp-http.test.ts` | MCP over Streamable HTTP: OAuth challenge, end-to-end order, session pinning |
| `test/http-api.test.ts` | REST API, headers, versioning, scopes, hosted page, OMS events, metrics |
| `test/adapters.test.ts` | Siri and Google conversations through the adapters |
| `test/webhooks-and-state.test.ts` | Webhook signing and retry; state machines; production config guard |
| `test/specs.test.ts` | Generated OpenAPI and MCP specs are current and complete |
