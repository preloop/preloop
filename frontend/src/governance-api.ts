import { REST_API_PREFIX } from './api-path';
/** Console contract for the governance review plugin, behind governance_review. */
import { extractErrorMessage, fetchWithAuth } from './api';

export type FindingKind = 'violation' | 'inefficiency' | 'anomaly';
export type FindingSeverity = 'high' | 'medium' | 'low';
export type DismissReason = 'expected' | 'fixed' | 'not_applicable';
export interface GovernanceFinding {
  id: string;
  run_id: string;
  kind: FindingKind;
  category: string;
  severity: FindingSeverity;
  title: string;
  rule_ref?: string;
  evidence: FindingEvidence[];
  session_ids: string[];
  user_id?: string;
  user_name?: string;
  agent_ref?: string;
  suggestion: string;
  estimated_weekly_savings?: number;
  dismissed_by_id?: string;
  dismissed_by_name?: string;
  dismissed_at?: string;
  dismissed_reason?: string;
  created_at: string;
}
/** One day of the spend outlier's review window. */
export interface SpendDay {
  day: string;
  cost: number;
  is_outlier: boolean;
}
/** A session that contributed to an outlier day, largest first. */
export interface ContributingSession {
  runtime_session_id: string;
  title?: string | null;
  agent_name?: string | null;
  started_at?: string | null;
  cost?: number | null;
  href?: string | null;
  agent_href?: string | null;
  flow_href?: string | null;
  flow_name?: string | null;
}
export interface SpendEvidence {
  day: string;
  cost: number;
  baseline?: number | null;
  ratio?: number | null;
  daily_series?: SpendDay[];
  contributing_sessions?: ContributingSession[];
}
/** One attached tool, measured; null where it could not be measured. */
export interface ToolEvidence {
  name: string;
  sessions_attached: number;
  sessions_used: number;
  request_count?: number;
  schema_tokens_per_request?: number | null;
  estimated_weekly_tokens?: number | null;
  estimated_weekly_savings?: number | null;
  capability?: string | null;
}
export interface FindingEvidence {
  excerpt: string;
  source: string;
  audit_event_id?: string;
  api_usage_id?: string;
  id?: string;
  spend?: SpendEvidence;
  tool?: ToolEvidence;
}
export interface GovernanceRun {
  id: string;
  window_start: string;
  window_end: string;
  status: string;
  started_at: string;
  finished_at?: string;
  sessions_considered: number;
  sessions_summarized: number;
  segments: number;
  summaries_started_at?: string;
  stop_reason?: string;
  platform_cost?: number;
  account_cost?: number;
  mode_after?: string;
}
export interface FindingFilters {
  kind?: string;
  severity?: string;
  user_id?: string;
  run_id?: string;
  category?: string;
  dismissed?: boolean;
  limit?: number;
  offset?: number;
}
const BASE = REST_API_PREFIX + '/governance-review';
async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetchWithAuth(`${BASE}${path}`, {
    ...options,
    passive: !options?.method || options.method === 'GET',
  });
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new Error(
      extractErrorMessage(
        body,
        response.status === 404
          ? 'Finding not found or unavailable.'
          : 'Could not load governance review.'
      )
    );
  }
  return response.json();
}
function query(params: Record<string, unknown>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== '') search.set(key, String(value));
  }
  return `?${search}`;
}
export const getGovernanceRuns = (
  offset = 0,
  run_id?: string
): Promise<GovernanceRun[]> =>
  request(`/runs${query({ limit: 25, offset, run_id })}`);
export const getGovernanceFindings = (
  filters: FindingFilters
): Promise<GovernanceFinding[]> =>
  request(`/findings${query({ limit: 50, ...filters })}`);
export const getGovernanceFinding = (id: string): Promise<GovernanceFinding> =>
  request(`/findings/${encodeURIComponent(id)}`);
export const dismissGovernanceFinding = (
  id: string,
  reason: DismissReason
): Promise<GovernanceFinding> =>
  request(`/findings/${encodeURIComponent(id)}/dismiss`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reason }),
  });
export const restoreGovernanceFinding = (
  id: string
): Promise<GovernanceFinding> =>
  request(`/findings/${encodeURIComponent(id)}/restore`, { method: 'POST' });

/** Weekly report email switches: the account's and the signed-in user's. */
export interface WeeklyReportPreferences {
  /** Account email analytics, shown only with the enterprise capability. */
  tracking_enabled?: boolean;
  /** The full report for owners and admins. */
  account_enabled: boolean;
  user_enabled: boolean;
  /** Personal reports for regular members; absent on older servers. */
  members_enabled?: boolean;
  /** Members see policy findings attributed to them. */
  member_policy_findings_enabled?: boolean;
}
async function preferences<T>(options?: RequestInit): Promise<T> {
  const response = await fetchWithAuth(
    REST_API_PREFIX + '/weekly-report/preferences',
    {
      ...options,
      passive: !options?.method,
    }
  );
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new Error(
      extractErrorMessage(
        body,
        response.status === 403
          ? 'Only owners and admins can change this for the account.'
          : 'Could not load weekly report settings.'
      )
    );
  }
  return response.json();
}
export const getWeeklyReportPreferences =
  (): Promise<WeeklyReportPreferences> => preferences();
export const updateWeeklyReportPreferences = (
  body: Partial<WeeklyReportPreferences>
): Promise<WeeklyReportPreferences> =>
  preferences({
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
