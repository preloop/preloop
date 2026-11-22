import { fetchWithAuth, getUserProfile } from './api';
import { REST_API_PREFIX } from './api-path';
import { hasFleetPermission } from './device-fleet-api';

export type ExtensionApprovalState = 'approved' | 'unapproved' | 'unknown';
export type ExtensionSource =
  | 'official_marketplace'
  | 'third_party_marketplace'
  | 'git'
  | 'local'
  | 'builtin'
  | 'unknown';
export interface ExtensionKey {
  agent_kind: string;
  extension_kind: 'plugin' | 'skill' | 'hook' | 'command';
  extension_id: string;
  approval_state: ExtensionApprovalState;
}
export interface InstalledExtension extends ExtensionKey {
  fingerprint: string;
  profile_ref: string;
  id_hashed: boolean;
  version: string | null;
  source_kind: ExtensionSource;
  source_ref_hash: string | null;
  scope: 'user' | 'project';
  project_ref_hash: string | null;
  enabled: 'true' | 'false' | 'unknown';
  present: boolean;
  first_seen_at: string;
  last_seen_at: string;
}
export interface ExtensionAggregate extends ExtensionKey {
  device_count: number;
  versions: string[];
  source_kinds: ExtensionSource[];
}
export interface ExtensionPage {
  items: (InstalledExtension | ExtensionAggregate)[];
  next_cursor: string | null;
  completeness: 'complete' | 'partial' | 'unknown';
}
export interface ExtensionFilters {
  fingerprint?: string;
  state?: ExtensionApprovalState;
  source_kind?: ExtensionSource;
  cursor?: string;
}
export interface ExtensionDecision {
  approval_state: ExtensionApprovalState;
  version_constraint: string | null;
  note: string | null;
}

/** Resolve permissions before offering account-wide decisions. */
export async function extensionAccess(): Promise<{
  view: boolean;
  manage: boolean;
}> {
  const user = await getUserProfile();
  return {
    view: hasFleetPermission(user, 'view_agents'),
    manage: hasFleetPermission(user, 'manage_agents'),
  };
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetchWithAuth(
    `${REST_API_PREFIX}/agents${path}`,
    options
  );
  if (!response.ok)
    throw new Error(`Extension request failed (${response.status})`);
  return response.json() as Promise<T>;
}

export interface ExtensionPolicyFile {
  schema: 'preloop.extension-policy.v1';
  account_ref: string;
  generated_at: string;
  entries: Array<{
    agent_kind: string;
    extension_kind: ExtensionKey['extension_kind'];
    extension_id: string;
    approval_state: ExtensionApprovalState;
    version_constraint: string | null;
    content_hashes: string[];
  }>;
}

/** Fetch the offline policy file. It has no notes, credentials, or device data. */
export async function fetchExtensionPolicyText(): Promise<string> {
  const policy = await request<ExtensionPolicyFile>(
    '/extension-approvals/export'
  );
  return `${JSON.stringify(policy, null, 2)}\n`;
}

export function getExtensions(
  filters: ExtensionFilters = {}
): Promise<ExtensionPage> {
  const params = new URLSearchParams({ limit: '50' });
  if (!filters.fingerprint) params.set('group', 'extension');
  for (const [key, value] of Object.entries(filters))
    if (value) params.set(key, value);
  return request(`/discovery-extensions?${params}`);
}

/** Preserve existing notes/constraints when the table changes only the state. */
export async function setExtensionApproval(
  extension: ExtensionKey,
  approval_state: ExtensionApprovalState
): Promise<void> {
  const path = [
    extension.agent_kind,
    extension.extension_kind,
    extension.extension_id,
  ]
    .map(encodeURIComponent)
    .join('/');
  const response = await fetchWithAuth(
    `${REST_API_PREFIX}/agents/extension-approvals/${path}`
  );
  if (!response.ok && response.status !== 404)
    throw new Error(`Extension request failed (${response.status})`);
  const existing: ExtensionDecision | null = response.ok
    ? await response.json()
    : null;
  await request(`/extension-approvals/${path}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      approval_state,
      version_constraint: existing?.version_constraint ?? null,
      note: existing?.note ?? null,
    }),
  });
}
