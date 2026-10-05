/**
 * Runtime configuration. Every value is read once, at start-up, from the
 * environment. Sandbox defaults let the whole system run with no setup; the
 * production checks at the bottom refuse to start with those defaults when
 * VOICE_ENV=production.
 */

export interface Config {
  env: "sandbox" | "production";
  port: number;
  /** Public base URL of this service. Used for OAuth resource metadata and confirmation links. */
  publicBaseUrl: string;

  auth: {
    /** Expected `iss` of access tokens issued by GIMME's OAuth authorization server. */
    issuer: string;
    /** Expected `aud` — this resource server. */
    audience: string;
    /** JWKS endpoint of the authorization server (production). */
    jwksUrl?: string;
    /** Shared HS256 secret (sandbox only — lets tests and the demo mint tokens). */
    sandboxSigningSecret?: string;
    /** Authorization server metadata advertised to MCP clients. */
    authorizationServerUrl: string;
  };

  /** Key that signs customer purchase authorizations. Never shared with any client. */
  authorizationSigningKey: string;

  ttl: {
    sessionSeconds: number;
    orderIntentSeconds: number;
    purchaseAuthorizationSeconds: number;
    idempotencyRecordSeconds: number;
  };

  confirmation: {
    /** Platforms whose purchase confirmations must carry device authentication (unlock / biometric). */
    requireDeviceAuthFor: string[];
    /** Allow MCP form-mode elicitation as a confirmation channel. URL mode is always preferred. */
    allowMcpFormElicitation: boolean;
  };

  webhooks: {
    subscribers: { url: string; secret: string; events: string[] }[];
    maxAttempts: number;
  };

  /** Internal shared secret that GIMME's OMS uses to push order events in. */
  internalEventsSecret: string;

  versions: {
    protocol: string;
    mcpContract: string;
    voiceApi: string;
  };
}

const SANDBOX_SECRET = "sandbox-only-access-token-secret-change-me-0123456789";
const SANDBOX_AUTHZ_KEY = "sandbox-only-purchase-authorization-key-0123456789";
const SANDBOX_INTERNAL = "sandbox-only-internal-events-secret";

function int(env: NodeJS.ProcessEnv, name: string, fallback: number): number {
  const raw = env[name];
  if (raw === undefined || raw === "") return fallback;
  const n = Number(raw);
  if (!Number.isInteger(n) || n <= 0) throw new Error(`${name} must be a positive integer`);
  return n;
}

function list(env: NodeJS.ProcessEnv, name: string, fallback: string[]): string[] {
  const raw = env[name];
  if (raw === undefined) return fallback;
  return raw.split(",").map((s) => s.trim()).filter(Boolean);
}

export function loadConfig(env: NodeJS.ProcessEnv = process.env): Config {
  const mode = env.VOICE_ENV === "production" ? "production" : "sandbox";
  const port = int(env, "PORT", 8787);
  const publicBaseUrl = (env.PUBLIC_BASE_URL ?? `http://localhost:${port}`).replace(/\/$/, "");

  const config: Config = {
    env: mode,
    port,
    publicBaseUrl,
    auth: {
      issuer: env.OAUTH_ISSUER ?? "https://auth.sandbox.gimme.local",
      audience: env.OAUTH_AUDIENCE ?? publicBaseUrl,
      jwksUrl: env.OAUTH_JWKS_URL || undefined,
      sandboxSigningSecret: mode === "sandbox" ? (env.SANDBOX_TOKEN_SECRET ?? SANDBOX_SECRET) : undefined,
      authorizationServerUrl: env.OAUTH_AUTHORIZATION_SERVER ?? env.OAUTH_ISSUER ?? "https://auth.sandbox.gimme.local",
    },
    authorizationSigningKey: env.PURCHASE_AUTHORIZATION_KEY ?? SANDBOX_AUTHZ_KEY,
    ttl: {
      sessionSeconds: int(env, "SESSION_TTL_SECONDS", 15 * 60),
      orderIntentSeconds: int(env, "ORDER_INTENT_TTL_SECONDS", 5 * 60),
      purchaseAuthorizationSeconds: int(env, "PURCHASE_AUTHORIZATION_TTL_SECONDS", 2 * 60),
      idempotencyRecordSeconds: int(env, "IDEMPOTENCY_TTL_SECONDS", 24 * 60 * 60),
    },
    confirmation: {
      requireDeviceAuthFor: list(env, "REQUIRE_DEVICE_AUTH_PLATFORMS", ["APPLE", "GOOGLE"]),
      allowMcpFormElicitation: (env.ALLOW_MCP_FORM_CONFIRMATION ?? "true") === "true",
    },
    webhooks: {
      subscribers: parseSubscribers(env.WEBHOOK_SUBSCRIBERS),
      maxAttempts: int(env, "WEBHOOK_MAX_ATTEMPTS", 6),
    },
    internalEventsSecret: env.INTERNAL_EVENTS_SECRET ?? SANDBOX_INTERNAL,
    versions: { protocol: "1.0", mcpContract: "1.0", voiceApi: "1.0" },
  };

  if (mode === "production") assertProductionSafe(config);
  return config;
}

/** WEBHOOK_SUBSCRIBERS is JSON: [{"url":"https://...","secret":"...","events":["order.*"]}] */
function parseSubscribers(raw: string | undefined): Config["webhooks"]["subscribers"] {
  if (!raw) return [];
  const parsed: unknown = JSON.parse(raw);
  if (!Array.isArray(parsed)) throw new Error("WEBHOOK_SUBSCRIBERS must be a JSON array");
  return parsed.map((s: { url?: unknown; secret?: unknown; events?: unknown }) => {
    if (typeof s.url !== "string" || typeof s.secret !== "string") {
      throw new Error("each webhook subscriber needs a url and a secret");
    }
    const events = Array.isArray(s.events) ? s.events.map(String) : ["*"];
    return { url: s.url, secret: s.secret, events };
  });
}

function assertProductionSafe(c: Config): void {
  const problems: string[] = [];
  if (!c.auth.jwksUrl) problems.push("OAUTH_JWKS_URL is required in production");
  if (c.authorizationSigningKey === SANDBOX_AUTHZ_KEY || c.authorizationSigningKey.length < 32) {
    problems.push("PURCHASE_AUTHORIZATION_KEY must be set to a secret of at least 32 characters");
  }
  if (c.internalEventsSecret === SANDBOX_INTERNAL) problems.push("INTERNAL_EVENTS_SECRET must be set");
  if (!c.publicBaseUrl.startsWith("https://")) problems.push("PUBLIC_BASE_URL must be https in production");
  if (problems.length) throw new Error(`Refusing to start in production:\n - ${problems.join("\n - ")}`);
}
