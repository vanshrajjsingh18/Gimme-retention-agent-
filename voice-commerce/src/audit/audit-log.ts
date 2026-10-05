/**
 * Immutable audit trail (§39).
 *
 * Entries are append-only and hash-chained: each entry's hash covers its own
 * content and the previous entry's hash, so altering or removing any past
 * entry breaks verification of every entry after it. Payment credentials
 * never reach the trail — values under sensitive keys are redacted before an
 * entry is hashed, and card-shaped strings are refused upstream.
 *
 * The sink is pluggable. The default keeps entries in memory and writes each
 * one as a JSON line to stdout (where the platform's log pipeline — e.g.
 * CloudWatch with a retention lock, or S3 Object Lock — makes it durable).
 */

import { createHash } from "node:crypto";
import { canonicalJson } from "../security/purchase-authorization.js";

export interface AuditEvent {
  action: string;
  outcome: "SUCCESS" | "FAILURE" | "DENIED";
  request_id: string;
  correlation_id?: string;
  session_id?: string;
  customer_id?: string;
  platform?: string;
  platform_user_id?: string;
  client_id?: string;
  channel?: "MCP" | "VOICE_API" | "ADAPTER" | "INTERNAL";
  order_intent_id?: string;
  order_id?: string;
  payment_reference?: string;
  failure_reason?: string;
  api_version?: string;
  data?: Record<string, unknown>;
  device?: { ip?: string; user_agent?: string };
}

export interface AuditEntry extends AuditEvent {
  seq: number;
  at: string;
  prev_hash: string;
  hash: string;
}

export interface AuditSink {
  write(entry: AuditEntry): void;
}

const SENSITIVE_KEY = /(token|secret|password|card|cvv|cvc|pan|authorization_header|wallet_payload)/i;
const ALLOWED_TOKENISH = new Set(["payment_method_id", "authorization_id", "payment_authorization_id", "token_id", "idempotency_key"]);

export function redact(value: unknown, depth = 0): unknown {
  if (depth > 8) return "[depth]";
  if (Array.isArray(value)) return value.map((v) => redact(v, depth + 1));
  if (value && typeof value === "object") {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(value as Record<string, unknown>)) {
      out[k] = SENSITIVE_KEY.test(k) && !ALLOWED_TOKENISH.has(k) ? "[REDACTED]" : redact(v, depth + 1);
    }
    return out;
  }
  return value;
}

export class AuditLog {
  private readonly entries: AuditEntry[] = [];
  private lastHash = "0".repeat(64);

  constructor(private readonly sinks: AuditSink[] = [], private readonly now: () => Date = () => new Date()) {}

  record(event: AuditEvent): AuditEntry {
    const body = redact({ ...event }) as AuditEvent;
    const seq = this.entries.length + 1;
    const at = this.now().toISOString();
    const prev_hash = this.lastHash;
    const hash = createHash("sha256").update(canonicalJson({ ...body, seq, at, prev_hash })).digest("hex");
    const entry: AuditEntry = Object.freeze({ ...body, seq, at, prev_hash, hash }) as AuditEntry;
    this.entries.push(entry);
    this.lastHash = hash;
    for (const s of this.sinks) s.write(entry);
    return entry;
  }

  /** Recompute the chain. Returns the seq of the first broken entry, or null if intact. */
  verify(entries: readonly AuditEntry[] = this.entries): number | null {
    let prev = "0".repeat(64);
    for (const e of entries) {
      const { hash, ...rest } = e;
      const expected = createHash("sha256").update(canonicalJson(rest)).digest("hex");
      if (e.prev_hash !== prev || expected !== hash) return e.seq;
      prev = hash;
    }
    return null;
  }

  query(filter: Partial<Pick<AuditEvent, "customer_id" | "order_intent_id" | "order_id" | "session_id">>): AuditEntry[] {
    return this.entries.filter((e) =>
      Object.entries(filter).every(([k, v]) => v === undefined || (e as unknown as Record<string, unknown>)[k] === v),
    );
  }

  all(): readonly AuditEntry[] {
    return this.entries;
  }
}

export const stdoutAuditSink: AuditSink = {
  write(entry) {
    process.stdout.write(`${JSON.stringify({ type: "audit", ...entry })}\n`);
  },
};
