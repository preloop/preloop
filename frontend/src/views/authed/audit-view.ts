/**
 * Audit Log View - Unified Timeline
 *
 * Displays a single filterable timeline where tool call attempts are primary rows
 * and related events (policy decisions, approval lifecycle) appear as indented
 * sub-rows, correlated by a shared correlation_id.
 */

import { html, css, unsafeCSS, nothing } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import { AuthedElement, fetchWithAuth, PermissionError } from '../../api';
import { permissionErrorFromResponse } from '../../permissions';
import { parseUTCDate } from '../../utils/date';
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
import consoleStyles from '../../styles/console-styles.css?inline';
import { reducedMotionStyles } from '../../styles/reduced-motion';
import '../../components/view-header.ts';
import '../../components/permission-denied';

// Types
interface AuditLog {
  id: string;
  account_id: string;
  user_id: string | null;
  action: string;
  resource_type: string | null;
  resource_id: string | null;
  status: string;
  ip_address: string | null;
  user_agent: string | null;
  details: Record<string, any> | null;
  timestamp: string;
}

interface SubEvent {
  id: string;
  action: string;
  status: string;
  details: Record<string, any> | null;
  timestamp: string;
}

interface AuditGroup {
  correlation_id: string | null;
  primary_event: AuditLog;
  sub_events: SubEvent[];
  outcome: string;
}

interface GroupedResponse {
  groups: AuditGroup[];
  total: number;
  skip: number;
  limit: number;
}

interface User {
  id: string;
  username: string;
  email: string;
  full_name: string | null;
}

// Event type filter options
const EVENT_TYPE_OPTIONS = [
  { value: 'tool_call', label: 'Tool Calls' },
  { value: 'model_gateway_request', label: 'Gateway Requests' },
  { value: 'runtime_session_created', label: 'Sessions Started' },
  { value: 'runtime_session_updated', label: 'Sessions Updated' },
  { value: 'runtime_session_ended', label: 'Sessions Ended' },
  { value: 'config:tool_configuration', label: 'Tool Enabled / Disabled' },
  { value: 'config:tool_rule', label: 'Rule Changes' },
  { value: 'config:approval_workflow', label: 'Approval Workflow Changes' },
  { value: 'config:mcp_server', label: 'MCP Server Changes' },
  { value: 'config:tracker', label: 'Tracker Changes' },
  { value: 'config:flow', label: 'Flow Changes' },
];

// Outcome filter options
const OUTCOME_OPTIONS = [
  { value: 'allow', label: 'Allowed' },
  { value: 'deny', label: 'Denied' },
  { value: 'require_approval', label: 'Approval Required' },
  { value: 'approved', label: 'Approved' },
  { value: 'declined', label: 'Declined' },
  { value: 'executed', label: 'Executed' },
  { value: 'failed', label: 'Failed' },
  { value: 'budget_denied', label: 'Budget Denied' },
  { value: 'expired', label: 'Expired' },
];

@customElement('audit-view')
export class AuditView extends AuthedElement {
  // Timeline data
  @state() private _groups: AuditGroup[] = [];
  @state() private _loading = false;
  @state() private _permissionError: PermissionError | null = null;
  @state() private _total = 0;
  @state() private _page = 0;
  @state() private _pageSize = 50;

  // Filters
  @state() private _eventTypeFilters: string[] = [];
  @state() private _outcomeFilters: string[] = [];
  @state() private _toolNameFilter = '';
  @state() private _startDate = '';
  @state() private _endDate = '';
  @state() private _minCost = '';
  @state() private _maxCost = '';

  // Expanded groups (correlation_id or primary event id -> expanded)
  @state() private _expandedGroups = new Set<string>();

  // Users for display
  @state() private _users: User[] = [];
  private _userMap = new Map<string, User>();

  // Realtime subscription handle + debounced refresh timer.
  private _unsubscribeRealtime: (() => void) | null = null;
  private _refreshTimer: number | null = null;
  // Live indicator pulse — flips briefly when a websocket event arrives so
  // the user sees the page is wired to the realtime bus.
  @state() private _livePulse = false;
  private _livePulseTimer: number | null = null;

  // ── Lifecycle ──────────────────────────────────────────────────────

  connectedCallback() {
    super.connectedCallback();

    // Parse URL parameters to initialize filters
    const params = new URLSearchParams(window.location.search);
    const eventType = params.get('event_type');
    if (eventType) {
      this._eventTypeFilters = [eventType];
    }
    const outcome = params.get('outcome');
    if (outcome) {
      this._outcomeFilters = [outcome];
    }
    const minCost = params.get('min_cost');
    if (minCost) {
      this._minCost = minCost;
    }
    const maxCost = params.get('max_cost');
    if (maxCost) {
      this._maxCost = maxCost;
    }

    this._loadUsers();
    this._loadTimeline();
    this._connectRealtime();
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    if (this._unsubscribeRealtime) {
      this._unsubscribeRealtime();
      this._unsubscribeRealtime = null;
    }
    if (this._refreshTimer !== null) {
      window.clearTimeout(this._refreshTimer);
      this._refreshTimer = null;
    }
    if (this._livePulseTimer !== null) {
      window.clearTimeout(this._livePulseTimer);
      this._livePulseTimer = null;
    }
  }

  private _connectRealtime() {
    const onAuditEvent = () => this._scheduleRealtimeRefresh();
    this._unsubscribeRealtime = unifiedWebSocketManager.subscribe(
      'audit',
      onAuditEvent
    );
    void unifiedWebSocketManager.connect();
  }

  private _scheduleRealtimeRefresh() {
    // Pulse the live indicator immediately so the user sees feedback even
    // before the debounced refetch actually runs.
    this._livePulse = true;
    if (this._livePulseTimer !== null) {
      window.clearTimeout(this._livePulseTimer);
    }
    this._livePulseTimer = window.setTimeout(() => {
      this._livePulse = false;
      this._livePulseTimer = null;
    }, 1500);

    if (this._refreshTimer !== null) {
      window.clearTimeout(this._refreshTimer);
    }
    // Debounce so a burst of websocket events (notification fan-out across
    // channels, then approval, then execution) results in a single refetch.
    this._refreshTimer = window.setTimeout(() => {
      this._refreshTimer = null;
      // Only auto-refresh page 0 — paging back through history shouldn't
      // shift under the user's feet when new events arrive.
      if (this._page === 0) {
        void this._loadTimeline();
      }
    }, 400);
  }

