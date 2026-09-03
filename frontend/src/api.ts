import { LitElement } from 'lit';
import { Router } from '@vaadin/router';
import { DEFAULT_SIMILARITY_THRESHOLD } from './config';
import { PermissionError, permissionErrorFromResponse } from './permissions';
import type {
  ApprovalBypass,
  ApprovalBypassMode,
  ApprovalBypassStatus,
  FetchIssuesListParams,
  SearchIssuesParams,
  SearchIssuesResponse,
  ApiKey,
  Project,
  Organization,
  Issue,
  DuplicatePair,
  DuplicatesResponse,
  IssueComplianceResult,
  CompliancePromptMetadata,
  ComplianceSuggestion,
  DependencyPair,
  DependencyResponse,
  FlowGatewayEventsResponse,
  FlowGatewayEvent,
  AccountManagedAgentListResponse,
  AgentControlCommandRequest,
  AgentControlCommandResponse,
  AgentControlVoiceTranscriptRequest,
  ManagedAgentDetailResponse,
  ManagedAgentSummary,
  ManagedAgentModelBindingSummary,
  ManagedAgentUpdateRequest,
  AccountGovernanceDefaults,
  AccountGovernanceDefaultsResponse,
  SubjectGovernanceConfig,
  SubjectGovernanceResponse,
  AccountGatewayUsageSearchResponse,
  AccountRuntimeSessionDetailResponse,
  AccountRuntimeSessionListResponse,
  RuntimeSessionSummary,
  RuntimeSessionUpdateRequest,
  RuntimeSessionActivityListResponse,
  RuntimeSessionRequestListResponse,
  RuntimeSessionSummaryInsight,
  RuntimeSessionInteractionSummary,
  RuntimeSessionOptimizationJobStatusResponse,
  RuntimeSessionOptimizationJobSubmitResponse,
  RuntimeSessionOptimizationResponse,
  RuntimeSessionReplayResponse,
  RuntimeSessionOptimizationActionSpec,
  RuntimeSessionOptimizationAppliedAction,
  RuntimeSessionOptimizationActionListResponse,
  ToolOutputFilter,
  ToolOutputFilterListResponse,
  ToolOutputFilterCreateRequest,
  AccountGatewayUsageSummaryResponse,
  AccountRateLimitReportResponse,
  FlowGatewayUsageSummaryResponse,
  AIModelGatewayUsageSummaryResponse,
  ApiKeyGatewayUsageSummaryResponse,
  AIModelRuntimeSessionListResponse,
  AIModelGatewayUsageSearchResponse,
  AIModel,
  DashboardTelemetryResponse,
  CostAnalyticsSummaryResponse,
  CostReconciliationResponse,
  ProviderBillingConnection,
  RepriceResponse,
  ToolUsageStatsResponse,
  ModelPriceOverride,
  ModelPriceOverrideCreate,
  ModelPriceOverrideUpdate,
  SpeechToTextResponse,
  TextToSpeechRequest,
  ApprovalDecisionOptions,
} from './types';

// Global refresh promise to prevent concurrent refresh requests
interface RefreshResult {
  token: string | null;
  // True when the refresh token was definitively rejected (401/403) and the
  // session is over. False for transient failures (5xx, network) where the
  // session must be preserved so a deploy blip does not log the user out.
  terminal: boolean;
}
let refreshPromise: Promise<RefreshResult> | null = null;

// Short-TTL in-memory caches with single-flight dedupe for hot auth/bootstrap
// endpoints. Invalidated on logout / auth-change so navigations reuse results
// without serving stale identity or feature flags across sessions.
const FEATURES_CACHE_TTL_MS = 45_000;
const USER_PROFILE_CACHE_TTL_MS = 45_000;

type TimedCacheEntry<T> = {
  data: T;
  expiresAt: number;
};

let featuresCache: TimedCacheEntry<FeaturesResponse> | null = null;
let featuresInflight: Promise<FeaturesResponse> | null = null;
let featuresEpoch = 0;
let userProfileCache: TimedCacheEntry<UserProfile> | null = null;
let userProfileInflight: Promise<UserProfile> | null = null;
let userProfileEpoch = 0;

export function invalidateApiCaches(): void {
  featuresCache = null;
  featuresInflight = null;
  featuresEpoch += 1;
  userProfileCache = null;
  userProfileInflight = null;
  userProfileEpoch += 1;
  if (typeof sessionStorage !== 'undefined') {
    try {
      sessionStorage.removeItem('preloop.agents.gateway_summary.v1');
      for (let i = sessionStorage.length - 1; i >= 0; i -= 1) {
        const key = sessionStorage.key(i);
        if (key?.startsWith('preloop.cost.previous_summary.v1:')) {
          sessionStorage.removeItem(key);
        }
      }
    } catch {
      // ignore storage errors
    }
  }
}

if (typeof window !== 'undefined') {
  window.addEventListener('storage', (event) => {
    // Notify the app when the accessToken is changed by another tab
    if (event.key === 'accessToken') {
      window.dispatchEvent(
        new CustomEvent('auth-change', { bubbles: true, composed: true })
      );
    }
  });
  window.addEventListener('auth-change', () => {
    invalidateApiCaches();
  });
}

export function extractErrorMessage(
  errorData: any,
  defaultMessage: string
): string {
  // OpenAI-error-shaped bodies from the gateway:
  // { "error": { "message": ..., "type": ..., "code": ..., "provider_detail"?: ... } }
  // The message is already scrubbed server-side; prefer it so upstream provider
  // failures (e.g. "No allowed providers are available...") reach the user
  // instead of a generic fallback.
  const gatewayError = errorData?.error;
  if (gatewayError && typeof gatewayError === 'object') {
    if (
      typeof gatewayError.message === 'string' &&
      gatewayError.message.trim()
    ) {
      return gatewayError.message;
    }
    if (
      typeof gatewayError.provider_detail === 'string' &&
      gatewayError.provider_detail.trim()
    ) {
      return gatewayError.provider_detail;
    }
  }
  if (errorData && errorData.detail) {
    if (Array.isArray(errorData.detail)) {
      return errorData.detail
        .map((item: any) => item.msg || JSON.stringify(item))
        .join(', ');
    } else if (typeof errorData.detail === 'object') {
      return JSON.stringify(errorData.detail);
    }
    return String(errorData.detail);
  }
  return defaultMessage;
}

async function attemptRefresh(refreshTokenValue: string): Promise<Response> {
  return fetch(`/api/v1/auth/refresh`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ refresh_token: refreshTokenValue }),
  });
}

function endSessionAndRedirect(): void {
  localStorage.removeItem('accessToken');
  localStorage.removeItem('refreshToken');

  if (typeof window !== 'undefined') {
    window.dispatchEvent(
      new CustomEvent('auth-change', { bubbles: true, composed: true })
    );
    if (
      !window.location.pathname.startsWith('/login') &&
      !window.location.pathname.startsWith('/register')
    ) {
      localStorage.setItem(
        'loginRedirect',
        window.location.pathname + window.location.search + window.location.hash
      );
    }
  }

  Router.go('/login');
}

async function refreshToken(): Promise<RefreshResult> {
  // If a refresh is already in progress, wait for it
  if (refreshPromise) {
    return refreshPromise;
  }

  // Start a new refresh
  refreshPromise = (async (): Promise<RefreshResult> => {
    try {
      const refreshTokenValue = localStorage.getItem('refreshToken');
      if (!refreshTokenValue) {
        console.error('No refresh token available');
        endSessionAndRedirect();
        return { token: null, terminal: true };
      }

      let response: Response | null = null;
      try {
        response = await attemptRefresh(refreshTokenValue);
      } catch {
        // Network error (offline, deploy blip). Retry once below.
        response = null;
      }

      // Retry once on transient failure (network error or 5xx). Only a
      // definitive 401/403 means the refresh token itself is dead.
      if (!response || response.status >= 500) {
        try {
          response = await attemptRefresh(refreshTokenValue);
        } catch {
          response = null;
        }
      }

      if (!response || response.status >= 500) {
        // Still transient: keep the session. The next 401 will try again.
        console.error('Token refresh failed transiently; keeping session');
        return { token: null, terminal: false };
      }

      if (!response.ok) {
        // Definitive rejection (401/403/4xx): session is over.
        console.error(`Token refresh rejected with status ${response.status}`);
        endSessionAndRedirect();
        return { token: null, terminal: true };
      }

      const data = await response.json();
      localStorage.setItem('accessToken', data.access_token);
      localStorage.setItem('refreshToken', data.refresh_token);

      if (typeof window !== 'undefined') {
        // Dispatch to current window
        window.dispatchEvent(
          new CustomEvent('auth-change', { bubbles: true, composed: true })
        );
      }

      return { token: data.access_token, terminal: false };
    } catch (error) {
      // Unexpected error (e.g. malformed JSON body). Treat as transient so a
      // broken deploy cannot log everyone out.
      console.error('Error refreshing token:', error);
      return { token: null, terminal: false };
    } finally {
      // Clear the refresh promise so future requests can refresh again
      refreshPromise = null;
    }
  })();

  return refreshPromise;
}

export async function fetchWithAuth(
  url: string,
  options: RequestInit = {}
): Promise<Response> {
  let accessToken = localStorage.getItem('accessToken');

  if (!accessToken) {
    // This case should ideally not be hit if the app is correctly protecting routes
    console.error('No access token found');
    if (
      typeof window !== 'undefined' &&
      !window.location.pathname.startsWith('/login') &&
      !window.location.pathname.startsWith('/register')
    ) {
      localStorage.setItem(
        'loginRedirect',
        window.location.pathname + window.location.search + window.location.hash
      );
    }
    Router.go('/login');
    throw new Error('Not authenticated');
  }

  const headers = new Headers(options.headers || {});
  headers.set('Authorization', `Bearer ${accessToken}`);
  options.headers = headers;

  let response = await fetch(url, options);

  if (response.status === 401) {
    // Check if the 401 is actually a gateway upstream error (e.g., invalid Anthropic or OpenAI API key).
    // Gateway errors return a JSON envelope with an 'error' object rather than FastAPI's 'detail'.
    let isUpstreamGatewayError = false;
    try {
      const errorData = await response.clone().json();
      if (errorData && typeof errorData === 'object' && 'error' in errorData) {
        isUpstreamGatewayError = true;
      }
    } catch (e) {
      // Ignore parse errors, assume it's a normal access token expiration
    }

    if (isUpstreamGatewayError) {
      console.log(
        'Gateway upstream returned 401, returning error directly without refreshing token'
      );
      return response;
    }

    console.log('Access token expired, attempting to refresh...');

    // If another tab or process already refreshed the token, use the new one directly
    const currentToken = localStorage.getItem('accessToken');
    if (currentToken && currentToken !== accessToken) {
      console.log('Token was already refreshed, retrying request');
      headers.set('Authorization', `Bearer ${currentToken}`);
      options.headers = headers;
      return fetch(url, options);
    }

    const refreshResult = await refreshToken();
    if (refreshResult.token) {
      headers.set('Authorization', `Bearer ${refreshResult.token}`);
      options.headers = headers;
      // Retry the request with the new token
      response = await fetch(url, options);
    } else if (refreshResult.terminal) {
      // Refresh token definitively rejected; refreshToken() already cleared
      // storage and redirected to /login.
      throw new Error('Failed to refresh token, redirecting to login.');
    }
    // Transient refresh failure: fall through and return the original 401
    // response without destroying the session. The next request retries.
  }

  if (response.status === 429) {
    window.dispatchEvent(
      new CustomEvent('show-upgrade-modal', {
        bubbles: true,
        composed: true,
      })
    );
  }

  if (response.status === 402) {
    // Premium gate (T2): the endpoint answered with the upgrade contract.
    // Read the feature from a clone so callers can still consume the body.
    let feature = '';
    try {
      const body = await response.clone().json();
      if (body?.detail?.code === 'upgrade_required') {
        feature = String(body.detail.feature || '');
      }
    } catch {
      // Non-JSON 402 — still show the generic upgrade modal.
    }
    window.dispatchEvent(
      new CustomEvent('show-upgrade-modal', {
        detail: { feature, code: 'upgrade_required' },
        bubbles: true,
        composed: true,
      })
    );
  }

  return response;
}

/**
 * Start a Stripe checkout from inside the console (upgrade-modal flow).
 *
 * ``returnTo`` must be a same-origin path; checkout-success reconciles the
 * subscription by session_id (webhook-independent) and redirects back there.
 * Single shared helper — the modal, pricing page, and any future upgrade
 * button all call this, so plan/interval/return handling never drifts.
 */
let _checkoutInFlight = false;
export async function startCheckout(
  planId: string,
  interval: 'month' | 'year',
  returnTo?: string
): Promise<void> {
  if (_checkoutInFlight) return; // double-click guard: one Stripe tab
  _checkoutInFlight = true;
  try {
    const response = await fetchWithAuth(
      '/api/v1/billing/create-checkout-session',
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          plan_id: planId,
          interval,
          return_to: returnTo ?? null,
        }),
      }
    );
    if (!response.ok) {
      throw new Error('Failed to create checkout session');
    }
    const result = await response.json();
    if (result.action === 'redirect' && result.url) {
      window.location.href = result.url;
      return;
    }
    throw new Error('Unexpected checkout response');
  } finally {
    _checkoutInFlight = false;
  }
}

export interface Entitlements {
  premium: boolean;
  reason: string;
}

/** Authed boot-time entitlement state (passive UI only; 402s are the gate). */
export async function getEntitlements(): Promise<Entitlements> {
  const response = await fetchWithAuth('/api/v1/billing/entitlements');
  if (!response.ok) {
    // Fail-open for UI purposes: never degrade the console over a hint.
    return { premium: true, reason: 'unavailable' };
  }
  return response.json();
}

export type { UserPermissions } from './permissions';
export {
  PermissionError,
  hasPermission,
  hasAnyPermission,
  isRbacActive,
  permissionErrorFromResponse,
} from './permissions';
export async function fetchPublic(
  url: string,
  options: RequestInit = {}
): Promise<Response> {
  const response = await fetch(url, options);
  // You might want to add basic error handling here if needed
  return response;
}

export class AuthedElement extends LitElement {
  protected async fetchData(url: string, options: RequestInit = {}) {
    try {
      const response = await fetchWithAuth(url, options);
      if (response.status === 403) {
        throw await permissionErrorFromResponse(response);
      }
      if (!response.ok) {
        throw new Error(`HTTP error! status: ${response.status}`);
      }
      return await response.json();
    } catch (error) {
      console.error('Failed to fetch data:', error);
      // Re-throw permission errors so views can render a dedicated empty state
      // instead of silently collapsing into a blank page.
      if (error instanceof PermissionError) {
        throw error;
      }
      // The fetchWithAuth function handles redirection on auth failure
      return null;
    }
  }
}

