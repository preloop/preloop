import { LitElement, html, css, unsafeCSS, type TemplateResult } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import { repeat } from 'lit/directives/repeat.js';
import {
  getTools,
  getMCPServers,
  deleteMCPServer,
  scanMCPServer,
  updateMCPServer,
  createToolConfiguration,
  updateToolConfiguration,
  getApprovalWorkflows,
  deleteApprovalWorkflow,
  getFeatures,
  getUserProfile,
  getAIModels,
  getToolUsageStats,
  createAccessRule,
  updateAccessRule,
  deleteAccessRule,
  fetchWithAuth,
  generatePolicy,
} from '../../api';
import '../../components/mcp-server-form';
import '../../components/mcp-server-card';
import '../../components/tools-editor-component';
import '../../components/mcp-setup-dialog';
import '../../components/approval-workflow-dialog';
import type { Tool, ApprovalWorkflow } from '../../components/tool-card';
import type { MCPServer } from '../../components/mcp-server-card';
import type { AccessRuleSummary } from '../../components/governance-rule-set-editor';
import type { RuleFormData } from '../../components/tool-rule-editor';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/divider/divider.js';
import '@shoelace-style/shoelace/dist/components/icon-button/icon-button.js';
import '@shoelace-style/shoelace/dist/components/dialog/dialog.js';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/copy-button/copy-button.js';
import '@shoelace-style/shoelace/dist/components/dropdown/dropdown.js';
import '@shoelace-style/shoelace/dist/components/menu/menu.js';
import '@shoelace-style/shoelace/dist/components/menu-item/menu-item.js';
import '@shoelace-style/shoelace/dist/components/tooltip/tooltip.js';
import '@shoelace-style/shoelace/dist/components/card/card.js';
import '@shoelace-style/shoelace/dist/components/checkbox/checkbox.js';
import consoleStyles from '../../styles/console-styles.css?inline';

import type { ToolWithRules } from '../../components/tools-editor-component';
import type { GatewayUsageByTool } from '../../types';

interface StarterPolicyDiffChange {
  path: string;
  operation: 'add' | 'remove' | 'modify';
  old_value?: unknown;
  new_value?: unknown;
}

interface StarterPolicyDiff {
  has_changes: boolean;
  summary: string;
  changes: StarterPolicyDiffChange[];
}

@customElement('tools-view')
export class ToolsView extends LitElement {
  @state() private tools: ToolWithRules[] = [];
  @state() private mcpServers: MCPServer[] = [];
  @state() private approvalPolicies: ApprovalWorkflow[] = [];
  @state() private loading = true;
  @state() private error: string | null = null;
  @state() private isAddingMCPServer = false;
  @state() private editingMCPServer: MCPServer | null = null;
  @state() private currentUser: { id?: string } | null = null;
  @state() private showSetupDialog = false;
  @state() private features: { [key: string]: boolean | string[] } = {};
  @state() private filterText = '';
  @state() private isExporting = false;
  @state() private oauthAlert: 'success' | 'error' | null = null;
  @state() private hasDefaultAIModel = false;
  @state() private showStarterPolicyDialog = false;
  @state() private starterPolicyServer: MCPServer | null = null;
  @state() private starterPolicyYaml = '';
  @state() private starterPolicyWarnings: string[] = [];
  @state() private starterPolicyError: string | null = null;
  @state() private isGeneratingStarterPolicy = false;
  @state() private isApplyingStarterPolicy = false;
  @state() private isPreviewingStarterPolicyDiff = false;
  @state() private starterPolicyDiff: StarterPolicyDiff | null = null;
  @state() private starterPolicyReviewConfirmed = false;
  @state() private toolUsageStats: GatewayUsageByTool[] = [];
  @state() private toolStatsLoading = false;

  // Single active filter — only one at a time (besides text/policy)
  @state() private activeFilter:
    | 'all'
    | 'available'
    | 'enabled'
    | 'disabled'
    | 'unavailable'
    | 'builtin'
    | 'mcp'
    | 'has_rules'
    | 'no_rules'
    | 'require_approval'
    | 'no_approval' = 'available';
  @state() private filterPolicyId: string | null = null;

  // Approval workflow dialog
  @state() private showPolicyDialog = false;
  @state() private editingPolicy: ApprovalWorkflow | null = null;

