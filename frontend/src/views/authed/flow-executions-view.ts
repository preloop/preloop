import { html, css, nothing, unsafeCSS } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import { router } from '../../router';
import {
  getFlowExecutions,
  retryFlowExecution,
  sendCommandToExecution,
} from '../../api';
import { AuthedElement } from '../../api';
import { unifiedWebSocketManager } from '../../services/unified-websocket-manager';
import { confirmDialog } from '../../components/confirm-dialog';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/button-group/button-group.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/tooltip/tooltip.js';
import {
  parseUTCDate,
  formatRelativeTime,
  formatUTCDateTime,
} from '../../utils/date';
import { RUNNING_STATUSES, executionDurationText } from '../../utils/execution';
import {
  executionSubjectCss,
  renderExecutionSubject,
} from '../../utils/execution-subject';
import {
  executionModelCss,
  executionStatusLabel,
  executionStatusVariant,
  formatEstimatedCost,
  renderExecutionModel,
} from '../../utils/execution-presentation';
import type { ExecutionModelUsage } from '../../utils/execution-presentation';
import consoleStyles from '../../styles/console-styles.css?inline';
import { reducedMotionStyles } from '../../styles/reduced-motion';
import '../../components/view-header.ts';
import '../../components/resource-actions.ts';
import type { ResourceAction } from '../../components/resource-actions';

interface FlowExecution {
  id: string;
  flow_id: string;
  flow_name?: string;
  status: string;
  start_time: string;
  end_time?: string;
  tool_calls_count?: number;
  estimated_cost?: number | null;
  /**
   * Short human-readable description of what triggered this execution, e.g.
   * 'preloop/preloop #78 · Pull Request Updated · 5167595c'. Computed when the
   * execution is created; absent on executions that predate subjects.
   */
  trigger_subject?: string | null;
  /** Link to the triggering pull/merge request, when the payload carries one. */
  trigger_subject_url?: string | null;
  /** Alias that served most of the run's gateway requests (wave 7). */
  model_alias?: string | null;
  provider_name?: string | null;
  models_used?: ExecutionModelUsage[] | null;
}

/** How often the elapsed time of running rows is recomputed. */
const DURATION_TICK_MS = 1000;

