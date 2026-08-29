import { LitElement, html, css, unsafeCSS } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import '@shoelace-style/shoelace/dist/components/divider/divider.js';
import '@shoelace-style/shoelace/dist/components/dialog/dialog.js';
import { router } from '../../router';
import { Router } from '@vaadin/router';
import { unifiedWebSocketManager } from '../../services/unified-websocket-manager';
import {
  getFlows,
  getFlowPresets,
  deleteFlow,
  getFlowExecutions,
  triggerFlowExecution,
} from '../../api';
import { formatLocalDateTime } from '../../utils/date';
import { executionDurationText } from '../../utils/execution';
import consoleStyles from '../../styles/console-styles.css?inline';

interface ScheduleState {
  active: boolean;
  type: string;
  description: string;
  timezone: string;
  next_run_at: string | null;
  cron?: string;
}

interface Flow {
  id: string;
  name: string;
  description?: string;
  icon?: string;
  account_id?: string;
  trigger_event_source?: string;
  is_enabled?: boolean;
  schedule_state?: ScheduleState | null;
  execution_stats?: {
    total_execs?: number;
    running_execs?: number;
    last_seen_at?: string | null;
    estimated_cost?: number;
  };
}

interface FlowExecution {
  id: string;
  flow_id: string;
  status: string;
  start_time: string;
  end_time?: string;
}

@customElement('flows-view')
export class FlowsView extends LitElement {
  static styles = [
    unsafeCSS(consoleStyles),
    css`
      :host {
        display: block;
      }
      .flows-grid {
        display: grid;
        grid-template-columns: repeat(3, 1fr);
        gap: 28px;
        margin-bottom: 32px;
      }

      @media (max-width: 1400px) {
        .flows-grid {
          grid-template-columns: repeat(2, 1fr);
        }
      }

      @media (max-width: 900px) {
        .flows-grid {
          grid-template-columns: 1fr;
        }
      }

      .presets-grid {
        display: grid;
        grid-template-columns: repeat(3, 1fr);
        gap: 28px;
      }

      @media (max-width: 1400px) {
        .presets-grid {
          grid-template-columns: repeat(2, 1fr);
        }
      }

      @media (max-width: 900px) {
        .presets-grid {
          grid-template-columns: 1fr;
        }
      }

      .flows-grid > sl-card,
      .presets-grid > sl-card {
        width: 100%;
        min-width: 0;
        box-sizing: border-box;
      }
      .flow-card {
        cursor: pointer;
        transition:
          transform 0.2s,
          box-shadow 0.2s;
        height: 100%;
      }
      .flow-card::part(base) {
        height: 100%;
        display: flex;
        flex-direction: column;
      }
      .flow-card::part(body) {
        flex: 1;
        display: flex;
        flex-direction: column;
      }
      .flow-card:hover {
        transform: translateY(-4px);
        box-shadow: 0 8px 16px rgba(0, 0, 0, 0.1);
      }
      .flow-header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        margin-bottom: 12px;
      }
      .flow-title {
        display: flex;
        align-items: center;
        gap: 8px;
        font-size: var(--sl-font-size-large);
        font-weight: 600;
        min-width: 0;
      }
      .flow-description {
        color: var(--sl-color-neutral-600);
        margin-bottom: 12px;
        font-size: var(--sl-font-size-small);
        height: 5.75rem;
        overflow: hidden;
      }
      .flow-description-text {
        display: -webkit-box;
        -webkit-line-clamp: 2;
        -webkit-box-orient: vertical;
        overflow: hidden;
        line-height: 1.35;
      }
      .flow-description-placeholder {
        color: var(--sl-color-neutral-400);
        font-style: italic;
      }
      .flow-description-action {
        margin-top: var(--sl-spacing-2x-small);
      }
      .flow-stats {
        display: flex;
        gap: 16px;
        margin-top: 12px;
        padding-top: 12px;
        border-top: 1px solid var(--sl-color-neutral-200);
      }
      .stat-item {
        display: flex;
        align-items: center;
        gap: 4px;
        font-size: var(--sl-font-size-small);
        color: var(--sl-color-neutral-600);
      }
      .flow-footer {
        display: flex;
        gap: 8px;
        justify-content: space-between;
        align-items: center;
      }
      .flow-footer-actions {
        display: flex;
        gap: 8px;
      }
      .active-executions {
        margin-bottom: 32px;
      }
      .executions-list {
        display: flex;
        flex-direction: column;
        gap: 8px;
        margin-top: 12px;
      }
      .execution-item {
        display: flex;
        justify-content: space-between;
        align-items: center;
        padding: 12px;
        background: var(--sl-color-neutral-50);
        border-radius: 4px;
        transition: background 0.2s;
      }
      .execution-item:hover {
        background: var(--sl-color-neutral-100);
      }
      .execution-info {
        display: flex;
        align-items: center;
        gap: 12px;
        flex: 1;
      }
      .section-header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        margin: 24px 0 16px 0;
      }
      .presets-collapsed {
        text-align: center;
        padding: 24px 16px;
        color: var(--sl-color-neutral-500);
        font-size: var(--sl-font-size-medium);
        background: var(--sl-color-neutral-50);
        border-radius: 4px;
      }
      .empty-state-wrapper {
        display: flex;
        justify-content: center;
        width: 100%;
        margin-top: var(--sl-spacing-large);
        margin-bottom: var(--sl-spacing-large);
      }
      .empty-card {
        width: 100%;
        max-width: 580px;
        border: 1px solid rgba(139, 92, 246, 0.35);
        box-shadow: 0 12px 40px rgba(139, 92, 246, 0.12);
        border-radius: var(--sl-border-radius-large);
      }
      .empty-card-body {
        display: flex;
        flex-direction: column;
        align-items: center;
        text-align: center;
        padding: var(--sl-spacing-large);
      }
      .empty-icon-circle {
        width: 72px;
        height: 72px;
        border-radius: 50%;
        background: radial-gradient(
          circle,
          rgba(139, 92, 246, 0.2) 0%,
          rgba(99, 102, 241, 0.05) 70%
        );
        display: flex;
        align-items: center;
        justify-content: center;
        margin-bottom: var(--sl-spacing-medium);
      }
      .empty-icon-circle sl-icon {
        font-size: 2.5rem;
        color: #8b5cf6;
      }
      .empty-card-title {
        margin: 0 0 var(--sl-spacing-2x-small);
        font-size: 1.25rem;
        font-weight: 700;
        color: var(--sl-color-neutral-900);
      }
      .empty-card-desc {
        margin: 0 0 var(--sl-spacing-large);
        max-width: 440px;
        font-size: 0.95rem;
        line-height: 1.55;
        color: var(--sl-color-neutral-600);
      }
      .empty-cta-btn {
        width: 100%;
        max-width: 280px;
        --sl-color-primary-600: #6366f1;
        --sl-color-primary-700: #4f46e5;
      }
    `,
  ];

