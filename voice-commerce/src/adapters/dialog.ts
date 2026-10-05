/**
 * Platform-neutral voice dialog (§19, §35).
 *
 * A voice platform hands over one turn at a time ("order my usual", "the
 * twelve-pack", "yes", "where's my order?"). This drives the service through
 * the ordering state machine and returns one short spoken reply plus what
 * the platform should expect next. The Apple and Google adapters are thin
 * translations of their platform's request/response shapes onto this.
 *
 * It holds no ordering rules: every decision is a VoiceCommerceService call.
 * It only remembers conversational context (an unanswered clarifying
 * question, the intent awaiting a yes/no) between turns.
 */

import type { z } from "zod";
import type { VoiceCommerceRuntime } from "../app.js";
import type * as C from "../contracts/schemas.js";
import type { CallContext } from "../core/models.js";
import type { ItemRequest } from "../core/resolver.js";
import { GimmeError, toGimmeError, type ErrorBody } from "../domain/errors.js";
import type { Platform } from "../domain/types.js";
import { MemoryStore, type KeyValueStore } from "../store/store.js";
import { spokenName } from "../core/speech.js";

export const DIALOG_INTENTS = [
  "ORDER_USUAL",
  "ORDER_ITEMS",
  "REORDER_LAST",
  "SELECT_OPTION",
  "CONFIRM",
  "DECLINE",
  "ORDER_STATUS",
  "CANCEL_ORDER",
  "RECEIPT",
] as const;
export type DialogIntent = (typeof DIALOG_INTENTS)[number];

export interface DialogTurn {
  platform: Platform;
  platform_user_id: string;
  conversation_id: string;
  session_id?: string;
  intent: DialogIntent;
  items?: ItemRequest[];
  /** SELECT_OPTION: the SKU the customer picked from the options offered. */
  option_sku?: string;
  /** CONFIRM: what the platform showed the customer and how it authenticated them. */
  confirmation?: { confirmed_total: number; device_authenticated: boolean; method: string; platform_confirmation_id?: string };
  wallet_token?: string;
  order_id?: string;
}

export type Expect = "NONE" | "SELECTION" | "CONFIRMATION" | "DEVICE_CONFIRMATION" | "APP_HANDOFF";

export interface DialogResponse {
  session_id: string;
  speech: string;
  expect: Expect;
  end_conversation: boolean;
  confirmation?: z.infer<typeof C.ConfirmationRequest>;
  order_intent_id?: string;
  options?: { sku: string; name: string; price: number }[];
  order?: { order_id: string; status: string; estimated_minutes: number | null; total?: number };
  error?: ErrorBody;
}

interface DialogState {
  session_id: string;
  pending_items?: ItemRequest[];
  resolved_items?: { sku: string; quantity: number }[];
  clarifying_index?: number;
  awaiting_intent_id?: string;
}

export class VoiceDialog {
  constructor(
    private readonly rt: VoiceCommerceRuntime,
    private readonly state: KeyValueStore<DialogState> = new MemoryStore<DialogState>(),
  ) {}

  async handle(ctx: CallContext, turn: DialogTurn): Promise<DialogResponse> {
    const key = `${ctx.principal.customerId}:${turn.platform}:${turn.conversation_id}`;
    let st = await this.state.get(key);
    if (!st || (turn.session_id && turn.session_id !== st.session_id)) {
      const s = turn.session_id
        ? await this.rt.service.getSessionView(ctx, turn.session_id)
        : await this.rt.service.identifyCustomer(ctx, { platform: turn.platform, platform_user_id: turn.platform_user_id, conversation_id: turn.conversation_id });
      st = { session_id: s.session_id };
    }
    const save = () => this.state.set(key, st!, this.rt.config.ttl.sessionSeconds);
    let res: DialogResponse;
    try {
      res = await this.dispatch(ctx, turn, st);
    } catch (err) {
      res = this.errorResponse(st, toGimmeError(err));
    }
    await save(); // after errorResponse, which may move the conversation to a replacement intent
    return res;
  }

