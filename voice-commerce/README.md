# GIMME Voice Commerce

Voice and agent ordering for GIMME: "Hey Siri, order my usual from GIMME." Siri, Google and AI agents are interfaces into one deterministic commerce engine. GIMME's backend stays the source of truth for identity, price, stock, eligibility and payment, and a customer's explicit confirmation is required before anything is charged.

```
Customer: Hey Siri, order my usual from GIMME.
Siri:     Your usual GIMME order is two Heineken 12-packs and one Cloudy Bay Sauvignon Blanc. It's $111.94 delivered to home. Want me to place it?
Customer: Yes.   [Face ID]
Siri:     Done. Your GIMME order is confirmed and should arrive in about 45 minutes.
```

(That is real output from `npm run demo` against the sandbox.)

## Quick start

```bash
cd voice-commerce
npm ci
npm test            # 98 tests: MVP flow, every §38 threat, every §41 sandbox scenario, MCP, REST, adapters
npm run demo        # example Siri + Google conversations, generated live
npm run dev         # HTTP server on :8787 (REST + /mcp); prints sandbox bearer tokens
npm run mcp:stdio   # MCP over stdio for desktop MCP clients
```

Requires Node 20 or later.

## What's here

```
src/
  core/voice-commerce-service.ts   the engine — every ordering rule lives here and nowhere else
  core/resolver.ts, speech.ts      "a dozen Heinekens" → SKUs; short spoken replies
  contracts/schemas.ts             zod contracts → MCP schemas, REST validation, OpenAPI
  mcp/                             17 MCP tools (stdio + Streamable HTTP with OAuth)
  http/                            REST Voice API /v1/voice, hosted confirmation page, OMS events
  adapters/                        platform-neutral dialog + Apple and Google adapters
  security/                        OAuth token verification, scopes, signed purchase authorizations, guards
  domain/                          error taxonomy, state machines, types
  backend/ports.ts                 interfaces to GIMME's core systems
  backend/sandbox/                 complete in-memory GIMME for the sandbox
  audit/, events/, observability/  hash-chained audit, signed webhooks, metrics + funnel
adapters/apple/GIMMEIntents.swift  iOS App Intents reference implementation
docs/                              specs and contracts (below)
test/                              vitest suites
```

## Documentation (§51 deliverables)

| # | Deliverable | Where |
| --- | --- | --- |
| 1 | Architecture diagram | [docs/architecture.md](docs/architecture.md) |
| 2 | MCP server | `src/mcp/` |
| 3 | MCP tool definitions | [docs/mcp-tools.md](docs/mcp-tools.md), [docs/mcp-tools.json](docs/mcp-tools.json) (generated) |
| 4 | OpenAPI specification | [docs/openapi.json](docs/openapi.json) (generated) |
| 5 | Authentication specification | [docs/security.md#authentication](docs/security.md#authentication) |
| 6 | OAuth scope specification | [docs/security.md#scopes-22](docs/security.md#scopes-22) |
| 7 | Apple integration contract | [docs/apple-integration.md](docs/apple-integration.md), `adapters/apple/` |
| 8 | Google integration contract | [docs/google-integration.md](docs/google-integration.md) |
| 9 | Payment authorization flow | [docs/security.md#payment-authorization-flow-13-37](docs/security.md#payment-authorization-flow-13-37) |
| 10 | Order state machine | [docs/architecture.md#state-machines](docs/architecture.md#state-machines), `src/domain/state-machine.ts` |
| 11 | Error taxonomy | [docs/mcp-tools.md#error-codes](docs/mcp-tools.md#error-codes), `src/domain/errors.ts` |
| 12 | Webhook specification | [docs/voice-api.md#webhooks-30](docs/voice-api.md#webhooks-30) |
| 13 | Database / object models | [docs/architecture.md#object-models](docs/architecture.md#object-models) |
| 14 | Sandbox environment | [docs/sandbox.md](docs/sandbox.md), `src/backend/sandbox/` |
| 15 | Automated tests | `test/` (98 tests) |
| 16 | Security / threat model | [docs/security.md#threat-model-38](docs/security.md#threat-model-38) |
| 17 | API documentation | [docs/voice-api.md](docs/voice-api.md) |
| 18 | Example Siri conversation flows | [docs/apple-integration.md#example-conversations-18](docs/apple-integration.md#example-conversations-18) |
| 19 | Example Google conversation flows | [docs/google-integration.md#example-conversations-19](docs/google-integration.md#example-conversations-19) |
| 20 | Deployment documentation | [docs/deployment.md](docs/deployment.md) |

## Definition of done (§50): honest status

| # | Item | Status |
| --- | --- | --- |
| 1 | MCP server deployed in a sandbox | **Runs locally** (`npm run dev`); the compiled server was smoke-tested. **Not deployed to a hosted environment yet.** The Dockerfile is written but unbuilt (no Docker daemon was available here). |
| 2 | All tools have strict schemas | Done: zod input and output on all 17 tools, enforced by the MCP SDK |
| 3 | OAuth / authentication works | Done for a resource server: JWT verification (JWKS in production), RFC 9728 metadata, `WWW-Authenticate` challenges. GIMME's authorization server is a dependency. |
| 4–9 | Identity, search, inventory, delivery, checkout, order intent | Done, against the sandbox backend |
| 10 | Explicit customer authorization captured | Done: signed, digest-bound, single use; native, elicitation and hosted channels |
| 11 | Tokenized payment authorization | Done against the sandbox gateway port. A real provider adapter is still needed. |
| 12–13 | Idempotent orders; duplicates prevented | Done and tested, including concurrent calls |
| 14–15 | Order status; cancellation where permitted | Done |
| 16 | Auditable | Done: hash-chained trail. A durable WORM sink is a deployment step. |
| 17–20 | Apple and Google contracts and mappings | Done. The Swift reference has not been compiled here, and the Google surface must be confirmed at implementation time. |
| 21 | No raw payment credentials stored | Done: none accepted, card-shaped input refused, redaction tested |
| 22 | Compliance enforced server-side | Done. Licensed hours must be confirmed against GIMME's licence. |
| 23–24 | Sandbox tests and failure/retry scenarios | Done (98 passing) |
| 25–30 | API docs, OpenAPI, MCP spec, architecture, threat model, deployment | Done |

**To reach production** (details in [docs/deployment.md](docs/deployment.md#before-production-what-is-not-built-yet)):

- implement the `GimmeBackend` ports against GIMME's real services and payment provider
- move the state store to Redis before running more than one instance
- add a durable audit sink
- put the hosted confirmation page behind GIMME sign-in
- stand up the OAuth authorization server and account linking
- build the iOS and Android clients