  @state()
  private flows: Flow[] = [];

  @state()
  private presets: Flow[] = [];

  @state()
  private presetsLoading = false;

  private presetsLoaded = false;

  @state()
  private executions: FlowExecution[] = [];

  @state()
  private isAlertVisible = false;

  @state()
  private isLoading = true;

  @state()
  private triggeringFlowId: string | null = null;

  @state()
  private deletingFlowId: string | null = null;

  @state()
  private showPresets = true;

  @state()
  private expandedDescription: {
    title: string;
    description: string;
  } | null = null;

  private unsubscribe?: () => void;
  private hasInitializedPresetVisibility = false;

  async connectedCallback() {
    super.connectedCallback();
    await this.loadData();
    this.isAlertVisible = !localStorage.getItem('flows-alert-dismissed');

    // Connect to WebSocket for real-time updates instead of polling
    this.connectWebSocket();
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    // Clean up WebSocket connection
    this.unsubscribe?.();
  }

  async loadData() {
    this.isLoading = true;
    try {
      // Defer /flows/presets (~38KB) until the presets UI is shown.
      const [flows, executions] = await Promise.all([
        getFlows(),
        getFlowExecutions({
          limit: 20,
          status: ['PENDING', 'INITIALIZING', 'STARTING', 'RUNNING'],
        }),
      ]);
      this.flows = flows;
      this.executions = executions;

      if (this.flows.length === 0) {
        this.showPresets = true;
      } else if (!this.hasInitializedPresetVisibility) {
        this.showPresets = false;
      }
      this.hasInitializedPresetVisibility = true;

      if (this.showPresets) {
        void this.ensurePresetsLoaded();
      }
    } finally {
      this.isLoading = false;
    }
  }

  private async ensurePresetsLoaded(): Promise<void> {
    if (this.presetsLoaded || this.presetsLoading) {
      return;
    }
    this.presetsLoading = true;
    try {
      const presets = await getFlowPresets();
      this.presets = this.sortPresets(presets);
      this.presetsLoaded = true;
    } catch (error) {
      console.error('Failed to load flow presets:', error);
    } finally {
      this.presetsLoading = false;
    }
  }

