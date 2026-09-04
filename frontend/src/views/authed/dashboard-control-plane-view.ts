import { css, html, nothing, unsafeCSS } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import { repeat } from 'lit/directives/repeat.js';

import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/card/card.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/progress-bar/progress-bar.js';
import '../../components/mcp-setup-dialog.ts';
import '../../components/budget-limits-dialog.ts';
import '../../components/activity-feed.ts';
import '../../components/inventory-card.ts';
import '../../components/usage-card.ts';
import '../../components/view-header.ts';
import '../../components/relative-time-label.ts';
import {
  AuthedElement,
  fetchWithAuth,
  getAIModels,
  getAIModelsOverview,
  getAccountAgents,
  getAccountGatewayUsageSearch,
  getAccountGatewayUsageSummary,
  getAccountRateLimitReport,
  getAccountRuntimeSessions,
  getApiKeys,
  getBudgetPolicies,
  BudgetPolicy,
  getFlowExecutions,
  getFlows,
  getIssueCount,
  getMCPServers,
  getTools,
  getTrackers,
  getUsers,
  getFeatures,
  getUserProfile,
  hasPermission,
  type UserPermissions,
} from '../../api';
import '../../components/preloop-invite-dialog';
import '../../components/preloop-flow-form';
import '../../components/add-ai-model-modal';
import '../../components/preloop-deploy-wizard';
import { isSaaS } from '../../brand-config';
import { normalizeObservedSession } from '../../utils/session-observer';
import { getAgentStatusChip } from '../../utils/agent-display';
import {
  attentionItemAnchor,
  ATTENTION_KIND_META,
  deriveAttentionItems,
  type AttentionApproval,
  type AttentionInputs,
  type AttentionItem,
} from '../../utils/attention';
import {
  loadAttentionInputs,
  type PrefetchedAttentionInputs,
} from '../../utils/attention-data';
import { unifiedWebSocketManager } from '../../services/unified-websocket-manager';
import type {
  AccountGatewayUsageSummaryResponse,
  AccountRateLimitReportResponse,
  AIModelOverviewItem,
  GatewayUsageBySession,
  GatewayUsageByTool,
  GatewayUsageSearchResultItem,
  ManagedAgentSummary,
  RuntimeSessionSummary,
  AIModel,
} from '../../types';
import { parseUTCDate } from '../../utils/date';
import { formatRelativeTime } from '../../components/relative-time-label';
import { executionSubjectCss } from '../../utils/execution-subject';
import {
  hasUsageBreakdown,
  mergeGatewaySummaryPreservingBreakdown,
} from '../../utils/gateway-summary';
import {
  pickDefaultModel,
  selectableModels,
} from '../../utils/ai-model-selection';
import type { Tool } from '../../components/tool-card';
import type {
  InventoryAgentRow,
  InventoryFlowRow,
  InventoryModelRow,
  InventoryToolRow,
  InventoryUserRow,
} from '../../components/inventory-card';
import consoleStyles from '../../styles/console-styles.css?inline';
import { reducedMotionStyles } from '../../styles/reduced-motion';

/**
 * A user as the admin list returns them: the shared `User` type predates
 * roles being carried on the list, and the Inventory only reads four fields.
 */
interface AccountUser {
  id: string;
  username?: string | null;
  email?: string | null;
  full_name?: string | null;
  is_active?: boolean;
  last_login?: string | null;
  roles?: Array<{ name?: string | null }> | null;
  inherited_roles?: Array<{ name?: string | null }> | null;
}

interface AuditEvent {
  id: string;
  action: string;
  status: string;
  timestamp: string;
  details: Record<string, unknown> | null;
}

interface AuditGroup {
  correlation_id: string | null;
  primary_event: AuditEvent;
  sub_events: AuditEvent[];
  outcome: string;
}

interface GroupedAuditResponse {
  groups: AuditGroup[];
  total: number;
}

interface Tracker {
  id: string;
  name: string;
  type: string;
}

interface MCPServer {
  id: string;
  name: string;
  url: string;
  status: string;
}

/**
 * The server's maximum page for `/flows/executions` (the endpoint clamps
 * `limit` to 100 and takes no start date). The Inventory Flows tab derives its
 * run and failure counts from this one page, so when the page comes back full
 * and its oldest row is still inside the range there may be in-range runs the
 * page never saw — which the card says in the cell title rather than printing
 * a capped number as if it were the range.
 */
const FLOW_EXECUTIONS_PAGE_SIZE = 100;

/**
 * Sessions above the fold answer one question - has anything ever run here -
 * so the first wave asks for a short page and the attention loader asks for
 * the window it needs later.
 */
const FOLD_SESSIONS_LIMIT = 20;

/** The page of gateway calls the failures card reads on a live refresh. */
const GATEWAY_FAILURES_REFRESH_LIMIT = 25;

/** How long a burst of events on one topic is collected before it is served. */
const REALTIME_DEBOUNCE_MS = 250;

/**
 * The floor between two refreshes of the same topic. `gateway_activity` is
 * published once per model call, so without a floor a single busy agent is a
 * refresh loop.
 */
const REALTIME_TOPIC_INTERVAL_MS = 10000;

/** How often the expensive background reads run while the tab is visible. */
const BACKGROUND_REFRESH_MS = 60000;

/**
 * How much of each long list the sessionStorage cache keeps. The cards read
 * the first rows only; keeping every row is what pushed the cache over the
 * per origin quota on busy accounts.
 */
const CACHED_SESSIONS = 20;
const CACHED_INTERACTIONS = 25;
const CACHED_FLOW_EXECUTIONS = 25;

/**
 * Timestamps for the Overview load, read by the performance spec. Free when
 * nothing is measuring, and never a reason for a failure here.
 */
export function markOverviewTiming(name: string): void {
  try {
    performance.mark(name);
  } catch {
    // performance.mark is unavailable or the buffer is full; timings are
    // diagnostics only.
  }
}

interface FlowExecution {
  id: string;
  flow_id: string;
  flow_name?: string;
  status: string;
  start_time: string;
  end_time: string | null;
  error_message: string | null;
  /** What the run was about, derived from the trigger when it was created.
      Five runs of one flow are otherwise indistinguishable on this card. */
  trigger_subject?: string | null;
  trigger_subject_url?: string | null;
  trigger_event_details?: Record<string, unknown> | null;
  /** Which layer broke a failed run (#361); absent on older servers. */
  failure_category?: string | null;
}

interface ApprovalRequest {
  id: string;
  tool_name: string;
  status: string;
  requested_at: string;
  resolved_at?: string | null;
  expires_at?: string | null;
  summary?: string | null;
  managed_agent_name?: string | null;
  flow_name?: string | null;
  is_question?: boolean;
}

interface UsageSessionSubject {
  kind: 'agent' | 'flow' | 'session';
  name: string;
  href: string;
}

interface TopModelSubjectGroup {
  groupId: string;
  kind: 'agent' | 'flow' | 'other';
  subject: UsageSessionSubject;
  sessions: GatewayUsageBySession[];
  totalCost: number;
  totalRequests: number;
}

export const NEXT_STEPS_DISMISSED_KEY = 'dashboard_next_steps_dismissed';

/**
 * What the last completed load concluded about the checklist. The card is
 * derived from four separate fetches; before they land, every step looks
 * undone, which is how a finished account saw "Next steps" flash back on
 * every refresh. Remembering the answer means the page can stay quiet until
 * it knows better.
 */
export const NEXT_STEPS_DONE_KEY = 'dashboard_next_steps_all_done';

/** The wire formats the model gateway speaks, as URL prefixes. */
type GatewayFormat = '/openai/v1' | '/anthropic/v1' | '/google/v1';

interface NextStep {
  /** Nice to have: never keeps the card alive on its own. */
  optional?: boolean;
  id: string;
  label: string;
  done: boolean;
  href?: string;
  onClick?: () => void;
}

interface DashboardMetric {
  label: string;
  value: string | number;
  icon: string;
  href?: string;
  tone?: 'primary' | 'neutral' | 'success' | 'warning' | 'danger';
}

@customElement('dashboard-view')
export class DashboardView extends AuthedElement {
  @state() private loading = true;
  @state() private fetchingGatewaySummary = true;
  /**
   * A range change refetches the summary with the old one still on screen.
   * The card dims what it has rather than blanking while the new range
   * arrives, so this is deliberately not `fetchingGatewaySummary`.
   */
  @state() private updatingUsage = false;
  @state() private fetchingRecentExecutions = true;
  /**
   * Flows, their runs, the resolved approvals and the gateway call log: the
   * cards below the fold. They are fetched after the first paint, so the
   * Inventory tabs that read them say "still coming" while the tabs that are
   * already filled show their rows.
   */
  @state() private fetchingInventory = true;
  @state() private fetchingApprovals = true;
  @state() private fetchingAudit = true;
  @state() private fetchingMCPAndTools = true;
  /**
   * The users list is its own request now: it used to wait behind MCP
   * servers, tools, models and API keys in one `Promise.all`, so the Users
   * tab stayed a skeleton until the slowest of the five answered even though
   * the names and roles had arrived first.
   */
  @state() private fetchingUsers = true;
  /**
   * The second, expensive gateway summary (`include_breakdown=true`) that
   * fills the usage columns of the Agents and Users tabs. Separate from
   * `fetchingGatewaySummary`, which is the cheap totals call.
   */
  @state() private fetchingUsageBreakdown = true;
  @state() private error: string | null = null;
  @state() private gatewaySummary: AccountGatewayUsageSummaryResponse | null =
    null;
  /** The window before `gatewayTimeRange`, for the Usage card's delta. */
  @state()
  private priorGatewaySummary: AccountGatewayUsageSummaryResponse | null = null;
  @state() private runtimeSessions: RuntimeSessionSummary[] = [];
  @state() private managedAgents: ManagedAgentSummary[] = [];
  @state() private budgetAgents: ManagedAgentSummary[] = [];
  @state() private gatewayInteractions: GatewayUsageSearchResultItem[] = [];
  @state() private auditGroups: AuditGroup[] = [];
  @state() private trackers: Tracker[] = [];
  @state() private totalIssues = 0;
  @state() private mcpServers: MCPServer[] = [];
  @state() private tools: Tool[] = [];
  @state() private flowExecutions: FlowExecution[] = [];
  /** Flow ids and names, so the Inventory lists flows that never ran. */
  @state() private flows: Array<{ id: string; name: string }> = [];
  @state() private flowExecutionsCount = 0;
  @state() private failedExecutionsCount = 0;
  @state() private succeededFlowExecutionsCount = 0;
  @state() private pendingApprovals: ApprovalRequest[] = [];
  @state() private lastUpdatedAt: string | null = null;
  @state() private hasFlows = false;
  @state() private hasAIModels = false;
  @state() private aiModelsCount = 0;
  @state() private enabledUsersCount = 0;
  /** Active users, kept for the Inventory's Users tab (Cloud/Enterprise). */
  @state() private accountUsers: AccountUser[] = [];
  /**
   * This account's permissions, or null when RBAC is off (OSS, DISABLE_RBAC).
   *
   * `undefined` means the profile has not answered yet; both that and null
   * read as unrestricted, the same way the shell treats them, so a slow or
   * failed profile call never hides something the operator can use.
   */
  @state() private permissions: UserPermissions = undefined;
  @state() private toolCallsCount = 0;
  @state() private failedToolCallsCount = 0;
  @state() private totalFlowsCount = 0;
  @state() private totalAgentsCount = 0;
  @state() private totalRuntimeSessionsCount = 0;
  @state() private gatewayTimeRange: 'day' | 'week' | 'month' | 'year' =
    'month';
  @state() private fetchingBudget = false;
  @state() private fetchingAgents = false;
  @state() private showSetupDialog = false;
  @state() private showBudgetDialog = false;
  @state() private welcomeCardDismissed = false;
  @state() private nextStepsDismissed = false;
  @state() private userManagementEnabled = false;
  /** True once the feature flags have answered, either way. */
  @state() private featuresResolved = false;
  /** Rate-limited requests for the gateway row; null when the call failed. */
  @state() private rateLimitReport: AccountRateLimitReportResponse | null =
    null;
  /** Active API keys for the card header; null when we could not count them. */
  @state() private apiKeysCount: number | null = null;
  /** Which wire format the model gateway row is showing a URL for. */
  @state() private gatewayFormat: GatewayFormat = '/openai/v1';

  @state() private aiModels: AIModel[] = [];
  /**
   * Per-model usage for the Inventory Models tab, from the batch overview.
   *
   * One request for every model on the tab. Joining on the model id also
   * removes the alias guessing the tab used to do against the account usage
   * breakdown, which mislabelled any model renamed at the gateway.
   */
  @state() private aiModelOverview: AIModelOverviewItem[] = [];
  @state() private isInviteDialogOpen = false;
  @state() private computeFeatureEnabled = false;
  @state() private isEnterprise = false;
  @state() private isAdmin = false;

  @state() private budgetPolicies: BudgetPolicy[] = [];
  /**
   * Inputs for the hero "need attention" count and the side card, fetched
   * through the shared loader rather than reusing the cards' own data: the
   * cards are scoped to what they display (five recent executions, approvals
   * minus the expired ones), and reusing them made the Overview disagree with
   * /console/attention.
   */
  @state() private attentionInputs: AttentionInputs | null = null;
  @state()
  private approvalStats = {
    total: 0,
    approved: 0,
    declined: 0,
    expired: 0,
    avgApprovalTime: 0,
  };

  private unsubscribeRealtime?: () => void;
  private refreshInFlight = false;
  /** When the last full load started, which is what the event gate reads. */
  private lastFetchStartedAt = 0;
  /** One pending timer per topic key, so topics never queue behind each other. */
  private refreshTimers: Record<string, number> = {};
  /** When each topic key last ran, for its 10s floor. */
  private lastTopicRefresh: Record<string, number> = {};
  private backgroundTimer: number | null = null;
  /** The budget fetch, for the parts of the page that do need to wait on it. */
  private budgetReady: Promise<void> = Promise.resolve();
  /** Set while a coalesced sessionStorage write is pending. */
  private cacheWriteScheduled = false;
  /** So a full cache is reported once per session, not once per write. */
  private cacheQuotaWarned = false;
  /** Memoised attention derivation; see `attentionItems`. */
  private attentionMemo: {
    inputs: AttentionInputs | null;
    items: AttentionItem[];
  } | null = null;

  private formatDate(dateStr: string | null | undefined): string {
    if (!dateStr) return '';
    try {
      const date = parseUTCDate(dateStr);
      return new Intl.DateTimeFormat(undefined, {
        month: 'short',
        day: 'numeric',
        hour: 'numeric',
        minute: '2-digit',
      }).format(date);
    } catch {
      return dateStr;
    }
  }

