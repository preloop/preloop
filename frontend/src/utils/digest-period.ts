/** Validated exact UTC windows retain microseconds instead of Date rounding. */
export type DigestPeriod = { startDate: string; endDate: string };
export type DigestLink = {
  period: DigestPeriod | null;
  accountId: string | null;
  error: string | null;
  blocked: boolean;
};

function utcKey(value: string): string | null {
  const match =
    /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?Z$/.exec(
      value
    );
  if (!match) return null;
  const [, year, month, day, hour, minute, second, fraction = ''] = match;
  const date = new Date(`${year}-${month}-${day}T${hour}:${minute}:${second}Z`);
  if (
    !Number.isFinite(date.getTime()) ||
    date.toISOString().slice(0, 19) !== value.slice(0, 19)
  )
    return null;
  return value.slice(0, 19) + '.' + fraction.padEnd(6, '0');
}

export function parseDigestLink(search: string): DigestLink {
  const params = new URLSearchParams(search);
  const accounts = params.getAll('account_id');
  const starts = params.getAll('start_date');
  const ends = params.getAll('end_date');
  const result: DigestLink = {
    period: null,
    accountId: null,
    error: null,
    blocked: false,
  };
  if (accounts.length) {
    if (
      accounts.length !== 1 ||
      !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(
        accounts[0]
      )
    ) {
      return {
        ...result,
        error: 'Invalid digest account link.',
        blocked: true,
      };
    }
    result.accountId = accounts[0].toLowerCase();
  }
  if (!starts.length && !ends.length && !accounts.length) return result;
  const start = starts.length === 1 ? utcKey(starts[0]) : null;
  const end = ends.length === 1 ? utcKey(ends[0]) : null;
  if (!start || !end || start >= end) {
    return {
      ...result,
      error: 'Invalid digest date range. Showing your saved period.',
    };
  }
  return { ...result, period: { startDate: starts[0], endDate: ends[0] } };
}

export function withoutDigestPeriod(href: string): string {
  const url = new URL(href, window.location.origin);
  for (const name of ['account_id', 'start_date', 'end_date'])
    url.searchParams.delete(name);
  return url.pathname + url.search + url.hash;
}
