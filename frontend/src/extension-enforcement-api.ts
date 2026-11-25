import { fetchWithAuth } from './api';
import { REST_API_PREFIX } from './api-path';

export type OverlayPlatform = 'darwin' | 'linux' | 'windows';
export interface EnforcementOptions {
  block_unapproved_plugins: boolean;
  restrict_marketplaces: boolean;
  managed_hooks_only: boolean;
  block_user_project_skills: boolean;
  disable_skill_shell: boolean;
  disable_sideload_flags: boolean;
}
export interface Marketplace {
  name: string;
  state: 'allowed' | 'blocked';
  register: boolean;
  source: { source: string; [key: string]: unknown };
}
export interface EnforcementPolicy {
  revision: number;
  options: EnforcementOptions;
  marketplaces: Marketplace[];
  updated_at?: string | null;
}
export interface RenderedOverlay {
  settings_json: string;
  policy_revision_hash: string;
  keys: string[];
  omitted: Array<{ key: string; subject: string | null; reason: string }>;
  inputs: {
    policy_revision: number;
    approvals_updated_at: string | null;
    approval_count: number;
  };
  platform: OverlayPlatform;
  target_path: string;
  minimum_claude_code_version: string;
  vendor_console_status: 'unverified_for_preloop_routed_agents';
}
export interface OverlayRegistration {
  manifest: { app_id: string; platform: string; policy_revision_hash: string };
  registered_at: string;
}
const base = `${REST_API_PREFIX}/agents/extension-policy/claude_code`;
async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetchWithAuth(url, init);
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    const detail =
      typeof body.detail === 'string'
        ? body.detail
        : Array.isArray(body.detail)
          ? body.detail.map((d: { msg: string }) => d.msg).join('; ')
          : `Request failed (${response.status})`;
    throw new Error(detail);
  }
  return response.json() as Promise<T>;
}
export const getEnforcementPolicy = (): Promise<EnforcementPolicy> =>
  request(base);
export const saveEnforcementPolicy = (
  policy: EnforcementPolicy
): Promise<EnforcementPolicy> =>
  request(base, {
    method: 'PUT',
    headers: {
      'Content-Type': 'application/json',
      'If-Match': String(policy.revision),
    },
    body: JSON.stringify({
      marketplaces: policy.marketplaces,
      options: policy.options,
    }),
  });
export const renderEnforcementPolicy = (
  platform: OverlayPlatform
): Promise<RenderedOverlay> =>
  request(`${base}/managed-settings?platform=${platform}`);
export async function getOverlayRegistrations(): Promise<
  OverlayRegistration[]
> {
  return (
    await request<{ bundles: OverlayRegistration[] }>(
      `${REST_API_PREFIX}/agents/managed-config/bundles`
    )
  ).bundles;
}
