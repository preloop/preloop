import { css, html, nothing, unsafeCSS } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import { repeat } from 'lit/directives/repeat.js';

import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/card/card.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/progress-bar/progress-bar.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import '@shoelace-style/shoelace/dist/components/skeleton/skeleton.js';
import '../../components/agent-talk-composer.ts';
import '../../components/mcp-setup-dialog.ts';
import '../../components/budget-limits-dialog.ts';
import '../../components/time-range-select.ts';
import '../../components/usage-card.ts';
import '../../components/view-header.ts';
import {
  AuthedElement,
  fetchWithAuth,
  getAIModels,
  getAccountAgents,
  getAccountGatewayUsageSearch,
  getAccountGatewayUsageSummary,
  getAccountRuntimeSessions,
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
} from '../../api';
import '../../components/preloop-invite-dialog';
import '../../components/preloop-flow-form';
import '../../components/add-ai-model-modal';
import '../../components/preloop-deploy-wizard';
import { isSaaS } from '../../brand-config';
import { normalizeObservedSession } from '../../utils/session-observer';
import { renderAgentIcon } from '../../utils/agent-icons';
import {
  getAgentSourceLabel,
  renderAgentIdentityBadges,
  sessionBelongsToAgent,
} from '../../utils/agent-display';
import {
  ATTENTION_KIND_META,
  deriveAttentionItems,
  type AttentionInputs,
  type AttentionItem,
} from '../../utils/attention';
import { loadAttentionInputs } from '../../utils/attention-data';
import { unifiedWebSocketManager } from '../../services/unified-websocket-manager';
import type {
  AccountGatewayUsageSummaryResponse,
  GatewayUsageBySession,
  GatewayUsageSearchResultItem,
  ManagedAgentSummary,
  RuntimeSessionSummary,
  AIModel,
} from '../../types';
import { parseUTCDate } from '../../utils/date';
import { executionDurationText } from '../../utils/execution';
import {
  TOP_MODEL_GROUP_PREVIEW_LIMIT,
  TOP_MODEL_SESSION_PREVIEW_LIMIT,
  compareByUsageMetric,
  mergeGatewaySummaryPreservingBreakdown,
  previewWindow,
} from '../../utils/top-models-overview';
import { getAgentControlState } from '../../utils/agent-control';
import {
  pickDefaultModel,
  selectableModels,
} from '../../utils/ai-model-selection';
import type { Tool } from '../../components/tool-card';
import consoleStyles from '../../styles/console-styles.css?inline';
import { reducedMotionStyles } from '../../styles/reduced-motion';

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

interface FlowExecution {
  id: string;
  flow_id: string;
  flow_name?: string;
  status: string;
  start_time: string;
  end_time: string | null;
  error_message: string | null;
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

interface DashboardMetric {
  label: string;
  value: string | number;
  icon: string;
  href?: string;
  tone?: 'primary' | 'neutral' | 'success' | 'warning' | 'danger';
}

@customElement('dashboard-view')
export class DashboardView extends AuthedElement {
  private initialLoadTime = Date.now();
  @state() private loading = true;
  @state() private fetchingGatewaySummary = true;
  @state() private fetchingRecentExecutions = true;
  @state() private fetchingApprovals = true;
  @state() private fetchingAudit = true;
  @state() private fetchingMCPAndTools = true;
  @state() private error: string | null = null;
  @state() private gatewaySummary: AccountGatewayUsageSummaryResponse | null =
    null;
  @state() private runtimeSessions: RuntimeSessionSummary[] = [];
  @state() private managedAgents: ManagedAgentSummary[] = [];
  @state() private budgetAgents: ManagedAgentSummary[] = [];
  @state() private gatewayInteractions: GatewayUsageSearchResultItem[] = [];
  @state() private auditGroups: AuditGroup[] = [];
  @state() private trackers: Tracker[] = [];
  @state() private totalIssues = 0;
  @state() private mcpServers: MCPServer[] = [];
  @state() private tools: Tool[] = [];
  @state() private recentFlowExecutions: FlowExecution[] = [];
  @state() private flowExecutionsCount = 0;
  @state() private failedExecutionsCount = 0;
  @state() private succeededFlowExecutionsCount = 0;
  @state() private pendingApprovals: ApprovalRequest[] = [];
  @state() private lastUpdatedAt: string | null = null;
  @state() private hasFlows = false;
  @state() private hasAIModels = false;
  @state() private aiModelsCount = 0;
  @state() private enabledUsersCount = 0;
  @state() private toolCallsCount = 0;
  @state() private failedToolCallsCount = 0;
  @state() private totalFlowsCount = 0;
  @state() private totalAgentsCount = 0;
  @state() private totalRuntimeSessionsCount = 0;
  @state() private gatewayTimeRange: 'day' | 'week' | 'month' | 'year' =
    'month';
  @state() private fetchingBudget = false;
  @state() private activeAgentsTimeRange: '5m' | '1h' | '1d' | '1w' | '1mo' =
    '1d';
  @state() private fetchingActiveAgents = false;
  @state() private topModelsSortMetric: 'spend' | 'usage' = 'spend';
  @state() private expandedOverviewGroups = new Set<string>();
  @state() private expandedPreviewKeys = new Set<string>();
  @state() private showSetupDialog = false;
  @state() private showBudgetDialog = false;
  @state() private welcomeCardDismissed = false;

  @state() private aiModels: AIModel[] = [];
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
  private refreshTimer: number | null = null;
  private refreshInFlight = false;

