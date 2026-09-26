import { LitElement, html, css, unsafeCSS, nothing } from 'lit';
import { customElement, state } from 'lit/decorators.js';

import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/card/card.js';
import '@shoelace-style/shoelace/dist/components/details/details.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/option/option.js';
import '@shoelace-style/shoelace/dist/components/select/select.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import '../../components/view-header.ts';
import '../../components/legal-hold-control';
import '../../components/json-tree.ts';
import '../../components/list-toolbar.ts';
import '../../components/preloop-session-observer.ts';
import '../../components/token-figures.ts';
import {
  getAccountRuntimeSessionDetail,
  getAccountRuntimeSessions,
  getEntitlements,
  getFeatures,
  getFlowExecutionGatewayEvents,
  getRuntimeSessionGatewayEvents,
  getAccountRuntimeSessionActivityTimeline,
  getAccountRuntimeSessionInteractions,
  searchRuntimeSessions,
  updateAccountRuntimeSession,
  type RuntimeSessionDetailParams,
  type RuntimeSessionInteractionsParams,
  type RuntimeSessionListParams,
} from '../../api';
import type {
  AccountGatewayUsageSearchResponse,
  AccountRuntimeSessionDetailResponse,
  FlowGatewayConversationPreviewMessage,
  FlowGatewayEvent,
  FlowGatewayEventPayload,
  AccountRuntimeSessionListResponse,
  GatewayUsageByModel,
  GatewayUsageSearchResultItem,
  RuntimeSessionActivityItem,
  RuntimeSessionSummary,
  SessionSearchResponse,
  SessionSearchResult,
  SessionSearchSnippet,
} from '../../types';
import consoleStyles from '../../styles/console-styles.css?inline';
import { unifiedWebSocketManager } from '../../services/unified-websocket-manager';

type DateRangePreset = 'last-7' | 'last-30' | 'last-90' | 'all' | 'custom';

/**
 * Keystrokes settle for this long before a search goes out. The query is a
 * server round trip over the whole corpus, so one request per character would
 * be one wasted search per character.
 */
const SEARCH_DEBOUNCE_MS = 400;

/**
 * Snippets shown per matching session. Tunable: a handful of lines is enough
 * to tell a hit from a near miss, and more turns the result list into a
 * transcript nobody asked to read.
 */
const SNIPPETS_PER_SESSION = 3;

/** Matching sessions requested per search. */
const SEARCH_RESULT_LIMIT = 25;

/**
 * Readable names for the corpus source kinds, so a snippet says why it
 * matched rather than showing the column value.
 */
const MATCH_TAG_LABELS: Record<string, string> = {
  gateway_interaction: 'Model call',
  transcript_message: 'Transcript',
  tool_call: 'Tool call',
  operator_note: 'Operator note',
  session_summary: 'Session summary',
  flow_log: 'Flow log',
};

/**
 * Corpus kinds whose source_id names a turn the transcript can scroll to.
 *
 * Gateway interactions store the api usage id, tool calls and transcript
 * messages store the activity row id, and the transcript keys turns by those
 * same ids. Session summaries, operator notes and flow logs name something
 * else, so a click opens the session rather than a turn.
 */
const TURN_JUMP_KINDS = new Set([
  'gateway_interaction',
  'tool_call',
  'transcript_message',
]);

@customElement('runtime-sessions-view')
export class RuntimeSessionsView extends LitElement {
  @state()
  private sessions: AccountRuntimeSessionListResponse | null = null;

  @state()
  private detail: AccountRuntimeSessionDetailResponse | null = null;

  @state()
  private loading = true;

  @state()
  private detailLoading = false;

  @state()
  private error: string | null = null;

  @state()
  private selectedSessionId: string | null = null;

  @state()
  private interactions: AccountGatewayUsageSearchResponse | null = null;

  @state()
  private activityTimeline: RuntimeSessionActivityItem[] | null = null;

  @state()
  private interactionsLoading = false;

  @state()
  private activityTimelineLoading = false;

  @state()
  private selectedRange: DateRangePreset = 'last-30';

  @state()
  private startDate = '';

  @state()
  private endDate = '';

  @state()
  private searchQuery = '';

  // Content search state. A non empty query puts the page in search mode: the
  // box asks the ranked search endpoint about what agents said and did, not
  // the list endpoint about identifier columns.
  @state()
  private searchResults: SessionSearchResponse | null = null;

  @state()
  private searchLoading = false;

  @state()
  private searchError: string | null = null;

  /**
   * The turn the transcript should open at, as the identifier the corpus
   * publishes for a matching turn. Mirrored into the location so the link is
   * shareable and survives a reload.
   */
  @state()
  private focusTurnId: string | null = null;

  @state()
  private sessionSourceType = 'all';

  @state()
  private status = 'all';

  @state()
  private interactionQuery = '';

  @state()
  private gatewaySearchQuery = '';

  @state()
  private actionLoading = false;

  @state()
  private gatewayEvents: FlowGatewayEvent[] = [];

  @state()
  private gatewayEventsLoading = false;

  @state()
  private gatewayEventsError: string | null = null;

  @state()
  private featureFlags: Record<string, boolean | string[]> = {};

  private initialized = false;
  private unsubscribeRealtime?: () => void;
  private refreshTimer: number | null = null;
  private searchDebounce: number | null = null;
  private loadSequence = 0;
  private searchSequence = 0;
  private onPopState = () => this.applyLocation();

