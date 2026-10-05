/**
 * MCP over Streamable HTTP with OAuth bearer tokens, through the SDK client.
 */

import { afterEach, describe, expect, it } from "vitest";
import type { Server } from "node:http";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import { ElicitRequestSchema } from "@modelcontextprotocol/sdk/types.js";
import { createHttpApp } from "../src/http/app.js";
import { mintSandboxToken } from "../src/security/access-tokens.js";
import { AGENT_SCOPES } from "../src/security/scopes.js";
import { harness } from "./helpers.js";

let server: Server | undefined;
afterEach(() => server?.close());

async function start() {
  const h = harness();
  const app = createHttpApp(h.rt);
  await new Promise<void>((r) => {
    server = app.listen(0, () => r());
  });
  const port = (server!.address() as { port: number }).port;
  return { h, url: new URL(`http://127.0.0.1:${port}/mcp`) };
}

async function client(url: URL, token: string, elicit = true) {
  const c = new Client({ name: "http-agent", version: "1.0.0" }, { capabilities: elicit ? { elicitation: { form: {} } } : {} });
  if (elicit) c.setRequestHandler(ElicitRequestSchema, async () => ({ action: "accept", content: { confirm: true } }));
  const transport = new StreamableHTTPClientTransport(url, { requestInit: { headers: { authorization: `Bearer ${token}` } } });
  await c.connect(transport);
  return { c, transport };
}

describe("MCP over HTTP", () => {
  it("refuses to initialize without a token, with an OAuth challenge", async () => {
    const { url } = await start();
    const r = await fetch(url, { method: "POST", headers: { "content-type": "application/json", accept: "application/json, text/event-stream" }, body: "{}" });
    expect(r.status).toBe(401);
    expect(r.headers.get("www-authenticate")).toContain("/.well-known/oauth-protected-resource/mcp");
  });

  it("places an order end to end, with elicitation round-tripping over HTTP", async () => {
    const { h, url } = await start();
    const token = await mintSandboxToken(h.config, { customerId: "TEST_CUSTOMER", scopes: AGENT_SCOPES, clientId: "http-agent" });
    const { c } = await client(url, token);
    const call = async (name: string, args: Record<string, unknown>) => (await c.callTool({ name, arguments: args })).structuredContent as Record<string, any>;
    const sid = (await call("identify_customer", { platform: "MCP_AGENT", platform_user_id: "u" })).session_id;
    const oid = (await call("create_order_intent", { session_id: sid, items: [{ sku: "STP12PK", quantity: 1 }] })).order_intent.order_intent_id;
    const auth = await call("request_authorization", { order_intent_id: oid });
    expect(auth.status).toBe("AUTHORIZED");
    const pay = await call("authorize_payment", { order_intent_id: oid, authorization_token: auth.authorization_token });
    const placed = await call("place_order", { order_intent_id: oid, payment_authorization_id: pay.payment_authorization_id, idempotency_key: "http-mcp-1" });
    expect(placed).toMatchObject({ success: true, total: 41.96 });
    await c.close();
  });

  it("pins an MCP session to the customer and client that opened it", async () => {
    const { h, url } = await start();
    const mine = await mintSandboxToken(h.config, { customerId: "TEST_CUSTOMER", scopes: AGENT_SCOPES, clientId: "http-agent" });
    const theirs = await mintSandboxToken(h.config, { customerId: "TEST_CUSTOMER_NO_PAYMENT", scopes: AGENT_SCOPES, clientId: "http-agent" });
    const { c, transport } = await client(url, mine, false);
    const r = await fetch(url, {
      method: "POST",
      headers: {
        authorization: `Bearer ${theirs}`,
        "mcp-session-id": transport.sessionId!,
        "mcp-protocol-version": "2025-06-18",
        "content-type": "application/json",
        accept: "application/json, text/event-stream",
      },
      body: JSON.stringify({ jsonrpc: "2.0", id: 9, method: "tools/list" }),
    });
    expect(r.status).toBe(403);
    await c.close();
  });
});