@customElement('flow-executions-view')
export class FlowExecutionsView extends AuthedElement {
  static styles = [
    reducedMotionStyles,
    unsafeCSS(consoleStyles),
    unsafeCSS(executionSubjectCss),
    unsafeCSS(executionModelCss),
    css`
      :host {
        display: block;
      }
      .table-wrapper {
        overflow-x: auto;
        margin-top: 1rem;
      }
      table {
        width: 100%;
        border-collapse: collapse;
        min-width: 960px;
        font-size: var(--console-text-body);
      }
      /* A cell grid draws a box around every value in the table (wave 4).
         Rows are separated by a hairline and nothing else, and the header is
         the semibold label, not a filled band. */
      th,
      td {
        border: none;
        border-bottom: 1px solid var(--console-hairline);
        padding: 8px;
        text-align: left;
        vertical-align: middle;
      }
      th {
        background-color: transparent;
        color: var(--console-meta-color);
        font-weight: var(--sl-font-weight-semibold);
        font-size: var(--console-text-meta);
        white-space: nowrap;
      }
      tbody tr:last-child td {
        border-bottom: none;
      }
      .execution-row {
        cursor: pointer;
      }
      .execution-row:hover td {
        background-color: var(--console-hover-tint);
      }
      /* The flow name is the row's real anchor, so cmd-click opens a tab. */
      .row-link {
        color: var(--console-link-color);
        display: block;
        font-weight: var(--sl-font-weight-semibold);
        overflow: hidden;
        text-decoration: none;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .row-link:hover,
      .row-link:focus-visible {
        text-decoration: underline;
      }
      .flow-cell {
        max-width: 220px;
      }
      /* The subject is the primary way to tell executions apart, so give it
         room while keeping long repo/branch names from widening the table. */
      .subject-cell {
        max-width: 380px;
        min-width: 220px;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .subject-cell .execution-subject.is-fallback {
        font-family: var(--sl-font-mono);
      }
      .status-cell {
        display: flex;
        align-items: center;
        gap: 8px;
        white-space: nowrap;
      }
      /* One of the page's two ambient animations: the dot that says a run is
         still going. The chip beside it stays a soft tint. */
      .status-indicator {
        width: 8px;
        height: 8px;
        border-radius: 50%;
        flex-shrink: 0;
        animation: pulse 2s infinite;
      }
      .status-indicator.running {
        background-color: var(--sl-color-primary-600);
      }
      .status-indicator.pending {
        background-color: var(--sl-color-warning-600);
      }
      @keyframes pulse {
        0%,
        100% {
          opacity: 1;
        }
        50% {
          opacity: 0.5;
        }
      }
      .started-cell,
      .duration-cell {
        color: var(--console-meta-color);
        font-variant-numeric: tabular-nums;
        white-space: nowrap;
      }
      td.numeric,
      th.numeric {
        text-align: right;
        font-variant-numeric: tabular-nums;
        white-space: nowrap;
      }
      .actions-cell {
        width: 48px;
      }
      .row-actions {
        display: flex;
        justify-content: flex-end;
      }
      .header-controls {
        display: flex;
        justify-content: space-between;
        align-items: center;
        gap: 12px;
        flex-wrap: wrap;
        margin-bottom: 16px;
      }
      .filter-controls {
        display: flex;
        gap: 8px;
        align-items: center;
        flex-wrap: wrap;
      }
      .connection-status {
        display: flex;
        align-items: center;
        gap: 8px;
        font-size: var(--console-text-meta);
        color: var(--console-meta-color);
      }
      .connection-dot {
        width: 8px;
        height: 8px;
        border-radius: 50%;
        background-color: var(--sl-color-neutral-400);
      }
      .connection-dot.live {
        background-color: var(--sl-color-success-600);
      }
      .connection-dot.dropped {
        background-color: var(--sl-color-danger-600);
      }
      .result-count {
        color: var(--console-meta-color);
        font-size: var(--console-text-meta);
        margin-bottom: 12px;
      }
      .load-error {
        margin-bottom: 16px;
      }
      .load-error .retry-button {
        display: block;
        margin-top: 8px;
      }
      .pagination {
        display: flex;
        justify-content: space-between;
        align-items: center;
        margin-top: 16px;
        padding-top: 12px;
        border-top: 1px solid var(--console-hairline);
      }
      .pagination-page {
        color: var(--console-meta-color);
        font-size: var(--console-text-meta);
      }
    `,
  ];

  @state()
  private executions: FlowExecution[] = [];

  /** True while the unified socket reports `connected`. */
  @state()
  private wsConnected = false;

  /**
   * Whether live updates were ever running on this page.
   *
   * A page that has not connected yet is not broken, so it says "Live
   * updates off" in neutral ink. Only a connection that existed and then
   * went away earns the red dot.
   */
  @state()
  private wsWasConnected = false;

  @state()
  private statusFilter = 'all';

  /**
   * Set from `?flow_id=` so links can point at the failed runs of one flow
   * (the Attention page groups failures per flow and links here).
   */
  @state()
  private flowIdFilter: string | null = null;

  @state()
  private flowNameFilter: string | null = null;

  @state()
  private currentPage = 1;

  @state()
  private pageSize = 25;

  @state()
  private hasNextPage = false;

  /** Clock the Duration column of running rows is measured against. */
  @state()
  private durationNow: Date = new Date();

  /** Set when the executions fetch failed, so the page says so. */
  @state()
  private loadError: string | null = null;

  private durationTickIntervalId?: number;

  private unsubscribe?: () => void;
  /** The connection-state listener, kept so it can be dropped on disconnect. */
  private unsubscribeState?: () => void;

  async connectedCallback() {
    super.connectedCallback();
    this.applyQueryParams();
    await this.loadExecutions();
    this.connectWebSocket();
  }