  async refreshExecutions() {
    this.executions = await getFlowExecutions({
      limit: 20,
      status: ['PENDING', 'INITIALIZING', 'STARTING', 'RUNNING'],
    });
  }

  private connectWebSocket() {
    this.unsubscribe = unifiedWebSocketManager.subscribe(
      'flow_executions',
      (message: any) => this.handleWebSocketMessage(message)
    );

    // Track connection state
    unifiedWebSocketManager.onStateChange((state) => {
      console.log(`Flows view WebSocket state: ${state}`);
    });
  }

  private handleWebSocketMessage(message: any) {
    console.log('Flow updates message:', message);

    // Handle status updates
    if (message.type === 'status_update' && message.execution_id) {
      const executionIndex = this.executions.findIndex(
        (exec) => exec.id === message.execution_id
      );

      if (executionIndex >= 0) {
        // Update existing execution
        const updated = [...this.executions];
        updated[executionIndex] = {
          ...updated[executionIndex],
          status: message.payload.status,
          ...(message.payload.end_time && {
            end_time: message.payload.end_time,
          }),
        };
        this.executions = updated;
      } else {
        // New execution started, reload the list
        this.refreshExecutions();
      }
    }

    // Handle new executions
    if (message.type === 'execution_started' && message.payload) {
      this.refreshExecutions();
    }
  }

  render() {
    if (this.isLoading) {
      return html`
        <view-header
          headerText="Flows"
          description="Event-driven agent runs. A flow starts an agent when something happens — a new issue, a webhook — and stops it when the run completes."
          width="extra-wide"
        ></view-header>
        <div style="display: flex; justify-content: center; padding: 48px;">
          <sl-spinner style="font-size: 3rem;"></sl-spinner>
        </div>
      `;
    }

    const activeExecutions = this.executions.filter(
      (e) => e.status === 'RUNNING' || e.status === 'STARTING'
    );

    return html`
      <view-header
        headerText="Flows"
        description="Event-driven agent runs. A flow starts an agent when something happens — a new issue, a webhook — and stops it when the run completes."
        width="extra-wide"
      >
        ${
          this.flows.length > 0
            ? html`
                <div slot="main-column">
                  <sl-button
                    variant="primary"
                    @click=${() => Router.go('/console/flows/new')}
                  >
                    <sl-icon slot="prefix" name="plus-lg"></sl-icon>
                    Create New Flow
                  </sl-button>
                </div>
              `
            : ''
        }
      </view-header>
      <div class="column-layout extra-wide">
        <div class="main-column">
          ${
            activeExecutions.length > 0
              ? html`
                  <div class="active-executions">
                    <div class="section-header">
                      <h2>
                        <sl-icon name="lightning-fill"></sl-icon>
                        Active Executions
                      </h2>
                      <sl-button
                        size="small"
                        href=${router.urlForPath('/console/flows/executions')}
                      >
                        View All
                      </sl-button>
                    </div>
                    <div class="executions-list">
                      ${activeExecutions
                        .slice(0, 5)
                        .map((exec) => this.renderExecutionItem(exec))}
                    </div>
                  </div>
                `
              : ''
          }
          ${
            this.flows.length > 0
              ? html`
                  <div class="flows-grid">
                    ${this.flows.map((flow) => this.renderFlowCard(flow))}
                  </div>
                `
              : html`
                  <div class="empty-state empty-state-wrapper">
                    <sl-card class="empty-card">
                      <div class="empty-card-body">
                        <div class="empty-icon-circle">
                          <sl-icon name="diagram-3"></sl-icon>
                        </div>
                        <h3 class="empty-card-title">No flows yet</h3>
                        <p class="empty-card-desc">
                          No flows yet. Create your first custom flow or clone a
                          starter preset below.
                        </p>
                        <sl-button
                          class="empty-cta-btn"
                          variant="primary"
                          @click=${() => Router.go('/console/flows/new')}
                        >
                          <sl-icon slot="prefix" name="plus-lg"></sl-icon>
                          Create New Flow
                        </sl-button>
                      </div>
                    </sl-card>
                  </div>
                `
          }

          <sl-divider></sl-divider>

          <div class="section-header">
            <h2>Presets</h2>
            ${
              this.flows.length > 0
                ? html`
                    <sl-button size="small" @click=${this.togglePresets}>
                      <sl-icon
                        slot="prefix"
                        name=${this.showPresets ? 'chevron-up' : 'chevron-down'}
                      ></sl-icon>
                      ${this.showPresets ? 'Hide presets' : 'Show presets'}
                    </sl-button>
                  `
                : ''
            }
          </div>
          ${
            this.showPresets
              ? this.presetsLoading && this.presets.length === 0
                ? html`<div class="presets-collapsed">Loading presets...</div>`
                : html`
                    <div class="presets-grid">
                      ${this.presets.map((preset) => this.renderPresetCard(preset))}
                    </div>
                  `
              : html`<div class="presets-collapsed">
                  Presets are hidden. Use "Show presets" to explore starter
                  workflows.
                </div>`
          }
        </div>
      </div>
      <sl-dialog
        label=${this.expandedDescription?.title || 'Flow description'}
        ?open=${Boolean(this.expandedDescription)}
        @sl-after-hide=${() => {
          this.expandedDescription = null;
        }}
      >
        <div style="white-space: pre-wrap; color: var(--sl-color-neutral-700);">
          ${this.expandedDescription?.description || ''}
        </div>
        <sl-button
          slot="footer"
          variant="primary"
          @click=${() => {
            this.expandedDescription = null;
          }}
        >
          Close
        </sl-button>
      </sl-dialog>
    `;
  }

