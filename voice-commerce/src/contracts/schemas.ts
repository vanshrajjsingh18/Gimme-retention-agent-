/**
 * Contract schemas — the single source of truth for every input and output
 * shape. The MCP tool definitions, the REST Voice API validation, the
 * generated OpenAPI document and the generated MCP tool specification all
 * come from these. Change a contract here and every surface changes with it.
 *
 * Amounts are decimal NZD with two places at this edge; internally they are
 * integer cents.
 */

import { z } from "zod";
import { ERROR_CODES, SUGGESTED_ACTIONS } from "../domain/errors.js";
import { ORDER_INTENT_STATUSES, VOICE_STAGES } from "../domain/state-machine.js";
import { CUSTOMER_ELIGIBILITY, ORDER_STATUSES, PLATFORMS } from "../domain/types.js";

// ------------------------------------------------------------------ shared

const id = (description: string) => z.string().min(1).max(128).regex(/^[A-Za-z0-9_.:\-]+$/).describe(description);
export const SessionId = id("Voice session id returned by identify_customer / POST /v1/voice/sessions.");
export const Sku = id("GIMME product SKU.");
export const Amount = z.number().describe("Decimal amount in the given currency, two decimal places.");
export const Currency = z.literal("NZD");
export const Platform = z.enum(PLATFORMS);

export const CartLineInput = z.object({
  sku: Sku,
  quantity: z.number().int().min(1).max(48).describe("Number of packs/items (not individual units)."),
});
export const CartLinesInput = z.array(CartLineInput).min(1).max(20);

export const LineView = z.object({
  sku: Sku,
  name: z.string(),
  quantity: z.number().int(),
  unit_price: Amount,
  line_total: Amount,
  alcoholic: z.boolean(),
});

export const FeesView = z.object({ delivery_fee: Amount, service_fee: Amount, packaging_fee: Amount });

export const AddressView = z.object({ id: z.string(), label: z.string(), summary: z.string(), is_default: z.boolean() });
export const PaymentMethodView = z.object({
  id: z.string(),
  label: z.string().describe('Display label only, e.g. "Visa ending 4242". Never a card number.'),
  kind: z.enum(["CARD_TOKEN", "APPLE_PAY", "GOOGLE_PAY", "ACCOUNT_CREDIT"]),
  is_default: z.boolean(),
});

export const ErrorBody = z.object({
  success: z.literal(false),
  error_code: z.enum(ERROR_CODES),
  message: z.string().describe("Customer-safe, short enough to speak."),
  recoverable: z.boolean(),
  suggested_action: z.enum(SUGGESTED_ACTIONS),
  details: z.record(z.string(), z.unknown()).optional(),
  request_id: z.string().optional(),
});

export const ConfirmationRequest = z
  .object({
    requires_user_confirmation: z.literal(true),
    confirmation_type: z.literal("PURCHASE"),
    merchant: z.literal("GIMME"),
    amount: Amount,
    currency: Currency,
    summary: z.string().describe("One-line spoken/visual summary of exactly what will be charged."),
    requires_device_authentication: z.boolean(),
    expires_at: z.string(),
  })
  .describe("Hand to the platform's native purchase confirmation UI (§25).");

export const OrderIntentView = z.object({
  order_intent_id: z.string(),
  session_id: z.string(),
  status: z.enum(ORDER_INTENT_STATUSES),
  source: z.enum(["BASKET", "USUAL", "REORDER"]),
  items: z.array(LineView),
  delivery_address: z.object({ id: z.string(), label: z.string(), summary: z.string() }),
  payment_method: z.object({ id: z.string(), label: z.string() }),
  subtotal: Amount,
  fees: FeesView,
  discount: Amount,
  promotion_code: z.string().optional(),
  total: Amount,
  currency: Currency,
  estimated_delivery_minutes: z.number().int(),
  delivery_requirements: z.array(z.string()),
  authorization_status: z.enum(["PENDING", "AUTHORIZED", "DECLINED"]),
  payment_status: z.enum(["PENDING", "AUTHORIZED", "CAPTURED", "VOIDED", "FAILED"]),
  order_id: z.string().optional(),
  created_at: z.string(),
  expires_at: z.string(),
});

// --------------------------------------------------------------- handshake

