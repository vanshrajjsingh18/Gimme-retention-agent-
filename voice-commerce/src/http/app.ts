/**
 * HTTP surface:
 *
 *   /v1/voice/...                     REST Voice API (§27) — the adapters' handshake point
 *   /v1/voice/adapters/apple/intents  Apple App Intents adapter
 *   /v1/voice/adapters/google/fulfillment  Google adapter
 *   /v1/voice/confirm/:id             GIMME-hosted purchase confirmation page
 *   /v1/internal/order-events         OMS -> voice status events (HMAC-signed)
 *   /mcp                              MCP over Streamable HTTP (OAuth bearer)
 *   /.well-known/oauth-protected-resource[/mcp]   RFC 9728 metadata for MCP clients
 *   /health, /metrics
 */

import { randomUUID } from "node:crypto";
import express, { type NextFunction, type Request, type Response } from "express";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import type { AuthInfo } from "@modelcontextprotocol/sdk/server/auth/types.js";
import { isInitializeRequest } from "@modelcontextprotocol/sdk/types.js";
import type { ZodType, z } from "zod";
import type { VoiceCommerceRuntime } from "../app.js";
import * as C from "../contracts/schemas.js";
import type { CallContext } from "../core/models.js";
import { GimmeError, toGimmeError } from "../domain/errors.js";
import { ORDER_STATUSES, type Order } from "../domain/types.js";
import { verifyWebhookSignature } from "../events/events.js";
import { createMcpServer } from "../mcp/server.js";
import type { Principal } from "../security/access-tokens.js";
import { ALL_SCOPES } from "../security/scopes.js";
import { VoiceDialog, DIALOG_INTENTS, type DialogTurn } from "../adapters/dialog.js";
import { fromApple, toApple, type AppleIntentRequest } from "../adapters/apple.js";
import { fromGoogle, toGoogle, type GoogleFulfillmentRequest } from "../adapters/google.js";
import { confirmationPage } from "./confirm-page.js";

declare module "express-serve-static-core" {
  interface Request {
    principal?: Principal;
    auth?: AuthInfo;
    requestId: string;
    correlationId: string;
    rawBody?: string;
  }
}

const SUPPORTED_API_VERSIONS = ["1.0"];

