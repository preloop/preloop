import { fetchWithAuth } from '../api';

export interface CopilotSeatActivity {
  organization: string;
  usage_source: 'vendor_reported';
  metered_by_gateway: false;
  is_active: boolean;
  snapshot_day: string | null;
  active_days: number;
  seats: {
    github_login: string;
    user_id: string | null;
    user_name?: string | null;
    last_activity_at: string | null;
    last_activity_editor: string | null;
    active: boolean;
    governed_agent: boolean;
    premium_net_amount: number | null;
  }[];
  gaps: {
    github_login: string;
    user_id: string | null;
    reason: 'unmapped_login' | 'no_governed_agent';
  }[];
}

export interface CopilotMetricCounts {
  user_initiated_interaction_count?: number | null;
  code_generation_activity_count?: number | null;
  code_acceptance_activity_count?: number | null;
}

export interface CopilotOrganizationMetrics {
  organization: string;
  usage_source: 'vendor_reported';
  metered_by_gateway: false;
  days: (CopilotMetricCounts & {
    day: string;
    status: 'available' | 'unavailable';
    reason: string | null;
    active_users: number | null;
    engaged_users: number | null;
    feature_usage: (CopilotMetricCounts & {
      feature: string;
      engaged_users?: number | null;
    })[];
    editor_usage: (CopilotMetricCounts & { editor: string })[];
  })[];
}

/** Passive decoration never opens a checkout dialog or exposes unentitled data. */
async function read<T>(path: string): Promise<T | null> {
  const response = await fetchWithAuth(path, { passive: true });
  if ([402, 403, 404].includes(response.status)) return null;
  if (!response.ok) throw new Error('Copilot activity could not be loaded.');
  const body = await response.json();
  if (
    body?.usage_source !== 'vendor_reported' ||
    body?.metered_by_gateway !== false ||
    (path.includes('seat-activity')
      ? !Array.isArray(body.seats) || !Array.isArray(body.gaps)
      : !Array.isArray(body.days))
  ) {
    throw new Error('Copilot activity response is unavailable.');
  }
  return body;
}

export function getCopilotSeatActivity(): Promise<CopilotSeatActivity | null> {
  return read('/api/v1/cost/copilot/seat-activity?active_days=30');
}

export function getCopilotOrganizationMetrics(
  startDate?: string,
  endDate?: string
): Promise<CopilotOrganizationMetrics | null> {
  const params = new URLSearchParams();
  if (startDate) params.set('start_date', startDate);
  if (endDate) params.set('end_date', endDate);
  return read(`/api/v1/cost/copilot/organization-metrics?${params}`);
}
