import { LitElement, html, css, unsafeCSS, nothing } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import {
  getTrackers,
  getAIModels,
  getAllTools,
  getMCPServers,
  getAccountAgents,
  getAccountOrganization,
  getRunners,
  listOrganizations,
  listProjects,
  getFlowPresets,
  type RunnerRecord,
} from '../api';
import type { Flow } from '../types';
import {
  buildRunnerPoolOptions,
  describeNextRunnerPool,
} from '../utils/runner-pool';
import { getAgentControlState } from '../utils/agent-control';
import { getTrackerEventOptions } from '../constants/tracker-event-types';
import consoleStyles from '../styles/console-styles.css?inline';
import './add-tracker-modal';
import './add-ai-model-modal';
import './schedule-config-editor';
import { defaultScheduleConfig } from './schedule-config-editor';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/textarea/textarea.js';
import '@shoelace-style/shoelace/dist/components/select/select.js';
import '@shoelace-style/shoelace/dist/components/option/option.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/checkbox/checkbox.js';
import '@shoelace-style/shoelace/dist/components/radio-group/radio-group.js';
import '@shoelace-style/shoelace/dist/components/radio/radio.js';
import '@shoelace-style/shoelace/dist/components/card/card.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';

@customElement('preloop-flow-form')
export class PreloopFlowForm extends LitElement {
  static styles = [
    unsafeCSS(consoleStyles),
    css`
      :host {
        display: block;
      }

      .form-grid {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: var(--sl-spacing-large);
      }

      @media (max-width: 768px) {
        .form-grid {
          grid-template-columns: 1fr;
        }
      }

      sl-card {
        width: 100%;
        margin-bottom: var(--sl-spacing-large);
      }

      sl-card::part(base) {
        gap: var(--sl-spacing-large);
      }

      form {
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-large);
      }

      sl-input,
      sl-textarea,
      sl-select {
        margin-bottom: var(--sl-spacing-medium);
      }

      sl-input:last-child,
      sl-textarea:last-child,
      sl-select:last-child {
        margin-bottom: 0;
      }

      sl-textarea.prompt {
        max-height: 50rem;
        overflow: auto;
      }

      .runner-pool-hint {
        margin: 0 0 var(--sl-spacing-medium);
        color: var(--sl-color-neutral-600);
        font-size: 0.85rem;
        line-height: 1.45;
      }

      .runner-pool-custom {
        margin-top: calc(-1 * var(--sl-spacing-small));
      }

      .card-header-title {
        display: flex;
        align-items: center;
        gap: var(--sl-spacing-small);
        font-weight: 600;
        font-size: var(--sl-font-size-large);
      }

      .creation-mode-toggle {
        margin-bottom: var(--sl-spacing-large);
        padding: var(--sl-spacing-medium);
        background: var(--sl-color-neutral-50);
        border-radius: 8px;
        border: 1px solid var(--sl-color-neutral-200);
      }
      .creation-mode-toggle h3 {
        margin: 0 0 var(--sl-spacing-small) 0;
        font-size: 1rem;
      }
      .preset-card {
        cursor: pointer;
        transition:
          transform 0.2s ease,
          box-shadow 0.2s ease;
      }
      .preset-card::part(base) {
        height: 100%;
      }
      .preset-card:hover {
        transform: translateY(-2px);
        box-shadow: var(--sl-shadow-large);
      }
    `,
  ];

  @property({ type: Object })
  flow: any = {};

  @state()
  private triggerType: 'webhook' | 'tracker' | 'schedule' = 'webhook';

  @state()
  private flowExecutionPath: 'ephemeral' | 'persistent' = 'ephemeral';

  @state()
  private targetAgentId = '';

  @state()
  private trackers: any[] = [];

  @state()
  private models: any[] = [];

  @state()
  private availableTools: any[] = [];

  @state()
  private mcpServers: any[] = [];

  @state()
  private longRunningAgents: any[] = [];

  @state()
  private organizations: any[] = [];

  @state()
  private projects: any[] = [];

  @state()
  private _loadingReferenceData = true;

  @state()
  private isSaving = false;

  @state()
  private formError: string | null = null;

  @state()
  private customEventType = '';

  @state()
  private isPollingOrganizations = false;

  @state()
  private isPollingProjects = false;

  @state()
  private presets: any[] = [];

  @state()
  private creationMode: 'scratch' | 'preset' = 'preset';

  @state()
  private sourcePresetId: string | null = null;

  @state()
  private isAddingTracker = false;

  @state()
  private filtersExpanded = false;

  @state()
  private runners: RunnerRecord[] = [];

  @state()
  private accountDefaultRunnerPool: string | null = null;

  @state()
  private customRunnerPool = '';

  @state()
  private isAddingAIModel = false;

  private orgPollingInterval?: number;
  private projectPollingInterval?: number;
  private lastSyncedTriggerKey?: string;

  willUpdate(changedProperties: Map<string | number | symbol, unknown>) {
    if (changedProperties.has('flow')) {
      if (this.flow?.id) {
        this.creationMode = 'scratch';
      }
      void this.syncTriggerStateFromFlow();
    }
  }

  private handleGithubOauthStarting = () => {
    sessionStorage.setItem(
      'preloop_flow_form_state',
      JSON.stringify({
        flow: this.flow,
        triggerType: this.triggerType,
        flowExecutionPath: this.flowExecutionPath,
        targetAgentId: this.targetAgentId,
      })
    );
  };

  async connectedCallback() {
    super.connectedCallback();
    this.addEventListener(
      'github-oauth-starting',
      this.handleGithubOauthStarting
    );
    if (this.flow && this.flow.id) {
      this.creationMode = 'scratch';
    }
    await this.loadReferenceData();
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    this.removeEventListener(
      'github-oauth-starting',
      this.handleGithubOauthStarting
    );
    if (this.orgPollingInterval) clearInterval(this.orgPollingInterval);
    if (this.projectPollingInterval) clearInterval(this.projectPollingInterval);
  }

