import { LitElement, html, css, unsafeCSS } from 'lit';
import { repeat } from 'lit/directives/repeat.js';
import { customElement, property, state } from 'lit/decorators.js';
import { Router } from '@vaadin/router';
import {
  getFlow,
  createFlow,
  updateFlow,
  getTrackers,
  getAIModels,
  listOrganizations,
  listProjects,
  getAllTools,
  getMCPServers,
  getAccountAgents,
  previewFlowSchedule,
} from '../../api';
import { unifiedWebSocketManager } from '../../services/unified-websocket-manager';
import { parseUTCDate, formatLocalDateTime } from '../../utils/date';
import { executionDurationText } from '../../utils/execution';
import {
  executionSubjectCss,
  renderExecutionSubject,
} from '../../utils/execution-subject';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/textarea/textarea.js';
import '@shoelace-style/shoelace/dist/components/select/select.js';
import '@shoelace-style/shoelace/dist/components/option/option.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/checkbox/checkbox.js';
import '@shoelace-style/shoelace/dist/components/card/card.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/radio-group/radio-group.js';
import '@shoelace-style/shoelace/dist/components/radio/radio.js';
import '../../components/preloop-flow-form';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import '@shoelace-style/shoelace/dist/components/dialog/dialog.js';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '../../components/icon-selector.ts';
import '../../components/resource-actions.ts';
import type { ResourceAction } from '../../components/resource-actions.ts';
import consoleStyles from '../../styles/console-styles.css?inline';
import { getTrackerEventOptions } from '../../constants/tracker-event-types';
import type { Flow } from '../../types';
import { consoleDialogStyles } from '../../styles/console-dialog';

@customElement('flow-view')
export class FlowView extends LitElement {
  private initialized = false;
  private _formInstanceId = 0;
  private _routeSearch = '';

  onBeforeEnter(location: any) {
    const nextFlowId = location.params.flowId as string | undefined;
    const nextSearch = location.search || '';
    const nextIsEditing =
      new URLSearchParams(nextSearch).get('edit') === 'true';
    const changed =
      this.flowId !== nextFlowId ||
      this._routeSearch !== nextSearch ||
      this.isEditing !== nextIsEditing;

    this.flowId = nextFlowId;
    this._routeSearch = nextSearch;
    this.isEditing = nextIsEditing;

    if (this.initialized && changed) {
      void this.loadFlowData(new URLSearchParams(nextSearch));
    }
  }

  static styles = [
    consoleDialogStyles,
    unsafeCSS(consoleStyles),
    unsafeCSS(executionSubjectCss),
    css`
      :host {
        display: block;
        padding: var(--sl-spacing-large);
        max-width: 80rem;
        margin: 0 auto;
      }
      /* The subject column takes the slack: fixed layout plus a zero max
         width makes the cell shrink to its share and ellipsise inside it,
         instead of a long repo name widening the whole table. */
      .executions-table {
        table-layout: fixed;
      }
      .executions-table .subject-cell {
        max-width: 0;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      /* Flow-specific styles */
      .form-grid {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: var(--sl-spacing-large);
      }
      sl-card {
        width: 100%;
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
      .preset-card:hover {
        transform: translateY(-2px);
        box-shadow: var(--sl-shadow-large);
      }

      div[slot='header'] > sl-icon {
        margin-bottom: -2px;
      }
    `,
  ];

  @property()
  flowId?: string;

  @state()
  private flow: Flow = {
    name: '',
    agent_type: 'codex',
    allowed_mcp_servers: [],
    allowed_mcp_tools: [],
    is_enabled: true,
  };

  @state() private longRunningAgents: any[] = [];
  @state() private flowExecutionPath: 'ephemeral' | 'persistent' = 'ephemeral';
  @state() private targetAgentId = '';

  @state()
  private isNew = true;

  @state()
  private isEditing = false;

  @state()
  private flowReady = false;

  @state()
  private trackers: any[] = [];

  @state()
  private models: any[] = [];

  @state()
  private mcpServers: any[] = [];

  @state()
  private availableTools: any[] = [];

  @state()
  private organizations: any[] = [];

  @state()
  private projects: any[] = [];

  @state()
  private recentExecutions: any[] = [];

  @state()
  private isAdmin = false;

  @state()
  private triggerType: 'webhook' | 'tracker' | 'schedule' = 'webhook';

  /** Next run times (ISO strings) for schedule-triggered flows. */
  @state()
  private scheduleNextRuns: string[] = [];

  @state()
  private isPollingOrganizations = false;

  @state()
  private isPollingProjects = false;

  @state()
  private showTestRunModal = false;

  @state()
  private _loadingReferenceData = false;

  @state()
  private formError: string | null = null;

  @state()
  private testRunPlaceholders: Record<string, string> = {};

  private organizationPollingInterval?: number;
  private projectPollingInterval?: number;
  private unsubscribe?: () => void;

  disconnectedCallback() {
    super.disconnectedCallback();
    // Clean up polling intervals
    if (this.organizationPollingInterval) {
      clearInterval(this.organizationPollingInterval);
    }
    if (this.projectPollingInterval) {
      clearInterval(this.projectPollingInterval);
    }
    // Disconnect from WebSocket
    this.unsubscribe?.();
  }

  async connectedCallback() {
    super.connectedCallback();

    try {
      const { getUserProfile } = await import('../../api');
      const currentUser = await getUserProfile();
      this.isAdmin = currentUser.is_superuser || false;
    } catch (error) {
      console.error('Failed to get current user:', error);
      this.isAdmin = false;
    }

    if (!this.initialized) {
      this.initialized = true;
      const urlParams = new URLSearchParams(window.location.search);
      this.isEditing = urlParams.get('edit') === 'true';
      this._routeSearch = window.location.search || '';
      await this.loadFlowData(urlParams);
    }
  }

