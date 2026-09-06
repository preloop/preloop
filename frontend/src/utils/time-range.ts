/**
 * The one place the console works out what a range means.
 *
 * Four sibling pages used to compute their own "30 days" and disagreed about
 * the answer: the Overview asked for one calendar month back, Cost for the
 * last 30 x 24 hours, API usage and the model detail page for the 30 local
 * calendar days ending tonight. The same account then read 18.9K, 18,306,
 * 18,504 and 18,419 requests for the same nominal window, which is a bug in
 * the arithmetic, not in the data. Every page now resolves its window here,
 * so the same key asks the server the same question.
 *
 * A rolling key ("30d") is exactly N x 24 hours ending now. A calendar key
 * ("This month") runs from the start of the period to now. `all` has no
 * bounds at all.
 */

export type TimeRangeKey =
  | 'today'
  | 'this-week'
  | 'this-month'
  | 'last-month'
  | 'last-24h'
  | 'last-7'
  | 'last-30'
  | 'last-90'
  | 'last-365'
  | 'all';

export interface TimeRangeWindow {
  /** ISO 8601 start, or null when the range has no lower bound. */
  startDate: string | null;
  /** ISO 8601 end, or null when the range has no upper bound. */
  endDate: string | null;
}

/** How many days each rolling key covers. */
const ROLLING_DAYS: Partial<Record<TimeRangeKey, number>> = {
  'last-24h': 1,
  'last-7': 7,
  'last-30': 30,
  'last-90': 90,
  'last-365': 365,
};

/**
 * The spellings the console grew before the math was shared. The Overview and
 * the agent detail strip say `day`/`week`/`month`/`year`; the chips say
 * `24h`/`7d`/`30d`/`1y`; Cost says `last-30`.
 */
const KEY_ALIASES: Record<string, TimeRangeKey> = {
  day: 'last-24h',
  week: 'last-7',
  month: 'last-30',
  quarter: 'last-90',
  year: 'last-365',
  '24h': 'last-24h',
  '7d': 'last-7',
  '30d': 'last-30',
  '90d': 'last-90',
  '1y': 'last-365',
  'last-1y': 'last-365',
};

const KNOWN_KEYS: TimeRangeKey[] = [
  'today',
  'this-week',
  'this-month',
  'last-month',
  'last-24h',
  'last-7',
  'last-30',
  'last-90',
  'last-365',
  'all',
];

/** Empty or missing keys resolve to this window. Unknown keys do not. */
const DEFAULT_TIME_RANGE_KEY: TimeRangeKey = 'last-30';

/** The short label a stat carries beside its name ("$ est. · 30d"). */
const SHORT_LABELS: Record<TimeRangeKey, string> = {
  today: 'today',
  'this-week': 'this week',
  'this-month': 'this month',
  'last-month': 'last month',
  'last-24h': '24h',
  'last-7': '7d',
  'last-30': '30d',
  'last-90': '90d',
  'last-365': '1y',
  all: 'all time',
};

/**
 * Read a range key written in any of the console's spellings. Returns null
 * for anything this module does not know, so a caller can fall back rather
 * than silently resolve the wrong window.
 */
export function normalizeTimeRangeKey(
  value: string | null | undefined
): TimeRangeKey | null {
  if (!value) return null;
  const key = value.trim();
  if ((KNOWN_KEYS as string[]).includes(key)) return key as TimeRangeKey;
  return KEY_ALIASES[key] ?? null;
}

/**
 * Empty and missing keys keep the historical last-30 default. A key this
 * module does not know is rejected so a typo cannot silently ask for 30 days.
 */
function coerceTimeRangeKey(
  key: TimeRangeKey | string | null | undefined
): TimeRangeKey {
  if (key == null) return DEFAULT_TIME_RANGE_KEY;
  const raw = String(key).trim();
  if (!raw) return DEFAULT_TIME_RANGE_KEY;
  const normalized = normalizeTimeRangeKey(raw);
  if (!normalized) {
    throw new RangeError(`Unrecognized time range key: ${raw}`);
  }
  return normalized;
}

