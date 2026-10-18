import { CapabilityOffError } from './capabilities';
import { fetchWithAuth } from './api';

/** Person presence states (spec 6.1). */
export type PersonState = 'active' | 'idle' | 'away';
/** Agent work states (spec 6.3). */
export type AgentState =
  'working' | 'waiting_approval' | 'waiting_reply' | 'idle' | 'ended';

export interface ActivityMetrics {
  active_people: number;
  idle_people: number;
  people_in_window: number;
  active_agents: number;
  waiting_agents: number;
  requests_per_min: number | null;
  requests_in_window: number;
  tool_calls_per_min: number | null;
  tool_calls_in_window: number;
  prompts_in_window: number;
  spend_rate_usd_per_hour: number | null;
  spend_in_window_usd: number | null;
  reply_latency_median_s: number | null;
  reply_samples: number;
  approval_latency_median_s: number | null;
  approval_samples: number;
}

export interface ActivitySeriesBucket {
  bucket_start: string;
  requests: number;
  tool_calls: number;
  prompts: number;
  spend_usd: number;
}

export interface ActivityPerson {
  user_id: string;
  display_name?: string;
  avatar_url?: string | null;
  state: PersonState;
  since: string | null;
  last_signal_at: string | null;
  last_signal_kind: 'prompt' | 'approval' | 'console' | null;
  awaiting_reply_since: string | null;
  awaiting_approval_count: number;
  devices?: { device_key: string; hostname?: string; state: string }[];
  agents?: string[];
  reply_latency_median_s: number | null;
  reply_samples: number;
  approval_latency_median_s: number | null;
  approval_samples: number;
  requests_in_window?: number;
  spend_in_window_usd?: number | null;
  href?: string;
  awaiting_reply_session_href?: string;
}

export interface ActivityAgent {
  managed_agent_id: string;
  display_name: string;
  agent_kind?: string;
  state: AgentState;
  since: string | null;
  last_activity_at: string | null;
  owner_user_id?: string | null;
  owner_display_name?: string;
  runtime_session_id?: string | null;
  session_title?: string | null;
  cwd_basename?: string | null;
  device?: { device_key: string; hostname?: string } | null;
  pending_approval_id?: string | null;
  requests_per_min?: number;
  href?: string;
  session_href?: string;
}

export interface ActivityBoard {
  schema_version: number;
  generated_at: string;
  window_minutes: number | null;
  presence_enabled: boolean;
  viewer: {
    user_id: string;
    can_see_people: boolean;
    can_see_devices: boolean;
  };
  metrics: ActivityMetrics;
  series: ActivitySeriesBucket[];
  people: ActivityPerson[];
  me?: ActivityPerson | null;
  agents: ActivityAgent[];
  truncated: boolean;
  more?: { people?: number; agents?: number };
  unavailable: string[];
  history_before?: string | null;
}

/** The board answered 403: the viewer cannot read activity. */
export class ActivityForbiddenError extends Error {
  constructor() {
    super('You do not have permission to view activity.');
    this.name = 'ActivityForbiddenError';
  }
}

async function read<T>(path: string): Promise<T> {
  const response = await fetchWithAuth(path, { passive: true });
  if (response.status === 404) throw new CapabilityOffError(path);
  if (response.status === 403) throw new ActivityForbiddenError();
  if (!response.ok) throw new Error('Could not load activity.');
  return (await response.json()) as T;
}

/**
 * Read the live activity board.
 *
 * @param windowMinutes - Window in minutes, or null for any time.
 */
export function getActivityBoard(
  windowMinutes: number | null
): Promise<ActivityBoard> {
  const query =
    windowMinutes === null
      ? ''
      : `?window_minutes=${Math.round(windowMinutes)}`;
  return read<ActivityBoard>(`/api/v1/activity/board${query}`);
}

/** The caller's own presence row and the signals it is derived from. */
export function getMyActivity(): Promise<
  ActivityPerson & { signals_used: string[] }
> {
  return read(`/api/v1/activity/me`);
}
