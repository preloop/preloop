import { LitElement, html, css, unsafeCSS } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import { unsafeHTML } from 'lit/directives/unsafe-html.js';
import { AnsiUp } from 'ansi_up';
import { Router } from '@vaadin/router';
import DOMPurify from 'dompurify';
import { unifiedWebSocketManager } from '../../services/unified-websocket-manager';

const ansiConverter = new AnsiUp();
import consoleStyles from '../../styles/console-styles.css?inline';
import { reducedMotionStyles } from '../../styles/reduced-motion';
import {
  getFlowExecution,
  getFlow,
  sendCommandToExecution,
  getFlowExecutionMetrics,
  getFlowExecutionLogs,
  getFlowExecutionGatewayEvents,
  getFlowExecutionGatewayEvent,
  retryFlowExecution,
} from '../../api';
import type { FlowGatewayEvent } from '../../types';
import {
  formatLocalTime,
  formatRelativeTime,
  formatUTCDateTime,
  parseUTCDate,
} from '../../utils/date';
import { RUNNING_STATUSES, executionDurationText } from '../../utils/execution';
import {
  executionSubjectCss,
  isSubjectFallback,
  renderExecutionSubject,
} from '../../utils/execution-subject';
import {
  executionModelCss,
  executionStatusLabel,
  executionStatusVariant,
  formatEstimatedCost,
  renderExecutionModel,
  type ExecutionModelSource,
  type ExecutionModelUsage,
} from '../../utils/execution-presentation';
import '../../components/preloop-gateway-event.ts';
import '../../components/view-header.ts';
import '../../components/json-tree.ts';
import '../../components/session-chat-view';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/dropdown/dropdown.js';
import '@shoelace-style/shoelace/dist/components/icon-button/icon-button.js';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/menu/menu.js';
import '@shoelace-style/shoelace/dist/components/menu-item/menu-item.js';
import '@shoelace-style/shoelace/dist/components/tab-group/tab-group.js';
import '@shoelace-style/shoelace/dist/components/tab/tab.js';
import '@shoelace-style/shoelace/dist/components/tab-panel/tab-panel.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import '@shoelace-style/shoelace/dist/components/details/details.js';
import '@shoelace-style/shoelace/dist/components/copy-button/copy-button.js';

interface FlowExecutionUpdate {
  execution_id: string;
  timestamp: string;
  type: string;
  payload: any;
}

interface FlowExecution {
  id: string;
  flow_id: string;
  status: string;
  start_time: string;
  end_time?: string;
  result?: any;
  model_alias?: string | null;
  provider_name?: string | null;
  models_used?: ExecutionModelUsage[] | null;
  actions_taken_summary?: any[];
  model_output_summary?: string;
  resolved_input_prompt?: string;
  trigger_event_details?: any;
  trigger_event_id?: string;
  agent_session_reference?: string;
  error_message?: string;
  mcp_usage_logs?: any[];
  tool_calls_count?: number;
  total_tokens?: number;
  estimated_cost?: number;
  execution_logs?: FlowExecutionUpdate[];
}

interface Flow {
  id: string;
  name: string;
  description?: string;
  agent_type: string;
  trigger_event_source: string;
  trigger_event_type: string;
}

interface ToolActivityEntry {
  key: string;
  timestamp: string;
  toolName: string;
  serverName: string;
  status?: string;
  detail?: string;
  payload?: any;
}

/** Tabs of the execution page, in the order they are shown. */
const EXECUTION_TABS = [
  'timeline',
  'output',
  'transcript',
  'logs',
  'input',
] as const;
type ExecutionTab = (typeof EXECUTION_TABS)[number];

const TAB_STORAGE_KEY = 'preloop.execution-view.tab';

/** Same threshold the session widget uses before it offers "jump to latest". */
const TIMELINE_FOLLOW_THRESHOLD_PX = 48;

function isExecutionTab(value: unknown): value is ExecutionTab {
  return (
    typeof value === 'string' &&
    (EXECUTION_TABS as readonly string[]).includes(value)
  );
}

function timelineTime(timestamp: string): number {
  const parsed = timestamp ? parseUTCDate(timestamp).getTime() : NaN;
  return Number.isNaN(parsed) ? 0 : parsed;
}

/**
 * Failed runs get one danger-toned line under the strip, so the first line
 * of the error is what has to fit; the full text lives in the Output tab.
 */
function firstErrorLine(message?: string | null): string {
  if (!message) return '';
  const line = message.split('\n').find((part) => part.trim().length > 0);
  return (line || '').trim();
}

/**
 * The gateway-events endpoint returns every log row of the execution; only
 * the model calls carry request/response detail worth a card.
 */
function isModelGatewayCall(event: FlowGatewayEvent): boolean {
  return (event.type || '').includes('model_gateway_call');
}

function shortenIdentifier(value: string, length = 8): string {
  if (!value) return '';
  return value.length <= length ? value : `${value.slice(0, length)}…`;
}

/** One entry of the merged timeline before consecutive log lines are folded. */
interface TimelineItem {
  kind: 'gateway' | 'tool' | 'status' | 'log';
  key: string;
  timestamp: string;
  gatewayEvent?: FlowGatewayEvent;
  tool?: ToolActivityEntry;
  log?: FlowExecutionUpdate;
  statusLabel?: string;
  statusVariant?: string;
  statusDetail?: string;
}

/** A timeline entry as rendered: either one event or a run of log lines. */
type TimelineRow =
  | { kind: 'event'; key: string; timestamp: string; item: TimelineItem }
  | {
      kind: 'logs';
      key: string;
      timestamp: string;
      logs: FlowExecutionUpdate[];
    };

@customElement('flow-execution-view')
export class FlowExecutionView extends LitElement {
  // Vaadin Router lifecycle callback
  onBeforeEnter(location: any) {
    this.executionId = location.params.executionId;
  }