  // ── Data loading ───────────────────────────────────────────────────

  private async _loadUsers() {
    try {
      const res = await fetchWithAuth('/api/v1/users');
      if (res.ok) {
        const data = await res.json();
        this._users = data.users || data || [];
        this._userMap = new Map(this._users.map((u: User) => [u.id, u]));
      }
    } catch (e) {
      console.error('Failed to load users:', e);
    }
  }

  private async _loadTimeline() {
    this._loading = true;
    this._permissionError = null;
    try {
      const params = new URLSearchParams();
      params.set('skip', String(this._page * this._pageSize));
      params.set('limit', String(this._pageSize));
      for (const t of this._eventTypeFilters) {
        params.append('event_type', t);
      }
      for (const o of this._outcomeFilters) {
        params.append('outcome', o);
      }
      if (this._toolNameFilter) params.set('tool_name', this._toolNameFilter);
      if (this._startDate)
        params.set('start_date', new Date(this._startDate).toISOString());
      if (this._endDate)
        params.set('end_date', new Date(this._endDate).toISOString());
      if (this._minCost) params.set('min_cost', this._minCost);
      if (this._maxCost) params.set('max_cost', this._maxCost);

      const res = await fetchWithAuth(`/api/v1/audit-logs/grouped?${params}`);
      if (res.status === 403) {
        this._permissionError = await permissionErrorFromResponse(res);
        this._groups = [];
        this._total = 0;
        return;
      }
      if (res.ok) {
        const data: GroupedResponse = await res.json();
        this._groups = data.groups;
        this._total = data.total;
      }
    } catch (e) {
      console.error('Failed to load timeline:', e);
    } finally {
      this._loading = false;
    }
  }

  private _applyFilters() {
    this._page = 0;
    this._loadTimeline();
  }

  private _clearFilters() {
    this._eventTypeFilters = [];
    this._outcomeFilters = [];
    this._toolNameFilter = '';
    this._startDate = '';
    this._endDate = '';
    this._minCost = '';
    this._maxCost = '';
    this._page = 0;
    this._loadTimeline();
  }

  // ── Helpers ────────────────────────────────────────────────────────

  private _getGroupKey(group: AuditGroup): string {
    return group.correlation_id || group.primary_event.id;
  }

  private _toggleGroup(key: string) {
    const next = new Set(this._expandedGroups);
    if (next.has(key)) {
      next.delete(key);
    } else {
      next.add(key);
    }
    this._expandedGroups = next;
  }

  private _getUserDisplay(userId: string | null): string {
    if (!userId) return 'System';
    const u = this._userMap.get(userId);
    return u ? u.full_name || u.username : userId.slice(0, 8);
  }

  private _formatTimestamp(ts: string): string {
    const d = parseUTCDate(ts);
    const now = new Date();
    const diff = now.getTime() - d.getTime();
    if (diff < 60000) return 'just now';
    if (diff < 3600000) return `${Math.floor(diff / 60000)}m ago`;
    if (diff < 86400000) return `${Math.floor(diff / 3600000)}h ago`;
    return d.toLocaleDateString(undefined, {
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    });
  }

  private _formatFullTimestamp(ts: string): string {
    return parseUTCDate(ts).toLocaleString();
  }

  private _formatActorLabel(event: AuditLog | SubEvent): string {
    const actor =
      'user_id' in event ? this._getUserDisplay(event.user_id) : 'System';
    const apiKeyName = event.details?.api_key_name;
    if (apiKeyName) {
      return actor === 'System'
        ? `API token ${apiKeyName}`
        : `${actor} via ${apiKeyName}`;
    }
    return actor;
  }

  private _formatCurrency(value?: number | null): string {
    const amount = Number(value || 0);
    if (amount === 0) return '$0.00';
    return amount >= 0.01 ? `$${amount.toFixed(2)}` : `$${amount.toFixed(4)}`;
  }

  private _getEventCost(details: Record<string, any> | null): number | null {
    if (!details || details.estimated_cost == null) return null;
    const value = Number(details.estimated_cost);
    return Number.isFinite(value) ? value : null;
  }

  private _getEventTokens(details: Record<string, any> | null): number | null {
    if (!details) return null;
    if (details.total_tokens != null) {
      const value = Number(details.total_tokens);
      return Number.isFinite(value) ? value : null;
    }
    const prompt = Number(details.prompt_tokens || 0);
    const completion = Number(details.completion_tokens || 0);
    const total = prompt + completion;
    return total > 0 ? total : null;
  }

  private _renderEventCostTokens(details: Record<string, any> | null) {
    const tokens = this._getEventTokens(details);
    const cost = this._getEventCost(details);
    if (tokens == null && cost == null) return nothing;
    return html`
      ${
        tokens != null
          ? html`<span class="event-cost">${tokens.toLocaleString()} tok</span>`
          : nothing
      }
      ${
        cost != null
          ? html`<span class="event-cost">${this._formatCurrency(cost)}</span>`
          : nothing
      }
    `;
  }

  private _hasExpandableDetails(details: Record<string, any> | null): boolean {
    if (!details) return false;
    return Object.keys(details).some(
      (key) =>
        ![
          'tool_name',
          'duration_ms',
          'execution_time_ms',
          'decision',
          'event',
        ].includes(key)
    );
  }

  private _formatDetailValue(value: unknown): string {
    if (value == null) return '';
    if (typeof value === 'string') return value;
    if (typeof value === 'number' || typeof value === 'boolean')
      return String(value);
    try {
      return JSON.stringify(value);
    } catch {
      return String(value);
    }
  }

  private _prettyDetailLabel(key: string): string {
    return key
      .replace(/_/g, ' ')
      .replace(/\b\w/g, (char) => char.toUpperCase());
  }

  private _renderDetailItem(label: string, value: unknown) {
    const rendered = this._formatDetailValue(value);
    if (!rendered) return nothing;
    return html`
      <div class="detail-item">
        <span class="detail-label">${label}</span>
        <span class="detail-value">${rendered}</span>
      </div>
    `;
  }

  private _renderJsonDetail(label: string, value: unknown) {
    if (value == null) return nothing;
    let rendered = '';
    try {
      rendered =
        typeof value === 'string'
          ? value
          : JSON.stringify(value, null, 2) || '';
    } catch {
      rendered = String(value);
    }
    if (!rendered) return nothing;
    return html`
      <div class="detail-block">
        <span class="detail-label">${label}</span>
        <pre class="detail-json">${rendered}</pre>
      </div>
    `;
  }

