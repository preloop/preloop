import { html, css, nothing, unsafeCSS } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import { router } from '../../router';
import {
  getAccountOrganization,
  getFlowExecutionsPage,
  getFlows,
  retryFlowExecution,
  sendCommandToExecution,
} from '../../api';
import { AuthedElement } from '../../api';
import { unifiedWebSocketManager } from '../../services/unified-websocket-manager';
import { confirmDialog } from '../../components/confirm-dialog';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/option/option.js';
import '@shoelace-style/shoelace/dist/components/select/select.js';
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
  renderExecutionRunnerKind,
  shouldShowRunnerKind,
} from '../../utils/execution-presentation';
import type {
  ExecutionModelUsage,
  ExecutionRunner,
} from '../../utils/execution-presentation';
import { renderFailureCategoryChip } from '../../utils/failure-category';
import consoleStyles from '../../styles/console-styles.css?inline';
import { reducedMotionStyles } from '../../styles/reduced-motion';
import '../../components/view-header.ts';
import '../../components/resource-actions.ts';
import '../../components/list-toolbar.ts';
import '../../components/time-range-select.ts';
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
  /**
   * Which layer broke a failed run: `runner_conflict`, `model_transient`,
   * `no_confirmation`, ... Derived by the server at failure time (#361) and
   * absent both on runs that did not fail and on servers older than it.
   */
  failure_category?: string | null;
  /** Alias that served most of the run's gateway requests (wave 7). */
  model_alias?: string | null;
  provider_name?: string | null;
  models_used?: ExecutionModelUsage[] | null;
  runner?: ExecutionRunner | null;
}

/** How often the elapsed time of running rows is recomputed. */
const DURATION_TICK_MS = 1000;

/** How long the bar waits after the last keystroke before it asks again. */
const SEARCH_DEBOUNCE_MS = 300;

/** The ranges the pill offers, and how far back each one reaches. */
const RANGE_OPTIONS: Array<{ value: string; label: string; days: number }> = [
  { value: 'day', label: '24h', days: 1 },
  { value: 'week', label: '7d', days: 7 },
  { value: 'month', label: '30d', days: 30 },
  { value: 'year', label: '1y', days: 365 },
  { value: 'all', label: 'All', days: 0 },
];

