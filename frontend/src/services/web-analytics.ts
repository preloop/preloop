/**
 * Thin wrapper for the privacy-friendly web analytics snippet that the
 * console nginx injects at serve time when the helm `analytics` values are
 * enabled (see helm/preloop/values.yaml). Follows the Plausible custom-event
 * convention: window.plausible('Event', { props }). The injected stub queues
 * events fired before the tracker script loads; when analytics is disabled
 * the stub is absent and calls are silent no-ops.
 */

import { activityTracker } from './activity-tracker';

type PlausibleFn = (
  event: string,
  options?: { props?: Record<string, string> }
) => void;

const CURRENT_PATH_KEY = 'webAnalytics:currentPath';
const PREV_PATH_KEY = 'webAnalytics:prevPath';

/**
 * Remember the SPA route the visitor was on before the current one, so
 * conversion events can carry a `prev_path` property (e.g. "signed up after
 * viewing /pricing"). Called from the router's location-changed listener.
 */
export function recordPathChange(path: string): void {
  try {
    const current = sessionStorage.getItem(CURRENT_PATH_KEY);
    if (current === path) return;
    if (current) sessionStorage.setItem(PREV_PATH_KEY, current);
    sessionStorage.setItem(CURRENT_PATH_KEY, path);
  } catch {
    // sessionStorage unavailable (private mode etc.) — path memory is optional.
  }
}

function getPrevPath(): string | undefined {
  try {
    return sessionStorage.getItem(PREV_PATH_KEY) ?? undefined;
  } catch {
    return undefined;
  }
}

/**
 * Fire a named conversion/goal event, optionally with properties. The page
 * the visitor navigated from (within the SPA session) is attached
 * automatically as `prev_path`.
 *
 * Every goal is mirrored into the first-party ActivityTracker as a
 * conversion event with the same name, so Plausible goals and the admin
 * funnel stay aligned by construction — even when Plausible is disabled.
 */
export function trackGoal(event: string, props?: Record<string, string>): void {
  const prevPath = getPrevPath();
  const merged = {
    ...(prevPath ? { prev_path: prevPath } : {}),
    ...props,
  };

  // First-party mirror (fires regardless of the Plausible snippet).
  try {
    activityTracker.trackConversion(
      event,
      undefined,
      Object.keys(merged).length ? merged : undefined
    );
  } catch {
    // Analytics must never break the app.
  }

  const plausible = (window as unknown as { plausible?: PlausibleFn })
    .plausible;
  if (typeof plausible !== 'function') return;
  try {
    plausible(
      event,
      Object.keys(merged).length ? { props: merged } : undefined
    );
  } catch {
    // Analytics must never break the app.
  }
}
