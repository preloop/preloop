import { html, css } from 'lit';
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
import '../../components/question-answer-panel';
import '../../components/approval-rule-context-block';
import type { QuestionAnswerDetail } from '../../components/question-answer-panel';
import '@shoelace-style/shoelace/dist/components/card/card.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/textarea/textarea.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/divider/divider.js';

/** One workflow-history entry as returned by the history API. */
export interface ApprovalTimelineEntry {
  event_type: string;
  detail: string;
  comment?: string | null;
  actor_email?: string | null;
  timestamp: string;
}

@customElement('approval-view')
export class ApprovalView extends AuthedElement {
  @property({ type: String })
  requestId: string = '';

  @state()
  private approvalRequest: ApprovalRequest | null = null;

  @state()
  private history: ApprovalTimelineEntry[] = [];

  @state()
  private loading = true;

  @state()
  private error: string | null = null;

  /**
   * True when the request is rendered from the public token payload instead
   * of the authenticated API (viewer is signed in but not a member of the
   * request's account, e.g. an escalation recipient). Decisions then go
   * through the token endpoint.
   */
  @state()
  private publicOnly = false;

  @state()
  private comment = '';

  @state()
  private submitting = false;

  @state()
  private actionResult: { type: 'success' | 'error'; message: string } | null =
    null;

  private unsubscribe?: () => void;

  static styles = css`
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

    .timeline {
      list-style: none;
      margin: 0;
      padding: 0;
    }

    .timeline li {
      display: flex;
      gap: 0.75rem;
      padding: 0.5rem 0;
      border-bottom: 1px solid var(--sl-color-neutral-200);
    }

    .timeline li:last-child {
      border-bottom: none;
    }

    .timeline-icon {
      flex: none;
      color: var(--sl-color-neutral-500);
      font-size: 1rem;
      margin-top: 0.1rem;
    }

    .timeline-body {
      flex: 1;
    }

    .timeline-detail {
      margin: 0;
      color: var(--sl-color-neutral-900);
      line-height: 1.4;
    }

    .timeline-meta {
      margin: 0.15rem 0 0 0;
      font-size: 0.8rem;
      color: var(--sl-color-neutral-600);
    }

    .timeline-comment {
      margin: 0.35rem 0 0 0;
      padding: 0.5rem;
      background: var(--sl-color-neutral-100);
      border-radius: 4px;
      font-size: 0.85rem;
      white-space: pre-wrap;
      word-break: break-word;
    }

    .expired-banner {
      margin-bottom: 1.5rem;
    }
  `;

  async connectedCallback() {
    super.connectedCallback();
    // Extract requestId from URL if not set. Both the console route
    // (/console/approval/:id) and the legacy top-level link (/approval/:id,
    // which the backend redirects here) must resolve (issue #335).
    if (!this.requestId) {
      const path = window.location.pathname;
      const match = path.match(/\/(?:console\/)?approval\/([^/?]+)/);
      if (match) {
        this.requestId = match[1];
      }
    }
    await this.loadApprovalRequest();

    // Connect to WebSocket for real-time approval updates
    this.connectWebSocket();
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    // Disconnect from WebSocket when view is destroyed
    this.unsubscribe?.();
  }

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

      // Show notification based on event type
      if (message.type === 'approval_approved') {
        this.actionResult = {
          type: 'success',
          message: 'This request was approved!',
        };
      } else if (message.type === 'approval_declined') {
        this.actionResult = {
          type: 'error',
          message: 'This request was declined.',
        };
      } else if (message.type === 'approval_expired') {
        this.actionResult = {
          type: 'error',
          message: 'This request has expired.',
        };
      }

