/**
 * The MCP server through a real MCP client: tool listing, schemas, the full
 * ordering flow with form elicitation, the out-of-band confirmation path, and
 * structured errors.
 */

import { describe, expect, it } from "vitest";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import { ElicitRequestSchema } from "@modelcontextprotocol/sdk/types.js";
import { createMcpServer } from "../src/mcp/server.js";
import { TOOLS } from "../src/mcp/tools.js";
import { harness } from "./helpers.js";
import { AGENT_SCOPES } from "../src/security/scopes.js";

async function connect(h: ReturnType<typeof harness>, opts: { elicitation?: "form" | "url" | "none"; answer?: (msg: string) => { action: "accept" | "decline" | "cancel"; content?: Record<string, unknown> } } = {}) {
  const principal = { customerId: "TEST_CUSTOMER", clientId: "mcp-test-agent", scopes: [...AGENT_SCOPES], tokenId: "t", expiresAt: 9e9 };
  const server = createMcpServer(h.rt, { stdioPrincipal: principal });
  const mode = opts.elicitation ?? "none";
  const client = new Client(
    { name: "test-agent", version: "1.0.0" },
    { capabilities: mode === "none" ? {} : { elicitation: mode === "form" ? { form: {} } : { url: {} } } },
  );
  const prompts: string[] = [];
  if (mode !== "none") {
    client.setRequestHandler(ElicitRequestSchema, async (req) => {
      prompts.push(req.params.message);
      return opts.answer ? opts.answer(req.params.message) : { action: "accept", content: { confirm: true } };
    });
  }
  const [a, b] = InMemoryTransport.createLinkedPair();
  await Promise.all([server.connect(a), client.connect(b)]);
  const call = async (name: string, args: Record<string, unknown>) => {
    const r = await client.callTool({ name, arguments: args });
    return { isError: !!r.isError, data: r.structuredContent as Record<string, any>, text: (r.content as { text: string }[])[0]!.text };
  };
  return { client, call, prompts };
}