  async loadReferenceData() {
    this._loadingReferenceData = true;

    // Restore saved state if returning from OAuth
    const savedStateStr = sessionStorage.getItem('preloop_flow_form_state');
    let restoredFromOAuth = false;
    if (savedStateStr) {
      sessionStorage.removeItem('preloop_flow_form_state');
      try {
        const saved = JSON.parse(savedStateStr);
        if (saved && saved.flow) {
          this.flow = saved.flow;
          this.triggerType = saved.triggerType || 'webhook';
          this.flowExecutionPath = saved.flowExecutionPath || 'ephemeral';
          this.targetAgentId = saved.targetAgentId || '';
          restoredFromOAuth = true;
        }
      } catch (e) {
        console.error('Failed to restore saved flow form state:', e);
      }
    }

    try {
      const [
        trackers,
        models,
        tools,
        servers,
        agentsRes,
        presets,
        runners,
        account,
      ] = await Promise.all([
        getTrackers().catch(() => []),
        getAIModels().catch(() => []),
        getAllTools().catch(() => []),
        getMCPServers().catch(() => []),
        getAccountAgents({ limit: 100 }).catch(() => ({ items: [] })),
        getFlowPresets().catch(() => []),
        getRunners().catch(() => []),
        getAccountOrganization().catch(() => null),
      ]);

      this.trackers = trackers;
      this.models = models;
      this.availableTools = tools;
      this.mcpServers = servers;
      this.longRunningAgents = agentsRes.items || [];
      this.presets = presets;
      this.runners = runners;
      this.accountDefaultRunnerPool = account?.default_runner_pool ?? null;

      if (
        restoredFromOAuth &&
        this.triggerType === 'tracker' &&
        this.trackers.length > 0
      ) {
        const newestTracker = this.trackers[this.trackers.length - 1];
        this.flow.trigger_event_source = newestTracker.id;
      }

      // Check if preset_id URL parameter is present
      const urlParams = new URLSearchParams(window.location.search);
      const presetId = urlParams.get('preset_id');
      if (presetId && this.presets.length > 0) {
        const preset = this.presets.find((p) => p.id === presetId);
        if (preset) {
          this.selectPreset(preset);
        }
      }

      // Check if agent_id URL parameter is present
      const urlAgentId = urlParams.get('agent_id');
      if (urlAgentId) {
        this.targetAgentId = urlAgentId;
        this.flowExecutionPath = 'persistent';
        this.creationMode = 'scratch';
      }

      // Initialize flow fields if empty
      if (!this.flow.allowed_mcp_servers) {
        this.flow.allowed_mcp_servers = ['preloop-mcp'];
      }
      if (!this.flow.allowed_mcp_tools) {
        this.flow.allowed_mcp_tools = [];
      }
      if (!this.flow.git_clone_config) {
        this.flow.git_clone_config = { enabled: false };
      }

      // Determine initial execution path and target agent
      if (this.flow && this.flow.agent_config) {
        const cfg =
          typeof this.flow.agent_config === 'string'
            ? JSON.parse(this.flow.agent_config)
            : this.flow.agent_config;
        if (cfg && cfg.execution_path === 'persistent') {
          this.flowExecutionPath = 'persistent';
          this.targetAgentId = cfg.target_agent_id || '';
        }
      }

      // Determine trigger type and load tracker scope data
      await this.syncTriggerStateFromFlow(true);

      if (this.flowExecutionPath === 'persistent') {
        if (!this.targetAgentId && this.longRunningAgents.length > 0) {
          const enabledAgents = this.longRunningAgents.filter(
            (a) => getAgentControlState(a).enabled
          );
          if (enabledAgents.length > 0) {
            this.targetAgentId = enabledAgents[0].id;
          }
        }
        this.updateModelSelectionForAgent();
      }
    } catch (e) {
      console.error('Failed to load reference data for flow form:', e);
    } finally {
      this._loadingReferenceData = false;
      this.requestUpdate();
    }
  }

  private async syncTriggerStateFromFlow(force = false) {
    const flowId = this.flow?.id;
    const source = this.flow?.trigger_event_source;
    const orgId = this.flow?.trigger_organization_id;
    const syncKey = `${flowId ?? 'new'}:${source ?? ''}:${orgId ?? ''}:${this.trackers.length}`;

    if (!force && syncKey === this.lastSyncedTriggerKey) {
      return;
    }

    if (source === 'webhook') {
      this.triggerType = 'webhook';
    } else if (source === 'schedule') {
      this.triggerType = 'schedule';
    } else if (source) {
      this.triggerType = 'tracker';
    } else if (this.flow?.webhook_config) {
      this.triggerType = 'webhook';
    }

    if (this.triggerType === 'tracker' && source) {
      const allOrgs = await listOrganizations().catch(() => []);
      this.organizations = allOrgs.filter(
        (org: any) => org.tracker_id === source
      );

      if (this.organizations.length === 0) {
        this.startPollingOrganizations(source);
      } else if (this.orgPollingInterval) {
        clearInterval(this.orgPollingInterval);
        this.isPollingOrganizations = false;
      }

      if (this.flow.trigger_organization_id) {
        const allProjs = await listProjects().catch(() => []);
        this.projects = allProjs;

        const orgProjects = allProjs.filter(
          (project: any) =>
            project.organization_id === this.flow.trigger_organization_id
        );
        if (orgProjects.length === 0) {
          this.startPollingProjects(this.flow.trigger_organization_id);
        } else if (this.projectPollingInterval) {
          clearInterval(this.projectPollingInterval);
          this.isPollingProjects = false;
        }
      }
    }

    this.lastSyncedTriggerKey = syncKey;
    this.requestUpdate();
  }

  private updateModelSelectionForAgent() {
    if (this.flowExecutionPath === 'persistent' && this.targetAgentId) {
      const agent = this.longRunningAgents.find(
        (a) => a.id === this.targetAgentId
      );
      if (agent) {
        const configuredModelIds =
          agent.configured_models?.map((m: any) => m.ai_model_id) || [];
        if (
          this.flow.ai_model_id &&
          !configuredModelIds.includes(this.flow.ai_model_id)
        ) {
          this.flow.ai_model_id =
            configuredModelIds.length > 0 ? configuredModelIds[0] : '';
        }
      } else {
        this.flow.ai_model_id = '';
      }
    }
  }

  private handleInputChange(field: keyof Flow, e: Event) {
    const target = e.target as HTMLInputElement | HTMLTextAreaElement;
    let value: string | number | null = target.value;
    if (target.type === 'number') {
      value = value === '' ? null : Number(value);
    }
    this.flow = { ...this.flow, [field]: value };
    this.requestUpdate();
  }

  /**
   * Drop event filters that no longer apply to the selected trigger.
   *
   * Filter keys are tracker- and event-type specific and the filters UI only
   * renders for tracker triggers, so a key left behind by a trigger or tracker
   * change would keep being enforced (webhook submits fail with 422, tracker
   * events are skipped) with no way for the user to see or clear it. Explicit
   * null rather than undefined so the backend's exclude_unset update path
   * clears a previously-saved trigger_config instead of keeping it.
   */
  private clearEventFilters() {
    this.flow.trigger_config = null;
    this.filtersExpanded = false;
  }

  private handleTriggerTypeChange(newType: 'webhook' | 'tracker' | 'schedule') {
    if (newType !== this.triggerType) {
      this.clearEventFilters();
    }
    this.triggerType = newType;
    if (newType === 'webhook') {
      this.flow.trigger_event_source = 'webhook';
      this.flow.trigger_event_types = ['webhook'];
      this.flow.trigger_organization_id = undefined;
      this.flow.trigger_project_ids = undefined;
    } else if (newType === 'schedule') {
      this.flow.trigger_event_source = 'schedule';
      this.flow.trigger_event_types = ['schedule'];
      this.flow.trigger_organization_id = undefined;
      this.flow.trigger_project_ids = undefined;
      if (!this.flow.schedule_config) {
        this.flow.schedule_config = defaultScheduleConfig();
      }
    } else {
      this.flow.trigger_event_source = undefined;
      this.flow.trigger_event_types = undefined;
    }
    this.requestUpdate();
  }

  private async handleTrackerChange(e: any) {
    const trackerId = e.target.value;
    if (trackerId !== this.flow.trigger_event_source) {
      this.clearEventFilters();
    }
    this.flow.trigger_event_source = trackerId;
    this.flow.trigger_event_types = undefined;
    this.flow.trigger_organization_id = undefined;
    this.flow.trigger_project_ids = undefined;

    const allOrgs = await listOrganizations().catch(() => []);
    this.organizations = allOrgs.filter(
      (org: any) => org.tracker_id === trackerId
    );

    if (this.organizations.length === 0) {
      this.startPollingOrganizations(trackerId);
    }
    this.requestUpdate();
  }

