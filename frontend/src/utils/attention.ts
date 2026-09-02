import type { BudgetPolicy } from '../api';
import type {
  AccountGatewayUsageSummaryResponse,
  GatewayUsageSearchResultItem,
  ManagedAgentSummary,
  RuntimeSessionSummary,
} from '../types';
import {
  formatDurationBetween,
  formatRelativeTime,
  parseUTCDate,
} from './date';
import { sessionBelongsToAgent } from './agent-display';

/**
 * Everything the console considers "needs attention", derived from data the
 * Overview already fetches. Pure so both the Overview side card and the
 * Attention page can render the same list without a second source of truth.
 */
export type AttentionKind =
  'approval' | 'agent' | 'flow' | 'model' | 'budget' | 'pricing';

export type AttentionSeverity = 'critical' | 'warning';

export interface AttentionItemAction {
  label: string;
  href?: string;
  event?: 'configure-limits';
}

export interface AttentionItem {
  /** Stable across refetches: `${kind}:${entityId}`. */
  id: string;
  kind: AttentionKind;
  severity: AttentionSeverity;
  title: string;
  detail: string;
  href: string;
  /** ISO timestamp used for ordering and relative time; null when unknown. */
  at: string | null;
  action?: AttentionItemAction;
}

/**
 * Structural shapes rather than the full API types: the attention rules only
 * read a handful of fields, and tests stay readable when a fixture is five
 * lines instead of forty. The real API types are assignable to these.
 */
export interface AttentionApproval {
  id: string;
  tool_name?: string | null;
  summary?: string | null;
  status?: string;
  requested_at: string;
  expires_at?: string | null;
  managed_agent_name?: string | null;
  flow_name?: string | null;
  is_question?: boolean;
  question?: string | null;
}

export interface AttentionFlowExecution {
  id: string;
  flow_id?: string | null;
  flow_name?: string | null;
  status: string;
  /** The executions API returns `start_time`; `started_at` is accepted too. */
  start_time?: string | null;
  started_at?: string | null;
  end_time?: string | null;
}

export interface AttentionInputs {
  approvals?: AttentionApproval[];
  agents?: ManagedAgentSummary[];
  sessions?: RuntimeSessionSummary[];
  executions?: AttentionFlowExecution[];
  gatewayFailures?: GatewayUsageSearchResultItem[];
  budgetPolicies?: BudgetPolicy[];
  usageSummary?: AccountGatewayUsageSummaryResponse | null;
  now?: Date;
}

export const ATTENTION_KIND_META: Record<
  AttentionKind,
  { label: string; plural: string; icon: string; sectionHref: string }
> = {
  approval: {
    label: 'Approval',
    plural: 'Approvals',
    icon: 'shield-check',
    sectionHref: '/console/approvals',
  },
  agent: {
    label: 'Agent',
    plural: 'Agents',
    icon: 'robot',
    sectionHref: '/console/agents',
  },
  flow: {
    label: 'Flow',
    plural: 'Flows',
    icon: 'diagram-3',
    sectionHref: '/console/flows/executions',
  },
  model: {
    label: 'Model',
    plural: 'Models',
    icon: 'cpu',
    sectionHref: '/console/ai-models',
  },
  budget: {
    label: 'Budget',
    plural: 'Budgets',
    icon: 'wallet2',
    sectionHref: '/console/cost',
  },
  pricing: {
    label: 'Pricing',
    plural: 'Pricing',
    icon: 'tags',
    sectionHref: '/console/cost',
  },
};

/** Section order on the Attention page and in the grouped map. */
export const ATTENTION_KIND_ORDER: AttentionKind[] = [
  'approval',
  'agent',
  'flow',
  'model',
  'budget',
  'pricing',
];

const SEVEN_DAYS_MS = 7 * 24 * 60 * 60 * 1000;
const FOURTEEN_DAYS_MS = 14 * 24 * 60 * 60 * 1000;

function formatUsd(value: number | null | undefined): string {
  return `$${Number(value || 0).toFixed(2)}`;
}

