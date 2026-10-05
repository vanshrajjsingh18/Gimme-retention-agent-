/**
 * §41 sandbox: every named test customer, product, payment and delivery
 * scenario, plus §32 (never silently recover from material changes).
 */

import { describe, expect, it } from "vitest";
import { expectCode, harness, NZ_3AM, orderEndToEnd, session } from "./helpers.js";

const intentFor = async (h: ReturnType<typeof harness>, ctx: ReturnType<ReturnType<typeof harness>["ctx"]>, items: { sku: string; quantity: number }[], extra: Record<string, unknown> = {}) => {
  const s = await session(h, ctx);
  return h.rt.service.createOrderIntent(ctx, { session_id: s.session_id, items, source: "BASKET", ...extra });
};

describe("test customers", () => {
  it("TEST_CUSTOMER can order", async () => {
    const h = harness();
    const { placed } = await orderEndToEnd(h, h.ctx(), [{ sku: "AVAILABLE_PRODUCT", quantity: 1 }]);
    expect(placed.success).toBe(true);
  });

  it("TEST_CUSTOMER_NO_PAYMENT is told to add a payment method in the app", async () => {
    const h = harness();
    const e = await expectCode(intentFor(h, h.ctx({ customerId: "TEST_CUSTOMER_NO_PAYMENT" }), [{ sku: "STP12PK", quantity: 1 }]), "PAYMENT_REQUIRED");
    expect(e.suggestedAction).toBe("ADD_PAYMENT_METHOD_IN_APP");
  });

  it("TEST_CUSTOMER_AGE_REVIEW cannot buy alcohol but can buy soft drinks", async () => {
    const h = harness();
    const ctx = h.ctx({ customerId: "TEST_CUSTOMER_AGE_REVIEW" });
    const s = await session(h, ctx);
    expect(s).toMatchObject({ eligible_to_order: false, eligibility: "AGE_VERIFICATION_REQUIRED" });
    const e = await expectCode(intentFor(h, ctx, [{ sku: "AGE_RESTRICTED_PRODUCT", quantity: 1 }]), "AGE_VERIFICATION_REQUIRED");
    expect(e.suggestedAction).toBe("VERIFY_AGE_IN_APP");
    const v = await h.rt.service.validateCustomer(ctx, { session_id: s.session_id, items: [{ sku: "COKE-1500", quantity: 2 }] });
    expect(v).toMatchObject({ status: "AGE_VERIFICATION_REQUIRED", can_order: true, restrictions: ["NO_ALCOHOL_UNTIL_VERIFIED"] });
    const ok = await intentFor(h, ctx, [{ sku: "COKE-1500", quantity: 2 }]);
    expect(ok.order_intent.status).toBe("AWAITING_CONFIRMATION");
    expect(ok.order_intent.delivery_requirements).toEqual([]);
  });

  it("TEST_CUSTOMER_RESTRICTED cannot order anything", async () => {
    const h = harness();
    const ctx = h.ctx({ customerId: "TEST_CUSTOMER_RESTRICTED" });
    await expectCode(intentFor(h, ctx, [{ sku: "COKE-1500", quantity: 1 }]), "ACCOUNT_RESTRICTED");
    const s = await session(h, ctx);
    expect(await h.rt.service.validateCustomer(ctx, { session_id: s.session_id })).toMatchObject({ can_order: false, restrictions: ["NO_ORDERS"] });
  });
});