  static styles = [
    reducedMotionStyles,
    css`
      /* One row per plane: name, endpoint, what it did. */
      .plane-row {
        align-items: center;
        column-gap: var(--sl-spacing-small);
        display: grid;
        grid-template-columns: minmax(120px, auto) minmax(0, 1fr) auto;
        padding: var(--sl-spacing-x-small) 0;
      }
      .plane-row + .plane-row {
        border-top: 1px solid var(--console-hairline);
      }
      .plane-name-cell {
        align-items: center;
        display: flex;
        gap: var(--sl-spacing-x-small);
      }
      .plane-name {
        color: var(--sl-color-neutral-900);
        font-size: var(--console-text-body);
      }
      /* Neutral until the plane has served something. Never red: a failure is
         a number on this row, not a broken gateway. */
      .plane-dot {
        background: var(--sl-color-neutral-300);
        border-radius: 50%;
        flex-shrink: 0;
        height: 8px;
        width: 8px;
      }
      .plane-dot.served {
        background: var(--sl-color-success-600);
      }
      .plane-endpoint {
        align-items: center;
        display: flex;
        gap: var(--sl-spacing-2x-small);
        min-width: 0;
      }
      .plane-endpoint .server-endpoint {
        display: flex;
        font-family: var(--sl-font-mono);
        font-size: var(--console-text-meta);
        min-width: 0;
        color: var(--sl-color-neutral-700);
      }
      /* The host gives way, the path stays: a middle ellipsis in two spans. */
      .endpoint-head {
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .endpoint-tail {
        flex-shrink: 0;
        white-space: nowrap;
      }
      /* Every pixel the chrome gives back is a pixel of hostname. */
      .plane-endpoint sl-icon-button::part(base) {
        padding: 2px;
      }
      .plane-endpoint .format-select {
        background: transparent;
        border: 1px solid var(--sl-color-neutral-300);
        border-radius: var(--sl-border-radius-small);
        color: var(--sl-color-primary-600);
        cursor: pointer;
        font-family: inherit;
        font-size: var(--console-text-meta);
        outline: none;
        padding: 1px 4px;
      }
      .plane-docs {
        color: var(--console-meta-color);
        display: flex;
      }
      .plane-stats {
        color: var(--sl-color-neutral-600);
        font-size: var(--console-text-meta);
        font-variant-numeric: tabular-nums;
        text-align: right;
        white-space: nowrap;
      }
      .plane-quiet {
        color: var(--console-meta-color);
      }
      .gateway-header-meta {
        align-items: center;
        color: var(--console-meta-color);
        display: flex;
        font-size: var(--console-text-meta);
        gap: var(--sl-spacing-x-small);
      }
      .gateway-header-meta > span + span::before {
        content: '· ';
      }
      .connect-first {
        align-items: center;
        display: flex;
        flex-wrap: wrap;
        font-size: var(--console-text-body);
        gap: var(--sl-spacing-small);
        padding: var(--sl-spacing-x-small) 0;
      }
      /* The one exception to "no filled box inside a card": a command you
         are meant to select and copy is a block of input, and it takes the
         page colour so it reads as recessed rather than raised. */
      .connect-command {
        align-items: center;
        background: var(--console-page);
        border: none;
        border-radius: var(--sl-border-radius-medium);
        display: flex;
        gap: var(--sl-spacing-2x-small);
        padding: 0 var(--sl-spacing-2x-small) 0 var(--sl-spacing-x-small);
      }
      .connect-command code {
        font-family: var(--sl-font-mono);
        font-size: 12px;
      }
      /* One amber line above the page, or nothing. It stays one line: the
         chips truncate before the strip is allowed to wrap, so "View all"
         never falls to a second row. */
      .attention-strip {
        align-items: center;
        /* A translucent mix of one warning token over the card surface reads
           as a tinted band in both themes, instead of an orange block. */
        background: color-mix(
          in srgb,
          var(--sl-color-warning-500) 10%,
          var(--console-surface)
        );
        border: 1px solid
          color-mix(in srgb, var(--sl-color-warning-500) 35%, transparent);
        border-radius: var(--sl-border-radius-medium);
        display: flex;
        flex-wrap: nowrap;
        gap: var(--sl-spacing-small);
        padding: var(--sl-spacing-x-small) var(--sl-spacing-medium);
      }
      .attention-strip-icon {
        color: var(--sl-color-warning-600);
        flex-shrink: 0;
        font-size: 18px;
      }
      .attention-strip-count {
        color: var(--sl-color-warning-800);
        font-weight: 600;
        font-variant-numeric: tabular-nums;
        white-space: nowrap;
      }
      .attention-strip-items {
        display: flex;
        flex: 0 1 auto;
        flex-wrap: nowrap;
        gap: var(--sl-spacing-x-small);
        min-width: 0;
        overflow: hidden;
      }
      .attention-chip-link {
        display: flex;
        max-width: 26ch;
        min-width: 0;
        text-decoration: none;
      }
      .attention-chip-link sl-badge {
        max-width: 100%;
        min-width: 0;
      }
      /* Quiet amber: the strip behind them is already the alarm, so the
         chips read as labels rather than as five more warnings. Soft chip
         recipe, one tone at 16% with no border. */
      .attention-chip-link sl-badge::part(base) {
        align-items: center;
        background-color: color-mix(
          in srgb,
          var(--sl-color-warning-500) 16%,
          transparent
        );
        border-width: 0;
        color: var(--sl-color-warning-800);
        display: flex;
        gap: var(--sl-spacing-3x-small);
        max-width: 100%;
        min-width: 0;
      }
      .attention-chip-link:hover sl-badge::part(base) {
        background-color: color-mix(
          in srgb,
          var(--sl-color-warning-500) 26%,
          transparent
        );
      }
      /* min-width lets the label shrink inside the badge, so a long one ends
         in an ellipsis instead of being cut mid-word by the strip. */
      .attention-chip-text {
        min-width: 0;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .attention-strip-all {
        color: var(--sl-color-primary-700);
        font-size: var(--console-text-meta);
        margin-left: auto;
        text-decoration: none;
        white-space: nowrap;
      }
      .attention-strip-all:hover {
        text-decoration: underline;
      }
      /* Nothing is wrong, one thing is worth a look: same line, no amber. */
      .attention-strip.low-only {
        background: var(--console-surface);
        border-color: var(--sl-color-neutral-200);
      }
      .attention-strip.low-only .attention-strip-icon {
        color: var(--sl-color-neutral-500);
      }
      .attention-strip.low-only .attention-strip-count {
        color: var(--sl-color-neutral-700);
      }
      /* Next steps: a checklist, not a wizard. */
      .next-steps-list {
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-2x-small);
      }
      .next-step-link {
        align-items: center;
        background: none;
        border: none;
        color: var(--sl-color-neutral-900);
        cursor: pointer;
        display: flex;
        font: inherit;
        gap: var(--sl-spacing-x-small);
        padding: var(--sl-spacing-2x-small) 0;
        text-align: left;
        text-decoration: none;
        width: 100%;
      }
      .next-step-link:hover .next-step-label {
        text-decoration: underline;
      }
      .next-step-mark {
        color: var(--sl-color-neutral-400);
        flex-shrink: 0;
        font-size: 16px;
      }
      .next-step-mark.done {
        color: var(--sl-color-success-600);
      }
      .next-step.done .next-step-label {
        color: var(--console-meta-color);
        text-decoration: line-through;
      }
      .updated-at {
        color: var(--console-meta-color);
        font-size: var(--sl-font-size-small);
        font-weight: var(--sl-font-weight-normal);
      }
      .capsule-eyebrow {
        color: var(--console-meta-color);
        font-size: var(--sl-font-size-x-small);
        font-weight: 600;
        letter-spacing: 0.04em;
        text-transform: uppercase;
      }
      .budget-track {
        position: relative;
        height: 6px;
        border-radius: 999px;
        background: var(--sl-color-neutral-200);
        overflow: hidden;
      }
      .budget-track-fill {
        position: absolute;
        top: 0;
        bottom: 0;
        left: var(--budget-fill-left, 0%);
        width: var(--budget-fill-width, 0%);
        background: var(--sl-color-success-600);
      }
      .budget-track-fill.success {
        background: var(--sl-color-success-600);
      }
      .budget-track-fill.warning {
        background: var(--sl-color-warning-600);
      }
      .budget-track-fill.danger {
        background: var(--sl-color-danger-600);
      }
      .budget-soft-marker {
        position: absolute;
        top: 0;
        bottom: 0;
        left: var(--budget-soft-position, 0%);
        width: 2px;
        background: var(--sl-color-warning-600);
        box-shadow: 0 0 0 1px var(--console-surface);
      }
      .budget-hard-marker {
        position: absolute;
        top: 0;
        right: 0;
        bottom: 0;
        width: 2px;
        background: var(--sl-color-danger-600);
      }
      /* Compliance-specific styles */
      .compliance-progress {
        margin-top: var(--sl-spacing-medium);
      }
      .compliance-stats {
        display: flex;
        justify-content: space-between;
        margin-bottom: var(--sl-spacing-small);
        font-size: var(--sl-font-size-small);
      }
      /* MCP Server Capsule */
      .mcp-server-capsule {
        display: flex;
        align-items: center;
        gap: var(--sl-spacing-medium);
        padding: var(--sl-spacing-small) var(--sl-spacing-medium);
        background: var(--sl-color-neutral-50);
        border: 1px solid var(--sl-color-neutral-200);
        border-radius: 100px;
        margin-top: var(--sl-spacing-2x-large);
        margin-bottom: var(--sl-spacing-large);
        margin-left: auto;
        margin-right: auto;
        max-width: 600px;
        transition: all 0.2s ease;
      }
      .mcp-server-capsule:hover {
        background: var(--sl-color-neutral-100);
        border-color: var(--sl-color-neutral-300);
      }
      .status-indicator {
        width: 8px;
        height: 8px;
        border-radius: 50%;
        background: var(--sl-color-success-600);
        box-shadow: 0 0 0 2px var(--sl-color-success-100);
        flex-shrink: 0;
      }
      .server-details {
        display: flex;
        align-items: center;
        gap: var(--sl-spacing-small);
        flex: 1;
        min-width: 0;
      }
      .server-endpoint {
        min-width: 0;
        font-family: monospace;
        font-size: var(--sl-font-size-small);
        color: var(--sl-color-neutral-900);
        font-weight: 500;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }
      .server-auth {
        font-size: var(--sl-font-size-x-small);
        color: var(--sl-color-neutral-600);
        padding: 0.125rem 0.5rem;
        background: var(--sl-color-neutral-0);
        border-radius: 12px;
        white-space: nowrap;
        flex-shrink: 0;
      }
      .capsule-link {
        color: var(--sl-color-primary-600);
        text-decoration: none;
        font-size: var(--sl-font-size-small);
        font-weight: 500;
        white-space: nowrap;
        flex-shrink: 0;
        float: right;
      }
      .capsule-link:hover {
        text-decoration: underline;
      }
      /* Phone width: the capsule is a two-row block, not a pill. Row one
         names the endpoint, row two is the thing you came to copy. Without
         this the select and the URL pushed the copy button outside the card. */
      @media (max-width: 640px) {
        .card-header-with-action {
          align-items: flex-start;
          flex-direction: column;
          gap: var(--sl-spacing-2x-small);
        }
        .mcp-server-capsule {
          border-radius: var(--sl-border-radius-medium);
          column-gap: var(--sl-spacing-x-small);
          flex-wrap: wrap;
          row-gap: var(--sl-spacing-x-small);
        }
        .mcp-server-capsule > * {
          min-width: 0;
        }
        /* Leaves room on the same row for the copy button that follows. */
        .server-details {
          flex: 1 1 calc(100% - 3rem);
        }
        .server-details select {
          max-width: 8rem;
        }
      }
      @media (min-width: 1024px) {
        .overview-layout {
          grid-template-columns: 1fr;
        }
      }
      /* Welcome card styles */
      .getting-started-steps {
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-medium);
        margin-top: var(--sl-spacing-large);
      }
      .step-item {
        display: flex;
        align-items: flex-start;
        gap: var(--sl-spacing-medium);
        padding: var(--sl-spacing-medium);
        background: var(--sl-color-neutral-0);
        border-radius: var(--sl-border-radius-medium);
      }
      .step-item.completed {
        background: var(--sl-color-success-50);
        border-color: var(--sl-color-success-200);
      }
      .step-icon {
        flex-shrink: 0;
      }
      .step-icon sl-icon {
        font-size: 1.5rem;
      }
      .step-content {
        flex: 1;
      }
      .step-title {
        font-weight: 600;
        margin-bottom: var(--sl-spacing-2x-small);
        color: var(--sl-color-neutral-900);
      }
      .step-description {
        font-size: var(--sl-font-size-small);
        color: var(--sl-color-neutral-600);
        margin-bottom: var(--sl-spacing-small);
      }
      .step-action {
        display: inline-block;
        margin-top: var(--sl-spacing-x-small);
      }
      .progress-overview {
        display: flex;
        align-items: center;
        gap: var(--sl-spacing-small);
        margin-top: var(--sl-spacing-large);
      }
      .progress-overview sl-progress-bar {
        flex: 1;
      }
      .progress-overview sl-progress-bar::part(base) {
        border: 1px solid rgba(230, 130, 50, 0.35);
      }
      .progress-overview sl-progress-bar::part(indicator) {
        background: var(--gradient-brand);
        position: relative;
        overflow: hidden;
      }
      .progress-overview sl-progress-bar::part(indicator)::after {
        content: '';
        position: absolute;
        top: 0;
        left: -100%;
        width: 100%;
        height: 100%;
        background: linear-gradient(
          90deg,
          transparent,
          rgba(255, 200, 100, 0.15),
          transparent
        );
        animation: shimmer 2.5s infinite;
      }
      @keyframes shimmer {
        0% {
          left: -100%;
        }
        100% {
          left: 100%;
        }
      }
      .progress-text {
        font-size: var(--sl-font-size-small);
        font-weight: 500;
      }
    `,

    unsafeCSS(consoleStyles),
    unsafeCSS(executionSubjectCss),
    css`
      :host {
        display: block;
      }

      .deploy-grid {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: var(--sl-spacing-large);
        margin-bottom: var(--sl-spacing-large);
      }
      .inner-deploy-grid {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: var(--sl-spacing-medium);
        margin-top: var(--sl-spacing-small);
      }
      @media (max-width: 768px) {
        .deploy-grid,
        .inner-deploy-grid {
          grid-template-columns: 1fr;
        }
      }

      /* Overview only: the cards are dense, so a large gap between them read
         as "unrelated sections" and cost a scroll. */
      .dashboard-stack {
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-medium);
      }

      .main-column,
      .side-column {
        gap: var(--sl-spacing-medium);
      }

      /* The side column is a rail, not a stack that grows the page.
         The .main-content element is the scroll port (console-shell), so a
         sticky child of it sticks at the top of what the operator can see,
         and a column that is exactly viewport-tall subtracts the header.

         align-self: stretch makes the rail as tall as the row it shares
         with the main column, so on a short page it stops at the main
         column's foot instead of pushing the page down; the max-height
         then caps it at one viewport. Usage keeps its natural height and
         the feed takes what is left (flex: 1 1 0, so its own rows never
         vote on how tall the column wants to be) and scrolls internally. */
      @media (min-width: 1200px) {
        .column-layout.dashboard > .side-column {
          position: sticky;
          top: var(--sl-spacing-medium);
          align-self: stretch;
          max-height: calc(
            100dvh - var(--console-header-height) - var(--sl-spacing-medium) * 2
          );
        }

        /* The floor is what makes the rail a list rather than a peephole:
           at 240px the card showed three lines and an expanded row had to be
           scrolled to be read. It is stated against the viewport as well as
           in pixels so a short laptop window shrinks the feed instead of
           pushing the Usage card off the rail. */
        .column-layout.dashboard > .side-column > activity-feed {
          flex: 1 1 0;
          min-height: min(360px, 34dvh);
          /* The rail is bounded and stretched above, so here (and only
             here) the column decides the feed's height and the card's own
             360px stop would only make the list shorter than the space it
             has been given. */
          --activity-feed-list-max-height: none;
        }
      }

      .summary-grid,
      .control-plane-grid,
      .analytics-grid {
        display: grid;
        gap: var(--sl-spacing-medium);
      }

      .summary-grid {
        grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      }

      .control-plane-grid {
        grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
      }

      .analytics-grid {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }

      .summary-card::part(base),
      .content-card::part(base) {
        height: 100%;
      }

      .welcome-card {
        grid-column: 1 / -1;
        margin-top: var(--sl-spacing-small);
        margin-bottom: var(--sl-spacing-medium);
      }
      .welcome-card::part(base) {
        height: 100%;
        border: none;
        box-shadow: 0 10px 32px rgba(19, 27, 46, 0.04);
      }

      .metric-label,
      .analytics-label,
      .analytics-subtext,
      .step-description,
      .row-meta,
      .summary-item span:last-child,
      .capsule-hint {
        color: var(--sl-color-neutral-600);
        font-size: var(--sl-font-size-small);
      }

      .metric-value,
      .analytics-value {
        color: var(--sl-color-neutral-900);
        font-weight: 700;
        line-height: 1.1;
      }

      .metric-value {
        font-size: 1.7rem;
        margin-top: var(--sl-spacing-2x-small);
      }

      .metric-subtext {
        color: var(--sl-color-neutral-600);
        font-size: var(--sl-font-size-small);
        margin-top: var(--sl-spacing-small);
      }

      .analytics-value {
        font-size: 1.35rem;
      }

      .card-header,
      .card-header-with-action,
      .row,
      .row-main,
      .row-meta,
      .welcome-header,
      .step-item,
      .budget-stat,
      .summary-item,
      .mcp-actions {
        display: flex;
      }

      .card-header,
      .card-header-with-action {
        align-items: center;
        justify-content: space-between;
        gap: var(--sl-spacing-small);
      }

      .card-title,
      .welcome-title,
      .step-title,
      .row-primary,
      .capsule-hint strong {
        color: var(--sl-color-neutral-900);
        font-weight: 700;
      }

      .welcome-header {
        justify-content: space-between;
        align-items: center;
        gap: var(--sl-spacing-medium);
      }

      .welcome-title {
        display: flex;
        align-items: center;
        gap: var(--sl-spacing-small);
        font-size: 1.15rem;
      }

      .welcome-content {
        margin-top: var(--sl-spacing-medium);
        color: var(--sl-color-neutral-700);
      }

      .step-item {
        gap: var(--sl-spacing-medium);
        align-items: flex-start;
      }

      .step-content {
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-x-small);
        flex: 1;
      }

      .step-actions {
        display: flex;
        gap: var(--sl-spacing-small);
        flex-wrap: wrap;
        margin-top: var(--sl-spacing-x-small);
      }

      .list,
      .budget-meter,
      .summary-list,
      .mcp-summary {
        display: flex;
        flex-direction: column;
      }

      .list,
      .budget-meter,
      .mcp-summary {
        gap: var(--sl-spacing-small);
      }

      .row {
        flex-direction: column;
        gap: var(--sl-spacing-2x-small);
        padding: var(--sl-spacing-small) 0;
        border-top: 1px solid var(--sl-color-neutral-200);
      }

      .row:first-child {
        border-top: none;
        padding-top: 0;
      }

      .row-main,
      .row-meta,
      .budget-stat,
      .summary-item {
        align-items: center;
        justify-content: space-between;
        gap: var(--sl-spacing-small);
      }

      .row-primary {
        overflow-wrap: anywhere;
      }

      .summary-item strong {
        color: var(--sl-color-neutral-900);
        font-weight: 600;
        text-align: right;
      }

      .row-link,
      .header-link,
      .capsule-link {
        color: var(--sl-color-primary-700);
        text-decoration: none;
      }

      .row-link:hover,
      .header-link:hover,
      .capsule-link:hover {
        text-decoration: underline;
      }

      .summary-list {
        list-style: none;
        padding: 0;
        margin: 0;
        gap: var(--sl-spacing-small);
      }

      .summary-item {
        padding: var(--sl-spacing-2x-small) 0;
        border-top: 1px solid var(--sl-color-neutral-200);
      }

      .summary-item:first-child {
        border-top: none;
        padding-top: 0;
      }

      .server-details {
        min-width: 0;
      }

      .server-endpoint {
        display: block;
        color: var(--sl-color-neutral-900);
        font-family: var(--sl-font-mono);
        font-size: var(--sl-font-size-small);
        overflow-wrap: anywhere;
      }

      .mcp-actions {
        gap: var(--sl-spacing-small);
        align-items: center;
      }

      /* One centered line in a 72px box. An empty card used to take as much
         vertical space as a full one, so a quiet account looked like a broken
         one. */
      @media (max-width: 1200px) {
        .column-layout.dashboard {
          grid-template-columns: 1fr;
        }
      }
      @media (max-width: 800px) {
        .card-header,
        .card-header-with-action,
        .row-main,
        .row-meta,
        .summary-item,
        .step-item,
        .welcome-header {
          align-items: flex-start;
          flex-direction: column;
        }

        .summary-item strong {
          text-align: left;
        }

        .analytics-grid {
          grid-template-columns: 1fr;
        }

        /* Phone: the strip is allowed the second row it needs, and the chips
           stop competing for one line of 390px. */
        .attention-strip {
          flex-wrap: wrap;
        }

        .attention-strip-items {
          flex-wrap: wrap;
          overflow: visible;
        }

        .attention-chip-link {
          max-width: 100%;
        }

        .attention-strip-all {
          margin-left: 0;
        }

        /* Phone: name and numbers on one line, endpoint and copy under it,
           so a 390px row never squeezes the URL into three characters. */
        .plane-row {
          grid-template-columns: auto minmax(0, 1fr);
          row-gap: var(--sl-spacing-2x-small);
        }

        .plane-name-cell {
          grid-column: 1;
          grid-row: 1;
        }

        /* "Model gateway" broken over two lines beside its numbers reads as
           two rows; the numbers wrap instead. */
        .plane-name {
          white-space: nowrap;
        }

        .plane-stats {
          grid-column: 2;
          grid-row: 1;
          white-space: normal;
        }

        .plane-endpoint {
          grid-column: 1 / -1;
          grid-row: 2;
        }

        /* A title and a dismiss button are a row at any width; stacking them
           put the x on a line of its own. */
        .next-steps-card .card-header-with-action {
          align-items: center;
          flex-direction: row;
          justify-content: space-between;
        }
      }
    `,
  ];