  private _renderEventDetails(details: Record<string, any> | null) {
    if (!details || !this._hasExpandableDetails(details)) return nothing;
    const preferredKeys = [
      'api_key_name',
      'api_key_id',
      'runtime_session_id',
      'session_reference',
      'session_source_type',
      'session_source_id',
      'runtime_principal_name',
      'runtime_principal_type',
      'runtime_principal_id',
      'flow_execution_id',
      'flow_id',
      'api_usage_id',
      'endpoint',
      'endpoint_kind',
      'status_code',
      'requested_model',
      'model_alias',
      'provider_name',
      'gateway_provider',
      'auth_subject_type',
      'upstream_request_id',
      'gateway_attempt',
      'is_retry',
      'retry_of_api_usage_id',
      'request_fingerprint',
      'prompt_tokens',
      'completion_tokens',
      'total_tokens',
      'estimated_cost',
      'error_type',
      'error_detail',
      'approval_workflow_id',
      'approval_id',
      'reason',
      'timeout_seconds',
      'condition_matched',
      'correlation_id',
      'execution_id',
      'rule_description',
      'permission',
      'config_type',
      'method',
      'failure_reason',
      'resource_type',
      'resource_id',
      // Approval-notification fan-out
      'channel',
      'recipient_count',
      'sent_count',
      'failed_count',
      'skipped_count',
      // Post-approval execution outcome
      'duration_ms',
      'error',
    ];
    const renderedKeys = new Set<string>();
    const preferredItems = preferredKeys
      .filter((key) => details[key] != null && details[key] !== '')
      .map((key) => {
        renderedKeys.add(key);
        return this._renderDetailItem(
          this._prettyDetailLabel(key),
          details[key]
        );
      });
    const remainingItems = Object.entries(details)
      .filter(
        ([key, value]) =>
          !renderedKeys.has(key) &&
          value != null &&
          typeof value !== 'object' &&
          !['tool_name', 'decision', 'event'].includes(key)
      )
      .map(([key, value]) =>
        this._renderDetailItem(this._prettyDetailLabel(key), value)
      );

    // Render recipient list as a small chip cluster when present.
    const recipientChips =
      Array.isArray(details.recipient_user_ids) &&
      details.recipient_user_ids.length > 0
        ? html`
            <div class="detail-block">
              <span class="detail-label">Recipients</span>
              <div class="recipient-chips">
                ${details.recipient_user_ids
                  .slice(0, 12)
                  .map(
                    (uid: string) => html`
                      <sl-tag size="small" variant="neutral"
                        >${this._getUserDisplay(uid)}</sl-tag
                      >
                    `
                  )}
                ${
                  details.recipient_user_ids.length > 12
                    ? html`<sl-tag size="small" variant="neutral"
                        >+${details.recipient_user_ids.length - 12} more</sl-tag
                      >`
                    : nothing
                }
              </div>
            </div>
          `
        : nothing;

    return html`
      <div class="event-details">
        ${
          details.runtime_session_id
            ? html`
                <div class="detail-block">
                  <span class="detail-label">Session observer</span>
                  <a
                    class="detail-value"
                    href=${`/console/runtime-sessions?sessionId=${encodeURIComponent(
                      details.runtime_session_id
                    )}`}
                  >
                    Open replay, costs, and optimization suggestions
                  </a>
                </div>
              `
            : nothing
        }
        ${preferredItems} ${remainingItems} ${recipientChips}
        ${this._renderJsonDetail('Arguments', details.tool_args)}
        ${this._renderJsonDetail('Result preview', details.result_preview)}
        ${this._renderJsonDetail('Budget', details.budget)}
        ${this._renderJsonDetail('New Value', details.new_value)}
        ${this._renderJsonDetail('Old Value', details.old_value)}
      </div>
    `;
  }

  private _getOutcomeBadge(outcome: string): {
    variant: string;
    label: string;
  } {
    switch (outcome) {
      case 'allow':
      case 'executed':
        return { variant: 'success', label: 'Allowed' };
      case 'approved':
        return { variant: 'success', label: 'Approved' };
      case 'deny':
        return { variant: 'danger', label: 'Denied' };
      case 'declined':
        return { variant: 'danger', label: 'Declined' };
      case 'require_approval':
        return { variant: 'warning', label: 'Approval Required' };
      case 'expired':
        return { variant: 'neutral', label: 'Expired' };
      case 'created':
        return { variant: 'success', label: 'Created' };
      case 'updated':
        return { variant: 'primary', label: 'Updated' };
      case 'failed':
      case 'failure':
        return { variant: 'danger', label: 'Failed' };
      case 'budget_denied':
        return { variant: 'danger', label: 'Budget Denied' };
      case 'success':
        return { variant: 'success', label: 'Success' };
      case 'denied':
        return { variant: 'danger', label: 'Denied' };
      case 'sent':
        return { variant: 'success', label: 'Sent' };
      case 'partial':
        return { variant: 'warning', label: 'Partial' };
      case 'no_devices':
        return { variant: 'neutral', label: 'No devices' };
      case 'skipped':
        return { variant: 'neutral', label: 'Skipped' };
      default:
        return { variant: 'neutral', label: outcome };
    }
  }

  private _getOutcomeColor(outcome: string): string {
    switch (outcome) {
      case 'allow':
      case 'executed':
      case 'approved':
      case 'success':
        return 'var(--sl-color-success-600)';
      case 'deny':
      case 'declined':
      case 'denied':
      case 'failed':
      case 'failure':
      case 'budget_denied':
        return 'var(--sl-color-danger-600)';
      case 'require_approval':
        return 'var(--sl-color-warning-600)';
      default:
        return 'var(--sl-color-neutral-400)';
    }
  }

  private _getActionIcon(action: string): string {
    if (action === 'tool_call') return 'terminal';
    if (action === 'model_gateway_request') return 'cpu';
    if (action.startsWith('policy_')) return 'shield-check';
    if (action === 'approval_notification_sent') return 'send';
    if (action === 'approval_tool_executed') return 'play-circle';
    if (action.startsWith('approval_')) return 'person-check';
    if (action === 'authentication') return 'key';
    if (action === 'configuration_change') return 'gear';
    if (action === 'permission_check') return 'lock';
    if (action.startsWith('runtime_session_')) return 'activity';
    if (action.startsWith('role_')) return 'people';
    return 'info-circle';
  }