function timestampOf(value: string | null | undefined): number {
  if (!value) return 0;
  const parsed = parseUTCDate(value).getTime();
  return Number.isNaN(parsed) ? 0 : parsed;
}

function joinReasons(reasons: string[]): string {
  return reasons.filter(Boolean).join(' · ');
}

function approvalItems(
  approvals: AttentionApproval[],
  now: Date
): AttentionItem[] {
  return approvals
    .filter((approval) => !approval.status || approval.status === 'pending')
    .map((approval) => {
      const subject =
        approval.summary || approval.tool_name || 'Approval request';
      const title = approval.is_question ? `Question: ${subject}` : subject;
      const requester = approval.managed_agent_name || approval.flow_name || '';
      // "pending 7w", never a bare date: an approval that has been waiting
      // seven weeks reads as urgent, "7/13/2026" reads as a log line.
      const elapsed = approval.requested_at
        ? formatRelativeTime(approval.requested_at, now, {
            maxRelativeDays: Infinity,
            withSuffix: false,
          })
        : '';
      const waiting = !elapsed
        ? ''
        : elapsed === 'just now'
          ? 'just requested'
          : `pending ${elapsed}`;
      const detail = joinReasons([requester, waiting]);
      return {
        id: `approval:${approval.id}`,
        kind: 'approval' as const,
        severity: 'critical' as const,
        title,
        detail,
        href: `/console/approval/${approval.id}`,
        at: approval.requested_at || null,
      };
    });
}

function latestSessionForAgent(
  agent: ManagedAgentSummary,
  sessions: RuntimeSessionSummary[]
): RuntimeSessionSummary | undefined {
  return sessions
    .filter((session) => sessionBelongsToAgent(session, agent))
    .sort(
      (left, right) =>
        timestampOf(right.last_activity_at || right.started_at) -
        timestampOf(left.last_activity_at || left.started_at)
    )[0];
}

function agentItems(
  agents: ManagedAgentSummary[],
  sessions: RuntimeSessionSummary[]
): AttentionItem[] {
  const items: AttentionItem[] = [];
  for (const agent of agents) {
    if (
      agent.lifecycle_state === 'suspended' ||
      agent.lifecycle_state === 'decommissioned'
    ) {
      continue;
    }
    const reasons: string[] = [];
    if (agent.live_validation_status === 'failed') {
      reasons.push('Live check failed');
    }
    // Only `incomplete` is a problem. `mcp_proxy_only` and `gateway_only` are
    // configurations someone chose, and an agent that has not run for weeks
    // is not a fault: neither ever reaches this list.
    if (agent.onboarding_state === 'incomplete') {
      reasons.push('Not connected');
    }
    const latestSession = latestSessionForAgent(agent, sessions);
    const failedRequests = latestSession?.failed_requests || 0;
    if (failedRequests > 0) {
      reasons.push(
        `${failedRequests} failed request${
          failedRequests === 1 ? '' : 's'
        } in last session`
      );
    }
    if (reasons.length === 0) {
      continue;
    }
    items.push({
      id: `agent:${agent.id}`,
      kind: 'agent',
      severity: 'warning',
      title: agent.display_name || agent.id,
      detail: joinReasons(reasons),
      href: `/console/agents/${agent.id}`,
      at:
        latestSession?.last_activity_at ||
        latestSession?.started_at ||
        agent.last_seen_at ||
        null,
    });
  }
  return items;
}

/**
 * One item per flow, not per run: a flow that fails on every trigger produced
 * seven identical rows and pushed everything else off the page. The count and
 * the age of the oldest failure carry the same information in one row.
 */
