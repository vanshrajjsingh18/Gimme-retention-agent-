/**
 * The ports to GIMME's existing systems. This service owns no customer,
 * product, price or order data: it asks these interfaces, which GIMME's core
 * platform implements (§46 — use existing services, don't duplicate data).
 *
 * The sandbox implementation (./sandbox) is a complete in-memory GIMME that
 * honours these contracts; production wires HTTP clients to the real
 * services behind the same interfaces. Nothing above this layer changes.
 */

import type {
  AddressSummary,
  CartLine,
  Customer,
  DeliveryCheck,
  InventoryCheck,
  Order,
  OrderStatus,
  PaymentMethodSummary,
  Platform,
  Product,
  Quote,
} from "../domain/types.js";

export interface CustomerService {
  /** Resolve an OAuth subject (the linked GIMME account) to a customer. Null if none. */
  getCustomer(customerId: string): Promise<Customer | null>;
  /** Is this platform user linked to this customer? Guards identity confusion. */
  isPlatformLinked(customerId: string, platform: Platform, platformUserId: string): Promise<boolean>;
  listAddresses(customerId: string): Promise<AddressSummary[]>;
  listPaymentMethods(customerId: string): Promise<PaymentMethodSummary[]>;
  /** The customer's habitual basket, as GIMME computes it. Empty if there isn't one. */
  getUsualOrder(customerId: string): Promise<CartLine[]>;
  /** Most recent orders first. */
  listOrders(customerId: string, limit: number): Promise<Order[]>;
}

export interface CatalogService {
  search(query: string, opts: { limit: number; storeHint?: string }): Promise<Product[]>;
  getProducts(skus: string[]): Promise<Map<string, Product>>;
}

export interface InventoryService {
  check(lines: CartLine[], addressId: string): Promise<InventoryCheck>;
  /**
   * Hold stock for an order about to be created. Must be atomic: either all
   * lines are held or none are. Returns a reservation id, or throws
   * INVENTORY_CHANGED if any line can no longer be held.
   */
  reserve(lines: CartLine[], addressId: string, reference: string): Promise<string>;
  release(reservationId: string): Promise<void>;
}

export interface DeliveryService {
  validate(customerId: string, addressId: string, opts: { containsAlcohol: boolean }): Promise<DeliveryCheck>;
}

export interface PricingService {
  /** The authoritative checkout calculation. Every amount a customer sees comes from here. */
  quote(input: {
    customerId: string;
    lines: CartLine[];
    addressId: string;
    promotionCode?: string;
  }): Promise<Quote>;
}

export interface OrderService {
  create(input: {
    customerId: string;
    quote: Quote;
    addressId: string;
    paymentReference: string;
    reservationId: string;
    idempotencyKey: string;
    source: Order["source"];
  }): Promise<Order>;
  get(orderId: string): Promise<Order | null>;
  /** Ask the OMS to cancel. It — not this service — decides whether that is possible. */
  cancel(orderId: string, reason: string): Promise<{ success: true; status: OrderStatus } | { success: false; reason: string; status: OrderStatus }>;
}

export interface PaymentAuthorizationResult {
  status: "AUTHORIZED" | "DECLINED" | "FAILED" | "TIMEOUT";
  payment_reference?: string;
  decline_reason?: string;
}

/**
 * Tokenised payments only (§13, §37). Inputs are references to payment
 * methods the provider already holds, or a single-use wallet token; no
 * method on this interface can accept card numbers.
 */
export interface PaymentGateway {
  authorize(input: {
    customerId: string;
    amountCents: number;
    currency: string;
    source: { type: "SAVED_METHOD"; paymentMethodId: string } | { type: "WALLET_TOKEN"; wallet: "APPLE_PAY" | "GOOGLE_PAY"; token: string };
    idempotencyKey: string;
    metadata: Record<string, string>;
  }): Promise<PaymentAuthorizationResult>;
  capture(paymentReference: string, amountCents: number, idempotencyKey: string): Promise<{ captured: boolean }>;
  void(paymentReference: string, idempotencyKey: string): Promise<void>;
}

export interface GimmeBackend {
  customers: CustomerService;
  catalog: CatalogService;
  inventory: InventoryService;
  delivery: DeliveryService;
  pricing: PricingService;
  orders: OrderService;
  payments: PaymentGateway;
}