  connectedCallback() {
    super.connectedCallback();
    this.loadDismissedState();
    this.loadCachedDashboardData();
    void this.fetchDashboardData();
    this.connectRealtime();
  }

  private async fetchAdminStatus() {
    try {
      const user = await getUserProfile();
      this.isAdmin = user?.is_superuser || false;
      this.permissions = user?.permissions ?? null;
    } catch (error) {
      console.error('Failed to fetch user profile:', error);
      this.isAdmin = false;
      this.permissions = null;
    }
  }

  /**
   * Whether this account has teammates to show *and* this operator may see
   * them.
   *
   * `GET /api/v1/users` requires `view_users`, which the system viewer role
   * does not carry. Gating the tab on the licence flag alone put a Users tab
   * in front of a viewer that answered "No teammates yet." after a swallowed
   * 403 - a sentence about the account when the truth was about the reader.
   * Without the permission there is no tab, which is what the sidebar already
   * does with /console/settings/users.
   */
  private get canViewUsers(): boolean {
    return (
      this.userManagementEnabled &&
      hasPermission(this.permissions, 'view_users')
    );
  }

  /**
   * Whether this page will ask for the people list itself. The activity feed
   * asks for the same list to put names on rows, so when the answer is yes it
   * waits for this one instead of making a second identical request.
   */
  private get fetchesUsers(): boolean {
    return (
      hasPermission(this.permissions, 'view_users') &&
      (isSaaS() || this.userManagementEnabled)
    );
  }

  private async fetchFeatures() {
    try {
      const res = await getFeatures();
      this.computeFeatureEnabled = !!res.features?.['compute'];
      this.userManagementEnabled = !!res.features?.['user_management'];
      this.isEnterprise = Array.isArray(res.plugins) && res.plugins.length > 0;
      return res;
    } catch {
      this.computeFeatureEnabled = false;
      this.isEnterprise = false;
      this.userManagementEnabled = false;
      return null;
    } finally {
      this.featuresResolved = true;
    }
  }

  disconnectedCallback(): void {
    super.disconnectedCallback();
    this.unsubscribeRealtime?.();
    for (const key of Object.keys(this.refreshTimers)) {
      window.clearTimeout(this.refreshTimers[key]);
      delete this.refreshTimers[key];
    }
    if (this.backgroundTimer !== null) {
      window.clearInterval(this.backgroundTimer);
      this.backgroundTimer = null;
    }
  }

  private getUsernameFromToken(): string {
    try {
      const token = localStorage.getItem('accessToken');
      if (!token) return 'anonymous';
      const payloadPart = token.split('.')[1];
      if (!payloadPart) return 'anonymous';
      const base64 = payloadPart.replace(/-/g, '+').replace(/_/g, '/');
      const jsonPayload = decodeURIComponent(
        atob(base64)
          .split('')
          .map((c) => '%' + ('00' + c.charCodeAt(0).toString(16)).slice(-2))
          .join('')
      );
      const decoded = JSON.parse(jsonPayload);
      return decoded.sub || 'anonymous';
    } catch (e) {
      console.error('Error decoding JWT token:', e);
      return 'anonymous';
    }
  }

  private loadCachedDashboardData(): void {
    try {
      const sub = this.getUsernameFromToken();
      if (sub === 'anonymous') return;
      const key = `preloop:dashboard:${sub}`;
      const raw = sessionStorage.getItem(key);
      if (!raw) return;

      const data = JSON.parse(raw);
      if (data.gatewaySummary) this.gatewaySummary = data.gatewaySummary;
      this.runtimeSessions = data.runtimeSessions || [];
      this.managedAgents = data.managedAgents || [];
      this.gatewayInteractions = data.gatewayInteractions || [];
      this.auditGroups = data.auditGroups || [];
      this.trackers = data.trackers || [];
      this.totalIssues = data.totalIssues || 0;
      this.mcpServers = data.mcpServers || [];
      this.tools = data.tools || [];
      // Restored like `tools`: both arrive in the slow secondary pass, and
      // the Inventory Models tab would otherwise sit on "No models yet · Add
      // a model" for the length of that pass on every reload.
      this.aiModels = data.aiModels || [];
      this.aiModelOverview = data.aiModelOverview || [];
      this.flowExecutions = data.flowExecutions || [];
      this.flows = data.flows || [];
      this.flowExecutionsCount = data.flowExecutionsCount || 0;
      this.failedExecutionsCount = data.failedExecutionsCount || 0;
      this.succeededFlowExecutionsCount =
        data.succeededFlowExecutionsCount || 0;
      this.pendingApprovals = data.pendingApprovals || [];
      this.aiModelsCount = data.aiModelsCount || 0;
      this.enabledUsersCount = data.enabledUsersCount || 0;
      this.toolCallsCount = data.toolCallsCount || 0;
      this.failedToolCallsCount = data.failedToolCallsCount || 0;
      this.totalFlowsCount = data.totalFlowsCount || 0;
      this.totalAgentsCount = data.totalAgentsCount || 0;
      this.totalRuntimeSessionsCount = data.totalRuntimeSessionsCount || 0;
      this.hasFlows = data.hasFlows || false;
      this.hasAIModels = data.hasAIModels || false;
      if (data.lastUpdatedAt) this.lastUpdatedAt = data.lastUpdatedAt;
      this.approvalStats = data.approvalStats || this.approvalStats;
      this.attentionInputs = data.attentionInputs || null;
      this.budgetPolicies = data.budgetPolicies || [];
      this.budgetAgents = data.budgetAgents || [];

      this.loading = false;
    } catch (e) {
      console.warn('Failed to load dashboard cache from sessionStorage', e);
    }
  }

  /**
   * The cache write is a full JSON.stringify of every list on the page, and
   * the load used to call it once per finished pass. Coalesce them into one
   * write when the browser is next idle so no fetch handler pays for it.
   */
  private scheduleCacheWrite(): void {
    if (this.cacheWriteScheduled) return;
    this.cacheWriteScheduled = true;
    const write = () => {
      this.cacheWriteScheduled = false;
      this.saveDashboardCache();
    };
    const idle = (
      window as Window & {
        requestIdleCallback?: (
          cb: () => void,
          opts?: { timeout: number }
        ) => number;
      }
    ).requestIdleCallback;
    if (typeof idle === 'function') {
      idle(write, { timeout: 2000 });
    } else {
      window.setTimeout(write, 0);
    }
  }

