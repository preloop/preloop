import { sumTokenUsage } from '../components/token-figures';
import type {
  GatewayTokenUsage,
  GatewayUsageBySession,
  ManagedAgentSummary,
} from '../types';

/** The fields of a managed agent that tie a gateway session to it. */
export type UsageAgent = Pick<
  ManagedAgentSummary,
  'id' | 'session_source_id' | 'runtime_session_id' | 'owner_user_id'
>;

/** Tokens and estimated spend, summed over some sessions. */
export interface UsageTotals {
  tokens: number;
  tokenUsage: GatewayTokenUsage | null;
  cost: number;
}

/**
 * The managed agent a source id belongs to.
 *
 * An exact id wins. Per-run sessions append a suffix to the agent's base
 * source id ("custom_ABC:demo-123", "hermes-0161afa5e616-<run>"), so
 * otherwise the agent whose base id prefixes it (delimited by ':' or '-')
 * matches, the longest base first so a more specific agent wins.
 *
 * Args:
 *   sourceId: An agent id, runtime session id or session source id.
 *   agents: The account's managed agents.
 *
 * Returns:
 *   The matching agent, or undefined.
 */
export function agentForSourceId<T extends UsageAgent>(
  sourceId: string | null | undefined,
  agents: readonly T[]
): T | undefined {
  if (!sourceId) return undefined;
  const exact = agents.find(
    (agent) =>
      agent.id === sourceId ||
      agent.session_source_id === sourceId ||
      agent.runtime_session_id === sourceId
  );
  if (exact) return exact;
  return agents
    .filter(
      (agent) =>
        Boolean(agent.session_source_id) &&
        (sourceId.startsWith(`${agent.session_source_id}:`) ||
          sourceId.startsWith(`${agent.session_source_id}-`))
    )
    .sort(
      (a, b) =>
        (b.session_source_id?.length ?? 0) - (a.session_source_id?.length ?? 0)
    )[0];
}

/**
 * The managed agent behind one row of the gateway session breakdown, read
 * the way the Overview reads it: agent id, then runtime session, source and
 * principal.
 *
 * Args:
 *   session: One `usage_by_session` row.
 *   agents: The account's managed agents.
 *
 * Returns:
 *   The agent that spent it, or undefined when no agent did.
 */
export function agentForUsageSession<T extends UsageAgent>(
  session: GatewayUsageBySession,
  agents: readonly T[]
): T | undefined {
  const ids = [
    session.agent_id,
    session.runtime_session_id,
    session.session_source_id,
    session.runtime_principal_id,
  ].filter((value): value is string => Boolean(value));
  for (const id of ids) {
    const agent = agentForSourceId(id, agents);
    if (agent) return agent;
  }
  return undefined;
}

/**
 * Tokens and spend per managed agent, from the gateway session breakdown.
 *
 * Args:
 *   sessions: The `usage_by_session` rows for the window.
 *   agents: The account's managed agents.
 *
 * Returns:
 *   Totals keyed by agent id; agents with no traffic are absent.
 */
export function usageByAgent(
  sessions: readonly GatewayUsageBySession[],
  agents: readonly UsageAgent[]
): Map<string, UsageTotals> {
  const totals = new Map<string, UsageTotals>();
  for (const session of sessions) {
    const agent = agentForUsageSession(session, agents);
    if (!agent) continue;
    const running = totals.get(agent.id) || {
      tokens: 0,
      tokenUsage: null,
      cost: 0,
    };
    running.tokens += session.token_usage?.total_tokens || 0;
    running.tokenUsage = sumTokenUsage([
      running.tokenUsage,
      session.token_usage,
    ]);
    running.cost += session.estimated_cost || 0;
    totals.set(agent.id, running);
  }
  return totals;
}

/**
 * What one person's agents spent. Spend reaches a person only through the
 * agents they own, which is the one link the gateway records between a
 * request and a human, so someone who owns no agent spent nothing here.
 *
 * Args:
 *   userId: The account member.
 *   sessions: The `usage_by_session` rows for the window.
 *   agents: The account's managed agents.
 *
 * Returns:
 *   The owner's totals and how many agents they own.
 */
export function usageForOwner(
  userId: string,
  sessions: readonly GatewayUsageBySession[],
  agents: readonly UsageAgent[]
): UsageTotals & { agents: number } {
  const perAgent = usageByAgent(sessions, agents);
  const owned = agents.filter((agent) => agent.owner_user_id === userId);
  let tokens = 0;
  let cost = 0;
  let tokenUsage: GatewayTokenUsage | null = null;
  for (const agent of owned) {
    const usage = perAgent.get(agent.id);
    if (!usage) continue;
    tokens += usage.tokens;
    cost += usage.cost;
    tokenUsage = sumTokenUsage([tokenUsage, usage.tokenUsage]);
  }
  return { agents: owned.length, tokens, tokenUsage, cost };
}
