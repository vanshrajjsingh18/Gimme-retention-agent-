// GIMME App Intents — reference implementation for the iOS app.
//
// Drop into the GIMME iOS app target (iOS 17+). It is a thin client of the
// GIMME Voice API's Apple adapter endpoint (POST /v1/voice/adapters/apple/intents):
// no prices, stock or rules live here. What lives here is Apple's part of the
// contract:
//
//  - authenticationPolicy = .requiresAuthentication on every purchasing
//    intent, so Siri requires an unlocked / Face ID-authenticated device
//    before perform() runs;
//  - requestConfirmation(...) with the exact amount the server returned,
//    so the customer's "yes" happens in Apple's own confirmation UI;
//  - the confirmed amount is sent back and checked server-side to the cent.
//
// Not compiled in this repository (no Xcode toolchain here). Wire
// `GIMMEAuth.accessToken()` to the app's existing OAuth session (the app is
// a first-party client and holds gimme.order.confirm).

import AppIntents
import Foundation

// MARK: - Voice API contract (mirrors src/adapters/apple.ts)

struct AppleIntentRequest: Encodable {
    var intent: String
    var apple_user_id: String
    var interaction_id: String
    var session_id: String? = nil
    var parameters: Parameters? = nil
    var confirmation: Confirmation? = nil

    struct Parameters: Encodable {
        var product_query: String? = nil
        var product_sku: String? = nil
        var quantity: Int? = nil
        var unit: String? = nil      // "PACK" | "UNIT"
        var order_id: String? = nil
    }
    struct Confirmation: Encodable {
        var confirmed_total: Decimal
        var device_authenticated: Bool
        var confirmation_id: String? = nil
    }
}

struct AppleIntentResponse: Decodable {
    var session_id: String
    var dialog: Dialog
    var needs_confirmation: NeedsConfirmation?
    var disambiguation: [Option]?
    var open_app: OpenApp?
    var order: Order?
    var error_code: String?

    struct Dialog: Decodable { var full: String; var supporting: String? }
    struct NeedsConfirmation: Decodable {
        var order_intent_id: String
        var amount: Decimal
        var currency: String
        var merchant: String
        var summary: String
        var requires_device_authentication: Bool
    }
    struct Option: Decodable { var sku: String; var title: String; var subtitle: String }
    struct OpenApp: Decodable { var reason: String }
    struct Order: Decodable { var order_id: String; var status: String; var estimated_minutes: Int?; var total: Decimal? }
}

enum GIMMEAuth {
    /// The signed-in customer's OAuth access token from the app's session store (Keychain).
    static func accessToken() async throws -> String { fatalError("wire to the GIMME app's auth session") }
    /// App-scoped stable identifier registered with GIMME at account linking (not the Apple ID).
    static func appleUserID() -> String { fatalError("wire to the GIMME app's linked identifier") }
}

actor GIMMEVoiceClient {
    static let shared = GIMMEVoiceClient()
    private let endpoint = URL(string: "https://voice.gimme.example/v1/voice/adapters/apple/intents")!
    private var sessions: [String: String] = [:] // interaction -> voice session id

    func send(_ intent: String, interaction: String, parameters: AppleIntentRequest.Parameters? = nil,
              confirmation: AppleIntentRequest.Confirmation? = nil) async throws -> AppleIntentResponse {
        var body = AppleIntentRequest(intent: intent, apple_user_id: GIMMEAuth.appleUserID(), interaction_id: interaction)
        body.session_id = sessions[interaction]
        body.parameters = parameters
        body.confirmation = confirmation

        var req = URLRequest(url: endpoint)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.setValue("Bearer \(try await GIMMEAuth.accessToken())", forHTTPHeaderField: "Authorization")
        req.setValue("1.0", forHTTPHeaderField: "GIMME-API-Version")
        req.setValue(interaction, forHTTPHeaderField: "X-Correlation-Id")
        req.httpBody = try JSONEncoder().encode(body)

        let (data, response) = try await URLSession.shared.data(for: req)
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            throw GIMMEIntentError.unavailable
        }
        let decoded = try JSONDecoder().decode(AppleIntentResponse.self, from: data)
        sessions[interaction] = decoded.session_id
        return decoded
    }
}

enum GIMMEIntentError: Error, CustomLocalizedStringResourceConvertible {
    case unavailable
    var localizedStringResource: LocalizedStringResource { "GIMME isn't available right now. You haven't been charged." }
}

// MARK: - Entities (§24)

struct GIMMEProduct: AppEntity {
    static let typeDisplayRepresentation: TypeDisplayRepresentation = "GIMME Product"
    static let defaultQuery = GIMMEProductQuery()
    var id: String           // SKU
    var name: String
    var priceText: String
    var displayRepresentation: DisplayRepresentation { DisplayRepresentation(title: "\(name)", subtitle: "\(priceText)") }
}

