/**
 * Asynchronous events (§30) and outbound webhooks.
 *
 * Every state change that matters to an adapter is published on the bus.
 * In-process subscribers (metrics, the audit trail) receive it synchronously;
 * external subscribers (the Apple and Google adapter backends) receive it as
 * a signed webhook, retried with exponential backoff.
 *
 * Signature: GIMME-Signature: t=<unix seconds>,v1=<hex HMAC-SHA256(secret, "<t>.<raw body>")>
 * Receivers must recompute the HMAC over the raw body, compare in constant
 * time, reject timestamps older than five minutes, and de-duplicate on
 * GIMME-Event-Id (deliveries are at-least-once).
 */

import { createHmac, randomUUID, timingSafeEqual } from "node:crypto";
import type { Logger } from "../observability/logger.js";

export const EVENT_TYPES = [
  "order_intent.created",
  "order_intent.confirmed",
  "order_intent.expired",
  "order_intent.superseded",
  "payment.authorized",
  "payment.failed",
  "order.created",
  "order.confirmed",
  "order.accepted",
  "order.preparing",
  "order.dispatched",
  "order.out_for_delivery",
  "order.delivered",
  "order.cancelled",
  "order.refunded",
  "order.failed",
] as const;
export type EventType = (typeof EVENT_TYPES)[number];

export interface DomainEvent {
  id: string;
  type: EventType;
  created_at: string;
  api_version: string;
  data: {
    customer_id: string;
    order_id?: string;
    order_intent_id?: string;
    status?: string;
    platform?: string;
    total?: number;
    currency?: string;
    estimated_minutes?: number;
    reason?: string;
  };
}

export type EventListener = (event: DomainEvent) => void;

export class EventBus {
  private readonly listeners: EventListener[] = [];

  subscribe(listener: EventListener): () => void {
    this.listeners.push(listener);
    return () => {
      const i = this.listeners.indexOf(listener);
      if (i >= 0) this.listeners.splice(i, 1);
    };
  }

  publish(type: EventType, data: DomainEvent["data"], apiVersion = "1.0"): DomainEvent {
    const event: DomainEvent = { id: `EVT-${randomUUID()}`, type, created_at: new Date().toISOString(), api_version: apiVersion, data };
    for (const l of this.listeners) {
      try {
        l(event);
      } catch {
        // One failing listener must not stop delivery to the others.
      }
    }
    return event;
  }
}

export function signWebhook(secret: string, body: string, timestamp: number): string {
  const mac = createHmac("sha256", secret).update(`${timestamp}.${body}`).digest("hex");
  return `t=${timestamp},v1=${mac}`;
}

export function verifyWebhookSignature(secret: string, body: string, header: string, toleranceSeconds = 300, nowSeconds = Math.floor(Date.now() / 1000)): boolean {
  const parts = Object.fromEntries(header.split(",").map((p) => p.split("=", 2) as [string, string]));
  const t = Number(parts.t);
  if (!Number.isFinite(t) || Math.abs(nowSeconds - t) > toleranceSeconds || !parts.v1) return false;
  const expected = Buffer.from(createHmac("sha256", secret).update(`${t}.${body}`).digest("hex"));
  const given = Buffer.from(parts.v1);
  return expected.length === given.length && timingSafeEqual(expected, given);
}

export interface WebhookSubscriber {
  url: string;
  secret: string;
  /** Event type patterns: exact ("order.delivered"), prefix ("order.*") or "*". */
  events: string[];
}

export interface DeliveryAttempt {
  event_id: string;
  url: string;
  attempt: number;
  status: number | "NETWORK_ERROR";
  delivered: boolean;
}

type FetchLike = (url: string, init: { method: string; headers: Record<string, string>; body: string }) => Promise<{ status: number }>;

export class WebhookDispatcher {
  readonly attempts: DeliveryAttempt[] = [];

  constructor(
    private readonly subscribers: WebhookSubscriber[],
    private readonly log: Logger,
    private readonly opts: {
      maxAttempts: number;
      fetch?: FetchLike;
      /** Delay before retry n (1-based). Exponential with a 1s base by default. */
      backoffMs?: (attempt: number) => number;
      sleep?: (ms: number) => Promise<void>;
    },
  ) {}

  attach(bus: EventBus): void {
    bus.subscribe((event) => {
      for (const sub of this.subscribers) {
        if (matches(sub.events, event.type)) void this.deliver(sub, event);
      }
    });
  }

  async deliver(sub: WebhookSubscriber, event: DomainEvent): Promise<boolean> {
    const doFetch: FetchLike = this.opts.fetch ?? ((url, init) => fetch(url, init));
    const sleep = this.opts.sleep ?? ((ms: number) => new Promise((r) => setTimeout(r, ms)));
    const backoff = this.opts.backoffMs ?? ((n: number) => Math.min(1000 * 2 ** (n - 1), 5 * 60_000));
    const body = JSON.stringify(event);
    for (let attempt = 1; attempt <= this.opts.maxAttempts; attempt++) {
      const headers = {
        "content-type": "application/json",
        "gimme-event-id": event.id,
        "gimme-event-type": event.type,
        "gimme-signature": signWebhook(sub.secret, body, Math.floor(Date.now() / 1000)),
      };
      let status: number | "NETWORK_ERROR";
      try {
        status = (await doFetch(sub.url, { method: "POST", headers, body })).status;
      } catch {
        status = "NETWORK_ERROR";
      }
      const delivered = typeof status === "number" && status >= 200 && status < 300;
      this.attempts.push({ event_id: event.id, url: sub.url, attempt, status, delivered });
      if (delivered) return true;
      // 4xx other than 408/429 means the receiver rejected it; retrying won't help.
      if (typeof status === "number" && status >= 400 && status < 500 && status !== 408 && status !== 429) break;
      if (attempt < this.opts.maxAttempts) await sleep(backoff(attempt));
    }
    this.log.warn("webhook delivery failed", { event_id: event.id, event_type: event.type, url: sub.url });
    return false;
  }
}

function matches(patterns: string[], type: string): boolean {
  return patterns.some((p) => p === "*" || p === type || (p.endsWith(".*") && type.startsWith(p.slice(0, -1))));
}