  private async loadFlowData(urlParams: URLSearchParams) {
    this.flowReady = false;
    this._formInstanceId += 1;

    const presetId = urlParams.get('preset_id');

    if (this.flowId) {
      this.isNew = false;
      this.flow = await getFlow(this.flowId);

      this.triggerType =
        this.flow.trigger_event_source === 'webhook'
          ? 'webhook'
          : this.flow.trigger_event_source === 'schedule'
            ? 'schedule'
            : 'tracker';

      void this.loadScheduleNextRuns();

      const allExecutions = await import('../../api').then((m) =>
        m.getFlowExecutions({ flowId: this.flowId, limit: 10 })
      );
      this.recentExecutions = allExecutions
        .sort(
          (a: any, b: any) =>
            parseUTCDate(b.start_time).getTime() -
            parseUTCDate(a.start_time).getTime()
        )
        .slice(0, 10);

      this._loadingReferenceData = true;
      try {
        const [
          trackers,
          models,
          tools,
          servers,
          allOrganizations,
          allProjects,
        ] = await Promise.all([
          getTrackers(),
          getAIModels(),
          getAllTools(),
          getMCPServers(),
          listOrganizations(),
          listProjects(),
        ]);
        this.trackers = trackers;
        this.models = models;
        this.availableTools = tools;
        this.mcpServers = servers;
        this.organizations = allOrganizations;
        this.projects = allProjects;
      } catch (error) {
        console.error('Failed to load reference data:', error);
      } finally {
        this._loadingReferenceData = false;
      }

      if (this.triggerType === 'tracker' && this.flow.trigger_event_source) {
        const trackerOrgs = (this.organizations || []).filter(
          (org: any) => org.tracker_id === this.flow.trigger_event_source
        );
        if (trackerOrgs.length === 0 && this.flow.trigger_organization_id) {
          this.startPollingOrganizations(this.flow.trigger_event_source);
        }

        if (this.flow.trigger_organization_id) {
          const orgProjects = (this.projects || []).filter(
            (proj: any) =>
              proj.organization_id === this.flow.trigger_organization_id
          );
          if (
            orgProjects.length === 0 &&
            this.flow.trigger_project_ids?.length
          ) {
            this.startPollingProjects(this.flow.trigger_organization_id);
          }
        }
      }

      if (!this.flow.allowed_mcp_servers?.includes('preloop-mcp')) {
        this.flow.allowed_mcp_servers = ['preloop-mcp'];
      }

      this.connectToFlowUpdates();
    } else {
      this.isNew = true;
      this.flow = {
        name: '',
        agent_type: 'codex',
        allowed_mcp_servers: ['preloop-mcp'],
        allowed_mcp_tools: [],
      };
    }

    try {
      const agentsRes = await getAccountAgents({ limit: 100 });
      this.longRunningAgents = agentsRes.items || [];

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
    } catch (e) {
      console.error('Failed to load long-running agents', e);
    } finally {
      this.flowReady = true;
    }

    if (presetId) {
      // preset handling remains in preloop-flow-form loadReferenceData
    }
  }

  private connectToFlowUpdates() {
    // Connect to WebSocket for real-time flow execution updates
    this.unsubscribe = unifiedWebSocketManager.subscribe(
      'flow_executions',
      (message) => {
        // Handle incoming WebSocket messages
        console.log('Received flow update:', message);

        // If this is an execution_started event for our flow, add it to recent executions
        if (
          message.type === 'execution_started' &&
          message.flow_id === this.flowId
        ) {
          // Create a new execution object from the update
          const newExecution = {
            id: message.execution_id,
            flow_id: message.flow_id,
            status: message.payload.status || 'PENDING',
            start_time: message.timestamp,
            flow_name: message.payload.flow_name,
          };

          // Add to the beginning of recent executions
          this.recentExecutions = [
            newExecution,
            ...this.recentExecutions,
          ].slice(0, 10);
        }

        // If this is a status update for an execution we're showing, update it
        if (message.type === 'status_update' && message.execution_id) {
          const executionIndex = this.recentExecutions.findIndex(
            (exec: any) => exec.id === message.execution_id
          );
          if (executionIndex !== -1) {
            // Update the execution status
            const updatedExecution = {
              ...this.recentExecutions[executionIndex],
              status: message.payload.status,
              end_time: message.payload.end_time,
            };
            this.recentExecutions = [
              ...this.recentExecutions.slice(0, executionIndex),
              updatedExecution,
              ...this.recentExecutions.slice(executionIndex + 1),
            ];
          }
        }
      }
    );

    // Track connection state
    unifiedWebSocketManager.onStateChange((state) => {
      console.log(`Flow view WebSocket state: ${state}`);
    });
  }

  private getFlowActions(): ResourceAction[] {
    const actions: ResourceAction[] = [
      {
        id: 'edit-flow',
        label: 'Edit Flow',
        icon: 'pencil',
        href: `/console/flows/${this.flowId}?edit=true`,
      },
      {
        id: 'toggle-enabled',
        label: this.flow.is_enabled ? 'Disable' : 'Enable',
        variant: this.flow.is_enabled ? 'default' : 'success',
        icon: this.flow.is_enabled ? 'pause-circle' : 'play-circle',
        onClick: () => this.toggleFlowEnabled(),
      },
      {
        id: 'test-run',
        label: 'Test Run',
        variant: 'primary',
        icon: 'play-circle',
        disabled: !this.flow.is_enabled,
        onClick: () => this.testRun(),
      },
    ];
    return actions;
  }

  render() {
    if (!this.flowReady) {
      return html`
        <div
          style="display: flex; justify-content: center; padding: var(--sl-spacing-2x-large);"
        >
          <sl-spinner style="font-size: 2rem;"></sl-spinner>
        </div>
      `;
    }

    if (!this.isNew && !this.isEditing) {
      // View mode - show flow details
      return this.renderFlowDetails();
    }

    // Edit/Create mode - show form
    return html`
      <view-header
        headerText="${this.isNew ? 'Create Flow' : 'Edit Flow'}"
        width="wide"
      >
        <div slot="top" style="margin-bottom: var(--sl-spacing-small);">
          <sl-button
            variant="text"
            size="small"
            href="/console/flows"
            style="margin-left: -12px;"
          >
            <sl-icon slot="prefix" name="arrow-left"></sl-icon> Back to Flows
          </sl-button>
        </div>
      </view-header>
      <div class="column-layout wide">
        <div class="main-column">${this.renderForm()}</div>
      </div>
    `;
  }