  private async handleOrganizationChange(e: any) {
    const orgId = e.target.value;
    this.flow.trigger_organization_id = orgId;
    this.flow.trigger_project_ids = undefined;

    const allProjs = await listProjects().catch(() => []);
    this.projects = allProjs;

    const orgProjects = allProjs.filter(
      (p: any) => p.organization_id === orgId
    );
    if (orgProjects.length === 0) {
      this.startPollingProjects(orgId);
    }
    this.requestUpdate();
  }

  private startPollingOrganizations(trackerId: string) {
    if (this.orgPollingInterval) clearInterval(this.orgPollingInterval);
    this.isPollingOrganizations = true;
    this.orgPollingInterval = window.setInterval(async () => {
      const allOrgs = await listOrganizations().catch(() => []);
      const orgs = allOrgs.filter((org: any) => org.tracker_id === trackerId);
      if (orgs.length > 0) {
        this.organizations = orgs;
        this.isPollingOrganizations = false;
        clearInterval(this.orgPollingInterval);
      }
    }, 2000);
  }

  private startPollingProjects(orgId: string) {
    if (this.projectPollingInterval) clearInterval(this.projectPollingInterval);
    this.isPollingProjects = true;
    this.projectPollingInterval = window.setInterval(async () => {
      const allProjs = await listProjects().catch(() => []);
      const projs = allProjs.filter((p: any) => p.organization_id === orgId);
      if (projs.length > 0) {
        this.projects = allProjs;
        this.isPollingProjects = false;
        clearInterval(this.projectPollingInterval);
      }
    }, 2000);
  }

  private getEventOptions() {
    const tracker = this.trackers.find(
      (t) => t.id === this.flow.trigger_event_source
    );
    if (!tracker) return [];
    return getTrackerEventOptions(tracker.tracker_type);
  }

  private isToolSelected(serverName: string, toolName: string): boolean {
    if (!this.flow.allowed_mcp_tools) return false;
    return this.flow.allowed_mcp_tools.some(
      (t: any) => t.server_name === serverName && t.tool_name === toolName
    );
  }

  private handleToolToggle(
    serverName: string,
    toolName: string,
    checked: boolean
  ) {
    if (!this.flow.allowed_mcp_tools) {
      this.flow.allowed_mcp_tools = [];
    }

    if (checked) {
      this.flow.allowed_mcp_tools.push({
        server_name: serverName,
        tool_name: toolName,
      });
    } else {
      this.flow.allowed_mcp_tools = this.flow.allowed_mcp_tools.filter(
        (t: any) => !(t.server_name === serverName && t.tool_name === toolName)
      );
    }
    this.requestUpdate();
  }

  private handleGitCloneToggle(checked: boolean) {
    this.flow.git_clone_config = {
      ...this.flow.git_clone_config,
      enabled: checked,
    };
    this.requestUpdate();
  }

  private async handleFormSubmit(e: Event) {
    e.preventDefault();
    this.formError = null;

    if (!this.flow.name) {
      this.formError = 'Flow Name is required.';
      return;
    }

    this.isSaving = true;
    try {
      const payload: any = {
        name: this.flow.name,
        description: this.flow.description || '',
        prompt_template: this.flow.prompt_template || '',
        agent_type: this.flow.agent_type || 'codex',
        agent_config:
          this.longRunningAgents.length > 0
            ? {
                execution_path: this.flowExecutionPath,
                target_agent_id:
                  this.flowExecutionPath === 'persistent'
                    ? this.targetAgentId
                    : undefined,
              }
            : this.flow.agent_config || {},
        allowed_mcp_servers: this.flow.allowed_mcp_servers || ['preloop-mcp'],
        allowed_mcp_tools: this.flow.allowed_mcp_tools || [],
        ai_model_id: this.flow.ai_model_id || undefined,
        trigger_event_source: this.flow.trigger_event_source || 'webhook',
        trigger_event_types: this.flow.trigger_event_types || ['webhook'],
        trigger_organization_id: this.flow.trigger_organization_id || undefined,
        trigger_project_ids: this.flow.trigger_project_ids || undefined,
        // Explicit null (not undefined) so the backend's exclude_unset update
        // path clears a previously-saved schedule_config when the trigger is
        // changed away from Schedule.
        schedule_config:
          this.triggerType === 'schedule'
            ? this.flow.schedule_config || defaultScheduleConfig()
            : null,
        git_clone_config: this.flow.git_clone_config || { enabled: false },
        max_iterations: this.flow.max_iterations || undefined,
        max_budget: this.flow.max_budget || undefined,
        is_enabled: this.flow.is_enabled ?? true,
        runner_pool: this.normalizedFlowRunnerPool(),
        // Sent only once filters exist on the form. An explicit null (set by
        // clearEventFilters) is forwarded so the backend clears saved filters.
        ...(this.flow.trigger_config !== undefined
          ? { trigger_config: this.flow.trigger_config }
          : {}),
      };

      if (!this.flow.id && this.sourcePresetId) {
        payload.source_preset_id = this.sourcePresetId;
        payload.prompt_customized = false;
        payload.tools_customized = false;
        payload.preset_update_available = false;
      }

      this.dispatchEvent(
        new CustomEvent('flow-submit', {
          bubbles: true,
          composed: true,
          detail: { flow: payload },
        })
      );
    } catch (e) {
      this.formError =
        e instanceof Error ? e.message : 'Failed to configure flow.';
    } finally {
      this.isSaving = false;
    }
  }

  private normalizedFlowRunnerPool(): string | null {
    const typed = (this.customRunnerPool || '').trim();
    if (typed) {
      return typed;
    }
    const selected = (this.flow.runner_pool || '').trim();
    return selected || null;
  }

  private runnerPoolSelectValue(): string {
    return (this.flow.runner_pool || '').trim();
  }

  private handleRunnerPoolSelect(event: Event) {
    const target = event.target as HTMLSelectElement;
    const value = (target.value || '').trim();
    this.flow.runner_pool = value || null;
    this.customRunnerPool = '';
    this.requestUpdate();
  }

  private handleCustomRunnerPool(event: Event) {
    const target = event.target as HTMLInputElement;
    const value = target.value;
    this.customRunnerPool = value;
    this.flow.runner_pool = value.trim() || null;
    this.requestUpdate();
  }

  private renderRunnerPoolField() {
    const options = buildRunnerPoolOptions(this.runners);
    const current = (this.flow.runner_pool || '').trim();
    const known = new Set(options.map((option) => option.value));
    if (current && !known.has(current)) {
      options.push({ value: current, label: current });
    }
    const hint = describeNextRunnerPool({
      flowPool: this.normalizedFlowRunnerPool(),
      accountPool: this.accountDefaultRunnerPool,
      runners: this.runners,
    });
    return html`
      <sl-select
        label="Runner pool"
        help-text="Pin a private runner, a label, or Preloop hosted. Leave the default to follow the account setting."
        .value=${this.runnerPoolSelectValue()}
        @sl-change=${this.handleRunnerPoolSelect}
      >
        ${options.map(
          (option) =>
            html`<sl-option value=${option.value}>${option.label}</sl-option>`
        )}
      </sl-select>
      <sl-input
        class="runner-pool-custom"
        label="Or type a runner label"
        placeholder="gpu"
        .value=${this.customRunnerPool}
        @sl-input=${this.handleCustomRunnerPool}
      ></sl-input>
      <p class="runner-pool-hint">${hint}</p>
    `;
  }

