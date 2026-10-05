/**
 * An in-memory GIMME that honours every port contract. It is deliberately
 * not a mock: stock is decremented on reservation, payments are idempotent
 * per key, orders move through the OMS state machine, and the clock is
 * injectable so trading-hours rules can be tested.
 */

import { randomUUID } from "node:crypto";
import { GimmeError } from "../../domain/errors.js";
import { canTransitionOrder } from "../../domain/state-machine.js";
import type { CartLine, Order, OrderStatus, PricedLine, Product, Quote } from "../../domain/types.js";
import type { GimmeBackend, PaymentAuthorizationResult } from "../ports.js";
import {
  ADDRESSES,
  CUSTOMERS,
  DELIVERY_HOURS,
  FEES,
  INITIAL_STOCK,
  PAYMENT_METHODS,
  PRICE_CHANGED_NEW_CENTS,
  PRODUCTS,
  PROMOTIONS,
} from "./fixtures.js";

export type OrderStatusListener = (order: Order, previous: OrderStatus | null) => void;

interface PaymentRecord {
  reference: string;
  customerId: string;
  amountCents: number;
  status: "AUTHORIZED" | "CAPTURED" | "VOIDED";
}

const SEARCH_SYNONYMS: Record<string, string[]> = {
  beer: ["beer", "lager", "pilsner", "ale"],
  beers: ["beer", "lager", "pilsner", "ale"],
  german: ["germany"],
  dutch: ["netherlands"],
  kiwi: ["new zealand"],
  wine: ["wine"],
  red: ["red"],
  white: ["white"],
  coke: ["coca-cola"],
  zero: ["non-alcoholic"],
  "alcohol-free": ["non-alcoholic"],
  heinekens: ["heineken"],
};

export class SandboxBackend implements GimmeBackend {
  now: () => Date = () => new Date();

  private readonly products = new Map<string, Product>(PRODUCTS.map((p) => [p.sku, { ...p }]));
  private readonly stock = new Map<string, number>(Object.entries(INITIAL_STOCK));
  private readonly reservations = new Map<string, CartLine[]>();
  private readonly quoteCounts = new Map<string, number>();
  private readonly ordersById = new Map<string, Order>();
  private readonly ordersByIdempotency = new Map<string, string>();
  private readonly paymentsByKey = new Map<string, { result: PaymentAuthorizationResult; attempts: number }>();
  private readonly paymentRecords = new Map<string, PaymentRecord>();
  private readonly listeners: OrderStatusListener[] = [];
  private orderSeq = 123455;

  onOrderStatus(listener: OrderStatusListener): void {
    this.listeners.push(listener);
  }

  // ---------------------------------------------------------- scenario hooks

  setStock(sku: string, quantity: number): void {
    this.stock.set(sku, quantity);
  }

  getStock(sku: string): number {
    return this.stock.get(sku) ?? 0;
  }

  setPrice(sku: string, cents: number): void {
    const prod = this.products.get(sku);
    if (prod) prod.price_cents = cents;
  }

  /** Every provider-side authorization, for asserting "charged exactly once". */
  paymentLedger(): PaymentRecord[] {
    return [...this.paymentRecords.values()];
  }

  /** Move an order to the next OMS status, as a store and courier would. */
  setOrderStatus(orderId: string, status: OrderStatus): Order {
    const order = this.ordersById.get(orderId);
    if (!order) throw new Error(`no order ${orderId}`);
    if (!canTransitionOrder(order.status, status)) {
      throw new Error(`illegal OMS transition ${order.status} -> ${status}`);
    }
    const previous = order.status;
    order.status = status;
    for (const l of this.listeners) l({ ...order }, previous);
    return { ...order };
  }

  // ------------------------------------------------------------------ ports

