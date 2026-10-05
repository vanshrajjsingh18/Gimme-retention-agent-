/**
 * Customer purchase authorizations (§12, §20).
 *
 * When a customer says "yes" to a specific order, the service issues a
 * signed, single-use authorization whose claims carry a digest of exactly
 * what was confirmed: the customer, the order intent, every line, the total,
 * the currency, the address, the payment method, the session and the
 * platform. Payment authorization verifies the signature and recomputes the
 * digest from the intent as it stands; if anything differs, the
 * authorization does not apply.
 *
 * That closes three attacks at once: a confirmation for one intent can't
 * authorize another (intent id + digest), a $50 confirmation can't authorize
 * a $150 charge (amount in the digest), and an old confirmation can't be
 * replayed (short expiry + single-use jti).
 */

import { createHash, randomUUID } from "node:crypto";
import { SignJWT, jwtVerify } from "jose";
import { GimmeError } from "../domain/errors.js";
import type { CartLine, Platform } from "../domain/types.js";

const TOKEN_TYPE = "gimme-purchase-authorization+jwt";

export interface IntentBinding {
  order_intent_id: string;
  customer_id: string;
  session_id: string;
  platform: Platform;
  lines: CartLine[];
  total_cents: number;
  currency: string;
  delivery_address_id: string;
  payment_method_id: string | null;
  quote_id: string;
}

export interface ConfirmationEvidence {
  /** How the customer confirmed: APPLE_APP_INTENT_CONFIRMATION, MCP_ELICITATION, HOSTED_CONFIRMATION_PAGE, ... */
  method: string;
  /** Did the platform authenticate the device owner (unlock, Face ID, voice match)? */
  device_authenticated: boolean;
  /** The platform's own id for the confirmation interaction, if it has one. */
  platform_confirmation_id?: string;
  confirmed_at: string;
}

export interface PurchaseAuthorizationClaims {
  authorization_id: string;
  order_intent_id: string;
  intent_digest: string;
  customer_id: string;
  session_id: string;
  platform: Platform;
  amount_cents: number;
  currency: string;
  confirmation: ConfirmationEvidence;
  expires_at: number;
}

/** Deterministic JSON: keys sorted at every level, lines sorted by SKU. */
export function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value && typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>)
      .filter(([, v]) => v !== undefined)
      .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
    return `{${entries.map(([k, v]) => `${JSON.stringify(k)}:${canonicalJson(v)}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

export function intentDigest(binding: IntentBinding): string {
  const normalised = {
    ...binding,
    lines: [...binding.lines].map((l) => ({ sku: l.sku, quantity: l.quantity })).sort((a, b) => a.sku.localeCompare(b.sku)),
  };
  return createHash("sha256").update(canonicalJson(normalised)).digest("hex");
}

export class PurchaseAuthorizer {
  private readonly key: Uint8Array;

  constructor(signingKey: string, private readonly ttlSeconds: number) {
    this.key = new TextEncoder().encode(signingKey);
  }

  async issue(binding: IntentBinding, confirmation: ConfirmationEvidence): Promise<{ token: string; claims: PurchaseAuthorizationClaims }> {
    const now = Math.floor(Date.now() / 1000);
    const claims: PurchaseAuthorizationClaims = {
      authorization_id: `AUTHZ-${randomUUID()}`,
      order_intent_id: binding.order_intent_id,
      intent_digest: intentDigest(binding),
      customer_id: binding.customer_id,
      session_id: binding.session_id,
      platform: binding.platform,
      amount_cents: binding.total_cents,
      currency: binding.currency,
      confirmation,
      expires_at: now + this.ttlSeconds,
    };
    const token = await new SignJWT({ ...claims })
      .setProtectedHeader({ alg: "HS256", typ: TOKEN_TYPE })
      .setSubject(binding.customer_id)
      .setJti(claims.authorization_id)
      .setIssuedAt(now)
      .setExpirationTime(claims.expires_at)
      .sign(this.key);
    return { token, claims };
  }

  /**
   * Verify a token against the intent as it stands now. Throws
   * AUTHORIZATION_REQUIRED for a bad or expired token and
   * CONFIRMATION_MISMATCH when it is valid but for something else.
   */
  async verify(token: string, current: IntentBinding): Promise<PurchaseAuthorizationClaims> {
    let payload: Record<string, unknown>;
    try {
      const res = await jwtVerify(token, this.key, { typ: TOKEN_TYPE, algorithms: ["HS256"] });
      payload = res.payload as Record<string, unknown>;
    } catch (err) {
      throw new GimmeError("AUTHORIZATION_REQUIRED", { cause: err, message: "That confirmation is no longer valid. Please confirm the order again." });
    }
    const claims = payload as unknown as PurchaseAuthorizationClaims;
    const mismatches: string[] = [];
    if (claims.order_intent_id !== current.order_intent_id) mismatches.push("order_intent_id");
    if (claims.customer_id !== current.customer_id) mismatches.push("customer_id");
    if (claims.session_id !== current.session_id) mismatches.push("session_id");
    if (claims.amount_cents !== current.total_cents) mismatches.push("amount");
    if (claims.currency !== current.currency) mismatches.push("currency");
    if (claims.intent_digest !== intentDigest(current)) mismatches.push("intent_digest");
    if (mismatches.length) {
      throw new GimmeError("CONFIRMATION_MISMATCH", { details: { mismatched: mismatches } });
    }
    return claims;
  }
}