  static styles = [
    unsafeCSS(consoleStyles),
    css`
      :host {
        display: block;
      }

      .page {
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-large);
      }

      /* The collection bar: the same inline row the Agents and Flows lists
         use, so the filters sit on the page instead of inside a card of
         their own. Slotted content lives in this view's tree, so these
         rules reach the selects and the Apply/Reset pair. */
      list-toolbar sl-select,
      list-toolbar sl-input[type='date'] {
        min-width: 180px;
      }

      .filter-actions {
        display: flex;
        gap: var(--sl-spacing-small);
        align-items: end;
      }

      .layout {
        display: grid;
        grid-template-columns: minmax(320px, 380px) minmax(0, 1fr);
        gap: var(--sl-spacing-large);
      }

      .session-list {
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-small);
      }

      .titles-upsell-hint {
        display: flex;
        align-items: center;
        gap: var(--sl-spacing-x-small);
        width: 100%;
        margin-bottom: var(--sl-spacing-small);
        padding: var(--sl-spacing-x-small) var(--sl-spacing-small);
        border: 1px dashed var(--sl-color-neutral-300);
        border-radius: var(--sl-border-radius-medium);
        background: transparent;
        color: var(--sl-color-neutral-600);
        font-size: var(--sl-font-size-x-small);
        text-align: left;
        cursor: pointer;
      }

      .titles-upsell-hint:hover {
        border-color: var(--sl-color-primary-400);
        color: var(--sl-color-primary-600);
      }

      .session-item {
        border: 1px solid var(--sl-color-neutral-200);
        border-radius: var(--sl-border-radius-medium);
        padding: var(--sl-spacing-medium);
        background: var(--sl-color-neutral-0);
        cursor: pointer;
      }

      .session-item.selected {
        border-color: var(--sl-color-primary-500);
        box-shadow: 0 0 0 1px var(--sl-color-primary-300);
      }

      .session-item-title {
        font-weight: 600;
        color: var(--sl-color-neutral-900);
        overflow-wrap: anywhere;
      }

      .session-item-meta {
        color: var(--sl-color-neutral-600);
        font-size: var(--sl-font-size-small);
        margin-top: var(--sl-spacing-2x-small);
        overflow-wrap: anywhere;
      }

      .detail-stack {
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-large);
      }

      .summary-grid {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
        gap: var(--sl-spacing-medium);
      }

      .summary-card {
        border: 1px solid var(--sl-color-neutral-200);
        border-radius: var(--sl-border-radius-medium);
        padding: var(--sl-spacing-medium);
        background: var(--sl-color-neutral-0);
      }

      .summary-label {
        color: var(--sl-color-neutral-600);
        font-size: var(--sl-font-size-small);
      }

      .summary-value {
        font-size: 1.3rem;
        font-weight: 700;
        color: var(--sl-color-neutral-900);
        margin-top: var(--sl-spacing-2x-small);
      }

      .summary-detail {
        color: var(--sl-color-neutral-600);
        font-size: var(--sl-font-size-small);
        margin-top: var(--sl-spacing-2x-small);
      }

      .breakdown-list,
      .interaction-list {
        display: flex;
        flex-direction: column;
      }

      .breakdown-header,
      .breakdown-row {
        display: grid;
        grid-template-columns: minmax(0, 2fr) 110px 110px 110px;
        gap: var(--sl-spacing-small);
        align-items: center;
        padding: var(--sl-spacing-small) 0;
        border-bottom: 1px solid var(--sl-color-neutral-200);
      }

      .breakdown-header {
        color: var(--sl-color-neutral-600);
        font-size: var(--sl-font-size-x-small);
        text-transform: uppercase;
        letter-spacing: 0.04em;
        font-weight: 600;
      }

      .breakdown-row:last-child,
      .interaction-row:last-child {
        border-bottom: none;
      }

      .cell-numeric {
        text-align: right;
        font-variant-numeric: tabular-nums;
      }

      .interaction-toolbar {
        display: flex;
        gap: var(--sl-spacing-medium);
        align-items: end;
        flex-wrap: wrap;
      }

      .interaction-toolbar sl-input {
        min-width: 260px;
      }

      .search-summary {
        color: var(--sl-color-neutral-600);
        font-size: var(--sl-font-size-small);
        margin: var(--sl-spacing-small) 0;
      }

      .interaction-row {
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-x-small);
        padding: var(--sl-spacing-medium) 0;
        border-bottom: 1px solid var(--sl-color-neutral-200);
      }

      .interaction-header {
        display: flex;
        justify-content: space-between;
        gap: var(--sl-spacing-small);
        align-items: flex-start;
      }

      .interaction-title {
        font-weight: 600;
        color: var(--sl-color-neutral-900);
      }

      .interaction-meta,
      .interaction-excerpt,
      .detail-meta {
        color: var(--sl-color-neutral-600);
        font-size: var(--sl-font-size-small);
        overflow-wrap: anywhere;
      }

      .interaction-excerpt {
        color: var(--sl-color-neutral-800);
      }

      .gateway-events-panel {
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-medium);
      }

      .gateway-event {
        border: 1px solid var(--sl-color-neutral-200);
        border-radius: var(--sl-border-radius-medium);
        background: var(--sl-color-neutral-0);
      }

      .gateway-event::part(summary) {
        padding: var(--sl-spacing-medium);
      }

      .gateway-event::part(content) {
        border-top: 1px solid var(--sl-color-neutral-200);
        padding: var(--sl-spacing-medium);
        background: var(--sl-color-neutral-50);
      }

      .gateway-event-summary,
      .gateway-event-meta {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
        gap: var(--sl-spacing-small);
      }

      .gateway-event-meta {
        margin-bottom: var(--sl-spacing-medium);
      }

      .gateway-event-label {
        color: var(--sl-color-neutral-600);
        font-size: var(--sl-font-size-x-small);
        text-transform: uppercase;
        letter-spacing: 0.04em;
        font-weight: 600;
        margin-bottom: var(--sl-spacing-2x-small);
      }

      .gateway-event-value {
        color: var(--sl-color-neutral-900);
        font-size: var(--sl-font-size-small);
        overflow-wrap: anywhere;
      }

      .gateway-badges {
        display: flex;
        flex-wrap: wrap;
        gap: var(--sl-spacing-2x-small);
      }

      .payload-section-title {
        font-size: var(--sl-font-size-small);
        font-weight: 600;
        color: var(--sl-color-neutral-700);
        margin-bottom: var(--sl-spacing-small);
      }

      .payload-block {
        background: var(--sl-color-neutral-100);
        border: 1px solid var(--sl-color-neutral-200);
        border-radius: var(--sl-border-radius-medium);
        padding: var(--sl-spacing-medium);
        max-height: 320px;
        overflow: auto;
      }

      .payload-block pre {
        margin: 0;
        white-space: pre-wrap;
        word-break: break-word;
        font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', 'Consolas', monospace;
        font-size: 12px;
        line-height: 1.5;
      }

      .conversation-preview-list {
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-small);
        margin-bottom: var(--sl-spacing-medium);
      }

      .conversation-preview-message {
        border: 1px solid var(--sl-color-neutral-200);
        border-radius: var(--sl-border-radius-medium);
        background: var(--sl-color-neutral-100);
        padding: var(--sl-spacing-medium);
      }

      .conversation-preview-header {
        display: flex;
        justify-content: space-between;
        gap: var(--sl-spacing-small);
        flex-wrap: wrap;
        margin-bottom: var(--sl-spacing-small);
      }

      .conversation-preview-title {
        font-weight: 600;
        color: var(--sl-color-neutral-800);
      }

      .conversation-preview-text {
        margin: 0;
        white-space: pre-wrap;
        word-break: break-word;
        font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', 'Consolas', monospace;
        font-size: 12px;
        line-height: 1.5;
        color: var(--sl-color-neutral-900);
      }

      .empty-state,
      .loading-state {
        text-align: center;
        padding: var(--sl-spacing-x-large);
        color: var(--sl-color-neutral-600);
      }

      .empty-state sl-icon,
      .loading-state sl-spinner {
        font-size: 2rem;
        margin-bottom: var(--sl-spacing-small);
      }

      /* Content search results: one block per session, a few snippets each. */
      .search-results {
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-large);
      }

      .search-result-header {
        display: flex;
        flex-wrap: wrap;
        align-items: baseline;
        gap: var(--sl-spacing-small);
      }

      .search-result-meta {
        color: var(--sl-color-neutral-600);
        font-size: var(--sl-font-size-small);
      }

      .snippet-list {
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-x-small);
        margin-top: var(--sl-spacing-x-small);
      }

      .snippet {
        display: block;
        width: 100%;
        text-align: left;
        border: 1px solid var(--sl-color-neutral-200);
        border-radius: var(--sl-border-radius-medium);
        background: var(--sl-color-neutral-0);
        padding: var(--sl-spacing-small);
        cursor: pointer;
        font: inherit;
        color: inherit;
      }

      .snippet:hover,
      .snippet:focus-visible {
        border-color: var(--sl-color-primary-400);
      }

      .snippet-header {
        display: flex;
        flex-wrap: wrap;
        gap: var(--sl-spacing-x-small);
        align-items: center;
        margin-bottom: var(--sl-spacing-2x-small);
        font-size: var(--sl-font-size-small);
        color: var(--sl-color-neutral-600);
      }

      .snippet-text {
        white-space: pre-wrap;
        word-break: break-word;
        font-size: var(--sl-font-size-small);
        line-height: 1.5;
      }

      .snippet-text mark {
        background: var(--sl-color-warning-200);
        color: inherit;
      }

      .snippet-text.muted {
        color: var(--sl-color-neutral-500);
        font-style: italic;
      }

      .snippet-open-hint {
        margin-top: var(--sl-spacing-2x-small);
        font-size: var(--console-text-meta);
        color: var(--console-meta-color);
      }

      .search-notices {
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-small);
      }

      @media (max-width: 1100px) {
        .layout {
          grid-template-columns: 1fr;
        }
      }

      @media (max-width: 720px) {
        .filter-actions {
          margin-left: 0;
          width: 100%;
        }

        .breakdown-header {
          display: none;
        }

        .breakdown-row {
          grid-template-columns: 1fr;
        }

        .cell-numeric {
          text-align: left;
        }
      }
    `,
  ];

  connectedCallback() {
    super.connectedCallback();

    if (!this.initialized) {
      if (this.selectedRange !== 'custom') {
        this.applyPresetDates(this.selectedRange);
      }
      this.initialized = true;
      // Read the location before the first load: a shared link carries the
      // query and the turn it was found in, not only the session.
      this.readLocation();
      void this.loadFeatureFlags();
      void this.loadSessions();
      if (this.isSearching) {
        void this.loadSearchResults();
      }
      this.connectRealtime();
      window.addEventListener('popstate', this.onPopState);
    }
  }

  disconnectedCallback(): void {
    super.disconnectedCallback();
    this.unsubscribeRealtime?.();
    if (this.refreshTimer !== null) {
      window.clearTimeout(this.refreshTimer);
      this.refreshTimer = null;
    }
    this.cancelSearchDebounce();
    window.removeEventListener('popstate', this.onPopState);
  }

  /** Whether the page is answering a content query rather than listing. */
  private get isSearching(): boolean {
    return this.searchQuery.trim() !== '';
  }

  /** Pull the deep linkable state out of the current location. */
  private readLocation(): void {
    const params = new URLSearchParams(window.location.search);
    this.selectedSessionId = params.get('sessionId');
    this.searchQuery = params.get('q') ?? '';
    this.focusTurnId = params.get('turn');
  }

