/**
 * Apple adapter (§23–§25).
 *
 * The App Intents in the GIMME iOS app (adapters/apple/swift) call this
 * endpoint. The app is a first-party client: its OAuth token carries
 * gimme.order.confirm, because the purchase confirmation happens in Apple's
 * own UI — the intent's `requestConfirmation` dialog, with the intent's
 * authentication policy requiring an unlocked, authenticated device. The app
 * reports what it showed and whether the device was authenticated; the
 * service checks the confirmed amount against the intent to the cent.
 *
 * This file only translates shapes. No business logic.
 */

import type { DialogIntent, DialogResponse, DialogTurn } from "./dialog.js";
import type { ItemRequest } from "../core/resolver.js";

/** App Intent names as declared in GIMMEIntents.swift. */
export const APPLE_INTENTS: Record<string, DialogIntent> = {
  OrderUsualFromGIMMEIntent: "ORDER_USUAL",
  OrderFromGIMMEIntent: "ORDER_ITEMS",
  ReorderFromGIMMEIntent: "REORDER_LAST",
  SelectGIMMEProductIntent: "SELECT_OPTION",
  ConfirmGIMMEOrderIntent: "CONFIRM",
  DeclineGIMMEOrderIntent: "DECLINE",
  GetGIMMEOrderStatusIntent: "ORDER_STATUS",
  CancelGIMMEOrderIntent: "CANCEL_ORDER",
  GetGIMMEReceiptIntent: "RECEIPT",
};

export interface AppleIntentRequest {
  intent: string;
  /** Stable, app-scoped identifier for the Apple user (not the Apple ID). */
  apple_user_id: string;
  /** Identifies one Siri/Shortcuts interaction across turns. */
  interaction_id: string;
  session_id?: string;
  parameters?: {
    product_query?: string;
    product_sku?: string;
    quantity?: number;
    unit?: "PACK" | "UNIT";
    order_id?: string;
  };
  /** Present on ConfirmGIMMEOrderIntent after `requestConfirmation` returned. */
  confirmation?: {
    confirmed_total: number;
    /** True when the intent ran with authenticationPolicy .requiresAuthentication on an unlocked device. */
    device_authenticated: boolean;
    confirmation_id?: string;
  };
  apple_pay_token?: string;
}

export interface AppleIntentResponse {
  session_id: string;
  /** IntentDialog: what Siri says, and what it shows. */
  dialog: { full: string; supporting?: string };
  /** When set, the App Intent must call requestConfirmation with this, then send ConfirmGIMMEOrderIntent. */
  needs_confirmation?: {
    order_intent_id: string;
    amount: number;
    currency: string;
    merchant: string;
    summary: string;
    requires_device_authentication: boolean;
  };
  /** Options for a disambiguation prompt (requestDisambiguation). */
  disambiguation?: { sku: string; title: string; subtitle: string }[];
  /** Open the GIMME app (age verification, add a card, link account, confirm on device). */
  open_app?: { reason: string };
  order?: DialogResponse["order"];
  error_code?: string;
}

export function fromApple(req: AppleIntentRequest): DialogTurn {
  const intent = APPLE_INTENTS[req.intent];
  if (!intent) throw new Error(`unsupported Apple intent ${req.intent}`);
  const p = req.parameters ?? {};
  const item: ItemRequest | undefined =
    p.product_query || p.product_sku
      ? {
          ...(p.product_query ? { query: p.product_query } : {}),
          ...(p.product_sku ? { sku: p.product_sku } : {}),
          ...(p.quantity ? { quantity: p.quantity } : {}),
          ...(p.unit ? { unit: p.unit } : {}),
        }
      : undefined;
  return {
    platform: "APPLE",
    platform_user_id: req.apple_user_id,
    conversation_id: req.interaction_id,
    ...(req.session_id ? { session_id: req.session_id } : {}),
    intent,
    ...(item && intent === "ORDER_ITEMS" ? { items: [item] } : {}),
    ...(intent === "SELECT_OPTION" && p.product_sku ? { option_sku: p.product_sku } : {}),
    ...(req.confirmation
      ? {
          confirmation: {
            confirmed_total: req.confirmation.confirmed_total,
            device_authenticated: req.confirmation.device_authenticated,
            method: "APPLE_APP_INTENT_CONFIRMATION",
            ...(req.confirmation.confirmation_id ? { platform_confirmation_id: req.confirmation.confirmation_id } : {}),
          },
        }
      : {}),
    ...(req.apple_pay_token ? { wallet_token: req.apple_pay_token } : {}),
    ...(p.order_id ? { order_id: p.order_id } : {}),
  };
}

export function toApple(res: DialogResponse): AppleIntentResponse {
  const out: AppleIntentResponse = { session_id: res.session_id, dialog: { full: res.speech } };
  if (res.expect === "CONFIRMATION" && res.confirmation && res.order_intent_id) {
    out.needs_confirmation = {
      order_intent_id: res.order_intent_id,
      amount: res.confirmation.amount,
      currency: res.confirmation.currency,
      merchant: res.confirmation.merchant,
      summary: res.confirmation.summary,
      requires_device_authentication: res.confirmation.requires_device_authentication,
    };
    out.dialog.supporting = res.confirmation.summary;
  }
  if (res.expect === "SELECTION" && res.options) {
    out.disambiguation = res.options.map((o) => ({ sku: o.sku, title: o.name, subtitle: `$${o.price.toFixed(2)}` }));
  }
  if (res.expect === "DEVICE_CONFIRMATION") out.open_app = { reason: "CONFIRM_ON_DEVICE" };
  if (res.expect === "APP_HANDOFF") out.open_app = { reason: res.error?.suggested_action ?? "OPEN_APP" };
  if (res.order) out.order = res.order;
  if (res.error) out.error_code = res.error.error_code;
  return out;
}
