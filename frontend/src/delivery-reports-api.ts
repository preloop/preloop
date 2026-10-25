/**
 * Console client for Delivery Report runs (`${REST_API_PREFIX}/delivery-reports`).
 *
 * The report itself is the run's stored result document
 * (`preloop.delivery_report/v1`, see backend/preloop/services/delivery_report.py);
 * the types below name only the fields the console reads.
 */
import { fetchWithAuth } from './api';
import { REST_API_PREFIX } from './api-path';
import { permissionErrorFromResponse } from './permissions';

export type PurposeVerdict =
  'covered' | 'partial' | 'unverified' | 'no_criteria';

export interface DeliveryReportRow {
  id: string;
  flow_id: string;
  flow_name: string | null;
  status: string;
  report_status: string | null;
  started_at: string | null;
  ended_at: string | null;
  window: { from: string | null; to: string | null };
  projects: string[];
  shipped: number;
  closed: number;
  closed_unverified: number;
  proposals: number;
  cost_usd: number | null;
}

export interface DeliveryReportList {
  items: DeliveryReportRow[];
  total: number;
}

export interface Criterion {
  text: string;
  met: boolean | null;
  evidence: string[];
}

export interface Purpose {
  verdict?: PurposeVerdict;
  source?: string;
  criteria?: Criterion[];
}

export interface RiskFlag {
  flag: string;
  evidence?: string[];
  source?: string;
}

export interface ShippedChange {
  ref: string;
  title?: string | null;
  url?: string | null;
  project?: string | null;
  area?: string | null;
  author?: string | null;
  merged_at?: string | null;
  issues?: string[];
  summary?: string | null;
  purpose?: Purpose;
  risk_flags?: RiskFlag[];
  evidence_gaps?: string[];
  metadata_only?: string | null;
  cost_usd?: number | null;
}

export interface ClosedIssue {
  ref: string;
  title?: string | null;
  url?: string | null;
  updated_at?: string | null;
  closed_by?: string[];
  purpose?: Purpose;
}

export interface OpenItem {
  kind: 'pull_request' | 'issue';
  ref: string;
  title?: string | null;
  url?: string | null;
  age_days?: number;
  draft?: boolean;
}

export interface ReopenRequest {
  ref: string;
  title?: string | null;
  url?: string | null;
  revision: string;
  reason?: string | null;
  evidence?: string[];
}

export interface FollowUp {
  title?: string | null;
  ref?: string | null;
  reason?: string | null;
  evidence?: string[];
  /** Set when the run opened the issue itself under the report policy. */
  issue_url?: string | null;
}

/** A decision stored on the run for one proposal (`proposal_actions`). */
export interface ProposalDecision {
  kind: 'open_issue' | 'reopen' | 'dismiss';
  status: 'pending' | 'applied' | 'dismissed';
  issue_url?: string | null;
  reason?: string | null;
  revision?: string | null;
  at?: string | null;
}

export interface Ratio {
  total?: number;
  linked?: number;
  with_criteria?: number;
  with_evidence?: number;
}

export interface ReportSummary {
  shipped?: number;
  closed?: number;
  closed_unverified?: number;
  risk_flagged?: number;
  open_past_window?: number;
  follow_ups_proposed?: number;
  reopen_requests?: number;
  evidence_gaps?: number;
  linkage?: Ratio;
  criteria_availability?: Ratio;
  evidence_availability?: Ratio;
  closed_verdicts?: Partial<Record<PurposeVerdict, number>>;
  assessments_skipped_for_budget?: boolean;
}

export interface DeliveryReportDocument {
  status?: string;
  reason?: string;
  window?: { from?: string | null; to?: string | null };
  summary?: ReportSummary;
  shipped?: ShippedChange[];
  closed_issues?: ClosedIssue[];
  open_past_window?: OpenItem[];
  follow_ups?: FollowUp[];
  reopen_requests?: ReopenRequest[];
  /** Decisions by proposal key: `follow_up:<index>` or `reopen:<ref>`. */
  proposal_actions?: Record<string, ProposalDecision>;
  open_questions?: { question: string; ref?: string | null }[];
  delta?: {
    previous_report?: Record<string, unknown> | null;
    open_questions?: {
      question: string;
      ref?: string | null;
      status: string;
    }[];
  };
  spend?: {
    cost_usd?: number | null;
    total_tokens?: number | null;
    limit_usd?: number | null;
    skipped_for_budget?: string[];
  };
}

export interface DeliveryReportActions {
  open_issue: boolean;
  reopen: boolean;
  dismiss: boolean;
}

export interface DeliveryReportDetail extends DeliveryReportRow {
  report: DeliveryReportDocument;
  stale_reopen_requests: Record<string, string>;
  actions: DeliveryReportActions;
}

async function getJson<T>(url: string): Promise<T> {
  const response = await fetchWithAuth(url);
  if (response.status === 403)
    throw await permissionErrorFromResponse(response);
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return (await response.json()) as T;
}

export function listDeliveryReports(params: {
  limit: number;
  offset: number;
  since?: string | null;
}): Promise<DeliveryReportList> {
  const query = new URLSearchParams({
    limit: String(params.limit),
    offset: String(params.offset),
  });
  if (params.since) query.set('since', params.since);
  return getJson(`${REST_API_PREFIX}/delivery-reports?${query}`);
}

export function getDeliveryReport(id: string): Promise<DeliveryReportDetail> {
  return getJson(
    `${REST_API_PREFIX}/delivery-reports/${encodeURIComponent(id)}`
  );
}

export type DeliveryReportAction =
  | { kind: 'open_issue'; index: number }
  | { kind: 'reopen'; ref: string }
  | { kind: 'dismiss'; key: string; reason: string };

/** Why the server refused an action, from its `{code, message}` detail. */
export class DeliveryReportActionError extends Error {
  constructor(
    message: string,
    readonly code: string | null
  ) {
    super(message);
  }
}

/**
 * Act on a proposal: open the follow-up, reopen the issue, or dismiss.
 * The click is the approval; the server refuses a stale reopen request and
 * a proposal that was already decided.
 */
export async function actOnDeliveryReport(
  id: string,
  action: DeliveryReportAction
): Promise<{ key: string; decision: ProposalDecision }> {
  const response = await fetchWithAuth(
    `${REST_API_PREFIX}/delivery-reports/${encodeURIComponent(id)}/actions`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(action),
    }
  );
  if (response.ok) return response.json();
  let detail: unknown = null;
  try {
    detail = (await response.json())?.detail;
  } catch {
    // A body that is not JSON keeps the generic message below.
  }
  if (detail && typeof detail === 'object' && 'message' in detail) {
    const d = detail as { message: string; code?: string };
    throw new DeliveryReportActionError(d.message, d.code ?? null);
  }
  throw new DeliveryReportActionError(
    typeof detail === 'string' ? detail : `HTTP ${response.status}`,
    null
  );
}