  static styles = [
    reducedMotionStyles,
    unsafeCSS(consoleStyles),
    unsafeCSS(executionSubjectCss),
    unsafeCSS(executionModelCss),
    css`
      :host {
        display: block;
      }
      .page-loading {
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        padding: 48px;
        gap: 16px;
      }
      .back-row {
        margin-bottom: var(--sl-spacing-small);
        margin-left: -12px;
      }
      /* Body size, not meta: this line says what the run was about, and on
         this page that is second in importance only to the flow name. */
      .execution-subject-line {
        font-size: var(--console-text-body);
        margin-top: var(--sl-spacing-2x-small);
        max-width: 100%;
      }
      .status-pill {
        display: flex;
        align-items: center;
        gap: 8px;
      }
      /* One of the page's two ambient animations: the dot that says this run
         is still going. The chip beside it stays a soft tint. */
      .status-dot {
        width: 8px;
        height: 8px;
        border-radius: 50%;
        background: var(--sl-color-primary-600);
        animation: execution-pulse 2s infinite;
      }
      @keyframes execution-pulse {
        0%,
        100% {
          opacity: 1;
        }
        50% {
          opacity: 0.4;
        }
      }
      .header-actions {
        display: flex;
        align-items: center;
        justify-content: flex-end;
        flex: 1;
        min-width: 0;
        gap: 8px;
        flex-wrap: wrap;
      }
      /* The facts about the run live on one hairline row, not in five boxes:
         labels in the meta register, values in body ink. */
      .summary-strip {
        display: flex;
        flex-wrap: wrap;
        align-items: baseline;
        gap: 8px 28px;
        padding: 12px 0;
        border-top: 1px solid var(--console-hairline);
        border-bottom: 1px solid var(--console-hairline);
        margin-bottom: 16px;
      }
      .strip-item {
        display: flex;
        align-items: baseline;
        gap: 6px;
        min-width: 0;
      }
      .strip-label {
        color: var(--console-meta-color);
        font-size: var(--console-text-meta);
      }
      .strip-value {
        display: inline-flex;
        align-items: baseline;
        gap: 6px;
        color: var(--sl-color-neutral-900);
        font-size: var(--console-text-body);
        font-variant-numeric: tabular-nums;
        overflow-wrap: anywhere;
      }
      .strip-note {
        color: var(--console-meta-color);
        font-size: var(--console-text-meta);
      }
      .strip-code {
        font-family: var(--sl-font-mono);
        font-size: var(--console-text-meta);
      }
      .strip-link {
        color: var(--sl-color-primary-700);
        font-family: var(--sl-font-mono);
        font-size: var(--console-text-meta);
        text-decoration: none;
      }
      .strip-link:hover,
      .strip-link:focus-visible {
        text-decoration: underline;
      }
      .strip-value sl-copy-button::part(button) {
        padding: 0 2px;
      }
      /* Red appears twice on this page: the status pill and this line. */
      .error-line {
        display: flex;
        align-items: center;
        gap: 8px;
        margin: -4px 0 16px;
        color: var(--sl-color-danger-700);
        font-size: var(--console-text-body);
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .execution-tabs {
        margin-bottom: 16px;
      }
      .execution-tabs::part(base) {
        --indicator-color: var(--sl-color-primary-600);
      }
      .panel-empty {
        padding: 32px 0;
        color: var(--console-meta-color);
        font-size: var(--console-text-body);
      }
      .timeline-panel {
        position: relative;
      }
      .timeline-toolbar {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
        padding-bottom: 8px;
      }
      .timeline-count {
        color: var(--console-meta-color);
        font-size: var(--console-text-meta);
        font-variant-numeric: tabular-nums;
      }
      /* While the run is live the stream owns a viewport-sized region and
         scrolls inside itself, so the newest item stays where the eye is.
         A finished run is a document: it scrolls with the page. */
      .timeline-stream.is-live {
        height: calc(100dvh - 340px);
        min-height: 320px;
        overflow-y: auto;
        padding-right: 4px;
      }
      .timeline-row {
        display: grid;
        grid-template-columns: 76px minmax(0, 1fr);
        gap: 12px;
        padding: 8px 0;
        border-bottom: 1px solid var(--console-hairline);
      }
      .timeline-row:last-child {
        border-bottom: none;
      }
      .timeline-time {
        color: var(--console-meta-color);
        font-size: var(--console-text-meta);
        font-variant-numeric: tabular-nums;
        white-space: nowrap;
      }
      .timeline-body {
        min-width: 0;
      }
      .timeline-title {
        color: var(--sl-color-neutral-900);
        font-size: var(--console-text-body);
        font-weight: 600;
      }
      .timeline-meta {
        color: var(--console-meta-color);
        font-size: var(--console-text-meta);
        overflow-wrap: anywhere;
      }
      .timeline-detail {
        margin: 6px 0;
      }
      .timeline-summary {
        display: flex;
        align-items: center;
        gap: 10px;
        flex-wrap: wrap;
      }
      .timeline-details::part(base) {
        border: none;
        background: transparent;
      }
      .timeline-details::part(header) {
        padding: 0;
      }
      .timeline-details::part(content) {
        padding: 8px 0 0;
        border: none;
      }
      .log-group-toggle {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        border: none;
        background: none;
        padding: 0;
        color: var(--console-meta-color);
        font-family: inherit;
        font-size: var(--console-text-meta);
        cursor: pointer;
      }
      .log-group-toggle:hover {
        color: var(--sl-color-neutral-900);
      }
      .log-group-lines {
        margin-top: 8px;
        padding: 8px 12px;
        border-radius: var(--sl-border-radius-medium);
        background: var(--sl-color-neutral-100);
        font-family: var(--sl-font-mono);
        font-size: 12px;
        max-height: 320px;
        overflow: auto;
      }
      .jump-latest {
        position: sticky;
        bottom: 12px;
        display: block;
        width: fit-content;
        margin: -12px auto 0;
      }
      .output-panel,
      .input-panel {
        display: flex;
        flex-direction: column;
        gap: 24px;
      }
      .section-title {
        display: flex;
        align-items: center;
        gap: 8px;
        margin: 0 0 8px;
        font-size: var(--console-text-body);
        font-weight: 600;
        color: var(--sl-color-neutral-900);
      }
      .error-block,
      .prompt-block,
      .output-summary {
        margin: 0;
        padding: 12px;
        border-radius: var(--sl-border-radius-medium);
        background: var(--sl-color-neutral-100);
        font-family: var(--sl-font-mono);
        font-size: 12px;
        line-height: 1.5;
        white-space: pre-wrap;
        word-break: break-word;
        max-height: 420px;
        overflow: auto;
      }
      .error-block {
        color: var(--sl-color-danger-700);
      }
      .logs-panel {
        display: flex;
        flex-direction: column;
        gap: 12px;
      }
      .logs-toolbar {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
        flex-wrap: wrap;
      }
      .logs-toolbar-meta {
        display: flex;
        align-items: center;
        gap: 12px;
      }
      .log-search {
        max-width: 320px;
        flex: 1;
      }
      .load-previous {
        text-align: center;
        padding: 6px 0 10px;
      }
      .transcript-panel {
        min-height: 240px;
      }
      .log-container {
        background-color: #1e1e1e;
        color: #d4d4d4;
        border: 1px solid var(--sl-color-neutral-300);
        border-radius: 4px;
        padding: 16px;
        height: 500px;
        overflow-y: auto;
        font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', 'Consolas', monospace;
        font-size: 13px;
        line-height: 1.5;
      }
      .log-container::-webkit-scrollbar {
        width: 8px;
      }
      .log-container::-webkit-scrollbar-track {
        background: #2d2d2d;
      }
      .log-container::-webkit-scrollbar-thumb {
        background: #555;
        border-radius: 4px;
      }
      .log-container::-webkit-scrollbar-thumb:hover {
        background: #666;
      }
      .log-entry {
        display: flex;
        margin-bottom: 4px;
        line-height: 1.5;
      }
      .log-timestamp {
        color: #858585;
        margin-right: 12px;
        user-select: none;
        -webkit-user-select: none;
        -moz-user-select: none;
        min-width: 90px;
        flex-shrink: 0;
      }
      .log-type {
        color: #4ec9b0;
        font-weight: 600;
        margin-right: 8px;
      }
      .log-type-error {
        color: #f48771;
      }
      .log-type-success {
        color: #b5cea8;
      }
      .log-type-warning {
        color: #dcdcaa;
      }
      .log-stderr {
        color: #f48771;
      }
      .log-metadata {
        background-color: #2d2d30;
        border-left: 3px solid #4ec9b0;
        padding-left: 8px;
      }
      .log-content {
        white-space: pre-wrap;
        word-wrap: break-word;
        flex: 1;
        overflow-wrap: break-word;
      }
      .empty-logs {
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        height: 100%;
        color: #858585;
        gap: 12px;
      }
      /* Log lines folded into the timeline read on the page surface, not in
         the terminal box the Logs tab uses. */
      .log-group-lines .log-entry {
        color: var(--sl-color-neutral-800);
      }
      .log-group-lines .log-timestamp {
        color: var(--console-meta-color);
      }
      /* The two navigations are buttons on a desktop and menu items on a
         phone, where the header only has room for the primary action. */
      .header-actions sl-menu-item.narrow-action {
        display: none;
      }
      @media (max-width: 700px) {
        .header-actions .secondary-action {
          display: none;
        }
        .header-actions sl-menu-item.narrow-action {
          display: block;
        }
        .summary-strip {
          gap: 8px 20px;
        }
        .timeline-row {
          grid-template-columns: 64px minmax(0, 1fr);
          gap: 8px;
        }
        .timeline-stream.is-live {
          height: calc(100dvh - 420px);
        }
      }
    `,
  ];

  @property()
  executionId?: string;

  @state()
  private execution: FlowExecution | null = null;

  @state()
  private flow: Flow | null = null;

  @state()
  private logs: FlowExecutionUpdate[] = [];

  @state()
  private hasMoreLogs = false;

  @state()
  private logsSkip = 0;

  @state()
  private isFetchingMoreLogs = false;

  @state()
  private gatewayEvents: FlowGatewayEvent[] = [];

  @state()
  private gatewayEventsSource: 'container' | 'database' | null = null;

  @state()
  private gatewayEventsError: string | null = null;

  @state()
  private isLoadingGatewayEvents = false;

  @state()
  private toolCalls = 0;

  @state()
  private liveToolActivityEvents: FlowExecutionUpdate[] = [];

  @state()
  private budgetUsed = 0;

  @state()
  private totalTokens = 0;

  @state()
  private hasPricing = false;

  @state()
  private isAutoScroll = true;

  @state()
  private loadingError: string | null = null;

  @state()
  private isLoading = false;

  @state()
  private isRetrying = false;

  /**
   * Clock used to render the elapsed time of a still-running execution.
   * Advanced once a second by `durationTickIntervalId` so the Timing card
   * counts up without waiting for a log message or a page reload.
   */
  @state()
  private durationNow: Date = new Date();

  /** Which tab is showing; seeded from `?tab=` or the remembered choice. */
  @state()
  private activeTab: ExecutionTab = 'timeline';

  /** Whether the timeline pins itself to the newest entry as items arrive. */
  @state()
  private followLive = true;

  @state()
  private atTimelineBottom = true;

  @state()
  private logSearchQuery = '';

  @state()
  private expandedLogGroups: Set<string> = new Set();

  private durationTickIntervalId?: number;

  private logContainerRef?: HTMLElement;
  private wsConnected = false;
  private autoScrollInterval?: number;
  private unsubscribe?: () => void;

  // Buffered log rendering - prevents scroll issues when many lines arrive at once
  private logBuffer: FlowExecutionUpdate[] = [];
  private bufferFlushInterval?: number;
  private readonly BUFFER_FLUSH_INTERVAL_MS = 500;
  private readonly MAX_LINES_PER_FLUSH = 5;

  connectedCallback() {
    super.connectedCallback();
    // A deep link wins over the remembered tab, so a shared "?tab=logs" URL
    // opens on logs for whoever receives it.
    this.activeTab = this.resolveInitialTab();
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    // Clean up auto-scroll interval when component is removed
    if (this.autoScrollInterval) {
      clearInterval(this.autoScrollInterval);
      this.autoScrollInterval = undefined;
    }
    // Clean up buffer flush interval
    if (this.bufferFlushInterval) {
      clearInterval(this.bufferFlushInterval);
      this.bufferFlushInterval = undefined;
    }
    // Stop the elapsed-duration ticker
    this.stopDurationTicker();
    // Unsubscribe from WebSocket
    this.unsubscribe?.();
  }

  /** Whether the loaded execution has not reached a terminal state yet. */
  private isExecutionRunning(): boolean {
    const status = this.execution?.status;
    return status !== undefined && RUNNING_STATUSES.has(status);
  }

  private startDurationTicker() {
    // Started from updated(); do not touch reactive state here, or every
    // render of a running execution would schedule another one.
    if (this.durationTickIntervalId !== undefined) return;
    this.durationTickIntervalId = window.setInterval(() => {
      this.durationNow = new Date();
    }, 1000);
  }

  private stopDurationTicker() {
    if (this.durationTickIntervalId !== undefined) {
      clearInterval(this.durationTickIntervalId);
      this.durationTickIntervalId = undefined;
    }
  }

  /**
   * Runs the elapsed-time ticker only while the execution is live, so a
   * finished run does not leave a timer behind re-rendering forever.
   */
  private syncDurationTicker() {
    if (this.isExecutionRunning()) {
      this.startDurationTicker();
    } else {
      this.stopDurationTicker();
    }
  }

  /**
   * Duration for the summary strip: the final span once the run ended, a
   * live-ticking elapsed value while it runs (measured against
   * `durationNow`, which the ticker refreshes), and a dash when the
   * timestamps cannot produce anything meaningful.
   */
  private renderDurationText(): string {
    const execution = this.execution;
    if (!execution) return '—';

    return executionDurationText(execution, this.durationNow) || '—';
  }

