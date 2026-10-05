/**
 * §18/§19 example conversations through the Apple and Google adapters, as
 * the platform would send them, turn by turn.
 */

import { describe, expect, it } from "vitest";
import request from "supertest";
import { createHttpApp } from "../src/http/app.js";
import { mintSandboxToken } from "../src/security/access-tokens.js";
import { ALL_SCOPES } from "../src/security/scopes.js";
import { harness } from "./helpers.js";

async function setup() {
  const h = harness();
  const app = createHttpApp(h.rt);
  const token = await mintSandboxToken(h.config, { customerId: "TEST_CUSTOMER", scopes: ALL_SCOPES, clientId: "gimme-ios-app" });
  const apple = (body: Record<string, unknown>) =>
    request(app).post("/v1/voice/adapters/apple/intents").set("authorization", `Bearer ${token}`).send({ apple_user_id: "apple-user-test", interaction_id: "siri-abc", ...body });
  const google = (body: Record<string, unknown>) =>
    request(app).post("/v1/voice/adapters/google/fulfillment").set("authorization", `Bearer ${token}`).send({ google_user_id: "google-user-test", conversation_id: "g-1", ...body });
  return { h, apple, google };
}

describe("Siri: \"Hey Siri, order my usual from GIMME\"", () => {
  it("reads back the order, confirms natively, places it", async () => {
    const { h, apple } = await setup();
    const t1 = await apple({ intent: "OrderUsualFromGIMMEIntent" });
    expect(t1.status).toBe(200);
    expect(t1.body.dialog.full).toBe("Your usual GIMME order is two Heineken 12-packs and one Cloudy Bay Sauvignon Blanc. It's $111.94 delivered to home. Want me to place it?");
    expect(t1.body.needs_confirmation).toMatchObject({ amount: 111.94, currency: "NZD", merchant: "GIMME", requires_device_authentication: true });

    // App Intent ran requestConfirmation on an authenticated device; customer said "Yes".
    const t2 = await apple({ intent: "ConfirmGIMMEOrderIntent", confirmation: { confirmed_total: 111.94, device_authenticated: true, confirmation_id: "apple-conf-1" } });
    expect(t2.body.dialog.full).toBe("Done. Your GIMME order is confirmed and should arrive in about 45 minutes.");
    expect(t2.body.order).toMatchObject({ status: "CONFIRMED", estimated_minutes: 45, total: 111.94 });
    expect(h.backend.paymentLedger()).toHaveLength(1);

    // "Hey Siri, where's my GIMME order?"
    const t3 = await apple({ intent: "GetGIMMEOrderStatusIntent", interaction_id: "siri-def" });
    expect(t3.body.dialog.full).toBe("Your GIMME order is confirmed and should arrive in about 45 minutes.");
  });

  it("falls back to on-device confirmation when the device isn't authenticated", async () => {
    const { h, apple } = await setup();
    await apple({ intent: "OrderUsualFromGIMMEIntent" });
    const t2 = await apple({ intent: "ConfirmGIMMEOrderIntent", confirmation: { confirmed_total: 111.94, device_authenticated: false } });
    expect(t2.body.dialog.full).toBe("I need you to confirm this purchase on your device.");
    expect(t2.body.open_app).toEqual({ reason: "CONFIRM_ON_DEVICE" });
    expect(h.backend.paymentLedger()).toHaveLength(0);
  });

  it('"No" places nothing', async () => {
    const { h, apple } = await setup();
    await apple({ intent: "OrderUsualFromGIMMEIntent" });
    const t2 = await apple({ intent: "DeclineGIMMEOrderIntent", confirmation: { confirmed_total: 111.94, device_authenticated: true } });
    expect(t2.body.dialog.full).toBe("No problem, I haven't placed the order.");
    expect(h.backend.paymentLedger()).toHaveLength(0);
  });

  it('"order me a dozen Heinekens" goes straight to a 12-pack', async () => {
    const { apple } = await setup();
    const t1 = await apple({ intent: "OrderFromGIMMEIntent", parameters: { product_query: "Heinekens", quantity: 12, unit: "UNIT" } });
    expect(t1.body.dialog.full).toBe("That's one Heineken 12-pack. It's $43.96 delivered to home. Want me to place it?");
  });

  it("an age-unverified customer is handed off to the app", async () => {
    const h = harness();
    const app = createHttpApp(h.rt);
    const token = await mintSandboxToken(h.config, { customerId: "TEST_CUSTOMER_AGE_REVIEW", scopes: ALL_SCOPES, clientId: "gimme-ios-app" });
    const r = await request(app)
      .post("/v1/voice/adapters/apple/intents")
      .set("authorization", `Bearer ${token}`)
      .send({ intent: "OrderUsualFromGIMMEIntent", apple_user_id: "apple-user-agereview", interaction_id: "i" });
    expect(r.body.dialog.full).toBe("You'll need to verify your age in the GIMME app before ordering.");
    expect(r.body.open_app).toEqual({ reason: "VERIFY_AGE_IN_APP" });
  });
});