  private saveDashboardCache(): void {
    try {
      const sub = this.getUsernameFromToken();
      if (sub === 'anonymous') return;
      const key = `preloop:dashboard:${sub}`;
      const cacheObj = {
        gatewaySummary: this.gatewaySummary,
        // Only what the cards actually show is worth keeping: the full lists
        // pushed this object past the sessionStorage quota on busy accounts,
        // and a quota failure threw away the whole cache.
        runtimeSessions: this.runtimeSessions.slice(0, CACHED_SESSIONS),
        managedAgents: this.managedAgents,
        gatewayInteractions: this.gatewayInteractions.slice(
          0,
          CACHED_INTERACTIONS
        ),
        auditGroups: this.auditGroups,
        trackers: this.trackers,
        totalIssues: this.totalIssues,
        mcpServers: this.mcpServers,
        tools: this.tools,
        aiModels: this.aiModels,
        aiModelOverview: this.aiModelOverview,
        flowExecutions: this.flowExecutions.slice(0, CACHED_FLOW_EXECUTIONS),
        flows: this.flows,
        flowExecutionsCount: this.flowExecutionsCount,
        failedExecutionsCount: this.failedExecutionsCount,
        succeededFlowExecutionsCount: this.succeededFlowExecutionsCount,
        pendingApprovals: this.pendingApprovals,
        aiModelsCount: this.aiModelsCount,
        enabledUsersCount: this.enabledUsersCount,
        toolCallsCount: this.toolCallsCount,
        failedToolCallsCount: this.failedToolCallsCount,
        totalFlowsCount: this.totalFlowsCount,
        totalAgentsCount: this.totalAgentsCount,
        totalRuntimeSessionsCount: this.totalRuntimeSessionsCount,
        hasFlows: this.hasFlows,
        hasAIModels: this.hasAIModels,
        lastUpdatedAt: this.lastUpdatedAt,
        approvalStats: this.approvalStats,
        attentionInputs: this.trimAttentionInputsForCache(),
        budgetPolicies: this.budgetPolicies,
        budgetAgents: this.budgetAgents,
      };
      sessionStorage.setItem(key, JSON.stringify(cacheObj));
    } catch (e) {
      if (e instanceof DOMException && e.name === 'QuotaExceededError') {
        // A half written cache is worse than none: drop it and carry on with
        // network data only for this session.
        try {
          const sub = this.getUsernameFromToken();
          sessionStorage.removeItem(`preloop:dashboard:${sub}`);
        } catch {
          // sessionStorage is unavailable; nothing left to clean up.
        }
        if (!this.cacheQuotaWarned) {
          this.cacheQuotaWarned = true;
          console.warn(
            'Dashboard cache disabled for this session: storage full'
          );
        }
        return;
      }
      console.warn('Failed to save dashboard cache to sessionStorage', e);
    }
  }

  /**
   * The usage summary inside the attention inputs carries per interaction
   * detail that the rules never read back; keep only the per model rollup.
   */
  private trimAttentionInputsForCache(): AttentionInputs | null {
    if (!this.attentionInputs) return null;
    const usage = this.attentionInputs.usageSummary;
    if (!usage) return this.attentionInputs;
    return {
      ...this.attentionInputs,
      usageSummary: {
        ...usage,
        requests_by_day: [],
        usage_by_flow: [],
        usage_by_session: [],
        usage_by_tool: [],
      },
    };
  }

  private loadDismissedState(): void {
    this.welcomeCardDismissed =
      localStorage.getItem('dashboard_welcome_dismissed') === 'true';
    this.nextStepsDismissed =
      localStorage.getItem(NEXT_STEPS_DISMISSED_KEY) === 'true';
  }

  /**
   * True once agents, budget policies, tools and the feature flags have all
   * answered. Until then the checklist has nothing to read.
   */
  private get nextStepsInputsResolved(): boolean {
    return (
      !this.loading &&
      !this.fetchingAgents &&
      !this.fetchingBudget &&
      !this.fetchingMCPAndTools &&
      this.featuresResolved
    );
  }

  /** `null` when no load has ever finished on this browser. */
  private readCoreStepsDone(): boolean | null {
    try {
      const stored = localStorage.getItem(NEXT_STEPS_DONE_KEY);
      return stored === null ? null : stored === 'true';
    } catch {
      return null;
    }
  }

  private rememberCoreStepsDone(done: boolean): void {
    try {
      localStorage.setItem(NEXT_STEPS_DONE_KEY, done ? 'true' : 'false');
    } catch {
      // Private mode: the card behaves as it did before, one refresh late.
    }
  }

  private dismissNextSteps(): void {
    this.nextStepsDismissed = true;
    try {
      localStorage.setItem(NEXT_STEPS_DISMISSED_KEY, 'true');
    } catch {
      // Private mode: the card stays hidden for this session only.
    }
  }

  private dismissWelcomeCard(): void {
    this.welcomeCardDismissed = true;
    // Only persist the dismissal once at least one agent is onboarded, so a
    // stray click can't permanently erase onboarding guidance for a user who
    // still has zero agents (session-only dismissal until then).
    if (this.managedAgents.length > 0) {
      localStorage.setItem('dashboard_welcome_dismissed', 'true');
    }
  }

  /**
   * One subscription per topic, each mapped to the smallest refresh that can
   * answer it.
   *
   * Every topic used to run the whole page again: 24 requests, and
   * `gateway_activity` is published once per model call, so one agent working
   * turned an open Overview into a refresh loop against the API. A topic now
   * costs at most the handful of requests that topic can change, at most once
   * every REALTIME_TOPIC_INTERVAL_MS. The attention inputs and the usage
   * breakdown are not on this path at all; they are on the visible-tab timer
   * below.
   */
  private connectRealtime(): void {
    const routes: Array<{
      topic: string;
      /** Topics that change the same data share a key and a floor. */
      key: string;
      run: () => Promise<void>;
    }> = [
      {
        topic: 'gateway_activity',
        key: 'gateway',
        run: () => this.refreshGatewayFold(),
      },
      {
        topic: 'runtime_sessions',
        key: 'fleet',
        run: () => this.refreshFleet(),
      },
      { topic: 'managed_agents', key: 'fleet', run: () => this.refreshFleet() },
      {
        topic: 'approvals',
        key: 'approvals',
        run: () => this.refreshPendingApprovals(),
      },
      {
        topic: 'flow_executions',
        key: 'flows',
        run: () => this.refreshFlowRuns(),
      },
      {
        topic: 'budget_health',
        key: 'budget',
        run: () => this.fetchBudgetSummary(),
      },
    ];
    // Not subscribed: `audit` (the feed ingests the event itself and the
    // exceptions card is on the background timer) and `system:authenticated`
    // (the initial fetch has already run by the time it arrives).
    const unsubscribers = routes.map((route) =>
      unifiedWebSocketManager.subscribe(route.topic, () =>
        this.scheduleTopicRefresh(route.key, route.run)
      )
    );
    this.unsubscribeRealtime = () => {
      for (const unsubscribe of unsubscribers) {
        unsubscribe();
      }
    };
    void unifiedWebSocketManager.connect();

    // The two expensive reads (attention inputs, usage breakdown) are worth
    // a minute of staleness and nothing more; a hidden tab is worth nothing.
    this.backgroundTimer = window.setInterval(() => {
      if (document.visibilityState !== 'visible') return;
      void this.refreshBackgroundInputs();
    }, BACKGROUND_REFRESH_MS);
  }

  /**
   * Run one topic's refresher, coalesced and rate limited.
   *
   * Events arriving while a run is scheduled are the same news, so they are
   * dropped rather than queued; a topic that has just run waits out the rest
   * of its floor instead of running again.
   */
  private scheduleTopicRefresh(key: string, run: () => Promise<void>): void {
    if (Date.now() - this.lastFetchStartedAt < 5000) {
      // The initial load is still landing; its data is newer than this event.
      return;
    }
    if (this.refreshTimers[key] !== undefined) {
      return;
    }
    const sinceLast = Date.now() - (this.lastTopicRefresh[key] || 0);
    const delay = Math.max(
      REALTIME_DEBOUNCE_MS,
      REALTIME_TOPIC_INTERVAL_MS - sinceLast
    );
    this.refreshTimers[key] = window.setTimeout(() => {
      delete this.refreshTimers[key];
      this.lastTopicRefresh[key] = Date.now();
      void run()
        .then(() => {
          this.lastUpdatedAt = new Date().toISOString();
          this.scheduleCacheWrite();
        })
        .catch((error) => {
          console.error(
            `Failed to refresh ${key} from a realtime event`,
            error
          );
        });
    }, delay);
  }

  /** What a gateway call can change: the totals, the deltas, the failures. */
  private async refreshGatewayFold(): Promise<void> {
    const startDateStr = this.getGatewayStartDate();
    const priorWindow = this.getPriorGatewayWindow(startDateStr);
    const [summary, priorSummary, rateLimitReport, interactions] =
      await Promise.all([
        this.catchWith403Handling(
          getAccountGatewayUsageSummary({
            startDate: startDateStr,
            includeBreakdown: false,
          }),
          null
        ),
        this.catchWith403Handling(
          getAccountGatewayUsageSummary({
            startDate: priorWindow.startDate,
            endDate: priorWindow.endDate,
            includeBreakdown: false,
          }),
          null
        ),
        this.catchWith403Handling(
          getAccountRateLimitReport({ startDate: startDateStr }),
          null
        ),
        this.catchWith403Handling(
          getAccountGatewayUsageSearch({
            limit: GATEWAY_FAILURES_REFRESH_LIMIT,
            startDate: startDateStr,
          }),
          { items: [] } as Awaited<
            ReturnType<typeof getAccountGatewayUsageSearch>
          >
        ),
      ]);
    // Merge, never replace: the breakdown on screen came from a heavier
    // request that this one does not make.
    this.gatewaySummary = mergeGatewaySummaryPreservingBreakdown(
      this.gatewaySummary,
      summary
    );
    this.priorGatewaySummary = priorSummary;
    this.rateLimitReport = rateLimitReport;
    this.gatewayInteractions = interactions.items || [];
  }

  /** What a session or agent event can change. */
  private async refreshFleet(): Promise<void> {
    const [runtimeSessions, managedAgents] = await Promise.all([
      this.catchWith403Handling(
        getAccountRuntimeSessions({
          status: 'all',
          limit: FOLD_SESSIONS_LIMIT,
          startDate: this.getGatewayStartDate(),
        }),
        { items: [] } as Awaited<ReturnType<typeof getAccountRuntimeSessions>>
      ),
      this.catchWith403Handling(
        getAccountAgents({ status: 'all', limit: 100 }),
        { items: [], total: 0 } as Awaited<ReturnType<typeof getAccountAgents>>
      ),
    ]);
    this.runtimeSessions = runtimeSessions.items || [];
    this.totalRuntimeSessionsCount =
      runtimeSessions.total ?? this.runtimeSessions.length;
    this.applyAgentsList(managedAgents);
  }

  /** What an approval event can change. */
  private async refreshPendingApprovals(): Promise<void> {
    const pending = await this.catchWith403Handling(
      this.fetchApprovalRequests('pending', 100),
      [] as ApprovalRequest[]
    );
    this.pendingApprovals = pending.filter((approval) =>
      this.isUnexpiredPendingApproval(approval)
    );
  }

  /** What a flow execution event can change. */
  private async refreshFlowRuns(): Promise<void> {
    const [flows, flowExecutions] = await Promise.all([
      this.catchWith403Handling(getFlows(), [] as any[]),
      this.catchWith403Handling(
        getFlowExecutions({ limit: FLOW_EXECUTIONS_PAGE_SIZE }),
        [] as FlowExecution[]
      ),
    ]);
    this.applyFlows(flows);
    this.applyFlowExecutions(flowExecutions);
  }

  /**
   * The reads that are too expensive for an event: the 30-day attention
   * breakdown and the selected range's breakdown. Once a minute, and only
   * while somebody is looking.
   */
  private async refreshBackgroundInputs(): Promise<void> {
    const startDateStr = this.getGatewayStartDate();
    await this.refreshUsageBreakdown(startDateStr);
    await this.refreshAttentionInputs();
    await this.refreshAuditExceptions();
    this.lastUpdatedAt = new Date().toISOString();
    this.scheduleCacheWrite();
  }

  private getGatewayStartDate(): string {
    const now = new Date();
    if (this.gatewayTimeRange === 'day') {
      const d = new Date(now);
      d.setDate(d.getDate() - 1);
      return d.toISOString();
    }
    if (this.gatewayTimeRange === 'week') {
      const d = new Date(now);
      d.setDate(d.getDate() - 7);
      return d.toISOString();
    }
    if (this.gatewayTimeRange === 'year') {
      const d = new Date(now);
      d.setFullYear(d.getFullYear() - 1);
      return d.toISOString();
    }
    const d = new Date(now);
    d.setMonth(d.getMonth() - 1);
    return d.toISOString();
  }

  /** The same boundary as `getGatewayStartDate()`, as epoch milliseconds. */
  private getGatewayStartMs(): number {
    return new Date(this.getGatewayStartDate()).getTime();
  }

  /**
   * The window immediately before the one on screen, same length. Computed
   * from the current start date so the two summaries always cover equal spans
   * (a month is not always 30 days).
   */
  private getPriorGatewayWindow(startDateStr: string): {
    startDate: string;
    endDate: string;
  } {
    const start = new Date(startDateStr).getTime();
    const span = Date.now() - start;
    return {
      startDate: new Date(start - span).toISOString(),
      endDate: new Date(start).toISOString(),
    };
  }

  private applyAgentsList(
    managedAgents: Awaited<ReturnType<typeof getAccountAgents>>
  ): void {
    this.managedAgents = managedAgents.items || [];
    this.budgetAgents = this.managedAgents;
    this.totalAgentsCount = managedAgents.total ?? this.managedAgents.length;
    if (this.managedAgents.length > 0 && !this.welcomeCardDismissed) {
      this.dismissWelcomeCard();
    }
  }

  private handleBudgetPoliciesChanged(
    event: CustomEvent<{ policies: BudgetPolicy[] }>
  ) {
    this.budgetPolicies = event.detail.policies;
  }