  async updated(changedProperties: Map<string, any>) {
    super.updated(changedProperties);

    // Keep the elapsed-duration ticker aligned with the execution's state:
    // start it once a running execution is loaded, stop it as soon as the
    // status turns terminal.
    this.syncDurationTicker();

    // While following, every new timeline entry pulls the stream down with
    // it; a paused stream stays exactly where the operator left it.
    if (
      this.followLive &&
      this.activeTab === 'timeline' &&
      this.isExecutionRunning()
    ) {
      this.scrollTimelineToBottom();
    }

    // When executionId property changes, fetch execution data
    if (
      changedProperties.has('executionId') &&
      this.executionId &&
      !this.wsConnected
    ) {
      // First, fetch execution data (which loads persisted logs)
      await this.fetchExecution();

      console.log(`After fetchExecution, logs.length = ${this.logs.length}`);
      console.log(`Execution status: ${this.execution?.status}`);
      console.log(
        `Has model_output_summary: ${!!this.execution?.model_output_summary}`
      );
      if (this.execution?.model_output_summary) {
        console.log(
          `model_output_summary length: ${this.execution.model_output_summary.length} chars`
        );
      }

      // Check if execution is still running
      const isRunning =
        this.execution &&
        (this.execution.status === 'RUNNING' ||
          this.execution.status === 'STARTING' ||
          this.execution.status === 'INITIALIZING' ||
          this.execution.status === 'PENDING');

      console.log(`isRunning: ${isRunning}`);

      // If finished, show model_output_summary in logs
      if (!isRunning && this.execution?.model_output_summary) {
        console.log(
          'Adding model_output_summary to logs (execution finished on page load)'
        );
        this.logs = [
          ...this.logs,
          {
            execution_id: this.executionId,
            timestamp: this.execution.end_time || new Date().toISOString(),
            type: 'model_output',
            payload: { content: this.execution.model_output_summary },
          },
        ];
        console.log(`Updated logs.length = ${this.logs.length}`);
      } else {
        console.log(
          `Not adding model_output: isRunning=${isRunning}, has_summary=${!!this.execution?.model_output_summary}`
        );
      }

      // Scroll to bottom after logs are loaded
      if (this.logs.length > 0) {
        setTimeout(() => this.scrollToBottom(), 100);
      }

      // Only connect to WebSocket if execution is still running
      if (isRunning) {
        this.wsConnected = true;
        this.isAutoScroll = true; // Enable auto-scroll for streaming
        this.startAutoScrollChecker(); // Start periodic scroll checker
        this.startBufferFlush(); // Start buffered log rendering

        // Subscribe to flow execution updates for this specific execution
        this.unsubscribe = unifiedWebSocketManager.subscribe(
          'flow_executions',
          (message: any) => this.handleWebSocketMessage(message),
          // Filter to only receive messages for this execution
          (message: any) => message.execution_id === this.executionId
        );

        // Track connection state
        unifiedWebSocketManager.onStateChange((state) => {
          const wasConnected = this.wsConnected;
          this.wsConnected = state === 'connected';

          // Add connection log if this is initial connection
          if (
            state === 'connected' &&
            !wasConnected &&
            this.logs.length === 0
          ) {
            console.log('Adding connection log message');
            this.logs = [
              {
                execution_id: this.executionId!,
                timestamp: new Date().toISOString(),
                type: 'connected',
                payload: { message: 'Connected to flow execution stream' },
              },
            ];
          }

          // Stop auto-scroll and buffer flush when disconnected
          if (state !== 'connected') {
            this.stopAutoScrollChecker();
            this.stopBufferFlush();
          }
        });
      } else {
        console.log(
          'Execution is finished, not connecting to WebSocket stream'
        );
      }
    }
  }

  async loadPreviousLogs() {
    if (!this.executionId || !this.hasMoreLogs || this.isFetchingMoreLogs)
      return;

    this.isFetchingMoreLogs = true;
    const FETCH_LIMIT = 500;

    // We want to skip what we've already fetched.
    this.logsSkip += FETCH_LIMIT;

    try {
      // Remember the current scroll position so we can restore it after adding logs at the top
      const scrollElement =
        this.logContainerRef ||
        (this.shadowRoot?.querySelector('.log-container') as HTMLElement);
      let prevScrollHeight = 0;
      let prevScrollTop = 0;
      if (scrollElement) {
        prevScrollHeight = scrollElement.scrollHeight;
        prevScrollTop = scrollElement.scrollTop;
      }

      const logsResult = await getFlowExecutionLogs(this.executionId, {
        tail: FETCH_LIMIT,
        skip: this.logsSkip,
      });

      if (
        logsResult &&
        Array.isArray(logsResult.logs) &&
        logsResult.logs.length > 0
      ) {
        // Prepend new logs to the existing logs array
        this.logs = [...logsResult.logs, ...this.logs];
        this.hasMoreLogs = !!logsResult.has_more;

        // Restore scroll position so content doesn't jump
        if (scrollElement) {
          // Wait for render cycle to complete
          this.updateComplete.then(() => {
            const newScrollHeight = scrollElement.scrollHeight;
            scrollElement.scrollTop =
              prevScrollTop + (newScrollHeight - prevScrollHeight);
          });
        }
      } else {
        // No more logs actually returned
        this.hasMoreLogs = false;
      }
    } catch (error) {
      console.error('Failed to load previous logs:', error);
      // Revert skip on failure
      this.logsSkip -= FETCH_LIMIT;
    } finally {
      this.isFetchingMoreLogs = false;
      this.requestUpdate();
    }
  }

  private handleWebSocketMessage(message: any) {
    console.log('WebSocket message:', message);

    // Handle connection confirmation
    if (message.type === 'connected') {
      return; // Already handled in onOpen callback
    }

    // Handle NATS forwarded messages
    if (message.execution_id === this.executionId) {
      if (message.type === 'model_gateway_call') {
        this.appendGatewayEvent(message as FlowGatewayEvent);
        return;
      }

      if (message.type === 'tool_call' || message.type === 'mcp_call') {
        this.liveToolActivityEvents = [...this.liveToolActivityEvents, message];
        this.toolCalls++;
        return;
      }

      // For agent log lines, add to buffer for controlled rendering
      // For other message types (status updates, etc.), add directly
      if (message.type === 'agent_log_line') {
        this.logBuffer.push(message);
      } else {
        this.logs = [...this.logs, message];
      }

      // Update execution status
      if (message.type === 'status_update' && this.execution) {
        const previousStatus = this.execution.status;

        if (message.payload.status) {
          this.execution.status = message.payload.status;
        }
        // Update other fields if provided
        if (message.payload.resolved_input_prompt) {
          this.execution.resolved_input_prompt =
            message.payload.resolved_input_prompt;
        }
        if (message.payload.model_output_summary) {
          this.execution.model_output_summary =
            message.payload.model_output_summary;

          // Check if execution just finished and model_output_summary is provided
          const wasRunning =
            previousStatus === 'RUNNING' ||
            previousStatus === 'STARTING' ||
            previousStatus === 'INITIALIZING' ||
            previousStatus === 'PENDING';
          const isNowFinished =
            this.execution.status !== 'RUNNING' &&
            this.execution.status !== 'STARTING' &&
            this.execution.status !== 'INITIALIZING' &&
            this.execution.status !== 'PENDING';

          // If execution just finished, flush remaining buffer and add model output
          if (wasRunning && isNowFinished) {
            // Flush any remaining buffered logs first
            this.stopBufferFlush();

            // Check if we haven't already added it
            const hasModelOutput = this.logs.some(
              (log) => log.type === 'model_output'
            );
            if (!hasModelOutput) {
              this.logs = [
                ...this.logs,
                {
                  execution_id: this.executionId!,
                  timestamp: new Date().toISOString(),
                  type: 'model_output',
                  payload: { content: this.execution.model_output_summary },
                },
              ];
            }
          }
        }
        this.requestUpdate();
      }
      // Handle real-time tool calls update
      if (message.type === 'tool_calls_update') {
        this.toolCalls = message.payload.tool_calls || 0;
      }

      // Handle real-time token usage update
      if (
        message.type === 'token_usage_update' &&
        !this.hasGatewayUsageEvents()
      ) {
        this.totalTokens = message.payload.total_tokens || 0;
        if (typeof message.payload.estimated_cost === 'number') {
          this.budgetUsed = message.payload.estimated_cost;
          this.hasPricing = true;
        }
      }

      // Track budget usage
      if (message.type === 'budget_update' && !this.hasGatewayUsageEvents()) {
        this.budgetUsed = message.payload.budget_used || 0;
      }

      // For non-buffered messages, scroll immediately
      if (message.type !== 'agent_log_line' && this.isAutoScroll) {
        this.updateComplete.then(() => this.scrollToBottom());
      }
    }
  }

  private copyAllLogs() {
    const text = this.logs
      .map((l) =>
        typeof l.payload === 'string'
          ? l.payload
          : l.payload?.content ||
            l.payload?.message ||
            l.payload?.line ||
            JSON.stringify(l.payload)
      )
      .join('\n');
    navigator.clipboard.writeText(text);
    this.dispatchEvent(
      new CustomEvent('show-toast', {
        bubbles: true,
        composed: true,
        detail: { message: 'Logs copied to clipboard!' },
      })
    );
  }

  startAutoScrollChecker() {
    // Clear any existing interval
    this.stopAutoScrollChecker();

    this.logContainerRef = this.shadowRoot?.querySelector(
      '.log-container'
    ) as HTMLElement;
    if (this.logContainerRef) {
      this.logContainerRef.addEventListener('scroll', () =>
        this.handleScroll()
      );
    }
    // Fallback: check scroll position every 500ms and force scroll if needed
    // Primary scrolling is done via updateComplete in handleWebSocketMessage
    this.autoScrollInterval = window.setInterval(() => {
      if (this.isAutoScroll && this.logContainerRef) {
        const { scrollTop, scrollHeight, clientHeight } = this.logContainerRef;
        const isAtBottom = scrollHeight - scrollTop - clientHeight < 50;

        // If not at bottom, force scroll (fallback for missed updates)
        if (!isAtBottom) {
          this.logContainerRef.scrollTop = this.logContainerRef.scrollHeight;
        }
      }
    }, 500);
  }

  stopAutoScrollChecker() {
    if (this.autoScrollInterval) {
      clearInterval(this.autoScrollInterval);
      this.autoScrollInterval = undefined;
    }
  }

  startBufferFlush() {
    // Clear any existing interval
    this.stopBufferFlush();

    // Flush buffer periodically
    this.bufferFlushInterval = window.setInterval(() => {
      this.flushLogBuffer();
    }, this.BUFFER_FLUSH_INTERVAL_MS);
  }

  stopBufferFlush() {
    if (this.bufferFlushInterval) {
      clearInterval(this.bufferFlushInterval);
      this.bufferFlushInterval = undefined;
    }
    // Flush any remaining logs when stopping
    if (this.logBuffer.length > 0) {
      this.logs = [...this.logs, ...this.logBuffer];
      this.logBuffer = [];
      if (this.isAutoScroll) {
        this.updateComplete.then(() => this.scrollToBottom());
      }
    }
  }

