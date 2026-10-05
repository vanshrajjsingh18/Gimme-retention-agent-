/**
 * Metrics (§40), exposed in Prometheus text format at /metrics.
 *
 * Counters and latency histograms, labelled by tool / route / platform. The
 * voice funnel is a counter with a `stage` label, incremented once per
 * session per stage, so a dashboard computes conversion between any two
 * stages as a ratio of two series:
 *
 *   sessions -> product_resolved -> cart_priced -> intent_created
 *            -> confirmed -> payment_authorized -> order_placed
 */

type Labels = Record<string, string>;

const BUCKETS_MS = [5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000];

function key(labels: Labels): string {
  return Object.keys(labels)
    .sort()
    .map((k) => `${k}="${String(labels[k]).replace(/["\\\n]/g, "_")}"`)
    .join(",");
}

export const FUNNEL_STAGES = [
  "session_started",
  "product_resolved",
  "cart_priced",
  "intent_created",
  "confirmation_requested",
  "confirmed",
  "confirmation_declined",
  "payment_authorized",
  "order_placed",
  "abandoned",
] as const;
export type FunnelStage = (typeof FUNNEL_STAGES)[number];

export class Metrics {
  private readonly counters = new Map<string, Map<string, number>>();
  private readonly histograms = new Map<string, Map<string, { buckets: number[]; sum: number; count: number }>>();
  private readonly help = new Map<string, string>();

  inc(name: string, labels: Labels = {}, by = 1, help?: string): void {
    if (help) this.help.set(name, help);
    const series = this.counters.get(name) ?? new Map<string, number>();
    const k = key(labels);
    series.set(k, (series.get(k) ?? 0) + by);
    this.counters.set(name, series);
  }

  observe(name: string, ms: number, labels: Labels = {}, help?: string): void {
    if (help) this.help.set(name, help);
    const series = this.histograms.get(name) ?? new Map();
    const k = key(labels);
    const h = series.get(k) ?? { buckets: BUCKETS_MS.map(() => 0), sum: 0, count: 0 };
    BUCKETS_MS.forEach((b, i) => {
      if (ms <= b) h.buckets[i]! += 1;
    });
    h.sum += ms;
    h.count += 1;
    series.set(k, h);
    this.histograms.set(name, series);
  }

  async time<R>(name: string, labels: Labels, fn: () => Promise<R>): Promise<R> {
    const start = performance.now();
    try {
      return await fn();
    } finally {
      this.observe(name, performance.now() - start, labels);
    }
  }

  funnel(stage: FunnelStage, platform: string): void {
    this.inc("gimme_voice_funnel_total", { stage, platform }, 1, "Voice commerce funnel progression, one increment per session per stage");
  }

  counter(name: string, labels: Labels = {}): number {
    return this.counters.get(name)?.get(key(labels)) ?? 0;
  }

  /** Sum of a counter across every label set that includes `match`. */
  sum(name: string, match: Labels = {}): number {
    const series = this.counters.get(name);
    if (!series) return 0;
    const parts = Object.entries(match).map(([k, v]) => `${k}="${v}"`);
    let total = 0;
    for (const [k, v] of series) if (parts.every((p) => k.split(",").includes(p))) total += v;
    return total;
  }

  render(): string {
    const lines: string[] = [];
    for (const [name, series] of this.counters) {
      if (this.help.has(name)) lines.push(`# HELP ${name} ${this.help.get(name)}`);
      lines.push(`# TYPE ${name} counter`);
      for (const [k, v] of series) lines.push(`${name}${k ? `{${k}}` : ""} ${v}`);
    }
    for (const [name, series] of this.histograms) {
      if (this.help.has(name)) lines.push(`# HELP ${name} ${this.help.get(name)}`);
      lines.push(`# TYPE ${name} histogram`);
      for (const [k, h] of series) {
        const sep = k ? `${k},` : "";
        BUCKETS_MS.forEach((b, i) => lines.push(`${name}_bucket{${sep}le="${b}"} ${h.buckets[i]}`));
        lines.push(`${name}_bucket{${sep}le="+Inf"} ${h.count}`);
        lines.push(`${name}_sum${k ? `{${k}}` : ""} ${h.sum.toFixed(3)}`);
        lines.push(`${name}_count${k ? `{${k}}` : ""} ${h.count}`);
      }
    }
    return `${lines.join("\n")}\n`;
  }
}
