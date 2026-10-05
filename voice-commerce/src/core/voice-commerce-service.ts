/**
 * The GIMME Voice Commerce engine: the one place where ordering rules live.
 *
 * Every channel — MCP tools, the REST Voice API, the Apple and Google
 * adapters — calls this class and nothing else. It is deterministic: a
 * language model can choose which method to call and with what, but every
 * fact (identity, price, stock, delivery, eligibility) is fetched from
 * GIMME's backend here, and every transactional step re-runs the checks it
 * depends on rather than trusting that an earlier call ran them (§19, §33).
 *
 *   identify  -> session bound to customer + OAuth client + platform user
 *   create intent  -> resolve, stock, delivery, compliance, price   (no money moves)
 *   confirm   -> signed single-use authorization bound to the intent digest
 *   authorize payment -> tokenised, idempotent per intent
 *   place order  -> final re-validation, reserve, re-price, create, capture
 */

import { createHash, randomBytes, randomUUID } from "node:crypto";
import type { Config } from "../config.js";
import type { GimmeBackend } from "../backend/ports.js";
import { GimmeError, toGimmeError, type ErrorCode } from "../domain/errors.js";
import { toAmount, toCents } from "../domain/money.js";
import {
  assertIntentTransition,
  canAdvance,
  isTerminalIntent,
  ORDER_FINAL_STATUSES,
  type OrderIntentStatus,
  type VoiceStage,
} from "../domain/state-machine.js";
import type { CartLine, Customer, CustomerEligibility, DeliveryCheck, Order, Platform, Product, Quote } from "../domain/types.js";
import type { AuditLog, AuditEvent } from "../audit/audit-log.js";
import type { EventBus } from "../events/events.js";
import type { Logger } from "../observability/logger.js";
import type { FunnelStage, Metrics } from "../observability/metrics.js";
import { SCOPES, requireScopes, type Scope } from "../security/scopes.js";
import { assertNoCardData, sanitizeForModel } from "../security/guards.js";
import { PurchaseAuthorizer, type ConfirmationEvidence, type IntentBinding } from "../security/purchase-authorization.js";
import type { KeyValueStore, LockManager } from "../store/store.js";
import type * as C from "../contracts/schemas.js";
import type { z } from "zod";
import type {
  CallContext,
  IdempotencyRecord,
  OrderIntentRecord,
  PaymentAuthorizationRecord,
  VoiceSession,
} from "./models.js";
import { resolveItem, type ItemRequest } from "./resolver.js";
import { speakLine, speakList, speakMinutes, speech, spokenName } from "./speech.js";
import { speakMoney } from "../domain/money.js";

type Out<T extends z.ZodType> = z.infer<T>;

export interface VoiceStores {
  sessions: KeyValueStore<VoiceSession>;
  intents: KeyValueStore<OrderIntentRecord>;
  paymentAuths: KeyValueStore<PaymentAuthorizationRecord>;
  usedAuthorizations: KeyValueStore<{ used_at: string; order_intent_id: string }>;
  idempotency: KeyValueStore<IdempotencyRecord>;
  locks: LockManager;
}

export interface ServiceDeps {
  config: Config;
  backend: GimmeBackend;
  stores: VoiceStores;
  audit: AuditLog;
  metrics: Metrics;
  events: EventBus;
  log: Logger;
  now?: () => Date;
}

const ALCOHOL_BLOCKING: CustomerEligibility[] = ["AGE_VERIFICATION_REQUIRED", "IDENTITY_VERIFICATION_REQUIRED"];
const ALL_BLOCKING: CustomerEligibility[] = ["ACCOUNT_RESTRICTED", "ORDER_NOT_PERMITTED"];

export class VoiceCommerceService {
  private readonly backend: GimmeBackend;
  private readonly stores: VoiceStores;
  private readonly authorizer: PurchaseAuthorizer;
  private readonly now: () => Date;

  constructor(private readonly deps: ServiceDeps) {
    this.backend = deps.backend;
    this.stores = deps.stores;
    this.now = deps.now ?? (() => new Date());
    this.authorizer = new PurchaseAuthorizer(deps.config.authorizationSigningKey, deps.config.ttl.purchaseAuthorizationSeconds);
  }

  // ================================================================ sessions

  /** identify_customer / POST /v1/voice/sessions — the handshake (§44). */
  async identifyCustomer(ctx: CallContext, input: Out<typeof C.IdentifyCustomerInput>): Promise<Out<typeof C.IdentifyCustomerOutput>> {
    return this.run(ctx, "identify_customer", [SCOPES.CUSTOMER_READ], input, async () => {
      const customerId = ctx.principal.customerId;
      const customer = await this.backend.customers.getCustomer(customerId);
      if (!customer) throw new GimmeError("CUSTOMER_NOT_FOUND");
      const linked = await this.backend.customers.isPlatformLinked(customerId, input.platform, input.platform_user_id);
      if (!linked) {
        throw new GimmeError("CUSTOMER_NOT_FOUND", {
          message: "This assistant isn't linked to that GIMME account. Link it in the GIMME app.",
          suggestedAction: "LINK_ACCOUNT",
          details: { reason: "PLATFORM_USER_NOT_LINKED" },
        });
      }
      const now = this.now();
      const session: VoiceSession = {
        session_id: `VS-${randomUUID()}`,
        customer_id: customerId,
        platform: input.platform,
        platform_user_id: input.platform_user_id,
        ...(input.platform_session_id ? { platform_session_id: input.platform_session_id } : {}),
        ...(input.conversation_id ? { conversation_id: input.conversation_id } : {}),
        client_id: ctx.principal.clientId,
        authenticated: true,
        stage: "DISCOVERY",
        stage_history: [{ stage: "DISCOVERY", at: now.toISOString() }],
        funnel: [],
        created_at: now.toISOString(),
        expires_at: new Date(now.getTime() + this.deps.config.ttl.sessionSeconds * 1000).toISOString(),
      };
      this.advance(session, "CUSTOMER_IDENTIFICATION");
      this.funnel(session, "session_started");
      await this.saveSession(session);

      const addresses = await this.backend.customers.listAddresses(customerId);
      const def = addresses.find((a) => a.is_default) ?? addresses[0];
      this.audit(ctx, { action: "session.created", outcome: "SUCCESS", session_id: session.session_id, customer_id: customerId, platform: input.platform, platform_user_id: input.platform_user_id });
      return {
        session_id: session.session_id,
        customer_id: customerId,
        authenticated: true,
        customer_name: sanitizeForModel(customer.first_name, 40),
        default_address: def ? { id: def.id, summary: sanitizeForModel(def.summary) } : null,
        eligible_to_order: customer.eligibility === "ELIGIBLE",
        eligibility: customer.eligibility,
        expires_at: session.expires_at,
        handshake: {
          protocol_version: this.deps.config.versions.protocol,
          mcp_version: this.deps.config.versions.mcpContract,
          voice_api_version: this.deps.config.versions.voiceApi,
          capability_version: "1.0",
          platform: input.platform,
          session_id: session.session_id,
          customer_id: customerId,
          client_id: ctx.principal.clientId,
          scopes: ctx.principal.scopes,
          request_id: ctx.requestId,
          ...(input.conversation_id ? { conversation_id: input.conversation_id } : {}),
        },
      };
    });
  }

  async getSessionView(ctx: CallContext, sessionId: string): Promise<Out<typeof C.SessionView>> {
    const s = await this.loadSession(ctx, sessionId);
    return {
      session_id: s.session_id,
      customer_id: s.customer_id,
      platform: s.platform,
      ...(s.platform_session_id ? { platform_session_id: s.platform_session_id } : {}),
      authenticated: s.authenticated,
      stage: s.stage,
      created_at: s.created_at,
      expires_at: s.expires_at,
    };
  }

  // ============================================================ read tools

  async searchProducts(ctx: CallContext, input: Out<typeof C.SearchProductsInput>): Promise<Out<typeof C.SearchProductsOutput>> {
    return this.run(ctx, "search_products", [SCOPES.PRODUCTS_READ], input, async () => {
      const session = await this.loadSession(ctx, input.session_id);
      const products = await this.backend.catalog.search(input.query, { limit: input.limit });
      const addressId = await this.defaultAddressId(session.customer_id).catch(() => null);
      const stock = addressId && products.length
        ? await this.backend.inventory.check(products.map((p) => ({ sku: p.sku, quantity: 1 })), addressId)
        : null;
      return {
        products: products.map((p) => ({
          ...this.productView(p),
          available: stock ? (stock.items.find((i) => i.sku === p.sku)?.available ?? false) : false,
        })),
      };
    });
  }

