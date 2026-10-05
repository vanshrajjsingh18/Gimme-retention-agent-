# Apple integration contract (Siri, Apple Intelligence, Shortcuts)

## Shape

```
"Hey Siri, order my usual from GIMME"
        │  App Shortcut phrase → OrderUsualFromGIMMEIntent (in the GIMME iOS app)
        │  authenticationPolicy = .requiresAuthentication  (device unlocked / Face ID)
        ▼
POST /v1/voice/adapters/apple/intents   Bearer <app's OAuth token: first-party, has gimme.order.confirm>
        │  src/adapters/apple.ts → VoiceDialog → VoiceCommerceService
        ▼
{ dialog.full: "Your usual GIMME order is … It's $111.94 delivered to home. Want me to place it?",
  needs_confirmation: { order_intent_id, amount: 111.94, currency: "NZD", merchant: "GIMME", summary } }
        │  App Intent: requestConfirmation(result: dialog, confirmationActionName: .order)   ← Apple's UI
        ▼  customer says "Yes"
POST …/apple/intents  { intent: "ConfirmGIMMEOrderIntent", confirmation: { confirmed_total: 111.94, device_authenticated: true } }
        │  server: total == intent total to the cent; device authenticated; sign authorization;
        │          authorize payment; final validation; create order; capture
        ▼
{ dialog.full: "Done. Your GIMME order is confirmed and should arrive in about 45 minutes.", order: {…} }
```

The reference implementation is [`adapters/apple/GIMMEIntents.swift`](../adapters/apple/GIMMEIntents.swift). It contains App Intents, App Entities, an `AppShortcutsProvider` and the Voice API client. It has no business logic. It was written against the iOS 17 App Intents API but **has not been compiled in this repository** (no Xcode toolchain here), so build and verify it in the iOS project.

## App Intent → Voice API mapping (§23)

| App Intent | Siri phrase (examples) | `intent` sent | Dialog intent | Authentication policy |
| --- | --- | --- | --- | --- |
| `OrderUsualFromGIMMEIntent` | "Order my usual from GIMME" | `OrderUsualFromGIMMEIntent` | `ORDER_USUAL` | `.requiresAuthentication` |
| `OrderFromGIMMEIntent` (product, quantity, units) | "Order a dozen Heinekens from GIMME" | `OrderFromGIMMEIntent` | `ORDER_ITEMS` | `.requiresAuthentication` |
| (disambiguation inside the above) | "The twelve-pack" | `SelectGIMMEProductIntent` | `SELECT_OPTION` | — |
| `ReorderFromGIMMEIntent` | "Reorder my last GIMME order" | `ReorderFromGIMMEIntent` | `REORDER_LAST` | `.requiresAuthentication` |
| (after `requestConfirmation`) | "Yes" | `ConfirmGIMMEOrderIntent` | `CONFIRM` | — |
| (`requestConfirmation` cancelled) | "No" | `DeclineGIMMEOrderIntent` | `DECLINE` | — |
| `GetGIMMEOrderStatusIntent` | "Where's my GIMME order?" | `GetGIMMEOrderStatusIntent` | `ORDER_STATUS` | `.requiresLocalDeviceAuthentication` |
| `CancelGIMMEOrderIntent` | "Cancel my GIMME order" | `CancelGIMMEOrderIntent` | `CANCEL_ORDER` | `.requiresAuthentication` |
| `GetGIMMEReceiptIntent` | "What's the total on my GIMME order?" | `GetGIMMEReceiptIntent` | `RECEIPT` | `.requiresLocalDeviceAuthentication` |

`SearchGIMMEProductsIntent` (§23) is served by `GIMMEProduct`'s `EntityStringQuery`. Siri resolves "Heineken" to a product entity, and the server does the matching.

## Entities (§24)

