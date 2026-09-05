import { LitElement, html, css, unsafeCSS } from 'lit';
import { customElement, state } from 'lit/decorators.js';
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
  getAccountGovernanceDefaults,
  updateAccountGovernanceDefaults,
  getAccountAgents,
} from '../../api';
import type { AccountGovernanceDefaults } from '../../types';
import '../../components/mcp-server-form';
import '../../components/tools-editor-component';
import '../../components/mcp-setup-dialog';
import '../../components/approval-workflow-dialog';
import '../../components/list-toolbar';
import type { Tool, ApprovalWorkflow } from '../../components/tool-card';
import type { MCPServer } from '../../components/mcp-server-card';
import type { AccessRuleSummary } from '../../components/governance-rule-set-editor';
import type { RuleFormData } from '../../components/tool-rule-editor';
import {
  loadViewMode,
  saveViewMode,
  type ListViewMode,
} from '../../utils/view-mode';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/icon-button/icon-button.js';
import '@shoelace-style/shoelace/dist/components/dialog/dialog.js';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/copy-button/copy-button.js';
import '@shoelace-style/shoelace/dist/components/dropdown/dropdown.js';
import '@shoelace-style/shoelace/dist/components/menu/menu.js';
import '@shoelace-style/shoelace/dist/components/menu-item/menu-item.js';
import '@shoelace-style/shoelace/dist/components/menu-label/menu-label.js';
import '@shoelace-style/shoelace/dist/components/tooltip/tooltip.js';
import '@shoelace-style/shoelace/dist/components/checkbox/checkbox.js';
import '@shoelace-style/shoelace/dist/components/switch/switch.js';
import '@shoelace-style/shoelace/dist/components/select/select.js';
import '@shoelace-style/shoelace/dist/components/option/option.js';
import '@shoelace-style/shoelace/dist/components/tab-group/tab-group.js';
import '@shoelace-style/shoelace/dist/components/tab/tab.js';
import '@shoelace-style/shoelace/dist/components/tab-panel/tab-panel.js';
import consoleStyles from '../../styles/console-styles.css?inline';

import {
  NATIVE_ADAPTERS,
  nativeAdapterGroupName,
  SEEN_FROM_AGENTS_LABEL,
  type ToolWithRules,
} from '../../components/tools-editor-component';
import type { GatewayUsageByTool } from '../../types';
import { consoleDialogStyles } from '../../styles/console-dialog';

type ToolsTab = 'mcp' | 'native';

interface ToolsFilters {
  query: string;
  statuses: string[];
  servers: string[];
  rules: string[];
  workflows: string[];
}

interface NativeFilters {
  query: string;
  agents: string[];
  rules: string[];
}

const TAB_STORAGE_KEY = 'preloop.tools.tab';
const VIEW_MODE_KEY = 'preloop.tools.view_mode';
// Cards only until the flat table lands; a List/Cards toggle would persist
// a choice that does not change the page.
const TOOLS_VIEWS: ListViewMode[] = ['cards'];
const EMPTY_FILTERS: ToolsFilters = {
  query: '',
  statuses: [],
  servers: [],
  rules: [],
  workflows: [],
};
const EMPTY_NATIVE_FILTERS: NativeFilters = {
  query: '',
  agents: [],
  rules: [],
};

function isToolsTab(value: string | null | undefined): value is ToolsTab {
  return value === 'mcp' || value === 'native';
}