  flushLogBuffer() {
    if (this.logBuffer.length === 0) return;

    // Take up to MAX_LINES_PER_FLUSH from buffer
    const linesToAdd = this.logBuffer.splice(0, this.MAX_LINES_PER_FLUSH);
    this.logs = [...this.logs, ...linesToAdd];

    // Scroll after adding lines
    if (this.isAutoScroll) {
      this.updateComplete.then(() => this.scrollToBottom());
    }
  }

  scrollToBottom() {
    // Get fresh reference in case DOM was updated
    const container =
      this.logContainerRef ||
      (this.shadowRoot?.querySelector('.log-container') as HTMLElement);
    if (container) {
      this.logContainerRef = container;
      // Use requestAnimationFrame for smoother scrolling after DOM paint
      requestAnimationFrame(() => {
        container.scrollTop = container.scrollHeight;
      });
    }
  }

  handleScroll() {
    if (!this.logContainerRef) return;

    // Check if user scrolled away from bottom
    const { scrollTop, scrollHeight, clientHeight } = this.logContainerRef;
    const isAtBottom = scrollHeight - scrollTop - clientHeight < 50; // 50px threshold

    // Disable auto-scroll when user manually scrolls away from bottom
    if (!isAtBottom && this.isAutoScroll) {
      console.log('User scrolled away from bottom, disabling auto-scroll');
      this.isAutoScroll = false;
    } else if (isAtBottom && !this.isAutoScroll) {
      // Re-enable auto-scroll when user scrolls back to bottom
      console.log('User scrolled to bottom, enabling auto-scroll');
      this.isAutoScroll = true;
      // Restart the checker if execution is still running
      if (this.wsConnected) {
        this.startAutoScrollChecker();
      }
    }
  }

  gatewayEventsLoaded = false;

  /**
   * True once the events were fetched with full payloads. The metadata-only
   * fetch drops `conversation_preview`, which is exactly what the transcript
   * is built from, so the transcript tab has to ask for the full payloads.
   */
  gatewayEventsFullLoaded = false;

  async loadGatewayEvents(metadataOnly: boolean = false) {
    if (!this.executionId) return;
    this.isLoadingGatewayEvents = true;
    try {
      const response = await getFlowExecutionGatewayEvents(
        this.executionId,
        undefined,
        metadataOnly
      );
      this.gatewayEvents = response.logs || [];
      this.gatewayEventsSource = response.source;
      this.gatewayEventsLoaded = true;
      this.gatewayEventsFullLoaded = !metadataOnly;
      this.applyGatewayMetricsFromEvents();
    } catch (error) {
      console.error('Failed to fetch gateway events:', error);
      this.gatewayEventsError =
        error instanceof Error
          ? error.message
          : 'Failed to load execution gateway events';
    } finally {
      this.isLoadingGatewayEvents = false;
    }
  }

  async handleGatewayEventExpand(eventId: string) {
    if (!this.executionId || !eventId) return;

    const event = this.gatewayEvents.find((ev) => ev.id === eventId);
    if (!event || (event as any).fullPayloadLoaded) return;

    try {
      const fullEvent = await getFlowExecutionGatewayEvent(
        this.executionId,
        eventId
      );
      event.payload = fullEvent.payload;
      (event as any).fullPayloadLoaded = true;
      this.requestUpdate();
    } catch (error) {
      console.error('Failed to load full gateway event payload:', error);
    }
  }

  async fetchExecution() {
    if (!this.executionId) return;

    try {
      this.isLoading = true;
      this.isLoadingGatewayEvents = true;
      this.loadingError = null;
      this.gatewayEventsError = null;
      this.gatewayEvents = [];
      this.gatewayEventsSource = null;
      this.gatewayEventsLoaded = false;
      this.gatewayEventsFullLoaded = false;
      this.liveToolActivityEvents = [];

      // Fetch execution details
      this.execution = await getFlowExecution(this.executionId);
      this.hydrateMetricsFromExecution();

      // Fetch logs
      const INITIAL_FETCH_LIMIT = 500;
      this.logsSkip = 0;

      const logsResult = await getFlowExecutionLogs(this.executionId, {
        tail: INITIAL_FETCH_LIMIT,
      }).catch((error) => {
        console.error('Failed to fetch logs:', error);
        if (
          this.execution &&
          this.execution.execution_logs &&
          Array.isArray(this.execution.execution_logs)
        ) {
          console.log(
            `Using fallback: Loaded ${this.execution.execution_logs.length} persisted logs from database`
          );
          return {
            logs: this.execution.execution_logs,
            source: 'fallback',
            has_more: false,
          };
        }
        console.log('No fallback logs available');
        return { logs: [], source: 'none', has_more: false };
      });

      if (logsResult && Array.isArray(logsResult.logs)) {
        console.log(
          `Loaded ${logsResult.logs.length} logs from ${logsResult.source}`
        );
        this.logs = logsResult.logs;
        this.hasMoreLogs = !!logsResult.has_more;
      }
      this.hydrateToolActivityLogs();

      // The timeline is the default tab and merges gateway requests, so the
      // events are part of the first paint rather than a tab-open fetch.
      // A deep link straight to the transcript needs the full payloads.
      this.isLoadingGatewayEvents = false;
      void this.loadGatewayEvents(this.activeTab !== 'transcript');

      // Fetch flow details
      if (this.execution && this.execution.flow_id) {
        try {
          this.flow = await getFlow(this.execution.flow_id);
        } catch (error) {
          console.error('Failed to fetch flow details:', error);
          // Don't fail the whole page if flow fetch fails
        }
      }

      // Fetch execution metrics (for completed executions)
      if (this.execution) {
        try {
          const metrics = await getFlowExecutionMetrics(this.executionId);
          this.toolCalls = Math.max(this.toolCalls, metrics.tool_calls);
          this.budgetUsed = Math.max(this.budgetUsed, metrics.estimated_cost);
          this.totalTokens = Math.max(
            this.totalTokens,
            metrics.token_usage.total_tokens
          );
          this.hasPricing = this.hasPricing || metrics.has_pricing;
          console.log('Loaded execution metrics:', metrics);
        } catch (error) {
          console.error('Failed to fetch execution metrics:', error);
          // Don't fail the whole page if metrics fetch fails
        }
      }

      this.isLoading = false;
    } catch (error) {
      console.error('Failed to fetch execution:', error);
      this.loadingError =
        error instanceof Error
          ? error.message
          : 'Failed to load execution details';
      this.isLoading = false;
      this.isLoadingGatewayEvents = false;
    }
  }

  private appendGatewayEvent(event: FlowGatewayEvent) {
    const nextEventKey = this.getGatewayEventKey(event);
    const exists = this.gatewayEvents.some(
      (existingEvent) => this.getGatewayEventKey(existingEvent) === nextEventKey
    );
    if (!exists) {
      this.gatewayEvents = [...this.gatewayEvents, event];
      this.gatewayEventsSource = 'container';
      this.applyGatewayMetricsFromEvents();
    }
  }

  private hasGatewayUsageEvents(): boolean {
    return this.gatewayEvents.some((event) =>
      this.gatewayEventHasUsageMetrics(event)
    );
  }

  private gatewayEventHasUsageMetrics(event: FlowGatewayEvent): boolean {
    return (
      typeof event.payload.total_tokens === 'number' ||
      typeof event.payload.estimated_cost === 'number'
    );
  }

  private applyGatewayMetricsFromEvents() {
    const summary = this.gatewayEvents.reduce(
      (totals, event) => {
        totals.totalTokens += this.getGatewayMetricNumber(
          event.payload.total_tokens
        );
        totals.estimatedCost += this.getGatewayMetricNumber(
          event.payload.estimated_cost
        );
        // A zero cost on a token-bearing call is an unpriced model, not a
        // free call, so it must not mark the execution as priced.
        if (
          (typeof event.payload.estimated_cost === 'number' &&
            (event.payload.estimated_cost > 0 ||
              !this.getGatewayMetricNumber(event.payload.total_tokens))) ||
          (event.payload.budget as { pricing_available?: unknown } | null)
            ?.pricing_available === true
        ) {
          totals.hasPricing = true;
        }
        return totals;
      },
      { totalTokens: 0, estimatedCost: 0, hasPricing: false }
    );

    if (
      summary.totalTokens > 0 ||
      summary.estimatedCost > 0 ||
      summary.hasPricing
    ) {
      this.totalTokens = summary.totalTokens;
      this.budgetUsed = summary.estimatedCost;
      this.hasPricing = summary.hasPricing;
    }
  }

  private getGatewayMetricNumber(value: number | null | undefined): number {
    return typeof value === 'number' && !Number.isNaN(value) ? value : 0;
  }

  private hydrateMetricsFromExecution() {
    if (!this.execution) {
      return;
    }

    const executionToolCalls =
      typeof this.execution.tool_calls_count === 'number'
        ? this.execution.tool_calls_count
        : Array.isArray(this.execution.mcp_usage_logs)
          ? this.execution.mcp_usage_logs.length
          : 0;
    this.toolCalls = executionToolCalls;

    if (!this.hasGatewayUsageEvents()) {
      if (typeof this.execution.total_tokens === 'number') {
        this.totalTokens = this.execution.total_tokens;
      }
      if (typeof this.execution.estimated_cost === 'number') {
        this.budgetUsed = this.execution.estimated_cost;
        // Only a non-zero stored cost proves the execution was priced. A 0
        // alongside spent tokens means "we could not price this", not "this
        // was free", and must fall through to the token display.
        if (this.execution.estimated_cost > 0) {
          this.hasPricing = true;
        }
      }
    }
  }