  private async dispatch(ctx: CallContext, turn: DialogTurn, st: DialogState): Promise<DialogResponse> {
    const svc = this.rt.service;
    switch (turn.intent) {
      case "ORDER_USUAL":
      case "REORDER_LAST": {
        if (turn.intent === "REORDER_LAST") {
          const r = await svc.reorderPrevious(ctx, { session_id: st.session_id });
          return this.awaitConfirmation(st, r.order_intent.order_intent_id, r.speech, r.confirmation);
        }
        const resolved = await svc.resolveItems(ctx, { session_id: st.session_id, request: { type: "USUAL" } });
        if (resolved.status !== "RESOLVED") return this.say(st, resolved.speech ?? "I couldn't find your usual order.", "NONE", true);
        return this.intentFor(ctx, st, resolved.items, "USUAL");
      }
      case "ORDER_ITEMS": {
        if (!turn.items?.length) return this.say(st, "What would you like from GIMME?", "NONE", false);
        st.pending_items = turn.items;
        st.resolved_items = [];
        st.clarifying_index = 0;
        return this.continueResolution(ctx, st);
      }
      case "SELECT_OPTION": {
        if (!turn.option_sku) return this.say(st, "Sorry, which product did you want?", "NONE", false);
        if (st.clarifying_index === undefined || !st.pending_items) {
          // Picking an offered alternative: a new basket of exactly what was chosen, confirmed as usual.
          st.pending_items = [{ sku: turn.option_sku, quantity: 1 }];
          st.resolved_items = [];
          st.clarifying_index = 0;
          return this.continueResolution(ctx, st);
        }
        const asked = st.pending_items[st.clarifying_index]!;
        st.pending_items[st.clarifying_index] = { sku: turn.option_sku, quantity: asked.unit === "UNIT" ? 1 : (asked.quantity ?? 1) };
        return this.continueResolution(ctx, st);
      }
      case "CONFIRM":
        return this.confirmAndPlace(ctx, turn, st);
      case "DECLINE": {
        if (st.awaiting_intent_id && turn.confirmation) {
          await svc.confirmOrderIntent(
            ctx,
            st.awaiting_intent_id,
            { decision: "DECLINE", confirmed_total: turn.confirmation.confirmed_total, confirmed_currency: "NZD", evidence: { method: turn.confirmation.method, device_authenticated: turn.confirmation.device_authenticated } },
            { kind: "FIRST_PARTY" },
          ).catch(() => undefined);
        }
        delete st.awaiting_intent_id;
        this.rt.metrics.funnel("abandoned", turn.platform);
        return this.say(st, "No problem, I haven't placed the order.", "NONE", true);
      }
      case "ORDER_STATUS": {
        const s = await svc.getOrderStatus(ctx, { session_id: st.session_id, ...(turn.order_id ? { order_id: turn.order_id } : {}) });
        return { ...this.say(st, s.speech, "NONE", true), order: { order_id: s.order_id, status: s.status, estimated_minutes: s.estimated_minutes } };
      }
      case "CANCEL_ORDER": {
        const s = await svc.getOrderStatus(ctx, { ...(turn.order_id ? { order_id: turn.order_id } : {}) });
        const c = await svc.cancelOrder(ctx, { order_id: s.order_id });
        return { ...this.say(st, c.speech, "NONE", true), order: { order_id: c.order_id, status: c.status, estimated_minutes: null } };
      }
      case "RECEIPT": {
        const r = await svc.getReceipt(ctx, { ...(turn.order_id ? { order_id: turn.order_id } : {}) });
        return { ...this.say(st, r.speech, "NONE", true), order: { order_id: r.order_id, status: r.status, estimated_minutes: null, total: r.total } };
      }
    }
  }

  private async continueResolution(ctx: CallContext, st: DialogState): Promise<DialogResponse> {
    const items = st.pending_items!;
    for (let i = st.clarifying_index ?? 0; i < items.length; i++) {
      const r = await this.rt.service.resolveItems(ctx, { session_id: st.session_id, request: { type: "ITEMS", items: [items[i]!] } });
      if (r.status === "NEEDS_CLARIFICATION") {
        st.clarifying_index = i;
        return {
          ...this.say(st, r.clarification!.question, "SELECTION", false),
          options: r.clarification!.options.map((o) => ({ sku: o.sku, name: o.name, price: o.price })),
        };
      }
      if (r.status === "NOT_FOUND") {
        delete st.pending_items;
        delete st.clarifying_index;
        return this.say(st, r.speech ?? "I couldn't find that at GIMME.", "NONE", false);
      }
      st.resolved_items!.push(...r.items.map((x) => ({ sku: x.sku, quantity: x.quantity })));
    }
    const lines = st.resolved_items!;
    delete st.pending_items;
    delete st.clarifying_index;
    delete st.resolved_items;
    return this.intentFor(ctx, st, lines, "BASKET");
  }

