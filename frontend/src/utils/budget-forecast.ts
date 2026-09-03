/**
 * Straight-line budget forecast (wave 8).
 *
 * A budget row says what has been spent so far this period. On the 3rd of the
 * month "$12 of $300" reads as comfortable whether the account is on course
 * for $120 or for $1,200. The forecast turns spend-to-date into the number the
 * operator actually cares about: what this period ends at if the rest of it
 * looks like the part already spent.
 *
 * The projection is deliberately linear. Spend is bursty and this card is not
 * the place for a model with opinions; a straight line is the one rule an
 * operator can carry in their head and check by hand.
 */

/** Budget periods the forecast understands. `all_time` has no end to aim at. */
export type ForecastPeriod =
  'hourly' | 'daily' | 'weekly' | 'monthly' | 'yearly' | 'all_time';

/**
 * Below this the sample is too short to project from: one expensive minute
 * after midnight would forecast a five figure day.
 */
export const MIN_ELAPSED_FRACTION = 0.1;

export interface BudgetForecastInput {
  period: string;
  spend: number;
  softLimit?: number | null;
  hardLimit?: number | null;
  /** Period bounds from the server, when it sends them. ISO strings. */
  periodStart?: string | null;
  periodEnd?: string | null;
  now?: Date;
}

export interface BudgetForecast {
  /** Projected spend at the end of the period. */
  amount: number;
  /** Fraction of the period already gone, 0 to 1. */
  elapsedFraction: number;
  /** Exclusive end of the period. */
  end: Date;
  /** 'neutral' under both limits, 'warning' over the soft one, 'danger' over the hard one. */
  tone: 'neutral' | 'warning' | 'danger';
}

/**
 * Start of the period containing `now`, mirroring the server's alignment
 * (`get_period_start` in `models/crud/budget.py`): weeks start on Monday,
 * months on the 1st, years on 1 January.
 */
export function periodStartFor(period: string, now: Date): Date | null {
  const start = new Date(now.getTime());
  start.setMilliseconds(0);
  start.setSeconds(0);
  if (period === 'hourly') {
    start.setMinutes(0);
    return start;
  }
  start.setMinutes(0);
  start.setHours(0);
  if (period === 'daily') return start;
  if (period === 'weekly') {
    // getDay() is 0 for Sunday; the server counts weeks from Monday.
    const daysSinceMonday = (start.getDay() + 6) % 7;
    start.setDate(start.getDate() - daysSinceMonday);
    return start;
  }
  if (period === 'monthly') {
    start.setDate(1);
    return start;
  }
  if (period === 'yearly') {
    start.setMonth(0, 1);
    return start;
  }
  return null;
}

/** Exclusive end of the period containing `now`. */
export function periodEndFor(period: string, now: Date): Date | null {
  const start = periodStartFor(period, now);
  if (!start) return null;
  const end = new Date(start.getTime());
  if (period === 'hourly') end.setHours(end.getHours() + 1);
  else if (period === 'daily') end.setDate(end.getDate() + 1);
  else if (period === 'weekly') end.setDate(end.getDate() + 7);
  else if (period === 'monthly') end.setMonth(end.getMonth() + 1);
  else if (period === 'yearly') end.setFullYear(end.getFullYear() + 1);
  return end;
}

/**
 * Project this period's finishing spend, or null when there is nothing
 * honest to say: an open-ended budget, a period barely started, or no spend
 * at all.
 */
export function budgetForecast(
  input: BudgetForecastInput
): BudgetForecast | null {
  const now = input.now ?? new Date();
  if (input.period === 'all_time') return null;

  const parsed = (value?: string | null): Date | null => {
    if (!value) return null;
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? null : date;
  };
  const start = parsed(input.periodStart) ?? periodStartFor(input.period, now);
  const end = parsed(input.periodEnd) ?? periodEndFor(input.period, now);
  if (!start || !end) return null;

  const span = end.getTime() - start.getTime();
  if (span <= 0) return null;
  const elapsedFraction = (now.getTime() - start.getTime()) / span;
  if (elapsedFraction < MIN_ELAPSED_FRACTION) return null;
  // A clock skew or a stale period would otherwise deflate the forecast.
  if (elapsedFraction > 1) return null;

  const spend = Number(input.spend || 0);
  if (spend <= 0) return null;

  const amount = spend / elapsedFraction;
  const soft = Number(input.softLimit || 0);
  const hard = Number(input.hardLimit || 0);
  let tone: BudgetForecast['tone'] = 'neutral';
  if (soft > 0 && amount > soft) tone = 'warning';
  if (hard > 0 && amount > hard) tone = 'danger';

  return { amount, elapsedFraction, end, tone };
}

/**
 * How the end of the period is named in the sentence. Dates for the long
 * periods, a clock time for the hourly one, "midnight" for the daily one:
 * "by Sep 1" for today's budget would be read as tomorrow.
 */
export function forecastEndLabel(period: string, end: Date): string {
  if (period === 'hourly') {
    return end.toLocaleTimeString(undefined, {
      hour: 'numeric',
      minute: '2-digit',
    });
  }
  if (period === 'daily') return 'midnight';
  // The exclusive end is the first instant of the next period; the operator
  // thinks in terms of the last day inside it.
  const inclusive = new Date(end.getTime() - 1);
  return inclusive.toLocaleDateString(undefined, {
    month: 'short',
    day: 'numeric',
  });
}