struct GIMMEProductQuery: EntityStringQuery {
    func entities(for identifiers: [String]) async throws -> [GIMMEProduct] {
        identifiers.map { GIMMEProduct(id: $0, name: $0, priceText: "") } // resolved server-side by SKU
    }
    func entities(matching string: String) async throws -> [GIMMEProduct] {
        // Free text goes to the server as product_query; it does the matching.
        [GIMMEProduct(id: "query:\(string)", name: string, priceText: "")]
    }
}

struct GIMMEOrder: AppEntity {
    static let typeDisplayRepresentation: TypeDisplayRepresentation = "GIMME Order"
    static let defaultQuery = GIMMEOrderQuery()
    var id: String           // order id
    var status: String
    var displayRepresentation: DisplayRepresentation { DisplayRepresentation(title: "\(id)", subtitle: "\(status)") }
}

struct GIMMEOrderQuery: EntityQuery {
    func entities(for identifiers: [String]) async throws -> [GIMMEOrder] { identifiers.map { GIMMEOrder(id: $0, status: "") } }
    func suggestedEntities() async throws -> [GIMMEOrder] { [] }
}

// MARK: - The confirm-then-place step every ordering intent shares

protocol GIMMEOrderingIntent: AppIntent {}

extension GIMMEOrderingIntent {
    /// Present the server's summary in Siri's confirmation UI; only on "yes" ask the server to place it.
    func confirmAndPlace(_ first: AppleIntentResponse, interaction: String) async throws -> some IntentResult & ProvidesDialog {
        if let open = first.open_app {
            throw needsApp(open.reason, dialog: first.dialog.full)
        }
        guard let ask = first.needs_confirmation else {
            return .result(dialog: IntentDialog(stringLiteral: first.dialog.full))
        }
        // Apple's confirmation UI. Throws if the customer says no or cancels.
        do {
            try await requestConfirmation(
                result: .result(dialog: IntentDialog(stringLiteral: first.dialog.full)),
                confirmationActionName: .order,
                showPrompt: false
            )
        } catch {
            _ = try? await GIMMEVoiceClient.shared.send("DeclineGIMMEOrderIntent", interaction: interaction,
                confirmation: .init(confirmed_total: ask.amount, device_authenticated: true))
            throw error
        }
        // authenticationPolicy guarantees the device owner authenticated before perform() ran.
        let done = try await GIMMEVoiceClient.shared.send("ConfirmGIMMEOrderIntent", interaction: interaction,
            confirmation: .init(confirmed_total: ask.amount, device_authenticated: true))
        if done.needs_confirmation != nil {
            // The price changed between "yes" and checkout: ask again with the new amount.
            return try await confirmAndPlace(done, interaction: interaction)
        }
        if let open = done.open_app { throw needsApp(open.reason, dialog: done.dialog.full) }
        return .result(dialog: IntentDialog(stringLiteral: done.dialog.full))
    }

    private func needsApp(_ reason: String, dialog: String) -> Error {
        // Age/ID verification, adding a card, or on-device confirmation happen in the app, never by voice.
        GIMMEAppHandoff(message: dialog)
    }
}

struct GIMMEAppHandoff: Error, CustomLocalizedStringResourceConvertible {
    let message: String
    var localizedStringResource: LocalizedStringResource { LocalizedStringResource(stringLiteral: message) }
}

// MARK: - Intents (§23)

struct OrderUsualFromGIMMEIntent: GIMMEOrderingIntent {
    static let title: LocalizedStringResource = "Order My Usual"
    static let description = IntentDescription("Order your usual GIMME drinks after confirming the total.")
    static let authenticationPolicy: IntentAuthenticationPolicy = .requiresAuthentication
    static let openAppWhenRun = false

    func perform() async throws -> some IntentResult & ProvidesDialog {
        let interaction = UUID().uuidString
        let first = try await GIMMEVoiceClient.shared.send("OrderUsualFromGIMMEIntent", interaction: interaction)
        return try await confirmAndPlace(first, interaction: interaction)
    }
}

struct OrderFromGIMMEIntent: GIMMEOrderingIntent {
    static let title: LocalizedStringResource = "Order from GIMME"
    static let description = IntentDescription("Order drinks from GIMME, e.g. a dozen Heinekens.")
    static let authenticationPolicy: IntentAuthenticationPolicy = .requiresAuthentication
    static let openAppWhenRun = false

    @Parameter(title: "Product") var product: GIMMEProduct
    @Parameter(title: "Quantity", default: 1) var quantity: Int
    @Parameter(title: "Counting single bottles or cans", default: false) var countsUnits: Bool

    static var parameterSummary: some ParameterSummary {
        Summary("Order \(\.$quantity) \(\.$product) from GIMME")
    }