  private hydrateToolActivityLogs() {
    if (!this.execution?.mcp_usage_logs?.length) {
      return;
    }
    if (this.execution.mcp_usage_logs.length > this.toolCalls) {
      this.toolCalls = this.execution.mcp_usage_logs.length;
    }
    const existingToolLogKeys = new Set(
      this.logs
        .filter((log) => log.type === 'mcp_call')
        .map((log) =>
          [
            log.timestamp,
            log.payload?.server_name ?? 'MCP',
            log.payload?.tool_name ?? '',
          ].join(':')
        )
    );
    const synthesizedLogs = this.execution.mcp_usage_logs
      .map((entry) => {
        const timestamp =
          typeof entry?.timestamp === 'string'
            ? entry.timestamp
            : this.execution?.start_time || new Date().toISOString();
        const toolName =
          typeof entry?.tool_name === 'string' ? entry.tool_name.trim() : '';
        const serverName =
          typeof entry?.server_name === 'string' && entry.server_name.trim()
            ? entry.server_name.trim()
            : 'MCP';
        if (!toolName) {
          return null;
        }
        const key = [timestamp, serverName, toolName].join(':');
        if (existingToolLogKeys.has(key)) {
          return null;
        }
        existingToolLogKeys.add(key);
        return {
          execution_id: this.executionId!,
          timestamp,
          type: 'mcp_call',
          payload: {
            ...entry,
            tool_name: toolName,
            server_name: serverName,
          },
        } satisfies FlowExecutionUpdate;
      })
      .filter((entry): entry is FlowExecutionUpdate => entry !== null);
    if (synthesizedLogs.length > 0) {
      this.logs = [...this.logs, ...synthesizedLogs].sort(
        (left, right) =>
          new Date(left.timestamp).getTime() -
          new Date(right.timestamp).getTime()
      );
    }
  }

  private getGatewayEventKey(event: FlowGatewayEvent): string {
    const apiUsageId = event.payload?.api_usage_id;
    if (typeof apiUsageId === 'string' && apiUsageId) {
      return apiUsageId;
    }
    return [
      event.execution_id,
      event.timestamp ?? 'no-timestamp',
      event.payload?.upstream_request_id ?? 'no-request-id',
      event.payload?.model_alias ??
        event.payload?.requested_model ??
        'no-model',
    ].join(':');
  }

  private getTotalToolCallCount(): number {
    const persistedCount = Array.isArray(this.execution?.mcp_usage_logs)
      ? this.execution.mcp_usage_logs.length
      : 0;
    return Math.max(
      this.toolCalls,
      persistedCount,
      this.getToolActivityEntries().length
    );
  }

  private normalizeToolName(value: unknown): string | null {
    if (typeof value !== 'string') {
      return null;
    }
    const normalized = value.trim();
    if (!normalized) {
      return null;
    }
    if (
      /^v\d+$/i.test(normalized) ||
      /^(mcp|tool|tools|http|https)$/i.test(normalized) ||
      normalized.startsWith('/') ||
      normalized.includes('://') ||
      normalized.split('/').length > 1
    ) {
      return null;
    }
    return normalized;
  }

  private extractToolNameFromText(value: unknown): string | null {
    if (typeof value !== 'string') {
      return null;
    }
    for (const pattern of [
      /called tool:\s*([a-zA-Z0-9._:-]+)/i,
      /tool:\s*([a-zA-Z0-9._:-]+)/i,
    ]) {
      const match = value.match(pattern);
      if (match && match[1]) {
        return match[1].trim();
      }
    }
    return null;
  }

  private normalizeToolActivityEntry(
    payload: any,
    timestamp: string
  ): ToolActivityEntry | null {
    if (!payload || typeof payload !== 'object') {
      return null;
    }

    const explicitName =
      (typeof payload.tool_name === 'string' && payload.tool_name.trim()) ||
      (typeof payload.tool === 'string' && payload.tool.trim()) ||
      (typeof payload.name === 'string' && payload.name.trim());

    const toolName =
      explicitName ||
      this.extractToolNameFromText(payload.message) ||
      this.extractToolNameFromText(payload.result_summary) ||
      this.extractToolNameFromText(payload.error);

    if (!toolName) {
      return null;
    }

    const serverName =
      typeof payload.server_name === 'string' && payload.server_name.trim()
        ? payload.server_name.trim()
        : 'MCP';
    const detail =
      typeof payload.result_summary === 'string' &&
      payload.result_summary.trim()
        ? payload.result_summary.trim()
        : typeof payload.error === 'string' && payload.error.trim()
          ? payload.error.trim()
          : typeof payload.message === 'string' &&
              payload.message.trim() &&
              !payload.message.includes(toolName)
            ? payload.message.trim()
            : undefined;

    return {
      key: [
        timestamp || 'no-timestamp',
        serverName,
        toolName,
        payload.status || 'unknown',
        detail || '',
        crypto.randomUUID(), // Ensure keys are unique even if the same tool is called at the same second
      ].join(':'),
      timestamp,
      toolName,
      serverName,
      status: typeof payload.status === 'string' ? payload.status : undefined,
      detail,
      payload,
    };
  }

  private getToolActivityEntries(): ToolActivityEntry[] {
    const entries: ToolActivityEntry[] = [];

    const persistedLogs = Array.isArray(this.execution?.mcp_usage_logs)
      ? this.execution.mcp_usage_logs
      : [];
    for (const entry of persistedLogs) {
      const timestamp =
        typeof entry?.timestamp === 'string'
          ? entry.timestamp
          : this.execution?.start_time || new Date().toISOString();
      const normalized = this.normalizeToolActivityEntry(entry, timestamp);
      if (normalized) {
        entries.push(normalized);
      }
    }

    for (const log of this.liveToolActivityEvents) {
      if (log.type !== 'tool_call' && log.type !== 'mcp_call') {
        continue;
      }
      const normalized = this.normalizeToolActivityEntry(
        log.payload,
        log.timestamp
      );
      if (normalized) {
        entries.push(normalized);
      }
    }

    const seen = new Set<string>();
    return entries
      .sort(
        (left, right) =>
          new Date(right.timestamp).getTime() -
          new Date(left.timestamp).getTime()
      )
      .filter((entry) => {
        if (seen.has(entry.key)) {
          return false;
        }
        seen.add(entry.key);
        return true;
      });
  }

  private renderGatewayEvent(event: FlowGatewayEvent) {
    return html`
      <preloop-gateway-event
        .event=${{ ...event }}
        @gateway-event-expand=${(e: CustomEvent) => {
          if (e.detail.expanded) {
            this.handleGatewayEventExpand(event.id);
          }
        }}
      ></preloop-gateway-event>
    `;
  }

  /**
   * Tabs are deep-linkable (`?tab=logs`) and remembered per user, so a
   * reload lands where the operator was reading instead of always on the
   * Timeline.
   */
  private resolveInitialTab(): ExecutionTab {
    const fromUrl = new URLSearchParams(window.location.search).get('tab');
    if (isExecutionTab(fromUrl)) {
      return fromUrl;
    }
    try {
      const remembered = window.localStorage.getItem(TAB_STORAGE_KEY);
      if (isExecutionTab(remembered)) {
        return remembered;
      }
    } catch {
      // Private-mode storage failures must not keep the page from rendering.
    }
    return 'timeline';
  }

  private rememberTab(tab: ExecutionTab) {
    try {
      window.localStorage.setItem(TAB_STORAGE_KEY, tab);
    } catch {
      // Ignore: remembering the tab is a convenience, not a requirement.
    }
    const url = new URL(window.location.href);
    if (url.searchParams.get('tab') !== tab) {
      url.searchParams.set('tab', tab);
      window.history.replaceState({}, '', `${url.pathname}${url.search}`);
    }
  }

  handleTabShow(e: CustomEvent) {
    const name = e.detail?.name;
    if (!isExecutionTab(name)) return;
    this.activeTab = name;
    this.rememberTab(name);
    if (this.isLoadingGatewayEvents) return;
    if (name === 'transcript' && !this.gatewayEventsFullLoaded) {
      // The transcript needs conversation_preview, which the metadata-only
      // fetch strips, so opening it upgrades the events to full payloads once.
      this.loadGatewayEvents(false);
      return;
    }
    if (name === 'timeline' && !this.gatewayEventsLoaded) {
      this.loadGatewayEvents(true);
    }
  }

  /**
   * Pause/resume for the live stream. Pausing is how an operator reads
   * something that would otherwise scroll away; resuming snaps back to the
   * newest item, which is what "follow" means.
   */
  private toggleFollowLive() {
    this.followLive = !this.followLive;
    if (this.followLive) {
      this.scrollTimelineToBottom();
    }
  }

  private jumpToLatest() {
    this.followLive = true;
    this.scrollTimelineToBottom();
  }

  private scrollTimelineToBottom() {
    const stream = this.shadowRoot?.querySelector(
      '.timeline-stream'
    ) as HTMLElement | null;
    if (!stream) return;
    requestAnimationFrame(() => {
      stream.scrollTop = stream.scrollHeight;
      this.atTimelineBottom = true;
    });
  }

  private handleTimelineScroll(event: Event) {
    const stream = event.currentTarget as HTMLElement;
    const distance =
      stream.scrollHeight - stream.scrollTop - stream.clientHeight;
    const atBottom = distance < TIMELINE_FOLLOW_THRESHOLD_PX;
    if (atBottom !== this.atTimelineBottom) {
      this.atTimelineBottom = atBottom;
    }
    // Scrolling away from the newest item is the operator saying "hold
    // still"; the pill then offers the way back.
    if (!atBottom && this.followLive) {
      this.followLive = false;
    }
  }

  private toggleLogGroup(key: string) {
    const next = new Set(this.expandedLogGroups);
    if (next.has(key)) {
      next.delete(key);
    } else {
      next.add(key);
    }
    this.expandedLogGroups = next;
  }

  private handleLogSearchInput(event: Event) {
    this.logSearchQuery = (
      event.target as HTMLInputElement & { value: string }
    ).value;
  }

  private getFilteredLogs(): FlowExecutionUpdate[] {
    const query = this.logSearchQuery.trim().toLowerCase();
    if (!query) return this.logs;
    return this.logs.filter((log) =>
      this.getLogSearchText(log).includes(query)
    );
  }

  private getLogSearchText(log: FlowExecutionUpdate): string {
    const payload = log.payload;
    const text =
      typeof payload === 'string'
        ? payload
        : payload?.content ||
          payload?.message ||
          payload?.line ||
          JSON.stringify(payload || {});
    return `${log.type} ${log.timestamp} ${text}`.toLowerCase();
  }

