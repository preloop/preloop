/**
 * How long a flow has been failing, from its recent runs.
 *
 * A tracker can pass its own connection check while every run of a flow
 * fails to clone from it, so the flow page says so itself: the newest runs,
 * counted back to the first one that did not fail.
 */

/** The fields of a run this needs; the list endpoint returns all of them. */
export interface StreakRun {
  status: string;
  start_time?: string | null;
  failure_category?: string | null;
}

export interface FailureStreak {
  /** Consecutive failed runs, newest first. */
  count: number;
  /** Start time of the oldest run in the streak. */
  since: string | null;
  /** The category when every run in the streak shares one, else null. */
  category: string | null;
}

const FAILED_STATUSES = new Set(['FAILED', 'ERROR', 'TIMEOUT', 'TIMED_OUT']);

/** Fewer failures in a row than this is noise, not a streak. */
export const MIN_FAILURE_STREAK = 2;

/**
 * The run of failures at the head of `runs` (sorted newest first), or null
 * when the newest run did not fail or the streak is shorter than
 * {@link MIN_FAILURE_STREAK}.
 */
export function failureStreak(
  runs: readonly StreakRun[]
): FailureStreak | null {
  const streak: StreakRun[] = [];
  for (const run of runs) {
    if (!FAILED_STATUSES.has((run.status || '').toUpperCase())) break;
    streak.push(run);
  }
  if (streak.length < MIN_FAILURE_STREAK) return null;
  const categories = new Set(streak.map((run) => run.failure_category || ''));
  const [only] = categories;
  return {
    count: streak.length,
    since: streak[streak.length - 1].start_time || null,
    category: categories.size === 1 && only ? only : null,
  };
}
