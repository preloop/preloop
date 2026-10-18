import type {
  ActivityAgent,
  ActivityPerson,
  AgentState,
  PersonState,
} from '../live-activity-api';
import type { TimeRangeOption } from '../components/time-range-select';
import {
  FLEET_MAP_WINDOWS,
  type FleetMapWindow,
  windowMinutes,
  windowRefreshMs,
} from './fleet-map-activity';

/**
 * Pure derivations for the Activity board: labels, chip variants, ordering,
 * number formats and the window. The board shares the Fleet Map's window
 * vocabulary and refresh cadence, so "last hour" means the same on both.
 */

export const ACTIVITY_WINDOW_KEY = 'preloop:activity:window';
/** A live board defaults to the recent past, not 30 days. */
export const DEFAULT_ACTIVITY_WINDOW: FleetMapWindow = '1h';
/** Fewer samples than this render "—", never a median of one. */
export const MIN_SAMPLES = 3;
/** Past this the board says live data is paused. */
export const STALE_AFTER_MS = 60_000;
/** The polite live region speaks at most this often. */
export const ANNOUNCE_INTERVAL_MS = 30_000;

export const ACTIVITY_WINDOW_OPTIONS: TimeRangeOption[] = FLEET_MAP_WINDOWS.map(
  (item) => ({ value: item.value, label: item.label })
);

export { windowMinutes, windowRefreshMs };

export function loadActivityWindow(): FleetMapWindow {
  try {
    const saved = localStorage.getItem(ACTIVITY_WINDOW_KEY);
    if (FLEET_MAP_WINDOWS.some((item) => item.value === saved))
      return saved as FleetMapWindow;
  } catch {
    // Private mode: the default.
  }
  return DEFAULT_ACTIVITY_WINDOW;
}

export function saveActivityWindow(value: FleetMapWindow): void {
  try {
    localStorage.setItem(ACTIVITY_WINDOW_KEY, value);
  } catch {
    // A preference, not a requirement.
  }
}

/** Short label restating the window ("1h", "any time"). */
export function windowShortLabel(value: FleetMapWindow): string {
  return value === 'any' ? 'any time' : value;
}

export type ChipVariant = 'success' | 'warning' | 'neutral';

const PERSON_LABEL: Record<PersonState, string> = {
  active: 'Active',
  idle: 'Idle',
  away: 'Away',
};

const AGENT_LABEL: Record<AgentState, string> = {
  working: 'Working',
  waiting_approval: 'Waiting for approval',
  waiting_reply: 'Waiting for reply',
  idle: 'Idle',
  ended: 'Ended',
};

export const personStateLabel = (state: PersonState): string =>
  PERSON_LABEL[state] ?? 'Away';

export const personChipVariant = (state: PersonState): ChipVariant =>
  state === 'active' ? 'success' : 'neutral';

export const agentChipVariant = (state: AgentState): ChipVariant =>
  state === 'working'
    ? 'success'
    : state === 'waiting_approval'
      ? 'warning'
      : 'neutral';

/** "Waiting for approval · 6m": the duration rides inside the chip. */
export function agentStateLabel(
  agent: Pick<ActivityAgent, 'state' | 'since'>,
  nowMs: number
): string {
  const base = AGENT_LABEL[agent.state] ?? 'Idle';
  if (
    (agent.state === 'waiting_approval' || agent.state === 'waiting_reply') &&
    agent.since
  ) {
    return `${base} · ${shortDuration(nowMs - Date.parse(agent.since))}`;
  }
  return base;
}

const PERSON_RANK: Record<PersonState, number> = {
  active: 0,
  idle: 1,
  away: 2,
};
const AGENT_RANK: Record<AgentState, number> = {
  waiting_approval: 0,
  working: 1,
  waiting_reply: 2,
  idle: 3,
  ended: 4,
};

const time = (value: string | null | undefined): number =>
  value ? Date.parse(value) || 0 : 0;