    func perform() async throws -> some IntentResult & ProvidesDialog {
        let interaction = UUID().uuidString
        let isQuery = product.id.hasPrefix("query:")
        var first = try await GIMMEVoiceClient.shared.send("OrderFromGIMMEIntent", interaction: interaction, parameters: .init(
            product_query: isQuery ? String(product.id.dropFirst(6)) : nil,
            product_sku: isQuery ? nil : product.id,
            quantity: quantity,
            unit: countsUnits ? "UNIT" : "PACK"))
        // "We have three Heineken options… Which one?"
        if let options = first.disambiguation, !options.isEmpty {
            let entities = options.map { GIMMEProduct(id: $0.sku, name: $0.title, priceText: $0.subtitle) }
            let chosen = try await $product.requestDisambiguation(among: entities, dialog: IntentDialog(stringLiteral: first.dialog.full))
            first = try await GIMMEVoiceClient.shared.send("SelectGIMMEProductIntent", interaction: interaction,
                parameters: .init(product_sku: chosen.id))
        }
        return try await confirmAndPlace(first, interaction: interaction)
    }
}

struct ReorderFromGIMMEIntent: GIMMEOrderingIntent {
    static let title: LocalizedStringResource = "Reorder Last GIMME Order"
    static let authenticationPolicy: IntentAuthenticationPolicy = .requiresAuthentication
    static let openAppWhenRun = false

    func perform() async throws -> some IntentResult & ProvidesDialog {
        let interaction = UUID().uuidString
        let first = try await GIMMEVoiceClient.shared.send("ReorderFromGIMMEIntent", interaction: interaction)
        return try await confirmAndPlace(first, interaction: interaction)
    }
}

struct GetGIMMEOrderStatusIntent: AppIntent {
    static let title: LocalizedStringResource = "Where's My GIMME Order"
    static let openAppWhenRun = false
    // Read-only, but personal (what you ordered, when it arrives): require device authentication.
    static let authenticationPolicy: IntentAuthenticationPolicy = .requiresLocalDeviceAuthentication

    func perform() async throws -> some IntentResult & ProvidesDialog {
        let r = try await GIMMEVoiceClient.shared.send("GetGIMMEOrderStatusIntent", interaction: UUID().uuidString)
        return .result(dialog: IntentDialog(stringLiteral: r.dialog.full))
    }
}

struct CancelGIMMEOrderIntent: AppIntent {
    static let title: LocalizedStringResource = "Cancel GIMME Order"
    static let authenticationPolicy: IntentAuthenticationPolicy = .requiresAuthentication
    static let openAppWhenRun = false

    func perform() async throws -> some IntentResult & ProvidesDialog {
        try await requestConfirmation(result: .result(dialog: "Cancel your latest GIMME order?"), confirmationActionName: .go)
        let r = try await GIMMEVoiceClient.shared.send("CancelGIMMEOrderIntent", interaction: UUID().uuidString)
        return .result(dialog: IntentDialog(stringLiteral: r.dialog.full))
    }
}

struct GetGIMMEReceiptIntent: AppIntent {
    static let title: LocalizedStringResource = "GIMME Order Total"
    static let authenticationPolicy: IntentAuthenticationPolicy = .requiresLocalDeviceAuthentication
    static let openAppWhenRun = false

    func perform() async throws -> some IntentResult & ProvidesDialog {
        let r = try await GIMMEVoiceClient.shared.send("GetGIMMEReceiptIntent", interaction: UUID().uuidString)
        return .result(dialog: IntentDialog(stringLiteral: r.dialog.full))
    }
}

// MARK: - Siri phrases

struct GIMMEShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(intent: OrderUsualFromGIMMEIntent(),
                    phrases: ["Order my usual from \(.applicationName)", "Get my usual \(.applicationName) order", "\(.applicationName) my usual"],
                    shortTitle: "Order My Usual", systemImageName: "cart")
        AppShortcut(intent: OrderFromGIMMEIntent(),
                    phrases: ["Order \(\.$product) from \(.applicationName)", "Get \(\.$product) from \(.applicationName)"],
                    shortTitle: "Order Drinks", systemImageName: "takeoutbag.and.cup.and.straw")
        AppShortcut(intent: ReorderFromGIMMEIntent(),
                    phrases: ["Reorder my last \(.applicationName) order"],
                    shortTitle: "Reorder", systemImageName: "arrow.clockwise")
        AppShortcut(intent: GetGIMMEOrderStatusIntent(),
                    phrases: ["Where's my \(.applicationName) order", "Track my \(.applicationName) order"],
                    shortTitle: "Track Order", systemImageName: "box.truck")
        AppShortcut(intent: GetGIMMEReceiptIntent(),
                    phrases: ["What's the total on my \(.applicationName) order"],
                    shortTitle: "Order Total", systemImageName: "receipt")
    }
}
