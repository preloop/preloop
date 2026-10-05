/** Human administration only. Issued secrets have no cache or storage path. */
import { fetchWithAuth } from './api';
export const CI_ACTIONS = [
  'flow:trigger',
  'execution:read',
  'execution:result:read',
  'execution:stop',
  'subscription:create',
  'subscription:read',
  'subscription:update',
  'subscription:delete',
  'subscription:secret:rotate',
] as const;
export type CiAction = (typeof CI_ACTIONS)[number];
export interface CiGrant {
  version: 1;
  project_id: string;
  flow_id: string;
  actions: CiAction[];
}
export interface CiKey {
  id: string;
  actions: CiAction[];
  is_active: boolean;
  expires_at: string | null;
  created_at: string | null;
  last_used_at: string | null;
}
export interface CiIdentity {
  id: string;
  name: string;
  is_active: boolean;
  credential_version: number;
  grant: CiGrant;
  repository_identifier: string;
  repository_slug: string;
  tracker_type: string;
  keys: CiKey[];
}
export interface CiCapabilities {
  available: boolean;
  can_view: boolean;
  can_manage: boolean;
  supported_actions: CiAction[];
}
export interface CiPreview {
  grant: CiGrant;
  repository_identifier: string;
  repository_slug: string;
  tracker_type: string;
  runner_pool: 'server';
}
export interface CiIssued {
  key_id: string;
  token: string;
  secret_note: string;
  principal_id?: string;
  identity?: CiIdentity;
}
export interface CiSubscriptionIssued {
  id: string;
  url: string;
  event_types: ['flow.execution.finished'];
  secret: string;
}
const BASE = '/api/v1/ci-identities';
const id = encodeURIComponent;
async function request<T>(
  path: string,
  method = 'GET',
  body?: unknown
): Promise<T> {
  const response = await fetchWithAuth(BASE + path, {
    method,
    cache: 'no-store',
    headers: { 'Content-Type': 'application/json' },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  // Never include a server error body, which might contain credential material.
  if (!response.ok)
    throw new Error(
      'Restricted CI request could not be completed. Refresh permissions and check the resource binding.'
    );
  return response.status === 204 ? (undefined as T) : response.json();
}
export const ciAdministration = {
  capabilities: (): Promise<CiCapabilities> => request('/capabilities'),
  list: (): Promise<CiIdentity[]> => request(''),
  get: (principal: string): Promise<CiIdentity> => request(`/${id(principal)}`),
  preview: (grant: CiGrant): Promise<CiPreview> =>
    request('/preview', 'POST', { grant }),
  create: (
    name: string,
    grant: CiGrant,
    expires_at?: string
  ): Promise<CiIssued> =>
    request('', 'POST', { name, grant, ...(expires_at ? { expires_at } : {}) }),
  change: (
    principal: string,
    update: { grant?: CiGrant; enabled?: boolean }
  ): Promise<CiIdentity> => request(`/${id(principal)}`, 'PATCH', update),
  issue: (principal: string, expires_at?: string): Promise<CiIssued> =>
    request(`/${id(principal)}/keys`, 'POST', expires_at ? { expires_at } : {}),
  rotate: (
    principal: string,
    key: string,
    expires_at?: string
  ): Promise<CiIssued> =>
    request(
      `/${id(principal)}/keys/${id(key)}/rotate`,
      'POST',
      expires_at ? { expires_at } : {}
    ),
  revoke: (principal: string, key: string): Promise<void> =>
    request(`/${id(principal)}/keys/${id(key)}`, 'DELETE'),
  subscribe: (
    principal: string,
    key_id: string,
    url: string,
    description: string
  ): Promise<CiSubscriptionIssued> =>
    request(`/${id(principal)}/subscriptions`, 'POST', {
      key_id,
      url,
      description,
      event_types: ['flow.execution.finished'],
    }),
};
export function completeCiCapabilities(
  capabilities: CiCapabilities | null
): boolean {
  return (
    capabilities?.available === true &&
    Array.isArray(capabilities.supported_actions) &&
    capabilities.supported_actions.length === CI_ACTIONS.length &&
    CI_ACTIONS.every((action) =>
      capabilities.supported_actions.includes(action)
    )
  );
}
