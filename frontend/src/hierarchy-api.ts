/**
 * Console client for the multi-account, account hierarchy and access rule
 * endpoints.
 *
 * These endpoints are served by an extension plugin and do not exist in the
 * open-source server. A 404 on a collection means "capability off" and is
 * raised as {@link CapabilityOffError}, which views answer by hiding. A 404
 * on one item (a subaccount, a shared resource) is {@link NotFoundError}: the
 * id is not visible from this account, which views show as not found and
 * never as another account's data.
 *
 * Only gated views import this module, so it ships in their chunks.
 */
import {
  extractErrorMessage,
  fetchWithAuth,
  getUserProfile,
  invalidateApiCaches,
} from './api';
import { CapabilityOffError } from './capabilities';

export class NotFoundError extends Error {
  constructor(message = 'Not found') {
    super(message);
    this.name = 'NotFoundError';
  }
}

export function isNotFound(error: unknown): error is NotFoundError {
  return error instanceof NotFoundError;
}

type NotFoundMeaning = 'capability-off' | 'not-found';

async function request<T>(
  path: string,
  init: RequestInit & { passive?: boolean } = {},
  onNotFound: NotFoundMeaning = 'capability-off',
  fallback = 'Request failed'
): Promise<T> {
  const headers =
    init.body !== undefined
      ? { 'Content-Type': 'application/json', ...(init.headers || {}) }
      : init.headers;
  const response = await fetchWithAuth(path, {
    ...init,
    ...(headers ? { headers } : {}),
  });
  if (response.status === 404) {
    if (onNotFound === 'capability-off') throw new CapabilityOffError(path);
    throw new NotFoundError();
  }
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new Error(extractErrorMessage(body, fallback));
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

function items<T>(body: unknown): T[] {
  if (Array.isArray(body)) return body as T[];
  const list = (body as { items?: unknown } | null)?.items;
  return Array.isArray(list) ? (list as T[]) : [];
}

const enc = encodeURIComponent;

/** The account the console is signed in to. */
export async function currentAccountId(): Promise<string> {
  return (await getUserProfile()).account_id;
}

// ---------------------------------------------------------------------------
// Memberships and switching (multi_account)

export interface Membership {
  account_id: string;
  account_name: string;
  slug?: string | null;
  parent_account_id?: string | null;
  role?: string | null;
  last_used_at?: string | null;
  is_current?: boolean;
}

export async function getMemberships(): Promise<Membership[]> {
  return items<Membership>(
    await request('/api/v1/me/memberships', { passive: true })
  );
}

export interface TokenPair {
  access_token: string;
  refresh_token: string;
  account_id?: string;
}

const LAST_USED_KEY = 'preloop.accounts.last_used.v1';

/** Account ids this browser switched to, most recent first. */
export function lastUsedAccounts(): string[] {
  try {
    const raw = JSON.parse(localStorage.getItem(LAST_USED_KEY) || '[]');
    return Array.isArray(raw)
      ? raw.filter((id): id is string => typeof id === 'string')
      : [];
  } catch {
    return [];
  }
}

function rememberAccount(accountId: string) {
  const next = [
    accountId,
    ...lastUsedAccounts().filter((id) => id !== accountId),
  ];
  try {
    localStorage.setItem(LAST_USED_KEY, JSON.stringify(next.slice(0, 20)));
  } catch {
    // Ordering is a convenience; storage errors do not block the switch.
  }
}

/**
 * Detail routes and the endpoint that says whether their resource exists.
 * After a switch the console stays on the page when it does, and goes to the
 * dashboard when it does not.
 */
const DETAIL_PROBES: ReadonlyArray<[RegExp, (id: string) => string]> = [
  [/^\/console\/ai-models\/([^/]+)$/, (id) => `/api/v1/ai-models/${id}`],
  [
    /^\/console\/flows\/executions\/([^/]+)$/,
    (id) => `/api/v1/flows/executions/${id}`,
  ],
  [
    /^\/console\/flows\/(?!new$|executions$)([^/]+)$/,
    (id) => `/api/v1/flows/${id}`,
  ],
  [/^\/console\/approval\/([^/]+)$/, (id) => `/api/v1/approval-requests/${id}`],
  [
    /^\/console\/settings\/api-keys\/([^/]+)$/,
    (id) => `/api/v1/auth/api-keys/${id}`,
  ],
];

/** Routes that name one resource by id; unknown ones fall back to the dashboard. */
const OTHER_DETAIL_ROUTES: readonly RegExp[] = [
  /^\/console\/agents\/[^/]+(\/talk)?$/,
  /^\/console\/trackers\/[^/]+(\/issues\/[^/]+)?$/,
  /^\/console\/shared\/[^/]+\/[^/]+$/,
];

/** Where to land after switching, given the page the switch started on. */
export async function switchTarget(pathname: string): Promise<string> {
  const path = pathname.replace(/\/+$/, '') || '/console';
  if (!path.startsWith('/console')) return '/console';
  for (const [pattern, probe] of DETAIL_PROBES) {
    const match = pattern.exec(path);
    if (!match) continue;
    try {
      const response = await fetchWithAuth(probe(match[1]), {
        cache: 'no-store',
      });
      return response.ok ? pathname : '/console';
    } catch {
      return '/console';
    }
  }
  if (OTHER_DETAIL_ROUTES.some((pattern) => pattern.test(path))) {
    return '/console';
  }
  return pathname;
}

/**
 * Switch the console to another membership.
 *
 * The server returns a token pair for the target account. Both tokens are
 * replaced before anything else happens, every cache that belongs to the
 * previous account is dropped, and the console then reloads the current page
 * (or the dashboard when the page's resource does not exist in the new
 * account). A response without both tokens changes nothing.
 */
export async function switchAccount(
  accountId: string,
  navigate: (url: string) => void = (url) => window.location.assign(url)
): Promise<void> {
  const pair = await request<TokenPair>(
    '/api/v1/auth/switch-account',
    { method: 'POST', body: JSON.stringify({ account_id: accountId }) },
    'capability-off',
    'Could not switch account'
  );
  applyTokenPair(pair);
  rememberAccount(accountId);
  const { pathname, search } = window.location;
  const target = await switchTarget(pathname).catch(() => '/console');
  navigate(target === pathname ? pathname + search : target);
}

/** Replace both stored tokens and drop every per-account cache. */
export function applyTokenPair(pair: TokenPair): void {
  if (!pair?.access_token || !pair?.refresh_token) {
    throw new Error('The server did not return a token pair for the account');
  }
  localStorage.setItem('accessToken', pair.access_token);
  localStorage.setItem('refreshToken', pair.refresh_token);
  invalidateApiCaches();
  window.dispatchEvent(
    new CustomEvent('auth-change', { bubbles: true, composed: true })
  );
}

// ---------------------------------------------------------------------------
// Subaccounts (account_hierarchy)

export type Tags = Record<string, string>;

export interface Subaccount {
  id: string;
  name: string;
  slug?: string | null;
  tags?: Tags;
  created_at?: string | null;
}

const subaccountsPath = (accountId: string) =>
  `/api/v1/accounts/${enc(accountId)}/subaccounts`;

export async function listSubaccounts(
  accountId: string
): Promise<Subaccount[]> {
  return items<Subaccount>(await request(subaccountsPath(accountId)));
}

export async function createSubaccount(
  accountId: string,
  body: { name: string; tags?: Tags }
): Promise<Subaccount> {
  return request(
    subaccountsPath(accountId),
    { method: 'POST', body: JSON.stringify(body) },
    'capability-off',
    'Could not create the subaccount'
  );
}

export async function getSubaccount(
  accountId: string,
  subaccountId: string
): Promise<Subaccount> {
  return request(
    `${subaccountsPath(accountId)}/${enc(subaccountId)}`,
    {},
    'not-found'
  );
}

export async function updateSubaccount(
  accountId: string,
  subaccountId: string,
  body: { name?: string; tags?: Tags }
): Promise<Subaccount> {
  return request(
    `${subaccountsPath(accountId)}/${enc(subaccountId)}`,
    { method: 'PATCH', body: JSON.stringify(body) },
    'not-found',
    'Could not update the subaccount'
  );
}

export async function detachSubaccount(
  accountId: string,
  subaccountId: string
): Promise<void> {
  await request(
    `${subaccountsPath(accountId)}/${enc(subaccountId)}/detach`,
    { method: 'POST' },
    'not-found',
    'Could not detach the subaccount'
  );
}

export async function deleteSubaccount(
  accountId: string,
  subaccountId: string
): Promise<void> {
  await request(
    `${subaccountsPath(accountId)}/${enc(subaccountId)}`,
    { method: 'DELETE' },
    'not-found',
    'Could not delete the subaccount'
  );
}

// ---------------------------------------------------------------------------
// Access grants (account_hierarchy)

export type GrantLevel = 'read' | 'operate' | 'admin';

export interface AccessGrant {
  id: string;
  subject_type: 'user' | 'team';
  subject_id: string;
  subject_name?: string | null;
  level: GrantLevel;
  /** `all` includes subaccounts created later. */
  target: 'all' | 'selected';
  subaccount_ids?: string[];
}

const grantsPath = (accountId: string) =>
  `/api/v1/accounts/${enc(accountId)}/access-grants`;

export async function listAccessGrants(
  accountId: string
): Promise<AccessGrant[]> {
  return items<AccessGrant>(await request(grantsPath(accountId)));
}

export async function createAccessGrant(
  accountId: string,
  body: Omit<AccessGrant, 'id' | 'subject_name'>
): Promise<AccessGrant> {
  return request(
    grantsPath(accountId),
    { method: 'POST', body: JSON.stringify(body) },
    'capability-off',
    'Could not create the grant'
  );
}

export async function deleteAccessGrant(
  accountId: string,
  grantId: string
): Promise<void> {
  await request(
    `${grantsPath(accountId)}/${enc(grantId)}`,
    { method: 'DELETE' },
    'not-found',
    'Could not remove the grant'
  );
}

// ---------------------------------------------------------------------------
// Sharing (account_hierarchy)

export type ShareableKind =
  | 'ai_model'
  | 'mcp_server'
  | 'managed_agent'
  | 'flow'
  | 'runner_pool'
  | 'policy';

export type ShareTarget =
  | { type: 'all' }
  | { type: 'selected'; subaccount_ids: string[] }
  | { type: 'tag'; key: string; value: string };

export interface Share {
  id: string;
  resource_type: ShareableKind;
  resource_id: string;
  target: ShareTarget;
}

const sharesPath = (accountId: string) =>
  `/api/v1/accounts/${enc(accountId)}/shares`;

export async function listShares(
  accountId: string,
  resourceType: ShareableKind,
  resourceId: string
): Promise<Share[]> {
  const query = `?resource_type=${enc(resourceType)}&resource_id=${enc(resourceId)}`;
  return items<Share>(await request(sharesPath(accountId) + query));
}

export async function createShare(
  accountId: string,
  body: Omit<Share, 'id'>
): Promise<Share> {
  return request(
    sharesPath(accountId),
    { method: 'POST', body: JSON.stringify(body) },
    'capability-off',
    'Could not share the resource'
  );
}

export async function deleteShare(
  accountId: string,
  shareId: string
): Promise<void> {
  await request(
    `${sharesPath(accountId)}/${enc(shareId)}`,
    { method: 'DELETE' },
    'not-found',
    'Could not stop sharing'
  );
}

/** How a resource shared by a parent looks from a subaccount. */
export interface SharedResource {
  kind: ShareableKind;
  id: string;
  name: string;
  provider?: string | null;
  identifier?: string | null;
  description?: string | null;
  price?: string | null;
  shared_from: { account_id: string; account_name: string };
}

export async function getSharedResource(
  accountId: string,
  kind: string,
  resourceId: string
): Promise<SharedResource> {
  return request(
    `/api/v1/accounts/${enc(accountId)}/shared-resources/${enc(kind)}/${enc(resourceId)}`,
    {},
    'not-found'
  );
}

// ---------------------------------------------------------------------------
// Usage and attention rollups (account_hierarchy)

export interface UsageRollupRow {
  subaccount_id: string;
  subaccount_name: string;
  model?: string | null;
  day?: string | null;
  requests: number;
  cost_usd: number;
}

export interface UsageRollup {
  rows: UsageRollupRow[];
  total_cost_usd?: number;
}

export async function getUsageRollup(
  accountId: string,
  params: { subaccount_id?: string; start?: string; end?: string } = {}
): Promise<UsageRollup> {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value) query.set(key, value);
  }
  const suffix = query.toString() ? `?${query}` : '';
  const body = await request<Partial<UsageRollup>>(
    `/api/v1/accounts/${enc(accountId)}/usage/rollup${suffix}`,
    { passive: true }
  );
  return {
    rows: Array.isArray(body?.rows) ? body.rows : [],
    total_cost_usd: body?.total_cost_usd,
  };
}

