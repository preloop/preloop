import { LitElement, html, css, nothing, unsafeCSS } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import { repeat } from 'lit/directives/repeat.js';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/option/option.js';
import '@shoelace-style/shoelace/dist/components/select/select.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import '@shoelace-style/shoelace/dist/components/divider/divider.js';
import '@shoelace-style/shoelace/dist/components/dialog/dialog.js';
import '../../components/list-toolbar.ts';
import '../../components/resource-actions.ts';
import '../../components/time-range-select.ts';
import { router } from '../../router';
import { Router } from '@vaadin/router';
import { unifiedWebSocketManager } from '../../services/unified-websocket-manager';
import {
  getFlows,
  getFlowPresets,
  getFlowExecutions,
  getTrackers,
  deleteFlow,
  triggerFlowExecution,
  updateFlow,
} from '../../api';
import { confirmDialog, showToast } from '../../components/confirm-dialog';
import type { ResourceAction } from '../../components/resource-actions.ts';
import {
  formatLocalDateTime,
  formatRelativeTime,
  parseUTCDate,
} from '../../utils/date';
import { executionDurationText } from '../../utils/execution';
// Re-exported below: the list and the flow detail page state the trigger the
// same way, so the reading lives in one module.
import { flowTriggerSummary } from '../../utils/flow-trigger';
import {
  executionSubjectCss,
  renderExecutionSubject,
  type ExecutionSubjectSource,
} from '../../utils/execution-subject';
import consoleStyles from '../../styles/console-styles.css?inline';
import type { Flow } from '../../types';
import { consoleDialogStyles } from '../../styles/console-dialog';
import {
  effectiveViewMode,
  loadViewMode,
  saveViewMode,
  subscribeNarrowViewport,
  type ListViewMode,
  type NarrowViewportSubscription,
} from '../../utils/view-mode';

export { flowTriggerSummary };

/** A flow row from the list endpoints, where id and name are always present. */
type FlowListItem = Flow & { id: string; name: string };

interface FlowExecution extends ExecutionSubjectSource {
  id: string;
  flow_id: string;
  flow_name?: string | null;
  status: string;
  start_time: string;
  end_time?: string;
}

export type FlowsViewMode = 'list' | 'cards';

const VIEW_MODE_KEY = 'preloop.flows.view_mode';

/**
 * How many recent runs the page reads to build the in-range counts.
 *
 * There is no date filter on the executions list, so the range is applied in
 * the browser over the newest N runs. Every count derived from it carries the
 * window in its `title` when the window is full, because a number that is a
 * sample of a wider period has to say so (DESIGN.md, The Inventory box).
 */
const EXECUTIONS_SAMPLE_LIMIT = 200;

/** One day, for the ranges whose labels are counted in hours and days. */
const DAY_MS = 24 * 60 * 60 * 1000;

/**
 * Thin wrapper around `loadViewMode` so flows-view.test.ts can assert the
 * default/cast directly. Also used to initialise and restore the page view.
 */
export function loadInitialFlowsViewMode(): FlowsViewMode {
  return loadViewMode(VIEW_MODE_KEY) as FlowsViewMode;
}

export type FlowRange = 'day' | 'week' | 'month' | 'year';

/**
 * What a flow is doing, in one word.
 *
 * - `paused`: the operator turned it off, or its schedule is suspended. It
 *   exists and will not fire.
 * - `draft`: enabled, but nothing can start it - no trigger source and no
 *   run behind it. It was created and never wired up.
 * - `enabled`: everything else.
 */
export type FlowStatus = 'enabled' | 'paused' | 'draft';

export type FlowStatusFilter = FlowStatus | 'failing';

export type FlowListSortKey =
  'flow' | 'trigger' | 'status' | 'last_run' | 'runs' | 'failed' | 'cost';

export type SortDirection = 'asc' | 'desc';

/** One normalised table (and card) row. */
export interface FlowListRow {
  id: string;
  name: string;
  /** The preset the flow was cloned from, or 'Custom flow'. */
  presetLabel: string;
  /** Stable filter value: the source preset id, or 'custom'. */
  presetValue: string;
  icon: string;
  description: string;
  detailUrl: string;
  triggerLabel: string;
  triggerTitle: string;
  status: FlowStatus;
  statusLabel: string;
  lastRun: FlowExecution | null;
  /**
   * When the most recent run is older than the executions sample, the server
   * still knows when it was; the cell says so rather than "No run".
   */
  lastRunAt: string | null;
  runs: number;
  failed: number;
  cost: number;
  /** True when runs, failed and cost all come from the server's window. */
  countsFromServer: boolean;
  source: FlowListItem;
}

const FLOW_STATUS_LABELS: Record<FlowStatus, string> = {
  enabled: 'Enabled',
  paused: 'Paused',
  draft: 'Draft',
};

/** Soft chips only: a paused flow is a state, not an outcome (DESIGN.md). */
const FLOW_STATUS_VARIANTS: Record<FlowStatus, 'success' | 'neutral'> = {
  enabled: 'success',
  paused: 'neutral',
  draft: 'neutral',
};

export function flowStatusOf(flow: Flow): FlowStatus {
  if (flow.is_enabled === false) return 'paused';
  if (
    flow.trigger_event_source === 'schedule' &&
    flow.schedule_state &&
    !flow.schedule_state.active
  ) {
    return 'paused';
  }
  const hasRun = Number(flow.execution_stats?.total_execs || 0) > 0;
  if (!flow.trigger_event_source && !hasRun) return 'draft';
  return 'enabled';
}

/**
 * A backend timestamp as epoch milliseconds.
 *
 * `Date.parse` reads a naive `2026-09-03T09:00:00` as local time, while the
 * range boundary this is compared against is UTC. West of Greenwich that
 * shifted every run count, the failures filter and the last-run sort by the
 * offset; `parseUTCDate` is what the rest of the console uses for the same
 * reason.
 */
function timeValue(value: string | null | undefined): number {
  if (!value) return Number.NEGATIVE_INFINITY;
  const parsed = parseUTCDate(value).getTime();
  return Number.isNaN(parsed) ? Number.NEGATIVE_INFINITY : parsed;
}

function compareFlowRowsByKey(
  a: FlowListRow,
  b: FlowListRow,
  key: FlowListSortKey
): number {
  switch (key) {
    case 'flow':
      return a.name.localeCompare(b.name, undefined, { sensitivity: 'base' });
    case 'trigger':
      return a.triggerLabel.localeCompare(b.triggerLabel, undefined, {
        sensitivity: 'base',
      });
    case 'status':
      return a.statusLabel.localeCompare(b.statusLabel, undefined, {
        sensitivity: 'base',
      });
    case 'runs':
      return a.runs - b.runs;
    case 'failed':
      return a.failed - b.failed;
    case 'cost':
      return a.cost - b.cost;
    case 'last_run':
    default:
      return (
        timeValue(a.lastRun?.start_time) - timeValue(b.lastRun?.start_time)
      );
  }
}