  static styles = [
    unsafeCSS(consoleStyles),
    css`
      mcp-setup-dialog {
        display: contents;
      }

      /* Top section: summary + MCP card side by side */
      .top-section {
        display: flex;
        justify-content: space-between;
        align-items: flex-start;
        gap: var(--sl-spacing-large);
      }

      @media (max-width: 900px) {
        .top-section {
          flex-direction: column;
          margin-bottom: var(--sl-spacing-small);
          gap: var(--sl-spacing-small);
        }
      }

      /* MCP Server card */
      .builtin-server-card {
        flex: 0 1 400px;
        max-width: 400px;
      }

      @media (max-width: 900px) {
        .builtin-server-card {
          max-width: none;
          width: 100%;
        }
      }

      .builtin-server-card::part(body) {
        padding: var(--sl-spacing-small) var(--sl-spacing-medium);
      }

      .builtin-server-header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        margin-bottom: var(--sl-spacing-2x-small);
      }

      .builtin-server-name {
        font-size: var(--sl-font-size-medium);
        font-weight: var(--sl-font-weight-semibold);
        margin: 0;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }

      .info-row {
        display: flex;
        justify-content: space-between;
        align-items: center;
        padding: 0.15rem 0;
        font-size: var(--sl-font-size-x-small);
      }

      .info-label {
        font-weight: 600;
        color: var(--sl-color-neutral-700);
      }

      .info-value-container {
        display: flex;
        align-items: center;
        gap: var(--sl-spacing-x-small);
      }

      .info-value {
        color: var(--sl-color-neutral-900);
        font-family: monospace;
        background: var(--sl-color-neutral-50);
        padding: 0.15rem 0.4rem;
        border-radius: 4px;
        font-size: var(--sl-font-size-x-small);
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
        max-width: 220px;
      }

      .info-link {
        color: var(--sl-color-primary-600);
        text-decoration: none;
        font-size: var(--sl-font-size-x-small);
        white-space: nowrap;
      }

      .info-link:hover {
        text-decoration: underline;
      }

      sl-card::part(footer) {
        padding: var(--sl-spacing-x-small) var(--sl-spacing-medium);
        border-top: 1px solid var(--sl-color-neutral-200);
      }

      /* Summary table */
      .summary-table-wrapper {
        flex: 0 1 auto;
      }

      .summary-table {
        border-collapse: collapse;
        font-size: var(--sl-font-size-small);
      }

      .summary-table td {
        padding: 0;
        vertical-align: middle;
        white-space: nowrap;
      }

      .summary-stat {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: var(--sl-spacing-medium);
        color: var(--sl-color-neutral-700);
        cursor: pointer;
        padding: 4px 8px;
        border-radius: var(--sl-border-radius-small);
        transition: background 0.1s ease;
        white-space: nowrap;
      }

      .summary-stat:hover {
        background: var(--sl-color-neutral-100);
      }

      .summary-stat.active {
        background: var(--sl-color-primary-100);
        color: var(--sl-color-primary-700);
        font-weight: var(--sl-font-weight-semibold);
      }

      .summary-stat strong {
        font-variant-numeric: tabular-nums;
        color: var(--sl-color-neutral-900);
      }

      .summary-stat.active strong {
        color: var(--sl-color-primary-700);
      }

      .summary-stat.muted {
        opacity: 0.4;
        cursor: default;
      }

      .summary-stat.muted:hover {
        background: none;
      }

      /* Filter area: policy row + search row */
      .filter-area {
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-x-small);
        margin-bottom: var(--sl-spacing-large);
      }

      .policy-row {
        display: flex;
        justify-content: flex-end;
        margin-bottom: 0.6em;
      }

      .filter-bar {
        display: flex;
        align-items: center;
        gap: var(--sl-spacing-small);
        flex-wrap: wrap;
      }

      .filter-search {
        flex: 1;
        min-width: 200px;
      }

      .filter-buttons {
        display: flex;
        gap: var(--sl-spacing-2x-small);
        flex-shrink: 0;
        flex-wrap: wrap;
      }

      .filter-chip {
        font-size: var(--sl-font-size-x-small);
      }

      .filter-chip[variant='primary']::part(base) {
        font-weight: 600;
      }

      /* Section headers */
      .section-header {
        display: flex;
        align-items: center;
        gap: var(--sl-spacing-small);
        padding: var(--sl-spacing-small) 0;
        margin-top: var(--sl-spacing-medium);
        cursor: pointer;
        user-select: none;
      }

      .section-header:first-of-type {
        margin-top: 0;
      }

      .section-header:hover .section-title {
        color: var(--sl-color-neutral-900);
      }

      .section-icon {
        color: var(--sl-color-neutral-500);
        transition: transform 0.2s ease;
        flex-shrink: 0;
      }

      .section-icon.open {
        transform: rotate(90deg);
      }

      .section-title {
        font-size: var(--sl-font-size-small);
        font-weight: var(--sl-font-weight-bold);
        color: var(--sl-color-neutral-600);
        text-transform: uppercase;
        letter-spacing: 0.05em;
      }

      .section-meta {
        font-size: var(--sl-font-size-x-small);
        color: var(--sl-color-neutral-500);
      }

      .section-actions {
        display: flex;
        gap: var(--sl-spacing-2x-small);
      }

      .section-line {
        flex: 1;
        height: 1px;
        background: var(--sl-color-neutral-200);
      }

      /* Tool list */
      .tool-list {
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-x-small);
        margin-bottom: var(--sl-spacing-medium);
      }

      .loading-indicator {
        display: flex;
        justify-content: center;
        align-items: center;
        height: 200px;
      }

      .empty-state {
        text-align: center;
        padding: var(--sl-spacing-x-large);
        color: var(--sl-color-neutral-500);
      }

      /* Policy chip bar (above filter bar, right-aligned) */
      .policy-chip-bar {
        display: flex;
        align-items: center;
        gap: var(--sl-spacing-x-small);
        flex-wrap: wrap;
        font-size: var(--sl-font-size-small);
      }

      .policy-chip-bar-label {
        color: var(--sl-color-neutral-500);
        font-size: var(--sl-font-size-x-small);
        font-weight: var(--sl-font-weight-semibold);
        text-transform: uppercase;
        letter-spacing: 0.04em;
        white-space: nowrap;
        padding-right: var(--sl-spacing-2x-small);
      }

      .policy-chip {
        display: inline-flex;
        align-items: center;
        gap: 4px;
        padding: 2px 4px 2px 10px;
        border: 1px solid var(--sl-color-neutral-200);
        border-radius: var(--sl-border-radius-pill);
        font-size: var(--sl-font-size-x-small);
        color: var(--sl-color-neutral-700);
        cursor: pointer;
        transition: all 0.15s ease;
        white-space: nowrap;
        background: var(--sl-color-neutral-0);
      }

      .policy-chip:hover {
        border-color: var(--sl-color-neutral-400);
        background: var(--sl-color-neutral-50);
      }

      .policy-chip.active {
        border-color: var(--sl-color-primary-400);
        background: var(--sl-color-primary-50);
        color: var(--sl-color-primary-700);
      }

      .policy-chip .policy-chip-name {
        max-width: 180px;
        overflow: hidden;
        text-overflow: ellipsis;
      }

      .policy-chip sl-icon-button {
        font-size: 0.65rem;
        color: var(--sl-color-neutral-500);
      }

      .policy-chip sl-icon-button:hover {
        color: var(--sl-color-neutral-900);
      }

      .policy-chip .policy-chip-type {
        font-size: 0.6rem;
        padding: 1px 5px;
        border-radius: var(--sl-border-radius-pill);
        background: var(--sl-color-neutral-100);
        color: var(--sl-color-neutral-500);
      }

      .policy-chip.active .policy-chip-type {
        background: var(--sl-color-primary-100);
        color: var(--sl-color-primary-600);
      }

      .policy-chip-add {
        display: inline-flex;
        align-items: center;
        gap: 3px;
        padding: 2px 10px;
        border: 1px dashed var(--sl-color-neutral-300);
        border-radius: var(--sl-border-radius-pill);
        font-size: var(--sl-font-size-x-small);
        color: var(--sl-color-neutral-500);
        cursor: pointer;
        transition: all 0.15s ease;
        background: none;
        white-space: nowrap;
      }

      .policy-chip-add:hover {
        border-color: var(--sl-color-primary-400);
        color: var(--sl-color-primary-600);
        background: var(--sl-color-primary-50);
      }

      .policy-chip-empty {
        color: var(--sl-color-neutral-400);
        font-size: var(--sl-font-size-x-small);
        font-style: italic;
      }

      /* Active filter tags */
      .active-filters {
        display: flex;
        align-items: center;
        gap: var(--sl-spacing-2x-small);
        flex-wrap: wrap;
      }

      .active-filter-tag {
        display: inline-flex;
        align-items: center;
        gap: var(--sl-spacing-2x-small);
        padding: 2px 8px;
        background: var(--sl-color-primary-100);
        color: var(--sl-color-primary-700);
        border-radius: var(--sl-border-radius-pill);
        font-size: var(--sl-font-size-x-small);
        font-weight: 500;
      }

      .active-filter-tag sl-icon-button {
        font-size: 0.7rem;
      }

      .starter-policy-description {
        color: var(--sl-color-neutral-600);
        font-size: var(--sl-font-size-small);
        line-height: 1.5;
        margin: 0 0 var(--sl-spacing-medium);
      }

      .starter-policy-meta {
        color: var(--sl-color-neutral-500);
        font-size: var(--sl-font-size-x-small);
        margin-bottom: var(--sl-spacing-small);
      }

      .starter-policy-warnings {
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-x-small);
        margin-bottom: var(--sl-spacing-medium);
      }

      .starter-policy-preview-header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: var(--sl-spacing-small);
        margin-bottom: var(--sl-spacing-x-small);
      }

      .starter-policy-preview-title {
        color: var(--sl-color-neutral-700);
        font-size: var(--sl-font-size-small);
        font-weight: var(--sl-font-weight-semibold);
      }

      .starter-policy-preview {
        background: var(--sl-color-neutral-50);
        border: 1px solid var(--sl-color-neutral-200);
        border-radius: var(--sl-border-radius-medium);
        max-height: 420px;
        overflow: auto;
        padding: var(--sl-spacing-medium);
      }

      .starter-policy-preview pre {
        font-family: var(--sl-font-mono);
        font-size: 0.8125rem;
        line-height: 1.6;
        margin: 0;
        white-space: pre-wrap;
        word-break: break-word;
      }

      .starter-policy-loading {
        display: flex;
        flex-direction: column;
        align-items: center;
        gap: var(--sl-spacing-small);
        color: var(--sl-color-neutral-600);
        padding: var(--sl-spacing-large) 0;
      }

      .starter-policy-diff-summary {
        margin-bottom: var(--sl-spacing-medium);
      }

      .starter-policy-diff-container {
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-medium);
        margin-bottom: var(--sl-spacing-medium);
        max-height: 360px;
        overflow-y: auto;
      }

      .starter-policy-diff-section {
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-x-small);
      }

      .starter-policy-diff-section-title {
        display: flex;
        align-items: center;
        gap: var(--sl-spacing-small);
        color: var(--sl-color-neutral-700);
        font-size: var(--sl-font-size-small);
        font-weight: var(--sl-font-weight-semibold);
      }

      .starter-policy-diff-item {
        border-radius: var(--sl-border-radius-medium);
        border-left: 3px solid var(--sl-color-neutral-300);
        padding: var(--sl-spacing-small) var(--sl-spacing-medium);
      }

      .starter-policy-diff-item.add {
        background: var(--sl-color-success-50);
        border-left-color: var(--sl-color-success-600);
      }

      .starter-policy-diff-item.modify {
        background: var(--sl-color-warning-50);
        border-left-color: var(--sl-color-warning-600);
      }

      .starter-policy-diff-item.remove {
        background: var(--sl-color-danger-50);
        border-left-color: var(--sl-color-danger-600);
      }

      .starter-policy-diff-path {
        color: var(--sl-color-neutral-800);
        font-family: var(--sl-font-mono);
        font-size: 0.75rem;
        line-height: 1.5;
        word-break: break-word;
      }

      .starter-policy-diff-values {
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-x-small);
        margin-top: var(--sl-spacing-small);
      }

      .starter-policy-diff-value-label {
        display: inline-block;
        color: var(--sl-color-neutral-600);
        font-size: var(--sl-font-size-x-small);
        font-weight: var(--sl-font-weight-semibold);
        margin-bottom: 2px;
        text-transform: uppercase;
      }

      .starter-policy-diff-value pre {
        background: rgba(255, 255, 255, 0.75);
        border: 1px solid var(--sl-color-neutral-200);
        border-radius: var(--sl-border-radius-small);
        font-family: var(--sl-font-mono);
        font-size: 0.75rem;
        line-height: 1.5;
        margin: 0;
        max-height: 160px;
        overflow: auto;
        padding: var(--sl-spacing-x-small);
        white-space: pre-wrap;
        word-break: break-word;
      }

      .starter-policy-review-confirm {
        margin-bottom: var(--sl-spacing-medium);
      }

      .starter-policy-footer {
        display: flex;
        justify-content: flex-end;
        gap: var(--sl-spacing-small);
      }

      .tool-stats-panel {
        margin-top: var(--sl-spacing-medium);
        border-top: 1px solid var(--sl-color-neutral-200);
        padding-top: var(--sl-spacing-medium);
      }

      .tool-stats-title {
        font-size: var(--sl-font-size-x-small);
        font-weight: var(--sl-font-weight-bold);
        color: var(--sl-color-neutral-600);
        text-transform: uppercase;
        letter-spacing: 0.05em;
        margin-bottom: var(--sl-spacing-x-small);
      }

      .tool-stats-table {
        width: 100%;
        border-collapse: collapse;
        font-size: var(--sl-font-size-x-small);
      }

      .tool-stats-table th,
      .tool-stats-table td {
        padding: 4px 8px;
        text-align: left;
        border-bottom: 1px solid var(--sl-color-neutral-100);
      }

      .tool-stats-table th {
        color: var(--sl-color-neutral-500);
        font-weight: 600;
      }

      .tool-stats-table td:last-child,
      .tool-stats-table th:last-child {
        text-align: right;
      }
    `,
  ];

