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
  let cursor: string | null = null;
  let existing: ExtensionDecision | undefined;
  do {
    const params = new URLSearchParams({ limit: '500' });
    if (cursor) params.set('cursor', cursor);
    const page = await request<{
      items: (ExtensionKey & ExtensionDecision)[];
      next_cursor: string | null;
    }>(`/extension-approvals?${params}`);
    existing = page.items.find(
      (row) =>
        row.agent_kind === extension.agent_kind &&
        row.extension_kind === extension.extension_kind &&
        row.extension_id === extension.extension_id
    );
    cursor = page.next_cursor;
  } while (!existing && cursor);
  const path = [
    extension.agent_kind,
    extension.extension_kind,
    extension.extension_id,
  ]
    .map(encodeURIComponent)
    .join('/');
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
