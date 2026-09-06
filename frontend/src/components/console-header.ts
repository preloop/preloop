import { LitElement, html, css, type PropertyValues } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/dropdown/dropdown.js';
import '@shoelace-style/shoelace/dist/components/menu/menu.js';
import '@shoelace-style/shoelace/dist/components/menu-item/menu-item.js';
import '@shoelace-style/shoelace/dist/components/icon-button/icon-button.js';
import '@shoelace-style/shoelace/dist/components/divider/divider.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import './talking-indicator';
import './theme-switcher.ts';
import './user-avatar.ts';
import * as api from '../api';
import { Router } from '@vaadin/router';
import {
  ConnectionState,
  unifiedWebSocketManager,
} from '../services/unified-websocket-manager';
import {
  formatFutureRelativeTime,
  formatRelativeTime,
  parseUTCDate,
} from '../utils/date';
import { approvalRequesterName } from '../utils/approval-identity';
import {
  ATTENTION_SUMMARY_EVENT,
  formatAttentionSummary,
  readAttentionSummary,
  type AttentionSummary,
} from '../utils/attention-summary';

interface UserDetails {
  username: string;
  email: string;
  full_name: string;
  avatar_url?: string | null;
}

interface FlowExecution {
  id: string;
  flow_id: string;
  flow_name?: string;
  status: string;
  start_time: string;
  end_time: string | null;
}

interface ApprovalRequest {
  id: string;
  tool_name: string;
  tool_args?: Record<string, unknown>;
  status: 'pending' | 'approved' | 'declined' | 'expired' | 'cancelled';
  requested_at: string;
  expires_at?: string;
  execution_id?: string;
  agent_reasoning?: string;
  managed_agent_name?: string | null;
}

interface UserNotification {
  id: string;
  type:
    | 'approval'
    | 'team_added'
    | 'team_removed'
    | 'policy_added'
    | 'policy_removed'
    | 'role_changed'
    | 'system';
  title: string;
  message: string;
  created_at: string;
  read: boolean;
  /** Console route this notification is about, when it is about something. */
  href?: string;
  metadata?: Record<string, unknown>;
}

/** Bell headline for each way an approval can stop waiting. */
const APPROVAL_RESOLUTION_TITLES: Record<string, string> = {
  approval_approved: 'Approval approved',
  approval_declined: 'Approval declined',
  approval_expired: 'Approval expired',
  approval_cancelled: 'Approval cancelled',
};

@customElement('console-header')
export class ConsoleHeader extends LitElement {
  @state()
  private _user: UserDetails | null = null;

  @state()
  private _runningExecutions: FlowExecution[] = [];

  @state()
  private _pendingApprovals: ApprovalRequest[] = [];

  @state()
  private _userNotifications: UserNotification[] = [];

  @state()
  private _processingApproval: string | null = null;

  /**
   * Counts published by whoever last derived the attention items (the
   * Overview strip, the Attention page). Null until one of them has run in
   * this tab, in which case the empty state says only what it knows.
   */
  @state()
  private _attentionSummary: AttentionSummary | null = null;

  private handleAttentionSummary = (event: Event) => {
    this._attentionSummary = (event as CustomEvent<AttentionSummary>).detail;
  };

  private unsubscribeFlow?: () => void;
  private unsubscribeApprovals?: () => void;
  private unsubscribeNotifications?: () => void;
  private unsubscribeConnectionState?: () => void;
  private approvalExpiryTimer?: ReturnType<typeof setTimeout>;
  private pendingApprovalsRefreshTimer?: ReturnType<typeof setTimeout>;
  private loadingPendingApprovals = false;
  private pendingApprovalsReload = false;

  private refreshPendingApprovals = (): void => {
    this.pruneAndScheduleApprovalExpiry();
    this.schedulePendingApprovalsRefresh();
  };

  /**
   * Coalesce focus/visibility/reconnect into one follow-up fetch, and retry
   * if that fetch was requested while a load was already in flight.
   */
  private schedulePendingApprovalsRefresh(): void {
    if (this.pendingApprovalsRefreshTimer !== undefined) return;
    this.pendingApprovalsRefreshTimer = setTimeout(() => {
      this.pendingApprovalsRefreshTimer = undefined;
      void this.loadPendingApprovals();
    }, 0);
  }

  private handleVisibilityChange = (): void => {
    if (document.visibilityState === 'visible') {
      this.refreshPendingApprovals();
    }
  };

  // Track notification IDs to prevent duplicates
  private shownExecutionNotifications: Set<string> = new Set();
  private shownApprovalNotifications: Set<string> = new Set();

  // Approvals decided from this bell. The resolution broadcast comes back to
  // this tab too, and telling the operator about their own click is noise.
  private decidedHere: Set<string> = new Set();