  private _formatChannelLabel(channel: string | undefined | null): string {
    if (!channel) return 'channel';
    const map: Record<string, string> = {
      email: 'Email',
      mobile_push: 'Mobile push',
      slack: 'Slack',
      mattermost: 'Mattermost',
      webhook: 'Webhook',
    };
    return map[channel] || channel;
  }

  private _formatRecipientNames(userIds: string[] | undefined): string {
    if (!userIds || userIds.length === 0) return '';
    const names = userIds
      .slice(0, 3)
      .map((id) => this._getUserDisplay(id))
      .join(', ');
    return userIds.length > 3 ? `${names} +${userIds.length - 3} more` : names;
  }

  private _getSubEventLabel(sub: SubEvent): string {
    const d = sub.details || {};
    switch (sub.action) {
      case 'policy_allow':
        return `Policy: Allow${d.rule_description ? ` — ${d.rule_description}` : ''}`;
      case 'policy_deny':
        return `Policy: Deny${d.rule_description ? ` — ${d.rule_description}` : ''}`;
      case 'policy_require_approval': {
        const desc = d.rule_description?.includes('Rule matched: None')
          ? 'Default Rule'
          : d.rule_description;
        return `Policy: Require Approval${desc ? ` — ${desc}` : ''}`;
      }
      case 'approval_created': {
        const timeout = d.timeout_seconds
          ? ` (timeout: ${Math.round(d.timeout_seconds / 60)}min)`
          : '';
        return `Approval requested${d.tool_name ? ` for ${d.tool_name}` : ''}${timeout}`;
      }
      case 'approval_approved':
        return `Approved${d.approver_id ? ` by ${this._getUserDisplay(d.approver_id)}` : ''}${d.reason ? ` — ${d.reason}` : ''}`;
      case 'approval_denied':
        return `Declined${d.approver_id ? ` by ${this._getUserDisplay(d.approver_id)}` : ''}${d.reason ? ` — ${d.reason}` : ''}`;
      case 'approval_expired':
        return 'Approval expired (timed out)';
      case 'approval_escalated':
        return `Escalated${d.escalation_reason ? ` — ${d.escalation_reason}` : ''}`;
      case 'approval_notification_sent': {
        const channel = this._formatChannelLabel(d.channel);
        const recipients = this._formatRecipientNames(d.recipient_user_ids);
        const sent = typeof d.sent_count === 'number' ? d.sent_count : null;
        const failed = typeof d.failed_count === 'number' ? d.failed_count : 0;
        const skipped =
          typeof d.skipped_count === 'number' ? d.skipped_count : 0;
        let summary = `Notified via ${channel}`;
        if (sub.status === 'no_devices') {
          summary += ' — no registered devices';
        } else if (sub.status === 'failed') {
          summary += ` — failed${d.error ? ` (${d.error})` : ''}`;
        } else if (sent !== null) {
          summary += `: ${sent} sent`;
          if (failed) summary += `, ${failed} failed`;
          if (skipped) summary += `, ${skipped} skipped`;
        }
        if (recipients) summary += ` (${recipients})`;
        return summary;
      }
      case 'approval_tool_executed': {
        const tn = d.tool_name ? ` ${d.tool_name}` : '';
        if (sub.status === 'failed') {
          return `Tool${tn} execution failed${d.error ? ` — ${d.error}` : ''}`;
        }
        return `Tool${tn} executed successfully`;
      }
      case 'runtime_session_created':
        return 'Runtime session started';
      case 'runtime_session_updated':
        return 'Runtime session updated';
      case 'runtime_session_ended':
        return 'Runtime session ended';
      default:
        return sub.action.replace(/_/g, ' ');
    }
  }

  private _getPrimaryLabel(event: AuditLog): string {
    switch (event.action) {
      case 'tool_call':
        return event.resource_id || event.details?.tool_name || 'Unknown tool';
      case 'authentication':
        return `Login: ${event.details?.username || 'unknown'}`;
      case 'configuration_change': {
        const ct = event.details?.config_type || event.resource_id || 'unknown';
        const act = event.details?.action || 'changed';
        const labels: Record<string, string> = {
          mcp_server: 'MCP Server',
          tool_configuration: 'Tool',
          tool_rule: 'Tool Rule',
          approval_workflow: 'Approval Workflow',
          tracker: 'Tracker',
          flow: 'Flow',
        };
        const pretty = labels[ct] || ct;
        const name = event.details?.new_value
          ? typeof event.details.new_value === 'object'
            ? event.details.new_value.name
            : ''
          : event.details?.old_value &&
              typeof event.details.old_value === 'object'
            ? event.details.old_value.name
            : '';
        return `${pretty} ${act}${name ? `: ${name}` : ''}`;
      }
      case 'permission_check':
        return `Permission: ${event.details?.permission || event.resource_id || 'check'}`;
      case 'runtime_session_created':
        return 'Runtime session started';
      case 'runtime_session_updated':
        return 'Runtime session updated';
      case 'runtime_session_ended':
        return 'Runtime session ended';
      case 'model_gateway_request': {
        const modelLabel =
          event.details?.requested_model ||
          event.details?.model_alias ||
          event.resource_id ||
          'request';
        const providerLabel =
          event.details?.gateway_provider ||
          event.details?.provider_name ||
          'Gateway';
        if (event.status === 'budget_denied') {
          return `${providerLabel} budget denied: ${modelLabel}`;
        }
        if (event.status === 'success' || event.status === 'executed') {
          return `${providerLabel} request succeeded: ${modelLabel}`;
        }
        return `${providerLabel} request failed: ${modelLabel}`;
      }
      case 'role_assigned':
        return `Role assigned: ${event.details?.role || ''}`;
      case 'role_removed':
        return `Role removed: ${event.details?.role || ''}`;
      default:
        return event.action.replace(/_/g, ' ');
    }
  }

  private _getArgsSummary(details: Record<string, any> | null): string {
    if (!details?.tool_args) return '';
    const args = details.tool_args;
    const entries = Object.entries(args);
    if (entries.length === 0) return '';
    const parts = entries.slice(0, 3).map(([k, v]) => {
      const vs = typeof v === 'string' ? v : JSON.stringify(v);
      return `${k}=${vs.length > 30 ? vs.slice(0, 30) + '…' : vs}`;
    });
    if (entries.length > 3) parts.push('…');
    return parts.join(', ');
  }