export function createHttpApp(rt: VoiceCommerceRuntime): express.Express {
  const app = express();
  app.disable("x-powered-by");
  app.set("trust proxy", true);
  app.use(
    express.json({
      limit: "64kb",
      verify: (req, _res, buf) => {
        (req as Request).rawBody = buf.toString("utf8");
      },
    }),
  );
  app.use(express.urlencoded({ extended: false, limit: "8kb" }));

  // Request + correlation ids on every request and response (§40).
  app.use((req, res, next) => {
    req.requestId = header(req, "x-request-id") ?? `req_${randomUUID()}`;
    req.correlationId = header(req, "x-correlation-id") ?? req.requestId;
    res.setHeader("x-request-id", req.requestId);
    res.setHeader("x-correlation-id", req.correlationId);
    const start = performance.now();
    res.on("finish", () => {
      const route = (req.route?.path as string | undefined) ?? req.path.split("/").slice(0, 4).join("/");
      rt.metrics.observe("gimme_http_latency_ms", performance.now() - start, { method: req.method, route, status: String(res.statusCode) });
      rt.log.info("http", { method: req.method, route, status: res.statusCode, request_id: req.requestId, correlation_id: req.correlationId, ms: Math.round(performance.now() - start) });
    });
    next();
  });

  // ----------------------------------------------------------- public
  app.get("/health", (_req, res) => {
    res.json({ status: "ok", env: rt.config.env, versions: rt.config.versions });
  });
  app.get("/metrics", (_req, res) => {
    res.type("text/plain; version=0.0.4").send(rt.metrics.render());
  });

  const resourceMetadata = (resource: string) => ({
    resource,
    authorization_servers: [rt.config.auth.authorizationServerUrl],
    scopes_supported: ALL_SCOPES,
    bearer_methods_supported: ["header"],
    resource_name: "GIMME Voice Commerce",
    resource_documentation: `${rt.config.publicBaseUrl}/docs`,
  });
  app.get("/.well-known/oauth-protected-resource", (_req, res) => {
    res.json(resourceMetadata(rt.config.publicBaseUrl));
  });
  app.get("/.well-known/oauth-protected-resource/mcp", (_req, res) => {
    res.json(resourceMetadata(`${rt.config.publicBaseUrl}/mcp`));
  });

  // Hosted confirmation page. The single-use nonce in the link proves which intent; in production
  // this route sits behind GIMME web sign-in so the person confirming is the account holder.
  app.get("/v1/voice/confirm/:intentId", async (req, res) => {
    try {
      const d = await rt.service.describeIntentForConfirmation(String(req.params.intentId), String(req.query.nonce ?? ""));
      res.type("html").send(confirmationPage({ ...d, intentId: String(req.params.intentId), nonce: String(req.query.nonce) }));
    } catch {
      res.status(404).type("html").send(confirmationPage({ notFound: true }));
    }
  });
  app.post("/v1/voice/confirm/:intentId", async (req, res) => {
    const nonce = String(req.body?.nonce ?? "");
    const decision = req.body?.decision === "CONFIRM" ? "CONFIRM" : "DECLINE";
    try {
      const d = await rt.service.describeIntentForConfirmation(String(req.params.intentId), nonce);
      const ctx: CallContext = {
        principal: { customerId: d.customer_id, clientId: "gimme-hosted-confirmation", scopes: [], tokenId: "hosted", expiresAt: 0 },
        requestId: req.requestId,
        correlationId: req.correlationId,
        channel: "VOICE_API",
        apiVersion: "1.0",
        device: deviceOf(req),
      };
      await rt.service.confirmOrderIntent(
        ctx,
        String(req.params.intentId),
        { decision, confirmed_total: d.total, confirmed_currency: "NZD", evidence: { method: "HOSTED_CONFIRMATION_PAGE", device_authenticated: false } },
        { kind: "HOSTED_PAGE", nonce },
      );
      res.type("html").send(confirmationPage({ done: decision }));
    } catch (err) {
      res.status(toGimmeError(err).httpStatus).type("html").send(confirmationPage({ error: toGimmeError(err).message }));
    }
  });

  // OMS -> voice: order status changes, HMAC-signed with the internal secret.
  app.post("/v1/internal/order-events", (req, res) => {
    const sig = header(req, "gimme-signature") ?? "";
    if (!req.rawBody || !verifyWebhookSignature(rt.config.internalEventsSecret, req.rawBody, sig)) {
      res.status(401).json(new GimmeError("AUTHENTICATION_REQUIRED").toBody(req.requestId));
      return;
    }
    const body = req.body as { order?: Order; previous_status?: Order["status"] | null };
    if (!body.order || !ORDER_STATUSES.includes(body.order.status)) {
      res.status(400).json(new GimmeError("INVALID_REQUEST").toBody(req.requestId));
      return;
    }
    rt.service.publishOrderStatus(body.order, body.previous_status ?? null);
    res.status(202).json({ accepted: true });
  });

  // ----------------------------------------------------- authenticated
  const auth = bearer(rt);
  const v1 = express.Router();
  v1.use(auth);
  v1.use((req, res, next) => {
    const v = header(req, "gimme-api-version");
    if (v && !SUPPORTED_API_VERSIONS.includes(v)) {
      res.status(400).json(new GimmeError("INVALID_REQUEST", { message: `Unsupported GIMME-API-Version ${v}`, details: { supported: SUPPORTED_API_VERSIONS } }).toBody(req.requestId));
      return;
    }
    res.setHeader("gimme-api-version", "1.0");
    next();
  });

  const svc = rt.service;
  const dialog = new VoiceDialog(rt);

  route(v1, "post", "/sessions", C.IdentifyCustomerInput, (ctx, b) => svc.identifyCustomer(ctx, b), 201);
  v1.get("/sessions/:id", handle((ctx, req) => svc.getSessionView(ctx, String(req.params.id))));
  route(v1, "post", "/resolve", C.ResolveItemsInput, (ctx, b) => svc.resolveItems(ctx, b));
  route(v1, "post", "/products/search", C.SearchProductsInput, (ctx, b) => svc.searchProducts(ctx, b));
  v1.get("/preferences", handle((ctx, req) => svc.getCustomerPreferences(ctx, parse(C.GetPreferencesInput, req.query))));
  route(v1, "post", "/inventory/check", C.CheckInventoryInput, (ctx, b) => svc.checkInventory(ctx, b));
  route(v1, "post", "/delivery/validate", C.ValidateDeliveryInput, (ctx, b) => svc.validateDelivery(ctx, b));
  route(v1, "post", "/customer/validate", C.ValidateCustomerInput, (ctx, b) => svc.validateCustomer(ctx, b));
  route(v1, "post", "/cart/calculate", C.CalculateCartInput, (ctx, b) => svc.calculateCart(ctx, b));
  route(v1, "post", "/order-intents", C.CreateOrderIntentInput, (ctx, b, req) => svc.createOrderIntent(ctx, { ...b, ...(idemHeader(req) && !b.idempotency_key ? { idempotency_key: idemHeader(req)! } : {}) }), 201);
  v1.get("/order-intents/:id", handle((ctx, req) => svc.getOrderIntent(ctx, { order_intent_id: String(req.params.id) })));
  v1.post(
    "/order-intents/:id/confirmation-request",
    handle(async (ctx, req) => {
      const { intent, confirmationUrl } = await svc.beginAuthorization(ctx, String(req.params.id));
      const view = svc.intentView(intent);
      return { order_intent: view, confirmation_url: confirmationUrl };
    }),
  );
  v1.post(
    "/order-intents/:id/confirm",
    handle((ctx, req) => svc.confirmOrderIntent(ctx, String(req.params.id), parse(C.ConfirmOrderIntentInput, req.body), { kind: "FIRST_PARTY" })),
  );
  v1.post(
    "/order-intents/:id/authorize-payment",
    handle((ctx, req) => svc.authorizePayment(ctx, parse(C.AuthorizePaymentInput, { ...req.body, order_intent_id: String(req.params.id) }))),
  );
  v1.post(
    "/orders",
    handle(
      (ctx, req) => svc.placeOrder(ctx, parse(C.PlaceOrderInput, { ...req.body, idempotency_key: req.body?.idempotency_key ?? idemHeader(req) })),
      201,
    ),
  );
  route(v1, "post", "/orders/reorder", C.ReorderInput, (ctx, b, req) => svc.reorderPrevious(ctx, { ...b, ...(idemHeader(req) && !b.idempotency_key ? { idempotency_key: idemHeader(req)! } : {}) }), 201);
  v1.get("/orders/latest", handle((ctx, req) => svc.getOrderStatus(ctx, parse(C.GetOrderStatusInput, req.query))));
  v1.get("/orders/:id", handle((ctx, req) => svc.getOrderStatus(ctx, parse(C.GetOrderStatusInput, { ...req.query, order_id: String(req.params.id) }))));
  v1.get("/orders/:id/receipt", handle((ctx, req) => svc.getReceipt(ctx, { order_id: String(req.params.id) })));
  v1.post("/orders/:id/cancel", handle((ctx, req) => svc.cancelOrder(ctx, parse(C.CancelOrderInput, { ...req.body, order_id: String(req.params.id) }))));

  // Dialog + platform adapters.
  v1.post(
    "/dialog/turn",
    handle((ctx, req) => {
      const t = req.body as DialogTurn;
      if (!t || !DIALOG_INTENTS.includes(t.intent) || typeof t.conversation_id !== "string" || typeof t.platform_user_id !== "string") {
        throw new GimmeError("INVALID_REQUEST");
      }
      return dialog.handle({ ...ctx, channel: "ADAPTER" }, t);
    }),
  );
  v1.post(
    "/adapters/apple/intents",
    handle(async (ctx, req) => {
      let turn: DialogTurn;
      try {
        turn = fromApple(req.body as AppleIntentRequest);
      } catch (e) {
        throw new GimmeError("PLATFORM_NOT_SUPPORTED", { message: (e as Error).message });
      }
      return toApple(await dialog.handle({ ...ctx, channel: "ADAPTER" }, turn));
    }),
  );
  v1.post(
    "/adapters/google/fulfillment",
    handle(async (ctx, req) => {
      let turn: DialogTurn;
      try {
        turn = fromGoogle(req.body as GoogleFulfillmentRequest);
      } catch (e) {
        throw new GimmeError("PLATFORM_NOT_SUPPORTED", { message: (e as Error).message });
      }
      return toGoogle(await dialog.handle({ ...ctx, channel: "ADAPTER" }, turn));
    }),
  );

  app.use("/v1/voice", v1);

  // --------------------------------------------------------------- MCP
  mountMcp(app, rt, auth);

  app.use((req, res) => {
    res.status(404).json(new GimmeError("INVALID_REQUEST", { message: "Not found" }).toBody(req.requestId));
  });
  // Body-parser and other express errors.
  app.use((err: unknown, req: Request, res: Response, _next: NextFunction) => {
    const e = err instanceof GimmeError ? err : new GimmeError("INVALID_REQUEST", { cause: err });
    res.status(e.httpStatus).json(e.toBody(req.requestId));
  });

  return app;

  // ----------------------------------------------------------- helpers

  function ctxOf(req: Request): CallContext {
    return {
      principal: req.principal!,
      requestId: req.requestId,
      correlationId: req.correlationId,
      channel: "VOICE_API",
      apiVersion: "1.0",
      device: deviceOf(req),
    };
  }

  function handle(fn: (ctx: CallContext, req: Request) => Promise<unknown>, okStatus = 200) {
    return async (req: Request, res: Response) => {
      try {
        res.status(okStatus).json(await fn(ctxOf(req), req));
      } catch (err) {
        const e = toGimmeError(err);
        res.status(e.httpStatus).json(e.toBody(req.requestId));
      }
    };
  }

  function route<S extends ZodType>(
    r: express.Router,
    method: "post",
    path: string,
    schema: S,
    fn: (ctx: CallContext, body: z.infer<S>, req: Request) => Promise<unknown>,
    okStatus = 200,
  ): void {
    r[method](path, handle((ctx, req) => fn(ctx, parse(schema, req.body), req), okStatus));
  }
}