  /**
   * Budget policies and the agents that name their subjects. The spend the
   * Usage card shows comes from the one gateway summary the page already
   * fetches for `gatewayTimeRange`, plus each policy's period-aligned
   * `current_spend_usd`, so there is no second summary request.
   */
  private async fetchBudgetSummary(
    options: {
      sharedAgents?: Awaited<ReturnType<typeof getAccountAgents>> | null;
      features?: Awaited<ReturnType<typeof getFeatures>> | null;
    } = {}
  ) {
    this.fetchingBudget = true;
    try {
      const [budgetAgents, featuresRes] = await Promise.all([
        options.sharedAgents
          ? Promise.resolve(options.sharedAgents)
          : this.managedAgents.length > 0
            ? Promise.resolve({
                items: this.managedAgents,
                total: this.totalAgentsCount,
              } as Awaited<ReturnType<typeof getAccountAgents>>)
            : getAccountAgents({ status: 'all', limit: 100 }).catch(() => ({
                items: [] as ManagedAgentSummary[],
                total: 0,
              })),
        options.features !== undefined
          ? Promise.resolve(options.features)
          : getFeatures().catch(() => null),
      ]);

      const billingEnabled = featuresRes?.features?.billing === true;
      const policies = billingEnabled
        ? await getBudgetPolicies().catch(() => [] as BudgetPolicy[])
        : [];

      this.budgetPolicies = Array.isArray(policies) ? policies : [];
      this.budgetAgents = budgetAgents.items || [];
      this.scheduleCacheWrite();
    } finally {
      this.fetchingBudget = false;
    }
  }

  private async catchWith403Handling<T>(
    promise: Promise<T>,
    defaultValue: T
  ): Promise<T> {
    try {
      return await promise;
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      if (message.includes('403')) {
        return defaultValue;
      }
      console.error('Dashboard data fetch error:', error);
      return defaultValue;
    }
  }

  /**
   * The first wave: what the top of the page needs and nothing else.
   *
   * Eight small requests in parallel, then `loading` is off. Everything
   * heavier - the flows and their runs, the gateway call log, the tools, the
   * usage breakdown, the attention inputs - runs after the first paint in
   * {@link fetchDeferredData}. Nothing here is awaited twice: budget policies
   * used to sit between the wave and `loading = false`, so the whole page
   * waited on a request only the Usage card and the checklist read.
   */
  private async fetchDashboardData(
    options: { preserveLoadingState?: boolean } = {}
  ) {
    if (this.refreshInFlight) {
      return;
    }
    this.refreshInFlight = true;
    this.lastFetchStartedAt = Date.now();
    markOverviewTiming('overview-fetch-start');

    if (!options.preserveLoadingState) {
      this.fetchingGatewaySummary = true;
      this.fetchingRecentExecutions = true;
      this.fetchingInventory = true;
      this.fetchingApprovals = true;
      this.fetchingAgents = true;
      this.fetchingBudget = true;
      this.fetchingAudit = true;
      this.fetchingMCPAndTools = true;
      this.loading = true;
    }
    this.error = null;

    const startDateStr = this.getGatewayStartDate();
    const priorWindow = this.getPriorGatewayWindow(startDateStr);

    try {
      const adminPromise = this.fetchAdminStatus();
      const [
        gatewaySummary,
        priorGatewaySummary,
        rateLimitReport,
        pendingApprovals,
        runtimeSessions,
        managedAgents,
        featuresRes,
      ] = await Promise.all([
        this.catchWith403Handling(
          getAccountGatewayUsageSummary({
            startDate: startDateStr,
            includeBreakdown: false,
          }),
          null
        ),
        this.catchWith403Handling(
          getAccountGatewayUsageSummary({
            startDate: priorWindow.startDate,
            endDate: priorWindow.endDate,
            includeBreakdown: false,
          }),
          null
        ),
        // One call, same window as the summary: without it the model row
        // cannot say how many requests a provider throttled.
        this.catchWith403Handling(
          getAccountRateLimitReport({ startDate: startDateStr }),
          null
        ),
        this.catchWith403Handling(
          this.fetchApprovalRequests('pending', 100),
          [] as ApprovalRequest[]
        ),
        // Above the fold a session answers one question - has anything ever
        // run on this account - so a short page is enough. The attention
        // loader asks for the window and the depth its rules need.
        this.catchWith403Handling(
          getAccountRuntimeSessions({
            status: 'all',
            limit: FOLD_SESSIONS_LIMIT,
            startDate: startDateStr,
          }),
          {
            items: [],
          } as Awaited<ReturnType<typeof getAccountRuntimeSessions>>
        ),
        this.catchWith403Handling(
          getAccountAgents({ status: 'all', limit: 100 }),
          {
            items: [],
            total: 0,
          } as Awaited<ReturnType<typeof getAccountAgents>>
        ),
        this.fetchFeatures(),
      ]);
      await adminPromise;

      // One batch of assignments with no await between them, so Lit renders
      // the finished fold once instead of eight times.
      this.rateLimitReport = rateLimitReport;
      this.gatewaySummary = mergeGatewaySummaryPreservingBreakdown(
        this.gatewaySummary,
        gatewaySummary
      );
      this.priorGatewaySummary = priorGatewaySummary;
      this.fetchingGatewaySummary = false;
      this.updatingUsage = false;

      this.pendingApprovals = pendingApprovals.filter((approval) =>
        this.isUnexpiredPendingApproval(approval)
      );
      this.fetchingApprovals = false;

      this.runtimeSessions = runtimeSessions.items || [];
      this.totalRuntimeSessionsCount =
        runtimeSessions.total ?? this.runtimeSessions.length;
      this.applyAgentsList(managedAgents);
      this.fetchingAgents = false;

      this.lastUpdatedAt = new Date().toISOString();
      this.loading = false;
      markOverviewTiming('overview-fold-ready');

      // Not awaited: the Usage card and the checklist wait on
      // `fetchingBudget` themselves, and nothing above the fold does.
      this.budgetReady = this.fetchBudgetSummary({
        sharedAgents: managedAgents,
        features: featuresRes,
      });
      this.scheduleCacheWrite();

      void this.fetchDeferredData(startDateStr, {
        // A range change reloads what the range changes; the flows, the
        // people and the tool catalogue are the same at any range.
        rangeChangeOnly: options.preserveLoadingState === true,
      });
    } catch (error) {
      console.error(
        'Failed to complete background loading of overview dashboard',
        error
      );
      this.error = 'Failed to load some overview dashboard data.';
      this.fetchingGatewaySummary = false;
      this.updatingUsage = false;
      this.fetchingRecentExecutions = false;
      this.fetchingInventory = false;
      this.fetchingApprovals = false;
      this.fetchingAgents = false;
      this.fetchingBudget = false;
      this.fetchingAudit = false;
      this.fetchingMCPAndTools = false;
      this.loading = false;
    } finally {
      this.refreshInFlight = false;
    }
  }

  /**
   * Everything below the fold, after the first paint.
   *
   * The three groups run in parallel; the attention loader runs last because
   * it is handed the approvals, agents, policies and breakdown the rest of
   * this pass already fetched, instead of fetching its own copies of all
   * four.
   */
  private async fetchDeferredData(
    startDateStr: string,
    options: { rangeChangeOnly?: boolean } = {}
  ): Promise<void> {
    if (options.rangeChangeOnly) {
      await Promise.all([
        this.refreshGatewayInteractions(startDateStr),
        this.refreshUsageBreakdown(startDateStr),
      ]);
      this.scheduleCacheWrite();
      markOverviewTiming('overview-deferred-ready');
      return;
    }
    const inventoryPromise = this.fetchInventoryData(startDateStr);
    const breakdownPromise = this.refreshUsageBreakdown(startDateStr);
    const secondaryPromise = this.fetchSecondaryDashboardData();

    await Promise.all([inventoryPromise, breakdownPromise]);
    markOverviewTiming('overview-inventory-ready');
    // The policies are one of the attention inputs, so this is the one place
    // that does wait for them.
    await this.budgetReady.catch(() => undefined);
    // Attention always uses its own rolling 30-day window. The Overview
    // "month" range is a calendar month, which is not the same 30 days.
    await this.refreshAttentionInputs();
    await secondaryPromise;
    this.scheduleCacheWrite();
    markOverviewTiming('overview-deferred-ready');
  }

  /**
   * The page of gateway calls behind the failures card. Its rows are the ones
   * that happened inside the range on screen, so a range change reloads it.
   */
  private async refreshGatewayInteractions(
    startDateStr: string
  ): Promise<void> {
    const interactions = await this.catchWith403Handling(
      getAccountGatewayUsageSearch({ limit: 100, startDate: startDateStr }),
      { items: [] } as Awaited<ReturnType<typeof getAccountGatewayUsageSearch>>
    );
    this.gatewayInteractions = interactions.items || [];
  }

  /** Flows, their runs, resolved approvals and the gateway call log. */
  private async fetchInventoryData(startDateStr: string): Promise<void> {
    this.fetchingInventory = true;
    this.fetchingRecentExecutions = true;
    try {
      const [flows, flowExecutions, allApprovalRequests] = await Promise.all([
        this.catchWith403Handling(getFlows(), [] as any[]),
        // The Inventory Flows tab needs a last run, a run count and a
        // failure count for every flow in the account, so this reads the
        // server's maximum page rather than the five rows the old card
        // showed.
        this.catchWith403Handling(
          getFlowExecutions({ limit: FLOW_EXECUTIONS_PAGE_SIZE }),
          [] as FlowExecution[]
        ),
        this.catchWith403Handling(
          this.fetchApprovalRequests(undefined, 100),
          [] as ApprovalRequest[]
        ),
        // The failures card shows a handful, and it should be the handful
        // that happened in the range the page is showing.
        this.refreshGatewayInteractions(startDateStr),
      ]);

      this.applyFlows(flows);
      this.applyFlowExecutions(flowExecutions);
      this.calculateApprovalStats(allApprovalRequests);
    } catch (error) {
      console.error('Failed to load the Inventory data', error);
    } finally {
      this.fetchingInventory = false;
      this.fetchingRecentExecutions = false;
    }
  }

  private applyFlows(flows: Array<{ id: string; name?: string }>): void {
    const list = flows || [];
    this.hasFlows = list.length > 0;
    this.totalFlowsCount = list.length;
    this.flows = list.map((flow) => ({
      id: flow.id,
      name: flow.name || 'Untitled flow',
    }));
  }

  private applyFlowExecutions(flowExecutions: FlowExecution[]): void {
    const sorted = [...(flowExecutions || [])].sort(
      (left, right) =>
        new Date(right.start_time).getTime() -
        new Date(left.start_time).getTime()
    );
    this.flowExecutionsCount = sorted.length;
    this.failedExecutionsCount = sorted.filter(
      (execution) => execution.status === 'FAILED'
    ).length;
    this.succeededFlowExecutionsCount = sorted.filter(
      (execution) =>
        execution.status === 'SUCCEEDED' || execution.status === 'COMPLETED'
    ).length;
    this.flowExecutions = sorted;
  }

  /**
   * Audit exceptions, teammates, the tool catalogue and the model list. The
   * usage breakdown used to be awaited here too; it is now started beside
   * this pass so the attention loader can reuse it.
   */
  private async fetchSecondaryDashboardData() {
    this.fetchingAudit = true;
    this.fetchingMCPAndTools = true;

    const pAudit = (async () => {
      try {
        const [
          audit,
          trackers,
          issueCount,
          toolCallsStats,
          failedToolCallsStats,
        ] = await Promise.all([
          this.catchWith403Handling(this.fetchAuditExceptions(), {
            groups: [],
            total: 0,
          }),
          this.catchWith403Handling(getTrackers(), [] as Tracker[]),
          this.catchWith403Handling(getIssueCount(), { total_issues: 0 }),
          this.catchWith403Handling(
            fetchWithAuth(
              '/api/v1/audit-logs/grouped?event_type=tool_call&limit=1'
            ).then((r) => r.json()),
            { total: 0 }
          ),
          this.catchWith403Handling(
            fetchWithAuth(
              '/api/v1/audit-logs/grouped?event_type=tool_call&outcome=failed&limit=1'
            ).then((r) => r.json()),
            { total: 0 }
          ),
        ]);
        this.auditGroups = audit.groups || [];
        this.trackers = trackers;
        this.totalIssues = issueCount.total_issues;
        this.toolCallsCount = toolCallsStats?.total || 0;
        this.failedToolCallsCount = failedToolCallsStats?.total || 0;
      } catch (error) {
        console.error('Failed to load audit data', error);
      } finally {
        this.fetchingAudit = false;
      }
    })();

    // Its own request, awaited by nothing else: the Users tab can list the
    // people the moment their names arrive, without waiting for the tools.
    const pUsers = (async () => {
      this.fetchingUsers = true;
      try {
        const users = await this.catchWith403Handling(
          // User management is a licensed feature, not a hosting model:
          // a self-hosted Enterprise account has teammates too, and the
          // Inventory's Users tab is gated on the same flag and on
          // view_users. Both the flags and the profile have answered by
          // the time this secondary fetch runs, so a reader without the
          // permission does not spend a request on a certain 403.
          this.fetchesUsers
            ? getUsers()
            : Promise.resolve({
                users: [],
                total: 0,
                skip: 0,
                limit: 0,
              }),
          {
            users: [],
            total: 0,
            skip: 0,
            limit: 0,
          }
        );
        this.accountUsers = Array.isArray(users.users)
          ? (users.users as AccountUser[]).filter(
              (user) => user.is_active !== false
            )
          : [];
        this.enabledUsersCount = Array.isArray(users.users)
          ? users.users.filter((u: { is_active?: boolean }) => u.is_active)
              .length
          : 0;
      } catch (error) {
        console.error('Failed to load users', error);
      } finally {
        this.fetchingUsers = false;
      }
    })();

    const pTools = (async () => {
      try {
        const [mcpServers, tools, aiModels, apiKeys] = await Promise.all([
          this.catchWith403Handling(getMCPServers(), [] as MCPServer[]),
          this.catchWith403Handling(getTools(), [] as Tool[]),
          this.catchWith403Handling(getAIModels(), []),
          // Already the api-keys page's endpoint; here it is only a count.
          this.catchWith403Handling(getApiKeys(), null),
        ]);
        // "Active" means usable: not revoked, not past its expiry.
        this.apiKeysCount = apiKeys
          ? apiKeys.filter(
              (key) =>
                key.activity_status !== 'revoked' &&
                (!key.expires_at ||
                  parseUTCDate(key.expires_at).getTime() > Date.now())
            ).length
          : null;
        this.mcpServers = mcpServers;
        this.tools = tools;
        this.aiModels = aiModels || [];
        // Exclude speech models, then never auto-select a principal-bound
        // OAuth model — it cannot serve server-side generation.
        const filtered = selectableModels(
          this.aiModels.filter(
            (m) => m.model_kind !== 'stt' && m.model_kind !== 'tts'
          )
        );
        if (
          filtered.length > 0 &&
          !filtered.some((m) => m.id === this.deployModel)
        ) {
          this.deployModel = pickDefaultModel(filtered)?.id || '';
        }
        this.hasAIModels = (aiModels || []).length > 0;
        this.aiModelsCount = Array.isArray(aiModels) ? aiModels.length : 0;
      } catch (error) {
        console.error('Failed to load MCP and tools data', error);
      } finally {
        this.fetchingMCPAndTools = false;
      }
    })();

    await Promise.all([pAudit, pTools, pUsers]);
    this.scheduleCacheWrite();
  }