export interface AttentionRollupRow {
  subaccount_id: string;
  subaccount_name: string;
  count: number;
}

export async function getAttentionRollup(
  accountId: string
): Promise<AttentionRollupRow[]> {
  return items<AttentionRollupRow>(
    await request(`/api/v1/accounts/${enc(accountId)}/attention/rollup`, {
      passive: true,
    })
  );
}

// ---------------------------------------------------------------------------
// Tags (abac_rules)

export interface ResourceTags {
  tags: Tags;
  /** Keys the caller may read but not change (governed by the parent). */
  governed_keys?: string[];
}

const tagsPath = (kind: string, resourceId: string) =>
  `/api/v1/tags/${enc(kind)}/${enc(resourceId)}`;

export async function getTags(
  kind: string,
  resourceId: string
): Promise<ResourceTags> {
  const body = await request<Partial<ResourceTags>>(tagsPath(kind, resourceId));
  return { tags: body?.tags ?? {}, governed_keys: body?.governed_keys ?? [] };
}

export async function setTags(
  kind: string,
  resourceId: string,
  tags: Tags
): Promise<ResourceTags> {
  const body = await request<Partial<ResourceTags>>(
    tagsPath(kind, resourceId),
    { method: 'PUT', body: JSON.stringify({ tags }) },
    'not-found',
    'Could not save tags'
  );
  return { tags: body?.tags ?? tags, governed_keys: body?.governed_keys ?? [] };
}

