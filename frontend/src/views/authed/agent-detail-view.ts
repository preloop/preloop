import { LitElement, css, html, unsafeCSS, TemplateResult, nothing } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';

import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/card/card.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/option/option.js';
import '@shoelace-style/shoelace/dist/components/select/select.js';
import '@shoelace-style/shoelace/dist/components/textarea/textarea.js';
import '@shoelace-style/shoelace/dist/components/tooltip/tooltip.js';
import '../../components/governance-rule-set-editor.ts';
import '../../components/budget-policy-editor.ts';
import '../../components/tools-editor-component.ts';
import '../../components/tool-cost-flags-panel.ts';
import '../../components/preloop-session-observer.ts';
import '../../components/view-header.ts';
import '../../components/resource-actions.ts';
import '../../components/agent-talk-composer.ts';
import type { ResourceAction } from '../../components/resource-actions.ts';
import {
  fetchWithAuth,
  getApprovalWorkflows,
  getAgentGovernance,
  getAccountAgent,
  getAccountRuntimeSessionDetail,
  getFeatures,
  getAIModels,
  getTools,
  getMCPServers,
  removeAccountAgent,
  updateAgentGovernance,
  updateAccountAgent,
  getFlows,
} from '../../api';
import type {
  AccountRuntimeSessionDetailResponse,
  GatewayUsageByModel,
  ManagedAgentDetailResponse,
  ManagedAgentModelBindingSummary,
  ManagedAgentServerActivitySummary,
  ManagedAgentSummary,
  ManagedAgentToolActivitySummary,
  ManagedAgentUsageAggregate,
  RuntimeSessionActivityItem,
  RuntimeSessionSummary,
  SubjectGovernanceConfig,
} from '../../types';
import type { AccessRuleSummary } from '../../components/governance-rule-set-editor';
import consoleStyles from '../../styles/console-styles.css?inline';
import { unifiedWebSocketManager } from '../../services/unified-websocket-manager';
import {
  normalizeScopedToolRules,
  serializeScopedToolRules,
  type ScopedToolRules,
} from '../../utils/scoped-governance';
import { getAgentControlState } from '../../utils/agent-control';
import { renderAgentIcon } from '../../utils/agent-icons';

interface GovernanceToolDefinition {
  name: string;
  description?: string;
  schema?: Record<string, unknown>;
}

@customElement('agent-detail-view')
export class AgentDetailView extends LitElement {
  @property({ type: String })
  agentId = '';

  @state()
  private agent: ManagedAgentSummary | null = null;

  @state()
  private runtimeDetail: AccountRuntimeSessionDetailResponse | null = null;

  @state()
  private aggregate: ManagedAgentUsageAggregate | null = null;

  @state()
  private usageByModel: GatewayUsageByModel[] = [];

  @state()
  private activityByServer: ManagedAgentServerActivitySummary[] = [];

  @state()
  private activityByTool: ManagedAgentToolActivitySummary[] = [];

  @state()
  private sessions: RuntimeSessionSummary[] = [];

  @state()
  private activeTab:
    | 'sessions'
    | 'tools'
    | 'models'
    | 'vnc'
    | 'ssh'
    | 'dashboard'
    | 'associated-flows' = 'sessions';

  @state()
  private associatedFlows: any[] = [];

  @state()
  private sshTerminalOutput: string[] = [
    'Preloop Secure Agent Shell (Hermes Node)',
    'Logged in as preloop-agent. System: Alpine Linux 3.19',
    'Type "help" to list available custom shell actions.',
    '',
  ];

  @state()
  private sshCommandText = '';

  @state()
  private selectedSessionId: string | null = null;

  @state()
  private loading = true;

  @state()
  private error: string | null = null;

  @state()
  private initialized = false;

  @state()
  private availableUsers: Array<{
    id: string;
    username: string;
    email: string;
  }> = [];

  @state()
  private mcpServers: any[] = [];

  @state()
  private selectedOwnerUserId = '';

  @state()
  private editableDisplayName = '';

  @state()
  private actionLoading = false;

  @state()
  private liveActivity = {
    modelCalls: 0,
    toolCalls: 0,
    lastActivityAt: null as string | null,
  };

  @state()
  private budgetTimeRange: 'day' | 'week' | 'month' | 'year' = 'month';

  @state()
  private governance: SubjectGovernanceConfig = {
    allowed_models: [],
    model_budgets: {},
    tool_rules: {},
    tool_enabled_overrides: {},
    approval_workflow_id: null,
  };

  @state()
  private allowedModelsText = '';

  @state()
  private modelBudgetsText = '{}';

  @state()
  private scopedToolRules: ScopedToolRules = {};

  @state()
  private toolEnabledOverrides: Record<string, boolean> = {};

  @state()
  private toolCatalog: GovernanceToolDefinition[] = [];

  @state()
  private approvalWorkflows: any[] = [];

  @state()
  private availableModels: any[] = [];

  @state()
  private featureFlags: { [key: string]: boolean | string[] } = {};

  @state()
  private governanceToolToAdd = '';

  @state()
  private showTagsDialog = false;

  @state()
  private tagsDialogInput = '';

  @state()
  private governanceCustomToolName = '';

  @state()
  private isFullscreen = false;

  private unsubscribeRealtime?: () => void;
  private refreshTimer: number | null = null;

  static styles = [
    unsafeCSS(consoleStyles),
    css`
      :host {
        display: block;
      }

      .page,
      .stack,
      .timeline {
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-large);
      }

      .summary-grid {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
        gap: var(--sl-spacing-medium);
      }

      .header-actions {
        display: flex;
        justify-content: flex-end;
        align-items: center;
        gap: var(--sl-spacing-small);
        flex: 1;
        min-width: min(100%, 360px);
      }

      .split-pane-layout {
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-large);
      }

      .split-pane-layout > sl-card {
        max-width: 100%;
        overflow: hidden;
      }

      @media (max-width: 1000px) {
        .split-pane-layout {
          grid-template-columns: 1fr;
        }
      }

      .stat-card {
        border: 1px solid var(--sl-color-neutral-200);
        border-radius: var(--sl-border-radius-medium);
        padding: var(--sl-spacing-medium);
        background: var(--sl-color-neutral-50);
      }

      .summary-grid .stat-card {
        border-color: transparent;
        box-shadow: none;
      }

      .stat-label,
      .meta-line,
      .timeline-meta,
      .empty-state,
      .loading-state {
        color: var(--sl-color-neutral-600);
        font-size: var(--sl-font-size-small);
      }

      .stat-label {
        display: inline-flex;
        align-items: center;
        gap: var(--sl-spacing-2x-small);
      }

      .info-icon {
        color: var(--sl-color-neutral-500);
        font-size: 0.95rem;
      }

      .stat-value {
        margin-top: var(--sl-spacing-2x-small);
        font-size: 1.35rem;
        font-weight: 700;
        color: var(--sl-color-neutral-900);
      }

      .hero {
        display: flex;
        justify-content: space-between;
        align-items: start;
        gap: var(--sl-spacing-medium);
        flex-wrap: wrap;
      }

      .hero-title {
        font-size: 1.25rem;
        font-weight: 600;
        color: var(--sl-color-neutral-900);
      }

      .agent-overview {
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-small);
        padding: var(--sl-spacing-large);
        border-radius: var(--sl-border-radius-large);
        background: var(--sl-color-neutral-50);
        border: 1px solid var(--sl-color-neutral-200);
      }

      .section-header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        gap: var(--sl-spacing-medium);
        width: 100%;
      }

      .badge-row {
        display: flex;
        flex-wrap: wrap;
        gap: var(--sl-spacing-small);
      }

      .server-badges {
        display: flex;
        flex-wrap: wrap;
        gap: var(--sl-spacing-2x-small);
      }

      .session-link {
        color: var(--sl-color-primary-700);
        text-decoration: none;
      }

      .session-link:hover {
        text-decoration: underline;
      }

      .timeline-item {
        border-bottom: 1px solid var(--sl-color-neutral-200);
        padding-bottom: var(--sl-spacing-medium);
      }

      .timeline-item:last-child {
        border-bottom: none;
        padding-bottom: 0;
      }

      .timeline-title {
        font-weight: 600;
        color: var(--sl-color-neutral-900);
      }

      .loading-state,
      .empty-state {
        text-align: center;
        padding: var(--sl-spacing-x-large);
      }

      .loading-state sl-spinner {
        font-size: 2rem;
        margin-bottom: var(--sl-spacing-small);
      }

      .control-row {
        display: flex;
        gap: var(--sl-spacing-small);
        flex-wrap: wrap;
        align-items: end;
      }

      .control-row sl-select {
        min-width: 220px;
      }

      .agent-control-card::part(base) {
        border-color: var(--sl-color-primary-200);
        background: linear-gradient(
          180deg,
          var(--sl-color-primary-50),
          var(--sl-color-neutral-0)
        );
      }

      .agent-control-panel {
        display: grid;
        grid-template-columns: minmax(0, 1fr) minmax(240px, 320px);
        gap: var(--sl-spacing-large);
        align-items: start;
      }

      .agent-control-composer {
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-medium);
      }

      .agent-control-status {
        border: 1px solid var(--sl-color-neutral-200);
        border-radius: var(--sl-border-radius-medium);
        padding: var(--sl-spacing-medium);
        background: var(--sl-color-neutral-0);
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-small);
      }

      .agent-control-status-title {
        color: var(--sl-color-neutral-900);
        font-weight: var(--sl-font-weight-semibold);
      }

      @media (max-width: 900px) {
        .agent-control-panel {
          grid-template-columns: 1fr;
        }
      }
    `,
  ];

  @state()
  private changeOwnerDialogOpen = false;

  @state()
  private updateBudgetsDialogOpen = false;

  @state()
  private budgetsDialogJson = '{}';

  onBeforeEnter(location: { params: { agentId?: string } }) {
    const nextAgentId = location.params.agentId ?? '';
    const changed = this.agentId !== nextAgentId;
    this.agentId = nextAgentId;

    // Honor a ?tab= deep-link (e.g. optimization "Scope tools" links here with
    // ?tab=tools to land directly on the Tools & Governance tab).
    const requestedTab = new URLSearchParams(window.location.search).get('tab');
    const validTabs = [
      'sessions',
      'tools',
      'models',
      'vnc',
      'ssh',
      'dashboard',
      'associated-flows',
    ];
    if (requestedTab && validTabs.includes(requestedTab)) {
      this.activeTab = requestedTab as typeof this.activeTab;
    }

    if (this.initialized && changed) {
      void this.loadData();
    }
  }

