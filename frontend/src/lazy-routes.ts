import type { Route } from './router';

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
