/**
 * REST Voice API (§27) over HTTP, with real bearer tokens.
 */

import { describe, expect, it } from "vitest";
import request from "supertest";
import { createHttpApp } from "../src/http/app.js";
import { mintSandboxToken } from "../src/security/access-tokens.js";
import { AGENT_SCOPES, ALL_SCOPES, SCOPES } from "../src/security/scopes.js";
import { signWebhook } from "../src/events/events.js";
import { harness } from "./helpers.js";

async function setup() {
  const h = harness();
  const app = createHttpApp(h.rt);
  const appToken = await mintSandboxToken(h.config, { customerId: "TEST_CUSTOMER", scopes: ALL_SCOPES, clientId: "gimme-ios-app" });
  const agentToken = await mintSandboxToken(h.config, { customerId: "TEST_CUSTOMER", scopes: AGENT_SCOPES, clientId: "agent" });
  return { h, app, appToken, agentToken };
}

describe("Voice API", () => {
  it("rejects missing or bad tokens with an OAuth challenge pointing at resource metadata", async () => {
    const { app } = await setup();
    const r = await request(app).post("/v1/voice/sessions").send({});
    expect(r.status).toBe(401);
    expect(r.headers["www-authenticate"]).toContain('resource_metadata="http://localhost:8787/.well-known/oauth-protected-resource"');
    expect(r.body).toMatchObject({ success: false, error_code: "AUTHENTICATION_REQUIRED" });
    const bad = await request(app).post("/v1/voice/sessions").set("authorization", "Bearer not.a.jwt").send({});
    expect(bad.status).toBe(401);
  });

  it("serves RFC 9728 protected resource metadata", async () => {
    const { app } = await setup();
    const r = await request(app).get("/.well-known/oauth-protected-resource/mcp");
    expect(r.body).toMatchObject({ resource: "http://localhost:8787/mcp", authorization_servers: ["https://auth.sandbox.gimme.local"], bearer_methods_supported: ["header"] });
    expect(r.body.scopes_supported).toContain("gimme.payment.authorize");
  });

  it("runs the full ordering flow, echoing request and correlation ids", async () => {
    const { app, appToken, h } = await setup();
    const auth = { authorization: `Bearer ${appToken}`, "gimme-api-version": "1.0", "x-correlation-id": "siri-turn-42" };

    const s = await request(app).post("/v1/voice/sessions").set(auth).send({ platform: "APPLE", platform_user_id: "apple-user-test", conversation_id: "conv-9" });
    expect(s.status).toBe(201);
    expect(s.headers["x-correlation-id"]).toBe("siri-turn-42");
    expect(s.headers["x-request-id"]).toMatch(/^req_/);
    const sid = s.body.session_id;

    const resolved = await request(app).post("/v1/voice/resolve").set(auth).send({ session_id: sid, request: { type: "USUAL" } });
    const items = resolved.body.items.map((i: { sku: string; quantity: number }) => ({ sku: i.sku, quantity: i.quantity }));
    const intent = await request(app).post("/v1/voice/order-intents").set(auth).set("idempotency-key", "intent-conv-9").send({ session_id: sid, items, source: "USUAL" });
    expect(intent.status).toBe(201);
    const oid = intent.body.order_intent.order_intent_id;

    const confirmed = await request(app)
      .post(`/v1/voice/order-intents/${oid}/confirm`)
      .set(auth)
      .send({ decision: "CONFIRM", confirmed_total: intent.body.order_intent.total, confirmed_currency: "NZD", evidence: { method: "APPLE_APP_INTENT_CONFIRMATION", device_authenticated: true } });
    expect(confirmed.status).toBe(200);

    const pay = await request(app).post(`/v1/voice/order-intents/${oid}/authorize-payment`).set(auth).send({ authorization_token: confirmed.body.authorization_token });
    expect(pay.status).toBe(200);

    const key = `APPLE:TEST_CUSTOMER:conv-9:${oid}`;
    const order = await request(app).post("/v1/voice/orders").set(auth).set("idempotency-key", key).send({ order_intent_id: oid, payment_authorization_id: pay.body.payment_authorization_id });
    expect(order.status).toBe(201);
    expect(order.body).toMatchObject({ success: true, status: "CONFIRMED", idempotent_replay: false });

    const again = await request(app).post("/v1/voice/orders").set(auth).set("idempotency-key", key).send({ order_intent_id: oid, payment_authorization_id: pay.body.payment_authorization_id });
    expect(again.body).toMatchObject({ order_id: order.body.order_id, idempotent_replay: true });

    const status = await request(app).get(`/v1/voice/orders/${order.body.order_id}`).set(auth);
    expect(status.body).toMatchObject({ order_id: order.body.order_id, status: "CONFIRMED" });
    const latest = await request(app).get("/v1/voice/orders/latest").set(auth);
    expect(latest.body.order_id).toBe(order.body.order_id);
    const receipt = await request(app).get(`/v1/voice/orders/${order.body.order_id}/receipt`).set(auth);
    expect(receipt.body.payment).toBe("Visa ending 4242");
    const cancel = await request(app).post(`/v1/voice/orders/${order.body.order_id}/cancel`).set(auth).send({});
    expect(cancel.body).toMatchObject({ success: true, status: "CANCELLED" });

    expect(h.rt.audit.query({ order_id: order.body.order_id })[0]!.correlation_id).toBe("siri-turn-42");
  });

  it("a third-party agent token cannot record a confirmation", async () => {
    const { app, agentToken } = await setup();
    const auth = { authorization: `Bearer ${agentToken}` };
    const s = await request(app).post("/v1/voice/sessions").set(auth).send({ platform: "MCP_AGENT", platform_user_id: "x" });
    const intent = await request(app).post("/v1/voice/order-intents").set(auth).send({ session_id: s.body.session_id, items: [{ sku: "HEI6PK", quantity: 1 }] });
    const r = await request(app)
      .post(`/v1/voice/order-intents/${intent.body.order_intent.order_intent_id}/confirm`)
      .set(auth)
      .send({ decision: "CONFIRM", confirmed_total: intent.body.order_intent.total, confirmed_currency: "NZD", evidence: { method: "AGENT", device_authenticated: true } });
    expect(r.status).toBe(403);
    expect(r.body).toMatchObject({ error_code: "INSUFFICIENT_SCOPE", details: { missing_scopes: [SCOPES.ORDER_CONFIRM] } });
  });

  it("validates bodies and versions", async () => {
    const { app, appToken } = await setup();
    const auth = { authorization: `Bearer ${appToken}` };
    const bad = await request(app).post("/v1/voice/order-intents").set(auth).send({ session_id: "VS-1", items: [] });
    expect(bad.status).toBe(400);
    expect(bad.body.error_code).toBe("INVALID_REQUEST");
    expect(bad.body.details.issues[0].path).toBe("items");
    const v = await request(app).post("/v1/voice/sessions").set(auth).set("gimme-api-version", "9.9").send({});
    expect(v.status).toBe(400);
  });

  it("hosted confirmation page: shows the exact order, confirms once", async () => {
    const { app, agentToken } = await setup();
    const auth = { authorization: `Bearer ${agentToken}` };
    const s = await request(app).post("/v1/voice/sessions").set(auth).send({ platform: "MCP_AGENT", platform_user_id: "x" });
    const intent = await request(app).post("/v1/voice/order-intents").set(auth).send({ session_id: s.body.session_id, items: [{ sku: "HEI6PK", quantity: 1 }] });
    const oid = intent.body.order_intent.order_intent_id;
    const cr = await request(app).post(`/v1/voice/order-intents/${oid}/confirmation-request`).set(auth).send({});
    const url = new URL(cr.body.confirmation_url);
    const page = await request(app).get(url.pathname + url.search);
    expect(page.status).toBe(200);
    expect(page.text).toContain("Place order for $28.96");
    const nonce = url.searchParams.get("nonce")!;
    const post = await request(app).post(url.pathname).type("form").send({ nonce, decision: "CONFIRM" });
    expect(post.text).toContain("Confirmed");
    const reuse = await request(app).get(url.pathname + url.search);
    expect(reuse.status).toBe(404);
  });

  it("accepts HMAC-signed OMS events and rejects unsigned ones", async () => {
    const { app, h } = await setup();
    const seen: string[] = [];
    h.rt.events.subscribe((e) => seen.push(e.type));
    const order = { order_id: "GIMME-1", customer_id: "TEST_CUSTOMER", status: "OUT_FOR_DELIVERY", lines: [], subtotal_cents: 0, fees: { delivery_cents: 0, service_cents: 0, packaging_cents: 0 }, discount_cents: 0, total_cents: 0, currency: "NZD", delivery_address_id: "ADDR_HOME", payment_reference: "P", created_at: new Date().toISOString(), source: { channel: "VOICE", platform: "APPLE", order_intent_id: "OI-1" } };
    const body = JSON.stringify({ order, previous_status: "DISPATCHED" });
    const unsigned = await request(app).post("/v1/internal/order-events").set("content-type", "application/json").send(body);
    expect(unsigned.status).toBe(401);
    const sig = signWebhook(h.config.internalEventsSecret, body, Math.floor(Date.now() / 1000));
    const ok = await request(app).post("/v1/internal/order-events").set("content-type", "application/json").set("gimme-signature", sig).send(body);
    expect(ok.status).toBe(202);
    expect(seen).toEqual(["order.out_for_delivery"]);
  });

  it("exposes Prometheus metrics", async () => {
    const { app, appToken } = await setup();
    await request(app).post("/v1/voice/sessions").set("authorization", `Bearer ${appToken}`).send({ platform: "APPLE", platform_user_id: "apple-user-test" });
    const m = await request(app).get("/metrics");
    expect(m.text).toContain('gimme_voice_funnel_total{platform="APPLE",stage="session_started"} 1');
    expect(m.text).toContain("# TYPE gimme_operation_latency_ms histogram");
  });
});
