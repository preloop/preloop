import { fetchWithAuth } from '../api';
import { REST_API_PREFIX } from '../api-path';

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
    engagement_window_days?: 28;
    engagement_active_users?: number | null;
    feature_usage: (CopilotMetricCounts & {
      feature: string;
      engaged_users?: number | null;
    })[];
    editor_usage: (CopilotMetricCounts & { editor: string })[];
  })[];
}

type RecordValue = Record<string, unknown>;
const record = (value: unknown): value is RecordValue =>
  value !== null && typeof value === 'object' && !Array.isArray(value);
const nullableString = (value: unknown): boolean =>
  value === null || typeof value === 'string';
const nullableNumber = (value: unknown): boolean =>
  value === null || (typeof value === 'number' && Number.isFinite(value));
const items = (value: unknown, valid: (item: unknown) => boolean): boolean =>
  Array.isArray(value) && value.every(valid);
const counts = (value: RecordValue): boolean =>
  [
    'user_initiated_interaction_count',
    'code_generation_activity_count',
    'code_acceptance_activity_count',
  ].every((key) => value[key] === undefined || nullableNumber(value[key]));

function validActivity(value: RecordValue): boolean {
  return (
    typeof value.is_active === 'boolean' &&
    nullableString(value.snapshot_day) &&
    typeof value.active_days === 'number' &&
    Number.isInteger(value.active_days) &&
    value.active_days > 0 &&
    items(
      value.seats,
      (seat) =>
        record(seat) &&
        typeof seat.github_login === 'string' &&
        nullableString(seat.user_id) &&
        (seat.user_name === undefined || nullableString(seat.user_name)) &&
        nullableString(seat.last_activity_at) &&
        nullableString(seat.last_activity_editor) &&
        typeof seat.active === 'boolean' &&
        typeof seat.governed_agent === 'boolean' &&
        nullableNumber(seat.premium_net_amount)
    ) &&
    items(
      value.gaps,
      (gap) =>
        record(gap) &&
        typeof gap.github_login === 'string' &&
        nullableString(gap.user_id) &&
        ['unmapped_login', 'no_governed_agent'].includes(String(gap.reason))
    )
  );
}

function validMetrics(value: RecordValue): boolean {
  return items(
    value.days,
    (day) =>
      record(day) &&
      typeof day.day === 'string' &&
      ['available', 'unavailable'].includes(String(day.status)) &&
      nullableString(day.reason) &&
      nullableNumber(day.active_users) &&
      nullableNumber(day.engaged_users) &&
      counts(day) &&
      (day.engagement_window_days === undefined ||
        day.engagement_window_days === 28) &&
      (day.engagement_active_users === undefined ||
        nullableNumber(day.engagement_active_users)) &&
      items(
        day.feature_usage,
        (row) =>
          record(row) &&
          typeof row.feature === 'string' &&
          counts(row) &&
          (row.engaged_users === undefined || nullableNumber(row.engaged_users))
      ) &&
      items(
        day.editor_usage,
        (row) => record(row) && typeof row.editor === 'string' && counts(row)
      )
  );
}

/** Passive decoration never opens a checkout dialog or exposes unentitled data. */
async function read<T>(path: string): Promise<T | null> {
  const response = await fetchWithAuth(path, { passive: true });
  if ([402, 403, 404].includes(response.status)) return null;
  if (!response.ok) throw new Error('Copilot activity could not be loaded.');
  const body = await response.json();
  if (
    !record(body) ||
    typeof body.organization !== 'string' ||
    body.usage_source !== 'vendor_reported' ||
    body.metered_by_gateway !== false ||
    (path.includes('seat-activity')
      ? !validActivity(body)
      : !validMetrics(body))
  ) {
    throw new Error('Copilot activity response is unavailable.');
  }
  return body as T;
}

export function getCopilotSeatActivity(): Promise<CopilotSeatActivity | null> {
  return read(`${REST_API_PREFIX}/cost/copilot/seat-activity?active_days=30`);
}

export function getCopilotOrganizationMetrics(
  startDate?: string,
  endDate?: string
): Promise<CopilotOrganizationMetrics | null> {
  const params = new URLSearchParams();
  if (startDate) params.set('start_date', startDate);
  if (endDate) params.set('end_date', endDate);
  return read(`${REST_API_PREFIX}/cost/copilot/organization-metrics?${params}`);
}
