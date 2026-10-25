import { REST_API_PREFIX } from '../api-path';
import { fetchWithAuth } from '../api';

export interface InferenceHookConfig {
  expected_tenant_id: string;
  policy_snapshot_id: string;
  enabled: boolean;
  evaluation_budget_ms: number;
  unknown_event_action: 'deny' | 'allow_uninspected';
  declared_rollout: 'unknown' | 'off' | 'shadow' | 'enforcing';
}

export interface InferenceHookIntegration extends InferenceHookConfig {
  integration_id: string;
  callback_path: string;
  coverage_state: 'unverified';
  vendor_settings_state: 'unknown' | 'declared';
  pilot_state: 'pending';
}

export interface InferenceHookHealth {
  integration_id: string;
  enabled: boolean;
  callback_count: number;
  unsupported_event_count: number;
  last_callback_at: string | null;
  coverage_state: 'unverified';
  vendor_settings_state: 'unknown' | 'declared';
  pilot_state: 'pending';
}

const BASE = REST_API_PREFIX + '/anthropic/inference-hooks';

export function isIntegrationId(value: string): boolean {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(
    value
  );
}

function integrationPath(id: string): string {
  if (!isIntegrationId(id)) throw new Error('Enter a valid integration UUID.');
  return `${BASE}/${encodeURIComponent(id)}`;
}

async function request<T>(
  path: string,
  method = 'GET',
  body?: unknown
): Promise<T> {
  const response = await fetchWithAuth(path, {
    method,
    cache: 'no-store',
    ...(body === undefined
      ? {}
      : {
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        }),
  });
  // A server error may contain secret-bearing request diagnostics. Never echo it.
  if (!response.ok)
    throw new Error(
      response.status === 403
        ? 'Permission denied. Managing policies is required.'
        : response.status === 404
          ? 'Integration or feature not found.'
          : 'The integration request failed. Check the server and retry.'
    );
  try {
    return (await response.json()) as T;
  } catch {
    throw new Error('The server returned an unreadable integration response.');
  }
}

export const readInferenceHook = (id: string) =>
  request<InferenceHookIntegration>(integrationPath(id));

export const readInferenceHookHealth = (id: string) =>
  request<InferenceHookHealth>(`${integrationPath(id)}/health`);

export const createInferenceHook = (
  config: InferenceHookConfig,
  signingSecret: string
) =>
  request<InferenceHookIntegration>(BASE, 'POST', {
    ...config,
    signing_secret: signingSecret,
  });

export const updateInferenceHook = (id: string, config: InferenceHookConfig) =>
  request<InferenceHookIntegration>(integrationPath(id), 'PUT', config);

export const disableInferenceHook = (id: string) =>
  request<InferenceHookIntegration>(`${integrationPath(id)}/disable`, 'POST');

export const rotateInferenceHookSecret = (
  id: string,
  signingSecret: string,
  overlapSeconds: number
) =>
  request<InferenceHookIntegration>(`${integrationPath(id)}/rotate`, 'POST', {
    signing_secret: signingSecret,
    overlap_seconds: overlapSeconds,
  });
