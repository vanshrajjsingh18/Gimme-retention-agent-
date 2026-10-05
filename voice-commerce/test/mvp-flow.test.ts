/**
 * §47 MVP: "Order my usual from GIMME", then "Where's my GIMME order?",
 * driven step by step through the service exactly as an adapter would.
 */

import { describe, expect, it } from "vitest";
import { harness } from "./helpers.js";

describe("MVP: order my usual", () => {
  it("authenticates, resolves, validates, prices, confirms, pays, places, and tracks", async () => {
    const h = harness();
    const app = h.appCtx();
    const svc = h.rt.service;

    // 1. Authenticate customer (identity from the token, link checked against the platform user).
    const id = await svc.identifyCustomer(app, { platform: "APPLE", platform_user_id: "apple-user-test", conversation_id: "siri-1" });
    expect(id).toMatchObject({ customer_id: "TEST_CUSTOMER", customer_name: "Sam", eligible_to_order: true, default_address: { id: "ADDR_HOME" } });
    expect(id.handshake).toMatchObject({ protocol_version: "1.0", mcp_version: "1.0", platform: "APPLE", session_id: id.session_id });

    // 2. Retrieve usual order.
    const usual = await svc.resolveItems(app, { session_id: id.session_id, request: { type: "USUAL" } });
    expect(usual.status).toBe("RESOLVED");
    expect(usual.items.map((i) => [i.sku, i.quantity])).toEqual([["HEI12PK", 2], ["WINE-SB", 1]]);

    // 3-6. Inventory, delivery, compliance and price, all inside order-intent creation.
    const created = await svc.createOrderIntent(app, { session_id: id.session_id, items: usual.items.map(({ sku, quantity }) => ({ sku, quantity })), source: "USUAL" });
    const intent = created.order_intent;
    // 2 x 34.99 + 32.99 = 102.97; + 3.99 + 3.99 + 0.99 fees = 111.94
    expect(intent).toMatchObject({ status: "AWAITING_CONFIRMATION", subtotal: 102.97, total: 111.94, currency: "NZD", estimated_delivery_minutes: 45 });
    expect(intent.delivery_requirements).toContain("ID_CHECK_ON_DELIVERY");

    // 7. Ask for confirmation — short, speakable, every number from the backend.
    expect(created.speech).toBe("Your usual GIMME order is two Heineken 12-packs and one Cloudy Bay Sauvignon Blanc. It's $111.94 delivered to home. Want me to place it?");
    expect(created.confirmation).toMatchObject({ requires_user_confirmation: true, confirmation_type: "PURCHASE", amount: 111.94, currency: "NZD", merchant: "GIMME", requires_device_authentication: true });

    // Customer: "Yes." — Siri confirmed on an authenticated device.
    const auth = await svc.confirmOrderIntent(
      app,
      intent.order_intent_id,
      { decision: "CONFIRM", confirmed_total: 111.94, confirmed_currency: "NZD", evidence: { method: "APPLE_APP_INTENT_CONFIRMATION", device_authenticated: true } },
      { kind: "FIRST_PARTY" },
    );
    expect(auth.status).toBe("AUTHORIZED");

    // 8. Authorize payment (tokenised saved method).
    const pay = await svc.authorizePayment(app, { order_intent_id: intent.order_intent_id, authorization_token: auth.authorization_token! });
    expect(pay).toMatchObject({ authorized: true, amount: 111.94 });

    // 9. Place order.
    const placed = await svc.placeOrder(app, {
      order_intent_id: intent.order_intent_id,
      payment_authorization_id: pay.payment_authorization_id,
      idempotency_key: `APPLE:TEST_CUSTOMER:siri-1:${intent.order_intent_id}`,
    });

    // 10. Order number and ETA.
    expect(placed).toMatchObject({ success: true, status: "CONFIRMED", total: 111.94, estimated_delivery_minutes: 45, idempotent_replay: false });
    expect(placed.order_id).toMatch(/^GIMME-\d+$/);
    expect(placed.speech).toBe("Done. Your GIMME order is confirmed and should arrive in about 45 minutes.");

    // Charged exactly once, for exactly the confirmed amount.
    const ledger = h.backend.paymentLedger();
    expect(ledger).toHaveLength(1);
    expect(ledger[0]).toMatchObject({ amountCents: 11194, status: "CAPTURED" });
    expect(h.backend.getStock("HEI12PK")).toBe(198);

    // "Where's my GIMME order?"
    h.clock.advance(30 * 60_000);
    h.backend.setOrderStatus(placed.order_id, "ACCEPTED");
    h.backend.setOrderStatus(placed.order_id, "PREPARING");
    h.backend.setOrderStatus(placed.order_id, "DISPATCHED");
    h.backend.setOrderStatus(placed.order_id, "OUT_FOR_DELIVERY");
    // A new conversation half an hour later: the old voice session has expired, as it should.
    const later = await svc.identifyCustomer(app, { platform: "APPLE", platform_user_id: "apple-user-test", conversation_id: "siri-2" });
    const status = await svc.getOrderStatus(app, { session_id: later.session_id });
    expect(status).toMatchObject({ order_id: placed.order_id, status: "OUT_FOR_DELIVERY", estimated_minutes: 15 });
    expect(status.speech).toBe("Your GIMME order is on its way and should arrive in about 15 minutes. Have your ID ready.");

    // The funnel recorded every stage once.
    expect(h.rt.metrics.counter("gimme_voice_funnel_total", { stage: "session_started", platform: "APPLE" })).toBe(2); // order + status call
    for (const stage of ["product_resolved", "cart_priced", "intent_created", "confirmed", "payment_authorized", "order_placed"]) {
      expect(h.rt.metrics.counter("gimme_voice_funnel_total", { stage, platform: "APPLE" })).toBe(1);
    }

    // The audit trail is complete, ordered and intact.
    const trail = h.rt.audit.query({ order_intent_id: intent.order_intent_id }).map((e) => e.action);
    expect(trail).toEqual(["order_intent.created", "order_intent.confirmed", "payment.authorize", "order.created"]);
    const orderEntry = h.rt.audit.query({ order_id: placed.order_id })[0]!;
    expect(orderEntry).toMatchObject({ session_id: id.session_id, platform: "APPLE", platform_user_id: "apple-user-test", payment_reference: pay.payment_reference });
    expect(orderEntry.data).toMatchObject({ total: 111.94, currency: "NZD", delivery_address_id: "ADDR_HOME", confirmation_method: "APPLE_APP_INTENT_CONFIRMATION" });
    expect(h.rt.audit.verify()).toBeNull();
  });

  it("publishes order events for adapters as the OMS moves the order", async () => {
    const h = harness();
    const seen: string[] = [];
    h.rt.events.subscribe((e) => seen.push(e.type));
    const app = h.appCtx();
    const s = await h.rt.service.identifyCustomer(app, { platform: "APPLE", platform_user_id: "apple-user-test" });
    const c = await h.rt.service.createOrderIntent(app, { session_id: s.session_id, items: [{ sku: "HEI6PK", quantity: 1 }], source: "BASKET" });
    const a = await h.rt.service.confirmOrderIntent(app, c.order_intent.order_intent_id, { decision: "CONFIRM", confirmed_total: c.order_intent.total, confirmed_currency: "NZD", evidence: { method: "X", device_authenticated: true } }, { kind: "FIRST_PARTY" });
    const p = await h.rt.service.authorizePayment(app, { order_intent_id: c.order_intent.order_intent_id, authorization_token: a.authorization_token! });
    const o = await h.rt.service.placeOrder(app, { order_intent_id: c.order_intent.order_intent_id, payment_authorization_id: p.payment_authorization_id, idempotency_key: "evt-test-1" });
    h.backend.setOrderStatus(o.order_id, "ACCEPTED");
    h.backend.setOrderStatus(o.order_id, "PREPARING");
    h.backend.setOrderStatus(o.order_id, "DISPATCHED");
    h.backend.setOrderStatus(o.order_id, "OUT_FOR_DELIVERY");
    h.backend.setOrderStatus(o.order_id, "DELIVERED");
    expect(seen).toEqual([
      "order_intent.created",
      "order_intent.confirmed",
      "payment.authorized",
      "order.created",
      "order.confirmed",
      "order.accepted",
      "order.preparing",
      "order.dispatched",
      "order.out_for_delivery",
      "order.delivered",
    ]);
  });
});
