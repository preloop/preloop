import { LitElement, css, html, nothing } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/button-group/button-group.js';
import '@shoelace-style/shoelace/dist/components/card/card.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import {
  applyRuntimeSessionOptimization,
  getAccountAgent,
  getAccountRuntimeSessionActivityTimeline,
  getAccountRuntimeSessionDetail,
  getAccountRuntimeSessions,
  getAIModels,
  getAIModelRuntimeSessions,
  getApiKeyGatewayUsageSummary,
  getRuntimeSessionGatewayEventDetail,
  getRuntimeSessionGatewayEvents,
  listRuntimeSessionOptimizationActions,
  optimizeRuntimeSession,
  summarizeRuntimeSessionGatewayEvent,
  updateAccountRuntimeSession,
} from '../api';
import type {
  AIModel,
  FlowGatewayEvent,
  RuntimeSessionInteractionSummary,
  RuntimeSessionOptimizationAppliedAction,
  RuntimeSessionOptimizationResponse,
  RuntimeSessionActivityItem,
  RuntimeSessionSummary,
} from '../types';
import { unifiedWebSocketManager } from '../services/unified-websocket-manager';
import type {
  ObservedSession,
  SessionObserverFeatures,
  SessionObserverScope,
  SessionReplayMode,
} from '../utils/session-observer';
import {
  formatCost,
  formatNumber,
  normalizeObservedSessions,
} from '../utils/session-observer';
import './session-list-panel';
import './session-replay-panel';

type SessionInput = RuntimeSessionSummary | Record<string, unknown>;
type EventPageState = {
  nextOffset: number | null;
  total: number | null;
  hasMore: boolean;
};

const EVENT_PAGE_SIZE = 25;
const REPLAY_METADATA_LIMIT = 5000;

const DEFAULT_FEATURES: Required<SessionObserverFeatures> = {
  summaries: true,
  optimization: false,
  auditLinks: true,
  liveFollow: true,
  replayModes: true,
  rawPayloads: true,
  endSession: false,
};

@customElement('preloop-session-observer')
export class PreloopSessionObserver extends LitElement {
  @property({ type: String })
  scope: SessionObserverScope = 'account';

  @property({ type: String })
  scopeId = '';

  @property({ type: Array })
  sessions: SessionInput[] | null = null;

  @property({ type: Boolean })
  hideSidebar = false;

  @property({ type: String })
  layout: 'full' | 'embedded' = 'embedded';

  @property({ type: String })
  defaultReplayMode: SessionReplayMode = 'timeline';

  @property({ type: Object })
  features: SessionObserverFeatures = {};

  @property({ type: String })
  selectedSessionId: string | null = null;

  @state()
  private observedSessions: ObservedSession[] = [];

  @state()
  private activeSessionId: string | null = null;

  @state()
  private loadedEvents: Record<string, FlowGatewayEvent[]> = {};

  @state()
  private loadedReplayMetadata: Record<string, FlowGatewayEvent[]> = {};

  @state()
  private loadedEventPages: Record<string, EventPageState> = {};

  @state()
  private loadedActivity: Record<string, RuntimeSessionActivityItem[]> = {};

  @state()
  private loadedEventDetails: Record<string, FlowGatewayEvent> = {};

  @state()
  private loadedInteractionSummaries: Record<
    string,
    RuntimeSessionInteractionSummary
  > = {};

  @state()
  private loadedOptimizations: Record<
    string,
    RuntimeSessionOptimizationResponse
  > = {};

  @state()
  private loadedOptimizationActions: Record<
    string,
    RuntimeSessionOptimizationAppliedAction[]
  > = {};

  @state()
  private applyingOptimizationSuggestionId: string | null = null;

  @state()
  private aiModels: AIModel[] = [];

  @state()
  private loading = false;

  @state()
  private loadingSessionId: string | null = null;

  @state()
  private loadingMoreEventsForSessionId: string | null = null;

  @state()
  private loadingReplayMetadataForSessionId: string | null = null;

  @state()
  private loadingOptimizationForSessionId: string | null = null;

