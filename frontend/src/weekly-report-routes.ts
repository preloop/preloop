import type { Route, RouteAction } from './router';

const reportViews = new Set([
  'cost-view',
  'approvals-view',
  'attention-view',
  'governance-view',
]);

/** Mount report lists before ordinary page construction or data subscriptions. */
export function withWeeklyReportRoutes(routes: readonly Route[]): Route[] {
  return routes.map((route) => {
    const previousAction = route.action;
    const action: RouteAction = async function (
      this: Route,
      context,
      commands
    ) {
      const result = await previousAction?.call(this, context, commands);
      if (result != null) return result;
      if (new URLSearchParams(context.search).has('kind')) {
        await import('./components/weekly-report-list');
        return commands.component('weekly-report-list');
      }
      return result;
    };
    return {
      ...route,
      ...(route.children
        ? { children: withWeeklyReportRoutes(route.children) }
        : {}),
      ...(reportViews.has(route.component ?? '') ? { action } : {}),
    };
  });
}