  /**
   * Restore the view the location describes, for the back button.
   *
   * A snippet click pushes a location, so going back has to put the page in
   * the state that location names rather than leaving a url that no longer
   * matches what is on screen.
   */
  private applyLocation(): void {
    const previousQuery = this.searchQuery;
    this.readLocation();
    this.cancelSearchDebounce();
    if (!this.isSearching) {
      this.clearSearchResults();
      // Keep the list that is already on screen; a hard reload would blank
      // the observer for the length of the round trip.
      void this.loadSessions(this.sessions !== null);
      return;
    }
    if (this.searchQuery !== previousQuery || !this.searchResults) {
      void this.loadSearchResults();
    }
  }

  private connectRealtime(): void {
    const scheduleRefresh = () => this.scheduleRefresh();
    const unsubscribers = [
      unifiedWebSocketManager.subscribe('runtime_sessions', scheduleRefresh),
      unifiedWebSocketManager.subscribe('managed_agents', scheduleRefresh),
      unifiedWebSocketManager.subscribe('gateway_activity', (message: any) =>
        this.handleGatewayActivity(message)
      ),
      unifiedWebSocketManager.subscribe('audit', scheduleRefresh),
    ];
    this.unsubscribeRealtime = () => {
      for (const unsubscribe of unsubscribers) {
        unsubscribe();
      }
    };
    void unifiedWebSocketManager.connect();
  }

  private handleGatewayActivity(message: any): void {
    const payload = message?.payload ?? {};
    const sessionId = payload.runtime_session_id;

    if (sessionId === this.selectedSessionId) {
      // Create an optimistic event
      const newEvent = {
        id: message.id || crypto.randomUUID(),
        execution_id: message.execution_id || '',
        timestamp: payload.timestamp || new Date().toISOString(),
        type: message.type,
        payload: {
          ...payload,
          outcome:
            message.type === 'model_gateway_request_started'
              ? 'pending'
              : payload.status_code >= 400
                ? 'error'
                : 'success',
        },
      };

      if (
        this.gatewayEvents &&
        (message.type === 'model_gateway_call' ||
          message.type === 'model_gateway_request_started' ||
          message.type === 'tool_call')
      ) {
        let nextEvents = this.gatewayEvents as any[];
        // Filter out started if completed arrived
        if (message.type !== 'model_gateway_request_started') {
          nextEvents = nextEvents.filter(
            (e) =>
              !(
                e.type === 'model_gateway_request_started' &&
                Math.abs(
                  new Date(e.timestamp || new Date().toISOString()).getTime() -
                    new Date(
                      payload.timestamp || new Date().toISOString()
                    ).getTime()
                ) < 60000
              )
          );
        }
        this.gatewayEvents = [newEvent, ...nextEvents];
      }

      // Update interactions list if visible
      if (message.type === 'model_gateway_call' && this.interactions) {
        const newInteraction = {
          id: message.id || crypto.randomUUID(),
          request: payload.request || {},
          response: payload.response || {},
          error_detail: payload.error_detail,
          timestamp: payload.timestamp || new Date().toISOString(),
          requested_model: payload.requested_model,
          model_alias: payload.model_alias,
          provider_name: payload.provider_name,
          status_code: payload.status_code,
          estimated_cost: payload.estimated_cost,
          total_tokens: payload.total_tokens,
          prompt_tokens: payload.prompt_tokens,
          completion_tokens: payload.completion_tokens,
        } as unknown as GatewayUsageSearchResultItem;
        this.interactions = {
          ...this.interactions,
          items: [newInteraction, ...this.interactions.items],
        };
      }
    }

    // Call scheduleRefresh anyway for non-selected items logic
    this.scheduleRefresh();
  }

  private scheduleRefresh(): void {
    // Live traffic refreshes the list, not a search: re-running a ranked query
    // on every gateway event would reshuffle results under the reader.
    if (this.isSearching) {
      return;
    }
    if (this.refreshTimer !== null) {
      window.clearTimeout(this.refreshTimer);
    }
    this.refreshTimer = window.setTimeout(() => {
      this.refreshTimer = null;
      void this.loadSessions(true);
    }, 250);
  }

  private getLocalDateString(date: Date): string {
    const year = date.getFullYear();
    const month = `${date.getMonth() + 1}`.padStart(2, '0');
    const day = `${date.getDate()}`.padStart(2, '0');
    return `${year}-${month}-${day}`;
  }

  private applyPresetDates(range: Exclude<DateRangePreset, 'custom'>) {
    if (range === 'all') {
      this.startDate = '';
      this.endDate = '';
      return;
    }

    const today = new Date();
    const startDate = new Date(today);
    const days = range === 'last-7' ? 7 : range === 'last-30' ? 30 : 90;
    startDate.setDate(startDate.getDate() - (days - 1));
    this.startDate = this.getLocalDateString(startDate);
    this.endDate = this.getLocalDateString(today);
  }

  /** Start of the requested range as an instant, or null when unbounded. */
  private rangeStartIso(): string | null {
    return this.startDate
      ? new Date(`${this.startDate}T00:00:00`).toISOString()
      : null;
  }

  /** End of the requested range as an instant, or null when unbounded. */
  private rangeEndIso(): string | null {
    return this.endDate
      ? new Date(`${this.endDate}T23:59:59.999`).toISOString()
      : null;
  }

  private buildListParams(): RuntimeSessionListParams {
    const params: RuntimeSessionListParams = {
      limit: 50,
      status: this.status as 'all' | 'active' | 'ended',
    };

    const startDate = this.rangeStartIso();
    const endDate = this.rangeEndIso();
    if (startDate) {
      params.startDate = startDate;
    }
    if (endDate) {
      params.endDate = endDate;
    }
    // The query no longer reaches the list endpoint at all: a typed query is
    // a question about content, and the list filter only ever matched four
    // identifier columns.
    if (this.sessionSourceType !== 'all') {
      params.sessionSourceType = this.sessionSourceType;
    }

    return params;
  }

  /**
   * Run one ranked content search for the current query and filters.
   *
   * Sequenced like the list load: a slow earlier search must never overwrite
   * the answer to what the operator is typing now.
   */
  private async loadSearchResults(): Promise<void> {
    const query = this.searchQuery.trim();
    if (!query) {
      this.clearSearchResults();
      return;
    }

    const seq = ++this.searchSequence;
    this.searchLoading = true;
    this.searchError = null;
    try {
      const results = await searchRuntimeSessions({
        query,
        startDate: this.rangeStartIso() ?? undefined,
        endDate: this.rangeEndIso() ?? undefined,
        limit: SEARCH_RESULT_LIMIT,
        maxSnippetsPerSession: SNIPPETS_PER_SESSION,
      });
      if (seq !== this.searchSequence) return;
      this.searchResults = results;
    } catch (error) {
      if (seq !== this.searchSequence) return;
      console.error('Failed to search session content:', error);
      this.searchResults = null;
      this.searchError =
        error instanceof Error
          ? error.message
          : 'Failed to search session content';
    } finally {
      if (seq === this.searchSequence) {
        this.searchLoading = false;
      }
    }
  }

  private clearSearchResults(): void {
    // A newer sequence number also abandons any search still in flight.
    this.searchSequence += 1;
    this.searchResults = null;
    this.searchError = null;
    this.searchLoading = false;
    this.focusTurnId = null;
  }

  private buildDetailParams(): RuntimeSessionDetailParams {
    return {};
  }

  private buildInteractionsParams(): RuntimeSessionInteractionsParams {
    const params: RuntimeSessionInteractionsParams = {
      interactionLimit: 50,
    };

    if (this.interactionQuery.trim()) {
      params.interactionQuery = this.interactionQuery.trim();
    }

    return params;
  }

  private async loadFeatureFlags() {
    try {
      const features = await getFeatures();
      this.featureFlags = features.features || {};
    } catch {
      this.featureFlags = {};
    }
    try {
      // Passive premium hint only (title upsell); 402s remain the real gate.
      this.isPremium = (await getEntitlements()).premium;
    } catch {
      this.isPremium = true;
    }
  }

  @state() private isPremium = true;

  /** Open the shell upgrade modal for AI session titles (passive list hint). */
  private openTitlesUpgrade(): void {
    window.dispatchEvent(
      new CustomEvent('show-upgrade-modal', {
        detail: { feature: 'session_titles', code: 'upgrade_required' },
        bubbles: true,
        composed: true,
      })
    );
  }