  static styles = css`
    :host {
      display: block;
    }
    .header-container {
      display: flex;
      justify-content: flex-end;
      align-items: center;
      padding: 0.4rem;
      border-bottom: 1px solid var(--console-hairline);
    }
    .nav-toggle {
      display: flex;
      align-items: center;
      margin-right: auto;
    }
    .nav-toggle sl-icon-button {
      font-size: 1.5rem;
    }
    .user-menu {
      display: flex;
      align-items: center;
      gap: 0.5rem;
    }
    .user-menu sl-icon-button {
      font-size: 1.8rem;
    }
    .theme-switcher-container {
      padding: 0.5rem 1rem;
    }
    .user-menu-trigger {
      display: inline-flex;
      align-items: center;
      padding: 0;
      border: none;
      border-radius: 50%;
      background: none;
      cursor: pointer;
      color: inherit;
      font: inherit;
    }
    .user-menu-trigger:focus-visible {
      outline: var(--sl-focus-ring);
      outline-offset: var(--sl-focus-ring-offset);
    }
    .user-info {
      padding: 0.5rem 1rem;
      line-height: 1.4;
    }
    .user-name {
      font-weight: bold;
    }
    .user-email {
      color: var(--console-meta-color);
    }
    .notification-button {
      position: relative;
    }
    .notification-badge {
      position: absolute;
      top: -4px;
      right: -4px;
      min-width: 18px;
      height: 18px;
      padding: 0 4px;
      font-size: 0.7rem;
      font-weight: 600;
      line-height: 18px;
      text-align: center;
      color: white;
      background-color: var(--sl-color-danger-500);
      border-radius: 9px;
    }
    .notification-dropdown {
      min-width: 380px;
      max-width: 420px;
      max-height: 500px;
      overflow-y: auto;
      /* A popover is the one thing allowed on the raised rung. */
      background: var(--console-surface-raised);
      border: 1px solid var(--console-hairline);
      border-radius: var(--console-card-radius);
      box-shadow: var(--console-raised-shadow);
    }
    .notification-section {
      border-bottom: 1px solid var(--console-hairline);
    }
    .dropdown-footer {
      border-top: 1px solid var(--console-hairline);
      padding: var(--sl-spacing-x-small) var(--sl-spacing-medium);
      position: sticky;
      bottom: 0;
      background: var(--console-surface-raised);
    }
    .dropdown-footer a {
      color: var(--console-link-color);
      font-size: var(--sl-font-size-small);
      text-decoration: none;
    }
    .dropdown-footer a:hover {
      text-decoration: underline;
    }
    .notification-section:last-child {
      border-bottom: none;
    }
    .section-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 0.75rem 1rem;
      background-color: transparent;
      border-bottom: 1px solid var(--console-hairline);
    }
    .section-title {
      display: flex;
      align-items: center;
      gap: 0.5rem;
      font-weight: 600;
      font-size: 0.875rem;
      color: var(--sl-color-neutral-700);
    }
    .section-count {
      font-size: 0.75rem;
      color: var(--console-meta-color);
    }
    .section-link {
      font-size: 0.75rem;
      color: var(--console-link-color);
      text-decoration: none;
      cursor: pointer;
    }
    .section-link:hover {
      text-decoration: underline;
    }
    .execution-list,
    .approval-list,
    .notification-list {
      max-height: 200px;
      overflow-y: auto;
    }
    .execution-item,
    .approval-item,
    .notification-item {
      padding: 0.75rem 1rem;
      cursor: pointer;
      border-bottom: 1px solid var(--console-hairline);
    }
    .execution-item:last-child,
    .approval-item:last-child,
    .notification-item:last-child {
      border-bottom: none;
    }
    .execution-item:hover,
    .approval-item:hover,
    .notification-item:hover {
      background-color: var(--console-hover-tint);
    }
    .execution-name,
    .approval-name,
    .notification-title {
      font-weight: 500;
      margin-bottom: 0.25rem;
      font-size: 0.875rem;
    }
    .execution-time,
    .approval-time,
    .notification-time {
      font-size: 0.75rem;
      color: var(--console-meta-color);
    }
    .approval-actions {
      display: flex;
      gap: 0.5rem;
      margin-top: 0.5rem;
    }
    .approval-actions sl-button {
      font-size: 0.75rem;
    }
    .notification-item.unread {
      background-color: color-mix(
        in srgb,
        var(--sl-color-primary-500) 10%,
        transparent
      );
    }
    .notification-item.unread::before {
      content: '';
      position: absolute;
      left: 0;
      top: 0;
      bottom: 0;
      width: 3px;
      background-color: var(--sl-color-primary-500);
    }
    .notification-item {
      position: relative;
    }
    .no-items {
      padding: 1rem;
      text-align: center;
      color: var(--console-meta-color);
      font-size: 0.875rem;
    }
    .empty-state {
      padding: 2rem 1rem;
      text-align: center;
      color: var(--console-meta-color);
    }
    .empty-state sl-icon {
      font-size: 2rem;
      margin-bottom: 0.5rem;
      opacity: 0.5;
    }
    .empty-state-detail {
      font-size: var(--console-text-meta, 0.8125rem);
      margin-top: 0.25rem;
    }
    .theme-switcher-container {
      text-align: center;
    }
  `;

