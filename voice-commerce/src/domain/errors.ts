/**
 * The error taxonomy (§31). Every failure anywhere in the system surfaces as a
 * GimmeError with one of these codes, so adapters can branch on a code rather
 * than parse a message.
 */

export const ERROR_CODES = [
  "AUTHENTICATION_REQUIRED",
  "AUTHORIZATION_REQUIRED",
  "INSUFFICIENT_SCOPE",
  "SESSION_INVALID",
  "CUSTOMER_NOT_FOUND",
  "AGE_VERIFICATION_REQUIRED",
  "IDENTITY_VERIFICATION_REQUIRED",
  "ACCOUNT_RESTRICTED",
  "PRODUCT_NOT_FOUND",
  "PRODUCT_AMBIGUOUS",
  "PRODUCT_UNAVAILABLE",
  "INVENTORY_CHANGED",
  "PRICE_CHANGED",
  "DELIVERY_UNAVAILABLE",
  "STORE_CLOSED",
  "OUTSIDE_SERVICE_AREA",
  "ADDRESS_NOT_FOUND",
  "PAYMENT_REQUIRED",
  "PAYMENT_FAILED",
  "PAYMENT_DECLINED",
  "PAYMENT_TIMEOUT",
  "CONFIRMATION_MISMATCH",
  "CONFIRMATION_DECLINED",
  "ORDER_INTENT_NOT_FOUND",
  "ORDER_INTENT_INVALID_STATE",
  "ORDER_EXPIRED",
  "ORDER_ALREADY_CREATED",
  "ORDER_NOT_FOUND",
  "ORDER_CANNOT_BE_CANCELLED",
  "IDEMPOTENCY_CONFLICT",
  "PLATFORM_NOT_SUPPORTED",
  "COMPLIANCE_FAILURE",
  "INVALID_REQUEST",
  "UNKNOWN_ERROR",
] as const;

export type ErrorCode = (typeof ERROR_CODES)[number];

export const SUGGESTED_ACTIONS = [
  "NONE",
  "AUTHENTICATE",
  "LINK_ACCOUNT",
  "START_NEW_SESSION",
  "VERIFY_AGE_IN_APP",
  "VERIFY_IDENTITY_IN_APP",
  "CONTACT_SUPPORT",
  "ASK_CUSTOMER_TO_CLARIFY",
  "SHOW_ALTERNATIVES",
  "RECONFIRM_PRICE",
  "RECONFIRM_ORDER",
  "CHOOSE_ANOTHER_ADDRESS",
  "TRY_LATER",
  "ADD_PAYMENT_METHOD_IN_APP",
  "CHOOSE_ANOTHER_PAYMENT_METHOD",
  "CONFIRM_ON_DEVICE",
  "CREATE_NEW_ORDER_INTENT",
  "CHECK_ORDER_STATUS",
  "RETRY",
] as const;

export type SuggestedAction = (typeof SUGGESTED_ACTIONS)[number];

interface ErrorSpec {
  http: number;
  recoverable: boolean;
  action: SuggestedAction;
  message: string;
}