export async function getApiUsageStats() {
  const response = await fetchWithAuth('/api/v1/auth/api-usage');
  if (!response.ok) {
    throw new Error('Failed to fetch API usage stats');
  }
  return response.json();
}

export interface GatewayUsageSummaryParams {
  startDate?: string;
  endDate?: string;
  runtimePrincipalId?: string;
  includeBreakdown?: boolean;
}

export interface GatewayUsageSearchParams extends GatewayUsageSummaryParams {
  query?: string;
  providerName?: string;
  modelAlias?: string;
  flowId?: string;
  runtimeSessionId?: string;
  sessionSourceType?: string;
  limit?: number;
  offset?: number;
}

export interface RuntimeSessionListParams extends GatewayUsageSummaryParams {
  query?: string;
  sessionSourceType?: string;
  status?: 'all' | 'active' | 'ended';
  limit?: number;
  offset?: number;
}

export interface RuntimeSessionDetailParams {}

export interface RuntimeSessionInteractionsParams {
  interactionQuery?: string;
  interactionLimit?: number;
  interactionOffset?: number;
}

export interface ManagedAgentListParams {
  query?: string;
  tags?: string;
  ownerUsername?: string;
  agentKind?: string;
  lastSeenAfter?: string;
  status?: 'all' | 'active' | 'ended';
  limit?: number;
  offset?: number;
}

function buildGatewayUsageQuery(params: GatewayUsageSearchParams = {}): string {
  const queryParams = new URLSearchParams();

  if (params.startDate) {
    queryParams.set('start_date', params.startDate);
  }

  if (params.endDate) {
    queryParams.set('end_date', params.endDate);
  }

  if (params.query) {
    queryParams.set('query', params.query);
  }

  if (params.providerName) {
    queryParams.set('provider_name', params.providerName);
  }

  if (params.modelAlias) {
    queryParams.set('model_alias', params.modelAlias);
  }

  if (params.flowId) {
    queryParams.set('flow_id', params.flowId);
  }

  if (params.runtimeSessionId) {
    queryParams.set('runtime_session_id', params.runtimeSessionId);
  }

  if (params.runtimePrincipalId) {
    queryParams.set('runtime_principal_id', params.runtimePrincipalId);
  }

  if (params.sessionSourceType) {
    queryParams.set('session_source_type', params.sessionSourceType);
  }

  if (params.includeBreakdown !== undefined) {
    queryParams.set('include_breakdown', String(params.includeBreakdown));
  }

  if (typeof params.limit === 'number') {
    queryParams.set('limit', String(params.limit));
  }

  if (typeof params.offset === 'number') {
    queryParams.set('offset', String(params.offset));
  }

  const queryString = queryParams.toString();
  return queryString ? `?${queryString}` : '';
}

function buildRuntimeSessionListQuery(
  params: RuntimeSessionListParams = {}
): string {
  const queryParams = new URLSearchParams();

  if (params.startDate) {
    queryParams.set('start_date', params.startDate);
  }
  if (params.endDate) {
    queryParams.set('end_date', params.endDate);
  }
  if (params.query) {
    queryParams.set('query', params.query);
  }
  if (params.sessionSourceType) {
    queryParams.set('session_source_type', params.sessionSourceType);
  }
  if (params.status) {
    queryParams.set('status', params.status);
  }
  if (typeof params.limit === 'number') {
    queryParams.set('limit', String(params.limit));
  }
  if (typeof params.offset === 'number') {
    queryParams.set('offset', String(params.offset));
  }

  const queryString = queryParams.toString();
  return queryString ? `?${queryString}` : '';
}

function buildManagedAgentListQuery(
  params: ManagedAgentListParams = {}
): string {
  const queryParams = new URLSearchParams();

  if (params.query) {
    queryParams.set('query', params.query);
  }
  if (params.tags) {
    queryParams.set('tags', params.tags);
  }
  if (params.ownerUsername) {
    queryParams.set('owner_username', params.ownerUsername);
  }
  if (params.agentKind) {
    queryParams.set('agent_kind', params.agentKind);
  }
  if (params.lastSeenAfter) {
    queryParams.set('last_seen_after', params.lastSeenAfter);
  }
  if (params.status) {
    queryParams.set('status', params.status);
  }
  if (typeof params.limit === 'number') {
    queryParams.set('limit', String(params.limit));
  }
  if (typeof params.offset === 'number') {
    queryParams.set('offset', String(params.offset));
  }

  const queryString = queryParams.toString();
  return queryString ? `?${queryString}` : '';
}

export async function getAccountGatewayUsageSummary(
  params: GatewayUsageSummaryParams = {}
): Promise<AccountGatewayUsageSummaryResponse> {
  const response = await fetchWithAuth(
    `/api/v1/account/gateway-usage/summary${buildGatewayUsageQuery(params)}`
  );
  if (!response.ok) {
    throw new Error('Failed to fetch account gateway usage summary');
  }
  return response.json();
}

export async function getAccountRateLimitReport(
  params: GatewayUsageSummaryParams = {}
): Promise<AccountRateLimitReportResponse> {
  const response = await fetchWithAuth(
    `/api/v1/account/gateway-usage/rate-limits${buildGatewayUsageQuery(params)}`
  );
  if (!response.ok) {
    throw new Error('Failed to fetch rate limit report');
  }
  return response.json();
}

/**
 * A console attention item the account has silenced.
 *
 * `fingerprint` is why the item was showing when it was dismissed; the item
 * comes back by itself when the reason changes.
 */
export interface AttentionDismissal {
  id: string;
  item_id: string;
  fingerprint: string;
  reason: 'expected' | 'snoozed' | 'fixed';
  snooze_until: string | null;
  dismissed_by_user_id: string | null;
  dismissed_by_username: string | null;
  created_at: string;
}

/** Distinguishes "nothing is dismissed" from "this backend has no such API". */
export const DISMISSALS_UNSUPPORTED = 'unsupported' as const;

/**
 * Active dismissals, or `DISMISSALS_UNSUPPORTED` when the backend predates
 * the endpoint. A console pointed at an older instance must show its inbox
 * without dismiss controls rather than an error.
 */
export async function getAttentionDismissals(): Promise<
  AttentionDismissal[] | typeof DISMISSALS_UNSUPPORTED
> {
  const response = await fetchWithAuth('/api/v1/attention/dismissals');
  if (response.status === 404 || response.status === 405) {
    return DISMISSALS_UNSUPPORTED;
  }
  if (!response.ok) {
    throw new Error('Failed to fetch attention dismissals');
  }
  const body = await response.json();
  return (body?.items || []) as AttentionDismissal[];
}

export async function dismissAttentionItem(
  itemId: string,
  body: {
    fingerprint: string;
    reason: 'expected' | 'snoozed' | 'fixed';
    snooze_days?: number;
  }
): Promise<AttentionDismissal> {
  const response = await fetchWithAuth(
    `/api/v1/attention/dismissals/${encodeURIComponent(itemId)}`,
    {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }
  );
  if (!response.ok) {
    throw new Error('Failed to dismiss item');
  }
  return response.json();
}

export async function restoreAttentionItem(itemId: string): Promise<void> {
  const response = await fetchWithAuth(
    `/api/v1/attention/dismissals/${encodeURIComponent(itemId)}`,
    { method: 'DELETE' }
  );
  if (!response.ok) {
    throw new Error('Failed to restore item');
  }
}

export async function getCostAnalyticsSummary(
  params: GatewayUsageSummaryParams = {}
): Promise<CostAnalyticsSummaryResponse> {
  const response = await fetchWithAuth(
    `/api/v1/cost/summary${buildGatewayUsageQuery(params)}`
  );
  if (!response.ok) {
    throw new Error('Failed to fetch cost analytics summary');
  }
  return response.json();
}

export async function getToolUsageStats(
  params: GatewayUsageSummaryParams = {}
): Promise<ToolUsageStatsResponse> {
  const response = await fetchWithAuth(
    `/api/v1/tools/stats${buildGatewayUsageQuery(params)}`
  );
  if (!response.ok) {
    throw new Error('Failed to fetch tool usage stats');
  }
  return response.json();
}

export async function getModelPriceOverrides(options?: {
  modelAlias?: string;
  activeOnly?: boolean;
}): Promise<ModelPriceOverride[]> {
  const params = new URLSearchParams();
  if (options?.modelAlias) params.set('model_alias', options.modelAlias);
  if (options?.activeOnly) params.set('active_only', 'true');
  const query = params.toString();
  const response = await fetchWithAuth(
    `/api/v1/billing/cost/pricing-overrides${query ? `?${query}` : ''}`
  );
  if (!response.ok) {
    throw new Error('Failed to fetch model price overrides');
  }
  return response.json();
}

export async function createModelPriceOverride(
  data: ModelPriceOverrideCreate
): Promise<ModelPriceOverride> {
  const response = await fetchWithAuth(
    '/api/v1/billing/cost/pricing-overrides',
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    }
  );
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new Error(
      extractErrorMessage(errorData, 'Failed to create model price override')
    );
  }
  return response.json();
}

export async function updateModelPriceOverride(
  id: string,
  data: ModelPriceOverrideUpdate
): Promise<ModelPriceOverride> {
  const response = await fetchWithAuth(
    `/api/v1/billing/cost/pricing-overrides/${id}`,
    {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    }
  );
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new Error(
      extractErrorMessage(errorData, 'Failed to update model price override')
    );
  }
  return response.json();
}

export async function deleteModelPriceOverride(id: string): Promise<void> {
  const response = await fetchWithAuth(
    `/api/v1/billing/cost/pricing-overrides/${id}`,
    {
      method: 'DELETE',
    }
  );
  if (!response.ok) {
    throw new Error('Failed to delete model price override');
  }
}

export async function repriceCost(data: {
  start_date: string;
  end_date: string;
  only_unpriced?: boolean;
  dry_run?: boolean;
}): Promise<RepriceResponse> {
  const response = await fetchWithAuth('/api/v1/billing/cost/reprice', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new Error(extractErrorMessage(errorData, 'Failed to reprice usage'));
  }
  return response.json();
}

export async function getProviderBillingConnections(): Promise<
  ProviderBillingConnection[]
> {
  const response = await fetchWithAuth(
    '/api/v1/billing/provider-billing/connections'
  );
  if (!response.ok) {
    throw new Error('Failed to fetch provider billing connections');
  }
  return response.json();
}

export async function createProviderBillingConnection(data: {
  provider: string;
  admin_api_key: string;
}): Promise<ProviderBillingConnection> {
  const response = await fetchWithAuth(
    '/api/v1/billing/provider-billing/connections',
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    }
  );
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new Error(
      extractErrorMessage(
        errorData,
        'Failed to create provider billing connection'
      )
    );
  }
  return response.json();
}

export async function deleteProviderBillingConnection(
  id: string
): Promise<void> {
  const response = await fetchWithAuth(
    `/api/v1/billing/provider-billing/connections/${id}`,
    { method: 'DELETE' }
  );
  if (!response.ok) {
    throw new Error('Failed to delete provider billing connection');
  }
}

export async function syncProviderBillingConnection(
  id: string
): Promise<unknown> {
  const response = await fetchWithAuth(
    `/api/v1/billing/provider-billing/connections/${id}/sync`,
    { method: 'POST' }
  );
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new Error(
      extractErrorMessage(errorData, 'Failed to sync provider billing')
    );
  }
  return response.json();
}

export async function getCostReconciliation(params: {
  startDate?: string;
  endDate?: string;
  provider?: string;
}): Promise<CostReconciliationResponse> {
  const query = new URLSearchParams();
  if (params.startDate) query.set('start_date', params.startDate);
  if (params.endDate) query.set('end_date', params.endDate);
  if (params.provider) query.set('provider', params.provider);
  const suffix = query.toString();
  const response = await fetchWithAuth(
    `/api/v1/billing/provider-billing/reconciliation${suffix ? `?${suffix}` : ''}`
  );
  if (!response.ok) {
    throw new Error('Failed to fetch cost reconciliation');
  }
  return response.json();
}

/**
 * A single evidence-grounded "tool cost flag": Preloop's finding that an
 * agent's tool definition is wasting money. Surfaced by the Phase B detection
 * engine. The `tool_source` ("payload" | "mcp") is a HEURISTIC label and is
 * NOT authoritative. `disable_eligible` is false when the tool name is too
 * ambiguous for safe one-click disable (deferred phase).
 */
export interface ToolCostFlag {
  id: string;
  tool_name: string;
  tool_source: string; // heuristic label: "payload" | "mcp" (not authoritative)
  flag_kind: string;
  evidence: { claim?: string; [key: string]: unknown };
  estimated_weekly_cost: number;
  status: 'open' | 'dismissed' | 'snoozed';
  disable_eligible: boolean;
  window_start: string;
  window_end: string;
}

/**
 * Fetch open tool cost flags. The backend response shape is being finalized in
 * parallel, so accept EITHER a bare array OR an object envelope `{ flags: [...] }`
 * and normalize to an array. Dismissed flags are excluded by the default GET.
 */
export async function getToolCostFlags(): Promise<ToolCostFlag[]> {
  const response = await fetchWithAuth('/api/v1/billing/cost/tool-flags');
  if (!response.ok) {
    throw new Error('Failed to fetch tool cost flags');
  }
  const data = await response.json();
  // Defensive: check for `.flags` envelope first, then fall back to a bare array.
  if (data && Array.isArray(data.flags)) {
    return data.flags as ToolCostFlag[];
  }
  return Array.isArray(data) ? (data as ToolCostFlag[]) : [];
}

/** Dismiss a single tool cost flag. */
export async function dismissToolCostFlag(id: string): Promise<void> {
  const response = await fetchWithAuth(
    `/api/v1/billing/cost/tool-flags/${id}/dismiss`,
    {
      method: 'POST',
    }
  );
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new Error(
      extractErrorMessage(errorData, 'Failed to dismiss tool cost flag')
    );
  }
}

/**
 * Optionally re-run tool cost flag detection. Returns the refreshed flags,
 * normalized the same way as {@link getToolCostFlags}. The endpoint is
 * optional; callers should degrade gracefully if it 404s.
 */
export async function refreshToolCostFlags(): Promise<ToolCostFlag[]> {
  const response = await fetchWithAuth(
    '/api/v1/billing/cost/tool-flags/refresh',
    {
      method: 'POST',
    }
  );
  if (!response.ok) {
    throw new Error('Failed to refresh tool cost flags');
  }
  const data = await response.json();
  if (data && Array.isArray(data.flags)) {
    return data.flags as ToolCostFlag[];
  }
  return Array.isArray(data) ? (data as ToolCostFlag[]) : [];
}