  async resolveItems(ctx: CallContext, input: Out<typeof C.ResolveItemsInput>): Promise<Out<typeof C.ResolveItemsOutput>> {
    return this.run(ctx, "resolve_items", [SCOPES.PRODUCTS_READ, SCOPES.CUSTOMER_READ], input, async () => {
      const session = await this.loadSession(ctx, input.session_id);
      this.advance(session, "PRODUCT_RESOLUTION");
      let lines: CartLine[];
      if (input.request.type === "USUAL") {
        lines = await this.backend.customers.getUsualOrder(session.customer_id);
        if (!lines.length) {
          return { status: "NOT_FOUND", items: [], not_found: ["usual order"], speech: "You don't have a usual GIMME order yet. What would you like?" };
        }
      } else if (input.request.type === "LAST_ORDER") {
        const [last] = await this.backend.customers.listOrders(session.customer_id, 1);
        if (!last) return { status: "NOT_FOUND", items: [], not_found: ["last order"], speech: "I can't find a previous GIMME order. What would you like?" };
        lines = last.lines.map((l) => ({ sku: l.sku, quantity: l.quantity }));
      } else {
        const resolved: { sku: string; quantity: number; name: string }[] = [];
        const notFound: string[] = [];
        for (const req of input.request.items) {
          const r = await resolveItem(this.backend.catalog, req as ItemRequest);
          if (r.status === "RESOLVED") resolved.push(r.item);
          else if (r.status === "NOT_FOUND") notFound.push(r.query);
          else {
            await this.saveSession(session);
            return {
              status: "NEEDS_CLARIFICATION",
              items: resolved,
              clarification: {
                query: r.query,
                question: r.question,
                options: r.options.map((o) => ({ sku: o.sku, name: o.name, pack_size: o.pack_size, price: toAmount(o.price_cents) })),
              },
              speech: r.question,
            };
          }
        }
        await this.saveSession(session);
        if (notFound.length) {
          return { status: "NOT_FOUND", items: resolved, not_found: notFound, speech: `I couldn't find ${speakList(notFound.map((q) => sanitizeForModel(q, 60)))} at GIMME.` };
        }
        this.funnel(session, "product_resolved");
        await this.saveSession(session);
        return { status: "RESOLVED", items: resolved };
      }
      const products = await this.backend.catalog.getProducts(lines.map((l) => l.sku));
      this.funnel(session, "product_resolved");
      await this.saveSession(session);
      return {
        status: "RESOLVED",
        items: lines
          .filter((l) => products.has(l.sku))
          .map((l) => ({ sku: l.sku, quantity: l.quantity, name: sanitizeForModel(products.get(l.sku)!.name) })),
        ...(lines.some((l) => !products.has(l.sku)) ? { not_found: lines.filter((l) => !products.has(l.sku)).map((l) => l.sku) } : {}),
      };
    });
  }

  async getCustomerPreferences(ctx: CallContext, input: Out<typeof C.GetPreferencesInput>): Promise<Out<typeof C.GetPreferencesOutput>> {
    return this.run(ctx, "get_customer_preferences", [SCOPES.CUSTOMER_READ], input, async () => {
      const session = await this.loadSession(ctx, input.session_id);
      const cid = session.customer_id;
      const [usual, orders, addresses, methods] = await Promise.all([
        this.backend.customers.getUsualOrder(cid),
        this.backend.customers.listOrders(cid, 3),
        this.backend.customers.listAddresses(cid),
        this.backend.customers.listPaymentMethods(cid),
      ]);
      const products = await this.backend.catalog.getProducts(usual.map((l) => l.sku));
      return {
        usual_order: usual
          .filter((l) => products.has(l.sku))
          .map((l) => ({ sku: l.sku, quantity: l.quantity, name: sanitizeForModel(products.get(l.sku)!.name) })),
        recent_orders: orders.map((o) => ({
          order_id: o.order_id,
          placed_at: o.created_at,
          total: toAmount(o.total_cents),
          item_count: o.lines.reduce((n, l) => n + l.quantity, 0),
        })),
        addresses: addresses.map((a) => ({ id: a.id, label: sanitizeForModel(a.label, 40), summary: sanitizeForModel(a.summary), is_default: a.is_default })),
        payment_methods: methods.map((m) => ({ id: m.id, label: sanitizeForModel(m.label, 40), kind: m.kind, is_default: m.is_default })),
        preferred_address_id: (addresses.find((a) => a.is_default) ?? addresses[0])?.id ?? null,
        preferred_payment_method_id: (methods.find((m) => m.is_default) ?? methods[0])?.id ?? null,
      };
    });
  }

  async checkInventory(ctx: CallContext, input: Out<typeof C.CheckInventoryInput>): Promise<Out<typeof C.CheckInventoryOutput>> {
    return this.run(ctx, "check_inventory", [SCOPES.INVENTORY_READ], input, async () => {
      const session = await this.loadSession(ctx, input.session_id);
      const addressId = await this.resolveAddress(session.customer_id, input.address_id);
      await this.requireProducts(input.items);
      const check = await this.backend.inventory.check(input.items, addressId);
      this.advance(session, "INVENTORY_VALIDATION");
      await this.saveSession(session);
      const unavailable = check.items.filter((i) => !i.available).map((i) => i.sku);
      return {
        available: check.available,
        ...(check.available ? {} : { reason: "OUT_OF_STOCK" as const }),
        items: check.items.map((i) => ({ sku: i.sku, requested: i.requested, available: i.available })),
        alternatives: unavailable.length ? await this.alternativesFor(unavailable, addressId) : [],
      };
    });
  }

  async validateDelivery(ctx: CallContext, input: Out<typeof C.ValidateDeliveryInput>): Promise<Out<typeof C.ValidateDeliveryOutput>> {
    return this.run(ctx, "validate_delivery", [SCOPES.INVENTORY_READ], input, async () => {
      const session = await this.loadSession(ctx, input.session_id);
      const addressId = await this.resolveAddress(session.customer_id, input.address_id);
      // Without a basket, assume alcohol: the stricter rules apply.
      const containsAlcohol = input.items ? (await this.requireProducts(input.items)).some((p) => p.alcoholic) : true;
      const d = await this.backend.delivery.validate(session.customer_id, addressId, { containsAlcohol });
      this.advance(session, "DELIVERY_VALIDATION");
      await this.saveSession(session);
      return {
        eligible: d.eligible,
        ...(d.reason ? { reason: d.reason } : {}),
        address_id: addressId,
        ...(d.estimated_delivery_minutes !== undefined ? { estimated_delivery_minutes: d.estimated_delivery_minutes } : {}),
        ...(d.delivery_window ? { delivery_window: d.delivery_window } : {}),
        requirements: d.requirements,
      };
    });
  }

  async validateCustomer(ctx: CallContext, input: Out<typeof C.ValidateCustomerInput>): Promise<Out<typeof C.ValidateCustomerOutput>> {
    return this.run(ctx, "validate_customer", [SCOPES.CUSTOMER_READ], input, async () => {
      const session = await this.loadSession(ctx, input.session_id);
      const customer = await this.requireCustomer(session.customer_id);
      const products = input.items ? await this.requireProducts(input.items) : null;
      const containsAlcohol = products ? products.some((p) => p.alcoholic) : true;
      const blocked = complianceBlock(customer.eligibility, containsAlcohol);
      this.advance(session, "COMPLIANCE_VALIDATION");
      await this.saveSession(session);
      const restrictions: string[] = [];
      if (ALCOHOL_BLOCKING.includes(customer.eligibility)) restrictions.push("NO_ALCOHOL_UNTIL_VERIFIED");
      if (ALL_BLOCKING.includes(customer.eligibility)) restrictions.push("NO_ORDERS");
      if (containsAlcohol && !blocked) restrictions.push("ID_CHECK_ON_DELIVERY");
      return { status: customer.eligibility, can_order: !blocked, restrictions };
    });
  }

  async calculateCart(ctx: CallContext, input: Out<typeof C.CalculateCartInput>): Promise<Out<typeof C.CalculateCartOutput>> {
    return this.run(ctx, "calculate_cart", [SCOPES.CART_CREATE], input, async () => {
      const session = await this.loadSession(ctx, input.session_id);
      const addressId = await this.resolveAddress(session.customer_id, input.address_id);
      await this.requireProducts(input.items);
      const q = await this.backend.pricing.quote({
        customerId: session.customer_id,
        lines: input.items,
        addressId,
        ...(input.promotion_code ? { promotionCode: input.promotion_code } : {}),
      });
      this.funnel(session, "cart_priced");
      this.advance(session, "PRICE_CALCULATION");
      await this.saveSession(session);
      return {
        quote_id: q.quote_id,
        items: q.lines.map(lineView),
        subtotal: toAmount(q.subtotal_cents),
        delivery_fee: toAmount(q.fees.delivery_cents),
        service_fee: toAmount(q.fees.service_cents),
        packaging_fee: toAmount(q.fees.packaging_cents),
        discount: toAmount(q.discount_cents),
        ...(q.promotion_code ? { promotion_code: q.promotion_code } : {}),
        total: toAmount(q.total_cents),
        currency: q.currency,
        estimated_delivery_minutes: q.estimated_delivery_minutes,
        note: "Prices are confirmed only when an order intent is created and the customer confirms it.",
      };
    });
  }