  private _canExpandGroup(group: AuditGroup): boolean {
    return (
      group.sub_events.length > 0 ||
      this._hasExpandableDetails(group.primary_event.details)
    );
  }

  // ── Pagination ─────────────────────────────────────────────────────

  private get _totalPages(): number {
    return Math.max(1, Math.ceil(this._total / this._pageSize));
  }

  private _prevPage() {
    if (this._page > 0) {
      this._page--;
      this._loadTimeline();
    }
  }

  private _nextPage() {
    if (this._page < this._totalPages - 1) {
      this._page++;
      this._loadTimeline();
    }
  }

  // ── Render ─────────────────────────────────────────────────────────

  render() {
    return html`
      <view-header
        headerText="Audit Timeline"
        description="The permanent record of governed activity: tool calls, approvals, and policy decisions, with outcomes and timestamps."
        width="wide"
      >
        <sl-badge slot="title-prefix" pill variant="neutral"
          >${this._total} events</sl-badge
        >
        <sl-tooltip slot="title-prefix" content="Live updates over websocket">
          <span
            class="live-indicator ${this._livePulse ? 'pulsing' : ''}"
            aria-label="Realtime updates active"
          >
            <span class="live-dot"></span>
            <span class="live-label">LIVE</span>
          </span>
        </sl-tooltip>
      </view-header>
      <div class="column-layout wide">
        <div class="main-column audit-view" style="padding-top: 0;">
          ${
            this._permissionError
              ? html`<permission-denied
                  required-permission=${
                    this._permissionError.requiredPermission ||
                    'view_audit_logs'
                  }
                  message=${this._permissionError.message}
                ></permission-denied>`
              : html`
                  ${this._renderFilterBar()}
                  ${
                    this._loading
                      ? html`<div class="loading">
                          <sl-spinner style="font-size: 2rem;"></sl-spinner>
                        </div>`
                      : this._groups.length === 0
                        ? html`<div class="empty-state">
                            No audit events yet. Governed tool calls, approvals,
                            and policy decisions are recorded here as your
                            agents work.
                          </div>`
                        : html`
                            <div class="timeline">
                              ${this._groups.map((g) => this._renderGroup(g))}
                            </div>
                            ${this._renderPagination()}
                          `
                  }
                `
          }
        </div>
      </div>
    `;
  }

  private _renderFilterBar() {
    return html`
      <div class="filter-bar">
        <sl-input
          placeholder="Search tool name…"
          size="small"
          clearable
          .value=${this._toolNameFilter}
          @sl-input=${(e: Event) => {
            this._toolNameFilter = (e.target as HTMLInputElement).value;
          }}
          @sl-clear=${() => {
            this._toolNameFilter = '';
            this._applyFilters();
          }}
          @keydown=${(e: KeyboardEvent) => {
            if (e.key === 'Enter') this._applyFilters();
          }}
        >
          <sl-icon name="search" slot="prefix"></sl-icon>
        </sl-input>

        <sl-select
          placeholder="Event Type"
          size="small"
          clearable
          multiple
          max-options-visible="2"
          .value=${this._eventTypeFilters}
          @sl-change=${(e: Event) => {
            const sel = e.target as any;
            this._eventTypeFilters = Array.isArray(sel.value)
              ? sel.value
              : sel.value
                ? [sel.value]
                : [];
            this._applyFilters();
          }}
        >
          ${EVENT_TYPE_OPTIONS.map(
            (opt) => html`
              <sl-option value=${opt.value}>${opt.label}</sl-option>
            `
          )}
        </sl-select>

        <sl-select
          placeholder="Outcomes"
          size="small"
          clearable
          multiple
          max-options-visible="2"
          .value=${this._outcomeFilters}
          @sl-change=${(e: Event) => {
            const sel = e.target as any;
            this._outcomeFilters = Array.isArray(sel.value)
              ? sel.value
              : sel.value
                ? [sel.value]
                : [];
            this._applyFilters();
          }}
        >
          ${OUTCOME_OPTIONS.map(
            (opt) => html`
              <sl-option value=${opt.value}>${opt.label}</sl-option>
            `
          )}
        </sl-select>

        <sl-input
          type="date"
          size="small"
          placeholder="From"
          .value=${this._startDate}
          @sl-change=${(e: Event) => {
            this._startDate = (e.target as HTMLInputElement).value;
            this._applyFilters();
          }}
        ></sl-input>

        <sl-input
          type="date"
          size="small"
          placeholder="To"
          .value=${this._endDate}
          @sl-change=${(e: Event) => {
            this._endDate = (e.target as HTMLInputElement).value;
            this._applyFilters();
          }}
        ></sl-input>

        <sl-input
          type="number"
          size="small"
          placeholder="Min $"
          min="0"
          step="0.0001"
          .value=${this._minCost}
          @sl-input=${(e: Event) => {
            this._minCost = (e.target as HTMLInputElement).value;
          }}
          @keydown=${(e: KeyboardEvent) => {
            if (e.key === 'Enter') this._applyFilters();
          }}
        ></sl-input>

        <sl-input
          type="number"
          size="small"
          placeholder="Max $"
          min="0"
          step="0.0001"
          .value=${this._maxCost}
          @sl-input=${(e: Event) => {
            this._maxCost = (e.target as HTMLInputElement).value;
          }}
          @keydown=${(e: KeyboardEvent) => {
            if (e.key === 'Enter') this._applyFilters();
          }}
        ></sl-input>

        ${
          this._eventTypeFilters.length ||
          this._outcomeFilters.length ||
          this._toolNameFilter ||
          this._startDate ||
          this._endDate ||
          this._minCost ||
          this._maxCost
            ? html`<sl-button
                size="small"
                variant="text"
                @click=${this._clearFilters}
                >Clear</sl-button
              >`
            : nothing
        }
      </div>
    `;
  }

