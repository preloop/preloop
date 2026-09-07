/**
 * The console's router.
 *
 * A deliberately small replacement for the subset of `@vaadin/router` this app
 * ever used: nested routes with reused ancestors, `:params`, a `(.*)`
 * catch-all, `action`/`commands`, `redirect`, `onBeforeEnter` /
 * `onBeforeLeave` / `onAfterEnter`, in-app anchor interception through shadow
 * roots, back/forward, and a location-changed event for analytics.
 *
 * Owning component resolution is the point: `component: 'agents-view'` can
 * carry a `load()` that fetches the chunk before the element is created, which
 * is what makes route-level code splitting a router feature rather than a
 * wrapper around one.
 */

/** The `location` object handed to actions, guards and routed elements. */
export interface RouterLocation {
  /** Pathname of the resolved URL, without search or hash. */
  pathname: string;
  /** Query string including the leading `?`, or an empty string. */
  search: string;
  /** Fragment including the leading `#`, or an empty string. */
  hash: string;
  /** Decoded `:name` path parameters. */
  params: Record<string, string>;
  /** Parsed `search`, for callers that would otherwise build this again. */
  searchParams: URLSearchParams;
  /** The deepest matched route. */
  route: Route;
  /** Every route from the root of the match down to `route`. */
  routes: Route[];
}

/** Result markers an action or guard can return. */
export interface RouterCommands {
  /** Create the element for `name` and render it for this route. */
  component(name: string): HTMLElement;
  /** Navigate to `path` instead of rendering this route. */
  redirect(path: string): RedirectResult;
  /** Stay where we are. Only meaningful from `onBeforeLeave`. */
  prevent(): PreventResult;
}

export interface RedirectResult {
  readonly redirect: string;
}

export interface PreventResult {
  readonly cancel: true;
}

export type RouteAction = (
  context: RouterLocation,
  commands: RouterCommands
) => unknown | Promise<unknown>;

export interface Route {
  /** Path segment. Child paths are relative to the parent, leading `/` or not. */
  path: string;
  /** Custom element name to render when this route matches. */
  component?: string;
  /** Navigate here instead of rendering. Replaces the current history entry. */
  redirect?: string;
  children?: readonly Route[];
  action?: RouteAction;
  /**
   * Fetch the module that defines `component` before the element is created.
   * Resolved once and cached; a rejection is surfaced to the outlet with a
   * retry rather than left as a blank page.
   */
  load?: () => Promise<unknown>;
}

/** Element hooks a routed view may implement. */
interface RoutedElement extends HTMLElement {
  location?: RouterLocation;
  onBeforeEnter?: (
    location: RouterLocation,
    commands: RouterCommands,
    router: Router
  ) => unknown | Promise<unknown>;
  onBeforeLeave?: (
    location: RouterLocation,
    commands: RouterCommands,
    router: Router
  ) => unknown | Promise<unknown>;
  onAfterEnter?: (
    location: RouterLocation,
    commands: RouterCommands,
    router: Router
  ) => unknown;
}

/** How the router should render a pending or failed `Route.load()`. */
export interface LoadingRenderer {
  /** Show that a chunk is on the way. Returns a teardown function. */
  pending(parent: Element): () => void;
  /** Render the failure with a way to try again. */
  failed(parent: Element, error: unknown, retry: () => void): void;
}

/** A route path compiled to a matcher, with the chain that produced it. */
interface CompiledRoute {
  pattern: RegExp;
  keys: string[];
  chain: Route[];
}

/** Nested redirects are a configuration bug, not a state to recover from. */
const MAX_REDIRECTS = 8;

/** History entries this router wrote itself; see `dispatchIgnoredPopstate`. */
const IGNORED_POPSTATE = 'preloop-router-ignore';

/** Fired after every successful navigation. */
export const LOCATION_CHANGED = 'preloop-router-location-changed';

/**
 * The name Vaadin Router used. Still fired so that anything outside this
 * package that listened for it keeps working; in-tree consumers listen for
 * {@link LOCATION_CHANGED}. Drop once no listener remains.
 */