  customers: GimmeBackend["customers"] = {
    getCustomer: async (customerId) => {
      const c = CUSTOMERS.find((x) => x.customer_id === customerId);
      return c ? { customer_id: c.customer_id, first_name: c.first_name, eligibility: c.eligibility } : null;
    },
    isPlatformLinked: async (customerId, platform, platformUserId) => {
      const c = CUSTOMERS.find((x) => x.customer_id === customerId);
      if (!c) return false;
      // For agent platforms the OAuth grant itself is the account link.
      if (platform === "MCP_AGENT" || platform === "WEB_AGENT" || platform === "SANDBOX") return true;
      return (c.platform_links[platform] ?? []).includes(platformUserId);
    },
    listAddresses: async (customerId) =>
      ADDRESSES.filter((a) => a.customer_id === customerId).map(({ id, label, summary, is_default }) => ({ id, label, summary, is_default })),
    listPaymentMethods: async (customerId) => (PAYMENT_METHODS[customerId] ?? []).map((m) => ({ ...m })),
    getUsualOrder: async (customerId) => (CUSTOMERS.find((x) => x.customer_id === customerId)?.usual_order ?? []).map((l) => ({ ...l })),
    listOrders: async (customerId, limit) =>
      [...this.ordersById.values()]
        .filter((o) => o.customer_id === customerId)
        .sort((a, b) => b.created_at.localeCompare(a.created_at))
        .slice(0, limit)
        .map((o) => ({ ...o })),
  };