  private handleCancel() {
    this.dispatchEvent(
      new CustomEvent('flow-cancel', {
        bubbles: true,
        composed: true,
      })
    );
  }

  private closeAddTrackerDialog() {
    this.isAddingTracker = false;
  }

  private openAddTrackerDialog() {
    this.isAddingTracker = true;
  }

  private async handleTrackerAdded(event: CustomEvent) {
    if (!event.detail?.hasWarnings) {
      this.isAddingTracker = false;
    }
    this.trackers = await getTrackers().catch(() => []);

    if (this.triggerType === 'tracker' && this.trackers.length > 0) {
      const newestTracker = this.trackers[this.trackers.length - 1];
      this.flow.trigger_event_source = newestTracker.id;

      const allOrganizations = await listOrganizations().catch(() => []);
      this.organizations = allOrganizations.filter(
        (org: any) => org.tracker_id === newestTracker.id
      );

      if (this.organizations.length === 0) {
        this.startPollingOrganizations(newestTracker.id);
      }
    }
    this.requestUpdate();
  }

  private openAddAIModelDialog() {
    this.isAddingAIModel = true;
  }

  private closeAIModelDialog() {
    this.isAddingAIModel = false;
  }

  private async handleAIModelCreated(event: CustomEvent) {
    const newModel = event.detail.model;
    this.isAddingAIModel = false;
    this.models = await getAIModels().catch(() => []);
    if (newModel && newModel.id) {
      this.flow.ai_model_id = newModel.id;
    }
    this.requestUpdate();
  }

  private selectPreset(preset: any) {
    this.flow = { ...preset };
    this.sourcePresetId = preset.id;

    if (preset.allowed_mcp_tools && Array.isArray(preset.allowed_mcp_tools)) {
      this.flow.allowed_mcp_tools = preset.allowed_mcp_tools.map(
        (tool: { name: string }) => ({
          server_name: 'preloop-mcp',
          tool_name: tool.name,
        })
      );
    } else {
      this.flow.allowed_mcp_tools = [];
    }

    if (!this.flow.allowed_mcp_servers?.includes('preloop-mcp')) {
      this.flow.allowed_mcp_servers = ['preloop-mcp'];
    }

    this.flow.is_enabled = true;
    this._autoPopulatePresetFields();
    this.creationMode = 'scratch';
  }

  private async _autoPopulatePresetFields() {
    if (this.trackers.length > 0 && !this.flow.trigger_event_source) {
      const tracker = this.trackers[this.trackers.length - 1];
      this.flow.trigger_event_source = tracker.id;
      this.triggerType = 'tracker';

      if (tracker.tracker_type === 'github') {
        this.flow.trigger_event_types = [
          'pull_request_opened',
          'pull_request_updated',
        ];
      } else if (tracker.tracker_type === 'gitlab') {
        this.flow.trigger_event_types = [
          'merge_request_opened',
          'merge_request_updated',
        ];
      }

      const allOrganizations = await listOrganizations().catch(() => []);
      this.organizations = allOrganizations.filter(
        (org: any) => org.tracker_id === tracker.id
      );

      if (this.organizations.length > 0) {
        const org = this.organizations[0];
        this.flow.trigger_organization_id = org.id;

        const allProjects = await listProjects().catch(() => []);
        this.projects = allProjects;
        const orgProjects = allProjects.filter(
          (proj: any) => proj.organization_id === org.id
        );
        if (orgProjects.length > 0) {
          this.flow.trigger_project_ids = orgProjects.map(
            (proj: any) => proj.id
          );
        }
      } else {
        this.startPollingOrganizations(tracker.id);
      }
    }

    if (!this.flow.ai_model_id) {
      let selectableModels = this.models.filter(
        (m) => m.model_kind !== 'stt' && m.model_kind !== 'tts'
      );
      if (this.flowExecutionPath === 'persistent' && this.targetAgentId) {
        const agent = this.longRunningAgents.find(
          (a) => a.id === this.targetAgentId
        );
        if (agent) {
          const configuredModelIds =
            agent.configured_models?.map((m: any) => m.ai_model_id) || [];
          selectableModels = selectableModels.filter((m) =>
            configuredModelIds.includes(m.id)
          );
        }
      }
      if (selectableModels.length > 0) {
        this.flow.ai_model_id =
          selectableModels[selectableModels.length - 1].id;
      }
    }

    this.requestUpdate();
  }

  private renderCreationModeSelector() {
    return html`
      <div class="creation-mode-toggle">
        <h3
          style="margin: 0 0 var(--sl-spacing-small) 0; font-size: 1rem; font-weight: 600;"
        >
          How would you like to start?
        </h3>
        <p
          style="margin: 0 0 var(--sl-spacing-small) 0; color: var(--sl-color-neutral-600); font-size: var(--sl-font-size-small);"
        >
          Choose whether to build a flow from scratch or start from a preset
          template.
        </p>
        <sl-radio-group
          value=${this.creationMode}
          @sl-change=${(event: CustomEvent) => {
            this.creationMode = (event.target as HTMLInputElement).value as
              'scratch' | 'preset';
            this.requestUpdate();
          }}
        >
          <sl-radio value="scratch">Create from scratch</sl-radio>
          <sl-radio value="preset">Use a preset template</sl-radio>
        </sl-radio-group>
      </div>
    `;
  }

  private renderPresets() {
    const sortedPresets = [...this.presets].sort((a, b) => {
      const aIsPR = a.name?.toLowerCase().includes('pull request reviewer')
        ? 0
        : 1;
      const bIsPR = b.name?.toLowerCase().includes('pull request reviewer')
        ? 0
        : 1;
      return aIsPR - bIsPR;
    });

    return html`
      ${this.renderCreationModeSelector()}
      <h2
        style="font-size: var(--sl-font-size-large); font-weight: 600; color: var(--sl-color-neutral-800); margin: var(--sl-spacing-large) 0 var(--sl-spacing-medium) 0;"
      >
        Select a Preset
      </h2>
      <div class="form-grid" style="margin-bottom: var(--sl-spacing-large);">
        ${sortedPresets.map(
          (preset) => html`
            <sl-card
              class="preset-card"
              @click=${() => this.selectPreset(preset)}
            >
              <div slot="header" style="font-weight: 600;">${preset.name}</div>
              ${preset.description}
            </sl-card>
          `
        )}
      </div>
      <div
        style="display: flex; gap: var(--sl-spacing-medium); justify-content: flex-end; margin-bottom: var(--sl-spacing-2x-large);"
      >
        <sl-button variant="default" @click=${this.handleCancel}>
          Cancel
        </sl-button>
        <sl-button
          variant="primary"
          @click=${() => (this.creationMode = 'scratch')}
        >
          Create from scratch
        </sl-button>
      </div>
    `;
  }