export const HandshakeInfo = z.object({
  protocol_version: z.string(),
  mcp_version: z.string(),
  voice_api_version: z.string(),
  capability_version: z.string(),
  platform: Platform,
  session_id: z.string(),
  customer_id: z.string(),
  client_id: z.string(),
  scopes: z.array(z.string()),
  request_id: z.string(),
  conversation_id: z.string().optional(),
});

// -------------------------------------------------------- identify_customer

export const IdentifyCustomerInput = z.object({
  platform: Platform,
  platform_user_id: z.string().min(1).max(256).describe("The assistant platform's stable id for this user (must be linked to the GIMME account)."),
  platform_session_id: z.string().max(256).optional(),
  conversation_id: z.string().max(256).optional().describe("Platform conversation id; part of the idempotency key for mutations."),
});
export const IdentifyCustomerOutput = z.object({
  session_id: z.string(),
  customer_id: z.string(),
  authenticated: z.boolean(),
  customer_name: z.string().describe("First name only."),
  default_address: z.object({ id: z.string(), summary: z.string() }).nullable(),
  eligible_to_order: z.boolean(),
  eligibility: z.enum(CUSTOMER_ELIGIBILITY),
  expires_at: z.string(),
  handshake: HandshakeInfo,
});

// ----------------------------------------------------------- search_products

export const SearchProductsInput = z.object({
  session_id: SessionId,
  query: z.string().min(1).max(200).describe('Natural-language product query, e.g. "Heineken" or "German beers".'),
  limit: z.number().int().min(1).max(20).default(5),
});
export const ProductView = z.object({
  sku: z.string(),
  name: z.string(),
  brand: z.string(),
  category: z.string(),
  country: z.string().optional(),
  pack_size: z.number().int(),
  alcoholic: z.boolean(),
  price: Amount,
  currency: Currency,
  available: z.boolean().describe("In stock for delivery to the session's default address right now."),
});
export const SearchProductsOutput = z.object({ products: z.array(ProductView) });

// ------------------------------------------------------------ resolve_items

export const ItemRequest = z.object({
  query: z.string().min(1).max(200).optional(),
  sku: Sku.optional(),
  quantity: z.number().int().min(1).max(96).optional(),
  unit: z.enum(["PACK", "UNIT"]).optional().describe('UNIT when the customer counted bottles/cans ("a dozen Heinekens" = 12 UNIT).'),
});
export const ResolveItemsInput = z.object({
  session_id: SessionId,
  request: z.discriminatedUnion("type", [
    z.object({ type: z.literal("USUAL") }),
    z.object({ type: z.literal("LAST_ORDER") }),
    z.object({ type: z.literal("ITEMS"), items: z.array(ItemRequest).min(1).max(10) }),
  ]),
});
export const ResolveItemsOutput = z.object({
  status: z.enum(["RESOLVED", "NEEDS_CLARIFICATION", "NOT_FOUND"]),
  items: z.array(z.object({ sku: z.string(), quantity: z.number().int(), name: z.string() })),
  clarification: z
    .object({
      query: z.string(),
      question: z.string(),
      options: z.array(z.object({ sku: z.string(), name: z.string(), pack_size: z.number().int(), price: Amount })),
    })
    .optional(),
  not_found: z.array(z.string()).optional(),
  speech: z.string().optional(),
});

// -------------------------------------------------- get_customer_preferences

export const GetPreferencesInput = z.object({ session_id: SessionId });
export const GetPreferencesOutput = z.object({
  usual_order: z.array(z.object({ sku: z.string(), quantity: z.number().int(), name: z.string() })),
  recent_orders: z.array(z.object({ order_id: z.string(), placed_at: z.string(), total: Amount, item_count: z.number().int() })),
  addresses: z.array(AddressView),
  payment_methods: z.array(PaymentMethodView),
  preferred_address_id: z.string().nullable(),
  preferred_payment_method_id: z.string().nullable(),
});

// ----------------------------------------------------------- check_inventory

export const CheckInventoryInput = z.object({
  session_id: SessionId,
  items: CartLinesInput,
  address_id: id("Saved address id; defaults to the customer's default address.").optional(),
});
export const AlternativeView = z.object({
  for_sku: z.string(),
  sku: z.string(),
  name: z.string(),
  price: Amount,
  requires_customer_approval: z.literal(true).describe("Never substitute without the customer explicitly choosing this."),
});
export const CheckInventoryOutput = z.object({
  available: z.boolean(),
  reason: z.literal("OUT_OF_STOCK").optional(),
  items: z.array(z.object({ sku: z.string(), requested: z.number().int(), available: z.boolean() })),
  alternatives: z.array(AlternativeView),
});