  // ===================================================== pre-transaction

  async createOrderIntent(ctx: CallContext, input: Out<typeof C.CreateOrderIntentInput>): Promise<Out<typeof C.CreateOrderIntentOutput>> {
    return this.run(ctx, "create_order_intent", [SCOPES.CART_CREATE], input, async () => {
      const session = await this.loadSession(ctx, input.session_id);
      return this.idempotent(session.customer_id, "create_order_intent", input.idempotency_key, fingerprint(input), () =>
        this.buildIntent(ctx, session, input),
      );
    });
  }

  async getOrderIntent(ctx: CallContext, input: Out<typeof C.GetOrderIntentInput>): Promise<Out<typeof C.GetOrderIntentOutput>> {
    return this.run(ctx, "get_order_intent", [SCOPES.CART_CREATE], input, async () => {
      const intent = await this.loadIntent(ctx, input.order_intent_id);
      return { order_intent: this.intentView(intent) };
    });
  }

  /**
   * Begin customer authorization for an intent. Returns the confirmation
   * request to show the customer, and a GIMME-hosted confirmation link for
   * channels that can't run a native confirmation. It does not authorize
   * anything by itself.
   */
  async beginAuthorization(ctx: CallContext, orderIntentId: string): Promise<{ intent: OrderIntentRecord; confirmationUrl: string }> {
    requireScopes(ctx.principal.scopes, [SCOPES.CART_CREATE]);
    return this.stores.locks.withLock(`intent:${orderIntentId}`, async () => {
      const intent = await this.loadIntent(ctx, orderIntentId);
      this.assertUsable(intent, ["AWAITING_CONFIRMATION"]);
      const nonce = randomBytes(24).toString("base64url");
      intent.confirmation_nonce_hash = sha256(nonce);
      await this.saveIntent(intent);
      const session = await this.stores.sessions.get(intent.session_id);
      if (session) {
        this.funnel(session, "confirmation_requested");
        this.advance(session, "CUSTOMER_CONFIRMATION");
        await this.saveSession(session);
      }
      return {
        intent,
        confirmationUrl: `${this.deps.config.publicBaseUrl}/v1/voice/confirm/${encodeURIComponent(intent.order_intent_id)}?nonce=${nonce}`,
      };
    });
  }

  /**
   * Record the customer's decision. Callers:
   *  - first-party channels over REST (scope gimme.order.confirm) — the iOS
   *    app after Siri's native confirmation, the Android app;
   *  - the MCP server after the customer answered an elicitation;
   *  - the hosted confirmation page (proved by the single-use nonce).
   * A third-party agent's token can never reach this.
   */
  async confirmOrderIntent(
    ctx: CallContext,
    orderIntentId: string,
    input: Out<typeof C.ConfirmOrderIntentInput>,
    channel: { kind: "FIRST_PARTY" } | { kind: "MCP_ELICITATION" } | { kind: "HOSTED_PAGE"; nonce: string },
  ): Promise<Out<typeof C.RequestAuthorizationOutput>> {
    const scopes: Scope[] = channel.kind === "FIRST_PARTY" ? [SCOPES.ORDER_CONFIRM] : [];
    return this.run(ctx, "confirm_order_intent", scopes, { order_intent_id: orderIntentId, ...input }, async () =>
      this.stores.locks.withLock(`intent:${orderIntentId}`, async () => {
        const intent =
          channel.kind === "HOSTED_PAGE" ? await this.loadIntentByNonce(orderIntentId, channel.nonce) : await this.loadIntent(ctx, orderIntentId);
        this.assertUsable(intent, ["AWAITING_CONFIRMATION"]);
        const confirmation = this.confirmationRequest(intent);

        if (input.decision === "DECLINE") {
          this.transition(intent, "CANCELLED");
          intent.authorization_status = "DECLINED";
          await this.saveIntent(intent);
          await this.touchFunnel(intent.session_id, "confirmation_declined");
          this.audit(ctx, { action: "order_intent.declined", outcome: "SUCCESS", order_intent_id: intent.order_intent_id, customer_id: intent.customer_id, session_id: intent.session_id });
          return { status: "DECLINED", order_intent_id: intent.order_intent_id, confirmation, speech: speech.declined() };
        }

        let confirmedCents: number;
        try {
          confirmedCents = toCents(input.confirmed_total);
        } catch {
          throw new GimmeError("CONFIRMATION_MISMATCH", { details: { reason: "INVALID_AMOUNT" } });
        }
        if (confirmedCents !== intent.total_cents || input.confirmed_currency !== intent.currency) {
          this.audit(ctx, {
            action: "order_intent.confirmation_mismatch",
            outcome: "DENIED",
            order_intent_id: intent.order_intent_id,
            customer_id: intent.customer_id,
            data: { confirmed_total: input.confirmed_total, intent_total: toAmount(intent.total_cents) },
          });
          throw new GimmeError("CONFIRMATION_MISMATCH", {
            details: { confirmed_total: input.confirmed_total, order_total: toAmount(intent.total_cents) },
          });
        }
        if (this.deps.config.confirmation.requireDeviceAuthFor.includes(intent.platform) && !input.evidence.device_authenticated) {
          throw new GimmeError("AUTHENTICATION_REQUIRED", {
            message: speech.confirmOnDevice(),
            suggestedAction: "CONFIRM_ON_DEVICE",
            details: { reason: "DEVICE_AUTHENTICATION_REQUIRED", platform: intent.platform },
          });
        }

        const evidence: ConfirmationEvidence = {
          method: input.evidence.method,
          device_authenticated: input.evidence.device_authenticated,
          ...(input.evidence.platform_confirmation_id ? { platform_confirmation_id: input.evidence.platform_confirmation_id } : {}),
          confirmed_at: this.now().toISOString(),
        };
        const { token, claims } = await this.authorizer.issue(binding(intent), evidence);
        this.transition(intent, "CONFIRMED");
        intent.authorization_status = "AUTHORIZED";
        intent.authorization_id = claims.authorization_id;
        intent.authorization_token = token;
        intent.confirmation = evidence;
        delete intent.confirmation_nonce_hash; // single use
        await this.saveIntent(intent);

        const session = await this.stores.sessions.get(intent.session_id);
        if (session) {
          this.funnel(session, "confirmed");
          this.advance(session, "CUSTOMER_CONFIRMATION");
          await this.saveSession(session);
        }
        this.deps.events.publish("order_intent.confirmed", {
          customer_id: intent.customer_id,
          order_intent_id: intent.order_intent_id,
          platform: intent.platform,
          total: toAmount(intent.total_cents),
          currency: intent.currency,
        });
        this.audit(ctx, {
          action: "order_intent.confirmed",
          outcome: "SUCCESS",
          order_intent_id: intent.order_intent_id,
          customer_id: intent.customer_id,
          session_id: intent.session_id,
          platform: intent.platform,
          data: { authorization_id: claims.authorization_id, total: toAmount(intent.total_cents), currency: intent.currency, confirmation: evidence, channel: channel.kind },
        });
        return {
          status: "AUTHORIZED",
          order_intent_id: intent.order_intent_id,
          authorization_id: claims.authorization_id,
          authorization_token: token,
          authorization_expires_at: new Date(claims.expires_at * 1000).toISOString(),
          confirmation,
          speech: "Thanks, placing your order now.",
        };
      }),
    );
  }

  // =================================================== high-risk mutations