export async function getDashboardTelemetry(): Promise<DashboardTelemetryResponse> {
  const response = await fetchWithAuth('/api/v1/account/telemetry/dashboard');
  if (!response.ok) {
    throw new Error('Failed to fetch dashboard telemetry');
  }
  return response.json();
}

export async function getFlowGatewayUsageSummary(
  flowId: string,
  params: GatewayUsageSummaryParams = {}
): Promise<FlowGatewayUsageSummaryResponse> {
  const response = await fetchWithAuth(
    `/api/v1/flows/${flowId}/gateway-usage/summary${buildGatewayUsageQuery(params)}`
  );
  if (!response.ok) {
    throw new Error('Failed to fetch flow gateway usage summary');
  }
  return response.json();
}

export async function getAccountGatewayUsageSearch(
  params: GatewayUsageSearchParams = {}
): Promise<AccountGatewayUsageSearchResponse> {
  const response = await fetchWithAuth(
    `/api/v1/account/gateway-usage/search${buildGatewayUsageQuery(params)}`
  );
  if (!response.ok) {
    throw new Error('Failed to fetch account gateway usage search results');
  }
  return response.json();
}

export async function getAccountRuntimeSessions(
  params: RuntimeSessionListParams = {}
): Promise<AccountRuntimeSessionListResponse> {
  const response = await fetchWithAuth(
    `/api/v1/runtime-sessions${buildRuntimeSessionListQuery(params)}`
  );
  if (!response.ok) {
    throw new Error('Failed to fetch sessions');
  }
  return response.json();
}

export async function getAccountAgents(
  params: ManagedAgentListParams = {}
): Promise<AccountManagedAgentListResponse> {
  const response = await fetchWithAuth(
    `/api/v1/agents${buildManagedAgentListQuery(params)}`
  );
  if (!response.ok) {
    throw new Error('Failed to fetch managed agents');
  }
  return response.json();
}

export async function getAccountAgent(
  agentId: string,
  params: { start_date?: string; end_date?: string } = {}
): Promise<ManagedAgentDetailResponse> {
  const queryParams = new URLSearchParams();
  if (params.start_date) queryParams.set('start_date', params.start_date);
  if (params.end_date) queryParams.set('end_date', params.end_date);
  const queryStr = queryParams.toString();
  const response = await fetchWithAuth(
    `/api/v1/agents/${agentId}${queryStr ? `?${queryStr}` : ''}`
  );
  if (!response.ok) {
    throw new Error('Failed to fetch managed agent');
  }
  return response.json();
}

export async function updateAccountAgent(
  agentId: string,
  payload: ManagedAgentUpdateRequest
): Promise<ManagedAgentSummary> {
  const response = await fetchWithAuth(`/api/v1/agents/${agentId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    throw new Error('Failed to update managed agent');
  }
  return response.json();
}

export async function removeAccountAgent(
  agentId: string
): Promise<{ message: string }> {
  const response = await fetchWithAuth(`/api/v1/agents/${agentId}`, {
    method: 'DELETE',
  });
  if (!response.ok) {
    throw new Error('Failed to remove managed agent');
  }
  return response.json();
}

export interface CreateManagedAgentRequest {
  display_name: string;
  description?: string;
}

/**
 * Register a custom agent that the CLI cannot auto-discover (e.g. a hosted
 * LangGraph or custom SDK agent). Returns the managed agent summary.
 * POST /api/v1/agents
 */
export async function createManagedAgent(
  payload: CreateManagedAgentRequest
): Promise<ManagedAgentSummary> {
  const response = await fetchWithAuth('/api/v1/agents', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new Error(
      extractErrorMessage(errorData, 'Failed to register custom agent')
    );
  }
  return response.json();
}

export interface UpdateManagedAgentRequest {
  display_name?: string;
  tags?: Record<string, string>;
}

/**
 * Update a managed agent's display name and/or tags after registration.
 * PATCH /api/v1/agents/{agentId}
 *
 * The registration endpoint (POST /api/v1/agents) accepts only
 * {display_name, description}; tags are set here in a follow-up PATCH. Tags are
 * a flat string->string map (e.g. {env: "prod", db: "true"}). Returns the
 * updated managed agent summary.
 */
export async function updateManagedAgent(
  agentId: string,
  payload: UpdateManagedAgentRequest
): Promise<ManagedAgentSummary> {
  const response = await fetchWithAuth(`/api/v1/agents/${agentId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new Error(
      extractErrorMessage(errorData, 'Failed to update managed agent')
    );
  }
  return response.json();
}

export interface ManagedAgentCredentialCreateRequest {
  name: string;
  description?: string;
  scopes?: string[];
  expires_in_days?: number;
}

export interface ManagedAgentCredentialCreateResult {
  // The credential summary metadata (durable record).
  credential: {
    id: string;
    name: string;
    scopes: string[];
    key_prefix?: string | null;
    [key: string]: unknown;
  };
  // The presented token. Shown ONCE and cannot be recovered afterwards.
  token: string;
}

/**
 * Mint a durable gateway credential for a managed agent. The returned token is
 * presented a single time and cannot be retrieved again.
 * POST /api/v1/agents/{agentId}/credentials
 */
export async function createManagedAgentCredential(
  agentId: string,
  payload: ManagedAgentCredentialCreateRequest
): Promise<ManagedAgentCredentialCreateResult> {
  const response = await fetchWithAuth(
    `/api/v1/agents/${agentId}/credentials`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }
  );
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new Error(
      extractErrorMessage(errorData, 'Failed to mint agent credential')
    );
  }
  return response.json();
}

export interface ManagedAgentModelBindingSyncItem {
  ai_model_id: string;
  binding_type?: string;
  config_key: string;
  gateway_alias: string;
  is_primary?: boolean;
  status?: string;
}

export interface ManagedAgentModelBindingSyncRequest {
  bindings: ManagedAgentModelBindingSyncItem[];
}

/**
 * Replace the explicit AI model bindings for a managed agent. This is the set
 * of gateway-enabled models the agent is allowed to route through Preloop.
 * PUT /api/v1/agents/{agentId}/model-bindings
 * Returns the persisted binding summaries.
 */
export async function replaceManagedAgentModelBindings(
  agentId: string,
  payload: ManagedAgentModelBindingSyncRequest
): Promise<ManagedAgentModelBindingSummary[]> {
  const response = await fetchWithAuth(
    `/api/v1/agents/${agentId}/model-bindings`,
    {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }
  );
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new Error(
      extractErrorMessage(errorData, 'Failed to set agent model bindings')
    );
  }
  return response.json();
}

export async function sendAgentControlCommand(
  agentId: string,
  payload: AgentControlCommandRequest
): Promise<AgentControlCommandResponse> {
  const url = `/api/v1/agents/${agentId}/control/commands`;
  const response = await fetchWithAuth(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });

  if (response.ok) {
    return response.json().catch(() => ({}));
  }

  // Older backends only accept { message, metadata }. If the new shape is
  // rejected, retry once with session targeting folded into metadata.
  if (
    (response.status === 400 || response.status === 422) &&
    (payload.target_session_id || payload.session_mode)
  ) {
    const legacyResponse = await fetchWithAuth(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        message: payload.message,
        metadata: {
          ...(payload.metadata || {}),
          target_session_id: payload.target_session_id ?? null,
          session_mode: payload.session_mode ?? null,
          start_new_session: payload.start_new_session ?? false,
        },
      }),
    });

    if (legacyResponse.ok) {
      return legacyResponse.json().catch(() => ({}));
    }

    const legacyErrorData = await legacyResponse.json().catch(() => ({}));
    throw new Error(
      extractErrorMessage(
        legacyErrorData,
        'Failed to send Agent Control command'
      )
    );
  }

  const errorData = await response.json().catch(() => ({}));
  throw new Error(
    extractErrorMessage(errorData, 'Failed to send Agent Control command')
  );
}

export async function sendAgentControlVoiceTranscript(
  agentId: string,
  payload: AgentControlVoiceTranscriptRequest
): Promise<AgentControlCommandResponse> {
  const response = await fetchWithAuth(
    `/api/v1/agents/${agentId}/control/voice-transcripts`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }
  );

  if (response.ok) {
    return response.json().catch(() => ({}));
  }

  const errorData = await response.json().catch(() => ({}));
  throw new Error(
    extractErrorMessage(
      errorData,
      'Failed to send Agent Control voice transcript'
    )
  );
}

export async function sendAgentControlTakeover(
  agentId: string,
  payload: {
    target_session_id?: string | null;
    start_new_session?: boolean;
    spawn_worktree?: boolean;
  } = {}
): Promise<AgentControlCommandResponse> {
  const response = await fetchWithAuth(
    `/api/v1/agents/${agentId}/control/takeover`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }
  );
  if (response.ok) {
    return response.json().catch(() => ({}));
  }
  const errorData = await response.json().catch(() => ({}));
  throw new Error(
    extractErrorMessage(errorData, 'Failed to take over this session')
  );
}

export async function sendAgentControlRelease(
  agentId: string,
  payload: { target_session_id?: string | null } = {}
): Promise<AgentControlCommandResponse> {
  const response = await fetchWithAuth(
    `/api/v1/agents/${agentId}/control/release`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }
  );
  if (response.ok) {
    return response.json().catch(() => ({}));
  }
  const errorData = await response.json().catch(() => ({}));
  throw new Error(
    extractErrorMessage(errorData, 'Failed to release this session')
  );
}

export async function getAccountGovernanceDefaults(): Promise<AccountGovernanceDefaultsResponse> {
  const response = await fetchWithAuth('/api/v1/account/governance-defaults');
  if (!response.ok) {
    throw new Error('Failed to fetch account governance defaults');
  }
  return response.json();
}

export async function updateAccountGovernanceDefaults(
  defaults: AccountGovernanceDefaults
): Promise<AccountGovernanceDefaultsResponse> {
  const response = await fetchWithAuth('/api/v1/account/governance-defaults', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(defaults),
  });
  if (!response.ok) {
    throw new Error('Failed to update account governance defaults');
  }
  return response.json();
}

export async function getAgentGovernance(
  agentId: string
): Promise<SubjectGovernanceResponse> {
  const response = await fetchWithAuth(`/api/v1/agents/${agentId}/governance`);
  if (!response.ok) {
    throw new Error('Failed to fetch agent governance');
  }
  return response.json();
}

export async function updateAgentGovernance(
  agentId: string,
  config: SubjectGovernanceConfig
): Promise<SubjectGovernanceResponse> {
  const response = await fetchWithAuth(`/api/v1/agents/${agentId}/governance`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(config),
  });
  if (!response.ok) {
    throw new Error('Failed to update agent governance');
  }
  return response.json();
}

export async function getAccountRuntimeSessionDetail(
  runtimeSessionId: string,
  _params: RuntimeSessionDetailParams = {}
): Promise<AccountRuntimeSessionDetailResponse> {
  const response = await fetchWithAuth(
    `/api/v1/runtime-sessions/${runtimeSessionId}`
  );
  if (!response.ok) {
    throw new Error('Failed to fetch session detail');
  }
  return response.json();
}

export async function getAccountRuntimeSessionInteractions(
  runtimeSessionId: string,
  params: RuntimeSessionInteractionsParams = {}
): Promise<AccountGatewayUsageSearchResponse> {
  const queryParams = new URLSearchParams();

  if (params.interactionQuery) {
    queryParams.set('interaction_query', params.interactionQuery);
  }
  if (typeof params.interactionLimit === 'number') {
    queryParams.set('interaction_limit', String(params.interactionLimit));
  }
  if (typeof params.interactionOffset === 'number') {
    queryParams.set('interaction_offset', String(params.interactionOffset));
  }

  const queryString = queryParams.toString();
  const response = await fetchWithAuth(
    `/api/v1/runtime-sessions/${runtimeSessionId}/interactions${queryString ? `?${queryString}` : ''}`
  );
  if (!response.ok) {
    throw new Error('Failed to fetch session interactions');
  }
  return response.json();
}

export async function getAccountRuntimeSessionActivityTimeline(
  runtimeSessionId: string
): Promise<RuntimeSessionActivityListResponse> {
  const response = await fetchWithAuth(
    `/api/v1/runtime-sessions/${runtimeSessionId}/activity`
  );
  if (!response.ok) {
    throw new Error('Failed to fetch session activity timeline');
  }
  return response.json();
}

export async function summarizeRuntimeSession(
  runtimeSessionId: string
): Promise<RuntimeSessionSummaryInsight> {
  const response = await fetchWithAuth(
    `/api/v1/runtime-sessions/${runtimeSessionId}/summaries`,
    { method: 'POST' }
  );
  if (!response.ok) {
    throw new Error('Failed to summarize runtime session');
  }
  return response.json();
}

export async function optimizeRuntimeSession(
  runtimeSessionId: string,
  options: {
    regenerate?: boolean;
    modelId?: string | null;
    eventIds?: string[];
    sourceKinds?: string[];
    fromIndex?: number;
    toIndex?: number;
    cacheOnly?: boolean;
  } = {}
): Promise<RuntimeSessionOptimizationResponse> {
  const response = await fetchWithAuth(
    `/api/v1/billing/cost/runtime-sessions/${runtimeSessionId}/optimizations`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        regenerate: Boolean(options.regenerate),
        model_id: options.modelId || null,
        event_ids: options.eventIds || [],
        source_kinds: options.sourceKinds || [],
        from_index: options.fromIndex ?? null,
        to_index: options.toIndex ?? null,
        cache_only: Boolean(options.cacheOnly),
      }),
    }
  );
  if (!response.ok) {
    throw new Error('Failed to optimize runtime session');
  }
  return response.json();
}

/**
 * Submit the optimization analysis as an async background job.
 * POST /api/v1/billing/cost/runtime-sessions/{id}/optimizations/jobs
 *
 * The backend is idempotent: while a job for this session is still active,
 * re-submitting returns that job instead of queuing a second model pass.
 */
export async function submitRuntimeSessionOptimizationJob(
  runtimeSessionId: string,
  options: {
    regenerate?: boolean;
    modelId?: string | null;
    eventIds?: string[];
    sourceKinds?: string[];
    fromIndex?: number;
    toIndex?: number;
  } = {}
): Promise<RuntimeSessionOptimizationJobSubmitResponse> {
  const response = await fetchWithAuth(
    `/api/v1/billing/cost/runtime-sessions/${runtimeSessionId}/optimizations/jobs`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        regenerate: Boolean(options.regenerate),
        model_id: options.modelId || null,
        event_ids: options.eventIds || [],
        source_kinds: options.sourceKinds || [],
        from_index: options.fromIndex ?? null,
        to_index: options.toIndex ?? null,
      }),
    }
  );
  if (!response.ok) {
    throw new Error('Failed to submit optimization job');
  }
  return response.json();
}