function flowItems(
  executions: AttentionFlowExecution[],
  now: Date
): AttentionItem[] {
  const cutoff = now.getTime() - SEVEN_DAYS_MS;
  const recentFailures = executions
    .filter((execution) => execution.status === 'FAILED')
    .filter((execution) => {
      const startedAt = execution.start_time || execution.started_at;
      return Boolean(startedAt) && timestampOf(startedAt) >= cutoff;
    });

  const groups = new Map<
    string,
    { flowId: string | null; name: string; runs: AttentionFlowExecution[] }
  >();
  for (const execution of recentFailures) {
    const name = execution.flow_name || 'Unnamed flow';
    const key = execution.flow_id || `name:${name}`;
    const group = groups.get(key) || {
      flowId: execution.flow_id || null,
      name,
      runs: [] as AttentionFlowExecution[],
    };
    group.runs.push(execution);
    groups.set(key, group);
  }

  return Array.from(groups.entries()).map(([key, group]) => {
    const runs = [...group.runs].sort(
      (left, right) =>
        timestampOf(right.start_time || right.started_at) -
        timestampOf(left.start_time || left.started_at)
    );
    const latest = runs[0];
    const oldest = runs[runs.length - 1];
    const latestStart = (latest.start_time || latest.started_at) as string;
    const oldestStart = (oldest.start_time || oldest.started_at) as string;
    const duration = formatDurationBetween(latestStart, latest.end_time, now);

    const span = formatRelativeTime(oldestStart, now, {
      maxRelativeDays: Infinity,
      withSuffix: false,
    });
    const detail =
      runs.length === 1
        ? joinReasons([
            'Failed',
            formatRelativeTime(latestStart, now),
            duration,
          ])
        : joinReasons([
            `${runs.length} failed runs in ${
              span === 'just now' ? '1m' : span
            }`,
            `latest ${formatRelativeTime(latestStart, now)}`,
            duration,
          ]);

    // A single failure opens the run itself; a group opens the failed runs of
    // that flow, which is what the count is about.
    const href =
      runs.length === 1 || !group.flowId
        ? `/console/flows/executions/${latest.id}`
        : `/console/flows/executions?flow_id=${encodeURIComponent(
            group.flowId
          )}&status=FAILED`;

    return {
      id: `flow:${key}`,
      kind: 'flow' as const,
      severity: 'warning' as const,
      title: group.name,
      detail,
      href,
      at: latestStart,
    };
  });
}

function modelItems(
  failures: GatewayUsageSearchResultItem[],
  now: Date
): AttentionItem[] {
  const groups = new Map<
    string,
    { count: number; lastAt: string | null; modelId: string | null }
  >();
  for (const failure of failures) {
    if (failure.outcome === 'success') {
      continue;
    }
    const key = failure.model_alias || failure.provider_name || 'Unknown model';
    const existing = groups.get(key) || {
      count: 0,
      lastAt: null as string | null,
      modelId: null as string | null,
    };
    existing.count += 1;
    if (timestampOf(failure.timestamp) > timestampOf(existing.lastAt)) {
      existing.lastAt = failure.timestamp;
    }
    const modelId = (failure as { ai_model_id?: string | null }).ai_model_id;
    if (!existing.modelId && modelId) {
      existing.modelId = modelId;
    }
    groups.set(key, existing);
  }

  return Array.from(groups.entries()).map(([key, group]) => ({
    id: `model:${key}`,
    kind: 'model' as const,
    severity: 'warning' as const,
    title: key,
    detail: joinReasons([
      `${group.count} failed request${group.count === 1 ? '' : 's'}`,
      group.lastAt ? `last ${formatRelativeTime(group.lastAt, now)}` : '',
    ]),
    href: group.modelId
      ? `/console/ai-models/${group.modelId}`
      : '/console/ai-models',
    at: group.lastAt,
  }));
}

function budgetScopeName(policy: BudgetPolicy): string {
  if (policy.subject_type === 'global' || policy.subject_type === 'account') {
    return 'Global';
  }
  if (policy.subject_type === 'ai_model') {
    return policy.model_alias || 'Model';
  }
  if (policy.subject_type === 'managed_agent') {
    return 'Agent';
  }
  return policy.subject_type.replace(/_/g, ' ');
}

