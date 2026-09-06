import { html, css, unsafeCSS } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import { Router } from '@vaadin/router';
import { AuthedElement, approveRequest, declineRequest } from '../../api';
import type { ApprovalRequest } from '../../types';
import '../../components/question-answer-panel';
import type { QuestionAnswerDetail } from '../../components/question-answer-panel';
import {
  formatFutureRelativeTime,
  formatRelativeTime,
  parseUTCDate,
} from '../../utils/date';
import { formatApprovalRequester } from '../../utils/approval-identity';
import {
  APPROVAL_REQUESTS_PAGE_LIMIT,
  approvalStatusLabel,
  isExpiringSoon,
  isUnexpiredPendingRequest,
  normalizeApprovalRequest,
  partitionApprovalRequests,
} from '../../utils/approvals';
import { confirmDialog, showToast } from '../../components/confirm-dialog';
import { unifiedWebSocketManager } from '../../services/unified-websocket-manager';
import '../../components/approval-rule-context-block';
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
import '@shoelace-style/shoelace/dist/components/dropdown/dropdown.js';
import '@shoelace-style/shoelace/dist/components/menu/menu.js';
import '@shoelace-style/shoelace/dist/components/menu-item/menu-item.js';
import '@shoelace-style/shoelace/dist/components/divider/divider.js';
import consoleStyles from '../../styles/console-styles.css?inline';

/**
 * Ids the operator has already had on screen, so a request that arrived since
 * the last visit can carry a "new" dot. Client-side on purpose: the record has
 * no `viewed_at` column, and a per-browser memory is honest about that.
 */
const SEEN_STORAGE_KEY = 'preloop.approvals.seen';

/** Cap on the stored seen set, so the key cannot grow without bound. */
const SEEN_STORAGE_LIMIT = 500;

interface ApprovalStats {
  total: number;
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

  /** Pending, not expired: the requests an operator can still decide. */
  @state()
  private waitingRequests: ApprovalRequest[] = [];

  /** Everything already decided, expired or cancelled. */
  @state()
  private historyRequests: ApprovalRequest[] = [];

  /** Id of the request whose row decision is in flight, if any. */
  @state()
  private decidingId: string | null = null;

  @state()
  private loading = true;