describe("test products", () => {
  it("OUT_OF_STOCK_PRODUCT is unavailable with alternatives, never substituted", async () => {
    const h = harness();
    const ctx = h.ctx();
    const s = await session(h, ctx);
    const inv = await h.rt.service.checkInventory(ctx, { session_id: s.session_id, items: [{ sku: "OUT_OF_STOCK_PRODUCT", quantity: 1 }] });
    expect(inv).toMatchObject({ available: false, reason: "OUT_OF_STOCK" });
    expect(inv.alternatives.map((a) => a.for_sku)).toContain("OUT_OF_STOCK_PRODUCT");
    await expectCode(intentFor(h, ctx, [{ sku: "OUT_OF_STOCK_PRODUCT", quantity: 1 }]), "PRODUCT_UNAVAILABLE");
  });

  it("PRICE_CHANGED_PRODUCT: no charge at the new price; a replacement intent asks again (§32)", async () => {
    const h = harness();
    const ctx = h.ctx();
    const s = await session(h, ctx);
    const c = await h.rt.service.createOrderIntent(ctx, { session_id: s.session_id, items: [{ sku: "PRICE_CHANGED_PRODUCT", quantity: 1 }], source: "BASKET" });
    expect(c.order_intent.total).toBe(28.97); // 20.00 + 8.97 fees
    const id = c.order_intent.order_intent_id;
    const a = await h.rt.service.confirmOrderIntent(ctx, id, { decision: "CONFIRM", confirmed_total: 28.97, confirmed_currency: "NZD", evidence: { method: "T", device_authenticated: true } }, { kind: "MCP_ELICITATION" });
    const p = await h.rt.service.authorizePayment(ctx, { order_intent_id: id, authorization_token: a.authorization_token! });
    const e = await expectCode(h.rt.service.placeOrder(ctx, { order_intent_id: id, payment_authorization_id: p.payment_authorization_id, idempotency_key: "price-change-1" }), "PRICE_CHANGED");
    expect(e.message).toBe("The price has changed to $36.47. Do you want me to continue?");
    expect(e.details).toMatchObject({ previous_total: 28.97, new_total: 36.47 });
    // Nothing charged; the hold was released; stock untouched.
    expect(h.backend.paymentLedger()[0]!.status).toBe("VOIDED");
    expect(h.backend.getStock("PRICE_CHANGED_PRODUCT")).toBe(200);
    // The old intent is dead; the replacement awaits a fresh yes.
    const old = await h.rt.service.getOrderIntent(ctx, { order_intent_id: id });
    expect(old.order_intent.status).toBe("SUPERSEDED");
    const replacement = await h.rt.service.getOrderIntent(ctx, { order_intent_id: e.details!.replacement_order_intent_id as string });
    expect(replacement.order_intent).toMatchObject({ status: "AWAITING_CONFIRMATION", total: 36.47 });
    // The old authorization cannot be used on the replacement.
    await expectCode(h.rt.service.authorizePayment(ctx, { order_intent_id: replacement.order_intent.order_intent_id, authorization_token: a.authorization_token! }), "AUTHORIZATION_REQUIRED");
  });

  it("AGE_RESTRICTED_PRODUCT is purchasable by a verified customer, with ID checked on delivery", async () => {
    const h = harness();
    const c = await intentFor(h, h.ctx(), [{ sku: "AGE_RESTRICTED_PRODUCT", quantity: 1 }]);
    expect(c.order_intent.delivery_requirements).toContain("ID_CHECK_ON_DELIVERY");
  });

  it("UNKNOWN_PRODUCT is not found", async () => {
    const h = harness();
    const e = await expectCode(intentFor(h, h.ctx(), [{ sku: "UNKNOWN_PRODUCT", quantity: 1 }]), "PRODUCT_NOT_FOUND");
    expect(e.details).toEqual({ skus: ["UNKNOWN_PRODUCT"] });
  });
});

