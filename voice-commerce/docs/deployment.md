# Deployment

## Run the sandbox

```bash
cd voice-commerce
npm ci
npm test               # full suite
npm run demo           # prints example Siri/Google conversations from real output
npm run dev            # http://localhost:8787 — prints two sandbox tokens on start
npm run mcp:stdio      # MCP over stdio for desktop MCP clients (sandbox TEST_CUSTOMER)
```

To connect an MCP client over HTTP, point it at `http://localhost:8787/mcp` with `Authorization: Bearer <agent token printed at start-up>`.

Docker:

```bash
docker build -t gimme-voice-commerce voice-commerce
docker run -p 8787:8787 gimme-voice-commerce
```

## Configuration

| Variable | Default (sandbox) | Production |
| --- | --- | --- |
| `VOICE_ENV` | `sandbox` | `production`. Start-up refuses unsafe settings. |
| `PORT` | `8787` | |
| `PUBLIC_BASE_URL` | `http://localhost:8787` | **https** URL clients use. Used for OAuth metadata and confirmation links. |
| `OAUTH_ISSUER` | `https://auth.sandbox.gimme.local` | GIMME authorization server issuer |
| `OAUTH_AUDIENCE` | `PUBLIC_BASE_URL` | The `aud` your AS puts in tokens for this API |
| `OAUTH_JWKS_URL` | — | **Required**: the AS JWKS endpoint |
| `OAUTH_AUTHORIZATION_SERVER` | issuer | Advertised in RFC 9728 metadata |
| `SANDBOX_TOKEN_SECRET` | built-in | Ignored in production |
| `PURCHASE_AUTHORIZATION_KEY` | built-in sandbox key | **Required**, 32+ random characters, from Secrets Manager |
| `INTERNAL_EVENTS_SECRET` | built-in | **Required**: shared with the OMS for `/v1/internal/order-events` |
| `WEBHOOK_SUBSCRIBERS` | `[]` | JSON: `[{"url","secret","events":["order.*"]}]` |
| `WEBHOOK_MAX_ATTEMPTS` | `6` | |
| `SESSION_TTL_SECONDS` | `900` | |
| `ORDER_INTENT_TTL_SECONDS` | `300` | |
| `PURCHASE_AUTHORIZATION_TTL_SECONDS` | `120` | |
| `IDEMPOTENCY_TTL_SECONDS` | `86400` | |
| `REQUIRE_DEVICE_AUTH_PLATFORMS` | `APPLE,GOOGLE` | |
| `ALLOW_MCP_FORM_CONFIRMATION` | `true` | Set to `false` to require URL or hosted confirmation for agents |
| `LOG_LEVEL` | `info` | |

## Before production: what is not built yet

The sandbox runs the whole system end to end. These pieces depend on GIMME's real systems and are not in this repository yet:

1. **Production backend adapters.** Implement `GimmeBackend` (`src/backend/ports.ts`) as HTTP or gRPC clients to GIMME's customer, catalogue, inventory, delivery, pricing and order services and to the payment provider, then wire them in `src/server.ts`. That file exits in production until this is done, by design. The ports document the guarantees each one needs. The main ones are an atomic `inventory.reserve`, an idempotent `orders.create` keyed by `idempotencyKey`, and provider-side idempotency on `payments.authorize`. `test/` can be pointed at a staging backend to check the contracts.
2. **Shared state store.** `src/store/store.ts` is in-memory, which is correct for **one instance**. Before running more than one, implement `KeyValueStore` and `LockManager` on Redis (`SET NX EX`, a lock with a TTL and owner token). The interfaces were written for exactly that.
3. **Durable audit sink.** Point an `AuditSink` at write-once storage (S3 Object Lock, or CloudWatch Logs with a retention lock).
4. **Hosted confirmation page sign-in.** Put `/v1/voice/confirm/*` behind GIMME web sign-in so the person confirming is the account holder, not just someone holding the link.
5. **Authorization server.** GIMME's OAuth AS must issue JWT access tokens with `sub` = customer id, the scopes above, and `aud` = this API. It also needs to support account linking for Google, and dynamic client registration or a pre-registered client for MCP agents. `gimme.order.confirm` goes only to GIMME's own apps.
6. **Licensed hours and alcohol rules.** Confirm the delivery window and remote-sale requirements against GIMME's licence conditions. The sandbox uses 07:00–23:00 Pacific/Auckland.
7. **iOS and Android code.** Build the Swift reference in the iOS project. Implement the Android side against whichever Google surface is approved (see [google-integration.md](google-integration.md)).

## AWS mapping (§46)

```
Route 53 ─▶ ALB / API Gateway (TLS, WAF, rate limits)
              │
              ▼
        ECS Fargate service: gimme-voice-commerce   (2+ tasks once the Redis store is in)
              │      │            │
              │      │            └─▶ ElastiCache Redis   sessions, intents, idempotency, locks
              │      └─▶ Secrets Manager   PURCHASE_AUTHORIZATION_KEY, INTERNAL_EVENTS_SECRET, webhook secrets
              └─▶ GIMME core services (VPC) + payment provider
        stdout JSON ─▶ CloudWatch Logs (audit stream with retention lock) ─▶ S3 Object Lock archive
        /metrics ─▶ Amazon Managed Prometheus / CloudWatch agent ─▶ dashboards (funnel, latency, failure rates)
        Webhook fan-out: in-process today; move to EventBridge → SQS → delivery worker if subscribers grow
```

Use ECS rather than Lambda: MCP Streamable HTTP holds an open stream during elicitation, and the state is per-session. Only bring in EventBridge or SQS when webhook volume calls for it.

## Health and operations

- `GET /health` reports the environment and versions. `GET /metrics` serves Prometheus text. Keep `/metrics` and `/v1/internal/*` off the public internet.
- Rotating `PURCHASE_AUTHORIZATION_KEY` invalidates authorizations issued in the last 2 minutes only. Customers asked to confirm again just say yes again.
- Rotating `INTERNAL_EVENTS_SECRET` needs a coordinated change with the OMS. Add dual-secret verification if zero-downtime rotation is required.
