import {
  getAccountAgents,
  getAttentionDismissals,
  DISMISSALS_UNSUPPORTED,
  type AttentionDismissal,
  getAccountGatewayUsageSearch,
  getAccountGatewayUsageSummary,
  getAccountRuntimeSessions,
  getBudgetPolicies,
  getFlowExecutions,
  getModelPriceOverrides,
  listApprovalRequests,
  type BudgetPolicy,
} from '../api';
import type {
  AccountGatewayUsageSummaryResponse,
  GatewayUsageSearchResultItem,
  ManagedAgentSummary,
  RuntimeSessionSummary,
} from '../types';
import type {
  AttentionApproval,
  AttentionFlowExecution,
  AttentionInputs,
  AttentionPriceOverride,
} from './attention';
import { parseUTCDate } from './date';

const DAY_MS = 24 * 60 * 60 * 1000;

/**
 * One place that decides which requests feed `deriveAttentionItems`.
 *
 * The Overview hero count and the Attention page used to fetch their own
 * inputs with different parameters (approvals filtered by expiry on one side,
 * `limit: 200` agents that the API rejects with a 422 on the other, ten mixed
 * flow executions versus the twenty-five most recent failures), so the same
 * account showed "3 need attention" next to a page listing nine items. Both
 * call this loader now, so a disagreement means a bug in the derivation, not
 * in the fetch parameters.
 *
 * The agents limit is 100 because that is the maximum the `/api/v1/agents`
 * endpoint accepts (`limit: int = Query(20, ge=1, le=100)`); anything larger
 * is a 422 that silently empties the Agents section.
 */
export const ATTENTION_QUERY = {
  approvalsLimit: 100,
  agentsLimit: 100,
  sessionsLimit: 100,
  sessionsWindowDays: 7,
  executionsLimit: 25,
  gatewayFailuresLimit: 12,
  usageWindowDays: 30,
} as const;

/**
 * The API still reports a request as `pending` after its expiry passes
 * (backend issue #335), and nobody can act on one of those. The Overview
 * filtered them out and the Attention page did not, which is how the same
 * account showed two different approval counts. The filter lives here so
 * there is one answer for both views; the backend should flip those rows to
 * `expired` separately.
 */
function isUnexpiredPendingApproval(
  approval: AttentionApproval,
  now: Date
): boolean {
  if (approval.status && approval.status !== 'pending') {
    return false;
  }
  if (!approval.expires_at) {
    return true;
  }
  return parseUTCDate(approval.expires_at).getTime() > now.getTime();
}

export interface LoadedAttentionInputs extends AttentionInputs {
  /**
   * False when the server has no dismissals endpoint (older deployment): the
   * page hides its dismiss controls instead of showing buttons that 404.
   */
  dismissalsSupported: boolean;
}

/**
 * Inputs the caller already has in hand.
 *
 * The Overview fetches approvals, agents, budget policies and a usage
 * breakdown for its own cards, and used to make the attention loader fetch
 * all four again a second later. Anything passed here is used as-is and
 * costs no request; anything left out is fetched exactly as before, so a
 * caller with nothing to share behaves identically.
 */
export interface PrefetchedAttentionInputs {
  approvals?: AttentionApproval[];
  agents?: ManagedAgentSummary[];
  sessions?: RuntimeSessionSummary[];
  executions?: AttentionFlowExecution[];
  budgetPolicies?: BudgetPolicy[];
  usageSummary?: AccountGatewayUsageSummaryResponse | null;
}

export interface LoadAttentionInputsOptions {
  /** Injected in tests and by callers that already know "now". */
  now?: Date;
  /** Skip the budget policies call when billing is off (it 403s). */
  includeBudgetPolicies?: boolean;
  /** Data the caller already fetched; each entry removes one request. */
  prefetched?: PrefetchedAttentionInputs;
}

/**
 * Fetches every input of the attention rules. Individual failures (a 403 for a
 * permission this operator lacks, a 422, a flaky endpoint) drop that one input
 * instead of failing the whole page, exactly like the page-level
 * `Promise.allSettled` it replaces.
 */
