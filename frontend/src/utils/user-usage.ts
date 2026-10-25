import { sumTokenUsage } from '../components/token-figures';
import type {
  GatewayTokenUsage,
  GatewayUsageByAgent,
  ManagedAgentSummary,
  SubscriptionUsageSummary,
} from '../types';
import {
  combineSubscriptionUsage,
  subscriptionEstimate,
  type SubscriptionEstimate,
} from './subscription-estimate';

/** The fields of a managed agent that tie usage to its owner. */
export type UsageAgent = Pick<ManagedAgentSummary, 'id' | 'owner_user_id'>;

/** Tokens and estimated spend, summed over some agents. */
export interface UsageTotals {
  tokens: number;
  tokenUsage: GatewayTokenUsage | null;
  cost: number;
  /** The agents' subscription traffic as one block (D-05); null: none. */
  subscription: SubscriptionUsageSummary | null;
  /** Spend plus the API-equivalent cost of that traffic, an estimate. */
  withoutSubscriptions: SubscriptionEstimate;
}

/**
 * What one person's agents spent. Spend reaches a person only through the
 * agents they own, which is the one link the gateway records between a
 * request and a human, so someone who owns no agent spent nothing here.
 *
 * The per-agent totals are the summary's `usage_by_agent` section: the same
 * attribution as the agents list, the agent page and the Overview Users tab,
 * over the whole window. Summing the session breakdown instead read a list
 * capped at the most recent sessions (B-02).
 *
 * Args:
 *   userId: The account member.
 *   usage: The `usage_by_agent` rows for the window.
 *   agents: The account's managed agents.
 *
 * Returns:
 *   The owner's totals and how many agents they own.
 */
export function usageForOwner(
  userId: string,
  usage: readonly GatewayUsageByAgent[],
  agents: readonly UsageAgent[]
): UsageTotals & { agents: number } {
  const perAgent = new Map(usage.map((row) => [row.agent_id, row]));
  const owned = agents.filter((agent) => agent.owner_user_id === userId);
  let tokens = 0;
  let cost = 0;
  let tokenUsage: GatewayTokenUsage | null = null;
  const blocks: (SubscriptionUsageSummary | null | undefined)[] = [];
  for (const agent of owned) {
    const row = perAgent.get(agent.id);
    if (!row) continue;
    tokens += row.token_usage?.total_tokens || 0;
    cost += row.estimated_cost || 0;
    tokenUsage = sumTokenUsage([tokenUsage, row.token_usage]);
    blocks.push(row.subscription_usage);
  }
  const subscription = combineSubscriptionUsage(blocks);
  return {
    agents: owned.length,
    tokens,
    tokenUsage,
    cost,
    subscription,
    withoutSubscriptions: subscriptionEstimate(cost, subscription),
  };
}