  /** Start time plus, when known, how long the run took or has been going. */
  private executionSecondaryText(exec: FlowExecution): string {
    const started = this.formatDate(exec.start_time);
    const duration = executionDurationText(exec);
    return duration ? `${started} · ${duration}` : started;
  }

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
      .tool-counts {
        display: flex;
        flex-wrap: wrap;
        gap: var(--sl-spacing-2x-large);
        margin-top: var(--sl-spacing-small);
        justify-content: center;
      }
      .tool-count {
        display: flex;
        flex-direction: column;
        align-items: center;
        gap: var(--sl-spacing-small);
        font-size: var(--sl-font-size-small);
      }
      .tool-count sl-icon {
        font-size: 2.5rem;
      }
      .tool-count-value {
        font-size: 1.5rem;
        font-weight: 700;
      }
      .tool-count-label {
        font-size: var(--sl-font-size-small);
        color: var(--sl-color-neutral-600);
        text-align: center;
      }
      .hover-underline:hover {
        text-decoration: underline;
      }
      .metrics-grid {
        display: grid;
        grid-template-columns: repeat(5, minmax(0, 1fr));
        gap: var(--sl-spacing-large);
        row-gap: var(--sl-spacing-2x-large);
        align-items: start;
        margin-bottom: var(--sl-spacing-2x-large);
      }
      .tool-count-value {
        font-variant-numeric: tabular-nums;
      }
      .updated-at {
        color: var(--sl-color-neutral-500);
        font-size: var(--sl-font-size-small);
        font-weight: var(--sl-font-weight-normal);
      }
      .capsule-eyebrow {
        color: var(--sl-color-neutral-500);
        font-size: var(--sl-font-size-x-small);
        font-weight: 600;
        letter-spacing: 0.04em;
        text-transform: uppercase;
      }
      .range-note {
        color: var(--sl-color-neutral-500);
        font-size: var(--sl-font-size-small);
        font-weight: var(--sl-font-weight-normal);
        font-variant-numeric: tabular-nums;
      }
      .attention-title {
        align-items: center;
        display: flex;
        gap: var(--sl-spacing-2x-small);
      }
      .attention-title sl-icon {
        color: var(--sl-color-warning-600);
      }
      .attention-row {
        align-items: flex-start;
        display: flex;
        gap: var(--sl-spacing-x-small);
      }
      .severity-dot {
        border-radius: 50%;
        flex-shrink: 0;
        height: 8px;
        margin-top: 6px;
        width: 8px;
        background: var(--sl-color-warning-600);
      }
      .severity-dot.critical {
        background: var(--sl-color-danger-600);
      }
      .attention-kind-icon {
        color: var(--sl-color-neutral-500);
        flex-shrink: 0;
        margin-top: 3px;
      }
      .attention-main {
        display: flex;
        flex-direction: column;
        gap: 2px;
        min-width: 0;
      }
      .attention-detail {
        color: var(--sl-color-neutral-500);
        font-size: var(--sl-font-size-x-small);
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .attention-more {
        color: var(--sl-color-primary-600);
        display: inline-block;
        font-size: var(--sl-font-size-small);
        margin-top: var(--sl-spacing-small);
        text-decoration: none;
      }
      .attention-more:hover {
        text-decoration: underline;
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
        box-shadow: 0 0 0 1px var(--sl-color-neutral-0);
      }
      .budget-hard-marker {
        position: absolute;
        top: 0;
        right: 0;
        bottom: 0;
        width: 2px;
        background: var(--sl-color-danger-600);
      }
      .expandable-group {
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-2x-small);
      }
      .expandable-header {
        align-items: flex-start;
        cursor: pointer;
        display: flex;
        gap: var(--sl-spacing-x-small);
        user-select: none;
      }
      .expand-icon {
        color: var(--sl-color-neutral-500);
        flex-shrink: 0;
        margin-top: 2px;
        transition: transform 0.2s ease;
      }
      .expand-icon.open {
        transform: rotate(90deg);
      }
      .expandable-leading {
        align-items: flex-start;
        display: flex;
        flex: 1;
        gap: var(--sl-spacing-small);
        min-width: 0;
      }
      .expandable-identity {
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-3x-small);
        min-width: 0;
      }
      .expandable-identity .row-primary {
        display: block;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .agent-meta-line {
        color: var(--sl-color-neutral-500);
        font-size: var(--sl-font-size-x-small);
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .agent-identity-badges {
        display: flex;
        flex-wrap: wrap;
        gap: var(--sl-spacing-2x-small);
      }
      .agent-identity-badges sl-badge {
        max-width: 100%;
      }
      .expandable-trailing {
        align-items: center;
        display: flex;
        flex-shrink: 0;
        gap: var(--sl-spacing-x-small);
        margin-left: auto;
      }
      .expandable-content {
        border-left: 2px solid var(--sl-color-neutral-200);
        display: flex;
        flex-direction: column;
        gap: 4px;
        margin-left: calc(1rem + var(--sl-spacing-x-small));
        padding-left: var(--sl-spacing-medium);
      }
      .expandable-subheader {
        align-items: center;
        cursor: pointer;
        display: flex;
        gap: var(--sl-spacing-x-small);
        user-select: none;
      }
      .expandable-subheader .row-primary {
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .overview-nested-groups {
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-2x-small);
        margin-top: var(--sl-spacing-2x-small);
      }
      .overview-see-more {
        align-self: flex-start;
        margin-top: 2px;
      }
      .expandable-subheader-metric {
        color: var(--sl-color-neutral-500);
        flex-shrink: 0;
        font-size: var(--sl-font-size-x-small);
        margin-left: auto;
        white-space: nowrap;
      }
      .nested-session-list {
        display: flex;
        flex-direction: column;
        gap: 4px;
      }
      .nested-session-row {
        align-items: center;
        display: flex;
        font-size: var(--sl-font-size-small);
        gap: var(--sl-spacing-small);
        justify-content: space-between;
      }
      .nested-session-row a {
        min-width: 0;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .nested-session-metric {
        color: var(--sl-color-neutral-500);
        flex-shrink: 0;
        font-size: var(--sl-font-size-x-small);
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

      .loading-container {
        display: flex;
        justify-content: center;
        padding: var(--sl-spacing-2x-large);
      }

      .dashboard-stack {
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-large);
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
      .empty-state,
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

      .row-value,
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

      .empty-state {
        border: 1px dashed var(--sl-color-neutral-300);
        border-radius: var(--sl-border-radius-medium);
        padding: var(--sl-spacing-large);
        text-align: center;
        background: var(--sl-color-neutral-0);
      }

      /* Failed flows: visible but calm — accent border only, no red wash */
      .item-card.failed-execution {
        background: var(--sl-color-neutral-50);
        border-left-color: #ff5d5d;
      }
      .item-card.failed-execution .item-error {
        color: #ff5d5d;
        font-style: normal;
      }

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

        .row-value,
        .summary-item strong {
          text-align: left;
        }

        .analytics-grid {
          grid-template-columns: 1fr;
        }

        .metrics-grid {
          grid-template-columns: repeat(3, minmax(0, 1fr));
          gap: var(--sl-spacing-medium);
          row-gap: var(--sl-spacing-large);
        }

        .tool-count {
          min-width: 0;
        }

        .tool-count-value {
          font-size: clamp(1rem, 7vw, 1.5rem);
          overflow-wrap: anywhere;
        }

        .tool-count-label {
          font-size: var(--sl-font-size-x-small);
          line-height: 1.25;
        }
      }
    `,
  ];

  connectedCallback() {
    super.connectedCallback();
    this.initialLoadTime = Date.now(); // Reset load time on DOM connection to gate initial WebSocket reloads
    this.loadDismissedState();
    this.loadCachedDashboardData();
    void this.fetchDashboardData();
    this.connectRealtime();
  }

  private async fetchAdminStatus() {
    try {
      const user = await getUserProfile();
      this.isAdmin = user?.is_superuser || false;
    } catch (error) {
      console.error('Failed to fetch user profile:', error);
      this.isAdmin = false;
    }
  }

  private async fetchFeatures() {
    try {
      const res = await getFeatures();
      this.computeFeatureEnabled = !!res.features?.['compute'];
      this.isEnterprise = Array.isArray(res.plugins) && res.plugins.length > 0;
      return res;
    } catch {
      this.computeFeatureEnabled = false;
      this.isEnterprise = false;
      return null;
    }
  }

  disconnectedCallback(): void {
    super.disconnectedCallback();
    this.unsubscribeRealtime?.();
    if (this.refreshTimer !== null) {
      window.clearTimeout(this.refreshTimer);
      this.refreshTimer = null;
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
      this.recentFlowExecutions = data.recentFlowExecutions || [];
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

  private saveDashboardCache(): void {
    try {
      const sub = this.getUsernameFromToken();
      if (sub === 'anonymous') return;
      const key = `preloop:dashboard:${sub}`;
      const cacheObj = {
        gatewaySummary: this.gatewaySummary,
        runtimeSessions: this.runtimeSessions,
        managedAgents: this.managedAgents,
        gatewayInteractions: this.gatewayInteractions,
        auditGroups: this.auditGroups,
        trackers: this.trackers,
        totalIssues: this.totalIssues,
        mcpServers: this.mcpServers,
        tools: this.tools,
        recentFlowExecutions: this.recentFlowExecutions,
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
        attentionInputs: this.attentionInputs,
        budgetPolicies: this.budgetPolicies,
        budgetAgents: this.budgetAgents,
      };
      sessionStorage.setItem(key, JSON.stringify(cacheObj));
    } catch (e) {
      console.warn('Failed to save dashboard cache to sessionStorage', e);
    }
  }

  private loadDismissedState(): void {
    this.welcomeCardDismissed =
      localStorage.getItem('dashboard_welcome_dismissed') === 'true';
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

  private connectRealtime(): void {
    const scheduleRefresh = () => this.scheduleRefresh();
    const unsubscribers = [
      unifiedWebSocketManager.subscribe('runtime_sessions', scheduleRefresh),
      unifiedWebSocketManager.subscribe('managed_agents', scheduleRefresh),
      unifiedWebSocketManager.subscribe('gateway_activity', scheduleRefresh),
      unifiedWebSocketManager.subscribe('budget_health', scheduleRefresh),
      unifiedWebSocketManager.subscribe('audit', scheduleRefresh),
      unifiedWebSocketManager.subscribe('approvals', scheduleRefresh),
      unifiedWebSocketManager.subscribe('flow_executions', scheduleRefresh),
      unifiedWebSocketManager.subscribe(
        'system',
        scheduleRefresh,
        (message) => message?.type === 'authenticated'
      ),
    ];
    this.unsubscribeRealtime = () => {
      for (const unsubscribe of unsubscribers) {
        unsubscribe();
      }
    };
    void unifiedWebSocketManager.connect();
  }

  private scheduleRefresh(): void {
    const timeSinceLoad = Date.now() - this.initialLoadTime;
    if (timeSinceLoad < 5000) {
      // Skip redundant WebSocket auth/event reloads on initial page load
      return;
    }
    if (this.lastUpdatedAt) {
      const elapsed = Date.now() - new Date(this.lastUpdatedAt).getTime();
      if (elapsed < 5000) {
        // Skip redundant WebSocket auth reload on initial page load
        return;
      }
    }
    if (this.refreshTimer !== null) {
      window.clearTimeout(this.refreshTimer);
    }
    this.refreshTimer = window.setTimeout(() => {
      this.refreshTimer = null;
      void this.fetchDashboardData({ preserveLoadingState: true });
    }, 250);
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

  private getActiveAgentsStartDate(): string | undefined {
    const now = new Date();
    if (this.activeAgentsTimeRange === '5m') {
      const d = new Date(now);
      d.setMinutes(d.getMinutes() - 5);
      return d.toISOString();
    }
    if (this.activeAgentsTimeRange === '1h') {
      const d = new Date(now);
      d.setHours(d.getHours() - 1);
      return d.toISOString();
    }
    if (this.activeAgentsTimeRange === '1d') {
      const d = new Date(now);
      d.setDate(d.getDate() - 1);
      return d.toISOString();
    }
    if (this.activeAgentsTimeRange === '1w') {
      const d = new Date(now);
      d.setDate(d.getDate() - 7);
      return d.toISOString();
    }
    if (this.activeAgentsTimeRange === '1mo') {
      const d = new Date(now);
      d.setMonth(d.getMonth() - 1);
      return d.toISOString();
    }
    return undefined;
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

  private async fetchActiveAgentsData() {
    this.fetchingActiveAgents = true;
    try {
      const startDateStr = this.getActiveAgentsStartDate();
      const runtimeSessionsParams: {
        status: 'all';
        limit: number;
        startDate?: string;
      } = { status: 'all', limit: 100 };
      if (startDateStr) {
        runtimeSessionsParams.startDate = startDateStr;
      }

      const [runtimeSessions, managedAgents] = await Promise.all([
        this.catchWith403Handling(
          getAccountRuntimeSessions(runtimeSessionsParams),
          { items: [] } as Awaited<ReturnType<typeof getAccountRuntimeSessions>>
        ),
        this.catchWith403Handling(
          getAccountAgents({ status: 'all', limit: 100 }),
          {
            items: [],
          } as Awaited<ReturnType<typeof getAccountAgents>>
        ),
      ]);

      this.runtimeSessions = runtimeSessions.items || [];
      this.totalRuntimeSessionsCount =
        runtimeSessions.total ?? this.runtimeSessions.length;
      this.applyAgentsList(managedAgents);
      this.saveDashboardCache();
    } catch (error) {
      console.error('Failed to load active agents data', error);
    } finally {
      this.fetchingActiveAgents = false;
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
      this.saveDashboardCache();
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

  private async fetchDashboardData(
    options: { preserveLoadingState?: boolean } = {}
  ) {
    if (this.refreshInFlight) {
      return;
    }
    this.refreshInFlight = true;

    if (!options.preserveLoadingState) {
      this.fetchingGatewaySummary = true;
      this.fetchingRecentExecutions = true;
      this.fetchingApprovals = true;
      this.fetchingActiveAgents = true;
      this.fetchingBudget = true;
      this.fetchingAudit = true;
      this.fetchingMCPAndTools = true;
      this.loading = true;
    }
    this.error = null;

    const startDateStr = this.getGatewayStartDate();
    const attentionPromise = this.refreshAttentionInputs();

    try {
      // Wave 1 (above-the-fold): gateway metrics, budget, recent executions,
      // active agents, approvals, features/admin — each unique resource once.
      const agentsPromise = this.catchWith403Handling(
        getAccountAgents({ status: 'all', limit: 100 }),
        {
          items: [],
          total: 0,
        } as Awaited<ReturnType<typeof getAccountAgents>>
      );
      const gatewaySummaryPromise = this.catchWith403Handling(
        getAccountGatewayUsageSummary({
          startDate: startDateStr,
          includeBreakdown: false,
        }),
        null
      );
      const featuresPromise = this.fetchFeatures();
      const adminPromise = this.fetchAdminStatus();

      const [
        gatewaySummary,
        gatewayInteractions,
        flows,
        flowExecutions,
        pendingApprovals,
        allApprovalRequests,
        runtimeSessions,
        managedAgents,
        featuresRes,
      ] = await Promise.all([
        gatewaySummaryPromise,
        this.catchWith403Handling(getAccountGatewayUsageSearch({ limit: 12 }), {
          items: [],
        } as Awaited<ReturnType<typeof getAccountGatewayUsageSearch>>),
        this.catchWith403Handling(getFlows(), [] as any[]),
        this.catchWith403Handling(
          getFlowExecutions({ limit: 10 }),
          [] as FlowExecution[]
        ),
        this.catchWith403Handling(
          this.fetchApprovalRequests('pending', 100),
          []
        ),
        this.catchWith403Handling(
          this.fetchApprovalRequests(undefined, 100),
          []
        ),
        this.catchWith403Handling(
          getAccountRuntimeSessions({
            status: 'all',
            limit: 100,
            startDate: this.getActiveAgentsStartDate(),
          }),
          {
            items: [],
          } as Awaited<ReturnType<typeof getAccountRuntimeSessions>>
        ),
        agentsPromise,
        featuresPromise,
      ]);
      await adminPromise;

      this.gatewaySummary = mergeGatewaySummaryPreservingBreakdown(
        this.gatewaySummary,
        gatewaySummary
      );
      this.gatewayInteractions = gatewayInteractions.items || [];
      this.fetchingGatewaySummary = false;

      this.hasFlows = (flows || []).length > 0;
      this.totalFlowsCount = (flows || []).length;
      const sortedFlowExecutions = [...(flowExecutions || [])].sort(
        (left, right) =>
          new Date(right.start_time).getTime() -
          new Date(left.start_time).getTime()
      );
      this.flowExecutionsCount = sortedFlowExecutions.length;
      this.failedExecutionsCount = sortedFlowExecutions.filter(
        (execution) => execution.status === 'FAILED'
      ).length;
      this.succeededFlowExecutionsCount = sortedFlowExecutions.filter(
        (execution) =>
          execution.status === 'SUCCEEDED' || execution.status === 'COMPLETED'
      ).length;
      this.recentFlowExecutions = sortedFlowExecutions.slice(0, 5);
      this.fetchingRecentExecutions = false;

      this.pendingApprovals = pendingApprovals.filter((approval) =>
        this.isUnexpiredPendingApproval(approval)
      );
      this.calculateApprovalStats(allApprovalRequests);
      this.fetchingApprovals = false;

      this.runtimeSessions = runtimeSessions.items || [];
      this.totalRuntimeSessionsCount =
        runtimeSessions.total ?? this.runtimeSessions.length;
      this.applyAgentsList(managedAgents);
      this.fetchingActiveAgents = false;

      await this.fetchBudgetSummary({
        sharedAgents: managedAgents,
        features: featuresRes,
      });

      await attentionPromise;

      this.lastUpdatedAt = new Date().toISOString();
      this.loading = false;
      this.saveDashboardCache();

      // Wave 2 is slow (full session breakdown + audit/tools). On a live
      // websocket refresh only upgrade the top-models breakdown so the card
      // does not flash empty while the rest of the dashboard stays put.
      if (options.preserveLoadingState) {
        void this.refreshTopModelsBreakdown(startDateStr).then(() =>
          this.saveDashboardCache()
        );
      } else {
        void this.fetchSecondaryDashboardData(startDateStr);
      }
    } catch (error) {
      console.error(
        'Failed to complete background loading of overview dashboard',
        error
      );
      this.error = 'Failed to load some overview dashboard data.';
      this.fetchingGatewaySummary = false;
      this.fetchingRecentExecutions = false;
      this.fetchingApprovals = false;
      this.fetchingActiveAgents = false;
      this.fetchingBudget = false;
      this.fetchingAudit = false;
      this.fetchingMCPAndTools = false;
      this.loading = false;
    } finally {
      this.refreshInFlight = false;
    }
  }

  private async fetchSecondaryDashboardData(gatewayStartDate: string) {
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

    const pTools = (async () => {
      try {
        const [mcpServers, tools, aiModels, users] = await Promise.all([
          this.catchWith403Handling(getMCPServers(), [] as MCPServer[]),
          this.catchWith403Handling(getTools(), [] as Tool[]),
          this.catchWith403Handling(getAIModels(), []),
          this.catchWith403Handling(
            isSaaS()
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
          ),
        ]);
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
        this.enabledUsersCount = Array.isArray(users.users)
          ? users.users.filter((u: { is_active?: boolean }) => u.is_active)
              .length
          : 0;
      } catch (error) {
        console.error('Failed to load MCP and tools data', error);
      } finally {
        this.fetchingMCPAndTools = false;
      }
    })();

    await Promise.all([
      pAudit,
      pTools,
      this.refreshTopModelsBreakdown(gatewayStartDate),
    ]);
    this.saveDashboardCache();
  }

  private async refreshTopModelsBreakdown(gatewayStartDate: string) {
    try {
      const detailed = await this.catchWith403Handling(
        getAccountGatewayUsageSummary({
          startDate: gatewayStartDate,
          includeBreakdown: true,
        }),
        null
      );
      if (!detailed) {
        return;
      }
      this.gatewaySummary = detailed;
    } catch (error) {
      console.error('Failed to load gateway breakdown for top models', error);
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

  private get activeAgents(): ManagedAgentSummary[] {
    return [...this.managedAgents].sort(
      (left, right) =>
        new Date(right.last_seen_at).getTime() -
        new Date(left.last_seen_at).getTime()
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
    return this.recentFlowExecutions.filter(
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

  private formatRuntimeSessionId(value: string | null | undefined): string {
    if (!value) {
      return 'Session';
    }
    return value.length > 8 ? value.substring(0, 8) : value;
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

  private renderNestedSessionRow(
    session: GatewayUsageBySession | RuntimeSessionSummary,
    metric: string
  ) {
    return html`
      <div class="nested-session-row">
        <a class="row-link" href=${this.getSessionDetailHref(session)}>
          ${this.getSessionDisplayTitle(session)}
        </a>
        <span class="nested-session-metric">${metric}</span>
      </div>
    `;
  }

  private toggleOverviewGroup(groupId: string) {
    const next = new Set(this.expandedOverviewGroups);
    if (next.has(groupId)) {
      next.delete(groupId);
    } else {
      next.add(groupId);
    }
    this.expandedOverviewGroups = next;
  }

  private isOverviewGroupExpanded(groupId: string): boolean {
    return this.expandedOverviewGroups.has(groupId);
  }

  private togglePreviewKey(key: string) {
    const next = new Set(this.expandedPreviewKeys);
    if (next.has(key)) {
      next.delete(key);
    } else {
      next.add(key);
    }
    this.expandedPreviewKeys = next;
  }

  private isPreviewExpanded(key: string): boolean {
    return this.expandedPreviewKeys.has(key);
  }

  private renderSeeMoreControl(key: string, overflow: number) {
    if (overflow <= 0) {
      return nothing;
    }
    const expanded = this.isPreviewExpanded(key);
    return html`
      <sl-button
        class="overview-see-more"
        size="small"
        variant="text"
        @click=${(event: Event) => {
          event.preventDefault();
          event.stopPropagation();
          this.togglePreviewKey(key);
        }}
      >
        ${expanded ? 'Show less' : `See ${overflow} more`}
        <sl-icon
          slot="suffix"
          name=${expanded ? 'chevron-up' : 'chevron-down'}
        ></sl-icon>
      </sl-button>
    `;
  }

  private nestedSessionRepeatKey(
    session: GatewayUsageBySession | RuntimeSessionSummary
  ): string {
    if ('total_requests' in session) {
      return session.id;
    }
    return (
      session.runtime_session_id ||
      session.flow_execution_id ||
      session.session_source_id ||
      `${session.model_alias}-${session.last_request_at}`
    );
  }

  private renderCappedNestedSessions(
    sessions: Array<GatewayUsageBySession | RuntimeSessionSummary>,
    previewKey: string
  ) {
    const sorted = [...sessions].sort((left, right) =>
      compareByUsageMetric(left, right, this.topModelsSortMetric)
    );
    const { visible, overflow } = previewWindow(
      sorted,
      TOP_MODEL_SESSION_PREVIEW_LIMIT,
      this.isPreviewExpanded(previewKey)
    );
    return html`
      <div class="nested-session-list">
        ${repeat(
          visible,
          (session) => this.nestedSessionRepeatKey(session),
          (session) =>
            this.renderNestedSessionRow(
              session,
              `${this.formatNumber(
                'total_requests' in session
                  ? session.total_requests
                  : session.request_count
              )} req`
            )
        )}
        ${this.renderSeeMoreControl(previewKey, overflow)}
      </div>
    `;
  }

  private renderExpandIcon(groupId: string) {
    return html`
      <sl-icon
        class="expand-icon ${
          this.isOverviewGroupExpanded(groupId) ? 'open' : ''
        }"
        name="chevron-right"
      ></sl-icon>
    `;
  }

  private renderFlowIcon(): ReturnType<typeof html> {
    // flow.svg strokes with currentColor, so it adapts to light/dark on its
    // own — do not apply the inverting .flow-icon filter here.
    return html`<sl-icon
      src="/images/flow.svg"
      style="font-size: 1rem; color: var(--sl-color-neutral-600);"
    ></sl-icon>`;
  }

  private renderExpandableSubGroup(
    groupId: string,
    name: string,
    href: string | null,
    summaryMetric: string,
    content: ReturnType<typeof html>,
    icon: ReturnType<typeof html> | null = null
  ) {
    const expanded = this.isOverviewGroupExpanded(groupId);
    return html`
      <div class="expandable-group">
        <div
          class="expandable-subheader"
          @click=${() => this.toggleOverviewGroup(groupId)}
        >
          ${this.renderExpandIcon(groupId)}
          ${
            icon
              ? html`<span
                  class="group-icon"
                  aria-hidden="true"
                  style="display: inline-flex; align-items: center; margin-right: var(--sl-spacing-2x-small);"
                  >${icon}</span
                >`
              : nothing
          }
          ${
            href
              ? html`<a
                  class="row-link row-primary"
                  href=${href}
                  @click=${(event: Event) => event.stopPropagation()}
                  >${name}</a
                >`
              : html`<span class="row-primary">${name}</span>`
          }
          <span class="expandable-subheader-metric">${summaryMetric}</span>
        </div>
        ${expanded ? html`<div class="expandable-content">${content}</div>` : nothing}
      </div>
    `;
  }

  private renderAgentExpandableGroup(
    groupId: string,
    agent: ManagedAgentSummary,
    sessions: Array<GatewayUsageBySession | RuntimeSessionSummary>,
    trailingMetric: string,
    options: {
      showTalkComposer?: boolean;
      onAgentControlSent?: () => void;
      nestedPreviewKey?: string;
    } = {}
  ) {
    const expanded = this.isOverviewGroupExpanded(groupId);
    const agentKind = agent.agent_kind || agent.session_source_type;
    const showTalkComposer = options.showTalkComposer ?? false;

    return html`
      <div class="expandable-group">
        <div
          class="expandable-header"
          @click=${() => this.toggleOverviewGroup(groupId)}
        >
          ${this.renderExpandIcon(groupId)}
          <div class="expandable-leading">
            ${renderAgentIcon(
              agentKind,
              'font-size: 1.25rem; color: var(--sl-color-neutral-800); flex-shrink: 0;'
            )}
            <div class="expandable-identity">
              <a
                class="row-link row-primary"
                href=${`/console/agents/${agent.id}`}
                @click=${(event: Event) => event.stopPropagation()}
              >
                ${agent.display_name || 'Agent'}
              </a>
              <div class="agent-meta-line">
                ${getAgentSourceLabel(agentKind)}${
                  agent.session_source_id ? ` · ${agent.session_source_id}` : ''
                }
              </div>
              ${renderAgentIdentityBadges(agent)}
            </div>
          </div>
          <div
            class="expandable-trailing"
            @click=${(event: Event) => event.stopPropagation()}
          >
            ${
              showTalkComposer && getAgentControlState(agent).visible
                ? html`
                    <agent-talk-composer
                      .agent=${agent}
                      .sessions=${sessions}
                      sourceContext="dashboard-active-agents"
                      compact
                      @agent-control-sent=${() =>
                        options.onAgentControlSent?.()}
                    ></agent-talk-composer>
                  `
                : null
            }
            <span class="row-value">${trailingMetric}</span>
          </div>
        </div>
        ${
          expanded
            ? html`
                <div class="expandable-content">
                  ${
                    sessions.length > 0
                      ? options.nestedPreviewKey
                        ? this.renderCappedNestedSessions(
                            sessions,
                            options.nestedPreviewKey
                          )
                        : html`
                            <div class="nested-session-list">
                              ${sessions.map((session) =>
                                this.renderNestedSessionRow(
                                  session,
                                  `${this.formatNumber(
                                    'total_requests' in session
                                      ? session.total_requests
                                      : session.request_count
                                  )} req`
                                )
                              )}
                            </div>
                          `
                      : html`<div class="nested-session-metric">
                          No recent sessions
                        </div>`
                  }
                </div>
              `
            : nothing
        }
      </div>
    `;
  }

  private renderActiveAgentGroup(
    agent: ManagedAgentSummary,
    sessions: RuntimeSessionSummary[]
  ) {
    return this.renderAgentExpandableGroup(
      `active-agent:${agent.id}`,
      agent,
      sessions,
      `${this.formatCurrency(agent.estimated_cost)} · ${this.formatRelativeTime(agent.last_seen_at)}`,
      {
        showTalkComposer: true,
        onAgentControlSent: () => this.fetchActiveAgentsData(),
      }
    );
  }

  private renderCollapsibleGroup(
    groupId: string,
    name: string,
    href: string | null,
    summaryMetric: string,
    content: ReturnType<typeof html>,
    icon: ReturnType<typeof html> | null = null
  ) {
    return this.renderExpandableSubGroup(
      groupId,
      name,
      href,
      summaryMetric,
      content,
      icon
    );
  }

  private formatRelativeTime(value: string | null | undefined): string {
    if (!value) {
      return 'Never';
    }
    const timestamp = parseUTCDate(value).getTime();
    const deltaMinutes = Math.round((Date.now() - timestamp) / 60000);
    if (deltaMinutes < 1) {
      return 'just now';
    }
    if (deltaMinutes < 60) {
      return `${deltaMinutes}m ago`;
    }
    const deltaHours = Math.round(deltaMinutes / 60);
    if (deltaHours < 24) {
      return `${deltaHours}h ago`;
    }
    return `${Math.round(deltaHours / 24)}d ago`;
  }

  private formatLastUpdatedLabel(): string {
    if (this.lastUpdatedAt) {
      return this.formatRelativeTime(this.lastUpdatedAt);
    }
    if (
      this.loading ||
      this.fetchingGatewaySummary ||
      this.fetchingRecentExecutions ||
      this.fetchingBudget ||
      this.fetchingActiveAgents
    ) {
      return 'Loading…';
    }
    return 'Never';
  }

  private getStatusColor(status: string): string {
    switch (status.toLowerCase()) {
      case 'active':
      case 'succeeded':
      case 'approved':
        return 'success';
      case 'failed':
      case 'error':
      case 'declined':
        return 'danger';
      case 'running':
      case 'pending':
        return 'warning';
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
    return deriveAttentionItems(this.attentionInputs);
  }

  /**
   * Same loader, same parameters as the Attention page. Runs alongside the
   * dashboard fetch rather than inside it so a slow attention input never
   * holds up the cards above the fold.
   */
  private async refreshAttentionInputs(): Promise<void> {
    try {
      this.attentionInputs = await loadAttentionInputs();
      this.saveDashboardCache();
    } catch (error) {
      console.error('Failed to load attention inputs', error);
    }
  }

  private get gatewayRangeLabel(): string {
    if (this.gatewayTimeRange === 'day') return '24h';
    if (this.gatewayTimeRange === 'week') return '7d';
    if (this.gatewayTimeRange === 'year') return '1y';
    return '30d';
  }

  private get enabledToolsCount(): number {
    return this.tools.filter((tool) => tool.is_enabled).length;
  }

  private get activeAgentsCount(): number {
    return this.managedAgents.filter(
      (agent) =>
        agent.is_active_now ||
        agent.activity_status === 'active_now' ||
        agent.activity_status === 'recently_active'
    ).length;
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

  private isFlowBackedUsageSession(session: GatewayUsageBySession): boolean {
    return Boolean(
      session.flow_execution_id ||
      session.flow_id ||
      session.session_source_type === 'flow_execution' ||
      session.runtime_principal_type === 'flow_execution'
    );
  }

  private isAgentBackedUsageSession(
    session: GatewayUsageBySession,
    agent?: ManagedAgentSummary
  ): boolean {
    return Boolean(
      agent ||
      session.agent_id ||
      session.session_source_type === 'managed_agent' ||
      session.runtime_principal_type === 'managed_agent'
    );
  }

  private getUsageSessionSubject(
    session: GatewayUsageBySession
  ): UsageSessionSubject {
    if (this.isFlowBackedUsageSession(session)) {
      return {
        kind: 'flow',
        name: session.flow_name || session.runtime_principal_name || 'Flow',
        href: session.flow_id
          ? `/console/flows/${session.flow_id}`
          : '/console/flows',
      };
    }

    const agent = this.getManagedAgentForUsageSession(session);
    if (this.isAgentBackedUsageSession(session, agent)) {
      const agentId =
        agent?.id ||
        session.agent_id ||
        (session.session_source_type === 'managed_agent'
          ? session.session_source_id
          : null) ||
        (session.runtime_principal_type === 'managed_agent'
          ? session.runtime_principal_id
          : null);
      return {
        kind: 'agent',
        name:
          agent?.display_name ||
          session.agent_name ||
          session.runtime_principal_name ||
          'Managed agent',
        href: agentId ? `/console/agents/${agentId}` : '/console/agents',
      };
    }

    return {
      kind: 'session',
      name: this.getSessionDisplayTitle(session),
      href: session.runtime_session_id
        ? `/console/runtime-sessions?sessionId=${session.runtime_session_id}`
        : '/console/runtime-sessions',
    };
  }

  private renderEmptyState(message: string) {
    return html`<div class="empty-state">${message}</div>`;
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

  private renderRecentFlowExecutionsCard() {
    if (
      this.fetchingRecentExecutions &&
      this.recentFlowExecutions.length === 0
    ) {
      return nothing;
    }

    return html`
      <!-- Flow Executions -->
      <sl-card>
        <div slot="header" class="chart-header">
          <sl-icon name="diagram-3"></sl-icon>
          Recent Flow Executions
          ${
            this.failedFlowExecutions.length > 0
              ? html`<sl-badge variant="danger"
                  >${this.failedFlowExecutions.length} failed</sl-badge
                >`
              : ''
          }
        </div>

        ${
          this.recentFlowExecutions.length === 0
            ? html`
                <div class="empty-state">
                  <sl-icon name="inbox"></sl-icon>
                  <p>
                    No flow executions yet.
                    <a href="/console/flows">Create a flow</a>
                  </p>
                </div>
              `
            : html`
                <div class="item-list">
                  ${this.recentFlowExecutions.slice(0, 5).map(
                    (exec) => html`
                      <div
                        class="item-card ${
                          exec.status === 'FAILED' ? 'failed-execution' : ''
                        }"
                      >
                        <div class="item-info">
                          <span class="item-name"
                            >${exec.flow_name || 'Unnamed Flow'}</span
                          >
                          ${
                            exec.error_message
                              ? html`<span
                                  class="item-error"
                                  title=${exec.error_message}
                                  >${exec.error_message}</span
                                >`
                              : ''
                          }
                          <span class="item-secondary"
                            >${this.executionSecondaryText(exec)}</span
                          >
                        </div>
                        <div class="item-actions">
                          <sl-tag
                            size="small"
                            variant="${this.getStatusColor(exec.status)}"
                          >
                            ${exec.status}
                          </sl-tag>
                          <sl-button
                            size="small"
                            href="/console/flows/executions/${exec.id}"
                          >
                            View
                          </sl-button>
                        </div>
                      </div>
                    `
                  )}
                </div>

                <div class="quick-actions">
                  <sl-button size="small" href="/console/flows/executions">
                    <sl-icon slot="prefix" name="list"></sl-icon>
                    View All Executions
                  </sl-button>
                  <sl-button size="small" href="/console/flows">
                    <sl-icon slot="prefix" name="plus-circle"></sl-icon>
                    Create Flow
                  </sl-button>
                </div>
              `
        }
      </sl-card>
    `;
  }
  private renderUsageCard() {
    return html`
      <usage-card
        .summary=${this.gatewaySummary}
        .policies=${this.budgetPolicies}
        .loading=${this.fetchingGatewaySummary || this.fetchingBudget}
        .error=${this.error}
        .timeRange=${this.gatewayTimeRange}
        .toolCallsCount=${this.toolCallsCount}
        @range-change=${(event: CustomEvent<{ value: string }>) => {
          event.stopPropagation();
          this.gatewayTimeRange = event.detail.value as
            'day' | 'week' | 'month' | 'year';
          void this.fetchDashboardData({ preserveLoadingState: true });
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
                  <sl-badge variant="danger">${item.status_code}</sl-badge>
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

  private collectTopModelSubjectGroups(
    modelKey: string,
    modelSessions: GatewayUsageBySession[]
  ): TopModelSubjectGroup[] {
    const agentGroups = new Map<
      string,
      { subject: UsageSessionSubject; sessions: GatewayUsageBySession[] }
    >();
    const flowGroups = new Map<
      string,
      { subject: UsageSessionSubject; sessions: GatewayUsageBySession[] }
    >();
    const otherSessions: GatewayUsageBySession[] = [];

    modelSessions.forEach((session) => {
      const subject = this.getUsageSessionSubject(session);
      if (subject.kind === 'agent') {
        const agent = this.getManagedAgentForUsageSession(session);
        const key =
          agent?.id ||
          session.agent_id ||
          session.runtime_session_id ||
          session.session_source_id ||
          session.runtime_principal_id ||
          subject.href;
        if (!agentGroups.has(key)) {
          agentGroups.set(key, { subject, sessions: [] });
        }
        agentGroups.get(key)!.sessions.push(session);
      } else if (subject.kind === 'flow') {
        const key = session.flow_id || session.flow_name || subject.href;
        if (!flowGroups.has(key)) {
          flowGroups.set(key, { subject, sessions: [] });
        }
        flowGroups.get(key)!.sessions.push(session);
      } else {
        otherSessions.push(session);
      }
    });

    const groups: TopModelSubjectGroup[] = [];
    const pushGroup = (
      kind: TopModelSubjectGroup['kind'],
      groupId: string,
      subject: UsageSessionSubject,
      sessions: GatewayUsageBySession[]
    ) => {
      groups.push({
        groupId,
        kind,
        subject,
        sessions,
        totalCost: sessions.reduce((acc, row) => acc + row.estimated_cost, 0),
        totalRequests: sessions.reduce(
          (acc, row) => acc + row.request_count,
          0
        ),
      });
    };

    agentGroups.forEach(({ subject, sessions }, groupKey) => {
      pushGroup(
        'agent',
        `top-model:${modelKey}:agent:${groupKey}`,
        subject,
        sessions
      );
    });
    flowGroups.forEach(({ subject, sessions }, groupKey) => {
      pushGroup(
        'flow',
        `top-model:${modelKey}:flow:${groupKey}`,
        subject,
        sessions
      );
    });
    if (otherSessions.length > 0) {
      pushGroup(
        'other',
        `top-model:${modelKey}:other`,
        { kind: 'session', name: 'Other', href: '/console/runtime-sessions' },
        otherSessions
      );
    }

    groups.sort((left, right) =>
      compareByUsageMetric(
        {
          estimated_cost: left.totalCost,
          request_count: left.totalRequests,
        },
        {
          estimated_cost: right.totalCost,
          request_count: right.totalRequests,
        },
        this.topModelsSortMetric
      )
    );
    return groups;
  }

  private renderTopModelSubjectGroup(group: TopModelSubjectGroup) {
    const trailingMetric = `${this.formatCurrency(group.totalCost)} (${this.formatNumber(group.totalRequests)} req)`;
    const nestedPreviewKey = `${group.groupId}:sessions`;
    const nested = this.renderCappedNestedSessions(
      group.sessions,
      nestedPreviewKey
    );

    if (group.kind === 'agent') {
      const agent = this.getManagedAgentForUsageSession(group.sessions[0]);
      if (agent) {
        return this.renderAgentExpandableGroup(
          group.groupId,
          agent,
          group.sessions,
          trailingMetric,
          { nestedPreviewKey }
        );
      }
    }

    return this.renderCollapsibleGroup(
      group.groupId,
      group.subject.name,
      group.kind === 'other' ? null : group.subject.href,
      trailingMetric,
      nested,
      group.kind === 'flow' ? this.renderFlowIcon() : null
    );
  }

  private renderTopModelsSkeleton() {
    return html`
      <sl-card class="content-card">
        <div slot="header" class="card-header-with-action">
          <div class="card-title">Top Models</div>
        </div>
        <div class="list">
          ${[0, 1, 2, 3].map(
            () => html`
              <div class="row">
                <sl-skeleton
                  effect="sheen"
                  style="width: 60%; height: 0.9rem;"
                ></sl-skeleton>
                <sl-skeleton
                  effect="sheen"
                  style="width: 35%; height: 0.75rem; margin-top: var(--sl-spacing-2x-small);"
                ></sl-skeleton>
              </div>
            `
          )}
        </div>
      </sl-card>
    `;
  }

  private renderTopModelsCard() {
    const rawModels = this.gatewaySummary?.usage_by_model || [];
    if (rawModels.length === 0) {
      return this.fetchingGatewaySummary
        ? this.renderTopModelsSkeleton()
        : nothing;
    }

    const aggregatedModels = new Map<string, any>();
    rawModels.forEach((m) => {
      const key = `${m.model_alias}-${m.provider_name}`;
      if (!aggregatedModels.has(key)) {
        aggregatedModels.set(key, { ...m });
      } else {
        const existing = aggregatedModels.get(key)!;
        existing.request_count += m.request_count;
        existing.estimated_cost += m.estimated_cost;
        existing.prompt_tokens =
          (existing.prompt_tokens || 0) + (m.prompt_tokens || 0);
        existing.completion_tokens =
          (existing.completion_tokens || 0) + (m.completion_tokens || 0);
        if (m.ai_model_id && !existing.ai_model_id) {
          existing.ai_model_id = m.ai_model_id;
        }
      }
    });

    const models = Array.from(aggregatedModels.values());
    models.sort((a, b) => compareByUsageMetric(a, b, this.topModelsSortMetric));

    const allSessions = this.gatewaySummary?.usage_by_session || [];

    return html`
      <sl-card class="content-card">
        <div slot="header" class="card-header-with-action">
          <div style="display: flex; align-items: center; gap: 4px;">
            Top Models
            <select
              style="background: transparent; border: none; font-size: inherit; font-weight: inherit; font-family: inherit; color: inherit; cursor: pointer; outline: none; padding: 0;"
              .value=${this.topModelsSortMetric}
              @change=${(e: Event) => {
                this.topModelsSortMetric = (e.target as HTMLSelectElement)
                  .value as any;
              }}
            >
              <option value="spend">by Spend</option>
              <option value="usage">by Usage</option>
            </select>
            <span class="range-note">· ${this.gatewayRangeLabel}</span>
          </div>
          <div
            style="display: flex; gap: var(--sl-spacing-small); align-items: center;"
          >
            <a href="/console/ai-models" class="header-action-link"
              >Model fleet</a
            >
          </div>
        </div>
        <div class="list">
          ${repeat(
            models.slice(0, 6),
            (item) =>
              `${item.ai_model_id}-${item.model_alias}-${item.provider_name}`,
            (item) => {
              const modelKey = `${item.ai_model_id}-${item.model_alias}-${item.provider_name}`;
              const modelSessions = allSessions.filter(
                (s) =>
                  (s.ai_model_id &&
                    item.ai_model_id &&
                    s.ai_model_id === item.ai_model_id) ||
                  (s.model_alias === item.model_alias &&
                    (!s.provider_name ||
                      !item.provider_name ||
                      s.provider_name === item.provider_name))
              );
              const groups = this.collectTopModelSubjectGroups(
                modelKey,
                modelSessions
              );
              const groupsPreviewKey = `top-model-groups:${modelKey}`;
              const { visible, overflow } = previewWindow(
                groups,
                TOP_MODEL_GROUP_PREVIEW_LIMIT,
                this.isPreviewExpanded(groupsPreviewKey)
              );

              return html`
                <div
                  class="row"
                  style="flex-direction: column; align-items: stretch; gap: var(--sl-spacing-2x-small);"
                >
                  <div
                    style="display: flex; justify-content: space-between; align-items: center;"
                  >
                    <div
                      style="display: flex; align-items: center; gap: var(--sl-spacing-2x-small); overflow: hidden;"
                    >
                      ${
                        item.ai_model_id
                          ? html`
                              <a
                                class="row-link row-primary"
                                href=${`/console/ai-models/${item.ai_model_id}`}
                                style="white-space: nowrap; overflow: hidden; text-overflow: ellipsis;"
                              >
                                ${item.model_alias || 'Unknown model'}
                              </a>
                            `
                          : html`
                              <span
                                class="row-primary"
                                style="white-space: nowrap; overflow: hidden; text-overflow: ellipsis;"
                              >
                                ${item.model_alias || 'Unknown model'}
                              </span>
                            `
                      }
                    </div>
                    <span class="row-value">
                      ${
                        this.topModelsSortMetric === 'usage'
                          ? `${this.formatNumber(item.request_count)} requests`
                          : this.formatCurrency(item.estimated_cost)
                      }
                    </span>
                  </div>
                  <div
                    style="display: flex; justify-content: space-between; align-items: center; font-size: var(--sl-font-size-x-small); color: var(--sl-color-neutral-500);"
                  >
                    <span>${item.provider_name || 'provider unknown'}</span>
                    <span
                      >${
                        this.topModelsSortMetric === 'spend'
                          ? `${this.formatNumber(item.request_count)} requests`
                          : this.formatCurrency(item.estimated_cost)
                      }</span
                    >
                  </div>

                  ${
                    groups.length > 0
                      ? html`
                          <div class="overview-nested-groups">
                            ${repeat(
                              visible,
                              (group) => group.groupId,
                              (group) => this.renderTopModelSubjectGroup(group)
                            )}
                            ${this.renderSeeMoreControl(
                              groupsPreviewKey,
                              overflow
                            )}
                          </div>
                        `
                      : ''
                  }
                </div>
              `;
            }
          )}
        </div>
      </sl-card>
    `;
  }

  private renderSessionsAttentionCard() {
    if (
      this.fetchingGatewaySummary &&
      (this.gatewaySummary?.usage_by_session || []).length === 0
    ) {
      return html`
        <sl-card class="content-card">
          <div slot="header" class="card-header-with-action">
            Top sessions by usage
          </div>
          <div
            class="loading-container"
            style="padding: var(--sl-spacing-2x-large); display: flex; justify-content: center; align-items: center;"
          >
            <sl-spinner style="font-size: 1.5rem;"></sl-spinner>
          </div>
        </sl-card>
      `;
    }
    const items = this.gatewaySummary?.usage_by_session || [];
    if (!this.fetchingGatewaySummary && items.length === 0) {
      return nothing;
    }

    return html`
      <sl-card class="content-card">
        <div slot="header" class="card-header-with-action">
          Top sessions by usage
          <div
            style="display: flex; gap: var(--sl-spacing-small); align-items: center;"
          >
            <a href="/console/runtime-sessions" class="header-action-link"
              >Model fleet</a
            >
          </div>
        </div>
        <div class="list">
          ${repeat(
            items.slice(0, 6),
            (item) =>
              item.runtime_session_id ||
              `${item.session_source_type}-${item.session_source_id}-${item.model_alias}`,
            (item) => html`
              <div class="row">
                <div class="row-main">
                  <a
                    class="row-link row-primary"
                    href=${
                      item.runtime_session_id
                        ? `/console/runtime-sessions?sessionId=${item.runtime_session_id}`
                        : '/console/runtime-sessions'
                    }
                  >
                    ${this.getSessionDisplayTitle(item)}
                  </a>
                  <span class="row-value">
                    ${this.formatCurrency(item.estimated_cost)}
                  </span>
                </div>
                <div class="row-meta">
                  <span>
                    ${item.model_alias || item.provider_name || 'model unknown'}
                  </span>
                  <span>${this.formatDateTime(item.last_request_at)}</span>
                </div>
              </div>
            `
          )}
        </div>
      </sl-card>
    `;
  }

  private sessionBelongsToAgent(
    session: RuntimeSessionSummary,
    agent: ManagedAgentSummary
  ): boolean {
    const base = agent.session_source_id;
    const candidates = [
      session.runtime_principal_id,
      session.session_source_id,
    ].filter((value): value is string => Boolean(value));
    return candidates.some(
      (id) =>
        id === base ||
        id === agent.id ||
        (Boolean(base) &&
          (id.startsWith(`${base}:`) || id.startsWith(`${base}-`)))
    );
  }

  private renderActiveExecutionsCard() {
    if (this.managedAgents.length === 0 && this.runtimeSessions.length === 0) {
      return nothing;
    }

    const agentsWithSessions: Array<{
      agent: (typeof this.activeAgents)[0];
      sessions: typeof this.activeSessions;
    }> = [];
    const usedSessionIds = new Set<string>();

    for (const agent of this.activeAgents) {
      const sessions = this.activeSessions.filter((s) =>
        this.sessionBelongsToAgent(s, agent)
      );
      if (sessions.length > 0) {
        agentsWithSessions.push({ agent, sessions });
      }
      sessions.forEach((s) => usedSessionIds.add(s.id));
    }

    // Sort agents by last activity
    agentsWithSessions.sort((a, b) => {
      const aTime = a.agent.last_seen_at
        ? new Date(a.agent.last_seen_at).getTime()
        : 0;
      const bTime = b.agent.last_seen_at
        ? new Date(b.agent.last_seen_at).getTime()
        : 0;
      return bTime - aTime;
    });

    const unmatchedSessions = this.activeSessions.filter(
      (s) => !usedSessionIds.has(s.id)
    );

    // Flow-execution sessions group under their flow; only sessions with no
    // resolvable agent AND no flow remain as true "Other".
    const flowGroupMap = new Map<
      string,
      {
        name: string;
        flowId: string | null;
        sessions: typeof unmatchedSessions;
      }
    >();
    const orphanSessions: typeof unmatchedSessions = [];
    for (const s of unmatchedSessions) {
      const isFlow = Boolean(
        s.flow_id ||
        s.flow_execution_id ||
        s.runtime_principal_type === 'flow_execution' ||
        s.session_source_type === 'flow_execution'
      );
      if (isFlow) {
        const key = s.flow_id || s.flow_name || 'flow';
        if (!flowGroupMap.has(key)) {
          flowGroupMap.set(key, {
            name: s.flow_name || s.runtime_principal_name || 'Flow',
            flowId: s.flow_id || null,
            sessions: [],
          });
        }
        flowGroupMap.get(key)!.sessions.push(s);
      } else {
        orphanSessions.push(s);
      }
    }
    const flowGroups = Array.from(flowGroupMap.values()).sort(
      (a, b) => b.sessions.length - a.sessions.length
    );
    orphanSessions.sort((a, b) => {
      const aTime =
        a.last_activity_at || a.started_at
          ? new Date(a.last_activity_at || a.started_at).getTime()
          : 0;
      const bTime =
        b.last_activity_at || b.started_at
          ? new Date(b.last_activity_at || b.started_at).getTime()
          : 0;
      return bTime - aTime;
    });

    const hasAnyExecutions =
      agentsWithSessions.length > 0 ||
      flowGroups.length > 0 ||
      orphanSessions.length > 0;

    return html`
      <sl-card class="content-card">
        <div slot="header" class="card-header-with-action">
          <div
            class="card-title"
            style="display: flex; align-items: center; gap: var(--sl-spacing-2x-small);"
          >
            Active agents
            ${
              this.fetchingActiveAgents
                ? html`<sl-spinner
                    style="font-size: 1rem; width: 1rem; height: 1rem;"
                  ></sl-spinner>`
                : ''
            }
          </div>
          <time-range-select
            style="margin-left: auto; margin-right: var(--sl-spacing-small);"
            ariaLabel="Active agents time range"
            .value=${this.activeAgentsTimeRange}
            .options=${[
              { value: '5m', label: '5m' },
              { value: '1h', label: '1h' },
              { value: '1d', label: '1d' },
              { value: '1w', label: '1w' },
              { value: '1mo', label: '1mo' },
            ]}
            @range-change=${(event: CustomEvent<{ value: string }>) => {
              event.stopPropagation();
              this.activeAgentsTimeRange = event.detail.value as
                '5m' | '1h' | '1d' | '1w' | '1mo';
              this.fetchActiveAgentsData();
            }}
          ></time-range-select>
          <div
            style="display: flex; gap: var(--sl-spacing-small); align-items: center;"
          >
            <a href="/console/runtime-sessions" class="header-action-link"
              >View all</a
            >
          </div>
        </div>
        ${
          this.fetchingActiveAgents && !hasAnyExecutions
            ? html`<div
                class="loading-container"
                style="padding: var(--sl-spacing-small); display: flex; justify-content: center; align-items: center;"
              >
                <sl-spinner style="font-size: 1.5rem;"></sl-spinner>
              </div>`
            : html`
                <div class="list">
                  ${
                    !hasAnyExecutions
                      ? this.renderEmptyState('No active agents right now.')
                      : ''
                  }
                  ${repeat(
                    agentsWithSessions.slice(0, 8),
                    (item) => item.agent.id,
                    (item) => html`
                      <div class="row">
                        ${this.renderActiveAgentGroup(item.agent, item.sessions)}
                      </div>
                    `
                  )}
                  ${repeat(
                    flowGroups.slice(0, 8),
                    (group) => group.flowId || group.name,
                    (group) => html`
                      <div class="row">
                        ${this.renderCollapsibleGroup(
                          `active-agents:flow:${group.flowId || group.name}`,
                          group.name,
                          group.flowId
                            ? `/console/flows/${group.flowId}`
                            : '/console/flows',
                          `${this.formatCurrency(
                            group.sessions.reduce(
                              (total, session) =>
                                total + (session.estimated_cost || 0),
                              0
                            )
                          )} · ${group.sessions.length} exec`,
                          html`
                            <div class="nested-session-list">
                              ${group.sessions
                                .slice(0, 8)
                                .map((session) =>
                                  this.renderNestedSessionRow(
                                    session,
                                    `${this.formatNumber(session.total_requests)} req`
                                  )
                                )}
                            </div>
                          `,
                          this.renderFlowIcon()
                        )}
                      </div>
                    `
                  )}
                  ${
                    orphanSessions.length > 0
                      ? html`
                          <div class="row">
                            ${this.renderCollapsibleGroup(
                              'active-agents:other',
                              'Other',
                              null,
                              `${this.formatCurrency(
                                orphanSessions.reduce(
                                  (total, session) =>
                                    total + (session.estimated_cost || 0),
                                  0
                                )
                              )} · ${orphanSessions.length} sessions`,
                              html`
                                <div class="nested-session-list">
                                  ${orphanSessions
                                    .slice(0, 8)
                                    .map((session) =>
                                      this.renderNestedSessionRow(
                                        session,
                                        `${this.formatNumber(session.total_requests)} req`
                                      )
                                    )}
                                </div>
                              `
                            )}
                          </div>
                        `
                      : ''
                  }
                </div>
              `
        }
      </sl-card>
    `;
  }

  private renderAttentionCard() {
    const items = this.attentionItems;
    if (items.length === 0) {
      return nothing;
    }
    const visible = items.slice(0, 5);
    const overflow = items.length - visible.length;

    return html`
      <sl-card class="content-card">
        <div slot="header" class="card-header-with-action">
          <div class="card-title attention-title">
            <sl-icon name="exclamation-triangle"></sl-icon>
            Needs attention
            <sl-badge variant="warning" pill>${items.length}</sl-badge>
          </div>
          <a href="/console/attention" class="header-action-link">View all</a>
        </div>
        <div class="list">
          ${repeat(
            visible,
            (item) => item.id,
            (item) => html`
              <div class="row attention-row">
                <span
                  class="severity-dot ${item.severity}"
                  aria-hidden="true"
                ></span>
                <sl-icon
                  class="attention-kind-icon"
                  name=${ATTENTION_KIND_META[item.kind].icon}
                  aria-hidden="true"
                ></sl-icon>
                <div class="attention-main">
                  <a class="row-link row-primary" href=${item.href}
                    >${item.title}</a
                  >
                  <span class="attention-detail">${item.detail}</span>
                </div>
              </div>
            `
          )}
        </div>
        ${
          overflow > 0
            ? html`<a class="attention-more" href="/console/attention"
                >and ${overflow} more</a
              >`
            : nothing
        }
      </sl-card>
    `;
  }

  private renderMetricItem(metric: DashboardMetric) {
    const iconColor =
      metric.tone === 'danger'
        ? 'var(--sl-color-danger-600)'
        : metric.tone === 'warning'
          ? 'var(--sl-color-warning-600)'
          : metric.tone === 'success'
            ? 'var(--sl-color-success-600)'
            : metric.tone === 'primary'
              ? 'var(--sl-color-primary-600)'
              : 'var(--sl-color-neutral-400)';
    const content = html`
      ${
        metric.icon.includes('/')
          ? html`<sl-icon
              src=${metric.icon}
              style="color: ${iconColor};"
            ></sl-icon>`
          : html`<sl-icon
              name=${metric.icon}
              style="color: ${iconColor};"
            ></sl-icon>`
      }
      <div class="tool-count-value">${metric.value}</div>
      <div class="tool-count-label ${metric.href ? 'hover-underline' : ''}">
        ${metric.label}
      </div>
    `;

    if (metric.href) {
      return html`
        <a
          class="tool-count"
          href=${metric.href}
          style="color: inherit; text-decoration: none;"
          >${content}</a
        >
      `;
    }

    return html`<div class="tool-count">${content}</div>`;
  }

  private get gatewayMetrics(): DashboardMetric[] {
    const attentionCount = this.attentionItems.length;
    return [
      {
        label: 'agents',
        value: this.formatNumber(this.totalAgentsCount),
        icon: 'robot',
        href: '/console/agents',
        tone: 'primary',
      },
      {
        label: 'flows',
        value: this.formatNumber(this.totalFlowsCount),
        icon: '/images/flow.svg',
        href: '/console/flows',
        tone: 'primary',
      },
      {
        label: 'models',
        value: this.formatNumber(this.aiModelsCount),
        icon: 'cpu',
        href: '/console/ai-models',
        tone: 'primary',
      },
      {
        label: 'tools',
        value: this.formatNumber(this.enabledToolsCount),
        icon: 'tools',
        href: '/console/tools',
        tone: 'primary',
      },
      {
        label: 'need attention',
        value: this.formatNumber(attentionCount),
        icon: attentionCount > 0 ? 'exclamation-triangle' : 'check-circle',
        href: '/console/attention',
        tone: attentionCount > 0 ? 'warning' : 'success',
      },
    ];
  }

  private renderPreloopGatewayCard() {
    return html`
      <!-- Preloop Gateway Status -->
      <sl-card class="content-card">
        <div slot="header" class="card-header-with-action">
          <div class="chart-header">
            <sl-icon
              src="/assets/preloop-badge.svg"
              slot="prefix"
              class="mcp-icon"
              alt="Gateway"
            ></sl-icon>
            Preloop Gateway
            <sl-tooltip
              content="Unified proxy for AI models and MCP tools with budget and access controls"
            >
              <sl-icon name="question-circle"></sl-icon>
            </sl-tooltip>
            <span class="updated-at"
              >Updated ${this.formatLastUpdatedLabel()}</span
            >
          </div>
          <div
            style="display: flex; gap: var(--sl-spacing-small); align-items: center;"
          >
            <a href="/console/settings/api-keys" class="header-action-link"
              >Manage Keys</a
            >
          </div>
        </div>

        <div class="metrics-grid">
          ${
            this.fetchingGatewaySummary && !this.gatewaySummary
              ? html`
                  <div
                    style="grid-column: 1 / -1; display: flex; justify-content: center; align-items: center; padding: var(--sl-spacing-2x-large);"
                  >
                    <sl-spinner style="font-size: 2rem;"></sl-spinner>
                  </div>
                `
              : html`
                  <div style="display: contents;">
                    ${this.gatewayMetrics.map((metric) =>
                      this.renderMetricItem(metric)
                    )}
                  </div>
                `
          }
        </div>
        <!-- AI Model Gateway Endpoint -->
        <div
          class="mcp-server-capsule"
          style="margin-top: 0; margin-bottom: var(--sl-spacing-small);"
        >
          <div class="status-indicator"></div>
          <span class="capsule-eyebrow">Model gateway</span>
          <sl-badge variant="primary" size="small" style="margin-right: -4px;"
            >AI models</sl-badge
          >
          <div
            class="server-details"
            style="display: flex; gap: var(--sl-spacing-small); align-items: center;"
          >
            <span
              style="font-size: var(--sl-font-size-x-small); font-weight: 600; color: var(--sl-color-neutral-500); text-transform: uppercase;"
              >Format:</span
            >
            <select
              aria-label="Gateway API Format"
              style="background: transparent; border: 1px solid var(--sl-color-neutral-300); border-radius: var(--sl-border-radius-small); padding: 1px 6px; font-size: inherit; font-family: inherit; color: var(--sl-color-primary-600); cursor: pointer; outline: none; margin-right: var(--sl-spacing-2x-small);"
              @change=${(e: Event) => {
                const target = e.target as HTMLSelectElement;
                const endpointSpan = target.parentElement?.querySelector(
                  '.server-endpoint'
                ) as HTMLElement;
                if (endpointSpan) {
                  endpointSpan.innerText = `${window.location.origin}${target.value}`;
                }
              }}
            >
              <option value="/openai/v1">OpenAI</option>
              <option value="/anthropic/v1">Anthropic</option>
              <option value="/google/v1">Gemini</option>
            </select>
            <span class="server-endpoint"
              >${window.location.origin}/openai/v1</span
            >
            <a
              href="https://docs.preloop.ai/guide/ai-proxy"
              target="_blank"
              style="display: flex; color: var(--sl-color-neutral-500); margin-left: auto;"
            >
              <sl-icon name="info-circle"></sl-icon>
            </a>
          </div>
          <sl-tooltip content="Copy URL">
            <sl-icon-button
              name="clipboard"
              style="font-size: 1rem;"
              @click=${(e: Event) => {
                const capsule = (e.target as HTMLElement).closest(
                  '.mcp-server-capsule'
                );
                const url =
                  capsule?.querySelector('.server-endpoint')?.textContent ||
                  `${window.location.origin}/openai/v1`;
                navigator.clipboard.writeText(url);
                this.dispatchEvent(
                  new CustomEvent('show-toast', {
                    bubbles: true,
                    composed: true,
                    detail: { message: 'AI Gateway URL copied!' },
                  })
                );
              }}
            ></sl-icon-button>
          </sl-tooltip>
        </div>

        <!-- Built-in MCP Server Endpoint -->
        <div class="mcp-server-capsule" style="margin-top: 0;">
          <div class="status-indicator"></div>
          <span class="capsule-eyebrow">Tool firewall</span>
          <sl-badge variant="neutral" size="small" style="margin-right: -4px;"
            >MCP tools</sl-badge
          >
          <div class="server-details">
            <span class="server-endpoint">${window.location.origin}/mcp</span>
            <a
              href="https://docs.preloop.ai/guide/mcp-server"
              target="_blank"
              style="display: flex; color: var(--sl-color-neutral-500); margin-left: auto;"
            >
              <sl-icon name="info-circle"></sl-icon>
            </a>
          </div>
          <sl-tooltip content="Copy URL">
            <sl-icon-button
              name="clipboard"
              style="font-size: 1rem;"
              @click=${() => {
                navigator.clipboard.writeText(`${window.location.origin}/mcp`);
                this.dispatchEvent(
                  new CustomEvent('show-toast', {
                    bubbles: true,
                    composed: true,
                    detail: { message: 'MCP URL copied!' },
                  })
                );
              }}
            ></sl-icon-button>
          </sl-tooltip>
        </div>
      </sl-card>
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
      <view-header headerText="Overview" width="extra-wide"></view-header>
      <div class="extra-wide" style="margin-bottom: var(--sl-spacing-large);">
        ${
          this.error
            ? html`<sl-alert variant="danger" open>${this.error}</sl-alert>`
            : nothing
        }
        ${this.renderWelcomeCard()}
      </div>

      <div class="column-layout dashboard extra-wide">
        <div class="main-column">
          <div class="dashboard-stack">
            ${this.renderPreloopGatewayCard()}
            ${this.renderActiveExecutionsCard()}
            ${this.renderRecentFlowExecutionsCard()}

            <div
              style="display: grid; grid-template-columns: 1fr 1fr; gap: var(--sl-spacing-large); margin-top: var(--sl-spacing-large);"
            >
              ${this.renderGatewayFailuresCard()}
              ${this.renderAuditExceptionsCard()}
            </div>
          </div>
        </div>

        <div class="side-column">
          ${this.renderAttentionCard()} ${this.renderUsageCard()}
          ${this.renderTopModelsCard()}
        </div>
        <mcp-setup-dialog
          ?open=${this.showSetupDialog}
          @close=${() => (this.showSetupDialog = false)}
        ></mcp-setup-dialog>
        <budget-limits-dialog
          ?open=${this.showBudgetDialog}
          billingEnabled
          @sl-hide=${() => (this.showBudgetDialog = false)}
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