  private async loadSessions(isSoftRefresh = false) {
    const seq = ++this.loadSequence;
    if (!isSoftRefresh) {
      this.loading = true;
      this.error = null;
    }

    try {
      const result = await getAccountRuntimeSessions(this.buildListParams());
      if (seq !== this.loadSequence) return;
      this.sessions = result;
      if (
        // In search mode the selection belongs to the results, which are not
        // this page of the list; the list must not steal it back.
        !this.isSearching &&
        (!this.selectedSessionId ||
          !this.sessions.items.some(
            (item) => item.id === this.selectedSessionId
          ))
      ) {
        this.selectedSessionId = this.sessions.items[0]?.id ?? null;
        this.syncUrl();
      }
      // Paint the session list immediately. Detail/activity/events are owned by
      // <preloop-session-observer> and load after selection, do not block the
      // list on getAccountRuntimeSessionDetail.
    } catch (error) {
      if (seq !== this.loadSequence) return;
      console.error('Failed to load sessions:', error);
      if (!isSoftRefresh) {
        this.error =
          error instanceof Error ? error.message : 'Failed to load sessions';
        this.sessions = null;
        this.detail = null;
      }
    } finally {
      if (seq === this.loadSequence) {
        this.loading = false;
      }
    }
  }

  private async loadDetail(isSoftRefresh = false) {
    if (!this.selectedSessionId) {
      this.detail = null;
      this.interactions = null;
      this.activityTimeline = null;
      this.gatewayEvents = [];
      this.gatewayEventsError = null;
      return;
    }

    if (!isSoftRefresh) {
      this.detailLoading = true;
    }
    try {
      this.detail = await getAccountRuntimeSessionDetail(
        this.selectedSessionId,
        this.buildDetailParams()
      );
      // Disabled in favor of unified-session-history
      // this.loadInteractions(isSoftRefresh);
      // this.loadActivityTimeline(isSoftRefresh);
      // await this.loadGatewayEvents(
      //   this.detail.session.flow_execution_id,
      //   isSoftRefresh
      // );
    } catch (error) {
      console.error('Failed to load session detail:', error);
      if (!isSoftRefresh) {
        this.error =
          error instanceof Error
            ? error.message
            : 'Failed to load session detail';
        this.detail = null;
        this.gatewayEvents = [];
        this.gatewayEventsError = null;
      }
    } finally {
      if (!isSoftRefresh) {
        this.detailLoading = false;
      }
    }
  }

  private async loadInteractions(isSoftRefresh = false) {
    if (!this.selectedSessionId) return;
    if (!isSoftRefresh) this.interactionsLoading = true;
    try {
      this.interactions = await getAccountRuntimeSessionInteractions(
        this.selectedSessionId,
        this.buildInteractionsParams()
      );
    } catch (error) {
      console.error('Failed to load interactions:', error);
    } finally {
      if (!isSoftRefresh) this.interactionsLoading = false;
    }
  }

  private async loadActivityTimeline(isSoftRefresh = false) {
    if (!this.selectedSessionId) return;
    if (!isSoftRefresh) this.activityTimelineLoading = true;
    try {
      const resp = await getAccountRuntimeSessionActivityTimeline(
        this.selectedSessionId
      );
      this.activityTimeline = resp.items;
    } catch (error) {
      console.error('Failed to load activity timeline:', error);
    } finally {
      if (!isSoftRefresh) this.activityTimelineLoading = false;
    }
  }

  private async loadGatewayEvents(
    flowExecutionId: string | null | undefined,
    isSoftRefresh = false
  ): Promise<void> {
    if (!flowExecutionId && !this.selectedSessionId) {
      this.gatewayEvents = [];
      this.gatewayEventsError = null;
      if (!isSoftRefresh) this.gatewayEventsLoading = false;
      return;
    }

    if (!isSoftRefresh) this.gatewayEventsLoading = true;
    this.gatewayEventsError = null;
    try {
      let result;
      if (flowExecutionId) {
        result = await getFlowExecutionGatewayEvents(flowExecutionId);
      } else {
        result = await getRuntimeSessionGatewayEvents(this.selectedSessionId!);
      }
      this.gatewayEvents = (result.logs || []).filter(
        (event) => event?.type === 'model_gateway_call'
      );
    } catch (error) {
      console.error('Failed to load gateway events:', error);
      if (!isSoftRefresh) {
        this.gatewayEvents = [];
        this.gatewayEventsError =
          error instanceof Error
            ? error.message
            : 'Failed to load gateway events';
      }
    } finally {
      if (!isSoftRefresh) this.gatewayEventsLoading = false;
    }
  }

  /**
   * Mirror the view into the location.
   *
   * The query and the focused turn ride along with the session, so a link to
   * "the place in this session where that happened" reproduces that view when
   * it is opened again. Opening a snippet pushes rather than replaces, which
   * is what gives the back button something to return to.
   */
  private syncUrl(options: { push?: boolean } = {}) {
    const url = new URL(window.location.href);
    if (this.selectedSessionId) {
      url.searchParams.set('sessionId', this.selectedSessionId);
    } else {
      url.searchParams.delete('sessionId');
    }
    const query = this.searchQuery.trim();
    if (query) {
      url.searchParams.set('q', query);
    } else {
      url.searchParams.delete('q');
    }
    if (this.focusTurnId) {
      url.searchParams.set('turn', this.focusTurnId);
    } else {
      url.searchParams.delete('turn');
    }
    const target = `${url.pathname}${url.search}`;
    if (options.push) {
      window.history.pushState({}, '', target);
    } else {
      window.history.replaceState({}, '', target);
    }
  }

  private handleRangeChange(event: Event) {
    const value = (event.target as HTMLInputElement & { value: string })
      .value as DateRangePreset;
    this.selectedRange = value;
    if (value !== 'custom') {
      this.applyPresetDates(value);
      void this.loadSessions();
    }
  }

  private handleStartDateChange(event: Event) {
    this.startDate = (
      event.target as HTMLInputElement & { value: string }
    ).value;
    this.selectedRange = 'custom';
  }

  private handleEndDateChange(event: Event) {
    this.endDate = (event.target as HTMLInputElement & { value: string }).value;
    this.selectedRange = 'custom';
  }

  /**
   * The bar searches session content as you type.
   *
   * A query asks what the agents said and did, so it goes to the ranked
   * content search rather than the list filter over identifier columns. An
   * empty box is still browsing, so it returns to the plain list. Either way
   * the keystrokes are debounced: one request when typing stops, not one per
   * character.
   */
  private handleSearchChange(event: CustomEvent<{ value: string }>) {
    this.searchQuery = event.detail.value;
    this.cancelSearchDebounce();
    this.searchDebounce = window.setTimeout(() => {
      this.searchDebounce = null;
      void this.runQuery();
    }, SEARCH_DEBOUNCE_MS);
  }

  /** Search when there is a query, list when there is not. */
  private async runQuery(): Promise<void> {
    if (this.isSearching) {
      this.syncUrl();
      await this.loadSearchResults();
      return;
    }
    this.clearSearchResults();
    this.syncUrl();
    await this.loadSessions(this.sessions !== null);
  }

  private cancelSearchDebounce(): void {
    if (this.searchDebounce !== null) {
      window.clearTimeout(this.searchDebounce);
      this.searchDebounce = null;
    }
  }

  private handleSessionSourceTypeChange(event: Event) {
    this.sessionSourceType = (
      event.target as HTMLInputElement & { value: string }
    ).value;
  }

  private handleStatusChange(event: Event) {
    this.status = (event.target as HTMLInputElement & { value: string }).value;
  }

  private handleInteractionQueryChange(event: Event) {
    this.interactionQuery = (
      event.target as HTMLInputElement & { value: string }
    ).value;
  }

  private handleInteractionQueryKeydown(event: KeyboardEvent) {
    if (event.key !== 'Enter') {
      return;
    }
    event.preventDefault();
    void this.applyInteractionSearch();
  }

  private handleGatewaySearchQueryChange(event: Event) {
    this.gatewaySearchQuery = (
      event.target as HTMLInputElement & { value: string }
    ).value;
  }

  private async applyFilters() {
    this.cancelSearchDebounce();
    await this.runQuery();
    if (this.isSearching) {
      // The filters bound the list as well, so it stays in step for the
      // moment the query is cleared.
      await this.loadSessions();
    }
  }

  private async clearFilters() {
    this.cancelSearchDebounce();
    this.selectedRange = 'last-30';
    this.applyPresetDates('last-30');
    this.searchQuery = '';
    this.sessionSourceType = 'all';
    this.status = 'all';
    this.interactionQuery = '';
    this.clearSearchResults();
    this.syncUrl();
    await this.loadSessions();
  }