function budgetItems(policies: BudgetPolicy[]): AttentionItem[] {
  const items: AttentionItem[] = [];
  for (const policy of policies) {
    const spend = policy.current_spend_usd;
    if (typeof spend !== 'number') {
      continue;
    }
    const hardLimit = policy.hard_limit_usd || 0;
    const softLimit = policy.soft_limit_usd || 0;
    let severity: AttentionSeverity | null = null;
    let detail = '';
    if (hardLimit > 0 && spend >= hardLimit) {
      severity = 'critical';
      detail = `Hard limit reached (${formatUsd(spend)} / ${formatUsd(
        hardLimit
      )})`;
    } else if (softLimit > 0 && spend >= softLimit) {
      severity = 'warning';
      detail = `Soft limit exceeded (${formatUsd(spend)} / ${formatUsd(
        softLimit
      )})`;
    }
    if (!severity) {
      continue;
    }
    items.push({
      id: `budget:${policy.id}`,
      kind: 'budget',
      severity,
      title: `${budgetScopeName(policy)} · ${policy.period}`,
      detail,
      href: '/console/attention#budgets',
      at: policy.period_end || null,
      action: { label: 'Configure limits', event: 'configure-limits' },
    });
  }
  return items;
}

function pricingItems(
  usageSummary: AccountGatewayUsageSummaryResponse | null | undefined,
  now: Date
): AttentionItem[] {
  if (!usageSummary) {
    return [];
  }
  const catalog = usageSummary.price_catalog;
  const reasons: string[] = [];
  const fetchedAt = catalog?.fetched_at || null;
  const modelCount = catalog?.model_count ?? null;
  const stale =
    Boolean(fetchedAt) &&
    now.getTime() - timestampOf(fetchedAt) > FOURTEEN_DAYS_MS;
  if (catalog && (stale || modelCount === 0)) {
    reasons.push(
      fetchedAt
        ? `Price catalog stale since ${formatRelativeTime(fetchedAt, now)}`
        : 'Price catalog has no models'
    );
  }
  const unpriced = usageSummary.unpriced_requests || 0;
  if (unpriced > 0) {
    reasons.push(`${unpriced} request${unpriced === 1 ? '' : 's'} unpriced`);
  }
  if (reasons.length === 0) {
    return [];
  }
  return [
    {
      id: 'pricing:catalog',
      kind: 'pricing',
      severity: 'warning',
      title: 'Price catalog',
      detail: joinReasons(reasons),
      href: '/console/cost',
      at: fetchedAt,
      action: { label: 'Update prices', href: '/console/cost' },
    },
  ];
}

/** Critical first, then most recent first; items without a timestamp last. */
export function sortAttentionItems(items: AttentionItem[]): AttentionItem[] {
  return [...items].sort((left, right) => {
    if (left.severity !== right.severity) {
      return left.severity === 'critical' ? -1 : 1;
    }
    const leftAt = left.at ? timestampOf(left.at) : null;
    const rightAt = right.at ? timestampOf(right.at) : null;
    if (leftAt === null && rightAt === null) return 0;
    if (leftAt === null) return 1;
    if (rightAt === null) return -1;
    return rightAt - leftAt;
  });
}

export function deriveAttentionItems(inputs: AttentionInputs): AttentionItem[] {
  const now = inputs.now || new Date();
  const items: AttentionItem[] = [
    ...approvalItems(inputs.approvals || [], now),
    ...agentItems(inputs.agents || [], inputs.sessions || []),
    ...flowItems(inputs.executions || [], now),
    ...modelItems(inputs.gatewayFailures || [], now),
    ...budgetItems(inputs.budgetPolicies || []),
    ...pricingItems(inputs.usageSummary, now),
  ];
  return sortAttentionItems(items);
}

/** Non-empty kinds only, in the canonical section order. */
export function groupAttentionItems(
  items: AttentionItem[]
): Map<AttentionKind, AttentionItem[]> {
  const grouped = new Map<AttentionKind, AttentionItem[]>();
  for (const kind of ATTENTION_KIND_ORDER) {
    const forKind = items.filter((item) => item.kind === kind);
    if (forKind.length > 0) {
      grouped.set(kind, forKind);
    }
  }
  return grouped;
}

/** "2 approvals", "1 agent" - the chip label used by the Attention page. */
export function attentionKindChipLabel(
  kind: AttentionKind,
  count: number
): string {
  const meta = ATTENTION_KIND_META[kind];
  const noun = count === 1 ? meta.label : meta.plural;
  return `${count} ${noun.toLowerCase()}`;
}
