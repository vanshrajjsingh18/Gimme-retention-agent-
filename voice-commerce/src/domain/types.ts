/**
 * Core domain objects. Money is held as integer cents everywhere inside the
 * system and only converted to decimal at the edge (see money.ts), so no
 * rounding error can make a confirmed amount differ from a charged one.
 */

export const PLATFORMS = ["APPLE", "GOOGLE", "MCP_AGENT", "WEB_AGENT", "SANDBOX"] as const;
export type Platform = (typeof PLATFORMS)[number];

export type Cents = number;
export type Currency = "NZD";

export interface Product {
  sku: string;
  name: string;
  brand: string;
  category: string;
  country?: string;
  /** Number of individual units in the pack (12 for a 12-pack). */
  pack_size: number;
  unit_volume_ml?: number;
  alcoholic: boolean;
  abv?: number;
  price_cents: Cents;
  currency: Currency;
  tags: string[];
}

export interface CartLine {
  sku: string;
  quantity: number;
}

export interface PricedLine extends CartLine {
  name: string;
  unit_price_cents: Cents;
  line_total_cents: Cents;
  alcoholic: boolean;
}

export interface Fees {
  delivery_cents: Cents;
  service_cents: Cents;
  packaging_cents: Cents;
}

/** An authoritative price quote from the GIMME pricing engine. */
export interface Quote {
  quote_id: string;
  lines: PricedLine[];
  subtotal_cents: Cents;
  fees: Fees;
  discount_cents: Cents;
  promotion_code?: string;
  total_cents: Cents;
  currency: Currency;
  estimated_delivery_minutes: number;
  priced_at: string;
}

export interface AddressSummary {
  id: string;
  label: string;
  /** A short, speakable summary — never the full street address. */
  summary: string;
  is_default: boolean;
}

export interface PaymentMethodSummary {
  id: string;
  /** e.g. "Visa ending 4242" — never a full PAN. */
  label: string;
  kind: "CARD_TOKEN" | "APPLE_PAY" | "GOOGLE_PAY" | "ACCOUNT_CREDIT";
  is_default: boolean;
}

export const CUSTOMER_ELIGIBILITY = [
  "ELIGIBLE",
  "AGE_VERIFICATION_REQUIRED",
  "IDENTITY_VERIFICATION_REQUIRED",
  "ACCOUNT_RESTRICTED",
  "ORDER_NOT_PERMITTED",
] as const;
export type CustomerEligibility = (typeof CUSTOMER_ELIGIBILITY)[number];

export interface Customer {
  customer_id: string;
  first_name: string;
  eligibility: CustomerEligibility;
}

export interface DeliveryCheck {
  eligible: boolean;
  reason?: "OUTSIDE_SERVICE_AREA" | "STORE_CLOSED" | "DELIVERY_UNAVAILABLE";
  store_id?: string;
  estimated_delivery_minutes?: number;
  delivery_window?: string;
  /** Conditions the courier must enforce, e.g. ID_CHECK_ON_DELIVERY. */
  requirements: string[];
}

export interface InventoryLine {
  sku: string;
  requested: number;
  available: boolean;
  available_quantity: number;
}

export interface InventoryCheck {
  available: boolean;
  store_id?: string;
  items: InventoryLine[];
}

export const ORDER_STATUSES = [
  "CREATED",
  "PAYMENT_AUTHORIZED",
  "CONFIRMED",
  "ACCEPTED",
  "PREPARING",
  "DISPATCHED",
  "OUT_FOR_DELIVERY",
  "DELIVERED",
  "CANCELLED",
  "REFUNDED",
  "FAILED",
] as const;
export type OrderStatus = (typeof ORDER_STATUSES)[number];

export interface Order {
  order_id: string;
  customer_id: string;
  status: OrderStatus;
  lines: PricedLine[];
  subtotal_cents: Cents;
  fees: Fees;
  discount_cents: Cents;
  total_cents: Cents;
  currency: Currency;
  delivery_address_id: string;
  payment_reference: string;
  created_at: string;
  estimated_delivery_at?: string;
  source: { channel: "VOICE"; platform: Platform; order_intent_id: string };
}
