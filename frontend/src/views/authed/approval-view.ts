import { html, css, unsafeCSS } from 'lit';
import { customElement, state, property } from 'lit/decorators.js';
import { AuthedElement } from '../../api';
import type { ApprovalDecisionOptions, ApprovalRequest } from '../../types';
import { unifiedWebSocketManager } from '../../services/unified-websocket-manager';
import {
  formatApprovalRequester,
  formatApprovalSource,
  getApprovalSource,
  withoutApprovalMetadata,
} from '../../utils/approval-identity';
import {
  approvalStatusLabel,
  isExpiringSoon,
  isUnexpiredPendingRequest,
  millisUntilExpiry,
  normalizeApprovalRequest,
} from '../../utils/approvals';
import { confirmDialog, showToast } from '../../components/confirm-dialog';
import '../../components/question-answer-panel';
import '../../components/approval-rule-context-block';
import type { QuestionAnswerDetail } from '../../components/question-answer-panel';
import consoleStyles from '../../styles/console-styles.css?inline';
import '@shoelace-style/shoelace/dist/components/card/card.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/textarea/textarea.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/divider/divider.js';

@customElement('approval-view')
export class ApprovalView extends AuthedElement {
  @property({ type: String })
  requestId: string = '';

  @state()
  private approvalRequest: ApprovalRequest | null = null;

  @state()
  private loading = true;

  @state()
  private error: string | null = null;

  @state()
  private comment = '';

  @state()
  private submitting = false;

  /**
   * Ticks once a second while the request is live, so the countdown moves and
   * an expiry that passes while the page is open flips the page to timed out
   * instead of leaving dead buttons on screen.
   */
  @state()
  private nowMs = Date.now();

  /** Set once this page has taken a decision, to show where to go next. */
  @state()
  private decisionTaken = false;

  /** Other requests still waiting for this operator, fetched after a decision. */
  @state()
  private waitingNext: ApprovalRequest[] = [];

  private unsubscribe?: () => void;
  private tickTimer?: ReturnType<typeof setInterval>;

