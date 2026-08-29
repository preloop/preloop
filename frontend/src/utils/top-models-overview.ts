import type { AccountGatewayUsageSummaryResponse } from '../types';

/** Collapsed rows shown per model (agents, flows, ungrouped sessions). */
export const TOP_MODEL_GROUP_PREVIEW_LIMIT = 4;

/** Nested sessions shown inside one expanded agent/flow group. */
export const TOP_MODEL_SESSION_PREVIEW_LIMIT = 4;

export type TopModelsSortMetric = 'spend' | 'usage';

export type UsageSortable = {
  estimated_cost?: number;
  request_count?: number;
  total_requests?: number;
};

/**
 * Keep per-model/session breakdowns when a lightweight summary refresh
 * would otherwise replace them with empty arrays (include_breakdown=false).
 */
export function mergeGatewaySummaryPreservingBreakdown(
  previous: AccountGatewayUsageSummaryResponse | null,
  incoming: AccountGatewayUsageSummaryResponse | null
): AccountGatewayUsageSummaryResponse | null {
  if (!incoming) {
    return previous;
  }
  if (!previous) {
    return incoming;
  }
  if (hasUsageBreakdown(incoming)) {
    return incoming;
  }
  if (!hasUsageBreakdown(previous)) {
    return incoming;
  }
  return {
    ...incoming,
    usage_by_model: previous.usage_by_model,
    usage_by_flow: previous.usage_by_flow,
    usage_by_session: previous.usage_by_session,
    usage_by_tool: previous.usage_by_tool,
    requests_by_day: previous.requests_by_day,
  };
}

export function hasUsageBreakdown(
  summary: AccountGatewayUsageSummaryResponse | null | undefined
): boolean {
  if (!summary) {
    return false;
  }
  return (
    (summary.usage_by_session?.length || 0) > 0 ||
    (summary.usage_by_model?.length || 0) > 0
  );
}

export function usageSortValue(
  item: UsageSortable,
  metric: TopModelsSortMetric
): number {
  if (metric === 'usage') {
    return item.total_requests ?? item.request_count ?? 0;
  }
  return item.estimated_cost ?? 0;
}

export function compareByUsageMetric(
  a: UsageSortable,
  b: UsageSortable,
  metric: TopModelsSortMetric
): number {
  return usageSortValue(b, metric) - usageSortValue(a, metric);
}

export function previewWindow<T>(
  items: T[],
  limit: number,
  expanded: boolean
): { visible: T[]; overflow: number } {
  const overflow = Math.max(0, items.length - limit);
  if (expanded || overflow === 0) {
    return { visible: items, overflow };
  }
  return { visible: items.slice(0, limit), overflow };
}
