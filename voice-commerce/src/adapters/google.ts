/**
 * Google adapter (§26).
 *
 * Google sunset Conversational Actions in June 2023, so this does not target
 * the Actions SDK. It defines one stable fulfillment contract that any
 * Google surface GIMME is approved for can be wired to — the GIMME Android
 * app exposing App Functions / App Actions to Gemini and Assistant, or a
 * future Google agent surface — with account linking providing the GIMME
 * OAuth token. When the Android app is the caller it is a first-party
 * client and confirms on-device (BiometricPrompt / device credential); a
 * surface that cannot show GIMME's confirmation gets APP_HANDOFF instead.
 *
 * This file only translates shapes. No business logic.
 */

import type { DialogIntent, DialogResponse, DialogTurn } from "./dialog.js";

export const GOOGLE_HANDLERS: Record<string, DialogIntent> = {
  "gimme.order.usual": "ORDER_USUAL",
  "gimme.order.items": "ORDER_ITEMS",
  "gimme.order.reorder_last": "REORDER_LAST",
  "gimme.order.select": "SELECT_OPTION",
  "gimme.order.confirm": "CONFIRM",
  "gimme.order.decline": "DECLINE",
  "gimme.order.status": "ORDER_STATUS",
  "gimme.order.cancel": "CANCEL_ORDER",
  "gimme.order.receipt": "RECEIPT",
};

export interface GoogleFulfillmentRequest {
  handler: string;
  /** Google account-linked user id as seen by the GIMME Android app / linking flow. */
  google_user_id: string;
  conversation_id: string;
  session_id?: string;
  params?: {
    items?: { query?: string; sku?: string; quantity?: number; unit?: "PACK" | "UNIT" }[];
    selected_sku?: string;
    order_id?: string;
  };
  confirmation?: { confirmed_total: number; device_authenticated: boolean; method?: string; confirmation_id?: string };
  google_pay_token?: string;
}

export interface GoogleFulfillmentResponse {
  session_id: string;
  prompt: { speech: string; text: string };
  expect: DialogResponse["expect"];
  end_conversation: boolean;
  /** Present when the surface must collect an explicit, on-device confirmation for this amount. */
  transaction_decision?: {
    order_intent_id: string;
    amount: { currency_code: string; units: number; nanos: number };
    merchant_name: string;
    summary: string;
    requires_device_authentication: boolean;
  };
  suggestions?: { sku: string; title: string; price: number }[];
  order?: DialogResponse["order"];
  error_code?: string;
}

export function fromGoogle(req: GoogleFulfillmentRequest): DialogTurn {
  const intent = GOOGLE_HANDLERS[req.handler];
  if (!intent) throw new Error(`unsupported Google handler ${req.handler}`);
  return {
    platform: "GOOGLE",
    platform_user_id: req.google_user_id,
    conversation_id: req.conversation_id,
    ...(req.session_id ? { session_id: req.session_id } : {}),
    intent,
    ...(req.params?.items ? { items: req.params.items } : {}),
    ...(req.params?.selected_sku ? { option_sku: req.params.selected_sku } : {}),
    ...(req.params?.order_id ? { order_id: req.params.order_id } : {}),
    ...(req.confirmation
      ? {
          confirmation: {
            confirmed_total: req.confirmation.confirmed_total,
            device_authenticated: req.confirmation.device_authenticated,
            method: req.confirmation.method ?? "ANDROID_APP_CONFIRMATION",
            ...(req.confirmation.confirmation_id ? { platform_confirmation_id: req.confirmation.confirmation_id } : {}),
          },
        }
      : {}),
    ...(req.google_pay_token ? { wallet_token: req.google_pay_token } : {}),
  };
}

export function toGoogle(res: DialogResponse): GoogleFulfillmentResponse {
  const out: GoogleFulfillmentResponse = {
    session_id: res.session_id,
    prompt: { speech: res.speech, text: res.speech },
    expect: res.expect,
    end_conversation: res.end_conversation,
  };
  if (res.expect === "CONFIRMATION" && res.confirmation && res.order_intent_id) {
    const units = Math.trunc(res.confirmation.amount);
    out.transaction_decision = {
      order_intent_id: res.order_intent_id,
      amount: { currency_code: res.confirmation.currency, units, nanos: Math.round((res.confirmation.amount - units) * 100) * 10_000_000 },
      merchant_name: res.confirmation.merchant,
      summary: res.confirmation.summary,
      requires_device_authentication: res.confirmation.requires_device_authentication,
    };
  }
  if (res.options) out.suggestions = res.options.map((o) => ({ sku: o.sku, title: o.name, price: o.price }));
  if (res.order) out.order = res.order;
  if (res.error) out.error_code = res.error.error_code;
  return out;
}