  static styles = [
    unsafeCSS(consoleStyles),
    css`
      :host {
        display: block;
        padding: 2rem;
        max-width: 840px;
        margin: 0 auto;
      }

      .header {
        margin-bottom: 2rem;
      }

      .header h1 {
        margin: 0 0 0.5rem 0;
        font-size: 1.75rem;
        display: flex;
        align-items: center;
        flex-wrap: wrap;
        gap: 0.75rem;
      }

      .header p {
        margin: 0;
        color: var(--sl-color-neutral-600);
      }

      .status-badge {
        font-size: 0.875rem;
      }

      .content-section {
        margin-bottom: 1.5rem;
      }

      .content-section h2 {
        font-size: 1.125rem;
        margin: 0 0 0.75rem 0;
        font-weight: 600;
      }

      .info-grid {
        display: grid;
        grid-template-columns: 150px 1fr;
        gap: 0.75rem;
        margin-bottom: 1rem;
      }

      .info-label {
        font-weight: 600;
        color: var(--sl-color-neutral-700);
      }

      .info-value {
        color: var(--sl-color-neutral-900);
      }

      .code-block {
        background: var(--sl-color-neutral-100);
        padding: 1rem;
        border-radius: 4px;
        overflow-x: auto;
        font-family: monospace;
        font-size: 0.875rem;
        white-space: pre-wrap;
        word-break: break-word;
      }

      .reasoning-text {
        background: var(--sl-color-primary-50);
        border-left: 3px solid var(--sl-color-primary-600);
        padding: 1rem;
        border-radius: 4px;
        margin-top: 0.5rem;
        color: var(--sl-color-neutral-900);
        line-height: 1.6;
      }

      .actions {
        display: flex;
        gap: 1rem;
        margin-top: 2rem;
      }

      .actions sl-button {
        flex: 1;
      }

      .comment-section {
        margin-top: 1.5rem;
      }

      .loading-state {
        display: flex;
        justify-content: center;
        align-items: center;
        padding: 3rem;
      }

      .resolved-info {
        margin-top: 1rem;
        padding: 1rem;
        background: var(--sl-color-neutral-50);
        border-radius: 4px;
      }

      .resolved-info h3 {
        margin: 0 0 0.5rem 0;
        font-size: 1rem;
        font-weight: 600;
      }

      .metadata {
        font-size: 0.875rem;
        color: var(--sl-color-neutral-600);
        margin-top: 1rem;
      }

      /* The decision travels with the page: whatever the operator has scrolled
       to, the bar with the countdown and the two buttons is on screen. */
      .decision-bar {
        position: sticky;
        bottom: 0;
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        gap: var(--sl-spacing-medium);
        margin: var(--sl-spacing-large) -2rem 0 -2rem;
        padding: var(--sl-spacing-medium) 2rem;
        background: var(--console-surface, var(--sl-color-neutral-0));
        border-top: 1px solid
          var(--console-hairline, var(--sl-color-neutral-200));
        z-index: 1;
      }

      .decision-summary {
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-2x-small);
        flex: 1 1 260px;
        min-width: 0;
      }

      .decision-title {
        font-weight: 600;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }

      .decision-title code {
        font-family: var(--sl-font-mono);
        font-size: var(--sl-font-size-small);
      }

      .decision-paused {
        font-size: var(--sl-font-size-small);
        color: var(--sl-color-neutral-600);
      }

      .decision-comment {
        flex: 1 1 200px;
        min-width: 160px;
      }

      .decision-buttons {
        display: flex;
        align-items: center;
        gap: var(--sl-spacing-small);
      }

      /* Destructive last, after a large gap, never beside the everyday action. */
      .decision-buttons .deny {
        margin-left: var(--sl-spacing-large);
      }

      kbd {
        font-family: var(--sl-font-mono);
        font-size: var(--sl-font-size-x-small);
        border: 1px solid var(--console-hairline, var(--sl-color-neutral-200));
        border-radius: var(--sl-border-radius-small);
        padding: 0 4px;
        margin-left: 6px;
        color: inherit;
      }

      .post-decision {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        gap: var(--sl-spacing-small);
        margin-top: var(--sl-spacing-medium);
        font-size: var(--sl-font-size-small);
      }

      .post-decision .separator,
      .post-decision .muted {
        color: var(--sl-color-neutral-600);
      }

      @media (max-width: 640px) {
        :host {
          padding: 1rem;
        }

        .decision-bar {
          margin-left: -1rem;
          margin-right: -1rem;
          padding-left: 1rem;
          padding-right: 1rem;
        }

        .decision-buttons {
          flex: 1 1 100%;
        }

        .decision-buttons sl-button {
          flex: 1;
        }

        .decision-buttons .deny {
          margin-left: var(--sl-spacing-medium);
        }
      }
    `,
  ];

  async connectedCallback() {
    super.connectedCallback();
    // Extract requestId from URL if not set
    if (!this.requestId) {
      const path = window.location.pathname;
      const match = path.match(/\/console\/approval\/([^/?]+)/);
      if (match) {
        this.requestId = match[1];
      }
    }
    await this.loadApprovalRequest();

    // Connect to WebSocket for real-time approval updates
    this.connectWebSocket();

    // The decision keys work wherever focus is, so the listener sits on the
    // document rather than on the host: a key pressed before anything inside
    // the page is focused never reaches the host element.
    document.addEventListener('keydown', this.handleKeyDown);
    this.startTicking();
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    // Disconnect from WebSocket when view is destroyed
    this.unsubscribe?.();
    document.removeEventListener('keydown', this.handleKeyDown);
    this.stopTicking();
  }

  private startTicking() {
    if (this.tickTimer) return;
    this.tickTimer = setInterval(() => {
      this.nowMs = Date.now();
      if (!this.isLive) this.stopTicking();
    }, 1000);
  }

  private stopTicking() {
    if (this.tickTimer) {
      clearInterval(this.tickTimer);
      this.tickTimer = undefined;
    }
  }

  /** True while the request is pending and its expiry is still ahead. */
  private get isLive(): boolean {
    return (
      !!this.approvalRequest &&
      isUnexpiredPendingRequest(this.approvalRequest, this.nowMs)
    );
  }

