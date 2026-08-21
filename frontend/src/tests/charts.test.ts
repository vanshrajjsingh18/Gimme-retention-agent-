import { describe, expect, it } from 'vitest';

import {
  countFormatter,
  currencyFormatter,
  humanizeLabel,
  labelFormatter,
  namedCountFormatter,
  percentFormatter,
} from '../utils/charts';

/**
 * Recharts hands formatters `ValueType | undefined` and `ReactNode`, so these
 * helpers exist to absorb values that are not the type the chart data implies.
 * These tests pin that behaviour, since a throw inside a formatter blanks the
 * whole chart at runtime.
 */
describe('chart formatters', () => {
  it('formats counts with a fixed series label', () => {
    expect(countFormatter('Customers')(1234)).toEqual(['1,234', 'Customers']);
  });

  it('coerces a string value rather than throwing', () => {
    expect(countFormatter('Customers')('42')).toEqual(['42', 'Customers']);
  });

  it('treats undefined and non-numeric values as zero', () => {
    expect(countFormatter('Customers')(undefined)).toEqual(['0', 'Customers']);
    expect(countFormatter('Customers')(null)).toEqual(['0', 'Customers']);
    expect(countFormatter('Customers')('nope')).toEqual(['0', 'Customers']);
  });

  it('formats currency', () => {
    expect(currencyFormatter('Revenue')(2500)).toEqual(['$2,500', 'Revenue']);
    expect(currencyFormatter('Revenue', true)(2500.5)).toEqual(['$2,500.50', 'Revenue']);
  });

  it('formats percentages', () => {
    expect(percentFormatter('Open rate')(0.42)).toEqual(['42.0%', 'Open rate']);
    expect(percentFormatter('Rate', 0)(0.5)).toEqual(['50%', 'Rate']);
  });

  it('names the series from the datum key', () => {
    expect(namedCountFormatter()(10, 'repeat')).toEqual(['10', 'Repeat']);
    expect(namedCountFormatter()(10, 'HIGH_VALUE')).toEqual(['10', 'High Value']);
  });

  it('humanizes axis and legend labels of any node type', () => {
    expect(humanizeLabel('AT_RISK')).toBe('At Risk');
    expect(humanizeLabel(undefined)).toBe('—');
    expect(humanizeLabel(5)).toBe('5');
  });

  it('applies a custom label template', () => {
    const format = labelFormatter((l) => `Score ${l}`);
    expect(format('45-70')).toBe('Score 45-70');
    expect(format(undefined)).toBe('Score ');
  });
});
