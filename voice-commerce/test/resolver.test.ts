import { describe, expect, it } from "vitest";
import { resolveItem } from "../src/core/resolver.js";
import { SandboxBackend } from "../src/backend/sandbox/sandbox-backend.js";
import { spokenName, speakLine } from "../src/core/speech.js";
import { harness, session } from "./helpers.js";

const catalog = new SandboxBackend().catalog;

describe("product resolution", () => {
  it('"a dozen Heinekens" is one Heineken 12-pack', async () => {
    const r = await resolveItem(catalog, { query: "Heinekens", quantity: 12, unit: "UNIT" });
    expect(r).toMatchObject({ status: "RESOLVED", item: { sku: "HEI12PK", quantity: 1 } });
  });

  it('"18 Heinekens" is three 6-packs (the only pack size that makes 18 exactly)', async () => {
    const r = await resolveItem(catalog, { query: "Heineken", quantity: 18, unit: "UNIT" });
    expect(r).toMatchObject({ status: "RESOLVED", item: { sku: "HEI6PK", quantity: 3 } });
  });

  it('"two Heineken 12 packs" is two 12-packs', async () => {
    const r = await resolveItem(catalog, { query: "Heineken 12 pack", quantity: 2 });
    expect(r).toMatchObject({ status: "RESOLVED", item: { sku: "HEI12PK", quantity: 2 } });
  });

  it('"Heineken" alone asks which pack, in one short sentence', async () => {
    const r = await resolveItem(catalog, { query: "Heineken" });
    expect(r.status).toBe("NEEDS_CLARIFICATION");
    if (r.status !== "NEEDS_CLARIFICATION") return;
    expect(r.options.map((o) => o.sku)).toEqual(["HEI6PK", "HEI12PK", "HEI24PK"]);
    expect(r.question).toBe("We have three Heineken options: Heineken 6-pack, Heineken 12-pack and Heineken 24-pack. Which one?");
  });

  it("never picks the alcohol-free variant unless asked", async () => {
    const plain = await resolveItem(catalog, { query: "Heineken six pack" });
    expect(plain).toMatchObject({ status: "RESOLVED", item: { sku: "HEI6PK" } });
    const zero = await resolveItem(catalog, { query: "Heineken zero" });
    expect(zero).toMatchObject({ status: "RESOLVED", item: { sku: "HEI00-6PK" } });
  });

  it('"German beers" finds German beers only', async () => {
    const r = await resolveItem(catalog, { query: "German beers" });
    expect(r.status).toBe("NEEDS_CLARIFICATION");
    if (r.status === "NEEDS_CLARIFICATION") expect(r.options.map((o) => o.sku).sort()).toEqual(["BEC12PK", "WAR6PK"]);
  });

  it("unknown products are not found, not guessed", async () => {
    expect(await resolveItem(catalog, { query: "Guinness" })).toMatchObject({ status: "NOT_FOUND" });
    expect(await resolveItem(catalog, { sku: "UNKNOWN_PRODUCT" })).toMatchObject({ status: "NOT_FOUND" });
  });

  it("speaks product names briefly", () => {
    expect(spokenName("Heineken 12 Pack 330ml Bottles")).toBe("Heineken 12-pack");
    expect(spokenName("Coca-Cola 1.5L")).toBe("Coca-Cola");
    expect(speakLine(2, "Heineken 12 Pack 330ml Bottles")).toBe("two Heineken 12-packs");
  });

  it("resolve_items reports clarification through the service", async () => {
    const h = harness();
    const ctx = h.ctx();
    const s = await session(h, ctx);
    const r = await h.rt.service.resolveItems(ctx, { session_id: s.session_id, request: { type: "ITEMS", items: [{ query: "Heineken" }] } });
    expect(r.status).toBe("NEEDS_CLARIFICATION");
    expect(r.speech).toBe(r.clarification!.question);
  });
});