/**
 * Poll one async optimization job's status, result, and error.
 * GET /api/v1/billing/cost/runtime-sessions/{id}/optimizations/jobs/{jobId}
 */
export async function getRuntimeSessionOptimizationJob(
  runtimeSessionId: string,
  jobId: string
): Promise<RuntimeSessionOptimizationJobStatusResponse> {
  const response = await fetchWithAuth(
    `/api/v1/billing/cost/runtime-sessions/${runtimeSessionId}/optimizations/jobs/${jobId}`
  );
  if (!response.ok) {
    throw new Error(`Failed to load optimization job (${response.status})`);
  }
  return response.json();
}

/**
 * Fetch the bundled example session's optimization suggestions.
 *
 * Used to show a new account what the Optimize tab produces before its own
 * agents have generated traffic. The response is flagged `is_example` and must
 * always be labelled as sample data — it is never the user's own session, and
 * its figures must not be folded into any account total.
 */
export async function getExampleSessionOptimization(): Promise<RuntimeSessionOptimizationResponse> {
  const response = await fetchWithAuth(
    '/api/v1/billing/cost/runtime-sessions/example/optimization'
  );
  if (!response.ok) {
    throw new Error('Failed to load example session optimization');
  }
  return response.json();
}

export async function applyRuntimeSessionOptimization(
  runtimeSessionId: string,
  payload: {
    suggestionId: string;
    suggestionTitle?: string | null;
    action: RuntimeSessionOptimizationActionSpec;
  }
): Promise<RuntimeSessionOptimizationAppliedAction> {
  const response = await fetchWithAuth(
    `/api/v1/billing/cost/runtime-sessions/${runtimeSessionId}/optimizations/apply`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        suggestion_id: payload.suggestionId,
        suggestion_title: payload.suggestionTitle || null,
        action: payload.action,
      }),
    }
  );
  if (!response.ok) {
    let detail = 'Failed to apply optimization action';
    try {
      const body = await response.json();
      if (body && typeof body.detail === 'string') detail = body.detail;
    } catch {
      // Keep the generic message when the body is not JSON.
    }
    throw new Error(detail);
  }
  return response.json();
}

export async function listRuntimeSessionOptimizationActions(
  runtimeSessionId: string
): Promise<RuntimeSessionOptimizationActionListResponse> {
  const response = await fetchWithAuth(
    `/api/v1/billing/cost/runtime-sessions/${runtimeSessionId}/optimizations/actions`,
    { method: 'GET' }
  );
  if (!response.ok) {
    throw new Error('Failed to load applied optimization actions');
  }
  return response.json();
}

/**
 * Verify a candidate optimization's savings by re-executing the session's
 * stored request with and without the candidate applied. This re-sends the
 * stored request upstream and spends budget, so `consented` must be true.
 * POST /api/v1/billing/cost/runtime-sessions/{id}/replay
 */
export async function replayRuntimeSession(
  runtimeSessionId: string,
  payload: {
    candidate: {
      removedToolNames?: string[];
      filteredOutputFields?: Record<string, string[]>;
    };
    suggestionId?: string | null;
    consented: boolean;
    nRuns?: number;
  }
): Promise<RuntimeSessionReplayResponse> {
  const response = await fetchWithAuth(
    `/api/v1/billing/cost/runtime-sessions/${runtimeSessionId}/replay`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        candidate: {
          removed_tool_names: payload.candidate.removedToolNames || [],
          filtered_output_fields: payload.candidate.filteredOutputFields || {},
        },
        suggestion_id: payload.suggestionId || null,
        consented: payload.consented,
        n_runs: payload.nRuns ?? 3,
      }),
    }
  );
  if (!response.ok) {
    let detail = 'Failed to verify savings';
    try {
      const body = await response.json();
      if (body && typeof body.detail === 'string') detail = body.detail;
    } catch {
      // Keep the generic message when the body is not JSON.
    }
    throw new Error(detail);
  }
  return response.json();
}

/**
 * List the account's tool output filters. Each filter drops a set of fields
 * from a given tool's output (optionally scoped to a single managed agent) so
 * that bloated tool responses stop entering the model's context window.
 * GET /api/v1/billing/cost/output-filters → { items: ToolOutputFilter[] }
 */
export async function listToolOutputFilters(): Promise<ToolOutputFilter[]> {
  const response = await fetchWithAuth('/api/v1/billing/cost/output-filters');
  if (!response.ok) {
    throw new Error('Failed to load tool output filters');
  }
  const data: ToolOutputFilterListResponse = await response.json();
  return Array.isArray(data?.items) ? data.items : [];
}

/**
 * Create a tool output filter that drops the given fields from a tool's output.
 * POST /api/v1/billing/cost/output-filters
 */
export async function createToolOutputFilter(
  payload: ToolOutputFilterCreateRequest
): Promise<ToolOutputFilter> {
  const response = await fetchWithAuth('/api/v1/billing/cost/output-filters', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      server_name: payload.server_name ?? null,
      tool_name: payload.tool_name,
      dropped_fields: payload.dropped_fields,
      managed_agent_id: payload.managed_agent_id ?? null,
    }),
  });
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new Error(
      extractErrorMessage(errorData, 'Failed to create tool output filter')
    );
  }
  return response.json();
}

/**
 * Delete a tool output filter by id.
 * DELETE /api/v1/billing/cost/output-filters/{id}
 */
export async function deleteToolOutputFilter(id: string): Promise<void> {
  const response = await fetchWithAuth(
    `/api/v1/billing/cost/output-filters/${id}`,
    { method: 'DELETE' }
  );
  if (!response.ok) {
    throw new Error('Failed to delete tool output filter');
  }
}

export async function updateAccountRuntimeSession(
  runtimeSessionId: string,
  payload: RuntimeSessionUpdateRequest
): Promise<RuntimeSessionSummary> {
  const response = await fetchWithAuth(
    `/api/v1/runtime-sessions/${runtimeSessionId}`,
    {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }
  );
  if (!response.ok) {
    throw new Error('Failed to update session');
  }
  return response.json();
}

export async function getRuntimeSessionGatewayEvents(
  sessionId: string,
  options:
    | number
    | {
        tail?: number;
        limit?: number;
        offset?: number;
        metadataOnly?: boolean;
      } = {}
): Promise<FlowGatewayEventsResponse> {
  const searchParams = new URLSearchParams();
  if (typeof options === 'number') {
    searchParams.set('tail', String(options));
  } else {
    if (options.tail !== undefined)
      searchParams.set('tail', String(options.tail));
    if (options.limit !== undefined)
      searchParams.set('limit', String(options.limit));
    if (options.offset !== undefined)
      searchParams.set('offset', String(options.offset));
    if (options.metadataOnly) searchParams.set('metadata_only', 'true');
  }
  const params = searchParams.toString() ? `?${searchParams.toString()}` : '';
  const response = await fetchWithAuth(
    `/api/v1/runtime-sessions/${sessionId}/gateway-events${params}`
  );
  if (!response.ok) {
    throw new Error('Failed to fetch runtime session gateway events');
  }
  return response.json();
}

export async function getRuntimeSessionRequests(
  sessionId: string,
  options: {
    limit?: number;
    offset?: number;
    failedOnly?: boolean;
    eventIds?: string[];
  } = {}
): Promise<RuntimeSessionRequestListResponse> {
  const searchParams = new URLSearchParams();
  if (options.limit !== undefined)
    searchParams.set('limit', String(options.limit));
  if (options.offset !== undefined)
    searchParams.set('offset', String(options.offset));
  if (options.failedOnly) searchParams.set('failed_only', 'true');
  if (options.eventIds) {
    for (const id of options.eventIds) searchParams.append('event_ids', id);
  }
  const params = searchParams.toString() ? `?${searchParams.toString()}` : '';
  const response = await fetchWithAuth(
    `/api/v1/runtime-sessions/${sessionId}/requests${params}`
  );
  if (!response.ok) {
    throw new Error('Failed to fetch runtime session requests');
  }
  return response.json();
}

export async function getRuntimeSessionGatewayEventDetail(
  sessionId: string,
  eventId: string
): Promise<FlowGatewayEvent> {
  const response = await fetchWithAuth(
    `/api/v1/runtime-sessions/${sessionId}/gateway-events/${eventId}`
  );
  if (!response.ok) {
    throw new Error('Failed to fetch runtime session gateway event detail');
  }
  return response.json();
}

export async function summarizeRuntimeSessionGatewayEvent(
  sessionId: string,
  eventId: string
): Promise<RuntimeSessionInteractionSummary> {
  const response = await fetchWithAuth(
    `/api/v1/runtime-sessions/${sessionId}/gateway-events/${eventId}/summary`,
    { method: 'POST' }
  );
  if (!response.ok) {
    throw new Error('Failed to summarize runtime session gateway event');
  }
  return response.json();
}

export async function getTrackers() {
  const response = await fetchWithAuth('/api/v1/trackers');
  if (!response.ok) {
    throw new Error('Failed to fetch trackers');
  }
  return response.json();
}

export async function addTracker(trackerData: any) {
  const response = await fetchWithAuth('/api/v1/trackers', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(trackerData),
  });
  if (!response.ok) {
    throw new Error('Failed to add tracker');
  }
  return response.json();
}

