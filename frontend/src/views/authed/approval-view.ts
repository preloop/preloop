import { LitElement, html, css } from 'lit';
import { customElement, state, property } from 'lit/decorators.js';
import { AuthedElement } from '../../api';
import { unifiedWebSocketManager } from '../../services/unified-websocket-manager';
import '@shoelace-style/shoelace/dist/components/card/card.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/textarea/textarea.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/divider/divider.js';

interface ApprovalRequest {
  id: string;
  account_id: string;
  tool_configuration_id: string;
  approval_workflow_id: string;
  execution_id: string | null;
  tool_name: string;
  tool_args: Record<string, any>;
  agent_reasoning: string | null;
  status: 'pending' | 'approved' | 'declined' | 'expired' | 'cancelled';
  requested_at: string;
  resolved_at: string | null;
  expires_at: string | null;
  approver_comment: string | null;
  webhook_posted_at: string | null;
  webhook_error: string | null;
}

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
  `;

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
        this.approvalRequest = data;
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

  private async handleApprove() {
    if (!this.approvalRequest) return;

    this.submitting = true;
    this.actionResult = null;

    try {
      const response = await fetch(
        `/api/v1/approval-requests/${this.requestId}/approve`,
        {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            Authorization: `Bearer ${localStorage.getItem('accessToken')}`,
          },
          body: JSON.stringify({
            approved: true,
            comment: this.comment || null,
          }),
        }
      );

      if (!response.ok) {
        const error = await response.json();
        throw new Error(error.detail || 'Failed to approve request');
      }

      const updated = await response.json();
      this.approvalRequest = updated;
      this.actionResult = {
        type: 'success',
        message: 'Request approved successfully!',
      };
      this.comment = '';
    } catch (err: any) {
      this.actionResult = {
        type: 'error',
        message: err.message || 'Failed to approve request',
      };
    } finally {
      this.submitting = false;
    }
  }

  private async handleDecline() {
    if (!this.approvalRequest) return;

    this.submitting = true;
    this.actionResult = null;

    try {
      const response = await fetch(
        `/api/v1/approval-requests/${this.requestId}/decline`,
        {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            Authorization: `Bearer ${localStorage.getItem('accessToken')}`,
          },
          body: JSON.stringify({
            approved: false,
            comment: this.comment || null,
          }),
        }
      );

      if (!response.ok) {
        const error = await response.json();
        throw new Error(error.detail || 'Failed to decline request');
      }

      const updated = await response.json();
      this.approvalRequest = updated;
      this.actionResult = {
        type: 'success',
        message: 'Request declined.',
      };
      this.comment = '';
    } catch (err: any) {
      this.actionResult = {
        type: 'error',
        message: err.message || 'Failed to decline request',
      };
    } finally {
      this.submitting = false;
    }
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

    const toolArgs = this.formatToolArgs(this.approvalRequest.tool_args);

    return html`
      <div class="header">
        <h1>
          <sl-icon name="shield-check"></sl-icon>
          Tool Execution Approval
          <sl-badge
            variant=${this.getStatusVariant(this.approvalRequest.status)}
            class="status-badge"
          >
            ${displayStatus}
          </sl-badge>
        </h1>
        <p>Review and approve or decline this tool execution request</p>
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

      <sl-card>
        <div class="content-section">
          <h2>Tool Information</h2>
          <div class="info-grid">
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
          isPending
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
          This approval request was generated by an automated agent and requires
          human review before the tool can be executed.
        </div>
      </sl-card>
    `;
  }
}
