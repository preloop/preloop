import { fetchWithAuth, getUserProfile } from './api';
import { REST_API_PREFIX } from './api-path';
import { hasFleetPermission } from './device-fleet-api';

export type ExtensionApprovalState = 'approved' | 'unapproved' | 'unknown';
/** Per-device state once content pins are applied (#2321). */
export type ExtensionEffectiveState =
  ExtensionApprovalState | 'content_changed' | 'content_unverified';
export type ContentHashStatus = 'ok' | 'too_large' | 'unreadable';
export interface ContentHashBucket {
  content_hash: string | null;
  status: ContentHashStatus;
  device_count: number;
  pinned: boolean;
}
export interface ExtensionPin {
  id: string;
  content_hash: string;
  version: string | null;
  note: string | null;
  pinned_by_user_id: string | null;
  pinned_by: string | null;
  pinned_at: string;
}
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
  /** Undefined means the decision could not be read; null means no constraint. */
  version_constraint?: string | null;
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
  effective_state?: ExtensionEffectiveState;
  content_hash?: string | null;
  content_hash_status?: ContentHashStatus;
}
export interface ExtensionAggregate extends ExtensionKey {
  device_count: number;
  versions: string[];
  source_kinds: ExtensionSource[];
  /** Worst per-device state among the devices this row counts. */
  effective_state?: ExtensionEffectiveState;
  pin_count?: number;
  content_hashes?: ContentHashBucket[];
}
export interface ExtensionCoverage {
  fingerprint: string;
  collector_version: string | null;
  completeness: 'complete' | 'partial' | 'unknown';
  reasons: string[];
}
export interface ExtensionPage {
  coverage?: ExtensionCoverage[];
  items: (InstalledExtension | ExtensionAggregate)[];
  next_cursor: string | null;
  completeness: 'complete' | 'partial' | 'unknown';
}
export interface ExtensionFilters {
  fingerprint?: string;
  agent_kind?: string;
  state?: ExtensionEffectiveState;
  source_kind?: ExtensionSource;
  cursor?: string;
}
export interface ExtensionDecision {
  approval_state: ExtensionApprovalState;
  version_constraint: string | null;
  note: string | null;
  pins?: ExtensionPin[];
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

export async function getExtensions(
  filters: ExtensionFilters = {}
): Promise<ExtensionPage> {
  const params = new URLSearchParams({ limit: '50' });
  if (!filters.fingerprint) params.set('group', 'extension');
  for (const [key, value] of Object.entries(filters))
    if (value) params.set(key, value);
  const page = await request<ExtensionPage>(`/discovery-extensions?${params}`);
  // The inventory DTO omits constraints. Read only decisions on this page,
  // once per identity, with bounded concurrency rather than an account scan.
  const identities = new Map<string, ExtensionKey>();
  for (const row of page.items) identities.set(approvalPath(row), row);
  const pending = [...identities.entries()];
  const constraints = new Map<string, string | null | undefined>();
  await Promise.all(
    Array.from({ length: Math.min(4, pending.length) }, async () => {
      for (let item = pending.pop(); item; item = pending.pop()) {
        const [key, row] = item;
        try {
          const decision = await getExtensionDecision(row);
          constraints.set(key, decision?.version_constraint ?? null);
        } catch {
          constraints.set(key, undefined);
        }
      }
    })
  );
  return {
    ...page,
    items: page.items.map((row) => ({
      ...row,
      version_constraint: constraints.get(approvalPath(row)),
    })),
  };
}

const approvalPath = (extension: ExtensionKey): string =>
  [extension.agent_kind, extension.extension_kind, extension.extension_id]
    .map(encodeURIComponent)
    .join('/');

/** Read pins for one extension; an extension with no decision has none. */
export async function getExtensionPins(
  extension: ExtensionKey
): Promise<ExtensionPin[]> {
  return (await getExtensionDecision(extension))?.pins ?? [];
}

/** Read one visible decision; a missing decision has no constraint or pins. */
async function getExtensionDecision(
  extension: ExtensionKey
): Promise<ExtensionDecision | null> {
  const response = await fetchWithAuth(
    `${REST_API_PREFIX}/agents/extension-approvals/${approvalPath(extension)}`
  );
  if (response.status === 404) return null;
  if (!response.ok)
    throw new Error(`Extension request failed (${response.status})`);
  return response.json() as Promise<ExtensionDecision>;
}

/** Approve one exact content hash under an approved extension. */
export function addExtensionPin(
  extension: ExtensionKey,
  content_hash: string
): Promise<ExtensionPin> {
  return request(`/extension-approvals/${approvalPath(extension)}/pins`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content_hash, version: null, note: null }),
  });
}

export async function deleteExtensionPin(
  extension: ExtensionKey,
  pinId: string
): Promise<void> {
  const response = await fetchWithAuth(
    `${REST_API_PREFIX}/agents/extension-approvals/${approvalPath(extension)}/pins/${encodeURIComponent(pinId)}`,
    { method: 'DELETE' }
  );
  if (!response.ok)
    throw new Error(`Extension request failed (${response.status})`);
}

/** Preserve existing notes/constraints when the table changes only the state.
 *
 * With ``contentHash`` this is "Approve this exact content": the approval and
 * its pin are written in one request.
 */
export async function setExtensionApproval(
  extension: ExtensionKey,
  approval_state: ExtensionApprovalState,
  contentHash?: string
): Promise<void> {
  const path = approvalPath(extension);
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
      ...(contentHash
        ? { pin: { content_hash: contentHash, version: null, note: null } }
        : {}),
    }),
  });
}