export async function updateTracker(trackerId: string, trackerData: any) {
  const response = await fetchWithAuth(`/api/v1/trackers/${trackerId}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(trackerData),
  });
  if (!response.ok) {
    throw new Error('Failed to update tracker');
  }
  return response.json();
}

export async function deleteTracker(trackerId: string): Promise<void> {
  const response = await fetchWithAuth(`/api/v1/trackers/${trackerId}`, {
    method: 'DELETE',
  });
  if (!response.ok) {
    throw new Error('Failed to delete tracker');
  }
}

export async function validateTrackerToken(
  type: string,
  token: string,
  url?: string,
  username?: string,
  id?: string
) {
  console.log('Validating tracker token', type, token, url, username);
  const payload: {
    tracker_id?: string;
    tracker_type: string;
    api_key: string;
    url?: string;
    connection_details?: { username?: string };
  } = {
    tracker_type: type,
    api_key: token,
  };
  if (id) {
    payload.tracker_id = id;
  }
  if (url) {
    payload.url = url;
  }
  if (type.toLowerCase() === 'jira' && username) {
    payload.connection_details = { username };
  }

  const response = await fetchWithAuth('/api/v1/trackers/test-and-list-orgs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });

  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new Error(errorData.message || 'Failed to validate token');
  }
  return response.json();
}

export async function listProjectsForOrg(
  trackerType: string,
  token: string,
  orgId: string,
  url?: string,
  username?: string,
  trackerId?: string
) {
  const payload: any = {
    tracker_id: trackerId,
    tracker_type: trackerType,
    api_key: token,
    organization_identifier: orgId,
  };
  if (url) {
    payload.url = url;
  }
  if (trackerType.toLowerCase() === 'jira' && username) {
    payload.connection_details = { username };
  }

  const response = await fetchWithAuth(
    '/api/v1/trackers/list-projects-for-org',
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }
  );

  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new Error(
      errorData.message || 'Failed to list projects for organization'
    );
  }
  return response.json();
}

export async function getDuplicateIssues(
  status: 'opened' | 'closed' | 'all' = 'opened'
) {
  const response = await fetchWithAuth(
    `/api/v1/issue-duplicates/?status=${status}`
  );
  if (!response.ok) {
    throw new Error('Failed to fetch duplicate issues');
  }
  return response.json();
}

export async function getIssueCount(): Promise<{ total_issues: number }> {
  const response = await fetchWithAuth('/api/v1/issues-count');
  if (!response.ok) {
    throw new Error('Failed to fetch issue count');
  }
  return response.json();
}

export async function searchIssues(
  params: FetchIssuesListParams
): Promise<any[]> {
  const queryParams = new URLSearchParams();

  // Use similarity search when there's a query, fulltext otherwise
  if (params.query && params.query.trim()) {
    queryParams.append('search_type', 'similarity');
    queryParams.append('embedding_type', 'issue');
    queryParams.append('query', params.query);
  } else {
    // Use fulltext search with empty query to list all issues
    queryParams.append('search_type', 'fulltext');
    queryParams.append('query', '');
    queryParams.append('sort', 'newest');
  }

  if (params.limit) {
    queryParams.append('limit', params.limit.toString());
  }

  if (params.skip) {
    queryParams.append('skip', params.skip.toString());
  }

  if (params.project_ids && params.project_ids.length > 0) {
    params.project_ids.forEach((id) => queryParams.append('project_id', id));
  }

  if (params.status) {
    queryParams.append('status', params.status);
  }

  const response = await fetchWithAuth(
    `/api/v1/search?${queryParams.toString()}`
  );
  if (!response.ok) {
    throw new Error('Failed to fetch issues list');
  }
  const data = await response.json();
  return data.results.map((r: any) => r.item);
}

export async function post(url: string, body: any) {
  const response = await window.fetch(url, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    credentials: 'include',
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    // Try to extract error detail from response body
    let errorMessage = `HTTP error! status: ${response.status}`;
    try {
      const errorData = await response.json();
      if (errorData.detail) {
        errorMessage = extractErrorMessage(errorData, errorMessage);
      }
    } catch (e) {
      // If JSON parsing fails, use the default error message
    }
    throw new Error(errorMessage);
  }
  return response.json();
}

export async function detectIssueDependencies(
  issueIds: string[]
): Promise<DependencyResponse> {
  const response = await fetchWithAuth('/api/v1/issue-dependencies/detect', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ issue_ids: issueIds }),
  });
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new Error(
      extractErrorMessage(errorData, 'Failed to detect issue dependencies')
    );
  }
  return response.json();
}

export async function extendIssueDependencyScan(
  issueIds: string[],
  extendBy: number
): Promise<DependencyResponse> {
  const response = await fetchWithAuth('/api/v1/issue-dependencies/extend', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ issue_ids: issueIds, extend_by: extendBy }),
  });
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new Error(
      extractErrorMessage(errorData, 'Failed to extend issue dependency scan')
    );
  }
  return response.json();
}

export async function commitIssueDependencies(
  dependencies: DependencyPair[]
): Promise<DependencyResponse> {
  const response = await fetchWithAuth('/api/v1/issue-dependencies/commit', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ dependencies }),
  });
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new Error(
      extractErrorMessage(errorData, 'Failed to commit issue dependencies')
    );
  }
  return response.json();
}

export interface UserProfile {
  username: string;
  email: string;
  full_name?: string | null;
  email_verified: boolean;
  is_superuser?: boolean;
  /** null/undefined = RBAC inactive (OSS); array = allow-list */
  permissions?: string[] | null;
  avatar_url?: string | null;
  avatar_source?: string | null;
}

export interface AvatarResponse {
  avatar_url: string | null;
  avatar_source: string | null;
}

export async function uploadAvatar(file: File): Promise<AvatarResponse> {
  const formData = new FormData();
  formData.append('file', file);
  const response = await fetchWithAuth('/api/v1/users/me/avatar', {
    method: 'PUT',
    body: formData,
  });
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    if (typeof detail.detail === 'string') {
      throw new Error(detail.detail);
    }
    if (response.status === 413) {
      throw new Error('Image too large to upload.');
    }
    throw new Error(`Failed to upload avatar (${response.status})`);
  }
  // Profile changed -- drop cached /me so the next reader gets fresh data.
  userProfileCache = null;
  return response.json();
}

export async function deleteAvatar(): Promise<AvatarResponse> {
  const response = await fetchWithAuth('/api/v1/users/me/avatar', {
    method: 'DELETE',
  });
  if (!response.ok) {
    throw new Error('Failed to delete avatar');
  }
  userProfileCache = null;
  return response.json();
}

// Account
export async function getUserProfile(): Promise<UserProfile> {
  const now = Date.now();
  if (userProfileCache && userProfileCache.expiresAt > now) {
    return userProfileCache.data;
  }
  if (userProfileInflight) {
    return userProfileInflight;
  }

  const epoch = userProfileEpoch;
  userProfileInflight = (async () => {
    try {
      const response = await fetchWithAuth('/api/v1/auth/users/me');
      if (!response.ok) {
        throw new Error('Failed to fetch user profile');
      }
      const data = (await response.json()) as UserProfile;
      if (epoch === userProfileEpoch) {
        userProfileCache = {
          data,
          expiresAt: Date.now() + USER_PROFILE_CACHE_TTL_MS,
        };
      }
      return data;
    } finally {
      if (epoch === userProfileEpoch) {
        userProfileInflight = null;
      }
    }
  })();

  return userProfileInflight;
}

export async function getAccountDetails() {
  const response = await fetchWithAuth('/api/v1/account/details');
  if (!response.ok) {
    throw new Error('Failed to fetch account details');
  }
  return response.json();
}

export async function updateUserProfile(details: { full_name: string }) {
  const response = await fetchWithAuth('/api/v1/auth/users/me', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(details),
  });
  if (!response.ok) {
    throw new Error('Failed to update user profile');
  }
  const data = await response.json();
  // Profile changed — drop cached /me so the next reader gets fresh data.
  userProfileCache = null;
  return data;
}

export async function changePassword(passwords: {
  current_password: string;
  new_password: string;
}) {
  const response = await fetchWithAuth('/api/v1/auth/users/me/password', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(passwords),
  });
  if (response.status === 204) {
    return;
  }
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new Error(
      extractErrorMessage(errorData, 'Failed to change password')
    );
  }
}

// API Keys
export async function getApiKeys(): Promise<ApiKey[]> {
  const response = await fetchWithAuth('/api/v1/auth/api-keys');
  if (!response.ok) {
    throw new Error('Failed to fetch API keys');
  }
  return response.json();
}

export async function createApiKey(
  name: string,
  expires_at: string | null
): Promise<ApiKey> {
  const body = { name, expires_at };
  const response = await fetchWithAuth('/api/v1/auth/api-keys', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new Error(extractErrorMessage(errorData, 'Failed to create API key'));
  }
  return response.json();
}

export async function deleteApiKey(keyId: string) {
  const response = await fetchWithAuth(`/api/v1/auth/api-keys/${keyId}`, {
    method: 'DELETE',
  });
  if (!response.ok) {
    throw new Error('Failed to delete API key');
  }
}

export async function getApiKey(keyId: string): Promise<ApiKey> {
  const response = await fetchWithAuth(`/api/v1/auth/api-keys/${keyId}`);
  if (!response.ok) {
    throw new Error('Failed to fetch API key');
  }
  return response.json();
}

export async function getApiKeyActivity(keyId: string): Promise<any[]> {
  const response = await fetchWithAuth(
    `/api/v1/auth/api-keys/${keyId}/activity`
  );
  if (!response.ok) {
    throw new Error('Failed to fetch API key activity');
  }
  return response.json();
}

export async function getApiKeyGovernance(
  keyId: string
): Promise<SubjectGovernanceResponse> {
  const response = await fetchWithAuth(
    `/api/v1/auth/api-keys/${keyId}/governance`
  );
  if (!response.ok) {
    throw new Error('Failed to fetch API key governance');
  }
  return response.json();
}

export async function updateApiKeyGovernance(
  keyId: string,
  config: SubjectGovernanceConfig
): Promise<SubjectGovernanceResponse> {
  const response = await fetchWithAuth(
    `/api/v1/auth/api-keys/${keyId}/governance`,
    {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(config),
    }
  );
  if (!response.ok) {
    throw new Error('Failed to update API key governance');
  }
  return response.json();
}

export async function getApiKeyGatewayUsageSummary(
  keyId: string,
  params: GatewayUsageSummaryParams = {}
): Promise<ApiKeyGatewayUsageSummaryResponse> {
  const query = new URLSearchParams();
  if (params.startDate) query.append('start_date', params.startDate);
  if (params.endDate) query.append('end_date', params.endDate);

  const queryString = query.toString();
  const url = `/api/v1/auth/api-keys/${keyId}/gateway-usage/summary${queryString ? `?${queryString}` : ''}`;

  const response = await fetchWithAuth(url);
  if (!response.ok) {
    throw new Error('Failed to fetch API key gateway usage summary');
  }
  return response.json();
}

// AI Models
export async function getAIModels(): Promise<AIModel[]> {
  const response = await fetchWithAuth('/api/v1/ai-models');
  if (!response.ok) {
    throw new Error('Failed to fetch AI models');
  }
  return response.json();
}

export async function getAIModel(modelId: string): Promise<AIModel> {
  const response = await fetchWithAuth(`/api/v1/ai-models/${modelId}`);
  if (!response.ok) {
    throw new Error('Failed to fetch AI model');
  }
  return response.json();
}

export async function createAIModel(model: any) {
  const response = await fetchWithAuth('/api/v1/ai-models', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(model),
  });
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new Error(
      extractErrorMessage(errorData, 'Failed to create AI model')
    );
  }
  return response.json();
}

export async function updateAIModel(modelId: string, model: any) {
  const response = await fetchWithAuth(`/api/v1/ai-models/${modelId}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(model),
  });
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new Error(
      extractErrorMessage(errorData, 'Failed to update AI model')
    );
  }
  return response.json();
}

export async function transcribeAudio(
  audio: Blob,
  options: { aiModelId?: string | null; filename?: string } = {}
): Promise<SpeechToTextResponse> {
  const formData = new FormData();
  formData.append('audio', audio, options.filename || 'audio.webm');
  if (options.aiModelId) {
    formData.append('ai_model_id', options.aiModelId);
  }
  const response = await fetchWithAuth('/api/v1/audio/transcriptions', {
    method: 'POST',
    body: formData,
  });
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new Error(
      extractErrorMessage(errorData, 'Failed to transcribe audio')
    );
  }
  return response.json();
}

export async function synthesizeSpeech(
  request: TextToSpeechRequest
): Promise<Blob> {
  const response = await fetchWithAuth('/api/v1/audio/speech', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(request),
  });
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new Error(
      extractErrorMessage(errorData, 'Failed to synthesize speech')
    );
  }
  return response.blob();
}

export async function deleteAIModel(modelId: string) {
  const response = await fetchWithAuth(`/api/v1/ai-models/${modelId}`, {
    method: 'DELETE',
  });
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new Error(
      extractErrorMessage(errorData, 'Failed to delete AI model')
    );
  }
}

/**
 * How a provider model list was obtained. "live" means the provider's own
 * catalog answered; "fallback" means a static known-models list stood in and
 * `error` carries a short safe reason code (never raw provider text).
 */
/**
 * Short, safe reasons the server reports for a fallback model list.
 *
 * This vocabulary mirrors the server's fixed set (see
 * `preloop.services.ai_model_provider`). Raw exception text is deliberately
 * never sent, because it can embed endpoint URLs or key material. An
 * authentication failure that the provider rejected as a bad key still
 * raises a 401 instead of returning a fallback list; `auth` is reserved
 * for provenance-only auth failures.
 */
export type AvailableModelsFallbackReason =
  | 'timeout'
  | 'network'
  | 'empty_response'
  | 'unsupported'
  | 'missing_endpoint'
  | 'sdk_missing'
  | 'missing_key'
  | 'auth'
  | 'subscription_oauth'
  | 'unknown';

export interface AvailableModelsResult {
  models: string[];
  source: 'live' | 'fallback';
  error?: AvailableModelsFallbackReason;
}

/**
 * AWS credential fields for the bedrock provider's model listing.
 * Carried in the POST body for the same reason as `apiKey`.
 */
export interface AwsDiscoveryAuth {
  accessKeyId?: string;
  secretAccessKey?: string;
  sessionToken?: string;
  region?: string;
}

/**
 * List the models a provider offers, for the model picker.
 *
 * The API key goes in the POST body, never the query string: as a query
 * parameter it was written to server access logs in plaintext.
 *
 * `apiEndpoint` is required for the openai-compatible and custom providers,
 * which have no fixed catalog and are listed from the endpoint's own
 * OpenAI-compatible GET /models.
 *
 * `aiModelId` is the existing model row when editing. The server decrypts
 * the stored key and lists live; the stored key is never returned. A typed
 * `apiKey` still wins.
 *
 * `awsAuth` carries explicit AWS credentials for the bedrock provider.
 *
 * Tolerates the old bare string[] response (pre-provenance servers) by
 * mapping it to { models, source: 'live' }.
 */
export async function getAvailableModelsForProvider(
  provider: string,
  apiKey?: string,
  modelKind: 'llm' | 'stt' | 'tts' = 'llm',
  apiEndpoint?: string,
  awsAuth?: AwsDiscoveryAuth,
  aiModelId?: string
): Promise<AvailableModelsResult> {
  const url = `/api/v1/ai-models/providers/${provider}/available-models`;
  const response = await fetchWithAuth(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      model_kind: modelKind,
      ...(apiKey ? { api_key: apiKey } : {}),
      ...(apiEndpoint ? { api_endpoint: apiEndpoint } : {}),
      ...(aiModelId ? { ai_model_id: aiModelId } : {}),
      ...(awsAuth?.accessKeyId
        ? { aws_access_key_id: awsAuth.accessKeyId }
        : {}),
      ...(awsAuth?.secretAccessKey
        ? { aws_secret_access_key: awsAuth.secretAccessKey }
        : {}),
      ...(awsAuth?.sessionToken
        ? { aws_session_token: awsAuth.sessionToken }
        : {}),
      ...(awsAuth?.region ? { aws_region_name: awsAuth.region } : {}),
    }),
  });
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new Error(
      extractErrorMessage(errorData, 'Failed to fetch available models')
    );
  }
  const data = await response.json();
  if (Array.isArray(data)) {
    return { models: data as string[], source: 'live' };
  }
  return {
    models: Array.isArray(data?.models) ? (data.models as string[]) : [],
    source: data?.source === 'fallback' ? 'fallback' : 'live',
    ...(data?.error ? { error: data.error } : {}),
  };
}

export async function getAIModelGatewayUsageSummary(
  modelId: string,
  params: GatewayUsageSummaryParams = {}
): Promise<AIModelGatewayUsageSummaryResponse> {
  const response = await fetchWithAuth(
    `/api/v1/ai-models/${modelId}/summary${buildGatewayUsageQuery(params)}`
  );
  if (!response.ok) {
    throw new Error('Failed to fetch AI model usage summary');
  }
  return response.json();
}

export async function getAIModelRuntimeSessions(
  modelId: string,
  params: RuntimeSessionListParams = {}
): Promise<AIModelRuntimeSessionListResponse> {
  const response = await fetchWithAuth(
    `/api/v1/ai-models/${modelId}/runtime-sessions${buildRuntimeSessionListQuery(params)}`
  );
  if (!response.ok) {
    throw new Error('Failed to fetch AI model runtime sessions');
  }
  return response.json();
}

export async function getAIModelGatewayUsageSearch(
  modelId: string,
  params: GatewayUsageSearchParams = {}
): Promise<AIModelGatewayUsageSearchResponse> {
  const response = await fetchWithAuth(
    `/api/v1/ai-models/${modelId}/interactions${buildGatewayUsageQuery(params)}`
  );
  if (!response.ok) {
    throw new Error('Failed to fetch AI model interactions');
  }
  return response.json();
}

// Flows
export async function getFlows(): Promise<any[]> {
  const response = await fetchWithAuth('/api/v1/flows');
  if (!response.ok) {
    throw new Error('Failed to fetch flows');
  }
  return response.json();
}

export async function getFlow(flowId: string): Promise<any> {
  const response = await fetchWithAuth(`/api/v1/flows/${flowId}`);
  if (!response.ok) {
    throw new Error('Failed to fetch flow');
  }
  return response.json();
}

export async function createFlow(flow: any): Promise<any> {
  const response = await fetchWithAuth('/api/v1/flows', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(flow),
  });
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new Error(errorData.detail || 'Failed to create flow');
  }
  return response.json();
}

