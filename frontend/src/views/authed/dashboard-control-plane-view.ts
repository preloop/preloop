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
import '../../components/agent-talk-composer.ts';
import '../../components/mcp-setup-dialog.ts';
import '../../components/budget-policy-editor.ts';
import '../../components/budget-health-card.ts';
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
  getApiUsageStats,
  getFlowExecutions,
  getFlows,
  getIssueCount,
  getMCPServers,
  getTools,
  getTrackers,
  getUsers,
  getFeatures,
  createFlow,
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
} from '../../utils/agent-display';
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
import { getAgentControlState } from '../../utils/agent-control';
import type { Tool } from '../../components/tool-card';
import consoleStyles from '../../styles/console-styles.css?inline';

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

interface ApiUsage {
  total_requests: number;
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
}

interface UsageSessionSubject {
  kind: 'agent' | 'flow' | 'session';
  name: string;
  href: string;
}

interface DashboardMetric {
  label: string;
  value: string | number;
  icon: string;
  href?: string;
  tone?: 'primary' | 'neutral' | 'success' | 'warning' | 'danger';
}

type BudgetPolicyUsage = {
  policy: BudgetPolicy;
  spend: number;
  hardLimit: number;
  softLimit: number;
  maxLimit: number;
  percent: number;
};

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
  @state() private apiUsage: ApiUsage | null = null;
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
  @state() private budgetTimeRange: 'day' | 'week' | 'month' | 'year' = 'month';
  @state() private budgetSummary: AccountGatewayUsageSummaryResponse | null =
    null;
  @state() private fetchingBudget = false;
  @state() private budgetSummariesByPeriod = new Map<
    string,
    AccountGatewayUsageSummaryResponse
  >();
  @state() private budgetPolicySummaries = new Map<
    string,
    AccountGatewayUsageSummaryResponse
  >();
  @state() private activeAgentsTimeRange: '5m' | '1h' | '1d' | '1w' | '1mo' =
    '1d';
  @state() private fetchingActiveAgents = false;
  @state() private topModelsSortMetric: 'spend' | 'usage' = 'spend';
  @state() private expandedOverviewGroups = new Set<string>();
  @state() private showSetupDialog = false;
  @state() private showBudgetDialog = false;
  @state() private welcomeCardDismissed = false;
  @state() private gatewayMetricsExpanded = false;

  @state() private aiModels: AIModel[] = [];
  @state() private isInviteDialogOpen = false;
  @state() private computeFeatureEnabled = false;
  @state() private isEnterprise = false;
  @state() private isAdmin = false;

  @state() private budgetPolicies: BudgetPolicy[] = [];
  @state() private dismissedExecutions: string[] = [];
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

  private dismissExecution(id: string) {
    if (!this.dismissedExecutions.includes(id)) {
      this.dismissedExecutions = [...this.dismissedExecutions, id];
      localStorage.setItem(
        'dashboard_dismissed_executions',
        JSON.stringify(this.dismissedExecutions)
      );
    }
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

      .updated-at {
        color: var(--sl-color-neutral-500);
        font-size: var(--sl-font-size-small);
        margin-top: -37px;
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
      .updated-at,
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
          grid-template-columns: repeat(auto-fit, minmax(96px, 1fr));
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
    void this.fetchBudgetSummary();
    void this.fetchActiveAgentsData();
    void this.fetchFeatures();
    void this.fetchAdminStatus();
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
    } catch {
      this.computeFeatureEnabled = false;
      this.isEnterprise = false;
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
      this.apiUsage = data.apiUsage;
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
      if (data.budgetSummary) this.budgetSummary = data.budgetSummary;
      this.budgetPolicies = data.budgetPolicies || [];
      this.budgetAgents = data.budgetAgents || [];

      if (data.budgetSummariesByPeriod) {
        this.budgetSummariesByPeriod = new Map(data.budgetSummariesByPeriod);
      }
      if (data.budgetPolicySummaries) {
        this.budgetPolicySummaries = new Map(data.budgetPolicySummaries);
      }

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
        apiUsage: this.apiUsage,
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
        budgetSummary: this.budgetSummary,
        budgetPolicies: this.budgetPolicies,
        budgetAgents: this.budgetAgents,
        budgetSummariesByPeriod: Array.from(
          this.budgetSummariesByPeriod.entries()
        ),
        budgetPolicySummaries: Array.from(this.budgetPolicySummaries.entries()),
      };
      sessionStorage.setItem(key, JSON.stringify(cacheObj));
    } catch (e) {
      console.warn('Failed to save dashboard cache to sessionStorage', e);
    }
  }

  private loadDismissedState(): void {
    this.welcomeCardDismissed =
      localStorage.getItem('dashboard_welcome_dismissed') === 'true';
    this.gatewayMetricsExpanded =
      localStorage.getItem('preloop_dashboard_metrics_expanded') === 'true';

    try {
      const dismissedExecsRaw = localStorage.getItem(
        'dashboard_dismissed_executions'
      );
      if (dismissedExecsRaw) {
        const parsed = JSON.parse(dismissedExecsRaw);
        if (Array.isArray(parsed)) {
          this.dismissedExecutions = parsed as string[];
        }
      }
    } catch (e) {
      console.warn('Failed to parse dismissed executions from localStorage', e);
    }
  }

  private dismissWelcomeCard(): void {
    this.welcomeCardDismissed = true;
    localStorage.setItem('dashboard_welcome_dismissed', 'true');
  }

  private toggleGatewayMetrics(): void {
    this.gatewayMetricsExpanded = !this.gatewayMetricsExpanded;
    localStorage.setItem(
      'preloop_dashboard_metrics_expanded',
      String(this.gatewayMetricsExpanded)
    );
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
      void this.fetchBudgetSummary();
      void this.fetchActiveAgentsData();
    }, 250);
  }

  private async fetchActiveAgentsData() {
    this.fetchingActiveAgents = true;
    try {
      const now = new Date();
      let startDateStr = '';

      if (this.activeAgentsTimeRange === '5m') {
        const d = new Date(now);
        d.setMinutes(d.getMinutes() - 5);
        startDateStr = d.toISOString();
      } else if (this.activeAgentsTimeRange === '1h') {
        const d = new Date(now);
        d.setHours(d.getHours() - 1);
        startDateStr = d.toISOString();
      } else if (this.activeAgentsTimeRange === '1d') {
        const d = new Date(now);
        d.setDate(d.getDate() - 1);
        startDateStr = d.toISOString();
      } else if (this.activeAgentsTimeRange === '1w') {
        const d = new Date(now);
        d.setDate(d.getDate() - 7);
        startDateStr = d.toISOString();
      } else if (this.activeAgentsTimeRange === '1mo') {
        const d = new Date(now);
        d.setMonth(d.getMonth() - 1);
        startDateStr = d.toISOString();
      }

      const runtimeSessionsParams: any = { status: 'all', limit: 100 };
      const agentsParams: any = { status: 'all', limit: 100 };
      if (startDateStr) {
        runtimeSessionsParams.startDate = startDateStr;
      }

      const [runtimeSessions, managedAgents] = await Promise.all([
        this.catchWith403Handling(
          getAccountRuntimeSessions(runtimeSessionsParams),
          { items: [] } as Awaited<ReturnType<typeof getAccountRuntimeSessions>>
        ),
        this.catchWith403Handling(getAccountAgents(agentsParams), {
          items: [],
        } as Awaited<ReturnType<typeof getAccountAgents>>),
      ]);

      this.runtimeSessions = runtimeSessions.items || [];
      this.totalRuntimeSessionsCount =
        runtimeSessions.total ?? this.runtimeSessions.length;
      this.managedAgents = managedAgents.items || [];
      if (this.managedAgents.length > 0 && !this.welcomeCardDismissed) {
        this.dismissWelcomeCard();
      }
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

  private async fetchBudgetSummary() {
    this.fetchingBudget = true;
    try {
      const [budgetSummary, budgetAgents] = await Promise.all([
        getAccountGatewayUsageSummary({
          startDate: this.getBudgetStartDate(this.budgetTimeRange),
          includeBreakdown: false,
        }).catch(() => null),
        getAccountAgents({ status: 'all', limit: 100 }).catch(() => ({
          items: [] as ManagedAgentSummary[],
        })),
      ]);
      const featuresRes = await fetchWithAuth('/api/v1/features')
        .then((r) => (r.ok ? r.json() : null))
        .catch(() => null);
      const billingEnabled = featuresRes?.features?.billing === true;
      const policies = billingEnabled
        ? await getBudgetPolicies().catch(() => [] as BudgetPolicy[])
        : [];
      this.budgetSummary = budgetSummary;
      this.budgetPolicies = Array.isArray(policies) ? policies : [];
      this.budgetAgents = budgetAgents.items || [];

      const periods = new Set(
        this.budgetPolicies.map((policy) => policy.period || 'monthly')
      );
      periods.add(this.timeRangeToBudgetPeriod(this.budgetTimeRange));

      const periodSummaries = await Promise.all(
        Array.from(periods).map(async (period) => {
          const summary = await getAccountGatewayUsageSummary({
            startDate: this.getBudgetPolicyStartDate(period),
            includeBreakdown: false,
          }).catch(() => null);
          return [period, summary] as const;
        })
      );
      const nextSummaries = new Map<
        string,
        AccountGatewayUsageSummaryResponse
      >();
      for (const [period, summary] of periodSummaries) {
        if (summary) {
          nextSummaries.set(period, summary);
        }
      }
      this.budgetSummariesByPeriod = nextSummaries;

      const managedAgentPolicies = this.budgetPolicies.filter(
        (policy) => policy.subject_type === 'managed_agent'
      );
      const policySummaries = await Promise.all(
        managedAgentPolicies.map(async (policy) => {
          const agent = this.getManagedAgentBySourceId(policy.subject_id);
          if (!agent?.session_source_id) {
            return [policy.id, null] as const;
          }
          const summary = await getAccountGatewayUsageSummary({
            startDate: this.getBudgetPolicyStartDate(policy.period),
            runtimePrincipalId: agent.session_source_id,
            includeBreakdown: false,
          }).catch(() => null);
          return [policy.id, summary] as const;
        })
      );
      const nextPolicySummaries = new Map<
        string,
        AccountGatewayUsageSummaryResponse
      >();
      for (const [policyId, summary] of policySummaries) {
        if (summary) {
          nextPolicySummaries.set(policyId, summary);
        }
      }
      this.budgetPolicySummaries = nextPolicySummaries;
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

    // Set individual loading flags to true
    this.fetchingGatewaySummary = true;
    this.fetchingRecentExecutions = true;
    this.fetchingApprovals = true;
    this.fetchingAudit = true;
    this.fetchingMCPAndTools = true;

    if (!options.preserveLoadingState) {
      this.loading = true;
    }
    this.error = null;

    const now = new Date();
    let startDateStr = '';

    if (this.gatewayTimeRange === 'day') {
      const d = new Date(now);
      d.setDate(d.getDate() - 1);
      startDateStr = d.toISOString();
    } else if (this.gatewayTimeRange === 'week') {
      const d = new Date(now);
      d.setDate(d.getDate() - 7);
      startDateStr = d.toISOString();
    } else if (this.gatewayTimeRange === 'month') {
      const d = new Date(now);
      d.setMonth(d.getMonth() - 1);
      startDateStr = d.toISOString();
    } else if (this.gatewayTimeRange === 'year') {
      const d = new Date(now);
      d.setFullYear(d.getFullYear() - 1);
      startDateStr = d.toISOString();
    }

    const p1 = (async () => {
      try {
        const [gatewaySummary, gatewayInteractions] = await Promise.all([
          this.catchWith403Handling(
            getAccountGatewayUsageSummary({ startDate: startDateStr }),
            null
          ),
          this.catchWith403Handling(
            getAccountGatewayUsageSearch({ limit: 12 }),
            {
              items: [],
            } as Awaited<ReturnType<typeof getAccountGatewayUsageSearch>>
          ),
        ]);
        this.gatewaySummary = gatewaySummary;
        this.gatewayInteractions = gatewayInteractions.items || [];
        if (!this.budgetSummary) {
          this.budgetSummary = gatewaySummary; // initialize if null
        }
      } catch (error) {
        console.error('Failed to load gateway summary data', error);
      } finally {
        this.fetchingGatewaySummary = false;
      }
    })();

    const p2 = (async () => {
      try {
        const [flows, flowExecutions] = await Promise.all([
          this.catchWith403Handling(getFlows(), [] as any[]),
          this.catchWith403Handling(
            getFlowExecutions({ limit: 10 }),
            [] as FlowExecution[]
          ),
        ]);
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
      } catch (error) {
        console.error('Failed to load flow executions data', error);
      } finally {
        this.fetchingRecentExecutions = false;
      }
    })();

    const p3 = (async () => {
      try {
        const [pendingApprovals, allApprovalRequests] = await Promise.all([
          this.catchWith403Handling(
            this.fetchApprovalRequests('pending', 3),
            []
          ),
          this.catchWith403Handling(
            this.fetchApprovalRequests(undefined, 100),
            []
          ),
        ]);
        this.pendingApprovals = pendingApprovals;
        this.calculateApprovalStats(allApprovalRequests);
      } catch (error) {
        console.error('Failed to load approvals data', error);
      } finally {
        this.fetchingApprovals = false;
      }
    })();

    const p4 = (async () => {
      try {
        const [
          audit,
          trackers,
          apiUsage,
          issueCount,
          toolCallsStats,
          failedToolCallsStats,
        ] = await Promise.all([
          this.catchWith403Handling(this.fetchAuditExceptions(), {
            groups: [],
            total: 0,
          }),
          this.catchWith403Handling(getTrackers(), [] as Tracker[]),
          this.catchWith403Handling(getApiUsageStats(), null),
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
        this.apiUsage = apiUsage;
        this.totalIssues = issueCount.total_issues;
        this.toolCallsCount = toolCallsStats?.total || 0;
        this.failedToolCallsCount = failedToolCallsStats?.total || 0;
      } catch (error) {
        console.error('Failed to load audit data', error);
      } finally {
        this.fetchingAudit = false;
      }
    })();

    const p5 = (async () => {
      try {
        const [mcpServers, tools, aiModels, users, totalAgentsStats] =
          await Promise.all([
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
            this.catchWith403Handling(
              getAccountAgents({ status: 'all', limit: 1 }),
              { total: 0 }
            ),
          ]);
        this.mcpServers = mcpServers;
        this.tools = tools;
        this.aiModels = aiModels || [];
        const filtered = this.aiModels.filter(
          (m) => m.model_kind !== 'stt' && m.model_kind !== 'tts'
        );
        if (
          filtered.length > 0 &&
          !filtered.some((m) => m.id === this.deployModel)
        ) {
          this.deployModel = filtered[0].id;
        }
        this.hasAIModels = (aiModels || []).length > 0;
        this.aiModelsCount = Array.isArray(aiModels) ? aiModels.length : 0;
        this.enabledUsersCount = Array.isArray(users.users)
          ? users.users.filter((u: { is_active?: boolean }) => u.is_active)
              .length
          : 0;
        this.totalAgentsCount = totalAgentsStats?.total || 0;
      } catch (error) {
        console.error('Failed to load MCP and tools data', error);
      } finally {
        this.fetchingMCPAndTools = false;
      }
    })();

    try {
      await Promise.all([p1, p2, p3, p4, p5]);
      this.lastUpdatedAt = new Date().toISOString();
      this.saveDashboardCache();
    } catch (error) {
      console.error(
        'Failed to complete background loading of overview dashboard',
        error
      );
      this.error = 'Failed to load some overview dashboard data.';
    } finally {
      this.loading = false;
      this.refreshInFlight = false;
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

  private getBudgetStartDate(range: 'day' | 'week' | 'month' | 'year'): string {
    const now = new Date();
    const start = new Date(now);
    if (range === 'day') {
      start.setDate(start.getDate() - 1);
    } else if (range === 'week') {
      start.setDate(start.getDate() - 7);
    } else if (range === 'month') {
      start.setMonth(start.getMonth() - 1);
    } else if (range === 'year') {
      start.setFullYear(start.getFullYear() - 1);
    }
    return start.toISOString();
  }

  private getBudgetPolicyStartDate(period: string): string | undefined {
    const now = new Date();
    const start = new Date(now);
    if (period === 'hourly') {
      start.setHours(start.getHours() - 1);
    } else if (period === 'daily') {
      start.setDate(start.getDate() - 1);
    } else if (period === 'weekly') {
      start.setDate(start.getDate() - 7);
    } else if (period === 'monthly') {
      start.setMonth(start.getMonth() - 1);
    } else if (period === 'yearly') {
      start.setFullYear(start.getFullYear() - 1);
    } else {
      return undefined;
    }
    return start.toISOString();
  }

  private timeRangeToBudgetPeriod(
    range: 'day' | 'week' | 'month' | 'year'
  ): string {
    if (range === 'day') return 'daily';
    if (range === 'week') return 'weekly';
    if (range === 'year') return 'yearly';
    return 'monthly';
  }

  private get gatewayFailures(): GatewayUsageSearchResultItem[] {
    return this.gatewayInteractions.filter(
      (item) => item.outcome !== 'success'
    );
  }

  private get failedFlowExecutions(): FlowExecution[] {
    return this.recentFlowExecutions.filter(
      (execution) =>
        execution.status === 'FAILED' &&
        !this.dismissedExecutions.includes(execution.id)
    );
  }

  private formatCurrency(value: number | null | undefined): string {
    return `$${(value || 0).toFixed(2)}`;
  }

  private formatNumber(value: number | null | undefined): string {
    return Intl.NumberFormat().format(value || 0);
  }

  private formatPercent(numerator: number, denominator: number): string {
    if (denominator <= 0) {
      return '0%';
    }
    return `${((numerator / denominator) * 100).toFixed(1)}%`;
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

  private renderExpandableSubGroup(
    groupId: string,
    name: string,
    href: string | null,
    summaryMetric: string,
    content: ReturnType<typeof html>
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
                      ? html`
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
    content: ReturnType<typeof html>
  ) {
    return this.renderExpandableSubGroup(
      groupId,
      name,
      href,
      summaryMetric,
      content
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

  private get inactiveAgentsCount(): number {
    return Math.max(0, this.totalAgentsCount - this.activeAgentsCount);
  }

  private get flowExecutionSuccessRate(): string {
    return this.formatPercent(
      this.succeededFlowExecutionsCount,
      this.flowExecutionsCount
    );
  }

  private get modelRequestSuccessRate(): string {
    return this.formatPercent(
      this.gatewaySummary?.successful_requests || 0,
      this.gatewaySummary?.total_requests || 0
    );
  }

  private get toolCallSuccessRate(): string {
    return this.formatPercent(
      this.toolCallsCount - this.failedToolCallsCount,
      this.toolCallsCount
    );
  }

  private get approvalRate(): string {
    const decidedApprovals =
      this.approvalStats.approved +
      this.approvalStats.declined +
      this.approvalStats.expired;
    return this.formatPercent(this.approvalStats.approved, decidedApprovals);
  }

  private getGlobalPolicyUsage() {
    return this.calculatePolicyUsages().find(
      (u) =>
        u.policy.subject_type === 'global' ||
        u.policy.subject_type === 'account'
    );
  }

  private getSelectedGlobalPolicyUsage(): BudgetPolicyUsage | undefined {
    const selectedPeriod = this.timeRangeToBudgetPeriod(this.budgetTimeRange);
    return this.calculatePolicyUsages().find(
      (u) =>
        (u.policy.subject_type === 'global' ||
          u.policy.subject_type === 'account') &&
        u.policy.period === selectedPeriod
    );
  }

  private budgetVariant() {
    const globalUsage =
      this.getSelectedGlobalPolicyUsage() || this.getGlobalPolicyUsage();
    if (globalUsage && globalUsage.maxLimit > 0) {
      if (globalUsage.percent >= 100) return 'danger';
      if (globalUsage.percent >= 80) return 'warning';
      return 'success';
    }

    const budget = this.budgetSummary?.budget;
    if (!budget) {
      return 'neutral';
    }
    if (budget.hard_limit_exceeded) {
      return 'danger';
    }
    if (budget.soft_limit_exceeded) {
      return 'warning';
    }
    return 'success';
  }

  private budgetPercent(): number {
    const globalUsage =
      this.getSelectedGlobalPolicyUsage() || this.getGlobalPolicyUsage();
    if (globalUsage && globalUsage.maxLimit > 0) {
      return globalUsage.percent;
    }

    const budget = this.budgetSummary?.budget;
    const limit = budget?.monthly_limit_usd || budget?.soft_limit_usd || 0;
    if (!limit) {
      return 0;
    }
    return Math.min(
      100,
      Math.round(((budget?.current_spend_usd || 0) / limit) * 100)
    );
  }

  private calculatePolicyUsages(): BudgetPolicyUsage[] {
    if (!this.budgetPolicies) return [];

    return this.budgetPolicies
      .map((policy) => {
        const summary =
          this.budgetPolicySummaries.get(policy.id) ||
          this.budgetSummariesByPeriod.get(policy.period) ||
          this.budgetSummary;
        let spend = 0;
        if (!summary) {
          spend = 0;
        } else if (
          policy.subject_type === 'global' ||
          policy.subject_type === 'account'
        ) {
          spend =
            summary.budget?.current_spend_usd || summary.estimated_cost || 0;
        } else if (policy.subject_type === 'ai_model') {
          spend = summary.usage_by_model
            .filter((m) => m.ai_model_id === policy.subject_id)
            .reduce((acc, m) => acc + m.estimated_cost, 0);
        } else if (policy.subject_type === 'managed_agent') {
          const agent = this.getManagedAgentBySourceId(policy.subject_id);
          const agentIds = new Set(
            [policy.subject_id, agent?.id, agent?.session_source_id].filter(
              Boolean
            ) as string[]
          );
          if (this.budgetPolicySummaries.has(policy.id)) {
            spend =
              summary.estimated_cost || summary.budget?.current_spend_usd || 0;
          } else {
            spend = summary.usage_by_session
              .filter(
                (s) =>
                  agentIds.has(s.session_source_id || '') ||
                  agentIds.has(s.runtime_principal_id || '')
              )
              .reduce((acc, s) => acc + s.estimated_cost, 0);
          }
        } else if (policy.subject_type === 'flow') {
          spend = summary.usage_by_flow
            .filter((flow) => flow.flow_id === policy.subject_id)
            .reduce((acc, flow) => acc + flow.estimated_cost, 0);
        } else if (policy.subject_type === 'api_key') {
          spend = summary.usage_by_session
            .filter(
              (session) =>
                session.session_source_id === policy.subject_id ||
                session.runtime_principal_id === policy.subject_id
            )
            .reduce((acc, session) => acc + session.estimated_cost, 0);
        }

        const hardLimit = policy.hard_limit_usd || 0;
        const softLimit = policy.soft_limit_usd || 0;
        const maxLimit = hardLimit || softLimit;
        const percent =
          maxLimit > 0
            ? Math.min(100, Math.round((spend / maxLimit) * 100))
            : 0;

        return { policy, spend, hardLimit, softLimit, maxLimit, percent };
      })
      .sort((a, b) => {
        const aGlobal =
          a.policy.subject_type === 'global' ||
          a.policy.subject_type === 'account';
        const bGlobal =
          b.policy.subject_type === 'global' ||
          b.policy.subject_type === 'account';
        if (aGlobal !== bGlobal) return aGlobal ? -1 : 1;
        return b.percent - a.percent;
      });
  }

  private getBudgetPolicyDisplayName(policy: BudgetPolicy): string {
    const period = this.formatBudgetPeriod(policy.period);
    if (policy.subject_type === 'global' || policy.subject_type === 'account') {
      return `Global · ${period}`;
    }
    if (policy.subject_type === 'managed_agent') {
      const agentName =
        this.getManagedAgentBySourceId(policy.subject_id)?.display_name ||
        'Managed agent';
      return `${agentName} · ${period}`;
    }
    if (policy.subject_type === 'ai_model') {
      return `${policy.model_alias || 'Model'} · ${period}`;
    }
    return `${policy.subject_type.replace(/_/g, ' ')} · ${period}`;
  }

  private formatBudgetPeriod(period: string): string {
    if (period === 'hourly') return '1h';
    if (period === 'daily') return '24h';
    if (period === 'weekly') return '7d';
    if (period === 'monthly') return '30d';
    if (period === 'yearly') return '1y';
    if (period === 'all_time') return 'all time';
    return period;
  }

  private getBudgetPolicyIcon(policy: BudgetPolicy): string {
    if (policy.subject_type === 'global' || policy.subject_type === 'account') {
      return 'globe';
    }
    if (policy.subject_type === 'managed_agent') {
      return 'robot';
    }
    if (policy.subject_type === 'ai_model') {
      return 'cpu';
    }
    return 'sliders';
  }

  private renderBudgetLimitRow(
    label: string,
    icon: string,
    spend: number,
    softLimit: number,
    hardLimit: number
  ) {
    const maxLimit = hardLimit || softLimit;
    const fillPercent =
      maxLimit > 0 ? Math.min(100, (spend / maxLimit) * 100) : 0;
    const softPercent =
      softLimit > 0 && maxLimit > 0
        ? Math.min(100, (softLimit / maxLimit) * 100)
        : 0;
    const successFillPercent =
      softLimit > 0 ? Math.min(fillPercent, softPercent) : fillPercent;
    const warningFillPercent =
      softLimit > 0 && fillPercent > softPercent
        ? fillPercent - softPercent
        : 0;
    return html`
      <div style="display: flex; flex-direction: column; gap: 4px;">
        <div
          style="display: flex; justify-content: space-between; font-size: var(--sl-font-size-small); align-items: center; gap: var(--sl-spacing-small);"
        >
          <span style="display: flex; align-items: center; gap: 4px;">
            <sl-icon name=${icon}></sl-icon>
            ${label}
          </span>
          <span style="font-weight: 500; text-align: right;">
            ${this.formatCurrency(spend)}
            ${
              maxLimit > 0
                ? html` / ${this.formatCurrency(maxLimit)}`
                : html`<span
                    style="color: var(--sl-color-neutral-500); font-weight: 400;"
                  >
                    spent</span
                  >`
            }
          </span>
        </div>
        ${
          maxLimit > 0
            ? html`
                <div class="budget-track">
                  <div
                    class="budget-track-fill success"
                    style="--budget-fill-width: ${successFillPercent}%;"
                  ></div>
                  ${
                    warningFillPercent > 0
                      ? html`<div
                          class="budget-track-fill warning"
                          style="--budget-fill-left: ${softPercent}%; --budget-fill-width: ${warningFillPercent}%;"
                        ></div>`
                      : nothing
                  }
                  ${
                    softLimit > 0 && hardLimit > 0 && softLimit < hardLimit
                      ? html`<div
                          class="budget-soft-marker"
                          title=${`Soft limit ${this.formatCurrency(softLimit)}`}
                          style="--budget-soft-position: ${softPercent}%;"
                        ></div>`
                      : nothing
                  }
                  ${
                    hardLimit > 0
                      ? html`<div
                          class="budget-hard-marker"
                          title=${`Hard limit ${this.formatCurrency(hardLimit)}`}
                        ></div>`
                      : nothing
                  }
                </div>
                <div
                  style="display: flex; justify-content: space-between; gap: var(--sl-spacing-small); color: var(--sl-color-neutral-500); font-size: var(--sl-font-size-x-small);"
                >
                  <span>
                    ${
                      softLimit > 0
                        ? html`Soft ${this.formatCurrency(softLimit)}`
                        : nothing
                    }
                  </span>
                  <span>
                    ${
                      hardLimit > 0
                        ? html`Hard ${this.formatCurrency(hardLimit)}`
                        : nothing
                    }
                  </span>
                </div>
              `
            : nothing
        }
      </div>
    `;
  }

  private getManagedAgentBySourceId(
    sourceId: string | null | undefined
  ): ManagedAgentSummary | undefined {
    if (!sourceId) {
      return undefined;
    }
    return [...this.managedAgents, ...this.budgetAgents].find(
      (agent) =>
        agent.id === sourceId ||
        agent.session_source_id === sourceId ||
        agent.runtime_session_id === sourceId
    );
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
          @click=${this.dismissWelcomeCard}
        >
          <sl-icon name="x-lg"></sl-icon>
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
              ? html`<sl-badge variant="danger" pulse
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
                  ${this.recentFlowExecutions
                    .filter(
                      (exec) => !this.dismissedExecutions.includes(exec.id)
                    )
                    .slice(0, 5)
                    .map(
                      (exec) => html`
                        <div
                          class="item-card ${
                            exec.status === 'FAILED' ? 'danger' : ''
                          }"
                        >
                          <div class="item-info">
                            <span class="item-name"
                              >${exec.flow_name || 'Unnamed Flow'}</span
                            >
                            ${
                              exec.error_message
                                ? html`<span class="item-error"
                                    >${exec.error_message}</span
                                  >`
                                : ''
                            }
                            <span class="item-secondary"
                              >${this.formatDate(exec.start_time)}</span
                            >
                          </div>
                          <div
                            style="display: flex; align-items: center; gap: var(--sl-spacing-small);"
                          >
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
                            <sl-icon-button
                              name="x-lg"
                              label="Dismiss"
                              @click=${(e: Event) => {
                                e.preventDefault();
                                this.dismissExecution(exec.id);
                              }}
                            ></sl-icon-button>
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
  private renderBudgetHealthCard() {
    return html`
      <budget-health-card
        .summary=${this.budgetSummary}
        .policies=${this.budgetPolicies}
        .agents=${[...this.managedAgents, ...this.budgetAgents]}
        .loading=${this.fetchingBudget}
        .timeRange=${this.budgetTimeRange}
        .showRangeSelector=${true}
        configurable
        @range-change=${(event: CustomEvent<{ value: string }>) => {
          this.budgetTimeRange = event.detail.value as any;
          this.fetchBudgetSummary();
        }}
        @configure=${() => (this.showBudgetDialog = true)}
      ></budget-health-card>
    `;
  }

  private renderBudgetHealthContent() {
    const policyUsages = this.calculatePolicyUsages();
    const selectedPeriod = this.timeRangeToBudgetPeriod(this.budgetTimeRange);
    const selectedGlobalUsage = policyUsages.find(
      (usage) =>
        (usage.policy.subject_type === 'global' ||
          usage.policy.subject_type === 'account') &&
        usage.policy.period === selectedPeriod
    );
    const additionalUsages = selectedGlobalUsage
      ? policyUsages.filter(
          (usage) => usage.policy.id !== selectedGlobalUsage.policy.id
        )
      : policyUsages;
    const globalSpend = this.budgetSummary?.budget?.current_spend_usd || 0;

    return html`
      <div
        style="display: flex; flex-direction: column; gap: var(--sl-spacing-medium);"
      >
        <div
          style="display: flex; flex-direction: column; gap: var(--sl-spacing-small);"
        >
          ${this.renderBudgetLimitRow(
            `Global spend · ${this.formatBudgetPeriod(selectedPeriod)}`,
            'globe',
            globalSpend,
            selectedGlobalUsage?.softLimit ||
              this.budgetSummary?.budget?.soft_limit_usd ||
              0,
            selectedGlobalUsage?.hardLimit ||
              this.budgetSummary?.budget?.monthly_limit_usd ||
              0
          )}
          ${additionalUsages.map((usage) =>
            this.renderBudgetLimitRow(
              this.getBudgetPolicyDisplayName(usage.policy),
              this.getBudgetPolicyIcon(usage.policy),
              usage.spend,
              usage.softLimit,
              usage.hardLimit
            )
          )}
        </div>
        <div style="margin-top: var(--sl-spacing-small);">
          <sl-button
            size="small"
            variant="default"
            @click=${() => (this.showBudgetDialog = true)}
            style="width: 100%;"
          >
            <sl-icon slot="prefix" name="gear"></sl-icon>
            Configure Limits
          </sl-button>
        </div>
      </div>
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

  private renderTopModelsCard() {
    const rawModels = this.gatewaySummary?.usage_by_model || [];
    if (rawModels.length === 0) {
      return nothing;
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
    models.sort((a, b) => {
      if (this.topModelsSortMetric === 'usage') {
        return b.request_count - a.request_count;
      } else {
        return b.estimated_cost - a.estimated_cost;
      }
    });

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

              const agentGroups = new Map<
                string,
                {
                  subject: UsageSessionSubject;
                  sessions: typeof modelSessions;
                }
              >();
              const flowGroups = new Map<
                string,
                {
                  subject: UsageSessionSubject;
                  sessions: typeof modelSessions;
                }
              >();
              const otherSessions: typeof modelSessions = [];

              modelSessions.forEach((s) => {
                const subject = this.getUsageSessionSubject(s);
                if (subject.kind === 'agent') {
                  const agent = this.getManagedAgentForUsageSession(s);
                  const key =
                    agent?.id ||
                    s.agent_id ||
                    s.runtime_session_id ||
                    s.session_source_id ||
                    s.runtime_principal_id ||
                    subject.href;
                  if (!agentGroups.has(key)) {
                    agentGroups.set(key, { subject, sessions: [] });
                  }
                  agentGroups.get(key)!.sessions.push(s);
                } else if (subject.kind === 'flow') {
                  const key = s.flow_id || s.flow_name || subject.href;
                  if (!flowGroups.has(key)) {
                    flowGroups.set(key, { subject, sessions: [] });
                  }
                  flowGroups.get(key)!.sessions.push(s);
                } else {
                  otherSessions.push(s);
                }
              });

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
                    agentGroups.size > 0 ||
                    flowGroups.size > 0 ||
                    otherSessions.length > 0
                      ? html`
                          <div class="overview-nested-groups">
                            ${Array.from(agentGroups.entries()).map(
                              ([groupKey, { subject, sessions }]) => {
                                const totalAgentCost = sessions.reduce(
                                  (acc, s) => acc + s.estimated_cost,
                                  0
                                );
                                const totalAgentReqs = sessions.reduce(
                                  (acc, s) => acc + s.request_count,
                                  0
                                );
                                const agent =
                                  this.getManagedAgentForUsageSession(
                                    sessions[0]
                                  );
                                const groupId = `top-model:${modelKey}:agent:${groupKey}`;
                                const trailingMetric = `${this.formatCurrency(totalAgentCost)} (${this.formatNumber(totalAgentReqs)} req)`;
                                if (agent) {
                                  return this.renderAgentExpandableGroup(
                                    groupId,
                                    agent,
                                    sessions,
                                    trailingMetric
                                  );
                                }
                                return this.renderCollapsibleGroup(
                                  groupId,
                                  subject.name,
                                  subject.href,
                                  trailingMetric,
                                  html`
                                    <div class="nested-session-list">
                                      ${sessions.map((s) =>
                                        this.renderNestedSessionRow(
                                          s,
                                          `${this.formatNumber(s.request_count)} req`
                                        )
                                      )}
                                    </div>
                                  `
                                );
                              }
                            )}
                            ${Array.from(flowGroups.entries()).map(
                              ([groupKey, { subject, sessions }]) => {
                                const totalFlowCost = sessions.reduce(
                                  (acc, s) => acc + s.estimated_cost,
                                  0
                                );
                                const totalFlowReqs = sessions.reduce(
                                  (acc, s) => acc + s.request_count,
                                  0
                                );
                                return this.renderCollapsibleGroup(
                                  `top-model:${modelKey}:flow:${groupKey}`,
                                  subject.name,
                                  subject.href,
                                  `${this.formatCurrency(totalFlowCost)} (${this.formatNumber(totalFlowReqs)} req)`,
                                  html`
                                    <div class="nested-session-list">
                                      ${sessions.map((s) =>
                                        this.renderNestedSessionRow(
                                          s,
                                          `${this.formatNumber(s.request_count)} req`
                                        )
                                      )}
                                    </div>
                                  `
                                );
                              }
                            )}
                            ${
                              otherSessions.length > 0
                                ? this.renderCollapsibleGroup(
                                    `top-model:${modelKey}:other`,
                                    'Other',
                                    null,
                                    `${this.formatCurrency(
                                      otherSessions.reduce(
                                        (acc, s) => acc + s.estimated_cost,
                                        0
                                      )
                                    )} (${this.formatNumber(
                                      otherSessions.reduce(
                                        (acc, s) => acc + s.request_count,
                                        0
                                      )
                                    )} req)`,
                                    html`
                                      <div class="nested-session-list">
                                        ${otherSessions.map((s) =>
                                          this.renderNestedSessionRow(
                                            s,
                                            `${this.formatNumber(s.request_count)} req`
                                          )
                                        )}
                                      </div>
                                    `
                                  )
                                : ''
                            }
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
      const sessions = this.activeSessions.filter(
        (s) =>
          (s.runtime_principal_id === agent.session_source_id ||
            s.runtime_principal_id === agent.id ||
            s.session_source_id === agent.session_source_id ||
            s.session_source_id === agent.id) &&
          (s.runtime_principal_type === agent.session_source_type ||
            s.session_source_type === agent.session_source_type)
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

    const orphanSessions = this.activeSessions.filter(
      (s) => !usedSessionIds.has(s.id)
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
      agentsWithSessions.length > 0 || orphanSessions.length > 0;

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
          <select
            style="background: transparent; border: none; font-size: var(--sl-font-size-small); color: var(--sl-color-neutral-600); cursor: pointer; outline: none; margin-left: auto; margin-right: var(--sl-spacing-small);"
            .value=${this.activeAgentsTimeRange}
            @change=${(e: Event) => {
              this.activeAgentsTimeRange = (e.target as HTMLSelectElement)
                .value as any;
              this.fetchActiveAgentsData();
            }}
          >
            <option value="5m">5m</option>
            <option value="1h">1h</option>
            <option value="1d">1d</option>
            <option value="1w">1w</option>
            <option value="1mo">1mo</option>
          </select>
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

  private renderPendingApprovalsCard() {
    if (this.pendingApprovals.length === 0) {
      return nothing;
    }

    return html`
      <sl-card class="content-card">
        <div slot="header" class="card-header-with-action">
          Pending approvals
          <sl-badge variant="warning">${this.pendingApprovals.length}</sl-badge>
        </div>
        <div class="list">
          ${repeat(
            this.pendingApprovals,
            (approval) => approval.id,
            (approval) => html`
              <div class="row">
                <div class="row-main">
                  <span class="row-primary">${approval.tool_name}</span>
                  <sl-button
                    size="small"
                    href=${`/console/approval/${approval.id}`}
                  >
                    Review
                  </sl-button>
                </div>
                <div class="row-meta">
                  <span>${approval.status}</span>
                  <span>${this.formatRelativeTime(approval.requested_at)}</span>
                </div>
              </div>
            `
          )}
        </div>
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
        label: 'approved requests',
        value: this.formatNumber(this.approvalStats.approved),
        icon: 'check-circle',
        href: '/console/approvals?status=approved',
        tone: 'success',
      },
      {
        label: 'inactive agents',
        value: this.formatNumber(this.inactiveAgentsCount),
        icon: 'pause-circle',
        href: '/console/agents',
        tone: this.inactiveAgentsCount > 0 ? 'warning' : 'neutral',
      },
      {
        label: 'flow executions',
        value: this.formatNumber(this.flowExecutionsCount),
        icon: 'play-circle',
        href: '/console/flows/executions',
        tone: 'primary',
      },
      {
        label: 'model requests',
        value: this.formatNumber(this.gatewaySummary?.total_requests || 0),
        icon: 'activity',
        href: '/console/audit?event_type=model_gateway_request',
        tone: 'primary',
      },
      {
        label: 'tool calls',
        value: this.formatNumber(this.toolCallsCount),
        icon: 'terminal',
        href: '/console/audit?event_type=tool_call',
        tone: 'primary',
      },
      {
        label: 'declined requests',
        value: this.formatNumber(this.approvalStats.declined),
        icon: 'x-circle',
        href: '/console/approvals?status=declined',
        tone: this.approvalStats.declined > 0 ? 'danger' : 'neutral',
      },
      {
        label: 'total runtime sessions',
        value: this.formatNumber(this.totalRuntimeSessionsCount),
        icon: 'collection',
        href: '/console/runtime-sessions',
        tone: 'primary',
      },
      {
        label: 'failed executions',
        value: this.formatNumber(this.failedExecutionsCount),
        icon: 'exclamation-triangle',
        href: '/console/flows/executions',
        tone: this.failedExecutionsCount > 0 ? 'danger' : 'neutral',
      },
      {
        label: 'failed requests',
        value: this.formatNumber(this.gatewaySummary?.failed_requests || 0),
        icon: 'exclamation-triangle',
        href: '/console/audit?event_type=model_gateway_request&outcome=failed',
        tone: this.gatewaySummary?.failed_requests ? 'danger' : 'neutral',
      },
      {
        label: 'failed tool calls',
        value: this.formatNumber(this.failedToolCallsCount),
        icon: 'exclamation-octagon',
        href: '/console/audit?event_type=tool_call&outcome=failed',
        tone: this.failedToolCallsCount > 0 ? 'danger' : 'neutral',
      },
      {
        label: 'timed out approval requests',
        value: this.formatNumber(this.approvalStats.expired),
        icon: 'clock',
        href: '/console/approvals?status=expired',
        tone: this.approvalStats.expired > 0 ? 'warning' : 'neutral',
      },
      {
        label: 'total tokens',
        value: this.formatNumber(
          this.gatewaySummary?.token_usage.total_tokens || 0
        ),
        icon: 'braces',
        href: '/console/api-usage',
        tone: 'primary',
      },
      {
        label: 'flow execution success rate',
        value: this.flowExecutionSuccessRate,
        icon: 'check-circle',
        href: '/console/flows/executions',
        tone: 'success',
      },
      {
        label: 'model request success rate',
        value: this.modelRequestSuccessRate,
        icon: 'check-circle',
        href: '/console/audit?event_type=model_gateway_request',
        tone: 'success',
      },
      {
        label: 'tool call success rate',
        value: this.toolCallSuccessRate,
        icon: 'check-circle',
        href: '/console/audit?event_type=tool_call',
        tone: 'success',
      },
      {
        label: 'approval rate',
        value: this.approvalRate,
        icon: 'shield-check',
        href: '/console/approvals',
        tone: 'success',
      },
    ];
  }

  private renderPreloopGatewayCard() {
    const visibleMetrics = this.gatewayMetricsExpanded
      ? this.gatewayMetrics
      : this.gatewayMetrics.slice(0, 5);

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
                    ${visibleMetrics.map((metric) =>
                      this.renderMetricItem(metric)
                    )}
                  </div>
                `
          }
        </div>
        <div
          style="display: flex; justify-content: center; margin-top: calc(-1 * var(--sl-spacing-medium)); margin-bottom: var(--sl-spacing-medium);"
        >
          <sl-button
            size="small"
            variant="text"
            @click=${this.toggleGatewayMetrics}
          >
            ${
              this.gatewayMetricsExpanded
                ? 'Show less metrics'
                : 'Show more metrics'
            }
            <sl-icon
              slot="suffix"
              name=${
                this.gatewayMetricsExpanded ? 'chevron-up' : 'chevron-down'
              }
            ></sl-icon>
          </sl-button>
        </div>

        <!-- AI Model Gateway Endpoint -->
        <div
          class="mcp-server-capsule"
          style="margin-top: 0; margin-bottom: var(--sl-spacing-small);"
        >
          <div class="status-indicator"></div>
          <sl-badge variant="primary" size="small" style="margin-right: -4px;"
            >AI models</sl-badge
          >
          <div
            class="server-details"
            style="display: flex; gap: var(--sl-spacing-small); align-items: center;"
          >
            <select
              style="background: transparent; border: none; font-size: inherit; font-family: inherit; color: var(--sl-color-primary-600); cursor: pointer; outline: none; margin-right: var(--sl-spacing-2x-small);"
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
      <p></p>
      <div class="extra-wide" style="margin-bottom: var(--sl-spacing-large);">
        ${
          this.error
            ? html`<sl-alert variant="danger" open>${this.error}</sl-alert>`
            : nothing
        }
        <div class="updated-at">
          Last updated ${this.formatRelativeTime(this.lastUpdatedAt)}
        </div>
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
          ${this.renderPendingApprovalsCard()} ${this.renderBudgetHealthCard()}
          ${this.renderTopModelsCard()}
        </div>
        <mcp-setup-dialog
          ?open=${this.showSetupDialog}
          @close=${() => (this.showSetupDialog = false)}
        ></mcp-setup-dialog>
        <sl-dialog
          label="Configure Budget Limits"
          ?open=${this.showBudgetDialog}
          @sl-after-hide=${(e: Event) => {
            if (e.target === e.currentTarget) {
              this.showBudgetDialog = false;
            }
          }}
          style="--width: 600px;"
        >
          ${
            this.showBudgetDialog
              ? html`
                  <budget-policy-editor
                    billingEnabled
                    @budget-policies-changed=${this.handleBudgetPoliciesChanged}
                  ></budget-policy-editor>
                `
              : nothing
          }
        </sl-dialog>
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