export function sortFlowListRows(
  rows: FlowListRow[],
  key: FlowListSortKey,
  direction: SortDirection
): FlowListRow[] {
  const sign = direction === 'asc' ? 1 : -1;
  return [...rows].sort((a, b) => {
    const result = compareFlowRowsByKey(a, b, key);
    // Same value: fall back to the name so the order never shuffles between
    // renders of the same data.
    if (result === 0) return a.name.localeCompare(b.name);
    return result * sign;
  });
}

export interface FlowFilterState {
  query: string;
  /** Source preset ids and/or 'custom'. Empty means every kind. */
  presets: string[];
  /** Empty means every status. */
  statuses: FlowStatusFilter[];
}

export const EMPTY_FLOW_FILTERS: FlowFilterState = {
  query: '',
  presets: [],
  statuses: [],
};

function matchesQuery(row: FlowListRow, query: string): boolean {
  const needle = query.trim().toLowerCase();
  if (!needle) return true;
  return [
    row.name,
    row.presetLabel,
    row.description,
    row.triggerLabel,
    row.statusLabel,
  ]
    .filter(Boolean)
    .some((field) => field.toLowerCase().includes(needle));
}

export function filterFlowRows(
  rows: FlowListRow[],
  filters: FlowFilterState
): FlowListRow[] {
  return rows.filter((row) => {
    if (!matchesQuery(row, filters.query)) return false;
    if (filters.presets.length && !filters.presets.includes(row.presetValue)) {
      return false;
    }
    if (filters.statuses.length) {
      const matched = filters.statuses.some((status) =>
        status === 'failing' ? row.failed > 0 : row.status === status
      );
      if (!matched) return false;
    }
    return true;
  });
}

@customElement('flows-view')
export class FlowsView extends LitElement {
  static styles = [
    consoleDialogStyles,
    unsafeCSS(consoleStyles),
    unsafeCSS(executionSubjectCss),
    css`
      :host {
        display: block;
      }

      /* --- List view --- */
      /* Fixed layout so the columns come from the colgroup and not from the
         longest flow name: a flow named after a repository path used to push
         the kebab past the right edge of the card. */
      .flows-table {
        table-layout: fixed;
        width: 100%;
        min-width: 1120px;
      }
      .table-scroll {
        overflow-x: auto;
        width: 100%;
      }
      .flows-table th,
      .flows-table td {
        padding: var(--sl-spacing-small);
        vertical-align: middle;
        min-width: 0;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .flows-table th {
        padding: 0;
      }
      /* Same measurement as the agents table: one medium sl-button is 48px
         wide, so the column is 56px of content plus 8px of padding a side.
         Anything narrower clips the kebab off its left edge. */
      .flows-table th.actions-cell,
      .flows-table td.actions-cell {
        width: 72px;
        text-align: right;
        padding-left: var(--sl-spacing-x-small);
        padding-right: var(--sl-spacing-x-small);
        overflow: visible;
      }
      .actions-cell resource-actions::part(container) {
        overflow: visible;
      }
      /* The two columns that carry words get the room; the counters are
         sized to their widest plausible number and nothing more. A flow
         called "Release Sentinel db-migration" must be readable without a
         tooltip, which is what the first screenshot round got wrong. */
      .col-flow {
        width: 25%;
      }
      .col-trigger {
        width: 14%;
      }
      .col-status {
        width: 96px;
      }
      .col-last-run {
        width: 23%;
      }
      .col-runs,
      .col-failed {
        width: 84px;
      }
      .col-cost {
        width: 96px;
      }
      .col-actions {
        width: 72px;
      }
      /* First and last columns keep the card's own gutter. */
      .flows-table th:first-child .sort-button,
      .flows-table td:first-child {
        padding-left: var(--sl-spacing-medium);
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
        padding: var(--sl-spacing-small);
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
      .flow-row {
        cursor: pointer;
      }
      .flow-row:hover td {
        background: var(--console-hover-tint);
      }
      .flow-identity {
        display: flex;
        align-items: center;
        gap: var(--sl-spacing-small);
        min-width: 0;
      }
      .flow-identity sl-icon {
        color: var(--sl-color-neutral-700);
        flex-shrink: 0;
        font-size: 18px;
      }
      .flow-identity-text {
        min-width: 0;
        overflow: hidden;
      }
      /* Names never wrap: one two-line name makes the whole table ragged. */
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
      .row-subtitle {
        color: var(--console-meta-color);
        font-size: var(--console-text-meta);
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .muted-cell {
        color: var(--console-meta-color);
      }
      /* Counters read as a column of digits, so they need less air than a
         sentence does; the width they give back goes to the names. */
      .flows-table td.numeric,
      .flows-table th.numeric,
      .flows-table th.numeric .sort-button {
        text-align: right;
        font-variant-numeric: tabular-nums;
        white-space: nowrap;
        padding-left: var(--sl-spacing-x-small);
        padding-right: var(--sl-spacing-x-small);
      }
      .last-run {
        display: flex;
        flex-direction: column;
        gap: 2px;
        min-width: 0;
      }
      .last-run-line {
        display: flex;
        align-items: center;
        gap: var(--sl-spacing-x-small);
        min-width: 0;
      }
      .last-run-line .status-chip {
        flex-shrink: 0;
      }
      .last-run .meta {
        color: var(--console-meta-color);
        font-size: var(--console-text-meta);
        white-space: nowrap;
      }
      .row-actions {
        display: flex;
        justify-content: flex-end;
      }
      .visually-hidden {
        position: absolute;
        width: 1px;
        height: 1px;
        overflow: hidden;
        clip: rect(0 0 0 0);
        white-space: nowrap;
      }

      /* --- Cards view --- */
      /* auto-fill, not auto-fit: two flows stay two 320px cards rather than
         stretching into two half-screen banners. */
      .flows-grid,
      .presets-grid {
        display: grid;
        grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
        gap: var(--sl-spacing-large);
        margin-bottom: var(--sl-spacing-large);
      }
      .flows-grid > sl-card,
      .presets-grid > sl-card {
        width: 100%;
        min-width: 0;
        box-sizing: border-box;
      }
      .flow-card {
        cursor: pointer;
        height: 100%;
      }
      .flow-card::part(base) {
        height: 100%;
        display: flex;
        flex-direction: column;
      }
      .flow-card::part(body) {
        flex: 1;
        display: flex;
        flex-direction: column;
      }
      .flow-header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        gap: var(--sl-spacing-small);
        min-width: 0;
      }
      .flow-title {
        display: flex;
        align-items: center;
        gap: 8px;
        font-size: var(--console-text-card-title);
        font-weight: 600;
        min-width: 0;
      }
      .flow-title a {
        color: inherit;
        text-decoration: none;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .flow-title a:hover,
      .flow-title a:focus-visible {
        text-decoration: underline;
      }
      .card-actions {
        flex-shrink: 0;
      }
      /* A fixed height keeps the meta rows and footers of a row of cards on
         the same line. 5.75rem was cut for a three-line clamp; the clamp is
         two lines plus the toggle, so the rest was a dead band under every
         description. */
      .flow-description {
        color: var(--console-meta-color);
        margin-bottom: 12px;
        font-size: var(--console-text-meta);
        height: 4rem;
        overflow: hidden;
      }
      .flow-description-text {
        display: -webkit-box;
        -webkit-line-clamp: 2;
        -webkit-box-orient: vertical;
        overflow: hidden;
        line-height: 1.35;
      }
      .flow-description-placeholder {
        color: var(--console-meta-color);
        font-style: italic;
      }
      .flow-description-action {
        margin-top: var(--sl-spacing-2x-small);
      }
      .card-meta {
        display: flex;
        flex-wrap: wrap;
        gap: var(--sl-spacing-x-small) var(--sl-spacing-medium);
        margin-top: 12px;
        padding-top: 12px;
        border-top: 1px solid var(--console-hairline);
        font-size: var(--console-text-meta);
        color: var(--console-meta-color);
      }
      .card-meta .stat-item {
        display: flex;
        align-items: center;
        gap: 4px;
        min-width: 0;
      }
      .flow-footer {
        display: flex;
        gap: 8px;
        justify-content: space-between;
        align-items: center;
      }
      .flow-footer-actions {
        display: flex;
        gap: 8px;
      }

      /* --- Page furniture --- */
      .active-executions {
        margin-bottom: var(--sl-spacing-large);
      }
      .executions-list {
        display: flex;
        flex-direction: column;
        gap: 8px;
        margin-top: 12px;
      }
      .execution-item {
        display: flex;
        justify-content: space-between;
        align-items: center;
        padding: 12px;
        border-bottom: 1px solid var(--console-hairline);
        cursor: pointer;
      }
      .execution-item:hover {
        background: var(--console-hover-tint);
      }
      .execution-info {
        display: flex;
        align-items: center;
        gap: 12px;
        flex: 1;
        min-width: 0;
      }
      .section-header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        margin: 24px 0 16px 0;
      }
      .section-header h2 {
        font-size: var(--console-text-card-title);
        font-weight: 600;
        margin: 0;
      }
      .presets-collapsed {
        text-align: center;
        padding: 24px 16px;
        color: var(--console-meta-color);
        font-size: var(--console-text-body);
      }
      .empty-state-wrapper {
        display: flex;
        justify-content: center;
        width: 100%;
        margin-top: var(--sl-spacing-large);
        margin-bottom: var(--sl-spacing-large);
      }
      .empty-card {
        width: 100%;
        max-width: 580px;
      }
      .empty-card::part(base) {
        border: 1px solid
          color-mix(in srgb, var(--sl-color-primary-600) 35%, transparent);
        box-shadow: var(--sl-shadow-large);
        border-radius: var(--sl-border-radius-large);
        overflow: hidden;
      }
      .empty-card-body {
        display: flex;
        flex-direction: column;
        align-items: center;
        text-align: center;
        padding: var(--sl-spacing-large);
      }
      .empty-icon-circle {
        width: 72px;
        height: 72px;
        border-radius: 50%;
        background: color-mix(
          in srgb,
          var(--sl-color-primary-600) 15%,
          transparent
        );
        color: var(--sl-color-primary-600);
        display: flex;
        align-items: center;
        justify-content: center;
        margin-bottom: var(--sl-spacing-medium);
      }
      .empty-icon-circle sl-icon {
        font-size: 2.5rem;
      }
      .empty-card-title {
        margin: 0 0 var(--sl-spacing-2x-small);
        font-size: 1.25rem;
        font-weight: 700;
        color: var(--sl-color-neutral-900);
      }
      .empty-card-desc {
        margin: 0 0 var(--sl-spacing-large);
        max-width: 440px;
        font-size: 0.95rem;
        line-height: 1.55;
        color: var(--sl-color-neutral-600);
      }
      .empty-cta-btn {
        width: 100%;
        max-width: 280px;
      }
    `,
  ];

