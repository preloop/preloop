import { html, css, unsafeCSS } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import { AuthedElement, approveRequest, declineRequest } from '../../api';
import type { ApprovalRequest } from '../../types';
import '../../components/question-answer-panel';
import type { QuestionAnswerDetail } from '../../components/question-answer-panel';
import {
  formatFutureRelativeTime,
  formatRelativeTime,
  parseUTCDate,
} from '../../utils/date';
import { unifiedWebSocketManager } from '../../services/unified-websocket-manager';
import '@shoelace-style/shoelace/dist/components/card/card.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/select/select.js';
import '@shoelace-style/shoelace/dist/components/option/option.js';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/tag/tag.js';
import '@shoelace-style/shoelace/dist/components/tooltip/tooltip.js';
import '@shoelace-style/shoelace/dist/components/divider/divider.js';
import '@shoelace-style/shoelace/dist/components/progress-bar/progress-bar.js';
import consoleStyles from '../../styles/console-styles.css?inline';

interface ApprovalStats {
  total: number;
  pending: number;
  approved: number;
  declined: number;
  expired: number;
  cancelled: number;
  avgResponseTimeMinutes: number;
  /** Percentage of HUMAN decisions that were approvals. Excludes bypassed/AI. */
  approvalRate: number;
  /** Requests auto-approved by a time-boxed bypass, with no human review. */
  autoApprovedByBypass: number;
}

@customElement('approvals-view')
export class ApprovalsView extends AuthedElement {
  @state()
  private approvalRequests: ApprovalRequest[] = [];

  @state()
  private filteredRequests: ApprovalRequest[] = [];

  @state()
  private loading = true;

  @state()
  private stats: ApprovalStats = {
    total: 0,
    pending: 0,
    approved: 0,
    declined: 0,
    expired: 0,
    cancelled: 0,
    avgResponseTimeMinutes: 0,
    approvalRate: 0,
    autoApprovedByBypass: 0,
  };

  @state()
  private statusFilter: string = 'all';

  @state()
  private toolFilter: string = 'all';

  @state()
  private searchQuery: string = '';

  /** Id of the question currently being answered, if any. */
  @state()
  private answeringId: string | null = null;

  @state()
  private answerError: string | null = null;

  private unsubscribe?: () => void;

