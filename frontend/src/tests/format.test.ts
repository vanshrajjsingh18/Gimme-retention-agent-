import { describe, expect, it } from 'vitest';

import {
  formatBusinessTime,
  formatCurrency,
  formatDate,
  formatDays,
  formatNumber,
  formatPercent,
  humanize,
} from '../utils/format';

describe('formatCurrency', () => {
  it('formats NZD without cents by default', () => {
    expect(formatCurrency(1234.56)).toBe('$1,235');
  });

  it('keeps cents when precise', () => {
    expect(formatCurrency(1234.56, true)).toBe('$1,234.56');
  });

  it('renders an em dash for missing values rather than $0', () => {
    // A missing metric and a genuine zero mean different things to an operator.
    expect(formatCurrency(null)).toBe('—');
    expect(formatCurrency(undefined)).toBe('—');
    expect(formatCurrency(Number.NaN)).toBe('—');
    expect(formatCurrency(0)).toBe('$0');
  });
});

describe('formatNumber', () => {
  it('groups thousands', () => {
    expect(formatNumber(1000)).toBe('1,000');
    expect(formatNumber(1234567)).toBe('1,234,567');
  });

  it('distinguishes missing from zero', () => {
    expect(formatNumber(null)).toBe('—');
    expect(formatNumber(0)).toBe('0');
  });
});

describe('formatPercent', () => {
  it('renders a 0-1 rate as a percentage', () => {
    expect(formatPercent(0.4237)).toBe('42.4%');
    expect(formatPercent(1)).toBe('100.0%');
    expect(formatPercent(0)).toBe('0.0%');
  });

  it('honours the digits argument', () => {
    expect(formatPercent(0.00423, 2)).toBe('0.42%');
    expect(formatPercent(0.5, 0)).toBe('50%');
  });

  it('handles missing values', () => {
    expect(formatPercent(null)).toBe('—');
  });
});

describe('formatDays', () => {
  it('uses singular and plural correctly', () => {
    expect(formatDays(1)).toBe('1 day');
    expect(formatDays(2)).toBe('2 days');
  });

  it('says Today rather than 0 days', () => {
    expect(formatDays(0)).toBe('Today');
  });

  it('handles a customer with no orders', () => {
    expect(formatDays(null)).toBe('—');
  });
});

describe('formatDate', () => {
  it('formats an ISO date', () => {
    expect(formatDate('2025-06-15T10:30:00')).toMatch(/15 Jun 2025/);
  });

  it('rejects unparseable input instead of showing Invalid Date', () => {
    expect(formatDate('not-a-date')).toBe('—');
    expect(formatDate(null)).toBe('—');
  });
});

describe('humanize', () => {
  it('turns CONSTANT_CASE into Title Case', () => {
    expect(humanize('HIGH_VALUE')).toBe('High Value');
    expect(humanize('AT_RISK')).toBe('At Risk');
    expect(humanize('ENCOURAGE_SECOND_ORDER')).toBe('Encourage Second Order');
  });

  it('handles single words and empty input', () => {
    expect(humanize('VIP')).toBe('Vip');
    expect(humanize('')).toBe('—');
    expect(humanize(null)).toBe('—');
  });
});

describe('formatBusinessTime', () => {
  it('renders a UTC timestamp in New Zealand time', () => {
    // 06:00 UTC on 23 June is 6pm in Auckland (NZST, UTC+12).
    expect(formatBusinessTime('2026-06-23T06:00:00')).toContain('06:00 pm');
    expect(formatBusinessTime('2026-06-23T06:00:00')).toContain('23 Jun');
  });

  it('follows daylight saving', () => {
    // The same 06:00 UTC in January is 7pm (NZDT, UTC+13) — an hour later.
    expect(formatBusinessTime('2026-01-23T06:00:00')).toContain('07:00 pm');
  });

  it('respects an explicit offset rather than double-converting it', () => {
    expect(formatBusinessTime('2026-06-23T18:00:00+12:00')).toContain('06:00 pm');
  });

  it('is unaffected by the viewer being somewhere else', () => {
    // The same instant expressed three ways must render identically.
    const nz = formatBusinessTime('2026-06-23T18:00:00+12:00');
    expect(formatBusinessTime('2026-06-23T06:00:00Z')).toBe(nz);
    expect(formatBusinessTime('2026-06-23T02:00:00-04:00')).toBe(nz);
  });

  it('handles a missing or unparseable value', () => {
    expect(formatBusinessTime(null)).toBe('—');
    expect(formatBusinessTime('not a date')).toBe('—');
  });
});