  catalog: GimmeBackend["catalog"] = {
    search: async (query, { limit }) => {
      const tokens = query
        .toLowerCase()
        .replace(/[^a-z0-9.\-' ]/g, " ")
        .split(/\s+/)
        .filter((t) => t.length > 1 && !STOPWORDS.has(t));
      if (tokens.length === 0) return [];
      const scored: { prod: Product; score: number }[] = [];
      for (const prod of this.products.values()) {
        const hay = [prod.name, prod.brand, prod.category, prod.country ?? "", ...prod.tags].join(" ").toLowerCase();
        let score = 0;
        for (const t of tokens) {
          const variants = SEARCH_SYNONYMS[t] ?? [t, t.replace(/s$/, "")];
          if (variants.some((v) => hay.includes(v))) score += prod.brand.toLowerCase().startsWith(t.replace(/s$/, "")) ? 3 : 1;
        }
        if (score > 0) scored.push({ prod, score });
      }
      return scored
        .sort((a, b) => b.score - a.score || a.prod.price_cents - b.prod.price_cents)
        .slice(0, limit)
        .map((s) => ({ ...s.prod }));
    },
    getProducts: async (skus) => {
      const out = new Map<string, Product>();
      for (const sku of skus) {
        const prod = this.products.get(sku);
        if (prod) out.set(sku, { ...prod });
      }
      return out;
    },
  };

  inventory: GimmeBackend["inventory"] = {
    check: async (lines) => {
      const items = lines.map((l) => {
        const have = this.stock.get(l.sku) ?? 0;
        return { sku: l.sku, requested: l.quantity, available: have >= l.quantity, available_quantity: have };
      });
      return { available: items.every((i) => i.available), store_id: "STORE_PONSONBY", items };
    },
    reserve: async (lines) => {
      for (const l of lines) {
        if ((this.stock.get(l.sku) ?? 0) < l.quantity) {
          throw new GimmeError("INVENTORY_CHANGED", { details: { sku: l.sku } });
        }
      }
      for (const l of lines) this.stock.set(l.sku, (this.stock.get(l.sku) ?? 0) - l.quantity);
      const id = `RSV-${randomUUID()}`;
      this.reservations.set(id, lines.map((l) => ({ ...l })));
      return id;
    },
    release: async (reservationId) => {
      const lines = this.reservations.get(reservationId);
      if (!lines) return;
      for (const l of lines) this.stock.set(l.sku, (this.stock.get(l.sku) ?? 0) + l.quantity);
      this.reservations.delete(reservationId);
    },
  };

  delivery: GimmeBackend["delivery"] = {
    validate: async (customerId, addressId, { containsAlcohol }) => {
      const addr = ADDRESSES.find((a) => a.id === addressId && a.customer_id === customerId);
      if (!addr) throw new GimmeError("ADDRESS_NOT_FOUND");
      const requirements = containsAlcohol ? ["ID_CHECK_ON_DELIVERY", "NO_DELIVERY_TO_INTOXICATED_PERSONS"] : [];
      if (addr.zone === "OUTSIDE") return { eligible: false, reason: "OUTSIDE_SERVICE_AREA", requirements };
      const hour = aucklandHour(this.now());
      if (hour < DELIVERY_HOURS.open || hour >= DELIVERY_HOURS.close) {
        return { eligible: false, reason: "STORE_CLOSED", requirements };
      }
      if (!addr.has_capacity) return { eligible: false, reason: "DELIVERY_UNAVAILABLE", requirements };
      return {
        eligible: true,
        store_id: "STORE_PONSONBY",
        estimated_delivery_minutes: addr.eta_minutes,
        delivery_window: `${addr.eta_minutes} minutes`,
        requirements,
      };
    },
  };

  pricing: GimmeBackend["pricing"] = {
    quote: async ({ customerId, lines, addressId, promotionCode }) => {
      const addr = ADDRESSES.find((a) => a.id === addressId && a.customer_id === customerId);
      if (!addr) throw new GimmeError("ADDRESS_NOT_FOUND");
      const priced: PricedLine[] = lines.map((l) => {
        const prod = this.products.get(l.sku);
        if (!prod) throw new GimmeError("PRODUCT_NOT_FOUND", { details: { sku: l.sku } });
        const n = (this.quoteCounts.get(l.sku) ?? 0) + 1;
        this.quoteCounts.set(l.sku, n);
        const unit = l.sku === "PRICE_CHANGED_PRODUCT" && n >= 2 ? PRICE_CHANGED_NEW_CENTS : prod.price_cents;
        return { sku: l.sku, quantity: l.quantity, name: prod.name, unit_price_cents: unit, line_total_cents: unit * l.quantity, alcoholic: prod.alcoholic };
      });
      const subtotal = priced.reduce((s, l) => s + l.line_total_cents, 0);
      let discount = 0;
      let appliedPromo: string | undefined;
      if (promotionCode) {
        const promo = PROMOTIONS[promotionCode.toUpperCase()];
        if (promo && subtotal >= promo.min_subtotal_cents) {
          discount = promo.discount_cents;
          appliedPromo = promotionCode.toUpperCase();
        }
      }
      const fees = { ...FEES };
      const total = subtotal + fees.delivery_cents + fees.service_cents + fees.packaging_cents - discount;
      const quote: Quote = {
        quote_id: `Q-${randomUUID()}`,
        lines: priced,
        subtotal_cents: subtotal,
        fees,
        discount_cents: discount,
        ...(appliedPromo ? { promotion_code: appliedPromo } : {}),
        total_cents: total,
        currency: "NZD",
        estimated_delivery_minutes: addr.eta_minutes,
        priced_at: this.now().toISOString(),
      };
      return quote;
    },
  };

  orders: GimmeBackend["orders"] = {
    create: async (input) => {
      const existing = this.ordersByIdempotency.get(input.idempotencyKey);
      if (existing) return { ...this.ordersById.get(existing)! };
      this.orderSeq += 1;
      const created = this.now();
      const order: Order = {
        order_id: `GIMME-${this.orderSeq}`,
        customer_id: input.customerId,
        status: "CONFIRMED",
        lines: input.quote.lines,
        subtotal_cents: input.quote.subtotal_cents,
        fees: input.quote.fees,
        discount_cents: input.quote.discount_cents,
        total_cents: input.quote.total_cents,
        currency: input.quote.currency,
        delivery_address_id: input.addressId,
        payment_reference: input.paymentReference,
        created_at: created.toISOString(),
        estimated_delivery_at: new Date(created.getTime() + input.quote.estimated_delivery_minutes * 60_000).toISOString(),
        source: input.source,
      };
      this.ordersById.set(order.order_id, order);
      this.ordersByIdempotency.set(input.idempotencyKey, order.order_id);
      this.reservations.delete(input.reservationId); // the order now owns the stock
      for (const l of this.listeners) l({ ...order }, null);
      return { ...order };
    },
    get: async (orderId) => {
      const o = this.ordersById.get(orderId);
      return o ? { ...o } : null;
    },
    cancel: async (orderId) => {
      const order = this.ordersById.get(orderId);
      if (!order) throw new GimmeError("ORDER_NOT_FOUND");
      if (order.status === "CONFIRMED" || order.status === "ACCEPTED") {
        this.setOrderStatus(orderId, "CANCELLED");
        for (const l of order.lines) this.stock.set(l.sku, (this.stock.get(l.sku) ?? 0) + l.quantity);
        const pay = this.paymentRecords.get(order.payment_reference);
        if (pay) pay.status = pay.status === "CAPTURED" ? pay.status : "VOIDED";
        return { success: true, status: "CANCELLED" };
      }
      const reason =
        order.status === "PREPARING" ? "ORDER_ALREADY_PREPARING"
        : order.status === "DISPATCHED" || order.status === "OUT_FOR_DELIVERY" ? "ORDER_ALREADY_DISPATCHED"
        : order.status === "DELIVERED" ? "ORDER_ALREADY_DELIVERED"
        : `ORDER_${order.status}`;
      return { success: false, reason, status: order.status };
    },
  };

  payments: GimmeBackend["payments"] = {
    authorize: async (input) => {
      const prior = this.paymentsByKey.get(input.idempotencyKey);
      if (prior) {
        prior.attempts += 1;
        // A timed-out authorization did go through at the provider; the retry learns its outcome.
        if (prior.result.status === "TIMEOUT") {
          prior.result = { status: "AUTHORIZED", payment_reference: prior.result.payment_reference };
        }
        return { ...prior.result };
      }
      let result: PaymentAuthorizationResult;
      const reference = `PAY-${randomUUID().slice(0, 8).toUpperCase()}`;
      const method = input.source.type === "SAVED_METHOD" ? input.source.paymentMethodId : input.source.token;
      const owned =
        input.source.type === "WALLET_TOKEN" || (PAYMENT_METHODS[input.customerId] ?? []).some((m) => m.id === method);
      if (!owned) {
        result = { status: "DECLINED", decline_reason: "UNKNOWN_PAYMENT_METHOD" };
      } else if (method === "PM_DECLINED" || method === "tok_declined") {
        result = { status: "DECLINED", decline_reason: "CARD_DECLINED" };
      } else if (method === "PM_TIMEOUT") {
        result = { status: "TIMEOUT", payment_reference: reference };
        this.paymentRecords.set(reference, { reference, customerId: input.customerId, amountCents: input.amountCents, status: "AUTHORIZED" });
      } else {
        result = { status: "AUTHORIZED", payment_reference: reference };
        this.paymentRecords.set(reference, { reference, customerId: input.customerId, amountCents: input.amountCents, status: "AUTHORIZED" });
      }
      this.paymentsByKey.set(input.idempotencyKey, { result, attempts: 1 });
      return { ...result };
    },
    capture: async (reference, amountCents) => {
      const rec = this.paymentRecords.get(reference);
      if (!rec || rec.status !== "AUTHORIZED" || amountCents > rec.amountCents) return { captured: false };
      rec.status = "CAPTURED";
      return { captured: true };
    },
    void: async (reference) => {
      const rec = this.paymentRecords.get(reference);
      if (rec && rec.status === "AUTHORIZED") rec.status = "VOIDED";
    },
  };
}

const STOPWORDS = new Set(["a", "an", "the", "of", "some", "me", "get", "find", "order", "please", "from", "gimme", "pack", "packs", "and", "for", "my"]);

function aucklandHour(d: Date): number {
  const h = new Intl.DateTimeFormat("en-NZ", { timeZone: "Pacific/Auckland", hour: "numeric", hourCycle: "h23" }).format(d);
  return Number(h);
}
