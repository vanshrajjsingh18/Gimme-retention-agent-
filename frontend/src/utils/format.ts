/** Display formatting helpers shared across pages and charts. */

const currency = new Intl.NumberFormat('en-NZ', {
  style: 'currency',
  currency: 'NZD',
  maximumFractionDigits: 0,
});

const currencyPrecise = new Intl.NumberFormat('en-NZ', {
  style: 'currency',
  currency: 'NZD',
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

const number = new Intl.NumberFormat('en-NZ');

/** A date-time string carrying no zone: `2026-09-05T03:29:52`, with optional fraction. */
const NAIVE_TIMESTAMP = /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?$/;

/**
 * Parse a timestamp from the API, which stores and returns naive UTC.
 *
 * `new Date('2026-09-05T03:29:52')` is read as the *viewer's* local time, so
 * the same row lands on a different instant depending on where the browser is.
 * In a UTC container that coincidence hides the bug; in Auckland — where this
 * actually runs — every naive timestamp would be read 12 or 13 hours early,
 * throwing off the NZ times the send windows are stated in. Anything that
 * already carries a zone or offset is left exactly as it is.
 */
function parseTimestamp(value: string): Date {
  return new Date(NAIVE_TIMESTAMP.test(value) ? `${value}Z` : value);
}

export function formatCurrency(value: number | null | undefined, precise = false): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  return precise ? currencyPrecise.format(value) : currency.format(value);
}

export function formatNumber(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  return number.format(value);
}

export function formatPercent(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  return `${(value * 100).toFixed(digits)}%`;
}

export function formatDate(value: string | null | undefined): string {
  if (!value) return '—';
  const date = parseTimestamp(value);
  if (Number.isNaN(date.getTime())) return '—';
  return date.toLocaleDateString('en-NZ', { day: '2-digit', month: 'short', year: 'numeric' });
}

export function formatDateTime(value: string | null | undefined): string {
  if (!value) return '—';
  const date = parseTimestamp(value);
  if (Number.isNaN(date.getTime())) return '—';
  return date.toLocaleString('en-NZ', {
    day: '2-digit',
    month: 'short',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

/** The business timezone every send decision is made in. */
export const BUSINESS_TIMEZONE = 'Pacific/Auckland';

/**
 * Render a timestamp in New Zealand time, whatever the viewer's own clock is.
 *
 * Send windows, quiet hours and per-day capping are all decided in NZ time, so
 * a screen labelled "NZ" has to show NZ — formatting in the browser's timezone
 * would tell an operator in another country, or a CI container running in UTC,
 * that an 18:00 send goes out at 06:00.
 */
export function formatBusinessTime(value: string | null | undefined): string {
  if (!value) return '—';
  const date = parseTimestamp(value);
  if (Number.isNaN(date.getTime())) return '—';
  return date.toLocaleString('en-NZ', {
    timeZone: BUSINESS_TIMEZONE,
    day: '2-digit',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit',
  });
}

export function formatRelative(value: string | null | undefined): string {
  if (!value) return '—';
  const date = parseTimestamp(value);
  if (Number.isNaN(date.getTime())) return '—';
  const seconds = Math.round((Date.now() - date.getTime()) / 1000);
  const units: [Intl.RelativeTimeFormatUnit, number][] = [
    ['year', 31536000],
    ['month', 2592000],
    ['day', 86400],
    ['hour', 3600],
    ['minute', 60],
  ];
  const formatter = new Intl.RelativeTimeFormat('en-NZ', { numeric: 'auto' });
  for (const [unit, secondsPerUnit] of units) {
    if (Math.abs(seconds) >= secondsPerUnit) {
      return formatter.format(-Math.round(seconds / secondsPerUnit), unit);
    }
  }
  return 'just now';
}

export function formatDays(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—';
  if (value === 0) return 'Today';
  return `${formatNumber(value)} ${value === 1 ? 'day' : 'days'}`;
}

/** Turn CONSTANT_CASE into Title Case for display. */
export function humanize(value: string | null | undefined): string {
  if (!value) return '—';
  return value
    .replace(/_/g, ' ')
    .toLowerCase()
    .replace(/\b\w/g, (c) => c.toUpperCase());
}
