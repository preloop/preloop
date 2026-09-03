import { expect } from '@open-wc/testing';

import {
  budgetForecast,
  forecastEndLabel,
  periodEndFor,
  periodStartFor,
} from './budget-forecast';

/** Local time, so the assertions match what an operator's browser computes. */
function at(iso: string): Date {
  return new Date(iso);
}

describe('budget-forecast', () => {
  it('aligns weeks to Monday like the server does', () => {
    // 2026-09-03 is a Thursday.
    const start = periodStartFor('weekly', at('2026-09-03T15:00:00'))!;
    expect(start.getDay()).to.equal(1);
    expect(start.getDate()).to.equal(31);
    expect(start.getHours()).to.equal(0);
    const end = periodEndFor('weekly', at('2026-09-03T15:00:00'))!;
    expect(end.getDate()).to.equal(7);
  });

  it('projects the period end from spend so far', () => {
    // Half of September gone, $60 spent: $120 by the end of the month.
    const forecast = budgetForecast({
      period: 'monthly',
      spend: 60,
      now: at('2026-09-16T00:00:00'),
    })!;
    expect(forecast.amount).to.be.closeTo(120, 0.5);
    expect(forecast.tone).to.equal('neutral');
    expect(forecastEndLabel('monthly', forecast.end)).to.equal('Sep 30');
  });

  it('turns amber over the soft limit and red over the hard one', () => {
    const base = {
      period: 'monthly' as const,
      spend: 60,
      now: at('2026-09-16T00:00:00'),
    };
    expect(budgetForecast({ ...base, softLimit: 200 })!.tone).to.equal(
      'neutral'
    );
    expect(
      budgetForecast({ ...base, softLimit: 100, hardLimit: 300 })!.tone
    ).to.equal('warning');
    expect(
      budgetForecast({ ...base, softLimit: 100, hardLimit: 110 })!.tone
    ).to.equal('danger');
  });

  it('says nothing in the first tenth of a period', () => {
    // Two days into September is 6.7% of the month.
    expect(
      budgetForecast({
        period: 'monthly',
        spend: 60,
        now: at('2026-09-03T00:00:00'),
      })
    ).to.be.null;
    expect(
      budgetForecast({
        period: 'monthly',
        spend: 60,
        now: at('2026-09-05T00:00:00'),
      })
    ).to.not.be.null;
  });

  it('has nothing to project for all time budgets or zero spend', () => {
    expect(
      budgetForecast({
        period: 'all_time',
        spend: 60,
        now: at('2026-09-16T00:00:00'),
      })
    ).to.be.null;
    expect(
      budgetForecast({
        period: 'monthly',
        spend: 0,
        now: at('2026-09-16T00:00:00'),
      })
    ).to.be.null;
  });

  it('prefers the period the server reports', () => {
    // Server window: a 10 day period, 5 days in, $50 spent.
    const forecast = budgetForecast({
      period: 'monthly',
      spend: 50,
      periodStart: '2026-09-01T00:00:00Z',
      periodEnd: '2026-09-11T00:00:00Z',
      now: new Date('2026-09-06T00:00:00Z'),
    })!;
    expect(forecast.amount).to.be.closeTo(100, 0.5);
  });

  it('names the end of a daily period midnight, not tomorrow', () => {
    const end = periodEndFor('daily', at('2026-09-03T15:00:00'))!;
    expect(forecastEndLabel('daily', end)).to.equal('midnight');
  });

  it('names the last day of a server period on the server clock', () => {
    // The server cuts periods in UTC, so September ends at 2026-10-01T00:00Z.
    // One millisecond back is 30 September in UTC and 1 October in Tokyo:
    // read locally, the sentence would name a day in the next period.
    const forecast = budgetForecast({
      period: 'monthly',
      spend: 50,
      periodStart: '2026-09-01T00:00:00Z',
      periodEnd: '2026-10-01T00:00:00Z',
      now: new Date('2026-09-16T00:00:00Z'),
    })!;
    expect(forecast.endBasis).to.equal('utc');
    expect(
      forecastEndLabel('monthly', forecast.end, forecast.endBasis)
    ).to.equal('Sep 30');
    // The same instant named on the reader's clock is what used to be shown;
    // it is the wrong day east of UTC and this is what the basis prevents.
    const inclusive = new Date(forecast.end.getTime() - 1);
    expect(
      forecastEndLabel('monthly', forecast.end, forecast.endBasis)
    ).to.equal(
      inclusive.toLocaleDateString(undefined, {
        month: 'short',
        day: 'numeric',
        timeZone: 'UTC',
      })
    );
  });

  it('keeps naming a locally cut period on the local clock', () => {
    // No server bounds: the client aligned the period on the browser's own
    // clock, so the label has to follow that clock, not UTC.
    const forecast = budgetForecast({
      period: 'monthly',
      spend: 60,
      now: at('2026-09-16T00:00:00'),
    })!;
    expect(forecast.endBasis).to.equal('local');
    expect(
      forecastEndLabel('monthly', forecast.end, forecast.endBasis)
    ).to.equal('Sep 30');
  });

  it("only calls a day midnight when it ends at the reader's midnight", () => {
    const local = periodEndFor('daily', at('2026-09-03T15:00:00'))!;
    expect(forecastEndLabel('daily', local, 'local')).to.equal('midnight');
    // A UTC-aligned day seen from a timezone with an offset ends at some
    // other hour of the reader's day, and the label says which.
    const utcDayEnd = new Date('2026-09-04T00:00:00Z');
    const expected =
      utcDayEnd.getHours() === 0 &&
      utcDayEnd.getMinutes() === 0 &&
      utcDayEnd.getSeconds() === 0
        ? 'midnight'
        : utcDayEnd.toLocaleTimeString(undefined, {
            hour: 'numeric',
            minute: '2-digit',
          });
    expect(forecastEndLabel('daily', utcDayEnd, 'utc')).to.equal(expected);
  });
});