  private handleKeyDown = (event: KeyboardEvent) => {
    if (!this.isLive || this.isQuestion || this.submitting) return;
    if (event.metaKey || event.ctrlKey || event.altKey) return;
    const target = event.composedPath()[0] as HTMLElement | undefined;
    const tag = target?.tagName?.toLowerCase() ?? '';
    if (
      ['input', 'textarea', 'select', 'sl-input', 'sl-textarea'].includes(
        tag
      ) ||
      target?.isContentEditable
    ) {
      return;
    }
    const key = event.key.toLowerCase();
    if (key === 'a') {
      event.preventDefault();
      void this.handleApprove();
    } else if (key === 'd') {
      event.preventDefault();
      void this.handleDeny();
    }
  };

  private connectWebSocket() {
    this.unsubscribe = unifiedWebSocketManager.subscribe(
      'approvals',
      (message: any) => this.handleWebSocketMessage(message),
      // Filter to only receive messages for this approval request
      (message: any) => message.request_id === this.requestId
    );

    // Track connection state
    unifiedWebSocketManager.onStateChange((state) => {
      console.log(`Approval view WebSocket state: ${state}`);
    });
  }

  private handleWebSocketMessage(message: any) {
    // Only process updates for the current approval request
    if (
      message.approval_request_id === this.requestId &&
      this.approvalRequest
    ) {
      console.log('Received approval update:', message);

      // Update the status
      this.approvalRequest = {
        ...this.approvalRequest,
        status: message.status,
        resolved_at: message.resolved_at || this.approvalRequest.resolved_at,
      };

      // Report the change the way the rest of the console reports results.
      if (message.type === 'approval_approved') {
        showToast('This request was approved.', 'success');
      } else if (message.type === 'approval_declined') {
        showToast('This request was denied.', 'neutral');
      } else if (message.type === 'approval_expired') {
        showToast('This request timed out.', 'warning');
      }
    }
  }

  private async loadApprovalRequest() {
    this.loading = true;
    this.error = null;

    try {
      const data = await this.fetchData(
        `/api/v1/approval-requests/${this.requestId}`
      );
      if (data) {
        // A request the sweeper has not caught up with yet is still `pending`
        // in the database long after its expiry. Read the clock here so the
        // page never offers a decision that the backend would reject.
        this.approvalRequest = normalizeApprovalRequest(data);
      } else {
        this.error = 'Approval request not found';
      }
    } catch (err: any) {
      this.error = err.message || 'Failed to load approval request';
      console.error('Error loading approval request:', err);
    } finally {
      this.loading = false;
    }
  }

  /** True when the request is an agent question (`ask_user`), not a tool call. */
  private get isQuestion(): boolean {
    return this.approvalRequest?.is_question === true;
  }

  private get questionText(): string {
    const request = this.approvalRequest;
    if (!request) return '';
    return request.question || request.summary || request.tool_name;
  }

