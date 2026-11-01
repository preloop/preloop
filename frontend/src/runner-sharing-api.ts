/**
 * Console client for runner sharing (EE #186). The endpoints are served by an
 * extension plugin; only the capability-gated panel imports this module.
 */
import { REST_API_PREFIX } from './api-path';
import { extractErrorMessage, fetchWithAuth } from './api';

export type ShareScope = 'flows' | 'sessions';
export type OwnerConsent = 'required' | 'not_required';
export type RunnerConsentSetting = 'inherit' | OwnerConsent;

export interface RunnerShare {
  id: string;
  runner_id: string;
  owner_user_id: string | null;
  grantee_type: 'team' | 'user';
  grantee_id: string;
  scopes: ShareScope[];
  harnesses: string[] | null;
  directory_ids: string[] | null;
  consent_state: 'active' | 'pending_owner_consent' | 'declined';
  consent_source: 'owner' | 'owner_accepted' | 'admin_override' | null;
  created_by_user_id: string | null;
  created_at: string | null;
  revoked_at: string | null;
}

export interface OwnerConsentPolicy {
  owner_consent: OwnerConsent;
  source: 'runner' | 'account' | 'default';
  runner_setting: RunnerConsentSetting;
  account_setting: OwnerConsent;
}

export interface RunnerSharing {
  runner_id: string;
  owner_user_id: string | null;
  policy: OwnerConsentPolicy;
  can_share: boolean;
  can_set_policy: boolean;
  grants: RunnerShare[];
}

export interface RunnerShareWrite {
  grantee_type: 'team' | 'user';
  grantee_id: string;
  scopes: ShareScope[];
  harnesses?: string[] | null;
  directory_ids?: string[] | null;
}

async function call<T>(
  path: string,
  init: RequestInit,
  fallback: string
): Promise<T> {
  const response = await fetchWithAuth(`${REST_API_PREFIX}${path}`, init);
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    const detail = (body as { detail?: unknown }).detail;
    if (detail && typeof detail === 'object' && 'message' in detail) {
      throw new Error(String((detail as { message: unknown }).message));
    }
    throw new Error(extractErrorMessage(body, fallback));
  }
  return response.json() as Promise<T>;
}

const json = (method: string, body?: unknown): RequestInit => ({
  method,
  headers: { 'Content-Type': 'application/json' },
  body: body === undefined ? undefined : JSON.stringify(body),
});

export function getRunnerSharing(runnerId: string): Promise<RunnerSharing> {
  return call(
    `/runners/${encodeURIComponent(runnerId)}/shares`,
    { method: 'GET' },
    'Failed to load runner sharing'
  );
}

export function createRunnerShare(
  runnerId: string,
  body: RunnerShareWrite
): Promise<RunnerShare> {
  return call(
    `/runners/${encodeURIComponent(runnerId)}/shares`,
    json('POST', body),
    'Failed to share the runner'
  );
}

export function answerRunnerShare(
  runnerId: string,
  shareId: string,
  accept: boolean
): Promise<RunnerShare> {
  return call(
    `/runners/${encodeURIComponent(runnerId)}/shares/${encodeURIComponent(
      shareId
    )}/${accept ? 'accept' : 'decline'}`,
    json('POST'),
    'Failed to answer the share'
  );
}

export function revokeRunnerShare(
  runnerId: string,
  shareId: string
): Promise<{ grant: RunnerShare; stopped_session_ids: string[] }> {
  return call(
    `/runners/${encodeURIComponent(runnerId)}/shares/${encodeURIComponent(
      shareId
    )}`,
    { method: 'DELETE' },
    'Failed to revoke the share'
  );
}

export function setRunnerConsentSetting(
  runnerId: string,
  setting: RunnerConsentSetting
): Promise<OwnerConsentPolicy> {
  return call(
    `/runners/${encodeURIComponent(runnerId)}/sharing-policy`,
    json('PUT', { owner_consent: setting }),
    'Failed to change the runner policy'
  );
}

export function setAccountOwnerConsent(
  value: OwnerConsent
): Promise<{ owner_consent: OwnerConsent; source: string }> {
  return call(
    '/account/runner-sharing-policy',
    json('PUT', { owner_consent: value }),
    'Failed to change the account policy'
  );
}