  renderFlowCard(flow: Flow) {
    const activeCount = flow.execution_stats?.running_execs || 0;
    const totalCount = flow.execution_stats?.total_execs || 0;

    return html`
      <sl-card
        class="flow-card"
        @click=${() => Router.go(`/console/flows/${flow.id}`)}
      >
        <div slot="header" class="flow-header">
          <div class="flow-title">${flow.name}</div>
          ${
            activeCount > 0
              ? html`<sl-badge variant="primary" pulse
                  >${activeCount} active</sl-badge
                >`
              : ''
          }
        </div>

        ${this.renderFlowDescription(flow)}

        <div class="flow-stats">
          <div class="stat-item">
            <sl-icon name="play-circle"></sl-icon>
            <span>${totalCount} executions</span>
          </div>
          ${this.renderScheduleStat(flow)}
        </div>

        <div slot="footer" class="flow-footer">
          <div class="flow-footer-actions">
            <sl-button
              size="small"
              href=${`/console/flows/${flow.id}?edit=true`}
              @click=${(e: Event) => e.stopPropagation()}
            >
              <sl-icon slot="prefix" name="pencil"></sl-icon>
              Edit
            </sl-button>
            <sl-button
              size="small"
              variant="danger"
              @click=${(e: Event) => {
                e.stopPropagation();
                this.deleteFlowHandler(flow.id, flow.name);
              }}
              ?loading=${this.deletingFlowId === flow.id}
            >
              <sl-icon slot="prefix" name="trash"></sl-icon>
              Delete
            </sl-button>
          </div>
          <sl-button
            size="small"
            variant="primary"
            @click=${(e: Event) => {
              e.stopPropagation();
              this.triggerTestRun(flow.id);
            }}
            ?loading=${this.triggeringFlowId === flow.id}
          >
            <sl-icon slot="prefix" name="play-fill"></sl-icon>
            Test Run
          </sl-button>
        </div>
      </sl-card>
    `;
  }

  renderPresetCard(preset: Flow) {
    return html`
      <sl-card class="flow-card">
        <div slot="header" class="flow-header">
          <div class="flow-title">
            <sl-icon name=${preset.icon || 'gear'}></sl-icon>
            ${preset.name}
          </div>
          <div class="flow-footer-actions">
            <sl-button size="small" @click=${() => this.clonePreset(preset.id)}>
              Use Template
            </sl-button>
            ${
              preset.account_id
                ? html`
                    <sl-button
                      size="small"
                      variant="danger"
                      @click=${() => this.removePreset(preset.id)}
                    >
                      Remove
                    </sl-button>
                  `
                : ''
            }
          </div>
        </div>
        ${this.renderFlowDescription(preset)}
      </sl-card>
    `;
  }

  renderExecutionItem(exec: FlowExecution) {
    const flow = this.flows.find((f) => f.id === exec.flow_id);
    const duration = executionDurationText(exec);
    return html`
      <div
        class="execution-item"
        @click=${() => Router.go(`/console/flows/executions/${exec.id}`)}
        style="cursor: pointer;"
      >
        <div class="execution-info">
          <sl-badge variant=${this.getStatusVariant(exec.status)}>
            ${exec.status}
          </sl-badge>
          <div>
            <strong>${flow?.name || 'Unknown Flow'}</strong>
            <div
              style="font-size: var(--sl-font-size-small); color: var(--sl-color-neutral-600);"
            >
              Started
              ${formatLocalDateTime(exec.start_time)}${
                duration ? ` · ${duration}` : ''
              }
            </div>
          </div>
        </div>
        <sl-button size="small">
          <sl-icon name="arrow-right"></sl-icon>
        </sl-button>
      </div>
    `;
  }

