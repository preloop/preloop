/**
 * Console client for the Delivery Report policy (#2020).
 *
 * One account layer and optional per-repository layers. Each save sends the
 * full set of overrides for its layer; a field left out falls back to the
 * layer below. Shapes follow openapi.yaml.
 */
import { extractErrorMessage, fetchWithAuth } from './api';
import { REST_API_PREFIX } from './api-path';

export type ReportCadence = 'weekly' | 'daily' | 'manual';
export type ReportMode = 'deterministic' | 'assessed';
export type WriteRule = 'allow' | 'approval' | 'deny';
export type CriteriaSource = 'triage_assessment' | 'description';
export type RecipientRule = 'account_owner' | 'sponsoring_lead';

export interface DeliveryReportPolicy {
  enabled: boolean;
  cadence: ReportCadence;
  weekday: string;
  at: string;
  timezone: string;
  projects: string[];
  report_mode: ReportMode;
  onboarding_assessed_run: boolean;
  per_run_usd: number;
  monthly_usd: number;
  drop_order: string[];
  max_assessed_changes: number;
  pr_ready_days: number;
  issue_inactive_days: number;
  ui_screenshot_min_lines: number;
  criteria_source: CriteriaSource;
  tracker_writes: {
    create_follow_up: WriteRule;
    reopen: WriteRule;
    close: 'deny';
    follow_up_label: string;
  };
  recipients: RecipientRule;
  sponsoring_lead_user_id: string | null;
  opted_out_user_ids: string[];
}

export type PolicyOverrides = Partial<
  Omit<DeliveryReportPolicy, 'tracker_writes'>
> & { tracker_writes?: Partial<DeliveryReportPolicy['tracker_writes']> };

export interface DeliveryReportPolicyLayer {
  level: 'account' | 'project';
  project_id: string | null;
  overrides: PolicyOverrides;
  effective: DeliveryReportPolicy;
  defaults: DeliveryReportPolicy;
  project_override_fields: string[];
}

/** A refused read or save, with the server's refusal code when it sent one. */
export class DeliveryReportPolicyError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code: string | null
  ) {
    super(message);
  }
}

function layerPath(projectId?: string): string {
  const base = `${REST_API_PREFIX}/delivery-report/policy`;
  return projectId ? `${base}/projects/${encodeURIComponent(projectId)}` : base;
}

async function parse(
  response: Response,
  fallback: string
): Promise<DeliveryReportPolicyLayer> {
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = body?.detail;
    if (detail && typeof detail === 'object' && 'message' in detail) {
      throw new DeliveryReportPolicyError(
        String(detail.message),
        response.status,
        String(detail.code ?? '') || null
      );
    }
    throw new DeliveryReportPolicyError(
      extractErrorMessage(body, fallback),
      response.status,
      null
    );
  }
  return body as DeliveryReportPolicyLayer;
}

export async function getDeliveryReportPolicy(
  projectId?: string
): Promise<DeliveryReportPolicyLayer> {
  return parse(
    await fetchWithAuth(layerPath(projectId)),
    'Could not load the report policy'
  );
}

export async function saveDeliveryReportPolicy(
  overrides: PolicyOverrides,
  projectId?: string
): Promise<DeliveryReportPolicyLayer> {
  return parse(
    await fetchWithAuth(layerPath(projectId), {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ overrides }),
    }),
    'Could not save the report policy'
  );
}

export async function clearDeliveryReportPolicy(
  projectId: string
): Promise<DeliveryReportPolicyLayer> {
  return parse(
    await fetchWithAuth(layerPath(projectId), { method: 'DELETE' }),
    'Could not reset the repository policy'
  );
}

/** Whether the signed-in person receives the report email. */
export interface DeliveryReportSubscription {
  opted_out: boolean;
  is_recipient: boolean;
  report_enabled: boolean;
}

const SUBSCRIPTION_PATH = '/api/v1/delivery-report/subscription';

async function parseSubscription(
  response: Response,
  fallback: string
): Promise<DeliveryReportSubscription> {
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    throw new DeliveryReportPolicyError(
      extractErrorMessage(body, fallback),
      response.status,
      null
    );
  }
  return body as DeliveryReportSubscription;
}

export async function getDeliveryReportSubscription(): Promise<DeliveryReportSubscription> {
  return parseSubscription(
    await fetchWithAuth(SUBSCRIPTION_PATH),
    'Could not load your report email setting'
  );
}

/** Opt the signed-in person out of, or back into, the report email. */
export async function saveDeliveryReportSubscription(
  optedOut: boolean
): Promise<DeliveryReportSubscription> {
  return parseSubscription(
    await fetchWithAuth(SUBSCRIPTION_PATH, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ opted_out: optedOut }),
    }),
    'Could not save your report email setting'
  );
}
