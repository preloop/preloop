import type { BudgetPolicy } from '../api';
import type {
  AccountGatewayUsageSummaryResponse,
  GatewayUsageByModel,
  GatewayUsageSearchResultItem,
  ManagedAgentSummary,
  RuntimeSessionSummary,
} from '../types';
import {
  formatDurationBetween,
  formatRelativeTime,
  parseUTCDate,
} from './date';
import {
  failureCategoryBreakdown,
  failureCategoryLabel,
} from './failure-category';
import { sessionBelongsToAgent } from './agent-display';
import { isCliOnboardableAgentKind } from './agent-kinds';
import { shellQuote } from './shell';

/**
 * Everything the console considers "needs attention", derived from data the
 * Overview already fetches. Pure so both the Overview side card and the
 * Attention page can render the same list without a second source of truth.
 */
export type AttentionKind =
  'approval' | 'agent' | 'flow' | 'model' | 'budget' | 'pricing';

/**
 * `low` is for things that are worth naming once and are usually fine: a model
 * priced at $0 is either a promotion or a mistake, and only its owner knows
 * which. Low items sort last and stay off the Overview strip while anything
 * louder is open.
 */
export type AttentionSeverity = 'critical' | 'warning' | 'low';

export interface AttentionItemAction {
  label: string;
  href?: string;
  event?: 'configure-limits';
}

/** One failed run behind a flow item's count. */
export interface AttentionFailedRun {
  id: string;
  startedAt: string | null;
  durationText: string;
  errorMessage: string;
  /** What the run was about. Without it, a flow that failed six times shows
      six rows that differ only by a timestamp. */
  subject?: string | null;
  subjectUrl?: string | null;
  /**
   * Which layer broke, as the server categorised it (#361). Absent on servers
   * that do not derive it, in which case the evidence table drops the column
   * rather than showing a row of blanks.
   */
  failureCategory?: string | null;
}

/** One gateway failure behind a model item's count. */
export interface AttentionModelFailure {
  at: string | null;
  statusCode: number | null;
  excerpt: string;
  sessionId: string | null;
}

/** One model the price catalog cannot price. */
export interface AttentionUnpricedModel {
  alias: string;
  provider: string;
  requests: number;
  tokens: number;
  lastRequestAt: string | null;
  aiModelId: string | null;
}

/** One sentence about an agent, with the thing to do about it. */
export interface AttentionAgentReason {
  text: string;
  /** Shown as a copyable command line when present. */
  command?: string;
  action?: { label: string; href: string };
  /**
   * When set, the row offers a danger-outline Remove for this agent. Used for
   * kinds the CLI cannot onboard, where "remove it if it is gone" is the
   * instruction rather than a command to run.
   */
  removeAgent?: { id: string; name: string };
}

/** The numbers behind a budget item. */
export interface AttentionBudgetDetail {
  spendUsd: number;
  softLimitUsd: number;
  hardLimitUsd: number;
  period: string;
}

/**
 * What the row shows when it is expanded: which runs failed, which models are
 * unpriced, what the gateway said. A count with no "which" is what made the
 * inbox unactionable.
 */
export interface AttentionEvidence {
  failedRuns?: AttentionFailedRun[];
  /** "Failed to start agent Job: (409) Conflict" plus how many runs said it. */
  mostCommonError?: { message: string; count: number; total: number };
  flowId?: string | null;
  agentReasons?: AttentionAgentReason[];
  modelFailures?: AttentionModelFailure[];
  unpricedModels?: AttentionUnpricedModel[];
  /** Models that do have a price, and that price is zero. */
  zeroPricedModels?: AttentionUnpricedModel[];
  /** True for the "no provider price list is loaded at all" shape. */
  catalogMissing?: boolean;
  unpricedRequests?: number;
  budget?: AttentionBudgetDetail;
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
  /**
   * Why this item is showing, in a form a dismissal can be compared against.
   * A new failed run, a new unpriced model or a new validation result changes
   * the fingerprint, which brings a dismissed item back.
   */
  fingerprint: string;
  /** Approvals are never dismissable: somebody is waiting on an answer. */
  dismissable: boolean;
  /**
   * When set, the row offers this single button instead of the Dismiss menu.
   * Used where there is really only one answer ("Expected"), so acknowledging
   * costs one click instead of a menu.
   */
  quickDismiss?: { label: string; reason: 'expected' | 'snoozed' | 'fixed' };
  evidence?: AttentionEvidence;
}

