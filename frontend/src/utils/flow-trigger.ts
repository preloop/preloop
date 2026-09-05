/**
 * How a flow is triggered, in one line, for every view that names it.
 *
 * The list and the flow detail page both answer "what starts this?" and used
 * to answer it differently: the list humanised the event ids
 * ("Pull request opened"), the detail printed them raw
 * ("pull_request_opened"). One reading of one field belongs in one place.
 */

import { formatLocalDateTime } from './date';
import type { Flow } from '../types';

/** "pull_request_updated" reads as "Pull request updated" in a column. */
function humaniseToken(token: string): string {
  const spaced = token.replace(/[_-]+/g, ' ').trim();
  if (!spaced) return '';
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

/**
 * The trigger, on one line: where events come from and which ones.
 *
 * `trigger_event_source` is 'webhook', 'schedule' or a tracker id, so the
 * tracker names are passed in; when they are not available (the request
 * failed, or has not landed yet) the event types alone still say more than
 * a UUID would.
 */
export function flowTriggerSummary(
  flow: Flow,
  trackerNames: Record<string, string> = {}
): { label: string; title: string } {
  const source = flow.trigger_event_source;
  if (source === 'schedule') {
    const schedule = flow.schedule_state;
    const label = schedule?.description || 'Schedule';
    const next = schedule?.next_run_at
      ? `Next run ${formatLocalDateTime(schedule.next_run_at)}`
      : schedule && !schedule.active
        ? 'Schedule paused'
        : 'No upcoming runs';
    return { label, title: `${label} · ${next}` };
  }
  const types = (flow.trigger_event_types || [])
    .map((type) => String(type))
    // A webhook flow whose only event type is "webhook" would read
    // "Webhook - Webhook"; say a thing once.
    .filter((type) => type.toLowerCase() !== String(source).toLowerCase())
    .map((type) => humaniseToken(type))
    .filter(Boolean);
  if (source === 'webhook') {
    const label = types.length ? `Webhook · ${types.join(', ')}` : 'Webhook';
    return { label, title: label };
  }
  if (!source) {
    return {
      label: 'Manual',
      title: 'No trigger configured. This flow runs when someone starts it.',
    };
  }
  const name = trackerNames[source];
  const parts = [name, types.join(', ')].filter(Boolean);
  const label = parts.length ? parts.join(' · ') : 'Tracker';
  return { label, title: label };
}