// ---------------------------------------------------------------------------
// Access rules (abac_rules)

export type RuleMode = 'additive' | 'require_permit';

export interface AccessRule {
  id?: string;
  name: string;
  effect: 'permit' | 'forbid';
  actions: string[];
  scope?: 'self' | 'subaccounts' | 'self_and_subaccounts';
  subject?: Record<string, unknown>;
  resource?: Record<string, unknown>;
  unless?: Record<string, unknown>;
  is_enabled?: boolean;
}

export interface AccessRuleSet {
  rules: AccessRule[];
  /** Parent rules that apply here; read-only in this account. */
  inherited: Array<AccessRule & { account_name?: string }>;
  modes: Record<string, RuleMode>;
}

export async function getAccessRules(): Promise<AccessRuleSet> {
  const body = await request<Partial<AccessRuleSet>>('/api/v1/access/rules');
  return {
    rules: body?.rules ?? [],
    inherited: body?.inherited ?? [],
    modes: body?.modes ?? {},
  };
}

export async function saveAccessRules(
  rules: AccessRule[]
): Promise<AccessRuleSet> {
  await request(
    '/api/v1/access/rules',
    { method: 'PUT', body: JSON.stringify({ rules }) },
    'capability-off',
    'Could not save the rules'
  );
  return getAccessRules();
}