export async function updateFlow(flowId: string, flow: any): Promise<any> {
  const response = await fetchWithAuth(`/api/v1/flows/${flowId}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(flow),
  });
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new Error(errorData.detail || 'Failed to update flow');
  }
  return response.json();
}

export async function deleteFlow(flowId: string) {
  const response = await fetchWithAuth(`/api/v1/flows/${flowId}`, {
    method: 'DELETE',
  });
  if (!response.ok) {
    // Surface the server's reason (e.g. 409: stop active executions first).
    const errorData = await response.json().catch(() => ({}));
    throw new Error(errorData.detail || 'Failed to delete flow');
  }
}

export interface SchedulePreview {
  type: string;
  description: string;
  timezone: string;
  next_run_times: string[];
}

/**
 * Preview a flow schedule trigger configuration.
 *
 * Returns a human-readable description and the next few run times.
 * Invalid configurations (bad cron, below the minimum interval, unknown
 * timezone) are rejected by the backend with a 422; the validation
 * message is surfaced as the thrown Error's message so callers can show
 * it inline.
 */
export async function previewFlowSchedule(
  scheduleConfig: unknown
): Promise<SchedulePreview> {
  const response = await fetchWithAuth('/api/v1/flows/schedule/preview', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ schedule_config: scheduleConfig }),
  });
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new Error(extractValidationMessage(errorData));
  }
  return response.json();
}

/**
 * Extract a readable message from a FastAPI error body.
 *
 * 422 validation errors carry `detail` as a list of {loc, msg, ...};
 * other errors carry `detail` as a string.
 */
function extractValidationMessage(errorData: any): string {
  const detail = errorData?.detail;
  if (typeof detail === 'string') {
    return detail;
  }
  if (Array.isArray(detail) && detail.length > 0) {
    return detail
      .map((item: any) =>
        String(item?.msg || 'Invalid value').replace(/^Value error,\s*/, '')
      )
      .join('; ');
  }
  return 'Invalid schedule configuration';
}

export async function getFlowPresets(): Promise<any[]> {
  const response = await fetchWithAuth('/api/v1/flows/presets');
  if (!response.ok) {
    throw new Error('Failed to fetch flow presets');
  }
  return response.json();
}

export async function getFlowExecutions(options?: {
  limit?: number;
  skip?: number;
  flowId?: string;
  status?: string | string[];
}): Promise<any[]> {
  const params = new URLSearchParams();
  if (options?.limit !== undefined) {
    params.set('limit', options.limit.toString());
  }
  if (options?.skip !== undefined) {
    params.set('skip', options.skip.toString());
  }
  if (options?.flowId) {
    params.set('flow_id', options.flowId);
  }
  if (options?.status !== undefined) {
    const statuses = Array.isArray(options.status)
      ? options.status
      : [options.status];
    for (const status of statuses) {
      params.append('status', status);
    }
  }
  const queryString = params.toString();
  const url = `/api/v1/flows/executions${queryString ? `?${queryString}` : ''}`;
  const response = await fetchWithAuth(url);
  if (!response.ok) {
    throw new Error('Failed to fetch flow executions');
  }
  return response.json();
}

export async function getFlowExecution(executionId: string): Promise<any> {
  const response = await fetchWithAuth(
    `/api/v1/flows/executions/${executionId}`
  );
  if (!response.ok) {
    throw new Error('Failed to fetch flow execution');
  }
  return response.json();
}

export async function getFlowExecutionMetrics(executionId: string): Promise<{
  tool_calls: number;
  api_requests: number;
  token_usage: {
    total_tokens: number;
    input_tokens: number;
    output_tokens: number;
  };
  estimated_cost: number;
  has_pricing: boolean;
}> {
  const response = await fetchWithAuth(
    `/api/v1/flows/executions/${executionId}/metrics`
  );
  if (!response.ok) {
    throw new Error('Failed to fetch execution metrics');
  }
  return response.json();
}

export async function getFlowExecutionLogs(
  executionId: string,
  options?: { tail?: number; skip?: number; limit?: number }
): Promise<{
  logs: any[];
  source: 'container' | 'database';
  has_more?: boolean;
}> {
  const params = new URLSearchParams();
  if (options?.tail !== undefined)
    params.append('tail', options.tail.toString());
  if (options?.skip !== undefined)
    params.append('skip', options.skip.toString());
  if (options?.limit !== undefined)
    params.append('limit', options.limit.toString());

  const queryString = params.toString();
  const url = `/api/v1/flows/executions/${executionId}/logs${queryString ? `?${queryString}` : ''}`;

  const response = await fetchWithAuth(url);
  if (!response.ok) {
    throw new Error('Failed to fetch execution logs');
  }
  return response.json();
}

export async function getFlowExecutionGatewayEvents(
  executionId: string,
  tail?: number,
  metadataOnly: boolean = false
): Promise<FlowGatewayEventsResponse> {
  const params = new URLSearchParams();
  if (tail !== undefined) params.append('tail', tail.toString());
  if (metadataOnly) params.append('metadata_only', 'true');
  const paramsStr = params.toString() ? `?${params.toString()}` : '';

  const response = await fetchWithAuth(
    `/api/v1/flows/executions/${executionId}/gateway-events${paramsStr}`
  );
  if (!response.ok) {
    throw new Error('Failed to fetch execution gateway events');
  }
  return response.json();
}

export async function getFlowExecutionGatewayEvent(
  executionId: string,
  eventId: string
): Promise<any> {
  const response = await fetchWithAuth(
    `/api/v1/flows/executions/${executionId}/gateway-events/${eventId}`
  );
  if (!response.ok) {
    throw new Error('Failed to fetch execution gateway event');
  }
  return response.json();
}

export async function triggerFlowExecution(
  flowId: string,
  triggerEventData?: Record<string, any>
): Promise<any> {
  const response = await fetchWithAuth(`/api/v1/flows/${flowId}/trigger`, {
    method: 'POST',
    headers: triggerEventData
      ? { 'Content-Type': 'application/json' }
      : undefined,
    body: triggerEventData ? JSON.stringify(triggerEventData) : undefined,
  });
  if (!response.ok) {
    throw new Error('Failed to trigger flow execution');
  }
  return response.json();
}

export async function getRunners(): Promise<RunnerRecord[]> {
  const response = await fetchWithAuth('/api/v1/runners');
  if (!response.ok) {
    throw new Error('Failed to fetch runners');
  }
  return response.json();
}

export interface RunnerRecord {
  id: string;
  name: string;
  hostname?: string | null;
  os?: string | null;
  arch?: string | null;
  labels?: string[];
  status: string;
  last_heartbeat?: string | null;
  current_execution_id?: string | null;
  registered_by_email?: string | null;
  registered_by_user_id?: string | null;
}

export async function sendCommandToExecution(
  executionId: string,
  command: string,
  payload?: any
): Promise<void> {
  const response = await fetchWithAuth(
    `/api/v1/flows/executions/${executionId}/command`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ command, payload }),
    }
  );
  if (!response.ok) {
    throw new Error('Failed to send command to execution');
  }
}

export async function retryFlowExecution(executionId: string): Promise<any> {
  const response = await fetchWithAuth(
    `/api/v1/flows/executions/${executionId}/retry`,
    {
      method: 'POST',
    }
  );
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Failed to retry flow execution');
  }
  return response.json();
}

export async function cloneFlowPreset(presetId: string): Promise<any> {
  const response = await fetchWithAuth(
    `/api/v1/flows/presets/${presetId}/clone`,
    {
      method: 'POST',
    }
  );
  if (!response.ok) {
    throw new Error('Failed to clone flow preset');
  }
  return response.json();
}

export async function listProjects(options?: {
  organizationId?: string;
  limit?: number;
}): Promise<Project[]> {
  const params = new URLSearchParams();
  if (options?.organizationId) {
    params.set('organization_id', options.organizationId);
  }
  if (options?.limit) {
    params.set('limit', String(options.limit));
  } else {
    params.set('limit', '1000');
  }
  const query = params.toString();
  const response = await fetchWithAuth(
    query ? `/api/v1/projects?${query}` : '/api/v1/projects'
  );
  if (!response.ok) {
    throw new Error('Failed to fetch projects');
  }
  const projects: Project[] = await response.json();
  return projects.map((project) => ({
    ...project,
    key: project.key || project.identifier,
  }));
}

export async function syncTracker(
  trackerId: string
): Promise<{ status: string }> {
  const response = await fetchWithAuth(`/api/v1/trackers/${trackerId}/sync`, {
    method: 'POST',
  });
  if (!response.ok) {
    let detail = 'Failed to queue tracker sync';
    try {
      const body = await response.json();
      detail =
        (typeof body.detail === 'string' && body.detail) ||
        body.message ||
        detail;
    } catch {
      detail = `${detail} (${response.status})`;
    }
    throw new Error(detail);
  }
  return response.json();
}

export async function getEmbeddingsForProjects(
  projectIds: string[]
): Promise<{ data: any[] } | null> {
  const params = new URLSearchParams();
  if (projectIds.length > 0) {
    params.append('project_ids', projectIds.join(','));
  }

  const queryString = params.toString();
  const url = queryString
    ? `/api/v1/embeddings?${queryString}`
    : '/api/v1/embeddings';

  try {
    const response = await fetchWithAuth(url);
    if (!response.ok) {
      console.error('Failed to fetch embeddings:', response.statusText);
      throw new Error('Failed to fetch embeddings for projects');
    }
    return await response.json();
  } catch (error) {
    console.error('Error in getEmbeddingsForProjects:', error);
    return null;
  }
}

export async function listOrganizations(): Promise<Organization[]> {
  const response = await fetchWithAuth('/api/v1/organizations');
  if (!response.ok) {
    throw new Error('Failed to fetch organizations');
  }
  const data: any = await response.json();
  return (data.items || []).map((org: Organization) => ({
    ...org,
    key: org.key || org.identifier,
    identifier: org.identifier || org.key,
  }));
}

export async function listIssueDuplicates(
  options: {
    limit?: number;
    skip?: number;
    project_ids?: string[];
    similarity_threshold?: number;
    status?: 'opened' | 'closed' | 'all';
    resolution?: 'resolved' | 'unresolved' | 'all';
  } = {}
): Promise<DuplicatesResponse> {
  const {
    limit = 10,
    skip = 0,
    project_ids = [],
    status = 'opened',
    similarity_threshold = DEFAULT_SIMILARITY_THRESHOLD,
    resolution = 'all',
  } = options;

  const params = new URLSearchParams({
    limit: limit.toString(),
    skip: skip.toString(),
  });
  project_ids.forEach((id) => params.append('project_ids', id));
  params.append('status', status);
  params.append('similarity_threshold', similarity_threshold.toString());
  if (resolution && resolution !== 'all') {
    params.append('resolution', resolution);
  }

  const response = await fetchWithAuth(
    `/api/v1/issue-duplicates?${params.toString()}`
  );
  if (!response.ok) {
    throw new Error('Failed to fetch duplicate issues');
  }
  return response.json();
}

export async function checkAIVerdict(issue1_id: string, issue2_id: string) {
  const response = await fetchWithAuth(
    `/api/v1/issue-duplicates/check?issue1_id=${issue1_id}&issue2_id=${issue2_id}`
  );
  if (!response.ok) {
    throw new Error('Failed to fetch AI verdict');
  }
  return response.json();
}

export async function getProjectDuplicateStats(options: {
  project_ids?: string[];
  status?: 'opened' | 'closed' | 'all';
  similarity_threshold?: number;
}): Promise<any> {
  const {
    project_ids = [],
    status = 'opened',
    similarity_threshold = DEFAULT_SIMILARITY_THRESHOLD,
  } = options;
  const params = new URLSearchParams();
  project_ids.forEach((id) => params.append('project_ids', id));
  params.append('status', status);
  params.append('similarity_threshold', similarity_threshold.toString());
  const url = `/api/v1/project-duplicate-stats?${params.toString()}`;
  console.log(url);
  const response = await fetchWithAuth(url);
  if (!response.ok) {
    throw new Error('Failed to fetch project duplicate stats');
  }
  return response.json();
}

export async function dismissDuplicatePair(
  issue1Id: string,
  issue2Id: string
): Promise<{ success: boolean }> {
  console.log(`Dismissing duplicate pair: ${issue1Id} and ${issue2Id}`);

  // Simulate network delay
  await new Promise((resolve) => setTimeout(resolve, 500));

  // In a real implementation, you would make a call to your backend here
  // to record the dismissal.

  return Promise.resolve({ success: true });
}

export async function getResolutionSuggestion(
  issue1_id: string,
  issue2_id: string,
  resolution: 'merged' | 'deconflicted'
): Promise<any> {
  const response = await fetchWithAuth('/api/v1/ai-suggestion', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ issue1_id, issue2_id, resolution }),
  });

  if (!response.ok) {
    throw new Error('Failed to get resolution suggestion');
  }

  return response.json();
}

export async function executeIssueDuplicateResolution(
  resolutionData: any
): Promise<any> {
  const response = await fetchWithAuth(
    '/api/v1/issue-duplicates/execute-resolution',
    {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(resolutionData),
    }
  );
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new Error(
      extractErrorMessage(
        errorData,
        'Failed to execute issue duplicate resolution'
      )
    );
  }
  return response.json();
}

export async function getIssueCompliance(
  issueId: string,
  promptName: string
): Promise<IssueComplianceResult> {
  const response = await fetchWithAuth(
    `/api/v1/issue_compliance/${issueId}?prompt_name=${promptName}`,
    {
      method: 'GET',
    }
  );
  if (!response.ok) {
    throw new Error('Failed to fetch issue compliance');
  }
  return response.json();
}

export async function getComplianceImprovementSuggestion(
  issueId: string,
  promptName: string
): Promise<ComplianceSuggestion> {
  const response = await fetchWithAuth(
    `/api/v1/issue_compliance_suggestion/${issueId}?prompt_name=${promptName}`
  );
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new Error(
      extractErrorMessage(
        errorData,
        'Failed to get compliance improvement suggestion'
      )
    );
  }
  return response.json();
}

export async function proposeResolution(resolutionData: any) {
  return await fetchWithAuth('/api/v1/issue-duplicates/propose-resolution', {
    method: 'PATCH',
    body: JSON.stringify(resolutionData),
  });
}

export async function updateIssueContent(
  issueId: string,
  title: string,
  description: string,
  changes: string
): Promise<Issue> {
  const response = await fetchWithAuth(
    `/api/v1/issue_compliance_update/${issueId}`,
    {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title, description, changes }),
    }
  );

  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new Error(
      extractErrorMessage(errorData, 'Failed to update issue content')
    );
  }

  return response.json();
}

export async function getCompliancePrompts(): Promise<
  CompliancePromptMetadata[]
> {
  const response = await fetchWithAuth('/api/v1/issue_compliance_prompts');
  if (!response.ok) {
    throw new Error('Failed to fetch compliance prompts');
  }
  return response.json();
}

// Billing
export async function fetchPlans() {
  const response = await fetchWithAuth('/api/v1/billing/plans');
  if (!response.ok) {
    throw new Error('Failed to fetch plans');
  }
  return response.json();
}

export async function getCurrentSubscription() {
  const response = await fetchWithAuth('/api/v1/billing/subscription');
  if (!response.ok) {
    if (response.status === 404) {
      return null; // No subscription found
    }
    throw new Error('Failed to fetch subscription');
  }
  return response.json();
}

// Tools API
export async function getTools(): Promise<any[]> {
  const response = await fetchWithAuth('/api/v1/tools');
  if (!response.ok) {
    throw new Error('Failed to fetch tools');
  }
  return response.json();
}

export async function createToolConfiguration(config: any): Promise<any> {
  const response = await fetchWithAuth('/api/v1/tool-configurations', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(config),
  });
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new Error(
      extractErrorMessage(errorData, 'Failed to create tool configuration')
    );
  }
  return response.json();
}

export async function getToolConfiguration(configId: string): Promise<any> {
  const response = await fetchWithAuth(
    `/api/v1/tool-configurations/${configId}`
  );
  if (!response.ok) {
    throw new Error('Failed to fetch tool configuration');
  }
  return response.json();
}

export async function updateToolConfiguration(
  configId: string,
  config: any
): Promise<any> {
  const response = await fetchWithAuth(
    `/api/v1/tool-configurations/${configId}`,
    {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(config),
    }
  );
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new Error(
      extractErrorMessage(errorData, 'Failed to update tool configuration')
    );
  }
  return response.json();
}

export async function deleteToolConfiguration(configId: string): Promise<void> {
  const response = await fetchWithAuth(
    `/api/v1/tool-configurations/${configId}`,
    {
      method: 'DELETE',
    }
  );
  if (!response.ok) {
    throw new Error('Failed to delete tool configuration');
  }
}

// Tool Approval Condition API
export async function getToolApprovalCondition(configId: string): Promise<any> {
  const response = await fetchWithAuth(
    `/api/v1/tool-configurations/${configId}/approval-condition`
  );
  if (!response.ok) {
    // 404 is expected if no condition exists yet
    if (response.status === 404) {
      return null;
    }
    throw new Error('Failed to fetch tool approval condition');
  }
  return response.json();
}

export async function updateToolApprovalCondition(
  configId: string,
  condition: string | null
): Promise<any> {
  const response = await fetchWithAuth(
    `/api/v1/tool-configurations/${configId}/condition`,
    {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ approval_condition: condition }),
    }
  );
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new Error(
      extractErrorMessage(errorData, 'Failed to update tool approval condition')
    );
  }
  return response.json();
}

// Access Rules API
export interface AccessRule {
  id: string;
  account_id: string;
  tool_configuration_id: string;
  action: 'allow' | 'deny' | 'require_approval';
  condition_expression: string | null;
  condition_type: 'simple' | 'cel';
  priority: number;
  description: string | null;
  is_enabled: boolean;
  approval_workflow_id: string | null;
}

export interface AccessRuleCreate {
  action: 'allow' | 'deny' | 'require_approval';
  condition_expression?: string | null;
  condition_type?: 'simple' | 'cel';
  priority?: number;
  description?: string | null;
  is_enabled?: boolean;
  approval_workflow_id?: string | null;
}

export interface AccessRuleUpdate {
  action?: 'allow' | 'deny' | 'require_approval';
  condition_expression?: string | null;
  condition_type?: 'simple' | 'cel';
  priority?: number;
  description?: string | null;
  is_enabled?: boolean;
  approval_workflow_id?: string | null;
}

export async function listAccessRules(configId: string): Promise<AccessRule[]> {
  const response = await fetchWithAuth(
    `/api/v1/tool-configurations/${configId}/access-rules`
  );
  if (!response.ok) {
    throw new Error('Failed to fetch access rules');
  }
  return response.json();
}

export async function createAccessRule(
  configId: string,
  rule: AccessRuleCreate
): Promise<AccessRule> {
  const response = await fetchWithAuth(
    `/api/v1/tool-configurations/${configId}/access-rules`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(rule),
    }
  );
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new Error(
      extractErrorMessage(errorData, 'Failed to create access rule')
    );
  }
  return response.json();
}

export async function updateAccessRule(
  ruleId: string,
  rule: AccessRuleUpdate
): Promise<AccessRule> {
  const response = await fetchWithAuth(`/api/v1/access-rules/${ruleId}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(rule),
  });
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new Error(
      extractErrorMessage(errorData, 'Failed to update access rule')
    );
  }
  return response.json();
}