describe("Google: clarification, price change, reorder", () => {
  it('"get me some Heineken" asks which pack, then orders the one chosen', async () => {
    const { google } = await setup();
    const t1 = await google({ handler: "gimme.order.items", params: { items: [{ query: "Heineken", quantity: 1 }] } });
    expect(t1.body.prompt.speech).toBe("We have three Heineken options: Heineken 6-pack, Heineken 12-pack and Heineken 24-pack. Which one?");
    expect(t1.body.expect).toBe("SELECTION");
    const t2 = await google({ handler: "gimme.order.select", session_id: t1.body.session_id, params: { selected_sku: "HEI6PK" } });
    expect(t2.body.prompt.speech).toBe("That's one Heineken 6-pack. It's $28.96 delivered to home. Want me to place it?");
    expect(t2.body.transaction_decision.amount).toEqual({ currency_code: "NZD", units: 28, nanos: 960_000_000 });
    const t3 = await google({ handler: "gimme.order.confirm", confirmation: { confirmed_total: 28.96, device_authenticated: true } });
    expect(t3.body.prompt.speech).toMatch(/^Done\./);
  });

  it("a price rise between yes and placement is put back to the customer", async () => {
    const { h, google } = await setup();
    const t1 = await google({ handler: "gimme.order.items", params: { items: [{ sku: "PRICE_CHANGED_PRODUCT", quantity: 1 }] } });
    expect(t1.body.transaction_decision.amount.units).toBe(28);
    const t2 = await google({ handler: "gimme.order.confirm", confirmation: { confirmed_total: 28.97, device_authenticated: true } });
    expect(t2.body.prompt.speech).toBe("The price has changed to $36.47. Do you want me to continue?");
    expect(t2.body.expect).toBe("CONFIRMATION");
    expect(h.backend.paymentLedger().every((p) => p.status === "VOIDED")).toBe(true);
    // Yes at the new price.
    const t3 = await google({ handler: "gimme.order.confirm", confirmation: { confirmed_total: 36.47, device_authenticated: true } });
    expect(t3.body.prompt.speech).toMatch(/^Done\./);
    expect(t3.body.order.total).toBe(36.47);
  });

  it('"Hey Google, reorder my last GIMME order" revalidates', async () => {
    const { google } = await setup();
    await google({ handler: "gimme.order.items", params: { items: [{ sku: "COR12PK", quantity: 1 }] } });
    await google({ handler: "gimme.order.confirm", confirmation: { confirmed_total: 45.96, device_authenticated: true } });
    const r = await google({ handler: "gimme.order.reorder_last", conversation_id: "g-2" });
    expect(r.body.prompt.speech).toBe("Your last GIMME order is one Corona Extra 12-pack. It's $45.96 delivered to home. Want me to place it?");
  });

  it("unknown handlers are refused", async () => {
    const { google } = await setup();
    const r = await google({ handler: "gimme.make.coffee" });
    expect(r.body.error_code).toBe("PLATFORM_NOT_SUPPORTED");
  });
});