/** The window a key covers, ending at `now`. */
export function resolveTimeRange(
  key: TimeRangeKey | string,
  now: Date = new Date()
): TimeRangeWindow {
  const normalized = coerceTimeRangeKey(key);
  if (normalized === 'all') {
    return { startDate: null, endDate: null };
  }

  const start = new Date(now.getTime());
  const end = new Date(now.getTime());

  if (normalized === 'today') {
    start.setHours(0, 0, 0, 0);
  } else if (normalized === 'this-week') {
    // Weeks start on Monday, the way the rest of the console counts them.
    const daysSinceMonday = (start.getDay() + 6) % 7;
    start.setDate(start.getDate() - daysSinceMonday);
    start.setHours(0, 0, 0, 0);
  } else if (normalized === 'this-month') {
    start.setDate(1);
    start.setHours(0, 0, 0, 0);
  } else if (normalized === 'last-month') {
    start.setMonth(start.getMonth() - 1, 1);
    start.setHours(0, 0, 0, 0);
    end.setDate(1);
    end.setHours(0, 0, 0, 0);
  } else {
    start.setDate(start.getDate() - (ROLLING_DAYS[normalized] ?? 30));
  }

  return { startDate: start.toISOString(), endDate: end.toISOString() };
}

/**
 * The window immediately before the one a key resolves to, for "vs prior 30d"
 * deltas. A calendar key steps back a whole period; a rolling key steps back
 * its own duration.
 *
 * The two shapes are deliberately different and both are correct.
 * `this-week`, `this-month` and `last-month` compare whole periods: the
 * previous window ends exactly where the current one starts. `today` and the
 * rolling keys compare equal durations: a window that is 9 hours old is
 * measured against the same 9 hours of the day before, not against a whole
 * day it would always lose to.
 */
export function resolvePreviousTimeRange(
  key: TimeRangeKey | string,
  now: Date = new Date()
): TimeRangeWindow {
  const normalized = coerceTimeRangeKey(key);
  if (normalized === 'all') {
    return { startDate: null, endDate: null };
  }

  const current = resolveTimeRange(normalized, now);
  const start = new Date(current.startDate as string);
  const end = new Date(current.endDate as string);

  if (normalized === 'today') {
    const durationMs = end.getTime() - start.getTime();
    const previousStart = new Date(start.getTime());
    previousStart.setDate(previousStart.getDate() - 1);
    return {
      startDate: previousStart.toISOString(),
      endDate: new Date(previousStart.getTime() + durationMs).toISOString(),
    };
  }

  if (normalized === 'this-week') {
    const previousStart = new Date(start.getTime());
    previousStart.setDate(previousStart.getDate() - 7);
    return {
      startDate: previousStart.toISOString(),
      endDate: start.toISOString(),
    };
  }

  if (normalized === 'this-month' || normalized === 'last-month') {
    const previousStart = new Date(start.getTime());
    previousStart.setMonth(previousStart.getMonth() - 1, 1);
    return {
      startDate: previousStart.toISOString(),
      endDate: start.toISOString(),
    };
  }

  const durationMs = end.getTime() - start.getTime();
  return {
    startDate: new Date(start.getTime() - durationMs).toISOString(),
    endDate: start.toISOString(),
  };
}

/** "30d" for a stat label, "this month" for a calendar key. */
export function timeRangeShortLabel(key: TimeRangeKey | string): string {
  const normalized = normalizeTimeRangeKey(String(key));
  return normalized ? SHORT_LABELS[normalized] : '30d';
}

/**
 * Which days the numbers cover, for the line beside the range control:
 * "Aug 7 to Sep 6". Empty when the window has no bounds to state.
 */
export function formatTimeRangeWindow(
  window:
    TimeRangeWindow | { startDate?: string | null; endDate?: string | null }
): string {
  const start = window.startDate ? new Date(window.startDate) : null;
  const end = window.endDate ? new Date(window.endDate) : null;
  if (!start || !end) return '';
  if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime())) return '';
  const day = (date: Date) =>
    date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
  return `${day(start)} to ${day(end)}`;
}
