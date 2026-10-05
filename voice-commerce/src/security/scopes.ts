/**
 * OAuth scopes (§22). Scopes are flat: no scope implies another. A token
 * that can read products cannot create orders, and one that can create
 * orders cannot authorize payment.
 */

import { GimmeError } from "../domain/errors.js";

export const SCOPES = {
  CUSTOMER_READ: "gimme.customer.read",
  PRODUCTS_READ: "gimme.products.read",
  INVENTORY_READ: "gimme.inventory.read",
  CART_CREATE: "gimme.cart.create",
  ORDER_CREATE: "gimme.order.create",
  ORDER_READ: "gimme.order.read",
  ORDER_CANCEL: "gimme.order.cancel",
  PAYMENT_AUTHORIZE: "gimme.payment.authorize",
  /**
   * Record a customer's purchase confirmation directly. Granted only to
   * first-party GIMME clients that run the platform's own confirmation UI
   * (the iOS app's App Intents, the Android app, GIMME's hosted confirmation
   * page). A third-party agent never holds it, so a model cannot confirm on
   * the customer's behalf — it must route confirmation to the customer.
   */
  ORDER_CONFIRM: "gimme.order.confirm",
} as const;

export type Scope = (typeof SCOPES)[keyof typeof SCOPES];
export const ALL_SCOPES: Scope[] = Object.values(SCOPES);

export const SCOPE_DESCRIPTIONS: Record<Scope, string> = {
  "gimme.customer.read": "Read your first name, saved address labels, payment method labels and order preferences",
  "gimme.products.read": "Search GIMME's catalogue",
  "gimme.inventory.read": "Check stock and delivery availability",
  "gimme.cart.create": "Price a basket and prepare an order for your confirmation",
  "gimme.order.create": "Place an order you have confirmed",
  "gimme.order.read": "Read your order status and receipts",
  "gimme.order.cancel": "Cancel an order, where GIMME allows it",
  "gimme.payment.authorize": "Authorize payment for an order you have confirmed, using a saved payment method",
  "gimme.order.confirm": "Record your purchase confirmation (GIMME's own apps only)",
};

/** Default scopes a third-party AI agent may request. Note: no gimme.order.confirm. */
export const AGENT_SCOPES: Scope[] = ALL_SCOPES.filter((s) => s !== SCOPES.ORDER_CONFIRM);

export function requireScopes(granted: readonly string[], required: readonly Scope[]): void {
  const missing = required.filter((s) => !granted.includes(s));
  if (missing.length) {
    throw new GimmeError("INSUFFICIENT_SCOPE", { details: { missing_scopes: missing } });
  }
}