  @state()
  private loadingEventDetails = new Set<string>();

  @state()
  private loadingInteractionSummaries = new Set<string>();

  @state()
  private error: string | null = null;

  @state()
  private replayMode: SessionReplayMode = 'timeline';

  @state()
  private followLive = true;

  @state()
  private summarizeVisibleContent = false;

  @state()
  private livePulse = false;

  @state()
  private searchQuery = '';

  private unsubscribeRealtime?: () => void;
  private refreshTimer: number | null = null;
  private livePulseTimer: number | null = null;

  static styles = css`
    :host {
      display: block;
    }

    .observer {
      display: grid;
      gap: var(--sl-spacing-large);
      min-height: 0;
    }

    .observer.with-sidebar {
      grid-template-columns: minmax(280px, 360px) minmax(0, 1fr);
    }

    .sidebar,
    .content {
      min-height: 0;
      overflow: auto;
    }

    .toolbar,
    .mode-row,
    .summary-row {
      align-items: center;
      display: flex;
      flex-wrap: wrap;
      gap: var(--sl-spacing-small);
      justify-content: space-between;
    }

    .content {
      display: flex;
      flex-direction: column;
      gap: var(--sl-spacing-medium);
    }

    .toolbar {
      background: var(--sl-color-neutral-50);
      border: 1px solid var(--sl-color-neutral-200);
      border-radius: var(--sl-border-radius-medium);
      padding: var(--sl-spacing-small);
    }

    .meta {
      color: var(--sl-color-neutral-600);
      font-size: var(--sl-font-size-small);
    }

    .live-indicator {
      align-items: center;
      background: var(--sl-color-neutral-100);
      border-radius: 999px;
      color: var(--sl-color-neutral-600);
      display: inline-flex;
      font-size: 0.72rem;
      font-weight: 700;
      gap: 5px;
      letter-spacing: 0.04em;
      padding: 3px 8px;
      text-transform: uppercase;
    }

    .live-dot {
      background: var(--sl-color-success-500);
      border-radius: 999px;
      height: 7px;
      width: 7px;
    }

    .live-indicator.pulsing {
      background: var(--sl-color-success-100);
      color: var(--sl-color-success-700);
    }

    .empty,
    .loading {
      color: var(--sl-color-neutral-600);
      padding: var(--sl-spacing-x-large);
      text-align: center;
    }

    @media (max-width: 950px) {
      .observer.with-sidebar {
        grid-template-columns: 1fr;
      }
    }
  `;

  connectedCallback(): void {
    super.connectedCallback();
    this.replayMode = this.defaultReplayMode;
    this.connectRealtime();
    void this.loadAIModels();
    void this.loadSessions();
  }

  disconnectedCallback(): void {
    super.disconnectedCallback();
    this.unsubscribeRealtime?.();
    if (this.refreshTimer !== null) window.clearTimeout(this.refreshTimer);
    if (this.livePulseTimer !== null) window.clearTimeout(this.livePulseTimer);
  }

  willUpdate(changed: Map<string | number | symbol, unknown>): void {
    if (changed.has('sessions')) {
      this.applySessions(this.sessions || []);
    }
    if (changed.has('selectedSessionId') && this.selectedSessionId) {
      void this.selectSession(this.selectedSessionId);
    }
  }

  public async reload(): Promise<void> {
    await this.loadSessions({ preserveSelection: true });
  }

  public async reloadActiveSession(): Promise<void> {
    if (!this.activeSessionId) return;
    delete this.loadedEvents[this.activeSessionId];
    delete this.loadedReplayMetadata[this.activeSessionId];
    delete this.loadedEventPages[this.activeSessionId];
    delete this.loadedActivity[this.activeSessionId];
    await this.selectSession(this.activeSessionId, { force: true });
  }

  private get enabledFeatures(): Required<SessionObserverFeatures> {
    return { ...DEFAULT_FEATURES, ...this.features };
  }

