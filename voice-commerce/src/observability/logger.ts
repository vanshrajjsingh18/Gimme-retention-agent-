/**
 * Structured JSON logging (§40). Every line carries the request id and
 * correlation id of the call that produced it. Values under sensitive keys
 * are redacted the same way the audit trail redacts them.
 */

import { redact } from "../audit/audit-log.js";

type Level = "debug" | "info" | "warn" | "error";
const ORDER: Record<Level, number> = { debug: 10, info: 20, warn: 30, error: 40 };

export interface Logger {
  debug(msg: string, fields?: Record<string, unknown>): void;
  info(msg: string, fields?: Record<string, unknown>): void;
  warn(msg: string, fields?: Record<string, unknown>): void;
  error(msg: string, fields?: Record<string, unknown>): void;
  child(fields: Record<string, unknown>): Logger;
}

export function createLogger(base: Record<string, unknown> = {}, minLevel: Level = (process.env.LOG_LEVEL as Level) || "info"): Logger {
  const emit = (level: Level, msg: string, fields?: Record<string, unknown>) => {
    if (ORDER[level] < ORDER[minLevel]) return;
    const line = { ts: new Date().toISOString(), level, msg, ...base, ...(fields ? (redact(fields) as object) : {}) };
    (level === "error" || level === "warn" ? process.stderr : process.stdout).write(`${JSON.stringify(line)}\n`);
  };
  return {
    debug: (m, f) => emit("debug", m, f),
    info: (m, f) => emit("info", m, f),
    warn: (m, f) => emit("warn", m, f),
    error: (m, f) => emit("error", m, f),
    child: (fields) => createLogger({ ...base, ...fields }, minLevel),
  };
}

/** For tests and stdio MCP (where stdout is the protocol channel). */
export const silentLogger: Logger = {
  debug() {},
  info() {},
  warn() {},
  error() {},
  child: () => silentLogger,
};

/** Logs to stderr only — required for the stdio MCP transport. */
export function stderrLogger(minLevel: Level = "info"): Logger {
  const emit = (level: Level, msg: string, fields?: Record<string, unknown>) => {
    if (ORDER[level] < ORDER[minLevel]) return;
    process.stderr.write(`${JSON.stringify({ ts: new Date().toISOString(), level, msg, ...(fields ? (redact(fields) as object) : {}) })}\n`);
  };
  const l: Logger = {
    debug: (m, f) => emit("debug", m, f),
    info: (m, f) => emit("info", m, f),
    warn: (m, f) => emit("warn", m, f),
    error: (m, f) => emit("error", m, f),
    child: () => l,
  };
  return l;
}