export async function deleteAccessRule(ruleId: string): Promise<void> {
  const response = await fetchWithAuth(`/api/v1/access-rules/${ruleId}`, {
    method: 'DELETE',
  });
  if (!response.ok) {
    throw new Error('Failed to delete access rule');
  }
}

export interface ModelIOCondition {
  expression: string;
  action: 'allow' | 'deny' | 'require_approval';
  condition_type?: 'simple' | 'cel';
  description?: string | null;
}

export interface ModelIODetectors {
  pii?: boolean | { types?: string[] };
  injection?: boolean;
  moderation?: boolean | { backend?: string };
}

export interface ModelIORule {
  id: string;
  target: 'model.request' | 'model.response';
  enabled?: boolean;
  description?: string | null;
  approval_workflow?: string | null;
  detectors?: ModelIODetectors | null;
  detector_timeout_ms?: number;
  on_detector_timeout?: 'allow' | 'deny';
  conditions: ModelIOCondition[];
}

export async function listModelIORules(): Promise<ModelIORule[]> {
  const response = await fetchWithAuth('/api/v1/policies/model-io-rules');
  if (!response.ok) {
    throw new Error('Failed to fetch model I/O rules');
  }
  const data = await response.json();
  return data.rules || [];
}

export async function createModelIORule(
  rule: ModelIORule
): Promise<ModelIORule> {
  const response = await fetchWithAuth('/api/v1/policies/model-io-rules', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(rule),
  });
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new Error(
      extractErrorMessage(errorData, 'Failed to save model I/O rule')
    );
  }
  return response.json();
}

export async function updateModelIORule(
  ruleId: string,
  rule: ModelIORule
): Promise<ModelIORule> {
  const response = await fetchWithAuth(
    `/api/v1/policies/model-io-rules/${encodeURIComponent(ruleId)}`,
    {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(rule),
    }
  );
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new Error(
      extractErrorMessage(errorData, 'Failed to update model I/O rule')
    );
  }
  return response.json();
}

export async function patchModelIORule(
  ruleId: string,
  patch: { enabled: boolean }
): Promise<ModelIORule> {
  const response = await fetchWithAuth(
    `/api/v1/policies/model-io-rules/${encodeURIComponent(ruleId)}`,
    {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    }
  );
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new Error(
      extractErrorMessage(errorData, 'Failed to update model I/O rule')
    );
  }
  return response.json();
}

export async function deleteModelIORule(ruleId: string): Promise<void> {
  const response = await fetchWithAuth(
    `/api/v1/policies/model-io-rules/${encodeURIComponent(ruleId)}`,
    { method: 'DELETE' }
  );
  if (!response.ok) {
    throw new Error('Failed to delete model I/O rule');
  }
}

// Approval Workflows API
export async function getApprovalWorkflows(): Promise<any[]> {
  const response = await fetchWithAuth('/api/v1/approval-workflows');
  if (!response.ok) {
    throw new Error('Failed to fetch approval workflows');
  }
  return response.json();
}

export async function createApprovalWorkflow(workflow: any): Promise<any> {
  const response = await fetchWithAuth('/api/v1/approval-workflows', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(workflow),
  });
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new Error(
      extractErrorMessage(errorData, 'Failed to create approval workflow')
    );
  }
  return response.json();
}

export async function getApprovalWorkflow(workflowId: string): Promise<any> {
  const response = await fetchWithAuth(
    `/api/v1/approval-workflows/${workflowId}`
  );
  if (!response.ok) {
    throw new Error('Failed to fetch approval workflow');
  }
  return response.json();
}

export async function updateApprovalWorkflow(
  workflowId: string,
  workflow: any
): Promise<any> {
  const response = await fetchWithAuth(
    `/api/v1/approval-workflows/${workflowId}`,
    {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(workflow),
    }
  );
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new Error(
      extractErrorMessage(errorData, 'Failed to update approval workflow')
    );
  }
  return response.json();
}

export async function deleteApprovalWorkflow(
  workflowId: string
): Promise<void> {
  const response = await fetchWithAuth(
    `/api/v1/approval-workflows/${workflowId}`,
    {
      method: 'DELETE',
    }
  );
  if (!response.ok) {
    throw new Error('Failed to delete approval workflow');
  }
}

// MCP Servers API
export async function getMCPServers(): Promise<any[]> {
  const response = await fetchWithAuth('/api/v1/mcp-servers');
  if (!response.ok) {
    throw new Error('Failed to fetch MCP servers');
  }
  return response.json();
}

export async function createMCPServer(server: any): Promise<any> {
  const response = await fetchWithAuth('/api/v1/mcp-servers', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(server),
  });
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new Error(
      extractErrorMessage(errorData, 'Failed to create MCP server')
    );
  }
  return response.json();
}

export async function getMCPServer(serverId: string): Promise<any> {
  const response = await fetchWithAuth(`/api/v1/mcp-servers/${serverId}`);
  if (!response.ok) {
    throw new Error('Failed to fetch MCP server');
  }
  return response.json();
}

export async function updateMCPServer(
  serverId: string,
  server: any
): Promise<any> {
  const response = await fetchWithAuth(`/api/v1/mcp-servers/${serverId}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(server),
  });
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new Error(
      extractErrorMessage(errorData, 'Failed to update MCP server')
    );
  }
  return response.json();
}

export async function deleteMCPServer(serverId: string): Promise<void> {
  const response = await fetchWithAuth(`/api/v1/mcp-servers/${serverId}`, {
    method: 'DELETE',
  });
  if (!response.ok) {
    throw new Error('Failed to delete MCP server');
  }
}

export async function scanMCPServer(serverId: string): Promise<any> {
  const response = await fetchWithAuth(`/api/v1/mcp-servers/${serverId}/scan`, {
    method: 'POST',
  });
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new Error(
      extractErrorMessage(errorData, 'Failed to scan MCP server')
    );
  }
  return response.json();
}

export async function getMCPServerTools(serverId: string): Promise<any[]> {
  const response = await fetchWithAuth(`/api/v1/mcp-servers/${serverId}/tools`);
  if (!response.ok) {
    throw new Error('Failed to fetch MCP server tools');
  }
  return response.json();
}

// Tools API - Get all available tools (built-in and external)
export async function getAllTools(): Promise<any[]> {
  const response = await fetchWithAuth('/api/v1/tools');
  if (!response.ok) {
    throw new Error('Failed to fetch tools');
  }
  return response.json();
}

// Approval Requests API
export async function getApprovalRequest(requestId: string): Promise<any> {
  const response = await fetchWithAuth(
    `/api/v1/approval-requests/${requestId}`
  );
  if (!response.ok) {
    throw new Error('Failed to fetch approval request');
  }
  return response.json();
}

export async function listApprovalRequests(params?: {
  status?: string;
  execution_id?: string;
  limit?: number;
  skip?: number;
}): Promise<any[]> {
  const queryParams = new URLSearchParams();
  if (params?.status) queryParams.append('status', params.status);
  if (params?.execution_id)
    queryParams.append('execution_id', params.execution_id);
  if (params?.limit) queryParams.append('limit', params.limit.toString());
  if (params?.skip) queryParams.append('skip', params.skip.toString());

  const response = await fetchWithAuth(
    `/api/v1/approval-requests?${queryParams.toString()}`
  );
  if (!response.ok) {
    throw new Error('Failed to fetch approval requests');
  }
  return response.json();
}

/**
 * Build the JSON body for an approve/decline decision.
 *
 * Accepts either a bare comment string (legacy call sites) or an options object
 * that can also carry a question answer (`selected_option` / `answer_text`).
 * Answer fields are only serialized when present so older backends keep seeing
 * the exact payload they saw before.
 */
export function buildApprovalDecisionBody(
  approved: boolean,
  commentOrOptions?: string | ApprovalDecisionOptions | null
): Record<string, unknown> {
  const options: ApprovalDecisionOptions =
    typeof commentOrOptions === 'string' || commentOrOptions == null
      ? { comment: commentOrOptions ?? null }
      : commentOrOptions;

  const body: Record<string, unknown> = {
    approved,
    comment: options.comment || null,
  };
  if (options.selected_option != null && options.selected_option !== '') {
    body.selected_option = options.selected_option;
  }
  if (options.answer_text != null && options.answer_text !== '') {
    body.answer_text = options.answer_text;
  }
  return body;
}

export async function approveRequest(
  requestId: string,
  commentOrOptions?: string | ApprovalDecisionOptions
): Promise<any> {
  const response = await fetchWithAuth(
    `/api/v1/approval-requests/${requestId}/approve`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(buildApprovalDecisionBody(true, commentOrOptions)),
    }
  );
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new Error(
      extractErrorMessage(errorData, 'Failed to approve request')
    );
  }
  return response.json();
}

export async function declineRequest(
  requestId: string,
  commentOrOptions?: string | ApprovalDecisionOptions
): Promise<any> {
  const response = await fetchWithAuth(
    `/api/v1/approval-requests/${requestId}/decline`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(buildApprovalDecisionBody(false, commentOrOptions)),
    }
  );
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new Error(
      extractErrorMessage(errorData, 'Failed to decline request')
    );
  }
  return response.json();
}

// ============================================================================
// User Management API Functions
// ============================================================================

export async function getUsers(
  skip = 0,
  limit = 100
): Promise<import('./types').UserListResponse> {
  const response = await fetchWithAuth(
    `/api/v1/users?skip=${skip}&limit=${limit}`
  );
  if (!response.ok) {
    throw new Error('Failed to fetch users');
  }
  return response.json();
}

export async function getUser(userId: string): Promise<import('./types').User> {
  const response = await fetchWithAuth(`/api/v1/users/${userId}`);
  if (!response.ok) {
    throw new Error('Failed to fetch user');
  }
  return response.json();
}

export async function createUser(
  userData: import('./types').UserCreate
): Promise<import('./types').User> {
  const response = await fetchWithAuth('/api/v1/users', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(userData),
  });
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new Error(extractErrorMessage(errorData, 'Failed to create user'));
  }
  return response.json();
}

export async function updateUser(
  userId: string,
  userData: import('./types').UserUpdate
): Promise<import('./types').User> {
  const response = await fetchWithAuth(`/api/v1/users/${userId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(userData),
  });
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new Error(extractErrorMessage(errorData, 'Failed to update user'));
  }
  return response.json();
}

export async function deactivateUser(userId: string): Promise<void> {
  const response = await fetchWithAuth(`/api/v1/users/${userId}/deactivate`, {
    method: 'POST',
  });
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new Error(
      extractErrorMessage(errorData, 'Failed to deactivate user')
    );
  }
}

// ============================================================================
// Team Management API Functions
// ============================================================================

export async function getTeams(
  skip = 0,
  limit = 100
): Promise<import('./types').TeamListResponse> {
  const response = await fetchWithAuth(
    `/api/v1/teams?skip=${skip}&limit=${limit}`
  );
  if (!response.ok) {
    throw new Error('Failed to fetch teams');
  }
  return response.json();
}

export async function getTeam(teamId: string): Promise<import('./types').Team> {
  const response = await fetchWithAuth(`/api/v1/teams/${teamId}`);
  if (!response.ok) {
    throw new Error('Failed to fetch team');
  }
  return response.json();
}

export async function createTeam(
  teamData: import('./types').TeamCreate
): Promise<import('./types').Team> {
  const response = await fetchWithAuth('/api/v1/teams', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(teamData),
  });
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new Error(extractErrorMessage(errorData, 'Failed to create team'));
  }
  return response.json();
}

