import { fetchWithAuth, getUserProfile, onApiCachesInvalidated } from './api';
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
export interface ExtensionDetails {
  name?: string;
  description?: string;
  description_withheld?: boolean;
  invocation?: string;
  script_count?: number | null;
  plugin?: string;
  author?: string;
  bundles?: {
    skills: number;
    commands: number;
    hooks: number;
    mcp_servers: number;
    agents: number;
  } | null;
  hook_matchers?: Array<{
    matcher?: string;
    matcher_withheld?: boolean;
    count: number;
    commands: Array<{
      executable: string;
      preloop_managed: boolean;
      script?: boolean;
      in_project?: boolean;
    }>;
  }>;
  /** Schema 5 decision metadata; hosts only, never URLs or paths. */
  marketplace?: string;
  homepage_host?: string;
  repository_host?: string;
  license?: string;
  modified_at?: string;
  tools_declared?: boolean;
  allowed_tools?: string[];
  hook_events?: string[];
  bundle_names?: {
    skills?: string[];
    commands?: string[];
    agents?: string[];
    mcp_servers?: string[];
  } | null;
  transport?: '' | 'stdio' | 'http' | 'sse';
  command?: string;
  url_host?: string;
}
export type ExtensionCapabilityKey =
  | 'shell'
  | 'files_read'
  | 'files_write'
  | 'network'
  | 'hooks'
  | 'mcp'
  | 'scripts'
  | 'unknown';
export interface ExtensionCapability {
  key: ExtensionCapabilityKey;
  label: string;
}
export interface ExtensionKey {
  details?: ExtensionDetails | null;
  agent_kind: string;
  extension_kind: 'plugin' | 'skill' | 'hook' | 'command' | 'mcp_server';
  extension_id: string;
  approval_state: ExtensionApprovalState;
  /** Derived on the server from details; absent on older servers. */
  capabilities?: ExtensionCapability[];
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
  /** Earliest first sighting across devices; absent on older servers. */
  first_seen_at?: string | null;
  /** People whose credentials reported it; absent on older servers. */
  people_count?: number;
}
export interface ExtensionCoverage {
  fingerprint: string;
  collector_version: string | null;
  completeness: 'complete' | 'partial' | 'unknown';
  reasons: string[];
  /** When the device's latest report ended; absent on older servers. */
  reported_at?: string | null;
}
export interface ExtensionPage {
  coverage?: ExtensionCoverage[];
  items: (InstalledExtension | ExtensionAggregate)[];
  next_cursor: string | null;
  completeness: 'complete' | 'partial' | 'unknown';
}
export interface ExtensionFilters {
  q?: string;
  extension_kind?: ExtensionKey['extension_kind'];
  extension_id?: string;
  fingerprint?: string;
  agent_kind?: string;
  state?: ExtensionEffectiveState;
  source_kind?: ExtensionSource;
  capability?: ExtensionCapabilityKey;
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

/** Count account-wide identities without reading rows or approval decisions. */
export async function getExtensionCount(): Promise<number> {
  return (await request<{ count: number }>('/discovery-extensions/count'))
    .count;
}

export async function getExtensions(
  filters: ExtensionFilters = {}
): Promise<ExtensionPage> {
  const params = new URLSearchParams({ limit: '200' });
  if (!filters.fingerprint) params.set('group', 'extension');
  for (const [key, value] of Object.entries(filters))
    if (value) params.set(key, value);
  // The inventory DTO omits constraints. Read the account's decisions in
  // parallel with the page, through the paginated list, not once per row.
  const [page, constraints] = await Promise.all([
    request<ExtensionPage>(`/discovery-extensions?${params}`),
    cachedVersionConstraints().catch(() => null),
  ]);
  return {
    ...page,
    items: page.items.map((row) => ({
      ...row,
      // Undefined means the constraint could not be read; null means none.
      version_constraint: constraints
        ? (constraints.get(approvalPath(row)) ?? null)
        : undefined,
    })),
  };
}

/** How long one constraint scan serves later pages and filter changes. */
export const CONSTRAINT_CACHE_MS = 60_000;
let constraintCache: {
  at: number;
  value: Promise<Map<string, string | null>>;
} | null = null;

/** Drop the constraint scan so the next read sees a decision just written. */
export function invalidateVersionConstraints(): void {
  constraintCache = null;
}

// Constraints are account data: sign-out and account switches drop them.
onApiCachesInvalidated(invalidateVersionConstraints);

/**
 * One account scan serves the first page, Load more and filter changes.
 * The console never edits constraints, so a short reuse window is safe; a
 * failed scan is not kept.
 */
function cachedVersionConstraints(): Promise<Map<string, string | null>> {
  const now = Date.now();
  if (!constraintCache || now - constraintCache.at > CONSTRAINT_CACHE_MS) {
    const value = getVersionConstraints();
    constraintCache = { at: now, value };
    value.catch(() => {
      if (constraintCache?.value === value) constraintCache = null;
    });
  }
  return constraintCache.value;
}

/** Every stored version constraint, keyed by approval path. */
async function getVersionConstraints(): Promise<Map<string, string | null>> {
  const constraints = new Map<string, string | null>();
  let cursor: string | null = null;
  // Bounded: 20 pages of 500 decisions.
  for (let page = 0; page < 20; page++) {
    const params = new URLSearchParams({ limit: '500' });
    if (cursor) params.set('cursor', cursor);
    const list = await request<{
      items: (ExtensionKey & { version_constraint: string | null })[];
      next_cursor: string | null;
    }>(`/extension-approvals?${params}`);
    for (const row of list.items)
      constraints.set(approvalPath(row), row.version_constraint);
    cursor = list.next_cursor;
    if (!cursor) return constraints;
  }
  throw new Error('Too many extension decisions to read constraints');
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
  invalidateVersionConstraints();
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

/** Change account-wide states in one transaction, with no pin or constraint edits. */
export async function setBulkExtensionApproval(
  rows: ExtensionKey[],
  approval_state: 'approved' | 'unapproved'
): Promise<void> {
  await request('/extension-approvals/bulk', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      approval_state,
      extensions: rows.map(({ agent_kind, extension_kind, extension_id }) => ({
        agent_kind,
        extension_kind,
        extension_id,
      })),
    }),
  });
  invalidateVersionConstraints();
}

/** Read device observations for exactly one extension, without aggregation. */
export async function getExtensionDevices(
  row: ExtensionKey,
  cursor?: string
): Promise<ExtensionPage> {
  const params = new URLSearchParams({
    limit: '200',
    agent_kind: row.agent_kind,
    extension_kind: row.extension_kind,
    extension_id: row.extension_id,
  });
  if (cursor) params.set('cursor', cursor);
  return request<ExtensionPage>(`/discovery-extensions?${params}`);
}
