import { expect } from '@open-wc/testing';

import {
  formatUsd,
  formatUsdExact,
  formatCurrencyAmount,
  formatCurrencyAmountExact,
  formatCurrencyCents,
} from './money';

describe('formatUsd', () => {
  it('formats dollars with cents and thousands separators', () => {
    expect(formatUsd(12345.678)).to.equal('$12,345.68');
    expect(formatUsd(3.5)).to.equal('$3.50');
    expect(formatUsd(1000000)).to.equal('$1,000,000.00');
  });

  it('prints zero, missing and non-numeric values as $0.00', () => {
    expect(formatUsd(0)).to.equal('$0.00');
    expect(formatUsd(null)).to.equal('$0.00');
    expect(formatUsd(undefined)).to.equal('$0.00');
    expect(formatUsd(Number.NaN)).to.equal('$0.00');
  });

  it('says "< $0.01" for a positive amount below a cent', () => {
    expect(formatUsd(0.000123)).to.equal('< $0.01');
    expect(formatUsd(0.0099)).to.equal('< $0.01');
    expect(formatUsd(0.01)).to.equal('$0.01');
  });

  it('keeps the sign of a negative amount', () => {
    expect(formatUsd(-1234.5)).to.equal('-$1,234.50');
  });
});

describe('formatUsdExact', () => {
  it('keeps up to six decimals for a tooltip', () => {
    expect(formatUsdExact(0.000123)).to.equal('$0.000123');
    expect(formatUsdExact(12345.6)).to.equal('$12,345.60');
    expect(formatUsdExact(null)).to.equal('$0.00');
  });
});

describe('provider currencies', () => {
  it('keeps cents conversion and USD display consistent', () => {
    expect(formatCurrencyCents(1234567)).to.equal('$12,345.67');
    expect(formatCurrencyCents(null)).to.equal('Unavailable');
    expect(formatCurrencyCents(Number.NaN)).to.equal('Unavailable');
    expect(formatCurrencyCents(100, 'invalid-currency')).to.equal(
      'Unavailable'
    );
    expect(formatCurrencyAmount(0.000123, 'usd')).to.equal('< $0.01');
  });
  it('preserves non-dollar invoice denomination and exact values', () => {
    expect(formatCurrencyAmount(12345.678, 'EUR')).to.equal('€12,345.68');
    expect(formatCurrencyAmountExact(0.0125, 'EUR')).to.equal('€0.0125');
    expect(formatCurrencyAmountExact(0.0125, 'USD')).to.equal('$0.0125');
  });
});