  /** `?status=FAILED&flow_id=<id>` preselects the filters on entry. */
  private applyQueryParams(): void {
    const params = new URLSearchParams(window.location.search);
    const status = params.get('status');
    if (status) {
      const known = [
        'all',
        'RUNNING',
        'PENDING',
        'SUCCEEDED',
        'FAILED',
        'CANCELLED',
      ];
      const normalized =
        status.toLowerCase() === 'all' ? 'all' : status.toUpperCase();
      if (known.includes(normalized)) {
        this.statusFilter = normalized;
      }
    }
    this.flowIdFilter = params.get('flow_id');
  }

  /**
   * A failure here used to be an unhandled rejection: it skipped
   * `connectWebSocket()` on entry, left the page without live updates for the
   * session, and rendered "No executions found" over a list that may be full.
   * The error is caught, shown, and retryable instead.
   */
  async loadExecutions() {
    try {
      const rows = await getFlowExecutions({
        limit: this.pageSize + 1,
        skip: (this.currentPage - 1) * this.pageSize,
        status: this.statusFilter === 'all' ? undefined : this.statusFilter,
        flowId: this.flowIdFilter || undefined,
      });
      this.loadError = null;
      this.hasNextPage = rows.length > this.pageSize;
      this.executions = rows.slice(0, this.pageSize);
      this.flowNameFilter = this.flowIdFilter
        ? this.executions.find((execution) => execution.flow_name)?.flow_name ||
          this.flowNameFilter
        : null;
      this.syncDurationTicker();
    } catch (error) {
      console.error('Failed to load flow executions:', error);
      this.loadError =
        error instanceof Error && error.message
          ? error.message
          : 'Could not load the executions.';
      this.hasNextPage = false;
      this.syncDurationTicker();
    }
  }

  private clearFlowFilter(): void {
    this.flowIdFilter = null;
    this.flowNameFilter = null;
    this.currentPage = 1;
    const url = new URL(window.location.href);
    url.searchParams.delete('flow_id');
    window.history.replaceState({}, '', url.toString());
    void this.loadExecutions();
  }

  get filteredExecutions(): FlowExecution[] {
    return this.executions;
  }

  get paginatedExecutions(): FlowExecution[] {
    return this.filteredExecutions;
  }

  setStatusFilter(status: string) {
    this.statusFilter = status;
    this.currentPage = 1; // Reset to first page when filter changes
    void this.loadExecutions();
  }

  nextPage() {
    if (this.hasNextPage) {
      this.currentPage++;
      void this.loadExecutions();
    }
  }

  prevPage() {
    if (this.currentPage > 1) {
      this.currentPage--;
      void this.loadExecutions();
    }
  }

  connectWebSocket() {
    // Subscribe to flow execution updates through unified WebSocket
    this.unsubscribe = unifiedWebSocketManager.subscribe(
      'flow_executions',
      (message: any) => this.handleWebSocketMessage(message)
    );

    // Track connection state. The unsubscribe is kept: dropping it leaked a
    // listener holding this view for every connect.
    this.unsubscribeState?.();
    this.unsubscribeState = unifiedWebSocketManager.onStateChange((state) => {
      this.wsConnected = state === 'connected';
      if (this.wsConnected) {
        this.wsWasConnected = true;
      }
    });
  }

