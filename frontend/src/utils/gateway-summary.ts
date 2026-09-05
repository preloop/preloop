import type { AccountGatewayUsageSummaryResponse } from '../types';

/**
 * Reading one gateway summary two ways.
 *
 * The Overview asks for a cheap summary on every refresh and a full
 * breakdown (per model, flow, session and tool) only on the slower second
 * pass. These helpers keep the two from fighting: a cheap refresh must not
 * blank the Inventory tables that the breakdown filled.
 */

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