describe("test payments", () => {
  it("PAYMENT_SUCCESS", async () => {
    const h = harness();
    await orderEndToEnd(h, h.ctx(), [{ sku: "HEI6PK", quantity: 1 }], { payment_method_id: "PM_SUCCESS" });
    expect(h.backend.paymentLedger()[0]!.status).toBe("CAPTURED");
  });

  it("PAYMENT_DECLINED: not charged, intent closed, suggests another method", async () => {
    const h = harness();
    const e = await expectCode(orderEndToEnd(h, h.ctx(), [{ sku: "HEI6PK", quantity: 1 }], { payment_method_id: "PM_DECLINED" }), "PAYMENT_DECLINED");
    expect(e.suggestedAction).toBe("CHOOSE_ANOTHER_PAYMENT_METHOD");
    expect(h.backend.paymentLedger()).toHaveLength(0);
  });

  it("PAYMENT_TIMEOUT then PAYMENT_RETRY: the retry learns the outcome, one authorization total", async () => {
    const h = harness();
    const ctx = h.ctx();
    const c = await intentFor(h, ctx, [{ sku: "HEI6PK", quantity: 1 }], { payment_method_id: "PM_TIMEOUT" });
    const id = c.order_intent.order_intent_id;
    const a = await h.rt.service.confirmOrderIntent(ctx, id, { decision: "CONFIRM", confirmed_total: c.order_intent.total, confirmed_currency: "NZD", evidence: { method: "T", device_authenticated: true } }, { kind: "MCP_ELICITATION" });
    const e = await expectCode(h.rt.service.authorizePayment(ctx, { order_intent_id: id, authorization_token: a.authorization_token! }), "PAYMENT_TIMEOUT");
    expect(e.suggestedAction).toBe("CHECK_ORDER_STATUS");
    const retry = await h.rt.service.authorizePayment(ctx, { order_intent_id: id, authorization_token: a.authorization_token! });
    expect(retry.authorized).toBe(true);
    expect(h.backend.paymentLedger()).toHaveLength(1);
    const placed = await h.rt.service.placeOrder(ctx, { order_intent_id: id, payment_authorization_id: retry.payment_authorization_id, idempotency_key: "timeout-retry-1" });
    expect(placed.success).toBe(true);
  });

  it("PAYMENT_DUPLICATE: a repeated place_order is answered from the original", async () => {
    const h = harness();
    const ctx = h.ctx();
    const { created, pay, placed } = await orderEndToEnd(h, ctx, [{ sku: "HEI6PK", quantity: 1 }]);
    const dup = await h.rt.service.placeOrder(ctx, { order_intent_id: created.order_intent.order_intent_id, payment_authorization_id: pay.payment_authorization_id, idempotency_key: `k-${created.order_intent.order_intent_id}` });
    expect(dup).toMatchObject({ order_id: placed.order_id, idempotent_replay: true });
  });

  it("Apple Pay wallet tokens are used once and never stored", async () => {
    const h = harness();
    const ctx = h.ctx();
    const c = await intentFor(h, ctx, [{ sku: "HEI6PK", quantity: 1 }], { wallet: "APPLE_PAY" });
    expect(c.order_intent.payment_method).toEqual({ id: "WALLET:APPLE_PAY", label: "Apple Pay" });
    const id = c.order_intent.order_intent_id;
    const a = await h.rt.service.confirmOrderIntent(ctx, id, { decision: "CONFIRM", confirmed_total: c.order_intent.total, confirmed_currency: "NZD", evidence: { method: "T", device_authenticated: true } }, { kind: "MCP_ELICITATION" });
    await expectCode(h.rt.service.authorizePayment(ctx, { order_intent_id: id, authorization_token: a.authorization_token! }), "PAYMENT_REQUIRED");
    const p = await h.rt.service.authorizePayment(ctx, { order_intent_id: id, authorization_token: a.authorization_token!, wallet_token: "tok_applepay_sandbox_ok" });
    expect(p.authorized).toBe(true);
    expect(JSON.stringify(h.rt.audit.all())).not.toContain("tok_applepay_sandbox_ok");
  });
});

describe("test delivery", () => {
  it("DELIVERY_AVAILABLE", async () => {
    const h = harness();
    const ctx = h.ctx();
    const s = await session(h, ctx);
    expect(await h.rt.service.validateDelivery(ctx, { session_id: s.session_id })).toMatchObject({ eligible: true, estimated_delivery_minutes: 45, address_id: "ADDR_HOME" });
  });

  it("OUTSIDE_ZONE", async () => {
    const h = harness();
    await expectCode(intentFor(h, h.ctx(), [{ sku: "HEI6PK", quantity: 1 }], { address_id: "ADDR_OUTSIDE_ZONE" }), "OUTSIDE_SERVICE_AREA");
  });

  it("DELIVERY_UNAVAILABLE (no courier capacity)", async () => {
    const h = harness();
    await expectCode(intentFor(h, h.ctx(), [{ sku: "HEI6PK", quantity: 1 }], { address_id: "ADDR_NO_CAPACITY" }), "DELIVERY_UNAVAILABLE");
  });

  it("STORE_CLOSED outside licensed hours, Auckland time", async () => {
    const h = harness();
    h.clock.now = new Date(NZ_3AM);
    const ctx = h.ctx();
    const s = await session(h, ctx);
    expect(await h.rt.service.validateDelivery(ctx, { session_id: s.session_id })).toMatchObject({ eligible: false, reason: "STORE_CLOSED" });
    await expectCode(intentFor(h, ctx, [{ sku: "HEI6PK", quantity: 1 }]), "STORE_CLOSED");
  });

  it("an address on someone else's account is not found", async () => {
    const h = harness();
    await expectCode(intentFor(h, h.ctx(), [{ sku: "HEI6PK", quantity: 1 }], { address_id: "ADDR_NP_HOME" }), "ADDRESS_NOT_FOUND");
  });
});