  async authorizePayment(ctx: CallContext, input: Out<typeof C.AuthorizePaymentInput>): Promise<Out<typeof C.AuthorizePaymentOutput>> {
    return this.run(ctx, "authorize_payment", [SCOPES.PAYMENT_AUTHORIZE], { order_intent_id: input.order_intent_id }, async () =>
      this.stores.locks.withLock(`intent:${input.order_intent_id}`, async () => {
        const intent = await this.loadIntent(ctx, input.order_intent_id);
        await this.loadSession(ctx, intent.session_id); // the paying client must own the session
        const token = input.authorization_token ?? intent.authorization_token;

        // A retry after success returns the same authorization rather than a second one.
        if (intent.status === "PAYMENT_AUTHORIZED" && intent.payment_authorization_id && token) {
          const existing = await this.stores.paymentAuths.get(intent.payment_authorization_id);
          if (existing) {
            await this.authorizer.verify(token, binding(intent));
            return paymentView(existing);
          }
        }
        this.assertUsable(intent, ["CONFIRMED"]);
        if (!token) throw new GimmeError("AUTHORIZATION_REQUIRED");
        const claims = await this.authorizer.verify(token, binding(intent));
        if (await this.stores.usedAuthorizations.get(claims.authorization_id)) {
          this.audit(ctx, { action: "payment.authorization_replay", outcome: "DENIED", order_intent_id: intent.order_intent_id, customer_id: intent.customer_id });
          throw new GimmeError("AUTHORIZATION_REQUIRED", { message: "That confirmation has already been used.", details: { reason: "AUTHORIZATION_ALREADY_USED" } });
        }

        const wallet = intent.payment_method_id.startsWith("WALLET:") ? (intent.payment_method_id.slice(7) as "APPLE_PAY" | "GOOGLE_PAY") : null;
        if (wallet && !input.wallet_token) throw new GimmeError("PAYMENT_REQUIRED", { message: "I need a wallet payment token to pay for this order.", details: { wallet } });
        if (!wallet && input.wallet_token) throw new GimmeError("INVALID_REQUEST", { details: { reason: "WALLET_TOKEN_FOR_SAVED_METHOD_INTENT" } });

        const result = await this.deps.metrics.time("gimme_payment_latency_ms", { op: "authorize" }, () =>
          this.backend.payments.authorize({
            customerId: intent.customer_id,
            amountCents: intent.total_cents,
            currency: intent.currency,
            source: wallet ? { type: "WALLET_TOKEN", wallet, token: input.wallet_token! } : { type: "SAVED_METHOD", paymentMethodId: intent.payment_method_id },
            // One intent can only ever produce one provider authorization, however often this is retried.
            idempotencyKey: `payauth:${intent.order_intent_id}`,
            metadata: { order_intent_id: intent.order_intent_id, customer_id: intent.customer_id, channel: "voice" },
          }),
        );

        if (result.status === "TIMEOUT") {
          this.deps.metrics.inc("gimme_payment_failures_total", { reason: "timeout" });
          this.audit(ctx, { action: "payment.authorize", outcome: "FAILURE", order_intent_id: intent.order_intent_id, customer_id: intent.customer_id, failure_reason: "PAYMENT_TIMEOUT" });
          // Intent stays CONFIRMED and the authorization unused: a retry asks the provider the same idempotent question.
          throw new GimmeError("PAYMENT_TIMEOUT");
        }
        if (result.status !== "AUTHORIZED" || !result.payment_reference) {
          this.transition(intent, "FAILED");
          intent.payment_status = "FAILED";
          await this.saveIntent(intent);
          const code: ErrorCode = result.status === "DECLINED" ? "PAYMENT_DECLINED" : "PAYMENT_FAILED";
          this.deps.metrics.inc("gimme_payment_failures_total", { reason: code.toLowerCase() });
          this.deps.events.publish("payment.failed", { customer_id: intent.customer_id, order_intent_id: intent.order_intent_id, reason: code });
          this.audit(ctx, { action: "payment.authorize", outcome: "FAILURE", order_intent_id: intent.order_intent_id, customer_id: intent.customer_id, failure_reason: result.decline_reason ?? code });
          throw new GimmeError(code, { details: { order_intent_id: intent.order_intent_id } });
        }

        await this.stores.usedAuthorizations.set(claims.authorization_id, { used_at: this.now().toISOString(), order_intent_id: intent.order_intent_id }, this.deps.config.ttl.idempotencyRecordSeconds);
        const record: PaymentAuthorizationRecord = {
          payment_authorization_id: `PAYAUTH-${randomUUID()}`,
          order_intent_id: intent.order_intent_id,
          customer_id: intent.customer_id,
          payment_reference: result.payment_reference,
          amount_cents: intent.total_cents,
          currency: intent.currency,
          status: "AUTHORIZED",
          created_at: this.now().toISOString(),
        };
        await this.stores.paymentAuths.set(record.payment_authorization_id, record, this.deps.config.ttl.idempotencyRecordSeconds);
        this.transition(intent, "PAYMENT_AUTHORIZED");
        intent.payment_status = "AUTHORIZED";
        intent.payment_authorization_id = record.payment_authorization_id;
        intent.payment_reference = record.payment_reference;
        await this.saveIntent(intent);
        await this.touchFunnel(intent.session_id, "payment_authorized", "PAYMENT_AUTHORIZATION");
        this.deps.events.publish("payment.authorized", { customer_id: intent.customer_id, order_intent_id: intent.order_intent_id, total: toAmount(intent.total_cents), currency: intent.currency });
        this.audit(ctx, {
          action: "payment.authorize",
          outcome: "SUCCESS",
          order_intent_id: intent.order_intent_id,
          customer_id: intent.customer_id,
          payment_reference: record.payment_reference,
          data: { authorization_id: claims.authorization_id, payment_authorization_id: record.payment_authorization_id, amount: toAmount(record.amount_cents), currency: record.currency },
        });
        return paymentView(record);
      }),
    );
  }

  async placeOrder(ctx: CallContext, input: Out<typeof C.PlaceOrderInput>): Promise<Out<typeof C.PlaceOrderOutput>> {
    return this.run(ctx, "place_order", [SCOPES.ORDER_CREATE], input, async () => {
      const fp = fingerprint({ order_intent_id: input.order_intent_id, payment_authorization_id: input.payment_authorization_id });
      return this.idempotent(ctx.principal.customerId, "place_order", input.idempotency_key, fp, () =>
        this.stores.locks.withLock(`intent:${input.order_intent_id}`, () => this.finalizeOrder(ctx, input)),
      );
    });
  }

