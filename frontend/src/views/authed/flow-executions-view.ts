import { html, css, unsafeCSS } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import { router } from '../../router';
import { getFlowExecutions } from '../../api';
import { AuthedElement } from '../../api';
import { unifiedWebSocketManager } from '../../services/unified-websocket-manager';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/button-group/button-group.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/select/select.js';
import '@shoelace-style/shoelace/dist/components/option/option.js';
import { parseUTCDate, formatLocalDateTime } from '../../utils/date';
import { executionDurationText } from '../../utils/execution';
import consoleStyles from '../../styles/console-styles.css?inline';
import { reducedMotionStyles } from '../../styles/reduced-motion';
import '../../components/view-header.ts';

interface FlowExecution {
  id: string;
  flow_id: string;
  flow_name?: string;
  status: string;
  start_time: string;
  end_time?: string;
  tool_calls_count?: number;
  /**
   * Short human-readable description of what triggered this execution, e.g.
   * 'preloop/preloop #78 · Pull Request Updated · 5167595c'. Computed when the
   * execution is created; absent on executions that predate subjects.
   */
  trigger_subject?: string | null;
  /** Link to the triggering pull/merge request, when the payload carries one. */
  trigger_subject_url?: string | null;
}

@customElement('flow-executions-view')
export class FlowExecutionsView extends AuthedElement {
  static styles = [
    reducedMotionStyles,
    unsafeCSS(consoleStyles),
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
        min-width: 800px;
      }
      th,
      td {
        border: 1px solid var(--sl-color-neutral-200);
        padding: 8px;
        text-align: left;
      }
      th {
        background-color: var(--sl-color-neutral-100);
      }
      /* The subject is the primary way to tell executions apart, so give it
         room while keeping long repo/branch names from widening the table. */
      .subject-cell {
        max-width: 340px;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .subject-cell a {
        color: inherit;
        text-decoration: none;
        border-bottom: 1px solid var(--sl-color-neutral-300);
      }
      .subject-cell a:hover,
      .subject-cell a:focus-visible {
        color: var(--sl-color-primary-600);
        border-bottom-color: var(--sl-color-primary-600);
      }
      /* Fallback for executions with no derivable subject: the short id. */
      .subject-fallback {
        font-family: var(--sl-font-mono);
        color: var(--sl-color-neutral-500);
      }
      .status-cell {
        display: flex;
        align-items: center;
        gap: 8px;
      }
      .status-indicator {
        width: 8px;
        height: 8px;
        border-radius: 50%;
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
      .header-controls {
        display: flex;
        justify-content: space-between;
        align-items: center;
        margin-bottom: 16px;
      }
      .connection-status {
        display: flex;
        align-items: center;
        gap: 8px;
        font-size: 0.9rem;
        color: var(--sl-color-neutral-600);
      }
      .connection-dot {
        width: 8px;
        height: 8px;
        border-radius: 50%;
        background-color: var(--sl-color-success-600);
      }
      .connection-dot.disconnected {
        background-color: var(--sl-color-danger-600);
      }
    `,
  ];

  @state()
  private executions: FlowExecution[] = [];

  @state()
  private wsConnected = false;

  @state()
  private statusFilter = 'all';

  @state()
  private currentPage = 1;

  @state()
  private pageSize = 20;

  @state()
  private hasNextPage = false;

  private unsubscribe?: () => void;

  async connectedCallback() {
    super.connectedCallback();
    await this.loadExecutions();
    this.connectWebSocket();
  }

  async loadExecutions() {
    const rows = await getFlowExecutions({
      limit: this.pageSize + 1,
      skip: (this.currentPage - 1) * this.pageSize,
      status: this.statusFilter === 'all' ? undefined : this.statusFilter,
    });
    this.hasNextPage = rows.length > this.pageSize;
    this.executions = rows.slice(0, this.pageSize);
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

  handleStatusFilterChange(event: Event | { target: { value: string } }) {
    const target = (event as any).target || event;
    this.setStatusFilter(target.value);
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

    // Track connection state
    unifiedWebSocketManager.onStateChange((state) => {
      this.wsConnected = state === 'connected';
      console.log(`Flow executions WebSocket state: ${state}`);
    });
  }

  handleWebSocketMessage(message: any) {
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
        // Maintain sort order after update
        this.executions = updated.sort(
          (a, b) =>
            parseUTCDate(b.start_time).getTime() -
            parseUTCDate(a.start_time).getTime()
        );
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
    // Unsubscribe from flow execution updates
    this.unsubscribe?.();
  }

  render() {
    return html`
      <view-header headerText="Flow Executions" width="wide"></view-header>
      <div class="column-layout wide">
        <div class="main-column">
          <div class="header-controls">
            <div
              style="display: flex; gap: 8px; align-items: center; flex-wrap: wrap;"
            >
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
            </div>
            <div class="connection-status">
              <div
                class="connection-dot ${this.wsConnected ? '' : 'disconnected'}"
              ></div>
              <span>${this.wsConnected ? 'Live Updates' : 'Disconnected'}</span>
            </div>
          </div>

          ${
            this.paginatedExecutions.length === 0
              ? html`
                  <div
                    style="text-align: center; padding: 40px; color: var(--sl-color-neutral-600);"
                  >
                    <sl-icon name="inbox" style="font-size: 3rem;"></sl-icon>
                    <p>No executions found.</p>
                  </div>
                `
              : html`
                  <div
                    style="margin-bottom: 12px; color: var(--sl-color-neutral-600); font-size: 0.9rem;"
                  >
                    Showing ${(this.currentPage - 1) * this.pageSize + 1} -
                    ${
                      (this.currentPage - 1) * this.pageSize +
                      this.paginatedExecutions.length
                    }
                    executions
                  </div>

                  <div class="table-wrapper">
                    <table>
                      <thead>
                        <tr>
                          <th>Flow Name</th>
                          <th>Subject</th>
                          <th>Status</th>
                          <th>Start Time</th>
                          <th>Duration</th>
                          <th>Tool Calls</th>
                          <th>Details</th>
                        </tr>
                      </thead>
                      <tbody>
                        ${this.paginatedExecutions.map(
                          (exec) => html`
                            <tr>
                              <td>${exec.flow_name || 'Unnamed Flow'}</td>
                              <td class="subject-cell">
                                ${this.renderSubject(exec)}
                              </td>
                              <td>
                                <div class="status-cell">
                                  ${
                                    exec.status === 'RUNNING' ||
                                    exec.status === 'PENDING'
                                      ? html`
                                          <div
                                            class="status-indicator ${exec.status.toLowerCase()}"
                                          ></div>
                                        `
                                      : ''
                                  }
                                  <sl-badge
                                    variant=${this.getStatusVariant(exec.status)}
                                    >${exec.status}</sl-badge
                                  >
                                </div>
                              </td>
                              <td>${formatLocalDateTime(exec.start_time)}</td>
                              <td>${executionDurationText(exec) || '—'}</td>
                              <td>${exec.tool_calls_count || 0}</td>
                              <td>
                                <sl-button
                                  size="small"
                                  href=${router.urlForPath(
                                    `/console/flows/executions/${exec.id}`
                                  )}
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
                  </div>

                  ${
                    this.currentPage > 1 || this.hasNextPage
                      ? html`
                          <div
                            style="display: flex; justify-content: space-between; align-items: center; margin-top: 16px; padding: 12px; background: var(--sl-color-neutral-50); border-radius: 4px;"
                          >
                            <sl-button
                              size="small"
                              @click=${this.prevPage}
                              ?disabled=${this.currentPage === 1}
                            >
                              <sl-icon name="chevron-left"></sl-icon>
                              Previous
                            </sl-button>
                            <div style="color: var(--sl-color-neutral-700);">
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

  /**
   * Render the identifying subject for an execution row.
   *
   * Prefers the server-computed trigger subject, linking to the underlying
   * pull/merge request when the trigger payload carried a URL. Executions
   * created before subjects were recorded (or whose trigger carried nothing
   * identifying) fall back to the short execution id, so the column is never
   * empty.
   */
  renderSubject(exec: FlowExecution) {
    const subject = exec.trigger_subject;
    if (!subject) {
      return html`<span class="subject-fallback" title=${exec.id}
        >${exec.id.slice(0, 8)}</span
      >`;
    }
    if (exec.trigger_subject_url) {
      return html`<a
        href=${exec.trigger_subject_url}
        target="_blank"
        rel="noopener noreferrer"
        title=${subject}
        @click=${(e: Event) => e.stopPropagation()}
        >${subject}</a
      >`;
    }
    return html`<span title=${subject}>${subject}</span>`;
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
}