function parse<S extends ZodType>(schema: S, value: unknown): z.infer<S> {
  const r = schema.safeParse(value ?? {});
  if (!r.success) {
    throw new GimmeError("INVALID_REQUEST", {
      details: { issues: r.error.issues.map((i) => ({ path: i.path.join("."), message: i.message })) },
    });
  }
  return r.data;
}

function header(req: Request, name: string): string | undefined {
  const v = req.headers[name];
  return typeof v === "string" && v.length > 0 && v.length <= 256 ? v : undefined;
}

function idemHeader(req: Request): string | undefined {
  return header(req, "idempotency-key");
}

function deviceOf(req: Request): { ip?: string; user_agent?: string } {
  return { ...(req.ip ? { ip: req.ip } : {}), ...(header(req, "user-agent") ? { user_agent: header(req, "user-agent")! } : {}) };
}

/** Bearer-token middleware. Sets req.principal and the MCP SDK's req.auth. */
function bearer(rt: VoiceCommerceRuntime) {
  return async (req: Request, res: Response, next: NextFunction) => {
    const h = req.headers.authorization;
    const token = h?.startsWith("Bearer ") ? h.slice(7).trim() : undefined;
    try {
      const principal = await rt.verifier.verify(token);
      req.principal = principal;
      req.auth = {
        token: token!,
        clientId: principal.clientId,
        scopes: principal.scopes,
        expiresAt: principal.expiresAt,
        extra: { principal },
      };
      next();
    } catch (err) {
      const e = toGimmeError(err);
      const isMcp = req.baseUrl.startsWith("/mcp") || req.path.startsWith("/mcp");
      res.setHeader(
        "WWW-Authenticate",
        `Bearer error="invalid_token", resource_metadata="${rt.config.publicBaseUrl}/.well-known/oauth-protected-resource${isMcp ? "/mcp" : ""}"`,
      );
      res.status(401).json(e.toBody(req.requestId));
    }
  };
}