  private static readonly _FILTER_STORAGE_KEY = 'preloop:tools-filter';
  private _pendingStarterPolicyServerId: string | null = null;
  private _pendingStarterPolicyFallbackToLatest = false;
  private _handledOauthStarterPolicy = false;

  connectedCallback() {
    super.connectedCallback();
    const saved = localStorage.getItem(ToolsView._FILTER_STORAGE_KEY);
    if (saved) {
      this.activeFilter = saved as typeof this.activeFilter;
    }

    // Check for OAuth callback hash (#setup_mcp=success or #setup_mcp=error)
    if (window.location.hash) {
      const hashParams = new URLSearchParams(window.location.hash.substring(1));
      const setupMcp = hashParams.get('setup_mcp');
      if (setupMcp === 'success') {
        this.oauthAlert = 'success';
      } else if (setupMcp === 'error') {
        this.oauthAlert = 'error';
      }
      // Clean up the hash
      window.history.replaceState({}, '', window.location.pathname);
    }

    this.loadData();
  }

  private _setFilter(filter: typeof this.activeFilter) {
    this.activeFilter = filter;
    localStorage.setItem(ToolsView._FILTER_STORAGE_KEY, filter);
  }

  private async loadData() {
    this.loading = true;
    this.error = null;
    let starterPolicyRequest: {
      serverId: string | null;
      fallbackToLatest: boolean;
    } | null = null;

    try {
      const [
        tools,
        servers,
        policies,
        featuresResponse,
        currentUser,
        aiModels,
      ] = await Promise.all([
        getTools(),
        getMCPServers(),
        getApprovalWorkflows(),
        getFeatures(),
        getUserProfile(),
        getAIModels(),
      ]);

      this.currentUser = currentUser;
      this.features = featuresResponse.features || {};
      this.tools = tools as ToolWithRules[];
      this.mcpServers = servers;
      this.approvalPolicies = policies;
      this.hasDefaultAIModel = aiModels.some((model) => model.is_default);
      // Tool usage stats are intentionally async and must not block the tools
      // list — kick off after list data is assigned so first paint stays fast.
      void this._loadToolUsageStats();

      if (
        this._pendingStarterPolicyServerId ||
        this._pendingStarterPolicyFallbackToLatest
      ) {
        starterPolicyRequest = {
          serverId: this._pendingStarterPolicyServerId,
          fallbackToLatest: this._pendingStarterPolicyFallbackToLatest,
        };
        this._pendingStarterPolicyServerId = null;
        this._pendingStarterPolicyFallbackToLatest = false;
      } else if (
        this.oauthAlert === 'success' &&
        !this._handledOauthStarterPolicy
      ) {
        starterPolicyRequest = { serverId: null, fallbackToLatest: true };
        this._handledOauthStarterPolicy = true;
      }
    } catch (err: any) {
      this.error = err.message || 'Failed to load data';
      console.error('Error loading tools data:', err);
    } finally {
      this.loading = false;
      if (starterPolicyRequest) {
        void this._openStarterPolicySuggestion(starterPolicyRequest.serverId, {
          fallbackToLatest: starterPolicyRequest.fallbackToLatest,
        });
      }
    }
  }

  private async _loadToolUsageStats() {
    this.toolStatsLoading = true;
    try {
      const end = new Date();
      const start = new Date(end);
      start.setDate(start.getDate() - 30);
      const stats = await getToolUsageStats({
        startDate: start.toISOString(),
        endDate: end.toISOString(),
      });
      this.toolUsageStats = stats.tools || [];
    } catch {
      this.toolUsageStats = [];
    } finally {
      this.toolStatsLoading = false;
    }
  }

  private _getToolStatsMap(): Map<string, GatewayUsageByTool> {
    return new Map(this.toolUsageStats.map((row) => [row.tool_name, row]));
  }

  private _formatCurrency(value?: number | null): string {
    const amount = Number(value || 0);
    if (amount === 0) return '$0.00';
    return amount >= 0.01 ? `$${amount.toFixed(2)}` : `$${amount.toFixed(4)}`;
  }

  // ─── Stats helpers ──────────────────────────────────

  private _getStats() {
    const all = this.tools;
    const available = all.filter((t) => t.is_supported !== false);
    const unavailable = all.filter((t) => t.is_supported === false);
    const enabled = available.filter((t) => t.is_enabled);
    const disabled = available.filter((t) => !t.is_enabled);
    const builtin = available.filter((t) => t.source === 'builtin');
    const proxied = available.filter((t) => t.source === 'mcp');
    const withRules = all.filter(
      (t) =>
        (t.access_rules && t.access_rules.length > 0) ||
        t.approval_workflow_id ||
        t.has_approval_condition
    );
    const withoutRules = all.length - withRules.length;
    const requireApproval = all.filter(
      (t) =>
        t.access_rules?.some(
          (r) => r.action === 'require_approval' && r.is_enabled
        ) || t.approval_workflow_id
    );
    const noApproval = all.length - requireApproval.length;

    return {
      total: all.length,
      available: available.length,
      unavailable: unavailable.length,
      enabled: enabled.length,
      disabled: disabled.length,
      builtin: builtin.length,
      proxied: proxied.length,
      withRules: withRules.length,
      withoutRules,
      requireApproval: requireApproval.length,
      noApproval,
      unavailableReasons: [
        ...new Set(
          unavailable
            .map((t) => t.unsupported_reason)
            .filter((r): r is string => !!r)
        ),
      ],
    };
  }

