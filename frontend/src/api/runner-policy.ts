import { REST_API_PREFIX } from '../api-path';
import { fetchWithAuth, extractErrorMessage } from '../api';

export type RunnerRequirement = 'optional' | 'required' | 'forbidden';
export interface RunnerPolicyEntry {
  requirement: RunnerRequirement;
  mandated_capabilities: ('flows' | 'inventory' | 'sessions')[];
  grace_days: number;
  enforcement: 'none' | 'warn';
  message?: string | null;
  since?: string | null;
}
export interface RunnerPolicy {
  account: RunnerPolicyEntry;
  roles: Record<string, RunnerPolicyEntry>;
  teams: Record<string, RunnerPolicyEntry>;
  stale_after_days: number;
}
export type ComplianceState =
  'compliant' | 'stale' | 'never_connected' | 'in_grace' | 'exempt';
export interface MemberCompliance {
  user_id: string;
  username: string;
  email?: string | null;
  requirement: RunnerRequirement;
  enforcement: 'none' | 'warn';
  policy_source: string;
  state: ComplianceState;
  grace_until?: string | null;
  last_heartbeat?: string | null;
  inventory_known: boolean;
  inventory_fresh: boolean;
  runners: {
    runner_id: string;
    name: string;
    status: string;
    last_heartbeat?: string | null;
  }[];
  harnesses: {
    harness: string;
    display_name?: string | null;
    governance: string;
    runner_name: string;
  }[];
  ungoverned_harnesses: string[];
}
export interface RunnerCompliance {
  generated_at: string;
  total: number;
  skip: number;
  limit: number;
  counts: Record<string, number>;
  members: MemberCompliance[];
}
export interface MyRunnerCompliance {
  requirement: RunnerRequirement;
  enforcement: 'none' | 'warn';
  state: ComplianceState;
  show_banner: boolean;
  message?: string | null;
}

/** EE supplies these capability-gated routes; OSS never calls them without the flag. */
export async function runnerPolicyRequest<T>(
  path: string,
  init?: RequestInit
): Promise<T> {
  const response = await fetchWithAuth(
    `${REST_API_PREFIX}/account/${path}`,
    init
  );
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(
      extractErrorMessage(body, 'Could not load runner policy information.')
    );
  }
  return response.json();
}

const COMPLIANCE_LABELS: Record<string, string> = {
  compliant: 'Compliant',
  stale: 'Stale',
  never_connected: 'Never connected',
  in_grace: 'In grace period',
  exempt: 'Exempt',
};

/** Preserve server states introduced after this console version. */
export const complianceLabel = (state: string): string =>
  COMPLIANCE_LABELS[state] ?? state;