  /**
   * Next-run/paused indicator for scheduled flows on the flow card.
   *
   * Paused flows (schedule suspended) get a warning badge; active
   * schedules show the next run time in local time.
   */
  renderScheduleStat(flow: Flow) {
    const schedule = flow.schedule_state;
    if (flow.trigger_event_source !== 'schedule' || !schedule) {
      return '';
    }
    if (!schedule.active) {
      return html`
        <div class="stat-item" title=${schedule.description}>
          <sl-badge variant="warning">Schedule paused</sl-badge>
        </div>
      `;
    }
    return html`
      <div class="stat-item" title=${schedule.description}>
        <sl-icon name="clock"></sl-icon>
        <span>
          ${
            schedule.next_run_at
              ? html`Next run ${formatLocalDateTime(schedule.next_run_at)}`
              : 'No upcoming runs'
          }
        </span>
      </div>
    `;
  }

  getStatusVariant(status: string) {
    switch (status) {
      case 'SUCCEEDED':
        return 'success';
      case 'FAILED':
        return 'danger';
      case 'RUNNING':
      case 'STARTING':
        return 'primary';
      default:
        return 'neutral';
    }
  }

  async triggerTestRun(flowId: string) {
    this.triggeringFlowId = flowId;
    try {
      const execution = await triggerFlowExecution(flowId);
      Router.go(`/console/flows/executions/${execution.id}`);
    } catch (error) {
      console.error('Failed to trigger test run:', error);
      // TODO: Show error notification
    } finally {
      this.triggeringFlowId = null;
    }
  }

  handleAlertDismiss() {
    this.isAlertVisible = false;
    localStorage.setItem('flows-alert-dismissed', 'true');
  }

  async clonePreset(presetId: string) {
    Router.go(`/console/flows/new?preset_id=${presetId}`);
  }

  async removePreset(presetId: string) {
    await deleteFlow(presetId);
    this.presetsLoaded = false;
    await this.ensurePresetsLoaded();
  }

  async deleteFlowHandler(flowId: string, flowName: string) {
    const confirmed = confirm(
      `Are you sure you want to delete the flow "${flowName}"? This action cannot be undone.`
    );
    if (!confirmed) return;

    this.deletingFlowId = flowId;
    try {
      await deleteFlow(flowId);
      // Reload flows list
      this.flows = await getFlows();
    } catch (error) {
      console.error('Failed to delete flow:', error);
      const message =
        error instanceof Error && error.message
          ? error.message
          : 'Failed to delete flow. Please try again.';
      alert(message);
    } finally {
      this.deletingFlowId = null;
    }
  }

  private togglePresets() {
    this.showPresets = !this.showPresets;
    if (this.showPresets) {
      void this.ensurePresetsLoaded();
    }
  }

  private truncateDescription(description: string): string {
    const maxLength = 140;
    if (description.length <= maxLength) {
      return description;
    }

    const truncated = description.slice(0, maxLength).trimEnd();
    const lastSpace = truncated.lastIndexOf(' ');
    const preview =
      lastSpace > 80 ? truncated.slice(0, lastSpace).trimEnd() : truncated;
    return `${preview}...`;
  }

  private renderFlowDescription(flow: Flow) {
    const description = flow.description?.trim();
    const shouldShowFull = (description?.length ?? 0) > 140;
    const preview = description ? this.truncateDescription(description) : '';

    return html`
      <div class="flow-description">
        ${
          description
            ? html`
                <div class="flow-description-text">${preview}</div>
                ${
                  shouldShowFull
                    ? html`
                        <sl-button
                          class="flow-description-action"
                          size="small"
                          variant="text"
                          @click=${(event: Event) => {
                            event.stopPropagation();
                            this.expandedDescription = {
                              title: flow.name,
                              description,
                            };
                          }}
                        >
                          Show full description
                        </sl-button>
                      `
                    : null
                }
              `
            : html`<span class="flow-description-placeholder"
                >No description</span
              >`
        }
      </div>
    `;
  }

  private sortPresets(presets: Flow[]): Flow[] {
    return [...presets].sort((a, b) => {
      const aIsPR = a.name?.toLowerCase().includes('pull request reviewer')
        ? 0
        : 1;
      const bIsPR = b.name?.toLowerCase().includes('pull request reviewer')
        ? 0
        : 1;
      return aIsPR - bIsPR;
    });
  }
}
