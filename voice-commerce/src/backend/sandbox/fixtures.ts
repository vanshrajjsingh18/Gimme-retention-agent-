/**
 * Sandbox data (§41). Every scenario named in the build brief has a fixture
 * here, and the names are the scenario names so a test reads as the brief
 * does: TEST_CUSTOMER_AGE_REVIEW, OUT_OF_STOCK_PRODUCT, PM_DECLINED, ...
 *
 * Prices are illustrative sandbox prices, not GIMME's live price list.
 */

import type { AddressSummary, CartLine, Customer, PaymentMethodSummary, Platform, Product } from "../../domain/types.js";

export interface SandboxAddress extends AddressSummary {
  customer_id: string;
  zone: "AUCKLAND_CENTRAL" | "AUCKLAND_NORTH" | "OUTSIDE";
  /** When false, the zone has no courier capacity (DELIVERY_UNAVAILABLE). */
  has_capacity: boolean;
  eta_minutes: number;
}

export interface SandboxCustomer extends Customer {
  platform_links: Partial<Record<Platform, string[]>>;
  usual_order: CartLine[];
}

const p = (
  sku: string,
  name: string,
  brand: string,
  category: string,
  price_cents: number,
  extra: Partial<Product> = {},
): Product => ({
  sku,
  name,
  brand,
  category,
  price_cents,
  currency: "NZD",
  pack_size: 1,
  alcoholic: category !== "Mixers & Soft Drinks" && category !== "Extras",
  tags: [],
  ...extra,
});

export const PRODUCTS: Product[] = [
  p("HEI6PK", "Heineken 6 Pack 330ml Bottles", "Heineken", "Beer", 1999, { country: "Netherlands", pack_size: 6, unit_volume_ml: 330, abv: 5, tags: ["lager"] }),
  p("HEI12PK", "Heineken 12 Pack 330ml Bottles", "Heineken", "Beer", 3499, { country: "Netherlands", pack_size: 12, unit_volume_ml: 330, abv: 5, tags: ["lager"] }),
  p("HEI24PK", "Heineken 24 Pack 330ml Cans", "Heineken", "Beer", 6299, { country: "Netherlands", pack_size: 24, unit_volume_ml: 330, abv: 5, tags: ["lager", "case"] }),
  p("HEI00-6PK", "Heineken 0.0 6 Pack 330ml Bottles", "Heineken", "Beer", 1599, { country: "Netherlands", pack_size: 6, unit_volume_ml: 330, abv: 0, alcoholic: false, tags: ["non-alcoholic", "zero", "lager"] }),
  p("COR12PK", "Corona Extra 12 Pack 355ml Bottles", "Corona", "Beer", 3699, { country: "Mexico", pack_size: 12, unit_volume_ml: 355, abv: 4.5, tags: ["lager"] }),
  p("STP12PK", "Steinlager Pure 12 Pack 330ml Bottles", "Steinlager", "Beer", 3299, { country: "New Zealand", pack_size: 12, unit_volume_ml: 330, abv: 5, tags: ["lager"] }),
  p("BEC12PK", "Beck's 12 Pack 330ml Bottles", "Beck's", "Beer", 2999, { country: "Germany", pack_size: 12, unit_volume_ml: 330, abv: 5, tags: ["lager", "pilsner"] }),
  p("WAR6PK", "Warsteiner Premium 6 Pack 500ml Cans", "Warsteiner", "Beer", 2199, { country: "Germany", pack_size: 6, unit_volume_ml: 500, abv: 4.8, tags: ["pilsner"] }),
  p("WINE-SB", "Cloudy Bay Sauvignon Blanc 750ml", "Cloudy Bay", "Wine", 3299, { country: "New Zealand", unit_volume_ml: 750, abv: 13, tags: ["white", "sauvignon blanc"] }),
  p("WINE-PN", "Oyster Bay Pinot Noir 750ml", "Oyster Bay", "Wine", 2199, { country: "New Zealand", unit_volume_ml: 750, abv: 13.5, tags: ["red", "pinot noir"] }),
  p("COKE-1500", "Coca-Cola 1.5L", "Coca-Cola", "Mixers & Soft Drinks", 449, { unit_volume_ml: 1500, tags: ["soft drink", "mixer"] }),
  p("ICE-5KG", "Party Ice 5kg Bag", "GIMME", "Extras", 699, { tags: ["ice"] }),

  // Scenario products, named for the scenario they exercise.
  p("AVAILABLE_PRODUCT", "Sandbox Available Lager 6 Pack", "Sandbox", "Beer", 1500, { pack_size: 6, abv: 5, tags: ["sandbox"] }),
  p("OUT_OF_STOCK_PRODUCT", "Sandbox Sold Out Pilsner 6 Pack", "Sandbox", "Beer", 1700, { pack_size: 6, abv: 5, tags: ["sandbox"] }),
  p("PRICE_CHANGED_PRODUCT", "Sandbox Repriced IPA 6 Pack", "Sandbox", "Beer", 2000, { pack_size: 6, abv: 6.5, tags: ["sandbox"] }),
  p("AGE_RESTRICTED_PRODUCT", "Sandbox Gin 700ml", "Sandbox", "Spirits", 5500, { abv: 40, tags: ["sandbox"] }),
];