  private connectRealtime(): void {
    const unsubscribers = [
      unifiedWebSocketManager.subscribe('gateway_activity', (message) =>
        this.handleGatewayActivity(message)
      ),
      unifiedWebSocketManager.subscribe('runtime_sessions', (message) =>
        this.handleRuntimeSessionActivity(message)
      ),
    ];
    this.unsubscribeRealtime = () => {
      for (const unsubscribe of unsubscribers) unsubscribe();
    };
    void unifiedWebSocketManager.connect();
  }

  private handleRuntimeSessionActivity(message: any): void {
    if (!this.matchesScope(message?.payload ?? {})) return;
    this.pulseLive();
    this.scheduleScopeRefresh();
  }

  private handleGatewayActivity(message: any): void {
    const payload = message?.payload ?? {};
    if (!this.matchesScope(payload)) return;
    this.pulseLive();

    const sessionId = payload.runtime_session_id;
    if (!sessionId) {
      this.scheduleScopeRefresh();
      return;
    }

    if (this.activeSessionId === sessionId && this.loadedEvents[sessionId]) {
      const event: FlowGatewayEvent = {
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
      const currentEvents = this.loadedEvents[sessionId] || [];
      this.loadedEvents = {
        ...this.loadedEvents,
        [sessionId]: this.mergeEvents([event], currentEvents),
      };
      const currentPage = this.loadedEventPages[sessionId];
      if (currentPage) {
        this.loadedEventPages = {
          ...this.loadedEventPages,
          [sessionId]: {
            ...currentPage,
            total: currentPage.total === null ? null : currentPage.total + 1,
          },
        };
      }
      if (this.followLive) {
        this.updateComplete.then(() => {
          const content = this.renderRoot.querySelector('.content');
          content?.scrollTo({ top: 0, behavior: 'smooth' });
        });
      }
    }
    this.scheduleScopeRefresh();
  }

  private matchesScope(payload: Record<string, any>): boolean {
    if (!this.scopeId) return true;
    if (this.scope === 'runtime_session') {
      return payload.runtime_session_id === this.scopeId;
    }
    if (this.scope === 'managed_agent') {
      return payload.managed_agent_id === this.scopeId;
    }
    if (this.scope === 'api_key') {
      return payload.api_key_id === this.scopeId;
    }
    if (this.scope === 'ai_model') {
      return payload.ai_model_id === this.scopeId;
    }
    return true;
  }

  private pulseLive(): void {
    this.livePulse = true;
    if (this.livePulseTimer !== null) window.clearTimeout(this.livePulseTimer);
    this.livePulseTimer = window.setTimeout(() => {
      this.livePulse = false;
      this.livePulseTimer = null;
    }, 1400);
  }

  private scheduleScopeRefresh(): void {
    if (this.refreshTimer !== null) window.clearTimeout(this.refreshTimer);
    this.refreshTimer = window.setTimeout(() => {
      this.refreshTimer = null;
      void this.loadSessions({ preserveSelection: true, soft: true });
    }, 500);
  }

  private async loadSessions(
    options: { preserveSelection?: boolean; soft?: boolean } = {}
  ): Promise<void> {
    if (this.sessions) {
      this.applySessions(this.sessions);
      return;
    }
    if (!options.soft) this.loading = true;
    this.error = null;
    try {
      let rows: SessionInput[] = [];
      if (this.scope === 'runtime_session' && this.scopeId) {
        const detail = await getAccountRuntimeSessionDetail(this.scopeId);
        rows = [detail.session];
      } else if (this.scope === 'managed_agent' && this.scopeId) {
        const detail = await getAccountAgent(this.scopeId);
        rows = detail.sessions;
      } else if (this.scope === 'api_key' && this.scopeId) {
        const summary = await getApiKeyGatewayUsageSummary(this.scopeId);
        rows = summary.usage_by_session as unknown as SessionInput[];
      } else if (this.scope === 'ai_model' && this.scopeId) {
        const sessions = await getAIModelRuntimeSessions(this.scopeId, {
          limit: 50,
          status: 'all',
        });
        rows = sessions.items;
      } else {
        const sessions = await getAccountRuntimeSessions({
          limit: 50,
          status: 'all',
        });
        rows = sessions.items;
      }
      this.applySessions(rows, options.preserveSelection);
    } catch (error) {
      console.error('Failed to load session observer data:', error);
      this.error =
        error instanceof Error ? error.message : 'Failed to load sessions';
    } finally {
      this.loading = false;
    }
  }

  private applySessions(rows: SessionInput[], preserveSelection = true): void {
    this.observedSessions = normalizeObservedSessions(rows);
    const requested = this.selectedSessionId || this.activeSessionId;
    const nextActive =
      preserveSelection &&
      requested &&
      this.observedSessions.some((session) => session.id === requested)
        ? requested
        : this.observedSessions[0]?.id || null;
    if (nextActive && nextActive !== this.activeSessionId) {
      void this.selectSession(nextActive);
    } else if (!nextActive) {
      this.activeSessionId = null;
    }
  }

  private async selectSession(
    sessionId: string,
    options: { force?: boolean } = {}
  ): Promise<void> {
    if (sessionId === this.activeSessionId && !options.force) {
      return;
    }
    this.activeSessionId = sessionId;
    this.dispatchEvent(
      new CustomEvent('session-selected', {
        detail: { sessionId },
        bubbles: true,
        composed: true,
      })
    );
    const session = this.activeSession;
    if (!session || !session.canLoadEvents) return;
    if (!options.force && this.loadedEvents[sessionId]) {
      return;
    }
    this.loadingSessionId = sessionId;
    try {
      const [events, activity] = await Promise.all([
        getRuntimeSessionGatewayEvents(sessionId, {
          limit: EVENT_PAGE_SIZE,
          offset: 0,
        }),
        getAccountRuntimeSessionActivityTimeline(sessionId).catch(() => ({
          items: [],
        })),
      ]);
      this.loadedEvents = {
        ...this.loadedEvents,
        [sessionId]: this.sortEventsDescending(events.logs || []),
      };
      this.loadedEventPages = {
        ...this.loadedEventPages,
        [sessionId]: {
          nextOffset:
            events.pagination?.next_offset ??
            ((events.logs || []).length >= EVENT_PAGE_SIZE
              ? (events.logs || []).length
              : null),
          total: events.pagination?.total ?? null,
          hasMore:
            events.pagination?.has_more ??
            (events.logs || []).length >= EVENT_PAGE_SIZE,
        },
      };
      this.loadedActivity = {
        ...this.loadedActivity,
        [sessionId]: activity.items || [],
      };
    } catch (error) {
      console.error('Failed to load selected session:', error);
      this.error =
        error instanceof Error
          ? error.message
          : 'Failed to load session replay';
    } finally {
      this.loadingSessionId = null;
    }
  }

  private sortEventsDescending(events: FlowGatewayEvent[]): FlowGatewayEvent[] {
    return [...events].sort(
      (left, right) =>
        new Date(right.timestamp || 0).getTime() -
        new Date(left.timestamp || 0).getTime()
    );
  }

  private mergeEvents(
    currentEvents: FlowGatewayEvent[],
    nextEvents: FlowGatewayEvent[]
  ): FlowGatewayEvent[] {
    const byId = new Map<string, FlowGatewayEvent>();
    for (const event of [...currentEvents, ...nextEvents]) {
      byId.set(event.id, event);
    }
    return this.sortEventsDescending(Array.from(byId.values()));
  }

  private async loadMoreEvents(sessionId: string): Promise<void> {
    if (this.loadingMoreEventsForSessionId === sessionId) return;
    const page = this.loadedEventPages[sessionId];
    if (!page?.hasMore || page.nextOffset === null) return;
    this.loadingMoreEventsForSessionId = sessionId;
    try {
      const events = await getRuntimeSessionGatewayEvents(sessionId, {
        limit: EVENT_PAGE_SIZE,
        offset: page.nextOffset,
      });
      this.loadedEvents = {
        ...this.loadedEvents,
        [sessionId]: this.mergeEvents(
          this.loadedEvents[sessionId] || [],
          events.logs || []
        ),
      };
      this.loadedEventPages = {
        ...this.loadedEventPages,
        [sessionId]: {
          nextOffset: events.pagination?.next_offset ?? null,
          total: events.pagination?.total ?? page.total,
          hasMore: events.pagination?.has_more ?? false,
        },
      };
    } catch (error) {
      console.error('Failed to load more session events:', error);
      this.error =
        error instanceof Error ? error.message : 'Failed to load more events';
    } finally {
      this.loadingMoreEventsForSessionId = null;
    }
  }

  private async loadReplayMetadata(sessionId: string): Promise<void> {
    if (
      this.loadedReplayMetadata[sessionId] ||
      this.loadingReplayMetadataForSessionId === sessionId
    ) {
      return;
    }
    this.loadingReplayMetadataForSessionId = sessionId;
    try {
      const events = await getRuntimeSessionGatewayEvents(sessionId, {
        limit: REPLAY_METADATA_LIMIT,
        offset: 0,
        metadataOnly: true,
      });
      this.loadedReplayMetadata = {
        ...this.loadedReplayMetadata,
        [sessionId]: this.sortEventsDescending(events.logs || []),
      };
    } catch (error) {
      console.error('Failed to load replay metadata:', error);
      this.error =
        error instanceof Error
          ? error.message
          : 'Failed to load replay metadata';
    } finally {
      this.loadingReplayMetadataForSessionId = null;
    }
  }

  private async loadSessionOptimization(
    sessionId: string,
    options: {
      regenerate?: boolean;
      modelId?: string | null;
      eventIds?: string[];
      sourceKinds?: string[];
      fromIndex?: number;
      toIndex?: number;
    } = {}
  ): Promise<void> {
    if (!options.regenerate && this.loadedOptimizations[sessionId]) return;
    if (this.loadingOptimizationForSessionId === sessionId) return;
    this.loadingOptimizationForSessionId = sessionId;
    try {
      const optimization = await optimizeRuntimeSession(sessionId, options);
      this.loadedOptimizations = {
        ...this.loadedOptimizations,
        [sessionId]: optimization,
      };
    } catch (error) {
      console.info('Using local session optimization fallback:', error);
    } finally {
      this.loadingOptimizationForSessionId = null;
    }
  }

  private async loadAIModels(): Promise<void> {
    try {
      this.aiModels = (await getAIModels()).filter(
        (model) => (model.model_kind || 'llm') === 'llm'
      );
    } catch (error) {
      console.info('Unable to load optimization model choices:', error);
      this.aiModels = [];
    }
  }

  private getActiveOptimizationSuggestions() {
    const optimization = this.activeSessionId
      ? this.loadedOptimizations[this.activeSessionId]
      : null;
    if (!optimization || !Array.isArray(optimization.suggestions)) return null;
    return optimization.suggestions.map((suggestion) => ({
      id: suggestion.id,
      title: suggestion.title,
      description: suggestion.description,
      expectedSavingsTokens: suggestion.expected_savings_tokens,
      expectedSavingsUsd: suggestion.expected_savings_usd,
      confidence: suggestion.confidence as 'low' | 'medium' | 'high',
      actionLabel: suggestion.action_label,
      evidence: suggestion.evidence,
      evidenceEventIds: suggestion.evidence_event_ids || [],
      action: suggestion.action || null,
    }));
  }

  private async loadOptimizationActions(sessionId: string): Promise<void> {
    try {
      const response = await listRuntimeSessionOptimizationActions(sessionId);
      this.loadedOptimizationActions = {
        ...this.loadedOptimizationActions,
        [sessionId]: response.items || [],
      };
    } catch (error) {
      console.info('Unable to load applied optimization actions:', error);
    }
  }

  private async applyOptimizationSuggestion(detail: {
    suggestionId: string;
    suggestionTitle?: string | null;
    action: { type: string; params: Record<string, unknown> };
  }): Promise<void> {
    if (!this.activeSessionId || this.applyingOptimizationSuggestionId) return;
    const sessionId = this.activeSessionId;
    this.applyingOptimizationSuggestionId = detail.suggestionId;
    try {
      const applied = await applyRuntimeSessionOptimization(sessionId, {
        suggestionId: detail.suggestionId,
        suggestionTitle: detail.suggestionTitle,
        action: detail.action,
      });
      this.loadedOptimizationActions = {
        ...this.loadedOptimizationActions,
        [sessionId]: [
          applied,
          ...(this.loadedOptimizationActions[sessionId] || []).filter(
            (item) => item.id !== applied.id
          ),
        ],
      };
    } catch (error) {
      this.error =
        error instanceof Error
          ? error.message
          : 'Failed to apply optimization action';
    } finally {
      this.applyingOptimizationSuggestionId = null;
    }
  }

  private async loadEventDetail(eventId: string): Promise<void> {
    if (!this.activeSessionId || this.loadedEventDetails[eventId]) return;
    const nextLoading = new Set(this.loadingEventDetails);
    nextLoading.add(eventId);
    this.loadingEventDetails = nextLoading;
    try {
      const detail = await getRuntimeSessionGatewayEventDetail(
        this.activeSessionId,
        eventId
      );
      this.loadedEventDetails = {
        ...this.loadedEventDetails,
        [eventId]: detail,
      };
    } catch (error) {
      console.error('Failed to load event detail:', error);
      this.error =
        error instanceof Error ? error.message : 'Failed to load event detail';
    } finally {
      const done = new Set(this.loadingEventDetails);
      done.delete(eventId);
      this.loadingEventDetails = done;
    }
  }

  private async loadInteractionSummary(eventId: string): Promise<void> {
    if (!this.activeSessionId || this.loadedInteractionSummaries[eventId])
      return;
    const nextLoading = new Set(this.loadingInteractionSummaries);
    nextLoading.add(eventId);
    this.loadingInteractionSummaries = nextLoading;
    try {
      const summary = await summarizeRuntimeSessionGatewayEvent(
        this.activeSessionId,
        eventId
      );
      this.loadedInteractionSummaries = {
        ...this.loadedInteractionSummaries,
        [eventId]: summary,
      };
    } catch (error) {
      console.error('Failed to summarize interaction:', error);
      this.error =
        error instanceof Error
          ? error.message
          : 'Failed to summarize interaction';
    } finally {
      const done = new Set(this.loadingInteractionSummaries);
      done.delete(eventId);
      this.loadingInteractionSummaries = done;
    }
  }

  private async endActiveSession(): Promise<void> {
    if (!this.activeSession || !this.activeSession.canLoadEvents) return;
    if (!window.confirm(`End session "${this.activeSession.title}"?`)) return;
    try {
      await updateAccountRuntimeSession(this.activeSession.id, {
        action: 'end',
        reason: 'Ended from session observer',
      });
      await this.loadSessions({ preserveSelection: true });
    } catch (error) {
      this.error =
        error instanceof Error ? error.message : 'Failed to end session';
    }
  }

  private toggleSummaries(): void {
    if (!this.summarizeVisibleContent) {
      const acknowledged =
        localStorage.getItem(
          'preloop.sessionObserver.summarizeCostAcknowledged'
        ) === 'true';
      if (!acknowledged) {
        const accepted = window.confirm(
          'Summarizing replay content may use your account default model and incur model costs as you browse or replay long messages. Continue?'
        );
        if (!accepted) return;
        localStorage.setItem(
          'preloop.sessionObserver.summarizeCostAcknowledged',
          'true'
        );
      }
    }
    this.summarizeVisibleContent = !this.summarizeVisibleContent;
  }

  private get activeSession(): ObservedSession | null {
    return (
      this.observedSessions.find(
        (session) => session.id === this.activeSessionId
      ) || null
    );
  }

  private get filteredSessions(): ObservedSession[] {
    const query = this.searchQuery.trim().toLowerCase();
    if (!query) return this.observedSessions;
    return this.observedSessions.filter((session) =>
      [
        session.title,
        session.subtitle,
        session.sessionReference,
        session.runtimePrincipalName,
        session.latestModelAlias,
      ]
        .filter(Boolean)
        .join('\n')
        .toLowerCase()
        .includes(query)
    );
  }

  private get activeEvents(): FlowGatewayEvent[] {
    return this.activeSessionId
      ? this.loadedEvents[this.activeSessionId] || []
      : [];
  }

  private get activeActivity(): RuntimeSessionActivityItem[] {
    return this.activeSessionId
      ? this.loadedActivity[this.activeSessionId] || []
      : [];
  }

  private get activeEventPage(): EventPageState | null {
    return this.activeSessionId
      ? this.loadedEventPages[this.activeSessionId] || null
      : null;
  }

  private get replayModeTabs(): Array<{
    mode: SessionReplayMode;
    label: string;
    icon: string | null;
  }> {
    const tabs: Array<{
      mode: SessionReplayMode;
      label: string;
      icon: string | null;
    }> = [
      { mode: 'timeline', label: 'Transcript', icon: 'list-ul' },
      { mode: 'replay', label: 'Replay', icon: 'play-circle' },
    ];
    if (this.enabledFeatures.optimization) {
      tabs.push({ mode: 'optimize', label: 'Optimize', icon: 'magic' });
    }
    return tabs;
  }

  private setReplayMode(mode: SessionReplayMode): void {
    if (this.replayMode === mode) return;
    this.replayMode = mode;
    if (mode === 'replay' && this.activeSessionId) {
      void this.loadReplayMetadata(this.activeSessionId);
    }
    if (mode === 'optimize' && this.activeSessionId) {
      void this.loadOptimizationActions(this.activeSessionId);
    }
  }

  private renderToolbar() {
    const session = this.activeSession;
    return html`
      <div class="toolbar">
        <div>
          <div class="summary-row">
            <span
              class="live-indicator ${this.livePulse ? 'pulsing' : ''}"
              title="Realtime session updates"
            >
              <span class="live-dot"></span>
              ${this.followLive ? 'Following live' : 'Paused'}
            </span>
            ${session
              ? html`
                  <span class="meta">
                    ${formatNumber(session.tokenUsage.total_tokens)} tokens ·
                    ${formatCost(session.estimatedCost)}
                  </span>
                `
              : nothing}
          </div>
        </div>
        <div class="mode-row">
          ${this.enabledFeatures.liveFollow
            ? html`
                <sl-button
                  size="small"
                  variant=${this.followLive ? 'primary' : 'default'}
                  @click=${() => (this.followLive = !this.followLive)}
                >
                  ${this.followLive ? 'Pause follow' : 'Follow live'}
                </sl-button>
              `
            : nothing}
          ${this.enabledFeatures.replayModes
            ? html`
                <sl-button-group>
                  ${this.replayModeTabs.map(
                    (tab) => html`
                      <sl-button
                        size="small"
                        variant=${this.replayMode === tab.mode
                          ? 'primary'
                          : 'default'}
                        @click=${() => this.setReplayMode(tab.mode)}
                      >
                        ${tab.icon
                          ? html`<sl-icon
                              slot="prefix"
                              name=${tab.icon}
                            ></sl-icon>`
                          : nothing}
                        ${tab.label}
                      </sl-button>
                    `
                  )}
                </sl-button-group>
              `
            : nothing}
          <sl-button size="small" @click=${() => this.reloadActiveSession()}>
            Refresh
          </sl-button>
          ${this.enabledFeatures.endSession && session?.canLoadEvents
            ? html`
                <sl-button
                  size="small"
                  variant="warning"
                  ?disabled=${session.status === 'ended'}
                  @click=${this.endActiveSession}
                >
                  End session
                </sl-button>
              `
            : nothing}
        </div>
      </div>
    `;
  }

  render() {
    if (this.loading && !this.observedSessions.length) {
      return html`
        <div class="loading">
          <sl-spinner></sl-spinner>
          <div>Loading sessions...</div>
        </div>
      `;
    }

    const content = html`
      <div class="content">
        ${this.renderToolbar()}
        ${this.error
          ? html`
              <sl-alert variant="danger" open>
                <sl-icon slot="icon" name="exclamation-triangle"></sl-icon>
                ${this.error}
              </sl-alert>
            `
          : nothing}
        <session-replay-panel
          .session=${this.activeSession}
          .events=${this.activeEvents}
          .timelineEvents=${this.activeSessionId
            ? this.loadedReplayMetadata[this.activeSessionId] || []
            : []}
          .activity=${this.activeActivity}
          .replayMode=${this.replayMode}
          .loading=${this.loadingSessionId === this.activeSessionId}
          .rawPayloads=${this.enabledFeatures.rawPayloads}
          .eventDetails=${this.loadedEventDetails}
          .loadingEventDetails=${this.loadingEventDetails}
          .interactionSummaries=${this.loadedInteractionSummaries}
          .loadingInteractionSummaries=${this.loadingInteractionSummaries}
          .summarizeVisibleContent=${this.summarizeVisibleContent}
          .hasMoreEvents=${this.activeEventPage?.hasMore ?? false}
          .loadingMoreEvents=${this.loadingMoreEventsForSessionId ===
          this.activeSessionId}
          .totalEvents=${this.activeEventPage?.total ?? null}
          .optimizationEnabled=${this.enabledFeatures.optimization}
          .availableModels=${this.aiModels}
          .optimizationResult=${this.activeSessionId
            ? this.loadedOptimizations[this.activeSessionId] || null
            : null}
          .optimizationSuggestions=${this.getActiveOptimizationSuggestions()}
          .optimizationAppliedActions=${this.activeSessionId
            ? this.loadedOptimizationActions[this.activeSessionId] || []
            : []}
          .applyingOptimizationSuggestionId=${this
            .applyingOptimizationSuggestionId}
          .loadingOptimization=${this.loadingOptimizationForSessionId ===
          this.activeSessionId}
          @session-event-detail-requested=${(event: CustomEvent) =>
            this.loadEventDetail(event.detail.eventId)}
          @session-interaction-summary-requested=${(event: CustomEvent) =>
            this.loadInteractionSummary(event.detail.eventId)}
          @session-events-page-requested=${() =>
            this.activeSessionId
              ? this.loadMoreEvents(this.activeSessionId)
              : undefined}
          @session-replay-metadata-requested=${() =>
            this.activeSessionId
              ? this.loadReplayMetadata(this.activeSessionId)
              : undefined}
          @session-optimization-requested=${(event: CustomEvent) => {
            if (!this.activeSessionId) return;
            this.loadSessionOptimization(this.activeSessionId, {
              regenerate: Boolean(event.detail?.regenerate),
              modelId: event.detail?.modelId || null,
              eventIds: event.detail?.eventIds || [],
              sourceKinds: event.detail?.sourceKinds || [],
              fromIndex: event.detail?.fromIndex,
              toIndex: event.detail?.toIndex,
            });
            this.loadOptimizationActions(this.activeSessionId);
          }}
          @session-optimization-apply=${(event: CustomEvent) =>
            this.applyOptimizationSuggestion(event.detail)}
          @session-optimization-actions-requested=${() =>
            this.activeSessionId
              ? this.loadOptimizationActions(this.activeSessionId)
              : undefined}
        ></session-replay-panel>
      </div>
    `;

    return html`
      <div
        class="observer ${this.hideSidebar ? '' : 'with-sidebar'}"
        style=${this.layout === 'full'
          ? 'min-height: 720px;'
          : 'min-height: 520px;'}
      >
        ${this.hideSidebar
          ? nothing
          : html`
              <div class="sidebar">
                <sl-input
                  placeholder="Search sessions"
                  clearable
                  .value=${this.searchQuery}
                  @sl-input=${(event: Event) => {
                    this.searchQuery = (event.target as HTMLInputElement).value;
                  }}
                  style="margin-bottom: var(--sl-spacing-small);"
                >
                  <sl-icon name="search" slot="prefix"></sl-icon>
                </sl-input>
                <session-list-panel
                  .sessions=${this.filteredSessions}
                  .activeSessionId=${this.activeSessionId}
                  @session-selected=${(event: CustomEvent) =>
                    this.selectSession(event.detail.sessionId)}
                ></session-list-panel>
              </div>
            `}
        ${content}
      </div>
    `;
  }
}
