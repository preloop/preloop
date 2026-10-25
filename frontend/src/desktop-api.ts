import { REST_API_PREFIX } from './api-path';
/**
 * Console client for brokered live desktop views (#870).
 *
 * Every route answers 404 `desktop_views_disabled` while the server runs
 * without `PRELOOP_DESKTOP_VIEWS_ENABLED`, and an older server has no route
 * at all. Both read as "no desktop here": the list call returns an empty
 * list and the console renders nothing, so the UI stays inert until the
 * backend ships and is switched on.
 */
import { fetchWithAuth, getFeatures, isSameOriginPath } from './api';
import { hasPermission, type UserPermissions } from './permissions';

/**
 * Whether the caller may ask to take control of a view. Viewing needs
 * `control_managed_agent`; control is an approval, so it also needs
 * `decide_approvals` (the OSS flow is self-confirm with a reason). With RBAC
 * inactive (`permissions: null`) both are granted.
 *
 * A profile that failed to load is not "RBAC inactive": without it the
 * answer is unknown, so the control affordance stays hidden. The server
 * authorizes the control request either way.
 */
export function canTakeDesktopControl(
  profile: { permissions?: UserPermissions } | null | undefined
): boolean {
  if (!profile) return false;
  return (
    hasPermission(profile.permissions, 'control_managed_agent') &&
    hasPermission(profile.permissions, 'decide_approvals')
  );
}

export interface DesktopViewHolder {
  view_id: string;
  user_id: string;
  username: string;
  since: string;
}

export interface DesktopEndpoint {
  id: string;
  endpoint_id: string;
  hostname: string;
  display: string;
  managed_agent_id: string | null;
  runner_id: string | null;
  state: 'online' | 'offline';
  last_seen_at: string;
  active_view: DesktopViewHolder | null;
}

export type DesktopViewState = 'pending' | 'live' | 'closed';

export interface DesktopView {
  view_id: string;
  endpoint_id: string;
  managed_agent_id: string | null;
  runtime_session_id: string | null;
  user_id: string;
  state: DesktopViewState;
  mode: 'view_only' | 'control';
  ws_url: string;
  created_at: string;
  live_at: string | null;
  expires_at: string;
  closed_at: string | null;
  closed_reason: string | null;
  control_expires_at: string | null;
}

/** A REST error from the desktop routes, carrying the contract's code. */
export class DesktopApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly holder: DesktopViewHolder | null;

  constructor(
    status: number,
    code: string,
    message: string,
    holder: DesktopViewHolder | null = null
  ) {
    super(message);
    this.name = 'DesktopApiError';
    this.status = status;
    this.code = code;
    this.holder = holder;
  }
}

async function errorFrom(response: Response): Promise<DesktopApiError> {
  let code = `http_${response.status}`;
  let message = `Request failed (${response.status}).`;
  let holder: DesktopViewHolder | null = null;
  try {
    const body = await response.json();
    const detail = body?.detail;
    if (detail && typeof detail === 'object') {
      if (typeof detail.code === 'string') code = detail.code;
      if (typeof detail.message === 'string') message = detail.message;
      if (detail.holder && typeof detail.holder === 'object') {
        holder = detail.holder as DesktopViewHolder;
      }
    } else if (typeof detail === 'string') {
      message = detail;
    }
  } catch {
    // Non-JSON error body: keep the status-derived code.
  }
  return new DesktopApiError(response.status, code, message, holder);
}

export interface DesktopEndpointFilter {
  managedAgentId?: string;
  runnerId?: string;
  runtimeSessionId?: string;
}

/**
 * Online desktop endpoints linked to an agent, runner or session.
 *
 * Returns an empty list without a request when `/features` says desktop
 * views are off, and an empty list when the feature is absent (404) or the
 * caller may not use it (403): in both cases there is nothing to offer.
 * Passive, so a failure never interrupts the page that asked.
 */