  async connectedCallback() {
    super.connectedCallback();
    this._attentionSummary = readAttentionSummary();
    window.addEventListener(
      ATTENTION_SUMMARY_EVENT,
      this.handleAttentionSummary as EventListener
    );
    window.addEventListener('focus', this.refreshPendingApprovals);
    document.addEventListener('visibilitychange', this.handleVisibilityChange);
    this.pruneAndScheduleApprovalExpiry();
    this.fetchUserDetails();
    this.connectToFlowUpdates();
    this.connectToApprovalUpdates();
    this.connectToNotificationUpdates();
    this.loadRunningExecutions();
    this.loadPendingApprovals();
    this.loadUserNotifications();
    // Request desktop notification permission when console loads.
    // Browsers may require a user gesture; if so, user can click the bell icon.
    this.requestNotificationPermission();
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    window.removeEventListener(
      ATTENTION_SUMMARY_EVENT,
      this.handleAttentionSummary as EventListener
    );
    this.unsubscribeFlow?.();
    this.unsubscribeApprovals?.();
    this.unsubscribeNotifications?.();
    this.unsubscribeConnectionState?.();
    window.removeEventListener('focus', this.refreshPendingApprovals);
    document.removeEventListener(
      'visibilitychange',
      this.handleVisibilityChange
    );
    clearTimeout(this.approvalExpiryTimer);
    this.approvalExpiryTimer = undefined;
    clearTimeout(this.pendingApprovalsRefreshTimer);
    this.pendingApprovalsRefreshTimer = undefined;
  }

  /**
   * Drop expired rows and arm the next deadline after the current update.
   * Pruning here (rather than in `willUpdate`) avoids mutating
   * `_pendingApprovals` mid-cycle, which would dirty the same update pass.
   */
  protected updated(changed: PropertyValues): void {
    if (!changed.has('_pendingApprovals')) return;
    this.pruneAndScheduleApprovalExpiry();
  }

  private unexpiredPendingApprovals(): ApprovalRequest[] {
    return this._pendingApprovals.filter((approval) =>
      this.isUnexpiredPendingApproval(approval)
    );
  }

  private pruneAndScheduleApprovalExpiry(): void {
    clearTimeout(this.approvalExpiryTimer);
    this.approvalExpiryTimer = undefined;
    if (!this.isConnected) return;

    const pending = this.unexpiredPendingApprovals();
    if (pending.length !== this._pendingApprovals.length) {
      this._pendingApprovals = pending;
    }
    const nextExpiry = pending.reduce((earliest, approval) => {
      if (!approval.expires_at) return earliest;
      return Math.min(earliest, parseUTCDate(approval.expires_at).getTime());
    }, Infinity);
    if (Number.isFinite(nextExpiry)) {
      // One timer for the nearest deadline, capped to the browser's signed
      // 32-bit delay limit. Long deadlines are rescheduled when it fires.
      this.approvalExpiryTimer = setTimeout(
        () => this.pruneAndScheduleApprovalExpiry(),
        Math.min(Math.max(nextExpiry - Date.now(), 1), 2_147_483_647)
      );
    }
  }

  private async loadRunningExecutions() {
    try {
      this._runningExecutions = await api.getFlowExecutions({
        limit: 10,
        status: ['PENDING', 'INITIALIZING', 'STARTING', 'RUNNING'],
      });
    } catch (error) {
      console.error('Failed to load running executions:', error);
    }
  }

  private async loadPendingApprovals() {
    if (!this.isConnected) return;
    if (this.loadingPendingApprovals) {
      this.pendingApprovalsReload = true;
      return;
    }
    this.loadingPendingApprovals = true;
    try {
      do {
        this.pendingApprovalsReload = false;
        const approvals = await api.listApprovalRequests({
          status: 'pending',
        });
        if (!this.isConnected) return;
        this._pendingApprovals = approvals
          .map((approval: any) => ({
            id: approval.id,
            tool_name: approval.tool_name,
            tool_args: approval.tool_args || {},
            status: approval.status,
            requested_at: approval.requested_at,
            expires_at: approval.expires_at,
            execution_id: approval.execution_id,
            agent_reasoning: approval.agent_reasoning,
            managed_agent_name: approval.managed_agent_name,
          }))
          .filter((approval: ApprovalRequest) =>
            this.isUnexpiredPendingApproval(approval)
          );
      } while (this.pendingApprovalsReload && this.isConnected);
    } catch (error) {
      console.error('Failed to load pending approvals:', error);
    } finally {
      this.loadingPendingApprovals = false;
      if (this.pendingApprovalsReload && this.isConnected) {
        void this.loadPendingApprovals();
      }
    }
  }

  private async loadUserNotifications() {
    // TODO: Implement when backend API is available
    // For now, notifications will only come through WebSocket
    try {
      // const notifications = await api.getUserNotifications();
      // this._userNotifications = notifications;
    } catch (error) {
      console.error('Failed to load user notifications:', error);
    }
  }

