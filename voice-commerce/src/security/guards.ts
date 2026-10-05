/**
 * Input and output guards.
 *
 *  - assertNoCardData: refuses any request that carries something shaped like
 *    a card number or CVV (§37). Schemas already give a card nowhere to go;
 *    this catches one smuggled into a free-text field, before it can reach a
 *    log or the audit trail.
 *  - sanitizeForModel: product names and other backend text are data, and
 *    are returned to models as data (§38 prompt injection). Control
 *    characters, markup and instruction-shaped text are stripped, and length
 *    is capped, so catalogue content cannot carry a payload to the model.
 *    The real defence is structural — no tool output can authorize anything —
 *    but there's no reason to hand a model a loaded string.
 */

import { GimmeError } from "../domain/errors.js";

function luhnValid(digits: string): boolean {
  let sum = 0;
  let double = false;
  for (let i = digits.length - 1; i >= 0; i--) {
    let d = digits.charCodeAt(i) - 48;
    if (double) {
      d *= 2;
      if (d > 9) d -= 9;
    }
    sum += d;
    double = !double;
  }
  return sum % 10 === 0;
}

// A run of 13-19 digits, optionally space/hyphen grouped, not embedded in a
// longer alphanumeric token (so digit stretches inside UUIDs don't match).
const CARD_CANDIDATE = /(?<![A-Za-z0-9])(?:\d[ -]?){12,18}\d(?![A-Za-z0-9])/g;
const CARD_KEYS = /^(card_?number|pan|cvv|cvc|cvv2|security_?code|card_?cvc|expiry|exp_?month|exp_?year)$/i;

export function containsCardData(value: unknown, depth = 0): boolean {
  if (depth > 8 || value === null || value === undefined) return false;
  if (typeof value === "string") {
    for (const m of value.matchAll(CARD_CANDIDATE)) {
      const digits = m[0].replace(/[ -]/g, "");
      if (digits.length >= 13 && digits.length <= 19 && luhnValid(digits)) return true;
    }
    return false;
  }
  if (typeof value === "number") return containsCardData(String(value), depth + 1);
  if (Array.isArray(value)) return value.some((v) => containsCardData(v, depth + 1));
  if (typeof value === "object") {
    return Object.entries(value as Record<string, unknown>).some(
      ([k, v]) => CARD_KEYS.test(k) || containsCardData(v, depth + 1),
    );
  }
  return false;
}

export function assertNoCardData(value: unknown): void {
  if (containsCardData(value)) {
    throw new GimmeError("INVALID_REQUEST", {
      message: "GIMME never accepts card details through a voice assistant. Use a saved payment method.",
      details: { reason: "RAW_PAYMENT_CREDENTIALS_REJECTED" },
    });
  }
}

const INSTRUCTION_SHAPED = /\b(ignore (all |any |the )?(previous|prior|above)|system prompt|you are now|disregard|assistant:|<\/?(system|instructions?)>)/gi;

export function sanitizeForModel(text: string, max = 120): string {
  const cleaned = text
    .normalize("NFKC")
    .replace(/[\u0000-\u001f\u007f-\u009f​-‏‪-‮⁦-⁩]/g, " ")
    .replace(/<[^>]*>/g, " ")
    .replace(INSTRUCTION_SHAPED, " ")
    .replace(/\s+/g, " ")
    .trim();
  return cleaned.length > max ? `${cleaned.slice(0, max - 1)}…` : cleaned;
}
