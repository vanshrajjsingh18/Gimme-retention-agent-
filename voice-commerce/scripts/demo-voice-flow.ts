/**
 * Prints example voice conversations (§18, §19) produced by the real system
 * against the sandbox: every line GIMME "says" below is actual output.
 *
 *   npm run demo
 */

import { createRuntime } from "../src/app.js";
import { loadConfig } from "../src/config.js";
import { VoiceDialog, type DialogTurn } from "../src/adapters/dialog.js";
import { fromApple, toApple, type AppleIntentRequest } from "../src/adapters/apple.js";
import { fromGoogle, toGoogle, type GoogleFulfillmentRequest } from "../src/adapters/google.js";
import { SandboxBackend } from "../src/backend/sandbox/sandbox-backend.js";
import type { CallContext } from "../src/core/models.js";
import { silentLogger } from "../src/observability/logger.js";
import { ALL_SCOPES } from "../src/security/scopes.js";

const backend = new SandboxBackend();
const rt = createRuntime({ config: loadConfig({}), log: silentLogger, backend, now: () => new Date("2026-10-05T07:30:00Z") /* 8:30pm Auckland */ });
const dialog = new VoiceDialog(rt);
const ctx = (customerId = "TEST_CUSTOMER"): CallContext => ({
  principal: { customerId, clientId: "gimme-ios-app", scopes: [...ALL_SCOPES], tokenId: "demo", expiresAt: 0 },
  requestId: "demo",
  correlationId: "demo",
  channel: "ADAPTER",
  apiVersion: "1.0",
});

const say = (who: string, text: string) => process.stdout.write(`  ${who.padEnd(9)} ${text}\n`);

async function siri(customer: string, said: string, req: Omit<AppleIntentRequest, "apple_user_id" | "interaction_id">, interaction: string, appleUser = "apple-user-test") {
  say("Customer:", said);
  const res = toApple(await dialog.handle(ctx(customer), fromApple({ ...req, apple_user_id: appleUser, interaction_id: interaction })));
  say("Siri:", res.dialog.full);
  if (res.needs_confirmation) say("", `[Siri confirmation sheet: ${res.needs_confirmation.summary}]`);
  if (res.disambiguation) say("", `[options: ${res.disambiguation.map((d) => `${d.title} ${d.subtitle}`).join(" | ")}]`);
  if (res.open_app) say("", `[opens GIMME app: ${res.open_app.reason}]`);
  return res;
}

async function google(said: string, req: Omit<GoogleFulfillmentRequest, "google_user_id" | "conversation_id">, conversation: string) {
  say("Customer:", said);
  const res = toGoogle(await dialog.handle(ctx(), fromGoogle({ ...req, google_user_id: "google-user-test", conversation_id: conversation })));
  say("Google:", res.prompt.speech);
  if (res.suggestions) say("", `[options: ${res.suggestions.map((s) => `${s.title} $${s.price.toFixed(2)}`).join(" | ")}]`);
  return res;
}

const heading = (t: string) => process.stdout.write(`\n${t}\n${"-".repeat(t.length)}\n`);

heading("1. Siri — order my usual");
await siri("TEST_CUSTOMER", "Hey Siri, order my usual from GIMME.", { intent: "OrderUsualFromGIMMEIntent" }, "s1");
say("Customer:", "Yes.   [Face ID passes]");
const placed = toApple(await dialog.handle(ctx(), fromApple({ intent: "ConfirmGIMMEOrderIntent", apple_user_id: "apple-user-test", interaction_id: "s1", confirmation: { confirmed_total: 111.94, device_authenticated: true } })));
say("Siri:", placed.dialog.full);

heading("2. Siri — where's my order?");
backend.setOrderStatus(placed.order!.order_id, "ACCEPTED");
backend.setOrderStatus(placed.order!.order_id, "PREPARING");
backend.setOrderStatus(placed.order!.order_id, "DISPATCHED");
await siri("TEST_CUSTOMER", "Hey Siri, where's my GIMME order?", { intent: "GetGIMMEOrderStatusIntent" }, "s2");

heading("3. Siri — a dozen Heinekens, device locked");
await siri("TEST_CUSTOMER", "Hey Siri, order me a dozen Heinekens from GIMME.", { intent: "OrderFromGIMMEIntent", parameters: { product_query: "Heinekens", quantity: 12, unit: "UNIT" } }, "s3");
say("Customer:", "Yes.   [phone is locked in a pocket]");
const locked = toApple(await dialog.handle(ctx(), fromApple({ intent: "ConfirmGIMMEOrderIntent", apple_user_id: "apple-user-test", interaction_id: "s3", confirmation: { confirmed_total: 43.96, device_authenticated: false } })));
say("Siri:", locked.dialog.full);

heading("4. Google — clarification");
const g1 = await google("Hey Google, get me some Heineken from GIMME.", { handler: "gimme.order.items", params: { items: [{ query: "Heineken", quantity: 1 }] } }, "g1");
await google("The six-pack.", { handler: "gimme.order.select", session_id: g1.session_id, params: { selected_sku: "HEI6PK" } }, "g1");
await google("Yes.", { handler: "gimme.order.confirm", confirmation: { confirmed_total: 28.96, device_authenticated: true } }, "g1");

heading("5. Google — price changes between yes and checkout");
await google("Hey Google, order the Sandbox IPA from GIMME.", { handler: "gimme.order.items", params: { items: [{ sku: "PRICE_CHANGED_PRODUCT", quantity: 1 }] } }, "g2");
await google("Yes.", { handler: "gimme.order.confirm", confirmation: { confirmed_total: 28.97, device_authenticated: true } }, "g2");
await google("Yes, go ahead.", { handler: "gimme.order.confirm", confirmation: { confirmed_total: 36.47, device_authenticated: true } }, "g2");

heading("6. Google — out of stock, no silent substitution");
backend.setStock("COR12PK", 0);
await google("Hey Google, order a Corona twelve-pack from GIMME.", { handler: "gimme.order.items", params: { items: [{ query: "Corona 12 pack", quantity: 1 }] } }, "g3");

heading("7. Siri — customer still needs age verification");
await siri("TEST_CUSTOMER_AGE_REVIEW", "Hey Siri, order my usual from GIMME.", { intent: "OrderUsualFromGIMMEIntent" }, "s4", "apple-user-agereview");

heading("Ledger");
for (const p of backend.paymentLedger()) say("", `${p.reference}  $${(p.amountCents / 100).toFixed(2)}  ${p.status}`);
process.stdout.write(`\nAudit trail: ${rt.audit.all().length} entries, chain ${rt.audit.verify() === null ? "intact" : "BROKEN"}.\n`);