  private _getFilteredTools(): ToolWithRules[] {
    let tools = this.tools;

    // Single active filter
    switch (this.activeFilter) {
      case 'all':
        break;
      case 'available':
        tools = tools.filter((t) => t.is_supported !== false);
        break;
      case 'enabled':
        tools = tools.filter((t) => t.is_supported !== false && t.is_enabled);
        break;
      case 'disabled':
        tools = tools.filter((t) => t.is_supported !== false && !t.is_enabled);
        break;
      case 'unavailable':
        tools = tools.filter((t) => t.is_supported === false);
        break;
      case 'builtin':
        tools = tools.filter((t) => t.source === 'builtin');
        break;
      case 'mcp':
        tools = tools.filter((t) => t.source === 'mcp');
        break;
      case 'has_rules':
        tools = tools.filter(
          (t) =>
            (t.access_rules && t.access_rules.length > 0) ||
            t.approval_workflow_id ||
            t.has_approval_condition
        );
        break;
      case 'no_rules':
        tools = tools.filter(
          (t) =>
            (!t.access_rules || t.access_rules.length === 0) &&
            !t.approval_workflow_id &&
            !t.has_approval_condition
        );
        break;
      case 'require_approval':
        tools = tools.filter(
          (t) =>
            t.access_rules?.some(
              (r) => r.action === 'require_approval' && r.is_enabled
            ) || t.approval_workflow_id
        );
        break;
      case 'no_approval':
        tools = tools.filter(
          (t) =>
            !t.access_rules?.some(
              (r) => r.action === 'require_approval' && r.is_enabled
            ) && !t.approval_workflow_id
        );
        break;
    }

    // Text filter
    if (this.filterText) {
      const search = this.filterText.toLowerCase();
      tools = tools.filter(
        (t) =>
          t.name.toLowerCase().includes(search) ||
          t.description?.toLowerCase().includes(search) ||
          t.source_name?.toLowerCase().includes(search)
      );
    }

    // Policy filter
    if (this.filterPolicyId) {
      tools = tools.filter(
        (t) => t.approval_workflow_id === this.filterPolicyId
      );
    }

    return tools;
  }

  private _getToolKey(tool: Tool): string {
    return `${tool.name}-${tool.source}-${tool.source_id || 'none'}`;
  }

  private _getServerTools(serverId: string): ToolWithRules[] {
    return this.tools.filter(
      (tool) => tool.source === 'mcp' && tool.source_id === serverId
    );
  }

  private _queueStarterPolicySuggestion(
    serverId: string | null,
    options: { fallbackToLatest?: boolean } = {}
  ) {
    this._pendingStarterPolicyServerId = serverId;
    this._pendingStarterPolicyFallbackToLatest =
      options.fallbackToLatest ?? false;
  }

  private _resolveStarterPolicyServer(
    serverId: string | null,
    options: { fallbackToLatest?: boolean } = {}
  ): MCPServer | null {
    if (serverId) {
      const matchingServer = this.mcpServers.find(
        (server) => server.id === serverId
      );
      if (matchingServer) {
        return matchingServer;
      }
    }

    if (!options.fallbackToLatest || this.mcpServers.length === 0) {
      return null;
    }

    return [...this.mcpServers].sort((a, b) => {
      const aTime = Date.parse(a.updated_at || a.created_at || '') || 0;
      const bTime = Date.parse(b.updated_at || b.created_at || '') || 0;
      return bTime - aTime;
    })[0];
  }

  private _buildStarterPolicyPrompt(
    server: MCPServer,
    tools: ToolWithRules[]
  ): string {
    const toolLines = tools
      .map((tool) => {
        const description = tool.description?.trim()
          ? `: ${tool.description.trim()}`
          : '';
        return `- ${tool.name}${description}`;
      })
      .join('\n');

    return `Generate a conservative starter policy update for the MCP server "${server.name}" (${server.url}).

Preserve the existing configuration and only add or adjust policy rules for this MCP server and its discovered tools.
Do not change rules for unrelated MCP servers, built-in tools, or HTTP tools.

Discovered tools:
${toolLines}

Policy intent:
- Allow clearly read-only, lookup, search, or otherwise low-risk tools without approval.
- Require approval for mutating, write, admin, destructive, irreversible, security-sensitive, or otherwise high-impact tools.
- If a tool's impact is ambiguous, prefer require_approval instead of allow.
- Do not add deny rules unless absolutely necessary.

Return valid Preloop policy YAML only.`;
  }

  private _starterPolicyFileName() {
    return this.starterPolicyServer
      ? `${this.starterPolicyServer.name.toLowerCase().replace(/[^a-z0-9]+/g, '-')}-starter-policy.yaml`
      : 'starter-policy.yaml';
  }

  private _getStarterPolicyDiffChanges(
    operation: StarterPolicyDiffChange['operation']
  ) {
    return (
      this.starterPolicyDiff?.changes.filter(
        (change) => change.operation === operation
      ) || []
    );
  }

  private _formatStarterPolicyDiffValue(value: unknown): string {
    if (value === undefined || value === null) {
      return '';
    }
    if (typeof value === 'string') {
      return value;
    }
    try {
      return JSON.stringify(value, null, 2);
    } catch {
      return String(value);
    }
  }

  private _renderStarterPolicyDiffSection(
    title: string,
    operation: StarterPolicyDiffChange['operation'],
    icon: string
  ) {
    const changes = this._getStarterPolicyDiffChanges(operation);
    if (changes.length === 0) {
      return html``;
    }

    return html`
      <div class="starter-policy-diff-section">
        <div class="starter-policy-diff-section-title">
          <sl-icon name=${icon}></sl-icon>
          <span>${title} (${changes.length})</span>
        </div>
        ${changes.map(
          (change) => html`
            <div class="starter-policy-diff-item ${operation}">
              <div class="starter-policy-diff-path">${change.path}</div>
              ${
                change.old_value !== undefined || change.new_value !== undefined
                  ? html`
                      <div class="starter-policy-diff-values">
                        ${
                          change.old_value !== undefined
                            ? html`
                                <div class="starter-policy-diff-value">
                                  <span class="starter-policy-diff-value-label"
                                    >Current</span
                                  >
                                  <pre>
${this._formatStarterPolicyDiffValue(change.old_value)}</pre>
                                </div>
                              `
                            : ''
                        }
                        ${
                          change.new_value !== undefined
                            ? html`
                                <div class="starter-policy-diff-value">
                                  <span class="starter-policy-diff-value-label"
                                    >Generated</span
                                  >
                                  <pre>
${this._formatStarterPolicyDiffValue(change.new_value)}</pre>
                                </div>
                              `
                            : ''
                        }
                      </div>
                    `
                  : ''
              }
            </div>
          `
        )}
      </div>
    `;
  }

  private async _previewStarterPolicyDiff(yaml: string) {
    this.isPreviewingStarterPolicyDiff = true;
    this.starterPolicyDiff = null;
    this.starterPolicyReviewConfirmed = false;

    try {
      const formData = new FormData();
      formData.append(
        'file',
        new File([yaml], this._starterPolicyFileName(), {
          type: 'application/x-yaml',
        })
      );

      const response = await fetchWithAuth('/api/v1/policies/diff', {
        method: 'POST',
        body: formData,
      });

      if (!response.ok) {
        const error = await response.json().catch(() => ({}));
        throw new Error(
          error.detail?.message ||
            error.detail ||
            'Failed to preview starter policy changes'
        );
      }

      this.starterPolicyDiff = (await response.json()) as StarterPolicyDiff;
    } finally {
      this.isPreviewingStarterPolicyDiff = false;
    }
  }