  private async finalizeOrder(ctx: CallContext, input: Out<typeof C.PlaceOrderInput>): Promise<Out<typeof C.PlaceOrderOutput>> {
    const intent = await this.loadIntent(ctx, input.order_intent_id);
    const session = await this.loadSession(ctx, intent.session_id);

    if (intent.status === "ORDER_PLACED") {
      throw new GimmeError("ORDER_ALREADY_CREATED", { details: { order_id: intent.order_id } });
    }
    this.assertUsable(intent, ["PAYMENT_AUTHORIZED"]);
    const payAuth = await this.stores.paymentAuths.get(input.payment_authorization_id);
    if (!payAuth || payAuth.order_intent_id !== intent.order_intent_id || payAuth.status !== "AUTHORIZED" || payAuth.amount_cents !== intent.total_cents) {
      throw new GimmeError("AUTHORIZATION_REQUIRED", { message: "That payment authorization doesn't belong to this order.", details: { reason: "PAYMENT_AUTHORIZATION_MISMATCH" } });
    }

    this.advance(session, "FINAL_VALIDATION");
    const abort = async (status: OrderIntentStatus, reservationId?: string) => {
      if (reservationId) await this.backend.inventory.release(reservationId);
      await this.backend.payments.void(payAuth.payment_reference, `void:${intent.order_intent_id}`);
      payAuth.status = "VOIDED";
      await this.stores.paymentAuths.set(payAuth.payment_authorization_id, payAuth, this.deps.config.ttl.idempotencyRecordSeconds);
      this.transition(intent, status);
      intent.payment_status = "VOIDED";
      await this.saveIntent(intent);
      await this.saveSession(session);
    };

    // 1. The customer may still order this basket.
    const customer = await this.requireCustomer(intent.customer_id);
    const containsAlcohol = intent.items.some((l) => l.alcoholic);
    const block = complianceBlock(customer.eligibility, containsAlcohol);
    if (block) {
      await abort("FAILED");
      throw new GimmeError(block);
    }
    // 2. GIMME can still deliver there.
    const delivery = await this.backend.delivery.validate(intent.customer_id, intent.delivery_address_id, { containsAlcohol });
    if (!delivery.eligible) {
      await abort("FAILED");
      throw deliveryError(delivery);
    }
    // 3. Hold the stock — atomically, so a race with another order can't oversell.
    const lines = intent.items.map((l) => ({ sku: l.sku, quantity: l.quantity }));
    let reservationId: string;
    try {
      reservationId = await this.backend.inventory.reserve(lines, intent.delivery_address_id, intent.order_intent_id);
    } catch (err) {
      await abort("FAILED");
      const e = toGimmeError(err);
      if (e.code === "INVENTORY_CHANGED") {
        const check = await this.backend.inventory.check(lines, intent.delivery_address_id);
        const gone = check.items.filter((i) => !i.available).map((i) => i.sku);
        throw new GimmeError("INVENTORY_CHANGED", { details: { unavailable: gone, alternatives: await this.alternativesFor(gone, intent.delivery_address_id) } });
      }
      throw e;
    }
    // 4. The price the customer confirmed is still the price.
    const fresh = await this.backend.pricing.quote({
      customerId: intent.customer_id,
      lines,
      addressId: intent.delivery_address_id,
      ...(intent.promotion_code ? { promotionCode: intent.promotion_code } : {}),
    });
    if (fresh.total_cents !== intent.total_cents) {
      await abort("SUPERSEDED", reservationId);
      if (session.active_order_intent_id === intent.order_intent_id) delete session.active_order_intent_id;
      // Offer the customer the new price as a fresh intent they must confirm again (§32).
      const replacement = await this.buildIntent(ctx, session, {
        session_id: session.session_id,
        items: lines,
        address_id: intent.delivery_address_id,
        payment_method_id: intent.payment_method_id.startsWith("WALLET:") ? undefined : intent.payment_method_id,
        ...(intent.payment_method_id.startsWith("WALLET:") ? { wallet: intent.payment_method_id.slice(7) as "APPLE_PAY" | "GOOGLE_PAY" } : {}),
        ...(intent.promotion_code ? { promotion_code: intent.promotion_code } : {}),
        source: intent.source,
      }).catch(() => null);
      intent.superseded_by = replacement?.order_intent.order_intent_id;
      await this.saveIntent(intent);
      this.deps.events.publish("order_intent.superseded", { customer_id: intent.customer_id, order_intent_id: intent.order_intent_id, reason: "PRICE_CHANGED" });
      this.audit(ctx, {
        action: "order.price_changed",
        outcome: "FAILURE",
        order_intent_id: intent.order_intent_id,
        customer_id: intent.customer_id,
        failure_reason: "PRICE_CHANGED",
        data: { confirmed_total: toAmount(intent.total_cents), new_total: toAmount(fresh.total_cents) },
      });
      throw new GimmeError("PRICE_CHANGED", {
        message: speech.priceChanged(replacement ? toCents(replacement.order_intent.total) : fresh.total_cents),
        details: {
          previous_total: toAmount(intent.total_cents),
          new_total: replacement ? replacement.order_intent.total : toAmount(fresh.total_cents),
          ...(replacement ? { replacement_order_intent_id: replacement.order_intent.order_intent_id, confirmation: replacement.confirmation } : {}),
        },
      });
    }

    // 5. Create the order, then capture. If capture fails the order is cancelled, never left unpaid.
    this.advance(session, "ORDER_CREATION");
    const order = await this.backend.orders.create({
      customerId: intent.customer_id,
      quote: fresh,
      addressId: intent.delivery_address_id,
      paymentReference: payAuth.payment_reference,
      reservationId,
      idempotencyKey: `order:${intent.order_intent_id}`,
      source: { channel: "VOICE", platform: intent.platform, order_intent_id: intent.order_intent_id },
    });
    const captured = await this.backend.payments.capture(payAuth.payment_reference, intent.total_cents, `capture:${intent.order_intent_id}`);
    if (!captured.captured) {
      await this.backend.orders.cancel(order.order_id, "PAYMENT_CAPTURE_FAILED");
      await abort("FAILED");
      throw new GimmeError("PAYMENT_FAILED");
    }
    payAuth.status = "CAPTURED";
    await this.stores.paymentAuths.set(payAuth.payment_authorization_id, payAuth, this.deps.config.ttl.idempotencyRecordSeconds);
    this.transition(intent, "ORDER_PLACED");
    intent.payment_status = "CAPTURED";
    intent.order_id = order.order_id;
    await this.saveIntent(intent);

    session.last_order_id = order.order_id;
    delete session.active_order_intent_id;
    this.advance(session, "ORDER_CONFIRMED");
    this.funnel(session, "order_placed");
    await this.saveSession(session);

    this.deps.metrics.inc("gimme_orders_total", { platform: intent.platform, source: intent.source }, 1, "Voice orders placed");
    this.audit(ctx, {
      action: "order.created",
      outcome: "SUCCESS",
      session_id: session.session_id,
      customer_id: intent.customer_id,
      platform: intent.platform,
      platform_user_id: session.platform_user_id,
      order_intent_id: intent.order_intent_id,
      order_id: order.order_id,
      payment_reference: payAuth.payment_reference,
      data: {
        products: intent.items.map((l) => ({ sku: l.sku, quantity: l.quantity, unit_price: toAmount(l.unit_price_cents) })),
        subtotal: toAmount(intent.subtotal_cents),
        fees: { delivery: toAmount(intent.fees.delivery_cents), service: toAmount(intent.fees.service_cents), packaging: toAmount(intent.fees.packaging_cents) },
        discount: toAmount(intent.discount_cents),
        total: toAmount(intent.total_cents),
        currency: intent.currency,
        delivery_address_id: intent.delivery_address_id,
        confirmation_timestamp: intent.confirmation?.confirmed_at,
        confirmation_method: intent.confirmation?.method,
        authorization_timestamp: payAuth.created_at,
        idempotency_key: input.idempotency_key,
      },
    });

    const minutes = delivery.estimated_delivery_minutes ?? fresh.estimated_delivery_minutes;
    return {
      success: true,
      order_id: order.order_id,
      status: order.status,
      total: toAmount(order.total_cents),
      currency: order.currency,
      estimated_delivery_minutes: minutes,
      idempotent_replay: false,
      speech: speech.orderPlaced(minutes),
    };
  }

  // ============================================================ after order

  async getOrderStatus(ctx: CallContext, input: Out<typeof C.GetOrderStatusInput>): Promise<Out<typeof C.GetOrderStatusOutput>> {
    return this.run(ctx, "get_order_status", [SCOPES.ORDER_READ], input, async () => {
      if (input.session_id) {
        const s = await this.loadSession(ctx, input.session_id);
        this.advance(s, "DELIVERY_TRACKING");
        await this.saveSession(s);
      }
      const order = await this.loadOrder(ctx, input.order_id);
      const minutes = ORDER_FINAL_STATUSES.includes(order.status) || !order.estimated_delivery_at
        ? null
        : Math.max(0, Math.round((Date.parse(order.estimated_delivery_at) - this.now().getTime()) / 60_000));
      return {
        order_id: order.order_id,
        status: order.status,
        estimated_minutes: minutes,
        placed_at: order.created_at,
        speech: statusSpeech(order.status, minutes),
      };
    });
  }

  async cancelOrder(ctx: CallContext, input: Out<typeof C.CancelOrderInput>): Promise<Out<typeof C.CancelOrderOutput>> {
    return this.run(ctx, "cancel_order", [SCOPES.ORDER_CANCEL], input, async () => {
      const order = await this.loadOrder(ctx, input.order_id);
      const result = await this.backend.orders.cancel(order.order_id, input.reason ?? "CUSTOMER_REQUESTED_VIA_VOICE");
      this.audit(ctx, {
        action: "order.cancel",
        outcome: result.success ? "SUCCESS" : "FAILURE",
        customer_id: order.customer_id,
        order_id: order.order_id,
        ...(result.success ? {} : { failure_reason: result.reason }),
      });
      if (result.success) {
        return { success: true, order_id: order.order_id, status: result.status, speech: "Your GIMME order has been cancelled. You won't be charged." };
      }
      return {
        success: false,
        order_id: order.order_id,
        status: result.status,
        reason: result.reason,
        speech: result.reason === "ORDER_ALREADY_DISPATCHED"
          ? "Your order is already on its way, so it can't be cancelled now."
          : "That order can't be cancelled now. Contact GIMME support if you need help.",
      };
    });
  }

  async reorderPrevious(ctx: CallContext, input: Out<typeof C.ReorderInput>): Promise<Out<typeof C.ReorderOutput>> {
    return this.run(ctx, "reorder_previous_order", [SCOPES.ORDER_READ, SCOPES.CART_CREATE], input, async () => {
      const session = await this.loadSession(ctx, input.session_id);
      const previous = input.order_id
        ? await this.loadOrder(ctx, input.order_id)
        : (await this.backend.customers.listOrders(session.customer_id, 1))[0];
      if (!previous) throw new GimmeError("ORDER_NOT_FOUND", { message: "I can't find a previous GIMME order to repeat." });
      // Recreate, don't duplicate: the new intent re-runs every check at today's prices (§17).
      const created = await this.idempotent(session.customer_id, "reorder_previous_order", input.idempotency_key, fingerprint(input), () =>
        this.buildIntent(ctx, session, {
          session_id: session.session_id,
          items: previous.lines.map((l) => ({ sku: l.sku, quantity: l.quantity })),
          address_id: input.address_id ?? previous.delivery_address_id,
          source: "REORDER",
        }),
      );
      const changed = toCents(created.order_intent.total) !== previous.total_cents;
      return {
        ...created,
        previous_order: { order_id: previous.order_id, total: toAmount(previous.total_cents) },
        total_changed: changed,
        speech: changed
          ? `Your last order cost ${speakMoney(previous.total_cents)}, and prices have changed since. ${created.speech}`
          : created.speech,
      };
    });
  }