/**
 * Stateful Streamable HTTP. Stateful because request_authorization elicits
 * from the customer mid-call, and the client's answer arrives as a separate
 * POST on the same MCP session. Each MCP session is pinned to the customer
 * and OAuth client that opened it; a request carrying anyone else's token is
 * refused, so a leaked Mcp-Session-Id is useless on its own.
 */
function mountMcp(app: express.Express, rt: VoiceCommerceRuntime, auth: ReturnType<typeof bearer>): void {
  const sessions = new Map<string, { transport: StreamableHTTPServerTransport; customerId: string; clientId: string; lastSeen: number }>();
  const IDLE_MS = 60 * 60 * 1000;
  setInterval(() => {
    const cutoff = Date.now() - IDLE_MS;
    for (const [id, s] of sessions) {
      if (s.lastSeen < cutoff) {
        sessions.delete(id);
        void s.transport.close();
      }
    }
  }, 5 * 60 * 1000).unref();

  app.all("/mcp", auth, async (req, res) => {
    const sid = header(req, "mcp-session-id");
    const principal = req.principal!;
    try {
      if (sid) {
        const s = sessions.get(sid);
        if (!s) {
          res.status(404).json({ jsonrpc: "2.0", error: { code: -32001, message: "Session not found" }, id: null });
          return;
        }
        if (s.customerId !== principal.customerId || s.clientId !== principal.clientId) {
          res.status(403).json({ jsonrpc: "2.0", error: { code: -32003, message: "Session belongs to another principal" }, id: null });
          return;
        }
        s.lastSeen = Date.now();
        await s.transport.handleRequest(req, res, req.body);
        return;
      }
      if (req.method !== "POST" || !isInitializeRequest(req.body)) {
        res.status(400).json({ jsonrpc: "2.0", error: { code: -32000, message: "Initialize first (no Mcp-Session-Id)" }, id: null });
        return;
      }
      const transport = new StreamableHTTPServerTransport({
        sessionIdGenerator: () => randomUUID(),
        onsessioninitialized: (id) => {
          sessions.set(id, { transport, customerId: principal.customerId, clientId: principal.clientId, lastSeen: Date.now() });
        },
      });
      transport.onclose = () => {
        if (transport.sessionId) sessions.delete(transport.sessionId);
      };
      const server = createMcpServer(rt);
      await server.connect(transport);
      await transport.handleRequest(req, res, req.body);
    } catch (err) {
      rt.log.error("mcp request failed", { request_id: req.requestId, error: String(err) });
      if (!res.headersSent) res.status(500).json({ jsonrpc: "2.0", error: { code: -32603, message: "Internal error" }, id: null });
    }
  });
}