// ---------------------------------------------------------- validate_delivery

export const ValidateDeliveryInput = z.object({
  session_id: SessionId,
  address_id: id("Saved address id; defaults to the customer's default address.").optional(),
  items: CartLinesInput.optional().describe("Basket, so alcohol-delivery rules can be applied."),
});
export const ValidateDeliveryOutput = z.object({
  eligible: z.boolean(),
  reason: z.enum(["OUTSIDE_SERVICE_AREA", "STORE_CLOSED", "DELIVERY_UNAVAILABLE"]).optional(),
  address_id: z.string(),
  estimated_delivery_minutes: z.number().int().optional(),
  delivery_window: z.string().optional(),
  requirements: z.array(z.string()),
});

// ---------------------------------------------------------- validate_customer

export const ValidateCustomerInput = z.object({ session_id: SessionId, items: CartLinesInput.optional() });
export const ValidateCustomerOutput = z.object({
  status: z.enum(CUSTOMER_ELIGIBILITY),
  can_order: z.boolean().describe("Whether this customer may place this basket (or, with no basket, any alcohol order)."),
  restrictions: z.array(z.string()),
});

// ------------------------------------------------------------- calculate_cart

export const CalculateCartInput = z.object({
  session_id: SessionId,
  items: CartLinesInput,
  address_id: id("Saved address id; defaults to the customer's default address.").optional(),
  promotion_code: z.string().max(40).regex(/^[A-Za-z0-9_-]+$/).optional(),
});
export const CalculateCartOutput = z.object({
  quote_id: z.string(),
  items: z.array(LineView),
  subtotal: Amount,
  delivery_fee: Amount,
  service_fee: Amount,
  packaging_fee: Amount,
  discount: Amount,
  promotion_code: z.string().optional(),
  total: Amount,
  currency: Currency,
  estimated_delivery_minutes: z.number().int(),
  note: z.string().describe("A quote is informational. Only an order intent can be confirmed."),
});

// -------------------------------------------------------- create_order_intent

export const CreateOrderIntentInput = z.object({
  session_id: SessionId,
  items: CartLinesInput,
  address_id: id("Saved address id; defaults to the customer's default address.").optional(),
  payment_method_id: id("Saved payment method id; defaults to the customer's default.").optional(),
  wallet: z.enum(["APPLE_PAY", "GOOGLE_PAY"]).optional().describe("Pay with a platform wallet token at authorization time instead of a saved method."),
  promotion_code: z.string().max(40).regex(/^[A-Za-z0-9_-]+$/).optional(),
  source: z.enum(["BASKET", "USUAL", "REORDER"]).default("BASKET"),
  idempotency_key: z.string().min(8).max(200).optional(),
});
export const CreateOrderIntentOutput = z.object({
  order_intent: OrderIntentView,
  confirmation: ConfirmationRequest,
  speech: z.string(),
});

// --------------------------------------------------------- order intent read

export const GetOrderIntentInput = z.object({ order_intent_id: id("Order intent id.") });
export const GetOrderIntentOutput = z.object({ order_intent: OrderIntentView });

// ------------------------------------------------------- request_authorization

export const RequestAuthorizationInput = z.object({ order_intent_id: id("Order intent id awaiting confirmation.") });
export const RequestAuthorizationOutput = z.object({
  status: z.enum(["AUTHORIZED", "DECLINED", "PENDING_CUSTOMER_CONFIRMATION"]),
  order_intent_id: z.string(),
  authorization_id: z.string().optional(),
  authorization_token: z.string().optional().describe("Single-use, short-lived, bound to this exact order. Pass to authorize_payment."),
  authorization_expires_at: z.string().optional(),
  confirmation_url: z.string().optional().describe("When PENDING: where the customer confirms (GIMME-hosted)."),
  confirmation: ConfirmationRequest,
  speech: z.string(),
});

// Confirmation recorded by a first-party confirmation channel (REST only).
export const ConfirmOrderIntentInput = z.object({
  decision: z.enum(["CONFIRM", "DECLINE"]),
  confirmed_total: Amount.describe("The total the customer was shown and agreed to. Must equal the intent total exactly."),
  confirmed_currency: Currency,
  evidence: z.object({
    method: z.string().min(1).max(64).describe("e.g. APPLE_APP_INTENT_CONFIRMATION, ANDROID_APP_CONFIRMATION"),
    device_authenticated: z.boolean().describe("The platform authenticated the device owner (unlock / biometric) for this confirmation."),
    platform_confirmation_id: z.string().max(256).optional(),
  }),
});

