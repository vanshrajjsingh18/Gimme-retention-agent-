import type { ReactNode } from 'react';

import { formatCurrency, formatNumber, formatPercent, humanize } from './format';

/**
 * Recharts passes tooltip values as `ValueType | undefined` and labels as
 * `ReactNode`, so every formatter has to cope with a value that is not the
 * number or string the chart data says it is. These helpers do that coercion
 * once, in one place, instead of at every call site.
 */

function toNumber(value: unknown): number {
  if (typeof value === 'number') return value;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

function toText(value: unknown): string {
  return value === null || value === undefined ? '' : String(value);
}

export const axisTick = { fontSize: 11, fill: '#64748b' } as const;
export const smallAxisTick = { fontSize: 10, fill: '#64748b' } as const;
export const tooltipStyle = { fontSize: 12, borderRadius: 8 } as const;

/** Tooltip formatter rendering a count with a fixed series label. */
export function countFormatter(label: string) {
  return (value: unknown): [string, string] => [formatNumber(toNumber(value)), label];
}

/** Tooltip formatter rendering NZD currency with a fixed series label. */
export function currencyFormatter(label: string, precise = false) {
  return (value: unknown): [string, string] => [
    formatCurrency(toNumber(value), precise),
    label,
  ];
}

/** Tooltip formatter rendering a 0-1 rate as a percentage. */
export function percentFormatter(label: string, digits = 1) {
  return (value: unknown): [string, string] => [
    formatPercent(toNumber(value), digits),
    label,
  ];
}

/** Tooltip formatter that names the series from the datum key. */
export function namedCountFormatter() {
  return (value: unknown, name: unknown): [string, string] => [
    formatNumber(toNumber(value)),
    humanize(toText(name)),
  ];
}

/** Axis / tooltip label formatter turning CONSTANT_CASE into Title Case. */
export function humanizeLabel(label: ReactNode): string {
  return humanize(toText(label));
}

/** Tooltip label formatter with a custom template, e.g. "3 orders". */
export function labelFormatter(render: (value: string) => string) {
  return (label: ReactNode): string => render(toText(label));
}
