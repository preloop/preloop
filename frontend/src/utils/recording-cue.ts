import { html, nothing } from 'lit';
import type {
  FlowGatewayEvent,
  RecordingSync,
  RuntimeSessionActivityItem,
} from '../types';

/** Identity correlation only: no client-side time or nearest-row guesses. */
export function recordingEventActivity(
  event: FlowGatewayEvent | null,
  sync?: RecordingSync
): string | null {
  if (!event || !sync) return null;
  return (
    sync.mappings.find(
      (item) =>
        item.activity_id === event.payload?.activity_id ||
        item.api_usage_id === event.id ||
        (!!item.api_usage_id &&
          item.api_usage_id === event.payload?.api_usage_id)
    )?.activity_id ?? null
  );
}

export function recordingToolActivity(
  key: string,
  callId: string | null,
  rows: RuntimeSessionActivityItem[],
  mergedKeys: string[] = []
): string | null {
  const keys = [key, ...mergedKeys];
  return (
    rows.find(
      (row) =>
        row.activity_type === 'tool_call' &&
        ((!!row.activity_id && keys.includes(`native:${row.activity_id}`)) ||
          (!!callId &&
            (row.metadata?.tool_call_id === callId ||
              row.metadata?.correlation_id === callId)))
    )?.activity_id ?? null
  );
}

export function recordingCue(
  host: HTMLElement,
  activityId: string | null | undefined,
  sync: RecordingSync | undefined,
  enabled: boolean
) {
  if (!enabled || !activityId || !sync) return nothing;
  const entry = sync.mappings.find((item) => item.activity_id === activityId);
  if (!entry)
    return html`<small title="No recording for this moment"
      >Not in this recording</small
    >`;
  return html`<button
    type="button"
    class="recording-cue"
    title=${entry.approximate ? 'Approximate timing' : `Play at ${(entry.offset_ms / 1000).toFixed(1)} s`}
    @click=${() => host.dispatchEvent(new CustomEvent('recording-seek', { detail: { activityId }, bubbles: true, composed: true }))}
  >
    Play from here
  </button>`;
}