  @state() private flows: FlowListItem[] = [];
  @state() private presets: FlowListItem[] = [];
  @state() private presetsLoading = false;
  @state() private executions: FlowExecution[] = [];
  @state() private activeExecutions: FlowExecution[] = [];
  @state() private trackerNames: Record<string, string> = {};
  @state() private isLoading = true;
  /** Set when the flows fetch failed, so the page says so instead of "none". */
  @state() private loadError: string | null = null;
  @state() private triggeringFlowId: string | null = null;
  @state() private showPresets = true;
  @state() private expandedDescription: {
    title: string;
    description: string;
  } | null = null;

  @state() private currentView: FlowsViewMode = loadInitialFlowsViewMode();
  @state() private narrowViewport = false;
  @state() private filters: FlowFilterState = { ...EMPTY_FLOW_FILTERS };
  @state() private range: FlowRange = 'month';
  @state() private sortKey: FlowListSortKey = 'last_run';
  @state() private sortDirection: SortDirection = 'desc';

  private presetsLoaded = false;
  private unsubscribe?: () => void;
  private hasInitializedPresetVisibility = false;
  private narrowViewportSubscription: NarrowViewportSubscription | null = null;

  async connectedCallback() {
    super.connectedCallback();
    this.currentView = loadInitialFlowsViewMode();
    this.narrowViewportSubscription = subscribeNarrowViewport((narrow) => {
      this.narrowViewport = narrow;
    });
    this.narrowViewport = this.narrowViewportSubscription.matches;
    await this.loadData();
    this.connectWebSocket();
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    this.unsubscribe?.();
    this.narrowViewportSubscription?.disconnect();
    this.narrowViewportSubscription = null;
  }

  /**
   * The view actually painted. On a phone a seven-column table would either
   * scroll sideways or crush every cell, so `list` renders as cards.
   */
  private get effectiveView(): FlowsViewMode {
    return effectiveViewMode(
      this.currentView,
      this.narrowViewport
    ) as FlowsViewMode;
  }

  private handleSearchChange(event: CustomEvent<{ value: string }>) {
    this.filters = { ...this.filters, query: event.detail.value };
  }

  private handleViewChange(event: CustomEvent<{ value: ListViewMode }>) {
    if (event.detail.value === 'canvas') {
      return;
    }
    this.currentView = event.detail.value;
    saveViewMode(VIEW_MODE_KEY, event.detail.value);
  }

  /**
   * The start of the page range, as the gateway summary wants it.
   *
   * Each window is what its label says. "30d" is 30×24h, not
   * `setMonth(getMonth() - 1)`: a calendar month is 28 to 31 days, so the
   * label and the window disagreed by up to three days depending on which
   * month it was read in. "1y" stays a calendar year, which is what that
   * label means.
   */
  private rangeStartDate(): string {
    const now = Date.now();
    if (this.range === 'day') return new Date(now - DAY_MS).toISOString();
    if (this.range === 'week') return new Date(now - 7 * DAY_MS).toISOString();
    if (this.range === 'year') {
      const date = new Date();
      date.setFullYear(date.getFullYear() - 1);
      return date.toISOString();
    }
    return new Date(now - 30 * DAY_MS).toISOString();
  }

  private get rangeLabel(): string {
    if (this.range === 'day') return '24h';
    if (this.range === 'week') return '7d';
    if (this.range === 'year') return '1y';
    return '30d';
  }