  handleWebSocketMessage(message: any) {
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
        // Maintain sort order after update
        this.executions = updated.sort(
          (a, b) =>
            parseUTCDate(b.start_time).getTime() -
            parseUTCDate(a.start_time).getTime()
        );
        this.syncDurationTicker();
      } else {
        // New execution started, reload the list
        this.loadExecutions();
      }
    }

    // Handle new executions
    if (message.type === 'execution_started' && message.payload) {
      this.loadExecutions();
    }
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    this.stopDurationTicker();
    // Unsubscribe from flow execution updates
    this.unsubscribe?.();
    this.unsubscribe = undefined;
    this.unsubscribeState?.();
    this.unsubscribeState = undefined;
  }

  /**
   * The Duration column counts up on its own while a run is live, and the
   * timer exists only while there is something to count: a page of finished
   * runs leaves no interval behind.
   */
  private syncDurationTicker(): void {
    const hasRunningRow = this.executions.some((execution) =>
      RUNNING_STATUSES.has(execution.status)
    );
    if (hasRunningRow) {
      if (this.durationTickIntervalId === undefined) {
        this.durationTickIntervalId = window.setInterval(() => {
          this.durationNow = new Date();
        }, DURATION_TICK_MS);
      }
    } else {
      this.stopDurationTicker();
    }
  }

  private stopDurationTicker(): void {
    if (this.durationTickIntervalId !== undefined) {
      clearInterval(this.durationTickIntervalId);
      this.durationTickIntervalId = undefined;
    }
  }

  private executionUrl(execution: FlowExecution): string {
    return router.urlForPath(`/console/flows/executions/${execution.id}`);
  }

  /**
   * The whole row is clickable, but the flow name and the subject link are
   * real anchors, so cmd-click and middle-click keep working. Clicks that
   * started inside a link or the kebab are left alone.
   */
  private handleRowClick(event: MouseEvent, execution: FlowExecution): void {
    if (event.defaultPrevented) return;
    if (
      event.metaKey ||
      event.ctrlKey ||
      event.shiftKey ||
      event.button !== 0
    ) {
      return;
    }
    for (const node of event.composedPath()) {
      if (!(node instanceof HTMLElement)) continue;
      if (node.tagName === 'TR') break;
      const tag = node.tagName.toLowerCase();
      if (
        tag === 'a' ||
        tag === 'sl-button' ||
        tag === 'sl-icon-button' ||
        tag === 'sl-menu-item' ||
        tag === 'resource-actions'
      ) {
        return;
      }
    }
    window.location.href = this.executionUrl(execution);
  }

  /** Row menu: open, open the conversation, and the two run controls. */
  private getRowActions(execution: FlowExecution): ResourceAction[] {
    const url = this.executionUrl(execution);
    const actions: ResourceAction[] = [
      { id: 'open', label: 'Open', icon: 'box-arrow-up-right', href: url },
      {
        id: 'open-session',
        label: 'Open session',
        icon: 'chat-left-text',
        href: `${url}?tab=transcript`,
      },
    ];
    if (RUNNING_STATUSES.has(execution.status)) {
      actions.push({
        id: 'cancel',
        label: 'Cancel run',
        icon: 'x-circle',
        variant: 'danger',
        separated: true,
        onClick: () => void this.cancelExecution(execution),
      });
    } else if (this.canRetry(execution)) {
      actions.push({
        id: 'retry',
        label: 'Retry run',
        icon: 'arrow-repeat',
        separated: true,
        onClick: () => void this.retryExecution(execution),
      });
    }
    return actions;
  }

  /** Statuses the retry endpoint accepts (mirrors the execution page). */
  private canRetry(execution: FlowExecution): boolean {
    return ['FAILED', 'STOPPED', 'TIMEOUT', 'CANCELLED'].includes(
      execution.status
    );
  }

  /**
   * Stopping a run destroys work in progress and cannot be undone from here,
   * which is exactly the kind of thing the console asks about first.
   */
  private async cancelExecution(execution: FlowExecution): Promise<void> {
    const confirmed = await confirmDialog({
      title: 'Cancel run',
      message: `Stop the run of "${execution.flow_name || 'this flow'}"?`,
      detail:
        'The agent stops where it is. Work already done is kept in the run, but the run does not finish, and it cannot be resumed — only retried from the start.',
      confirmLabel: 'Cancel run',
      cancelLabel: 'Keep running',
      variant: 'danger',
    });
    if (!confirmed) return;

    try {
      await sendCommandToExecution(execution.id, 'stop');
      await this.loadExecutions();
    } catch (error) {
      this.showToast(
        error instanceof Error ? error.message : 'Could not cancel the run'
      );
    }
  }

  private async retryExecution(execution: FlowExecution): Promise<void> {
    try {
      const result = await retryFlowExecution(execution.id);
      if (result?.id) {
        window.location.href = router.urlForPath(
          `/console/flows/executions/${result.id}`
        );
        return;
      }
      // The retry was accepted but named no run, so there is nowhere to go:
      // say so rather than leaving the click looking ignored.
      this.showToast(
        'The retry did not return a new run. Check the executions list.'
      );
      await this.loadExecutions();
    } catch (error) {
      this.showToast(
        error instanceof Error ? error.message : 'Could not retry the run'
      );
    }
  }

  private showToast(message: string): void {
    this.dispatchEvent(
      new CustomEvent('show-toast', {
        bubbles: true,
        composed: true,
        detail: { message, variant: 'danger' },
      })
    );
  }

  /**
   * Live updates read as three states, and only the third is a fault: on,
   * off (never connected on this page), and lost (connected, then dropped).
   */
  private renderConnectionStatus() {
    const state = this.wsConnected
      ? 'live'
      : this.wsWasConnected
        ? 'dropped'
        : 'off';
    const label =
      state === 'live'
        ? 'Live updates on'
        : state === 'dropped'
          ? 'Live updates lost'
          : 'Live updates off';
    return html`
      <div class="connection-status" data-connection=${state}>
        <div class="connection-dot ${state === 'off' ? '' : state}"></div>
        <span>${label}</span>
      </div>
    `;
  }

  /**
   * The fetch failed. Rows already on screen stay — they were true when they
   * arrived — with the failure and the retry stated above them.
   */
  private renderLoadError() {
    if (!this.loadError) return nothing;
    return html`
      <sl-alert variant="danger" open class="load-error">
        <sl-icon slot="icon" name="exclamation-triangle"></sl-icon>
        Could not load the executions. ${this.loadError}
        <sl-button
          size="small"
          class="retry-button"
          @click=${() => void this.loadExecutions()}
        >
          <sl-icon slot="prefix" name="arrow-clockwise"></sl-icon>
          Try again
        </sl-button>
      </sl-alert>
    `;
  }

  render() {
    const firstRowNumber = (this.currentPage - 1) * this.pageSize + 1;
    return html`
      <view-header headerText="Flow Executions" width="wide"></view-header>
      <div class="column-layout wide">
        <div class="main-column">
          <div class="header-controls">
            <div class="filter-controls">
              <sl-button-group>
                <sl-button
                  size="small"
                  data-status="all"
                  variant=${this.statusFilter === 'all' ? 'primary' : 'default'}
                  @click=${() => this.setStatusFilter('all')}
                >
                  All
                </sl-button>
                <sl-button
                  size="small"
                  data-status="RUNNING"
                  variant=${this.statusFilter === 'RUNNING' ? 'primary' : 'default'}
                  @click=${() => this.setStatusFilter('RUNNING')}
                >
                  Running
                </sl-button>
                <sl-button
                  size="small"
                  data-status="PENDING"
                  variant=${this.statusFilter === 'PENDING' ? 'neutral' : 'default'}
                  @click=${() => this.setStatusFilter('PENDING')}
                >
                  Pending
                </sl-button>
                <sl-button
                  size="small"
                  data-status="SUCCEEDED"
                  variant=${this.statusFilter === 'SUCCEEDED' ? 'success' : 'default'}
                  @click=${() => this.setStatusFilter('SUCCEEDED')}
                >
                  Succeeded
                </sl-button>
                <sl-button
                  size="small"
                  data-status="FAILED"
                  variant=${this.statusFilter === 'FAILED' ? 'danger' : 'default'}
                  @click=${() => this.setStatusFilter('FAILED')}
                >
                  Failed
                </sl-button>
                <sl-button
                  size="small"
                  data-status="CANCELLED"
                  variant=${this.statusFilter === 'CANCELLED' ? 'warning' : 'default'}
                  @click=${() => this.setStatusFilter('CANCELLED')}
                >
                  Cancelled
                </sl-button>
              </sl-button-group>
              <sl-button size="small" @click=${this.loadExecutions}>
                <sl-icon name="arrow-clockwise"></sl-icon>
                Refresh
              </sl-button>
              ${
                this.flowIdFilter
                  ? html`<sl-button
                      size="small"
                      pill
                      class="flow-filter-chip"
                      @click=${this.clearFlowFilter}
                    >
                      Flow: ${this.flowNameFilter || this.flowIdFilter}
                      <sl-icon slot="suffix" name="x"></sl-icon>
                    </sl-button>`
                  : ''
              }
            </div>
            ${this.renderConnectionStatus()}
          </div>

          ${this.renderLoadError()}
          ${
            this.paginatedExecutions.length === 0
              ? this.loadError
                ? // The error above already said what happened; "No
                  // executions found" underneath it would contradict it.
                  nothing
                : html`
                    <div class="empty-state">
                      <sl-icon name="inbox"></sl-icon>
                      <p>No executions found.</p>
                    </div>
                  `
              : html`
                  <div class="result-count">
                    Showing ${firstRowNumber} -
                    ${firstRowNumber + this.paginatedExecutions.length - 1}
                    executions
                  </div>

                  <div class="table-wrapper">
                    <table>
                      <thead>
                        <tr>
                          <th>Flow</th>
                          <th>Subject</th>
                          <th>Status</th>
                          <th>Started</th>
                          <th>Duration</th>
                          <th>Model</th>
                          <th class="numeric">Tool calls</th>
                          <th class="numeric">$ est.</th>
                          <th class="actions-cell"></th>
                        </tr>
                      </thead>
                      <tbody>
                        ${this.paginatedExecutions.map((exec) =>
                          this.renderRow(exec)
                        )}
                      </tbody>
                    </table>
                  </div>

                  ${
                    this.currentPage > 1 || this.hasNextPage
                      ? html`
                          <div class="pagination">
                            <sl-button
                              size="small"
                              @click=${this.prevPage}
                              ?disabled=${this.currentPage === 1}
                            >
                              <sl-icon name="chevron-left"></sl-icon>
                              Previous
                            </sl-button>
                            <div class="pagination-page">
                              Page ${this.currentPage}
                            </div>
                            <sl-button
                              size="small"
                              @click=${this.nextPage}
                              ?disabled=${!this.hasNextPage}
                            >
                              Next
                              <sl-icon name="chevron-right"></sl-icon>
                            </sl-button>
                          </div>
                        `
                      : ''
                  }
                `
          }
        </div>
      </div>
    `;
  }

  private renderRow(exec: FlowExecution) {
    const isLive = RUNNING_STATUSES.has(exec.status);
    const variant = executionStatusVariant(exec.status);
    return html`
      <tr
        class="execution-row"
        @click=${(event: MouseEvent) => this.handleRowClick(event, exec)}
      >
        <td class="flow-cell">
          <a class="row-link" href=${this.executionUrl(exec)}
            >${exec.flow_name || 'Unnamed flow'}</a
          >
        </td>
        <td class="subject-cell">${renderExecutionSubject(exec)}</td>
        <td>
          <div class="status-cell">
            ${
              isLive
                ? html`<div
                    class="status-indicator ${
                      exec.status === 'PENDING' ? 'pending' : 'running'
                    }"
                  ></div>`
                : ''
            }
            <sl-badge
              class="chip ${variant === 'danger' ? 'solid' : ''}"
              pill
              variant=${variant}
              >${executionStatusLabel(exec.status)}</sl-badge
            >
          </div>
        </td>
        <td class="started-cell" title=${formatUTCDateTime(exec.start_time)}>
          ${formatRelativeTime(exec.start_time)}
        </td>
        <td class="duration-cell">
          ${executionDurationText(exec, this.durationNow) || '—'}
        </td>
        <td class="model-cell">${renderExecutionModel(exec)}</td>
        <td class="numeric">
          ${(exec.tool_calls_count || 0).toLocaleString()}
        </td>
        <td class="numeric">${formatEstimatedCost(exec.estimated_cost)}</td>
        <td class="actions-cell">
          <div
            class="row-actions"
            @click=${(event: Event) => event.stopPropagation()}
            @keydown=${(event: Event) => event.stopPropagation()}
          >
            <resource-actions
              .actions=${this.getRowActions(exec)}
              menu-only
            ></resource-actions>
          </div>
        </td>
      </tr>
    `;
  }

  /** Kept for callers and tests from earlier waves. */
  getStatusVariant(status: string) {
    return executionStatusVariant(status);
  }
}