  private async handleApprove(approvalId: string, event: Event) {
    event.stopPropagation();
    this._processingApproval = approvalId;
    this.decidedHere.add(approvalId);
    try {
      await api.approveRequest(approvalId);
      this._pendingApprovals = this._pendingApprovals.filter(
        (a) => a.id !== approvalId
      );
    } catch (error) {
      console.error('Failed to approve request:', error);
      // The decision never landed, so a later resolution is news again.
      this.decidedHere.delete(approvalId);
    } finally {
      this._processingApproval = null;
    }
  }

  private async handleDecline(approvalId: string, event: Event) {
    event.stopPropagation();
    this._processingApproval = approvalId;
    this.decidedHere.add(approvalId);
    try {
      await api.declineRequest(approvalId);
      this._pendingApprovals = this._pendingApprovals.filter(
        (a) => a.id !== approvalId
      );
    } catch (error) {
      console.error('Failed to decline request:', error);
      // The decision never landed, so a later resolution is news again.
      this.decidedHere.delete(approvalId);
    } finally {
      this._processingApproval = null;
    }
  }

  /**
   * Turn an approval resolution into a bell notification.
   *
   * Only for requests this bell was carrying: an approval nobody here was
   * waiting on is somebody else's news. Decisions made in this tab are
   * skipped too, because the operator who clicked Approve does not need to
   * be told that it was approved.
   */
  private recordApprovalResolution(message: {
    type: string;
    approval_request_id?: string;
    tool_name?: string;
    summary?: string;
  }) {
    const approvalId = message.approval_request_id;
    if (!approvalId) return;
    if (this.decidedHere.has(approvalId)) {
      this.decidedHere.delete(approvalId);
      return;
    }
    const pending = this._pendingApprovals.find(
      (approval) => approval.id === approvalId
    );
    if (!pending) return;

    const title = APPROVAL_RESOLUTION_TITLES[message.type];
    if (!title) return;

    const notification: UserNotification = {
      id: `approval-${approvalId}-${message.type}`,
      type: 'approval',
      title,
      message: message.summary || message.tool_name || pending.tool_name,
      created_at: new Date().toISOString(),
      read: false,
      href: `/console/approval/${approvalId}`,
    };
    this._userNotifications = [
      notification,
      ...this._userNotifications.filter((n) => n.id !== notification.id),
    ];
  }

  private markNotificationAsRead(notificationId: string) {
    this._userNotifications = this._userNotifications.map((n) =>
      n.id === notificationId ? { ...n, read: true } : n
    );
    // TODO: Call API to mark as read when backend supports it
    // api.markNotificationRead(notificationId);
  }

  /**
   * Open what the notification is about, then mark it read. A bell item that
   * names an approval and goes nowhere when clicked is a dead end.
   */
  private handleNotificationClick(notification: UserNotification) {
    this.markNotificationAsRead(notification.id);
    if (notification.href) {
      Router.go(notification.href);
    }
  }

  private get totalNotificationCount(): number {
    const unreadNotifications = this._userNotifications.filter(
      (n) => !n.read
    ).length;
    return (
      this._runningExecutions.length +
      this.unexpiredPendingApprovals().length +
      unreadNotifications
    );
  }

  private connectToFlowUpdates() {
    this.unsubscribeFlow = unifiedWebSocketManager.subscribe(
      'flow_executions',
      (message) => {
        console.log('Console header received flow update:', message);

        // Handle new execution
        if (message.type === 'execution_started') {
          const newExecution: FlowExecution = {
            id: message.execution_id,
            flow_id: message.flow_id,
            status: message.payload?.status || 'PENDING',
            start_time: message.timestamp,
            end_time: null,
            flow_name: message.payload?.flow_name,
          };

          // Add to running executions if not already there
          const exists = this._runningExecutions.some(
            (exec) => exec.id === newExecution.id
          );
          if (!exists) {
            this._runningExecutions = [
              newExecution,
              ...this._runningExecutions,
            ];
            // Show desktop notification for new execution
            this.showExecutionNotification(newExecution);
          }
        }

        // Handle status updates
        if (message.type === 'status_update' && message.execution_id) {
          const status = message.payload?.status;
          const executionIndex = this._runningExecutions.findIndex(
            (exec) => exec.id === message.execution_id
          );

          if (executionIndex !== -1) {
            // If status is no longer running/pending, remove from list
            if (
              status !== 'RUNNING' &&
              status !== 'PENDING' &&
              status !== 'STARTING' &&
              status !== 'INITIALIZING'
            ) {
              const finishedExecution = this._runningExecutions[executionIndex];
              this._runningExecutions = [
                ...this._runningExecutions.slice(0, executionIndex),
                ...this._runningExecutions.slice(executionIndex + 1),
              ];
              // Show desktop notification for finished execution
              this.showExecutionFinishedNotification(finishedExecution, status);
            } else {
              // Update the execution
              const updatedExecution = {
                ...this._runningExecutions[executionIndex],
                status: status,
                end_time: message.payload?.end_time || null,
              };
              this._runningExecutions = [
                ...this._runningExecutions.slice(0, executionIndex),
                updatedExecution,
                ...this._runningExecutions.slice(executionIndex + 1),
              ];
            }
          }
        }
      }
    );

    // Track connection state
    this.unsubscribeConnectionState = unifiedWebSocketManager.onStateChange(
      (state) => {
        if (state === ConnectionState.CONNECTED) {
          this.refreshPendingApprovals();
        }
      }
    );
  }