  @state()
  private stats: ApprovalStats = {
    total: 0,
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

  /**
   * Ticks once a second while anything in "Waiting for you" can still expire,
   * so a request that times out with the list open leaves that group and
   * loses its Approve/Deny buttons instead of offering a dead decision.
   */
  @state()
  private nowMs = Date.now();

  /**
   * Which row the keyboard is on, as an index into `navigableRequests`. -1
   * means the keyboard has not been used yet, so no row steals the tab stop.
   */
  @state()
  private focusedIndex = -1;

  /** Rows picked with X. Bulk actions land in a later slice (E6). */
  @state()
  private selectedIds: string[] = [];

  /** Waiting rows the operator had not seen when the page loaded. */
  @state()
  private newIds: string[] = [];

  /** True while a deny confirmation is open, so A cannot fire behind it. */
  private confirming = false;

  /** Set when the focused row must be focused after the next render. */
  private pendingFocus = false;

  private unsubscribe?: () => void;
  private tickTimer?: ReturnType<typeof setInterval>;

  static styles = [
    unsafeCSS(consoleStyles),
    css`
      /* One hairline strip, not six boxes: these are counts, not cards. */
      .stat-strip {
        display: flex;
        flex-wrap: wrap;
        gap: 0.25rem 0.75rem;
        padding: var(--sl-spacing-small) 0;
        margin-bottom: var(--sl-spacing-medium);
        border-top: 1px solid var(--console-hairline);
        border-bottom: 1px solid var(--console-hairline);
        color: var(--console-meta-color);
        font-size: var(--console-text-meta);
        font-variant-numeric: tabular-nums;
      }

      .stat-strip strong {
        color: var(--console-body-color);
        font-weight: 600;
      }

      /* A quiet dot, not a hairline: at hairline weight the middot vanishes
         and the strip reads "3 requests 1 waiting". */
      .stat-strip .separator {
        color: var(--console-meta-color);
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

      /* The keyboard row: a ring for the keyboard, nothing for the pointer. */
      .approval-item:focus-visible {
        outline: 2px solid var(--sl-color-primary-500);
        outline-offset: 2px;
      }

      /* A selected row is marked at its edge, not filled: a row is never
         tinted by its state. */
      .approval-item.selected {
        border-left: 3px solid var(--sl-color-primary-500);
        padding-left: calc(var(--sl-spacing-medium) - 2px);
      }

      /* Meta text, at the meta size and the meta colour: neutral-500 at 12px
         does not hold contrast on a dark card. */
      .key-legend {
        font-size: var(--console-text-meta);
        color: var(--console-meta-color);
        margin-left: auto;
      }

      .new-dot {
        width: 6px;
        height: 6px;
        border-radius: 50%;
        background: var(--sl-color-primary-600);
        flex-shrink: 0;
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
        flex-wrap: wrap;
        justify-content: flex-end;
      }

      /* Destructive last, after a large gap, never beside the everyday action. */
      .approval-actions .row-deny {
        margin-left: var(--sl-spacing-large);
      }

      .approval-actions .row-details {
        color: var(--sl-color-primary-600);
        font-size: var(--sl-font-size-small);
        text-decoration: none;
        margin-left: var(--sl-spacing-small);
      }

      .approval-actions .row-details:hover {
        text-decoration: underline;
      }

      .approval-group {
        margin-bottom: var(--sl-spacing-large);
      }

      .group-header {
        display: flex;
        align-items: center;
        gap: var(--sl-spacing-small);
        margin-bottom: var(--sl-spacing-small);
      }

      .group-header h2 {
        margin: 0;
        font-size: var(--sl-font-size-medium);
        font-weight: 600;
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

  /** Bound once so the host listener can be removed on disconnect. */
  private readonly onKeyDown = (event: KeyboardEvent) =>
    this.handleKeyDown(event);

  async connectedCallback() {
    super.connectedCallback();
    this.addEventListener('keydown', this.onKeyDown);
    await this.loadApprovalRequests();
    this.connectWebSocket();
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    this.removeEventListener('keydown', this.onKeyDown);
    this.unsubscribe?.();
    this.stopTicking();
  }

  private startTicking() {
    if (this.tickTimer) return;
    this.tickTimer = setInterval(() => {
      this.nowMs = Date.now();
      this.applyFilters();
    }, 1000);
  }

  private stopTicking() {
    if (this.tickTimer) {
      clearInterval(this.tickTimer);
      this.tickTimer = undefined;
    }
  }

  /** Tick only while a waiting row still has an expiry that can pass. */
  private syncExpiryTick() {
    if (this.waitingRequests.some((request) => request.expires_at)) {
      this.startTicking();
    } else {
      this.stopTicking();
    }
  }

  /** Waiting rows first, then history: the order the keyboard walks. */
  private get navigableRequests(): ApprovalRequest[] {
    return [...this.waitingRequests, ...this.historyRequests];
  }

  /** Ids of requests this browser has already shown, oldest dropped first. */
  private readSeenIds(): string[] {
    try {
      const raw = window.localStorage.getItem(SEEN_STORAGE_KEY);
      if (!raw) return [];
      const parsed = JSON.parse(raw);
      return Array.isArray(parsed)
        ? parsed.filter((id): id is string => typeof id === 'string')
        : [];
    } catch {
      return [];
    }
  }

  private writeSeenIds(ids: string[]) {
    try {
      window.localStorage.setItem(
        SEEN_STORAGE_KEY,
        JSON.stringify(ids.slice(-SEEN_STORAGE_LIMIT))
      );
    } catch {
      // A blocked or full storage only costs the dot, never the list.
    }
  }

  /**
   * Mark which waiting rows arrived since the last visit, then record every
   * waiting row as seen so the dot clears on the next load.
   */
  private markNewSinceLastVisit() {
    const seen = new Set(this.readSeenIds());
    const waitingIds = this.waitingRequests.map((request) => request.id);
    this.newIds = waitingIds.filter((id) => !seen.has(id));
    if (this.newIds.length === 0) return;
    this.writeSeenIds([...seen, ...this.newIds]);
  }

  /**
   * Keys are handled on the host so a focused row, the list, or the page
   * itself all reach the same handler. Typing in a filter, and keys that
   * already belong to a button or link, are left alone.
   */
  private handleKeyDown(event: KeyboardEvent) {
    if (event.metaKey || event.ctrlKey || event.altKey) return;
    const path = event.composedPath();
    const interactive = path.some((node) => {
      const tag = (node as HTMLElement)?.tagName?.toLowerCase();
      return (
        tag === 'input' ||
        tag === 'textarea' ||
        tag === 'sl-input' ||
        tag === 'sl-textarea' ||
        tag === 'sl-select' ||
        tag === 'button' ||
        tag === 'a' ||
        tag === 'sl-button' ||
        tag === 'sl-menu-item' ||
        tag === 'sl-icon-button' ||
        (node as HTMLElement)?.isContentEditable === true
      );
    });
    if (interactive) return;

    const requests = this.navigableRequests;
    if (requests.length === 0) return;
    const key = event.key;

    if (key === 'j' || key === 'J' || key === 'ArrowDown') {
      event.preventDefault();
      this.moveFocus(1);
      return;
    }
    if (key === 'k' || key === 'K' || key === 'ArrowUp') {
      event.preventDefault();
      this.moveFocus(-1);
      return;
    }

    const focused = requests[this.focusedIndex];
    if (!focused) return;

    if (key === 'Enter') {
      event.preventDefault();
      Router.go(`/console/approval/${focused.id}`);
      return;
    }
    if (key === 'x' || key === 'X') {
      event.preventDefault();
      this.toggleSelection(focused.id);
      return;
    }
    if (key === 'a' || key === 'A') {
      if (!this.canDecide(focused)) return;
      event.preventDefault();
      void this.handleRowApprove(focused);
      return;
    }
    if (key === 'd' || key === 'D') {
      if (!this.canDecide(focused)) return;
      event.preventDefault();
      void this.handleRowDeny(focused);
    }
  }

  /** Only waiting, non-question rows can be decided from the keyboard. */
  private canDecide(request: ApprovalRequest): boolean {
    if (this.confirming) return false;
    // The buttons go disabled while a decision is in flight; the keys have to
    // do the same, or two quick presses POST the same decision twice.
    if (this.decidingId) return false;
    if (this.isQuestion(request)) return false;
    return this.waitingRequests.some((waiting) => waiting.id === request.id);
  }

  private moveFocus(delta: number) {
    const last = this.navigableRequests.length - 1;
    const next = this.focusedIndex < 0 ? 0 : this.focusedIndex + delta;
    this.focusedIndex = Math.min(Math.max(next, 0), last);
    this.pendingFocus = true;
  }

  private toggleSelection(id: string) {
    this.selectedIds = this.selectedIds.includes(id)
      ? this.selectedIds.filter((selected) => selected !== id)
      : [...this.selectedIds, id];
  }

  protected updated() {
    if (!this.pendingFocus) return;
    this.pendingFocus = false;
    const row = this.renderRoot.querySelector<HTMLElement>(
      `.approval-item[data-index="${this.focusedIndex}"]`
    );
    row?.focus();
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
        managed_agent_name: message.managed_agent_name || null,
        // The broadcast carries the matched-rule snapshot so a live-arriving
        // row explains itself the same way a fetched one does. Null when the
        // approval was raised without rule evaluation.
        rule_context: message.rule_context || null,
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

      if (!isUnexpiredPendingRequest(newApproval)) {
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
      const data = await this.fetchData(
        `/api/v1/approval-requests?limit=${APPROVAL_REQUESTS_PAGE_LIMIT}`
      );
      if (data && Array.isArray(data)) {
        // Sort by requested_at descending (most recent first)
        this.approvalRequests = (data as ApprovalRequest[])
          .map((request) => normalizeApprovalRequest(request))
          .sort(
            (a, b) =>
              parseUTCDate(b.requested_at).getTime() -
              parseUTCDate(a.requested_at).getTime()
          );
        this.applyFilters();
        this.markNewSinceLastVisit();
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
    const now = this.nowMs;
    const normalized = this.approvalRequests.map((request) =>
      normalizeApprovalRequest(request, now)
    );
    if (normalized.some((request, i) => request !== this.approvalRequests[i])) {
      this.approvalRequests = normalized;
      this.calculateStats();
    }

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
          r.managed_agent_name?.toLowerCase().includes(query) ||
          r.execution_id?.toLowerCase().includes(query) ||
          r.agent_reasoning?.toLowerCase().includes(query) ||
          JSON.stringify(r.tool_args).toLowerCase().includes(query)
      );
    }

    this.filteredRequests = filtered;

    // What still needs a person comes first, soonest expiry at the top; the
    // rest is history and keeps its newest-first order.
    const { waiting, history } = partitionApprovalRequests(filtered, now);
    this.waitingRequests = waiting;
    this.historyRequests = history;
    this.syncExpiryTick();
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

  /**
   * Re-read the clock at click time so a row whose expiry passed between
   * ticks cannot still post Approve or Deny.
   */
  private ensureStillWaiting(request: ApprovalRequest): boolean {
    this.nowMs = Date.now();
    if (isUnexpiredPendingRequest(request, this.nowMs)) return true;
    this.applyFilters();
    return false;
  }

  /**
   * Row-level approve: the same call the detail page makes, taken where the
   * request is seen. Approving is not destructive, so it does not confirm.
   */
  private async handleRowApprove(request: ApprovalRequest) {
    if (!this.ensureStillWaiting(request)) return;
    this.decidingId = request.id;
    try {
      const updated = await approveRequest(request.id);
      this.applyResolution(request.id, {
        status: 'approved',
        resolved_at: updated?.resolved_at ?? null,
      });
      showToast(`Approved ${request.tool_name}.`, 'success');
    } catch (error: any) {
      showToast(error?.message || 'Failed to approve the request', 'danger');
      console.error('Failed to approve request:', error);
    } finally {
      this.decidingId = null;
    }
  }

  /** Denying stops the agent, so it confirms first (DESIGN.md destructive). */
  private async handleRowDeny(request: ApprovalRequest) {
    if (!this.ensureStillWaiting(request)) return;
    this.confirming = true;
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
    this.confirming = false;
    if (!confirmed) return;
    if (!this.ensureStillWaiting(request)) return;
    this.decidingId = request.id;
    try {
      const updated = await declineRequest(request.id);
      this.applyResolution(request.id, {
        status: 'declined',
        resolved_at: updated?.resolved_at ?? null,
      });
      showToast(`Denied ${request.tool_name}.`, 'neutral');
    } catch (error: any) {
      showToast(error?.message || 'Failed to deny the request', 'danger');
      console.error('Failed to deny request:', error);
    } finally {
      this.decidingId = null;
    }
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
        <view-header headerText="Approval requests" width="wide"></view-header>
        <div class="loading-container">
          <sl-spinner style="font-size: 3rem;"></sl-spinner>
        </div>
      `;
    }

    return html`
      <view-header
        headerText="Approval requests"
        description="Tool calls that waited for a human decision, and what happened to them. Approval rules live in Tools; per-agent overrides live on each agent's detail page."
        width="wide"
      >
        <sl-dropdown>
          <sl-button slot="trigger" size="small" caret>
            <sl-icon slot="prefix" name="gear"></sl-icon>
            Configure approvals
          </sl-button>
          <sl-menu>
            <sl-menu-item
              @click=${() => (window.location.href = '/console/tools')}
            >
              <sl-icon slot="prefix" name="tools"></sl-icon>
              MCP tool access rules
            </sl-menu-item>
            <sl-menu-item
              @click=${() => (window.location.href = '/console/tools')}
            >
              <sl-icon slot="prefix" name="shield-lock"></sl-icon>
              Native tool approvals (account default)
            </sl-menu-item>
            <sl-menu-item
              @click=${() => (window.location.href = '/console/agents')}
            >
              <sl-icon slot="prefix" name="robot"></sl-icon>
              Per-agent overrides
            </sl-menu-item>
          </sl-menu>
        </sl-dropdown>
      </view-header>
      <div class="column-layout wide">
        <div class="main-column">
          ${this.renderStatStrip()}

          <!-- Filters -->
          <div class="filters-row">
            <sl-select
              label="Status"
              value=${this.statusFilter}
              @sl-change=${this.handleStatusFilterChange}
            >
              <sl-option value="all">All statuses</sl-option>
              <sl-option value="pending">Pending</sl-option>
              <sl-option value="approved">Approved</sl-option>
              <sl-option value="declined">Denied</sl-option>
              <sl-option value="expired">Timed out</sl-option>
              <sl-option value="cancelled">Cancelled</sl-option>
            </sl-select>

            <sl-select
              label="Tool"
              value=${this.toolFilter}
              @sl-change=${this.handleToolFilterChange}
            >
              <sl-option value="all">All tools</sl-option>
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
                            Configure tools
                          </sl-button>`
                        : ''
                    }
                  </div>
                `
              : html`
                  ${this.renderGroup(
                    'Waiting for you',
                    this.waitingRequests,
                    true,
                    0
                  )}
                  ${this.renderGroup(
                    'History',
                    this.historyRequests,
                    false,
                    this.waitingRequests.length
                  )}
                `
          }
        </div>
      </div>
    `;
  }

  /**
   * The counts on one hairline strip. The list fetches a single page, so the
   * total is capped: when the page came back full the strip says "last 100"
   * rather than presenting a page count as an account total.
   */
  private renderStatStrip() {
    const stats = this.stats;
    const capped = stats.total >= APPROVAL_REQUESTS_PAGE_LIMIT;
    const avg =
      stats.avgResponseTimeMinutes > 0
        ? stats.avgResponseTimeMinutes < 60
          ? `${stats.avgResponseTimeMinutes}m`
          : `${Math.round(stats.avgResponseTimeMinutes / 60)}h`
        : null;
    const facts: Array<unknown> = [
      capped
        ? html`Last <strong>${APPROVAL_REQUESTS_PAGE_LIMIT}</strong> requests`
        : html`<strong>${stats.total}</strong> requests`,
      html`<strong>${this.waitingRequests.length}</strong> waiting`,
      html`<strong>${stats.approved}</strong> approved`,
      html`<strong>${stats.declined}</strong> denied`,
      html`<strong>${stats.expired}</strong> timed out`,
    ];
    if (stats.approved + stats.declined > 0) {
      facts.push(
        html`<strong>${Math.round(stats.approvalRate)}%</strong> approved by a
          person`
      );
    }
    if (avg) {
      facts.push(html`avg response <strong>${avg}</strong>`);
    }
    return html`
      <div class="stat-strip">
        ${facts.map(
          (fact, index) =>
            html`${
                index > 0
                  ? html`<span class="separator" aria-hidden="true">·</span>`
                  : ''
              }<span>${fact}</span>`
        )}
      </div>
    `;
  }

  /**
   * One group of rows under its own heading. "Waiting for you" carries the
   * decision, so it is rendered first and its rows get Approve and Deny.
   */
  private renderGroup(
    title: string,
    requests: ApprovalRequest[],
    waiting: boolean,
    indexOffset: number
  ) {
    if (requests.length === 0) return '';
    return html`
      <div class="approval-group">
        <div class="group-header">
          <h2>${title}</h2>
          <sl-badge
            pill
            class="chip"
            variant=${waiting ? 'warning' : 'neutral'}
          >
            ${requests.length}
          </sl-badge>
          ${
            waiting
              ? html`<span class="key-legend"
                  >J and K move · A approve · D deny · X select · Enter
                  opens</span
                >`
              : ''
          }
        </div>
        <div
          class="approval-list"
          role="grid"
          aria-multiselectable="true"
          aria-label=${title}
        >
          ${requests.map((request, index) =>
            this.renderRequest(request, waiting, indexOffset + index)
          )}
        </div>
      </div>
    `;
  }

  private renderRequest(
    request: ApprovalRequest,
    waiting: boolean,
    index: number
  ) {
    const focused = this.focusedIndex === index;
    const selected = this.selectedIds.includes(request.id);
    const isNew = waiting && this.newIds.includes(request.id);
    return html`
      <div
        class="approval-item ${request.status} ${
          this.isQuestion(request) ? 'question' : ''
        } ${selected ? 'selected' : ''}"
        role="row"
        data-index=${index}
        data-request-id=${request.id}
        aria-selected=${selected ? 'true' : 'false'}
        tabindex=${focused || (this.focusedIndex < 0 && index === 0) ? 0 : -1}
        @focus=${() => {
          this.focusedIndex = index;
        }}
      >
        <div class="approval-row" role="gridcell">
          <div class="approval-info">
            <div class="approval-tool">
              ${
                isNew
                  ? html`<span
                      class="new-dot"
                      title="Arrived since your last visit"
                      aria-label="New since your last visit"
                    ></span>`
                  : ''
              }
              <sl-icon
                name=${this.isQuestion(request) ? 'chat-left-quote' : 'tools'}
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
              <sl-badge
                pill
                class="chip"
                variant=${this.getStatusVariant(request.status)}
              >
                <sl-icon name=${this.getStatusIcon(request.status)}></sl-icon>
                ${approvalStatusLabel(request.status)}
              </sl-badge>
              <sl-badge pill class="tag-chip">
                <sl-icon name="cpu"></sl-icon>
                ${formatApprovalRequester(
                  request.managed_agent_name,
                  request.tool_args
                )}
              </sl-badge>
              ${
                request.auto_approved_reason
                  ? html`<sl-tooltip
                      content="Auto-approved by a time-boxed bypass. No person reviewed this call."
                    >
                      <sl-badge pill class="chip" variant="warning">
                        <sl-icon name="exclamation-triangle"></sl-icon>
                        Not reviewed
                      </sl-badge>
                    </sl-tooltip>`
                  : ''
              }
            </div>
            ${
              request.summary
                ? html`
                    <div class="approval-meta" style="margin-top: 2px;">
                      <span class="approval-meta-item">
                        <code style="font-size: 0.8em;"
                          >${request.tool_name}</code
                        >
                      </span>
                    </div>
                  `
                : ''
            }
            ${
              request.rule_context
                ? html`
                    <div class="approval-meta" style="margin-top: 2px;">
                      <approval-rule-context-block
                        compact
                        .ruleContext=${request.rule_context}
                      ></approval-rule-context-block>
                    </div>
                  `
                : ''
            }
            <div class="approval-meta">
              <sl-tooltip content=${this.formatFullDate(request.requested_at)}>
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
                          <sl-icon name="check2-square"></sl-icon>
                          Resolved ${this.formatDate(request.resolved_at)}
                        </span>
                      </sl-tooltip>
                    `
                  : ''
              }
              ${
                request.expires_at && waiting
                  ? html`
                      <sl-tooltip
                        content="Expires: ${this.formatFullDate(
                          request.expires_at
                        )}"
                      >
                        <sl-badge
                          pill
                          class="chip"
                          variant=${
                            isExpiringSoon(request, this.nowMs)
                              ? 'warning'
                              : 'neutral'
                          }
                        >
                          <sl-icon name="hourglass"></sl-icon>
                          expires ${this.formatExpiryDate(request.expires_at)}
                        </sl-badge>
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
                      "${request.agent_reasoning.substring(0, 100)}${
                        request.agent_reasoning.length > 100 ? '...' : ''
                      }"
                    </div>
                  `
                : ''
            }
          </div>
          <div class="approval-actions">
            ${
              waiting && !this.isQuestion(request)
                ? html`
                    <sl-button
                      class="row-approve"
                      size="small"
                      variant="success"
                      ?loading=${this.decidingId === request.id}
                      ?disabled=${this.decidingId === request.id}
                      @click=${() => this.handleRowApprove(request)}
                    >
                      Approve
                    </sl-button>
                    <sl-button
                      class="row-deny"
                      size="small"
                      variant="danger"
                      outline
                      ?disabled=${this.decidingId === request.id}
                      @click=${() => this.handleRowDeny(request)}
                    >
                      Deny
                    </sl-button>
                  `
                : ''
            }
            <a class="row-details" href="/console/approval/${request.id}"
              >${waiting ? 'Details' : 'View'}</a
            >
          </div>
        </div>
        ${
          this.isQuestion(request) && waiting
            ? html`
                <div role="gridcell">
                  <question-answer-panel
                    compact
                    .question=${this.questionText(request)}
                    .options=${request.question_options ?? []}
                    .allowFreeText=${request.allow_free_text === true}
                    .submitting=${this.answeringId === request.id}
                    @question-answer=${(e: CustomEvent<QuestionAnswerDetail>) =>
                      this.handleQuestionAnswer(request, e)}
                    @question-dismiss=${() => this.handleQuestionDismiss(request)}
                  ></question-answer-panel>
                  ${
                    this.answerError
                      ? html`<div class="answer-error">
                          ${this.answerError}
                        </div>`
                      : ''
                  }
                </div>
              `
            : ''
        }
      </div>
    `;
  }
}
