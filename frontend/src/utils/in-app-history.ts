/**
 * Whether the history entry behind this one belongs to the console.
 *
 * `document.referrer` cannot answer that: the router navigates with
 * `history.pushState`, which never updates it, so after an in-app click the
 * referrer still names whatever page loaded the app (or nothing). Instead
 * the router stamps every entry it writes with its in-app depth: a push is
 * one deeper than the entry it was made from, a replace keeps the depth, and
 * the first page loaded has none. The stamp lives in `history.state`, so it
 * follows back/forward and survives a reload.
 */

/** The `history.state` key the router stores the depth under. */
export const IN_APP_DEPTH_KEY = 'preloopInAppDepth';

/** In-app depth recorded on a history state; 0 when there is none. */
export function inAppDepth(state: unknown = window.history.state): number {
  if (!state || typeof state !== 'object') return 0;
  const depth = (state as Record<string, unknown>)[IN_APP_DEPTH_KEY];
  return typeof depth === 'number' && Number.isFinite(depth) && depth > 0
    ? depth
    : 0;
}

/**
 * The state the router writes for a navigation: one deeper than the current
 * entry for a push, the current depth for a replace (null when that is 0,
 * which is what the router wrote before depths existed).
 */
export function historyStateForNavigation(
  mode: 'push' | 'replace',
  current: unknown = window.history.state
): Record<string, number> | null {
  const depth = inAppDepth(current) + (mode === 'push' ? 1 : 0);
  return depth > 0 ? { [IN_APP_DEPTH_KEY]: depth } : null;
}

/**
 * True when `history.back()` lands on a console page this tab navigated
 * from. False on a page opened directly, from a shared link or a new tab,
 * and after Back has already returned to the first page loaded.
 */
export function hasInAppHistory(): boolean {
  return window.history.length > 1 && inAppDepth() > 0;
}