  static styles = [
    unsafeCSS(consoleStyles),
    css`
      .stats-grid {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
        gap: var(--sl-spacing-medium);
        margin-bottom: var(--sl-spacing-large);
      }

      .stat-card {
        background: var(--sl-color-neutral-0);
        border: 1px solid var(--sl-color-neutral-200);
        border-radius: var(--sl-border-radius-medium);
        padding: var(--sl-spacing-medium);
        text-align: center;
      }

      .filters-row {
        display: flex;
        gap: var(--sl-spacing-medium);
        margin-bottom: var(--sl-spacing-large);
        flex-wrap: wrap;
        align-items: flex-end;
      }

      .filters-row sl-select {
        min-width: 150px;
      }

      .filters-row sl-input {
        flex: 1;
        min-width: 200px;
      }

      .approval-list {
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-small);
      }

      .approval-item {
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-small);
        padding: var(--sl-spacing-medium);
        background: var(--sl-color-neutral-0);
        border: 1px solid var(--sl-color-neutral-200);
        border-radius: var(--sl-border-radius-medium);
        transition: all 0.2s ease;
      }

      .approval-row {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: var(--sl-spacing-medium);
      }

      .approval-item.question {
        border-left: 3px solid #30c9e8;
      }

      .answer-error {
        color: var(--sl-color-danger-600);
        font-size: var(--sl-font-size-small);
      }

      .approval-item:hover {
        border-color: var(--sl-color-primary-300);
        box-shadow: 0 2px 8px rgba(0, 0, 0, 0.05);
      }

      .approval-item.pending {
        border-left: 3px solid var(--sl-color-warning-500);
      }

      .approval-item.approved {
        border-left: 3px solid var(--sl-color-success-500);
      }

      .approval-item.declined {
        border-left: 3px solid var(--sl-color-danger-500);
      }

      .approval-item.expired {
        border-left: 3px solid var(--sl-color-neutral-400);
      }

      .approval-item.cancelled {
        border-left: 3px solid var(--sl-color-neutral-400);
      }

      .approval-info {
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-2x-small);
        flex: 1;
        min-width: 0;
      }

      .approval-tool {
        font-weight: 600;
        color: var(--sl-color-neutral-900);
        display: flex;
        align-items: center;
        gap: var(--sl-spacing-small);
      }

      .approval-tool code {
        font-family: monospace;
        background: var(--sl-color-neutral-100);
        padding: 0.125rem 0.375rem;
        border-radius: var(--sl-border-radius-small);
        font-size: var(--sl-font-size-small);
      }

      .approval-meta {
        display: flex;
        gap: var(--sl-spacing-medium);
        font-size: var(--sl-font-size-small);
        color: var(--sl-color-neutral-600);
        flex-wrap: wrap;
      }

      .approval-meta-item {
        display: flex;
        align-items: center;
        gap: var(--sl-spacing-2x-small);
      }

      .approval-actions {
        display: flex;
        align-items: center;
        gap: var(--sl-spacing-small);
      }

      .rate-bar {
        margin-top: var(--sl-spacing-medium);
      }

      .rate-labels {
        display: flex;
        justify-content: space-between;
        font-size: var(--sl-font-size-x-small);
        color: var(--sl-color-neutral-600);
        margin-bottom: var(--sl-spacing-2x-small);
      }

      .summary-row {
        display: flex;
        gap: var(--sl-spacing-large);
        margin-bottom: var(--sl-spacing-large);
      }

      .summary-card {
        flex: 1;
      }

      .response-time-breakdown {
        display: flex;
        gap: var(--sl-spacing-large);
        margin-top: var(--sl-spacing-medium);
      }

      .response-time-item {
        text-align: center;
      }

      .response-time-value {
        font-size: 1.5rem;
        font-weight: 600;
        color: var(--sl-color-primary-600);
      }

      .response-time-label {
        font-size: var(--sl-font-size-x-small);
        color: var(--sl-color-neutral-600);
      }
    `,
  ];

  async connectedCallback() {
    super.connectedCallback();
    await this.loadApprovalRequests();
    this.connectWebSocket();
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    this.unsubscribe?.();
  }

  private connectWebSocket() {
    this.unsubscribe = unifiedWebSocketManager.subscribe(
      'approvals',
      (message: any) => this.handleWebSocketMessage(message)
    );
  }

  private handleWebSocketMessage(message: any) {
    console.log('Approvals view received update:', message);

    // Handle new approval request
    if (message.type === 'approval_created') {
      const newApproval: ApprovalRequest = {
        id: message.approval_request_id,
        account_id: message.account_id || '',
        tool_configuration_id: message.tool_configuration_id || '',
        approval_workflow_id: message.approval_workflow_id || '',
        execution_id: message.execution_id || null,
        tool_name: message.tool_name,
        summary: message.summary || null,
        tool_args: message.tool_args || {},
        agent_reasoning: message.agent_reasoning || null,
        status: 'pending',
        requested_at: message.requested_at || new Date().toISOString(),
        resolved_at: null,
        expires_at: message.expires_at || null,
        approver_comment: null,
        is_question: message.is_question === true,
        question: message.question || null,
        question_options: message.question_options || [],
        allow_free_text: message.allow_free_text === true,
      };

      if (!this.isUnexpiredPendingRequest(newApproval)) {
        return;
      }

      // Add to the beginning of the list
      this.approvalRequests = [newApproval, ...this.approvalRequests];
      this.applyFilters();
      this.calculateStats();
    }

    // Handle status updates
    if (
      message.type === 'approval_approved' ||
      message.type === 'approval_declined' ||
      message.type === 'approval_expired' ||
      message.type === 'approval_cancelled'
    ) {
      const index = this.approvalRequests.findIndex(
        (r) => r.id === message.approval_request_id
      );
      if (index !== -1) {
        const status = message.type.replace(
          'approval_',
          ''
        ) as ApprovalRequest['status'];
        this.approvalRequests = [
          ...this.approvalRequests.slice(0, index),
          {
            ...this.approvalRequests[index],
            status,
            resolved_at: message.resolved_at || new Date().toISOString(),
          },
          ...this.approvalRequests.slice(index + 1),
        ];
        this.applyFilters();
        this.calculateStats();
      }
    }
  }