describe("MCP server", () => {
  it("lists every tool with strict input and output schemas and GIMME metadata", async () => {
    const { client } = await connect(harness());
    const { tools } = await client.listTools();
    expect(tools.map((t) => t.name).sort()).toEqual(TOOLS.map((t) => t.name).sort());
    for (const t of tools) {
      expect(t.inputSchema.type).toBe("object");
      expect(t.outputSchema?.type).toBe("object");
      expect(t._meta?.["gimme/contract_version"]).toBe("1.0");
    }
    const place = tools.find((t) => t.name === "place_order")!;
    expect(place.annotations).toMatchObject({ destructiveHint: true, readOnlyHint: false, idempotentHint: true });
    expect(place._meta?.["gimme/required_scopes"]).toEqual(["gimme.order.create"]);
    expect(place.inputSchema.required).toContain("idempotency_key");
  });

  it("orders end to end, with the customer confirming through elicitation", async () => {
    const h = harness();
    const { call, prompts } = await connect(h, { elicitation: "form" });
    const id = await call("identify_customer", { platform: "MCP_AGENT", platform_user_id: "agent-user", conversation_id: "c1" });
    expect(id.isError).toBe(false);
    const sid = id.data.session_id;

    const resolved = await call("resolve_items", { session_id: sid, request: { type: "ITEMS", items: [{ query: "Heinekens", quantity: 12, unit: "UNIT" }] } });
    expect(resolved.data.items).toEqual([{ sku: "HEI12PK", quantity: 1, name: "Heineken 12 Pack 330ml Bottles" }]);

    const intent = await call("create_order_intent", { session_id: sid, items: [{ sku: "HEI12PK", quantity: 1 }] });
    expect(intent.data.speech).toBe("That's one Heineken 12-pack. It's $43.96 delivered to home. Want me to place it?");
    const oid = intent.data.order_intent.order_intent_id;

    const auth = await call("request_authorization", { order_intent_id: oid });
    expect(auth.data.status).toBe("AUTHORIZED");
    expect(prompts).toEqual(["Confirm your GIMME order: 1 x Heineken 12 Pack 330ml Bottles — $43.96 NZD, delivered to Home, paid with Visa ending 4242."]);

    const pay = await call("authorize_payment", { order_intent_id: oid, authorization_token: auth.data.authorization_token });
    expect(pay.data).toMatchObject({ authorized: true, amount: 43.96 });
    const placed = await call("place_order", { order_intent_id: oid, payment_authorization_id: pay.data.payment_authorization_id, idempotency_key: `MCP_AGENT:TEST_CUSTOMER:c1:${oid}` });
    expect(placed.data).toMatchObject({ success: true, status: "CONFIRMED", estimated_delivery_minutes: 45 });
    expect(placed.text.startsWith("Done. Your GIMME order is confirmed")).toBe(true);

    const status = await call("get_order_status", {});
    expect(status.data).toMatchObject({ order_id: placed.data.order_id, status: "CONFIRMED" });
    // Every tool call is in the audit trail.
    expect(h.rt.audit.all().filter((e) => e.action.startsWith("mcp.tool.")).map((e) => e.action)).toEqual([
      "mcp.tool.identify_customer",
      "mcp.tool.resolve_items",
      "mcp.tool.create_order_intent",
      "mcp.tool.request_authorization",
      "mcp.tool.authorize_payment",
      "mcp.tool.place_order",
      "mcp.tool.get_order_status",
    ]);
  });

  it("customer declining in the elicitation cancels the intent", async () => {
    const h = harness();
    const { call } = await connect(h, { elicitation: "form", answer: () => ({ action: "decline" }) });
    const sid = (await call("identify_customer", { platform: "MCP_AGENT", platform_user_id: "u" })).data.session_id;
    const oid = (await call("create_order_intent", { session_id: sid, items: [{ sku: "HEI6PK", quantity: 1 }] })).data.order_intent.order_intent_id;
    const r = await call("request_authorization", { order_intent_id: oid });
    expect(r.data.status).toBe("DECLINED");
    const pay = await call("authorize_payment", { order_intent_id: oid });
    expect(pay.isError).toBe(true);
    expect(pay.data.error_code).toBe("ORDER_INTENT_INVALID_STATE");
  });

  it("without elicitation, the customer confirms on GIMME's hosted page; the agent cannot", async () => {
    const h = harness();
    const { call } = await connect(h, { elicitation: "none" });
    const sid = (await call("identify_customer", { platform: "MCP_AGENT", platform_user_id: "u" })).data.session_id;
    const oid = (await call("create_order_intent", { session_id: sid, items: [{ sku: "HEI6PK", quantity: 1 }] })).data.order_intent.order_intent_id;
    const r = await call("request_authorization", { order_intent_id: oid });
    expect(r.data.status).toBe("PENDING_CUSTOMER_CONFIRMATION");
    expect(r.data.confirmation_url).toMatch(new RegExp(`/v1/voice/confirm/${oid}\\?nonce=`));

    // The agent tries to pay before the customer confirms.
    const early = await call("authorize_payment", { order_intent_id: oid });
    expect(early.data.error_code).toBe("AUTHORIZATION_REQUIRED");

    // The customer confirms on the page (nonce from the link).
    const nonce = new URL(r.data.confirmation_url).searchParams.get("nonce")!;
    const d = await h.rt.service.describeIntentForConfirmation(oid, nonce);
    await h.rt.service.confirmOrderIntent(
      { principal: { customerId: d.customer_id, clientId: "gimme-hosted-confirmation", scopes: [], tokenId: "h", expiresAt: 0 }, requestId: "r", correlationId: "r", channel: "VOICE_API", apiVersion: "1.0" },
      oid,
      { decision: "CONFIRM", confirmed_total: d.total, confirmed_currency: "NZD", evidence: { method: "HOSTED_CONFIRMATION_PAGE", device_authenticated: false } },
      { kind: "HOSTED_PAGE", nonce },
    );
    // The nonce is single-use.
    await expect(h.rt.service.describeIntentForConfirmation(oid, nonce)).rejects.toThrow();

    expect((await call("get_order_intent", { order_intent_id: oid })).data.order_intent.status).toBe("CONFIRMED");
    const pay = await call("authorize_payment", { order_intent_id: oid });
    expect(pay.data.authorized).toBe(true);
    const placed = await call("place_order", { order_intent_id: oid, payment_authorization_id: pay.data.payment_authorization_id, idempotency_key: "hosted-flow-1" });
    expect(placed.data.success).toBe(true);
  });

  it("returns structured, machine-readable errors", async () => {
    const h = harness();
    h.backend.setStock("HEI12PK", 0);
    const { call } = await connect(h);
    const sid = (await call("identify_customer", { platform: "MCP_AGENT", platform_user_id: "u" })).data.session_id;
    const r = await call("create_order_intent", { session_id: sid, items: [{ sku: "HEI12PK", quantity: 1 }] });
    expect(r.isError).toBe(true);
    expect(r.data).toMatchObject({ success: false, error_code: "PRODUCT_UNAVAILABLE", recoverable: true, suggested_action: "SHOW_ALTERNATIVES" });
    expect(r.data.details.alternatives.length).toBeGreaterThan(0);
    expect(r.data.request_id).toMatch(/^req_/);
  });

  it("rejects input that violates the schema", async () => {
    const { call } = await connect(harness());
    const r = await call("create_order_intent", { session_id: "x", items: [{ sku: "HEI12PK", quantity: 0 }] });
    expect(r.isError).toBe(true);
  });
});