// ------------------------------------------------------------ authorize_payment

export const AuthorizePaymentInput = z.object({
  order_intent_id: id("Confirmed order intent id."),
  authorization_token: z
    .string()
    .min(20)
    .max(4096)
    .optional()
    .describe(
      "From request_authorization / confirm. Omit only when the customer confirmed out-of-band (hosted confirmation page); the server then uses the authorization it recorded for this intent.",
    ),
  wallet_token: z
    .string()
    .min(4)
    .max(8192)
    .optional()
    .describe("Single-use Apple Pay / Google Pay payment token, when the intent was created with a wallet. Never a card number."),
});
export const AuthorizePaymentOutput = z.object({
  authorized: z.literal(true),
  payment_authorization_id: z.string(),
  payment_reference: z.string(),
  amount: Amount,
  currency: Currency,
});

// ------------------------------------------------------------------ place_order

export const PlaceOrderInput = z.object({
  order_intent_id: id("Order intent with an authorized payment."),
  payment_authorization_id: id("From authorize_payment."),
  idempotency_key: z
    .string()
    .min(8)
    .max(200)
    .describe("Stable across retries. Recommended: <platform>:<customer_id>:<conversation_id>:<order_intent_id>."),
});
export const PlaceOrderOutput = z.object({
  success: z.literal(true),
  order_id: z.string(),
  status: z.enum(ORDER_STATUSES),
  total: Amount,
  currency: Currency,
  estimated_delivery_minutes: z.number().int(),
  idempotent_replay: z.boolean(),
  speech: z.string(),
});

// ------------------------------------------------------------ get_order_status

export const GetOrderStatusInput = z.object({
  session_id: SessionId.optional(),
  order_id: id("GIMME order id; defaults to the customer's most recent order.").optional(),
});
export const GetOrderStatusOutput = z.object({
  order_id: z.string(),
  status: z.enum(ORDER_STATUSES),
  estimated_minutes: z.number().int().nullable(),
  placed_at: z.string(),
  speech: z.string(),
});

// --------------------------------------------------------------- cancel_order

export const CancelOrderInput = z.object({
  order_id: id("GIMME order id."),
  reason: z.string().max(200).optional(),
});
export const CancelOrderOutput = z.object({
  success: z.boolean(),
  order_id: z.string(),
  status: z.enum(ORDER_STATUSES),
  reason: z.string().optional(),
  speech: z.string(),
});

// ---------------------------------------------------- reorder_previous_order

export const ReorderInput = z.object({
  session_id: SessionId,
  order_id: id("Order to repeat; defaults to the most recent.").optional(),
  address_id: id("Deliver somewhere other than the default address.").optional(),
  idempotency_key: z.string().min(8).max(200).optional(),
});
export const ReorderOutput = CreateOrderIntentOutput.extend({
  previous_order: z.object({ order_id: z.string(), total: Amount }),
  total_changed: z.boolean().describe("The new total differs from what the previous order cost."),
});

// --------------------------------------------------------- get_order_receipt

export const GetReceiptInput = z.object({ order_id: id("GIMME order id; defaults to the most recent.").optional() });
export const GetReceiptOutput = z.object({
  order_id: z.string(),
  placed_at: z.string(),
  status: z.enum(ORDER_STATUSES),
  items: z.array(LineView),
  subtotal: Amount,
  fees: FeesView,
  discount: Amount,
  total: Amount,
  currency: Currency,
  payment: z.string().describe('Payment label, e.g. "Visa ending 4242".'),
  delivered_to: z.string().describe("Address label and suburb only."),
  speech: z.string(),
});

// --------------------------------------------------------------- session view

export const SessionView = z.object({
  session_id: z.string(),
  customer_id: z.string(),
  platform: Platform,
  platform_session_id: z.string().optional(),
  authenticated: z.boolean(),
  stage: z.enum(VOICE_STAGES),
  created_at: z.string(),
  expires_at: z.string(),
});

export type IdentifyCustomerOutputT = z.infer<typeof IdentifyCustomerOutput>;
export type OrderIntentViewT = z.infer<typeof OrderIntentView>;
export type ConfirmationRequestT = z.infer<typeof ConfirmationRequest>;