  private connectToApprovalUpdates() {
    this.unsubscribeApprovals = unifiedWebSocketManager.subscribe(
      'approvals',
      (message) => {
        console.log('Console header received approval update:', message);

        // Handle new approval request
        if (message.type === 'approval_created') {
          const newApproval: ApprovalRequest = {
            id: message.approval_request_id,
            tool_name: message.tool_name,
            tool_args: message.tool_args || {},
            status: 'pending',
            requested_at: message.requested_at || new Date().toISOString(),
            expires_at: message.expires_at,
            execution_id: message.execution_id,
            agent_reasoning: message.agent_reasoning,
            managed_agent_name: message.managed_agent_name || null,
          };

          // Add to pending approvals if not already there
          const exists = this._pendingApprovals.some(
            (approval) => approval.id === newApproval.id
          );
          if (!exists && this.isUnexpiredPendingApproval(newApproval)) {
            this._pendingApprovals = [newApproval, ...this._pendingApprovals];
            // Show desktop notification for new approval request
            this.showApprovalNotification(newApproval);
          }
        }

        // Handle approval resolution (approved, declined, expired, cancelled)
        if (
          message.type === 'approval_approved' ||
          message.type === 'approval_declined' ||
          message.type === 'approval_expired' ||
          message.type === 'approval_cancelled'
        ) {
          // Show desktop notification for resolution
          this.showApprovalResolvedNotification(
            message.approval_request_id,
            message.tool_name || 'Tool',
            message.type
          );
          // Leave a trail in the bell before the row disappears: an approval
          // that resolved elsewhere is the one bell item an operator most
          // wants to reopen, and until now it vanished without a word.
          this.recordApprovalResolution(message);
          // Remove from pending approvals
          this._pendingApprovals = this._pendingApprovals.filter(
            (approval) => approval.id !== message.approval_request_id
          );
        }
      }
    );
  }

  private connectToNotificationUpdates() {
    // TODO: Subscribe to 'notifications' WebSocket channel when backend supports it
    this.unsubscribeNotifications = unifiedWebSocketManager.subscribe(
      'system',
      (message) => {
        console.log('Console header received system message:', message);

        // Handle notification-type messages
        if (
          message.type === 'team_member_added' ||
          message.type === 'policy_assigned' ||
          message.type === 'role_changed'
        ) {
          const notification: UserNotification = {
            id: message.id || crypto.randomUUID(),
            type: this.mapMessageTypeToNotificationType(message.type),
            title: message.title || this.getNotificationTitle(message.type),
            message: message.message || '',
            created_at: message.timestamp || new Date().toISOString(),
            read: false,
            metadata: message.payload,
          };
          this._userNotifications = [notification, ...this._userNotifications];
        }
      }
    );
  }

  private mapMessageTypeToNotificationType(
    messageType: string
  ): UserNotification['type'] {
    const typeMap: Record<string, UserNotification['type']> = {
      team_member_added: 'team_added',
      team_member_removed: 'team_removed',
      policy_assigned: 'policy_added',
      policy_unassigned: 'policy_removed',
      role_changed: 'role_changed',
    };
    return typeMap[messageType] || 'system';
  }

  private getNotificationTitle(messageType: string): string {
    const titleMap: Record<string, string> = {
      team_member_added: 'Added to Team',
      team_member_removed: 'Removed from Team',
      policy_assigned: 'Policy Assigned',
      policy_unassigned: 'Policy Removed',
      role_changed: 'Role Updated',
    };
    return titleMap[messageType] || 'System Notification';
  }

  // ============================================
  // Desktop Notification Methods
  // ============================================

  /**
   * Request notification permission from the browser if not already granted.
   */
  private async requestNotificationPermission(): Promise<void> {
    if (!('Notification' in window)) {
      console.log('Desktop notifications not supported in this browser');
      return;
    }

    if (Notification.permission === 'default') {
      try {
        const permission = await Notification.requestPermission();
        console.log(`Notification permission: ${permission}`);
      } catch (error) {
        console.error('Failed to request notification permission:', error);
      }
    }
  }