  renderFlowDetails() {
    return html`
      <!-- Test Run Modal for Trigger Event Placeholders -->
      <sl-dialog
        label="Provide Test Values for Trigger Event"
        .open=${this.showTestRunModal}
        @sl-request-close=${this.cancelTestRun}
      >
        <p style="margin-bottom: 1rem; color: var(--sl-color-neutral-600);">
          Your flow prompt includes template variables that reference trigger
          event data. Please provide test values for these placeholders:
        </p>

        ${Object.keys(this.testRunPlaceholders).map(
          (placeholder) => html`
            <sl-input
              label="${placeholder}"
              placeholder="Enter test value"
              .value=${this.testRunPlaceholders[placeholder]}
              @sl-input=${(e: any) =>
                this.updatePlaceholderValue(placeholder, e.target.value)}
              style="margin-bottom: 1rem;"
            ></sl-input>
          `
        )}

        <div slot="footer" style="display: flex; gap: 8px;">
          <sl-button variant="default" @click=${this.cancelTestRun}>
            Cancel
          </sl-button>
          <sl-button variant="primary" @click=${this.submitTestRun}>
            Run Test
          </sl-button>
        </div>
      </sl-dialog>

      <view-header headerText="${this.flow.name}" width="wide">
        <div slot="top" style="margin-bottom: var(--sl-spacing-small);">
          <sl-button
            variant="text"
            size="small"
            href="/console/flows"
            style="margin-left: -12px;"
          >
            <sl-icon slot="prefix" name="arrow-left"></sl-icon> Back to Flows
          </sl-button>
        </div>
        <div
          slot="main-column"
          style="display: flex; justify-content: flex-end; flex: 1; min-width: 0;"
        >
          <resource-actions
            .actions=${this.getFlowActions()}
          ></resource-actions>
        </div>
      </view-header>
      <div class="column-layout wide">
        <div class="main-column">
          <!-- Flow Info Card -->
          <sl-card>
            <div slot="header">
              <sl-icon name="info-circle"></sl-icon>
              Flow Details
            </div>
            <div
              style="display: grid; grid-template-columns: 150px 1fr; gap: var(--sl-spacing-medium);"
            >
              <strong>Name:</strong>
              <span>${this.flow.name}</span>

              ${
                this.flow.description
                  ? html`
                      <strong>Description:</strong>
                      <span>${this.flow.description}</span>
                    `
                  : ''
              }

              <strong>Agent Type:</strong>
              <sl-badge>${this.flow.agent_type}</sl-badge>

              ${
                this.flow.ai_model_id
                  ? html`
                      <strong>AI Model:</strong>
                      <span>${this.getModelName(this.flow.ai_model_id)}</span>
                    `
                  : ''
              }

              <strong>Trigger:</strong>
              <span>${this.getTriggerSummary()}</span>

              ${
                this.flow.trigger_organization_id
                  ? html`
                      <strong>Organization:</strong>
                      <span
                        >${this.getOrganizationName(
                          this.flow.trigger_organization_id
                        )}</span
                      >
                    `
                  : ''
              }
              ${
                this.flow.trigger_project_ids?.length
                  ? html`
                      <strong>Projects:</strong>
                      <span
                        >${this.flow.trigger_project_ids
                          .map((id) => this.getProjectName(id))
                          .join(', ')}</span
                      >
                    `
                  : ''
              }

              <strong>Status:</strong>
              <sl-badge
                variant="${this.flow.is_enabled ? 'success' : 'neutral'}"
              >
                ${this.flow.is_enabled ? 'Enabled' : 'Disabled'}
              </sl-badge>

              ${
                this.flow.git_clone_config?.enabled
                  ? html`
                      <strong>Git Clone:</strong>
                      <sl-badge variant="primary">Enabled</sl-badge>
                    `
                  : ''
              }
              ${
                this.flow.custom_commands?.enabled && this.isAdmin
                  ? html`
                      <strong>Custom Commands:</strong>
                      <sl-badge variant="warning">Enabled</sl-badge>
                    `
                  : ''
              }
            </div>
          </sl-card>

          ${
            this.flow.prompt_template
              ? html`
                  <sl-card>
                    <div slot="header">
                      <sl-icon name="chat-left-text"></sl-icon>
                      Prompt Template
                    </div>
                    <pre
                      style="white-space: pre-wrap; word-wrap: break-word; font-family: var(--sl-font-mono); font-size: var(--sl-font-size-small); background: var(--sl-color-neutral-50); padding: var(--sl-spacing-medium); border-radius: var(--sl-border-radius-medium); margin: 0; max-height: 300px; overflow-y: auto;"
                    >
${this.flow.prompt_template}</pre>
                  </sl-card>
                `
              : ''
          }
          ${this.renderScheduleCard()}
          ${
            this.flow.trigger_event_source === 'webhook' &&
            this.flow.webhook_config
              ? html`
                  <sl-card>
                    <div slot="header">
                      <sl-icon name="link-45deg"></sl-icon>
                      Webhook URL
                    </div>
                    <div>
                      <p
                        style="margin-bottom: var(--sl-spacing-medium); color: var(--sl-color-neutral-600);"
                      >
                        Use this URL to trigger the flow from external services.
                        Keep it secret!
                      </p>
                      <div
                        style="display: flex; gap: var(--sl-spacing-small); align-items: center;"
                      >
                        <sl-input
                          readonly
                          style="flex: 1;"
                          value="${
                            window.location.origin
                          }/api/v1/webhooks/flows/${this.flowId}/${
                            this.flow.webhook_config.webhook_secret
                          }"
                        ></sl-input>
                        <sl-button @click=${() => this.copyWebhookUrl()}>
                          <sl-icon name="clipboard"></sl-icon>
                          Copy
                        </sl-button>
                      </div>
                    </div>
                  </sl-card>
                `
              : ''
          }
          ${
            this.flow.git_clone_config?.enabled &&
            (this.flow.git_clone_config.repositories?.length || 0) > 0
              ? html`
                  <sl-card>
                    <div slot="header">
                      <sl-icon name="git"></sl-icon>
                      Git Clone Configuration
                    </div>
                    ${(this.flow.git_clone_config.repositories || []).map(
                      (repo, index) => html`
                        <div
                          style="border-bottom: ${
                            index <
                            (this.flow.git_clone_config?.repositories?.length ||
                              0) -
                              1
                              ? '1px solid var(--sl-color-neutral-200)'
                              : 'none'
                          }; padding-bottom: ${
                            index <
                            (this.flow.git_clone_config?.repositories?.length ||
                              0) -
                              1
                              ? '12px'
                              : '0'
                          }; margin-bottom: ${
                            index <
                            (this.flow.git_clone_config?.repositories?.length ||
                              0) -
                              1
                              ? '12px'
                              : '0'
                          };"
                        >
                          <strong style="display: block; margin-bottom: 8px;">
                            Repository ${index + 1}
                          </strong>
                          <div
                            style="display: grid; grid-template-columns: 150px 1fr; gap: var(--sl-spacing-small); padding-left: var(--sl-spacing-medium);"
                          >
                            <strong>Tracker:</strong>
                            <span
                              >${
                                this.trackers.find(
                                  (t) => t.id === repo.tracker_id
                                )?.name || repo.tracker_id
                              }</span
                            >

                            ${
                              repo.repository_url
                                ? html`
                                    <strong>Repository:</strong>
                                    <span>${repo.repository_url}</span>
                                  `
                                : html`
                                    <strong>Repository:</strong>
                                    <span
                                      style="color: var(--sl-color-neutral-600);"
                                      >Auto-detect from trigger</span
                                    >
                                  `
                            }

                            <strong>Clone Path:</strong>
                            <span>${repo.clone_path}</span>

                            ${
                              repo.branch
                                ? html`
                                    <strong>Branch:</strong>
                                    <span>${repo.branch}</span>
                                  `
                                : ''
                            }
                          </div>
                        </div>
                      `
                    )}
                  </sl-card>
                `
              : ''
          }
          ${
            this.flow.custom_commands?.enabled && this.isAdmin
              ? html`
                  <sl-card>
                    <div slot="header">
                      <sl-icon name="terminal"></sl-icon>
                      Custom Commands
                      <sl-badge
                        variant="warning"
                        size="small"
                        style="margin-left: 8px;"
                        >Admin Only</sl-badge
                      >
                    </div>
                    <div>
                      <strong style="display: block; margin-bottom: 8px;"
                        >Commands:</strong
                      >
                      <pre
                        style="background: var(--sl-color-neutral-50); padding: 12px; border-radius: 4px; overflow-x: auto;"
                      >
${(this.flow.custom_commands.commands || []).join('\n')}</pre>
                    </div>
                  </sl-card>
                `
              : ''
          }

          <!-- Recent Executions -->
          <sl-card>
            <div slot="header">
              <sl-icon name="clock-history"></sl-icon>
              Recent Executions
            </div>
            ${
              this.recentExecutions.length === 0
                ? html`<p>No executions yet. Click "Test Run" to start one.</p>`
                : html`
                    <table class="styled-table executions-table">
                      <thead>
                        <tr>
                          <th>Subject</th>
                          <th>Status</th>
                          <th>Started</th>
                          <th>Duration</th>
                          <th>Actions</th>
                        </tr>
                      </thead>
                      <tbody>
                        ${this.recentExecutions.map(
                          (exec) => html`
                            <tr>
                              <!-- Every row on this page is the same flow, so
                                   the subject is the only column that says
                                   which run is which. -->
                              <td class="subject-cell">
                                ${renderExecutionSubject(exec)}
                              </td>
                              <td>
                                <sl-badge
                                  class="chip"
                                  pill
                                  variant=${this.getStatusVariant(exec.status)}
                                >
                                  ${exec.status}
                                </sl-badge>
                              </td>
                              <td>${formatLocalDateTime(exec.start_time)}</td>
                              <td>${executionDurationText(exec) || 'n/a'}</td>
                              <td>
                                <sl-button
                                  size="small"
                                  href="/console/flows/executions/${exec.id}"
                                >
                                  <sl-icon name="eye"></sl-icon>
                                  View
                                </sl-button>
                              </td>
                            </tr>
                          `
                        )}
                      </tbody>
                    </table>
                  `
            }
          </sl-card>
        </div>
      </div>
    `;
  }

  /** One-line trigger summary for the flow details grid. */
  getTriggerSummary(): string {
    if (this.flow.trigger_event_source === 'webhook') {
      return 'Webhook';
    }
    if (this.flow.trigger_event_source === 'schedule') {
      return this.flow.schedule_state?.description || 'Schedule';
    }
    return `${this.getTrackerName(this.flow.trigger_event_source)} - ${
      this.flow.trigger_event_types?.join(', ') || 'No events'
    }`;
  }

  /** Fetch the next few run times for a schedule-triggered flow. */
  private async loadScheduleNextRuns() {
    if (
      this.flow?.trigger_event_source !== 'schedule' ||
      !this.flow.schedule_config
    ) {
      this.scheduleNextRuns = [];
      return;
    }
    try {
      const preview = await previewFlowSchedule(this.flow.schedule_config);
      this.scheduleNextRuns = preview.next_run_times.slice(0, 3);
    } catch (error) {
      console.error('Failed to preview flow schedule:', error);
      this.scheduleNextRuns = [];
    }
  }

  /**
   * Schedule summary card for schedule-triggered flows: human-readable
   * cadence, paused state (paused flow = schedule suspended), the next
   * runs, and the status of the most recent run.
   */
  renderScheduleCard() {
    if (
      this.flow.trigger_event_source !== 'schedule' ||
      !this.flow.schedule_config
    ) {
      return '';
    }
    const schedule = this.flow.schedule_state;
    const paused = !this.flow.is_enabled;
    const lastRun = this.recentExecutions[0];
    return html`
      <sl-card>
        <div slot="header">
          <sl-icon name="clock"></sl-icon>
          Schedule
        </div>
        <div
          style="display: grid; grid-template-columns: 150px 1fr; gap: var(--sl-spacing-medium);"
        >
          <strong>Runs:</strong>
          <span>${schedule?.description || 'Schedule'}</span>

          <strong>Status:</strong>
          <span>
            ${
              paused
                ? html`
                    <sl-badge variant="warning">Paused</sl-badge>
                    <span
                      style="color: var(--sl-color-neutral-600); margin-left: var(--sl-spacing-x-small);"
                    >
                      Schedule suspended — enable the flow to resume runs.
                    </span>
                  `
                : html`<sl-badge variant="success">Active</sl-badge>`
            }
          </span>

          ${
            !paused
              ? html`
                  <strong>Next runs:</strong>
                  <span>
                    ${
                      this.scheduleNextRuns.length > 0
                        ? html`
                            <ol
                              style="margin: 0; padding-left: 1.25rem; display: flex; flex-direction: column; gap: var(--sl-spacing-2x-small);"
                            >
                              ${this.scheduleNextRuns.map(
                                (t) => html`<li>${formatLocalDateTime(t)}</li>`
                              )}
                            </ol>
                          `
                        : 'No upcoming runs'
                    }
                  </span>
                `
              : ''
          }
          ${
            lastRun
              ? html`
                  <strong>Last run:</strong>
                  <span>
                    <sl-badge variant=${this.getStatusVariant(lastRun.status)}>
                      ${lastRun.status}
                    </sl-badge>
                    <span
                      style="color: var(--sl-color-neutral-600); margin-left: var(--sl-spacing-x-small);"
                    >
                      ${formatLocalDateTime(lastRun.start_time)}
                    </span>
                  </span>
                `
              : ''
          }
        </div>
      </sl-card>
    `;
  }

  getStatusVariant(status: string) {
    switch (status) {
      case 'SUCCEEDED':
        return 'success';
      case 'FAILED':
        return 'danger';
      case 'RUNNING':
        return 'primary';
      default:
        return 'neutral';
    }
  }

  private extractTriggerEventPlaceholders(): string[] {
    if (!this.flow.prompt_template) return [];

    // Extract all {{trigger_event.*}} placeholders
    const regex = /\{\{(trigger_event\.[^}]+)\}\}/g;
    const matches = [];
    let match;

    while ((match = regex.exec(this.flow.prompt_template)) !== null) {
      matches.push(match[1]); // Get the placeholder without the {{ }}
    }

    // Return unique placeholders
    return [...new Set(matches)];
  }

  async testRun() {
    if (!this.flowId) return;

    // Check if prompt template has trigger_event placeholders
    const placeholders = this.extractTriggerEventPlaceholders();

    if (placeholders.length > 0) {
      // Initialize placeholder values with empty strings
      this.testRunPlaceholders = {};
      placeholders.forEach((placeholder) => {
        this.testRunPlaceholders[placeholder] = '';
      });
      // Show modal to collect placeholder values
      this.showTestRunModal = true;
    } else {
      // No placeholders, trigger immediately
      await this.executeTestRun();
    }
  }

  async toggleFlowEnabled() {
    if (!this.flowId) return;

    try {
      // Toggle the enabled state
      const newEnabledState = !this.flow.is_enabled;

      // Update the flow on the backend; use the response so computed
      // fields (e.g. schedule_state for scheduled flows) stay fresh.
      const updated = await updateFlow(this.flowId, {
        is_enabled: newEnabledState,
      });

      // Update local state
      this.flow = {
        ...this.flow,
        ...(updated && typeof updated === 'object' ? updated : {}),
        is_enabled: newEnabledState,
      };

      // Show feedback
      const message = newEnabledState
        ? 'Flow enabled successfully'
        : 'Flow disabled successfully';
      console.log(message);
    } catch (error) {
      console.error('Failed to toggle flow enabled state:', error);
      alert('Failed to update flow. Please try again.');
    }
  }

  private async executeTestRun(triggerEventData?: Record<string, any>) {
    if (!this.flowId) return;
    try {
      const execution = await import('../../api').then((m) =>
        m.triggerFlowExecution(this.flowId!, triggerEventData)
      );
      // Navigate to execution view
      window.location.href = `/console/flows/executions/${execution.id}`;
    } catch (error) {
      console.error('Failed to trigger flow execution:', error);
      alert('Failed to trigger flow execution');
    }
  }

  private async submitTestRun() {
    // Build nested object from placeholder keys
    const triggerEventData: Record<string, any> = {};

    Object.entries(this.testRunPlaceholders).forEach(([key, value]) => {
      // key is like "trigger_event.payload.object_attributes.url"
      // Remove "trigger_event." prefix
      const path = key.replace('trigger_event.', '').split('.');

      // Build nested object
      let current = triggerEventData;
      for (let i = 0; i < path.length; i++) {
        const segment = path[i];
        if (i === path.length - 1) {
          // Last segment, set the value
          current[segment] = value;
        } else {
          // Create nested object if it doesn't exist
          if (!current[segment]) {
            current[segment] = {};
          }
          current = current[segment];
        }
      }
    });

    // Close modal
    this.showTestRunModal = false;

    // Execute test run with custom data
    await this.executeTestRun(triggerEventData);
  }

  private cancelTestRun() {
    this.showTestRunModal = false;
    this.testRunPlaceholders = {};
  }

  private updatePlaceholderValue(placeholder: string, value: string) {
    this.testRunPlaceholders = {
      ...this.testRunPlaceholders,
      [placeholder]: value,
    };
  }

  copyWebhookUrl() {
    if (!this.flow.webhook_config) return;
    const webhookUrl = `${window.location.origin}/api/v1/webhooks/flows/${this.flowId}/${this.flow.webhook_config.webhook_secret}`;
    navigator.clipboard.writeText(webhookUrl).then(() => {
      alert('Webhook URL copied to clipboard!');
    });
  }

  /**
   * Get AI models selectable for the selected agent type.
   *
   * The backend adapts gateway-backed models to the protocol each agent speaks
   * (OpenAI-, Anthropic-, or Gemini-compatible), so the form should not hide
   * models based on provider names.
   */
  getSelectableModels() {
    return this.models;
  }

  private getAgentProtocolLabel(agentType: string): string {
    switch (agentType) {
      case 'gemini':
        return 'Gemini-compatible gateway endpoint';
      case 'codex':
      case 'opencode':
      case 'aider':
      case 'openhands':
      default:
        return 'OpenAI-compatible gateway endpoint';
    }
  }

  renderForm() {
    return repeat(
      this.flowReady ? [this._formInstanceId] : [],
      (instanceId) => instanceId,
      () => html`
        <preloop-flow-form
          .flow=${this.flow}
          @flow-submit=${async (e: CustomEvent) => {
            const payload = e.detail.flow;
            try {
              if (this.isNew) {
                if (this.sourcePresetId) {
                  payload.source_preset_id = this.sourcePresetId;
                  payload.prompt_customized = false;
                  payload.tools_customized = false;
                  payload.preset_update_available = false;
                }
                const newFlow = await createFlow(payload);
                Router.go(`/console/flows/${newFlow.id}`);
              } else {
                await updateFlow(this.flowId!, payload);
                Router.go(`/console/flows/${this.flowId}`);
              }
            } catch (error: any) {
              e.target.formError =
                error?.message || 'Failed to save flow. Please try again.';
            }
          }}
          @flow-cancel=${() =>
            Router.go(
              this.isNew ? '/console/flows' : `/console/flows/${this.flowId}`
            )}
        ></preloop-flow-form>
      `
    );
  }

  handleInputChange(field: keyof Flow, e: Event) {
    const target = e.target as HTMLInputElement | HTMLTextAreaElement;
    let value: string | number | null = target.value;
    if (target.type === 'number') {
      value = value === '' ? null : Number(value);
    }
    this.flow = { ...this.flow, [field]: value };
  }

  async handleSubmit(e: Event) {
    e.preventDefault();
    this.formError = null;

    // Build payload with required fields
    const payload: any = {
      name: this.flow.name,
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
      allowed_mcp_servers: this.flow.allowed_mcp_servers || [],
      allowed_mcp_tools: this.flow.allowed_mcp_tools || [],
    };

    // Add optional fields if they have values
    const optionalFields: (keyof Flow)[] = [
      'description',
      'icon',
      'trigger_event_source',
      'trigger_event_types',
      'trigger_organization_id',
      'trigger_project_ids',
      'trigger_config',
      'webhook_config',
      'schedule_config',
      'ai_model_id',
      'agent_type',
      'git_clone_config',
      'custom_commands',
      'max_iterations',
      'max_budget',
      'is_preset',
      'is_enabled',
    ];

    for (const field of optionalFields) {
      const value = this.flow[field];
      if (value !== null && value !== undefined && value !== '') {
        payload[field] = value;
      }
    }

    try {
      if (this.isNew) {
        // If creating from a preset, include template tracking fields
        if (this.sourcePresetId) {
          payload.source_preset_id = this.sourcePresetId;
          // Mark as not customized initially - backend will compute hashes
          payload.prompt_customized = false;
          payload.tools_customized = false;
          payload.preset_update_available = false;
        }
        const newFlow = await createFlow(payload);
        Router.go(`/console/flows/${newFlow.id}`);
      } else {
        await updateFlow(this.flowId!, payload);
        // Redirect to flow view after successful update
        Router.go(`/console/flows/${this.flowId}`);
      }
    } catch (error: any) {
      // Extract error message - API functions now throw with actual error messages
      this.formError =
        error?.message || 'Failed to save flow. Please try again.';
      // Scroll to bottom where the error and submit button are
      window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' });
    }
  }
  startPollingOrganizations(trackerId: string) {
    // Stop any existing polling
    if (this.organizationPollingInterval) {
      clearInterval(this.organizationPollingInterval);
    }

    this.isPollingOrganizations = true;
    this.organizationPollingInterval = window.setInterval(async () => {
      const allOrganizations = await listOrganizations();
      const orgs = allOrganizations.filter(
        (org: any) => org.tracker_id === trackerId
      );

      if (orgs.length > 0) {
        this.organizations = orgs;
        this.isPollingOrganizations = false;
        if (this.organizationPollingInterval) {
          clearInterval(this.organizationPollingInterval);
          this.organizationPollingInterval = undefined;
        }
        this.requestUpdate();
      }
    }, 2000);
  }

  startPollingProjects(orgId: string) {
    // Stop any existing polling
    if (this.projectPollingInterval) {
      clearInterval(this.projectPollingInterval);
    }

    this.isPollingProjects = true;
    this.projectPollingInterval = window.setInterval(async () => {
      const allProjects = await listProjects();
      const orgProjects = allProjects.filter(
        (proj: any) => proj.organization_id === orgId
      );

      if (orgProjects.length > 0) {
        // Store all projects for git clone project selection
        this.projects = allProjects;
        this.isPollingProjects = false;
        if (this.projectPollingInterval) {
          clearInterval(this.projectPollingInterval);
          this.projectPollingInterval = undefined;
        }
        this.requestUpdate();
      }
    }, 2000);
  }

  async handleTrackerChange(e: any) {
    const trackerId = e.target.value;

    // Handle special options
    if (trackerId === 'add_new') {
      // Navigate to trackers page - user can then click "Add New Tracker"
      window.location.href = '/console/trackers';
      return;
    }

    // Normal tracker selected
    this.flow.trigger_event_source = trackerId;
    this.flow.trigger_event_types = undefined; // Reset event types when tracker changes
    this.flow.trigger_organization_id = undefined;
    this.flow.trigger_project_ids = undefined;

    const allOrganizations = await listOrganizations();
    this.organizations = allOrganizations.filter(
      (org: any) => org.tracker_id === trackerId
    );

    // Start polling if no organizations yet
    if (this.organizations.length === 0) {
      this.startPollingOrganizations(trackerId);
    }

    this.requestUpdate();
  }

  async handleOrganizationChange(e: any) {
    const orgId = e.target.value;
    this.flow.trigger_organization_id = orgId;
    this.flow.trigger_project_ids = undefined;

    // Load all projects (needed for git clone project selection)
    const allProjects = await listProjects();
    this.projects = allProjects;

    // Start polling if no projects for this org yet
    const orgProjects = allProjects.filter(
      (proj: any) => proj.organization_id === orgId
    );
    if (orgProjects.length === 0) {
      this.startPollingProjects(orgId);
    }
  }

  @state()
  private customEventType = '';

  @state()
  private filtersExpanded = false;

  getEventOptions() {
    const tracker = this.trackers.find(
      (t) => t.id === this.flow.trigger_event_source
    );
    if (!tracker) return [];
    return getTrackerEventOptions(tracker.tracker_type);
  }

  handleEventChange(e: any) {
    const values = Array.from(e.target.value || []) as string[];
    this.flow.trigger_event_types = values.length > 0 ? values : undefined;
    if (!values.includes('other')) {
      this.customEventType = '';
    }
    this.requestUpdate();
  }

  openFilterModal() {
    // TODO: Implement the filter modal
    alert('Filter modal not yet implemented');
  }

  getDefaultSelectedTools(): { server_name: string; tool_name: string }[] {
    return [];
  }

  renderToolSelection() {
    if (this._loadingReferenceData || this.availableTools.length === 0) {
      return html`
        <div
          style="display: flex; align-items: center; gap: var(--sl-spacing-small); padding: var(--sl-spacing-medium); color: var(--sl-color-neutral-500);"
        >
          <sl-spinner style="font-size: 1rem;"></sl-spinner>
          Loading tools...
        </div>
      `;
    }

    // Group tools by source
    const builtinTools = this.availableTools.filter(
      (tool) => tool.source === 'builtin'
    );
    const supportedBuiltinTools = builtinTools.filter(
      (tool) => tool.is_supported !== false
    );
    const mcpTools = this.availableTools.filter(
      (tool) => tool.source === 'mcp'
    );

    return html`
      <div>
        ${
          builtinTools.length > 0
            ? html`
                <div style="margin-bottom: 1.5rem;">
                  <h4
                    style="margin-bottom: 0.75rem; font-size: 0.875rem; color: var(--sl-color-neutral-600); text-transform: uppercase; font-weight: 600;"
                  >
                    Built-in Tools
                  </h4>
                  <div
                    style="display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 0.75rem;"
                  >
                    ${supportedBuiltinTools.map(
                      (tool) => html`
                        <sl-checkbox
                          .checked=${this.isToolSelected(
                            'preloop-mcp',
                            tool.name
                          )}
                          @sl-change=${(e: any) =>
                            this.handleToolToggle(
                              'preloop-mcp',
                              tool.name,
                              e.target.checked
                            )}
                          ?disabled=${
                            !tool.is_enabled || tool.is_supported === false
                          }
                        >
                          ${tool.name}
                          ${
                            !tool.is_enabled
                              ? html`<sl-badge variant="neutral" size="small"
                                  >Disabled</sl-badge
                                >`
                              : ''
                          }
                        </sl-checkbox>
                      `
                    )}
                  </div>
                </div>
              `
            : ''
        }
        ${
          mcpTools.length > 0
            ? html`
                <div>
                  <h4
                    style="margin-bottom: 0.75rem; font-size: 0.875rem; color: var(--sl-color-neutral-600); text-transform: uppercase; font-weight: 600;"
                  >
                    MCP Server Tools
                  </h4>
                  <div
                    style="display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 0.75rem;"
                  >
                    ${mcpTools.map(
                      (tool) => html`
                        <sl-checkbox
                          .checked=${this.isToolSelected(
                            'preloop-mcp',
                            tool.name
                          )}
                          @sl-change=${(e: any) =>
                            this.handleToolToggle(
                              'preloop-mcp',
                              tool.name,
                              e.target.checked
                            )}
                          ?disabled=${
                            !tool.is_enabled || tool.is_supported === false
                          }
                        >
                          ${tool.name}
                          <sl-badge variant="primary" size="small"
                            >${tool.source_name}</sl-badge
                          >
                          ${
                            tool.is_supported === false
                              ? html`<sl-badge variant="warning" size="small"
                                  >Unsupported</sl-badge
                                >`
                              : ''
                          }
                          ${
                            !tool.is_enabled
                              ? html`<sl-badge variant="neutral" size="small"
                                  >Disabled</sl-badge
                                >`
                              : ''
                          }
                        </sl-checkbox>
                      `
                    )}
                  </div>
                </div>
              `
            : ''
        }
      </div>
    `;
  }

  isToolSelected(serverName: string, toolName: string): boolean {
    if (!this.flow.allowed_mcp_tools) return false;
    return this.flow.allowed_mcp_tools.some(
      (tool) => tool.server_name === serverName && tool.tool_name === toolName
    );
  }

  handleToolToggle(serverName: string, toolName: string, checked: boolean) {
    if (!this.flow.allowed_mcp_tools) {
      this.flow.allowed_mcp_tools = [];
    }

    if (checked) {
      // Add tool
      this.flow.allowed_mcp_tools.push({
        server_name: serverName,
        tool_name: toolName,
      });
    } else {
      // Remove tool
      this.flow.allowed_mcp_tools = this.flow.allowed_mcp_tools.filter(
        (tool) =>
          !(tool.server_name === serverName && tool.tool_name === toolName)
      );
    }

    this.requestUpdate();
  }

  getGitTrackers() {
    // Return only GitHub and GitLab trackers
    return this.trackers.filter(
      (t) => t.tracker_type === 'github' || t.tracker_type === 'gitlab'
    );
  }

  getModelName(modelId: string): string {
    const model = this.models.find((m: any) => m.id === modelId);
    return model?.name || modelId;
  }

  getTrackerName(trackerId: string | undefined): string {
    if (!trackerId) return 'Unknown';
    const tracker = this.trackers.find((t: any) => t.id === trackerId);
    return tracker?.name || trackerId;
  }

  getOrganizationName(orgId: string | undefined): string {
    if (!orgId) return 'Unknown';
    const org = this.organizations.find((o: any) => o.id === orgId);
    return org?.name || orgId;
  }

  getProjectName(projectId: string | undefined): string {
    if (!projectId) return 'Unknown';
    const project = this.projects.find((p: any) => p.id === projectId);
    return project?.name || projectId;
  }

  handleGitCloneToggle(enabled: boolean) {
    if (enabled) {
      const gitTrackers = this.getGitTrackers();

      // Initialize git clone config with defaults
      this.flow.git_clone_config = {
        enabled: true,
        repositories: [],
        git_user_name: 'Preloop',
        git_user_email: 'git@preloop.ai',
        source_branch: 'main',
        target_branch: '',
        create_pull_request: false,
        pull_request_title: '',
        pull_request_description: '',
      };

      // Auto-add repository based on available trackers
      if (gitTrackers.length === 1) {
        // Single tracker - auto-select it
        this.addGitRepositoryWithTracker(gitTrackers[0].id);
      } else if (this.flow.trigger_event_source) {
        // Multiple trackers but trigger is set - use trigger tracker
        const triggerTracker = gitTrackers.find(
          (t) => t.id === this.flow.trigger_event_source
        );
        if (triggerTracker) {
          this.addGitRepositoryWithTracker(triggerTracker.id);
        }
      }
    } else {
      this.flow.git_clone_config = { enabled: false, repositories: [] };
    }
    this.requestUpdate();
  }

  addGitRepository() {
    if (!this.flow.git_clone_config) {
      this.flow.git_clone_config = { enabled: true, repositories: [] };
    }

    const gitTrackers = this.getGitTrackers();
    const defaultTracker = gitTrackers[0]?.id || '';

    this.flow.git_clone_config.repositories =
      this.flow.git_clone_config.repositories || [];
    const repoCount = this.flow.git_clone_config.repositories.length;

    this.flow.git_clone_config.repositories.push({
      tracker_id: defaultTracker,
      clone_path:
        repoCount === 0 ? '/workspace' : `/workspace-${repoCount + 1}`,
    });
    this.requestUpdate();
  }

  addGitRepositoryWithTracker(trackerId: string) {
    if (!this.flow.git_clone_config) {
      this.flow.git_clone_config = { enabled: true, repositories: [] };
    }

    this.flow.git_clone_config.repositories =
      this.flow.git_clone_config.repositories || [];
    const repoCount = this.flow.git_clone_config.repositories.length;

    this.flow.git_clone_config.repositories.push({
      tracker_id: trackerId,
      clone_path:
        repoCount === 0 ? '/workspace' : `/workspace-${repoCount + 1}`,
    });
    this.requestUpdate();
  }

  removeGitRepository(index: number) {
    if (this.flow.git_clone_config?.repositories) {
      this.flow.git_clone_config.repositories.splice(index, 1);
      this.requestUpdate();
    }
  }

  renderGitRepositories() {
    const repositories = this.flow.git_clone_config?.repositories || [];
    const gitTrackers = this.getGitTrackers();

    if (repositories.length === 0) {
      return html`
        <p style="margin-top: 0.5rem; color: var(--sl-color-neutral-600);">
          No repositories configured. Click "Add Repository" to get started.
        </p>
      `;
    }

    return html`
      ${repositories.map(
        (repo, index) => html`
          <div
            style="border: 1px solid var(--sl-color-neutral-200); border-radius: 4px; padding: 1rem; margin-top: 0.5rem;"
          >
            <div
              style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.5rem;"
            >
              <strong>Repository ${index + 1}</strong>
              <sl-button
                size="small"
                variant="danger"
                @click=${() => this.removeGitRepository(index)}
              >
                <sl-icon name="trash"></sl-icon>
              </sl-button>
            </div>

            ${
              gitTrackers.length > 1
                ? html`
                    <sl-select
                      label="Tracker"
                      .value=${repo.tracker_id}
                      @sl-change=${(e: any) => {
                        repo.tracker_id = e.target.value;
                        this.requestUpdate();
                      }}
                    >
                      ${gitTrackers.map(
                        (tracker) =>
                          html`<sl-option value=${tracker.id}
                            >${tracker.name}
                            (${tracker.tracker_type})</sl-option
                          >`
                      )}
                    </sl-select>
                  `
                : html`
                    <p style="margin-bottom: 0.5rem;">
                      <strong>Tracker:</strong> ${gitTrackers[0]?.name}
                    </p>
                  `
            }

            <sl-input
              label="Repository URL (optional)"
              placeholder="Leave empty to use trigger project"
              .value=${repo.repository_url || ''}
              @sl-input=${(e: any) => {
                repo.repository_url = e.target.value;
              }}
              help-text="Manually specify repository URL or leave empty to use the project selected in trigger"
            ></sl-input>

            <sl-input
              label="Clone Path"
              .value=${repo.clone_path}
              @sl-input=${(e: any) => {
                repo.clone_path = e.target.value;
              }}
              help-text="Absolute path (starts with /) or relative to /workspace/"
            ></sl-input>

            <sl-input
              label="Branch (optional)"
              placeholder="Leave empty for default branch"
              .value=${repo.branch || ''}
              @sl-input=${(e: any) => {
                repo.branch = e.target.value;
              }}
            ></sl-input>
          </div>
        `
      )}
    `;
  }

  handleTriggerTypeChange(newType: 'webhook' | 'tracker') {
    this.triggerType = newType;

    if (newType === 'webhook') {
      // Set webhook trigger
      this.flow.trigger_event_source = 'webhook';
      this.flow.trigger_event_types = ['webhook'];
      // Clear tracker-specific fields
      this.flow.trigger_organization_id = undefined;
      this.flow.trigger_project_ids = undefined;
    } else {
      // Clear webhook fields
      this.flow.trigger_event_source = undefined;
      this.flow.trigger_event_types = undefined;
    }

    this.requestUpdate();
  }

  renderWebhookTriggerFields() {
    // If editing and webhook config exists, show the URL
    if (!this.isNew && this.flow.webhook_config) {
      return html`
        <div>
          <p
            style="margin-bottom: var(--sl-spacing-medium); color: var(--sl-color-neutral-600);"
          >
            This flow will be triggered when a POST request is sent to the
            webhook URL below.
          </p>
          <div>
            <label
              style="display: block; margin-bottom: var(--sl-spacing-2x-small); font-weight: 600;"
            >
              Webhook URL
            </label>
            <div
              style="display: flex; gap: var(--sl-spacing-small); align-items: center;"
            >
              <sl-input
                readonly
                style="flex: 1;"
                value="${window.location.origin}/api/v1/webhooks/flows/${
                  this.flowId
                }/${this.flow.webhook_config.webhook_secret}"
              ></sl-input>
              <sl-button @click=${() => this.copyWebhookUrl()}>
                <sl-icon name="clipboard"></sl-icon>
                Copy
              </sl-button>
            </div>
          </div>
          <div>
            <label
              style="display: block; margin-bottom: 0.5rem; font-weight: 500;"
            >
              Example Payload
            </label>
            <sl-textarea
              readonly
              rows="6"
              value='{
  "data": "your custom data",
  "event": "custom_event",
  "any_key": "any_value"
}'
              style="font-family: monospace;"
            ></sl-textarea>
            <p
              style="margin-top: 0.5rem; color: var(--sl-color-neutral-600); font-size: 0.875rem;"
            >
              The payload will be available in your prompt template via
              <code>{{trigger_event.payload.*}}</code>
            </p>
          </div>
        </div>
      `;
    }

    // For new flows, show info message
    return html`
      <div>
        <p style="color: var(--sl-color-neutral-600); margin: 0;">
          <sl-icon name="info-circle"></sl-icon>
          The webhook URL will be generated after you create the flow. You can
          then use it to trigger this flow from external services.
        </p>
      </div>
    `;
  }

  renderTrackerTriggerFields() {
    // If no trackers, show add tracker button
    if (this.trackers.length === 0) {
      return html`
        <div style="text-align: center; padding: var(--sl-spacing-2x-large);">
          <p
            style="margin-bottom: var(--sl-spacing-medium); color: var(--sl-color-neutral-600);"
          >
            You don't have any trackers configured yet.
          </p>
          <sl-button variant="primary" @click=${this.openAddTrackerDialog}>
            <sl-icon slot="prefix" name="plus-lg"></sl-icon>
            Add New Tracker
          </sl-button>
        </div>
      `;
    }

    return html`
      <div class="form-grid">
        <sl-select
          label="Tracker"
          .value=${this.flow.trigger_event_source || ''}
          @sl-change=${this.handleTrackerChange}
        >
          ${this.trackers.map(
            (tracker) =>
              html`<sl-option value=${tracker.id}>${tracker.name}</sl-option>`
          )}
        </sl-select>
        <sl-select
          label="Organization"
          .value=${this.flow.trigger_organization_id || ''}
          @sl-change=${this.handleOrganizationChange}
          ?disabled=${
            this.isPollingOrganizations || !this.flow.trigger_event_source
          }
        >
          ${
            this.isPollingOrganizations
              ? html`<sl-option value="">
                  <sl-spinner style="font-size: 1rem;"></sl-spinner>
                  Loading organizations...
                </sl-option>`
              : this.organizations.length === 0 &&
                  this.flow.trigger_organization_id
                ? html`<sl-option value=${this.flow.trigger_organization_id}>
                    ${this.flow.trigger_organization_id} (syncing...)
                  </sl-option>`
                : this.organizations.map(
                    (org) =>
                      html`<sl-option value=${org.id}>${org.name}</sl-option>`
                  )
          }
        </sl-select>
        <sl-select
          label="Projects"
          multiple
          clearable
          .value=${this.flow.trigger_project_ids || []}
          @sl-change=${(e: any) => {
            const values = Array.from(e.target.value || []) as string[];
            this.flow.trigger_project_ids =
              values.length > 0 ? values : undefined;
            this.requestUpdate();
          }}
          ?disabled=${
            this.isPollingProjects || !this.flow.trigger_organization_id
          }
          help-text="Select one or more projects, or leave empty for all projects in the organization"
        >
          ${
            this.isPollingProjects
              ? html`<sl-option value="">
                  <sl-spinner style="font-size: 1rem;"></sl-spinner>
                  Loading projects...
                </sl-option>`
              : (() => {
                  // Filter projects by selected organization for trigger
                  const orgProjects = this.projects.filter(
                    (proj: any) =>
                      proj.organization_id === this.flow.trigger_organization_id
                  );
                  return orgProjects.map(
                    (proj: any) =>
                      html`<sl-option value=${proj.id}>${proj.name}</sl-option>`
                  );
                })()
          }
        </sl-select>
        <sl-select
          label="Events"
          multiple
          .value=${this.flow.trigger_event_types || []}
          @sl-change=${(e: any) => {
            const values = Array.from(e.target.value || []) as string[];
            this.flow.trigger_event_types =
              values.length > 0 ? values : undefined;
            this.requestUpdate();
          }}
          help-text="Select one or more events to trigger this flow"
        >
          ${this.getEventOptions().map(
            (event) =>
              html`<sl-option value=${event.value}>${event.name}</sl-option>`
          )}
          <sl-option value="other">Other</sl-option>
        </sl-select>
        ${
          this.flow.trigger_event_types?.includes('other')
            ? html`
                <sl-input
                  label="Custom Event"
                  .value=${this.customEventType}
                  @sl-input=${(e: any) => (this.customEventType = e.target.value)}
                ></sl-input>
              `
            : ''
        }
      </div>

      ${this.flow.trigger_event_source ? this.renderEventFilters() : ''}
    `;
  }

  renderEventFilters() {
    if (!this.flow.trigger_config) {
      this.flow.trigger_config = {};
    }

    const tracker = this.trackers.find(
      (t) => t.id === this.flow.trigger_event_source
    );
    if (!tracker) return '';

    // Check if any filters are defined
    const hasFilters =
      this.flow.trigger_config &&
      Object.keys(this.flow.trigger_config).length > 0;

    // Show filters if expanded or if any filter is already defined
    const showFilters = this.filtersExpanded || hasFilters;

    // Determine if this is a PR/MR event
    const eventTypes = this.flow.trigger_event_types || [];
    const isMREvent = eventTypes.some(
      (et) => et?.includes('merge_request') || et?.includes('pull_request')
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
                      : ''
                  }

                  <!-- Labels filter -->
                  <sl-input
                    label="Labels (comma-separated)"
                    placeholder="e.g., bug, critical, backend"
                    .value=${
                      (
                        this.flow.trigger_config?.labels as string[] | undefined
                      )?.join(', ') || ''
                    }
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
                      : ''
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
                      : ''
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
                                : ''
                          }
                        `
                      : ''
                  }
                </div>

                <sl-alert variant="primary" open style="margin-top: 1rem;">
                  <sl-icon slot="icon" name="info-circle"></sl-icon>
                  <strong>How filters work:</strong> Leave empty to match all
                  events. When multiple filters are set, ALL conditions must
                  match for the flow to trigger.
                </sl-alert>
              `
            : ''
        }
      </div>
    `;
  }
}