      // A state transition just landed; refresh the workflow timeline.
      this.loadHistory();
    }
  }

  private async loadApprovalRequest() {
    this.loading = true;
    this.error = null;
    this.publicOnly = false;

    try {
      const data = await this.fetchData(
        `/api/v1/approval-requests/${this.requestId}`
      );
      if (data) {
        this.approvalRequest = data;
        await this.loadHistory();
        return;
      }
      // Authenticated read failed (not a member of the account or request
      // gone). Fall back to the public token payload when the link carried
      // one, so escalation recipients can still see the request (issue #335).
      if (await this.loadPublicRequest()) {
        return;
      }
      this.error = 'Approval request not found';
    } catch (err: any) {
      this.error = err.message || 'Failed to load approval request';
      console.error('Error loading approval request:', err);
    } finally {
      this.loading = false;
    }
  }

  /** Load the request via the public token endpoint. True when it worked. */
  private async loadPublicRequest(): Promise<boolean> {
    const token = new URLSearchParams(window.location.search).get('token');
    if (!token) {
      return false;
    }
    try {
      const response = await fetch(
        `/approval/${this.requestId}/data?token=${encodeURIComponent(token)}`
      );
      if (!response.ok) {
        return false;
      }
      const data = await response.json();
      // The public payload is a subset of ApprovalRequest; fill the gaps the
      // render path reads so the card renders without blowing up.
      this.approvalRequest = {
        ...data,
        tool_configuration_id: data.tool_configuration_id ?? '',
        approval_workflow_id: data.approval_workflow_id ?? '',
        account_id: data.account_id ?? '',
      } as ApprovalRequest;
      this.history = Array.isArray(data.history) ? data.history : [];
      this.publicOnly = true;
      return true;
    } catch (err) {
      console.error('Public token fallback failed:', err);
      return false;
    }
  }

  /** Fetch the workflow-history timeline (authenticated reads only). */
  private async loadHistory() {
    try {
      const events = await this.fetchData(
        `/api/v1/approval-requests/${this.requestId}/history`
      );
      if (Array.isArray(events)) {
        this.history = events as ApprovalTimelineEntry[];
      }
    } catch (err) {
      // Timeline is supplementary; the request card must still render.
      console.error('Error loading approval history:', err);
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
    this.actionResult = null;

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

    // Public-token rendering (viewer outside the account): the decision goes
    // through the token endpoint, which authorizes exactly this request.
    if (this.publicOnly) {
      const token = new URLSearchParams(window.location.search).get('token');
      try {
        const response = await fetch(
          `/approval/${this.requestId}/decide?token=${encodeURIComponent(
            token || ''
          )}`,
          {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              action: action === 'approve' ? 'approve' : 'decline',
              comment: options.comment || null,
            }),
          }
        );
        if (!response.ok) {
          const error = await response.json();
          throw new Error(error.detail || `Failed to ${action} request`);
        }
        const updated = await response.json();
        this.approvalRequest = {
          ...this.approvalRequest!,
          status: updated.status,
          resolved_at: updated.resolved_at ?? this.approvalRequest!.resolved_at,
        };
        this.history = Array.isArray(updated.history)
          ? updated.history
          : this.history;
        this.actionResult = { type: 'success', message: successMessage };
        this.comment = '';
      } catch (err: any) {
        this.actionResult = {
          type: 'error',
          message: err.message || `Failed to ${action} request`,
        };
      } finally {
        this.submitting = false;
      }
      return;
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
      this.actionResult = { type: 'success', message: successMessage };
      this.comment = '';
      this.loadHistory();
    } catch (err: any) {
      this.actionResult = {
        type: 'error',
        message: err.message || `Failed to ${action} request`,
      };
    } finally {
      this.submitting = false;
    }
  }

  private async handleApprove() {
    await this.submitDecision(
      'approve',
      { comment: this.comment },
      'Request approved successfully!'
    );
  }

  private async handleDecline() {
    await this.submitDecision(
      'decline',
      { comment: this.comment },
      'Request declined.'
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

  private timelineIcon(event: ApprovalTimelineEntry): string {
    switch (event.event_type) {
      case 'approval_requested':
        return 'shield-check';
      case 'notification_sent':
        return 'send';
      case 'viewed':
        return 'eye';
      case 'vote_received':
        return 'hand-thumbs-up';
      case 'approval_complete':
        return 'check-circle';
      case 'escalation_triggered':
        return 'arrow-up-circle';
      case 'expired':
        return 'clock-history';
      case 'cancelled':
        return 'slash-circle';
      default:
        return 'dot';
    }
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
          <strong>Not Found:</strong> Approval request not found
        </sl-alert>
      `;
    }

    const isPending = this.approvalRequest.status === 'pending';
    const isResolved = [
      'approved',
      'declined',
      'expired',
      'cancelled',
    ].includes(this.approvalRequest.status);
    const displayStatus =
      this.approvalRequest.status === 'expired'
        ? 'TIMED OUT'
        : this.approvalRequest.status.toUpperCase();

    const toolArgs = this.formatToolArgs(
      withoutApprovalMetadata(this.approvalRequest.tool_args)
    );
    const source = formatApprovalSource(
      getApprovalSource(this.approvalRequest.tool_args)
    );
    const requester = formatApprovalRequester(
      this.approvalRequest.managed_agent_name,
      this.approvalRequest.tool_args
    );

    const isQuestion = this.isQuestion;

    return html`
      <div class="header">
        <h1>
          <sl-icon
            name=${isQuestion ? 'chat-left-quote' : 'shield-check'}
          ></sl-icon>
          ${isQuestion ? 'Agent Question' : 'Tool Execution Approval'}
          <sl-badge
            variant=${this.getStatusVariant(this.approvalRequest.status)}
            class="status-badge"
          >
            ${displayStatus}
          </sl-badge>
        </h1>
        <p>
          ${
            isQuestion
              ? `${requester} is waiting on your answer before it continues`
              : `Review ${requester}'s tool execution request`
          }
        </p>
      </div>

      ${
        this.actionResult
          ? html`
              <sl-alert
                variant=${
                  this.actionResult.type === 'success' ? 'success' : 'danger'
                }
                open
                closable
                @sl-hide=${() => (this.actionResult = null)}
              >
                <sl-icon
                  slot="icon"
                  name=${
                    this.actionResult.type === 'success'
                      ? 'check-circle'
                      : 'exclamation-octagon'
                  }
                ></sl-icon>
                ${this.actionResult.message}
              </sl-alert>
            `
          : ''
      }
      ${
        this.approvalRequest.status === 'expired'
          ? html`
              <sl-alert variant="warning" open class="expired-banner">
                <sl-icon slot="icon" name="clock-history"></sl-icon>
                <strong>Expired:</strong> no response within the window
              </sl-alert>
            `
          : ''
      }

      <sl-card>
        ${
          isQuestion && isPending
            ? html`
                <div class="content-section">
                  <question-answer-panel
                    .question=${this.questionText}
                    .options=${this.approvalRequest.question_options ?? []}
                    .allowFreeText=${
                      this.approvalRequest.allow_free_text === true
                    }
                    .submitting=${this.submitting}
                    @question-answer=${this.handleQuestionAnswer}
                    @question-dismiss=${this.handleQuestionDismiss}
                  ></question-answer-panel>
                </div>
              `
            : ''
        }
        ${
          this.approvalRequest.summary && !isQuestion
            ? html`
                <div class="content-section">
                  <h2>Request</h2>
                  <div class="reasoning-text">
                    ${this.approvalRequest.summary}
                  </div>
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
          <h2>Tool Information</h2>
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

            <div class="info-label">Tool Name:</div>
            <div class="info-value">
              <strong>${this.approvalRequest.tool_name}</strong>
            </div>

            <div class="info-label">Request ID:</div>
            <div class="info-value">
              <code style="font-size: 0.75rem;"
                >${this.approvalRequest.id}</code
              >
            </div>

            <div class="info-label">Requested:</div>
            <div class="info-value">
              ${this.formatDate(this.approvalRequest.requested_at)}
            </div>

            ${
              this.approvalRequest.expires_at
                ? html`
                    <div class="info-label">Expires:</div>
                    <div class="info-value">
                      ${this.formatDate(this.approvalRequest.expires_at)}
                    </div>
                  `
                : ''
            }
            ${
              this.approvalRequest.execution_id
                ? html`
                    <div class="info-label">Execution ID:</div>
                    <div class="info-value">
                      <code style="font-size: 0.75rem;"
                        >${this.approvalRequest.execution_id}</code
                      >
                    </div>
                  `
                : ''
            }
          </div>
        </div>

        ${
          this.approvalRequest.rule_context
            ? html`
                <div class="content-section">
                  <approval-rule-context-block
                    .ruleContext=${this.approvalRequest.rule_context}
                    .toolName=${this.approvalRequest.tool_name}
                  ></approval-rule-context-block>
                </div>
              `
            : ''
        }
        ${
          this.approvalRequest.agent_reasoning
            ? html`
                <div class="content-section">
                  <h2>Agent Reasoning</h2>
                  <div class="reasoning-text">
                    ${this.approvalRequest.agent_reasoning}
                  </div>
                </div>
              `
            : ''
        }

        <div class="content-section">
          <h2>Tool Arguments</h2>
          <div class="code-block">${toolArgs}</div>
        </div>

        ${
          this.history.length
            ? html`
                <div class="content-section">
                  <h2>Workflow History</h2>
                  <ul class="timeline">
                    ${this.history.map(
                      (event) => html`
                        <li>
                          <sl-icon
                            class="timeline-icon"
                            name=${this.timelineIcon(event)}
                          ></sl-icon>
                          <div class="timeline-body">
                            <p class="timeline-detail">${event.detail}</p>
                            <p class="timeline-meta">
                              ${
                                event.actor_email
                                  ? html`<strong>${event.actor_email}</strong>
                                      · `
                                  : ''
                              }
                              ${this.formatDate(event.timestamp)}
                            </p>
                            ${
                              event.comment
                                ? html`<div class="timeline-comment">
                                    ${event.comment}
                                  </div>`
                                : ''
                            }
                          </div>
                        </li>
                      `
                    )}
                  </ul>
                </div>
              `
            : ''
        }
        ${
          isResolved
            ? html`
                <div class="resolved-info">
                  <h3>
                    ${
                      this.approvalRequest.status === 'approved'
                        ? '✅ Approved'
                        : this.approvalRequest.status === 'expired'
                          ? '⏱️ Timed Out'
                          : this.approvalRequest.status === 'cancelled'
                            ? '🚫 Cancelled'
                            : '❌ Declined'
                    }
                  </h3>
                  ${
                    this.approvalRequest.resolved_at
                      ? html`<p>
                          Resolved at:
                          ${this.formatDate(this.approvalRequest.resolved_at)}
                        </p>`
                      : ''
                  }
                  ${
                    this.approvalRequest.approver_comment
                      ? html`
                          <p><strong>Comment:</strong></p>
                          <div class="code-block">
                            ${this.approvalRequest.approver_comment}
                          </div>
                        `
                      : ''
                  }
                </div>
              `
            : ''
        }
        ${
          isPending && !isQuestion
            ? html`
                <sl-divider></sl-divider>

                <div class="comment-section">
                  <h2>Your Decision</h2>
                  <sl-textarea
                    label="Comment (optional)"
                    placeholder="Add a comment explaining your decision..."
                    rows="4"
                    .value=${this.comment}
                    @sl-input=${(e: any) => (this.comment = e.target.value)}
                    ?disabled=${this.submitting}
                  ></sl-textarea>
                </div>

                <div class="actions">
                  <sl-button
                    variant="success"
                    size="large"
                    @click=${this.handleApprove}
                    ?loading=${this.submitting}
                    ?disabled=${this.submitting}
                  >
                    <sl-icon slot="prefix" name="check-circle"></sl-icon>
                    Approve
                  </sl-button>
                  <sl-button
                    variant="danger"
                    size="large"
                    @click=${this.handleDecline}
                    ?loading=${this.submitting}
                    ?disabled=${this.submitting}
                  >
                    <sl-icon slot="prefix" name="x-circle"></sl-icon>
                    Decline
                  </sl-button>
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
    `;
  }
}
