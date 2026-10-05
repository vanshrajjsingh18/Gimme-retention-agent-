/**
 * The voice layer's own objects (§28, §29). These are short-lived and hold
 * references to GIMME data (customer id, address id, payment method id),
 * never copies of payment credentials.
 */

import type { VoiceStage, AuthorizationStatus, OrderIntentStatus, PaymentStatus } from "../domain/state-machine.js";
import type { Cents, Currency, Fees, Platform, PricedLine } from "../domain/types.js";
import type { ConfirmationEvidence } from "../security/purchase-authorization.js";
import type { Principal } from "../security/access-tokens.js";

export interface CallContext {
  principal: Principal;
  requestId: string;
  correlationId: string;
  channel: "MCP" | "VOICE_API" | "ADAPTER";
  apiVersion: string;
  device?: { ip?: string; user_agent?: string };
}

export interface VoiceSession {
  session_id: string;
  customer_id: string;
  platform: Platform;
  platform_user_id: string;
  platform_session_id?: string;
  conversation_id?: string;
  /** The OAuth client and token family the session is bound to (§38 session hijacking). */
  client_id: string;
  authenticated: true;
  stage: VoiceStage;
  stage_history: { stage: VoiceStage; at: string }[];
  funnel: string[];
  active_order_intent_id?: string;
  last_order_id?: string;
  created_at: string;
  expires_at: string;
}

export interface OrderIntentRecord {
  order_intent_id: string;
  customer_id: string;
  session_id: string;
  platform: Platform;
  source: "BASKET" | "USUAL" | "REORDER";
  reorder_of?: string;
  items: PricedLine[];
  delivery_address_id: string;
  delivery_address_summary: string;
  delivery_address_label: string;
  /** A saved payment method id, or WALLET:APPLE_PAY / WALLET:GOOGLE_PAY. */
  payment_method_id: string;
  payment_method_label: string;
  quote_id: string;
  subtotal_cents: Cents;
  fees: Fees;
  discount_cents: Cents;
  promotion_code?: string;
  total_cents: Cents;
  currency: Currency;
  estimated_delivery_minutes: number;
  delivery_requirements: string[];
  status: OrderIntentStatus;
  authorization_status: AuthorizationStatus;
  payment_status: PaymentStatus;
  authorization_id?: string;
  /** The signed purchase authorization, kept so an out-of-band confirmation can be spent by the session's client. */
  authorization_token?: string;
  confirmation?: ConfirmationEvidence;
  payment_authorization_id?: string;
  payment_reference?: string;
  order_id?: string;
  superseded_by?: string;
  /** SHA-256 of the single-use nonce in the hosted confirmation link. */
  confirmation_nonce_hash?: string;
  created_at: string;
  expires_at: string;
}

export interface PaymentAuthorizationRecord {
  payment_authorization_id: string;
  order_intent_id: string;
  customer_id: string;
  payment_reference: string;
  amount_cents: Cents;
  currency: Currency;
  status: "AUTHORIZED" | "CAPTURED" | "VOIDED";
  created_at: string;
}

export interface IdempotencyRecord {
  fingerprint: string;
  state: "IN_PROGRESS" | "DONE";
  response?: unknown;
  error?: { code: string; details?: Record<string, unknown> };
}