  /**
   * Show desktop notification when a flow execution starts.
   */
  private showExecutionNotification(execution: FlowExecution): void {
    if (!('Notification' in window)) {
      console.log('[Notification] Browser does not support Notification API');
      return;
    }

    if (Notification.permission !== 'granted') {
      console.log(
        `[Notification] Permission not granted (current: ${Notification.permission}), requesting...`
      );
      // Proactively request if still default
      if (Notification.permission === 'default') {
        Notification.requestPermission();
      }
      return;
    }

    // Prevent duplicate notifications for the same execution
    if (this.shownExecutionNotifications.has(execution.id)) {
      console.log(`[Notification] Already shown for execution ${execution.id}`);
      return;
    }
    this.shownExecutionNotifications.add(execution.id);
    console.log(
      `[Notification] Showing start notification for ${execution.flow_name || 'Flow'} (${execution.id})`
    );

    try {
      const notification = new Notification('Flow Execution Started', {
        body: `${execution.flow_name || 'Flow'} is now running`,
        icon: '/images/logos/preloop_logo_dark.svg',
        tag: `execution-${execution.id}`,
      });

      notification.onclick = () => {
        window.focus();
        Router.go(`/console/flows/executions/${execution.id}`);
        notification.close();
      };

      // Auto-close after 10 seconds
      setTimeout(() => notification.close(), 10000);
    } catch (error) {
      console.error('Failed to show execution notification:', error);
    }
  }

  /**
   * Show desktop notification when a flow execution finishes.
   */
  private showExecutionFinishedNotification(
    execution: FlowExecution,
    status: string
  ): void {
    if (!('Notification' in window) || Notification.permission !== 'granted') {
      console.log(
        `[Notification] Cannot show finished notification (permission: ${'Notification' in window ? Notification.permission : 'unsupported'})`
      );
      return;
    }

    console.log(
      `[Notification] Showing finished notification for ${execution.flow_name || 'Flow'} (${execution.id}) — status: ${status}`
    );

    // Build notification based on final status
    const succeeded = status === 'SUCCEEDED';
    const title = succeeded
      ? 'Flow Execution Succeeded'
      : 'Flow Execution Failed';
    const body = `${execution.flow_name || 'Flow'} ${succeeded ? 'completed successfully' : `finished with status: ${status}`}`;

    try {
      const notification = new Notification(title, {
        body,
        icon: '/images/logos/preloop_logo_dark.svg',
        tag: `execution-done-${execution.id}`,
      });

      notification.onclick = () => {
        window.focus();
        Router.go(`/console/flows/executions/${execution.id}`);
        notification.close();
      };

      // Auto-close after 10 seconds
      setTimeout(() => notification.close(), 10000);
    } catch (error) {
      console.error('Failed to show execution finished notification:', error);
    }
  }

  /**
   * Show desktop notification when an approval is requested.
   */
  private showApprovalNotification(approval: ApprovalRequest): void {
    if (!('Notification' in window) || Notification.permission !== 'granted') {
      return;
    }

    // Prevent duplicate notifications for the same approval
    if (this.shownApprovalNotifications.has(approval.id)) {
      return;
    }
    this.shownApprovalNotifications.add(approval.id);

    try {
      const body = approval.agent_reasoning
        ? `${approvalRequesterName(approval)}: ${approval.tool_name}: ${approval.agent_reasoning.substring(0, 100)}${approval.agent_reasoning.length > 100 ? '...' : ''}`
        : `${approvalRequesterName(approval)} requests approval for ${approval.tool_name}`;

      const notification = new Notification('Approval Required', {
        body,
        icon: '/images/logos/preloop_logo_dark.svg',
        tag: `approval-${approval.id}`,
        requireInteraction: true, // Stay until dismissed
      });

      notification.onclick = () => {
        window.focus();
        Router.go(`/console/approval/${approval.id}`);
        notification.close();
      };
    } catch (error) {
      console.error('Failed to show approval notification:', error);
    }
  }

  /**
   * Show desktop notification when an approval is resolved.
   */
  private showApprovalResolvedNotification(
    approvalId: string,
    toolName: string,
    eventType: string
  ): void {
    if (!('Notification' in window) || Notification.permission !== 'granted') {
      return;
    }

    const statusMap: Record<string, { title: string; emoji: string }> = {
      approval_approved: { title: 'Approved', emoji: '✅' },
      approval_declined: { title: 'Declined', emoji: '❌' },
      approval_expired: { title: 'Expired', emoji: '⏰' },
      approval_cancelled: { title: 'Cancelled', emoji: '🚫' },
    };

    const status = statusMap[eventType] || { title: 'Resolved', emoji: '📋' };

    try {
      const notification = new Notification(
        `${status.emoji} Approval ${status.title}`,
        {
          body: `${toolName} was ${status.title.toLowerCase()}`,
          icon: '/images/logos/preloop_logo_dark.svg',
          tag: `approval-resolved-${approvalId}`,
        }
      );

      notification.onclick = () => {
        window.focus();
        notification.close();
      };

      // Auto-close after 8 seconds
      setTimeout(() => notification.close(), 8000);
    } catch (error) {
      console.error('Failed to show approval resolved notification:', error);
    }
  }