  async loadData() {
    this.isLoading = true;
    try {
      // Defer /flows/presets (~38KB) until the presets UI is shown or a flow
      // turns out to have been cloned from one.
      //
      // The flows call is the one that can empty the page, so its failure is
      // caught and shown rather than left to become an unhandled rejection
      // that skips the socket and renders "No flows yet" over an account that
      // has flows.
      const [flows, executions, activeExecutions] = await Promise.all([
        getFlows({ statsSince: this.rangeStartDate() }).catch((error) => {
          console.error('Failed to load flows:', error);
          return null;
        }),
        getFlowExecutions({ limit: EXECUTIONS_SAMPLE_LIMIT }).catch(() => []),
        getFlowExecutions({
          limit: 20,
          status: ['PENDING', 'INITIALIZING', 'STARTING', 'RUNNING'],
        }).catch(() => []),
      ]);
      if (flows === null) {
        this.loadError = 'Could not load your flows.';
        this.executions = executions;
        this.activeExecutions = activeExecutions;
        return;
      }
      this.loadError = null;
      this.flows = flows;
      this.executions = executions;
      this.activeExecutions = activeExecutions;

      if (this.flows.length === 0) {
        this.showPresets = true;
      } else if (!this.hasInitializedPresetVisibility) {
        this.showPresets = false;
      }
      this.hasInitializedPresetVisibility = true;

      if (this.showPresets) {
        void this.ensurePresetsLoaded();
      } else if (this.flows.some((flow) => flow.source_preset_id)) {
        // Cloned flows name their preset in the list and in the Type filter,
        // so the names are worth one background fetch once we know some flow
        // has a preset behind it.
        void this.ensurePresetsLoaded();
      }
      void this.loadTrackerNames();
    } finally {
      this.isLoading = false;
    }
  }

  /**
   * Re-read the flows for a new range.
   *
   * Runs, failures and spend are all per-range, so a range change refetches
   * them together. The page keeps its current numbers on screen while the
   * request is in flight instead of blanking to a spinner (DESIGN.md, "A
   * range change is not a page load").
   */
  private async loadRangeStats(): Promise<void> {
    try {
      const flows = await getFlows({ statsSince: this.rangeStartDate() });
      if (Array.isArray(flows)) this.flows = flows;
    } catch (error) {
      // Keep the numbers already on screen; they name their own window.
      console.error('Failed to refresh flow stats:', error);
    }
  }

  /**
   * `trigger_event_source` is a tracker id for tracker triggers, so the
   * Trigger column needs the names. A failure is not fatal: the column falls
   * back to the event types.
   */
  private async loadTrackerNames(): Promise<void> {
    try {
      const trackers = await getTrackers();
      const names: Record<string, string> = {};
      for (const tracker of trackers || []) {
        if (tracker?.id) names[tracker.id] = tracker.name || tracker.id;
      }
      this.trackerNames = names;
    } catch (error) {
      this.trackerNames = {};
    }
  }

  private async ensurePresetsLoaded(): Promise<void> {
    if (this.presetsLoaded || this.presetsLoading) {
      return;
    }
    this.presetsLoading = true;
    try {
      const presets = await getFlowPresets();
      this.presets = this.sortPresets(presets);
      this.presetsLoaded = true;
    } catch (error) {
      console.error('Failed to load flow presets:', error);
    } finally {
      this.presetsLoading = false;
    }
  }

  async refreshExecutions() {
    const [executions, activeExecutions] = await Promise.all([
      getFlowExecutions({ limit: EXECUTIONS_SAMPLE_LIMIT }).catch(
        () => this.executions
      ),
      getFlowExecutions({
        limit: 20,
        status: ['PENDING', 'INITIALIZING', 'STARTING', 'RUNNING'],
      }).catch(() => this.activeExecutions),
    ]);
    this.executions = executions;
    this.activeExecutions = activeExecutions;
  }

  private connectWebSocket() {
    this.unsubscribe = unifiedWebSocketManager.subscribe(
      'flow_executions',
      (message: any) => this.handleWebSocketMessage(message)
    );
  }

  private handleWebSocketMessage(message: any) {
    if (message.type === 'status_update' && message.execution_id) {
      const index = this.activeExecutions.findIndex(
        (exec) => exec.id === message.execution_id
      );
      if (index >= 0) {
        const updated = [...this.activeExecutions];
        updated[index] = {
          ...updated[index],
          status: message.payload.status,
          ...(message.payload.end_time && {
            end_time: message.payload.end_time,
          }),
        };
        this.activeExecutions = updated;
      } else {
        void this.refreshExecutions();
      }
    }

    if (message.type === 'execution_started' && message.payload) {
      void this.refreshExecutions();
    }
  }

  // --- Rows ---

  /** Preset id to name, as far as the presets fetch has told us. */
  private get presetNames(): Record<string, string> {
    const names: Record<string, string> = {};
    for (const preset of this.presets) {
      if (preset.id) names[preset.id] = preset.name;
    }
    return names;
  }

  /** Runs inside the page range, newest first, grouped by flow. */
  private get runsInRange(): Map<
    string,
    { runs: number; failed: number; last: FlowExecution | null }
  > {
    const since = timeValue(this.rangeStartDate());
    const grouped = new Map<
      string,
      { runs: number; failed: number; last: FlowExecution | null }
    >();
    const ordered = [...this.executions].sort(
      (a, b) => timeValue(b.start_time) - timeValue(a.start_time)
    );
    for (const execution of ordered) {
      if (!execution.flow_id) continue;
      if (timeValue(execution.start_time) < since) continue;
      const entry = grouped.get(execution.flow_id) || {
        runs: 0,
        failed: 0,
        last: null,
      };
      entry.runs += 1;
      if (execution.status === 'FAILED') entry.failed += 1;
      // The list is sorted newest first, so the first row wins.
      entry.last = entry.last || execution;
      grouped.set(execution.flow_id, entry);
    }
    return grouped;
  }

  /** True when the executions sample filled up, so the counts are a floor. */
  private get executionsSampleIsFull(): boolean {
    return this.executions.length >= EXECUTIONS_SAMPLE_LIMIT;
  }

  /**
   * True when the server counted the runs for this range.
   *
   * It answers `execution_stats.since` when asked for a window, and then the
   * counts are the database's, not a tally of the 200 executions this page
   * happens to hold.
   */
  private get countsFromServer(): boolean {
    return this.flows.some((flow) => Boolean(flow.execution_stats?.since));
  }

  private get sampleNote(): string {
    if (this.countsFromServer) return '';
    return this.executionsSampleIsFull
      ? ` (from the most recent ${EXECUTIONS_SAMPLE_LIMIT} runs)`
      : '';
  }