export async function exportAccessRulesYaml(): Promise<string> {
  const body = await request<{ yaml?: string }>('/api/v1/access/rules/export');
  return body?.yaml ?? '';
}

export async function applyAccessRulesYaml(yaml: string): Promise<void> {
  await request(
    '/api/v1/access/rules/apply',
    { method: 'POST', body: JSON.stringify({ yaml }) },
    'capability-off',
    'Could not apply the rules'
  );
}

export interface ExplainRequest {
  subject: string;
  action: string;
  resource: string;
}

export interface ExplainResult {
  effect: 'permit' | 'deny';
  reason: string;
  rule_ids?: string[];
}

export async function explainAccess(
  body: ExplainRequest
): Promise<ExplainResult> {
  return request(
    '/api/v1/access/explain',
    { method: 'POST', body: JSON.stringify(body) },
    'capability-off',
    'Could not explain the decision'
  );
}

export interface ModePreview {
  action: string;
  mode: RuleMode;
  /** Subjects that hold access now and would lose it under the new mode. */
  losing_access: Array<{ kind: string; id: string; name?: string | null }>;
  /** Echoed back on save, so the server knows this preview was shown. */
  preview_token: string;
}

export async function previewMode(
  action: string,
  mode: RuleMode
): Promise<ModePreview> {
  return request(
    '/api/v1/access/modes/preview',
    { method: 'POST', body: JSON.stringify({ action, mode }) },
    'capability-off',
    'Could not preview the change'
  );
}

export async function setMode(
  action: string,
  mode: RuleMode,
  previewToken?: string
): Promise<void> {
  await request(
    `/api/v1/access/modes/${enc(action)}`,
    {
      method: 'PUT',
      body: JSON.stringify({ mode, preview_token: previewToken ?? null }),
    },
    'capability-off',
    'Could not change the mode'
  );
}
