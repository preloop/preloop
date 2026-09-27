/**
 * Console client for spend outlier alerts (#960).
 *
 * Findings are listed here; dismiss and restore go through the existing
 * attention dismissal endpoints like every other attention item.
 */
import { extractErrorMessage, fetchWithAuth } from './api';

export type SpendOutlierRule = 'daily_spend' | 'model_mix' | 'session_cost';

/** The numbers behind a finding. Which keys are set depends on the rule. */
export interface SpendOutlierDetails {
  spend_usd?: number;
  median_usd?: number;
  multiple?: number;
  threshold_multiple?: number;
  history_days?: number;
  model?: string;
  share?: number;
  previous_share?: number;
  previous_day?: string;
  threshold_share?: number;
  model_usd?: number;
  threshold_usd?: number;
  session_id?: string;
  gateway_usd?: number;
  /** Spend imported from outside the gateway (for example Copilot). */
  imported_usd?: number;
  imported_sources?: string[];
}

export interface SpendOutlierFinding {
  id: string;
  item_id: string;
  fingerprint: string;
  rule: SpendOutlierRule;
  rule_label: string;
  user_id: string | null;
  user_name: string;
  runtime_session_id: string | null;
  session_title: string | null;
  /** UTC day, `YYYY-MM-DD`. */
  day: string;
  detected_at: string;
  details: SpendOutlierDetails;
  summary: string;
}

export interface SpendOutlierSettings {
  daily_multiple: number;
  min_history_days: number;
  top_tier_model_prefixes: string[];
  /** Fraction, 0.5 = 50 percent. */
  top_tier_share: number;
  /** Null turns the session rule off. */
  session_cost_threshold_usd: number | null;
  configured?: boolean;
}

const BASE = '/api/v1/attention/spend-outliers';

/**
 * Open spend outliers. Passive: the list loads with the dashboard, and a
 * member without cost access gets a 403 that should drop the input quietly.
 */
export async function getSpendOutliers(): Promise<SpendOutlierFinding[]> {
  const response = await fetchWithAuth(BASE, { passive: true });
  if (!response.ok) {
    throw new Error('Failed to fetch spend outliers');
  }
  const body = await response.json();
  return (body?.items || []) as SpendOutlierFinding[];
}

export async function getSpendOutlierSettings(): Promise<SpendOutlierSettings> {
  const response = await fetchWithAuth(`${BASE}/settings`);
  if (!response.ok) {
    const errorData = await response.json().catch(() => null);
    throw new Error(
      extractErrorMessage(errorData, 'Failed to load spend alert settings')
    );
  }
  return response.json();
}

export async function updateSpendOutlierSettings(
  settings: SpendOutlierSettings
): Promise<SpendOutlierSettings> {
  const response = await fetchWithAuth(`${BASE}/settings`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      daily_multiple: settings.daily_multiple,
      min_history_days: settings.min_history_days,
      top_tier_model_prefixes: settings.top_tier_model_prefixes,
      top_tier_share: settings.top_tier_share,
      session_cost_threshold_usd: settings.session_cost_threshold_usd,
    }),
  });
  if (!response.ok) {
    const errorData = await response.json().catch(() => null);
    throw new Error(
      extractErrorMessage(errorData, 'Failed to save spend alert settings')
    );
  }
  return response.json();
}
