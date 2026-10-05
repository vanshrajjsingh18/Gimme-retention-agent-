/**
 * Short spoken responses (§35). Every sentence a voice platform speaks is
 * built here from authoritative values, so no amount, ETA or product name
 * in speech can come from anywhere but GIMME's backend.
 */

import { speakMoney } from "../domain/money.js";
import type { Cents } from "../domain/types.js";
import { sanitizeForModel } from "../security/guards.js";

const WORDS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve"];

export function speakNumber(n: number): string {
  return WORDS[n] ?? String(n);
}

/** "Heineken 12 Pack 330ml Bottles" -> "Heineken 12-pack"; "Coca-Cola 1.5L" -> "Coca-Cola". */
export function spokenName(name: string): string {
  const clean = sanitizeForModel(name, 80)
    .replace(/\s\d+(\.\d+)?\s?(ml|l)\b.*$/i, "")
    .replace(/\s(\d+)\s?pack\b/i, " $1-pack")
    .trim();
  return clean || sanitizeForModel(name, 80);
}

export function speakLine(quantity: number, name: string): string {
  const n = spokenName(name);
  const plural = quantity > 1 && /-pack$/.test(n) ? "s" : "";
  return `${speakNumber(quantity)} ${n}${plural}`;
}

export function speakList(parts: string[]): string {
  if (parts.length <= 1) return parts[0] ?? "";
  return `${parts.slice(0, -1).join(", ")} and ${parts[parts.length - 1]}`;
}

export function speakMinutes(minutes: number): string {
  if (minutes < 60) return `about ${minutes} minutes`;
  const h = Math.round(minutes / 6) / 10;
  return `about ${h} ${h === 1 ? "hour" : "hours"}`;
}

export const speech = {
  orderSummary(opts: { lead?: string; lines: { quantity: number; name: string }[]; totalCents: Cents; addressLabel: string }): string {
    const what = speakList(opts.lines.map((l) => speakLine(l.quantity, l.name)));
    const lead = opts.lead ? `${opts.lead} is ${what}.` : `That's ${what}.`;
    return `${lead} It's ${speakMoney(opts.totalCents)} delivered to ${opts.addressLabel}. Want me to place it?`;
  },
  orderPlaced(minutes: number): string {
    return `Done. Your GIMME order is confirmed and should arrive in ${speakMinutes(minutes)}.`;
  },
  priceChanged(newTotal: Cents): string {
    return `The price has changed to ${speakMoney(newTotal)}. Do you want me to continue?`;
  },
  clarify(brand: string, options: string[]): string {
    const n = speakNumber(options.length);
    return `We have ${n} ${brand} options: ${speakList(options)}. Which one?`;
  },
  confirmOnDevice(): string {
    return "I need you to confirm this purchase on your device.";
  },
  declined(): string {
    return "No problem, I haven't placed the order.";
  },
};