  private _renderGroup(group: AuditGroup) {
    const key = this._getGroupKey(group);
    const expanded = this._expandedGroups.has(key);
    const event = group.primary_event;
    const hasSubs = group.sub_events.length > 0;
    const canExpand = this._canExpandGroup(group);
    const badge = this._getOutcomeBadge(group.outcome);
    const borderColor = this._getOutcomeColor(group.outcome);
    const isToolCall = event.action === 'tool_call';
    const argsSummary = isToolCall ? this._getArgsSummary(event.details) : '';
    const execTime =
      event.details?.execution_time_ms ?? event.details?.duration_ms;

    return html`
      <div
        class="timeline-group ${isToolCall ? 'tool-call' : 'standalone'}"
        style="--group-color: ${borderColor}"
      >
        <div
          class="primary-row ${canExpand ? 'has-subs' : ''}"
          @click=${() => canExpand && this._toggleGroup(key)}
        >
          <div class="row-left">
            <sl-icon
              name=${this._getActionIcon(event.action)}
              class="action-icon"
            ></sl-icon>
            <span class="primary-label">${this._getPrimaryLabel(event)}</span>
            ${
              argsSummary
                ? html`<span class="args-summary">${argsSummary}</span>`
                : nothing
            }
          </div>
          <div class="row-right">
            ${this._renderEventCostTokens(event.details)}
            ${
              execTime != null
                ? html`<span class="exec-time">${execTime}ms</span>`
                : nothing
            }
            <sl-badge variant=${badge.variant} pill>${badge.label}</sl-badge>
            <span class="user-name">${this._formatActorLabel(event)}</span>
            <sl-tooltip content=${this._formatFullTimestamp(event.timestamp)}>
              <span class="timestamp"
                >${this._formatTimestamp(event.timestamp)}</span
              >
            </sl-tooltip>
            ${
              canExpand
                ? html`<sl-icon
                    name=${expanded ? 'chevron-up' : 'chevron-down'}
                    class="expand-icon"
                  ></sl-icon>`
                : html`<span class="expand-spacer"></span>`
            }
          </div>
        </div>

        ${
          expanded
            ? html`
                ${
                  hasSubs && isToolCall
                    ? this._renderStorySummary(group)
                    : nothing
                }
                ${this._renderEventDetails(event.details)}
                ${
                  hasSubs
                    ? html`
                        <div class="sub-events">
                          ${group.sub_events.map((sub) =>
                            this._renderSubEvent(sub)
                          )}
                        </div>
                      `
                    : nothing
                }
              `
            : nothing
        }
      </div>
    `;
  }

  private _renderStorySummary(group: AuditGroup) {
    if (!group.sub_events.length) return nothing;

    const toolName =
      group.primary_event.details?.tool_name ||
      group.primary_event.resource_id ||
      'a tool';
    let story = `The agent requested the ${toolName} tool. `;

    let policySubevent = null;
    let approvalSubevent = null;
    let approvalResolutionSubevent = null;
    let escalationSubevent = null;
    const notificationSubevents: SubEvent[] = [];
    let executionSubevent: SubEvent | null = null;

    for (const sub of group.sub_events) {
      if (sub.action.startsWith('policy_')) policySubevent = sub;
      else if (sub.action === 'approval_created') approvalSubevent = sub;
      else if (sub.action === 'approval_escalated') escalationSubevent = sub;
      else if (
        sub.action === 'approval_approved' ||
        sub.action === 'approval_denied' ||
        sub.action === 'approval_expired'
      ) {
        approvalResolutionSubevent = sub;
      } else if (sub.action === 'approval_notification_sent') {
        notificationSubevents.push(sub);
      } else if (sub.action === 'approval_tool_executed') {
        executionSubevent = sub;
      }
    }

    if (policySubevent) {
      const rd = policySubevent.details?.rule_description;
      const isNone =
        rd &&
        (rd.includes('Rule matched: None') ||
          rd.includes('No specific rule matched') ||
          rd.includes('No access rules defined'));
      const ruleDesc = isNone ? 'Default fallback policy' : rd || 'Policy';

      if (policySubevent.action === 'policy_allow') {
        story += `${ruleDesc} automatically allowed the request. `;
      } else if (policySubevent.action === 'policy_deny') {
        story += `${ruleDesc} denied the request. `;
      } else if (policySubevent.action === 'policy_require_approval') {
        story += `${ruleDesc} required approval. `;
      }
    }

    if (approvalSubevent) {
      story += `An approval request was created. `;

      if (notificationSubevents.length > 0) {
        const channelSummaries = notificationSubevents
          .map((n) => {
            const channelLabel = this._formatChannelLabel(n.details?.channel);
            const sent =
              typeof n.details?.sent_count === 'number'
                ? n.details.sent_count
                : null;
            if (n.status === 'no_devices') {
              return `${channelLabel.toLowerCase()} (no devices)`;
            }
            if (n.status === 'failed') {
              return `${channelLabel.toLowerCase()} (failed)`;
            }
            return sent !== null
              ? `${channelLabel.toLowerCase()} (${sent})`
              : channelLabel.toLowerCase();
          })
          .join(', ');
        const totalRecipients = new Set<string>();
        for (const n of notificationSubevents) {
          const ids = n.details?.recipient_user_ids;
          if (Array.isArray(ids)) {
            for (const uid of ids) totalRecipients.add(uid);
          }
        }
        if (totalRecipients.size > 0) {
          const names = this._formatRecipientNames(Array.from(totalRecipients));
          story += `Approvers ${names} were notified via ${channelSummaries}. `;
        } else {
          story += `Approvers were notified via ${channelSummaries}. `;
        }
      }

      if (escalationSubevent) {
        story += `The request was later escalated. `;
      }
      if (approvalResolutionSubevent) {
        const u = approvalResolutionSubevent.details?.approver_id
          ? this._getUserDisplay(approvalResolutionSubevent.details.approver_id)
          : 'A user';
        if (approvalResolutionSubevent.action === 'approval_approved') {
          story += `${u} approved it. `;
        } else if (approvalResolutionSubevent.action === 'approval_denied') {
          story += `${u} declined it`;
          if (approvalResolutionSubevent.details?.reason) {
            story += ` — "${approvalResolutionSubevent.details.reason}"`;
          }
          story += '. ';
        } else if (approvalResolutionSubevent.action === 'approval_expired') {
          story += `The approval request timed out. `;
        }
      } else {
        story += `It is currently pending approval. `;
      }
    }

    if (executionSubevent) {
      // Async-poll path emits a dedicated execution sub-event with full
      // outcome information — prefer this over the generic group outcome.
      if (executionSubevent.status === 'executed') {
        story += `The tool then ran successfully`;
        const dur = executionSubevent.details?.duration_ms;
        if (typeof dur === 'number') story += ` in ${dur}ms`;
        story += '.';
      } else if (executionSubevent.status === 'failed') {
        const err = executionSubevent.details?.error;
        story += `The tool then failed${err ? ` — ${err}` : ''}.`;
      }
    } else if (
      group.outcome === 'success' ||
      group.outcome === 'executed' ||
      group.outcome === 'allow'
    ) {
      if (
        !approvalSubevent ||
        approvalResolutionSubevent?.action === 'approval_approved' ||
        !policySubevent ||
        policySubevent.action === 'policy_allow'
      ) {
        story += `The tool was successfully executed.`;
      }
    }

    return html`
      <div
        class="story-summary"
        style="padding: 12px 16px; background: var(--sl-color-neutral-50); border-radius: var(--sl-border-radius-medium); font-size: var(--sl-font-size-small); color: var(--sl-color-neutral-700); margin-bottom: 12px; border: 1px solid var(--sl-color-neutral-200);"
      >
        <sl-icon
          name="book"
          style="margin-right: 6px; color: var(--sl-color-neutral-500);"
        ></sl-icon>
        <strong>Summary:</strong> ${story}
      </div>
    `;
  }