function selectValue(event: Event): string {
  const select = event.target as HTMLElement & { value: string | string[] };
  const value = Array.isArray(select.value) ? select.value[0] : select.value;
  return value || '';
}

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
  // Account-wide native tool-approval defaults (inherited by agents).
  @state() private governanceDefaults: AccountGovernanceDefaults | null = null;
  @state() private governanceOverrideAgents: {
    id: string;
    name: string;
  }[] = [];
  @state() private savingGovernanceDefaults = false;
  @state() private governanceSaveError: string | null = null;
  @state() private governanceLoadFailed = false;
  @state() private isAddingMCPServer = false;
  @state() private editingMCPServer: MCPServer | null = null;
  @state() private currentUser: { id?: string } | null = null;
  @state() private showSetupDialog = false;
  @state() private features: { [key: string]: boolean | string[] } = {};
  @state() private activeTab: ToolsTab = 'mcp';
  @state() private viewMode: ListViewMode = loadViewMode(VIEW_MODE_KEY);
  @state() private filters: ToolsFilters = { ...EMPTY_FILTERS };
  @state() private nativeFilters: NativeFilters = { ...EMPTY_NATIVE_FILTERS };
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

  // Approval workflow dialog
  @state() private showPolicyDialog = false;
  @state() private editingPolicy: ApprovalWorkflow | null = null;

  static styles = [
    consoleDialogStyles,
    unsafeCSS(consoleStyles),
    css`
      mcp-setup-dialog {
        display: contents;
      }

      .tab-intro {
        color: var(--console-meta-color, var(--sl-color-neutral-600));
        font-size: var(--console-text-meta, var(--sl-font-size-small));
        margin: 0 0 var(--sl-spacing-small);
      }

      .summary-strip {
        display: flex;
        flex-wrap: wrap;
        align-items: baseline;
        gap: 8px 0;
        padding: 12px 0;
        border-top: 1px solid
          var(--console-hairline, var(--sl-color-neutral-200));
        border-bottom: 1px solid
          var(--console-hairline, var(--sl-color-neutral-200));
        margin-bottom: var(--sl-spacing-medium);
        color: var(--sl-color-neutral-900);
        font-size: var(--console-text-body, var(--sl-font-size-small));
        font-variant-numeric: tabular-nums;
      }

      .strip-sep {
        color: var(--console-meta-color, var(--sl-color-neutral-500));
        margin: 0 8px;
      }

      .strip-count {
        background: none;
        border: none;
        padding: 0;
        color: inherit;
        font: inherit;
        font-variant-numeric: tabular-nums;
        cursor: pointer;
      }

      .strip-count:hover {
        color: var(--sl-color-primary-700);
      }

      .strip-count.active {
        font-weight: var(--sl-font-weight-semibold);
        color: var(--sl-color-primary-700);
      }

      /* Meta note at the end of a summary strip (token cost on MCP, the
         account default on Native). */
      .strip-note {
        color: var(--console-meta-color, var(--sl-color-neutral-600));
      }

      .strip-note strong {
        font-variant-numeric: tabular-nums;
        color: var(--sl-color-neutral-900);
      }

      .toolbar-wrap {
        width: 100%;
        margin-bottom: var(--sl-spacing-medium);
      }

      sl-select::part(form-control-label) {
        position: absolute;
        width: 1px;
        height: 1px;
        padding: 0;
        margin: -1px;
        overflow: hidden;
        clip: rect(0 0 0 0);
        white-space: nowrap;
        border: 0;
      }

      .defaults-strip {
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-x-small);
        padding: 12px 0;
        border-top: 1px solid
          var(--console-hairline, var(--sl-color-neutral-200));
        border-bottom: 1px solid
          var(--console-hairline, var(--sl-color-neutral-200));
        margin-bottom: var(--sl-spacing-medium);
      }

      .defaults-strip-row {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        gap: 8px 12px;
      }

      .defaults-strip .meta-line {
        color: var(--console-meta-color, var(--sl-color-neutral-600));
        font-size: var(--console-text-meta, var(--sl-font-size-small));
      }

      .defaults-strip .governance-notice {
        color: var(--sl-color-warning-700);
        font-size: var(--sl-font-size-small);
        display: flex;
        gap: 6px;
        align-items: center;
      }

      .defaults-strip .governance-error {
        color: var(--sl-color-danger-600);
        font-size: var(--sl-font-size-small);
      }

      .native-empty {
        color: var(--console-meta-color, var(--sl-color-neutral-600));
        font-size: var(--console-text-body, var(--sl-font-size-small));
        padding: var(--sl-spacing-large) 0;
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
    `,
  ];

  private _pendingStarterPolicyServerId: string | null = null;
  private _pendingStarterPolicyFallbackToLatest = false;
  private _handledOauthStarterPolicy = false;

  connectedCallback() {
    super.connectedCallback();
    this.activeTab = this._resolveInitialTab();

    // Check for OAuth callback hash (#setup_mcp=success or #setup_mcp=error)
    if (window.location.hash) {
      const hashParams = new URLSearchParams(window.location.hash.substring(1));
      const setupMcp = hashParams.get('setup_mcp');
      if (setupMcp === 'success') {
        this.oauthAlert = 'success';
      } else if (setupMcp === 'error') {
        this.oauthAlert = 'error';
      }
      // Arriving from an approval's "Review this rule" link. There is no
      // per-tool route yet, so narrow the list to that tool instead of
      // dropping the reviewer into the full catalogue.
      const tool = hashParams.get('tool');
      if (tool) {
        this.filters = { ...this.filters, query: tool };
      }
      // Clean up the hash without dropping ?tab=.
      const url = new URL(window.location.href);
      url.hash = '';
      window.history.replaceState({}, '', `${url.pathname}${url.search}`);
    }

    this._rememberTab(this.activeTab);
    this.loadData();
  }

  private _resolveInitialTab(): ToolsTab {
    const fromUrl = new URLSearchParams(window.location.search).get('tab');
    if (isToolsTab(fromUrl)) {
      return fromUrl;
    }
    try {
      const remembered = window.localStorage.getItem(TAB_STORAGE_KEY);
      if (isToolsTab(remembered)) {
        return remembered;
      }
    } catch {
      // Private-mode storage failures must not keep the page from rendering.
    }
    return 'mcp';
  }

  private _rememberTab(tab: ToolsTab) {
    try {
      window.localStorage.setItem(TAB_STORAGE_KEY, tab);
    } catch {
      // Remembering the tab is a convenience, not a requirement.
    }
    const url = new URL(window.location.href);
    if (url.searchParams.get('tab') !== tab) {
      url.searchParams.set('tab', tab);
      window.history.replaceState({}, '', `${url.pathname}${url.search}`);
    }
  }

  private _handleTabShow(event: CustomEvent<{ name: string }>) {
    const name = event.detail?.name;
    if (!isToolsTab(name)) {
      return;
    }
    this.activeTab = name;
    this._rememberTab(name);
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
      // Same for the native-approvals defaults card: best-effort side data.
      void this._loadGovernanceDefaults();

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
    }
  }

  private _getToolStatsMap(): Map<string, GatewayUsageByTool> {
    return new Map(this.toolUsageStats.map((row) => [row.tool_name, row]));
  }

  private _isMcpTool(tool: ToolWithRules): boolean {
    return tool.source !== 'agent';
  }

  private _mcpTools(): ToolWithRules[] {
    return this.tools.filter((tool) => this._isMcpTool(tool));
  }

  private _nativeTools(): ToolWithRules[] {
    return this.tools.filter((tool) => tool.source === 'agent');
  }

  private _toolHasRules(tool: ToolWithRules): boolean {
    return Boolean(
      (tool.access_rules && tool.access_rules.length > 0) ||
      tool.approval_workflow_id ||
      tool.has_approval_condition
    );
  }

  private _toolRequiresApproval(tool: ToolWithRules): boolean {
    return Boolean(
      tool.access_rules?.some(
        (rule) => rule.action === 'require_approval' && rule.is_enabled
      ) || tool.approval_workflow_id
    );
  }

  private _toolUsesWorkflow(tool: ToolWithRules, workflowId: string): boolean {
    return (
      tool.approval_workflow_id === workflowId ||
      Boolean(
        tool.access_rules?.some(
          (rule) => rule.approval_workflow_id === workflowId
        )
      )
    );
  }

  private _toolMatchesStatus(tool: ToolWithRules, status: string): boolean {
    const available = tool.is_supported !== false;
    if (status === 'enabled') {
      return available && tool.is_enabled;
    }
    if (status === 'disabled') {
      return available && !tool.is_enabled;
    }
    if (status === 'unavailable') {
      return !available;
    }
    return true;
  }

  private _toolMatchesServer(tool: ToolWithRules, server: string): boolean {
    if (server === 'preloop') {
      return tool.source === 'builtin';
    }
    return tool.source === 'mcp' && tool.source_id === server;
  }

  private _toolMatchesRuleFilter(tool: ToolWithRules, rule: string): boolean {
    if (rule === 'with_rules') {
      return this._toolHasRules(tool);
    }
    if (rule === 'no_rules') {
      return !this._toolHasRules(tool);
    }
    if (rule === 'require_approval') {
      return this._toolRequiresApproval(tool);
    }
    return true;
  }

  // ─── Stats helpers ──────────────────────────────────

  private _getStats() {
    const all = this._mcpTools();
    const available = all.filter((tool) => tool.is_supported !== false);
    const unavailable = all.filter((tool) => tool.is_supported === false);
    const enabled = available.filter((tool) => tool.is_enabled);
    const withRules = all.filter((tool) => this._toolHasRules(tool));
    const requireApproval = all.filter((tool) =>
      this._toolRequiresApproval(tool)
    );
    const contextTaxTokens = enabled.reduce((sum, tool) => {
      const tokens = tool.schema_tokens_estimate;
      return sum + (typeof tokens === 'number' && tokens > 0 ? tokens : 0);
    }, 0);

    return {
      total: all.length,
      unavailable: unavailable.length,
      enabled: enabled.length,
      withRules: withRules.length,
      requireApproval: requireApproval.length,
      contextTaxTokens,
      unavailableReasons: [
        ...new Set(
          unavailable
            .map((tool) => tool.unsupported_reason)
            .filter((reason): reason is string => !!reason)
        ),
      ],
    };
  }

  private _getFilteredTools(): ToolWithRules[] {
    let tools = this._mcpTools();
    const { query, statuses, servers, rules, workflows } = this.filters;

    if (statuses.length > 0) {
      tools = tools.filter((tool) =>
        statuses.some((status) => this._toolMatchesStatus(tool, status))
      );
    }
    if (servers.length > 0) {
      tools = tools.filter((tool) =>
        servers.some((server) => this._toolMatchesServer(tool, server))
      );
    }
    if (rules.length > 0) {
      tools = tools.filter((tool) =>
        rules.some((rule) => this._toolMatchesRuleFilter(tool, rule))
      );
    }
    if (workflows.length > 0) {
      tools = tools.filter((tool) =>
        workflows.some((workflowId) => this._toolUsesWorkflow(tool, workflowId))
      );
    }
    if (query) {
      const search = query.toLowerCase();
      tools = tools.filter(
        (tool) =>
          tool.name.toLowerCase().includes(search) ||
          tool.description?.toLowerCase().includes(search) ||
          tool.source_name?.toLowerCase().includes(search)
      );
    }

    return tools;
  }

  private _toolMatchesNativeRuleFilter(
    tool: ToolWithRules,
    rule: string
  ): boolean {
    if (rule === 'blocked') {
      return !tool.is_enabled;
    }
    if (rule === 'allowed') {
      return tool.is_enabled;
    }
    return this._toolMatchesRuleFilter(tool, rule);
  }

  private _getFilteredNativeTools(): ToolWithRules[] {
    let tools = this._nativeTools();
    const { query, agents, rules } = this.nativeFilters;

    if (agents.length > 0) {
      const wanted = new Set(
        agents.map((agent) => nativeAdapterGroupName(agent))
      );
      tools = tools.filter((tool) => {
        const names =
          tool.adapters && tool.adapters.length > 0
            ? tool.adapters.map((adapter) => nativeAdapterGroupName(adapter))
            : [SEEN_FROM_AGENTS_LABEL];
        return names.some((name) => wanted.has(name));
      });
    }
    if (rules.length > 0) {
      tools = tools.filter((tool) =>
        rules.some((rule) => this._toolMatchesNativeRuleFilter(tool, rule))
      );
    }
    if (query) {
      const search = query.toLowerCase();
      tools = tools.filter(
        (tool) =>
          tool.name.toLowerCase().includes(search) ||
          tool.description?.toLowerCase().includes(search) ||
          (tool.adapters || []).some((adapter) =>
            adapter.toLowerCase().includes(search)
          )
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
        scopeMcpServerName: server.name,
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
      if (this.filters.workflows.includes(policy.id)) {
        this.filters = {
          ...this.filters,
          workflows: this.filters.workflows.filter((id) => id !== policy.id),
        };
      }
      await this.loadData();
    } catch (err: any) {
      this.error = err.message || 'Failed to delete policy';
    }
  }

  // ─── Render helpers ──────────────────────────────────

  private _clearFilters() {
    this.filters = { ...EMPTY_FILTERS };
  }

  private _setFilterValues(patch: Partial<ToolsFilters>) {
    this.filters = { ...this.filters, ...patch };
  }

  private _toggleSingleFilter(
    key: 'statuses' | 'servers' | 'rules' | 'workflows',
    value: string
  ) {
    const current = this.filters[key];
    const already = current.length === 1 && current[0] === value;
    this._setFilterValues({ [key]: already ? [] : [value] });
  }

  private _handleSearchChange(event: CustomEvent<{ value: string }>) {
    this._setFilterValues({ query: event.detail.value });
  }

  private _handleViewChange(event: CustomEvent<{ value: ListViewMode }>) {
    this.viewMode = event.detail.value;
    saveViewMode(VIEW_MODE_KEY, event.detail.value);
  }

  private _handleStatusFilterChange(event: Event) {
    const value = selectValue(event);
    this._setFilterValues({ statuses: value ? [value] : [] });
  }

  private _handleServerFilterChange(event: Event) {
    const value = selectValue(event);
    this._setFilterValues({ servers: value ? [value] : [] });
  }

  private _handleRulesFilterChange(event: Event) {
    const value = selectValue(event);
    this._setFilterValues({ rules: value ? [value] : [] });
  }

  private _handleWorkflowFilterChange(event: Event) {
    const value = selectValue(event);
    this._setFilterValues({ workflows: value ? [value] : [] });
  }

  private _setNativeFilterValues(patch: Partial<NativeFilters>) {
    this.nativeFilters = { ...this.nativeFilters, ...patch };
  }

  private _clearNativeFilters() {
    this.nativeFilters = { ...EMPTY_NATIVE_FILTERS };
  }

  private _toggleSingleNativeFilter(value: string) {
    const current = this.nativeFilters.rules;
    const already = current.length === 1 && current[0] === value;
    this._setNativeFilterValues({ rules: already ? [] : [value] });
  }

  private _handleNativeSearchChange(event: CustomEvent<{ value: string }>) {
    this._setNativeFilterValues({ query: event.detail.value });
  }

  private _handleNativeAgentFilterChange(event: Event) {
    const value = selectValue(event);
    this._setNativeFilterValues({ agents: value ? [value] : [] });
  }

  private _handleNativeRulesFilterChange(event: Event) {
    const value = selectValue(event);
    this._setNativeFilterValues({ rules: value ? [value] : [] });
  }

  private async _loadGovernanceDefaults() {
    try {
      const response = await getAccountGovernanceDefaults();
      this.governanceDefaults = response.defaults;
      this.governanceLoadFailed = false;
      if (response.override_agent_ids.length > 0) {
        // Resolve override agent names for the override line. One list call
        // covers typical fleets; unknown ids degrade to showing the id so
        // the link still works.
        try {
          const agents = await getAccountAgents({ limit: 100 });
          const byId = new Map(
            (agents.items || []).map((agent) => [agent.id, agent])
          );
          this.governanceOverrideAgents = response.override_agent_ids.map(
            (id) => ({
              id,
              name: byId.get(id)?.display_name || id,
            })
          );
        } catch {
          this.governanceOverrideAgents = response.override_agent_ids.map(
            (id) => ({ id, name: id })
          );
        }
      } else {
        this.governanceOverrideAgents = [];
      }
    } catch {
      // Card is additive; a load failure must not degrade the Tools page.
      this.governanceDefaults = null;
      this.governanceLoadFailed = true;
    }
  }

  private async _saveGovernanceDefaults(
    patch: Partial<AccountGovernanceDefaults>
  ): Promise<boolean> {
    const next: AccountGovernanceDefaults = {
      ...(this.governanceDefaults || {}),
      ...patch,
    };
    this.savingGovernanceDefaults = true;
    try {
      const response = await updateAccountGovernanceDefaults(next);
      this.governanceDefaults = response.defaults;
      this.governanceSaveError = null;
      return true;
    } catch {
      this.governanceSaveError = 'Could not save. Try again.';
      return false;
    } finally {
      this.savingGovernanceDefaults = false;
    }
  }

  /**
   * True when the account default asks a human before a native tool call.
   * `null` means "not set", which the backend treats as enforced. With the
   * defaults unread we claim nothing, and the strip states no default.
   */
  private _nativeAsksByDefault(): boolean {
    if (!this.governanceDefaults) {
      return false;
    }
    return this.governanceDefaults.native_tool_approvals !== 'off';
  }

  private _defaultWorkflowLabel(): string {
    const name = this.approvalPolicies.find((p) => p.is_default)?.name;
    return name ? `Default workflow (${name})` : 'Default workflow';
  }

  private _nativeWorkflowSelectValue(defaults: {
    approval_workflow_id?: string | null;
  }): string {
    const selected = defaults.approval_workflow_id ?? '';
    const defaultId = this.approvalPolicies.find((p) => p.is_default)?.id;
    return selected && selected === defaultId ? '' : selected;
  }

  /**
   * Account-default strip for native tool-call approvals. Same three ids as
   * the PR 382 card, flattened to one hairline row on the Native tab.
   */
  private _renderNativeApprovalDefaultsCard() {
    if (this.governanceLoadFailed) {
      return html`
        <div class="defaults-strip" id="native-approvals-defaults-card">
          <div class="governance-error">
            Could not load this setting.
            <a
              href="#"
              @click=${(e: Event) => {
                e.preventDefault();
                void this._loadGovernanceDefaults();
              }}
              >Retry</a
            >
          </div>
        </div>
      `;
    }
    if (!this.governanceDefaults) {
      return html``;
    }
    const defaults = this.governanceDefaults;
    const enforced = defaults.native_tool_approvals !== 'off';
    const overrides = this.governanceOverrideAgents;
    const shownOverrides = overrides.slice(0, 5);
    const hiddenOverrideCount = overrides.length - shownOverrides.length;
    return html`
      <div class="defaults-strip" id="native-approvals-defaults-card">
        <div class="defaults-strip-row">
          <sl-switch
            id="native-approvals-default-switch"
            size="small"
            ?checked=${enforced}
            ?disabled=${this.savingGovernanceDefaults}
            @sl-change=${async (e: Event) => {
              const target = e.target as HTMLInputElement;
              const saved = await this._saveGovernanceDefaults({
                native_tool_approvals: target.checked ? 'enforce' : 'off',
              });
              if (!saved) {
                target.checked = enforced;
              }
            }}
          >
            Ask a human before
            running${this.savingGovernanceDefaults ? ' Saving...' : ''}
          </sl-switch>
          ${
            enforced
              ? html`
                  <span class="strip-sep" aria-hidden="true">·</span>
                  <sl-select
                    id="native-approvals-default-workflow"
                    size="small"
                    hoist
                    label="Send requests to"
                    style="min-width: 260px;"
                    ?disabled=${this.savingGovernanceDefaults}
                    .value=${this._nativeWorkflowSelectValue(defaults)}
                    @sl-change=${(e: Event) => {
                      const select = e.target as HTMLSelectElement;
                      const previous =
                        this._nativeWorkflowSelectValue(defaults);
                      void this._saveGovernanceDefaults({
                        approval_workflow_id: select.value || null,
                      }).then((saved) => {
                        if (!saved) {
                          select.value = previous;
                        }
                      });
                    }}
                  >
                    <sl-option value=""
                      >${this._defaultWorkflowLabel()}</sl-option
                    >
                    ${this.approvalPolicies
                      .filter((workflow) => !workflow.is_default)
                      .map(
                        (workflow) => html`
                          <sl-option value=${workflow.id}>
                            ${workflow.name}
                          </sl-option>
                        `
                      )}
                  </sl-select>
                `
              : ''
          }
          ${
            overrides.length > 0
              ? html`
                  <span class="strip-sep" aria-hidden="true">·</span>
                  <div class="meta-line" id="native-approvals-override-list">
                    ${
                      overrides.length === 1
                        ? '1 agent uses its own setting: '
                        : `${overrides.length} agents use their own setting: `
                    }
                    ${shownOverrides.map(
                      (agent, index) =>
                        html`${index > 0 ? ', ' : ''}<a
                            href="/console/agents/${agent.id}"
                            >${agent.name}</a
                          >`
                    )}${
                      hiddenOverrideCount > 0
                        ? html`,
                            <a href="/console/agents"
                              >and ${hiddenOverrideCount} more</a
                            >`
                        : ''
                    }
                  </div>
                `
              : ''
          }
        </div>
        ${
          !enforced
            ? html`
                <div class="governance-notice" role="status">
                  <sl-icon name="exclamation-triangle"></sl-icon>
                  Native tool calls run without asking. Calls are still
                  recorded. Agents set to always ask keep asking.
                </div>
              `
            : ''
        }
        ${
          this.governanceSaveError
            ? html`<div class="governance-error">
                ${this.governanceSaveError}
              </div>`
            : ''
        }
      </div>
    `;
  }

  private _toolNoun(count: number): string {
    return count === 1 ? 'tool' : 'tools';
  }

  private _resultsLabel(shown: number, total: number): string {
    const noun = this._toolNoun(total);
    if (shown === total) {
      return `${shown} ${noun}`;
    }
    return `${shown} of ${total} ${noun}`;
  }

  private _renderStripCount(
    count: number,
    label: string,
    options: {
      active?: boolean;
      tooltip?: string;
      onClick?: () => void;
    } = {}
  ) {
    const button = html`
      <button
        type="button"
        class="strip-count${options.active ? ' active' : ''}"
        aria-pressed=${String(Boolean(options.active))}
        @click=${options.onClick}
      >
        ${count} ${label}
      </button>
    `;
    if (options.tooltip) {
      return html`<sl-tooltip content=${options.tooltip}
        >${button}</sl-tooltip
      >`;
    }
    return button;
  }

  private _renderSummaryStrip() {
    const stats = this._getStats();
    const status = this.filters.statuses[0] || '';
    const rule = this.filters.rules[0] || '';
    const noFilters =
      this.filters.statuses.length === 0 &&
      this.filters.servers.length === 0 &&
      this.filters.rules.length === 0 &&
      this.filters.workflows.length === 0 &&
      !this.filters.query;
    const unavailableTooltip =
      stats.unavailableReasons.length > 0
        ? stats.unavailableReasons.join('; ')
        : 'Some tools require trackers to be configured';

    return html`
      <div class="summary-strip">
        ${this._renderStripCount(stats.total, this._toolNoun(stats.total), {
          active: noFilters,
          onClick: () => this._clearFilters(),
        })}
        <span class="strip-sep" aria-hidden="true">·</span>
        ${this._renderStripCount(stats.enabled, 'enabled', {
          active: status === 'enabled',
          onClick: () => this._toggleSingleFilter('statuses', 'enabled'),
        })}
        <span class="strip-sep" aria-hidden="true">·</span>
        ${this._renderStripCount(stats.withRules, 'with rules', {
          active: rule === 'with_rules',
          onClick: () => this._toggleSingleFilter('rules', 'with_rules'),
        })}
        <span class="strip-sep" aria-hidden="true">·</span>
        ${this._renderStripCount(stats.requireApproval, 'require approval', {
          active: rule === 'require_approval',
          onClick: () => this._toggleSingleFilter('rules', 'require_approval'),
        })}
        ${
          stats.contextTaxTokens > 0
            ? html`<span class="strip-sep" aria-hidden="true">·</span>
                <span class="strip-note">
                  Enabled tools add
                  <strong
                    >~${stats.contextTaxTokens.toLocaleString()} tokens</strong
                  >
                  to every request
                </span>`
            : ''
        }
        ${
          stats.unavailable > 0
            ? html`<span class="strip-sep" aria-hidden="true">·</span>
                ${this._renderStripCount(stats.unavailable, 'unavailable', {
                  active: status === 'unavailable',
                  tooltip: unavailableTooltip,
                  onClick: () =>
                    this._toggleSingleFilter('statuses', 'unavailable'),
                })}`
            : ''
        }
      </div>
    `;
  }

  private _renderWorkflowsMenu() {
    const count = this.approvalPolicies.length;
    return html`
      <sl-dropdown class="workflows-menu" hoist>
        <sl-button slot="trigger" caret> Workflows (${count}) </sl-button>
        <sl-menu>
          ${this.approvalPolicies.map(
            (policy) => html`
              <sl-menu-label>${policy.name}</sl-menu-label>
              <sl-menu-item
                data-workflow-edit=${policy.id}
                @click=${() => this._openPolicyDialog(policy)}
                >Edit</sl-menu-item
              >
              <sl-menu-item
                data-workflow-delete=${policy.id}
                @click=${() => this._handleDeletePolicy(policy)}
                >Delete</sl-menu-item
              >
            `
          )}
          <sl-menu-item
            data-workflow-new
            @click=${() => this._openPolicyDialog(null)}
            >New workflow</sl-menu-item
          >
        </sl-menu>
      </sl-dropdown>
    `;
  }

  private _renderMcpToolbar() {
    const filtered = this._getFilteredTools();
    const total = this._mcpTools().length;
    return html`
      <div class="toolbar-wrap">
        <list-toolbar
          .search=${this.filters.query}
          searchPlaceholder="Search tools"
          toggleLabel="Tools view"
          .view=${'cards'}
          .views=${TOOLS_VIEWS}
          @search-change=${this._handleSearchChange}
          @view-change=${this._handleViewChange}
        >
          <sl-select
            class="status-filter"
            label="Status"
            clearable
            placeholder="Any status"
            .value=${this.filters.statuses[0] || ''}
            @sl-change=${this._handleStatusFilterChange}
          >
            <sl-option value="enabled">Enabled</sl-option>
            <sl-option value="disabled">Disabled</sl-option>
            <sl-option value="unavailable">Unavailable</sl-option>
          </sl-select>
          <sl-select
            class="server-filter"
            label="Server"
            clearable
            placeholder="Any server"
            .value=${this.filters.servers[0] || ''}
            @sl-change=${this._handleServerFilterChange}
          >
            <sl-option value="preloop">Preloop</sl-option>
            ${this.mcpServers.map(
              (server) =>
                html`<sl-option value=${server.id}>${server.name}</sl-option>`
            )}
          </sl-select>
          <sl-select
            class="rules-filter"
            label="Rules"
            clearable
            placeholder="Any rules"
            .value=${this.filters.rules[0] || ''}
            @sl-change=${this._handleRulesFilterChange}
          >
            <sl-option value="with_rules">With rules</sl-option>
            <sl-option value="no_rules">No rules</sl-option>
            <sl-option value="require_approval">Requires approval</sl-option>
          </sl-select>
          <sl-select
            class="workflow-filter"
            label="Workflow"
            clearable
            placeholder="Any workflow"
            .value=${this.filters.workflows[0] || ''}
            @sl-change=${this._handleWorkflowFilterChange}
          >
            ${this.approvalPolicies.map(
              (policy) =>
                html`<sl-option value=${policy.id}>${policy.name}</sl-option>`
            )}
          </sl-select>
          ${this._renderWorkflowsMenu()}
          <span slot="count"
            >${this._resultsLabel(filtered.length, total)}</span
          >
        </list-toolbar>
      </div>
    `;
  }

  private _renderMcpEditor() {
    return html`
      <tools-editor-component
        family="mcp"
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
        @scan-server=${(e: CustomEvent) => this._handleScanMCPServer(e.detail)}
        @suggest-starter-policy=${(e: CustomEvent) =>
          this._openStarterPolicySuggestion(e.detail, {
            manual: true,
          })}
        @edit-server=${(e: CustomEvent) => (this.editingMCPServer = e.detail)}
        @delete-server=${(e: CustomEvent) =>
          this._handleDeleteMCPServer(e.detail)}
      ></tools-editor-component>
    `;
  }

  private _renderMcpTab() {
    return html`
      <p class="tab-intro">
        Served through Preloop's MCP firewall: the Preloop server and any MCP
        server you add.
      </p>
      ${this._renderSummaryStrip()} ${this._renderMcpToolbar()}
      ${this._renderMcpEditor()}
    `;
  }

  private _getNativeStats() {
    const all = this._nativeTools();
    const blocked = all.filter((tool) => !tool.is_enabled);
    const withRules = all.filter((tool) => this._toolHasRules(tool));
    return {
      total: all.length,
      allowed: all.length - blocked.length,
      blocked: blocked.length,
      withRules: withRules.length,
    };
  }

  /**
   * The Native tab had no summary strip while MCP did, so the same page ran
   * two toolbar patterns and never stated the account default in the list
   * itself (B-T2).
   */
  private _renderNativeSummaryStrip() {
    const stats = this._getNativeStats();
    const rule = this.nativeFilters.rules[0] || '';
    const noFilters =
      this.nativeFilters.agents.length === 0 &&
      this.nativeFilters.rules.length === 0 &&
      !this.nativeFilters.query;
    return html`
      <div class="summary-strip native-summary-strip">
        ${this._renderStripCount(stats.total, this._toolNoun(stats.total), {
          active: noFilters,
          onClick: () => this._clearNativeFilters(),
        })}
        <span class="strip-sep" aria-hidden="true">·</span>
        ${this._renderStripCount(stats.allowed, 'allowed', {
          active: rule === 'allowed',
          onClick: () => this._toggleSingleNativeFilter('allowed'),
        })}
        <span class="strip-sep" aria-hidden="true">·</span>
        ${this._renderStripCount(stats.blocked, 'blocked', {
          active: rule === 'blocked',
          onClick: () => this._toggleSingleNativeFilter('blocked'),
        })}
        <span class="strip-sep" aria-hidden="true">·</span>
        ${this._renderStripCount(stats.withRules, 'with rules', {
          active: rule === 'with_rules',
          onClick: () => this._toggleSingleNativeFilter('with_rules'),
        })}
        ${
          this.governanceDefaults
            ? html`<span class="strip-sep" aria-hidden="true">·</span>
                <span class="strip-note"
                  >${
                    this._nativeAsksByDefault()
                      ? 'asks a human by default'
                      : 'runs without asking by default'
                  }</span
                >`
            : ''
        }
      </div>
    `;
  }

  private _renderNativeToolbar() {
    const filtered = this._getFilteredNativeTools();
    const total = this._nativeTools().length;
    return html`
      <div class="toolbar-wrap native-toolbar">
        <list-toolbar
          .search=${this.nativeFilters.query}
          searchPlaceholder="Search native tools"
          toggleLabel="Native tools view"
          .view=${'cards'}
          .views=${TOOLS_VIEWS}
          @search-change=${this._handleNativeSearchChange}
          @view-change=${this._handleViewChange}
        >
          <sl-select
            class="agent-filter"
            label="Agent"
            clearable
            placeholder="Any agent"
            .value=${this.nativeFilters.agents[0] || ''}
            @sl-change=${this._handleNativeAgentFilterChange}
          >
            ${NATIVE_ADAPTERS.map(
              (adapter) =>
                html`<sl-option value=${adapter.value}
                  >${adapter.label}</sl-option
                >`
            )}
          </sl-select>
          <sl-select
            class="native-rules-filter"
            label="Rules"
            clearable
            placeholder="Any rules"
            .value=${this.nativeFilters.rules[0] || ''}
            @sl-change=${this._handleNativeRulesFilterChange}
          >
            <sl-option value="with_rules">With rules</sl-option>
            <sl-option value="no_rules">No rules</sl-option>
            <sl-option value="require_approval">Requires approval</sl-option>
            <sl-option value="allowed">Allowed</sl-option>
            <sl-option value="blocked">Blocked</sl-option>
          </sl-select>
          <span slot="count"
            >${this._resultsLabel(filtered.length, total)}</span
          >
        </list-toolbar>
      </div>
    `;
  }

  private _renderNativeEditor() {
    return html`
      <tools-editor-component
        family="native"
        .tools=${this._getFilteredNativeTools()}
        .accountAsksByDefault=${this._nativeAsksByDefault()}
        .approvalPolicies=${this.approvalPolicies}
        .features=${this.features}
        mode="global"
        @toggle-enabled=${this._handleToggleEnabled}
        @save-rule=${this._handleSaveRule}
        @delete-rule=${this._handleDeleteRule}
        @policy-created=${this._handlePolicyCreated}
        @reorder-rules=${this._handleReorderRules}
        @tool-updated=${() => this.loadData()}
      ></tools-editor-component>
    `;
  }

  private _renderNativeTab() {
    const nativeTools = this._nativeTools();
    return html`
      <p class="tab-intro">
        Built into the agent itself, such as Bash, Edit and Write. Governed by
        the Preloop hook that "preloop agents onboard --approvals" installs.
      </p>
      ${this._renderNativeApprovalDefaultsCard()}
      ${
        nativeTools.length === 0
          ? html`
              <p class="native-empty">
                No native tool calls have reached Preloop yet. Onboard an agent
                with "preloop agents onboard --approvals" and its Bash, Edit and
                Write calls will show up here.
              </p>
            `
          : html`${this._renderNativeSummaryStrip()}
            ${this._renderNativeToolbar()} ${this._renderNativeEditor()}`
      }
    `;
  }

  private _renderTabs() {
    const mcpCount = this._mcpTools().length;
    const nativeCount = this._nativeTools().length;
    return html`
      <sl-tab-group @sl-tab-show=${this._handleTabShow}>
        <sl-tab slot="nav" panel="mcp" ?active=${this.activeTab === 'mcp'}
          >MCP tools ${mcpCount}</sl-tab
        >
        <sl-tab slot="nav" panel="native" ?active=${this.activeTab === 'native'}
          >Native tools ${nativeCount}</sl-tab
        >
        <sl-tab-panel name="mcp" ?active=${this.activeTab === 'mcp'}>
          ${this._renderMcpTab()}
        </sl-tab-panel>
        <sl-tab-panel name="native" ?active=${this.activeTab === 'native'}>
          ${this._renderNativeTab()}
        </sl-tab-panel>
      </sl-tab-group>
    `;
  }

  render() {
    const removedStarterPolicyChanges =
      this._getStarterPolicyDiffChanges('remove');

    return html`
      <view-header
        headerText="Tools"
        description="Tools your agents can call and the rules that govern them."
        width="extra-wide"
      >
        <div slot="main-column">
          <sl-button
            size="small"
            variant="primary"
            @click=${() => (this.isAddingMCPServer = true)}
          >
            <sl-icon slot="prefix" name="plus-lg"></sl-icon>
            Add MCP server
          </sl-button>

          <sl-button size="small" @click=${() => (this.showSetupDialog = true)}>
            Connect an agent
          </sl-button>

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
                  <strong>OAuth failed.</strong> Could not authenticate with the
                  external MCP server. Try again.
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
              : this._renderTabs()
          }
          <mcp-setup-dialog
            ?open=${this.showSetupDialog}
            @close=${() => (this.showSetupDialog = false)}
          ></mcp-setup-dialog>
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
