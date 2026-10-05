import { loadConfig, type Config } from "../src/config.js";
import { createRuntime, type VoiceCommerceRuntime } from "../src/app.js";
import { SandboxBackend } from "../src/backend/sandbox/sandbox-backend.js";
import type { CallContext } from "../src/core/models.js";
import { GimmeError } from "../src/domain/errors.js";
import { silentLogger } from "../src/observability/logger.js";
import type { Principal } from "../src/security/access-tokens.js";
import { AGENT_SCOPES, ALL_SCOPES } from "../src/security/scopes.js";
import type { Platform } from "../src/domain/types.js";

/** 2pm in Auckland (NZDT, UTC+13) on 5 Oct 2026: inside delivery hours. */
export const NZ_AFTERNOON = new Date("2026-10-05T01:00:00Z");
/** 3am in Auckland: outside delivery hours. */
export const NZ_3AM = new Date("2026-10-04T14:00:00Z");

export interface Harness {
  rt: VoiceCommerceRuntime;
  backend: SandboxBackend;
  config: Config;
  clock: { now: Date; advance(ms: number): void };
  ctx(opts?: Partial<{ customerId: string; clientId: string; scopes: string[] }>): CallContext;
  /** A first-party app context (holds gimme.order.confirm). */
  appCtx(customerId?: string): CallContext;
}

export function harness(overrides: Partial<Config> = {}, envOverrides: NodeJS.ProcessEnv = {}): Harness {
  const config = { ...loadConfig({ ...envOverrides }), ...overrides };
  const backend = new SandboxBackend();
  const clock = {
    now: new Date(NZ_AFTERNOON),
    advance(ms: number) {
      this.now = new Date(this.now.getTime() + ms);
    },
  };
  const rt = createRuntime({ config, log: silentLogger, backend, now: () => clock.now, fetch: async () => ({ status: 200 }) });
  let n = 0;
  const ctx: Harness["ctx"] = (o = {}) => {
    const principal: Principal = {
      customerId: o.customerId ?? "TEST_CUSTOMER",
      clientId: o.clientId ?? "test-agent",
      scopes: o.scopes ?? [...AGENT_SCOPES],
      tokenId: `tok-${++n}`,
      expiresAt: Math.floor(Date.now() / 1000) + 3600,
    };
    return { principal, requestId: `req-${n}`, correlationId: `corr-${n}`, channel: "VOICE_API", apiVersion: "1.0" };
  };
  return {
    rt,
    backend,
    config,
    clock,
    ctx,
    appCtx: (customerId = "TEST_CUSTOMER") => ctx({ customerId, clientId: "gimme-ios-app", scopes: [...ALL_SCOPES] }),
  };
}

/** Expect a promise to reject with a GimmeError of this code; returns the error. */
export async function expectCode(p: Promise<unknown>, code: string): Promise<GimmeError> {
  try {
    await p;
  } catch (err) {
    if (err instanceof GimmeError && err.code === code) return err;
    throw new Error(`expected ${code}, got ${err instanceof GimmeError ? err.code : String(err)}`);
  }
  throw new Error(`expected ${code}, but it succeeded`);
}

export async function session(h: Harness, ctx: CallContext, platform: Platform = "MCP_AGENT", platformUserId = "agent-user") {
  return h.rt.service.identifyCustomer(ctx, { platform, platform_user_id: platformUserId, conversation_id: "conv-1" });
}

/** Run create -> confirm -> authorize -> place on one context. */
export async function orderEndToEnd(h: Harness, ctx: CallContext, items: { sku: string; quantity: number }[], opts: { payment_method_id?: string } = {}) {
  const s = await session(h, ctx);
  const created = await h.rt.service.createOrderIntent(ctx, { session_id: s.session_id, items, source: "BASKET", ...opts });
  const id = created.order_intent.order_intent_id;
  const auth = await h.rt.service.confirmOrderIntent(
    ctx,
    id,
    { decision: "CONFIRM", confirmed_total: created.order_intent.total, confirmed_currency: "NZD", evidence: { method: "TEST", device_authenticated: true } },
    { kind: "MCP_ELICITATION" },
  );
  const pay = await h.rt.service.authorizePayment(ctx, { order_intent_id: id, authorization_token: auth.authorization_token! });
  const placed = await h.rt.service.placeOrder(ctx, { order_intent_id: id, payment_authorization_id: pay.payment_authorization_id, idempotency_key: `k-${id}` });
  return { session: s, created, auth, pay, placed };
}
