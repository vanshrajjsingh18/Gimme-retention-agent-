/**
 * Product resolution (§5, §35): turn what a customer said into SKUs, or into
 * one short clarifying question. Deterministic — the platform's language
 * model extracts { query, quantity, unit } from speech; this decides what
 * that means against the live catalogue.
 *
 *   "a dozen Heinekens"      -> { query: "Heineken", quantity: 12, unit: "UNIT" } -> 1 x Heineken 12-pack
 *   "two Heineken 12 packs"  -> { query: "Heineken 12 pack", quantity: 2 }        -> 2 x Heineken 12-pack
 *   "some Heineken"          -> "We have three Heineken options: ... Which one?"
 *
 * It never substitutes: if the customer named a brand, only that brand is
 * offered, and a zero-alcohol variant is only chosen when asked for.
 */

import type { CatalogService } from "../backend/ports.js";
import type { Product } from "../domain/types.js";
import { sanitizeForModel } from "../security/guards.js";
import { spokenName, speech } from "./speech.js";

export interface ItemRequest {
  query?: string;
  sku?: string;
  quantity?: number;
  /** PACK (default): quantity counts packs. UNIT: quantity counts single bottles/cans. */
  unit?: "PACK" | "UNIT";
}

export interface ResolvedItem {
  sku: string;
  quantity: number;
  name: string;
}

export interface ResolutionOption {
  sku: string;
  name: string;
  pack_size: number;
  price_cents: number;
}

export type ItemResolution =
  | { status: "RESOLVED"; item: ResolvedItem }
  | { status: "NEEDS_CLARIFICATION"; query: string; question: string; options: ResolutionOption[] }
  | { status: "NOT_FOUND"; query: string };

const ZERO_ALCOHOL = /\b(zero|0\.0|non[- ]?alcoholic|alcohol[- ]free|no alcohol)\b/i;
const PACK_HINT = /\b(\d+)[- ]?(pack|pk|bottles|cans)\b|\b(six|twelve|dozen|twenty[- ]four|case)\b/i;
const PACK_WORDS: Record<string, number> = { six: 6, twelve: 12, dozen: 12, "twenty-four": 24, "twenty four": 24, case: 24 };

const NATIONALITY: Record<string, string> = { german: "germany", dutch: "netherlands", kiwi: "new zealand", nz: "new zealand", mexican: "mexico" };
const GENERIC = new Set(["beer", "beers", "wine", "wines", "drink", "drinks", "some", "the", "a", "an", "of", "pack", "packs", "bottles", "cans", "please", "me", "get", "order", "my", "from", "gimme", "and"]);

function haystack(p: Product): string {
  return [p.name, p.brand, p.category, p.country ?? "", ...p.tags].join(" ").toLowerCase();
}

/** Query words that should narrow the match (nationality mapped to country), minus generic words and pack hints. */
function distinctiveTokens(query: string): string[] {
  return query
    .toLowerCase()
    .replace(PACK_HINT, " ")
    .split(/[^a-z0-9.']+/)
    .filter((t) => t.length > 1 && !GENERIC.has(t) && !/^\d+$/.test(t) && !ZERO_ALCOHOL.test(t))
    .map((t) => NATIONALITY[t] ?? t.replace(/s$/, ""));
}

function packHint(query: string): number | undefined {
  const m = query.match(PACK_HINT);
  if (!m) return undefined;
  if (m[1]) return Number(m[1]);
  return PACK_WORDS[m[3]!.toLowerCase()];
}

export async function resolveItem(catalog: CatalogService, req: ItemRequest): Promise<ItemResolution> {
  const quantity = req.quantity ?? 1;

  if (req.sku) {
    const found = (await catalog.getProducts([req.sku])).get(req.sku);
    return found
      ? { status: "RESOLVED", item: { sku: found.sku, quantity, name: sanitizeForModel(found.name) } }
      : { status: "NOT_FOUND", query: req.sku };
  }

  const query = (req.query ?? "").trim();
  if (!query) return { status: "NOT_FOUND", query };
  let candidates = await catalog.search(query, { limit: 20 });
  if (candidates.length === 0) return { status: "NOT_FOUND", query };

  // Alcohol-free only when asked for; otherwise never silently pick it.
  const wantsZero = ZERO_ALCOHOL.test(query);
  const byAlcohol = candidates.filter((p) => (wantsZero ? !p.alcoholic : p.alcoholic));
  if (byAlcohol.length) candidates = byAlcohol;
  else if (wantsZero) return { status: "NOT_FOUND", query };

  // If the customer named a brand, stay within it.
  const q = query.toLowerCase();
  const named = candidates.filter((p) => q.includes(p.brand.toLowerCase()) || q.includes(p.brand.toLowerCase().replace(/'s$/, "")) || q.includes(`${p.brand.toLowerCase()}s`));
  if (named.length) candidates = named;

  // Every distinctive word must match: "German beers" keeps German beers, not every beer.
  for (const token of distinctiveTokens(query)) {
    const narrowed = candidates.filter((p) => haystack(p).includes(token));
    if (narrowed.length) candidates = narrowed;
  }

  const hinted = packHint(query);
  if (hinted !== undefined) {
    const sized = candidates.filter((p) => p.pack_size === hinted);
    if (sized.length) candidates = sized;
  }

  // "A dozen Heinekens": 12 units -> one 12-pack (or two 6-packs if that's all there is).
  if (req.unit === "UNIT") {
    const exact = candidates.filter((p) => p.pack_size === quantity);
    if (exact.length === 1) return resolved(exact[0]!, 1);
    if (exact.length === 0) {
      // Fewest packs that make the count exactly: the largest pack size that divides it.
      const best = candidates
        .filter((p) => quantity % p.pack_size === 0)
        .sort((a, b) => b.pack_size - a.pack_size)[0];
      if (best) return resolved(best, quantity / best.pack_size);
    }
  }

  if (candidates.length === 1) return resolved(candidates[0]!, quantity);

  const options = candidates.slice(0, 3).map(toOption);
  const sameBrand = new Set(candidates.map((p) => p.brand)).size === 1;
  const label = sameBrand ? sanitizeForModel(candidates[0]!.brand) : "matching";
  return {
    status: "NEEDS_CLARIFICATION",
    query,
    question: speech.clarify(label, options.map((o) => spokenName(o.name))),
    options,
  };
}

function resolved(p: Product, quantity: number): ItemResolution {
  return { status: "RESOLVED", item: { sku: p.sku, quantity, name: sanitizeForModel(p.name) } };
}

function toOption(p: Product): ResolutionOption {
  return { sku: p.sku, name: sanitizeForModel(p.name), pack_size: p.pack_size, price_cents: p.price_cents };
}