  /**
   * One chronological stream: gateway requests, tool calls, status changes
   * and the run's own log lines, oldest first. Consecutive log lines fold
   * into a single "N log lines" row so the events an operator is looking
   * for are not buried under container chatter.
   */
  private buildTimelineItems(): TimelineItem[] {
    const items: TimelineItem[] = [];
    const execution = this.execution;

    if (execution) {
      items.push({
        kind: 'status',
        key: 'status-start',
        timestamp: execution.start_time,
        statusLabel: 'Run started',
        statusVariant: 'neutral',
      });
      if (execution.end_time && !this.isExecutionRunning()) {
        items.push({
          kind: 'status',
          key: 'status-end',
          timestamp: execution.end_time,
          statusLabel: executionStatusLabel(execution.status),
          statusVariant: executionStatusVariant(execution.status),
          statusDetail: firstErrorLine(execution.error_message),
        });
      }
    }

    for (const event of this.gatewayEvents) {
      // The gateway-events endpoint returns every log row of the execution,
      // not only the model calls. The non-call rows are the same rows the
      // logs endpoint already gave us, so only model calls earn a card here.
      if (!isModelGatewayCall(event)) continue;
      items.push({
        kind: 'gateway',
        key: `gateway-${this.getGatewayEventKey(event)}`,
        timestamp: event.timestamp || execution?.start_time || '',
        gatewayEvent: event,
      });
    }

    for (const entry of this.getToolActivityEntries()) {
      items.push({
        kind: 'tool',
        key: `tool-${entry.key}`,
        timestamp: entry.timestamp,
        tool: entry,
      });
    }

    this.logs.forEach((log, index) => {
      // Tool calls get their own row above; repeating them as log lines
      // would double every tool in the stream.
      if (log.type === 'tool_call' || log.type === 'mcp_call') return;
      items.push({
        kind: 'log',
        key: `log-${index}`,
        timestamp: log.timestamp,
        log,
      });
    });

    return items
      .map((item, index) => ({ item, index }))
      .sort((left, right) => {
        const delta =
          timelineTime(left.item.timestamp) -
          timelineTime(right.item.timestamp);
        return delta !== 0 ? delta : left.index - right.index;
      })
      .map((entry) => entry.item);
  }

  private getTimelineRows(): TimelineRow[] {
    const rows: TimelineRow[] = [];
    for (const item of this.buildTimelineItems()) {
      if (item.kind === 'log' && item.log) {
        const last = rows[rows.length - 1];
        if (last && last.kind === 'logs') {
          last.logs.push(item.log);
          continue;
        }
        rows.push({
          kind: 'logs',
          key: `logs-${item.key}`,
          timestamp: item.timestamp,
          logs: [item.log],
        });
        continue;
      }
      rows.push({
        kind: 'event',
        key: item.key,
        timestamp: item.timestamp,
        item,
      });
    }
    return rows;
  }

  private renderTimelineTime(timestamp: string) {
    if (!timestamp) return html`<span class="timeline-time">--:--:--</span>`;
    return html`<span
      class="timeline-time"
      title=${formatUTCDateTime(timestamp)}
      >${formatLocalTime(timestamp)}</span
    >`;
  }

  private renderTimelineRow(row: TimelineRow) {
    if (row.kind === 'logs') {
      const expanded = this.expandedLogGroups.has(row.key);
      const count = row.logs.length;
      return html`
        <div class="timeline-row timeline-logs">
          ${this.renderTimelineTime(row.timestamp)}
          <div class="timeline-body">
            <button
              class="log-group-toggle"
              type="button"
              aria-expanded=${expanded ? 'true' : 'false'}
              @click=${() => this.toggleLogGroup(row.key)}
            >
              <sl-icon
                name=${expanded ? 'chevron-down' : 'chevron-right'}
              ></sl-icon>
              ${count} log line${count === 1 ? '' : 's'}
            </button>
            ${
              expanded
                ? html`<div class="log-group-lines">
                    ${row.logs.map((log) => this.renderLogEntry(log))}
                  </div>`
                : ''
            }
          </div>
        </div>
      `;
    }

    const item = row.item;
    if (item.kind === 'gateway' && item.gatewayEvent) {
      return html`
        <div class="timeline-row timeline-gateway">
          ${this.renderTimelineTime(row.timestamp)}
          <div class="timeline-body">
            ${this.renderGatewayEvent(item.gatewayEvent)}
          </div>
        </div>
      `;
    }

    if (item.kind === 'tool' && item.tool) {
      const tool = item.tool;
      const failed = tool.status === 'error' || tool.status === 'failed';
      return html`
        <div class="timeline-row timeline-tool">
          ${this.renderTimelineTime(row.timestamp)}
          <div class="timeline-body">
            <sl-details class="timeline-details">
              <div slot="summary" class="timeline-summary">
                <span class="timeline-title">${tool.toolName}</span>
                <span class="timeline-meta">${tool.serverName}</span>
                ${
                  tool.status
                    ? html`<sl-badge
                        class="chip"
                        pill
                        variant=${failed ? 'danger' : 'success'}
                        >${executionStatusLabel(tool.status)}</sl-badge
                      >`
                    : ''
                }
              </div>
              ${
                tool.detail
                  ? html`<div class="timeline-meta timeline-detail">
                      ${tool.detail}
                    </div>`
                  : ''
              }
              <json-tree .data=${tool.payload}></json-tree>
            </sl-details>
          </div>
        </div>
      `;
    }

    return html`
      <div class="timeline-row timeline-status">
        ${this.renderTimelineTime(row.timestamp)}
        <div class="timeline-body">
          <span class="timeline-title">${item.statusLabel}</span>
          ${
            item.statusDetail
              ? html`<span class="timeline-meta">${item.statusDetail}</span>`
              : ''
          }
        </div>
      </div>
    `;
  }

  private renderTimelinePanel(running: boolean) {
    const rows = this.getTimelineRows();
    return html`
      <div class="timeline-panel">
        <div class="timeline-toolbar">
          <span class="timeline-count"
            >${rows.length} entr${rows.length === 1 ? 'y' : 'ies'}</span
          >
          ${
            running
              ? html`
                  <sl-button
                    size="small"
                    variant="text"
                    class="follow-pill"
                    data-testid="follow-live"
                    @click=${this.toggleFollowLive}
                  >
                    <sl-icon
                      slot="prefix"
                      name=${this.followLive ? 'pause-fill' : 'play-fill'}
                    ></sl-icon>
                    ${this.followLive ? 'Following live' : 'Paused'}
                  </sl-button>
                `
              : ''
          }
        </div>
        <div
          class="timeline-stream ${running ? 'is-live' : ''}"
          data-testid="timeline-stream"
          @scroll=${this.handleTimelineScroll}
        >
          ${
            rows.length === 0
              ? html`<div class="panel-empty">
                  Nothing recorded for this run yet.
                </div>`
              : rows.map((row) => this.renderTimelineRow(row))
          }
        </div>
        ${
          running && !this.atTimelineBottom
            ? html`<sl-button
                class="jump-latest"
                data-testid="jump-latest"
                size="small"
                pill
                @click=${this.jumpToLatest}
                >Jump to latest</sl-button
              >`
            : ''
        }
      </div>
    `;
  }

  private renderOutputPanel(execution: FlowExecution) {
    const hasAnything =
      execution.error_message ||
      execution.result ||
      execution.model_output_summary ||
      (execution.actions_taken_summary || []).length > 0;

    if (!hasAnything) {
      return html`<div class="panel-empty">
        This run reported no result, summary or error.
      </div>`;
    }

    return html`
      <div class="output-panel">
        ${
          execution.error_message
            ? html`
                <section class="output-section">
                  <h2 class="section-title">Error</h2>
                  <pre class="error-block">${execution.error_message}</pre>
                </section>
              `
            : ''
        }
        ${
          execution.result
            ? html`
                <section class="output-section">
                  <h2 class="section-title">Result</h2>
                  <json-tree .data=${execution.result}></json-tree>
                </section>
              `
            : ''
        }
        ${
          execution.model_output_summary
            ? html`
                <section class="output-section">
                  <h2 class="section-title">
                    Summary
                    <sl-copy-button
                      value=${execution.model_output_summary}
                      copy-label="Copy summary"
                    ></sl-copy-button>
                  </h2>
                  <!-- Agents often report their whole stdout here, so it is
                       bounded and monospaced instead of running the page. -->
                  <pre class="output-summary" data-testid="output-summary">
${execution.model_output_summary}</pre>
                </section>
              `
            : ''
        }
        ${
          (execution.actions_taken_summary || []).length > 0
            ? html`
                <section class="output-section">
                  <h2 class="section-title">Actions taken</h2>
                  <json-tree
                    .data=${execution.actions_taken_summary}
                  ></json-tree>
                </section>
              `
            : ''
        }
      </div>
    `;
  }

  private renderTranscriptPanel(running: boolean) {
    return html`
      <div class="transcript-panel">
        <session-chat-view
          .events=${this.gatewayEvents}
          .loading=${
            // While the events are being upgraded to full payloads there is
            // no conversation to build yet, and claiming "nothing captured"
            // in that gap would be a lie.
            this.isLoadingGatewayEvents && !this.gatewayEventsFullLoaded
          }
          .followLive=${running && this.followLive}
          ?scrollable=${running}
          emptyText="No model conversation was captured for this run."
        ></session-chat-view>
      </div>
    `;
  }

