/**
 * Verifies OAuth 2.0 bearer access tokens issued by GIMME's authorization
 * server (§22). This service is a resource server only: it never issues
 * access tokens in production, it verifies them against the authorization
 * server's JWKS. In the sandbox a shared HS256 secret stands in so tests and
 * the demo can mint tokens.
 *
 * Identity comes from the token's `sub` and nowhere else. No tool accepts a
 * customer id as an argument that could override it.
 */

import { createRemoteJWKSet, jwtVerify, SignJWT, type JWTPayload } from "jose";
import { randomUUID } from "node:crypto";
import type { Config } from "../config.js";
import { GimmeError } from "../domain/errors.js";

export interface Principal {
  /** The GIMME customer id — the token subject. */
  customerId: string;
  /** The OAuth client the customer granted access to (Siri adapter, Google adapter, an agent). */
  clientId: string;
  scopes: string[];
  tokenId: string;
  expiresAt: number;
  /** When the customer last authenticated to the authorization server, if the AS reports it. */
  authTime?: number;
}

export class AccessTokenVerifier {
  private readonly key: Parameters<typeof jwtVerify>[1];

  constructor(private readonly config: Config) {
    if (config.auth.jwksUrl) {
      this.key = createRemoteJWKSet(new URL(config.auth.jwksUrl));
    } else if (config.auth.sandboxSigningSecret) {
      this.key = new TextEncoder().encode(config.auth.sandboxSigningSecret);
    } else {
      throw new Error("No access-token verification key configured");
    }
  }

  async verify(token: string | undefined): Promise<Principal> {
    if (!token) throw new GimmeError("AUTHENTICATION_REQUIRED");
    let payload: JWTPayload;
    try {
      ({ payload } = await jwtVerify(token, this.key as Uint8Array, {
        issuer: this.config.auth.issuer,
        audience: this.config.auth.audience,
        requiredClaims: ["sub", "exp"],
      }));
    } catch (err) {
      throw new GimmeError("AUTHENTICATION_REQUIRED", { cause: err, message: "Your GIMME sign-in has expired. Please sign in again." });
    }
    const scope = typeof payload.scope === "string" ? payload.scope : "";
    const clientId =
      (typeof payload.client_id === "string" && payload.client_id) || (typeof payload.azp === "string" && payload.azp) || "unknown";
    return {
      customerId: payload.sub!,
      clientId,
      scopes: scope.split(" ").filter(Boolean),
      tokenId: payload.jti ?? `${payload.sub}:${payload.iat ?? 0}`,
      expiresAt: payload.exp!,
      ...(typeof payload.auth_time === "number" ? { authTime: payload.auth_time } : {}),
    };
  }
}

/** Sandbox only: mint an access token the verifier will accept. */
export async function mintSandboxToken(
  config: Config,
  opts: { customerId: string; scopes: string[]; clientId?: string; ttlSeconds?: number },
): Promise<string> {
  if (!config.auth.sandboxSigningSecret) throw new Error("Sandbox tokens are disabled outside the sandbox");
  const now = Math.floor(Date.now() / 1000);
  return new SignJWT({ scope: opts.scopes.join(" "), client_id: opts.clientId ?? "sandbox-client", auth_time: now })
    .setProtectedHeader({ alg: "HS256" })
    .setSubject(opts.customerId)
    .setIssuer(config.auth.issuer)
    .setAudience(config.auth.audience)
    .setIssuedAt(now)
    .setExpirationTime(now + (opts.ttlSeconds ?? 3600))
    .setJti(randomUUID())
    .sign(new TextEncoder().encode(config.auth.sandboxSigningSecret));
}