  private applyInteractionSearch() {
    this.interactions = null;
    this.loadInteractions();
  }

  private getGatewaySearchText(event: FlowGatewayEvent): string {
    const payload = event.payload || {};
    const previewText = this.getGatewayPreviewMessages(payload)
      .map(
        (message) =>
          `${message.source || ''} ${message.role || ''} ${message.text || ''}`
      )
      .join('\n');

    return [
      event.type,
      event.timestamp || '',
      payload.endpoint || '',
      payload.endpoint_kind || '',
      payload.model_alias || '',
      payload.requested_model || '',
      payload.provider_name || '',
      payload.gateway_provider || '',
      payload.error_detail || '',
      payload.message || '',
      previewText,
      this.formatGatewayPayload(payload),
    ]
      .filter(Boolean)
      .join('\n')
      .toLowerCase();
  }

  private getFilteredGatewayEvents(): FlowGatewayEvent[] {
    const query = this.gatewaySearchQuery.trim().toLowerCase();
    if (!query) {
      return this.gatewayEvents;
    }
    return this.gatewayEvents.filter((event) =>
      this.getGatewaySearchText(event).includes(query)
    );
  }

  private selectSession(sessionId: string) {
    if (sessionId === this.selectedSessionId) {
      // The observer echoes its own auto selection back. That is not the
      // operator choosing another session, so a focused turn survives it.
      return;
    }
    this.selectedSessionId = sessionId;
    // Picking another session is not landing on a turn any more.
    this.focusTurnId = null;
    this.syncUrl();
    // Observer loads activity/events for the selection; avoid a duplicate
    // parent getAccountRuntimeSessionDetail fetch.
  }

  /**
   * Open the session detail at the turn a snippet came from.
   *
   * The corpus names the turn (its source id), the transcript scrolls to it,
   * and the location records both, so the answer to "where did that happen"
   * is a link rather than a two hour session to scroll through. Kinds that
   * have no turn in the transcript still open the session; they just omit
   * the turn so the page does not pretend it jumped.
   */
  private snippetJumpsToTurn(snippet: SessionSearchSnippet): boolean {
    return TURN_JUMP_KINDS.has(snippet.source_kind);
  }

  private openSnippet(
    result: SessionSearchResult,
    snippet: SessionSearchSnippet
  ) {
    this.selectedSessionId = result.runtime_session_id;
    this.focusTurnId = this.snippetJumpsToTurn(snippet)
      ? snippet.source_id
      : null;
    this.syncUrl({ push: true });
  }

  private formatNumber(value: number | null | undefined): string {
    return typeof value === 'number' ? value.toLocaleString() : '0';
  }

  /**
   * The honest count every other collection page states, in the same place:
   * how many sessions the filters matched. Empty while the first page is in
   * flight so the bar never claims "0 sessions" before the answer arrives.
   */
  private get sessionCountLabel(): string {
    if (this.isSearching) {
      if (this.searchLoading || !this.searchResults) {
        return '';
      }
      const matched = this.searchResults.total;
      return `${this.formatNumber(matched)} matching session${
        matched === 1 ? '' : 's'
      }`;
    }
    if (this.loading || !this.sessions) {
      return '';
    }
    const total = this.sessions.total ?? this.sessions.items.length;
    return `${this.formatNumber(total)} session${total === 1 ? '' : 's'}`;
  }

  private formatCost(value: number | null | undefined): string {
    if (typeof value !== 'number' || Number.isNaN(value)) {
      return '$0.00';
    }
    return value >= 0.01 ? `$${value.toFixed(2)}` : `$${value.toFixed(4)}`;
  }

  private formatDateTime(value: string | null | undefined): string {
    if (!value) {
      return 'Unknown';
    }
    return new Intl.DateTimeFormat(undefined, {
      month: 'short',
      day: 'numeric',
      year: 'numeric',
      hour: 'numeric',
      minute: '2-digit',
    }).format(new Date(value));
  }

  private getSessionDisplayName(session: RuntimeSessionSummary): string {
    return (
      session.runtime_principal_name ??
      session.flow_name ??
      session.session_reference ??
      `${this.getSourceLabel(session.session_source_type)} ${session.session_source_id}`
    );
  }

  private getSessionVariant(
    session: RuntimeSessionSummary
  ): 'success' | 'primary' | 'neutral' {
    if (session.activity_status === 'active_now') {
      return 'success';
    }
    if (session.activity_status === 'ended') {
      return 'neutral';
    }
    return 'primary';
  }

  private getSessionLabel(session: RuntimeSessionSummary): string {
    if (session.activity_status === 'active_now') {
      return 'Active now';
    }
    if (session.activity_status === 'ended') {
      return 'Ended';
    }
    return 'Idle';
  }

  private getSourceLabel(sourceType: string | null | undefined): string {
    if (!sourceType) {
      return 'Session';
    }
    if (sourceType === 'flow_execution') {
      return 'Flow execution';
    }
    return sourceType
      .split(/[_-]+/g)
      .filter(Boolean)
      .map((segment) => segment.charAt(0).toUpperCase() + segment.slice(1))
      .join(' ');
  }

  private async endSelectedSession(): Promise<void> {
    if (!this.detail?.session || this.detail.session.ended_at) {
      return;
    }
    const session = this.detail.session;
    const confirmed = window.confirm(
      `End session "${this.getSessionDisplayName(session)}"?`
    );
    if (!confirmed) {
      return;
    }
    this.actionLoading = true;
    try {
      await updateAccountRuntimeSession(session.id, { action: 'end' });
      await this.loadSessions();
    } catch (error) {
      console.error('Failed to end session:', error);
      this.error =
        error instanceof Error ? error.message : 'Failed to update session';
    } finally {
      this.actionLoading = false;
    }
  }

  private hasActiveFilters(): boolean {
    return (
      this.selectedRange !== 'last-30' ||
      this.searchQuery !== '' ||
      this.sessionSourceType !== 'all' ||
      this.status !== 'all'
    );
  }

  private emptySessionsText(): string {
    return this.hasActiveFilters()
      ? 'No sessions matched the current filters.'
      : 'No sessions yet. A session is recorded automatically the first time an onboarded agent makes a model or tool call through the gateway. Onboard an agent from the Agents page to see your first one.';
  }

  /**
   * How far the corpus reaches inside the range being searched, when it stops
   * short of it.
   *
   * The endpoint publishes the newest content it has indexed for the account,
   * and the corpus fills forward, so a marker that falls inside the requested
   * range means the newer part of that range cannot match yet. Saying so is
   * the difference between "nothing matched" and "nothing is indexed".
   */
  private partialCoverageThrough(): string | null {
    const marker = this.searchResults?.indexed_through ?? null;
    if (!marker) {
      return null;
    }
    const markerTime = new Date(marker).getTime();
    if (Number.isNaN(markerTime)) {
      return null;
    }
    const start = this.rangeStartIso();
    const startTime = start ? new Date(start).getTime() : null;
    const end = this.rangeEndIso();
    const endTime = end ? new Date(end).getTime() : Date.now();
    if (startTime !== null && markerTime < startTime) {
      return null;
    }
    if (markerTime >= endTime) {
      return null;
    }
    return marker;
  }

  /**
   * How far back the corpus reaches, when it stops inside the range searched.
   *
   * The companion to the marker above, and the one that matters on a
   * deployment that has not run the backfill: indexing happens on write, so
   * the corpus begins on the day search was deployed and every session older
   * than that is absent rather than unmatched. A reader cannot tell those
   * apart from a result count, and the difference is the whole question they
   * are asking. Nothing is shown once the backfill reports it walked the
   * retained history: at that point an empty answer really does mean nobody
   * did that.
   */
  private coverageFloorFrom(): string | null {
    const results = this.searchResults;
    if (!results || results.backfill_complete) {
      return null;
    }
    const marker = results.indexed_from ?? null;
    if (!marker) {
      return null;
    }
    const markerTime = new Date(marker).getTime();
    if (Number.isNaN(markerTime)) {
      return null;
    }
    const start = this.rangeStartIso();
    // An unbounded range starts before any corpus, so any floor is inside it.
    const startTime = start ? new Date(start).getTime() : null;
    if (startTime !== null && markerTime <= startTime) {
      return null;
    }
    // A floor past the end of the range is not suppressed: it means none of
    // the range is indexed, which is the strongest version of this warning,
    // not the absence of one. renderSearchNotices() says so in its own words.
    return marker;
  }