  private async loadApprovalRequests() {
    this.loading = true;
    try {
      // Fetch approval requests (API max limit is 100)
      const data = await this.fetchData('/api/v1/approval-requests?limit=100');
      if (data && Array.isArray(data)) {
        // Sort by requested_at descending (most recent first)
        this.approvalRequests = (data as ApprovalRequest[])
          .map((request) => this.normalizeApprovalRequest(request))
          .sort(
            (a, b) =>
              parseUTCDate(b.requested_at).getTime() -
              parseUTCDate(a.requested_at).getTime()
          );
        this.applyFilters();
        this.calculateStats();
      }
    } catch (error) {
      console.error('Failed to load approval requests:', error);
    } finally {
      this.loading = false;
    }
  }

  private calculateStats() {
    const requests = this.approvalRequests;
    const total = requests.length;
    const pending = requests.filter((r) => r.status === 'pending').length;
    const approved = requests.filter((r) => r.status === 'approved').length;
    const declined = requests.filter((r) => r.status === 'declined').length;
    const expired = requests.filter((r) => r.status === 'expired').length;
    const cancelled = requests.filter((r) => r.status === 'cancelled').length;

    // Calculate average response time for resolved requests
    let totalResponseTime = 0;
    let resolvedCount = 0;
    requests.forEach((r) => {
      if (
        r.resolved_at &&
        (r.status === 'approved' || r.status === 'declined')
      ) {
        const requestTime = parseUTCDate(r.requested_at).getTime();
        const resolvedTime = parseUTCDate(r.resolved_at).getTime();
        totalResponseTime += (resolvedTime - requestTime) / 60000; // minutes
        resolvedCount++;
      }
    });

    const avgResponseTimeMinutes =
      resolvedCount > 0 ? Math.round(totalResponseTime / resolvedCount) : 0;

    // Approval rate counts HUMAN decisions only. Requests auto-approved by a
    // bypass (or decided by AI) never reached a person, so folding them in
    // would overstate how much oversight actually happened - the opposite of
    // what this number is for.
    const humanApproved = requests.filter(
      (r) =>
        r.status === 'approved' && !r.auto_approved_reason && !r.decided_by_ai
    ).length;
    const humanDeclined = requests.filter(
      (r) =>
        r.status === 'declined' && !r.auto_approved_reason && !r.decided_by_ai
    ).length;
    const decidedCount = humanApproved + humanDeclined;
    const approvalRate =
      decidedCount > 0 ? (humanApproved / decidedCount) * 100 : 0;

    // Surfaced separately so an operator can see the unsupervised volume
    // rather than having it silently blended into "approved".
    const autoApprovedByBypass = requests.filter(
      (r) => !!r.auto_approved_reason
    ).length;

    this.stats = {
      total,
      pending,
      approved,
      declined,
      expired,
      cancelled,
      avgResponseTimeMinutes,
      approvalRate,
      autoApprovedByBypass,
    };
  }

  private applyFilters() {
    let filtered = [...this.approvalRequests];

    // Status filter
    if (this.statusFilter !== 'all') {
      filtered = filtered.filter((r) => r.status === this.statusFilter);
    }

    // Tool filter
    if (this.toolFilter !== 'all') {
      filtered = filtered.filter((r) => r.tool_name === this.toolFilter);
    }

    // Search query (searches tool name, execution ID, and reasoning)
    if (this.searchQuery.trim()) {
      const query = this.searchQuery.toLowerCase();
      filtered = filtered.filter(
        (r) =>
          r.tool_name.toLowerCase().includes(query) ||
          r.summary?.toLowerCase().includes(query) ||
          r.execution_id?.toLowerCase().includes(query) ||
          r.agent_reasoning?.toLowerCase().includes(query) ||
          JSON.stringify(r.tool_args).toLowerCase().includes(query)
      );
    }

    this.filteredRequests = filtered;
  }