export async function loadAttentionInputs(
  options: LoadAttentionInputsOptions = {}
): Promise<LoadedAttentionInputs> {
  const now = options.now || new Date();
  const sessionsStart = new Date(
    now.getTime() - ATTENTION_QUERY.sessionsWindowDays * DAY_MS
  ).toISOString();
  const usageStart = new Date(
    now.getTime() - ATTENTION_QUERY.usageWindowDays * DAY_MS
  ).toISOString();
  const prefetched = options.prefetched || {};

  const [
    approvals,
    agents,
    sessions,
    executions,
    failures,
    policies,
    summary,
    dismissals,
    priceOverrides,
  ] = await Promise.allSettled([
    prefetched.approvals
      ? Promise.resolve(prefetched.approvals)
      : listApprovalRequests({
          status: 'pending',
          limit: ATTENTION_QUERY.approvalsLimit,
        }),
    prefetched.agents
      ? Promise.resolve({ items: prefetched.agents })
      : getAccountAgents({ status: 'all', limit: ATTENTION_QUERY.agentsLimit }),
    prefetched.sessions
      ? Promise.resolve({ items: prefetched.sessions })
      : getAccountRuntimeSessions({
          status: 'all',
          limit: ATTENTION_QUERY.sessionsLimit,
          startDate: sessionsStart,
        }),
    prefetched.executions
      ? Promise.resolve(prefetched.executions)
      : getFlowExecutions({
          status: 'FAILED',
          limit: ATTENTION_QUERY.executionsLimit,
        }),
    getAccountGatewayUsageSearch({
      limit: ATTENTION_QUERY.gatewayFailuresLimit,
    }),
    prefetched.budgetPolicies
      ? Promise.resolve(prefetched.budgetPolicies)
      : options.includeBudgetPolicies === false
        ? Promise.resolve([] as BudgetPolicy[])
        : getBudgetPolicies(),
    // The breakdown is what turns "336 requests unpriced" into "these seven
    // models have no price", which is the part somebody can act on. Always
    // a fixed ATTENTION_QUERY.usageWindowDays window so Overview and
    // /console/attention agree, unless the caller already holds a breakdown
    // over the same window and hands it in.
    prefetched.usageSummary
      ? Promise.resolve(prefetched.usageSummary)
      : getAccountGatewayUsageSummary({
          startDate: usageStart,
          includeBreakdown: true,
        }),
    getAttentionDismissals(),
    // An override is a price, including one of $0. Without this the console
    // asks for a price somebody set a month ago. A 403 on an account without
    // the feature drops the list, and the rules fall back to spend.
    getModelPriceOverrides({ activeOnly: true }),
  ]);

  const dismissalList =
    dismissals.status === 'fulfilled' &&
    dismissals.value !== DISMISSALS_UNSUPPORTED
      ? (dismissals.value as AttentionDismissal[])
      : [];

  return {
    approvals:
      approvals.status === 'fulfilled' && Array.isArray(approvals.value)
        ? (approvals.value as AttentionApproval[]).filter((approval) =>
            isUnexpiredPendingApproval(approval, now)
          )
        : [],
    agents:
      agents.status === 'fulfilled'
        ? ((agents.value.items || []) as ManagedAgentSummary[])
        : [],
    sessions:
      sessions.status === 'fulfilled'
        ? ((sessions.value.items || []) as RuntimeSessionSummary[])
        : [],
    executions:
      executions.status === 'fulfilled' && Array.isArray(executions.value)
        ? (executions.value as AttentionFlowExecution[])
        : [],
    gatewayFailures:
      failures.status === 'fulfilled'
        ? (
            (failures.value.items || []) as GatewayUsageSearchResultItem[]
          ).filter((item) => item.outcome !== 'success')
        : [],
    budgetPolicies:
      policies.status === 'fulfilled' && Array.isArray(policies.value)
        ? (policies.value as BudgetPolicy[])
        : [],
    usageSummary:
      summary.status === 'fulfilled' && summary.value
        ? (summary.value as AccountGatewayUsageSummaryResponse)
        : null,
    priceOverrides:
      priceOverrides.status === 'fulfilled' &&
      Array.isArray(priceOverrides.value)
        ? (priceOverrides.value as AttentionPriceOverride[])
        : [],
    dismissals: dismissalList,
    dismissalsSupported:
      dismissals.status === 'fulfilled' &&
      dismissals.value !== DISMISSALS_UNSUPPORTED,
  };
}
