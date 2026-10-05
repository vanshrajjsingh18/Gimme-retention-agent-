/**
 * Composition root: builds one VoiceCommerce runtime from config and a
 * backend. The HTTP server, the stdio MCP server, the tests and the demo all
 * start here, so they exercise the same wiring.
 */

import type { Config } from "./config.js";
import type { GimmeBackend } from "./backend/ports.js";
import { SandboxBackend } from "./backend/sandbox/sandbox-backend.js";
import { AuditLog, type AuditSink } from "./audit/audit-log.js";
import { EventBus, WebhookDispatcher } from "./events/events.js";
import { Metrics } from "./observability/metrics.js";
import type { Logger } from "./observability/logger.js";
import { AccessTokenVerifier } from "./security/access-tokens.js";
import { MemoryLockManager, MemoryStore } from "./store/store.js";
import { VoiceCommerceService, type VoiceStores } from "./core/voice-commerce-service.js";

export interface VoiceCommerceRuntime {
  config: Config;
  backend: GimmeBackend;
  service: VoiceCommerceService;
  verifier: AccessTokenVerifier;
  audit: AuditLog;
  metrics: Metrics;
  events: EventBus;
  webhooks: WebhookDispatcher;
  log: Logger;
  now: () => Date;
}

export function createRuntime(opts: {
  config: Config;
  log: Logger;
  backend?: GimmeBackend;
  auditSinks?: AuditSink[];
  now?: () => Date;
  fetch?: ConstructorParameters<typeof WebhookDispatcher>[2]["fetch"];
}): VoiceCommerceRuntime {
  const now = opts.now ?? (() => new Date());
  const clock = () => now().getTime();
  const backend = opts.backend ?? new SandboxBackend();
  const stores: VoiceStores = {
    sessions: new MemoryStore(clock),
    intents: new MemoryStore(clock),
    paymentAuths: new MemoryStore(clock),
    usedAuthorizations: new MemoryStore(clock),
    idempotency: new MemoryStore(clock),
    locks: new MemoryLockManager(),
  };
  const audit = new AuditLog(opts.auditSinks ?? [], now);
  const metrics = new Metrics();
  const events = new EventBus();
  const webhooks = new WebhookDispatcher(opts.config.webhooks.subscribers, opts.log, {
    maxAttempts: opts.config.webhooks.maxAttempts,
    ...(opts.fetch ? { fetch: opts.fetch } : {}),
  });
  webhooks.attach(events);
  events.subscribe((e) => metrics.inc("gimme_events_total", { type: e.type }, 1, "Domain events published"));

  const service = new VoiceCommerceService({ config: opts.config, backend, stores, audit, metrics, events, log: opts.log, now });

  // The sandbox OMS reports status changes in-process; production OMS posts to /v1/internal/order-events.
  if (backend instanceof SandboxBackend) {
    backend.now = now;
    backend.onOrderStatus((order, previous) => service.publishOrderStatus(order, previous));
  }

  return { config: opts.config, backend, service, verifier: new AccessTokenVerifier(opts.config), audit, metrics, events, webhooks, log: opts.log, now };
}