export async function listOnlineDesktopEndpoints(
  filter: DesktopEndpointFilter
): Promise<DesktopEndpoint[]> {
  // A server that says desktop views are off has nothing to list: do not
  // ask (it answers 404). A server too old to say still gets asked.
  try {
    if ((await getFeatures()).features?.desktop_views === false) return [];
  } catch {
    // Unknown: ask, as before.
  }
  const params = new URLSearchParams({ state: 'online' });
  if (filter.managedAgentId)
    params.set('managed_agent_id', filter.managedAgentId);
  if (filter.runnerId) params.set('runner_id', filter.runnerId);
  if (filter.runtimeSessionId)
    params.set('runtime_session_id', filter.runtimeSessionId);
  let response: Response;
  try {
    response = await fetchWithAuth(
      `${REST_API_PREFIX}/desktop/endpoints?${params.toString()}`,
      { passive: true }
    );
  } catch {
    return [];
  }
  if (!response.ok) return [];
  try {
    const body = await response.json();
    const endpoints = Array.isArray(body?.endpoints) ? body.endpoints : [];
    return endpoints.filter(
      (endpoint: DesktopEndpoint) => endpoint?.state === 'online'
    );
  } catch {
    return [];
  }
}

export interface CreateDesktopViewOptions {
  durationSeconds?: number;
  runtimeSessionId?: string;
}

/** Opens a view on one endpoint (`id` or host `endpoint_id`). */
export async function createDesktopView(
  endpointId: string,
  options: CreateDesktopViewOptions = {}
): Promise<DesktopView> {
  const body: Record<string, unknown> = {};
  if (options.durationSeconds) body.duration_seconds = options.durationSeconds;
  if (options.runtimeSessionId)
    body.runtime_session_id = options.runtimeSessionId;
  const response = await fetchWithAuth(
    `${REST_API_PREFIX}/desktop/endpoints/${encodeURIComponent(endpointId)}/views`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }
  );
  if (!response.ok) throw await errorFrom(response);
  return (await response.json()) as DesktopView;
}

export async function getDesktopView(viewId: string): Promise<DesktopView> {
  const response = await fetchWithAuth(
    `${REST_API_PREFIX}/desktop/views/${encodeURIComponent(viewId)}`,
    { cache: 'no-store' }
  );
  if (!response.ok) throw await errorFrom(response);
  return (await response.json()) as DesktopView;
}

/** Ends a view. Idempotent on the server; a 404 means it is already gone. */
export async function endDesktopView(viewId: string): Promise<void> {
  const response = await fetchWithAuth(
    `${REST_API_PREFIX}/desktop/views/${encodeURIComponent(viewId)}`,
    { method: 'DELETE' }
  );
  if (!response.ok && response.status !== 404) throw await errorFrom(response);
}

export async function requestDesktopControl(
  viewId: string,
  reason: string
): Promise<DesktopView> {
  const response = await fetchWithAuth(
    `${REST_API_PREFIX}/desktop/views/${encodeURIComponent(viewId)}/control`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ reason }),
    }
  );
  if (!response.ok) throw await errorFrom(response);
  return (await response.json()) as DesktopView;
}

export async function releaseDesktopControl(viewId: string): Promise<void> {
  const response = await fetchWithAuth(
    `${REST_API_PREFIX}/desktop/views/${encodeURIComponent(viewId)}/control/release`,
    { method: 'POST' }
  );
  if (!response.ok) throw await errorFrom(response);
}

/**
 * Absolute viewer socket URL for a view's `ws_url` path.
 *
 * Browsers cannot set an Authorization header on a WebSocket, so the user
 * JWT goes in `?token=` (the contract's browser path). The URL is built at
 * connect time and never rendered or logged.
 *
 * `ws_url` comes from the server and must be a path on this origin: an
 * absolute or protocol-relative URL would carry the token to another host,
 * so anything else throws instead of connecting.
 */
export function desktopViewerSocketUrl(
  wsPath: unknown,
  token: string,
  location: Pick<Location, 'protocol' | 'host'> = window.location
): string {
  if (!isSameOriginPath(wsPath)) {
    throw new Error('The server returned a desktop socket on another origin.');
  }
  const protocol = location.protocol === 'https:' ? 'wss' : 'ws';
  const url = new URL(`${protocol}://${location.host}${wsPath}`);
  if (url.host !== location.host) {
    throw new Error('The server returned a desktop socket on another origin.');
  }
  url.searchParams.set('token', token);
  return url.toString();
}