  private _renderSubEvent(sub: SubEvent) {
    const badge = this._getOutcomeBadge(sub.status);
    return html`
      <div class="sub-event-row">
        <div class="connector"></div>
        <sl-icon
          name=${this._getActionIcon(sub.action)}
          class="sub-icon"
        ></sl-icon>
        <div class="sub-content">
          <div class="sub-main-row">
            <span class="sub-label">${this._getSubEventLabel(sub)}</span>
            ${
              sub.details?.condition_matched
                ? html`<code class="condition-code"
                    >${sub.details.condition_matched}</code
                  >`
                : nothing
            }
            <span class="sub-spacer"></span>
            ${this._renderEventCostTokens(sub.details)}
            <sl-badge variant=${badge.variant} pill size="small"
              >${badge.label}</sl-badge
            >
            <span class="sub-actor">${this._formatActorLabel(sub)}</span>
            <sl-tooltip content=${this._formatFullTimestamp(sub.timestamp)}>
              <span class="sub-timestamp"
                >${this._formatTimestamp(sub.timestamp)}</span
              >
            </sl-tooltip>
          </div>
          ${this._renderEventDetails(sub.details)}
        </div>
      </div>
    `;
  }

  private _renderPagination() {
    const start = this._page * this._pageSize + 1;
    const end = Math.min(start + this._pageSize - 1, this._total);

    return html`
      <div class="pagination">
        <span class="page-info">Showing ${start}–${end} of ${this._total}</span>
        <div class="page-controls">
          <sl-button
            size="small"
            variant="text"
            ?disabled=${this._page === 0}
            @click=${this._prevPage}
          >
            <sl-icon name="chevron-left"></sl-icon>
          </sl-button>
          <span class="page-num"
            >Page ${this._page + 1} of ${this._totalPages}</span
          >
          <sl-button
            size="small"
            variant="text"
            ?disabled=${this._page >= this._totalPages - 1}
            @click=${this._nextPage}
          >
            <sl-icon name="chevron-right"></sl-icon>
          </sl-button>
        </div>
      </div>
    `;
  }

  // ── Styles ─────────────────────────────────────────────────────────

