/**
 * The three deterministic state machines (§19, §29, §15).
 *
 *  - VoiceStage: where a voice conversation is in the ordering funnel.
 *  - OrderIntentStatus: the lifecycle of a basket awaiting authorization.
 *  - OrderStatus: the lifecycle of a placed order, owned by GIMME's OMS.
 *
 * Transitions are tables, not if-statements, so the allowed paths are
 * reviewable in one place and every illegal transition throws.
 */

import { GimmeError } from "./errors.js";
import type { OrderStatus } from "./types.js";

// ---------------------------------------------------------------- voice stage

export const VOICE_STAGES = [
  "DISCOVERY",
  "CUSTOMER_IDENTIFICATION",
  "PRODUCT_RESOLUTION",
  "INVENTORY_VALIDATION",
  "DELIVERY_VALIDATION",
  "COMPLIANCE_VALIDATION",
  "PRICE_CALCULATION",
  "ORDER_INTENT_CREATED",
  "CUSTOMER_CONFIRMATION",
  "PAYMENT_AUTHORIZATION",
  "FINAL_VALIDATION",
  "ORDER_CREATION",
  "ORDER_CONFIRMED",
  "DELIVERY_TRACKING",
] as const;
export type VoiceStage = (typeof VOICE_STAGES)[number];

/**
 * Each stage may advance only to the next stage. Two non-linear moves are
 * allowed: starting a new basket (back to PRODUCT_RESOLUTION) from any point
 * after identification, and asking about an existing order
 * (DELIVERY_TRACKING) from any point after identification.
 */
export function canAdvance(from: VoiceStage, to: VoiceStage): boolean {
  const i = VOICE_STAGES.indexOf(from);
  const j = VOICE_STAGES.indexOf(to);
  if (j === i + 1) return true;
  if (j === i) return true; // re-running a step (e.g. re-pricing) is not progress, but is legal
  const identified = i >= VOICE_STAGES.indexOf("CUSTOMER_IDENTIFICATION");
  if (identified && to === "PRODUCT_RESOLUTION") return true;
  if (identified && to === "DELIVERY_TRACKING") return true;
  return false;
}

/**
 * The validation stages a single server-side call runs through in order. The
 * service runs all of them itself rather than trusting that a model called
 * the earlier tools — a skipped tool call cannot skip a check.
 */
export const INTENT_CREATION_PATH: VoiceStage[] = [
  "PRODUCT_RESOLUTION",
  "INVENTORY_VALIDATION",
  "DELIVERY_VALIDATION",
  "COMPLIANCE_VALIDATION",
  "PRICE_CALCULATION",
  "ORDER_INTENT_CREATED",
];

// ------------------------------------------------------- order intent status

export const ORDER_INTENT_STATUSES = [
  "AWAITING_CONFIRMATION",
  "CONFIRMED",
  "PAYMENT_AUTHORIZED",
  "ORDER_PLACED",
  "EXPIRED",
  "CANCELLED",
  "SUPERSEDED",
  "FAILED",
] as const;
export type OrderIntentStatus = (typeof ORDER_INTENT_STATUSES)[number];

const INTENT_TRANSITIONS: Record<OrderIntentStatus, OrderIntentStatus[]> = {
  AWAITING_CONFIRMATION: ["CONFIRMED", "EXPIRED", "CANCELLED", "SUPERSEDED", "FAILED"],
  CONFIRMED: ["PAYMENT_AUTHORIZED", "EXPIRED", "CANCELLED", "SUPERSEDED", "FAILED"],
  PAYMENT_AUTHORIZED: ["ORDER_PLACED", "EXPIRED", "CANCELLED", "SUPERSEDED", "FAILED"],
  ORDER_PLACED: [],
  EXPIRED: [],
  CANCELLED: [],
  SUPERSEDED: [],
  FAILED: [],
};

export function isTerminalIntent(status: OrderIntentStatus): boolean {
  return INTENT_TRANSITIONS[status].length === 0;
}

export function assertIntentTransition(from: OrderIntentStatus, to: OrderIntentStatus): void {
  if (!INTENT_TRANSITIONS[from].includes(to)) {
    throw new GimmeError("ORDER_INTENT_INVALID_STATE", {
      details: { from, to },
      message: from === "ORDER_PLACED" ? "That order has already been placed." : undefined,
    });
  }
}

export type AuthorizationStatus = "PENDING" | "AUTHORIZED" | "DECLINED";
export type PaymentStatus = "PENDING" | "AUTHORIZED" | "CAPTURED" | "VOIDED" | "FAILED";

// --------------------------------------------------------------- order status

const ORDER_TRANSITIONS: Record<OrderStatus, OrderStatus[]> = {
  CREATED: ["PAYMENT_AUTHORIZED", "CONFIRMED", "CANCELLED", "FAILED"],
  PAYMENT_AUTHORIZED: ["CONFIRMED", "CANCELLED", "FAILED"],
  CONFIRMED: ["ACCEPTED", "CANCELLED", "FAILED"],
  ACCEPTED: ["PREPARING", "CANCELLED", "FAILED"],
  PREPARING: ["DISPATCHED", "CANCELLED", "FAILED"],
  DISPATCHED: ["OUT_FOR_DELIVERY", "FAILED"],
  OUT_FOR_DELIVERY: ["DELIVERED", "FAILED"],
  DELIVERED: ["REFUNDED"],
  CANCELLED: ["REFUNDED"],
  REFUNDED: [],
  FAILED: ["REFUNDED"],
};

export function canTransitionOrder(from: OrderStatus, to: OrderStatus): boolean {
  return ORDER_TRANSITIONS[from].includes(to);
}

/** Statuses after which nothing a voice assistant does can change the order. */
export const ORDER_FINAL_STATUSES: OrderStatus[] = ["DELIVERED", "CANCELLED", "REFUNDED", "FAILED"];