  get rows(): FlowListRow[] {
    const presetNames = this.presetNames;
    const runs = this.runsInRange;
    return this.flows.map((flow) => {
      const entry = runs.get(flow.id);
      const status = flowStatusOf(flow);
      // Runs, failures and spend for the page range come from one place. The
      // server measures all three over the window the header names; the
      // sample is the fallback for a response that predates `stats_since`,
      // and then there is no spend to state, because the only figure the
      // page has for it was measured over a different window.
      const stats = flow.execution_stats;
      const countsFromServer = Boolean(stats?.since);
      const rowRuns = countsFromServer
        ? Number(stats?.runs || 0)
        : entry?.runs || 0;
      const rowFailed = countsFromServer
        ? Number(stats?.failed || 0)
        : entry?.failed || 0;
      // Zero runs in the window means zero spend in the window. Printing a
      // figure next to "No run in the last 30d" is how the list came to
      // contradict itself.
      const rowCost =
        countsFromServer && rowRuns > 0 ? Number(stats?.cost || 0) : 0;
      const trigger = flowTriggerSummary(flow, this.trackerNames);
      const presetId = flow.source_preset_id
        ? String(flow.source_preset_id)
        : '';
      return {
        id: flow.id,
        name: flow.name || 'Untitled flow',
        presetValue: presetId || 'custom',
        presetLabel: presetId
          ? presetNames[presetId] || 'From a preset'
          : 'Custom flow',
        icon: flow.icon || 'diagram-3',
        description: flow.description?.trim() || '',
        detailUrl: `/console/flows/${encodeURIComponent(flow.id)}`,
        triggerLabel: trigger.label,
        triggerTitle: trigger.title,
        status,
        statusLabel: FLOW_STATUS_LABELS[status],
        lastRun: entry?.last || null,
        lastRunAt:
          entry?.last?.start_time ||
          (countsFromServer ? stats?.last_run_at : null) ||
          null,
        runs: rowRuns,
        failed: rowFailed,
        cost: rowCost,
        countsFromServer,
        source: flow,
      };
    });
  }

  /** The rows the filters matched, in the chosen order. */
  get visibleRows(): FlowListRow[] {
    return sortFlowListRows(
      filterFlowRows(this.rows, this.filters),
      this.sortKey,
      this.sortDirection
    );
  }

  /** The Type filter's options: the presets these flows came from, plus Custom. */
  private get presetOptions(): Array<{ value: string; label: string }> {
    const seen = new Map<string, string>();
    for (const row of this.rows) {
      if (!seen.has(row.presetValue))
        seen.set(row.presetValue, row.presetLabel);
    }
    return [...seen.entries()]
      .map(([value, label]) => ({ value, label }))
      .sort((a, b) => {
        // "Custom flow" last: it is the absence of a preset, not a preset.
        if (a.value === 'custom') return 1;
        if (b.value === 'custom') return -1;
        return a.label.localeCompare(b.label);
      });
  }

  private get resultsLabel(): string {
    if (this.isLoading) return '';
    const shown = this.visibleRows.length;
    const total = this.rows.length;
    const noun = shown === 1 ? 'flow' : 'flows';
    return shown === total
      ? `${shown} ${noun}`
      : `${shown} of ${total} ${total === 1 ? 'flow' : 'flows'}`;
  }

  private get hasActiveFilters(): boolean {
    return Boolean(
      this.filters.query ||
      this.filters.presets.length ||
      this.filters.statuses.length
    );
  }

  /**
   * Clicking a header sorts by that column; clicking the active one flips the
   * direction. Text columns start ascending (A first), numeric and time
   * columns start descending, which is what an operator scanning for the
   * busiest or most recent flow expects.
   */
  private toggleSort(key: FlowListSortKey) {
    if (this.sortKey === key) {
      this.sortDirection = this.sortDirection === 'asc' ? 'desc' : 'asc';
      return;
    }
    this.sortKey = key;
    this.sortDirection =
      key === 'runs' || key === 'failed' || key === 'cost' || key === 'last_run'
        ? 'desc'
        : 'asc';
  }

  // --- Row actions ---

  private getRowActions(row: FlowListRow): ResourceAction[] {
    const paused = row.status === 'paused';
    const actions: ResourceAction[] = [
      {
        id: 'open',
        label: 'Open',
        icon: 'box-arrow-up-right',
        href: row.detailUrl,
      },
      {
        id: 'run-now',
        label: 'Run now',
        icon: 'play-circle',
        disabled: paused || this.triggeringFlowId === row.id,
        loading: this.triggeringFlowId === row.id,
        tooltip: paused ? 'Resume the flow before running it' : undefined,
        onClick: () => void this.triggerRun(row),
      },
      {
        id: 'edit',
        label: 'Edit',
        icon: 'pencil',
        href: `${row.detailUrl}?edit=true`,
      },
      {
        id: 'toggle-enabled',
        label: paused ? 'Resume' : 'Pause',
        icon: paused ? 'play-circle' : 'pause-circle',
        onClick: () => void this.toggleFlowEnabled(row),
      },
      {
        id: 'delete',
        label: 'Delete',
        icon: 'trash',
        variant: 'danger',
        outline: true,
        separated: true,
        onClick: () => void this.deleteFlowHandler(row),
      },
    ];
    return actions;
  }

  private async triggerRun(row: FlowListRow) {
    this.triggeringFlowId = row.id;
    try {
      const execution = await triggerFlowExecution(row.id);
      Router.go(`/console/flows/executions/${execution.id}`);
    } catch (error) {
      showToast(
        error instanceof Error && error.message
          ? error.message
          : 'Could not start the flow. Try again.',
        'danger'
      );
    } finally {
      this.triggeringFlowId = null;
    }
  }

  private async toggleFlowEnabled(row: FlowListRow) {
    const next = row.status === 'paused';
    try {
      const updated = await updateFlow(row.id, { is_enabled: next });
      this.flows = this.flows.map((flow) =>
        flow.id === row.id
          ? {
              ...flow,
              ...(updated && typeof updated === 'object' ? updated : {}),
              is_enabled: next,
            }
          : flow
      );
      showToast(
        next ? `${row.name} resumed` : `${row.name} paused`,
        next ? 'success' : 'neutral'
      );
    } catch (error) {
      showToast(
        error instanceof Error && error.message
          ? error.message
          : 'Could not update the flow. Try again.',
        'danger'
      );
    }
  }

  async deleteFlowHandler(row: FlowListRow) {
    const confirmed = await confirmDialog({
      title: 'Delete flow',
      message: `Delete "${row.name}"?`,
      detail:
        'The flow and its trigger stop immediately. Past runs stay in the executions list. This cannot be undone.',
      confirmLabel: 'Delete flow',
      variant: 'danger',
    });
    if (!confirmed) return;

    try {
      await deleteFlow(row.id);
      this.flows = this.flows.filter((flow) => flow.id !== row.id);
      showToast(`${row.name} deleted`, 'success');
    } catch (error) {
      showToast(
        error instanceof Error && error.message
          ? error.message
          : 'Could not delete the flow. Try again.',
        'danger'
      );
    }
  }

  // --- Rendering ---