  private async submitDecision(
    action: 'approve' | 'decline',
    options: ApprovalDecisionOptions,
    successMessage: string
  ) {
    if (!this.approvalRequest) return;

    this.submitting = true;

    const body: Record<string, unknown> = {
      approved: action === 'approve',
      comment: options.comment || null,
    };
    if (options.selected_option) {
      body.selected_option = options.selected_option;
    }
    if (options.answer_text) {
      body.answer_text = options.answer_text;
    }

    try {
      const response = await fetch(
        `/api/v1/approval-requests/${this.requestId}/${action}`,
        {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            Authorization: `Bearer ${localStorage.getItem('accessToken')}`,
          },
          body: JSON.stringify(body),
        }
      );

      if (!response.ok) {
        const error = await response.json();
        throw new Error(error.detail || `Failed to ${action} request`);
      }

      const updated = await response.json();
      this.approvalRequest = updated;
      this.comment = '';
      this.decisionTaken = true;
      showToast(successMessage, action === 'approve' ? 'success' : 'neutral');
      await this.loadWaitingNext();
    } catch (err: any) {
      showToast(err.message || `Failed to ${action} request`, 'danger');
    } finally {
      this.submitting = false;
    }
  }

  /**
   * After a decision the page has nothing left to do, so it says where the
   * work is: back to the list, or straight into the next request waiting.
   */
  private async loadWaitingNext() {
    try {
      const data = await this.fetchData(
        '/api/v1/approval-requests?status=pending&limit=100'
      );
      if (Array.isArray(data)) {
        this.waitingNext = (data as ApprovalRequest[]).filter(
          (request) =>
            request.id !== this.requestId &&
            isUnexpiredPendingRequest(request, Date.now())
        );
      }
    } catch (error) {
      // The decision landed; a missing queue count is not worth an error.
      console.error('Failed to load the waiting queue:', error);
    }
  }

  private async handleApprove() {
    await this.submitDecision(
      'approve',
      { comment: this.comment },
      'Request approved.'
    );
  }

  /** Denying stops the agent, so it confirms first (DESIGN.md destructive). */
  private async handleDeny() {
    const request = this.approvalRequest;
    if (!request) return;
    const confirmed = await confirmDialog({
      title: 'Deny this request?',
      message: `${request.tool_name} will not run.`,
      detail: `${formatApprovalRequester(
        request.managed_agent_name,
        request.tool_args
      )} is told no and continues without it.`,
      confirmLabel: 'Deny',
      variant: 'danger',
    });
    if (!confirmed) return;
    await this.submitDecision(
      'decline',
      { comment: this.comment },
      'Request denied.'
    );
  }

  /** An answered question is submitted as an approve with the answer attached. */
  private async handleQuestionAnswer(e: CustomEvent<QuestionAnswerDetail>) {
    const { selectedOption, answerText } = e.detail;
    await this.submitDecision(
      'approve',
      {
        selected_option: selectedOption ?? null,
        answer_text: answerText ?? null,
      },
      'Answer sent to the agent.'
    );
  }

  /** Dismissing a question declines it, exactly as the mobile apps do. */
  private async handleQuestionDismiss() {
    await this.submitDecision('decline', {}, 'Question dismissed.');
  }

  private formatDate(dateStr: string): string {
    const date = new Date(dateStr);
    return date.toLocaleString('en-US', {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
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

  /** "expires in 4m 12s" while it matters, coarser once it is hours away. */
  private formatCountdown(request: ApprovalRequest): string | null {
    const remaining = millisUntilExpiry(request, this.nowMs);
    if (remaining === null) return null;
    if (remaining <= 0) return 'expired';
    const totalSeconds = Math.floor(remaining / 1000);
    if (totalSeconds < 60) return `expires in ${totalSeconds}s`;
    const minutes = Math.floor(totalSeconds / 60);
    if (minutes < 60) {
      return `expires in ${minutes}m ${totalSeconds % 60}s`;
    }
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `expires in ${hours}h ${minutes % 60}m`;
    return `expires in ${Math.floor(hours / 24)}d`;
  }

  /** The one-line summary the decision bar leads with. */
  private decisionSummary(request: ApprovalRequest): string {
    const args = withoutApprovalMetadata(request.tool_args);
    const firstValue = Object.values(args).find(
      (value) => typeof value === 'string' && value.trim().length > 0
    ) as string | undefined;
    return request.summary?.trim() || firstValue?.trim() || request.tool_name;
  }

  private formatToolArgs(args: Record<string, any>): string {
    // Convert args to JSON string with proper formatting
    const jsonStr = JSON.stringify(args, null, 2);

    // Replace escaped newlines with actual newlines for better readability
    // This handles strings that contain \n, \r\n, etc.
    return jsonStr
      .replace(/\\n/g, '\n')
      .replace(/\\r/g, '\r')
      .replace(/\\t/g, '\t');
  }

  render() {
    if (this.loading) {
      return html`
        <div class="loading-state">
          <sl-spinner style="font-size: 3rem;"></sl-spinner>
        </div>
      `;
    }

    if (this.error) {
      return html`
        <sl-alert variant="danger" open>
          <sl-icon slot="icon" name="exclamation-octagon"></sl-icon>
          <strong>Error:</strong> ${this.error}
        </sl-alert>
      `;
    }

    if (!this.approvalRequest) {
      return html`
        <sl-alert variant="warning" open>
          <sl-icon slot="icon" name="exclamation-triangle"></sl-icon>
          <strong>Not found:</strong> Approval request not found
        </sl-alert>
      `;
    }

    // A request whose expiry passed is timed out, whatever the record says:
    // the buttons would post a decision the backend refuses.
    const request = normalizeApprovalRequest(this.approvalRequest, this.nowMs);
    const isPending = isUnexpiredPendingRequest(request, this.nowMs);
    const isResolved = [
      'approved',
      'declined',
      'expired',
      'cancelled',
    ].includes(request.status);
    const displayStatus = approvalStatusLabel(request.status);
    const countdown = isPending ? this.formatCountdown(request) : null;
    const expiringSoon = isPending && isExpiringSoon(request, this.nowMs);

    const toolArgs = this.formatToolArgs(
      withoutApprovalMetadata(request.tool_args)
    );
    const source = formatApprovalSource(getApprovalSource(request.tool_args));
    const requester = formatApprovalRequester(
      request.managed_agent_name,
      request.tool_args
    );

    const isQuestion = this.isQuestion;

    return html`
      <div class="header">
        <h1>
          <sl-icon
            name=${isQuestion ? 'chat-left-quote' : 'shield-check'}
          ></sl-icon>
          ${isQuestion ? 'Agent question' : 'Tool execution approval'}
          <sl-badge
            pill
            class="chip status-badge"
            variant=${this.getStatusVariant(request.status)}
          >
            ${displayStatus}
          </sl-badge>
          ${
            countdown
              ? html`<sl-badge
                  pill
                  class="chip status-badge"
                  variant=${expiringSoon ? 'warning' : 'neutral'}
                >
                  <sl-icon name="hourglass"></sl-icon>
                  ${countdown}
                </sl-badge>`
              : ''
          }
        </h1>
        <p>
          ${
            isQuestion
              ? `${requester} is waiting on your answer before it continues`
              : `Review ${requester}'s tool execution request`
          }
        </p>
      </div>

      <sl-card>
        ${
          isQuestion && isPending
            ? html`
                <div class="content-section">
                  <question-answer-panel
                    .question=${this.questionText}
                    .options=${request.question_options ?? []}
                    .allowFreeText=${request.allow_free_text === true}
                    .submitting=${this.submitting}
                    @question-answer=${this.handleQuestionAnswer}
                    @question-dismiss=${this.handleQuestionDismiss}
                  ></question-answer-panel>
                </div>
              `
            : ''
        }
        ${
          request.summary && !isQuestion
            ? html`
                <div class="content-section">
                  <h2>Request</h2>
                  <div class="reasoning-text">${request.summary}</div>
                </div>
              `
            : ''
        }
        ${
          isQuestion && !isPending
            ? html`
                <div class="content-section">
                  <h2>Question</h2>
                  <div class="reasoning-text">${this.questionText}</div>
                </div>
              `
            : ''
        }
        <div class="content-section">
          <h2>Tool information</h2>
          <div class="info-grid">
            <div class="info-label">Requested by:</div>
            <div class="info-value">
              <strong>${requester}</strong>
            </div>

            ${
              source
                ? html`
                    <div class="info-label">Adapter:</div>
                    <div class="info-value">${source}</div>
                  `
                : ''
            }

            <div class="info-label">Tool name:</div>
            <div class="info-value">
              <strong>${request.tool_name}</strong>
            </div>

            <div class="info-label">Request ID:</div>
            <div class="info-value">
              <code style="font-size: 0.75rem;">${request.id}</code>
            </div>

            <div class="info-label">Requested:</div>
            <div class="info-value">
              ${this.formatDate(request.requested_at)}
            </div>

            ${
              request.expires_at
                ? html`
                    <div class="info-label">Expires:</div>
                    <div class="info-value">
                      ${this.formatDate(request.expires_at)}
                    </div>
                  `
                : ''
            }
            ${
              request.execution_id
                ? html`
                    <div class="info-label">Execution ID:</div>
                    <div class="info-value">
                      <code style="font-size: 0.75rem;"
                        >${request.execution_id}</code
                      >
                    </div>
                  `
                : ''
            }
          </div>
        </div>

        ${
          request.rule_context
            ? html`
                <div class="content-section">
                  <approval-rule-context-block
                    .ruleContext=${request.rule_context}
                    .toolName=${request.tool_name}
                  ></approval-rule-context-block>
                </div>
              `
            : ''
        }
        ${
          request.agent_reasoning
            ? html`
                <div class="content-section">
                  <h2>Agent reasoning</h2>
                  <div class="reasoning-text">${request.agent_reasoning}</div>
                </div>
              `
            : ''
        }

        <div class="content-section">
          <h2>Tool arguments</h2>
          <div class="code-block">${toolArgs}</div>
        </div>

        ${
          isResolved
            ? html`
                <div class="resolved-info">
                  <h3>${approvalStatusLabel(request.status)}</h3>
                  ${
                    request.resolved_at
                      ? html`<p>
                          Resolved at: ${this.formatDate(request.resolved_at)}
                        </p>`
                      : ''
                  }
                  ${
                    request.approver_comment
                      ? html`
                          <p><strong>Comment:</strong></p>
                          <div class="code-block">
                            ${request.approver_comment}
                          </div>
                        `
                      : ''
                  }
                </div>
              `
            : ''
        }

        <div class="metadata">
          <sl-icon name="info-circle"></sl-icon>
          ${
            isQuestion
              ? 'An automated agent asked this question and is paused until it gets an answer.'
              : 'This approval request was generated by an automated agent and requires human review before the tool can be executed.'
          }
        </div>
      </sl-card>

      ${this.decisionTaken ? this.renderPostDecision() : ''}
      ${
        isPending && !isQuestion
          ? this.renderDecisionBar(request, countdown, expiringSoon)
          : ''
      }
    `;
  }

  /** The decision, always on screen, whatever the operator has scrolled to. */
  private renderDecisionBar(
    request: ApprovalRequest,
    countdown: string | null,
    expiringSoon: boolean
  ) {
    const requester = formatApprovalRequester(
      request.managed_agent_name,
      request.tool_args
    );
    return html`
      <div class="decision-bar">
        <div class="decision-summary">
          <div class="decision-title" title=${this.decisionSummary(request)}>
            <code>${request.tool_name}</code> ${this.decisionSummary(request)}
          </div>
          <div class="decision-paused">
            ${requester} is paused until you decide
          </div>
          ${
            countdown
              ? html`<sl-badge
                  pill
                  class="chip countdown-chip"
                  variant=${expiringSoon ? 'warning' : 'neutral'}
                >
                  <sl-icon name="hourglass"></sl-icon>
                  ${countdown}
                </sl-badge>`
              : ''
          }
        </div>
        <sl-input
          class="decision-comment"
          size="small"
          placeholder="Comment (optional)"
          .value=${this.comment}
          @sl-input=${(e: any) => (this.comment = e.target.value)}
          ?disabled=${this.submitting}
        ></sl-input>
        <div class="decision-buttons">
          <sl-button
            class="approve"
            variant="success"
            @click=${this.handleApprove}
            ?loading=${this.submitting}
            ?disabled=${this.submitting}
          >
            <sl-icon slot="prefix" name="check-circle"></sl-icon>
            Approve<kbd>A</kbd>
          </sl-button>
          <sl-button
            class="deny"
            variant="danger"
            outline
            @click=${this.handleDeny}
            ?disabled=${this.submitting}
          >
            <sl-icon slot="prefix" name="x-circle"></sl-icon>
            Deny<kbd>D</kbd>
          </sl-button>
        </div>
      </div>
    `;
  }

  /** Where the work is now that this page has none left. */
  private renderPostDecision() {
    const waiting = this.waitingNext;
    return html`
      <div class="post-decision">
        <a href="/console/approvals">Back to approvals</a>
        <span class="separator">·</span>
        ${
          waiting.length > 0
            ? html`<a href="/console/approval/${waiting[0].id}"
                >Next waiting (${waiting.length})</a
              >`
            : html`<span class="muted">Nothing else is waiting for you</span>`
        }
      </div>
    `;
  }
}
