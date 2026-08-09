import { formatDurationBetween } from './date';

/**
 * Statuses that mean a flow execution has not reached a terminal state yet.
 *
 * Matches the running-like set used by the execution detail view; everything
 * else (SUCCEEDED, FAILED, STOPPED, TIMEOUT, CANCELLED, …) is terminal.
 */
export const RUNNING_STATUSES: ReadonlySet<string> = new Set([
  'PENDING',
  'STARTING',
  'INITIALIZING',
  'RUNNING',
]);

/**
 * Duration text for an execution row shared by the list views.
 *
 * Finished runs show their span, live runs show `Running · <elapsed>`
 * measured against `now` (recomputed on each render — list views add no
 * per-row timers and refresh via their existing polling/WebSocket updates),
 * and anything unusable — a legacy terminal row without an `end_time`, or
 * unparseable/skewed timestamps — yields an empty string so the caller can
 * render a placeholder (or nothing, in inline contexts).
 */
export function executionDurationText(
  exec: {
    status: string;
    start_time: string;
    end_time?: string | null;
  },
  now: Date = new Date()
): string {
  if (exec.end_time) {
    return formatDurationBetween(exec.start_time, exec.end_time);
  }
  if (RUNNING_STATUSES.has(exec.status)) {
    const elapsed = formatDurationBetween(exec.start_time, null, now);
    return elapsed ? `Running · ${elapsed}` : '';
  }
  return '';
}