  async getReceipt(ctx: CallContext, input: Out<typeof C.GetReceiptInput>): Promise<Out<typeof C.GetReceiptOutput>> {
    return this.run(ctx, "get_order_receipt", [SCOPES.ORDER_READ], input, async () => {
      const order = await this.loadOrder(ctx, input.order_id);
      const [addresses, methods] = await Promise.all([
        this.backend.customers.listAddresses(order.customer_id),
        this.backend.customers.listPaymentMethods(order.customer_id),
      ]);
      const addr = addresses.find((a) => a.id === order.delivery_address_id);
      const intent = await this.stores.intents.get(order.source.order_intent_id);
      const methodLabel = intent?.payment_method_label ?? methods.find((m) => m.is_default)?.label ?? "Saved payment method";
      return {
        order_id: order.order_id,
        placed_at: order.created_at,
        status: order.status,
        items: order.lines.map(lineView),
        subtotal: toAmount(order.subtotal_cents),
        fees: feesView(order.fees),
        discount: toAmount(order.discount_cents),
        total: toAmount(order.total_cents),
        currency: order.currency,
        payment: sanitizeForModel(methodLabel, 40),
        delivered_to: addr ? sanitizeForModel(addr.summary) : "Your saved address",
        speech: `Your GIMME order ${order.order_id} came to ${speakMoney(order.total_cents)}, paid with ${sanitizeForModel(methodLabel, 40)}.`,
      };
    });
  }

  /** Called when GIMME's OMS reports a status change (webhook in, or the sandbox listener). */
  publishOrderStatus(order: Order, previous: Order["status"] | null): void {
    const minutes = order.estimated_delivery_at && !ORDER_FINAL_STATUSES.includes(order.status)
      ? Math.max(0, Math.round((Date.parse(order.estimated_delivery_at) - this.now().getTime()) / 60_000))
      : undefined;
    const data = {
      customer_id: order.customer_id,
      order_id: order.order_id,
      order_intent_id: order.source.order_intent_id,
      platform: order.source.platform,
      status: order.status,
      total: toAmount(order.total_cents),
      currency: order.currency,
      ...(minutes !== undefined ? { estimated_minutes: minutes } : {}),
    };
    if (previous === null) this.deps.events.publish("order.created", data);
    const type = `order.${order.status.toLowerCase()}` as const;
    if (type !== "order.created" && type !== "order.payment_authorized") this.deps.events.publish(type as Parameters<EventBus["publish"]>[0], data);
  }

  // ============================================================== internals

  private async buildIntent(ctx: CallContext, session: VoiceSession, input: Out<typeof C.CreateOrderIntentInput>): Promise<Out<typeof C.CreateOrderIntentOutput>> {
    const cid = session.customer_id;
    const lines = mergeLines(input.items);

    // PRODUCT_RESOLUTION
    this.advance(session, "PRODUCT_RESOLUTION");
    const products = await this.requireProducts(lines);
    const containsAlcohol = products.some((p) => p.alcoholic);

    // COMPLIANCE first: a restricted customer learns nothing more about stock or price.
    const customer = await this.requireCustomer(cid);
    const block = complianceBlock(customer.eligibility, containsAlcohol);
    if (block) throw new GimmeError(block);

    // INVENTORY_VALIDATION
    const addressId = await this.resolveAddress(cid, input.address_id);
    this.advance(session, "INVENTORY_VALIDATION");
    const stock = await this.backend.inventory.check(lines, addressId);
    if (!stock.available) {
      const gone = stock.items.filter((i) => !i.available).map((i) => i.sku);
      throw new GimmeError("PRODUCT_UNAVAILABLE", {
        message: gone.length === 1 ? `${spokenName(products.find((p) => p.sku === gone[0])?.name ?? "That product")} is currently unavailable.` : undefined,
        details: { unavailable: gone, alternatives: await this.alternativesFor(gone, addressId) },
      });
    }

    // DELIVERY_VALIDATION
    this.advance(session, "DELIVERY_VALIDATION");
    const delivery = await this.backend.delivery.validate(cid, addressId, { containsAlcohol });
    if (!delivery.eligible) throw deliveryError(delivery);

    this.advance(session, "COMPLIANCE_VALIDATION");

    // Payment method — a reference only.
    const methods = await this.backend.customers.listPaymentMethods(cid);
    let paymentMethodId: string;
    let paymentLabel: string;
    if (input.wallet) {
      paymentMethodId = `WALLET:${input.wallet}`;
      paymentLabel = input.wallet === "APPLE_PAY" ? "Apple Pay" : "Google Pay";
    } else {
      const chosen = input.payment_method_id
        ? methods.find((m) => m.id === input.payment_method_id)
        : (methods.find((m) => m.is_default) ?? methods[0]);
      if (!chosen) throw new GimmeError("PAYMENT_REQUIRED", input.payment_method_id ? { details: { reason: "UNKNOWN_PAYMENT_METHOD" } } : {});
      paymentMethodId = chosen.id;
      paymentLabel = chosen.label;
    }

    // PRICE_CALCULATION — the authoritative quote is the only source of every amount.
    this.advance(session, "PRICE_CALCULATION");
    const quote: Quote = await this.backend.pricing.quote({
      customerId: cid,
      lines,
      addressId,
      ...(input.promotion_code ? { promotionCode: input.promotion_code } : {}),
    });
    const addresses = await this.backend.customers.listAddresses(cid);
    const addr = addresses.find((a) => a.id === addressId)!;

    // One live intent per session: a new basket supersedes the last unconfirmed one.
    if (session.active_order_intent_id) await this.supersede(session.active_order_intent_id);

    const now = this.now();
    const intent: OrderIntentRecord = {
      order_intent_id: `OI-${randomUUID()}`,
      customer_id: cid,
      session_id: session.session_id,
      platform: session.platform,
      source: input.source ?? "BASKET",
      items: quote.lines,
      delivery_address_id: addressId,
      delivery_address_summary: sanitizeForModel(addr.summary),
      delivery_address_label: sanitizeForModel(addr.label, 40),
      payment_method_id: paymentMethodId,
      payment_method_label: sanitizeForModel(paymentLabel, 40),
      quote_id: quote.quote_id,
      subtotal_cents: quote.subtotal_cents,
      fees: quote.fees,
      discount_cents: quote.discount_cents,
      ...(quote.promotion_code ? { promotion_code: quote.promotion_code } : {}),
      total_cents: quote.total_cents,
      currency: quote.currency,
      estimated_delivery_minutes: delivery.estimated_delivery_minutes ?? quote.estimated_delivery_minutes,
      delivery_requirements: delivery.requirements,
      status: "AWAITING_CONFIRMATION",
      authorization_status: "PENDING",
      payment_status: "PENDING",
      created_at: now.toISOString(),
      expires_at: new Date(now.getTime() + this.deps.config.ttl.orderIntentSeconds * 1000).toISOString(),
    };
    await this.saveIntent(intent);
    session.active_order_intent_id = intent.order_intent_id;
    this.advance(session, "ORDER_INTENT_CREATED");
    this.funnel(session, "cart_priced");
    this.funnel(session, "intent_created");
    await this.saveSession(session);

    this.deps.events.publish("order_intent.created", {
      customer_id: cid,
      order_intent_id: intent.order_intent_id,
      platform: intent.platform,
      total: toAmount(intent.total_cents),
      currency: intent.currency,
    });
    this.audit(ctx, {
      action: "order_intent.created",
      outcome: "SUCCESS",
      session_id: session.session_id,
      customer_id: cid,
      platform: session.platform,
      order_intent_id: intent.order_intent_id,
      data: {
        products: intent.items.map((l) => ({ sku: l.sku, quantity: l.quantity, unit_price: toAmount(l.unit_price_cents) })),
        total: toAmount(intent.total_cents),
        currency: intent.currency,
        delivery_address_id: addressId,
        payment_method_id: paymentMethodId,
        quote_id: quote.quote_id,
      },
    });

    const lead = intent.source === "USUAL" ? "Your usual GIMME order" : intent.source === "REORDER" ? "Your last GIMME order" : undefined;
    return {
      order_intent: this.intentView(intent),
      confirmation: this.confirmationRequest(intent),
      speech: speech.orderSummary({
        ...(lead ? { lead } : {}),
        lines: intent.items.map((l) => ({ quantity: l.quantity, name: l.name })),
        totalCents: intent.total_cents,
        addressLabel: intent.delivery_address_label.toLowerCase() === "home" ? "home" : intent.delivery_address_label,
      }),
    };
  }

  private async supersede(orderIntentId: string): Promise<void> {
    await this.stores.locks.withLock(`intent:${orderIntentId}`, async () => {
      const prior = await this.stores.intents.get(orderIntentId);
      if (prior && (prior.status === "AWAITING_CONFIRMATION" || prior.status === "CONFIRMED")) {
        this.transition(prior, "SUPERSEDED");
        await this.saveIntent(prior);
      }
    });
  }