  render() {
    if (this.isLoading) {
      return html`
        ${this.renderViewHeader()}
        <div style="display: flex; justify-content: center; padding: 48px;">
          <sl-spinner style="font-size: 3rem;"></sl-spinner>
        </div>
      `;
    }

    const running = this.activeExecutions.filter(
      (e) => e.status === 'RUNNING' || e.status === 'STARTING'
    );

    return html`
      ${this.renderViewHeader()}
      <div class="column-layout extra-wide">
        <div class="main-column">
          ${
            running.length > 0
              ? html`
                  <div class="active-executions">
                    <div class="section-header">
                      <h2>Running now</h2>
                      <sl-button
                        size="small"
                        href=${router.urlForPath('/console/flows/executions')}
                      >
                        View all
                      </sl-button>
                    </div>
                    <div class="executions-list">
                      ${running
                        .slice(0, 5)
                        .map((exec) => this.renderExecutionItem(exec))}
                    </div>
                  </div>
                `
              : nothing
          }
          ${
            this.loadError
              ? this.renderLoadError()
              : this.flows.length > 0
                ? html`
                    ${this.renderToolbar()}
                    ${
                      this.effectiveView === 'list'
                        ? this.renderListView()
                        : this.renderCardsView()
                    }
                  `
                : this.renderEmptyAccount()
          }

          <sl-divider></sl-divider>

          <div class="section-header">
            <h2>Presets</h2>
            ${
              this.flows.length > 0
                ? html`
                    <sl-button size="small" @click=${this.togglePresets}>
                      <sl-icon
                        slot="prefix"
                        name=${this.showPresets ? 'chevron-up' : 'chevron-down'}
                      ></sl-icon>
                      ${this.showPresets ? 'Hide presets' : 'Show presets'}
                    </sl-button>
                  `
                : nothing
            }
          </div>
          ${
            this.showPresets
              ? this.presetsLoading && this.presets.length === 0
                ? html`<div class="presets-collapsed">Loading presets...</div>`
                : html`
                    <div class="presets-grid">
                      ${this.presets.map((preset) =>
                        this.renderPresetCard(preset)
                      )}
                    </div>
                  `
              : html`<div class="presets-collapsed">
                  Presets are hidden. Use "Show presets" to explore starter
                  workflows.
                </div>`
          }
        </div>
      </div>
      <sl-dialog
        label=${this.expandedDescription?.title || 'Flow description'}
        ?open=${Boolean(this.expandedDescription)}
        @sl-after-hide=${() => {
          this.expandedDescription = null;
        }}
      >
        <div style="white-space: pre-wrap; color: var(--sl-color-neutral-700);">
          ${this.expandedDescription?.description || ''}
        </div>
        <sl-button
          slot="footer"
          variant="primary"
          @click=${() => {
            this.expandedDescription = null;
          }}
        >
          Close
        </sl-button>
      </sl-dialog>
    `;
  }

  private renderViewHeader() {
    return html`
      <view-header
        headerText="Flows"
        description="Event-driven agent runs. A flow starts an agent when something happens, a new issue or a webhook, and stops it when the run completes."
        width="extra-wide"
      >
        ${
          this.flows.length > 0
            ? html`
                <div slot="main-column">
                  <sl-button
                    variant="primary"
                    @click=${() => Router.go('/console/flows/new')}
                  >
                    <sl-icon slot="prefix" name="plus-lg"></sl-icon>
                    Create flow
                  </sl-button>
                </div>
              `
            : nothing
        }
      </view-header>
    `;
  }

  /**
   * Search, kind, status, the page range, the count and the view switcher, in
   * the order the agents page established so the two collections feel like one
   * product.
   */
  private renderToolbar() {
    const presetOptions = this.presetOptions;
    return html`
      <list-toolbar
        .search=${this.filters.query}
        searchPlaceholder="Search flows"
        toggleLabel="Flows view"
        .view=${this.currentView}
        .views=${['list', 'cards']}
        @search-change=${this.handleSearchChange}
        @view-change=${this.handleViewChange}
      >
        <sl-select
          class="preset-filter"
          multiple
          clearable
          max-options-visible="1"
          placeholder="All types"
          .value=${this.filters.presets}
          @sl-change=${(event: Event) => {
            const select = event.target as HTMLElement & {
              value: string | string[];
            };
            this.filters = {
              ...this.filters,
              presets: Array.isArray(select.value)
                ? [...select.value]
                : [select.value].filter(Boolean),
            };
          }}
        >
          ${repeat(
            presetOptions,
            (option) => option.value,
            (option) =>
              html`<sl-option value=${option.value}>${option.label}</sl-option>`
          )}
        </sl-select>

        <sl-select
          class="status-filter"
          multiple
          clearable
          max-options-visible="1"
          placeholder="Any status"
          .value=${this.filters.statuses}
          @sl-change=${(event: Event) => {
            const select = event.target as HTMLElement & {
              value: string | string[];
            };
            const values = Array.isArray(select.value)
              ? select.value
              : [select.value].filter(Boolean);
            this.filters = {
              ...this.filters,
              statuses: values as FlowStatusFilter[],
            };
          }}
        >
          <sl-option value="enabled">Enabled</sl-option>
          <sl-option value="paused">Paused</sl-option>
          <sl-option value="draft">Draft</sl-option>
          <!-- The count behind this is the page range, like the Failed
               column, so the option says which window it means. -->
          <sl-option value="failing"
            >Failed in the last ${this.rangeLabel}</sl-option
          >
        </sl-select>

        <time-range-select
          ariaLabel="Flows time range"
          .value=${this.range}
          .options=${[
            { value: 'day', label: '24h' },
            { value: 'week', label: '7d' },
            { value: 'month', label: '30d' },
            { value: 'year', label: '1y' },
          ]}
          @range-change=${(event: CustomEvent) => {
            this.range = event.detail.value as FlowRange;
            void this.loadRangeStats();
          }}
        ></time-range-select>
        <span slot="count">${this.resultsLabel}</span>
      </list-toolbar>
    `;
  }