/** PRICE_CHANGED_PRODUCT costs this from its second quote onward. */
export const PRICE_CHANGED_NEW_CENTS = 2750;

export const INITIAL_STOCK: Record<string, number> = Object.fromEntries(
  PRODUCTS.map((prod) => [prod.sku, prod.sku === "OUT_OF_STOCK_PRODUCT" ? 0 : 200]),
);

export const ADDRESSES: SandboxAddress[] = [
  { id: "ADDR_HOME", customer_id: "TEST_CUSTOMER", label: "Home", summary: "Home, Ponsonby", is_default: true, zone: "AUCKLAND_CENTRAL", has_capacity: true, eta_minutes: 45 },
  { id: "ADDR_WORK", customer_id: "TEST_CUSTOMER", label: "Work", summary: "Work, Auckland CBD", is_default: false, zone: "AUCKLAND_CENTRAL", has_capacity: true, eta_minutes: 35 },
  { id: "ADDR_OUTSIDE_ZONE", customer_id: "TEST_CUSTOMER", label: "Bach", summary: "Bach, Queenstown", is_default: false, zone: "OUTSIDE", has_capacity: true, eta_minutes: 0 },
  { id: "ADDR_NO_CAPACITY", customer_id: "TEST_CUSTOMER", label: "Mum's", summary: "Mum's place, Albany", is_default: false, zone: "AUCKLAND_NORTH", has_capacity: false, eta_minutes: 60 },
  { id: "ADDR_NP_HOME", customer_id: "TEST_CUSTOMER_NO_PAYMENT", label: "Home", summary: "Home, Grey Lynn", is_default: true, zone: "AUCKLAND_CENTRAL", has_capacity: true, eta_minutes: 40 },
  { id: "ADDR_AR_HOME", customer_id: "TEST_CUSTOMER_AGE_REVIEW", label: "Home", summary: "Home, Mt Eden", is_default: true, zone: "AUCKLAND_CENTRAL", has_capacity: true, eta_minutes: 40 },
  { id: "ADDR_RS_HOME", customer_id: "TEST_CUSTOMER_RESTRICTED", label: "Home", summary: "Home, Newmarket", is_default: true, zone: "AUCKLAND_CENTRAL", has_capacity: true, eta_minutes: 40 },
];

export const PAYMENT_METHODS: Record<string, PaymentMethodSummary[]> = {
  TEST_CUSTOMER: [
    { id: "PM_SUCCESS", label: "Visa ending 4242", kind: "CARD_TOKEN", is_default: true },
    { id: "PM_DECLINED", label: "Mastercard ending 0002", kind: "CARD_TOKEN", is_default: false },
    { id: "PM_TIMEOUT", label: "Visa ending 0119", kind: "CARD_TOKEN", is_default: false },
  ],
  TEST_CUSTOMER_NO_PAYMENT: [],
  TEST_CUSTOMER_AGE_REVIEW: [{ id: "PM_AR", label: "Visa ending 1111", kind: "CARD_TOKEN", is_default: true }],
  TEST_CUSTOMER_RESTRICTED: [{ id: "PM_RS", label: "Visa ending 2222", kind: "CARD_TOKEN", is_default: true }],
};

export const CUSTOMERS: SandboxCustomer[] = [
  {
    customer_id: "TEST_CUSTOMER",
    first_name: "Sam",
    eligibility: "ELIGIBLE",
    platform_links: { APPLE: ["apple-user-test"], GOOGLE: ["google-user-test"] },
    usual_order: [
      { sku: "HEI12PK", quantity: 2 },
      { sku: "WINE-SB", quantity: 1 },
    ],
  },
  {
    customer_id: "TEST_CUSTOMER_NO_PAYMENT",
    first_name: "Alex",
    eligibility: "ELIGIBLE",
    platform_links: { APPLE: ["apple-user-nopay"] },
    usual_order: [{ sku: "STP12PK", quantity: 1 }],
  },
  {
    customer_id: "TEST_CUSTOMER_AGE_REVIEW",
    first_name: "Jordan",
    eligibility: "AGE_VERIFICATION_REQUIRED",
    platform_links: { APPLE: ["apple-user-agereview"] },
    usual_order: [{ sku: "COR12PK", quantity: 1 }],
  },
  {
    customer_id: "TEST_CUSTOMER_RESTRICTED",
    first_name: "Riley",
    eligibility: "ACCOUNT_RESTRICTED",
    platform_links: { APPLE: ["apple-user-restricted"] },
    usual_order: [{ sku: "HEI12PK", quantity: 1 }],
  },
];

export const PROMOTIONS: Record<string, { min_subtotal_cents: number; discount_cents: number; description: string }> = {
  VOICE10: { min_subtotal_cents: 5000, discount_cents: 1000, description: "$10 off voice orders over $50" },
};

export const FEES = { delivery_cents: 399, service_cents: 399, packaging_cents: 99 };

/** Licensed delivery window, Pacific/Auckland local time. Confirm against GIMME's licence conditions. */
export const DELIVERY_HOURS = { open: 7, close: 23 };