  /**
   * Whether the corpus starts after the end of the range being searched.
   *
   * Then nothing in the range is indexed at all, so an empty answer carries
   * no information about what happened: it is a statement about the corpus.
   */
  private searchedRangeEndsBelowFloor(): boolean {
    const marker = this.coverageFloorFrom();
    if (!marker) {
      return false;
    }
    const end = this.rangeEndIso();
    const endTime = end ? new Date(end).getTime() : Date.now();
    return new Date(marker).getTime() > endTime;
  }

  /**
   * What the search could not do, in the endpoint's own words.
   *
   * The response carries a degraded block; an answer that ranked on keywords
   * alone says so rather than letting the reader assume the semantic half ran.
   */
  private degradedNotice(): string | null {
    const degraded = this.searchResults?.degraded;
    if (!degraded || degraded.reasons.length === 0) {
      return null;
    }
    return (
      degraded.detail ??
      'Semantic ranking did not run for this answer; these are keyword results.'
    );
  }

  private renderSearchNotices() {
    const coverage = this.partialCoverageThrough();
    const floor = this.coverageFloorFrom();
    const floorCoversNothing = this.searchedRangeEndsBelowFloor();
    const degraded = this.degradedNotice();
    if (!coverage && !floor && !degraded) {
      return '';
    }
    return html`
      <div class="search-notices">
        ${
          floor
            ? html`
                <sl-alert
                  variant=${floorCoversNothing ? 'warning' : 'neutral'}
                  open
                  data-testid="coverage-floor-notice"
                >
                  <sl-icon slot="icon" name="clock-history"></sl-icon>
                  ${
                    floorCoversNothing
                      ? html`Nothing in this date range is indexed: search
                        reaches back only to ${this.formatDateTime(floor)}. An
                        empty answer here means not indexed, not that nothing
                        happened. An operator switches on the history backfill
                        to widen this.`
                      : html`Search reaches back to
                        ${this.formatDateTime(floor)}. Sessions older than that
                        are not indexed yet, so they cannot match whatever they
                        contain. An operator switches on the history backfill to
                        widen this.`
                  }
                </sl-alert>
              `
            : ''
        }
        ${
          coverage
            ? html`
                <sl-alert variant="neutral" open data-testid="coverage-notice">
                  <sl-icon slot="icon" name="clock-history"></sl-icon>
                  Search covers content indexed through
                  ${this.formatDateTime(coverage)}. Anything newer in this range
                  is not searchable yet.
                </sl-alert>
              `
            : ''
        }
        ${
          degraded
            ? html`
                <sl-alert variant="warning" open data-testid="degraded-notice">
                  <sl-icon slot="icon" name="exclamation-triangle"></sl-icon>
                  ${degraded}
                </sl-alert>
              `
            : ''
        }
      </div>
    `;
  }

  /** The readable reason a snippet matched, with the role when there is one. */
  private matchTag(snippet: SessionSearchSnippet): string {
    const label = MATCH_TAG_LABELS[snippet.source_kind] ?? snippet.source_kind;
    return snippet.role ? `${label} · ${snippet.role}` : label;
  }

  /**
   * Render one snippet's text.
   *
   * The server marks the matching terms with mark tags around text that is
   * whatever an agent said, so the text is rendered as text and only the
   * marker positions are honoured. Nothing captured is ever rendered as
   * markup.
   */
  private renderSnippetText(snippet: SessionSearchSnippet) {
    if (!snippet.text) {
      return html`<span class="snippet-text muted"
        >No stored text for this match (${snippet.redaction_state}). Open the
        session to see the turn.</span
      >`;
    }
    const parts = snippet.text.split(/<mark>|<\/mark>/);
    return html`<span class="snippet-text"
      >${parts.map((part, index) =>
        index % 2 === 1 ? html`<mark>${part}</mark>` : part
      )}</span
    >`;
  }

  private searchResultTitle(result: SessionSearchResult): string {
    return (
      result.title ||
      result.session_reference ||
      result.session_source_id ||
      'Untitled session'
    );
  }

  private renderSearchResult(result: SessionSearchResult) {
    return html`
      <sl-card data-testid=${`search-result-${result.runtime_session_id}`}>
        <div class="search-result-header">
          <div class="session-item-title">
            ${this.searchResultTitle(result)}
          </div>
          <div class="search-result-meta">
            ${result.matched_chunk_count}
            match${result.matched_chunk_count === 1 ? '' : 'es'} ·
            ${this.formatDateTime(result.last_match_at ?? result.started_at)}
          </div>
        </div>
        <div class="snippet-list">
          ${result.snippets.map(
            (snippet) => html`
              <button
                class="snippet"
                type="button"
                data-testid=${`snippet-${snippet.document_id}`}
                @click=${() => this.openSnippet(result, snippet)}
              >
                <div class="snippet-header">
                  <sl-badge variant="neutral" pill
                    >${this.matchTag(snippet)}</sl-badge
                  >
                  <span>${this.formatDateTime(snippet.occurred_at)}</span>
                </div>
                ${this.renderSnippetText(snippet)}
                ${
                  this.snippetJumpsToTurn(snippet)
                    ? ''
                    : html`<div class="snippet-open-hint">
                        Opens the session
                      </div>`
                }
              </button>
            `
          )}
        </div>
      </sl-card>
    `;
  }

  private renderSearchResults() {
    // A keystroke makes searchQuery non-empty immediately, while the request
    // waits behind the debounce. Until a response (or error) exists, this is
    // still in flight: claiming "nothing matched" would be a lie.
    if (this.searchLoading || (!this.searchResults && !this.searchError)) {
      return html`
        <sl-card>
          <div class="loading-state" data-testid="search-loading">
            <sl-spinner></sl-spinner>
            <div>Searching session content...</div>
          </div>
        </sl-card>
      `;
    }

    if (this.searchError) {
      return html`
        <sl-alert variant="danger" open data-testid="search-error">
          <sl-icon slot="icon" name="exclamation-triangle"></sl-icon>
          ${this.searchError}
        </sl-alert>
      `;
    }

    const results = this.searchResults?.results ?? [];
    if (results.length === 0) {
      return html`
        <sl-card>
          <div class="empty-state" data-testid="search-empty">
            <sl-icon name="search"></sl-icon>
            <div>
              No session content matched that query in the selected range.
            </div>
          </div>
        </sl-card>
      `;
    }

    return html`
      <div class="search-results">
        ${results.map((result) => this.renderSearchResult(result))}
      </div>
    `;
  }

  /**
   * The matched sessions as list rows for the observer, so opening a snippet
   * can show a session the current list page never carried.
   *
   * Prefer the list summary when we already have it: that row carries the
   * ledger the observer toolbar prints. A reconstructed identity row would
   * render as "0 tokens · $0.00" for a session that spent real money.
   */
  private searchResultSessions(): Array<Record<string, unknown>> {
    const listedById = new Map(
      (this.sessions?.items ?? []).map((row) => [row.id, row])
    );
    return (this.searchResults?.results ?? []).map((result) => {
      const listed = listedById.get(result.runtime_session_id);
      if (listed) {
        return listed as unknown as Record<string, unknown>;
      }
      return {
        id: result.runtime_session_id,
        session_source_type: result.session_source_type,
        session_source_id: result.session_source_id,
        session_reference: result.session_reference,
        title: result.title,
        started_at: result.started_at,
        last_activity_at: result.last_activity_at,
      };
    });
  }

  private renderObserver(sessions: Array<Record<string, unknown>> | unknown[]) {
    return html`
      <sl-card>
        <preloop-session-observer
          scope="account"
          hideListSearch
          .sessions=${sessions}
          .emptyText=${this.emptySessionsText()}
          .selectedSessionId=${this.selectedSessionId}
          .focusTurnId=${this.focusTurnId}
          .syncModeToUrl=${true}
          layout="full"
          defaultReplayMode="conversation"
          .features=${{
            summaries: true,
            optimization: this.featureFlags.session_optimization === true,
            auditLinks: true,
            liveFollow: true,
            endSession: true,
            // On here and nowhere else: this is the page where reading one
            // session and wanting the one that went the same way is the
            // actual task. The panel stays collapsed until asked, and an
            // account with no embedded sessions gets a sentence saying so
            // rather than an empty box.
            similarSessions: true,
          }}
          @session-selected=${(event: CustomEvent) => {
            this.selectSession(event.detail.sessionId);
          }}
        ></preloop-session-observer>
      </sl-card>
    `;
  }