/** Defaults per code. `message` is customer-safe and short enough to speak. */
export const ERROR_SPECS: Record<ErrorCode, ErrorSpec> = {
  AUTHENTICATION_REQUIRED: { http: 401, recoverable: true, action: "AUTHENTICATE", message: "Please sign in to GIMME to continue." },
  AUTHORIZATION_REQUIRED: { http: 403, recoverable: true, action: "RECONFIRM_ORDER", message: "I need you to confirm this order first." },
  INSUFFICIENT_SCOPE: { http: 403, recoverable: false, action: "LINK_ACCOUNT", message: "This assistant isn't allowed to do that with your GIMME account." },
  SESSION_INVALID: { http: 401, recoverable: true, action: "START_NEW_SESSION", message: "That conversation has expired. Let's start again." },
  CUSTOMER_NOT_FOUND: { http: 404, recoverable: true, action: "LINK_ACCOUNT", message: "I couldn't find a GIMME account linked to this assistant." },
  AGE_VERIFICATION_REQUIRED: { http: 403, recoverable: true, action: "VERIFY_AGE_IN_APP", message: "You'll need to verify your age in the GIMME app before ordering." },
  IDENTITY_VERIFICATION_REQUIRED: { http: 403, recoverable: true, action: "VERIFY_IDENTITY_IN_APP", message: "You'll need to verify your identity in the GIMME app before ordering." },
  ACCOUNT_RESTRICTED: { http: 403, recoverable: false, action: "CONTACT_SUPPORT", message: "Your GIMME account can't place orders right now. Please contact GIMME support." },
  PRODUCT_NOT_FOUND: { http: 404, recoverable: true, action: "ASK_CUSTOMER_TO_CLARIFY", message: "I couldn't find that product at GIMME." },
  PRODUCT_AMBIGUOUS: { http: 409, recoverable: true, action: "ASK_CUSTOMER_TO_CLARIFY", message: "There's more than one match. Which one did you mean?" },
  PRODUCT_UNAVAILABLE: { http: 409, recoverable: true, action: "SHOW_ALTERNATIVES", message: "That product is currently unavailable." },
  INVENTORY_CHANGED: { http: 409, recoverable: true, action: "SHOW_ALTERNATIVES", message: "Stock changed while you were ordering, so I haven't placed it." },
  PRICE_CHANGED: { http: 409, recoverable: true, action: "RECONFIRM_PRICE", message: "The price has changed, so I haven't placed the order." },
  DELIVERY_UNAVAILABLE: { http: 409, recoverable: true, action: "TRY_LATER", message: "GIMME can't deliver right now." },
  STORE_CLOSED: { http: 409, recoverable: true, action: "TRY_LATER", message: "GIMME isn't delivering at the moment." },
  OUTSIDE_SERVICE_AREA: { http: 409, recoverable: true, action: "CHOOSE_ANOTHER_ADDRESS", message: "That address is outside GIMME's delivery area." },
  ADDRESS_NOT_FOUND: { http: 404, recoverable: true, action: "CHOOSE_ANOTHER_ADDRESS", message: "I couldn't find that delivery address on your account." },
  PAYMENT_REQUIRED: { http: 402, recoverable: true, action: "ADD_PAYMENT_METHOD_IN_APP", message: "There's no saved payment method. Add one in the GIMME app." },
  PAYMENT_FAILED: { http: 402, recoverable: true, action: "RETRY", message: "The payment didn't go through. You haven't been charged." },
  PAYMENT_DECLINED: { http: 402, recoverable: true, action: "CHOOSE_ANOTHER_PAYMENT_METHOD", message: "Your payment was declined. You haven't been charged." },
  PAYMENT_TIMEOUT: { http: 504, recoverable: true, action: "CHECK_ORDER_STATUS", message: "The payment is taking longer than expected. I'll check on it before trying again." },
  CONFIRMATION_MISMATCH: { http: 409, recoverable: true, action: "RECONFIRM_ORDER", message: "What was confirmed doesn't match this order, so I haven't placed it." },
  CONFIRMATION_DECLINED: { http: 409, recoverable: true, action: "NONE", message: "No problem, I haven't placed the order." },
  ORDER_INTENT_NOT_FOUND: { http: 404, recoverable: true, action: "CREATE_NEW_ORDER_INTENT", message: "I couldn't find that order. Let's start again." },
  ORDER_INTENT_INVALID_STATE: { http: 409, recoverable: true, action: "CREATE_NEW_ORDER_INTENT", message: "That order can't go any further. Let's start again." },
  ORDER_EXPIRED: { http: 410, recoverable: true, action: "CREATE_NEW_ORDER_INTENT", message: "That order timed out before it was confirmed. Let's start again." },
  ORDER_ALREADY_CREATED: { http: 409, recoverable: false, action: "CHECK_ORDER_STATUS", message: "That order has already been placed." },
  ORDER_NOT_FOUND: { http: 404, recoverable: false, action: "NONE", message: "I couldn't find that GIMME order." },
  ORDER_CANNOT_BE_CANCELLED: { http: 409, recoverable: false, action: "CONTACT_SUPPORT", message: "That order can't be cancelled now." },
  IDEMPOTENCY_CONFLICT: { http: 409, recoverable: false, action: "NONE", message: "That request was already used for a different order." },
  PLATFORM_NOT_SUPPORTED: { http: 400, recoverable: false, action: "NONE", message: "Ordering isn't available on this assistant yet." },
  COMPLIANCE_FAILURE: { http: 403, recoverable: false, action: "CONTACT_SUPPORT", message: "GIMME can't complete this order." },
  INVALID_REQUEST: { http: 400, recoverable: false, action: "NONE", message: "That request wasn't valid." },
  UNKNOWN_ERROR: { http: 500, recoverable: true, action: "RETRY", message: "Something went wrong at GIMME. You haven't been charged." },
};

export interface ErrorBody {
  success: false;
  error_code: ErrorCode;
  message: string;
  recoverable: boolean;
  suggested_action: SuggestedAction;
  details?: Record<string, unknown>;
  request_id?: string;
}

export class GimmeError extends Error {
  readonly code: ErrorCode;
  readonly details?: Record<string, unknown>;
  readonly recoverable: boolean;
  readonly suggestedAction: SuggestedAction;

  constructor(
    code: ErrorCode,
    opts: { message?: string; details?: Record<string, unknown>; suggestedAction?: SuggestedAction; cause?: unknown } = {},
  ) {
    const spec = ERROR_SPECS[code];
    super(opts.message ?? spec.message, opts.cause ? { cause: opts.cause } : undefined);
    this.name = "GimmeError";
    this.code = code;
    this.details = opts.details;
    this.recoverable = spec.recoverable;
    this.suggestedAction = opts.suggestedAction ?? spec.action;
  }

  get httpStatus(): number {
    return ERROR_SPECS[this.code].http;
  }

  toBody(requestId?: string): ErrorBody {
    return {
      success: false,
      error_code: this.code,
      message: this.message,
      recoverable: this.recoverable,
      suggested_action: this.suggestedAction,
      ...(this.details ? { details: this.details } : {}),
      ...(requestId ? { request_id: requestId } : {}),
    };
  }
}

/** Normalise anything thrown into a GimmeError without leaking internals. */
export function toGimmeError(err: unknown): GimmeError {
  if (err instanceof GimmeError) return err;
  return new GimmeError("UNKNOWN_ERROR", { cause: err });
}