export async function updateTeam(
  teamId: string,
  teamData: import('./types').TeamUpdate
): Promise<import('./types').Team> {
  const response = await fetchWithAuth(`/api/v1/teams/${teamId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(teamData),
  });
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new Error(extractErrorMessage(errorData, 'Failed to update team'));
  }
  return response.json();
}

export async function deleteTeam(teamId: string): Promise<void> {
  const response = await fetchWithAuth(`/api/v1/teams/${teamId}`, {
    method: 'DELETE',
  });
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new Error(extractErrorMessage(errorData, 'Failed to delete team'));
  }
}

export async function getTeamMembers(
  teamId: string
): Promise<import('./types').TeamMember[]> {
  const response = await fetchWithAuth(`/api/v1/teams/${teamId}/members`);
  if (!response.ok) {
    throw new Error('Failed to fetch team members');
  }
  return response.json();
}

export async function addTeamMember(
  teamId: string,
  userId: string,
  roleId?: string
): Promise<import('./types').TeamMember> {
  const response = await fetchWithAuth(`/api/v1/teams/${teamId}/members`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ user_id: userId, role_id: roleId }),
  });
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new Error(
      extractErrorMessage(errorData, 'Failed to add team member')
    );
  }
  return response.json();
}

export async function removeTeamMember(
  teamId: string,
  userId: string
): Promise<void> {
  const response = await fetchWithAuth(
    `/api/v1/teams/${teamId}/members/${userId}`,
    {
      method: 'DELETE',
    }
  );
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new Error(
      extractErrorMessage(errorData, 'Failed to remove team member')
    );
  }
}

export async function getTeamRoles(
  teamId: string
): Promise<import('./types').Role[]> {
  const response = await fetchWithAuth(`/api/v1/teams/${teamId}/roles`);
  if (!response.ok) {
    throw new Error('Failed to fetch team roles');
  }
  return response.json();
}

export async function assignTeamRole(
  teamId: string,
  roleId: string
): Promise<void> {
  const response = await fetchWithAuth(`/api/v1/teams/${teamId}/roles`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ role_id: roleId }),
  });
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new Error(
      extractErrorMessage(errorData, 'Failed to assign role to team')
    );
  }
}

export async function removeTeamRole(
  teamId: string,
  roleId: string
): Promise<void> {
  const response = await fetchWithAuth(
    `/api/v1/teams/${teamId}/roles/${roleId}`,
    {
      method: 'DELETE',
    }
  );
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new Error(
      extractErrorMessage(errorData, 'Failed to remove role from team')
    );
  }
}

// ============================================================================
// Invitation Management API Functions
// ============================================================================

export async function getInvitations(
  skip = 0,
  limit = 100,
  status?: 'pending' | 'accepted' | 'expired' | 'cancelled'
): Promise<import('./types').InvitationListResponse> {
  let url = `/api/v1/invitations?skip=${skip}&limit=${limit}`;
  if (status) {
    url += `&status=${status}`;
  }
  const response = await fetchWithAuth(url);
  if (!response.ok) {
    throw new Error('Failed to fetch invitations');
  }
  return response.json();
}

export async function createInvitation(
  invitationData: import('./types').InvitationCreate
): Promise<import('./types').UserInvitation> {
  const response = await fetchWithAuth('/api/v1/invitations', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(invitationData),
  });
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new Error(
      extractErrorMessage(errorData, 'Failed to create invitation')
    );
  }
  return response.json();
}

export async function resendInvitation(invitationId: string): Promise<void> {
  const response = await fetchWithAuth(
    `/api/v1/invitations/${invitationId}/resend`,
    {
      method: 'POST',
    }
  );
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new Error(
      extractErrorMessage(errorData, 'Failed to resend invitation')
    );
  }
}

export async function cancelInvitation(invitationId: string): Promise<void> {
  const response = await fetchWithAuth(`/api/v1/invitations/${invitationId}`, {
    method: 'DELETE',
  });
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new Error(
      extractErrorMessage(errorData, 'Failed to cancel invitation')
    );
  }
}

// ============================================================================
// Role Management API Functions
// ============================================================================

export async function getRoles(): Promise<import('./types').RoleListResponse> {
  const response = await fetchWithAuth('/api/v1/roles');
  if (!response.ok) {
    throw new Error('Failed to fetch roles');
  }
  return response.json();
}

export async function getUserRoles(
  userId: string
): Promise<import('./types').Role[]> {
  const response = await fetchWithAuth(`/api/v1/users/${userId}/roles`);
  if (!response.ok) {
    throw new Error('Failed to fetch user roles');
  }
  return response.json();
}

export async function assignUserRole(
  userId: string,
  roleId: string
): Promise<void> {
  const response = await fetchWithAuth(`/api/v1/users/${userId}/roles`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ role_id: roleId }),
  });
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new Error(extractErrorMessage(errorData, 'Failed to assign role'));
  }
}

export async function removeUserRole(
  userId: string,
  roleId: string
): Promise<void> {
  const response = await fetchWithAuth(
    `/api/v1/users/${userId}/roles/${roleId}`,
    {
      method: 'DELETE',
    }
  );
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new Error(extractErrorMessage(errorData, 'Failed to remove role'));
  }
}

// Features API
export interface FeaturesResponse {
  plugins: Array<{
    name: string;
    version: string;
    description: string;
  }>;
  features: {
    [key: string]: boolean | string[];
  };
}

export async function getFeatures(): Promise<FeaturesResponse> {
  const now = Date.now();
  if (featuresCache && featuresCache.expiresAt > now) {
    return featuresCache.data;
  }
  if (featuresInflight) {
    return featuresInflight;
  }

  const epoch = featuresEpoch;
  featuresInflight = (async () => {
    try {
      const response = await fetchPublic('/api/v1/features');
      if (!response.ok) {
        throw new Error('Failed to fetch features');
      }
      const data = (await response.json()) as FeaturesResponse;
      if (epoch === featuresEpoch) {
        featuresCache = {
          data,
          expiresAt: Date.now() + FEATURES_CACHE_TTL_MS,
        };
      }
      return data;
    } finally {
      if (epoch === featuresEpoch) {
        featuresInflight = null;
      }
    }
  })();

  return featuresInflight;
}

// Account Organization API
export interface AccountOrganization {
  id: string;
  organization_name: string | null;
  created_at: string;
  updated_at: string;
}

export async function getAccountOrganization(): Promise<AccountOrganization> {
  const response = await fetchWithAuth('/api/v1/account/details');
  if (!response.ok) {
    throw new Error('Failed to fetch account organization');
  }
  return response.json();
}

export async function updateAccountOrganization(
  details: Partial<AccountOrganization>
): Promise<AccountOrganization> {
  const response = await fetchWithAuth('/api/v1/account/details', {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(details),
  });
  if (!response.ok) {
    throw new Error('Failed to update account organization');
  }
  return response.json();
}

// GitHub App OAuth API
export interface TrackerAuthMethodsResponse {
  methods: string[];
  github_app_configured: boolean;
}

export async function getTrackerAuthMethods(): Promise<TrackerAuthMethodsResponse> {
  const response = await fetchWithAuth('/api/v1/trackers/auth-methods');
  if (!response.ok) {
    throw new Error('Failed to fetch tracker auth methods');
  }
  return response.json();
}

export interface GitHubAuthUrlResponse {
  authorization_url: string;
  state: string;
}

export async function getGitHubAuthUrl(): Promise<GitHubAuthUrlResponse> {
  const response = await fetchWithAuth('/api/v1/auth/github/authorize');
  if (!response.ok) {
    throw new Error('Failed to get GitHub authorization URL');
  }
  return response.json();
}

export interface GitHubInstallation {
  id: string;
  installation_id: number;
  target_type: string;
  target_id: number; // GitHub org/user ID - use this for scope rules
  target_login: string;
  permissions: Record<string, string>;
  repository_selection: string;
  is_suspended: boolean;
  created_at?: string;
}

export async function getGitHubInstallations(): Promise<GitHubInstallation[]> {
  const response = await fetchWithAuth('/api/v1/github/installations');
  if (!response.ok) {
    throw new Error('Failed to fetch GitHub installations');
  }
  const data = await response.json();
  return data.installations;
}

export async function getGitHubInstallation(
  installationId: string
): Promise<GitHubInstallation> {
  const response = await fetchWithAuth(
    `/api/v1/github/installations/${installationId}`
  );
  if (!response.ok) {
    throw new Error('Failed to fetch GitHub installation');
  }
  return response.json();
}

export async function unlinkGitHubInstallation(
  installationId: string
): Promise<void> {
  const response = await fetchWithAuth(
    `/api/v1/github/installations/${installationId}`,
    { method: 'DELETE' }
  );
  if (!response.ok) {
    throw new Error('Failed to unlink GitHub installation');
  }
}

export interface CompleteGitHubInstallationRequest {
  installation_id: string;
  code?: string;
}

export async function completeGitHubInstallation(
  data: CompleteGitHubInstallationRequest
): Promise<any> {
  // Backend expects installation_id and optional code as query parameters
  const params = new URLSearchParams();
  params.set('installation_id', data.installation_id);
  if (data.code) {
    params.set('code', data.code);
  }

  const response = await fetchWithAuth(
    `/api/v1/auth/github/complete-installation?${params.toString()}`,
    {
      method: 'POST',
    }
  );
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new Error(
      errorData.detail || 'Failed to complete GitHub installation'
    );
  }
  return response.json();
}

// Policy Generation API
export async function generatePolicy(options: {
  prompt: string;
  includeCurrentConfig?: boolean;
}): Promise<{ yaml: string; warnings: string[] }> {
  const response = await fetchWithAuth('/api/v1/policies/generate', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      prompt: options.prompt,
      include_current_config: options.includeCurrentConfig ?? true,
    }),
  });
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new Error(
      extractErrorMessage(errorData, 'Failed to generate policy')
    );
  }
  return response.json();
}

export async function generatePolicyFromAudit(options?: {
  startDate?: string;
  endDate?: string;
}): Promise<{ yaml: string; warnings: string[] }> {
  const response = await fetchWithAuth('/api/v1/policies/generate-from-audit', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      start_date: options?.startDate || null,
      end_date: options?.endDate || null,
    }),
  });
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new Error(
      extractErrorMessage(
        errorData,
        'Failed to generate policy from audit logs'
      )
    );
  }
  return response.json();
}

export interface BudgetPolicy {
  id: string;
  subject_type: string;
  subject_id: string | null;
  model_alias: string | null;
  period: 'hourly' | 'daily' | 'weekly' | 'monthly' | 'yearly' | 'all_time';
  hard_limit_usd: number | null;
  soft_limit_usd: number | null;
  notify_on_soft: boolean;
  notify_on_hard: boolean;
  notification_user_ids: string[] | null;
  notification_team_ids: string[] | null;
  notification_emails: string[] | null;
  // Spend within the policy's CURRENT period window (today for daily, this
  // month for monthly, ...), computed server-side from the period-aligned
  // budget spend buckets. Optional for backward compatibility.
  current_spend_usd?: number | null;
  period_start?: string | null;
  period_end?: string | null;
}

export interface BudgetPolicyCreate {
  subject_type: string;
  subject_id: string | null;
  model_alias: string | null;
  period: string;
  hard_limit_usd: number | null;
  soft_limit_usd: number | null;
  notify_on_soft: boolean;
  notify_on_hard: boolean;
  notification_user_ids: string[] | null;
  notification_team_ids: string[] | null;
  notification_emails: string[] | null;
}

export async function getBudgetPolicies(
  subject_type?: string,
  subject_id?: string
): Promise<BudgetPolicy[]> {
  const params = new URLSearchParams();
  if (subject_type) params.append('subject_type', subject_type);
  if (subject_id) params.append('subject_id', subject_id);
  const q = params.toString();
  const response = await fetchWithAuth(
    `/api/v1/budget/policies${q ? '?' + q : ''}`
  );
  if (!response.ok) throw new Error('Failed to fetch budget policies');
  return response.json();
}

export async function createBudgetPolicy(
  data: BudgetPolicyCreate
): Promise<BudgetPolicy> {
  const response = await fetchWithAuth('/api/v1/budget/policies', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
  if (!response.ok) throw new Error('Failed to create budget policy');
  return response.json();
}

export async function updateBudgetPolicy(
  id: string,
  data: Partial<BudgetPolicyCreate>
): Promise<BudgetPolicy> {
  const response = await fetchWithAuth(`/api/v1/budget/policies/${id}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
  if (!response.ok) throw new Error('Failed to update budget policy');
  return response.json();
}

export async function deleteBudgetPolicy(id: string): Promise<void> {
  const response = await fetchWithAuth(`/api/v1/budget/policies/${id}`, {
    method: 'DELETE',
  });
  if (!response.ok) throw new Error('Failed to delete budget policy');
}

// --- Approval bypasses (approval / notification fatigue escape hatch) -------

/**
 * Fetch the calling user's current bypass state.
 *
 * Drives the console warning banner. Deliberately cheap so it can be polled.
 */
export async function getApprovalBypassStatus(): Promise<ApprovalBypassStatus> {
  const response = await fetchWithAuth('/api/v1/approval-bypasses/status');
  if (!response.ok) {
    throw new Error('Failed to fetch approval bypass status');
  }
  return response.json();
}

/** List every active bypass in the account (including teammates'). */
export async function listApprovalBypasses(): Promise<ApprovalBypass[]> {
  const response = await fetchWithAuth('/api/v1/approval-bypasses');
  if (!response.ok) {
    throw new Error('Failed to fetch approval bypasses');
  }
  return response.json();
}

/**
 * Open a time-boxed bypass for the current user.
 *
 * `durationMinutes` is required and server-capped - there is intentionally no
 * way to express an indefinite bypass.
 */
export async function createApprovalBypass(params: {
  mode: ApprovalBypassMode;
  durationMinutes: number;
  managedAgentId?: string | null;
  reason?: string | null;
}): Promise<ApprovalBypass> {
  const response = await fetchWithAuth('/api/v1/approval-bypasses', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      mode: params.mode,
      duration_minutes: params.durationMinutes,
      managed_agent_id: params.managedAgentId ?? null,
      reason: params.reason ?? null,
      created_via: 'console',
    }),
  });
  if (!response.ok) {
    throw new Error('Failed to create approval bypass');
  }
  return response.json();
}

/** End a single bypass early. */
export async function revokeApprovalBypass(
  bypassId: string
): Promise<ApprovalBypass> {
  const response = await fetchWithAuth(
    `/api/v1/approval-bypasses/${bypassId}`,
    { method: 'DELETE' }
  );
  if (!response.ok) {
    throw new Error('Failed to revoke approval bypass');
  }
  return response.json();
}

/** Revoke every active bypass in the account - the "panic off" button. */
export async function revokeAllApprovalBypasses(): Promise<ApprovalBypass[]> {
  const response = await fetchWithAuth('/api/v1/approval-bypasses/revoke-all', {
    method: 'POST',
  });
  if (!response.ok) {
    throw new Error('Failed to revoke approval bypasses');
  }
  return response.json();
}