| App Entity | Identifier | Source |
| --- | --- | --- |
| `GIMMEProduct` | SKU (or `query:<text>` for unresolved speech) | `POST /v1/voice/products/search`, `resolve` |
| `GIMMEOrder` | order id | `GET /v1/voice/orders/{id}` |
| `GIMMEOrderIntent` | order intent id | transient; it lives only inside one interaction |
| `GIMMEAddress` | address id | `GET /v1/voice/preferences`; labels only |
| `GIMMEStore` | store id | from the delivery check, for display only |

Expose `GIMMEProduct` and `GIMMEOrder` through Apple Intelligence entity schemas once the app adopts them. That lets "Get me another one of those beers" resolve to a product already in context. The server already accepts a SKU wherever it accepts a query.

## Confirmation and authentication (§25, §36)

- Every purchasing intent declares `authenticationPolicy = .requiresAuthentication`. Siri won't run it on a locked device without unlock or Face ID. The app reports this as `device_authenticated: true`.
- The confirmation is Apple's own `requestConfirmation(result:confirmationActionName: .order)`, showing the server's `dialog.full`, with the amount from `needs_confirmation.amount`. GIMME never asks for a spoken password or PIN.
- The server returns this to the adapter as:

  ```json
  { "requires_user_confirmation": true, "confirmation_type": "PURCHASE", "amount": 96.50, "currency": "NZD", "merchant": "GIMME", "requires_device_authentication": true }
  ```

  The App Intent turns it into the native confirmation.
- If confirmation arrives without device authentication (CarPlay, a locked phone), the response is "I need you to confirm this purchase on your device." with `open_app.reason = CONFIRM_ON_DEVICE`. Nothing is charged and nothing is weakened.
- **CarPlay.** The App Intents are voice-only (`openAppWhenRun = false`) and never need the screen to complete. When Siri requires an unlock it defers to the phone, and the server's behaviour stays the same.

## Error handling in Siri

| `error_code` / `open_app.reason` | What Siri says / does |
| --- | --- |
| `VERIFY_AGE_IN_APP` | "You'll need to verify your age in the GIMME app before ordering." and opens the app |
| `ADD_PAYMENT_METHOD_IN_APP` | "There's no saved payment method. Add one in the GIMME app." |
| `LINK_ACCOUNT` | "This assistant isn't linked to that GIMME account. Link it in the GIMME app." |
| `PRODUCT_UNAVAILABLE` | Names the product and offers up to two alternatives through disambiguation. Never substitutes. |
| `PRICE_CHANGED` | "The price has changed to $X. Do you want me to continue?" and runs `requestConfirmation` again at the new amount |
| `STORE_CLOSED` / `OUTSIDE_SERVICE_AREA` | Speaks the message and ends |

## Example conversations (§18)

These were generated by `npm run demo` against the sandbox. Every GIMME line is real system output.

```
Customer: Hey Siri, order my usual from GIMME.
Siri:     Your usual GIMME order is two Heineken 12-packs and one Cloudy Bay Sauvignon Blanc. It's $111.94 delivered to home. Want me to place it?
          [Siri confirmation sheet: two Heineken 12-packs and one Cloudy Bay Sauvignon Blanc — $111.94 NZD, delivered to Home, paid with Visa ending 4242]
Customer: Yes.   [Face ID passes]
Siri:     Done. Your GIMME order is confirmed and should arrive in about 45 minutes.

Customer: Hey Siri, where's my GIMME order?
Siri:     Your GIMME order is on its way and should arrive in about 45 minutes. Have your ID ready.

Customer: Hey Siri, order me a dozen Heinekens from GIMME.
Siri:     That's one Heineken 12-pack. It's $43.96 delivered to home. Want me to place it?
Customer: Yes.   [phone is locked in a pocket]
Siri:     I need you to confirm this purchase on your device.

Customer: Hey Siri, order my usual from GIMME.          (customer awaiting age verification)
Siri:     You'll need to verify your age in the GIMME app before ordering.
          [opens GIMME app: VERIFY_AGE_IN_APP]
```
