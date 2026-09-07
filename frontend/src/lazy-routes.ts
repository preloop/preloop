import type { Route } from './router';

type ComponentLoaders = Readonly<Record<string, () => Promise<unknown>>>;

/** Load route modules before the router creates their custom elements. */
export function withLazyRoutes(
  routes: readonly Route[],
  loaders: ComponentLoaders
): Route[] {
  return routes.map((route) => {
    const load = route.component ? loaders[route.component] : undefined;
    const action = route.action;
    return {
      ...route,
      ...(Array.isArray(route.children)
        ? { children: withLazyRoutes(route.children, loaders) }
        : {}),
      ...(load
        ? {
            async action(context, commands) {
              // Preserve guards, redirects and OAuth fragment handling before
              // waiting for the module. Undefined lets the router create the
              // component and continue resolving any nested route.
              const result = await action?.call(this, context, commands);
              if (result !== undefined) return result;
              await load();
            },
          }
        : {}),
    };
  });
}
