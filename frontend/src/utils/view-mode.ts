/**
 * Persisted list/cards view mode for collection pages.
 *
 * Copied from the Flows page so Trackers and Models (and, later, Agents and
 * Flows themselves) share one storage contract. List is the default
 * (DESIGN.md, "Tables and views"): a table compares rows; cards are for
 * browsing and for phones. A stored choice wins. The narrow-viewport
 * fallback never overwrites that choice.
 */

export type ListViewMode = 'list' | 'cards';

export const LIST_VIEW_MODES: readonly ListViewMode[] = ['list', 'cards'];

/** List is the default so thirty items are compared, not browsed. */
export const DEFAULT_LIST_VIEW: ListViewMode = 'list';

/**
 * Below this width a multi-column table cannot hold its content, so cards
 * take over. Matches the Flows switcher hide rule.
 */
export const LIST_TO_CARDS_BREAKPOINT = '(max-width: 640px)';

export function isListViewMode(
  value: string | null | undefined,
  allowed: readonly string[] = LIST_VIEW_MODES
): value is ListViewMode {
  return Boolean(value && allowed.includes(value));
}

/**
 * Read the stored view for a page. Invalid or missing values fall back to
 * `fallback` (list, unless a page opts out).
 */
export function loadViewMode(
  storageKey: string,
  allowed: readonly string[] = LIST_VIEW_MODES,
  fallback: ListViewMode = DEFAULT_LIST_VIEW
): ListViewMode {
  try {
    const saved = localStorage.getItem(storageKey);
    if (isListViewMode(saved, allowed)) {
      return saved;
    }
  } catch {
    // Private mode and quota errors must not take the page down.
  }
  return fallback;
}

/** Persist the user's view choice. Failures are ignored. */
export function saveViewMode(storageKey: string, view: string): void {
  try {
    localStorage.setItem(storageKey, view);
  } catch {
    // Same as load: storage is a preference, not a requirement.
  }
}

/**
 * The view actually painted. On a phone a multi-column table would either
 * scroll sideways or crush every cell, so `list` renders as cards.
 */
export function effectiveViewMode(
  current: ListViewMode,
  narrowViewport: boolean
): ListViewMode {
  if (current === 'list' && narrowViewport) {
    return 'cards';
  }
  return current;
}

export interface NarrowViewportSubscription {
  matches: boolean;
  disconnect: () => void;
}

/**
 * Subscribe to the list-to-cards breakpoint. Returns the current match and
 * a disconnect function for `disconnectedCallback`.
 */
export function subscribeNarrowViewport(
  onChange: (narrow: boolean) => void,
  query: string = LIST_TO_CARDS_BREAKPOINT
): NarrowViewportSubscription {
  if (
    typeof window === 'undefined' ||
    typeof window.matchMedia !== 'function'
  ) {
    return { matches: false, disconnect: () => undefined };
  }
  const mediaQuery = window.matchMedia(query);
  const handler = (event: MediaQueryListEvent) => {
    onChange(event.matches);
  };
  mediaQuery.addEventListener('change', handler);
  return {
    matches: mediaQuery.matches,
    disconnect: () => mediaQuery.removeEventListener('change', handler),
  };
}
