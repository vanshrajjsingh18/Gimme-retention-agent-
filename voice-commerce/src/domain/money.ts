import type { Cents } from "./types.js";

/** Cents to the decimal amount shown at the API edge (8895 -> 88.95). */
export function toAmount(cents: Cents): number {
  return Math.round(cents) / 100;
}

/** Decimal amount from a client to cents (88.95 -> 8895). Rejects sub-cent precision. */
export function toCents(amount: number): Cents {
  const cents = Math.round(amount * 100);
  if (Math.abs(cents - amount * 100) > 1e-6) {
    throw new RangeError(`amount ${amount} has more than two decimal places`);
  }
  return cents;
}

/** Speakable money: 8640 -> "$86.40". */
export function speakMoney(cents: Cents): string {
  return `$${(cents / 100).toFixed(2)}`;
}