  private async _generateStarterPolicy(
    server: MCPServer,
    tools: ToolWithRules[]
  ) {
    this.isGeneratingStarterPolicy = true;
    this.starterPolicyError = null;
    this.starterPolicyYaml = '';
    this.starterPolicyWarnings = [];
    this.starterPolicyDiff = null;
    this.starterPolicyReviewConfirmed = false;

    try {
      const result = await generatePolicy({
        prompt: this._buildStarterPolicyPrompt(server, tools),
        includeCurrentConfig: true,
      });
      this.starterPolicyYaml = result.yaml;
      this.starterPolicyWarnings = result.warnings || [];
      await this._previewStarterPolicyDiff(result.yaml);
    } catch (err: any) {
      this.starterPolicyError =
        err.message || 'Failed to generate starter policy suggestion';
    } finally {
      this.isGeneratingStarterPolicy = false;
    }
  }

  private async _openStarterPolicySuggestion(
    serverId: string | null,
    options: { fallbackToLatest?: boolean; manual?: boolean } = {}
  ) {
    const server = this._resolveStarterPolicyServer(serverId, options);
    if (!server) {
      if (options.manual) {
        this.error = 'Could not determine which MCP server to use.';
      }
      return;
    }

    if (!this.hasDefaultAIModel) {
      if (options.manual) {
        this.error =
          'Set a default AI model in Settings > AI Models before generating a starter policy.';
      }
      return;
    }

    const serverTools = this._getServerTools(server.id);
    if (serverTools.length === 0) {
      if (options.manual) {
        this.error =
          'No MCP tools have been discovered for this server yet. Scan the server first.';
      }
      return;
    }

    this.showStarterPolicyDialog = true;
    this.starterPolicyServer = server;
    await this._generateStarterPolicy(server, serverTools);
  }

  private _closeStarterPolicyDialog() {
    this.showStarterPolicyDialog = false;
    this.starterPolicyServer = null;
    this.starterPolicyYaml = '';
    this.starterPolicyWarnings = [];
    this.starterPolicyError = null;
    this.isGeneratingStarterPolicy = false;
    this.isApplyingStarterPolicy = false;
    this.isPreviewingStarterPolicyDiff = false;
    this.starterPolicyDiff = null;
    this.starterPolicyReviewConfirmed = false;
  }

  private async _handleApplyStarterPolicy() {
    if (
      !this.starterPolicyYaml.trim() ||
      !this.starterPolicyDiff?.has_changes ||
      !this.starterPolicyReviewConfirmed
    ) {
      return;
    }

    this.isApplyingStarterPolicy = true;
    this.starterPolicyError = null;

    try {
      const formData = new FormData();
      formData.append(
        'file',
        new File([this.starterPolicyYaml], this._starterPolicyFileName(), {
          type: 'application/x-yaml',
        })
      );

      const response = await fetchWithAuth('/api/v1/policies/upload', {
        method: 'POST',
        body: formData,
      });

      if (!response.ok) {
        const error = await response.json().catch(() => ({}));
        throw new Error(
          error.detail?.message ||
            error.detail ||
            'Failed to apply starter policy'
        );
      }

      await this.loadData();
      this._closeStarterPolicyDialog();
      this.dispatchEvent(
        new CustomEvent('show-toast', {
          bubbles: true,
          composed: true,
          detail: { message: 'Starter policy applied.' },
        })
      );
    } catch (err: any) {
      this.starterPolicyError = err.message || 'Failed to apply starter policy';
    } finally {
      this.isApplyingStarterPolicy = false;
    }
  }

  // ─── Event handlers ──────────────────────────────────

  private async _handleToggleEnabled(e: CustomEvent) {
    const detail = e.detail;
    const tool: ToolWithRules = detail.tool ? detail.tool : detail;
    const isEnabled: boolean =
      detail.isEnabled !== undefined ? detail.isEnabled : !tool.is_enabled;

    try {
      this.tools = this.tools.map((t) => {
        if (this._getToolKey(t) === this._getToolKey(tool)) {
          return { ...t, is_enabled: isEnabled };
        }
        return t;
      });

      if (tool.config_id) {
        await updateToolConfiguration(tool.config_id, {
          is_enabled: isEnabled,
        });
      } else {
        const config = await createToolConfiguration({
          tool_name: tool.name,
          tool_source: tool.source,
          mcp_server_id: tool.source_id,
          is_enabled: isEnabled,
          account_id: '',
        });
        this.tools = this.tools.map((t) => {
          if (this._getToolKey(t) === this._getToolKey(tool)) {
            return { ...t, config_id: config.id };
          }
          return t;
        });
      }
    } catch (err: any) {
      this.error = err.message || 'Failed to toggle tool';
      await this.loadData();
    }
  }

  private async _handleSaveRule(e: CustomEvent) {
    const { tool, existingRule, formData } = e.detail as {
      tool: ToolWithRules;
      existingRule: AccessRuleSummary | null;
      formData: RuleFormData;
    };

    try {
      let configId: string = tool.config_id || '';
      if (!configId) {
        const config = await createToolConfiguration({
          tool_name: tool.name,
          tool_source: tool.source,
          mcp_server_id: tool.source_id,
          account_id: '',
        });
        configId = config.id;
        this.tools = this.tools.map((t) => {
          if (this._getToolKey(t) === this._getToolKey(tool)) {
            return { ...t, config_id: configId };
          }
          return t;
        });
      }

      if (existingRule) {
        await updateAccessRule(existingRule.id, {
          action: formData.action,
          condition_expression: formData.condition_expression,
          condition_type: formData.condition_type,
          description: formData.description,
          is_enabled: formData.is_enabled,
          approval_workflow_id: formData.approval_workflow_id,
        });
      } else {
        const existingRules = tool.access_rules || [];
        const maxPriority = existingRules.reduce(
          (max, r) => Math.max(max, r.priority),
          -1
        );

        await createAccessRule(configId, {
          action: formData.action,
          condition_expression: formData.condition_expression,
          condition_type: formData.condition_type,
          priority: maxPriority + 1,
          description: formData.description,
          is_enabled: formData.is_enabled,
          approval_workflow_id: formData.approval_workflow_id,
        });
      }

      await this.loadData();
    } catch (err: any) {
      this.error = err.message || 'Failed to save rule';
    }
  }

  private async _handleDeleteRule(e: CustomEvent) {
    const { rule } = e.detail;
    if (!confirm('Delete this access rule? This cannot be undone.')) {
      return;
    }

    try {
      await deleteAccessRule(rule.id);
      await this.loadData();
    } catch (err: any) {
      this.error = err.message || 'Failed to delete rule';
    }
  }

  private async _handleReorderRules(e: CustomEvent) {
    const { reorderedRules } = e.detail as {
      tool: any;
      reorderedRules: { id: string; priority: number }[];
    };

    try {
      // Update each rule's priority
      await Promise.all(
        reorderedRules.map((r) =>
          updateAccessRule(r.id, { priority: r.priority })
        )
      );
      await this.loadData();
    } catch (err: any) {
      this.error = err.message || 'Failed to reorder rules';
    }
  }

  private async _handlePolicyCreated() {
    try {
      // Reload policies so the new policy appears in all dropdowns
      this.approvalPolicies = await getApprovalWorkflows();
    } catch (err: any) {
      this.error = err.message || 'Failed to refresh approval workflows';
    }
  }

  // ─── MCP Server handlers ────────────────────────────