  private renderLogsPanel(running: boolean) {
    const filtered = this.getFilteredLogs();
    const query = this.logSearchQuery.trim();
    return html`
      <div class="logs-panel">
        <div class="logs-toolbar">
          <sl-input
            class="log-search"
            size="small"
            clearable
            placeholder="Search log lines"
            .value=${this.logSearchQuery}
            @sl-input=${this.handleLogSearchInput}
          >
            <sl-icon slot="prefix" name="search"></sl-icon>
          </sl-input>
          <div class="logs-toolbar-meta">
            <span class="timeline-count">
              ${
                query
                  ? `${filtered.length} of ${this.logs.length} lines`
                  : `${this.logs.length} line${this.logs.length === 1 ? '' : 's'}`
              }
            </span>
            ${
              this.logs.length > 0
                ? html`<sl-button
                    size="small"
                    variant="text"
                    @click=${this.copyAllLogs}
                  >
                    <sl-icon slot="prefix" name="clipboard"></sl-icon>
                    Copy logs
                  </sl-button>`
                : ''
            }
            ${
              running
                ? html`<sl-button
                    size="small"
                    variant="text"
                    data-testid="follow-logs"
                    @click=${() => (this.isAutoScroll = !this.isAutoScroll)}
                  >
                    <sl-icon
                      slot="prefix"
                      name=${this.isAutoScroll ? 'pause-fill' : 'play-fill'}
                    ></sl-icon>
                    ${this.isAutoScroll ? 'Following live' : 'Paused'}
                  </sl-button>`
                : ''
            }
          </div>
        </div>
        <div class="log-container">
          ${
            this.hasMoreLogs && !query
              ? html`
                  <div class="load-previous">
                    <sl-button
                      size="small"
                      variant="default"
                      ?loading=${this.isFetchingMoreLogs}
                      @click=${this.loadPreviousLogs}
                    >
                      Load previous logs
                    </sl-button>
                  </div>
                `
              : ''
          }
          ${
            filtered.length === 0
              ? html`<div class="empty-logs">
                  <p>
                    ${
                      query
                        ? `No log line matches "${query}".`
                        : 'Waiting for logs...'
                    }
                  </p>
                </div>`
              : filtered.map((log) => this.renderLogEntry(log))
          }
        </div>
      </div>
    `;
  }

  private renderInputPanel(execution: FlowExecution) {
    if (!execution.trigger_event_details && !execution.resolved_input_prompt) {
      return html`<div class="panel-empty">
        No trigger payload or resolved prompt was stored for this run.
      </div>`;
    }
    return html`
      <div class="input-panel">
        ${
          execution.trigger_event_details
            ? html`
                <section class="output-section">
                  <h2 class="section-title">Trigger event</h2>
                  <json-tree
                    .data=${execution.trigger_event_details}
                  ></json-tree>
                </section>
              `
            : ''
        }
        ${
          execution.resolved_input_prompt
            ? html`
                <section class="output-section">
                  <h2 class="section-title">
                    Resolved prompt
                    <sl-copy-button
                      value=${execution.resolved_input_prompt}
                    ></sl-copy-button>
                  </h2>
                  <pre class="prompt-block">
${execution.resolved_input_prompt}</pre>
                </section>
              `
            : ''
        }
      </div>
    `;
  }

  /**
   * Which model ran this execution.
   *
   * The API projects the answer from gateway usage, but a run whose usage
   * rows have aged out still has its gateway events on this page, so fall
   * back to counting those rather than showing a dash next to a page full
   * of model calls.
   */
  private getExecutionModelSource(
    execution: FlowExecution
  ): ExecutionModelSource {
    if (execution.model_alias || (execution.models_used || []).length > 0) {
      return execution;
    }

    const counts = new Map<string, ExecutionModelUsage>();
    for (const event of this.gatewayEvents) {
      const alias = event.payload?.model_alias;
      if (!alias) continue;
      const existing = counts.get(alias);
      if (existing) {
        existing.request_count = (existing.request_count || 0) + 1;
        continue;
      }
      counts.set(alias, {
        model_alias: alias,
        provider_name: event.payload?.provider_name || null,
        request_count: 1,
      });
    }

    const models = Array.from(counts.values()).sort(
      (left, right) => (right.request_count || 0) - (left.request_count || 0)
    );
    return {
      model_alias: models[0]?.model_alias || null,
      provider_name: models[0]?.provider_name || null,
      models_used: models,
    };
  }

