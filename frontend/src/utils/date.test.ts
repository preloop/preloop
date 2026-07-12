import { expect } from '@open-wc/testing';
import {
  formatFutureRelativeTime,
  formatRelativeTime,
  parseUTCDate,
} from './date';

describe('date utilities', () => {
  it('parses naive backend timestamps as UTC', () => {
    const parsed = parseUTCDate('2026-07-12T20:00:00');

    expect(parsed.getTime()).to.equal(Date.UTC(2026, 6, 12, 20, 0, 0));
  });

  it('formats relative time from naive UTC timestamps', () => {
    const now = new Date('2026-07-12T23:00:00Z');

    expect(formatRelativeTime('2026-07-12T20:00:00', now)).to.equal('3h ago');
  });

  it('formats future expiry times as time remaining', () => {
    const now = new Date('2026-07-12T20:00:00Z');

    expect(formatFutureRelativeTime('2026-07-12T22:00:00', now)).to.equal(
      'in 2h'
    );
  });

  it('does not describe past expiry times as future', () => {
    const now = new Date('2026-07-12T20:00:00Z');

    expect(formatFutureRelativeTime('2026-07-12T19:59:00', now)).to.equal(
      'expired'
    );
  });
});
