import type { MediaSyncMapping } from '../types';

/** Last mapped browser step at or before the playhead; never snap backwards. */
export function currentMediaStep(
  mappings: MediaSyncMapping[],
  timeMs: number
): MediaSyncMapping | null {
  return mappings.reduce<MediaSyncMapping | null>(
    (last, item) =>
      item.activity_type === 'browser_step' &&
      item.offset_ms <= timeMs &&
      (!last || item.offset_ms >= last.offset_ms)
        ? item
        : last,
    null
  );
}

export function adjacentMediaStep(
  mappings: MediaSyncMapping[],
  timeMs: number,
  direction: -1 | 1
): MediaSyncMapping | null {
  const steps = mappings
    .filter((item) => item.activity_type === 'browser_step')
    .sort((a, b) => a.offset_ms - b.offset_ms);
  return direction === 1
    ? (steps.find((item) => item.offset_ms > timeMs + 1) ?? null)
    : ([...steps].reverse().find((item) => item.offset_ms < timeMs - 1) ??
        null);
}

export function mediaOffsetInRange(
  offset: number,
  durationMs: number | null | undefined
): boolean {
  return (
    Number.isFinite(offset) &&
    offset >= 0 &&
    (durationMs == null || offset <= durationMs)
  );
}