  private getUniqueTools(): string[] {
    const tools = new Set(this.approvalRequests.map((r) => r.tool_name));
    return Array.from(tools).sort();
  }

  private formatDate(dateStr: string): string {
    return formatRelativeTime(dateStr);
  }

  private formatExpiryDate(dateStr: string): string {
    return formatFutureRelativeTime(dateStr);
  }

  private isUnexpiredPendingRequest(request: ApprovalRequest): boolean {
    if (request.status !== 'pending') {
      return false;
    }
    if (!request.expires_at) {
      return true;
    }
    return parseUTCDate(request.expires_at).getTime() > Date.now();
  }

  private normalizeApprovalRequest(request: ApprovalRequest): ApprovalRequest {
    if (
      request.status !== 'pending' ||
      this.isUnexpiredPendingRequest(request)
    ) {
      return request;
    }
    return {
      ...request,
      status: 'expired',
      resolved_at: request.resolved_at || request.expires_at,
    };
  }

  private formatFullDate(dateStr: string): string {
    const date = parseUTCDate(dateStr);
    return date.toLocaleString('en-US', {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    });
  }

  private getStatusVariant(
    status: string
  ): 'primary' | 'success' | 'warning' | 'danger' | 'neutral' {
    switch (status) {
      case 'pending':
        return 'warning';
      case 'approved':
        return 'success';
      case 'declined':
        return 'danger';
      case 'expired':
        return 'neutral';
      case 'cancelled':
        return 'neutral';
      default:
        return 'neutral';
    }
  }

  private getStatusIcon(status: string): string {
    switch (status) {
      case 'pending':
        return 'hourglass-split';
      case 'approved':
        return 'check-circle';
      case 'declined':
        return 'x-circle';
      case 'expired':
        return 'clock-history';
      case 'cancelled':
        return 'slash-circle';
      default:
        return 'question-circle';
    }
  }

  private handleStatusFilterChange(e: CustomEvent) {
    this.statusFilter = (e.target as HTMLSelectElement).value;
    this.applyFilters();
  }

  private handleToolFilterChange(e: CustomEvent) {
    this.toolFilter = (e.target as HTMLSelectElement).value;
    this.applyFilters();
  }

  private handleSearchInput(e: CustomEvent) {
    this.searchQuery = (e.target as HTMLInputElement).value;
    this.applyFilters();
  }

  private isQuestion(request: ApprovalRequest): boolean {
    return request.is_question === true;
  }

  private questionText(request: ApprovalRequest): string {
    return request.question || request.summary || request.tool_name;
  }

  private applyResolution(
    requestId: string,
    updated: Partial<ApprovalRequest>
  ) {
    const index = this.approvalRequests.findIndex((r) => r.id === requestId);
    if (index === -1) return;
    this.approvalRequests = [
      ...this.approvalRequests.slice(0, index),
      {
        ...this.approvalRequests[index],
        ...updated,
        resolved_at:
          updated.resolved_at ??
          this.approvalRequests[index].resolved_at ??
          new Date().toISOString(),
      },
      ...this.approvalRequests.slice(index + 1),
    ];
    this.applyFilters();
    this.calculateStats();
  }

  /** An answered question is submitted as an approve carrying the answer. */
  private async handleQuestionAnswer(
    request: ApprovalRequest,
    e: CustomEvent<QuestionAnswerDetail>
  ) {
    const { selectedOption, answerText } = e.detail;
    this.answeringId = request.id;
    this.answerError = null;
    try {
      const updated = await approveRequest(request.id, {
        selected_option: selectedOption ?? null,
        answer_text: answerText ?? null,
      });
      this.applyResolution(request.id, {
        status: 'approved',
        resolved_at: updated?.resolved_at ?? null,
        approver_comment:
          updated?.approver_comment ?? answerText ?? selectedOption ?? null,
      });
    } catch (error: any) {
      this.answerError = error?.message || 'Failed to send answer';
      console.error('Failed to answer question:', error);
    } finally {
      this.answeringId = null;
    }
  }

