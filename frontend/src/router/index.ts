/**
 * The console's router. See `./router.ts` for the implementation and for what
 * of `@vaadin/router`'s surface it deliberately keeps.
 */
export {
  Router,
  LOCATION_CHANGED,
  LEGACY_LOCATION_CHANGED,
  flattenRoutes,
  normalizePath,
} from './router';
export type {
  Route,
  RouteAction,
  RouterCommands,
  RouterLocation,
  RedirectResult,
  PreventResult,
  LoadingRenderer,
  LoadingSlot,
} from './router';

import { Router } from './router';

/** The one router the app runs on. `lit-app` gives it its outlet and routes. */
export const router = new Router();