  async fetchUserDetails() {
    try {
      this._user = await api.getUserProfile();
    } catch (error) {
      console.error('Failed to fetch user details', error);
    }
  }

  async signOut() {
    localStorage.removeItem('accessToken');
    localStorage.removeItem('refreshToken');
    window.dispatchEvent(
      new CustomEvent('auth-change', { bubbles: true, composed: true })
    );
    window.location.href = '/';
    fetch('/logout', { method: 'GET' }).catch((error) => {
      console.error('Logout request to server failed:', error);
    });
  }

  private isUnexpiredPendingApproval(approval: ApprovalRequest): boolean {
    if (approval.status !== 'pending') {
      return false;
    }
    if (!approval.expires_at) {
      return true;
    }
    return parseUTCDate(approval.expires_at).getTime() > Date.now();
  }

  private navigateToExecution(executionId: string) {
    Router.go(`/console/flows/executions/${executionId}`);
  }

  private renderExecutionsSection() {
    if (this._runningExecutions.length === 0) return '';

    return html`
      <div class="notification-section">
        <div class="section-header">
          <div class="section-title">
            <sl-icon name="activity"></sl-icon>
            Active executions
            <span class="section-count"
              >(${this._runningExecutions.length})</span
            >
          </div>
          <a
            class="section-link"
            @click=${() => Router.go('/console/flow-executions')}
            >View all</a
          >
        </div>
        <div class="execution-list">
          ${this._runningExecutions.slice(0, 5).map(
            (exec) => html`
              <div
                class="execution-item"
                @click=${() => this.navigateToExecution(exec.id)}
              >
                <div class="execution-name">
                  ${exec.flow_name || 'Flow Execution'}
                </div>
                <div class="execution-time">
                  <sl-badge variant="warning">${exec.status}</sl-badge>
                  • ${formatRelativeTime(exec.start_time)}
                </div>
              </div>
            `
          )}
        </div>
      </div>
    `;
  }

  private renderApprovalsSection() {
    const pending = this.unexpiredPendingApprovals();
    if (pending.length === 0) return '';

    return html`
      <div class="notification-section">
        <div class="section-header">
          <div class="section-title">
            <sl-icon name="shield-check"></sl-icon>
            Pending approvals
            <span class="section-count">(${pending.length})</span>
          </div>
          <a
            class="section-link"
            @click=${() => Router.go('/console/approvals')}
            >View all</a
          >
        </div>
        <div class="approval-list">
          ${pending.slice(0, 5).map(
            (approval) => html`
              <div
                class="approval-item"
                @click=${() => Router.go(`/console/approval/${approval.id}`)}
              >
                <div class="approval-name">${approval.tool_name}</div>
                <div class="approval-time">
                  ${approvalRequesterName(approval)} •
                  ${formatRelativeTime(approval.requested_at)}
                  ${
                    approval.expires_at
                      ? html` • Expires
                        ${formatFutureRelativeTime(approval.expires_at)}`
                      : ''
                  }
                </div>
                <div class="approval-actions">
                  <sl-button
                    size="small"
                    variant="success"
                    ?loading=${this._processingApproval === approval.id}
                    ?disabled=${this._processingApproval !== null}
                    @click=${(e: Event) => this.handleApprove(approval.id, e)}
                  >
                    <sl-icon slot="prefix" name="check-lg"></sl-icon>
                    Approve
                  </sl-button>
                  <sl-button
                    size="small"
                    variant="danger"
                    ?loading=${this._processingApproval === approval.id}
                    ?disabled=${this._processingApproval !== null}
                    @click=${(e: Event) => this.handleDecline(approval.id, e)}
                  >
                    <sl-icon slot="prefix" name="x-lg"></sl-icon>
                    Decline
                  </sl-button>
                </div>
              </div>
            `
          )}
        </div>
      </div>
    `;
  }

