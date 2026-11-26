import { fetchWithAuth, getUserProfile } from './api';
import { REST_API_PREFIX } from './api-path';
import { hasFleetPermission } from './device-fleet-api';
import type { ExtensionKey } from './extension-inventory-api';

export type ExtensionContentFileStatus =
  | 'stored'
  | 'binary'
  | 'too_large'
  | 'symlink'
  | 'private_key'
  | 'over_budget'
  | 'not_selected'
  | 'unreadable';

export interface ExtensionContentFile {
  path: string;
  sha256: string | null;
  size: number | null;
  status: ExtensionContentFileStatus;
  text: string | null;
  /** Placeholder type to how many values it replaced. */
  redactions: Record<string, number>;
  /** True when the text arrived unredacted and matched its hash. */
  verified: boolean;
}

export interface ExtensionContentHook {
  matcher: string | null;
  command_hash: string;
  status: 'stored' | 'too_large' | 'private_key' | 'over_budget';
  command: string | null;
  redactions: Record<string, number>;
  verified: boolean;
}

export interface ExtensionContent {
  extension_kind: ExtensionKey['extension_kind'];
  content_hash: string;
  files: ExtensionContentFile[];
  hooks: ExtensionContentHook[];
  /** Set when the device sent no text for the whole extension. */
  withheld: 'excluded' | 'too_large' | null;
  verification: 'verified' | 'partial' | 'unverified' | 'none';
  redacted: boolean;
  redaction_count: number;
  withheld_count: number;
  stored_bytes: number;
  redaction_rules: string | null;
  collector_version: string | null;
  first_stored_at: string;
  last_reported_at: string;
  retention_days: number;
}

export interface ExtensionContentSettings {
  collect_enabled: boolean;
  /** True until an administrator has acknowledged the new data class. */
  notice_pending: boolean;
  stored_contents: number;
  stored_bytes: number;
  retention_days: number;
  updated_by_user_id: string | null;
  updated_at: string | null;
}

const SETTINGS_PATH = `${REST_API_PREFIX}/account/extension-content/settings`;

/**
 * Read the stored content behind one inventory hash.
 *
 * Every successful call writes an audit row on the server, so call it for a
 * deliberate read, never to prefetch. Null means nothing is stored.
 */
export async function getExtensionContent(
  kind: ExtensionKey['extension_kind'],
  contentHash: string
): Promise<ExtensionContent | null> {
  const response = await fetchWithAuth(
    `${REST_API_PREFIX}/agents/extension-content/${encodeURIComponent(kind)}/${encodeURIComponent(contentHash)}`
  );
  if (response.status === 404) return null;
  if (!response.ok)
    throw new Error(`Extension content request failed (${response.status})`);
  return response.json() as Promise<ExtensionContent>;
}

export async function getExtensionContentSettings(): Promise<ExtensionContentSettings> {
  const response = await fetchWithAuth(SETTINGS_PATH);
  if (!response.ok)
    throw new Error(`Extension content settings failed (${response.status})`);
  return response.json() as Promise<ExtensionContentSettings>;
}

export async function updateExtensionContentSettings(change: {
  collect_enabled?: boolean;
  acknowledge_notice?: boolean;
}): Promise<ExtensionContentSettings> {
  const response = await fetchWithAuth(SETTINGS_PATH, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(change),
  });
  if (!response.ok)
    throw new Error(`Extension content settings failed (${response.status})`);
  return response.json() as Promise<ExtensionContentSettings>;
}

/** Whether the signed-in user may change the account switch. */
export async function canManageExtensionContent(): Promise<boolean> {
  return hasFleetPermission(await getUserProfile(), 'manage_policies');
}
