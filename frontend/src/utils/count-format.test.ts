import { expect } from '@open-wc/testing';
import { formatCount, formatCountExact } from './count-format';

describe('formatCount (U-01)', () => {
  it('keeps counts under 1000 whole and compacts the rest', () => {
    expect(formatCount(0)).to.equal('0');
    expect(formatCount(999)).to.equal('999');
    expect(formatCount(1998)).to.equal('2K');
    expect(formatCount(70196)).to.equal('70.2K');
    expect(formatCount(272950)).to.equal('273K');
    expect(formatCount(1202523)).to.equal('1.2M');
    expect(formatCount(null)).to.equal('0');
  });

  it('states the exact figure for a title', () => {
    expect(formatCountExact(272950)).to.equal('272,950');
  });
});