  connectedCallback(): void {
    super.connectedCallback();
    this.connectRealtime();
    if (!this.initialized) {
      this.initialized = true;
      if (this.agentId) {
        void this.loadData();
      }
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

  private connectRealtime(): void {
    const scheduleRefresh = () => this.scheduleRefresh();
    const unsubscribers = [
      unifiedWebSocketManager.subscribe('managed_agents', scheduleRefresh),
      unifiedWebSocketManager.subscribe('runtime_sessions', scheduleRefresh),
      unifiedWebSocketManager.subscribe('agent_control', (message) =>
        this.handleAgentControlEvent(message)
      ),
      unifiedWebSocketManager.subscribe('gateway_activity', (message) =>
        this.handleGatewayActivity(message)
      ),
      unifiedWebSocketManager.subscribe('audit', scheduleRefresh),
    ];
    this.unsubscribeRealtime = () => {
      for (const unsubscribe of unsubscribers) {
        unsubscribe();
      }
    };
    void unifiedWebSocketManager.connect();
  }

  private scheduleRefresh(): void {
    if (!this.agentId) {
      return;
    }
    if (this.refreshTimer !== null) {
      window.clearTimeout(this.refreshTimer);
    }
    this.refreshTimer = window.setTimeout(() => {
      this.refreshTimer = null;
      void this.loadData(true);
    }, 250);
  }

  private async loadData(isSoftRefresh = false): Promise<void> {
    if (!this.agentId) {
      if (!isSoftRefresh) this.error = 'Missing agent id.';
      this.loading = false;
      return;
    }

    if (!isSoftRefresh) {
      this.loading = true;
      this.error = null;
      this.aggregate = null;
      this.usageByModel = [];
      this.activityByServer = [];
      this.activityByTool = [];
    }

    let startDate: string | undefined;
    const now = new Date();
    if (this.budgetTimeRange === 'day') {
      startDate = new Date(now.getTime() - 24 * 60 * 60 * 1000).toISOString();
    } else if (this.budgetTimeRange === 'week') {
      startDate = new Date(
        now.getTime() - 7 * 24 * 60 * 60 * 1000
      ).toISOString();
    } else if (this.budgetTimeRange === 'month') {
      startDate = new Date(
        now.getTime() - 30 * 24 * 60 * 60 * 1000
      ).toISOString();
    } else if (this.budgetTimeRange === 'year') {
      startDate = new Date(
        now.getTime() - 365 * 24 * 60 * 60 * 1000
      ).toISOString();
    }

    try {
      const [
        detail,
        users,
        governance,
        tools,
        servers,
        workflows,
        features,
        models,
      ] = await Promise.all([
        getAccountAgent(this.agentId, { start_date: startDate }),
        this.fetchUsers(),
        getAgentGovernance(this.agentId),
        getTools(),
        getMCPServers(),
        getApprovalWorkflows(),
        getFeatures(),
        getAIModels(),
      ]);
      this.agent = detail.agent;
      this.availableModels = models || [];
      this.mcpServers = servers || [];
      this.aggregate = detail.aggregate;
      this.usageByModel = detail.usage_by_model;
      this.activityByServer = detail.activity_by_server;
      this.activityByTool = detail.activity_by_tool;
      this.sessions = detail.sessions;
      if (!isSoftRefresh) {
        this.liveActivity = {
          modelCalls: 0,
          toolCalls: 0,
          lastActivityAt: null,
        };
      }
      this.governance = governance.config;
      this.scopedToolRules = normalizeScopedToolRules(
        governance.config.tool_rules
      );
      this.toolEnabledOverrides =
        governance.config.tool_enabled_overrides || {};
      this.allowedModelsText = governance.config.allowed_models.join(', ');
      this.modelBudgetsText = JSON.stringify(
        governance.config.model_budgets || {},
        null,
        2
      );
      this.toolCatalog = tools || [];
      this.approvalWorkflows = workflows || [];
      this.featureFlags = features?.features || {};
      this.availableUsers = users;
      this.selectedOwnerUserId = detail.agent.owner_user_id ?? '';
      if (!isSoftRefresh) {
        this.editableDisplayName = detail.agent.display_name;
      }

      // Load and filter associated flows
      try {
        const flows = await getFlows();
        this.associatedFlows = (flows || []).filter((f: any) => {
          try {
            const config =
              typeof f.agent_config === 'string'
                ? JSON.parse(f.agent_config)
                : f.agent_config;
            return (
              config &&
              config.execution_path === 'persistent' &&
              config.target_agent_id === this.agentId
            );
          } catch (e) {
            return false;
          }
        });
      } catch (e) {
        console.warn('Failed to load associated flows', e);
      }

      if (
        !this.selectedSessionId ||
        !this.sessions.some((s) => s.id === this.selectedSessionId)
      ) {
        this.selectedSessionId =
          detail.agent.runtime_session_id ?? detail.sessions[0]?.id ?? null;
      }
      this.runtimeDetail = this.selectedSessionId
        ? await getAccountRuntimeSessionDetail(this.selectedSessionId)
        : null;
    } catch (error) {
      console.error('Failed to load managed agent detail:', error);
      if (!isSoftRefresh) {
        this.error =
          error instanceof Error
            ? error.message
            : 'Failed to load managed agent';
      }
    } finally {
      if (!isSoftRefresh) {
        this.loading = false;
      }
    }
  }

  private async selectSession(sessionId: string): Promise<void> {
    this.selectedSessionId = sessionId;
    try {
      this.runtimeDetail = await getAccountRuntimeSessionDetail(sessionId);
    } catch (error) {
      console.error('Failed to load runtime session detail:', error);
      this.error =
        error instanceof Error
          ? error.message
          : 'Failed to load runtime session detail';
    }
  }

  private async fetchUsers(): Promise<
    Array<{ id: string; username: string; email: string }>
  > {
    const response = await fetchWithAuth('/api/v1/users');
    if (!response.ok) {
      return [];
    }
    const data = await response.json();
    return data.users || [];
  }

  private getSourceLabel(sourceType: string | null | undefined): string {
    switch (sourceType) {
      case 'claude_code':
        return 'Claude Code';
      case 'claude_desktop':
        return 'Claude Desktop';
      case 'codex':
        return 'Codex';
      case 'openclaw':
        return 'OpenClaw';
      case 'gemini_cli':
        return 'Gemini CLI';
      case 'opencode':
        return 'OpenCode';
      case 'hermes':
        return 'Hermes';
      case 'desktop_agent':
        return 'Desktop Agent';
      case 'custom':
        return 'Custom Agent';
      default:
        return sourceType || 'Unknown';
    }
  }

  private formatMoney(amount: number | null | undefined): string {
    return `$${(amount || 0).toFixed(2)}`;
  }

  private getLifecycleVariant(): string {
    if (!this.agent) return 'neutral';
    if (this.agent.lifecycle_state === 'decommissioned') return 'danger';
    if (this.agent.lifecycle_state === 'suspended') return 'warning';
    if (this.agent.activity_status === 'active_now') return 'success';
    if (this.agent.activity_status === 'recently_active') return 'primary';
    if (this.agent.ended_at) return 'neutral';
    return 'primary';
  }

  private getLifecycleLabel(): string {
    if (!this.agent) return 'Unknown';
    if (this.agent.lifecycle_state === 'decommissioned')
      return 'Decommissioned';
    if (this.agent.lifecycle_state === 'suspended') return 'Suspended';
    if (this.agent.activity_status === 'active_now') return 'Active now';
    if (this.agent.activity_status === 'recently_active')
      return 'Recently active';
    if (this.agent.ended_at) return 'Ended';
    return 'Idle';
  }

  private getOnboardingVariant(): string {
    if (!this.agent) return 'neutral';
    if (this.agent.onboarding_state === 'fully_onboarded') return 'success';
    if (
      this.agent.onboarding_state === 'mcp_proxy_only' ||
      this.agent.onboarding_state === 'gateway_only'
    ) {
      return 'warning';
    }
    return 'neutral';
  }

  private getOnboardingLabel(): string {
    if (!this.agent) return 'Unknown';
    if (this.agent.onboarding_state === 'fully_onboarded')
      return 'Fully onboarded';
    if (this.agent.onboarding_state === 'mcp_proxy_only') return 'MCP only';
    if (this.agent.onboarding_state === 'gateway_only') return 'Gateway only';
    return 'Incomplete';
  }

  private getOnboardingDescription(): string {
    if (!this.agent) return 'This agent is not fully managed by Preloop yet.';
    if (this.agent.onboarding_state === 'fully_onboarded') {
      return 'Tool calls and model traffic both flow through Preloop.';
    }
    if (this.agent.onboarding_state === 'mcp_proxy_only') {
      return 'Tool calls flow through Preloop, but model traffic is still direct.';
    }
    if (this.agent.onboarding_state === 'gateway_only') {
      return 'Model traffic flows through Preloop, but MCP tool traffic is still direct.';
    }
    return 'This agent is not fully managed by Preloop yet.';
  }

  private renderInfoTooltip(content: string): TemplateResult {
    return html`
      <sl-tooltip content=${content}>
        <sl-icon class="info-icon" name="info-circle"></sl-icon>
      </sl-tooltip>
    `;
  }

  private renderStatLabel(label: string, explanation?: string): TemplateResult {
    return html`
      <div class="stat-label">
        ${label}${explanation ? this.renderInfoTooltip(explanation) : null}
      </div>
    `;
  }

  private getParsedModelBudgets(): Record<
    string,
    { monthly_usd_limit?: number }
  > {
    try {
      const parsed = JSON.parse(this.modelBudgetsText || '{}');
      return parsed && typeof parsed === 'object' ? parsed : {};
    } catch {
      return {};
    }
  }

  private getConfiguredModelBindings(): ManagedAgentModelBindingSummary[] {
    return this.agent?.configured_models || [];
  }

  private getPrimaryConfiguredModelAlias(): string | null {
    const primaryBinding = this.getConfiguredModelBindings().find(
      (binding) => binding.is_primary
    );
    return (
      primaryBinding?.gateway_alias?.trim() ||
      this.agent?.configured_model_alias?.trim() ||
      null
    );
  }

  private getConfiguredModelBinding(
    model: string
  ): ManagedAgentModelBindingSummary | null {
    return (
      this.getConfiguredModelBindings().find(
        (binding) => binding.gateway_alias === model
      ) || null
    );
  }

  private getDisplayedAgentModels(): string[] {
    const configuredModel = this.getPrimaryConfiguredModelAlias();
    const budgets = this.getParsedModelBudgets();
    const models = new Set<string>();

    if (configuredModel) models.add(configuredModel);
    for (const binding of this.getConfiguredModelBindings()) {
      if (binding.gateway_alias) models.add(binding.gateway_alias);
    }

    for (const model of this.governance?.allowed_models || []) {
      if (model) models.add(model);
    }

    for (const model of Object.keys(budgets)) {
      if (model) models.add(model);
    }

    if (models.size === 0) {
      for (const usage of this.usageByModel) {
        if (usage.model_alias) models.add(usage.model_alias);
      }
    }

    return Array.from(models).sort((a, b) => {
      if (configuredModel && a === configuredModel) return -1;
      if (configuredModel && b === configuredModel) return 1;

      const usageA = this.usageByModel.find((u) => u.model_alias === a);
      const usageB = this.usageByModel.find((u) => u.model_alias === b);
      const timeA = usageA?.last_request_at
        ? new Date(usageA.last_request_at).getTime()
        : 0;
      const timeB = usageB?.last_request_at
        ? new Date(usageB.last_request_at).getTime()
        : 0;
      return timeB - timeA;
    });
  }

  private getUsageForDisplayedModel(model: string): GatewayUsageByModel | null {
    const configuredBinding = this.getConfiguredModelBinding(model);
    const isConfiguredModel = !!configuredBinding;
    const configuredModelId =
      configuredBinding?.ai_model_id?.trim() ||
      (model === this.getPrimaryConfiguredModelAlias()
        ? this.agent?.configured_model_id?.trim()
        : null);
    if (isConfiguredModel && configuredModelId) {
      return (
        this.usageByModel.find(
          (usage) =>
            usage.model_alias === model &&
            usage.ai_model_id === configuredModelId
        ) || null
      );
    }
    return (
      this.usageByModel.find((usage) => usage.model_alias === model) || null
    );
  }

  private getDisplayedModelId(model: string): string | null {
    const configuredBinding = this.getConfiguredModelBinding(model);
    if (configuredBinding?.ai_model_id?.trim()) {
      return configuredBinding.ai_model_id.trim();
    }
    if (
      model === this.getPrimaryConfiguredModelAlias() &&
      this.agent?.configured_model_id?.trim()
    ) {
      return this.agent.configured_model_id.trim();
    }
    return this.getUsageForDisplayedModel(model)?.ai_model_id?.trim() || null;
  }

  private getLiveValidationVariant(): string {
    if (!this.agent?.live_validation_supported) return 'neutral';
    if (this.agent.live_validation_status === 'passed') return 'success';
    if (this.agent.live_validation_status === 'failed') return 'danger';
    if (this.agent.live_validation_status === 'not_run') return 'neutral';
    return 'warning';
  }

  private getLiveValidationLabel(): string {
    if (!this.agent?.live_validation_supported) return 'No live check';
    if (this.agent.live_validation_status === 'passed') return 'Live validated';
    if (this.agent.live_validation_status === 'failed')
      return 'Live check failed';
    // ``not_run`` means the CLI was never invoked with ``--live-validate`` —
    // it's an opt-in step, not a check that's currently in flight.
    if (this.agent.live_validation_status === 'not_run')
      return 'Live check not run';
    return 'Live check pending';
  }

  private handleGatewayActivity(message: any): void {
    const payload = message?.payload ?? {};
    if (!this.agent || payload.managed_agent_id !== this.agent.id) {
      return;
    }
    const type = message?.type;
    const nextActivityAt =
      payload.timestamp ??
      payload.last_activity_at ??
      this.liveActivity.lastActivityAt ??
      new Date().toISOString();
    this.liveActivity = {
      modelCalls:
        this.liveActivity.modelCalls + (type === 'model_gateway_call' ? 1 : 0),
      toolCalls: this.liveActivity.toolCalls + (type === 'mcp_call' ? 1 : 0),
      lastActivityAt: nextActivityAt,
    };
    this.agent = {
      ...this.agent,
      activity_status: 'active_now',
      last_seen_at: nextActivityAt ?? this.agent.last_seen_at,
      last_activity_at: nextActivityAt ?? this.agent.last_activity_at,
      last_request_at:
        type === 'model_gateway_call'
          ? (nextActivityAt ?? this.agent.last_request_at)
          : this.agent.last_request_at,
    };
    if (type === 'model_gateway_call' && this.aggregate) {
      this.aggregate = {
        ...this.aggregate,
        total_requests: this.aggregate.total_requests + 1,
        successful_requests:
          this.aggregate.successful_requests +
          ((payload.status_code ?? 200) < 400 ? 1 : 0),
        failed_requests:
          this.aggregate.failed_requests +
          ((payload.status_code ?? 200) >= 400 ? 1 : 0),
        estimated_cost:
          this.aggregate.estimated_cost + Number(payload.estimated_cost ?? 0),
        last_request_at: nextActivityAt ?? this.aggregate.last_request_at,
        latest_model_alias:
          (payload.model_alias as string | null) ??
          this.aggregate.latest_model_alias,
        latest_provider_name:
          (payload.provider_name as string | null) ??
          this.aggregate.latest_provider_name,
        token_usage: {
          ...(this.aggregate.token_usage || {}),
          total_tokens:
            (this.aggregate.token_usage?.total_tokens ?? 0) +
            (payload.total_tokens ?? 0),
        },
      };
    }

    this.scheduleRefresh();
  }

  private handleAgentControlEvent(message: any): void {
    const payload = message?.payload ?? message;
    const agentId = payload?.managed_agent_id ?? payload?.agent_id;
    if (!this.agent || agentId !== this.agent.id) {
      return;
    }

    this.scheduleRefresh();
  }

  private async saveOwnerAssignment(): Promise<void> {
    if (!this.agentId) return;
    this.actionLoading = true;
    try {
      await updateAccountAgent(this.agentId, {
        owner_user_id: this.selectedOwnerUserId || null,
      });
      await this.loadData();
    } catch (error) {
      console.error('Failed to update owner:', error);
      this.error =
        error instanceof Error ? error.message : 'Failed to update owner';
    } finally {
      this.actionLoading = false;
    }
  }

  private promptChangeOwner(): void {
    const defaultVal =
      this.agent?.owner_username || this.agent?.owner_email || '';
    const user = this.availableUsers.find(
      (u) => u.username === defaultVal.trim() || u.email === defaultVal.trim()
    );
    this.selectedOwnerUserId = user
      ? user.id
      : this.availableUsers.length > 0
        ? this.availableUsers[0].id
        : '';
    this.changeOwnerDialogOpen = true;
  }

  private async saveDisplayName(): Promise<void> {
    if (!this.agentId || !this.editableDisplayName.trim()) return;
    this.actionLoading = true;
    try {
      await updateAccountAgent(this.agentId, {
        display_name: this.editableDisplayName.trim(),
      });
      await this.loadData();
    } catch (error) {
      console.error('Failed to update agent name:', error);
      this.error =
        error instanceof Error ? error.message : 'Failed to update agent name';
    } finally {
      this.actionLoading = false;
    }
  }

  private promptRename(): void {
    const newName = window.prompt(
      'Enter the new name for this agent:',
      this.editableDisplayName
    );
    if (newName !== null && newName.trim() !== '') {
      this.editableDisplayName = newName.trim();
      this.saveDisplayName();
    }
  }

  private promptEditTags(): void {
    if (!this.agent) return;
    const currentTags = Object.entries(this.agent.tags || {})
      .map(([k, v]) => (v && v !== 'true' ? `${k}=${v}` : k))
      .join(' ');

    this.tagsDialogInput = currentTags;
    this.showTagsDialog = true;
  }

  private submitTagsDialog(): void {
    if (this.tagsDialogInput !== null) {
      const tags: Record<string, string> = {};
      this.tagsDialogInput.split(/\s+/).forEach((t: string) => {
        if (!t) return;
        const [k, ...vParts] = t.split('=');
        tags[k] = vParts.length > 0 ? vParts.join('=') : 'true';
      });
      void this.saveTags(tags);
    }
    this.showTagsDialog = false;
  }

  private async saveTags(tags: Record<string, string>): Promise<void> {
    if (!this.agentId) return;
    this.actionLoading = true;
    try {
      await updateAccountAgent(this.agentId, { tags });
      await this.loadData();
    } catch (error) {
      console.error('Failed to update tags:', error);
      this.error =
        error instanceof Error ? error.message : 'Failed to update tags';
    } finally {
      this.actionLoading = false;
    }
  }

  private async saveGovernance(): Promise<void> {
    if (!this.agentId) {
      return;
    }
    this.actionLoading = true;
    try {
      const parsedBudgets = JSON.parse(this.modelBudgetsText || '{}');
      const config: SubjectGovernanceConfig = {
        allowed_models: Object.keys(parsedBudgets).filter(
          (key) => key.trim() !== ''
        ),
        model_budgets: parsedBudgets,
        tool_rules: serializeScopedToolRules(this.scopedToolRules),
        tool_enabled_overrides: this.toolEnabledOverrides,
        approval_workflow_id: this.governance.approval_workflow_id ?? null,
      };
      const response = await updateAgentGovernance(this.agentId, config);
      this.governance = response.config;
      this.scopedToolRules = normalizeScopedToolRules(
        response.config.tool_rules
      );
      this.toolEnabledOverrides = response.config.tool_enabled_overrides || {};
      this.allowedModelsText = response.config.allowed_models.join(', ');
      this.modelBudgetsText = JSON.stringify(
        response.config.model_budgets || {},
        null,
        2
      );
    } catch (error) {
      console.error('Failed to update agent governance:', error);
      this.error =
        error instanceof Error ? error.message : 'Failed to update governance';
    } finally {
      this.actionLoading = false;
    }
  }

  private saveApprovalWorkflowSelection(workflowId: string | null): void {
    if ((this.governance.approval_workflow_id ?? null) === workflowId) {
      return;
    }
    this.governance = {
      ...this.governance,
      approval_workflow_id: workflowId,
    };
    void this.saveGovernance();
  }

  private getGovernanceTool(toolName: string): GovernanceToolDefinition | null {
    return (
      this.toolCatalog.find((tool) => tool.name === toolName.trim()) ?? null
    );
  }

  private getAvailableGovernanceTools(): GovernanceToolDefinition[] {
    const configured = new Set(Object.keys(this.scopedToolRules));
    return this.toolCatalog.filter((tool) => !configured.has(tool.name));
  }

  private addGovernanceToolScope(): void {
    const toolName = (
      this.governanceCustomToolName.trim() || this.governanceToolToAdd.trim()
    ).trim();
    if (!toolName || this.scopedToolRules[toolName]) {
      return;
    }
    this.scopedToolRules = {
      ...this.scopedToolRules,
      [toolName]: [],
    };
    this.governanceToolToAdd = '';
    this.governanceCustomToolName = '';
  }

  private toggleToolEnabledOverride(e: CustomEvent): void {
    const { tool, isEnabled } = e.detail;
    this.toolEnabledOverrides = {
      ...this.toolEnabledOverrides,
      [tool.name]: isEnabled,
    };
    void this.saveGovernance();
  }

  private revertScopedTool(e: CustomEvent): void {
    const { tool } = e.detail;
    if (this.scopedToolRules[tool.name]) {
      delete this.scopedToolRules[tool.name];
    }
    if (tool.name in this.toolEnabledOverrides) {
      delete this.toolEnabledOverrides[tool.name];
    }
    this.scopedToolRules = { ...this.scopedToolRules };
    this.toolEnabledOverrides = { ...this.toolEnabledOverrides };
    this.saveGovernance();
  }

  private removeGovernanceToolScope(toolName: string): void {
    const nextRules = { ...this.scopedToolRules };
    delete nextRules[toolName];
    this.scopedToolRules = nextRules;
  }

  private saveScopedToolRule(
    toolName: string,
    existingRule: AccessRuleSummary | null,
    formData: {
      action: 'allow' | 'deny' | 'require_approval';
      condition_expression: string | null;
      condition_type: 'simple' | 'cel';
      description: string | null;
      is_enabled: boolean;
      approval_workflow_id: string | null;
    }
  ): void {
    const currentRules = [...(this.scopedToolRules[toolName] || [])].sort(
      (left, right) => left.priority - right.priority
    );
    const nextRules = existingRule
      ? currentRules.map((rule) =>
          rule.id === existingRule.id ? { ...rule, ...formData } : rule
        )
      : [
          ...currentRules,
          {
            id: `scoped:${toolName}:${Date.now()}:${currentRules.length}`,
            priority: currentRules.length,
            ...formData,
          },
        ];
    this.scopedToolRules = {
      ...this.scopedToolRules,
      [toolName]: nextRules.map((rule, index) => ({
        ...rule,
        priority: index,
      })),
    };
    void this.saveGovernance();
  }

  private deleteScopedToolRule(toolName: string, ruleId: string): void {
    const nextRules = (this.scopedToolRules[toolName] || [])
      .filter((rule) => rule.id !== ruleId)
      .map((rule, index) => ({
        ...rule,
        priority: index,
      }));
    this.scopedToolRules = {
      ...this.scopedToolRules,
      [toolName]: nextRules,
    };
    void this.saveGovernance();
  }

  private reorderScopedToolRules(
    toolName: string,
    reorderedRules: { id: string; priority: number }[]
  ): void {
    const priorities = new Map(
      reorderedRules.map((rule) => [rule.id, rule.priority] as const)
    );
    const nextRules = [...(this.scopedToolRules[toolName] || [])]
      .map((rule) => ({
        ...rule,
        priority: priorities.get(rule.id) ?? rule.priority,
      }))
      .sort((left, right) => left.priority - right.priority)
      .map((rule, index) => ({
        ...rule,
        priority: index,
      }));
    this.scopedToolRules = {
      ...this.scopedToolRules,
      [toolName]: nextRules,
    };
    void this.saveGovernance();
  }

  private async refreshGovernanceWorkflows(): Promise<void> {
    try {
      this.approvalWorkflows = await getApprovalWorkflows();
    } catch (error) {
      console.error('Failed to refresh approval workflows:', error);
      this.error =
        error instanceof Error
          ? error.message
          : 'Failed to refresh approval workflows';
    }
  }

  private async removeAgent(): Promise<void> {
    if (!this.agentId || !this.agent) {
      return;
    }
    if (
      !window.confirm(
        `Remove ${this.agent.display_name} from the managed agents list? This only removes the Preloop registry record.`
      )
    ) {
      return;
    }
    this.actionLoading = true;
    try {
      await removeAccountAgent(this.agentId);
      window.location.href = '/console/agents';
    } catch (error) {
      console.error('Failed to remove managed agent:', error);
      this.error =
        error instanceof Error
          ? error.message
          : 'Failed to remove managed agent';
    } finally {
      this.actionLoading = false;
    }
  }

  private async updateAgentLifecycle(
    lifecycleAction: 'suspend' | 'resume'
  ): Promise<void> {
    if (!this.agentId || !this.agent) {
      return;
    }
    const actionLabel = lifecycleAction === 'suspend' ? 'halt' : 'resume';
    if (
      !window.confirm(
        `Are you sure you want to ${actionLabel} ${this.agent.display_name}?`
      )
    ) {
      return;
    }
    this.actionLoading = true;
    try {
      await updateAccountAgent(this.agentId, {
        lifecycle_action: lifecycleAction,
        reason:
          lifecycleAction === 'suspend'
            ? 'Manually halted by admin'
            : 'Manually resumed by admin',
      });
      await this.loadData();
    } catch (error) {
      console.error('Failed to update managed agent lifecycle:', error);
      this.error =
        error instanceof Error
          ? error.message
          : 'Failed to update managed agent lifecycle';
    } finally {
      this.actionLoading = false;
    }
  }

  private formatDateTime(value: string | null | undefined): string {
    if (!value) {
      return 'None';
    }
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) {
      return value;
    }
    return parsed.toLocaleString();
  }

  private renderTimelineItem(item: RuntimeSessionActivityItem) {
    return html`
      <div class="timeline-item">
        <div class="timeline-title">${item.title}</div>
        <div class="timeline-meta">
          ${this.formatDateTime(item.timestamp)}${
            item.status ? html` · ${item.status}` : null
          }${
            item.is_retry || (item.gateway_attempt || 1) > 1
              ? html` · retry #${item.gateway_attempt || 2}`
              : null
          }${item.summary ? html` · ${item.summary}` : null}
        </div>
      </div>
    `;
  }

  private get agentActions(): ResourceAction[] {
    if (!this.agent) return [];

    const actions: ResourceAction[] = [
      {
        id: 'rename',
        label: 'Rename',
        icon: 'pencil',
        onClick: () => this.promptRename(),
      },
      {
        id: 'remove',
        label: 'Remove',
        variant: 'danger',
        loading: this.actionLoading,
        onClick: () => this.removeAgent(),
      },
      {
        id: 'edit-tags',
        label: 'Edit Tags',
        icon: 'tag',
        onClick: () => this.promptEditTags(),
      },
    ];

    if (this.featureFlags.user_management) {
      actions.push({
        id: 'change-owner',
        label: 'Change Owner',
        icon: 'person-gear',
        onClick: () => this.promptChangeOwner(),
      });
    }

    const isSuspendedOrDecommissioned =
      this.agent.lifecycle_state === 'suspended' ||
      this.agent.lifecycle_state === 'decommissioned';

    if (isSuspendedOrDecommissioned) {
      actions.push({
        id: 'resume',
        label: 'Resume',
        variant: 'success',
        icon: 'plug',
        loading: this.actionLoading,
        onClick: () => this.updateAgentLifecycle('resume'),
        tooltip: "This action re-enables the agent's API keys.",
      });
    } else {
      actions.push({
        id: 'halt',
        label: 'Halt',
        variant: 'danger',
        icon: 'power',
        loading: this.actionLoading,
        onClick: () => this.updateAgentLifecycle('suspend'),
        tooltip:
          "This action immediately disables the agent's API keys and is fully reversible.",
      });
    }

    const controlState = getAgentControlState(this.agent);
    if (controlState.visible) {
      actions.push({
        id: 'talk',
        label: 'Talk',
        render: () => html`
          <agent-talk-composer
            .agent=${this.agent}
            .sessions=${this.sessions}
            sourceContext="agent-detail-view"
            @agent-control-sent=${() => this.loadData(true)}
          ></agent-talk-composer>
        `,
      });
    }

    return actions;
  }

  private handleSshKeyDown(e: KeyboardEvent) {
    if (e.key === 'Enter') {
      const command = this.sshCommandText.trim();
      if (!command) return;

      this.sshTerminalOutput = [...this.sshTerminalOutput, `$ ${command}`];

      // Generate response
      let response: string[] = [];
      const lowerCmd = command.toLowerCase();
      if (lowerCmd === 'help') {
        response = [
          'Preloop Agent CLI Custom Actions:',
          '  help          - Show this command list',
          '  ls -la        - List sandboxed files and folders',
          '  git status    - Display git version control state',
          '  cat agent.log - Cat the active runtime logging outputs',
          '  clear         - Clear terminal screen',
        ];
      } else if (lowerCmd === 'clear') {
        this.sshTerminalOutput = [];
        this.sshCommandText = '';
        this.requestUpdate();
        return;
      } else if (lowerCmd === 'ls -la' || lowerCmd === 'ls') {
        response = [
          'total 32',
          'drwxr-xr-x    4 preloop  staff         128 May 31 22:50 .',
          'drwxr-xr-x   24 preloop  staff         768 May 31 22:45 ..',
          '-rwxr-xr-x    1 preloop  staff         480 May 31 22:50 preloop.py',
          '-rw-r--r--    1 preloop  staff        2048 May 31 22:50 firewall.json',
          '-rw-r--r--    1 preloop  staff         180 May 31 22:50 auth_token.jwt',
          '-rw-r--r--    1 preloop  staff       12490 May 31 22:50 agent.log',
        ];
      } else if (lowerCmd === 'git status') {
        response = [
          'On branch main',
          "Your branch is up to date with 'origin/main'.",
          '',
          'nothing to commit, working tree clean',
        ];
      } else if (lowerCmd === 'cat agent.log') {
        response = [
          '2026-05-31 22:45:01 [INFO] Starting Hermes Agent Micro-service...',
          '2026-05-31 22:45:02 [INFO] Loading configuration from firewall.json...',
          '2026-05-31 22:45:03 [INFO] Secure proxy models handshaking successful.',
          '2026-05-31 22:45:05 [INFO] Gateway authenticated via token JWT.',
          '2026-05-31 22:45:10 [INFO] Telemetry heartbeat connected successfully.',
          '2026-05-31 22:45:12 [INFO] Running passive listener queue...',
        ];
      } else {
        response = [
          `sh: command not found: ${command}`,
          'Type "help" to see available mock commands.',
        ];
      }

      this.sshTerminalOutput = [...this.sshTerminalOutput, ...response, ''];
      this.sshCommandText = '';
      this.requestUpdate();

      // Scroll to bottom of terminal
      setTimeout(() => {
        const term = this.shadowRoot?.querySelector('.ssh-terminal-body');
        if (term) term.scrollTop = term.scrollHeight;
      }, 50);
    }
  }

  private renderVNCTab() {
    const tags = this.agent?.tags || {};
    const host = tags.host || '127.0.0.1';
    const username = tags.username || 'ubuntu';
    const port = tags.port || '22';

    return html`
      <div
        class="${this.isFullscreen ? 'fullscreen-mode' : ''}"
        style="${
          this.isFullscreen
            ? `
          position: fixed;
          top: 0;
          left: 0;
          width: 100vw;
          height: 100vh;
          z-index: 99999;
          background: #0f172a;
          color: #f1f5f9;
          padding: var(--sl-spacing-2x-large);
          box-sizing: border-box;
          display: flex;
          flex-direction: column;
          gap: var(--sl-spacing-large);
          overflow: auto;
        `
            : ''
        }"
      >
        <sl-card
          style="border: none; box-shadow: 0 10px 32px rgba(19,27,46,0.03); border-radius: var(--sl-border-radius-large); background: ${
            this.isFullscreen ? '#1e293b' : '#ffffff'
          }; width: 100%;"
        >
          <div style="padding: var(--sl-spacing-large);">
            <div
              style="display: flex; justify-content: space-between; align-items: center; margin-bottom: var(--sl-spacing-large); flex-wrap: wrap; gap: var(--sl-spacing-medium);"
            >
              <div>
                <div
                  style="font-weight: 700; font-size: 1.25rem; color: ${
                    this.isFullscreen
                      ? '#ffffff'
                      : 'var(--sl-color-neutral-800)'
                  };"
                >
                  Graphical UI Access (Secure VNC Desktop)
                </div>
                <div
                  style="font-size: var(--sl-font-size-small); color: ${
                    this.isFullscreen
                      ? '#cbd5e1'
                      : 'var(--sl-color-neutral-600)'
                  }; margin-top: 4px;"
                >
                  Securely tunnel and view the graphical operational desktop
                  environment of this agent VM.
                </div>
              </div>
              <div
                style="display: flex; gap: var(--sl-spacing-small); align-items: center;"
              >
                <sl-badge variant="success">VNC Port Pre-Exposed</sl-badge>
                <sl-button
                  size="small"
                  @click=${() => {
                    this.isFullscreen = !this.isFullscreen;
                    this.requestUpdate();
                  }}
                >
                  <sl-icon
                    slot="prefix"
                    name=${
                      this.isFullscreen
                        ? 'fullscreen-exit'
                        : 'arrows-angle-expand'
                    }
                  ></sl-icon>
                  ${this.isFullscreen ? 'Exit Fullscreen' : 'Fullscreen'}
                </sl-button>
              </div>
            </div>

            <div
              style="display: flex; flex-direction: column; gap: var(--sl-spacing-large);"
            >
              <div
                style="background: ${
                  this.isFullscreen ? '#0f172a' : 'var(--sl-color-neutral-50)'
                }; padding: var(--sl-spacing-large); border-radius: var(--sl-border-radius-medium); border: 1px solid ${
                  this.isFullscreen ? '#334155' : 'var(--sl-color-neutral-200)'
                };"
              >
                <div
                  style="font-weight: 600; font-size: var(--sl-font-size-small); color: ${
                    this.isFullscreen
                      ? '#38bdf8'
                      : 'var(--sl-color-primary-700)'
                  }; margin-bottom: var(--sl-spacing-medium); text-transform: uppercase;"
                >
                  Step 1: Securely Tunnel VNC Port (5901)
                </div>
                <p
                  style="font-size: var(--sl-font-size-small); line-height: 1.5; color: ${
                    this.isFullscreen
                      ? '#cbd5e1'
                      : 'var(--sl-color-neutral-600)'
                  };"
                >
                  Establish an SSH tunnel to forward traffic on the target
                  server port 5901 to your local workstation's port 5901:
                </p>
                <div
                  style="display: flex; align-items: center; gap: var(--sl-spacing-small); background: ${
                    this.isFullscreen
                      ? '#1e293b'
                      : 'var(--sl-color-neutral-100)'
                  }; padding: var(--sl-spacing-small) var(--sl-spacing-medium); border-radius: var(--sl-border-radius-medium); border: 1px solid ${
                    this.isFullscreen
                      ? '#475569'
                      : 'var(--sl-color-neutral-300)'
                  }; margin-top: var(--sl-spacing-small);"
                >
                  <code
                    style="font-family: var(--sl-font-mono); font-size: 0.85rem; color: ${
                      this.isFullscreen
                        ? '#38bdf8'
                        : 'var(--sl-color-neutral-800)'
                    }; flex: 1; overflow-x: auto; white-space: nowrap;"
                  >
                    ssh -L 5901:127.0.0.1:5901 ${username}@${host} -p ${port} -N
                  </code>
                  <sl-copy-button
                    value="ssh -L 5901:127.0.0.1:5901 ${username}@${host} -p ${port} -N"
                  ></sl-copy-button>
                </div>
              </div>

              <div
                style="background: ${
                  this.isFullscreen ? '#0f172a' : 'var(--sl-color-neutral-50)'
                }; padding: var(--sl-spacing-large); border-radius: var(--sl-border-radius-medium); border: 1px solid ${
                  this.isFullscreen ? '#334155' : 'var(--sl-color-neutral-200)'
                };"
              >
                <div
                  style="font-weight: 600; font-size: var(--sl-font-size-small); color: ${
                    this.isFullscreen
                      ? '#38bdf8'
                      : 'var(--sl-color-primary-700)'
                  }; margin-bottom: var(--sl-spacing-medium); text-transform: uppercase;"
                >
                  Step 2: Connect Local VNC Client
                </div>
                <p
                  style="font-size: var(--sl-font-size-small); line-height: 1.5; color: ${
                    this.isFullscreen
                      ? '#cbd5e1'
                      : 'var(--sl-color-neutral-600)'
                  }; margin: 0;"
                >
                  Once the port forwarding tunnel is running, open any standard
                  VNC viewer client (e.g. RealVNC, TigerVNC, or built-in macOS
                  Screen Sharing) and connect to:
                </p>
                <div
                  style="display: flex; align-items: center; gap: var(--sl-spacing-small); background: ${
                    this.isFullscreen
                      ? '#1e293b'
                      : 'var(--sl-color-neutral-100)'
                  }; padding: var(--sl-spacing-small) var(--sl-spacing-medium); border-radius: var(--sl-border-radius-medium); border: 1px solid ${
                    this.isFullscreen
                      ? '#475569'
                      : 'var(--sl-color-neutral-300)'
                  }; margin-top: var(--sl-spacing-small);"
                >
                  <code
                    style="font-family: var(--sl-font-mono); font-size: 0.85rem; color: ${
                      this.isFullscreen
                        ? '#38bdf8'
                        : 'var(--sl-color-neutral-800)'
                    }; flex: 1; overflow-x: auto; white-space: nowrap;"
                  >
                    vnc://localhost:5901
                  </code>
                  <sl-copy-button value="vnc://localhost:5901"></sl-copy-button>
                </div>
              </div>
            </div>
          </div>
        </sl-card>
      </div>
    `;
  }

  private renderSSHTab() {
    const tags = this.agent?.tags || {};
    const host = tags.host || '127.0.0.1';
    const username = tags.username || 'ubuntu';
    const port = tags.port || '22';

    return html`
      <div
        class="${this.isFullscreen ? 'fullscreen-mode' : ''}"
        style="${
          this.isFullscreen
            ? `
          position: fixed;
          top: 0;
          left: 0;
          width: 100vw;
          height: 100vh;
          z-index: 99999;
          background: #0f172a;
          color: #f1f5f9;
          padding: var(--sl-spacing-2x-large);
          box-sizing: border-box;
          display: flex;
          flex-direction: column;
          gap: var(--sl-spacing-large);
          overflow: auto;
        `
            : ''
        }"
      >
        <sl-card
          style="border: none; box-shadow: 0 10px 32px rgba(19,27,46,0.03); border-radius: var(--sl-border-radius-large); background: ${
            this.isFullscreen ? '#1e293b' : '#ffffff'
          }; width: 100%;"
        >
          <div style="padding: var(--sl-spacing-large);">
            <div
              style="display: flex; justify-content: space-between; align-items: center; margin-bottom: var(--sl-spacing-large); flex-wrap: wrap; gap: var(--sl-spacing-medium);"
            >
              <div>
                <div
                  style="font-weight: 700; font-size: 1.25rem; color: ${
                    this.isFullscreen
                      ? '#ffffff'
                      : 'var(--sl-color-neutral-800)'
                  };"
                >
                  Command Terminal (SSH Access)
                </div>
                <div
                  style="font-size: var(--sl-font-size-small); color: ${
                    this.isFullscreen
                      ? '#cbd5e1'
                      : 'var(--sl-color-neutral-600)'
                  }; margin-top: 4px;"
                >
                  Connect directly to the governed agent VM node.
                </div>
              </div>
              <div
                style="display: flex; gap: var(--sl-spacing-small); align-items: center;"
              >
                <sl-badge variant="primary">SSH Ready</sl-badge>
                <sl-button
                  size="small"
                  @click=${() => {
                    this.isFullscreen = !this.isFullscreen;
                    this.requestUpdate();
                  }}
                >
                  <sl-icon
                    slot="prefix"
                    name=${
                      this.isFullscreen
                        ? 'fullscreen-exit'
                        : 'arrows-angle-expand'
                    }
                  ></sl-icon>
                  ${this.isFullscreen ? 'Exit Fullscreen' : 'Fullscreen'}
                </sl-button>
              </div>
            </div>

            <div
              style="display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: var(--sl-spacing-large); margin-bottom: var(--sl-spacing-large);"
            >
              <div
                style="background: ${
                  this.isFullscreen ? '#0f172a' : 'var(--sl-color-neutral-50)'
                }; padding: var(--sl-spacing-large); border-radius: var(--sl-border-radius-medium); border: 1px solid ${
                  this.isFullscreen ? '#334155' : 'var(--sl-color-neutral-200)'
                };"
              >
                <div
                  style="font-weight: 600; font-size: var(--sl-font-size-small); color: ${
                    this.isFullscreen
                      ? '#38bdf8'
                      : 'var(--sl-color-primary-700)'
                  }; margin-bottom: var(--sl-spacing-medium); text-transform: uppercase;"
                >
                  Connection Details
                </div>
                <div
                  style="display: flex; flex-direction: column; gap: var(--sl-spacing-small); font-size: var(--sl-font-size-small);"
                >
                  <div style="display: flex; justify-content: space-between;">
                    <span style="color: var(--sl-color-neutral-500);"
                      >Host:</span
                    >
                    <strong style="font-family: monospace;">${host}</strong>
                  </div>
                  <div style="display: flex; justify-content: space-between;">
                    <span style="color: var(--sl-color-neutral-500);"
                      >Port:</span
                    >
                    <strong style="font-family: monospace;">${port}</strong>
                  </div>
                  <div style="display: flex; justify-content: space-between;">
                    <span style="color: var(--sl-color-neutral-500);"
                      >Username:</span
                    >
                    <strong style="font-family: monospace;">${username}</strong>
                  </div>
                  <div style="display: flex; justify-content: space-between;">
                    <span style="color: var(--sl-color-neutral-500);"
                      >Authentication:</span
                    >
                    <strong>SSH Key / Password</strong>
                  </div>
                </div>
              </div>

              <div
                style="background: ${
                  this.isFullscreen ? '#0f172a' : 'var(--sl-color-neutral-50)'
                }; padding: var(--sl-spacing-large); border-radius: var(--sl-border-radius-medium); border: 1px solid ${
                  this.isFullscreen ? '#334155' : 'var(--sl-color-neutral-200)'
                }; display: flex; flex-direction: column; justify-content: space-between;"
              >
                <div>
                  <div
                    style="font-weight: 600; font-size: var(--sl-font-size-small); color: ${
                      this.isFullscreen
                        ? '#38bdf8'
                        : 'var(--sl-color-primary-700)'
                    }; margin-bottom: var(--sl-spacing-small); text-transform: uppercase;"
                  >
                    Quick Connect Command
                  </div>
                  <p
                    style="font-size: var(--sl-font-size-small); margin: 0 0 var(--sl-spacing-medium) 0; color: ${
                      this.isFullscreen
                        ? '#cbd5e1'
                        : 'var(--sl-color-neutral-600)'
                    };"
                  >
                    Run this command in your local terminal to establish an
                    interactive SSH session.
                  </p>
                </div>
                <div
                  style="display: flex; align-items: center; gap: var(--sl-spacing-small); background: ${
                    this.isFullscreen
                      ? '#1e293b'
                      : 'var(--sl-color-neutral-100)'
                  }; padding: var(--sl-spacing-small) var(--sl-spacing-medium); border-radius: var(--sl-border-radius-medium); border: 1px solid ${
                    this.isFullscreen
                      ? '#475569'
                      : 'var(--sl-color-neutral-300)'
                  };"
                >
                  <code
                    style="font-family: var(--sl-font-mono); font-size: 0.85rem; color: ${
                      this.isFullscreen
                        ? '#38bdf8'
                        : 'var(--sl-color-neutral-800)'
                    }; flex: 1; overflow-x: auto; white-space: nowrap;"
                  >
                    ssh ${username}@${host} -p ${port}
                  </code>
                  <sl-copy-button
                    value="ssh ${username}@${host} -p ${port}"
                  ></sl-copy-button>
                </div>
              </div>
            </div>

            <div
              style="background: ${
                this.isFullscreen ? '#1e293b' : 'var(--sl-color-neutral-50)'
              }; padding: var(--sl-spacing-large); border-radius: var(--sl-border-radius-medium); border: 1px solid ${
                this.isFullscreen ? '#334155' : 'var(--sl-color-neutral-200)'
              };"
            >
              <h4
                style="margin: 0 0 var(--sl-spacing-small) 0; font-weight: 600; color: ${
                  this.isFullscreen ? '#ffffff' : 'var(--sl-color-neutral-800)'
                };"
              >
                How to access pre-exposed VNC / Web services
              </h4>
              <p
                style="margin: 0; font-size: var(--sl-font-size-small); line-height: 1.5; color: ${
                  this.isFullscreen ? '#cbd5e1' : 'var(--sl-color-neutral-600)'
                };"
              >
                Since VNC and Agent Web UIs are typically hosted internally
                within the isolated VM node sandbox for security, you should
                tunnel the ports securely using local port forwarding. E.g., to
                access a web dashboard on port 8000, run:
                <code
                  style="display: block; margin: var(--sl-spacing-small) 0; padding: var(--sl-spacing-small); background: ${
                    this.isFullscreen
                      ? '#0f172a'
                      : 'var(--sl-color-neutral-100)'
                  }; border-radius: 4px; font-family: var(--sl-font-mono); font-size: 0.85rem; color: ${
                    this.isFullscreen
                      ? '#38bdf8'
                      : 'var(--sl-color-neutral-800)'
                  };"
                >
                  ssh -L 8000:localhost:8000 ${username}@${host} -p ${port} -N
                </code>
                Then navigate to
                <a
                  href="http://localhost:8000"
                  target="_blank"
                  style="color: var(--sl-color-primary-600); font-weight: 500;"
                  >http://localhost:8000</a
                >
                on your browser.
              </p>
            </div>
          </div>
        </sl-card>
      </div>
    `;
  }

  private renderDashboardTab() {
    const tags = this.agent?.tags || {};
    const host = tags.host || '127.0.0.1';
    const username = tags.username || 'ubuntu';
    const port = tags.port || '22';

    return html`
      <div
        class="${this.isFullscreen ? 'fullscreen-mode' : ''}"
        style="${
          this.isFullscreen
            ? `
          position: fixed;
          top: 0;
          left: 0;
          width: 100vw;
          height: 100vh;
          z-index: 99999;
          background: #0f172a;
          color: #f1f5f9;
          padding: var(--sl-spacing-2x-large);
          box-sizing: border-box;
          display: flex;
          flex-direction: column;
          gap: var(--sl-spacing-large);
          overflow: auto;
        `
            : ''
        }"
      >
        <sl-card
          style="border: none; box-shadow: 0 10px 32px rgba(19,27,46,0.03); border-radius: var(--sl-border-radius-large); background: ${
            this.isFullscreen ? '#1e293b' : '#ffffff'
          }; width: 100%;"
        >
          <div style="padding: var(--sl-spacing-large);">
            <div
              style="display: flex; justify-content: space-between; align-items: center; margin-bottom: var(--sl-spacing-large); flex-wrap: wrap; gap: var(--sl-spacing-medium);"
            >
              <div>
                <div
                  style="font-weight: 700; font-size: 1.25rem; color: ${
                    this.isFullscreen
                      ? '#ffffff'
                      : 'var(--sl-color-neutral-800)'
                  };"
                >
                  Agent Web UI Portal Access
                </div>
                <div
                  style="font-size: var(--sl-font-size-small); color: ${
                    this.isFullscreen
                      ? '#cbd5e1'
                      : 'var(--sl-color-neutral-600)'
                  }; margin-top: 4px;"
                >
                  Access the web-based operational dashboards served directly
                  from the agent runtime.
                </div>
              </div>
              <div
                style="display: flex; gap: var(--sl-spacing-small); align-items: center;"
              >
                <sl-badge variant="neutral">Sandbox Port Forwarding</sl-badge>
                <sl-button
                  size="small"
                  @click=${() => {
                    this.isFullscreen = !this.isFullscreen;
                    this.requestUpdate();
                  }}
                >
                  <sl-icon
                    slot="prefix"
                    name=${
                      this.isFullscreen
                        ? 'fullscreen-exit'
                        : 'arrows-angle-expand'
                    }
                  ></sl-icon>
                  ${this.isFullscreen ? 'Exit Fullscreen' : 'Fullscreen'}
                </sl-button>
              </div>
            </div>

            <div
              style="display: flex; flex-direction: column; gap: var(--sl-spacing-large);"
            >
              <div
                style="background: ${
                  this.isFullscreen ? '#0f172a' : 'var(--sl-color-neutral-50)'
                }; padding: var(--sl-spacing-large); border-radius: var(--sl-border-radius-medium); border: 1px solid ${
                  this.isFullscreen ? '#334155' : 'var(--sl-color-neutral-200)'
                };"
              >
                <div
                  style="font-weight: 600; font-size: var(--sl-font-size-small); color: ${
                    this.isFullscreen
                      ? '#38bdf8'
                      : 'var(--sl-color-primary-700)'
                  }; margin-bottom: var(--sl-spacing-medium); text-transform: uppercase;"
                >
                  Port Forwarding Instructions
                </div>
                <p
                  style="font-size: var(--sl-font-size-small); line-height: 1.5; color: ${
                    this.isFullscreen
                      ? '#cbd5e1'
                      : 'var(--sl-color-neutral-600)'
                  };"
                >
                  The agent serves its Web UI inside the isolated VM node
                  sandbox (typically on port 8080/8000). Establish an SSH tunnel
                  to access it securely from your local browser:
                </p>
                <div
                  style="display: flex; align-items: center; gap: var(--sl-spacing-small); background: ${
                    this.isFullscreen
                      ? '#1e293b'
                      : 'var(--sl-color-neutral-100)'
                  }; padding: var(--sl-spacing-small) var(--sl-spacing-medium); border-radius: var(--sl-border-radius-medium); border: 1px solid ${
                    this.isFullscreen
                      ? '#475569'
                      : 'var(--sl-color-neutral-300)'
                  }; margin-top: var(--sl-spacing-small);"
                >
                  <code
                    style="font-family: var(--sl-font-mono); font-size: 0.85rem; color: ${
                      this.isFullscreen
                        ? '#38bdf8'
                        : 'var(--sl-color-neutral-800)'
                    }; flex: 1; overflow-x: auto; white-space: nowrap;"
                  >
                    ssh -L 8080:127.0.0.1:8080 ${username}@${host} -p ${port} -N
                  </code>
                  <sl-copy-button
                    value="ssh -L 8080:127.0.0.1:8080 ${username}@${host} -p ${port} -N"
                  ></sl-copy-button>
                </div>
              </div>

              <div
                style="background: ${
                  this.isFullscreen ? '#0f172a' : 'var(--sl-color-neutral-50)'
                }; padding: var(--sl-spacing-large); border-radius: var(--sl-border-radius-medium); border: 1px solid ${
                  this.isFullscreen ? '#334155' : 'var(--sl-color-neutral-200)'
                };"
              >
                <div
                  style="font-weight: 600; font-size: var(--sl-font-size-small); color: ${
                    this.isFullscreen
                      ? '#38bdf8'
                      : 'var(--sl-color-primary-700)'
                  }; margin-bottom: var(--sl-spacing-medium); text-transform: uppercase;"
                >
                  Access Dashboard
                </div>
                <p
                  style="font-size: var(--sl-font-size-small); line-height: 1.5; color: ${
                    this.isFullscreen
                      ? '#cbd5e1'
                      : 'var(--sl-color-neutral-600)'
                  }; margin-bottom: var(--sl-spacing-medium);"
                >
                  Once the tunnel is connected, access the web control panel of
                  the agent at:
                </p>
                <div
                  style="display: flex; gap: var(--sl-spacing-medium); align-items: center;"
                >
                  <sl-button
                    href="http://localhost:8080"
                    target="_blank"
                    variant="primary"
                    size="small"
                  >
                    <sl-icon slot="suffix" name="box-arrow-up-right"></sl-icon>
                    Open Web UI (localhost:8080)
                  </sl-button>
                </div>
              </div>
            </div>
          </div>
        </sl-card>
      </div>
    `;
  }

  private renderFlowsTab() {
    return html`
      <sl-card
        style="border: none; box-shadow: 0 10px 32px rgba(19,27,46,0.03); border-radius: var(--sl-border-radius-large); background: #ffffff; width: 100%;"
      >
        <div style="padding: var(--sl-spacing-large);">
          <div
            style="display: flex; justify-content: space-between; align-items: center; margin-bottom: var(--sl-spacing-medium);"
          >
            <div
              style="font-weight: 700; font-size: 1.15rem; color: var(--sl-color-neutral-800);"
            >
              Flows Using This Agent Node
            </div>
            ${
              this.associatedFlows.length > 0
                ? html`
                    <a
                      href="/console/flows/new?agent_id=${this.agentId}"
                      style="text-decoration: none;"
                    >
                      <sl-button variant="primary" size="small">
                        <sl-icon name="plus-lg" slot="prefix"></sl-icon>
                        Create New Flow
                      </sl-button>
                    </a>
                  `
                : nothing
            }
          </div>

          ${
            this.associatedFlows.length === 0
              ? html`
                  <div
                    style="
                    text-align: center;
                    padding: var(--sl-spacing-3x-large);
                    background: var(--sl-color-neutral-50);
                    border-radius: var(--sl-border-radius-medium);
                    color: var(--sl-color-neutral-500);
                  "
                  >
                    <sl-icon
                      name="diagram-3"
                      style="font-size: 2.5rem; margin-bottom: var(--sl-spacing-medium); opacity: 0.6;"
                    ></sl-icon>
                    <p
                      style="margin: 0; font-size: var(--sl-font-size-medium);"
                    >
                      This agent is not currently bound to any event-driven or
                      automated flows.
                    </p>
                    <p
                      style="margin: 4px 0 0 0; font-size: var(--sl-font-size-small); color: var(--sl-color-neutral-400);"
                    >
                      Go to the Flows panel to bind this persistent node to an
                      automation task.
                    </p>
                    <a
                      href="/console/flows/new?agent_id=${this.agentId}"
                      style="text-decoration: none; display: inline-block; margin-top: var(--sl-spacing-large);"
                    >
                      <sl-button variant="primary" size="small"
                        >Create New Flow</sl-button
                      >
                    </a>
                  </div>
                `
              : html`
                  <div
                    style="display: flex; flex-direction: column; gap: var(--sl-spacing-medium);"
                  >
                    ${this.associatedFlows.map(
                      (flow) => html`
                        <div
                          style="
                        background: #ffffff;
                        border: 1px solid var(--sl-color-neutral-200);
                        border-radius: var(--sl-border-radius-medium);
                        padding: var(--sl-spacing-large);
                        display: flex;
                        justify-content: space-between;
                        align-items: center;
                        box-shadow: 0 2px 4px rgba(19,27,46,0.01);
                      "
                        >
                          <div>
                            <div
                              style="font-weight: 600; font-size: var(--sl-font-size-medium); color: var(--sl-color-neutral-800); display: flex; align-items: center; gap: 8px;"
                            >
                              <sl-icon
                                name=${flow.icon || 'diagram-3'}
                                style="color: var(--sl-color-primary-500);"
                              ></sl-icon>
                              ${flow.name}
                            </div>
                            <div
                              style="font-size: var(--sl-font-size-small); color: var(--sl-color-neutral-500); margin-top: 4px;"
                            >
                              ${flow.description || 'No description provided.'}
                            </div>
                            <div
                              style="font-size: var(--sl-font-size-x-small); color: var(--sl-color-neutral-400); margin-top: 6px; display: flex; gap: 12px;"
                            >
                              <span
                                >Trigger:
                                <strong
                                  >${
                                    flow.trigger_event_source === 'webhook'
                                      ? 'Webhook'
                                      : 'Tracker'
                                  }</strong
                                ></span
                              >
                              <span
                                >Status:
                                <strong
                                  >${
                                    flow.is_enabled ? 'Enabled' : 'Disabled'
                                  }</strong
                                ></span
                              >
                            </div>
                          </div>
                          <a
                            href="/console/flows/${flow.id}"
                            style="text-decoration: none;"
                          >
                            <sl-button size="small">Configure Flow</sl-button>
                          </a>
                        </div>
                      `
                    )}
                  </div>
                `
          }
        </div>
      </sl-card>
    `;
  }

  render() {
    if (this.loading) {
      return html`
        <div class="loading-state">
          <sl-spinner></sl-spinner>
          <div>Loading agent details...</div>
        </div>
      `;
    }

    if (this.error) {
      return html`<sl-alert open variant="danger">${this.error}</sl-alert>`;
    }

    if (!this.agent) {
      return html`<div class="empty-state">Managed agent not found.</div>`;
    }

    const runtimeSessionUrl = this.agent.runtime_session_id
      ? `/console/runtime-sessions?sessionId=${encodeURIComponent(this.agent.runtime_session_id)}`
      : null;
    const aggregate = this.aggregate;

    return html`
      <view-header headerText=${this.agent.display_name}>
        <div slot="top" style="margin-bottom: var(--sl-spacing-small);">
          <sl-button
            variant="text"
            size="small"
            @click=${() => window.history.back()}
            style="margin-left: -12px;"
          >
            <sl-icon slot="prefix" name="arrow-left"></sl-icon> Back
          </sl-button>
        </div>
        <div
          slot="title-prefix"
          style="display: flex; align-items: center; color: var(--sl-color-neutral-900);"
        >
          ${renderAgentIcon(
            this.agent.agent_kind || this.agent.session_source_type,
            'font-size: 1.2em; display: block;'
          )}
        </div>
        <div slot="main-column" class="header-actions">
          <resource-actions .actions=${this.agentActions}></resource-actions>
        </div>
      </view-header>
      <div class="page" style="padding-top: 0;">
        <div
          class="agent-overview"
          style="flex-direction: row; justify-content: space-between; align-items: stretch; flex-wrap: wrap;"
        >
          <div style="flex: 1; min-width: 300px;">
            <div
              style="color: var(--sl-color-neutral-500); font-size: 0.9rem; margin-top: 4px;"
            >
              ${this.getSourceLabel(
                this.agent.agent_kind || this.agent.session_source_type
              )}
              · ${this.agent.session_source_id}
              ${
                this.agent.session_reference
                  ? ` · ${this.agent.session_reference}`
                  : ''
              }
            </div>

            <div
              style="display: flex; flex-direction: column; align-items: flex-start; gap: var(--sl-spacing-small); margin-top: var(--sl-spacing-small);"
            >
              <div class="badge-row">
                <sl-tooltip content=${this.getOnboardingDescription()}>
                  <sl-badge variant=${this.getOnboardingVariant()}>
                    ${this.getOnboardingLabel()}
                  </sl-badge>
                </sl-tooltip>
                <sl-tooltip
                  content=${`Last seen: ${this.formatDateTime(this.liveActivity.lastActivityAt || this.agent.last_seen_at)}`}
                >
                  <sl-badge variant=${this.getLifecycleVariant()}>
                    ${this.getLifecycleLabel()}
                  </sl-badge>
                </sl-tooltip>
                <sl-badge variant=${this.getLiveValidationVariant()}>
                  ${this.getLiveValidationLabel()}
                </sl-badge>
                ${
                  getAgentControlState(this.agent).visible
                    ? html`
                        <sl-tooltip
                          content=${getAgentControlState(this.agent).detail}
                        >
                          <sl-badge
                            variant=${
                              getAgentControlState(this.agent).badgeVariant
                            }
                          >
                            ${getAgentControlState(this.agent).label}
                          </sl-badge>
                        </sl-tooltip>
                      `
                    : html`<sl-badge variant="neutral"
                        >No Agent Control</sl-badge
                      >`
                }
                <sl-badge variant="primary"
                  >${this.agent.enrolled_via}</sl-badge
                >
                ${
                  this.liveActivity.modelCalls || this.liveActivity.toolCalls
                    ? html`
                        <sl-badge variant="primary">
                          Live
                          ${
                            this.liveActivity.modelCalls +
                            this.liveActivity.toolCalls
                          }
                        </sl-badge>
                      `
                    : null
                }
              </div>

              ${
                this.agent.tags && Object.keys(this.agent.tags).length > 0
                  ? html`
                      <div
                        style="display: flex; gap: var(--sl-spacing-small); align-items: center; margin-top: var(--sl-spacing-x-small);"
                      >
                        <div
                          style="font-size: var(--sl-font-size-small); font-weight: 500; color: var(--sl-color-neutral-700);"
                        >
                          Tags:
                        </div>
                        <div style="display: flex; flex-wrap: wrap; gap: 4px;">
                          ${Object.entries(this.agent.tags).map(
                            ([k, v]) => html`
                              <sl-badge
                                variant="neutral"
                                style="text-transform: none;"
                              >
                                <span style="opacity: 0.7">${k}</span>${
                                  v && v !== 'true'
                                    ? html`<span
                                          style="opacity: 0.4; margin: 0 4px;"
                                          >=</span
                                        >${v}`
                                    : ''
                                }
                              </sl-badge>
                            `
                          )}
                        </div>
                      </div>
                    `
                  : nothing
              }
            </div>
          </div>

          <div
            class="stat-card"
            style="min-width: 200px; display: flex; flex-direction: column; justify-content: space-between; border-color: transparent;"
          >
            <div
              style="display: flex; justify-content: space-between; align-items: center; width: 100%;"
            >
              ${this.renderStatLabel('Estimated Spend')}
              <select
                style="background: transparent; border: none; font-size: var(--sl-font-size-small); color: var(--sl-color-neutral-600); cursor: pointer; outline: none;"
                .value=${this.budgetTimeRange}
                @change=${(e: Event) => {
                  this.budgetTimeRange = (e.target as HTMLSelectElement)
                    .value as any;
                  // The API currently returns aggregate lifetime cost,
                  // time window filtering requires a backend update for agent-specific spend
                  this.loadData();
                }}
              >
                <option value="day">24h</option>
                <option value="week">7d</option>
                <option value="month">30d</option>
                <option value="year">1y</option>
              </select>
            </div>
            <div>
              <div class="stat-value">
                ${this.formatMoney(aggregate?.estimated_cost)}
              </div>
              <div class="meta-line">
                Based on ${aggregate?.total_requests ?? 0} requests
              </div>
            </div>
          </div>
        </div>

        <!-- Sub-view Tab Navigation -->
        ${(() => {
          const tags = this.agent?.tags || {};
          const supportsSSH =
            this.agent?.enrolled_via === 'kube_virt' ||
            this.agent?.enrolled_via === 'ssh' ||
            this.agent?.session_source_type === 'kube_virt' ||
            this.agent?.session_source_type === 'ssh' ||
            (tags && (tags.compute === 'kube_virt' || tags.compute === 'ssh'));
          const isVncEnabled = tags && tags.vnc === 'true';
          const controlEnabled = getAgentControlState(this.agent).enabled;

          return html`
            <div
              style="margin-top: var(--sl-spacing-large); margin-bottom: var(--sl-spacing-large); border-bottom: 1px solid var(--sl-color-neutral-200); padding-bottom: 4px;"
            >
              <sl-tab-group
                @sl-tab-show=${(e: any) =>
                  (this.activeTab = e.detail.name as any)}
                style="--indicator-color: var(--sl-color-primary-600);"
              >
                <sl-tab
                  slot="nav"
                  panel="sessions"
                  ?active=${this.activeTab === 'sessions'}
                  >Session History</sl-tab
                >
                <sl-tab
                  slot="nav"
                  panel="tools"
                  ?active=${this.activeTab === 'tools'}
                  >Tools & Governance</sl-tab
                >
                <sl-tab
                  slot="nav"
                  panel="models"
                  ?active=${this.activeTab === 'models'}
                  >Models & Spend</sl-tab
                >

                ${
                  supportsSSH
                    ? html`
                        <sl-tab
                          slot="nav"
                          panel="ssh"
                          ?active=${this.activeTab === 'ssh'}
                          >Command Terminal (SSH)</sl-tab
                        >
                        ${
                          isVncEnabled
                            ? html`
                                <sl-tab
                                  slot="nav"
                                  panel="vnc"
                                  ?active=${this.activeTab === 'vnc'}
                                  >Graphical UI (VNC)</sl-tab
                                >
                              `
                            : nothing
                        }
                        <sl-tab
                          slot="nav"
                          panel="dashboard"
                          ?active=${this.activeTab === 'dashboard'}
                          >Agent Web UI</sl-tab
                        >
                      `
                    : nothing
                }
                ${
                  controlEnabled
                    ? html`
                        <sl-tab
                          slot="nav"
                          panel="associated-flows"
                          ?active=${this.activeTab === 'associated-flows'}
                          >Associated Flows
                          (${this.associatedFlows.length})</sl-tab
                        >
                      `
                    : nothing
                }
              </sl-tab-group>
            </div>

            ${
              this.activeTab === 'sessions'
                ? html`
                    <sl-card
                      style="border: none; box-shadow: 0 10px 32px rgba(19,27,46,0.03); border-radius: var(--sl-border-radius-large); width: 100%;"
                    >
                      <div
                        class="stack"
                        style="padding: var(--sl-spacing-large);"
                      >
                        <div
                          class="hero"
                          style="margin-bottom: var(--sl-spacing-large);"
                        >
                          <div
                            style="display: flex; justify-content: space-between; align-items: center; width: 100%;"
                          >
                            <div>
                              <div
                                class="hero-title"
                                style="display: flex; align-items: center; gap: 8px;"
                              >
                                Sessions History
                                <sl-icon-button
                                  name="arrow-clockwise"
                                  style="font-size: 1.1rem; color: var(--sl-color-neutral-500);"
                                  @click=${() => this.loadData(true)}
                                ></sl-icon-button>
                              </div>
                              <div class="meta-line">
                                Expand a session to view its captured
                                interactions.
                              </div>
                            </div>
                          </div>
                        </div>
                        <preloop-session-observer
                          scope="managed_agent"
                          .scopeId=${this.agentId}
                          .sessions=${this.sessions}
                          layout="embedded"
                          defaultReplayMode="timeline"
                          .features=${{
                            summaries: true,
                            optimization:
                              this.featureFlags.session_optimization === true,
                            auditLinks: true,
                            liveFollow: true,
                          }}
                        ></preloop-session-observer>
                      </div>
                    </sl-card>
                  `
                : nothing
            }
            ${
              this.activeTab === 'tools'
                ? html`
                    <sl-card
                      class="tools-card"
                      style="width: 100%; overflow: auto; max-height: 800px; display: flex; flex-direction: column; border: none; box-shadow: 0 10px 32px rgba(19,27,46,0.03); border-radius: var(--sl-border-radius-large);"
                    >
                      <div
                        class="stack"
                        style="overflow-y: auto; overflow-x: hidden; height: 100%; padding: var(--sl-spacing-large);"
                      >
                        <div
                          class="hero"
                          style="flex-shrink: 0; margin-bottom: var(--sl-spacing-medium);"
                        >
                          <div
                            style="display: flex; justify-content: space-between; align-items: center; width: 100%;"
                          >
                            <div>
                              <div class="hero-title">Tools & Governance</div>
                              <div class="meta-line">
                                Agent-specific configurations overrides applying
                                only to this agent.
                              </div>
                            </div>
                          </div>
                        </div>

                        <div
                          class="stat-card"
                          style="display: flex; align-items: center; justify-content: space-between; gap: var(--sl-spacing-medium); flex-shrink: 0; margin-bottom: var(--sl-spacing-medium);"
                        >
                          <div>
                            <div
                              class="stat-label"
                              style="display: flex; align-items: center; gap: 6px;"
                            >
                              <sl-icon name="shield-lock"></sl-icon>
                              Native tool approvals
                            </div>
                            <div class="meta-line">
                              Approval workflow used when this agent's native
                              tool calls (e.g. shell commands, file edits)
                              require human approval.
                            </div>
                          </div>
                          <sl-select
                            id="agent-approval-workflow-select"
                            size="small"
                            hoist
                            style="min-width: 280px;"
                            .value=${this.governance.approval_workflow_id ?? ''}
                            @sl-change=${(e: Event) => {
                              const value = (e.target as HTMLSelectElement)
                                .value;
                              this.saveApprovalWorkflowSelection(value || null);
                            }}
                          >
                            <sl-option value=""
                              >Account default workflow</sl-option
                            >
                            ${this.approvalWorkflows.map(
                              (workflow: any) => html`
                                <sl-option value=${workflow.id}>
                                  ${workflow.name}${
                                    workflow.is_default
                                      ? ' (account default)'
                                      : ''
                                  }
                                </sl-option>
                              `
                            )}
                          </sl-select>
                        </div>

                        <tools-editor-component
                          mode="scoped"
                          ?collapseByDefault=${true}
                          .tools=${this.toolCatalog}
                          .mcpServers=${this.mcpServers}
                          .scopedToolRules=${this.scopedToolRules}
                          .toolEnabledOverrides=${this.toolEnabledOverrides}
                          .approvalPolicies=${this.approvalWorkflows}
                          .features=${this.featureFlags}
                          @save-rule=${(e: CustomEvent) =>
                            this.saveScopedToolRule(
                              e.detail.tool.name,
                              e.detail.existingRule || e.detail.rule,
                              e.detail.formData
                            )}
                          @delete-rule=${(e: CustomEvent) =>
                            this.deleteScopedToolRule(
                              e.detail.tool.name,
                              e.detail.rule.id
                            )}
                          @reorder-rules=${(e: CustomEvent) =>
                            this.reorderScopedToolRules(
                              e.detail.tool.name,
                              e.detail.reorderedRules
                            )}
                          @toggle-enabled=${this.toggleToolEnabledOverride}
                          @revert-tool=${this.revertScopedTool}
                          @policy-created=${() => this.loadData()}
                        ></tools-editor-component>
                      </div>
                    </sl-card>
                  `
                : nothing
            }
            ${
              this.activeTab === 'models'
                ? html`
                    <sl-card
                      style="border: none; box-shadow: 0 10px 32px rgba(19,27,46,0.03); border-radius: var(--sl-border-radius-large); width: 100%;"
                    >
                      <div
                        class="stack"
                        style="padding: var(--sl-spacing-large);"
                      >
                        <div
                          class="hero"
                          style="margin-bottom: var(--sl-spacing-large);"
                        >
                          <div
                            style="display: flex; justify-content: space-between; align-items: center; width: 100%;"
                          >
                            <div>
                              <div class="hero-title">Models & Spend</div>
                              <div class="meta-line">
                                Assign budget limits restricting maximum spend
                                per month. If a model does not have a budget, it
                                will be prohibited.
                              </div>
                            </div>
                            <sl-button
                              size="small"
                              @click=${() => {
                                this.budgetsDialogJson = this.modelBudgetsText;
                                this.updateBudgetsDialogOpen = true;
                              }}
                            >
                              <sl-icon slot="prefix" name="pencil"></sl-icon>
                              Edit Budgets
                            </sl-button>
                          </div>
                        </div>
                        <div
                          class="stack"
                          style="gap: var(--sl-spacing-medium);"
                        >
                          ${this.getDisplayedAgentModels().map(
                            (model: string) => {
                              const currentBudgets =
                                this.getParsedModelBudgets();
                              const budget = currentBudgets[model] || {};
                              const isConfiguredModel =
                                model === this.agent?.configured_model_alias;
                              const usage =
                                this.getUsageForDisplayedModel(model);
                              const modelId = this.getDisplayedModelId(model);
                              const showZeroSpend = isConfiguredModel && !usage;
                              return html`
                                <div
                                  class="stat-card"
                                  style="display: flex; gap: var(--sl-spacing-medium); align-items: center; justify-content: space-between;"
                                >
                                  <div class="stat-label">
                                    <sl-icon
                                      name="robot"
                                      style="margin-right: 4px;"
                                    ></sl-icon>
                                    ${
                                      modelId
                                        ? html`<a
                                            href="/console/ai-models/${encodeURIComponent(
                                              modelId
                                            )}"
                                            class="session-link"
                                            style="font-weight: 500;"
                                            >${model}</a
                                          >`
                                        : html`<span style="font-weight: 500;"
                                            >${model}</span
                                          >`
                                    }
                                  </div>
                                  ${
                                    usage ||
                                    budget.monthly_usd_limit ||
                                    showZeroSpend
                                      ? html`<div style="font-size: 0.9em;">
                                          ${
                                            usage || showZeroSpend
                                              ? html`<span
                                                  style="color: var(--sl-color-primary-600); font-weight: 600;"
                                                  >${this.formatMoney(
                                                    usage?.estimated_cost ?? 0
                                                  )}
                                                  spent</span
                                                >`
                                              : ''
                                          }
                                          ${
                                            (usage || showZeroSpend) &&
                                            budget.monthly_usd_limit
                                              ? ' / '
                                              : ''
                                          }
                                          ${
                                            budget.monthly_usd_limit
                                              ? html`<span
                                                  style="color: var(--sl-color-neutral-600);"
                                                  >${this.formatMoney(
                                                    budget.monthly_usd_limit
                                                  )}
                                                  budget</span
                                                >`
                                              : ''
                                          }
                                        </div>`
                                      : ''
                                  }
                                </div>
                              `;
                            }
                          )}
                        </div>
                      </div>
                    </sl-card>
                    <div style="margin-top: var(--sl-spacing-large);">
                      <div
                        class="hero-title"
                        style="margin-bottom: var(--sl-spacing-small);"
                      >
                        Expensive tool definitions
                      </div>
                      <tool-cost-flags-panel></tool-cost-flags-panel>
                    </div>
                  `
                : nothing
            }
            ${this.activeTab === 'vnc' ? this.renderVNCTab() : nothing}
            ${this.activeTab === 'ssh' ? this.renderSSHTab() : nothing}
            ${
              this.activeTab === 'dashboard'
                ? this.renderDashboardTab()
                : nothing
            }
            ${
              this.activeTab === 'associated-flows'
                ? this.renderFlowsTab()
                : nothing
            }
          `;
        })()}
      </div>

      <!-- Change Owner Dialog -->
      <sl-dialog
        class="owner-dialog"
        label="Change Owner"
        ?open=${this.changeOwnerDialogOpen}
        @sl-after-hide=${(e: Event) => {
          if (e.target === e.currentTarget) {
            this.changeOwnerDialogOpen = false;
          }
        }}
      >
        <sl-select
          label="Select New Owner"
          value=${this.selectedOwnerUserId || ''}
          @sl-change=${(e: any) => {
            this.selectedOwnerUserId = e.target.value;
          }}
          hoist
        >
          ${this.availableUsers.map(
            (u) => html`
              <sl-option value=${u.id}>${u.username} (${u.email})</sl-option>
            `
          )}
        </sl-select>
        <div slot="footer">
          <sl-button
            variant="primary"
            @click=${() => {
              this.saveOwnerAssignment();
              this.changeOwnerDialogOpen = false;
            }}
            >Confirm</sl-button
          >
          <sl-button
            @click=${() => {
              this.changeOwnerDialogOpen = false;
            }}
            >Cancel</sl-button
          >
        </div>
      </sl-dialog>

      <!-- Update Budgets Dialog -->
      <sl-dialog
        class="budgets-dialog"
        label="Update Budgets"
        ?open=${this.updateBudgetsDialogOpen}
        @sl-after-hide=${(e: Event) => {
          if (e.target === e.currentTarget) {
            this.updateBudgetsDialogOpen = false;
          }
        }}
        style="--width: 600px;"
      >
        <budget-policy-editor
          subjectType="managed_agent"
          .subjectId=${this.agentId || ''}
        ></budget-policy-editor>
        <div slot="footer">
          <sl-button
            @click=${() => {
              this.updateBudgetsDialogOpen = false;
            }}
            >Close</sl-button
          >
        </div>
      </sl-dialog>

      ${this.renderTagsDialog()}
    `;
  }

  private renderTagsDialog(): TemplateResult {
    return html`
      <sl-dialog
        label="Edit Tags"
        ?open=${this.showTagsDialog}
        @sl-request-close=${(e: CustomEvent) => {
          if (e.detail.source === 'overlay') {
            e.preventDefault();
          } else {
            this.showTagsDialog = false;
          }
        }}
      >
        <div style="margin-bottom: var(--sl-spacing-medium);">
          Enter tags separated by space. Use 'key=value' for key-value pairs, or
          just 'key' for boolean tags.
        </div>
        <sl-input
          placeholder="e.g. env=prod target=aws db"
          .value=${this.tagsDialogInput}
          @input=${(e: Event) =>
            (this.tagsDialogInput = (e.target as HTMLInputElement).value)}
          @keydown=${(e: KeyboardEvent) => {
            if (e.key === 'Enter') {
              this.submitTagsDialog();
            }
          }}
        ></sl-input>
        <sl-button
          slot="footer"
          variant="default"
          @click=${() => (this.showTagsDialog = false)}
        >
          Cancel
        </sl-button>
        <sl-button
          slot="footer"
          variant="primary"
          @click=${() => this.submitTagsDialog()}
        >
          Save
        </sl-button>
      </sl-dialog>
    `;
  }
}