  private renderModelBreakdown(models: GatewayUsageByModel[]) {
    if (models.length === 0) {
      return html`
        <div class="empty-state">
          <sl-icon name="cpu"></sl-icon>
          <div>No model usage was recorded for this session.</div>
        </div>
      `;
    }

    return html`
      <div class="breakdown-list">
        <div class="breakdown-header">
          <div>Model</div>
          <div class="cell-numeric">Requests</div>
          <div class="cell-numeric">Tokens</div>
          <div class="cell-numeric">Cost</div>
        </div>
        ${models.map(
          (model) => html`
            <div class="breakdown-row">
              <div>
                <div class="session-item-title">
                  ${model.model_alias || 'Unnamed model'}
                </div>
                <div class="session-item-meta">
                  ${model.provider_name || 'Unknown provider'}
                </div>
              </div>
              <div class="cell-numeric">
                ${this.formatNumber(model.request_count)}
              </div>
              <div class="cell-numeric">
                <token-figures
                  .usage=${model.token_usage}
                  expanded
                ></token-figures>
              </div>
              <div class="cell-numeric">
                ${this.formatCost(model.estimated_cost)}
              </div>
            </div>
          `
        )}
      </div>
    `;
  }

  private renderGatewayField(label: string, value: unknown) {
    return html`
      <div>
        <div class="gateway-event-label">${label}</div>
        <div class="gateway-event-value">${value ?? 'n/a'}</div>
      </div>
    `;
  }

  private formatGatewayPayload(payload: unknown): string {
    return JSON.stringify(payload, null, 2);
  }

  private formatGatewayLabel(value?: string | null): string {
    if (!value) {
      return 'Unknown';
    }
    return value
      .split('_')
      .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
      .join(' ');
  }

  private formatGatewayCost(cost?: number | null): string {
    if (typeof cost !== 'number' || Number.isNaN(cost)) {
      return '$0.00';
    }
    return cost >= 0.01 ? `$${cost.toFixed(2)}` : `$${cost.toFixed(4)}`;
  }

  private formatGatewayTokens(tokens?: number | null): string {
    if (typeof tokens !== 'number' || Number.isNaN(tokens)) {
      return '0';
    }
    return tokens.toLocaleString();
  }

  private getGatewayOutcomeVariant(outcome?: string | null) {
    if (outcome === 'error') {
      return 'danger';
    }
    if (outcome === 'budget_denied') {
      return 'warning';
    }
    if (outcome === 'success') {
      return 'success';
    }
    return 'neutral';
  }

  private getGatewayPreviewMessages(
    payload: FlowGatewayEventPayload
  ): FlowGatewayConversationPreviewMessage[] {
    return Array.isArray(payload.conversation_preview?.messages)
      ? payload.conversation_preview.messages
      : [];
  }

  private renderGatewayPreviewMessage(
    message: FlowGatewayConversationPreviewMessage
  ) {
    const previewText = message.text
      ? message.text
      : message.redacted
        ? 'Content redacted by capture policy.'
        : 'No text content captured.';

    return html`
      <div class="conversation-preview-message">
        <div class="conversation-preview-header">
          <div class="conversation-preview-title">
            ${this.formatGatewayLabel(message.source)}
            ${this.formatGatewayLabel(message.role)}
          </div>
          <div class="gateway-badges">
            ${
              message.redacted
                ? html`<sl-badge pill variant="warning">Redacted</sl-badge>`
                : ''
            }
            ${
              message.truncated
                ? html`<sl-badge pill variant="warning">Truncated</sl-badge>`
                : ''
            }
            ${
              typeof message.original_length === 'number'
                ? html`
                    <sl-badge pill variant="neutral">
                      ${message.original_length.toLocaleString()} chars
                    </sl-badge>
                  `
                : ''
            }
          </div>
        </div>
        <pre class="conversation-preview-text">${previewText}</pre>
        ${
          message.truncated
            ? html`
                <div class="search-summary">
                  This stored preview was truncated before display.
                </div>
              `
            : ''
        }
      </div>
    `;
  }

  private renderGatewayConversationPreview(payload: FlowGatewayEventPayload) {
    const messages = this.getGatewayPreviewMessages(payload);
    const metadata = payload.conversation_preview?.metadata;
    if (messages.length === 0) {
      return html`
        <div class="payload-section-title">Conversation Preview</div>
        <div class="payload-block">
          <pre>No conversation preview captured for this event.</pre>
        </div>
      `;
    }

    return html`
      <div class="payload-section-title">Conversation Preview</div>
      <div
        class="gateway-badges"
        style="margin-bottom: var(--sl-spacing-small);"
      >
        <sl-badge pill>${messages.length} messages</sl-badge>
        ${
          metadata?.has_redacted_content
            ? html`<sl-badge pill variant="warning"
                >Contains redactions</sl-badge
              >`
            : ''
        }
        ${
          metadata?.has_truncated_content
            ? html`<sl-badge pill variant="warning"
                >Contains truncation</sl-badge
              >`
            : ''
        }
      </div>
      <div class="conversation-preview-list">
        ${messages.map((message) => this.renderGatewayPreviewMessage(message))}
      </div>
    `;
  }

  private renderGatewayEvent(event: FlowGatewayEvent) {
    const payload = event.payload;

    return html`
      <sl-details class="gateway-event">
        <div slot="summary" class="gateway-event-summary">
          ${this.renderGatewayField(
            'Time',
            this.formatDateTime(event.timestamp || null)
          )}
          ${this.renderGatewayField(
            'Model',
            payload.model_alias || payload.requested_model || 'Unknown model'
          )}
          ${this.renderGatewayField(
            'Provider',
            payload.provider_name ||
              payload.gateway_provider ||
              'Unknown provider'
          )}
          ${this.renderGatewayField(
            'Outcome',
            html`
              <sl-badge
                variant=${this.getGatewayOutcomeVariant(payload.outcome)}
              >
                ${this.formatGatewayLabel(payload.outcome)}
              </sl-badge>
            `
          )}
          ${this.renderGatewayField(
            'Cost',
            this.formatGatewayCost(payload.estimated_cost)
          )}
          ${this.renderGatewayField(
            'Tokens',
            this.formatGatewayTokens(payload.total_tokens)
          )}
        </div>

        <div class="gateway-event-meta">
          ${this.renderGatewayField(
            'HTTP',
            payload.status_code
              ? `${payload.method || 'POST'} ${payload.status_code}`
              : payload.method || 'n/a'
          )}
          ${this.renderGatewayField(
            'Endpoint',
            payload.endpoint_kind || payload.endpoint || 'n/a'
          )}
          ${this.renderGatewayField(
            'Prompt Tokens',
            this.formatGatewayTokens(payload.prompt_tokens)
          )}
          ${this.renderGatewayField(
            'Completion Tokens',
            this.formatGatewayTokens(payload.completion_tokens)
          )}
        </div>

        ${
          payload.error_detail
            ? html`
                <sl-alert
                  variant="danger"
                  open
                  style="margin-bottom: var(--sl-spacing-medium);"
                >
                  <sl-icon slot="icon" name="exclamation-octagon"></sl-icon>
                  ${payload.error_detail}
                </sl-alert>
              `
            : ''
        }
        ${this.renderGatewayConversationPreview(payload)}
        <div class="payload-section-title">Event Payload</div>
        <div class="payload-block">
          <json-tree .data=${payload}></json-tree>
        </div>
      </sl-details>
    `;
  }

  private renderGatewayEventsPanel() {
    if (!this.detail) {
      return '';
    }

    const filteredEvents = this.getFilteredGatewayEvents();
    const query = this.gatewaySearchQuery.trim();

    return html`
      <sl-card>
        <div
          slot="header"
          class="session-item-title"
          style="display: flex; justify-content: space-between; gap: var(--sl-spacing-small); align-items: center;"
        >
          <span>Session Content</span>
          <sl-badge pill>
            ${
              query
                ? `${filteredEvents.length}/${this.gatewayEvents.length}`
                : this.gatewayEvents.length
            }
          </sl-badge>
        </div>
        <div class="gateway-events-panel">
          <div class="detail-meta">
            Normalized gateway events are rendered to show captured conversation
            previews and payload details.
          </div>
          <div class="interaction-toolbar">
            <sl-input
              label="Search captured session content"
              placeholder="Search previews, payloads, tool outputs, or errors"
              .value=${this.gatewaySearchQuery}
              @sl-input=${this.handleGatewaySearchQueryChange}
            ></sl-input>
          </div>
          <div class="search-summary">
            ${
              query
                ? `Showing ${filteredEvents.length} matching event${filteredEvents.length === 1 ? '' : 's'} for "${query}".`
                : `Showing all ${this.gatewayEvents.length} captured event${this.gatewayEvents.length === 1 ? '' : 's'}.`
            }
          </div>
          ${
            this.gatewayEventsError
              ? html`
                  <sl-alert variant="warning" open>
                    <sl-icon slot="icon" name="exclamation-triangle"></sl-icon>
                    ${this.gatewayEventsError}
                  </sl-alert>
                `
              : ''
          }
          ${
            this.gatewayEventsLoading && this.gatewayEvents.length === 0
              ? html`
                  <div class="loading-state">
                    <sl-spinner></sl-spinner>
                    <div>Loading captured session content...</div>
                  </div>
                `
              : this.gatewayEvents.length === 0
                ? html`
                    <div class="empty-state">
                      <sl-icon name="diagram-3"></sl-icon>
                      <div>
                        No flow gateway events were recorded for this session.
                      </div>
                    </div>
                  `
                : filteredEvents.length === 0
                  ? html`
                      <div class="empty-state">
                        <sl-icon name="search"></sl-icon>
                        <div>
                          No captured session content matched "${query}".
                        </div>
                      </div>
                    `
                  : filteredEvents.map((event) =>
                      this.renderGatewayEvent(event)
                    )
          }
        </div>
      </sl-card>
    `;
  }