  private confirmationRequest(intent: OrderIntentRecord): Out<typeof C.ConfirmationRequest> {
    const what = speakList(intent.items.map((l) => speakLine(l.quantity, l.name)));
    return {
      requires_user_confirmation: true,
      confirmation_type: "PURCHASE",
      merchant: "GIMME",
      amount: toAmount(intent.total_cents),
      currency: intent.currency,
      summary: `${what} — ${speakMoney(intent.total_cents)} ${intent.currency}, delivered to ${intent.delivery_address_label}, paid with ${intent.payment_method_label}`,
      requires_device_authentication: this.deps.config.confirmation.requireDeviceAuthFor.includes(intent.platform),
      expires_at: intent.expires_at,
    };
  }

  intentView(intent: OrderIntentRecord): Out<typeof C.OrderIntentView> {
    return {
      order_intent_id: intent.order_intent_id,
      session_id: intent.session_id,
      status: this.effectiveStatus(intent),
      source: intent.source,
      items: intent.items.map(lineView),
      delivery_address: { id: intent.delivery_address_id, label: intent.delivery_address_label, summary: intent.delivery_address_summary },
      payment_method: { id: intent.payment_method_id, label: intent.payment_method_label },
      subtotal: toAmount(intent.subtotal_cents),
      fees: feesView(intent.fees),
      discount: toAmount(intent.discount_cents),
      ...(intent.promotion_code ? { promotion_code: intent.promotion_code } : {}),
      total: toAmount(intent.total_cents),
      currency: intent.currency,
      estimated_delivery_minutes: intent.estimated_delivery_minutes,
      delivery_requirements: intent.delivery_requirements,
      authorization_status: intent.authorization_status,
      payment_status: intent.payment_status,
      ...(intent.order_id ? { order_id: intent.order_id } : {}),
      created_at: intent.created_at,
      expires_at: intent.expires_at,
    };
  }

  /** For the hosted confirmation page: the intent's customer-facing summary, proved by the nonce. */
  async describeIntentForConfirmation(
    orderIntentId: string,
    nonce: string,
  ): Promise<{ customer_id: string; summary: string; total: number; currency: string; status: OrderIntentStatus }> {
    const intent = await this.loadIntentByNonce(orderIntentId, nonce);
    return {
      customer_id: intent.customer_id,
      summary: this.confirmationRequest(intent).summary,
      total: toAmount(intent.total_cents),
      currency: intent.currency,
      status: this.effectiveStatus(intent),
    };
  }

  private effectiveStatus(intent: OrderIntentRecord): OrderIntentStatus {
    if (!isTerminalIntent(intent.status) && Date.parse(intent.expires_at) <= this.now().getTime()) return "EXPIRED";
    return intent.status;
  }

  private assertUsable(intent: OrderIntentRecord, allowed: OrderIntentStatus[]): void {
    const status = this.effectiveStatus(intent);
    if (status === "EXPIRED") throw new GimmeError("ORDER_EXPIRED", { details: { order_intent_id: intent.order_intent_id } });
    if (status === "ORDER_PLACED") throw new GimmeError("ORDER_ALREADY_CREATED", { details: { order_id: intent.order_id } });
    if (status === "SUPERSEDED") {
      throw new GimmeError("ORDER_INTENT_INVALID_STATE", {
        message: "That order was replaced by a newer one.",
        details: { status, ...(intent.superseded_by ? { superseded_by: intent.superseded_by } : {}) },
      });
    }
    if (!allowed.includes(status)) {
      throw new GimmeError(status === "AWAITING_CONFIRMATION" ? "AUTHORIZATION_REQUIRED" : "ORDER_INTENT_INVALID_STATE", { details: { status, expected: allowed } });
    }
  }

  private transition(intent: OrderIntentRecord, to: OrderIntentStatus): void {
    assertIntentTransition(intent.status, to);
    intent.status = to;
  }

  private advance(session: VoiceSession, to: VoiceStage): void {
    if (session.stage === to) return;
    if (canAdvance(session.stage, to)) {
      session.stage = to;
      session.stage_history.push({ stage: to, at: this.now().toISOString() });
    }
  }

  private funnel(session: VoiceSession, stage: FunnelStage): void {
    if (session.funnel.includes(stage)) return;
    session.funnel.push(stage);
    this.deps.metrics.funnel(stage, session.platform);
  }

  private async touchFunnel(sessionId: string, stage: FunnelStage, voiceStage?: VoiceStage): Promise<void> {
    const s = await this.stores.sessions.get(sessionId);
    if (!s) return;
    this.funnel(s, stage);
    if (voiceStage) this.advance(s, voiceStage);
    await this.saveSession(s);
  }

  private async loadSession(ctx: CallContext, sessionId: string): Promise<VoiceSession> {
    const s = await this.stores.sessions.get(sessionId);
    // Same error whether the session doesn't exist or belongs to someone else: no probing.
    if (!s || s.customer_id !== ctx.principal.customerId || s.client_id !== ctx.principal.clientId || Date.parse(s.expires_at) <= this.now().getTime()) {
      throw new GimmeError("SESSION_INVALID");
    }
    return s;
  }

  private async saveSession(s: VoiceSession): Promise<void> {
    // Sliding expiry, capped at four times the idle TTL from creation.
    const ttl = this.deps.config.ttl.sessionSeconds;
    const hardCap = Date.parse(s.created_at) + ttl * 4 * 1000;
    s.expires_at = new Date(Math.min(this.now().getTime() + ttl * 1000, hardCap)).toISOString();
    const remaining = Math.max(1, Math.ceil((Date.parse(s.expires_at) - this.now().getTime()) / 1000));
    await this.stores.sessions.set(s.session_id, s, remaining);
  }

  private async loadIntent(ctx: CallContext, id: string): Promise<OrderIntentRecord> {
    const intent = await this.stores.intents.get(id);
    if (!intent || intent.customer_id !== ctx.principal.customerId) throw new GimmeError("ORDER_INTENT_NOT_FOUND");
    return intent;
  }

  private async loadIntentByNonce(id: string, nonce: string): Promise<OrderIntentRecord> {
    const intent = await this.stores.intents.get(id);
    if (!intent || !intent.confirmation_nonce_hash || !safeEqualHex(intent.confirmation_nonce_hash, sha256(nonce))) {
      throw new GimmeError("ORDER_INTENT_NOT_FOUND");
    }
    return intent;
  }

  private async saveIntent(intent: OrderIntentRecord): Promise<void> {
    // Keep terminal intents for a day for audit and idempotent replays.
    await this.stores.intents.set(intent.order_intent_id, intent, this.deps.config.ttl.idempotencyRecordSeconds);
  }

  private async loadOrder(ctx: CallContext, orderId?: string): Promise<Order> {
    const order = orderId
      ? await this.backend.orders.get(orderId)
      : (await this.backend.customers.listOrders(ctx.principal.customerId, 1))[0] ?? null;
    if (!order || order.customer_id !== ctx.principal.customerId) throw new GimmeError("ORDER_NOT_FOUND");
    return order;
  }

  private async requireCustomer(customerId: string): Promise<Customer> {
    const c = await this.backend.customers.getCustomer(customerId);
    if (!c) throw new GimmeError("CUSTOMER_NOT_FOUND");
    return c;
  }

  private async requireProducts(lines: CartLine[]): Promise<Product[]> {
    const found = await this.backend.catalog.getProducts(lines.map((l) => l.sku));
    const missing = lines.filter((l) => !found.has(l.sku)).map((l) => l.sku);
    if (missing.length) throw new GimmeError("PRODUCT_NOT_FOUND", { details: { skus: missing } });
    return lines.map((l) => found.get(l.sku)!);
  }

  private async defaultAddressId(customerId: string): Promise<string> {
    const addresses = await this.backend.customers.listAddresses(customerId);
    const def = addresses.find((a) => a.is_default) ?? addresses[0];
    if (!def) throw new GimmeError("ADDRESS_NOT_FOUND", { message: "There's no saved delivery address. Add one in the GIMME app." });
    return def.id;
  }

  private async resolveAddress(customerId: string, addressId?: string): Promise<string> {
    if (!addressId) return this.defaultAddressId(customerId);
    const addresses = await this.backend.customers.listAddresses(customerId);
    if (!addresses.some((a) => a.id === addressId)) throw new GimmeError("ADDRESS_NOT_FOUND");
    return addressId;
  }

  /** In-stock products in the same category (and brand first). Offered, never applied. */
  private async alternativesFor(skus: string[], addressId: string): Promise<Out<typeof C.AlternativeView>[]> {
    const products = await this.backend.catalog.getProducts(skus);
    const out: Out<typeof C.AlternativeView>[] = [];
    for (const sku of skus) {
      const p = products.get(sku);
      if (!p) continue;
      // Same category and alcohol status; same pack size first, then nearest price.
      const pool = (await this.backend.catalog.search(`${p.brand} ${p.category}`, { limit: 30 }))
        .filter((c) => c.sku !== sku && c.category === p.category && c.alcoholic === p.alcoholic)
        .sort((a, b) => Number(b.pack_size === p.pack_size) - Number(a.pack_size === p.pack_size) || Math.abs(a.price_cents - p.price_cents) - Math.abs(b.price_cents - p.price_cents));
      if (!pool.length) continue;
      const stock = await this.backend.inventory.check(pool.map((c) => ({ sku: c.sku, quantity: 1 })), addressId);
      for (const c of pool.filter((c) => stock.items.find((i) => i.sku === c.sku)?.available).slice(0, 2)) {
        out.push({ for_sku: sku, sku: c.sku, name: sanitizeForModel(c.name), price: toAmount(c.price_cents), requires_customer_approval: true });
      }
    }
    return out;
  }