export const LEGACY_LOCATION_CHANGED = 'vaadin-router-location-changed';

function isRedirect(value: unknown): value is RedirectResult {
  return (
    typeof value === 'object' &&
    value !== null &&
    typeof (value as RedirectResult).redirect === 'string'
  );
}

function isPrevent(value: unknown): value is PreventResult {
  return (
    typeof value === 'object' &&
    value !== null &&
    (value as PreventResult).cancel === true
  );
}

/** Leading slash, no trailing slash, no empty segments. */
export function normalizePath(path: string): string {
  const collapsed = ('/' + path).replace(/\/{2,}/gu, '/');
  return collapsed.length > 1 ? collapsed.replace(/\/$/u, '') : '/';
}

/** Compile `/console/agents/:agentId` (or `(.*)`) into a matcher. */
function compilePath(path: string): { pattern: RegExp; keys: string[] } {
  const keys: string[] = [];
  if (path.includes('(.*)')) {
    return { pattern: /^\/.*$/u, keys };
  }
  const source = normalizePath(path)
    .split('/')
    .map((segment) => {
      if (!segment) return '';
      if (segment.startsWith(':')) {
        keys.push(segment.slice(1));
        return '/([^/]+)';
      }
      return '/' + segment.replace(/[.*+?^${}()|[\]\\]/gu, '\\$&');
    })
    .join('');
  return { pattern: new RegExp('^' + (source || '/') + '$', 'u'), keys };
}

/**
 * Flatten the route tree into an ordered list of matchers.
 *
 * Two things here are load-bearing and were both found by running the console's
 * suite against an earlier draft:
 *
 * 1. A child path is relative to its parent even when it is written with a
 *    leading slash. The console's table mixes `{ path: 'agents' }` and
 *    `{ path: '/agents' }` inside `/console`'s children.
 * 2. Children are emitted before their parent, so `/console` resolves to the
 *    `{ path: '' }` child (the overview) nested inside the shell rather than to
 *    the shell on its own.
 */
export function flattenRoutes(
  routes: readonly Route[],
  prefix = '',
  chain: readonly Route[] = []
): CompiledRoute[] {
  const flat: CompiledRoute[] = [];
  for (const route of routes) {
    const full = normalizePath(prefix + '/' + route.path);
    const nextChain = [...chain, route];
    if (route.children?.length) {
      flat.push(...flattenRoutes(route.children, full, nextChain));
    }
    flat.push({ ...compilePath(full), chain: nextChain });
  }
  return flat;
}

/** Split a URL-ish string into the three pieces a navigation needs. */
function splitUrl(path: string): {
  pathname: string;
  search: string;
  hash: string;
} {
  const url = new URL(path, window.location.origin);
  return { pathname: url.pathname, search: url.search, hash: url.hash };
}

/**
 * Anchors the router must not swallow, matching what Vaadin Router ignored:
 * modified clicks, other targets, downloads, opt-outs, cross-origin links, and
 * same-page fragment links (which the browser scrolls for us).
 */
function routableAnchor(event: MouseEvent): HTMLAnchorElement | undefined {
  if (event.defaultPrevented || event.button !== 0) return undefined;
  if (event.shiftKey || event.ctrlKey || event.altKey || event.metaKey) {
    return undefined;
  }
  const anchor = event
    .composedPath()
    .find(
      (node): node is HTMLAnchorElement =>
        (node as Element)?.nodeName?.toLowerCase?.() === 'a'
    );
  if (!anchor?.href) return undefined;
  if (anchor.target && anchor.target.toLowerCase() !== '_self')
    return undefined;
  if (anchor.hasAttribute('download')) return undefined;
  if (anchor.hasAttribute('router-ignore')) return undefined;
  const url = new URL(anchor.href, document.baseURI);
  if (url.origin !== window.location.origin) return undefined;
  if (url.pathname === window.location.pathname && url.hash) return undefined;
  return anchor;
}

/** Router instances that are listening, so the static `go` can reach them. */
const activeRouters = new Set<Router>();