  private async _handleServerAdded(e: CustomEvent) {
    this.isAddingMCPServer = false;
    this._queueStarterPolicySuggestion(e.detail?.server?.id || null);
    await this.loadData();
  }

  private async _handleServerUpdated() {
    this.editingMCPServer = null;
    await this.loadData();
  }

  private _closeServerForm() {
    this.isAddingMCPServer = false;
    this.editingMCPServer = null;
  }

  private _handleServerEdit(e: CustomEvent) {
    this.editingMCPServer = e.detail.server;
    this.isAddingMCPServer = false;
  }

  private async _handleScanMCPServer(serverId: string) {
    try {
      this._queueStarterPolicySuggestion(serverId);
      await scanMCPServer(serverId);
      await this.loadData();
    } catch (err: any) {
      this.error = err.message || 'Failed to scan MCP server';
    }
  }

  private async _handleDeleteMCPServer(serverId: string) {
    try {
      await deleteMCPServer(serverId);
      await this.loadData();
    } catch (err: any) {
      this.error = err.message || 'Failed to delete MCP server';
    }
  }

  private async _handleToggleMCPServer(e: CustomEvent) {
    const { id, enabled } = e.detail;
    try {
      await updateMCPServer(id, {
        status: enabled ? 'active' : 'disabled',
      });
      await this.loadData();
    } catch (err: any) {
      this.error = err.message || 'Failed to update MCP server status';
    }
  }

  // ─── Import / Export ─────────────────────────────────

  private async _exportPolicies() {
    this.isExporting = true;
    try {
      const response = await fetchWithAuth(
        '/api/v1/policies/export?format=yaml'
      );
      if (!response.ok) {
        const error = await response.json();
        throw new Error(error.detail || 'Failed to export');
      }
      const blob = await response.blob();
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = 'preloop-tools-config.yaml';
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      window.URL.revokeObjectURL(url);
    } catch (err: any) {
      this.error = err.message || 'Failed to export configuration';
    } finally {
      this.isExporting = false;
    }
  }

  private _triggerImport() {
    const input = document.createElement('input');
    input.type = 'file';
    input.accept = '.yaml,.yml';
    input.onchange = async () => {
      const file = input.files?.[0];
      if (!file) return;
      await this._importFile(file);
    };
    input.click();
  }

  private async _importFile(file: File) {
    try {
      const formData = new FormData();
      formData.append('file', file);

      const response = await fetchWithAuth('/api/v1/policies/upload', {
        method: 'POST',
        body: formData,
      });

      if (!response.ok) {
        const error = await response.json();
        throw new Error(
          error.detail?.message || error.detail || 'Failed to import'
        );
      }

      await this.loadData();
    } catch (err: any) {
      this.error = err.message || 'Failed to import configuration';
    }
  }

  // ─── Approval Workflow handlers ────────────────────────

  private _openPolicyDialog(policy: ApprovalWorkflow | null = null) {
    this.editingPolicy = policy;
    this.showPolicyDialog = true;
  }

  private _closePolicyDialog() {
    this.showPolicyDialog = false;
    this.editingPolicy = null;
  }

  private async _handlePolicySaved() {
    this._closePolicyDialog();
    await this.loadData();
  }

  private async _handleDeletePolicy(policy: ApprovalWorkflow) {
    if (
      !confirm(
        `Delete approval workflow "${policy.name}"? This cannot be undone.`
      )
    ) {
      return;
    }
    try {
      await deleteApprovalWorkflow(policy.id);
      if (this.filterPolicyId === policy.id) {
        this.filterPolicyId = null;
      }
      await this.loadData();
    } catch (err: any) {
      this.error = err.message || 'Failed to delete policy';
    }
  }

  // ─── Render helpers ──────────────────────────────────

  private _clearFilters() {
    this._setFilter('available');
    this.filterPolicyId = null;
  }

  private _renderTopSection() {
    const apiUrl = window.location.origin;
    const mcpUrl = `${apiUrl}/mcp`;
    const stats = this._getStats();

    // Helper: renders a single table cell with label left, number right, full-cell hover
    const statCell = (
      label: string | TemplateResult,
      value: number,
      filterKey: typeof this.activeFilter | '__none__',
      opts?: { muted?: boolean; tooltip?: string }
    ) => {
      const isActive =
        filterKey !== '__none__' && this.activeFilter === filterKey;
      const isMuted = opts?.muted || false;
      const onClick =
        filterKey === '__none__' || isMuted
          ? undefined
          : () => {
              this._setFilter(
                this.activeFilter === filterKey ? 'available' : filterKey
              );
              this.filterPolicyId = null;
            };

      const inner = html`
        <span
          class="summary-stat ${isActive ? 'active' : ''} ${
            isMuted ? 'muted' : ''
          }"
          @click=${onClick}
        >
          <span>${label}</span>
          <strong>${value}</strong>
        </span>
      `;

      return html`<td>
        ${
          opts?.tooltip
            ? html`<sl-tooltip content=${opts.tooltip}>${inner}</sl-tooltip>`
            : inner
        }
      </td>`;
    };

    return html`
      <div class="top-section">
        <!-- Left: Summary table -->
        <div class="summary-table-wrapper">
          <table class="summary-table">
            <tr>
              ${statCell('Total toolss', stats.total, 'all')}
              ${statCell('Available', stats.available, 'available')}
              ${statCell('Unavailable', stats.unavailable, 'unavailable', {
                muted: stats.unavailable === 0,
                tooltip:
                  stats.unavailableReasons.length > 0
                    ? stats.unavailableReasons.join('; ')
                    : 'Some tools require trackers to be configured',
              })}
            </tr>
            <tr>
              <td></td>
              ${statCell('Enabled', stats.enabled, 'enabled')}
              ${statCell('Disabled', stats.disabled, 'disabled')}
            </tr>
            <tr>
              <td></td>
              ${statCell('Built-in', stats.builtin, 'builtin')}
              ${statCell('Proxied', stats.proxied, 'mcp')}
            </tr>
            <tr>
              <td></td>
              ${statCell('With rules', stats.withRules, 'has_rules')}
              ${statCell('No rules', stats.withoutRules, 'no_rules')}
            </tr>
            <tr>
              <td></td>
              ${statCell(
                'Require approval',
                stats.requireApproval,
                'require_approval'
              )}
              ${statCell('No approval', stats.noApproval, 'no_approval')}
            </tr>
          </table>
          ${0 ? this._renderToolUsageStatsPanel() : ''}
        </div>

        <!-- Right: MCP Server card -->
        <sl-card class="builtin-server-card">
          <div class="card-content">
            <div class="builtin-server-header">
              <h3 class="builtin-server-name">Preloop MCP Server</h3>
              <sl-badge variant="primary" size="small">Built-in</sl-badge>
            </div>
            <div class="info-row">
              <span class="info-label">URL:</span>
              <code class="info-value" title=${mcpUrl}>${mcpUrl}</code>
              <sl-tooltip content="Copy URL">
                <sl-icon-button
                  name="clipboard"
                  style="font-size: 1rem;"
                  @click=${() => {
                    navigator.clipboard.writeText(mcpUrl);
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
            <div class="info-row">
              <span class="info-label">Auth:</span>
              <div class="info-value-container">
                <code class="info-value">Bearer Token</code>
                <a href="/console/settings/api-keys" class="info-link">
                  Keys &rarr;
                </a>
              </div>
            </div>
          </div>
          <div slot="footer">
            <sl-button
              size="small"
              style="width: 100%;"
              @click=${() => (this.showSetupDialog = true)}
            >
              <sl-icon slot="prefix" name="info-circle"></sl-icon>
              Setup Instructions
            </sl-button>
          </div>
        </sl-card>

        <mcp-setup-dialog
          ?open=${this.showSetupDialog}
          @close=${() => (this.showSetupDialog = false)}
        ></mcp-setup-dialog>
      </div>
    `;
  }