  private productView(p: Product): Omit<Out<typeof C.ProductView>, "available"> {
    return {
      sku: p.sku,
      name: sanitizeForModel(p.name),
      brand: sanitizeForModel(p.brand, 60),
      category: sanitizeForModel(p.category, 60),
      ...(p.country ? { country: sanitizeForModel(p.country, 60) } : {}),
      pack_size: p.pack_size,
      alcoholic: p.alcoholic,
      price: toAmount(p.price_cents),
      currency: p.currency,
    };
  }

  /**
   * Idempotent execution. The first call with a key runs and stores its
   * outcome; repeats with the same key and same request get that outcome;
   * the same key with a different request is refused.
   */
  private async idempotent<R>(customerId: string, op: string, key: string | undefined, fp: string, fn: () => Promise<R>): Promise<R> {
    if (!key) return fn();
    const storeKey = `${op}:${customerId}:${key}`;
    return this.stores.locks.withLock(`idem:${storeKey}`, async () => {
      const existing = await this.stores.idempotency.get(storeKey);
      if (existing) {
        if (existing.fingerprint !== fp) throw new GimmeError("IDEMPOTENCY_CONFLICT");
        this.deps.metrics.inc("gimme_duplicate_requests_prevented_total", { op }, 1, "Repeated mutation requests answered from the idempotency store");
        if (existing.error) throw new GimmeError(existing.error.code as ErrorCode, existing.error.details ? { details: existing.error.details } : {});
        const r = existing.response as R;
        return (r && typeof r === "object" && "idempotent_replay" in r ? { ...r, idempotent_replay: true } : r) as R;
      }
      const ttl = this.deps.config.ttl.idempotencyRecordSeconds;
      try {
        const response = await fn();
        await this.stores.idempotency.set(storeKey, { fingerprint: fp, state: "DONE", response }, ttl);
        return response;
      } catch (err) {
        const e = toGimmeError(err);
        // Only final, non-retryable outcomes are remembered; a timeout must be retryable with the same key.
        if (!e.recoverable || e.code === "ORDER_ALREADY_CREATED") {
          await this.stores.idempotency.set(storeKey, { fingerprint: fp, state: "DONE", error: { code: e.code, ...(e.details ? { details: e.details } : {}) } }, ttl);
        }
        throw e;
      }
    });
  }

  /** Common wrapper: scope check, card-data guard, metrics, structured errors, audit of failures. */
  private async run<R>(ctx: CallContext, op: string, scopes: Scope[], input: unknown, fn: () => Promise<R>): Promise<R> {
    const start = performance.now();
    try {
      requireScopes(ctx.principal.scopes, scopes);
      assertNoCardData(input);
      const r = await fn();
      this.deps.metrics.inc("gimme_operations_total", { op, outcome: "success", channel: ctx.channel });
      return r;
    } catch (err) {
      const e = toGimmeError(err);
      this.deps.metrics.inc("gimme_operations_total", { op, outcome: e.code, channel: ctx.channel });
      if (e.code === "UNKNOWN_ERROR") this.deps.log.error("operation failed", { op, request_id: ctx.requestId, error: String((err as Error)?.stack ?? err) });
      this.audit(ctx, {
        action: op,
        outcome: e.code === "INSUFFICIENT_SCOPE" || e.code === "SESSION_INVALID" ? "DENIED" : "FAILURE",
        customer_id: ctx.principal.customerId,
        failure_reason: e.code,
        ...(isRecord(input) && typeof input.order_intent_id === "string" ? { order_intent_id: input.order_intent_id } : {}),
        ...(isRecord(input) && typeof input.session_id === "string" ? { session_id: input.session_id } : {}),
      });
      throw e;
    } finally {
      this.deps.metrics.observe("gimme_operation_latency_ms", performance.now() - start, { op });
    }
  }

  private audit(ctx: CallContext, event: Omit<AuditEvent, "request_id">): void {
    this.deps.audit.record({
      request_id: ctx.requestId,
      correlation_id: ctx.correlationId,
      client_id: ctx.principal.clientId,
      channel: ctx.channel,
      api_version: ctx.apiVersion,
      ...(ctx.device ? { device: ctx.device } : {}),
      ...event,
    });
  }
}

// ---------------------------------------------------------------- helpers

function complianceBlock(e: CustomerEligibility, containsAlcohol: boolean): ErrorCode | null {
  if (e === "ACCOUNT_RESTRICTED") return "ACCOUNT_RESTRICTED";
  if (e === "ORDER_NOT_PERMITTED") return "COMPLIANCE_FAILURE";
  if (containsAlcohol && e === "AGE_VERIFICATION_REQUIRED") return "AGE_VERIFICATION_REQUIRED";
  if (containsAlcohol && e === "IDENTITY_VERIFICATION_REQUIRED") return "IDENTITY_VERIFICATION_REQUIRED";
  return null;
}

function deliveryError(d: DeliveryCheck): GimmeError {
  const code: ErrorCode = d.reason === "OUTSIDE_SERVICE_AREA" ? "OUTSIDE_SERVICE_AREA" : d.reason === "STORE_CLOSED" ? "STORE_CLOSED" : "DELIVERY_UNAVAILABLE";
  return new GimmeError(code, { details: { reason: d.reason } });
}

function binding(intent: OrderIntentRecord): IntentBinding {
  return {
    order_intent_id: intent.order_intent_id,
    customer_id: intent.customer_id,
    session_id: intent.session_id,
    platform: intent.platform as Platform,
    lines: intent.items.map((l) => ({ sku: l.sku, quantity: l.quantity })),
    total_cents: intent.total_cents,
    currency: intent.currency,
    delivery_address_id: intent.delivery_address_id,
    payment_method_id: intent.payment_method_id,
    quote_id: intent.quote_id,
  };
}

function mergeLines(lines: CartLine[]): CartLine[] {
  const m = new Map<string, number>();
  for (const l of lines) m.set(l.sku, (m.get(l.sku) ?? 0) + l.quantity);
  return [...m].map(([sku, quantity]) => ({ sku, quantity }));
}

function lineView(l: Quote["lines"][number]) {
  return {
    sku: l.sku,
    name: sanitizeForModel(l.name),
    quantity: l.quantity,
    unit_price: toAmount(l.unit_price_cents),
    line_total: toAmount(l.line_total_cents),
    alcoholic: l.alcoholic,
  };
}

function feesView(f: Quote["fees"]) {
  return { delivery_fee: toAmount(f.delivery_cents), service_fee: toAmount(f.service_cents), packaging_fee: toAmount(f.packaging_cents) };
}

function paymentView(r: PaymentAuthorizationRecord) {
  return {
    authorized: true as const,
    payment_authorization_id: r.payment_authorization_id,
    payment_reference: r.payment_reference,
    amount: toAmount(r.amount_cents),
    currency: r.currency,
  };
}

function statusSpeech(status: Order["status"], minutes: number | null): string {
  switch (status) {
    case "CREATED":
    case "PAYMENT_AUTHORIZED":
    case "CONFIRMED":
    case "ACCEPTED":
      return `Your GIMME order is confirmed${minutes !== null ? ` and should arrive in ${speakMinutes(Math.max(minutes, 1))}` : ""}.`;
    case "PREPARING":
      return `Your GIMME order is being packed${minutes !== null ? ` and should arrive in ${speakMinutes(Math.max(minutes, 1))}` : ""}.`;
    case "DISPATCHED":
    case "OUT_FOR_DELIVERY":
      return `Your GIMME order is on its way${minutes !== null ? ` and should arrive in ${speakMinutes(Math.max(minutes, 1))}` : ""}. Have your ID ready.`;
    case "DELIVERED":
      return "Your GIMME order has been delivered.";
    case "CANCELLED":
      return "That GIMME order was cancelled.";
    case "REFUNDED":
      return "That GIMME order was refunded.";
    case "FAILED":
      return "That GIMME order couldn't be completed. You haven't been charged.";
  }
}

function fingerprint(v: unknown): string {
  return createHash("sha256").update(JSON.stringify(v)).digest("hex");
}

function sha256(s: string): string {
  return createHash("sha256").update(s).digest("hex");
}

function safeEqualHex(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null;
}
