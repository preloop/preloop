import { expect } from '@open-wc/testing';

import {
  formatTimeRangeWindow,
  normalizeTimeRangeKey,
  resolvePreviousTimeRange,
  resolveTimeRange,
  timeRangeShortLabel,
} from './time-range';

describe('time-range', () => {
  // A fixed local instant, so the assertions below are about the math and not
  // about the day the suite runs.
  const now = new Date(2026, 8, 6, 14, 30, 0); // 6 Sep 2026, 14:30 local

  it('reads every spelling the console grew for the same window', () => {
    expect(normalizeTimeRangeKey('month')).to.equal('last-30');
    expect(normalizeTimeRangeKey('30d')).to.equal('last-30');
    expect(normalizeTimeRangeKey('last-30')).to.equal('last-30');
    expect(normalizeTimeRangeKey('day')).to.equal('last-24h');
    expect(normalizeTimeRangeKey('1y')).to.equal('last-365');
    expect(normalizeTimeRangeKey('nonsense')).to.equal(null);
    expect(normalizeTimeRangeKey('')).to.equal(null);
  });

  it('resolves the same window for every spelling of 30 days', () => {
    const canonical = resolveTimeRange('last-30', now);
    expect(resolveTimeRange('month', now)).to.deep.equal(canonical);
    expect(resolveTimeRange('30d', now)).to.deep.equal(canonical);
    expect(new Date(canonical.startDate as string).getTime()).to.equal(
      new Date(2026, 7, 7, 14, 30, 0).getTime()
    );
    expect(new Date(canonical.endDate as string).getTime()).to.equal(
      now.getTime()
    );
  });

  it('measures rolling ranges in whole days back from now', () => {
    const day = 24 * 60 * 60 * 1000;
    for (const [key, days] of [
      ['last-24h', 1],
      ['last-7', 7],
      ['last-90', 90],
      ['last-365', 365],
    ] as Array<[string, number]>) {
      const window = resolveTimeRange(key, now);
      const span =
        new Date(window.endDate as string).getTime() -
        new Date(window.startDate as string).getTime();
      // Whole days back from the same instant; DST can move the clock by an
      // hour, which is why this is a tolerance and not an equality.
      expect(Math.abs(span - days * day)).to.be.at.most(day);
    }
  });

  it('runs calendar ranges from the start of the period to now', () => {
    const today = resolveTimeRange('today', now);
    expect(new Date(today.startDate as string).getTime()).to.equal(
      new Date(2026, 8, 6, 0, 0, 0, 0).getTime()
    );

    const thisMonth = resolveTimeRange('this-month', now);
    expect(new Date(thisMonth.startDate as string).getTime()).to.equal(
      new Date(2026, 8, 1, 0, 0, 0, 0).getTime()
    );

    // 6 Sep 2026 is a Sunday, so the week started on 31 Aug.
    const thisWeek = resolveTimeRange('this-week', now);
    expect(new Date(thisWeek.startDate as string).getTime()).to.equal(
      new Date(2026, 7, 31, 0, 0, 0, 0).getTime()
    );

    const lastMonth = resolveTimeRange('last-month', now);
    expect(new Date(lastMonth.startDate as string).getTime()).to.equal(
      new Date(2026, 7, 1, 0, 0, 0, 0).getTime()
    );
    expect(new Date(lastMonth.endDate as string).getTime()).to.equal(
      new Date(2026, 8, 1, 0, 0, 0, 0).getTime()
    );
  });

  it('has no bounds for all time', () => {
    expect(resolveTimeRange('all', now)).to.deep.equal({
      startDate: null,
      endDate: null,
    });
    expect(resolvePreviousTimeRange('all', now)).to.deep.equal({
      startDate: null,
      endDate: null,
    });
  });

  it('puts the prior window immediately before the current one', () => {
    const current = resolveTimeRange('last-30', now);
    const previous = resolvePreviousTimeRange('last-30', now);
    expect(previous.endDate).to.equal(current.startDate);
    const currentSpan =
      new Date(current.endDate as string).getTime() -
      new Date(current.startDate as string).getTime();
    const previousSpan =
      new Date(previous.endDate as string).getTime() -
      new Date(previous.startDate as string).getTime();
    expect(previousSpan).to.equal(currentSpan);

    // A calendar month steps back a whole month, not 30 days.
    const previousMonth = resolvePreviousTimeRange('this-month', now);
    expect(new Date(previousMonth.startDate as string).getTime()).to.equal(
      new Date(2026, 7, 1, 0, 0, 0, 0).getTime()
    );
    expect(previousMonth.endDate).to.equal(
      resolveTimeRange('this-month', now).startDate
    );
  });

  it('labels a window in the register the stats use', () => {
    expect(timeRangeShortLabel('month')).to.equal('30d');
    expect(timeRangeShortLabel('this-month')).to.equal('this month');
    expect(timeRangeShortLabel('nonsense')).to.equal('30d');
    expect(formatTimeRangeWindow(resolveTimeRange('last-30', now))).to.equal(
      `${new Date(2026, 7, 7).toLocaleDateString(undefined, {
        month: 'short',
        day: 'numeric',
      })} to ${new Date(2026, 8, 6).toLocaleDateString(undefined, {
        month: 'short',
        day: 'numeric',
      })}`
    );
    expect(formatTimeRangeWindow({ startDate: null, endDate: null })).to.equal(
      ''
    );
  });
});