  private _renderToolUsageStatsPanel() {
    const rows = this.toolUsageStats.slice(0, 8);
    if (this.toolStatsLoading) {
      return html`<div class="tool-stats-panel">
        <div class="tool-stats-title">Tool activity (30 days)</div>
        <sl-spinner style="font-size: 1rem;"></sl-spinner>
      </div>`;
    }
    if (!rows.length) {
      return html`<div class="tool-stats-panel">
        <div class="tool-stats-title">Tool activity (30 days)</div>
        <span
          style="color: var(--sl-color-neutral-500); font-size: var(--sl-font-size-x-small);"
          >No tool invocations recorded yet.</span
        >
      </div>`;
    }
    return html`
      <div class="tool-stats-panel">
        <div class="tool-stats-title">Tool activity (30 days)</div>
        <table class="tool-stats-table" aria-label="Tool activity stats">
          <thead>
            <tr>
              <th scope="col">Tool</th>
              <th scope="col">Calls</th>
              <th scope="col">Schema cost</th>
              <th scope="col">Avg / call</th>
            </tr>
          </thead>
          <tbody>
            ${rows.map(
              (row) => html`
                <tr>
                  <td>${row.tool_name}</td>
                  <td>${row.invocation_count.toLocaleString()}</td>
                  <td>${this._formatCurrency(row.estimated_schema_cost)}</td>
                  <td>${this._formatCurrency(row.avg_cost_per_invocation)}</td>
                </tr>
              `
            )}
          </tbody>
        </table>
      </div>
    `;
  }

  private _renderFilterBar() {
    const filteredPolicy = this.filterPolicyId
      ? this.approvalPolicies.find((p) => p.id === this.filterPolicyId)
      : null;

    // Labels for the active filter tag display
    const filterLabels: Record<string, string> = {
      all: 'All tools',
      available: 'Available',
      enabled: 'Enabled',
      disabled: 'Disabled',
      unavailable: 'Unavailable',
      builtin: 'Built-in',
      mcp: 'Proxied',
      has_rules: 'With rules',
      no_rules: 'No rules',
      require_approval: 'Require approval',
      no_approval: 'No approval',
    };

    return html`
      <div class="filter-area">
        <div class="policy-row">${this._renderPolicyChipBar()}</div>
        <div class="filter-bar">
          <sl-input
            class="filter-search"
            size="small"
            placeholder="Filter tools..."
            clearable
            @sl-input=${(e: Event) =>
              (this.filterText = (e.target as any).value)}
          >
            <sl-icon slot="prefix" name="search"></sl-icon>
          </sl-input>

          <div class="active-filters">
            ${
              this.activeFilter !== 'all'
                ? html`<span class="active-filter-tag">
                    ${filterLabels[this.activeFilter] || this.activeFilter}
                    <sl-icon-button
                      name="x-lg"
                      @click=${() => this._setFilter('available')}
                    ></sl-icon-button>
                  </span>`
                : ''
            }
            ${
              this.filterPolicyId && filteredPolicy
                ? html`<span class="active-filter-tag">
                    Policy: ${filteredPolicy.name}
                    <sl-icon-button
                      name="x-lg"
                      @click=${() => (this.filterPolicyId = null)}
                    ></sl-icon-button>
                  </span>`
                : ''
            }
          </div>
        </div>
      </div>
    `;
  }

  private _renderPolicyChipBar() {
    const policies = this.approvalPolicies;

    return html`
      <div class="policy-chip-bar">
        <span class="policy-chip-bar-label">Approval Workflows</span>
        ${
          policies.length === 0
            ? html`<span class="policy-chip-empty">None defined</span>`
            : policies.map((policy) => {
                const isActive = this.filterPolicyId === policy.id;
                const isAi = policy.approval_type === 'ai_driven';
                return html`
                  <span
                    class="policy-chip ${isActive ? 'active' : ''}"
                    @click=${() => {
                      this.filterPolicyId = isActive ? null : policy.id;
                    }}
                  >
                    <span class="policy-chip-name">${policy.name}</span>
                    <span class="policy-chip-type"
                      >${isAi ? 'AI' : 'Human'}</span
                    >
                    <sl-icon-button
                      name="pencil"
                      label="Edit policy"
                      @click=${(e: Event) => {
                        e.stopPropagation();
                        this._openPolicyDialog(policy);
                      }}
                    ></sl-icon-button>
                    <sl-icon-button
                      name="x-lg"
                      label="Delete policy"
                      @click=${(e: Event) => {
                        e.stopPropagation();
                        this._handleDeletePolicy(policy);
                      }}
                    ></sl-icon-button>
                  </span>
                `;
              })
        }
        <span
          class="policy-chip-add"
          @click=${() => this._openPolicyDialog(null)}
        >
          <sl-icon name="plus-lg" style="font-size: 0.7rem;"></sl-icon>
          New
        </span>
      </div>
    `;
  }