/** Columns the page can sort the rows it holds by. */
type ExecutionSortKey =
  | 'flow'
  | 'subject'
  | 'status'
  | 'started'
  | 'duration'
  | 'model'
  | 'tools'
  | 'cost';

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
      /* Fixed layout, because content-driven widths made this table 1250px
         wide inside a 1125px wrapper at 1440: the cost column and the kebab
         were off-screen behind a scrollbar that only appeared on hover. The
         widths below are the ones the columns actually need; Subject takes
         whatever is left and ellipsises. */
      table {
        width: 100%;
        border-collapse: collapse;
        min-width: 960px;
        table-layout: fixed;
        font-size: var(--console-text-body);
      }
      th.col-flow {
        width: 176px;
      }
      th.col-status {
        width: 96px;
      }
      th.col-started {
        width: 76px;
      }
      th.col-duration {
        width: 72px;
      }
      th.col-model {
        width: 150px;
      }
      th.col-tools {
        width: 64px;
      }
      th.col-cost {
        width: 60px;
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
      /* The uppercase sortable eyebrow the Flows list uses, so the two
         tables carry one header recipe. The button is the whole cell, so the
         hit area is the label, not the six pixels of the caret. */
      th.sortable {
        padding: 0;
      }
      .sort-button {
        display: flex;
        align-items: center;
        gap: 4px;
        width: 100%;
        background: none;
        border: none;
        cursor: pointer;
        font: inherit;
        font-weight: var(--sl-font-weight-semibold);
        font-size: var(--sl-font-size-x-small);
        letter-spacing: 0.04em;
        text-transform: uppercase;
        color: var(--sl-color-neutral-600);
        padding: 8px;
      }
      th.numeric .sort-button {
        justify-content: flex-end;
      }
      .sort-button:hover,
      .sort-button:focus-visible {
        color: var(--sl-color-neutral-900);
      }
      th.active .sort-button {
        color: var(--sl-color-neutral-900);
      }
      .sort-caret {
        font-size: 0.75em;
        opacity: 0.55;
      }
      th.active .sort-caret {
        opacity: 1;
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
      /* A table cell, not a flex row: as flex the name and the pool chip
         shared one line, which pushed the whole row taller and the table
         wider. The chip now sits under the name, as it does on the flows
         list. */
      .flow-cell {
        display: table-cell;
        overflow: hidden;
      }
      /* The subject is the primary way to tell executions apart, so it gets
         the width the fixed columns leave over. */
      .subject-cell {
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .subject-cell .execution-subject.is-fallback {
        font-family: var(--sl-font-mono);
      }
      .model-cell {
        overflow: hidden;
      }
      .status-cell {
        display: flex;
        align-items: center;
        flex-wrap: wrap;
        gap: 4px 8px;
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
        width: 72px;
      }
      .row-actions {
        display: flex;
        justify-content: flex-end;
      }
      /* Under the bar, not inside it: whether updates are live is a state of
         the page, not a filter. */
      .header-controls {
        display: flex;
        justify-content: flex-start;
        align-items: center;
        gap: 12px;
        flex-wrap: wrap;
        margin: 8px 0 16px;
      }
      list-toolbar {
        margin-bottom: 4px;
      }
      list-toolbar sl-select {
        min-width: 180px;
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

  /** What the search box holds; the query it produced is debounced. */
  @state()
  private searchQuery = '';

  /** The window the range pill names. */
  @state()
  private range = 'month';

  /** `X-Total-Count`: how many executions the filters matched. */
  @state()
  private totalCount: number | null = null;

  /** Options for the All flows select, name and id only. */
  @state()
  private flowOptions: Array<{ id: string; name: string }> = [];

  /**
   * The account's default runner pool, so a row only says where it ran when
   * that is not where the default would have sent it.
   */
  @state()
  private accountDefaultPool: string | null = null;

  /**
   * Which column the header sorts on, or null for the order the server sent
   * (newest first). A page is a window on a larger set, so the list does not
   * silently reorder it until someone asks it to.
   */
  @state()
  private sortKey: ExecutionSortKey | null = null;

  @state()
  private sortDirection: 'asc' | 'desc' = 'desc';

  private searchDebounceId?: number;

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
    void this.loadFilterSources();
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
    // `?flow=` is what the flow page links with, `?flow_id=` what Attention
    // and the Overview inventory link with. Both mean the same filter.
    this.flowIdFilter = params.get('flow_id') || params.get('flow');
    const search = params.get('q');
    if (search) this.searchQuery = search;
    const range = params.get('range');
    if (range && RANGE_OPTIONS.some((option) => option.value === range)) {
      this.range = range;
    } else if (this.flowIdFilter || status) {
      // A deep link points at a run somebody wants to see. A 30d default
      // could hide exactly that run, so a filtered entry opens on All.
      this.range = 'all';
    }
  }

  /**
   * The two lists the bar needs: the flows to name in the select, and the
   * account default pool the runner chip is measured against. Neither is
   * worth failing the page over, so both fall back to "not known".
   */
  private async loadFilterSources(): Promise<void> {
    try {
      const flows = await getFlows();
      this.flowOptions = (Array.isArray(flows) ? flows : [])
        .filter((flow) => flow && flow.id)
        .map((flow) => ({
          id: String(flow.id),
          name: String(flow.name || 'Unnamed flow'),
        }))
        .sort((a, b) => a.name.localeCompare(b.name));
      if (this.flowIdFilter && !this.flowNameFilter) {
        this.flowNameFilter =
          this.flowOptions.find((flow) => flow.id === this.flowIdFilter)
            ?.name || this.flowNameFilter;
      }
    } catch {
      this.flowOptions = [];
    }
    try {
      const account = await getAccountOrganization();
      this.accountDefaultPool = account?.default_runner_pool ?? null;
    } catch {
      this.accountDefaultPool = null;
    }
  }

  /** The instant the range pill means, or undefined for All. */
  private get startedAfter(): string | undefined {
    const option = RANGE_OPTIONS.find((entry) => entry.value === this.range);
    if (!option || option.days <= 0) return undefined;
    return new Date(
      Date.now() - option.days * 24 * 60 * 60 * 1000
    ).toISOString();
  }

  /**
   * A failure here used to be an unhandled rejection: it skipped
   * `connectWebSocket()` on entry, left the page without live updates for the
   * session, and rendered "No executions found" over a list that may be full.
   * The error is caught, shown, and retryable instead.
   */
  async loadExecutions() {
    try {
      const page = await getFlowExecutionsPage({
        limit: this.pageSize + 1,
        skip: (this.currentPage - 1) * this.pageSize,
        status: this.statusFilter === 'all' ? undefined : this.statusFilter,
        flowId: this.flowIdFilter || undefined,
        search: this.searchQuery.trim() || undefined,
        startedAfter: this.startedAfter,
      });
      const rows = page.rows;
      this.totalCount = page.total;
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
    this.setFlowFilter(null);
  }

  /** The All flows select, and the deep links that preselect one flow. */
  private setFlowFilter(flowId: string | null): void {
    this.flowIdFilter = flowId;
    this.flowNameFilter = flowId
      ? this.flowOptions.find((flow) => flow.id === flowId)?.name || null
      : null;
    this.currentPage = 1;
    const url = new URL(window.location.href);
    url.searchParams.delete('flow');
    if (flowId) {
      url.searchParams.set('flow_id', flowId);
    } else {
      url.searchParams.delete('flow_id');
    }
    window.history.replaceState({}, '', url.toString());
    void this.loadExecutions();
  }

  /**
   * Typing asks the server, because a page of 25 rows cannot answer "where
   * is that run" for an account with thousands. One request per pause.
   */
  private handleSearchChange(value: string): void {
    this.searchQuery = value;
    if (this.searchDebounceId !== undefined) {
      clearTimeout(this.searchDebounceId);
    }
    this.searchDebounceId = window.setTimeout(() => {
      this.searchDebounceId = undefined;
      this.currentPage = 1;
      void this.loadExecutions();
    }, SEARCH_DEBOUNCE_MS);
  }

  private setRange(range: string): void {
    this.range = range;
    this.currentPage = 1;
    void this.loadExecutions();
  }

  get filteredExecutions(): FlowExecution[] {
    return this.executions;
  }

  /** The page's rows in the order the header says they are in. */
  get paginatedExecutions(): FlowExecution[] {
    const rows = [...this.filteredExecutions];
    const key = this.sortKey;
    if (!key) return rows;
    const direction = this.sortDirection === 'asc' ? 1 : -1;
    return rows.sort((a, b) => direction * this.compareExecutions(a, b, key));
  }

  private compareExecutions(
    a: FlowExecution,
    b: FlowExecution,
    key: ExecutionSortKey
  ): number {
    const text = (value: string | null | undefined) => (value || '').trim();
    const startedAt = (row: FlowExecution) =>
      parseUTCDate(row.start_time).getTime() || 0;
    const durationOf = (row: FlowExecution) => {
      const end = row.end_time
        ? parseUTCDate(row.end_time).getTime()
        : Date.now();
      const start = startedAt(row);
      return start ? end - start : 0;
    };
    switch (key) {
      case 'flow':
        return text(a.flow_name).localeCompare(text(b.flow_name));
      case 'subject':
        return text(a.trigger_subject).localeCompare(text(b.trigger_subject));
      case 'status':
        return text(a.status).localeCompare(text(b.status));
      case 'duration':
        return durationOf(a) - durationOf(b);
      case 'model':
        return text(a.model_alias).localeCompare(text(b.model_alias));
      case 'tools':
        return (a.tool_calls_count || 0) - (b.tool_calls_count || 0);
      case 'cost':
        return (a.estimated_cost || 0) - (b.estimated_cost || 0);
      case 'started':
      default:
        return startedAt(a) - startedAt(b);
    }
  }

  /** First click sorts descending, because recent and expensive lead. */
  private toggleSort(key: ExecutionSortKey): void {
    if (this.sortKey === key) {
      this.sortDirection = this.sortDirection === 'asc' ? 'desc' : 'asc';
      return;
    }
    this.sortKey = key;
    this.sortDirection = 'desc';
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
    // A keystroke 300 ms before the user navigates away must not spend a
    // request on a detached page, or write state into it afterwards.
    if (this.searchDebounceId !== undefined) {
      clearTimeout(this.searchDebounceId);
      this.searchDebounceId = undefined;
    }
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

  /**
   * Search, the flow, the status, the range, then the count: the bar the
   * Flows list established, so the two collections read as one product.
   */
  private renderToolbar() {
    return html`
      <list-toolbar
        .search=${this.searchQuery}
        searchPlaceholder="Search subject or flow"
        .views=${['list']}
        @search-change=${(event: CustomEvent) =>
          this.handleSearchChange(event.detail.value)}
      >
        <sl-select
          class="flow-filter"
          clearable
          placeholder="All flows"
          value=${this.flowIdFilter || ''}
          @sl-change=${(event: Event) => {
            const select = event.target as HTMLElement & { value: string };
            this.setFlowFilter(select.value || null);
          }}
        >
          ${this.flowOptions.map(
            (flow) => html`<sl-option value=${flow.id}>${flow.name}</sl-option>`
          )}
        </sl-select>

        <sl-select
          class="status-filter"
          placeholder="Any status"
          value=${this.statusFilter === 'all' ? '' : this.statusFilter}
          @sl-change=${(event: Event) => {
            const select = event.target as HTMLElement & { value: string };
            this.setStatusFilter(select.value || 'all');
          }}
        >
          <sl-option value="">Any status</sl-option>
          <sl-option value="RUNNING">Running</sl-option>
          <sl-option value="PENDING">Pending</sl-option>
          <sl-option value="SUCCEEDED">Succeeded</sl-option>
          <sl-option value="FAILED">Failed</sl-option>
          <sl-option value="CANCELLED">Cancelled</sl-option>
        </sl-select>

        <time-range-select
          ariaLabel="Executions time range"
          .value=${this.range}
          .options=${RANGE_OPTIONS.map(({ value, label }) => ({
            value,
            label,
          }))}
          @range-change=${(event: CustomEvent) =>
            this.setRange(event.detail.value as string)}
        ></time-range-select>

        <sl-button size="small" @click=${this.loadExecutions}>
          <sl-icon name="arrow-clockwise"></sl-icon>
          Refresh
        </sl-button>
        <span slot="count">${this.resultsLabel}</span>
      </list-toolbar>
    `;
  }

  /**
   * "25 of 1,412 executions" when the server sent the total, and just what
   * is on screen when it did not: the count is never guessed.
   */
  private get resultsLabel(): string {
    const shown = this.executions.length;
    const noun = shown === 1 ? 'execution' : 'executions';
    if (this.totalCount === null || this.totalCount <= shown) {
      return `${shown.toLocaleString()} ${noun}`;
    }
    return `${shown.toLocaleString()} of ${this.totalCount.toLocaleString()} executions`;
  }

  private renderSortableHeader(
    key: ExecutionSortKey,
    label: string,
    columnClass: string,
    numeric = false
  ) {
    const active = this.sortKey === key;
    const ariaSort = active
      ? this.sortDirection === 'asc'
        ? 'ascending'
        : 'descending'
      : 'none';
    return html`
      <th
        class="${columnClass} sortable ${numeric ? 'numeric' : ''} ${
          active ? 'active' : ''
        }"
        aria-sort=${ariaSort}
        scope="col"
      >
        <button
          type="button"
          class="sort-button"
          data-sort-key=${key}
          title="Sorts the executions on this page"
          @click=${() => this.toggleSort(key)}
        >
          <span>${label}</span>
          <sl-icon
            class="sort-caret"
            name=${
              active
                ? this.sortDirection === 'asc'
                  ? 'caret-up-fill'
                  : 'caret-down-fill'
                : 'chevron-expand'
            }
          ></sl-icon>
        </button>
      </th>
    `;
  }

  render() {
    return html`
      <view-header headerText="Flow executions" width="wide"></view-header>
      <div class="column-layout wide">
        <div class="main-column">
          ${this.renderToolbar()}
          <div class="header-controls">${this.renderConnectionStatus()}</div>

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
                  <div class="table-wrapper">
                    <table>
                      <thead>
                        <tr>
                          ${this.renderSortableHeader('flow', 'Flow', 'col-flow')}
                          ${this.renderSortableHeader(
                            'subject',
                            'Subject',
                            'col-subject'
                          )}
                          ${this.renderSortableHeader(
                            'status',
                            'Status',
                            'col-status'
                          )}
                          ${this.renderSortableHeader(
                            'started',
                            'Started',
                            'col-started'
                          )}
                          ${this.renderSortableHeader(
                            'duration',
                            'Duration',
                            'col-duration'
                          )}
                          ${this.renderSortableHeader(
                            'model',
                            'Model',
                            'col-model'
                          )}
                          ${this.renderSortableHeader(
                            'tools',
                            'Tool calls',
                            'col-tools',
                            true
                          )}
                          ${this.renderSortableHeader(
                            'cost',
                            '$ est.',
                            'col-cost',
                            true
                          )}
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
          <!-- Where it ran, only when that is news: see
               shouldShowRunnerKind. -->
          ${
            shouldShowRunnerKind(exec.runner, this.accountDefaultPool)
              ? renderExecutionRunnerKind(exec.runner)
              : nothing
          }
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
            <!-- "Failed" says that it broke; the category says what broke,
                 which is the difference between a provider hiccup and a flow
                 that never confirms it finished. -->
            ${renderFailureCategoryChip(exec.failure_category)}
          </div>
        </td>
        <td class="started-cell" title=${formatUTCDateTime(exec.start_time)}>
          ${formatRelativeTime(exec.start_time)}
        </td>
        <td class="duration-cell">
          ${executionDurationText(exec, this.durationNow) || '—'}
        </td>
        <!-- No provider column here, so the alias prints once: the cell used
             to read "deepseek/deepseek-v4-pro deepseek". -->
        <td class="model-cell">
          ${renderExecutionModel(exec, { aliasOnly: true })}
        </td>
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