/** The shape of a stored dismissal the rules need; the API type is assignable. */
export interface AttentionDismissalRecord {
  item_id: string;
  fingerprint: string;
  reason: string;
  snooze_until?: string | null;
  dismissed_by_username?: string | null;
  created_at?: string;
}

/** An item plus the dismissal that is currently hiding it. */
export interface DismissedAttentionItem {
  item: AttentionItem;
  dismissal: AttentionDismissalRecord;
}

export interface AttentionResult {
  /** What the strip, the hero count and the page all show. */
  items: AttentionItem[];
  /** Hidden by a dismissal that still matches; listed at the foot of the page. */
  dismissed: DismissedAttentionItem[];
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
  /** Already returned by the API; the console never showed it until wave 3. */
  error_message?: string | null;
  /** Also already returned; shown in the evidence table since wave 4. */
  trigger_subject?: string | null;
  trigger_subject_url?: string | null;
  /** One of the closed failure vocabulary (#361), on failed runs only. */
  failure_category?: string | null;
}

export interface AttentionInputs {
  approvals?: AttentionApproval[];
  agents?: ManagedAgentSummary[];
  sessions?: RuntimeSessionSummary[];
  executions?: AttentionFlowExecution[];
  gatewayFailures?: GatewayUsageSearchResultItem[];
  budgetPolicies?: BudgetPolicy[];
  usageSummary?: AccountGatewayUsageSummaryResponse | null;
  /**
   * Account price overrides. An override is a price, including one of $0,
   * so a model that has one is priced whatever its spend adds up to.
   */
  priceOverrides?: AttentionPriceOverride[];
  /**
   * Active dismissals. `undefined` (an older backend without the endpoint)
   * hides nothing and is not an error.
   */
  dismissals?: AttentionDismissalRecord[];
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

/** The Cost page reads `panel` and scrolls its pricing card into view. */
const PRICING_HREF = '/console/cost?panel=pricing';

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
        fingerprint: `requested:${approval.requested_at || ''}`,
        // Somebody is waiting for an answer: an approval can be decided, not
        // silenced.
        dismissable: false,
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
  sessions: RuntimeSessionSummary[],
  now: Date
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
    const evidence: AttentionAgentReason[] = [];
    const agentHref = `/console/agents/${agent.id}`;
    if (agent.live_validation_status === 'failed') {
      reasons.push('Live check failed');
      const checkedAt = agent.last_validated_at
        ? formatRelativeTime(agent.last_validated_at, now)
        : 'unknown';
      evidence.push({
        text: `Last check ${checkedAt}: the agent's credentials did not pass a live call through the gateway.`,
        action: { label: 'Open agent', href: agentHref },
      });
    }
    // Only `incomplete` is a problem. `mcp_proxy_only` and `gateway_only` are
    // configurations someone chose, and an agent that has not run for weeks
    // is not a fault: neither ever reaches this list.
    if (agent.onboarding_state === 'incomplete') {
      reasons.push('Not connected');
      // `agent_kind` is what the product is; older rows only carry the
      // transport they connected with, which is the same token for every kind
      // the CLI onboards.
      if (
        isCliOnboardableAgentKind(agent.agent_kind || agent.session_source_type)
      ) {
        evidence.push({
          text: 'Onboarding never completed. Onboard it on the machine it runs on, or remove it.',
          command: `preloop agents onboard ${shellQuote(
            agent.display_name || agent.id
          )}`,
          action: { label: 'Open agent', href: agentHref },
        });
      } else {
        // A custom or SDK-integrated agent is not something the CLI can find
        // on a machine, so the onboarding command would fail. What its owner
        // can do is start it, remove it, or say it is expected.
        evidence.push({
          text: 'Custom agents are started by you. Start it where it runs, remove it if it is gone, or dismiss this if it is expected.',
          action: { label: 'Open agent', href: agentHref },
          removeAgent: { id: agent.id, name: agent.display_name || agent.id },
        });
      }
    }
    const latestSession = latestSessionForAgent(agent, sessions);
    const failedRequests = latestSession?.failed_requests || 0;
    if (failedRequests > 0) {
      reasons.push(
        `${failedRequests} failed request${
          failedRequests === 1 ? '' : 's'
        } in last session`
      );
      const total = latestSession?.total_requests || failedRequests;
      const startedAt = latestSession?.started_at
        ? formatRelativeTime(latestSession.started_at, now)
        : 'recently';
      evidence.push({
        text: `${failedRequests} of ${total} requests failed in the session started ${startedAt}.`,
        action: latestSession
          ? {
              label: 'Open session',
              href: `/console/runtime-sessions?sessionId=${latestSession.id}`,
            }
          : undefined,
      });
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
      href: agentHref,
      at:
        latestSession?.last_activity_at ||
        latestSession?.started_at ||
        agent.last_seen_at ||
        null,
      // A new validation result, an onboarding that finally completed, or a
      // fresh batch of failed requests changes this and brings a dismissed
      // agent back.
      fingerprint: `${agent.onboarding_state}|${
        agent.live_validation_status
      }|${agent.last_validated_at || ''}|${
        latestSession?.id ?? 'none'
      }:${failedRequests}`,
      dismissable: true,
      evidence: { agentReasons: evidence },
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
    // What the count is made of: "5 failed: 3 model transient, 2 no
    // confirmation" is a different morning than "5 failed: 5 runner conflict".
    // Empty on servers that do not categorise, and the sentence falls back to
    // the count it had before.
    const breakdown = failureCategoryBreakdown(
      runs.map((run) => run.failure_category)
    );
    const singleCategory = failureCategoryLabel(latest.failure_category);
    const detail =
      runs.length === 1
        ? joinReasons([
            singleCategory ? `Failed: ${singleCategory}` : 'Failed',
            formatRelativeTime(latestStart, now),
            duration,
          ])
        : joinReasons([
            breakdown
              ? `${runs.length} failed: ${breakdown}`
              : `${runs.length} failed runs in ${
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

    const failedRuns: AttentionFailedRun[] = runs.map((run) => {
      const startedAt = run.start_time || run.started_at || null;
      return {
        id: run.id,
        startedAt,
        durationText: startedAt
          ? formatDurationBetween(startedAt, run.end_time, now)
          : '',
        errorMessage: (run.error_message || '').trim(),
        subject: run.trigger_subject || null,
        subjectUrl: run.trigger_subject_url || null,
        failureCategory: run.failure_category || null,
      };
    });

    return {
      id: `flow:${key}`,
      kind: 'flow' as const,
      severity: 'warning' as const,
      title: group.name,
      detail,
      href,
      at: latestStart,
      // The newest failed run: one more failure un-dismisses the flow.
      fingerprint: `run:${latest.id}`,
      dismissable: true,
      evidence: {
        failedRuns,
        mostCommonError: mostCommonError(failedRuns),
        flowId: group.flowId,
      },
    };
  });
}

/**
 * "Failed to start agent Job: (409) Conflict (5 of 5)": when every run died the
 * same way there is one thing to fix, and saying so saves reading five rows.
 */
function mostCommonError(
  runs: AttentionFailedRun[]
): { message: string; count: number; total: number } | undefined {
  const counts = new Map<string, number>();
  for (const run of runs) {
    const message = errorHeadline(run.errorMessage);
    if (!message) continue;
    counts.set(message, (counts.get(message) || 0) + 1);
  }
  if (counts.size === 0) {
    return undefined;
  }
  const [message, count] = Array.from(counts.entries()).sort(
    (left, right) => right[1] - left[1]
  )[0];
  return { message, count, total: runs.length };
}

/** Errors arrive with stack traces attached; the row shows the first line. */
export function firstLine(text: string | null | undefined): string {
  return (text || '').split('\n')[0].trim();
}

/**
 * Log plumbing at the head of a line: `timestamp=2026-09-03T19:32:45.726Z`
 * and `level=error`, quoted or bare. Runners hand the executor a logfmt line,
 * so the error a run recorded often starts with the two fields that say the
 * least: the timestamp is already in the Started column and the level is
 * always "error" on a failed run.
 */
const LOG_PREFIX_TOKEN =
  /^(?:timestamp|time|ts|level|lvl|severity)=(?:"[^"]*"|'[^']*'|\S*)\s*/i;

/** Strip the leading logfmt plumbing from one line. */
export function stripLogPrefix(text: string | null | undefined): string {
  let rest = (text || '').trim();
  let previous = '';
  while (rest && rest !== previous) {
    previous = rest;
    rest = rest.replace(LOG_PREFIX_TOKEN, '').trim();
  }
  return rest;
}

/**
 * The one line that says what went wrong: the first line of the error with
 * its log prefix removed. A line that is nothing but prefix ("timestamp=...
 * level=error") is skipped for the next line that carries words; if every
 * line is plumbing the raw first line is kept, because a console that hides
 * what it cannot parse stops being a record.
 */
export function errorHeadline(text: string | null | undefined): string {
  const lines = (text || '').split('\n');
  for (const line of lines) {
    const stripped = stripLogPrefix(line);
    if (stripped) {
      return stripped;
    }
  }
  return firstLine(text);
}

function modelItems(
  failures: GatewayUsageSearchResultItem[],
  now: Date
): AttentionItem[] {
  const groups = new Map<
    string,
    {
      count: number;
      lastAt: string | null;
      modelId: string | null;
      failures: GatewayUsageSearchResultItem[];
    }
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
      failures: [] as GatewayUsageSearchResultItem[],
    };
    existing.count += 1;
    existing.failures.push(failure);
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
    // The newest failure: another one after a dismissal shows the model again.
    fingerprint: `last:${group.lastAt || ''}`,
    dismissable: true,
    evidence: {
      modelFailures: [...group.failures]
        .sort(
          (left, right) =>
            timestampOf(right.timestamp) - timestampOf(left.timestamp)
        )
        .slice(0, 5)
        .map((failure) => ({
          at: failure.timestamp || null,
          statusCode: failure.status_code ?? null,
          excerpt: firstLine(failure.excerpt),
          sessionId: failure.runtime_session_id || null,
        })),
    },
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
      // The next period starts a new conversation about the same limit.
      fingerprint: `${policy.id}|${policy.period_start || ''}`,
      dismissable: true,
      evidence: {
        budget: {
          spendUsd: spend,
          softLimitUsd: softLimit,
          hardLimitUsd: hardLimit,
          period: policy.period,
        },
      },
    });
  }
  return items;
}

/**
 * The part of a `ModelPriceOverride` this module reads. Kept structural so
 * the rules stay a pure function of plain data.
 */
export interface AttentionPriceOverride {
  model_alias?: string | null;
  ai_model_id?: string | null;
  is_active?: boolean | null;
  effective_from?: string | null;
  effective_until?: string | null;
}

/** An alias matches whether or not it carries its provider prefix. */
function aliasKeys(alias: string | null | undefined): string[] {
  const value = (alias || '').trim().toLowerCase();
  if (!value) return [];
  const slash = value.lastIndexOf('/');
  return slash > 0 ? [value, value.slice(slash + 1)] : [value];
}

/**
 * Every alias and model id an in-force override prices. An override that has
 * been switched off, has not started, or has already ended prices nothing.
 */
export function pricedByOverrideKeys(
  overrides: AttentionPriceOverride[] | null | undefined,
  now: Date
): Set<string> {
  const keys = new Set<string>();
  for (const override of overrides || []) {
    if (override.is_active === false) continue;
    if (
      override.effective_from &&
      timestampOf(override.effective_from) > now.getTime()
    ) {
      continue;
    }
    if (
      override.effective_until &&
      timestampOf(override.effective_until) <= now.getTime()
    ) {
      continue;
    }
    for (const key of aliasKeys(override.model_alias)) {
      keys.add(key);
    }
    if (override.ai_model_id) {
      keys.add(override.ai_model_id.toLowerCase());
    }
  }
  return keys;
}

function hasPriceOverride(
  model: GatewayUsageByModel,
  keys: Set<string>
): boolean {
  if (keys.size === 0) return false;
  if (model.ai_model_id && keys.has(model.ai_model_id.toLowerCase())) {
    return true;
  }
  return aliasKeys(model.model_alias).some((key) => keys.has(key));
}

function unpricedModelOf(model: GatewayUsageByModel): AttentionUnpricedModel {
  return {
    alias: model.model_alias || model.provider_name || 'Unknown model',
    provider: model.provider_name || '',
    requests: model.request_count,
    tokens: model.token_usage?.total_tokens || 0,
    lastRequestAt: model.last_request_at || null,
    aiModelId: model.ai_model_id || null,
  };
}

/**
 * A model with no price at all: requests it served carry no cost, not a cost
 * of zero. The summary's aggregate `unpriced_requests` says how many; this
 * says which, which is the part an operator can act on.
 *
 * `unpriced_request_count` distinguishes "nobody knows what this costs" from
 * "this is free", which `estimated_cost === 0` cannot. Servers older than
 * wave 8 do not send it, so there the two collapse back into one warning
 * rather than disappearing, except where an account price override says
 * outright what the model costs: an override of $0 is an answer, and the
 * console asked for a price it had already been given.
 */
function unpricedModelsOf(
  usageSummary: AccountGatewayUsageSummaryResponse,
  overrideKeys: Set<string>
): AttentionUnpricedModel[] {
  return (usageSummary.usage_by_model || [])
    .filter((model) => {
      if (model.request_count <= 0) return false;
      if (model.unpriced_request_count === undefined) {
        if (hasPriceOverride(model, overrideKeys)) return false;
        return !model.estimated_cost;
      }
      return model.unpriced_request_count > 0;
    })
    .map(unpricedModelOf)
    .sort((left, right) => right.requests - left.requests);
}

/**
 * A model that is priced, at zero. Either somebody is on a promotion or an
 * override has a typo in it; the console cannot tell, so it asks once.
 */
function zeroPricedModelsOf(
  usageSummary: AccountGatewayUsageSummaryResponse
): AttentionUnpricedModel[] {
  return (usageSummary.usage_by_model || [])
    .filter((model) => (model.zero_priced_request_count || 0) > 0)
    .map(unpricedModelOf)
    .sort((left, right) => right.requests - left.requests);
}

/**
 * "3 models priced at $0" - low tone, one click to accept. Kept separate from
 * the unpriced item because the fix is different: an unpriced model needs a
 * price, a zero-priced model needs somebody to confirm the zero.
 */
function zeroPricedItem(
  usageSummary: AccountGatewayUsageSummaryResponse
): AttentionItem[] {
  const models = zeroPricedModelsOf(usageSummary);
  if (models.length === 0) {
    return [];
  }
  return [
    {
      id: 'pricing:zero-priced',
      kind: 'pricing',
      severity: 'low',
      title: `${models.length} model${
        models.length === 1 ? '' : 's'
      } priced at $0`,
      detail: 'Promo or mistake?',
      href: PRICING_HREF,
      at: models[0].lastRequestAt,
      action: { label: 'Review prices', href: PRICING_HREF },
      // A newly zero-priced model reopens the question; the same set does not.
      fingerprint: `zero:${models
        .map((model) => model.alias)
        .sort()
        .join(',')}`,
      dismissable: true,
      quickDismiss: { label: 'Expected', reason: 'expected' },
      evidence: { zeroPricedModels: models },
    },
  ];
}

function pricingItems(
  usageSummary: AccountGatewayUsageSummaryResponse | null | undefined,
  priceOverrides: AttentionPriceOverride[] | null | undefined,
  now: Date
): AttentionItem[] {
  if (!usageSummary) {
    return [];
  }
  const overrideKeys = pricedByOverrideKeys(priceOverrides, now);
  const zeroPriced = zeroPricedItem(usageSummary);
  const catalog = usageSummary.price_catalog;
  const fetchedAt = catalog?.fetched_at || null;
  const modelCount = catalog?.model_count ?? null;
  const unpricedRequests = usageSummary.unpriced_requests || 0;
  const unpricedModels = unpricedModelsOf(usageSummary, overrideKeys);
  const stale =
    Boolean(fetchedAt) &&
    now.getTime() - timestampOf(fetchedAt) > FOURTEEN_DAYS_MS;
  const catalogMissing = !catalog || modelCount === 0;

  const fingerprint = `${unpricedModels
    .map((model) => model.alias)
    .sort()
    .join(',')} catalog:${fetchedAt ?? 'none'}`;

  // Shape 1: nothing can be priced because no provider price list is loaded.
  if (catalogMissing && unpricedRequests > 0) {
    return [
      ...zeroPriced,
      {
        id: 'pricing:catalog',
        kind: 'pricing',
        severity: 'warning',
        title: 'No price catalog loaded',
        detail: `Estimated spend is missing for ${unpricedRequests} request${
          unpricedRequests === 1 ? '' : 's'
        } because no provider price list is loaded.`,
        href: PRICING_HREF,
        at: null,
        action: { label: 'Update prices', href: PRICING_HREF },
        fingerprint,
        dismissable: true,
        evidence: {
          catalogMissing: true,
          unpricedRequests,
          unpricedModels,
        },
      },
    ];
  }

  // Shape 2: a catalog is loaded but some models are not in it.
  if (unpricedModels.length > 0) {
    return [
      ...zeroPriced,
      {
        id: 'pricing:catalog',
        kind: 'pricing',
        severity: 'warning',
        title: `${unpricedModels.length} model${
          unpricedModels.length === 1 ? '' : 's'
        } without a price`,
        detail: joinReasons([
          `${unpricedRequests || 0} request${
            unpricedRequests === 1 ? '' : 's'
          } unpriced`,
          stale && fetchedAt
            ? `catalog last updated ${formatRelativeTime(fetchedAt, now)}`
            : '',
        ]),
        href: PRICING_HREF,
        at: fetchedAt,
        action: { label: 'Update prices', href: PRICING_HREF },
        fingerprint,
        dismissable: true,
        evidence: { unpricedModels, unpricedRequests },
      },
    ];
  }

  // Neither shape applies but the catalog itself has gone stale.
  if (catalog && stale && fetchedAt) {
    return [
      ...zeroPriced,
      {
        id: 'pricing:catalog',
        kind: 'pricing',
        severity: 'warning',
        title: 'Price catalog',
        detail: `Price catalog stale since ${formatRelativeTime(
          fetchedAt,
          now
        )}`,
        href: PRICING_HREF,
        at: fetchedAt,
        action: { label: 'Update prices', href: PRICING_HREF },
        fingerprint,
        dismissable: true,
        evidence: { unpricedRequests },
      },
    ];
  }
  return zeroPriced;
}

const SEVERITY_RANK: Record<AttentionSeverity, number> = {
  critical: 0,
  warning: 1,
  low: 2,
};

/**
 * Critical first, then warnings, then low; within a tone, most recent first,
 * items without a timestamp last.
 */
export function sortAttentionItems(items: AttentionItem[]): AttentionItem[] {
  return [...items].sort((left, right) => {
    if (left.severity !== right.severity) {
      return SEVERITY_RANK[left.severity] - SEVERITY_RANK[right.severity];
    }
    const leftAt = left.at ? timestampOf(left.at) : null;
    const rightAt = right.at ? timestampOf(right.at) : null;
    if (leftAt === null && rightAt === null) return 0;
    if (leftAt === null) return 1;
    if (rightAt === null) return -1;
    return rightAt - leftAt;
  });
}

/**
 * Is this dismissal still hiding this item? Same item, same fingerprint, and
 * (for a snooze) not yet expired. Anything else and the item comes back on its
 * own, which is the point: dismissing is "quiet until it changes", not "never
 * tell me again".
 */
function dismissalHides(
  dismissal: AttentionDismissalRecord,
  item: AttentionItem,
  now: Date
): boolean {
  if (dismissal.fingerprint !== item.fingerprint) {
    return false;
  }
  if (dismissal.snooze_until) {
    return timestampOf(dismissal.snooze_until) > now.getTime();
  }
  return true;
}

export function deriveAttentionItems(inputs: AttentionInputs): AttentionResult {
  const now = inputs.now || new Date();
  const derived: AttentionItem[] = [
    ...approvalItems(inputs.approvals || [], now),
    ...agentItems(inputs.agents || [], inputs.sessions || [], now),
    ...flowItems(inputs.executions || [], now),
    ...modelItems(inputs.gatewayFailures || [], now),
    ...budgetItems(inputs.budgetPolicies || []),
    ...pricingItems(inputs.usageSummary, inputs.priceOverrides, now),
  ];

  const byItemId = new Map<string, AttentionDismissalRecord>();
  for (const dismissal of inputs.dismissals || []) {
    byItemId.set(dismissal.item_id, dismissal);
  }

  const items: AttentionItem[] = [];
  const dismissed: DismissedAttentionItem[] = [];
  for (const item of derived) {
    const dismissal = byItemId.get(item.id);
    if (item.dismissable && dismissal && dismissalHides(dismissal, item, now)) {
      dismissed.push({ item, dismissal });
      continue;
    }
    items.push(item);
  }

  return {
    items: sortAttentionItems(items),
    dismissed: dismissed.sort(
      (left, right) =>
        timestampOf(right.dismissal.created_at) -
        timestampOf(left.dismissal.created_at)
    ),
  };
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

/**
 * Where the Overview strip chips point: the Attention page scrolls to this row
 * and tints it briefly. Ids contain `:` and `/` (model aliases), so the id is
 * encoded.
 */
export function attentionItemAnchor(itemId: string): string {
  return `/console/attention#item-${encodeURIComponent(itemId)}`;
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
