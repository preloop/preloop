import type {
  AccountGatewayUsageSummaryResponse,
  GatewayTokenUsage,
} from '../types';

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
    usage_by_agent: previous.usage_by_agent,
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
    (summary.usage_by_model?.length || 0) > 0 ||
    (summary.usage_by_agent?.length || 0) > 0
  );
}

/** One agent's usage in the summary period, as the Inventory shows it. */
export interface AgentPeriodUsage {
  requests: number;
  tokens: number;
  tokenUsage: GatewayTokenUsage | null;
  cost: number;
}

/**
 * Per-agent usage for the summary period, keyed by agent id.
 *
 * Read from the server's `usage_by_agent` breakdown, which applies the same
 * attribution as the agents list and the agent page (the agent's own
 * principal plus every session it owns, per-run sessions included). Summing
 * the session breakdown here instead disagreed with both pages: that list is
 * capped at the most recent sessions and the pages did not count a session's
 * rows written under a per-run principal.
 */
export function agentUsageById(
  summary: AccountGatewayUsageSummaryResponse | null | undefined
): Map<string, AgentPeriodUsage> {
  const usage = new Map<string, AgentPeriodUsage>();
  for (const row of summary?.usage_by_agent || []) {
    usage.set(row.agent_id, {
      requests: row.request_count || 0,
      tokens: row.token_usage?.total_tokens || 0,
      tokenUsage: row.token_usage ?? null,
      cost: row.estimated_cost || 0,
    });
  }
  return usage;
}