  private renderDetail() {
    if (this.detailLoading) {
      return html`
        <sl-card>
          <div class="loading-state">
            <sl-spinner></sl-spinner>
            <div>Loading session details...</div>
          </div>
        </sl-card>
      `;
    }

    if (!this.detail) {
      return html`
        <sl-card>
          <div class="empty-state">
            <sl-icon name="inbox"></sl-icon>
            <div>Select a session to inspect its activity.</div>
          </div>
        </sl-card>
      `;
    }

    const session = this.detail.session;

    return html`
      <div class="detail-stack">
        <sl-card>
          <div slot="header" class="session-item-title">
            <div
              style="display: flex; justify-content: space-between; gap: var(--sl-spacing-small); align-items: center; flex-wrap: wrap;"
            >
              <span>${this.getSessionDisplayName(session)}</span>
              <sl-badge variant=${this.getSessionVariant(session)}>
                ${this.getSessionLabel(session)}
              </sl-badge>
              <legal-hold-control
                resource-type="runtime_session"
                resource-id=${session.id}
                ?known-held=${session.legal_hold === true}
              ></legal-hold-control>
            </div>
          </div>
          <div class="detail-meta">
            ${this.getSourceLabel(session.session_source_type)} · Source ID
            <code>${session.session_source_id}</code>
          </div>
          ${
            session.session_reference
              ? html`
                  <div class="detail-meta">
                    Session reference <code>${session.session_reference}</code>
                  </div>
                `
              : ''
          }
          ${
            session.flow_execution_id
              ? html`
                  <div class="detail-meta">
                    Flow execution
                    <a
                      href=${`/console/flows/executions/${session.flow_execution_id}`}
                      >${session.flow_execution_id}</a
                    >
                  </div>
                `
              : ''
          }
          <div
            style="display: flex; justify-content: flex-end; margin-top: var(--sl-spacing-medium);"
          >
            <sl-button
              variant="warning"
              ?disabled=${Boolean(session.ended_at)}
              ?loading=${this.actionLoading}
              @click=${() => this.endSelectedSession()}
            >
              ${session.ended_at ? 'Session ended' : 'End session'}
            </sl-button>
          </div>
          <div
            class="summary-grid"
            style="margin-top: var(--sl-spacing-medium);"
          >
            <div class="summary-card">
              <div class="summary-label">Requests</div>
              <div class="summary-value">
                ${this.formatNumber(session.total_requests)}
              </div>
              <div class="summary-detail">
                ${this.formatNumber(session.successful_requests)} succeeded,
                ${this.formatNumber(session.failed_requests)} failed
              </div>
            </div>
            <div class="summary-card">
              <div class="summary-label">Tokens</div>
              <div class="summary-value">
                ${this.formatNumber(session.token_usage.total_tokens)}
              </div>
              <div class="summary-detail">
                <token-figures
                  .usage=${session.token_usage}
                  expanded
                ></token-figures>
              </div>
            </div>
            <div class="summary-card">
              <div class="summary-label">Estimated spend</div>
              <div class="summary-value">
                ${this.formatCost(session.estimated_cost)}
              </div>
              <div class="summary-detail">
                Last request ${this.formatDateTime(session.last_request_at)}
              </div>
            </div>
          </div>
        </sl-card>

        <sl-card>
          <div slot="header" class="session-item-title">Usage by model</div>
          ${this.renderModelBreakdown(this.detail.usage_by_model)}
        </sl-card>

        <sl-card style="--padding: 0;">
          <unified-session-history
            .sessions=${[this.detail.session]}
            hideSidebar
            style="height: 600px; display: block;"
          ></unified-session-history>
        </sl-card>
      </div>
    `;
  }

  render() {
    return html`
      <view-header
        headerText="Sessions"
        description="Everything your agents did, as it happened: prompts, responses, tool calls, and cost per session. Follow live or replay later."
        width="extra-wide"
      ></view-header>
      <div class="dashboard extra-wide">
        <div class="main-column">
          <div class="page">
            <list-toolbar
              searchPlaceholder="Search prompts, responses, and tool calls"
              searchLabel="Search session content"
              .search=${this.searchQuery}
              .views=${[]}
              @search-change=${this.handleSearchChange}
            >
              <sl-select
                label="Date range"
                value=${this.selectedRange}
                @sl-change=${this.handleRangeChange}
              >
                <sl-option value="last-7">Last 7 days</sl-option>
                <sl-option value="last-30">Last 30 days</sl-option>
                <sl-option value="last-90">Last 90 days</sl-option>
                <sl-option value="all">All time</sl-option>
                <sl-option value="custom">Custom</sl-option>
              </sl-select>
              <sl-input
                type="date"
                label="Start date"
                .value=${this.startDate}
                @sl-change=${this.handleStartDateChange}
              ></sl-input>
              <sl-input
                type="date"
                label="End date"
                .value=${this.endDate}
                @sl-change=${this.handleEndDateChange}
              ></sl-input>
              <sl-select
                label="Source type"
                value=${this.sessionSourceType}
                @sl-change=${this.handleSessionSourceTypeChange}
              >
                <sl-option value="all">All sources</sl-option>
                <sl-option value="flow_execution">Flow execution</sl-option>
                <sl-option value="claude_code">Claude Code</sl-option>
                <sl-option value="claude_desktop">Claude Desktop</sl-option>
                <sl-option value="codex">Codex</sl-option>
                <sl-option value="openclaw">OpenClaw</sl-option>
                <sl-option value="desktop_agent">Desktop agent</sl-option>
                <sl-option value="custom">Custom</sl-option>
              </sl-select>
              <sl-select
                label="Status"
                value=${this.status}
                @sl-change=${this.handleStatusChange}
              >
                <sl-option value="all">All</sl-option>
                <sl-option value="active">Active</sl-option>
                <sl-option value="ended">Ended</sl-option>
              </sl-select>
              <div class="filter-actions">
                <sl-button variant="primary" @click=${this.applyFilters}>
                  Apply
                </sl-button>
                <sl-button variant="default" @click=${this.clearFilters}>
                  Reset
                </sl-button>
              </div>
              <span slot="count">${this.sessionCountLabel}</span>
            </list-toolbar>
            ${
              this.isPremium
                ? nothing
                : html`
                    <button
                      type="button"
                      class="titles-upsell-hint"
                      @click=${this.openTitlesUpgrade}
                    >
                      Unlock AI titles for these sessions
                    </button>
                  `
            }
            ${
              this.error
                ? html`
                    <sl-alert variant="danger" open>
                      <sl-icon
                        slot="icon"
                        name="exclamation-triangle"
                      ></sl-icon>
                      ${this.error}
                    </sl-alert>
                  `
                : ''
            }
            ${
              this.isSearching
                ? html`
                    ${this.renderSearchNotices()} ${this.renderSearchResults()}
                    ${
                      this.selectedSessionId &&
                      (this.searchResults?.results.length ?? 0) > 0
                        ? this.renderObserver(this.searchResultSessions())
                        : ''
                    }
                  `
                : this.loading
                  ? html`
                      <sl-card>
                        <div class="loading-state">
                          <sl-spinner></sl-spinner>
                          <div>Loading sessions...</div>
                        </div>
                      </sl-card>
                    `
                  : this.renderObserver(this.sessions?.items || [])
            }
          </div>
        </div>
      </div>
    `;
  }
}