  private async intentFor(ctx: CallContext, st: DialogState, items: { sku: string; quantity: number }[], source: "USUAL" | "BASKET"): Promise<DialogResponse> {
    const r = await this.rt.service.createOrderIntent(ctx, { session_id: st.session_id, items, source });
    return this.awaitConfirmation(st, r.order_intent.order_intent_id, r.speech, r.confirmation);
  }

  private awaitConfirmation(st: DialogState, intentId: string, speech: string, confirmation: z.infer<typeof C.ConfirmationRequest>): DialogResponse {
    st.awaiting_intent_id = intentId;
    return { ...this.say(st, speech, "CONFIRMATION", false), confirmation, order_intent_id: intentId };
  }

  private async confirmAndPlace(ctx: CallContext, turn: DialogTurn, st: DialogState): Promise<DialogResponse> {
    const svc = this.rt.service;
    const intentId = st.awaiting_intent_id;
    if (!intentId) return this.say(st, "There's nothing waiting to be confirmed. What would you like to order?", "NONE", false);
    if (!turn.confirmation) {
      // A bare "yes" with no platform confirmation evidence can't authorize a purchase.
      throw new GimmeError("AUTHENTICATION_REQUIRED", { message: "I need you to confirm this purchase on your device.", suggestedAction: "CONFIRM_ON_DEVICE" });
    }
    const auth = await svc.confirmOrderIntent(
      ctx,
      intentId,
      {
        decision: "CONFIRM",
        confirmed_total: turn.confirmation.confirmed_total,
        confirmed_currency: "NZD",
        evidence: {
          method: turn.confirmation.method,
          device_authenticated: turn.confirmation.device_authenticated,
          ...(turn.confirmation.platform_confirmation_id ? { platform_confirmation_id: turn.confirmation.platform_confirmation_id } : {}),
        },
      },
      { kind: "FIRST_PARTY" },
    );
    const pay = await svc.authorizePayment(ctx, {
      order_intent_id: intentId,
      authorization_token: auth.authorization_token!,
      ...(turn.wallet_token ? { wallet_token: turn.wallet_token } : {}),
    });
    const placed = await svc.placeOrder(ctx, {
      order_intent_id: intentId,
      payment_authorization_id: pay.payment_authorization_id,
      // §21: platform + customer + conversation + intent.
      idempotency_key: `${turn.platform}:${ctx.principal.customerId}:${turn.conversation_id}:${intentId}`,
    });
    delete st.awaiting_intent_id;
    return {
      ...this.say(st, placed.speech, "NONE", true),
      order: { order_id: placed.order_id, status: placed.status, estimated_minutes: placed.estimated_delivery_minutes, total: placed.total },
    };
  }

  private errorResponse(st: DialogState, e: GimmeError): DialogResponse {
    const body = e.toBody();
    if (e.code === "PRICE_CHANGED" && typeof e.details?.replacement_order_intent_id === "string") {
      st.awaiting_intent_id = e.details.replacement_order_intent_id;
      return {
        ...this.say(st, e.message, "CONFIRMATION", false),
        confirmation: e.details.confirmation as z.infer<typeof C.ConfirmationRequest>,
        order_intent_id: e.details.replacement_order_intent_id,
        error: body,
      };
    }
    if (e.suggestedAction === "CONFIRM_ON_DEVICE") return { ...this.say(st, e.message, "DEVICE_CONFIRMATION", false), error: body };
    if (["VERIFY_AGE_IN_APP", "VERIFY_IDENTITY_IN_APP", "ADD_PAYMENT_METHOD_IN_APP", "LINK_ACCOUNT"].includes(e.suggestedAction)) {
      return { ...this.say(st, e.message, "APP_HANDOFF", true), error: body };
    }
    if (e.code === "PRODUCT_UNAVAILABLE" || e.code === "INVENTORY_CHANGED") {
      const alts = (e.details?.alternatives as { sku: string; name: string; price: number }[] | undefined) ?? [];
      delete st.awaiting_intent_id;
      if (alts.length) {
        return {
          ...this.say(st, `${e.message} Would you like ${alts.slice(0, 2).map((a) => spokenName(a.name)).join(" or ")} instead?`, "SELECTION", false),
          options: alts.map((a) => ({ sku: a.sku, name: a.name, price: a.price })),
          error: body,
        };
      }
    }
    if (!e.recoverable || e.code === "CONFIRMATION_DECLINED") delete st.awaiting_intent_id;
    return { ...this.say(st, e.message, "NONE", !e.recoverable), error: body };
  }

  private say(st: DialogState, speech: string, expect: Expect, end: boolean): DialogResponse {
    return { session_id: st.session_id, speech, expect, end_conversation: end };
  }
}