describe("other tools", () => {
  it("calculate_cart applies a promotion and is labelled informational", async () => {
    const h = harness();
    const ctx = h.ctx();
    const s = await session(h, ctx);
    const q = await h.rt.service.calculateCart(ctx, { session_id: s.session_id, items: [{ sku: "HEI12PK", quantity: 2 }], promotion_code: "VOICE10" });
    expect(q).toMatchObject({ subtotal: 69.98, delivery_fee: 3.99, service_fee: 3.99, packaging_fee: 0.99, discount: 10, total: 68.95, promotion_code: "VOICE10" });
  });

  it("reorder rebuilds the last order at today's prices and says so when it changed", async () => {
    const h = harness();
    const ctx = h.ctx();
    await orderEndToEnd(h, ctx, [{ sku: "HEI12PK", quantity: 1 }]);
    h.backend.setPrice("HEI12PK", 3999);
    const s = await session(h, ctx);
    const r = await h.rt.service.reorderPrevious(ctx, { session_id: s.session_id });
    expect(r.order_intent).toMatchObject({ source: "REORDER", status: "AWAITING_CONFIRMATION", total: 48.96 });
    expect(r.previous_order.total).toBe(43.96);
    expect(r.total_changed).toBe(true);
    expect(r.speech).toMatch(/^Your last order cost \$43\.96, and prices have changed since\. Your last GIMME order is one Heineken 12-pack\. It's \$48\.96 delivered to home\. Want me to place it\?$/);
  });

  it("cancel works before dispatch and reports the real state after", async () => {
    const h = harness();
    const ctx = h.ctx();
    const a = await orderEndToEnd(h, ctx, [{ sku: "HEI6PK", quantity: 1 }]);
    expect(await h.rt.service.cancelOrder(ctx, { order_id: a.placed.order_id })).toMatchObject({ success: true, status: "CANCELLED" });
    expect(h.backend.getStock("HEI6PK")).toBe(200);

    const b = await orderEndToEnd(h, ctx, [{ sku: "HEI6PK", quantity: 1 }]);
    for (const st of ["ACCEPTED", "PREPARING", "DISPATCHED"] as const) h.backend.setOrderStatus(b.placed.order_id, st);
    const r = await h.rt.service.cancelOrder(ctx, { order_id: b.placed.order_id });
    expect(r).toMatchObject({ success: false, reason: "ORDER_ALREADY_DISPATCHED", status: "DISPATCHED" });
  });

  it("receipt is safe: labels only", async () => {
    const h = harness();
    const ctx = h.ctx();
    const { placed } = await orderEndToEnd(h, ctx, [{ sku: "HEI6PK", quantity: 2 }]);
    const r = await h.rt.service.getReceipt(ctx, { order_id: placed.order_id });
    expect(r).toMatchObject({ payment: "Visa ending 4242", delivered_to: "Home, Ponsonby", total: 48.95 });
    expect(r.speech).toBe(`Your GIMME order ${placed.order_id} came to $48.95, paid with Visa ending 4242.`);
  });

  it("a new basket supersedes the session's unconfirmed one", async () => {
    const h = harness();
    const ctx = h.ctx();
    const s = await session(h, ctx);
    const first = await h.rt.service.createOrderIntent(ctx, { session_id: s.session_id, items: [{ sku: "HEI6PK", quantity: 1 }], source: "BASKET" });
    await h.rt.service.createOrderIntent(ctx, { session_id: s.session_id, items: [{ sku: "HEI12PK", quantity: 1 }], source: "BASKET" });
    expect((await h.rt.service.getOrderIntent(ctx, { order_intent_id: first.order_intent.order_intent_id })).order_intent.status).toBe("SUPERSEDED");
  });

  it("create_order_intent is idempotent per key", async () => {
    const h = harness();
    const ctx = h.ctx();
    const s = await session(h, ctx);
    const args = { session_id: s.session_id, items: [{ sku: "HEI6PK", quantity: 1 }], source: "BASKET" as const, idempotency_key: "intent-key-123" };
    const a = await h.rt.service.createOrderIntent(ctx, args);
    const b = await h.rt.service.createOrderIntent(ctx, args);
    expect(b.order_intent.order_intent_id).toBe(a.order_intent.order_intent_id);
    await expectCode(h.rt.service.createOrderIntent(ctx, { ...args, items: [{ sku: "HEI6PK", quantity: 2 }] }), "IDEMPOTENCY_CONFLICT");
  });
});
