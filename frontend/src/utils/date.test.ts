import { expect } from '@open-wc/testing';
import {
  calculateDuration,
  formatDurationBetween,
  formatAbsoluteTime,
  formatFutureRelativeTime,
  formatRelativeOrFuture,
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

  it('stays relative however old the timestamp is by default', () => {
    const now = new Date('2026-10-07T20:00:00Z');

    expect(formatRelativeTime('2026-10-01T20:00:00', now)).to.equal('6d ago');
    expect(formatRelativeTime('2026-08-26T20:00:00', now)).to.equal('6w ago');
    expect(formatRelativeTime('2024-10-01T20:00:00', now)).to.equal('2y ago');
  });

  it('stays relative up to the caller window and then shows the date', () => {
    const now = new Date('2026-07-12T20:00:00Z');
    const options = { maxRelativeDays: 30 };

    expect(formatRelativeTime('2026-07-04T20:00:00', now, options)).to.equal(
      '8d ago'
    );
    expect(formatRelativeTime('2026-06-20T20:00:00', now, options)).to.equal(
      '22d ago'
    );
    expect(formatRelativeTime('2026-06-01T20:00:00', now, options)).to.equal(
      new Date('2026-06-01T20:00:00Z').toLocaleDateString()
    );
  });

  it('counts weeks and years for pages that stay relative', () => {
    const now = new Date('2026-09-02T20:00:00Z');
    const options = { maxRelativeDays: Infinity, withSuffix: false };

    expect(formatRelativeTime('2026-07-13T20:00:00', now, options)).to.equal(
      '7w'
    );
    expect(formatRelativeTime('2024-09-02T20:00:00', now, options)).to.equal(
      '2y'
    );
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

  it('keeps far future times relative in weeks and years', () => {
    const now = new Date('2026-07-12T20:00:00Z');

    expect(formatFutureRelativeTime('2026-07-22T20:00:00', now)).to.equal(
      'in 10d'
    );
    expect(formatFutureRelativeTime('2026-09-12T20:00:00', now)).to.equal(
      'in 8w'
    );
    expect(formatFutureRelativeTime('2027-07-12T20:00:00', now)).to.equal(
      'in 1y'
    );
  });
});

describe('formatRelativeOrFuture', () => {
  const now = new Date('2026-07-12T20:00:00Z');

  it('reads past times as ages and future times as time remaining', () => {
    expect(formatRelativeOrFuture('2026-07-12T17:00:00', now)).to.equal(
      '3h ago'
    );
    expect(formatRelativeOrFuture('2026-07-15T20:00:00', now)).to.equal(
      'in 3d'
    );
  });
});

describe('formatAbsoluteTime', () => {
  it('renders one absolute format with date and minutes', () => {
    const value = '2026-07-12T20:05:00Z';
    expect(formatAbsoluteTime(value)).to.equal(
      new Date(value).toLocaleString(undefined, {
        dateStyle: 'medium',
        timeStyle: 'short',
      })
    );
  });

  it('returns an empty string for missing or invalid values', () => {
    expect(formatAbsoluteTime(null)).to.equal('');
    expect(formatAbsoluteTime('not a date')).to.equal('');
  });
});

describe('formatDurationBetween', () => {
  it('formats sub-minute spans in seconds', () => {
    expect(
      formatDurationBetween('2026-08-09T10:00:00Z', '2026-08-09T10:00:25Z')
    ).to.equal('25s');
  });

  it('formats zero-length spans as 0s', () => {
    expect(
      formatDurationBetween('2026-08-09T10:00:00Z', '2026-08-09T10:00:00Z')
    ).to.equal('0s');
  });

  it('formats sub-hour spans in minutes and seconds', () => {
    expect(
      formatDurationBetween('2026-08-09T10:00:00Z', '2026-08-09T10:04:32Z')
    ).to.equal('4m 32s');
  });

  it('formats spans over an hour in hours and minutes', () => {
    expect(
      formatDurationBetween('2026-08-09T10:00:00Z', '2026-08-09T11:05:00Z')
    ).to.equal('1h 5m');
  });

  it('keeps multi-day spans in hours rather than rolling into days', () => {
    expect(
      formatDurationBetween('2026-08-09T10:00:00Z', '2026-08-10T12:00:00Z')
    ).to.equal('26h 0m');
  });

  it('treats naive backend strings as UTC on both ends', () => {
    expect(
      formatDurationBetween('2026-08-09 10:00:00', '2026-08-09 10:04:32')
    ).to.equal('4m 32s');
  });

  it('returns an empty string when the start time is missing', () => {
    expect(formatDurationBetween(null, '2026-08-09T10:04:32Z')).to.equal('');
    expect(formatDurationBetween(undefined, '2026-08-09T10:04:32Z')).to.equal(
      ''
    );
    expect(formatDurationBetween('', '2026-08-09T10:04:32Z')).to.equal('');
  });

  it('returns an empty string when the end precedes the start', () => {
    expect(
      formatDurationBetween('2026-08-09T10:05:00Z', '2026-08-09T10:00:00Z')
    ).to.equal('');
  });

  it('returns an empty string for unparseable timestamps', () => {
    expect(
      formatDurationBetween('not-a-date', '2026-08-09T10:04:32Z')
    ).to.equal('');
  });

  it('never emits NaN when the end timestamp is garbage', () => {
    const now = new Date('2026-08-09T10:00:30Z');

    expect(
      formatDurationBetween('2026-08-09T10:00:00Z', 'garbage', now)
    ).to.equal('30s');
  });

  it('measures elapsed time against now when there is no end time', () => {
    const now = new Date('2026-08-09T10:07:05Z');

    expect(formatDurationBetween('2026-08-09T10:00:00Z', null, now)).to.equal(
      '7m 5s'
    );
    expect(
      formatDurationBetween('2026-08-09T10:00:00Z', undefined, now)
    ).to.equal('7m 5s');
  });

  it('keeps calculateDuration output identical', () => {
    expect(
      calculateDuration('2026-08-09T10:00:00Z', '2026-08-09T10:04:32Z')
    ).to.equal('4m 32s');
    expect(
      calculateDuration('2026-08-09T10:00:00Z', '2026-08-09T11:05:00Z')
    ).to.equal('1h 5m');
    expect(
      calculateDuration('2026-08-09T10:00:00Z', '2026-08-09T10:00:25Z')
    ).to.equal('25s');
  });
});
