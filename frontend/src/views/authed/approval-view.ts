import { html, css, unsafeCSS } from 'lit';
import { customElement, state, property } from 'lit/decorators.js';
import {
  AuthedElement,
  getAgentGovernance,
  getUserProfile,
  hasPermission,
  updateAgentGovernance,
} from '../../api';
import type {
  ApprovalDecisionOptions,
  ApprovalRequest,
  SubjectGovernanceConfig,
} from '../../types';
import { unifiedWebSocketManager } from '../../services/unified-websocket-manager';
import {
  formatApprovalRequester,
  formatApprovalSource,
  getApprovalSource,
  withoutApprovalMetadata,
} from '../../utils/approval-identity';
import {
  APPROVAL_REQUESTS_PAGE_LIMIT,
  approvalStatusLabel,
  formatNextWaitingLabel,
  isExpiringSoon,
  isUnexpiredPendingRequest,
  millisUntilExpiry,
  normalizeApprovalRequest,
} from '../../utils/approvals';
import { confirmDialog, showToast } from '../../components/confirm-dialog';
import { formatRelativeTime } from '../../utils/date';
import {
  normalizeScopedToolRules,
  serializeScopedToolRules,
} from '../../utils/scoped-governance';
import '../../components/question-answer-panel';
import '../../components/approval-rule-context-block';
import type { QuestionAnswerDetail } from '../../components/question-answer-panel';
import consoleStyles from '../../styles/console-styles.css?inline';
import '@shoelace-style/shoelace/dist/components/card/card.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/textarea/textarea.js';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/divider/divider.js';
import '@shoelace-style/shoelace/dist/components/checkbox/checkbox.js';
import '@shoelace-style/shoelace/dist/components/icon-button/icon-button.js';

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

  /**
   * How many pending rows the queue fetch returned, before client-side
   * filtering. Used to say "100+" when the page is full rather than a
   * count that looks total and is not.
   */
  @state()
  private waitingNextFetched = 0;

  /**
   * Ticked by the operator to turn this approval into a standing allow rule
   * for this agent and this tool. Read on approve, never on deny.
   */
  @state()
  private alwaysAllow = false;

  /**
   * Permissions of the signed-in user. `null` is not "no permissions": it is
   * the RBAC-inactive contract of `/auth/users/me` (OSS and DISABLE_RBAC),
   * where `hasPermission` is permissive. Use `permissionsLoaded` to tell
   * "still fetching" from "RBAC is off".
   */
  @state()
  private permissions: string[] | null = null;

  /** True once the profile fetch has settled, whatever it returned. */
  @state()
  private permissionsLoaded = false;

  /**
   * True while the deny confirmation is on screen. The decision keys listen on
   * the document, so without this an A pressed over an open "Deny this
   * request?" dialog would approve behind it.
   */
  private confirming = false;

  private unsubscribe?: () => void;
  private tickTimer?: ReturnType<typeof setInterval>;

  static styles = [
    unsafeCSS(consoleStyles),
    css`
      /* No page geometry here: the shell owns the width and the side inset
         (styles/console-styles.css, "The page box"). */
      :host {
        display: block;
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

      /* The facts on one hairline strip: no boxes, no grid of labels. */
      .fact-strip {
        display: flex;
        flex-wrap: wrap;
        gap: 0.35rem 1.5rem;
        padding: 0.75rem 0;
        margin-bottom: 1rem;
        border-top: 1px solid var(--console-hairline);
        border-bottom: 1px solid var(--console-hairline);
      }

      .fact {
        display: flex;
        align-items: center;
        gap: 0.4rem;
        font-size: var(--sl-font-size-small);
        min-width: 0;
      }

      .fact-label {
        color: var(--console-meta-color);
      }

      .fact code {
        font-family: monospace;
        font-size: 0.8125rem;
      }

      .fact a {
        color: var(--console-link-color);
      }

      .copy-id::part(base) {
        padding: 0;
        font-size: 0.875rem;
      }

      /* D27: a command block rests on the page surface, not on a third one. */
      .code-block {
        background: var(--console-page);
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

      /* Depth limit two: what happened is a hairline block on the card, not a
         filled panel inside it. */
      .resolved-info {
        margin-top: 1rem;
        padding-top: 1rem;
        border-top: 1px solid var(--console-hairline);
      }

      .resolved-info h3 {
        margin: 0 0 0.5rem 0;
        font-size: 1rem;
        font-weight: 600;
        display: flex;
        align-items: center;
        gap: 0.5rem;
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
        margin: var(--sl-spacing-large) 0 0;
        padding: var(--sl-spacing-medium) 0;
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

      /*
       * The label repeats the placeholder and would cost the bar a whole row.
       * Keep it in the accessibility tree (Shoelace wires it to the input, so
       * the field has a name) and take it out of the picture.
       */
      .decision-comment::part(form-control-label) {
        clip: rect(0 0 0 0);
        clip-path: inset(50%);
        height: 1px;
        overflow: hidden;
        position: absolute;
        white-space: nowrap;
        width: 1px;
      }

      /* One line, always: a wrapped "Always allow" label reads as two ideas. */
      .always-allow::part(label) {
        white-space: nowrap;
        font-size: var(--sl-font-size-small);
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
        .decision-buttons {
          flex: 1 1 100%;
        }

        .decision-buttons sl-button {
          flex: 1;
        }

        /* The stacked buttons take the full row, so the large gap still fits. */
        .decision-buttons .deny {
          margin-left: var(--sl-spacing-large);
        }
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
    `,
  ];

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

    // The decision keys work wherever focus is, so the listener sits on the
    // document rather than on the host: a key pressed before anything inside
    // the page is focused never reaches the host element.
    document.addEventListener('keydown', this.handleKeyDown);
    this.startTicking();
    void this.loadPermissions();
  }

  /**
   * Writing a standing rule needs `manage_agents`, the permission the backend
   * checks on `PUT /agents/{id}/governance`. Without it the checkbox is absent
   * rather than present and answering with a 403.
   */
  private async loadPermissions() {
    if (this.publicOnly) return;
    try {
      const profile = await getUserProfile();
      // Keep the null: on OSS and DISABLE_RBAC the endpoint returns no
      // permissions array at all, and `[]` would read as "allowed nothing".
      this.permissions = profile?.permissions ?? null;
    } catch {
      this.permissions = null;
    } finally {
      this.permissionsLoaded = true;
    }
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
    // A confirmation is a question in its own right: answer it with the mouse
    // or the dialog's own keys, not with the page shortcuts underneath it.
    if (this.confirming) return;
    // Holding the key down would re-ask the confirmation on every repeat.
    if (event.repeat) return;
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
    // Founder call: Approve fires immediately; Deny confirms. No undo window.
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

      // Report the change the way the rest of the console reports results,
      // unless this page took the decision: submitDecision already said so and
      // a broadcast echoed back here would toast the same thing twice.
      if (this.decisionTaken) return;
      if (message.type === 'approval_approved') {
        showToast('This request was approved.', 'success');
      } else if (message.type === 'approval_declined') {
        showToast('This request was denied.', 'neutral');
      } else if (message.type === 'approval_expired') {
        showToast('This request timed out.', 'warning');
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
        // A request the sweeper has not caught up with yet is still `pending`
        // in the database long after its expiry. Read the clock here so the
        // page never offers a decision that the backend would reject.
        this.approvalRequest = normalizeApprovalRequest(data);
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
        // An empty array is the default when the decide payload omits
        // history; do not wipe a timeline we already rendered.
        if (Array.isArray(updated.history) && updated.history.length > 0) {
          this.history = updated.history;
        }
        this.decisionTaken = true;
        showToast(successMessage, action === 'approve' ? 'success' : 'neutral');
        this.comment = '';
      } catch (err: any) {
        showToast(err.message || `Failed to ${action} request`, 'danger');
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
      const decided = this.approvalRequest;
      this.approvalRequest = updated;
      this.comment = '';
      this.decisionTaken = true;
      showToast(successMessage, action === 'approve' ? 'success' : 'neutral');
      // The standing rule is written only after the decision landed: a
      // failed approve must not leave a rule behind.
      if (action === 'approve' && this.alwaysAllow && decided) {
        this.alwaysAllow = false;
        await this.applyAlwaysAllow(decided);
      }
      this.loadHistory();
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
        `/api/v1/approval-requests?status=pending&limit=${APPROVAL_REQUESTS_PAGE_LIMIT}`
      );
      if (Array.isArray(data)) {
        this.waitingNextFetched = data.length;
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
    if (!request || this.confirming) return;
    this.confirming = true;
    let confirmed = false;
    try {
      confirmed = await confirmDialog({
        title: 'Deny this request?',
        message: `${request.tool_name} will not run.`,
        detail: `${formatApprovalRequester(
          request.managed_agent_name,
          request.tool_args
        )} is told no and continues without it.`,
        confirmLabel: 'Deny',
        variant: 'danger',
      });
    } finally {
      this.confirming = false;
    }
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
        ${this.renderFactStrip(request, requester, source, isPending, countdown)}

        <div class="content-section">
          <h2>${this.argsHeading(request)}</h2>
          <div class="code-block">${toolArgs}</div>
        </div>

        ${
          request.agent_reasoning
            ? html`
                <div class="content-section">
                  <h2>Why the agent wants this</h2>
                  <div class="reasoning-text">${request.agent_reasoning}</div>
                </div>
              `
            : ''
        }
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
        ${isResolved ? this.renderResolvedHeader(request) : ''}

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

  /**
   * The facts about this request on one hairline strip: who asked, what for,
   * where from, and when. The agent and the session are links, because "which
   * agent is this" is the first question a decision raises.
   */
  private renderFactStrip(
    request: ApprovalRequest,
    requester: string,
    source: string | null,
    isPending: boolean,
    countdown: string | null
  ) {
    const shortId = request.id.slice(0, 8);
    return html`
      <div class="fact-strip">
        <div class="fact">
          <span class="fact-label">Agent</span>
          ${
            request.managed_agent_id
              ? html`<a href="/console/agents/${request.managed_agent_id}"
                  >${requester}</a
                >`
              : html`<span>${requester}</span>`
          }
        </div>
        <div class="fact">
          <span class="fact-label">Tool</span>
          <code>${request.tool_name}</code>
        </div>
        ${
          source
            ? html`<div class="fact">
                <span class="fact-label">Adapter</span>
                <span>${source}</span>
              </div>`
            : ''
        }
        ${
          request.runtime_session_id
            ? html`<div class="fact">
                <span class="fact-label">Session</span>
                <a
                  href="/console/runtime-sessions?sessionId=${encodeURIComponent(
                    request.runtime_session_id
                  )}"
                  >${request.runtime_session_id.slice(0, 8)}</a
                >
              </div>`
            : ''
        }
        ${
          request.execution_id
            ? html`<div class="fact">
                <span class="fact-label">Execution</span>
                <a href="/console/flows/executions/${request.execution_id}"
                  >${request.execution_id.slice(0, 8)}</a
                >
              </div>`
            : ''
        }
        <div class="fact">
          <span class="fact-label">Requested</span>
          <span title=${this.formatDate(request.requested_at)}
            >${formatRelativeTime(request.requested_at)}</span
          >
        </div>
        ${
          // Once decided there is nothing left to expire, so the deadline goes.
          isPending && countdown
            ? html`<div class="fact">
                <span class="fact-label">Expires</span>
                <span title=${this.formatDate(request.expires_at || '')}
                  >${countdown.replace('expires ', '')}</span
                >
              </div>`
            : ''
        }
        <div class="fact">
          <span class="fact-label">Request</span>
          <code class="request-id">${shortId}</code>
          <sl-icon-button
            class="copy-id"
            name="clipboard"
            label="Copy the full request id"
            @click=${() => this.copyRequestId(request.id)}
          ></sl-icon-button>
        </div>
      </div>
    `;
  }

  /**
   * Can this operator turn the decision into a standing rule?
   *
   * Only for a request that names its agent: the rule is stored on that
   * agent's scoped governance, so an approval with no `managed_agent_id` has
   * nowhere to put it. A public-token viewer never sees it.
   */
  private canWriteAgentRules(request: ApprovalRequest): boolean {
    if (this.publicOnly) return false;
    if (!request.managed_agent_id) return false;
    // Wait for the profile rather than drawing a checkbox that may vanish.
    if (!this.permissionsLoaded) return false;
    return hasPermission(this.permissions, 'manage_agents');
  }

  /**
   * Write "allow <tool> for this agent" as a scoped rule on the agent.
   *
   * The evaluator returns on the first matching scoped rule in list order
   * (`policy_evaluator._evaluate_rule_candidates`) and a rule with no
   * condition matches everything, so the new allow has to go in front: the
   * catch-all `require_approval` that raised this request is usually already
   * there, and an allow behind it would never be reached. The previous rule
   * set is kept so Undo can put it back exactly.
   */
  private async applyAlwaysAllow(request: ApprovalRequest) {
    const agentId = request.managed_agent_id;
    if (!agentId) return;
    try {
      const current = await getAgentGovernance(agentId);
      const config = current.config;
      const previous = config.tool_rules || {};
      const rules = normalizeScopedToolRules(previous);
      const forTool = rules[request.tool_name] || [];
      rules[request.tool_name] = [
        {
          id: `scoped:${request.tool_name}:allow-from-${request.id.slice(
            0,
            8
          )}`,
          action: 'allow',
          condition_expression: null,
          condition_type: 'simple',
          priority: 0,
          description: `Created from approval ${request.id.slice(0, 8)}`,
          is_enabled: true,
          approval_workflow_id: null,
        },
        // Everything that was there keeps its relative order, one step down.
        ...forTool.map((rule, index) => ({ ...rule, priority: index + 1 })),
      ];
      await this.saveToolRules(
        agentId,
        config,
        serializeScopedToolRules(rules)
      );
      showToast(
        `Rule added. Future ${request.tool_name} calls from ${
          request.managed_agent_name || 'this agent'
        } run without asking.`,
        'success',
        {
          label: 'Undo',
          onClick: () => {
            void this.undoAlwaysAllow(agentId, config, previous);
          },
        }
      );
    } catch {
      showToast(
        'The decision was recorded, but the rule could not be saved.',
        'danger'
      );
    }
  }

  private async undoAlwaysAllow(
    agentId: string,
    config: SubjectGovernanceConfig,
    previous: Record<string, Array<Record<string, unknown>>>
  ) {
    try {
      await this.saveToolRules(agentId, config, previous);
      showToast('Rule removed.', 'neutral');
    } catch {
      showToast('Could not remove the rule.', 'danger');
    }
  }

  /** PUT the whole subject config back with only `tool_rules` changed. */
  private async saveToolRules(
    agentId: string,
    config: SubjectGovernanceConfig,
    toolRules: Record<string, Array<Record<string, unknown>>>
  ) {
    await updateAgentGovernance(agentId, {
      ...config,
      tool_rules: toolRules,
    });
  }

  /** Bash and friends carry a command; anything else carries arguments. */
  private argsHeading(request: ApprovalRequest): string {
    const args = withoutApprovalMetadata(request.tool_args);
    return typeof args.command === 'string' || typeof args.cmd === 'string'
      ? 'Command'
      : 'Arguments';
  }

  private async copyRequestId(id: string) {
    try {
      await navigator.clipboard.writeText(id);
      showToast('Request id copied.', 'success');
    } catch {
      showToast('Could not copy the request id.', 'danger');
    }
  }

  /**
   * What happened to a resolved request, in one line: who decided and how
   * long the agent waited. `responses[]` is not serialised by the API, so the
   * decider comes from the workflow-history timeline, which carries the actor.
   */
  private renderResolvedHeader(request: ApprovalRequest) {
    const label = approvalStatusLabel(request.status);
    const decider = this.decider();
    const elapsed = this.decisionElapsed(request);
    const parts: string[] = [
      decider ? `${label} by ${decider}` : label,
      elapsed ? `${elapsed} after request` : '',
    ].filter(Boolean);
    return html`
      <div class="resolved-info">
        <h3>
          <sl-icon name=${this.resolvedIcon(request.status)}></sl-icon>
          ${parts.join(' · ')}
        </h3>
        ${
          request.approver_comment
            ? html`
                <p><strong>Comment</strong></p>
                <div class="code-block">${request.approver_comment}</div>
              `
            : ''
        }
      </div>
    `;
  }

  private resolvedIcon(status: string): string {
    switch (status) {
      case 'approved':
        return 'check-circle';
      case 'declined':
        return 'x-circle';
      case 'expired':
        return 'clock-history';
      default:
        return 'slash-circle';
    }
  }

  /** Who decided: a person from the timeline, an AI judge, or a bypass. */
  private decider(): string | null {
    const request = this.approvalRequest;
    if (!request) return null;
    if (request.auto_approved_reason) return 'a time-boxed bypass';
    if (request.decided_by_ai) return 'an AI reviewer';
    const vote = [...this.history]
      .reverse()
      .find((event) => event.event_type === 'vote_received');
    if (!vote) return null;
    return vote.actor_email || 'an approver';
  }

  /** How long the agent waited, as "12s", "4m" or "2h". */
  private decisionElapsed(request: ApprovalRequest): string | null {
    if (!request.resolved_at) return null;
    const elapsed =
      new Date(request.resolved_at).getTime() -
      new Date(request.requested_at).getTime();
    if (!Number.isFinite(elapsed) || elapsed < 0) return null;
    const seconds = Math.round(elapsed / 1000);
    if (seconds < 60) return `${seconds}s`;
    const minutes = Math.round(seconds / 60);
    if (minutes < 60) return `${minutes}m`;
    const hours = Math.round(minutes / 60);
    if (hours < 24) return `${hours}h`;
    return `${Math.round(hours / 24)}d`;
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
          label="Comment (optional)"
          placeholder="Comment (optional)"
          .value=${this.comment}
          @sl-input=${(e: any) => (this.comment = e.target.value)}
          ?disabled=${this.submitting}
        ></sl-input>
        ${
          this.canWriteAgentRules(request)
            ? html`<sl-checkbox
                class="always-allow"
                ?checked=${this.alwaysAllow}
                ?disabled=${this.submitting}
                @sl-change=${(e: any) => (this.alwaysAllow = e.target.checked)}
              >
                Always allow ${request.tool_name} for
                ${request.managed_agent_name || 'this agent'}
              </sl-checkbox>`
            : ''
        }
        <div class="decision-buttons">
          <sl-button
            class="approve"
            variant="success"
            title="Approve (A)"
            @click=${this.handleApprove}
            ?loading=${this.submitting}
            ?disabled=${this.submitting}
          >
            <sl-icon slot="prefix" name="check-circle"></sl-icon>
            Approve<kbd aria-hidden="true">A</kbd>
          </sl-button>
          <sl-button
            class="deny"
            variant="danger"
            outline
            title="Deny (D)"
            @click=${this.handleDeny}
            ?disabled=${this.submitting}
          >
            <sl-icon slot="prefix" name="x-circle"></sl-icon>
            Deny<kbd aria-hidden="true">D</kbd>
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
                >${formatNextWaitingLabel(
                  waiting.length,
                  this.waitingNextFetched
                )}</a
              >`
            : html`<span class="muted">Nothing else is waiting for you</span>`
        }
      </div>
    `;
  }
}
