import { flattenRoutes, normalizePath } from './router';
import type { Route, Router } from './router';
import type { Capability, CapabilitySet } from './capabilities';

type ComponentLoaders = Readonly<Record<string, () => Promise<unknown>>>;

/**
 * Give every route whose component ships in its own chunk the loader that
 * fetches it.
 *
 * The router awaits `Route.load` after the route's own `action` and before it
 * creates the element, so guards, redirects and the OAuth fragment handling
 * still run first and a view the visitor never reaches is never downloaded.
 * A route with no loader is left exactly as it was declared.
 */
export function withLazyRoutes(
  routes: readonly Route[],
  loaders: ComponentLoaders
): Route[] {
  return routes.map((route) => {
    const load = route.component ? loaders[route.component] : undefined;
    return {
      ...route,
      ...(Array.isArray(route.children)
        ? { children: withLazyRoutes(route.children, loaders) }
        : {}),
      ...(load ? { load } : {}),
    };
  });
}

/** A console child route that exists only while its capability is reported. */
export interface CapabilityRoute {
  capability: Capability;
  path: string;
  component: string;
}

/**
 * Console routes behind a capability, relative to `/console`. Nothing here is
 * registered, and no module behind it is fetched, unless `/features` reports
 * the capability.
 */
export const CAPABILITY_ROUTES: readonly CapabilityRoute[] = [
  {
    capability: 'chat_connections',
    path: 'settings/chat',
    component: 'chat-connections-view',
  },
  {
    capability: 'account_hierarchy',
    path: 'settings/subaccounts',
    component: 'subaccounts-view',
  },
  {
    capability: 'account_hierarchy',
    path: 'settings/access-grants',
    component: 'access-grants-view',
  },
  {
    capability: 'account_hierarchy',
    path: 'shared/:kind/:resourceId',
    component: 'shared-resource-view',
  },
];

export const capabilityRouteLoaders: ComponentLoaders = {
  'chat-connections-view': () =>
    import('./views/authed/settings/chat-connections-view'),
  'subaccounts-view': () => import('./views/authed/hierarchy/subaccounts-view'),
  'access-grants-view': () =>
    import('./views/authed/hierarchy/access-grants-view'),
  'shared-resource-view': () =>
    import('./views/authed/hierarchy/shared-resource-view'),
};

/** The lazy child routes the given capabilities turn on. */
export function capabilityRoutesFor(
  capabilities: CapabilitySet,
  table: readonly CapabilityRoute[] = CAPABILITY_ROUTES,
  loaders: ComponentLoaders = capabilityRouteLoaders
): Route[] {
  return withLazyRoutes(
    table
      .filter((route) => capabilities.has(route.capability))
      .map(({ path, component }) => ({ path, component })),
    loaders
  );
}

/** Whether a path could be one of the gated console routes. */
export function isCapabilityPath(
  pathname: string,
  table: readonly CapabilityRoute[] = CAPABILITY_ROUTES
): boolean {
  const compiled = flattenRoutes([
    {
      path: '/console',
      children: table.map(({ path, component }) => ({ path, component })),
    },
  ]);
  const normalized = normalizePath(pathname);
  return compiled.some(
    (entry) => entry.chain.length > 1 && entry.pattern.test(normalized)
  );
}

/**
 * Adds the capability routes to the console route of an installed table.
 *
 * Routes are only ever added: a capability that goes away leaves its route
 * behind, and the view answers the 404 of its endpoint by hiding. Each
 * route is added at most once.
 */
export class CapabilityRouteGate {
  #added = new Set<string>();

  constructor(
    private readonly router: Router,
    private readonly consoleRoute: Route,
    private readonly load: () => Promise<CapabilitySet>,
    private readonly table: readonly CapabilityRoute[] = CAPABILITY_ROUTES,
    private readonly loaders: ComponentLoaders = capabilityRouteLoaders
  ) {}

  /** Read the capabilities and register whatever they turn on. */
  async sync(options: { render?: boolean } = {}): Promise<Route[]> {
    const capabilities = await this.load();
    const fresh = capabilityRoutesFor(
      capabilities,
      this.table,
      this.loaders
    ).filter((route) => !this.#added.has(route.path));
    if (fresh.length === 0) return [];
    for (const route of fresh) this.#added.add(route.path);
    this.consoleRoute.children = [
      ...fresh,
      ...(this.consoleRoute.children ?? []),
    ];
    await this.router.refreshRoutes({ render: options.render ?? true });
    return fresh;
  }
}