  private renderEventFilters() {
    const tracker = this.trackers.find(
      (t: any) => t.id === this.flow.trigger_event_source
    );
    if (!tracker) return nothing;

    // Check if any filters are defined. trigger_config stays absent until a
    // filter is actually set: the input handlers below create it lazily, so
    // render never mutates it.
    const hasFilters = Object.keys(this.flow.trigger_config ?? {}).length > 0;

    // Show filters if expanded or if any filter is already defined
    const showFilters = this.filtersExpanded || hasFilters;

    // Determine if this is a PR/MR event
    const eventTypes: string[] = this.flow.trigger_event_types || [];
    const isMREvent = eventTypes.some(
      (et: string) =>
        et?.includes('merge_request') || et?.includes('pull_request')
    );

    return html`
      <div style="margin-top: 1.5rem;">
        <div
          style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.75rem;"
        >
          <label style="font-weight: 500;">
            Event Filters (Optional)
            <span style="font-weight: 400; color: var(--sl-color-neutral-600);">
              - Only trigger when conditions match
            </span>
          </label>
          ${
            !showFilters
              ? html`
                  <sl-button
                    size="small"
                    @click=${() => (this.filtersExpanded = true)}
                  >
                    <sl-icon slot="prefix" name="plus-circle"></sl-icon>
                    Add Filters
                  </sl-button>
                `
              : html`
                  <sl-button
                    size="small"
                    variant="text"
                    @click=${() => (this.filtersExpanded = false)}
                  >
                    <sl-icon slot="prefix" name="dash-circle"></sl-icon>
                    Hide Filters
                  </sl-button>
                `
          }
        </div>

        ${
          showFilters
            ? html`
                <div class="form-grid">
                  <!-- Author/Creator filter -->
                  <sl-input
                    label="Created By (username)"
                    placeholder="e.g., octocat, admin@example.com"
                    .value=${this.flow.trigger_config?.author || ''}
                    @sl-input=${(e: any) => {
                      if (!this.flow.trigger_config)
                        this.flow.trigger_config = {};
                      const value = e.target.value.trim();
                      if (value) {
                        this.flow.trigger_config.author = value;
                      } else {
                        delete this.flow.trigger_config.author;
                      }
                      this.requestUpdate();
                    }}
                    help-text="Filter by who created the issue/PR"
                  ></sl-input>

                  <!-- Assignee filter -->
                  <sl-input
                    label="Assigned To (username)"
                    placeholder="e.g., john_doe"
                    .value=${this.flow.trigger_config?.assignee || ''}
                    @sl-input=${(e: any) => {
                      if (!this.flow.trigger_config)
                        this.flow.trigger_config = {};
                      const value = e.target.value.trim();
                      if (value) {
                        this.flow.trigger_config.assignee = value;
                      } else {
                        delete this.flow.trigger_config.assignee;
                      }
                      this.requestUpdate();
                    }}
                    help-text="Filter by assignee (matches if any assignee matches)"
                  ></sl-input>

                  <!-- Reviewer filter (PR/MR only) -->
                  ${
                    isMREvent
                      ? html`
                          <sl-input
                            label="${
                              tracker.tracker_type === 'gitlab'
                                ? 'Reviewer (username)'
                                : 'Requested Reviewer (username)'
                            }"
                            placeholder="e.g., jane_smith"
                            .value=${this.flow.trigger_config?.reviewer || ''}
                            @sl-input=${(e: any) => {
                              if (!this.flow.trigger_config)
                                this.flow.trigger_config = {};
                              const value = e.target.value.trim();
                              if (value) {
                                this.flow.trigger_config.reviewer = value;
                              } else {
                                delete this.flow.trigger_config.reviewer;
                              }
                              this.requestUpdate();
                            }}
                            help-text="Filter by reviewer (matches if any reviewer matches)"
                          ></sl-input>
                        `
                      : nothing
                  }

                  <!-- Labels filter -->
                  <sl-input
                    label="Labels (comma-separated)"
                    placeholder="e.g., bug, critical, backend"
                    .value=${(this.flow.trigger_config?.labels as string[])?.join(', ') || ''}
                    @sl-input=${(e: any) => {
                      if (!this.flow.trigger_config)
                        this.flow.trigger_config = {};
                      const value = e.target.value.trim();
                      if (value) {
                        this.flow.trigger_config.labels = value
                          .split(',')
                          .map((l: string) => l.trim())
                          .filter((l: string) => l.length > 0);
                      } else {
                        delete this.flow.trigger_config.labels;
                      }
                      this.requestUpdate();
                    }}
                    help-text="Filter by labels (triggers if ANY label matches)"
                  ></sl-input>

                  <!-- Milestone filter (GitHub/GitLab only) -->
                  ${
                    tracker.tracker_type !== 'jira'
                      ? html`
                          <sl-input
                            label="Milestone"
                            placeholder="e.g., v1.0, Sprint 10"
                            .value=${this.flow.trigger_config?.milestone || ''}
                            @sl-input=${(e: any) => {
                              if (!this.flow.trigger_config)
                                this.flow.trigger_config = {};
                              const value = e.target.value.trim();
                              if (value) {
                                this.flow.trigger_config.milestone = value;
                              } else {
                                delete this.flow.trigger_config.milestone;
                              }
                              this.requestUpdate();
                            }}
                            help-text="Filter by milestone name"
                          ></sl-input>
                        `
                      : nothing
                  }

                  <!-- Priority filter (Jira only) -->
                  ${
                    tracker.tracker_type === 'jira'
                      ? html`
                          <sl-select
                            label="Priority"
                            .value=${this.flow.trigger_config?.priority || ''}
                            @sl-change=${(e: any) => {
                              if (!this.flow.trigger_config)
                                this.flow.trigger_config = {};
                              const value = e.target.value;
                              if (value) {
                                this.flow.trigger_config.priority = value;
                              } else {
                                delete this.flow.trigger_config.priority;
                              }
                              this.requestUpdate();
                            }}
                            clearable
                          >
                            <sl-option value="">Any Priority</sl-option>
                            <sl-option value="Highest">Highest</sl-option>
                            <sl-option value="High">High</sl-option>
                            <sl-option value="Medium">Medium</sl-option>
                            <sl-option value="Low">Low</sl-option>
                            <sl-option value="Lowest">Lowest</sl-option>
                          </sl-select>

                          <sl-input
                            label="Issue Type"
                            placeholder="e.g., Task, Bug, Story"
                            .value=${this.flow.trigger_config?.issue_type || ''}
                            @sl-input=${(e: any) => {
                              if (!this.flow.trigger_config)
                                this.flow.trigger_config = {};
                              const value = e.target.value.trim();
                              if (value) {
                                this.flow.trigger_config.issue_type = value;
                              } else {
                                delete this.flow.trigger_config.issue_type;
                              }
                              this.requestUpdate();
                            }}
                            help-text="Filter by Jira issue type"
                          ></sl-input>
                        `
                      : nothing
                  }

                  <!-- Merge Request / Pull Request State Filters -->
                  ${
                    isMREvent && tracker.tracker_type !== 'jira'
                      ? html`
                          <sl-checkbox
                            ?checked=${this.flow.trigger_config?.merged === true}
                            @sl-change=${(e: any) => {
                              if (!this.flow.trigger_config)
                                this.flow.trigger_config = {};
                              if (e.target.checked) {
                                this.flow.trigger_config.merged = true;
                              } else {
                                delete this.flow.trigger_config.merged;
                              }
                              this.requestUpdate();
                            }}
                          >
                            Only when
                            ${
                              tracker.tracker_type === 'gitlab'
                                ? 'Merge Request'
                                : 'Pull Request'
                            }
                            is merged
                          </sl-checkbox>

                          <sl-checkbox
                            ?checked=${this.flow.trigger_config?.draft === false}
                            @sl-change=${(e: any) => {
                              if (!this.flow.trigger_config)
                                this.flow.trigger_config = {};
                              if (e.target.checked) {
                                this.flow.trigger_config.draft = false;
                              } else {
                                delete this.flow.trigger_config.draft;
                              }
                              this.requestUpdate();
                            }}
                          >
                            Only when marked as ready (not draft)
                          </sl-checkbox>

                          ${
                            tracker.tracker_type === 'gitlab'
                              ? html`
                                  <sl-checkbox
                                    ?checked=${
                                      this.flow.trigger_config
                                        ?.detailed_merge_status === 'approved'
                                    }
                                    @sl-change=${(e: any) => {
                                      if (!this.flow.trigger_config)
                                        this.flow.trigger_config = {};
                                      if (e.target.checked) {
                                        this.flow.trigger_config.detailed_merge_status =
                                          'approved';
                                      } else {
                                        delete this.flow.trigger_config
                                          .detailed_merge_status;
                                      }
                                      this.requestUpdate();
                                    }}
                                  >
                                    Only when approved
                                  </sl-checkbox>

                                  <sl-select
                                    label="Merge Status"
                                    .value=${this.flow.trigger_config?.state || ''}
                                    @sl-change=${(e: any) => {
                                      if (!this.flow.trigger_config)
                                        this.flow.trigger_config = {};
                                      const value = e.target.value;
                                      if (value) {
                                        this.flow.trigger_config.state = value;
                                      } else {
                                        delete this.flow.trigger_config.state;
                                      }
                                      this.requestUpdate();
                                    }}
                                    clearable
                                    help-text="Filter by merge request state"
                                  >
                                    <sl-option value="">Any State</sl-option>
                                    <sl-option value="opened">Opened</sl-option>
                                    <sl-option value="closed">Closed</sl-option>
                                    <sl-option value="merged">Merged</sl-option>
                                  </sl-select>
                                `
                              : tracker.tracker_type === 'github'
                                ? html`
                                    <sl-select
                                      label="Pull Request State"
                                      .value=${this.flow.trigger_config?.state || ''}
                                      @sl-change=${(e: any) => {
                                        if (!this.flow.trigger_config)
                                          this.flow.trigger_config = {};
                                        const value = e.target.value;
                                        if (value) {
                                          this.flow.trigger_config.state =
                                            value;
                                        } else {
                                          delete this.flow.trigger_config.state;
                                        }
                                        this.requestUpdate();
                                      }}
                                      clearable
                                      help-text="Filter by pull request state"
                                    >
                                      <sl-option value="">Any State</sl-option>
                                      <sl-option value="open">Open</sl-option>
                                      <sl-option value="closed"
                                        >Closed</sl-option
                                      >
                                    </sl-select>

                                    <sl-select
                                      label="Mergeable State"
                                      .value=${
                                        this.flow.trigger_config
                                          ?.mergeable_state || ''
                                      }
                                      @sl-change=${(e: any) => {
                                        if (!this.flow.trigger_config)
                                          this.flow.trigger_config = {};
                                        const value = e.target.value;
                                        if (value) {
                                          this.flow.trigger_config.mergeable_state =
                                            value;
                                        } else {
                                          delete this.flow.trigger_config
                                            .mergeable_state;
                                        }
                                        this.requestUpdate();
                                      }}
                                      clearable
                                      help-text="Filter by whether PR can be merged"
                                    >
                                      <sl-option value="">Any</sl-option>
                                      <sl-option value="clean"
                                        >Clean (can merge)</sl-option
                                      >
                                      <sl-option value="unstable"
                                        >Unstable (tests failing)</sl-option
                                      >
                                      <sl-option value="dirty"
                                        >Dirty (merge conflict)</sl-option
                                      >
                                      <sl-option value="blocked"
                                        >Blocked</sl-option
                                      >
                                    </sl-select>
                                  `
                                : nothing
                          }
                        `
                      : nothing
                  }
                </div>

                <sl-alert variant="primary" open style="margin-top: 1rem;">
                  <sl-icon slot="icon" name="info-circle"></sl-icon>
                  <strong>How filters work:</strong> Leave empty to match all
                  events. When multiple filters are set, ALL conditions must
                  match for the flow to trigger.
                </sl-alert>
              `
            : nothing
        }
      </div>
    `;
  }