  static styles = [
    reducedMotionStyles,
    unsafeCSS(consoleStyles),
    css`
      :host {
        display: block;
        padding: 1.5rem;
        max-width: 1200px;
        margin: 0 auto;
      }

      /* ── Header ─────────────────────────────── */
      .page-header {
        display: flex;
        align-items: center;
        gap: 0.75rem;
        margin-bottom: 1rem;
      }
      .page-header h2 {
        margin: 0;
        font-size: 1.25rem;
        font-weight: 600;
        color: var(--sl-color-neutral-900);
      }
      /* ── Live indicator ───────────────────── */
      .live-indicator {
        display: inline-flex;
        align-items: center;
        gap: 4px;
        font-size: 0.65rem;
        font-weight: 600;
        letter-spacing: 0.05em;
        color: var(--sl-color-neutral-500);
        padding: 2px 6px;
        border-radius: 999px;
        background: var(--sl-color-neutral-100);
        transition:
          background 0.2s ease,
          color 0.2s ease;
      }
      .live-indicator .live-dot {
        width: 6px;
        height: 6px;
        border-radius: 50%;
        background: var(--sl-color-success-500);
        box-shadow: 0 0 0 0 rgba(45, 196, 113, 0);
        transition: box-shadow 0.2s ease;
      }
      .live-indicator.pulsing {
        background: var(--sl-color-success-100);
        color: var(--sl-color-success-700);
      }
      .live-indicator.pulsing .live-dot {
        animation: live-pulse 1.4s ease-out;
      }
      @keyframes live-pulse {
        0% {
          box-shadow: 0 0 0 0 rgba(45, 196, 113, 0.6);
        }
        100% {
          box-shadow: 0 0 0 10px rgba(45, 196, 113, 0);
        }
      }

      /* ── Recipient chips ──────────────────── */
      .recipient-chips {
        display: flex;
        flex-wrap: wrap;
        gap: 4px;
        margin-top: 4px;
      }

      .total-badge {
        font-size: 0.75rem;
        color: var(--sl-color-neutral-500);
        background: var(--sl-color-neutral-100);
        padding: 0.15rem 0.5rem;
        border-radius: 999px;
      }

      /* ── Filter bar ────────────────────────── */
      .filter-bar {
        display: flex;
        align-items: center;
        gap: 0.5rem;
        margin-bottom: 1rem;
        flex-wrap: wrap;
      }
      .filter-bar sl-input {
        flex: 1 1 180px;
        min-width: 140px;
        max-width: 240px;
      }
      .filter-bar sl-select {
        flex: 0 1 170px;
        min-width: 140px;
      }
      .filter-bar sl-input[type='date'] {
        flex: 0 1 150px;
        min-width: 130px;
      }

      /* ── Loading / Empty ────────────────────── */
      .loading {
        display: flex;
        justify-content: center;
        padding: 3rem 0;
      }
      .empty-state {
        text-align: center;
        color: var(--sl-color-neutral-500);
        padding: 3rem 0;
        font-size: 0.9rem;
      }

      /* ── Timeline ──────────────────────────── */
      .timeline {
        display: flex;
        flex-direction: column;
        gap: 2px;
      }

      /* ── Group ─────────────────────────────── */
      .timeline-group {
        border-left: 3px solid var(--group-color, var(--sl-color-neutral-300));
        border-radius: 4px;
        background: var(--sl-color-neutral-0);
        transition: border-color 0.2s;
      }
      .timeline-group.tool-call {
        background: var(--sl-color-neutral-0);
      }
      .timeline-group.standalone {
        opacity: 0.8;
      }
      .timeline-group:hover {
        background: var(--sl-color-neutral-50);
      }

      /* ── Primary row ───────────────────────── */
      .primary-row {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 0.5rem 0.75rem;
        gap: 0.5rem;
        min-height: 40px;
      }
      .primary-row.has-subs {
        cursor: pointer;
      }
      .primary-row.has-subs:hover {
        background: var(--sl-color-neutral-50);
      }

      .row-left {
        display: flex;
        align-items: center;
        gap: 0.5rem;
        flex: 1;
        min-width: 0;
        overflow: hidden;
      }
      .row-right {
        display: flex;
        align-items: center;
        gap: 0.5rem;
        flex-shrink: 0;
      }

      .action-icon {
        font-size: 1rem;
        color: var(--sl-color-neutral-500);
        flex-shrink: 0;
      }
      .primary-label {
        font-weight: 600;
        font-size: 0.85rem;
        color: var(--sl-color-neutral-900);
        white-space: nowrap;
      }
      .args-summary {
        font-size: 0.75rem;
        color: var(--sl-color-neutral-500);
        font-family: var(--sl-font-mono);
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
        max-width: 300px;
      }

      .exec-time {
        font-size: 0.7rem;
        color: var(--sl-color-neutral-400);
        font-family: var(--sl-font-mono);
      }
      .event-cost {
        font-size: 0.7rem;
        color: var(--sl-color-neutral-500);
        font-family: var(--sl-font-mono);
        white-space: nowrap;
      }
      .user-name {
        font-size: 0.75rem;
        color: var(--sl-color-neutral-600);
        max-width: 100px;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .timestamp {
        font-size: 0.7rem;
        color: var(--sl-color-neutral-400);
        white-space: nowrap;
      }
      .expand-icon {
        font-size: 0.9rem;
        color: var(--sl-color-neutral-400);
      }
      .expand-spacer {
        width: 0.9rem;
      }

      .event-details {
        display: grid;
        gap: 0.5rem;
        padding: 0 0.75rem 0.75rem 2.25rem;
        border-top: 1px solid var(--sl-color-neutral-100);
        background: var(--sl-color-neutral-50);
      }
      .detail-item,
      .detail-block {
        display: flex;
        flex-direction: column;
        gap: 0.15rem;
      }
      .detail-label {
        font-size: 0.68rem;
        color: var(--sl-color-neutral-500);
        text-transform: uppercase;
        letter-spacing: 0.04em;
      }
      .detail-value {
        font-size: 0.78rem;
        color: var(--sl-color-neutral-700);
        word-break: break-word;
      }
      .detail-json {
        margin: 0;
        padding: 0.5rem;
        background: var(--sl-color-neutral-0);
        border: 1px solid var(--sl-color-neutral-200);
        border-radius: 6px;
        font-size: 0.72rem;
        overflow-x: auto;
      }

      /* ── Sub-events ────────────────────────── */
      .sub-events {
        padding: 0 0 0.4rem 0;
      }

      .sub-event-row {
        display: flex;
        align-items: center;
        gap: 0.4rem;
        padding: 0.25rem 0.75rem 0.25rem 1.5rem;
        font-size: 0.78rem;
        color: var(--sl-color-neutral-600);
        position: relative;
        align-items: flex-start;
      }

      .connector {
        position: absolute;
        left: 1rem;
        top: 0;
        bottom: 0;
        width: 1px;
        background: var(--sl-color-neutral-200);
      }
      .sub-event-row:last-child .connector {
        bottom: 50%;
      }

      .sub-icon {
        font-size: 0.8rem;
        color: var(--sl-color-neutral-400);
        flex-shrink: 0;
        z-index: 1;
      }
      .sub-label {
        flex-shrink: 1;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .sub-content {
        display: flex;
        flex: 1;
        flex-direction: column;
        gap: 0.35rem;
        min-width: 0;
      }
      .sub-main-row {
        display: flex;
        align-items: center;
        gap: 0.4rem;
        min-width: 0;
      }
      .condition-code {
        font-size: 0.7rem;
        background: var(--sl-color-neutral-100);
        padding: 0.1rem 0.35rem;
        border-radius: 3px;
        color: var(--sl-color-neutral-700);
        white-space: nowrap;
        flex-shrink: 0;
      }
      .sub-spacer {
        flex: 1;
      }
      .sub-actor {
        font-size: 0.68rem;
        color: var(--sl-color-neutral-500);
        max-width: 170px;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .sub-timestamp {
        font-size: 0.65rem;
        color: var(--sl-color-neutral-400);
        white-space: nowrap;
      }

      /* ── Pagination ────────────────────────── */
      .pagination {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 0.75rem 0;
        margin-top: 0.5rem;
        border-top: 1px solid var(--sl-color-neutral-200);
      }
      .page-info {
        font-size: 0.75rem;
        color: var(--sl-color-neutral-500);
      }
      .page-controls {
        display: flex;
        align-items: center;
        gap: 0.25rem;
      }
      .page-num {
        font-size: 0.75rem;
        color: var(--sl-color-neutral-600);
        padding: 0 0.5rem;
      }

      /* ── Responsive ────────────────────────── */
      @media (max-width: 768px) {
        :host {
          padding: 1rem;
        }
        .primary-row {
          flex-wrap: wrap;
        }
        .args-summary {
          display: none;
        }
        .row-right {
          width: 100%;
          justify-content: flex-end;
          margin-top: 0.25rem;
        }
        .event-details {
          padding-left: 1rem;
        }
        .filter-bar {
          flex-direction: column;
        }
        .filter-bar sl-input,
        .filter-bar sl-select {
          max-width: 100%;
          flex: 1 1 100%;
        }
        .sub-main-row {
          flex-wrap: wrap;
        }
        .sub-actor {
          max-width: 100%;
        }
      }
    `,
  ];
}