  /**
   * The breakdown behind the top-models card and the usage columns, plus the
   * per-model overview. Returns the breakdown so the attention loader can be
   * handed it instead of asking for a second one.
   */
  private async refreshUsageBreakdown(
    gatewayStartDate: string
  ): Promise<AccountGatewayUsageSummaryResponse | null> {
    this.fetchingUsageBreakdown = true;
    try {
      // Both in the deferred pass, so the fold is unaffected: the breakdown
      // feeds the top-models card, the overview feeds the Inventory Models
      // tab in one request rather than one per model.
      const [detailed, overview] = await Promise.all([
        this.catchWith403Handling(
          getAccountGatewayUsageSummary({
            startDate: gatewayStartDate,
            includeBreakdown: true,
          }),
          null
        ),
        this.catchWith403Handling(
          getAIModelsOverview({ startDate: gatewayStartDate }),
          null
        ),
      ]);
      if (overview?.models) {
        this.aiModelOverview = overview.models;
      }
      if (!detailed) {
        return null;
      }
      this.gatewaySummary = detailed;
      return detailed;
    } catch (error) {
      console.error('Failed to load gateway breakdown for top models', error);
      return null;
    } finally {
      // Off whatever happened: a 403 on this endpoint means the columns will
      // never fill, and a skeleton that never resolves is worse than a zero.
      this.fetchingUsageBreakdown = false;
    }
  }

  private async fetchAuditExceptions(): Promise<GroupedAuditResponse> {
    const params = new URLSearchParams();
    params.set('limit', '12');
    params.append('outcome', 'failed');
    params.append('outcome', 'budget_denied');
    const response = await fetchWithAuth(
      `/api/v1/audit-logs/grouped?${params}`
    );
    if (!response.ok) {
      throw new Error('Failed to fetch audit exceptions');
    }
    return response.json();
  }

  private async fetchApprovalRequests(
    status?: string,
    limit: number = 100
  ): Promise<ApprovalRequest[]> {
    const params = new URLSearchParams();
    params.set('limit', String(limit));
    if (status) {
      params.set('status', status);
    }
    const response = await fetchWithAuth(
      `/api/v1/approval-requests?${params.toString()}`
    );
    if (!response.ok) {
      throw new Error('Failed to fetch approval requests');
    }
    return response.json();
  }

  private isUnexpiredPendingApproval(approval: ApprovalRequest): boolean {
    if (approval.status !== 'pending') {
      return false;
    }
    if (!approval.expires_at) {
      return true;
    }
    return parseUTCDate(approval.expires_at).getTime() > Date.now();
  }

  private calculateApprovalStats(requests: ApprovalRequest[]): void {
    const total = requests.length;
    const approved = requests.filter(
      (request) => request.status === 'approved'
    ).length;
    const declined = requests.filter(
      (request) => request.status === 'declined'
    ).length;
    const expired = requests.filter(
      (request) => request.status === 'expired'
    ).length;

    let totalTimeMinutes = 0;
    let resolvedCount = 0;
    for (const request of requests) {
      if (
        (request.status === 'approved' || request.status === 'declined') &&
        request.resolved_at
      ) {
        totalTimeMinutes +=
          (parseUTCDate(request.resolved_at).getTime() -
            parseUTCDate(request.requested_at).getTime()) /
          60000;
        resolvedCount += 1;
      }
    }

    this.approvalStats = {
      total,
      approved,
      declined,
      expired,
      avgApprovalTime:
        resolvedCount > 0 ? Math.round(totalTimeMinutes / resolvedCount) : 0,
    };
  }

  private get isOnboarded(): boolean {
    return (
      (this.managedAgents && this.managedAgents.length > 0) ||
      (this.runtimeSessions && this.runtimeSessions.length > 0) ||
      this.totalAgentsCount > 0 ||
      this.flowExecutionsCount > 0 ||
      this.hasFlows
    );
  }

  private get activeSessions(): RuntimeSessionSummary[] {
    return [...this.runtimeSessions]
      .filter((session) => session.id && (session.total_requests || 0) > 0)
      .sort((left, right) => {
        const leftTs = left.last_activity_at || left.started_at;
        const rightTs = right.last_activity_at || right.started_at;
        return new Date(rightTs).getTime() - new Date(leftTs).getTime();
      });
  }

  private get gatewayFailures(): GatewayUsageSearchResultItem[] {
    return this.gatewayInteractions.filter(
      (item) => item.outcome !== 'success'
    );
  }

  private get failedFlowExecutions(): FlowExecution[] {
    return this.flowExecutions.filter(
      (execution) => execution.status === 'FAILED'
    );
  }

  private formatCurrency(value: number | null | undefined): string {
    return `$${(value || 0).toFixed(2)}`;
  }

  private formatNumber(value: number | null | undefined): string {
    return Intl.NumberFormat().format(value || 0);
  }

  private formatDateTime(value: string | null | undefined): string {
    if (!value) {
      return 'Never';
    }
    return parseUTCDate(value).toLocaleString();
  }

  private getSessionDisplayTitle(
    session: GatewayUsageBySession | RuntimeSessionSummary
  ): string {
    return normalizeObservedSession(session).title;
  }

  private getSessionDetailHref(
    session: GatewayUsageBySession | RuntimeSessionSummary
  ): string {
    if ('flow_execution_id' in session && session.flow_execution_id) {
      return `/console/flows/executions/${session.flow_execution_id}`;
    }
    if (
      session.session_source_type === 'flow_execution' &&
      session.session_source_id
    ) {
      return `/console/flows/executions/${session.session_source_id}`;
    }
    const observed = normalizeObservedSession(session);
    return observed.canLoadEvents
      ? `/console/runtime-sessions?sessionId=${observed.id}`
      : '/console/runtime-sessions';
  }

  private formatRelativeTime(value: string | null | undefined): string {
    return formatRelativeTime(value);
  }

  /**
   * The freshness stamp beside the title. The element owns its own thirty
   * second timer, so ageing the label no longer re-renders this page.
   */
  private renderLastUpdated() {
    return html`<relative-time-label
      .timestamp=${this.lastUpdatedAt}
      .fallback=${this.lastUpdatedFallback}
    ></relative-time-label>`;
  }

  /** What the header shows when there is no timestamp to age yet. */
  private get lastUpdatedFallback(): string {
    if (
      this.loading ||
      this.fetchingGatewaySummary ||
      this.fetchingRecentExecutions ||
      this.fetchingBudget ||
      this.fetchingAgents
    ) {
      return 'Loading…';
    }
    return 'Never';
  }

  /**
   * Chip colour is a taxonomy, not decoration: success means finished or
   * live, danger means failed. A run that is still going is a state, so it
   * is neutral; amber is reserved for things asking for a human.
   */
  private getStatusColor(status: string): string {
    switch (status.toLowerCase()) {
      case 'active':
      case 'succeeded':
      case 'completed':
      case 'approved':
        return 'success';
      case 'failed':
      case 'error':
      case 'declined':
        return 'danger';
      default:
        return 'neutral';
    }
  }

  /**
   * The same derivation the Attention page uses, over data this view already
   * fetches, so the hero count, the side card and /console/attention can never
   * disagree.
   */
  private get attentionItems(): AttentionItem[] {
    if (!this.attentionInputs) {
      return [];
    }
    // Memoised on the inputs object: the derivation is a thousand-line rules
    // module and this getter is read from the template, so it used to run on
    // every render, including the forty a load causes.
    if (this.attentionMemo?.inputs === this.attentionInputs) {
      return this.attentionMemo.items;
    }
    const items = deriveAttentionItems(this.attentionInputs).items;
    this.attentionMemo = { inputs: this.attentionInputs, items };
    return items;
  }

  /**
   * Same loader, same rules as the Attention page, over the inputs this page
   * already has where it has them: approvals, agents and budget policies come
   * from the fold, and on the default month range the usage breakdown comes
   * from the deferred pass. The rest is fetched exactly as before. Starts
   * after the first paint so a slow attention input never holds up the cards
   * above the fold.
   */
  private async refreshAttentionInputs(
    shared: PrefetchedAttentionInputs = {}
  ): Promise<void> {
    try {
      this.attentionInputs = await loadAttentionInputs({
        prefetched: {
          approvals: this.pendingApprovals as AttentionApproval[],
          agents: this.managedAgents,
          budgetPolicies: this.budgetPolicies,
          ...shared,
        },
      });
      this.scheduleCacheWrite();
    } catch (error) {
      console.error('Failed to load attention inputs', error);
    }
  }

  /** The exceptions card on its own, for the background timer. */
  private async refreshAuditExceptions(): Promise<void> {
    const audit = await this.catchWith403Handling(this.fetchAuditExceptions(), {
      groups: [],
      total: 0,
    });
    this.auditGroups = audit.groups || [];
  }

  private get gatewayRangeLabel(): string {
    if (this.gatewayTimeRange === 'day') return '24h';
    if (this.gatewayTimeRange === 'week') return '7d';
    if (this.gatewayTimeRange === 'year') return '1y';
    return '30d';
  }

  /** True once an agent talks to both the model gateway and the tool firewall. */
  private get hasFullyOnboardedAgent(): boolean {
    return this.managedAgents.some(
      (agent) => agent.onboarding_state === 'fully_onboarded'
    );
  }

  /**
   * True when at least one tool is disabled or approval-gated. The next-step
   * label ("Restrict a tool") matches this signal. Subject-scoped model lists
   * and other Policies-page rules are not visible here.
   */
  private get hasToolPolicy(): boolean {
    return this.tools.some(
      (tool) => !tool.is_enabled || Boolean(tool.approval_workflow_id)
    );
  }

  private get nextSteps(): NextStep[] {
    const steps: NextStep[] = [
      {
        id: 'agent',
        label: 'Onboard an agent',
        done: this.managedAgents.length > 0 || this.totalAgentsCount > 0,
        href: '/console/agents',
      },
      {
        id: 'budget',
        label: 'Set a spending limit',
        done: this.budgetPolicies.length > 0,
        onClick: () => (this.showBudgetDialog = true),
      },
      {
        id: 'policy',
        label: 'Restrict a tool',
        done: this.hasToolPolicy,
        href: '/console/policies',
      },
    ];
    if (this.userManagementEnabled) {
      steps.push({
        id: 'invite',
        optional: true,
        label: 'Invite a teammate',
        done: this.enabledUsersCount > 1,
        href: '/console/settings/invitations',
      });
    }
    return steps;
  }

  private get enabledToolsCount(): number {
    return this.tools.filter((tool) => tool.is_enabled).length;
  }

  private getManagedAgentBySourceId(
    sourceId: string | null | undefined
  ): ManagedAgentSummary | undefined {
    if (!sourceId) {
      return undefined;
    }
    const agents = [...this.managedAgents, ...this.budgetAgents];
    const exact = agents.find(
      (agent) =>
        agent.id === sourceId ||
        agent.session_source_id === sourceId ||
        agent.runtime_session_id === sourceId
    );
    if (exact) {
      return exact;
    }
    // Per-run sessions append a suffix to the agent's base source id, e.g.
    // "custom_ABC:demo-123" (custom) or "hermes-0161afa5e616-<timestamp|uuid>"
    // (runtime-token agents). Match the agent whose base source id the session
    // id starts with (delimited by ':' or '-'); prefer the longest match so a
    // more specific agent wins over a shorter prefix.
    return agents
      .filter(
        (agent) =>
          Boolean(agent.session_source_id) &&
          (sourceId.startsWith(`${agent.session_source_id}:`) ||
            sourceId.startsWith(`${agent.session_source_id}-`))
      )
      .sort(
        (a, b) =>
          (b.session_source_id?.length ?? 0) -
          (a.session_source_id?.length ?? 0)
      )[0];
  }

  private getManagedAgentForUsageSession(
    session: GatewayUsageBySession
  ): ManagedAgentSummary | undefined {
    const lookupIds = [
      session.agent_id,
      session.runtime_session_id,
      session.session_source_id,
      session.runtime_principal_id,
    ].filter((value): value is string => Boolean(value));

    for (const sourceId of lookupIds) {
      const agent = this.getManagedAgentBySourceId(sourceId);
      if (agent) {
        return agent;
      }
    }
    return undefined;
  }

  private handleDeployAgentSuccess(event: CustomEvent): void {
    const mockAgent = event.detail.agent;
    this.managedAgents = [mockAgent, ...(this.managedAgents || [])];
    this.totalAgentsCount = (this.totalAgentsCount || 0) + 1;
    this.requestUpdate();
  }

  private handleDeployFlowSuccess(event: CustomEvent): void {
    this.flowExecutionsCount = (this.flowExecutionsCount || 0) + 1;
    this.hasFlows = true;
    void this.fetchDashboardData();
    this.requestUpdate();
  }

  private handleDeployWizardDone(): void {
    this.dismissWelcomeCard();
  }

  private renderWelcomeCard() {
    if (this.welcomeCardDismissed) {
      return nothing;
    }

    return html`
      <div
        class="welcome-container"
        style="background: transparent; width: 100%;  display: flex; flex-direction: column; align-items: center; padding: 0 0 var(--sl-spacing-large) 0; position: relative;"
      >
        <sl-button
          size="small"
          variant="text"
          style="position: absolute; right: 0; top: 0;"
          aria-label="Dismiss get started"
          @click=${this.dismissWelcomeCard}
        >
          <sl-icon name="x-lg" label="Dismiss get started"></sl-icon>
        </sl-button>

        <img
          src="/assets/preloop-badge.svg"
          style="width: 56px; height: 56px; margin-bottom: var(--sl-spacing-small); margin-top: var(--sl-spacing-small); border-radius: var(--sl-border-radius-medium);"
        />

        <h2
          style="font-size: 1.75rem; font-weight: 700; color: var(--sl-color-neutral-900); margin: 0 0 var(--sl-spacing-medium) 0; text-align: center;"
        >
          Get Started with Preloop
        </h2>

        <div
          class="welcome-content"
          style="width: 100%; display: flex; flex-direction: column; align-items: center;"
        >
          <preloop-deploy-wizard
            .aiModels=${this.aiModels}
            .computeFeatureEnabled=${this.computeFeatureEnabled}
            .isEnterprise=${this.isEnterprise}
            .isAdmin=${this.isAdmin}
            hide-cancel
            @deploy-agent-success=${this.handleDeployAgentSuccess}
            @deploy-flow-success=${this.handleDeployFlowSuccess}
            @deploy-wizard-done=${this.handleDeployWizardDone}
          ></preloop-deploy-wizard>
        </div>
      </div>
    `;
  }