/** State precedence, then the newest signal first. */
export function sortPeople(people: ActivityPerson[]): ActivityPerson[] {
  return [...people].sort(
    (a, b) =>
      (PERSON_RANK[a.state] ?? 9) - (PERSON_RANK[b.state] ?? 9) ||
      time(b.last_signal_at) - time(a.last_signal_at)
  );
}

export function sortAgents(agents: ActivityAgent[]): ActivityAgent[] {
  return [...agents].sort(
    (a, b) =>
      (AGENT_RANK[a.state] ?? 9) - (AGENT_RANK[b.state] ?? 9) ||
      time(b.last_activity_at) - time(a.last_activity_at)
  );
}

/** "1m 40s", "48s", "2h 5m". */
export function formatDuration(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s`;
  if (s < 3600) {
    const m = Math.floor(s / 60);
    const rest = s % 60;
    return rest ? `${m}m ${rest}s` : `${m}m`;
  }
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return m ? `${h}h ${m}m` : `${h}h`;
}

/** One unit, for chips ("6m", "42m", "2h"). */
export function shortDuration(ms: number): string {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  return `${Math.floor(s / 86400)}d`;
}

/** Money: two decimals, four under a cent (DESIGN.md Numbers). */
export function formatUsd(value: number): string {
  const abs = Math.abs(value);
  const digits = abs > 0 && abs < 0.01 ? 4 : 2;
  return `$${value.toFixed(digits)}`;
}

/** Counts whole under 1000, compact at or above. */
export function formatCount(value: number): string {
  if (Math.abs(value) < 1000) return String(Math.round(value));
  if (Math.abs(value) < 1_000_000) return `${(value / 1000).toFixed(1)}K`;
  return `${(value / 1_000_000).toFixed(1)}M`;
}

/** A rate with one decimal under 10, whole otherwise. */
export function formatRate(value: number): string {
  return value < 10 && !Number.isInteger(value)
    ? value.toFixed(1)
    : String(Math.round(value));
}

export interface StatCell {
  value: string;
  /** Why a value is "—"; also the approximation note for medians. */
  title?: string;
}

/** A median cell, or "—" with the reason (spec 6.4). */
export function latencyCell(
  median: number | null | undefined,
  samples: number,
  noun: 'replies' | 'decisions'
): StatCell {
  if (median === null || median === undefined || samples < MIN_SAMPLES) {
    return {
      value: '—',
      title: `Fewer than ${MIN_SAMPLES} ${noun} in this window`,
    };
  }
  return {
    value: formatDuration(median),
    title: 'Approximate: computed from 10 latency buckets',
  };
}

/** Whether the last sweep is too old to call the board live. */
export function isStale(generatedAt: string, nowMs: number): boolean {
  const at = Date.parse(generatedAt);
  return Number.isFinite(at) && nowMs - at > STALE_AFTER_MS;
}

/** The live-region sentence. */
export function announcement(
  activePeople: number,
  activeAgents: number
): string {
  const people = `${activePeople} ${activePeople === 1 ? 'person' : 'people'} active`;
  const agents = `${activeAgents} ${activeAgents === 1 ? 'agent' : 'agents'} working`;
  return `${people}, ${agents}`;
}

/**
 * Throttle for the polite live region: speaks at most once per interval and
 * never repeats itself.
 */
export class AnnounceThrottle {
  private lastAt = -Infinity;
  private lastText = '';

  constructor(private intervalMs = ANNOUNCE_INTERVAL_MS) {}

  /** The text to announce now, or null to stay quiet. */
  next(text: string, nowMs: number): string | null {
    if (text === this.lastText) return null;
    if (nowMs - this.lastAt < this.intervalMs) return null;
    this.lastAt = nowMs;
    this.lastText = text;
    return text;
  }
}

/** Points for a sparkline polyline of `values` in a `width`×`height` box. */
export function sparklinePoints(
  values: number[],
  width: number,
  height: number
): string {
  if (values.length === 0) return '';
  const max = Math.max(1, ...values);
  const step = values.length > 1 ? width / (values.length - 1) : 0;
  return values
    .map((value, index) => {
      const x = values.length > 1 ? index * step : width / 2;
      const y = height - 2 - (value / max) * (height - 4);
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(' ');
}