  /** Dismissing a question declines it, exactly as the mobile apps do. */
  private async handleQuestionDismiss(request: ApprovalRequest) {
    this.answeringId = request.id;
    this.answerError = null;
    try {
      const updated = await declineRequest(request.id);
      this.applyResolution(request.id, {
        status: 'declined',
        resolved_at: updated?.resolved_at ?? null,
      });
    } catch (error: any) {
      this.answerError = error?.message || 'Failed to dismiss question';
      console.error('Failed to dismiss question:', error);
    } finally {
      this.answeringId = null;
    }
  }

  render() {
    if (this.loading) {
      return html`
        <view-header headerText="Approval Requests" width="wide"></view-header>
        <div class="loading-container">
          <sl-spinner style="font-size: 3rem;"></sl-spinner>
        </div>
      `;
    }

    return html`
      <view-header
        headerText="Approval Requests"
        description="Tool calls that waited for a human decision — who approved, who denied, and how long it took. Choose which tools require approval in Tools."
        width="wide"
      >
        <sl-button href="/console/tools" size="small">
          <sl-icon slot="prefix" name="gear"></sl-icon>
          Configure Approvals
        </sl-button>
      </view-header>
      <div class="column-layout wide">
        <div class="main-column">
          <!-- Analytics Summary -->
          <div class="stats-grid">
            <div class="stat-card">
              <div class="stat-value primary">${this.stats.total}</div>
              <div class="stat-label">Total Requests</div>
            </div>
            <div class="stat-card">
              <div class="stat-value warning">${this.stats.pending}</div>
              <div class="stat-label">Pending</div>
              ${
                this.stats.pending > 0
                  ? html`<div class="stat-subtext">Awaiting review</div>`
                  : ''
              }
            </div>
            <div class="stat-card">
              <div class="stat-value success">${this.stats.approved}</div>
              <div class="stat-label">Approved</div>
            </div>
            <div class="stat-card">
              <div class="stat-value danger">${this.stats.declined}</div>
              <div class="stat-label">Declined</div>
            </div>
            <div class="stat-card">
              <div class="stat-value neutral">${this.stats.expired}</div>
              <div class="stat-label">Timed Out</div>
              ${
                this.stats.expired > 0
                  ? html`<div class="stat-subtext">No response in time</div>`
                  : ''
              }
            </div>
            <div class="stat-card">
              <div class="stat-value primary">
                ${
                  this.stats.avgResponseTimeMinutes > 0
                    ? this.stats.avgResponseTimeMinutes < 60
                      ? `${this.stats.avgResponseTimeMinutes}m`
                      : `${Math.round(this.stats.avgResponseTimeMinutes / 60)}h`
                    : '-'
                }
              </div>
              <div class="stat-label">Avg Response</div>
            </div>
          </div>

          <!-- Approval Rate -->
          ${
            this.stats.approved + this.stats.declined > 0
              ? html`
                  <sl-card style="margin-bottom: var(--sl-spacing-large);">
                    <div slot="header" class="chart-header">
                      <sl-icon name="pie-chart"></sl-icon>
                      Approval Rate
                    </div>
                    <div class="rate-labels">
                      <span
                        >Approved: ${this.stats.approved}
                        (${Math.round(this.stats.approvalRate)}%)</span
                      >
                      <span
                        >Declined: ${this.stats.declined}
                        (${Math.round(100 - this.stats.approvalRate)}%)</span
                      >
                    </div>
                    <sl-progress-bar
                      value="${this.stats.approvalRate}"
                      style="--indicator-color: var(--sl-color-success-600); --track-color: var(--sl-color-danger-200);"
                    ></sl-progress-bar>
                  </sl-card>
                `
              : ''
          }

          <!-- Filters -->
          <div class="filters-row">
            <sl-select
              label="Status"
              value=${this.statusFilter}
              @sl-change=${this.handleStatusFilterChange}
            >
              <sl-option value="all">All Statuses</sl-option>
              <sl-option value="pending">Pending</sl-option>
              <sl-option value="approved">Approved</sl-option>
              <sl-option value="declined">Declined</sl-option>
              <sl-option value="expired">Timed Out</sl-option>
              <sl-option value="cancelled">Cancelled</sl-option>
            </sl-select>

            <sl-select
              label="Tool"
              value=${this.toolFilter}
              @sl-change=${this.handleToolFilterChange}
            >
              <sl-option value="all">All Tools</sl-option>
              ${this.getUniqueTools().map(
                (tool) => html`<sl-option value=${tool}>${tool}</sl-option>`
              )}
            </sl-select>

            <sl-input
              label="Search"
              placeholder="Search by tool, execution ID, or content..."
              clearable
              @sl-input=${this.handleSearchInput}
            >
              <sl-icon name="search" slot="prefix"></sl-icon>
            </sl-input>
          </div>

          <!-- Results count -->
          <div
            style="margin-bottom: var(--sl-spacing-medium); color: var(--sl-color-neutral-600); font-size: var(--sl-font-size-small);"
          >
            Showing ${this.filteredRequests.length} of
            ${this.approvalRequests.length} requests
          </div>

          <!-- Approval Requests List -->
          ${
            this.filteredRequests.length === 0
              ? html`
                  <div class="empty-state">
                    <sl-icon name="inbox"></sl-icon>
                    <p>
                      ${
                        this.approvalRequests.length === 0
                          ? 'No approval requests yet. Configure tools to require approval in the Tools section.'
                          : 'No requests match your filters.'
                      }
                    </p>
                    ${
                      this.approvalRequests.length === 0
                        ? html`<sl-button href="/console/tools">
                            <sl-icon slot="prefix" name="gear"></sl-icon>
                            Configure Tools
                          </sl-button>`
                        : ''
                    }
                  </div>
                `
              : html`
                  <div class="approval-list">
                    ${this.filteredRequests.map(
                      (request) => html`
                        <div
                          class="approval-item ${request.status} ${
                            this.isQuestion(request) ? 'question' : ''
                          }"
                        >
                          <div class="approval-row">
                            <div class="approval-info">
                              <div class="approval-tool">
                                <sl-icon
                                  name=${
                                    this.isQuestion(request)
                                      ? 'chat-left-quote'
                                      : 'tools'
                                  }
                                ></sl-icon>
                                ${
                                  this.isQuestion(request)
                                    ? html`<span
                                        style="font-weight: 500; max-width: 520px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;"
                                        title=${this.questionText(request)}
                                        >${this.questionText(request)}</span
                                      >`
                                    : request.summary
                                      ? html`<span
                                          style="font-weight: 500; max-width: 520px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;"
                                          title=${request.summary}
                                          >${
                                            request.summary.length > 120
                                              ? `${request.summary.substring(0, 120)}…`
                                              : request.summary
                                          }</span
                                        >`
                                      : html`<code>${request.tool_name}</code>`
                                }
                                <sl-tag
                                  size="small"
                                  variant=${this.getStatusVariant(request.status)}
                                >
                                  <sl-icon
                                    name=${this.getStatusIcon(request.status)}
                                    style="margin-right: 4px;"
                                  ></sl-icon>
                                  ${
                                    request.status === 'expired'
                                      ? 'timed out'
                                      : request.status
                                  }
                                </sl-tag>
                                ${
                                  request.auto_approved_reason
                                    ? html`<sl-tooltip
                                        content="Auto-approved by a time-boxed bypass. No person reviewed this call."
                                      >
                                        <sl-tag size="small" variant="warning">
                                          <sl-icon
                                            name="exclamation-triangle"
                                            style="margin-right: 4px;"
                                          ></sl-icon>
                                          not reviewed
                                        </sl-tag>
                                      </sl-tooltip>`
                                    : ''
                                }
                              </div>
                              ${
                                request.summary
                                  ? html`
                                      <div
                                        class="approval-meta"
                                        style="margin-top: 2px;"
                                      >
                                        <span class="approval-meta-item">
                                          <code style="font-size: 0.8em;"
                                            >${request.tool_name}</code
                                          >
                                        </span>
                                      </div>
                                    `
                                  : ''
                              }
                              <div class="approval-meta">
                                <sl-tooltip
                                  content=${this.formatFullDate(
                                    request.requested_at
                                  )}
                                >
                                  <span class="approval-meta-item">
                                    <sl-icon name="clock"></sl-icon>
                                    ${this.formatDate(request.requested_at)}
                                  </span>
                                </sl-tooltip>
                                ${
                                  request.execution_id
                                    ? html`
                                        <span class="approval-meta-item">
                                          <sl-icon name="diagram-3"></sl-icon>
                                          <a
                                            href="/console/flows/executions/${request.execution_id}"
                                            >Flow Execution</a
                                          >
                                        </span>
                                      `
                                    : ''
                                }
                                ${
                                  request.resolved_at
                                    ? html`
                                        <sl-tooltip
                                          content="Resolved: ${this.formatFullDate(
                                            request.resolved_at
                                          )}"
                                        >
                                          <span class="approval-meta-item">
                                            <sl-icon
                                              name="check2-square"
                                            ></sl-icon>
                                            Resolved
                                            ${this.formatDate(request.resolved_at)}
                                          </span>
                                        </sl-tooltip>
                                      `
                                    : ''
                                }
                                ${
                                  request.expires_at &&
                                  request.status === 'pending'
                                    ? html`
                                        <sl-tooltip
                                          content="Expires: ${this.formatFullDate(
                                            request.expires_at
                                          )}"
                                        >
                                          <span
                                            class="approval-meta-item"
                                            style="color: var(--sl-color-warning-600);"
                                          >
                                            <sl-icon name="hourglass"></sl-icon>
                                            Expires
                                            ${this.formatExpiryDate(
                                              request.expires_at
                                            )}
                                          </span>
                                        </sl-tooltip>
                                      `
                                    : ''
                                }
                              </div>
                              ${
                                !request.summary && request.agent_reasoning
                                  ? html`
                                      <div
                                        style="font-size: var(--sl-font-size-small); color: var(--sl-color-neutral-700); margin-top: var(--sl-spacing-2x-small); font-style: italic; max-width: 600px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;"
                                      >
                                        "${request.agent_reasoning.substring(
                                          0,
                                          100
                                        )}${
                                          request.agent_reasoning.length > 100
                                            ? '...'
                                            : ''
                                        }"
                                      </div>
                                    `
                                  : ''
                              }
                            </div>
                            <div class="approval-actions">
                              <sl-button
                                size="small"
                                variant=${
                                  request.status === 'pending'
                                    ? 'primary'
                                    : 'default'
                                }
                                href="/console/approval/${request.id}"
                              >
                                ${request.status === 'pending' ? 'Review' : 'View'}
                              </sl-button>
                            </div>
                          </div>
                          ${
                            this.isQuestion(request) &&
                            request.status === 'pending'
                              ? html`
                                  <question-answer-panel
                                    compact
                                    .question=${this.questionText(request)}
                                    .options=${request.question_options ?? []}
                                    .allowFreeText=${
                                      request.allow_free_text === true
                                    }
                                    .submitting=${
                                      this.answeringId === request.id
                                    }
                                    @question-answer=${(
                                      e: CustomEvent<QuestionAnswerDetail>
                                    ) => this.handleQuestionAnswer(request, e)}
                                    @question-dismiss=${() =>
                                      this.handleQuestionDismiss(request)}
                                  ></question-answer-panel>
                                  ${
                                    this.answerError
                                      ? html`<div class="answer-error">
                                          ${this.answerError}
                                        </div>`
                                      : ''
                                  }
                                `
                              : ''
                          }
                        </div>
                      `
                    )}
                  </div>
                `
          }
        </div>
      </div>
    `;
  }
}