export class Router {
  #outlet: Element | null = null;
  #flat: CompiledRoute[] = [];
  #chain: Route[] = [];
  #elements: RoutedElement[] = [];
  #renderId = 0;
  #listening = false;
  #loading: LoadingRenderer | null = null;
  #loaded = new WeakMap<Route, Promise<unknown>>();

  /** The location most recently rendered, or `null` before the first render. */
  location: RouterLocation | null = null;

  constructor(outlet?: Element | null) {
    if (outlet) this.setOutlet(outlet);
  }

  /**
   * Navigate to an in-app path. Returns whether a router picked it up, which
   * is what the anchor handler uses to decide against a full page load.
   */
  static go(
    path: string | { pathname: string; search?: string; hash?: string }
  ): boolean {
    const target =
      typeof path === 'string'
        ? splitUrl(path)
        : {
            pathname: path.pathname,
            search: path.search ?? '',
            hash: path.hash ?? '',
          };
    if (!activeRouters.size) return false;
    for (const router of activeRouters) {
      void router.render(target, { history: 'push' });
    }
    return true;
  }

  /**
   * An in-app URL for a path, with `:name` substitution and `<base href>`
   * applied. Lists use it to give rows real hrefs, so they stay
   * middle-clickable and copyable.
   */
  urlForPath(path: string, params?: Record<string, string | number>): string {
    let out = normalizePath(path);
    for (const [key, value] of Object.entries(params ?? {})) {
      out = out.replace(':' + key, encodeURIComponent(String(value)));
    }
    return new URL(out.replace(/^\//u, ''), document.baseURI).pathname;
  }

  setOutlet(outlet: Element | null): void {
    this.#outlet = outlet;
    if (outlet) this.subscribe();
  }

  getOutlet(): Element | null {
    return this.#outlet;
  }

  /**
   * Install the route table and render the current URL.
   *
   * Awaitable, unlike the fire-and-forget `Router.go`, because tests and the
   * app's bootstrap both want to know when the first view exists.
   */
  async setRoutes(routes: readonly Route[], skipRender = false): Promise<void> {
    this.#flat = flattenRoutes(routes);
    this.#chain = [];
    this.#elements = [];
    this.subscribe();
    if (!skipRender) {
      const { pathname, search, hash } = window.location;
      await this.render({ pathname, search, hash }, { history: 'replace' });
    }
  }

  /**
   * Decide what a pending or failed `Route.load()` looks like. Without one the
   * router simply waits, which is the right default for tests.
   */
  setLoadingRenderer(renderer: LoadingRenderer | null): void {
    this.#loading = renderer;
  }

  /** Match a pathname without rendering. Exposed for tests. */
  match(
    pathname: string
  ): { chain: Route[]; params: Record<string, string> } | null {
    const normalized = normalizePath(pathname);
    for (const candidate of this.#flat) {
      const found = candidate.pattern.exec(normalized);
      if (!found) continue;
      const params: Record<string, string> = {};
      candidate.keys.forEach((key, index) => {
        params[key] = decodeURIComponent(found[index + 1] ?? '');
      });
      return { chain: candidate.chain, params };
    }
    return null;
  }

  /**
   * Resolve a target and put the result in the outlet.
   *
   * Redirects are followed before any history entry is written, so a redirect
   * costs no back-button stop: pressing Back from `/console/settings/profile`
   * must not land on `/console/settings` and bounce forward again.
   */
  async render(
    target: string | { pathname: string; search?: string; hash?: string },
    options: { history?: 'push' | 'replace' | 'none' } = {}
  ): Promise<void> {
    const renderId = ++this.#renderId;
    const start =
      typeof target === 'string'
        ? splitUrl(target)
        : {
            pathname: target.pathname,
            search: target.search ?? '',
            hash: target.hash ?? '',
          };
    let current = start;

    for (let hop = 0; hop <= MAX_REDIRECTS; hop++) {
      const outcome = await this.#renderOnce(current, renderId);
      if (outcome.stale || outcome.cancelled) return;
      if (outcome.redirect) {
        current = splitUrl(outcome.redirect);
        continue;
      }
      this.#writeHistory(current, options.history ?? 'none', start);
      if (outcome.location) this.#announce(outcome.location);
      return;
    }
    console.error(`Too many redirects rendering ${start.pathname}`);
  }

  /** One resolution pass. Returns a redirect instead of following it. */
  async #renderOnce(
    target: { pathname: string; search: string; hash: string },
    renderId: number
  ): Promise<{
    redirect?: string;
    cancelled?: boolean;
    stale?: boolean;
    location?: RouterLocation;
  }> {
    if (!this.#outlet) return { stale: true };
    const hit = this.match(target.pathname);
    if (!hit) return { stale: true };

    const context: RouterLocation = {
      pathname: normalizePath(target.pathname),
      search: target.search,
      hash: target.hash,
      params: hit.params,
      searchParams: new URLSearchParams(target.search),
      route: hit.chain[hit.chain.length - 1],
      routes: hit.chain,
    };
    const commands = this.#commands();

    const leaving = this.#elements[this.#elements.length - 1];
    if (leaving?.onBeforeLeave) {
      const verdict = await leaving.onBeforeLeave(context, commands, this);
      if (isPrevent(verdict)) return { cancelled: true };
      if (isRedirect(verdict)) return { redirect: verdict.redirect };
      if (renderId !== this.#renderId) return { stale: true };
    }

    // Walk the chain outermost first, reusing an ancestor whose route and tag
    // are unchanged. Reuse is what keeps <console-shell> (its nav state, its
    // feature fetches, its websocket subscriptions) alive across in-console
    // navigation; a router that replaces the outlet wholesale rebuilds the
    // shell on every click.
    let parent: Element = this.#outlet;
    let diverged = false;
    const next: RoutedElement[] = [];

    for (let level = 0; level < hit.chain.length; level++) {
      const route = hit.chain[level];
      if (route.redirect) return { redirect: route.redirect };

      let element: RoutedElement | null = null;
      if (route.action) {
        const result = await route.action.call(route, context, commands);
        if (renderId !== this.#renderId) return { stale: true };
        if (isRedirect(result)) return { redirect: result.redirect };
        if (isPrevent(result)) return { cancelled: true };
        if (result instanceof HTMLElement) element = result as RoutedElement;
      }

      const reusable =
        !diverged &&
        !element &&
        this.#chain[level] === route &&
        this.#elements[level]?.parentElement === parent &&
        (!route.component ||
          this.#elements[level]?.localName === route.component);

      if (reusable) {
        element = this.#elements[level];
      } else if (!element && route.component) {
        const chunk = await this.#loadComponent(route, parent, target);
        if (chunk === 'failed') return { stale: true };
        if (renderId !== this.#renderId) return { stale: true };
        element = document.createElement(route.component) as RoutedElement;
      }

      if (!element) continue;
      if (!reusable) diverged = true;

      element.location = context;
      if (element.onBeforeEnter) {
        const verdict = await element.onBeforeEnter(context, commands, this);
        if (renderId !== this.#renderId) return { stale: true };
        if (isRedirect(verdict)) return { redirect: verdict.redirect };
        if (isPrevent(verdict)) return { cancelled: true };
      }
      if (!reusable) parent.replaceChildren(element);
      next.push(element);
      parent = element;
    }

    if (!next.length) return { stale: true };

    this.#chain = hit.chain.slice(0, next.length);
    this.#elements = next;
    this.location = context;
    next[next.length - 1]?.onAfterEnter?.(context, commands, this);
    return { location: context };
  }

  /**
   * Await a route's chunk, showing the pending state only if the wait is long
   * enough to be worth acknowledging, and a retry if the chunk never arrives.
   */
  async #loadComponent(
    route: Route,
    parent: Element,
    target: { pathname: string; search: string; hash: string }
  ): Promise<'ok' | 'failed'> {
    if (!route.load) return 'ok';
    const cached = this.#loaded.get(route);
    const pending = cached ?? route.load();
    this.#loaded.set(route, pending);
    const stopPending = this.#loading?.pending(parent);
    try {
      await pending;
      return 'ok';
    } catch (error) {
      // A failed chunk is usually a stale index against a redeployed build.
      // Offer the retry in place; a blank outlet reads as a broken app.
      this.#loaded.delete(route);
      console.error(
        `Failed to load route module for ${target.pathname}`,
        error
      );
      if (!this.#loading) return 'failed';
      this.#loading.failed(parent, error, () => {
        void this.render(target, { history: 'none' });
      });
      return 'failed';
    } finally {
      stopPending?.();
    }
  }

  #commands(): RouterCommands {
    return {
      component: (name: string) => document.createElement(name),
      redirect: (path: string) => ({ redirect: path }),
      prevent: () => ({ cancel: true }),
    };
  }

  /**
   * Write the history entry once the destination is known. A redirect
   * replaces the entry the navigation started from instead of adding one.
   */
  #writeHistory(
    final: { pathname: string; search: string; hash: string },
    mode: 'push' | 'replace' | 'none',
    start: { pathname: string; search: string; hash: string }
  ): void {
    if (mode === 'none') return;
    const redirected =
      final.pathname !== start.pathname || final.search !== start.search;
    // A render that landed where it was asked to has nothing to write: the URL
    // is already right for popstate, and on first load an action may have
    // rewritten it deliberately (the /console OAuth handler strips the token
    // fragment). Restoring the requested URL there would put the tokens back.
    if (mode === 'replace' && !redirected) return;
    const same =
      window.location.pathname === final.pathname &&
      window.location.search === final.search &&
      window.location.hash === final.hash;
    if (same) return;
    const url = final.pathname + final.search + final.hash;
    window.history[redirected ? 'replaceState' : 'pushState'](null, '', url);
    this.#dispatchIgnoredPopstate();
  }

  /**
   * Tell the page the URL moved. `main.ts` counts page views on `popstate` as
   * well as on the location-changed event, and `pushState` fires neither, so
   * the synthetic event keeps analytics whole. The marker state stops our own
   * popstate listener from resolving the same URL a second time.
   */
  #dispatchIgnoredPopstate(): void {
    window.dispatchEvent(
      new PopStateEvent('popstate', { state: IGNORED_POPSTATE })
    );
  }

  #announce(location: RouterLocation): void {
    const detail = { router: this, location };
    window.dispatchEvent(new CustomEvent(LOCATION_CHANGED, { detail }));
    window.dispatchEvent(new CustomEvent(LEGACY_LOCATION_CHANGED, { detail }));
  }

  /** Start handling popstate, in-app clicks and `Router.go`. */
  subscribe(): void {
    if (this.#listening) return;
    this.#listening = true;
    activeRouters.add(this);
    window.addEventListener('popstate', this.#onPopstate);
    document.addEventListener('click', this.#onClick);
  }

  /** Detach every listener. Tests use it; the app never unmounts its router. */
  unsubscribe(): void {
    if (!this.#listening) return;
    this.#listening = false;
    activeRouters.delete(this);
    window.removeEventListener('popstate', this.#onPopstate);
    document.removeEventListener('click', this.#onClick);
  }

  #onPopstate = (event: PopStateEvent): void => {
    if (event.state === IGNORED_POPSTATE) return;
    const { pathname, search, hash } = window.location;
    void this.render({ pathname, search, hash }, { history: 'none' });
  };

  #onClick = (event: MouseEvent): void => {
    // Before the outlet and the route table exist there is nothing to render
    // into, so let the browser do its normal thing rather than swallow a click.
    if (!this.#outlet || !this.#flat.length) return;
    const anchor = routableAnchor(event);
    if (!anchor) return;
    const url = new URL(anchor.href, document.baseURI);
    event.preventDefault();
    void this.render(
      { pathname: url.pathname, search: url.search, hash: url.hash },
      { history: 'push' }
    );
    // A followed link starts a new page, so it starts at the top. Back and
    // forward keep their position, which is why this lives on click only.
    window.scrollTo(0, 0);
  };
}

export default Router;