  /**
   * The four things a new account has to do, with their real state read from
   * the same data the rest of the page uses. It disappears on its own when
   * they are done, so nobody has to dismiss it to be rid of it.
   */
  private renderNextStepsCard() {
    if (this.nextStepsDismissed) {
      return nothing;
    }
    const remembered = this.readCoreStepsDone();
    if (!this.nextStepsInputsResolved && remembered !== false) {
      // Either this browser has never seen a finished load, or the last one
      // said the checklist was done. Both cases stay quiet until the four
      // fetches land, instead of drawing an empty checklist for a second.
      return nothing;
    }
    const steps = this.nextSteps;
    const allDone = steps.every((step) => step.done || step.optional);
    if (this.nextStepsInputsResolved) {
      this.rememberCoreStepsDone(allDone);
    }
    if (allDone) {
      return nothing;
    }

    return html`
      <sl-card class="content-card next-steps-card">
        <div slot="header" class="card-header-with-action">
          <div class="card-title">Next steps</div>
          <sl-icon-button
            name="x-lg"
            label="Dismiss next steps"
            @click=${this.dismissNextSteps}
          ></sl-icon-button>
        </div>
        <div class="next-steps-list">
          ${steps.map((step) => {
            const label = html`
              <sl-icon
                class="next-step-mark ${step.done ? 'done' : ''}"
                name=${step.done ? 'check-circle-fill' : 'circle'}
                aria-hidden="true"
              ></sl-icon>
              <span class="next-step-label">${step.label}</span>
            `;
            return html`
              <div class="next-step ${step.done ? 'done' : ''}">
                ${
                  step.href
                    ? html`<a class="next-step-link" href=${step.href}
                        >${label}</a
                      >`
                    : html`<button
                        class="next-step-link"
                        type="button"
                        @click=${step.onClick}
                      >
                        ${label}
                      </button>`
                }
              </div>
            `;
          })}
        </div>
      </sl-card>
    `;
  }
  private renderUsageCard() {
    return html`
      <usage-card
        .summary=${this.gatewaySummary}
        .priorSummary=${this.priorGatewaySummary}
        .policies=${this.budgetPolicies}
        .loading=${this.fetchingGatewaySummary || this.fetchingBudget}
        ?updating=${this.updatingUsage}
        .error=${this.error}
        .timeRange=${this.gatewayTimeRange}
        .toolCallsCount=${this.toolCallsCount}
        @range-change=${(event: CustomEvent<{ value: string }>) => {
          event.stopPropagation();
          this.gatewayTimeRange = event.detail.value as
            'day' | 'week' | 'month' | 'year';
          // The numbers on screen belong to the old range, so say they are
          // being replaced instead of clearing them.
          this.updatingUsage = true;
          void this.fetchDashboardData({ preserveLoadingState: true }).finally(
            () => {
              // A refresh already in flight returns without fetching; the
              // card should not be left spinning on it.
              this.updatingUsage = false;
            }
          );
        }}
        @configure-limits=${() => (this.showBudgetDialog = true)}
      ></usage-card>
    `;
  }

  private renderGatewayFailuresCard() {
    if (this.gatewayFailures.length === 0) {
      return nothing;
    }

    return html`
      <sl-card class="content-card">
        <div class="card-header">
          <div class="card-title">Gateway failures needing attention</div>
          <a class="row-link" href="/console/api-usage"
            >Open gateway activity</a
          >
        </div>
        <div class="list">
          ${repeat(
            this.gatewayFailures.slice(0, 6),
            (item) => item.api_usage_id,
            (item) => html`
              <div class="row">
                <div class="row-main">
                  <a
                    class="row-link row-primary"
                    href=${
                      item.runtime_session_id
                        ? `/console/runtime-sessions?sessionId=${item.runtime_session_id}`
                        : '/console/api-usage'
                    }
                  >
                    ${item.model_alias || item.provider_name || item.endpoint}
                  </a>
                  <sl-badge class="chip" pill variant="danger"
                    >${item.status_code}</sl-badge
                  >
                </div>
                <div class="row-meta">
                  <span>
                    ${
                      item.runtime_principal_name ||
                      this.getSessionDisplayTitle(item) ||
                      item.endpoint
                    }
                  </span>
                  <span>${this.formatRelativeTime(item.timestamp)}</span>
                </div>
              </div>
            `
          )}
        </div>
      </sl-card>
    `;
  }

  private renderAuditExceptionsCard() {
    if (this.auditGroups.length === 0) {
      return nothing;
    }

    return html`
      <sl-card class="content-card">
        <div class="card-header">
          <div class="card-title">Audit exceptions</div>
          <a class="row-link" href="/console/audit">Open audit timeline</a>
        </div>
        <div class="list">
          ${repeat(
            this.auditGroups.slice(0, 6),
            (group) => group.primary_event.id,
            (group) => html`
              <div class="row">
                <div class="row-main">
                  <span class="row-primary">
                    ${group.primary_event.action.replace(/_/g, ' ')}
                  </span>
                  <sl-badge
                    class="chip"
                    pill
                    variant=${
                      group.outcome === 'budget_denied' ? 'warning' : 'danger'
                    }
                  >
                    ${group.outcome}
                  </sl-badge>
                </div>
                <div class="row-meta">
                  <span>
                    ${
                      (group.primary_event.details?.requested_model as
                        string | undefined) ||
                      (group.primary_event.details?.tool_name as
                        string | undefined) ||
                      group.primary_event.id
                    }
                  </span>
                  <span>
                    ${this.formatRelativeTime(group.primary_event.timestamp)}
                  </span>
                </div>
              </div>
            `
          )}
        </div>
      </sl-card>
    `;
  }

  /**
   * One amber line across the top of the page, above everything else, or
   * nothing at all. The side card it replaces competed with Usage for the
   * same column and was read after it; a strip is read first because it is
   * first, and it costs one line instead of a card.
   */
  private renderAttentionStrip() {
    const all = this.attentionItems;
    // Low-tone items (a model priced at $0) are a question, not a problem.
    // They never take a slot from something that is actually wrong, so the
    // strip shows them only when nothing louder is open.
    const loud = all.filter((item) => item.severity !== 'low');
    const lowOnly = loud.length === 0;
    const items = lowOnly ? all : loud;
    if (items.length === 0) {
      return nothing;
    }
    const visible = items.slice(0, 3);

    return html`
      <div class="attention-strip ${lowOnly ? 'low-only' : ''}">
        <sl-icon
          class="attention-strip-icon"
          name=${lowOnly ? 'info-circle' : 'exclamation-triangle'}
          aria-hidden="true"
        ></sl-icon>
        <span class="attention-strip-count"
          >${this.formatNumber(items.length)}
          ${lowOnly ? 'worth a look' : 'need attention'}</span
        >
        <div class="attention-strip-items">
          ${repeat(
            visible,
            (item) => item.id,
            (item) => html`
              <a
                class="attention-chip-link"
                href=${attentionItemAnchor(item.id)}
              >
                <sl-badge
                  class="chip"
                  variant=${lowOnly ? 'neutral' : 'warning'}
                  pill
                >
                  <sl-icon
                    name=${ATTENTION_KIND_META[item.kind].icon}
                    aria-hidden="true"
                  ></sl-icon>
                  <span class="attention-chip-text"
                    >${item.title} · ${item.detail}</span
                  >
                </sl-badge>
              </a>
            `
          )}
        </div>
        <a class="attention-strip-all" href="/console/attention"
          >${
            items.length > visible.length
              ? html`+${this.formatNumber(items.length - visible.length)} more · `
              : nothing
          }View
          all <span aria-hidden="true">→</span></a
        >
      </div>
    `;
  }

  private get modelGatewayUrl(): string {
    return `${window.location.origin}${this.gatewayFormat}`;
  }

  private get toolFirewallUrl(): string {
    return `${window.location.origin}/mcp`;
  }

  private copyEndpoint(url: string, message: string): void {
    void navigator.clipboard.writeText(url);
    this.dispatchEvent(
      new CustomEvent('show-toast', {
        bubbles: true,
        composed: true,
        detail: { message },
      })
    );
  }