  private renderSortableHeader(
    key: FlowListSortKey,
    label: string,
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
        class="sortable ${numeric ? 'numeric' : ''} ${active ? 'active' : ''}"
        aria-sort=${ariaSort}
        scope="col"
      >
        <button
          type="button"
          class="sort-button"
          data-sort-key=${key}
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

  private renderListView() {
    const rows = this.visibleRows;
    if (rows.length === 0) {
      return html`<sl-card class="table-card"
        ><div class="empty-state">${this.emptyResultText}</div></sl-card
      >`;
    }
    const rangeNote = `In the last ${this.rangeLabel}${this.sampleNote}`;
    return html`
      <sl-card class="table-card">
        <div class="table-scroll">
          <table class="styled-table flows-table">
            <colgroup>
              <col class="col-flow" />
              <col class="col-trigger" />
              <col class="col-status" />
              <col class="col-last-run" />
              <col class="col-runs" />
              <col class="col-failed" />
              <col class="col-cost" />
              <col class="col-actions" />
            </colgroup>
            <thead>
              <tr>
                ${this.renderSortableHeader('flow', 'Flow')}
                ${this.renderSortableHeader('trigger', 'Trigger')}
                ${this.renderSortableHeader('status', 'Status')}
                ${this.renderSortableHeader('last_run', 'Last run')}
                ${this.renderSortableHeader('runs', 'Runs', true)}
                ${this.renderSortableHeader('failed', 'Failed', true)}
                ${this.renderSortableHeader('cost', '$ est.', true)}
                <th class="actions-cell">
                  <span class="visually-hidden">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              ${repeat(
                rows,
                (row) => row.id,
                (row) => this.renderListRow(row, rangeNote)
              )}
            </tbody>
          </table>
        </div>
      </sl-card>
    `;
  }

  private renderListRow(row: FlowListRow, rangeNote: string) {
    return html`
      <tr
        class="flow-row"
        @click=${(event: MouseEvent) => this.handleRowClick(event, row)}
      >
        <td class="flow-cell" title=${row.name}>
          <div class="flow-identity">
            <sl-icon name=${row.icon}></sl-icon>
            <div class="flow-identity-text">
              <a class="row-link" href=${row.detailUrl}>${row.name}</a>
              <div class="row-subtitle" title=${row.presetLabel}>
                ${row.presetLabel}
              </div>
            </div>
          </div>
        </td>
        <td class="muted-cell" title=${row.triggerTitle}>
          ${row.triggerLabel}
        </td>
        <td>
          <sl-badge
            class="status-chip"
            variant=${FLOW_STATUS_VARIANTS[row.status]}
            pill
            >${row.statusLabel}</sl-badge
          >
        </td>
        <td>${this.renderLastRun(row)}</td>
        <td class="numeric" title=${rangeNote}>
          ${
            row.runs > 0
              ? html`<a
                  class="row-link"
                  style="display: inline;"
                  href="/console/flows/executions?flow_id=${row.id}"
                  >${row.runs.toLocaleString()}</a
                >`
              : html`<span class="muted-cell">0</span>`
          }
        </td>
        <td class="numeric" title=${rangeNote}>
          ${
            row.failed > 0
              ? row.failed.toLocaleString()
              : html`<span class="muted-cell">0</span>`
          }
        </td>
        <td class="numeric" title=${`Estimated spend, last ${this.rangeLabel}`}>
          ${
            row.runs > 0
              ? this.formatMoney(row.cost)
              : html`<span class="muted-cell">-</span>`
          }
        </td>
        <td class="actions-cell">
          <div
            class="row-actions"
            @click=${(event: Event) => event.stopPropagation()}
            @keydown=${(event: Event) => event.stopPropagation()}
          >
            <resource-actions
              .actions=${this.getRowActions(row)}
              menu-only
            ></resource-actions>
          </div>
        </td>
      </tr>
    `;
  }

  private renderLastRun(row: FlowListRow) {
    const run = row.lastRun;
    if (!run) {
      // The server knows when the flow last ran even when that run is outside
      // the executions this page holds. Saying "No run" over a run the same
      // row counts is the contradiction this cell used to print.
      if (row.lastRunAt) {
        return html`<span
          class="muted-cell"
          title=${formatLocalDateTime(row.lastRunAt)}
          >Ran
          ${formatRelativeTime(row.lastRunAt, undefined, {
            maxRelativeDays: 30,
          })}</span
        >`;
      }
      return html`<span class="muted-cell"
        >No run in the last ${this.rangeLabel}</span
      >`;
    }
    const duration = executionDurationText(run);
    // Two lines, like the Flow cell: the outcome and what the run was about
    // on top, when and how long underneath. On one line the subject was the
    // only thing that could give way, and it did, down to "s.".
    return html`
      <a
        class="last-run"
        href=${`/console/flows/executions/${run.id}`}
        style="text-decoration: none; color: inherit;"
      >
        <span class="last-run-line">
          <sl-badge
            class="status-chip"
            pill
            variant=${this.getStatusVariant(run.status)}
            >${this.statusLabel(run.status)}</sl-badge
          >
          ${renderExecutionSubject(run)}
        </span>
        <span class="meta" title=${formatLocalDateTime(run.start_time)}
          >${formatRelativeTime(run.start_time, undefined, {
            maxRelativeDays: 30,
          })}${duration ? ` · ${duration}` : ''}</span
        >
      </a>
    `;
  }

  /**
   * The whole row is clickable for convenience, but the name is a real anchor
   * so cmd-click and middle-click open a tab. Let the browser handle those and
   * any click that started inside a link, a button or the kebab.
   */
  private handleRowClick(event: MouseEvent, row: FlowListRow) {
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
        tag === 'button' ||
        tag === 'sl-button' ||
        tag === 'sl-menu-item' ||
        tag === 'resource-actions'
      ) {
        return;
      }
    }
    Router.go(row.detailUrl);
  }

  private get emptyResultText(): string {
    return this.hasActiveFilters
      ? 'No flows match these filters.'
      : 'No flows yet.';
  }

  private renderCardsView() {
    const rows = this.visibleRows;
    if (rows.length === 0) {
      return html`<div class="empty-state">${this.emptyResultText}</div>`;
    }
    return html`
      <div class="flows-grid">
        ${repeat(
          rows,
          (row) => row.id,
          (row) => this.renderFlowCard(row)
        )}
      </div>
    `;
  }

  renderFlowCard(row: FlowListRow) {
    const running = Number(row.source.execution_stats?.running_execs || 0);
    return html`
      <sl-card class="flow-card" @click=${() => Router.go(row.detailUrl)}>
        <div slot="header" class="flow-header">
          <div class="flow-title">
            <sl-icon name=${row.icon}></sl-icon>
            <a
              href=${row.detailUrl}
              @click=${(event: Event) => event.stopPropagation()}
              >${row.name}</a
            >
          </div>
          <div
            class="card-actions"
            @click=${(event: Event) => event.stopPropagation()}
            @keydown=${(event: Event) => event.stopPropagation()}
          >
            <resource-actions
              .actions=${this.getRowActions(row)}
              menu-only
            ></resource-actions>
          </div>
        </div>

        ${this.renderFlowDescription(row.name, row.description)}

        <div class="card-meta">
          <span class="stat-item">
            <sl-badge
              class="status-chip"
              variant=${FLOW_STATUS_VARIANTS[row.status]}
              pill
              >${row.statusLabel}</sl-badge
            >
          </span>
          <span class="stat-item" title=${row.triggerTitle}>
            <sl-icon name="lightning"></sl-icon>${row.triggerLabel}
          </span>
          ${this.renderScheduleStat(row.source)}
          <span
            class="stat-item"
            title=${`In the last ${this.rangeLabel}${this.sampleNote}`}
          >
            <sl-icon name="play-circle"></sl-icon>${row.runs}
            runs${row.failed > 0 ? `, ${row.failed} failed` : ''}
          </span>
          ${
            row.runs > 0
              ? html`<span class="stat-item"
                  >${this.formatMoney(row.cost)} est.</span
                >`
              : nothing
          }
          ${
            running > 0
              ? html`<span class="stat-item"
                  ><sl-badge class="status-chip" variant="neutral" pill
                    >${running} running</sl-badge
                  ></span
                >`
              : nothing
          }
        </div>

        <div slot="footer" class="flow-footer">
          <div class="flow-footer-actions">
            <sl-button
              size="small"
              href=${`${row.detailUrl}?edit=true`}
              @click=${(e: Event) => e.stopPropagation()}
            >
              <sl-icon slot="prefix" name="pencil"></sl-icon>
              Edit
            </sl-button>
          </div>
          <sl-button
            size="small"
            variant="primary"
            ?disabled=${row.status === 'paused'}
            ?loading=${this.triggeringFlowId === row.id}
            @click=${(e: Event) => {
              e.stopPropagation();
              void this.triggerRun(row);
            }}
          >
            <sl-icon slot="prefix" name="play-fill"></sl-icon>
            Run now
          </sl-button>
        </div>
      </sl-card>
    `;
  }

  /**
   * When the next run happens, on the card only.
   *
   * The table says it in the Trigger cell's title, where a column cannot spare
   * a second line; a card has the room, and "when does this fire next" is the
   * question a scheduled flow raises. Neutral meta, not a warning badge: a
   * suspended schedule is a state the operator chose, and red is reserved for
   * failures (DESIGN.md, "Colour").
   */
  renderScheduleStat(flow: FlowListItem) {
    const schedule = flow.schedule_state;
    if (flow.trigger_event_source !== 'schedule' || !schedule) {
      return nothing;
    }
    return html`
      <span class="stat-item" title=${schedule.description || 'Schedule'}>
        <sl-icon name="clock"></sl-icon>
        <span
          >${
            !schedule.active
              ? 'Schedule paused'
              : schedule.next_run_at
                ? `Next run ${formatLocalDateTime(schedule.next_run_at)}`
                : 'No upcoming runs'
          }</span
        >
      </span>
    `;
  }

  /**
   * The flows fetch failed. "No flows yet" would be a lie with a Create flow
   * button under it, so the page says what happened and offers the retry.
   */
  private renderLoadError() {
    return html`
      <div class="empty-state empty-state-wrapper">
        <sl-card class="empty-card">
          <div class="empty-card-body">
            <div class="empty-icon-circle">
              <sl-icon name="exclamation-triangle"></sl-icon>
            </div>
            <h3 class="empty-card-title">Could not load your flows</h3>
            <p class="empty-card-desc">
              ${this.loadError} The list below is not empty — it is unknown.
            </p>
            <sl-button
              class="empty-cta-btn"
              variant="primary"
              @click=${() => void this.loadData()}
            >
              <sl-icon slot="prefix" name="arrow-clockwise"></sl-icon>
              Try again
            </sl-button>
          </div>
        </sl-card>
      </div>
    `;
  }

  private renderEmptyAccount() {
    return html`
      <div class="empty-state empty-state-wrapper">
        <sl-card class="empty-card">
          <div class="empty-card-body">
            <div class="empty-icon-circle">
              <sl-icon name="diagram-3"></sl-icon>
            </div>
            <h3 class="empty-card-title">No flows yet</h3>
            <p class="empty-card-desc">
              No flows yet. Create your first custom flow or clone a starter
              preset below.
            </p>
            <sl-button
              class="empty-cta-btn"
              variant="primary"
              @click=${() => Router.go('/console/flows/new')}
            >
              <sl-icon slot="prefix" name="plus-lg"></sl-icon>
              Create flow
            </sl-button>
          </div>
        </sl-card>
      </div>
    `;
  }

  renderPresetCard(preset: FlowListItem) {
    return html`
      <sl-card class="flow-card">
        <div slot="header" class="flow-header">
          <div class="flow-title">
            <sl-icon name=${preset.icon || 'gear'}></sl-icon>
            ${preset.name}
          </div>
          <div class="flow-footer-actions">
            <sl-button size="small" @click=${() => this.clonePreset(preset.id)}>
              Use preset
            </sl-button>
            ${
              preset.account_id
                ? html`
                    <sl-button
                      size="small"
                      variant="danger"
                      outline
                      @click=${() => this.removePreset(preset.id)}
                    >
                      Remove
                    </sl-button>
                  `
                : nothing
            }
          </div>
        </div>
        ${this.renderFlowDescription(preset.name, preset.description || '')}
      </sl-card>
    `;
  }

  renderExecutionItem(exec: FlowExecution) {
    const flow = this.flows.find((f) => f.id === exec.flow_id);
    const duration = executionDurationText(exec);
    return html`
      <div
        class="execution-item"
        @click=${() => Router.go(`/console/flows/executions/${exec.id}`)}
      >
        <div class="execution-info">
          <sl-badge
            class="status-chip"
            pill
            variant=${this.getStatusVariant(exec.status)}
          >
            ${this.statusLabel(exec.status)}
          </sl-badge>
          <div style="min-width: 0;">
            <strong>${flow?.name || exec.flow_name || 'Unknown flow'}</strong>
            <div class="row-subtitle">
              Started
              ${formatLocalDateTime(exec.start_time)}${
                duration ? ` · ${duration}` : ''
              }
            </div>
          </div>
        </div>
        <sl-button size="small">
          <sl-icon name="arrow-right"></sl-icon>
        </sl-button>
      </div>
    `;
  }

  /** Title case, so "SUCCEEDED" and "Active now" read as the same object. */
  private statusLabel(status: string): string {
    const text = String(status || '').replace(/_/g, ' ');
    return text.charAt(0).toUpperCase() + text.slice(1).toLowerCase();
  }

  getStatusVariant(status: string): 'success' | 'danger' | 'neutral' {
    switch (status) {
      case 'SUCCEEDED':
        return 'success';
      case 'FAILED':
        return 'danger';
      default:
        // Running and pending are neutral: a run in flight is not a problem.
        return 'neutral';
    }
  }

  /** Under a cent renders four decimals rather than collapsing to $0.00. */
  private formatMoney(value: number): string {
    if (!value) return '$0.00';
    if (value < 0.01) return `$${value.toFixed(4)}`;
    return `$${value.toFixed(2)}`;
  }

  async clonePreset(presetId: string) {
    Router.go(`/console/flows/new?preset_id=${presetId}`);
  }

  async removePreset(presetId: string) {
    await deleteFlow(presetId);
    this.presetsLoaded = false;
    await this.ensurePresetsLoaded();
  }

  private togglePresets() {
    this.showPresets = !this.showPresets;
    if (this.showPresets) {
      void this.ensurePresetsLoaded();
    }
  }

  private truncateDescription(description: string): string {
    const maxLength = 140;
    if (description.length <= maxLength) {
      return description;
    }

    const truncated = description.slice(0, maxLength).trimEnd();
    const lastSpace = truncated.lastIndexOf(' ');
    const preview =
      lastSpace > 80 ? truncated.slice(0, lastSpace).trimEnd() : truncated;
    return `${preview}...`;
  }

  private renderFlowDescription(title: string, description: string) {
    const text = description?.trim();
    const shouldShowFull = (text?.length ?? 0) > 140;
    const preview = text ? this.truncateDescription(text) : '';

    return html`
      <div class="flow-description">
        ${
          text
            ? html`
                <div class="flow-description-text">${preview}</div>
                ${
                  shouldShowFull
                    ? html`
                        <sl-button
                          class="flow-description-action"
                          size="small"
                          variant="text"
                          @click=${(event: Event) => {
                            event.stopPropagation();
                            this.expandedDescription = {
                              title,
                              description: text,
                            };
                          }}
                        >
                          Show full description
                        </sl-button>
                      `
                    : nothing
                }
              `
            : html`<span class="flow-description-placeholder"
                >No description</span
              >`
        }
      </div>
    `;
  }

  private sortPresets(presets: FlowListItem[]): FlowListItem[] {
    return [...presets].sort((a, b) => {
      const aIsPR = a.name?.toLowerCase().includes('pull request reviewer')
        ? 0
        : 1;
      const bIsPR = b.name?.toLowerCase().includes('pull request reviewer')
        ? 0
        : 1;
      return aIsPR - bIsPR;
    });
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'flows-view': FlowsView;
  }
}