  private renderNotificationsSection() {
    const unreadNotifications = this._userNotifications.filter((n) => !n.read);
    if (this._userNotifications.length === 0) return '';

    return html`
      <div class="notification-section">
        <div class="section-header">
          <div class="section-title">
            <sl-icon name="bell"></sl-icon>
            Notifications
            ${
              unreadNotifications.length > 0
                ? html`<span class="section-count"
                    >(${unreadNotifications.length} unread)</span
                  >`
                : ''
            }
          </div>
        </div>
        <div class="notification-list">
          ${this._userNotifications.slice(0, 5).map(
            (notification) => html`
              <!--
                A row without an href still does something when it is
                clicked: it marks itself read. That is a button, not
                presentation, and it stays focusable so the keyboard can
                reach it too.
              -->
              <div
                class="notification-item ${notification.read ? '' : 'unread'}"
                role=${notification.href ? 'link' : 'button'}
                tabindex="0"
                data-href=${notification.href ?? ''}
                @click=${() => this.handleNotificationClick(notification)}
                @keydown=${(event: KeyboardEvent) => {
                  if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault();
                    this.handleNotificationClick(notification);
                  }
                }}
              >
                <div class="notification-title">
                  <sl-icon
                    name=${this.getNotificationIcon(notification.type)}
                  ></sl-icon>
                  ${notification.title}
                </div>
                <div class="notification-time">
                  ${notification.message} ${notification.message ? ' • ' : ''}
                  ${formatRelativeTime(notification.created_at)}
                </div>
              </div>
            `
          )}
        </div>
      </div>
    `;
  }

  private getNotificationIcon(type: UserNotification['type']): string {
    const iconMap: Record<UserNotification['type'], string> = {
      approval: 'shield-check',
      team_added: 'people',
      team_removed: 'people',
      policy_added: 'file-earmark-text',
      policy_removed: 'file-earmark-text',
      role_changed: 'person-badge',
      system: 'info-circle',
    };
    return iconMap[type] || 'bell';
  }

  /**
   * "No notifications" over an amber strip saying "2 need attention" read as
   * a contradiction: both were true, and the bell was the one that sounded
   * wrong. The empty state now says what is empty (new notifications) and
   * repeats the attention counts the strip and the footer link already point
   * at, from the summary the Overview or the Attention page published.
   */
  private renderEmptyState() {
    const attention = formatAttentionSummary(this._attentionSummary);
    return html`
      <div class="empty-state">
        <sl-icon name="bell-slash"></sl-icon>
        <div>No new notifications</div>
        ${
          attention
            ? html`<div class="empty-state-detail">${attention}</div>`
            : ''
        }
      </div>
    `;
  }

  render() {
    const hasContent =
      this._runningExecutions.length > 0 ||
      this.unexpiredPendingApprovals().length > 0 ||
      this._userNotifications.length > 0;

    return html`
      <div class="header-container">
        <div class="nav-toggle">
          <slot name="nav-toggle"></slot>
        </div>
        <div class="user-menu">
          <!-- Open talk windows, left of the bell: they belong to the
               operator's current work, not to the notification history. -->
          <talking-indicator></talking-indicator>

          <!-- Notification Center -->
          <sl-dropdown distance="8" placement="bottom-end">
            <div
              slot="trigger"
              class="notification-button"
              @click=${() => this.requestNotificationPermission()}
            >
              <sl-icon-button
                name="bell"
                label="Notifications"
              ></sl-icon-button>
              ${
                this.totalNotificationCount > 0
                  ? html`<span class="notification-badge"
                      >${
                        this.totalNotificationCount > 99
                          ? '99+'
                          : this.totalNotificationCount
                      }</span
                    >`
                  : ''
              }
            </div>
            <div class="notification-dropdown">
              ${
                hasContent
                  ? html`
                      ${this.renderExecutionsSection()}
                      ${this.renderApprovalsSection()}
                      ${this.renderNotificationsSection()}
                    `
                  : this.renderEmptyState()
              }
              <div class="dropdown-footer">
                <a href="/console/attention">Everything needing attention →</a>
              </div>
            </div>
          </sl-dropdown>

          <!-- User Menu -->
          <sl-dropdown distance="8">
            <button
              slot="trigger"
              class="user-menu-trigger"
              type="button"
              aria-label="User Menu"
            >
              <user-avatar
                .image=${this._user?.avatar_url || ''}
                .label=${this._user?.full_name || this._user?.username || ''}
                .seed=${this._user?.username || ''}
                .size=${32}
              ></user-avatar>
            </button>
            <sl-menu>
              <div class="theme-switcher-container">
                <theme-switcher></theme-switcher>
              </div>
              <sl-divider></sl-divider>

              <div class="user-info">
                <user-avatar
                  .image=${this._user?.avatar_url || ''}
                  .label=${this._user?.full_name || this._user?.username || ''}
                  .seed=${this._user?.username || ''}
                  .size=${40}
                  style="margin-right: 0.5rem"
                ></user-avatar>
                <div class="user-name">
                  ${this._user?.full_name || this._user?.username}
                </div>
                <div class="user-email">${this._user?.email}</div>
              </div>
              <sl-divider></sl-divider>
              <sl-menu-item
                @click=${() => Router.go('/console/settings/profile')}
              >
                <sl-icon name="person-circle" slot="prefix"></sl-icon>
                Profile
              </sl-menu-item>
              <sl-menu-item
                @click=${() => Router.go('/console/settings/security')}
              >
                <sl-icon name="lock" slot="prefix"></sl-icon>
                Security
              </sl-menu-item>
              <sl-menu-item
                @click=${() =>
                  Router.go('/console/settings/notification-preferences')}
              >
                <sl-icon name="bell" slot="prefix"></sl-icon>
                Notification preferences
              </sl-menu-item>
              <sl-divider></sl-divider>
              <sl-menu-item @click=${this.signOut}>
                <sl-icon name="box-arrow-right" slot="prefix"></sl-icon>
                Sign out
              </sl-menu-item>
            </sl-menu>
          </sl-dropdown>
        </div>
      </div>
    `;
  }
}