  render() {
    const removedStarterPolicyChanges =
      this._getStarterPolicyDiffChanges('remove');

    return html`
      <view-header headerText="Tools" width="extra-wide">
        <div slot="main-column">
          <sl-dropdown>
            <sl-button slot="trigger" size="small" variant="primary" caret>
              <sl-icon slot="prefix" name="plus-lg"></sl-icon>
              Add Source
            </sl-button>
            <sl-menu>
              <sl-menu-item @click=${() => (this.isAddingMCPServer = true)}>
                <sl-icon slot="prefix" name="hdd-network"></sl-icon>
                MCP Server
              </sl-menu-item>
              <sl-menu-item disabled>
                <sl-icon slot="prefix" name="globe"></sl-icon>
                HTTP Tool (coming soon)
              </sl-menu-item>
            </sl-menu>
          </sl-dropdown>

          <sl-tooltip content="Import configuration from YAML">
            <sl-button size="small" @click=${this._triggerImport}>
              <sl-icon slot="prefix" name="upload"></sl-icon>
              Import
            </sl-button>
          </sl-tooltip>

          <sl-tooltip content="Export full tool configuration as YAML">
            <sl-button
              size="small"
              ?loading=${this.isExporting}
              @click=${this._exportPolicies}
            >
              <sl-icon slot="prefix" name="download"></sl-icon>
              Export
            </sl-button>
          </sl-tooltip>
        </div>
      </view-header>

      <div class="column-layout extra-wide">
        <div class="main-column">
          ${
            this.isAddingMCPServer
              ? html`<mcp-server-form
                  @server-added=${this._handleServerAdded}
                  @close-modal=${this._closeServerForm}
                ></mcp-server-form>`
              : ''
          }
          ${
            this.editingMCPServer
              ? html`<mcp-server-form
                  .server=${this.editingMCPServer}
                  @server-updated=${this._handleServerUpdated}
                  @close-modal=${this._closeServerForm}
                ></mcp-server-form>`
              : ''
          }
          ${
            this.oauthAlert === 'success'
              ? html`<sl-alert
                  variant="success"
                  open
                  closable
                  @sl-after-hide=${() => (this.oauthAlert = null)}
                >
                  <sl-icon slot="icon" name="check2-circle"></sl-icon>
                  <strong>OAuth Connected!</strong> The MCP server has been
                  successfully authenticated via OAuth.
                </sl-alert>`
              : ''
          }
          ${
            this.oauthAlert === 'error'
              ? html`<sl-alert
                  variant="danger"
                  open
                  closable
                  @sl-after-hide=${() => (this.oauthAlert = null)}
                >
                  <sl-icon slot="icon" name="exclamation-octagon"></sl-icon>
                  <strong>OAuth Failed</strong> — Could not authenticate with
                  the external MCP server. Please try again.
                </sl-alert>`
              : ''
          }
          ${
            this.error
              ? html`<sl-alert
                  variant="danger"
                  open
                  closable
                  @sl-after-hide=${() => (this.error = null)}
                >
                  <sl-icon slot="icon" name="exclamation-octagon"></sl-icon>
                  <strong>Error:</strong> ${this.error}
                </sl-alert>`
              : ''
          }
          ${
            this.loading
              ? html`<div class="loading-indicator">
                  <sl-spinner></sl-spinner>
                </div>`
              : html` ${this._renderTopSection()}
                  <div class="tool-groups">
                    ${this._renderFilterBar()}
                    <tools-editor-component
                      .tools=${this._getFilteredTools()}
                      .toolStats=${Object.fromEntries(this._getToolStatsMap())}
                      .mcpServers=${this.mcpServers}
                      .approvalPolicies=${this.approvalPolicies}
                      .features=${this.features}
                      .hasDefaultAIModel=${this.hasDefaultAIModel}
                      mode="global"
                      @toggle-enabled=${this._handleToggleEnabled}
                      @save-rule=${this._handleSaveRule}
                      @delete-rule=${this._handleDeleteRule}
                      @policy-created=${this._handlePolicyCreated}
                      @reorder-rules=${this._handleReorderRules}
                      @tool-updated=${() => this.loadData()}
                      @scan-server=${(e: CustomEvent) =>
                        this._handleScanMCPServer(e.detail)}
                      @suggest-starter-policy=${(e: CustomEvent) =>
                        this._openStarterPolicySuggestion(e.detail, {
                          manual: true,
                        })}
                      @edit-server=${(e: CustomEvent) =>
                        (this.editingMCPServer = e.detail)}
                      @delete-server=${(e: CustomEvent) =>
                        this._handleDeleteMCPServer(e.detail)}
                    ></tools-editor-component>
                  </div>`
          }
        </div>
        <div class="side-column"></div>
      </div>

      <approval-workflow-dialog
        ?open=${this.showPolicyDialog}
        .policy=${this.editingPolicy}
        .existingPolicies=${this.approvalPolicies}
        .features=${this.features}
        @close=${this._closePolicyDialog}
        @saved=${this._handlePolicySaved}
      ></approval-workflow-dialog>

      <sl-dialog
        label=${
          this.starterPolicyServer
            ? `Starter Policy Suggestion: ${this.starterPolicyServer.name}`
            : 'Starter Policy Suggestion'
        }
        ?open=${this.showStarterPolicyDialog}
        @sl-request-close=${this._closeStarterPolicyDialog}
        @sl-after-hide=${this._closeStarterPolicyDialog}
      >
        <p class="starter-policy-description">
          Review the generated YAML before applying it. This suggestion
          preserves current configuration context and only targets the selected
          MCP server.
        </p>

        ${
          this.starterPolicyServer
            ? html`<div class="starter-policy-meta">
                Server: <code>${this.starterPolicyServer.name}</code>
                (${this._getServerTools(this.starterPolicyServer.id).length}
                tools)
              </div>`
            : ''
        }
        ${
          this.starterPolicyError
            ? html`<sl-alert variant="danger" open>
                <sl-icon slot="icon" name="exclamation-octagon"></sl-icon>
                ${this.starterPolicyError}
              </sl-alert>`
            : ''
        }
        ${
          this.starterPolicyWarnings.length > 0
            ? html`<div class="starter-policy-warnings">
                ${this.starterPolicyWarnings.map(
                  (warning) =>
                    html`<sl-alert variant="warning" open>
                      <sl-icon
                        slot="icon"
                        name="exclamation-triangle"
                      ></sl-icon>
                      ${warning}
                    </sl-alert>`
                )}
              </div>`
            : ''
        }
        ${
          this.isPreviewingStarterPolicyDiff
            ? html`<div class="starter-policy-loading">
                <sl-spinner></sl-spinner>
                <span>Comparing generated policy against current state…</span>
              </div>`
            : this.starterPolicyDiff
              ? html`
                  <div class="starter-policy-diff-summary">
                    <sl-alert
                      variant=${
                        this.starterPolicyDiff.has_changes
                          ? 'primary'
                          : 'success'
                      }
                      open
                    >
                      <sl-icon
                        slot="icon"
                        name=${
                          this.starterPolicyDiff.has_changes
                            ? 'eye'
                            : 'check2-circle'
                        }
                      ></sl-icon>
                      ${
                        this.starterPolicyDiff.summary ||
                        (this.starterPolicyDiff.has_changes
                          ? 'Review these changes before applying.'
                          : 'No changes detected against the current policy.')
                      }
                    </sl-alert>
                  </div>
                  ${
                    removedStarterPolicyChanges.length > 0
                      ? html`<sl-alert variant="danger" open>
                          <sl-icon
                            slot="icon"
                            name="exclamation-octagon"
                          ></sl-icon>
                          This generated policy removes
                          ${removedStarterPolicyChanges.length} existing
                          configuration
                          item${
                            removedStarterPolicyChanges.length === 1 ? '' : 's'
                          }.
                        </sl-alert>`
                      : ''
                  }
                  ${
                    this.starterPolicyDiff.has_changes
                      ? html`
                          <div class="starter-policy-diff-container">
                            ${this._renderStarterPolicyDiffSection(
                              'Added',
                              'add',
                              'plus-circle-fill'
                            )}
                            ${this._renderStarterPolicyDiffSection(
                              'Modified',
                              'modify',
                              'pencil-fill'
                            )}
                            ${this._renderStarterPolicyDiffSection(
                              'Removed',
                              'remove',
                              'dash-circle-fill'
                            )}
                          </div>
                          <sl-checkbox
                            class="starter-policy-review-confirm"
                            ?checked=${this.starterPolicyReviewConfirmed}
                            @sl-change=${(e: Event) =>
                              (this.starterPolicyReviewConfirmed = (
                                e.target as any
                              ).checked)}
                          >
                            I reviewed this diff and want to apply these changes
                            to the current policy.
                          </sl-checkbox>
                        `
                      : ''
                  }
                `
              : html``
        }
        ${
          this.isGeneratingStarterPolicy
            ? html`<div class="starter-policy-loading">
                <sl-spinner></sl-spinner>
                <span>Generating starter policy…</span>
              </div>`
            : this.starterPolicyYaml
              ? html`
                  <div class="starter-policy-preview-header">
                    <span class="starter-policy-preview-title"
                      >Generated YAML</span
                    >
                    <sl-copy-button
                      .value=${this.starterPolicyYaml}
                    ></sl-copy-button>
                  </div>
                  <div class="starter-policy-preview">
                    <pre>${this.starterPolicyYaml}</pre>
                  </div>
                `
              : html``
        }

        <div slot="footer" class="starter-policy-footer">
          <sl-button variant="default" @click=${this._closeStarterPolicyDialog}>
            Close
          </sl-button>
          <sl-button
            variant="default"
            ?disabled=${
              !this.starterPolicyServer || this.isApplyingStarterPolicy
            }
            ?loading=${this.isGeneratingStarterPolicy}
            @click=${() =>
              this.starterPolicyServer &&
              this._generateStarterPolicy(
                this.starterPolicyServer,
                this._getServerTools(this.starterPolicyServer.id)
              )}
          >
            <sl-icon slot="prefix" name="magic"></sl-icon>
            Regenerate
          </sl-button>
          <sl-button
            variant="primary"
            ?disabled=${
              !this.starterPolicyYaml ||
              this.isGeneratingStarterPolicy ||
              this.isPreviewingStarterPolicyDiff ||
              !this.starterPolicyDiff?.has_changes ||
              !this.starterPolicyReviewConfirmed
            }
            ?loading=${this.isApplyingStarterPolicy}
            @click=${this._handleApplyStarterPolicy}
          >
            <sl-icon slot="prefix" name="check-lg"></sl-icon>
            Apply Reviewed Changes
          </sl-button>
        </div>
      </sl-dialog>
    `;
  }
}
