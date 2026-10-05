import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { mcpToolsJson, mcpToolsMarkdown, openApi } from "../scripts/generate-specs.js";
import { TOOLS } from "../src/mcp/tools.js";

const read = (f: string) => readFileSync(new URL(`../docs/${f}`, import.meta.url), "utf8");

describe("generated specifications", () => {
  it("are up to date (run `npm run docs:generate` if this fails)", () => {
    expect(read("openapi.json")).toBe(`${JSON.stringify(openApi(), null, 2)}\n`);
    expect(read("mcp-tools.json")).toBe(`${JSON.stringify(mcpToolsJson(), null, 2)}\n`);
    expect(read("mcp-tools.md")).toBe(mcpToolsMarkdown());
  });

  it("every tool declares everything §42 asks for", () => {
    for (const t of mcpToolsJson().tools) {
      expect(t.inputSchema).toHaveProperty("type", "object");
      expect(t.outputSchema).toHaveProperty("type", "object");
      expect(t.required_scopes.length).toBeGreaterThan(0);
      expect(t.error_codes.length).toBeGreaterThan(0);
      expect(typeof t.side_effects).toBe("string");
      expect(typeof t.idempotency).toBe("string");
    }
  });

  it("classifies tools as §43 does", () => {
    const by = (c: string) => TOOLS.filter((t) => t.classification === c).map((t) => t.name);
    expect(by("HIGH_RISK_MUTATION").sort()).toEqual(["authorize_payment", "cancel_order", "place_order"]);
    expect(by("PRE_TRANSACTION")).toEqual(expect.arrayContaining(["calculate_cart", "create_order_intent"]));
    expect(by("READ")).toEqual(expect.arrayContaining(["identify_customer", "search_products", "get_customer_preferences", "check_inventory", "validate_delivery", "get_order_status", "get_order_receipt"]));
  });
});