  /**
   * One hairline row instead of five cards: what ran, how long, on which
   * model, what it cost and where to find the session. Values are the
   * loudest thing in the row; the labels stay in the meta register.
   */
  private renderSummaryStrip(execution: FlowExecution) {
    const toolEntries = this.getToolActivityEntries();
    const failedTools = toolEntries.filter(
      (entry) => entry.status === 'error' || entry.status === 'failed'
    ).length;
    const toolCount = this.getTotalToolCallCount();
    const sessionReference = execution.agent_session_reference;

    const costText = this.hasPricing
      ? formatEstimatedCost(this.budgetUsed)
      : this.totalTokens > 0
        ? 'Not priced'
        : '—';

    return html`
      <div class="summary-strip" data-testid="summary-strip">
        <div class="strip-item">
          <span class="strip-label">Started</span>
          <span
            class="strip-value"
            data-testid="strip-started"
            title=${formatUTCDateTime(execution.start_time)}
            >${formatRelativeTime(execution.start_time, this.durationNow)}</span
          >
        </div>
        <div class="strip-item">
          <span class="strip-label">Duration</span>
          <span class="strip-value" data-testid="strip-duration"
            >${this.renderDurationText()}</span
          >
        </div>
        <div class="strip-item">
          <span class="strip-label">Model</span>
          <span class="strip-value" data-testid="strip-model"
            >${renderExecutionModel(this.getExecutionModelSource(execution))}</span
          >
        </div>
        <div class="strip-item">
          <span class="strip-label">Tokens</span>
          <span class="strip-value" data-testid="strip-tokens"
            >${
              this.totalTokens > 0 ? this.totalTokens.toLocaleString() : '—'
            }</span
          >
        </div>
        <div class="strip-item">
          <span class="strip-label">$ est.</span>
          <span class="strip-value" data-testid="strip-cost">${costText}</span>
        </div>
        <div class="strip-item">
          <span class="strip-label">Tools</span>
          <span class="strip-value" data-testid="strip-tools">
            ${toolCount.toLocaleString()}${
              failedTools > 0
                ? html`<span class="strip-note">(${failedTools} failed)</span>`
                : ''
            }
          </span>
        </div>
        <div class="strip-item">
          <span class="strip-label">Agent</span>
          <span class="strip-value">${this.flow?.agent_type || '—'}</span>
        </div>
        <div class="strip-item">
          <span class="strip-label">Session</span>
          <span class="strip-value">
            ${
              sessionReference
                ? html`<a
                    class="strip-link"
                    href="/console/runtime-sessions?query=${encodeURIComponent(
                      execution.id
                    )}"
                    title=${sessionReference}
                    >${shortenIdentifier(sessionReference)}</a
                  >`
                : '—'
            }
          </span>
        </div>
        <div class="strip-item">
          <span class="strip-label">Execution</span>
          <span class="strip-value">
            <code class="strip-code" title=${execution.id}
              >${shortenIdentifier(execution.id)}</code
            >
            <sl-copy-button
              value=${execution.id}
              copy-label="Copy execution id"
            ></sl-copy-button>
          </span>
        </div>
      </div>
    `;
  }

  private renderHeaderActions(execution: FlowExecution, running: boolean) {
    const sessionReference = execution.agent_session_reference;
    return html`
      <div slot="main-column" class="header-actions">
        ${
          running
            ? html`
                <sl-button
                  size="small"
                  variant="danger"
                  outline
                  @click=${this.stopExecution}
                >
                  <sl-icon slot="prefix" name="x-circle"></sl-icon> Cancel
                </sl-button>
              `
            : ''
        }
        ${
          this.canRetry()
            ? html`
                <sl-button
                  size="small"
                  variant="default"
                  ?loading=${this.isRetrying}
                  @click=${this.retryExecution}
                >
                  <sl-icon slot="prefix" name="arrow-repeat"></sl-icon> Retry
                </sl-button>
              `
            : ''
        }
        ${
          sessionReference
            ? html`
                <sl-button
                  class="secondary-action"
                  size="small"
                  variant="default"
                  href="/console/runtime-sessions?query=${encodeURIComponent(
                    execution.id
                  )}"
                >
                  <sl-icon slot="prefix" name="chat-left-text"></sl-icon> Open
                  session
                </sl-button>
              `
            : ''
        }
        ${
          this.flow
            ? html`
                <sl-button
                  class="secondary-action"
                  size="small"
                  variant="default"
                  href="/console/flows/${this.flow.id}"
                >
                  <sl-icon slot="prefix" name="diagram-3"></sl-icon> View flow
                </sl-button>
              `
            : ''
        }
        <sl-dropdown hoist>
          <sl-icon-button
            slot="trigger"
            name="three-dots-vertical"
            label="More actions"
          ></sl-icon-button>
          <sl-menu>
            ${
              // On a phone the header has room for the primary action only,
              // so the two navigations move into the menu rather than
              // stacking a column of buttons next to the title.
              sessionReference
                ? html`<sl-menu-item
                    class="narrow-action"
                    @click=${() =>
                      Router.go(
                        `/console/runtime-sessions?query=${encodeURIComponent(
                          execution.id
                        )}`
                      )}
                  >
                    Open session
                  </sl-menu-item>`
                : ''
            }
            ${
              this.flow
                ? html`<sl-menu-item
                    class="narrow-action"
                    @click=${() =>
                      this.flow && Router.go(`/console/flows/${this.flow.id}`)}
                  >
                    View flow
                  </sl-menu-item>`
                : ''
            }
            <sl-menu-item
              @click=${() =>
                this.copyToClipboard(execution.id, 'Execution id copied')}
            >
              Copy execution id
            </sl-menu-item>
            <sl-menu-item
              ?disabled=${!sessionReference}
              @click=${() =>
                sessionReference &&
                this.copyToClipboard(sessionReference, 'Session id copied')}
            >
              Copy session id
            </sl-menu-item>
          </sl-menu>
        </sl-dropdown>
      </div>
    `;
  }

  private async copyToClipboard(value: string, message: string) {
    try {
      await navigator.clipboard.writeText(value);
      this.dispatchEvent(
        new CustomEvent('show-toast', {
          bubbles: true,
          composed: true,
          detail: { message },
        })
      );
    } catch (error) {
      console.error('Failed to copy to clipboard:', error);
    }
  }

  render() {
    // Waiting for router to set executionId
    if (!this.executionId) {
      return html`
        <view-header headerText="Flow execution" width="wide"></view-header>
        <div class="page-loading">
          <sl-spinner style="font-size: 3rem;"></sl-spinner>
          <p>Loading...</p>
        </div>
      `;
    }

    if (this.isLoading) {
      return html`
        <view-header headerText="Flow execution" width="wide"></view-header>
        <div class="page-loading">
          <sl-spinner style="font-size: 3rem;"></sl-spinner>
          <p>Loading execution details...</p>
        </div>
      `;
    }

    if (this.loadingError || !this.execution) {
      return html`
        <view-header headerText="Flow execution" width="wide">
          <div slot="top" class="back-row">
            <sl-button
              variant="text"
              size="small"
              href="/console/flows/executions"
            >
              <sl-icon slot="prefix" name="arrow-left"></sl-icon> All executions
            </sl-button>
          </div>
        </view-header>
        <div class="column-layout wide">
          <div class="main-column">
            <sl-alert variant="danger" open>
              <sl-icon slot="icon" name="exclamation-triangle"></sl-icon>
              <strong>Could not load this execution</strong><br />
              ${this.loadingError || 'Execution not found'}
            </sl-alert>
          </div>
        </div>
      `;
    }

    const execution = this.execution;
    const running = this.isExecutionRunning();
    const statusVariant = executionStatusVariant(execution.status);
    const errorLine = firstErrorLine(execution.error_message);

    return html`
      <view-header
        headerText=${this.flow?.name || 'Flow execution'}
        width="wide"
      >
        <div slot="top" class="back-row">
          <sl-button
            variant="text"
            size="small"
            href="/console/flows/executions"
          >
            <sl-icon slot="prefix" name="arrow-left"></sl-icon> All executions
          </sl-button>
        </div>
        <div slot="title-prefix" class="status-pill">
          ${running ? html`<span class="status-dot"></span>` : ''}
          <sl-badge
            class="chip ${statusVariant === 'danger' ? 'solid' : ''}"
            pill
            variant=${statusVariant}
            >${executionStatusLabel(execution.status)}</sl-badge
          >
        </div>
        <!-- The title is the flow name, which every run of it shares. What
             this particular run was about goes directly under it, linked to
             the pull request or issue it came from. -->
        ${
          !isSubjectFallback(execution)
            ? html`<div slot="description" class="execution-subject-line">
                ${renderExecutionSubject(execution)}
              </div>`
            : ''
        }
        ${this.renderHeaderActions(execution, running)}
      </view-header>
      <div class="column-layout wide">
        <div class="main-column">
          ${this.renderSummaryStrip(execution)}
          ${
            errorLine
              ? html`<div
                  class="error-line"
                  data-testid="error-line"
                  title=${execution.error_message || ''}
                >
                  <sl-icon name="exclamation-triangle"></sl-icon>
                  <span>${errorLine}</span>
                </div>`
              : ''
          }
          <sl-tab-group
            class="execution-tabs"
            @sl-tab-show=${this.handleTabShow}
          >
            <sl-tab
              slot="nav"
              panel="timeline"
              ?active=${this.activeTab === 'timeline'}
              >Timeline</sl-tab
            >
            <sl-tab
              slot="nav"
              panel="output"
              ?active=${this.activeTab === 'output'}
              >Output</sl-tab
            >
            <sl-tab
              slot="nav"
              panel="transcript"
              ?active=${this.activeTab === 'transcript'}
              >Transcript</sl-tab
            >
            <sl-tab slot="nav" panel="logs" ?active=${this.activeTab === 'logs'}
              >Logs</sl-tab
            >
            <sl-tab
              slot="nav"
              panel="input"
              ?active=${this.activeTab === 'input'}
              >Input</sl-tab
            >

            <sl-tab-panel
              name="timeline"
              ?active=${this.activeTab === 'timeline'}
              >${this.renderTimelinePanel(running)}</sl-tab-panel
            >
            <sl-tab-panel name="output" ?active=${this.activeTab === 'output'}
              >${this.renderOutputPanel(execution)}</sl-tab-panel
            >
            <sl-tab-panel
              name="transcript"
              ?active=${this.activeTab === 'transcript'}
              >${this.renderTranscriptPanel(running)}</sl-tab-panel
            >
            <sl-tab-panel name="logs" ?active=${this.activeTab === 'logs'}
              >${this.renderLogsPanel(running)}</sl-tab-panel
            >
            <sl-tab-panel name="input" ?active=${this.activeTab === 'input'}
              >${this.renderInputPanel(execution)}</sl-tab-panel
            >
          </sl-tab-group>
        </div>
      </div>
    `;
  }

  renderLogEntry(log: FlowExecutionUpdate) {
    const time = formatLocalTime(log.timestamp);

    // For model output (summary), show as a highlighted section
    if (log.type === 'model_output') {
      return html`
        <div class="log-entry log-metadata" style="border-left-color: #b5cea8;">
          <span class="log-timestamp">${time}</span>
          <span class="log-type log-type-success">[Summary]</span>
          <div class="log-content" style="margin-top: 8px;">
            <pre
              style="white-space: pre-wrap; word-wrap: break-word; margin: 0; color: #b5cea8;"
            >
${log.payload.content}</pre>
          </div>
        </div>
      `;
    }

    // For log lines, show timestamp + content
    if (log.type === 'agent_log_line') {
      const content =
        log.payload.line || log.payload.message || log.payload.content || '';
      const stream = log.payload.stream || 'stdout';
      const streamClass = stream === 'stderr' ? 'log-stderr' : '';

      // Check for Kubernetes status messages (pod initializing, etc.)
      // These are JSON objects with "kind":"Status" - display a friendly message instead
      if (content.startsWith('{"kind":"Status"')) {
        try {
          const statusObj = JSON.parse(content);
          // Show a friendly message for pod initialization
          if (
            statusObj.reason === 'BadRequest' &&
            statusObj.message?.includes('PodInitializing')
          ) {
            return html`
              <div
                class="log-entry log-metadata"
                style="border-left-color: #dcdcaa;"
              >
                <span class="log-timestamp">${time}</span>
                <span class="log-type log-type-warning">[Initializing]</span>
                <span>Container is starting up, please wait...</span>
              </div>
            `;
          }
          // For other status messages, show a condensed version
          if (statusObj.message) {
            return html`
              <div
                class="log-entry log-metadata"
                style="border-left-color: #858585;"
              >
                <span class="log-timestamp">${time}</span>
                <span class="log-type">[K8s Status]</span>
                <span>${statusObj.message}</span>
              </div>
            `;
          }
        } catch {
          // Not valid JSON, fall through to regular display
        }
      }

      return html`
        <div class="log-entry ${streamClass}">
          <span class="log-timestamp">${time}</span>
          <span class="log-content"
            >${unsafeHTML(
              DOMPurify.sanitize(ansiConverter.ansi_to_html(content))
            )}</span
          >
        </div>
      `;
    }

    // For metadata/status updates, show with different styling
    const typeClass = this.getLogTypeClass(log.type);
    const message = this.formatMetadataMessage(log);

    return html`
      <div class="log-entry log-metadata">
        <span class="log-timestamp">${time}</span>
        <span class="log-type ${typeClass}"
          >[${this.formatLogType(log.type)}]</span
        >
        <span>${message}</span>
      </div>
    `;
  }

  formatLogType(type: string): string {
    // Convert snake_case to Title Case
    return type
      .split('_')
      .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
      .join(' ');
  }

  formatMetadataMessage(log: FlowExecutionUpdate): string {
    if (typeof log.payload === 'string') {
      return log.payload;
    }

    switch (log.type) {
      case 'status_update':
        return `Status: ${log.payload.status}`;
      case 'connected':
        return log.payload.message || 'Connected to execution stream';
      case 'agent_started':
        return 'Agent session started';
      case 'agent_stopped':
        return 'Agent session stopped';
      case 'tool_call':
      case 'mcp_call':
        return `Called tool: ${
          this.normalizeToolActivityEntry(log.payload, log.timestamp)
            ?.toolName || 'structured MCP call'
        }`;
      case 'budget_update':
        return `Budget used: $${log.payload.budget_used?.toFixed(2) || '0.00'}`;
      default:
        return log.payload.message || JSON.stringify(log.payload);
    }
  }

  getLogTypeClass(type: string): string {
    if (type.includes('error') || type.includes('fail')) {
      return 'log-type-error';
    }
    if (type.includes('success') || type.includes('complete')) {
      return 'log-type-success';
    }
    if (type.includes('warning') || type.includes('warn')) {
      return 'log-type-warning';
    }
    return '';
  }

  async stopExecution() {
    if (!this.executionId) return;

    try {
      // Send stop command to backend (which stops the container directly)
      await sendCommandToExecution(this.executionId, 'stop');

      // Wait a moment for the container to stop
      await new Promise((resolve) => setTimeout(resolve, 500));

      // Refresh execution details to get updated status
      this.execution = await getFlowExecution(this.executionId);

      // Fetch final logs from the stopped container
      try {
        const logsResponse = await getFlowExecutionLogs(this.executionId, {
          tail: 500,
        });
        if (logsResponse.logs && Array.isArray(logsResponse.logs)) {
          this.logs = logsResponse.logs;
          this.hasMoreLogs = !!logsResponse.has_more;
          console.log(
            `Loaded ${logsResponse.logs.length} logs after stop from ${logsResponse.source}`
          );
        }
      } catch (error) {
        console.error('Failed to fetch logs after stop:', error);
      }

      // Stop auto-scroll checker and flush remaining buffer
      this.stopAutoScrollChecker();
      this.stopBufferFlush();
      this.isAutoScroll = false;

      // Force UI update
      this.requestUpdate();
    } catch (error) {
      console.error('Failed to stop execution:', error);
      // TODO: Show error notification to user
    }
  }

  canRetry(): boolean {
    if (!this.execution) return false;
    const retryableStatuses = ['FAILED', 'STOPPED', 'TIMEOUT', 'CANCELLED'];
    return retryableStatuses.includes(this.execution.status);
  }

  async retryExecution() {
    if (!this.executionId) return;

    try {
      this.isRetrying = true;
      const result = await retryFlowExecution(this.executionId);

      // Navigate to the new execution
      // Backend returns { id, status, flow_id }
      if (result.id) {
        window.location.href = `/console/flows/executions/${result.id}`;
      }
    } catch (error) {
      console.error('Failed to retry execution:', error);
      // Show error message
      const message =
        error instanceof Error ? error.message : 'Failed to retry execution';
      this.dispatchEvent(
        new CustomEvent('show-toast', {
          bubbles: true,
          composed: true,
          detail: { message, variant: 'danger' },
        })
      );
    } finally {
      this.isRetrying = false;
    }
  }

  /** Kept for callers outside the render path; the shared helper decides. */
  getStatusVariant(status: string) {
    return executionStatusVariant(status);
  }
}