  render() {
    if (this._loadingReferenceData) {
      return html`
        <div
          style="display: flex; flex-direction: column; align-items: center; justify-content: center; padding: var(--sl-spacing-3x-large); gap: var(--sl-spacing-medium);"
        >
          <sl-spinner style="font-size: 2.5rem;"></sl-spinner>
          <div style="color: var(--sl-color-neutral-600);">
            Loading flow reference models, tools, and trackers...
          </div>
        </div>
      `;
    }

    if (!this.flow.id && this.creationMode === 'preset') {
      return this.renderPresets();
    }

    let selectableModels = this.models.filter(
      (m) => m.model_kind !== 'stt' && m.model_kind !== 'tts'
    );
    if (this.flowExecutionPath === 'persistent' && this.targetAgentId) {
      const agent = this.longRunningAgents.find(
        (a) => a.id === this.targetAgentId
      );
      if (agent) {
        const configuredModelIds =
          agent.configured_models?.map((m: any) => m.ai_model_id) || [];
        selectableModels = selectableModels.filter((m) =>
          configuredModelIds.includes(m.id)
        );
      }
    }
    const builtinTools = this.availableTools.filter(
      (t) => t.source === 'builtin'
    );
    const mcpTools = this.availableTools.filter((t) => t.source === 'mcp');

    return html`
      ${
        this.isAddingTracker
          ? html`<add-tracker-modal
              @tracker-added=${this.handleTrackerAdded}
              @close-modal=${this.closeAddTrackerDialog}
            ></add-tracker-modal>`
          : ''
      }
      <add-ai-model-modal
        ?open=${this.isAddingAIModel}
        @model-created=${this.handleAIModelCreated}
        @close-modal=${this.closeAIModelDialog}
      ></add-ai-model-modal>

      ${!this.flow.id ? this.renderCreationModeSelector() : ''}

      <form @submit=${this.handleFormSubmit}>
        <sl-card>
          <div slot="header" class="card-header-title">
            <sl-icon name="info-circle"></sl-icon> Flow Information
          </div>
          <sl-input
            label="Flow Name"
            .value=${this.flow.name || ''}
            @sl-input=${(e: Event) => this.handleInputChange('name', e)}
            required
            placeholder="e.g. PR Code Reviewer"
          ></sl-input>
          <sl-textarea
            label="Description"
            .value=${this.flow.description || ''}
            @sl-input=${(e: Event) => this.handleInputChange('description', e)}
            placeholder="Describe the purpose of this flow..."
          ></sl-textarea>
        </sl-card>

        <sl-card>
          <div slot="header" class="card-header-title">
            <sl-icon name="calendar-event"></sl-icon> Trigger Configuration
          </div>

          <div style="margin-bottom: var(--sl-spacing-large);">
            <label
              style="display: block; margin-bottom: 0.5rem; font-weight: 500;"
            >
              Trigger Type
            </label>
            <sl-radio-group
              value=${this.triggerType}
              @sl-change=${(e: any) =>
                this.handleTriggerTypeChange(e.target.value)}
              style="display: flex; gap: var(--sl-spacing-large);"
            >
              <sl-radio value="webhook">Webhook</sl-radio>
              <sl-radio value="tracker">Tracker Event</sl-radio>
              <sl-radio value="schedule">Schedule</sl-radio>
            </sl-radio-group>
          </div>

          ${
            this.triggerType === 'webhook'
              ? html`
                  <div>
                    <p
                      style="color: var(--sl-color-neutral-600); margin-bottom: var(--sl-spacing-medium);"
                    >
                      This flow will be triggered by an external POST HTTP
                      webhook call. Webhook endpoint URLs will be generated
                      after creation.
                    </p>
                  </div>
                `
              : this.triggerType === 'schedule'
                ? html`
                    <div>
                      <p
                        style="color: var(--sl-color-neutral-600); margin-bottom: var(--sl-spacing-medium);"
                      >
                        This flow runs automatically on the schedule below.
                        Pausing the flow suspends the schedule.
                      </p>
                      <schedule-config-editor
                        .value=${this.flow.schedule_config}
                        @schedule-change=${(e: CustomEvent) => {
                          this.flow.schedule_config = e.detail.value;
                        }}
                      ></schedule-config-editor>
                    </div>
                  `
                : html`
                    <div class="form-grid">
                      <div
                        style="display: flex; flex-direction: column; gap: var(--sl-spacing-2x-small);"
                      >
                        <sl-select
                          label="Tracker"
                          placeholder="Select tracking source"
                          .value=${this.flow.trigger_event_source || ''}
                          @sl-change=${this.handleTrackerChange}
                          style="margin-bottom: 0;"
                        >
                          ${this.trackers.map(
                            (t) =>
                              html`<sl-option .value=${t.id}
                                >${t.name} (${t.tracker_type})</sl-option
                              >`
                          )}
                        </sl-select>
                        <sl-button
                          size="small"
                          variant="text"
                          @click=${this.openAddTrackerDialog}
                          style="align-self: flex-start; margin-top: -0.25rem; height: auto; padding: 0;"
                        >
                          <sl-icon slot="prefix" name="plus-lg"></sl-icon> Add
                          New Tracker
                        </sl-button>
                      </div>

                      <sl-select
                        label="Organization"
                        placeholder="Select organization"
                        .value=${this.flow.trigger_organization_id || ''}
                        @sl-change=${this.handleOrganizationChange}
                        ?disabled=${
                          this.isPollingOrganizations ||
                          !this.flow.trigger_event_source
                        }
                      >
                        ${this.organizations.map(
                          (org) =>
                            html`<sl-option .value=${org.id}
                              >${org.name}</sl-option
                            >`
                        )}
                      </sl-select>

                      <sl-select
                        label="Projects (Optional)"
                        placeholder="All projects"
                        multiple
                        clearable
                        .value=${this.flow.trigger_project_ids || []}
                        @sl-change=${(e: any) => {
                          this.flow.trigger_project_ids = e.target.value;
                        }}
                        ?disabled=${!this.flow.trigger_organization_id}
                      >
                        ${this.projects
                          .filter(
                            (p) =>
                              p.organization_id ===
                              this.flow.trigger_organization_id
                          )
                          .map(
                            (p) =>
                              html`<sl-option .value=${p.id}
                                >${p.name || p.identifier || p.key}</sl-option
                              >`
                          )}
                      </sl-select>

                      <sl-select
                        label="Events"
                        placeholder="Select trigger event kinds"
                        multiple
                        .value=${this.flow.trigger_event_types || []}
                        @sl-change=${(e: any) => {
                          this.flow.trigger_event_types = e.target.value;
                        }}
                      >
                        ${this.getEventOptions().map(
                          (ev) =>
                            html`<sl-option .value=${ev.value}
                              >${ev.name}</sl-option
                            >`
                        )}
                      </sl-select>
                    </div>
                    ${this.flow.trigger_event_source ? this.renderEventFilters() : nothing}
                  `
          }
        </sl-card>

        <sl-card>
          <div slot="header" class="card-header-title">
            <sl-icon name="robot"></sl-icon> AI Agent & Model Configuration
          </div>

          ${
            this.longRunningAgents.length > 0
              ? html`
                  <div style="margin-bottom: var(--sl-spacing-large);">
                    <label
                      style="display: block; margin-bottom: 0.5rem; font-weight: 500;"
                    >
                      Execution Mode
                    </label>
                    <sl-radio-group
                      value=${this.flowExecutionPath}
                      @sl-change=${(e: any) => {
                        this.flowExecutionPath = e.target.value as
                          'ephemeral' | 'persistent';
                        if (this.flowExecutionPath === 'persistent') {
                          if (
                            !this.targetAgentId &&
                            this.longRunningAgents.length > 0
                          ) {
                            const enabledAgents = this.longRunningAgents.filter(
                              (a) => getAgentControlState(a).enabled
                            );
                            if (enabledAgents.length > 0) {
                              this.targetAgentId = enabledAgents[0].id;
                            }
                          }
                          this.updateModelSelectionForAgent();
                        }
                        this.requestUpdate();
                      }}
                      style="display: flex; gap: var(--sl-spacing-large);"
                    >
                      <sl-radio value="ephemeral"
                        >Ephemeral (Provision on-demand short-lived
                        agent)</sl-radio
                      >
                      <sl-radio value="persistent"
                        >Persistent (Govern persistent agent node)</sl-radio
                      >
                    </sl-radio-group>
                  </div>
                `
              : nothing
          }
          ${
            this.flowExecutionPath === 'persistent' &&
            this.longRunningAgents.length > 0
              ? html`
                  <sl-select
                    label="Target Long-Running Agent"
                    .value=${this.targetAgentId}
                    @sl-change=${(e: any) => {
                      this.targetAgentId = e.target.value;
                      this.updateModelSelectionForAgent();
                      this.requestUpdate();
                    }}
                    required
                  >
                    ${this.longRunningAgents
                      .filter((a) => getAgentControlState(a).enabled)
                      .map(
                        (a) =>
                          html`<sl-option .value=${a.id}
                            >${a.display_name}
                            (${a.agent_kind || 'ssh'})</sl-option
                          >`
                      )}
                  </sl-select>
                `
              : html`
                  <sl-select
                    label="AI Agent Runtime Type"
                    .value=${this.flow.agent_type || 'codex'}
                    @sl-change=${(e: any) => {
                      this.flow.agent_type = e.target.value;
                      this.requestUpdate();
                    }}
                  >
                    <sl-option value="codex">Codex CLI</sl-option>
                    <sl-option value="gemini">Gemini CLI</sl-option>
                    <sl-option value="opencode">OpenCode</sl-option>
                  </sl-select>
                `
          }

          <div
            style="display: flex; flex-direction: column; gap: var(--sl-spacing-2x-small); margin-bottom: var(--sl-spacing-medium);"
          >
            <sl-select
              label="AI Model"
              placeholder="Select AI model"
              .value=${this.flow.ai_model_id || ''}
              @sl-change=${(e: any) => {
                this.flow.ai_model_id = e.target.value;
              }}
              style="margin-bottom: 0;"
            >
              ${selectableModels.map(
                (m) => html`<sl-option .value=${m.id}>${m.name}</sl-option>`
              )}
            </sl-select>
            <sl-button
              size="small"
              variant="text"
              @click=${this.openAddAIModelDialog}
              style="align-self: flex-start; margin-top: -0.25rem; height: auto; padding: 0;"
            >
              <sl-icon slot="prefix" name="plus-lg"></sl-icon> Add New AI Model
            </sl-button>
          </div>

          ${this.renderRunnerPoolField()}

          <sl-textarea
            class="prompt"
            label="System/Flow Prompt Template"
            rows="6"
            placeholder="System instruction that directs the agent's goal..."
            .value=${this.flow.prompt_template || ''}
            @sl-input=${(e: Event) =>
              this.handleInputChange('prompt_template', e)}
          ></sl-textarea>
        </sl-card>

        <sl-card>
          <div slot="header" class="card-header-title">
            <sl-icon name="tools"></sl-icon> Allowed MCP Tools
          </div>

          <div
            style="display: flex; flex-direction: column; gap: var(--sl-spacing-medium);"
          >
            ${
              builtinTools.length > 0
                ? html`
                    <div>
                      <h5
                        style="font-weight: 600; color: var(--sl-color-neutral-600); text-transform: uppercase; font-size: 0.8rem; margin: 0 0 0.5rem 0;"
                      >
                        Built-in Tools
                      </h5>
                      <div
                        style="display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: var(--sl-spacing-medium);"
                      >
                        ${builtinTools.map(
                          (t) => html`
                            <sl-checkbox
                              .checked=${this.isToolSelected(
                                'preloop-mcp',
                                t.name
                              )}
                              @sl-change=${(e: any) =>
                                this.handleToolToggle(
                                  'preloop-mcp',
                                  t.name,
                                  e.target.checked
                                )}
                              ?disabled=${t.is_supported === false}
                            >
                              ${t.name}
                            </sl-checkbox>
                          `
                        )}
                      </div>
                    </div>
                  `
                : nothing
            }
            ${
              mcpTools.length > 0
                ? html`
                    <div style="margin-top: var(--sl-spacing-medium);">
                      <h5
                        style="font-weight: 600; color: var(--sl-color-neutral-600); text-transform: uppercase; font-size: 0.8rem; margin: 0 0 0.5rem 0;"
                      >
                        MCP Server Tools
                      </h5>
                      <div
                        style="display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: var(--sl-spacing-medium);"
                      >
                        ${mcpTools.map(
                          (t) => html`
                            <sl-checkbox
                              .checked=${this.isToolSelected(
                                'preloop-mcp',
                                t.name
                              )}
                              @sl-change=${(e: any) =>
                                this.handleToolToggle(
                                  'preloop-mcp',
                                  t.name,
                                  e.target.checked
                                )}
                              ?disabled=${t.is_supported === false}
                            >
                              ${t.name}
                              <sl-badge variant="neutral" size="small"
                                >${t.source_name || 'external'}</sl-badge
                              >
                            </sl-checkbox>
                          `
                        )}
                      </div>
                    </div>
                  `
                : nothing
            }
          </div>
        </sl-card>

        <sl-card>
          <div slot="header" class="card-header-title">
            <sl-icon name="git"></sl-icon> Git Clone Configuration
          </div>
          <sl-checkbox
            .checked=${this.flow.git_clone_config?.enabled || false}
            @sl-change=${(e: any) =>
              this.handleGitCloneToggle(e.target.checked)}
            style="margin-bottom: var(--sl-spacing-medium);"
          >
            Enable Git Workspace Cloning
          </sl-checkbox>

          ${
            this.flow.git_clone_config?.enabled
              ? html`
                  <div
                    class="form-grid"
                    style="margin-top: var(--sl-spacing-medium);"
                  >
                    <sl-input
                      label="Git Author Name"
                      .value=${
                        this.flow.git_clone_config?.git_user_name || 'Preloop'
                      }
                      @sl-input=${(e: any) => {
                        this.flow.git_clone_config = {
                          ...this.flow.git_clone_config,
                          git_user_name: e.target.value,
                        };
                      }}
                    ></sl-input>

                    <sl-input
                      label="Git Author Email"
                      .value=${
                        this.flow.git_clone_config?.git_user_email ||
                        'git@preloop.ai'
                      }
                      @sl-input=${(e: any) => {
                        this.flow.git_clone_config = {
                          ...this.flow.git_clone_config,
                          git_user_email: e.target.value,
                        };
                      }}
                    ></sl-input>

                    <sl-input
                      label="Source Branch"
                      .value=${
                        this.flow.git_clone_config?.source_branch || 'main'
                      }
                      @sl-input=${(e: any) => {
                        this.flow.git_clone_config = {
                          ...this.flow.git_clone_config,
                          source_branch: e.target.value,
                        };
                      }}
                    ></sl-input>

                    <sl-checkbox
                      .checked=${
                        this.flow.git_clone_config?.create_pull_request || false
                      }
                      @sl-change=${(e: any) => {
                        this.flow.git_clone_config = {
                          ...this.flow.git_clone_config,
                          create_pull_request: e.target.checked,
                        };
                      }}
                      style="align-self: center;"
                    >
                      Create Pull/Merge Request on Commit
                    </sl-checkbox>
                  </div>
                `
              : nothing
          }
        </sl-card>

        <sl-card>
          <div slot="header" class="card-header-title">
            <sl-icon name="shield"></sl-icon> Execution Limits & Safety
            Boundaries
          </div>
          <div class="form-grid">
            <sl-input
              type="number"
              label="Maximum Iteration Count"
              .value=${this.flow.max_iterations || '30'}
              @sl-input=${(e: Event) =>
                this.handleInputChange('max_iterations', e)}
            ></sl-input>

            <sl-input
              type="number"
              label="Max Tokens Budget ($)"
              .value=${this.flow.max_budget || '10'}
              @sl-input=${(e: Event) => this.handleInputChange('max_budget', e)}
            ></sl-input>
          </div>
        </sl-card>

        ${
          this.formError
            ? html`
                <sl-alert variant="danger" open>
                  <sl-icon slot="icon" name="exclamation-octagon"></sl-icon>
                  <strong>Error:</strong> ${this.formError}
                </sl-alert>
              `
            : nothing
        }

        <div
          style="display: flex; gap: var(--sl-spacing-medium); justify-content: flex-end; margin-bottom: var(--sl-spacing-2x-large);"
        >
          <sl-button
            variant="default"
            @click=${this.handleCancel}
            ?disabled=${this.isSaving}
          >
            Cancel
          </sl-button>
          <sl-button type="submit" variant="primary" ?loading=${this.isSaving}>
            ${this.flow.id ? 'Save Flow Changes' : 'Create & Activate Flow'}
          </sl-button>
        </div>
      </form>
    `;
  }
}
