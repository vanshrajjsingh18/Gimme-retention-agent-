import { describe, expect, it } from "vitest";
import { EventBus, signWebhook, verifyWebhookSignature, WebhookDispatcher } from "../src/events/events.js";
import { silentLogger } from "../src/observability/logger.js";
import { assertIntentTransition, canAdvance, canTransitionOrder } from "../src/domain/state-machine.js";
import { loadConfig } from "../src/config.js";

describe("webhooks", () => {
  const event = () => new EventBus().publish("order.delivered", { customer_id: "C", order_id: "O" });

  it("signs deliveries so receivers can verify them", async () => {
    const calls: { headers: Record<string, string>; body: string }[] = [];
    const d = new WebhookDispatcher([{ url: "https://adapter.test/hook", secret: "s3cret", events: ["order.*"] }], silentLogger, {
      maxAttempts: 3,
      fetch: async (_u, init) => {
        calls.push(init);
        return { status: 200 };
      },
    });
    const bus = new EventBus();
    d.attach(bus);
    bus.publish("order.delivered", { customer_id: "C", order_id: "O" });
    bus.publish("payment.authorized", { customer_id: "C" }); // not subscribed
    await new Promise((r) => setTimeout(r, 0));
    expect(calls).toHaveLength(1);
    expect(calls[0]!.headers["gimme-event-type"]).toBe("order.delivered");
    expect(verifyWebhookSignature("s3cret", calls[0]!.body, calls[0]!.headers["gimme-signature"]!)).toBe(true);
    expect(verifyWebhookSignature("wrong", calls[0]!.body, calls[0]!.headers["gimme-signature"]!)).toBe(false);
  });

  it("retries 5xx with backoff and gives up on 4xx", async () => {
    const statuses = [503, 502, 200];
    const sleeps: number[] = [];
    const d = new WebhookDispatcher([], silentLogger, {
      maxAttempts: 5,
      fetch: async () => ({ status: statuses.shift()! }),
      sleep: async (ms) => void sleeps.push(ms),
    });
    expect(await d.deliver({ url: "u", secret: "s", events: ["*"] }, event())).toBe(true);
    expect(sleeps).toEqual([1000, 2000]);

    const d2 = new WebhookDispatcher([], silentLogger, { maxAttempts: 5, fetch: async () => ({ status: 410 }), sleep: async () => {} });
    expect(await d2.deliver({ url: "u", secret: "s", events: ["*"] }, event())).toBe(false);
    expect(d2.attempts).toHaveLength(1);
  });

  it("rejects stale signatures", () => {
    const body = "{}";
    const old = signWebhook("s", body, 1000);
    expect(verifyWebhookSignature("s", body, old, 300, 1000 + 301)).toBe(false);
    expect(verifyWebhookSignature("s", body, old, 300, 1000 + 299)).toBe(true);
  });
});

describe("state machines", () => {
  it("voice stages only move forward, except new basket / tracking", () => {
    expect(canAdvance("DISCOVERY", "CUSTOMER_IDENTIFICATION")).toBe(true);
    expect(canAdvance("CUSTOMER_IDENTIFICATION", "PAYMENT_AUTHORIZATION")).toBe(false);
    expect(canAdvance("ORDER_INTENT_CREATED", "PRODUCT_RESOLUTION")).toBe(true);
    expect(canAdvance("ORDER_CONFIRMED", "DELIVERY_TRACKING")).toBe(true);
    expect(canAdvance("DISCOVERY", "DELIVERY_TRACKING")).toBe(false);
  });

  it("order intents cannot skip confirmation or leave a terminal state", () => {
    expect(() => assertIntentTransition("AWAITING_CONFIRMATION", "PAYMENT_AUTHORIZED")).toThrow();
    expect(() => assertIntentTransition("AWAITING_CONFIRMATION", "ORDER_PLACED")).toThrow();
    expect(() => assertIntentTransition("ORDER_PLACED", "CANCELLED")).toThrow();
    expect(() => assertIntentTransition("CONFIRMED", "PAYMENT_AUTHORIZED")).not.toThrow();
  });

  it("orders follow the OMS lifecycle", () => {
    expect(canTransitionOrder("CONFIRMED", "ACCEPTED")).toBe(true);
    expect(canTransitionOrder("OUT_FOR_DELIVERY", "CANCELLED")).toBe(false);
    expect(canTransitionOrder("DELIVERED", "REFUNDED")).toBe(true);
  });
});

describe("config", () => {
  it("refuses to start in production with sandbox secrets", () => {
    expect(() => loadConfig({ VOICE_ENV: "production" })).toThrow(/OAUTH_JWKS_URL[\s\S]*PURCHASE_AUTHORIZATION_KEY[\s\S]*INTERNAL_EVENTS_SECRET[\s\S]*https/);
  });

  it("starts in production when properly configured, with no sandbox token secret", () => {
    const c = loadConfig({
      VOICE_ENV: "production",
      OAUTH_JWKS_URL: "https://auth.gimme.example/.well-known/jwks.json",
      PURCHASE_AUTHORIZATION_KEY: "x".repeat(48),
      INTERNAL_EVENTS_SECRET: "y".repeat(48),
      PUBLIC_BASE_URL: "https://voice.gimme.example",
    });
    expect(c.auth.sandboxSigningSecret).toBeUndefined();
  });
});