  /**
   * The host truncates and the path stays: a middle ellipsis without measuring
   * anything, so a long staging hostname never hides `/openai/v1`.
   */
  /**
   * The endpoint as a reader needs it: host first, path always.
   *
   * The scheme is dropped from the display (the copy button and the `title`
   * carry the exact URL) because on a card this narrow it costs seven
   * characters of hostname, and the path is what tells the two planes apart.
   * The host truncates from its end, the path never does: a middle ellipsis
   * in two spans, no width measuring.
   */
  private renderEndpoint(url: string) {
    const withoutScheme = url.replace(/^https?:\/\//, '');
    const separator = withoutScheme.indexOf('/');
    const head =
      separator === -1 ? withoutScheme : withoutScheme.slice(0, separator);
    const tail = separator === -1 ? '' : withoutScheme.slice(separator);
    return html`
      <span class="server-endpoint" title=${url}>
        <span class="endpoint-head">${head}</span
        ><span class="endpoint-tail">${tail}</span>
      </span>
    `;
  }

  /**
   * A dot, a plane, its endpoint and what it did. Green means the plane served
   * something in this window; it is never red, because a failure is a number
   * on the row and not a broken gateway.
   */
  private renderPlaneRow(options: {
    name: string;
    served: boolean;
    url: string;
    copyMessage: string;
    docsHref: string;
    docsLabel: string;
    stats: string[];
    formatSelect?: boolean;
  }) {
    return html`
      <div class="plane-row">
        <span class="plane-name-cell">
          <span
            class="plane-dot ${options.served ? 'served' : ''}"
            aria-hidden="true"
          ></span>
          <span class="plane-name">${options.name}</span>
        </span>
        <span class="plane-endpoint">
          ${
            options.formatSelect
              ? html`<select
                  class="format-select"
                  aria-label="Gateway API format"
                  .value=${this.gatewayFormat}
                  @change=${(event: Event) =>
                    (this.gatewayFormat = (event.target as HTMLSelectElement)
                      .value as GatewayFormat)}
                >
                  <option value="/openai/v1">OpenAI</option>
                  <option value="/anthropic/v1">Anthropic</option>
                  <option value="/google/v1">Gemini</option>
                </select>`
              : nothing
          }
          ${this.renderEndpoint(options.url)}
          <sl-tooltip content="Copy URL">
            <sl-icon-button
              name="clipboard"
              label="Copy URL"
              @click=${() =>
                this.copyEndpoint(options.url, options.copyMessage)}
            ></sl-icon-button>
          </sl-tooltip>
          <sl-tooltip content=${options.docsLabel}>
            <a class="plane-docs" href=${options.docsHref} target="_blank">
              <sl-icon name="info-circle"></sl-icon>
            </a>
          </sl-tooltip>
        </span>
        <span class="plane-stats">
          ${
            options.served
              ? options.stats.join(' · ')
              : html`<span class="plane-quiet">No traffic yet</span>`
          }
        </span>
      </div>
    `;
  }

  /** Nothing is connected yet: one line and the command that changes that. */
  private renderConnectFirstAgent() {
    const command = 'preloop agents onboard';
    return html`
      <div class="connect-first">
        <span>Connect your first agent</span>
        <span class="connect-command">
          <code>${command}</code>
          <sl-tooltip content="Copy command">
            <sl-icon-button
              name="clipboard"
              label="Copy command"
              @click=${() => this.copyEndpoint(command, 'Command copied')}
            ></sl-icon-button>
          </sl-tooltip>
        </span>
        <a class="header-action-link" href="/console/agents">Onboard</a>
      </div>
    `;
  }

  /**
   * Counts on the gateway rows follow the console rule: whole under 1000,
   * compact above it (`13.9K`). The row has to hold two numbers and a path
   * on a card that is a third of the window wide.
   */
  private formatCompactNumber(value: number | null | undefined): string {
    const amount = Number(value || 0);
    if (amount < 1000) {
      return String(Math.round(amount));
    }
    return new Intl.NumberFormat(undefined, {
      notation: 'compact',
      maximumFractionDigits: 1,
    }).format(amount);
  }

  private get modelGatewayStats(): string[] {
    const requests = this.gatewaySummary?.total_requests || 0;
    const failed = this.gatewaySummary?.failed_requests || 0;
    const rateLimited =
      this.rateLimitReport?.totals?.rate_limited_requests || 0;
    // A zero we do not measure is worse than a figure we leave out.
    const stats = [
      `${this.formatCompactNumber(requests)} request${requests === 1 ? '' : 's'}`,
    ];
    if (failed > 0) stats.push(`${this.formatCompactNumber(failed)} failed`);
    if (rateLimited > 0) {
      stats.push(`${this.formatCompactNumber(rateLimited)} rate limited`);
    }
    return stats;
  }

  private get toolFirewallStats(): string[] {
    const calls = this.toolCallsCount || 0;
    const failed = this.failedToolCallsCount || 0;
    const stats = [
      `${this.formatCompactNumber(calls)} tool call${calls === 1 ? '' : 's'}`,
    ];
    if (failed > 0) stats.push(`${this.formatCompactNumber(failed)} failed`);
    return stats;
  }

  /**
   * What the gateway is doing, one row per plane. The five inventory counts
   * that used to live here are the strip under the page title now: this card
   * is about traffic, not about how many things exist.
   */
  private renderGatewayCard() {
    const hasAgents = this.managedAgents.length > 0;
    return html`
      <sl-card class="content-card gateway-card">
        <div slot="header" class="card-header-with-action">
          <div class="chart-header">Gateway</div>
          <div class="gateway-header-meta">
            <span>${this.gatewayRangeLabel}</span>
            ${
              this.apiKeysCount !== null
                ? html`<span
                    >${this.formatNumber(this.apiKeysCount)} active API
                    key${this.apiKeysCount === 1 ? '' : 's'}</span
                  >`
                : nothing
            }
            <a href="/console/settings/api-keys" class="header-action-link"
              >Manage keys</a
            >
          </div>
        </div>

        ${
          hasAgents
            ? html`
                ${this.renderPlaneRow({
                  name: 'Model gateway',
                  served: (this.gatewaySummary?.total_requests || 0) > 0,
                  url: this.modelGatewayUrl,
                  copyMessage: 'Model gateway URL copied',
                  docsHref: 'https://docs.preloop.ai/guide/ai-proxy',
                  docsLabel: 'Model gateway docs',
                  stats: this.modelGatewayStats,
                  formatSelect: true,
                })}
                ${this.renderPlaneRow({
                  name: 'Tool firewall',
                  served: (this.toolCallsCount || 0) > 0,
                  url: this.toolFirewallUrl,
                  copyMessage: 'Tool firewall URL copied',
                  docsHref: 'https://docs.preloop.ai/guide/mcp-server',
                  docsLabel: 'Tool firewall docs',
                  stats: this.toolFirewallStats,
                })}
              `
            : this.renderConnectFirstAgent()
        }
      </sl-card>
    `;
  }

  /**
   * The Inventory tabs read the page's existing fetches; none of them adds a
   * request. Agents come from the agents list, their numbers from the gateway
   * summary's per-session breakdown (the agents list totals are lifetime, the
   * Inventory shows the page range). Flows come from the flows list joined to
   * the executions the page already fetches. Models come from the AI models
   * list joined to `usage_by_model`. Tools come from the tool catalogue joined
   * to `usage_by_tool`.
   */
  private get inventoryAgentRows(): InventoryAgentRow[] {
    const totals = new Map<
      string,
      { requests: number; tokens: number; cost: number }
    >();
    for (const session of this.gatewaySummary?.usage_by_session || []) {
      const agent = this.getManagedAgentForUsageSession(session);
      if (!agent) continue;
      const running = totals.get(agent.id) || {
        requests: 0,
        tokens: 0,
        cost: 0,
      };
      running.requests += session.request_count || 0;
      running.tokens += session.token_usage?.total_tokens || 0;
      running.cost += session.estimated_cost || 0;
      totals.set(agent.id, running);
    }
    return this.managedAgents.map((agent) => {
      const usage = totals.get(agent.id);
      return {
        id: agent.id,
        name: agent.display_name,
        kind: agent.agent_kind || agent.session_source_type || null,
        status: getAgentStatusChip(agent),
        modelAlias: agent.latest_model_alias || agent.configured_model_alias,
        requests: usage?.requests ?? 0,
        tokens: usage?.tokens ?? 0,
        cost: usage?.cost ?? 0,
        lastSeenAt: agent.last_seen_at || agent.last_activity_at || null,
      };
    });
  }

  /**
   * Runs the page range contains, newest first. `$ est.` comes from the
   * range-scoped gateway summary, so runs, failures and the last run are
   * scoped the same way: one row of the table must not hold two time windows.
   */
  private get inRangeFlowExecutions(): FlowExecution[] {
    const startMs = this.getGatewayStartMs();
    return this.flowExecutions.filter((execution) => {
      if (!execution.start_time) return false;
      return parseUTCDate(execution.start_time).getTime() >= startMs;
    });
  }

  /**
   * True when the executions page came back full and its oldest row is still
   * inside the range: the account ran more than one page inside the window, so
   * the counts below are "from the most recent 100 runs", not "in the range".
   */
  private get flowRunsCapped(): boolean {
    if (this.flowExecutions.length < FLOW_EXECUTIONS_PAGE_SIZE) return false;
    const oldest = this.flowExecutions[this.flowExecutions.length - 1];
    if (!oldest?.start_time) return false;
    return (
      parseUTCDate(oldest.start_time).getTime() >= this.getGatewayStartMs()
    );
  }

  private get inventoryFlowRows(): InventoryFlowRow[] {
    const costs = new Map<string, number>();
    for (const flow of this.gatewaySummary?.usage_by_flow || []) {
      if (flow.flow_id) {
        costs.set(flow.flow_id, flow.estimated_cost || 0);
      }
    }

    const runs = new Map<
      string,
      { runs: number; failed: number; last: FlowExecution | null }
    >();
    for (const execution of this.inRangeFlowExecutions) {
      if (!execution.flow_id) continue;
      const running = runs.get(execution.flow_id) || {
        runs: 0,
        failed: 0,
        last: null,
      };
      running.runs += 1;
      if (execution.status === 'FAILED') {
        running.failed += 1;
      }
      // The list arrives newest first, so the first row wins.
      running.last = running.last || execution;
      runs.set(execution.flow_id, running);
    }

    // Names for flows the list no longer holds come from every execution the
    // page fetched, in range or not: a deleted flow's spend is in the summary
    // and the row that carries it needs a name.
    const names = new Map<string, string>();
    for (const execution of this.flowExecutions) {
      if (execution.flow_id && execution.flow_name) {
        names.set(execution.flow_id, execution.flow_name);
      }
    }

    const known = new Set(this.flows.map((flow) => flow.id));
    const listed = this.flows.map((flow) => ({ id: flow.id, name: flow.name }));
    // A run whose flow was deleted still has spend in the window; show it
    // rather than losing the money. Only in-range spend or in-range runs earn
    // the row, so a flow that was deleted before the window opened is gone.
    for (const [flowId, name] of names) {
      if (!known.has(flowId) && (costs.has(flowId) || runs.has(flowId))) {
        listed.push({ id: flowId, name });
      }
    }

    return listed.map((flow) => {
      const counted = runs.get(flow.id);
      return {
        id: flow.id,
        name: flow.name,
        lastRun: counted?.last
          ? {
              id: counted.last.id,
              status: counted.last.status,
              start_time: counted.last.start_time,
              end_time: counted.last.end_time,
              trigger_subject: counted.last.trigger_subject,
              trigger_subject_url: counted.last.trigger_subject_url,
              trigger_event_details: counted.last.trigger_event_details,
              failure_category: counted.last.failure_category,
            }
          : null,
        runs: counted?.runs ?? 0,
        failed: counted?.failed ?? 0,
        cost: costs.get(flow.id) ?? 0,
      };
    });
  }

  /**
   * One row per configured model, plus the aliases the gateway still serves.
   *
   * Configured models come from the batch overview, which joins usage on the
   * model id. The account breakdown is only consulted for aliases that no
   * longer map to a configured model, so a model renamed at the gateway no
   * longer reads as zero traffic next to a phantom row for its old name.
   */
  private get inventoryModelRows(): InventoryModelRow[] {
    const overview = new Map(
      this.aiModelOverview.map((item) => [item.ai_model_id, item])
    );
    const knownModelIds = new Set(this.aiModels.map((model) => model.id));

    const rows: InventoryModelRow[] = this.aiModels.map((model) => {
      const used = overview.get(model.id);
      return {
        id: model.id,
        alias: model.name,
        provider: model.provider_name || 'Unknown',
        requests: used?.total_requests ?? 0,
        tokens: used?.token_usage?.total_tokens ?? 0,
        cost: used?.estimated_cost ?? 0,
      };
    });

    // Aliases the gateway served that are no longer in the models list.
    const seenAliases = new Set<string>();
    for (const model of this.gatewaySummary?.usage_by_model || []) {
      const alias = model.model_alias || model.ai_model_id;
      if (!alias || seenAliases.has(alias)) continue;
      if (model.ai_model_id && knownModelIds.has(model.ai_model_id)) continue;
      if (this.aiModels.some((known) => known.name === alias)) continue;
      seenAliases.add(alias);
      rows.push({
        id: model.ai_model_id,
        alias,
        provider: model.provider_name || 'Unknown',
        requests: model.request_count || 0,
        tokens: model.token_usage?.total_tokens || 0,
        cost: model.estimated_cost || 0,
      });
    }
    return rows;
  }

  /**
   * One row per active teammate. Spend reaches a person through the agents
   * they own, which is the only link the gateway records between a request
   * and a human; flows carry no owner at all, so there is no flows column.
   */
  private get inventoryUserRows(): InventoryUserRow[] {
    const perAgent = new Map<string, { tokens: number; cost: number }>();
    for (const session of this.gatewaySummary?.usage_by_session || []) {
      const agent = this.getManagedAgentForUsageSession(session);
      if (!agent) continue;
      const running = perAgent.get(agent.id) || { tokens: 0, cost: 0 };
      running.tokens += session.token_usage?.total_tokens || 0;
      running.cost += session.estimated_cost || 0;
      perAgent.set(agent.id, running);
    }

    const byOwner = new Map<
      string,
      { agents: number; tokens: number; cost: number }
    >();
    for (const agent of this.managedAgents) {
      const ownerId = agent.owner_user_id;
      if (!ownerId) continue;
      const running = byOwner.get(ownerId) || { agents: 0, tokens: 0, cost: 0 };
      running.agents += 1;
      const usage = perAgent.get(agent.id);
      running.tokens += usage?.tokens || 0;
      running.cost += usage?.cost || 0;
      byOwner.set(ownerId, running);
    }

    return this.accountUsers.map((user) => {
      const owned = byOwner.get(user.id);
      const roles = [...(user.roles || []), ...(user.inherited_roles || [])]
        .map((role) => role?.name)
        .filter((name): name is string => Boolean(name));
      return {
        id: user.id,
        name: user.full_name || user.username || user.email || 'Unknown',
        role: [...new Set(roles)].join(', '),
        lastLoginAt: user.last_login || null,
        agentsOwned: owned?.agents ?? 0,
        tokens: owned?.tokens ?? 0,
        cost: owned?.cost ?? 0,
      };
    });
  }

  private get inventoryToolRows(): InventoryToolRow[] {
    const usage = new Map<string, GatewayUsageByTool>();
    for (const tool of this.gatewaySummary?.usage_by_tool || []) {
      usage.set(tool.tool_name, tool);
    }
    return this.tools
      .filter((tool) => tool.is_enabled)
      .map((tool) => {
        const used = usage.get(tool.name);
        return {
          name: tool.name,
          server: tool.source_name || 'Built in',
          calls: used?.invocation_count ?? 0,
          failed: used?.failed_invocations ?? 0,
        };
      });
  }

  /**
   * True while the usage columns of the Agents and Users tabs have nothing
   * true to show. A breakdown already in hand (from the cache, or from the
   * range before this one) is shown rather than hidden: stale numbers that
   * are about to be replaced beat skeletons over data the page already has.
   */
  private get usageColumnsPending(): boolean {
    return (
      this.fetchingUsageBreakdown && !hasUsageBreakdown(this.gatewaySummary)
    );
  }

  /**
   * One box for what the account has: the counts that used to sit in the
   * stat strip now label its tabs, and the page range drives every column.
   */
  private renderInventoryCard() {
    return html`
      <inventory-card
        .agentRows=${this.inventoryAgentRows}
        .flowRows=${this.inventoryFlowRows}
        .modelRows=${this.inventoryModelRows}
        .toolRows=${this.inventoryToolRows}
        .userRows=${this.inventoryUserRows}
        .agentsTotal=${this.totalAgentsCount}
        .flowsTotal=${this.totalFlowsCount}
        .modelsTotal=${this.aiModelsCount}
        .toolsTotal=${this.enabledToolsCount}
        .usersTotal=${this.enabledUsersCount}
        ?showUsers=${this.canViewUsers}
        .rangeLabel=${this.gatewayRangeLabel}
        .flowRunsCapped=${this.flowRunsCapped}
        ?loading=${this.loading || this.fetchingMCPAndTools}
        .loadingAgents=${this.loading}
        .loadingFlows=${this.loading || this.fetchingInventory}
        .loadingModels=${this.loading || this.fetchingMCPAndTools}
        .loadingTools=${this.loading || this.fetchingMCPAndTools}
        .loadingUsers=${this.loading || this.fetchingUsers}
        ?usageLoading=${this.usageColumnsPending}
      ></inventory-card>
    `;
  }

  /** What just happened, live, with the audit page one click away. */
  private renderActivityFeed() {
    return html`
      <activity-feed
        .flows=${this.flows}
        .agents=${this.managedAgents}
        .executions=${this.flowExecutions}
        .budgetPolicies=${this.budgetPolicies}
        .users=${this.accountUsers}
        ?usersFromHost=${this.fetchesUsers}
        @open-budget-limits=${() => (this.showBudgetDialog = true)}
      ></activity-feed>
    `;
  }

  render() {
    if (!this.isOnboarded && !this.welcomeCardDismissed) {
      return html`
        <div
          class="extra-wide"
          style="margin-top: var(--sl-spacing-large); display: flex; justify-content: center; min-height: 80vh;"
        >
          ${this.renderWelcomeCard()}
        </div>
        <preloop-invite-dialog
          ?open=${this.isInviteDialogOpen}
          @close=${() => {
            this.isInviteDialogOpen = false;
          }}
        ></preloop-invite-dialog>
      `;
    }

    return html`
      <view-header headerText="Overview" width="extra-wide">
        <span
          slot="meta"
          class="updated-at"
          title=${
            this.lastUpdatedAt
              ? parseUTCDate(this.lastUpdatedAt).toLocaleString()
              : 'Not loaded yet'
          }
          >Updated ${this.renderLastUpdated()}</span
        >
      </view-header>
      <div class="extra-wide" style="margin-bottom: var(--sl-spacing-large);">
        ${
          this.error
            ? html`<sl-alert variant="danger" open>${this.error}</sl-alert>`
            : nothing
        }
        ${this.renderAttentionStrip()} ${this.renderWelcomeCard()}
      </div>

      <div class="column-layout dashboard extra-wide">
        <div class="main-column">
          <div class="dashboard-stack">
            ${this.renderGatewayCard()} ${this.renderNextStepsCard()}
            ${this.renderInventoryCard()}

            <div
              style="display: grid; grid-template-columns: 1fr 1fr; gap: var(--sl-spacing-medium);"
            >
              ${this.renderGatewayFailuresCard()}
              ${this.renderAuditExceptionsCard()}
            </div>
          </div>
        </div>

        <div class="side-column">
          ${this.renderUsageCard()} ${this.renderActivityFeed()}
        </div>
        <mcp-setup-dialog
          ?open=${this.showSetupDialog}
          @close=${() => (this.showSetupDialog = false)}
        ></mcp-setup-dialog>
        <budget-limits-dialog
          ?open=${this.showBudgetDialog}
          billingEnabled
          @budget-limits-hide=${() => (this.showBudgetDialog = false)}
          @budget-policies-changed=${this.handleBudgetPoliciesChanged}
        ></budget-limits-dialog>
        <preloop-invite-dialog
          ?open=${this.isInviteDialogOpen}
          @close=${() => {
            this.isInviteDialogOpen = false;
          }}
        ></preloop-invite-dialog>
      </div>
    `;
  }
}
