import { LitElement, css, html, nothing } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/button-group/button-group.js';
import '@shoelace-style/shoelace/dist/components/card/card.js';
import '@shoelace-style/shoelace/dist/components/dialog/dialog.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/icon-button/icon-button.js';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import {
  applyRuntimeSessionOptimization,
  createBudgetPolicy,
  getAccountAgent,
  getAccountRuntimeSessionActivityTimeline,
  getAccountRuntimeSessionDetail,
  getAccountRuntimeSessions,
  getAIModels,
  getAIModelRuntimeSessions,
  getApiKeyGatewayUsageSummary,
  getRuntimeSessionGatewayEventDetail,
  getRuntimeSessionGatewayEvents,
  getRuntimeSessionOptimizationJob,
  getRuntimeSessionRequests,
  listRuntimeSessionOptimizationActions,
  optimizeRuntimeSession,
  submitRuntimeSessionOptimizationJob,
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
  RuntimeSessionCacheSummary,
  RuntimeSessionRequestItem,
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
import { reducedMotionStyles } from '../styles/reduced-motion';
import './session-chat-view';
import './session-list-panel';
import './session-replay-panel';
import './session-request-timeline';

type SessionInput = RuntimeSessionSummary | Record<string, unknown>;
type EventPageState = {
  nextOffset: number | null;
  total: number | null;
  hasMore: boolean;
};

const EVENT_PAGE_SIZE = 25;
const REPLAY_METADATA_LIMIT = 5000;

// Sidebar collapse/expand animation duration. Must match the CSS transition
// below: the collapse completion (DOM swap to the picker bar) is driven by a
// timeout of this length rather than transitionend, which is unreliable when
// the tab is backgrounded or the transition is interrupted.
const SIDEBAR_ANIMATION_MS = 250;

// First-use hint for the Optimize tab: rendered once per user (same
// localStorage mechanism as dashboard_welcome_dismissed) until the drawer is
// opened or the hint is dismissed.
const OPTIMIZE_HINT_DISMISSED_KEY = 'optimize_hint_dismissed';

/** Read the Optimize hint dismiss flag; never throw if storage is unavailable. */
function readOptimizeHintDismissed(): boolean {
  try {
    return localStorage.getItem(OPTIMIZE_HINT_DISMISSED_KEY) === 'true';
  } catch {
    return false;
  }
}

// Async optimization analysis jobs: submit-then-poll cadence and the
// per-session sessionStorage key used to resume an in-flight job after a
// reload instead of showing the trigger button again.
const OPTIMIZE_JOB_POLL_INTERVAL_MS = 2500;
const OPTIMIZE_JOB_STORAGE_PREFIX = 'preloop_optimize_job_';

type OptimizationJobRequestOptions = {
  regenerate?: boolean;
  modelId?: string | null;
  eventIds?: string[];
  sourceKinds?: string[];
  fromIndex?: number;
  toIndex?: number;
};

/** Read the persisted active job id for a session; never throw. */
function readStoredOptimizationJobId(sessionId: string): string | null {
  try {
    return sessionStorage.getItem(`${OPTIMIZE_JOB_STORAGE_PREFIX}${sessionId}`);
  } catch {
    return null;
  }
}

/** Persist the active job id for a session; never throw. */
function storeOptimizationJobId(sessionId: string, jobId: string): void {
  try {
    sessionStorage.setItem(`${OPTIMIZE_JOB_STORAGE_PREFIX}${sessionId}`, jobId);
  } catch {
    // Privacy modes / missing storage: reload-resume simply won't apply.
  }
}

/** Drop the persisted job id for a session; never throw. */
function clearStoredOptimizationJobId(sessionId: string): void {
  try {
    sessionStorage.removeItem(`${OPTIMIZE_JOB_STORAGE_PREFIX}${sessionId}`);
  } catch {
    // Nothing to clean up when storage is unavailable.
  }
}

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

  // Deep-linkable replay mode: when the host view owns the URL (e.g. the
  // runtime sessions page), it opts in and the observer mirrors the active
  // mode into the "replay" query param so links land on the right tab.
  @property({ type: Boolean })
  syncModeToUrl = false;

  /** Optional override for the sidebar's no-sessions message. */
  @property({ type: String })
  emptyText = '';

  @state()
  private observedSessions: ObservedSession[] = [];

  @state()
  private activeSessionId: string | null = null;

  // Collapsed session list: once a session is selected the list gives its
  // grid column to the transcript and shrinks into a compact picker bar.
  // The operator's browse-vs-inspect intent drives it: browsing wants the
  // list, inspecting wants the real estate. Manually re-expanding pins the
  // list open until the next selection.
  @state()
  private sidebarCollapsed = false;

  // Transitional state while the sidebar column animates shut or open. An
  // instant swap read as "magical" — the list just vanished — so the column
  // visibly shrinks/grows to teach where it went. null when at rest.
  @state()
  private sidebarAnimating: 'collapsing' | 'expanding' | null = null;

  @state()
  private loadedEvents: Record<string, FlowGatewayEvent[]> = {};

  @state()
  private loadedReplayMetadata: Record<string, FlowGatewayEvent[]> = {};

  @state()
  private loadedEventPages: Record<string, EventPageState> = {};

  @state()
  private loadedActivity: Record<string, RuntimeSessionActivityItem[]> = {};

  @state()
  private loadedRequests: Record<string, RuntimeSessionRequestItem[]> = {};

  @state()
  private loadedRequestPages: Record<string, EventPageState> = {};

  @state()
  private requestsFailedOnly = false;

  @state()
  private requestsView = false;

  @state()
  private loadingRequestsForSessionId: string | null = null;

  @state()
  private requestFailedCount: Record<string, number> = {};

  // Whole-session prompt-cache rollup per session id. Sent with every requests
  // page (it covers the session, not the page), so the latest response wins.
  @state()
  private requestCacheSummary: Record<string, RuntimeSessionCacheSummary> = {};

  @state()
  private budgetDialogSession: ObservedSession | null = null;

  @state()
  private budgetDialogLimit = '50';

  @state()
  private budgetDialogPeriod = 'daily';

  @state()
  private budgetDialogBusy = false;

  @state()
  private budgetDialogResult: string | null = null;

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

  // Async analysis job state per session id: 'analyzing' while a job is
  // pending/running (submit + poll), 'failed' after a failed job until retry.
  @state()
  private optimizationJobStates: Record<string, 'analyzing' | 'failed'> = {};

  // Live poll timers per session id, cleared on completion/disconnect.
  private optimizationJobPollTimers: Record<string, number> = {};

  // Last submitted analysis options per session id so Retry re-runs the same
  // scope/model selection.
  private optimizationJobOptions: Record<
    string,
    OptimizationJobRequestOptions
  > = {};

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

  @state()
  private optimizeHintDismissed = readOptimizeHintDismissed();

  private unsubscribeRealtime?: () => void;
  private refreshTimer: number | null = null;
  private livePulseTimer: number | null = null;

  static styles = [
    reducedMotionStyles,
    css`
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

      /* Collapsed list: single column, transcript takes the full width and a
         compact picker bar replaces the sidebar. */
      .observer.sidebar-collapsed {
        grid-template-columns: minmax(0, 1fr);
      }

      /* Animated hand-off between list and picker bar: the sidebar column
         visibly shrinks shut (or grows open) before the DOM swap, so the
         list's disappearance reads as "it slid away" rather than a sudden
         vanish new users cannot place. Both endpoints are minmax() tracks
         because grid-track interpolation requires matching functions. */
      .observer.with-sidebar.sidebar-anim-closed {
        grid-template-columns: minmax(0px, 0px) minmax(0, 1fr);
      }

      .observer.with-sidebar .sidebar {
        min-width: 0;
        overflow: hidden;
      }

      .observer.with-sidebar.sidebar-anim-closed .sidebar {
        opacity: 0;
      }

      @media (prefers-reduced-motion: no-preference) {
        .observer.with-sidebar {
          transition: grid-template-columns 250ms ease;
        }

        .observer.with-sidebar .sidebar {
          transition: opacity 200ms ease;
        }

        /* The picker bar takes the stage right after the column closes; a
           short fade-in ties it to the just-departed list. */
        .session-picker-bar {
          animation: picker-bar-enter 200ms ease;
        }

        @keyframes picker-bar-enter {
          from {
            opacity: 0;
            transform: translateY(-4px);
          }
          to {
            opacity: 1;
            transform: translateY(0);
          }
        }
      }

      .session-picker-bar {
        align-items: center;
        border: 1px solid var(--sl-color-neutral-200);
        border-radius: var(--sl-border-radius-medium);
        background: var(--sl-color-neutral-0);
        display: flex;
        gap: var(--sl-spacing-small);
        margin-bottom: var(--sl-spacing-small);
        padding: var(--sl-spacing-x-small) var(--sl-spacing-small);
      }

      .session-picker-bar .picker-select {
        flex: 1;
        font: inherit;
        max-width: 480px;
        min-width: 0;
        overflow: hidden;
        text-overflow: ellipsis;
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

      /* Optimize first-use hint: a single-line info bar under the tab strip.
       Semantic info cyan left border (this IS information), 4px radius. */
      .optimize-hint {
        align-items: baseline;
        background: var(--sl-color-neutral-50);
        border: 1px solid var(--sl-color-neutral-200);
        border-left: 3px solid var(--sl-color-cyan-500, #30c9e8);
        border-radius: 4px;
        color: var(--sl-color-neutral-700);
        display: flex;
        font-size: var(--sl-font-size-small);
        gap: var(--sl-spacing-x-small);
        line-height: 1.5;
        padding: var(--sl-spacing-x-small) var(--sl-spacing-small);
      }

      .optimize-hint-body {
        flex: 1;
        min-width: 0;
      }

      .optimize-hint-link {
        color: var(--sl-color-primary-600);
        cursor: pointer;
        font-weight: var(--sl-font-weight-semibold);
        text-decoration: underline;
        white-space: nowrap;
      }

      .optimize-hint-dismiss {
        background: none;
        border: none;
        color: var(--sl-color-neutral-500);
        cursor: pointer;
        font-size: var(--sl-font-size-medium);
        line-height: 1;
        padding: 0 2px;
      }

      /* Entry motion: one 250ms ease-out fade + 2px rise on first render,
       inside the DESIGN.md budget and behind the reduced-motion guard. */
      @media (prefers-reduced-motion: no-preference) {
        .optimize-hint {
          animation: optimize-hint-enter 250ms ease-out;
        }

        @keyframes optimize-hint-enter {
          from {
            opacity: 0;
            transform: translateY(2px);
          }
          to {
            opacity: 1;
            transform: translateY(0);
          }
        }
      }

      @media (max-width: 950px) {
        .observer.with-sidebar {
          grid-template-columns: 1fr;
        }
      }
    `,
  ];

  private readonly handleInspectRequests = (event: Event): void => {
    const detail = (event as CustomEvent).detail || {};
    void this.openRequestsView({
      failedOnly: Boolean(detail.failedOnly),
      eventIds: detail.eventIds || [],
    });
  };

  private readonly handleCreateBudget = (): void => {
    this.budgetDialogResult = null;
    this.budgetDialogSession = this.activeSession;
  };

  connectedCallback(): void {
    super.connectedCallback();
    this.replayMode = this.replayModeFromUrl() ?? this.defaultReplayMode;
    // An invalid/unavailable ?replay= value (e.g. a stale optimize link with
    // the feature off) falls back to the default mode above — clean the
    // param up front so the URL never advertises a mode that is not active.
    this.syncReplayModeToUrl();
    this.connectRealtime();
    // These typed optimization actions bubble up from the replay panel (and its
    // dialogs). Listen on the host so the buttons actually do something.
    this.addEventListener(
      'session-inspect-requests',
      this.handleInspectRequests
    );
    this.addEventListener('session-create-budget', this.handleCreateBudget);
    void this.loadAIModels();
    void this.loadSessions();
  }

  disconnectedCallback(): void {
    super.disconnectedCallback();
    this.unsubscribeRealtime?.();
    this.removeEventListener(
      'session-inspect-requests',
      this.handleInspectRequests
    );
    this.removeEventListener('session-create-budget', this.handleCreateBudget);
    if (this.refreshTimer !== null) window.clearTimeout(this.refreshTimer);
    if (this.livePulseTimer !== null) window.clearTimeout(this.livePulseTimer);
    this.clearSidebarAnimationTimer();
    for (const sessionId of Object.keys(this.optimizationJobPollTimers)) {
      this.clearOptimizationJobPollTimer(sessionId);
    }
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
      if (this.followLive && this.replayMode !== 'conversation') {
        // Newest-first modes pin to the top. The conversation view is
        // oldest-first and pins itself to the bottom via its followLive prop.
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
    // Parent views often pass `.sessions` — reuse them and skip a second list
    // fetch (events still load on selection).
    if (this.sessions) {
      this.applySessions(this.sessions, options.preserveSelection ?? true);
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
      // Stagger heavy event/activity loads so the session list can paint first.
      requestAnimationFrame(() => {
        void this.selectSession(nextActive);
      });
    } else if (!nextActive) {
      this.activeSessionId = null;
    }
  }

  private sidebarAnimationTimer: number | null = null;

  private clearSidebarAnimationTimer(): void {
    if (this.sidebarAnimationTimer !== null) {
      window.clearTimeout(this.sidebarAnimationTimer);
      this.sidebarAnimationTimer = null;
    }
  }

  private prefersReducedMotion(): boolean {
    try {
      return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    } catch {
      return false;
    }
  }

  // Animated collapse: shrink the sidebar column shut first, THEN swap in the
  // compact picker bar. The staging teaches new users where the list went —
  // an instant swap read as the list simply vanishing. Reduced-motion users
  // get the immediate swap they asked for.
  private collapseSidebar(): void {
    if (this.sidebarCollapsed || this.hideSidebar) return;
    this.clearSidebarAnimationTimer();
    if (this.prefersReducedMotion()) {
      this.sidebarAnimating = null;
      this.sidebarCollapsed = true;
      return;
    }
    this.sidebarAnimating = 'collapsing';
    this.sidebarAnimationTimer = window.setTimeout(() => {
      this.sidebarAnimationTimer = null;
      this.sidebarAnimating = null;
      this.sidebarCollapsed = true;
    }, SIDEBAR_ANIMATION_MS);
  }

  // Animated expand: render the sidebar in its closed-column state first,
  // then release it next frame so the column visibly grows open.
  private expandSidebar(): void {
    if (!this.sidebarCollapsed) return;
    this.clearSidebarAnimationTimer();
    this.sidebarCollapsed = false;
    if (this.prefersReducedMotion()) {
      this.sidebarAnimating = null;
      return;
    }
    this.sidebarAnimating = 'expanding';
    // Two frames: one to paint the closed state, one to start the transition.
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        if (this.sidebarAnimating === 'expanding') {
          this.sidebarAnimating = null;
        }
      });
    });
  }

  private async selectSession(
    sessionId: string,
    options: { force?: boolean; userInitiated?: boolean } = {}
  ): Promise<void> {
    if (sessionId === this.activeSessionId && !options.force) {
      // Re-clicking the already-active session still expresses "inspect this"
      // intent, so it may collapse the list even though nothing reloads.
      if (options.userInitiated) this.collapseSidebar();
      return;
    }
    this.activeSessionId = sessionId;
    // Only a USER selection hands the list's column to the transcript.
    // Auto-selection on load must leave the list visible: the operator has
    // not chosen anything yet, and hiding the list would make the page feel
    // like a single-session view.
    if (options.userInitiated) this.collapseSidebar();
    // Deep links can land straight in optimize mode; resume any persisted
    // in-flight analysis job for the newly active session.
    if (this.replayMode === 'optimize') {
      this.maybeResumeOptimizationJob(sessionId);
    }
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

  private async loadSessionRequests(
    sessionId: string,
    options: { reset?: boolean; failedOnly?: boolean; eventIds?: string[] } = {}
  ): Promise<void> {
    if (this.loadingRequestsForSessionId === sessionId && !options.reset)
      return;
    const failedOnly = options.failedOnly ?? this.requestsFailedOnly;
    const existing = options.reset ? [] : this.loadedRequests[sessionId] || [];
    const offset = options.reset ? 0 : existing.length;
    this.loadingRequestsForSessionId = sessionId;
    try {
      const response = await getRuntimeSessionRequests(sessionId, {
        limit: EVENT_PAGE_SIZE,
        offset,
        failedOnly,
        eventIds: options.eventIds,
      });
      this.loadedRequests = {
        ...this.loadedRequests,
        [sessionId]: [...existing, ...(response.items || [])],
      };
      this.loadedRequestPages = {
        ...this.loadedRequestPages,
        [sessionId]: {
          nextOffset: response.next_offset,
          total: response.total,
          hasMore: response.has_more,
        },
      };
      this.requestFailedCount = {
        ...this.requestFailedCount,
        [sessionId]: response.failed_count,
      };
      if (response.cache_summary) {
        this.requestCacheSummary = {
          ...this.requestCacheSummary,
          [sessionId]: response.cache_summary,
        };
      }
    } catch (error) {
      console.error('Failed to load session requests:', error);
      this.error =
        error instanceof Error ? error.message : 'Failed to load requests';
    } finally {
      this.loadingRequestsForSessionId = null;
    }
  }

  private async openRequestsView(options: {
    failedOnly?: boolean;
    eventIds?: string[];
  }): Promise<void> {
    if (!this.activeSessionId) return;
    this.requestsFailedOnly = Boolean(options.failedOnly);
    this.requestsView = true;
    await this.loadSessionRequests(this.activeSessionId, {
      reset: true,
      failedOnly: options.failedOnly,
      eventIds: options.eventIds,
    });
  }

  private async setRequestsFailedOnly(failedOnly: boolean): Promise<void> {
    if (!this.activeSessionId) return;
    this.requestsFailedOnly = failedOnly;
    await this.loadSessionRequests(this.activeSessionId, {
      reset: true,
      failedOnly,
    });
  }

  private async submitBudgetForActiveSession(): Promise<void> {
    const session = this.budgetDialogSession;
    if (!session || this.budgetDialogBusy) return;
    const raw = (session.raw || {}) as Record<string, unknown>;
    const subjectType =
      (raw.runtime_principal_type as string) === 'managed_agents'
        ? 'managed_agents'
        : (raw.runtime_principal_type as string) || 'account';
    const subjectId = (raw.runtime_principal_id as string) || null;
    this.budgetDialogBusy = true;
    this.budgetDialogResult = null;
    try {
      await createBudgetPolicy({
        subject_type: subjectId ? subjectType : 'account',
        subject_id: subjectId,
        model_alias: null,
        period: this.budgetDialogPeriod,
        hard_limit_usd: Number(this.budgetDialogLimit) || 0,
        soft_limit_usd: null,
        notify_on_soft: false,
        notify_on_hard: true,
        notification_emails: null,
      });
      this.budgetDialogResult = 'Budget created for this agent.';
    } catch (error) {
      this.budgetDialogResult =
        error instanceof Error ? error.message : 'Failed to create budget';
    } finally {
      this.budgetDialogBusy = false;
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
      cacheOnly?: boolean;
    } = {}
  ): Promise<void> {
    if (!options.regenerate && this.loadedOptimizations[sessionId]) return;
    if (this.loadingOptimizationForSessionId === sessionId) return;
    this.loadingOptimizationForSessionId = sessionId;
    try {
      const optimization = await optimizeRuntimeSession(sessionId, options);
      // A cache-only miss means nothing was generated before — leave the
      // session unloaded so the panel shows the "generate" prompt instead of an
      // empty-suggestions state.
      if (optimization.cache_miss) return;
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

  private setOptimizationJobState(
    sessionId: string,
    state: 'analyzing' | 'failed' | null
  ): void {
    if (state === null) {
      const { [sessionId]: _dropped, ...rest } = this.optimizationJobStates;
      this.optimizationJobStates = rest;
      return;
    }
    this.optimizationJobStates = {
      ...this.optimizationJobStates,
      [sessionId]: state,
    };
  }

  private clearOptimizationJobPollTimer(sessionId: string): void {
    const timer = this.optimizationJobPollTimers[sessionId];
    if (timer !== undefined) {
      window.clearTimeout(timer);
      delete this.optimizationJobPollTimers[sessionId];
    }
  }

  /**
   * Submit the async analysis job for one session and start polling it.
   * The backend is idempotent for active jobs, so re-submitting (double
   * click, retry racing an active job) converges on the same run.
   */
  private async startOptimizationJob(
    sessionId: string,
    options: OptimizationJobRequestOptions = {}
  ): Promise<void> {
    if (this.optimizationJobStates[sessionId] === 'analyzing') return;
    this.optimizationJobOptions[sessionId] = options;
    this.setOptimizationJobState(sessionId, 'analyzing');
    try {
      const job = await submitRuntimeSessionOptimizationJob(sessionId, options);
      storeOptimizationJobId(sessionId, job.job_id);
      this.scheduleOptimizationJobPoll(sessionId, job.job_id, 0);
    } catch (error) {
      console.info('Failed to submit optimization job:', error);
      this.setOptimizationJobState(sessionId, 'failed');
    }
  }

  private scheduleOptimizationJobPoll(
    sessionId: string,
    jobId: string,
    delayMs: number = OPTIMIZE_JOB_POLL_INTERVAL_MS
  ): void {
    this.clearOptimizationJobPollTimer(sessionId);
    this.optimizationJobPollTimers[sessionId] = window.setTimeout(() => {
      void this.pollOptimizationJob(sessionId, jobId);
    }, delayMs);
  }

  private async pollOptimizationJob(
    sessionId: string,
    jobId: string
  ): Promise<void> {
    try {
      const job = await getRuntimeSessionOptimizationJob(sessionId, jobId);
      if (job.status === 'succeeded') {
        if (job.result) {
          this.loadedOptimizations = {
            ...this.loadedOptimizations,
            [sessionId]: job.result,
          };
        }
        clearStoredOptimizationJobId(sessionId);
        this.setOptimizationJobState(sessionId, null);
        return;
      }
      if (job.status === 'failed') {
        clearStoredOptimizationJobId(sessionId);
        this.setOptimizationJobState(sessionId, 'failed');
        return;
      }
    } catch (error) {
      // A 404 means the job is gone for good (pruned, or a stale stored id);
      // anything else is transient (network blip) and polling continues.
      if (error instanceof Error && error.message.includes('(404)')) {
        clearStoredOptimizationJobId(sessionId);
        this.setOptimizationJobState(sessionId, 'failed');
        return;
      }
      console.info('Optimization job poll failed; retrying:', error);
    }
    this.scheduleOptimizationJobPoll(sessionId, jobId);
  }

  /**
   * Reload-resume: when a persisted active job exists for this session,
   * resume polling it (rendering the analyzing state) instead of showing
   * the trigger controls again.
   */
  private maybeResumeOptimizationJob(sessionId: string | null): void {
    if (!sessionId || this.optimizationJobStates[sessionId]) return;
    const jobId = readStoredOptimizationJobId(sessionId);
    if (!jobId) return;
    this.setOptimizationJobState(sessionId, 'analyzing');
    this.scheduleOptimizationJobPoll(sessionId, jobId, 0);
  }

  /** Retry after a failed analysis: submit a fresh job with the same options. */
  private retryOptimizationJob(): void {
    if (!this.activeSessionId) return;
    const sessionId = this.activeSessionId;
    this.setOptimizationJobState(sessionId, null);
    void this.startOptimizationJob(
      sessionId,
      this.optimizationJobOptions[sessionId] || {}
    );
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
      { mode: 'conversation', label: 'Chat', icon: 'chat-left-text' },
      { mode: 'timeline', label: 'Transcript', icon: 'list-ul' },
      { mode: 'replay', label: 'Replay', icon: 'play-circle' },
    ];
    if (this.enabledFeatures.optimization) {
      tabs.push({ mode: 'optimize', label: 'Optimize', icon: 'magic' });
    }
    return tabs;
  }

  // Reads the deep-linked replay mode. Invalid values (or 'optimize' when the
  // feature is off) fall back to defaultReplayMode so a stale link cannot open
  // an unavailable tab.
  private replayModeFromUrl(): SessionReplayMode | null {
    if (!this.syncModeToUrl) return null;
    const raw = new URLSearchParams(window.location.search).get('replay');
    if (raw === 'conversation' || raw === 'timeline' || raw === 'replay') {
      return raw;
    }
    if (raw === 'optimize' && this.enabledFeatures.optimization) return raw;
    return null;
  }

  // Mirrors the active mode into the URL so the current tab survives reloads
  // and can be shared. replaceState only: mode switches are not navigation
  // steps, and pushState would pollute the back button.
  private syncReplayModeToUrl(): void {
    if (!this.syncModeToUrl) return;
    const url = new URL(window.location.href);
    if (this.replayMode === this.defaultReplayMode) {
      // The default mode needs no marker; dropping it keeps shared URLs clean.
      url.searchParams.delete('replay');
    } else {
      url.searchParams.set('replay', this.replayMode);
    }
    window.history.replaceState(window.history.state, '', url.toString());
  }

  private setReplayMode(mode: SessionReplayMode): void {
    if (this.replayMode === mode) return;
    this.replayMode = mode;
    this.syncReplayModeToUrl();
    if (mode === 'optimize') {
      // Opening the drawer retires the first-use hint for good.
      this.dismissOptimizeHint();
    }
    if (mode === 'replay' && this.activeSessionId) {
      void this.loadReplayMetadata(this.activeSessionId);
    }
    if (mode === 'optimize' && this.activeSessionId) {
      void this.loadOptimizationActions(this.activeSessionId);
      // A persisted in-flight job (e.g. page reload mid-analysis) resumes
      // polling and renders the analyzing state instead of the trigger.
      this.maybeResumeOptimizationJob(this.activeSessionId);
      // Surface previously generated suggestions on open (cache-only: no
      // generation on a miss).
      void this.loadSessionOptimization(this.activeSessionId, {
        cacheOnly: true,
      });
    }
  }

  private dismissOptimizeHint(): void {
    if (this.optimizeHintDismissed) return;
    this.optimizeHintDismissed = true;
    try {
      localStorage.setItem(OPTIMIZE_HINT_DISMISSED_KEY, 'true');
    } catch {
      // Privacy modes / missing storage: keep the in-memory dismiss only.
    }
  }

  /**
   * First-use hint for Optimize: a single info bar under the tab strip,
   * rendered once per user, only when the active session has real ledger
   * numbers to show (never invented) and the drawer was never opened.
   */
  private renderOptimizeHint() {
    if (this.optimizeHintDismissed) return nothing;
    if (!this.enabledFeatures.optimization) return nothing;
    if (this.replayMode === 'optimize') return nothing;
    const session = this.activeSession;
    if (!session || (session.totalRequests || 0) < 1) return nothing;
    const tokens = session.tokenUsage?.total_tokens || 0;
    if (tokens <= 0) return nothing;
    const cost = session.estimatedCost || 0;
    const ledger =
      cost > 0
        ? `This session used ${formatNumber(tokens)} tokens (${formatCost(cost)}).`
        : `This session used ${formatNumber(tokens)} tokens.`;
    return html`
      <div class="optimize-hint" role="note">
        <div class="optimize-hint-body">
          <strong>${ledger}</strong> Optimize finds where they went and suggests
          cuts — you verify each one by replaying the session, without touching
          your agent. →
          <a
            class="optimize-hint-link"
            @click=${() => this.setReplayMode('optimize')}
            >Try Optimize</a
          >
        </div>
        <button
          class="optimize-hint-dismiss"
          type="button"
          aria-label="Dismiss"
          title="Dismiss"
          @click=${() => this.dismissOptimizeHint()}
        >
          ×
        </button>
      </div>
    `;
  }

  /**
   * Copy for the replay panel when no session is selected. With zero sessions
   * this must be a bounded static state (never a spinner): on an agent's
   * detail page it explains the first gateway call will appear here; on the
   * account-wide explorer it invites selecting a session.
   */
  private get replayEmptyText(): string {
    if (this.scope === 'managed_agent' && this.observedSessions.length === 0) {
      return 'No sessions yet for this agent. Its first gateway call will appear here live.';
    }
    return 'Select a session to follow it live or replay it.';
  }

  /**
   * Chat-style transcript (P1 of the transcript redesign): only top-level
   * user prompts and final agent responses expanded; tool calls/results,
   * system and injected segments collapsed. Rendered ALONGSIDE the replay
   * panel (which is hidden, not unmounted, in this mode) so switching tabs
   * never loses the panel's expand/replay/optimize state.
   */
  private renderConversationView() {
    if (!this.activeSession) {
      return html`<div class="empty">${this.replayEmptyText}</div>`;
    }
    return html`
      <session-chat-view
        .events=${this.activeEvents}
        .activity=${this.activeActivity}
        .loading=${
          this.activeSessionId !== null &&
          this.loadingSessionId === this.activeSessionId
        }
        .hasMoreEvents=${this.activeEventPage?.hasMore ?? false}
        .loadingMoreEvents=${
          this.loadingMoreEventsForSessionId === this.activeSessionId
        }
        .followLive=${this.followLive}
        @session-events-page-requested=${() =>
          this.activeSessionId
            ? this.loadMoreEvents(this.activeSessionId)
            : undefined}
      ></session-chat-view>
    `;
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
            ${
              session
                ? html`
                    <span class="meta">
                      ${formatNumber(session.tokenUsage.total_tokens)} tokens ·
                      ${formatCost(session.estimatedCost)}
                    </span>
                  `
                : nothing
            }
          </div>
        </div>
        <div class="mode-row">
          ${
            this.enabledFeatures.liveFollow
              ? html`
                  <sl-button
                    size="small"
                    variant=${this.followLive ? 'primary' : 'default'}
                    @click=${() => (this.followLive = !this.followLive)}
                  >
                    ${this.followLive ? 'Pause follow' : 'Follow live'}
                  </sl-button>
                `
              : nothing
          }
          ${
            this.enabledFeatures.replayModes
              ? html`
                  <sl-button-group>
                    ${this.replayModeTabs.map(
                      (tab) => html`
                        <sl-button
                          size="small"
                          variant=${
                            this.replayMode === tab.mode ? 'primary' : 'default'
                          }
                          @click=${() => this.setReplayMode(tab.mode)}
                        >
                          ${
                            tab.icon
                              ? html`<sl-icon
                                  slot="prefix"
                                  name=${tab.icon}
                                ></sl-icon>`
                              : nothing
                          }
                          ${tab.label}
                        </sl-button>
                      `
                    )}
                  </sl-button-group>
                `
              : nothing
          }
          <sl-button
            size="small"
            variant=${this.requestsView ? 'primary' : 'default'}
            @click=${() => {
              if (this.requestsView) {
                this.requestsView = false;
              } else {
                void this.openRequestsView({
                  failedOnly: this.requestsFailedOnly,
                });
              }
            }}
          >
            <sl-icon slot="prefix" name="list-columns-reverse"></sl-icon>
            Requests
          </sl-button>
          <sl-button size="small" @click=${() => this.reloadActiveSession()}>
            Refresh
          </sl-button>
          ${
            this.enabledFeatures.endSession && session?.canLoadEvents
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
              : nothing
          }
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
        ${this.renderToolbar()} ${this.renderOptimizeHint()}
        ${
          this.error
            ? html`
                <sl-alert variant="danger" open>
                  <sl-icon slot="icon" name="exclamation-triangle"></sl-icon>
                  ${this.error}
                </sl-alert>
              `
            : nothing
        }
        ${
          this.requestsView
            ? html`
                <session-request-timeline
                  .requests=${
                    this.activeSessionId
                      ? this.loadedRequests[this.activeSessionId] || []
                      : []
                  }
                  .total=${
                    this.activeSessionId
                      ? (this.loadedRequestPages[this.activeSessionId]?.total ??
                        0)
                      : 0
                  }
                  .failedCount=${
                    this.activeSessionId
                      ? (this.requestFailedCount[this.activeSessionId] ?? 0)
                      : 0
                  }
                  .failedOnly=${this.requestsFailedOnly}
                  .cacheSummary=${
                    this.activeSessionId
                      ? this.requestCacheSummary[this.activeSessionId]
                      : undefined
                  }
                  .loading=${
                    this.loadingRequestsForSessionId === this.activeSessionId
                  }
                  .hasMore=${
                    this.activeSessionId
                      ? (this.loadedRequestPages[this.activeSessionId]
                          ?.hasMore ?? false)
                      : false
                  }
                  @request-timeline-failed-only=${(event: CustomEvent) =>
                    this.setRequestsFailedOnly(
                      Boolean(event.detail?.failedOnly)
                    )}
                  @request-timeline-load-more=${() =>
                    this.activeSessionId
                      ? this.loadSessionRequests(this.activeSessionId)
                      : undefined}
                ></session-request-timeline>
              `
            : nothing
        }
        ${
          this.budgetDialogSession
            ? html`
                <sl-dialog
                  label="Create budget for this agent"
                  open
                  @sl-after-hide=${() => {
                    this.budgetDialogSession = null;
                  }}
                >
                  <div>
                    Create a hard spend budget for
                    <strong>${this.budgetDialogSession.title}</strong>.
                  </div>
                  <div style="display:flex;gap:8px;margin-top:12px;">
                    <sl-input
                      type="number"
                      label="Hard limit (USD)"
                      .value=${this.budgetDialogLimit}
                      @sl-input=${(event: Event) => {
                        this.budgetDialogLimit = (
                          event.target as HTMLInputElement
                        ).value;
                      }}
                    ></sl-input>
                  </div>
                  ${
                    this.budgetDialogResult
                      ? html`<div style="margin-top:8px;">
                          ${this.budgetDialogResult}
                        </div>`
                      : nothing
                  }
                  <sl-button
                    slot="footer"
                    @click=${() => {
                      this.budgetDialogSession = null;
                    }}
                  >
                    Close
                  </sl-button>
                  <sl-button
                    slot="footer"
                    variant="primary"
                    ?loading=${this.budgetDialogBusy}
                    @click=${() => this.submitBudgetForActiveSession()}
                  >
                    Create budget
                  </sl-button>
                </sl-dialog>
              `
            : nothing
        }
        ${
          this.replayMode === 'conversation'
            ? this.renderConversationView()
            : nothing
        }
        <session-replay-panel
          style=${this.replayMode === 'conversation' ? 'display: none;' : ''}
          .session=${this.activeSession}
          .emptyText=${this.replayEmptyText}
          .events=${this.activeEvents}
          .timelineEvents=${
            this.activeSessionId
              ? this.loadedReplayMetadata[this.activeSessionId] || []
              : []
          }
          .activity=${this.activeActivity}
          .replayMode=${this.replayMode}
          .loading=${
            this.activeSessionId !== null &&
            this.loadingSessionId === this.activeSessionId
          }
          .rawPayloads=${this.enabledFeatures.rawPayloads}
          .eventDetails=${this.loadedEventDetails}
          .loadingEventDetails=${this.loadingEventDetails}
          .interactionSummaries=${this.loadedInteractionSummaries}
          .loadingInteractionSummaries=${this.loadingInteractionSummaries}
          .summarizeVisibleContent=${this.summarizeVisibleContent}
          .hasMoreEvents=${this.activeEventPage?.hasMore ?? false}
          .loadingMoreEvents=${
            this.loadingMoreEventsForSessionId === this.activeSessionId
          }
          .totalEvents=${this.activeEventPage?.total ?? null}
          .optimizationEnabled=${this.enabledFeatures.optimization}
          .availableModels=${this.aiModels}
          .optimizationResult=${
            this.activeSessionId
              ? this.loadedOptimizations[this.activeSessionId] || null
              : null
          }
          .optimizationSuggestions=${this.getActiveOptimizationSuggestions()}
          .optimizationAppliedActions=${
            this.activeSessionId
              ? this.loadedOptimizationActions[this.activeSessionId] || []
              : []
          }
          .applyingOptimizationSuggestionId=${
            this.applyingOptimizationSuggestionId
          }
          .loadingOptimization=${
            this.loadingOptimizationForSessionId === this.activeSessionId
          }
          .optimizationJobState=${
            this.activeSessionId
              ? this.optimizationJobStates[this.activeSessionId] || null
              : null
          }
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
            // Generation is async now: submit a background job and poll it
            // (the UI shows the analyzing state meanwhile).
            void this.startOptimizationJob(this.activeSessionId, {
              regenerate: Boolean(event.detail?.regenerate),
              modelId: event.detail?.modelId || null,
              eventIds: event.detail?.eventIds || [],
              sourceKinds: event.detail?.sourceKinds || [],
              fromIndex: event.detail?.fromIndex,
              toIndex: event.detail?.toIndex,
            });
            this.loadOptimizationActions(this.activeSessionId);
          }}
          @session-optimization-retry=${() => this.retryOptimizationJob()}
          @session-optimization-apply=${(event: CustomEvent) =>
            this.applyOptimizationSuggestion(event.detail)}
          @session-optimization-actions-requested=${() =>
            this.activeSessionId
              ? this.loadOptimizationActions(this.activeSessionId)
              : undefined}
        ></session-replay-panel>
      </div>
    `;

    const listCollapsed =
      !this.hideSidebar && this.sidebarCollapsed && Boolean(this.activeSession);
    // While animating, the sidebar DOM stays mounted and only the column
    // width transitions; sidebar-anim-closed is the closed endpoint for both
    // directions (collapsing adds it, expanding starts from it and drops it).
    const animClosed =
      this.sidebarAnimating === 'collapsing' ||
      this.sidebarAnimating === 'expanding';
    const observerClass = this.hideSidebar
      ? 'observer'
      : listCollapsed
        ? 'observer sidebar-collapsed'
        : `observer with-sidebar${animClosed ? ' sidebar-anim-closed' : ''}`;
    return html`
      <div
        class=${observerClass}
        style=${
          this.layout === 'full' ? 'min-height: 720px;' : 'min-height: 520px;'
        }
      >
        ${
          this.hideSidebar
            ? nothing
            : listCollapsed
              ? html`
                  <div class="session-picker-bar">
                    <sl-icon-button
                      name="layout-sidebar"
                      label="Show session list"
                      title="Show session list"
                      @click=${() => this.expandSidebar()}
                    ></sl-icon-button>
                    <select
                      class="picker-select"
                      aria-label="Switch session"
                      .value=${this.activeSessionId || ''}
                      @change=${(event: Event) => {
                        const sessionId = (event.target as HTMLSelectElement)
                          .value;
                        if (sessionId)
                          void this.selectSession(sessionId, {
                            userInitiated: true,
                          });
                      }}
                    >
                      ${this.observedSessions.map(
                        (session) => html`
                          <option
                            value=${session.id}
                            ?selected=${session.id === this.activeSessionId}
                          >
                            ${session.title}${
                              session.subtitle ? ` — ${session.subtitle}` : ''
                            }
                          </option>
                        `
                      )}
                    </select>
                    <span class="meta">
                      ${this.observedSessions.length}
                      session${this.observedSessions.length === 1 ? '' : 's'}
                    </span>
                  </div>
                `
              : html`
                  <div class="sidebar">
                    <sl-input
                      placeholder="Search sessions"
                      clearable
                      .value=${this.searchQuery}
                      @sl-input=${(event: Event) => {
                        this.searchQuery = (
                          event.target as HTMLInputElement
                        ).value;
                      }}
                      style="margin-bottom: var(--sl-spacing-small);"
                    >
                      <sl-icon name="search" slot="prefix"></sl-icon>
                    </sl-input>
                    <session-list-panel
                      .sessions=${this.filteredSessions}
                      .activeSessionId=${this.activeSessionId}
                      .emptyText=${
                        this.observedSessions.length === 0 ? this.emptyText : ''
                      }
                      @session-selected=${(event: CustomEvent) =>
                        this.selectSession(event.detail.sessionId, {
                          userInitiated: true,
                        })}
                    ></session-list-panel>
                  </div>
                `
        }
        ${content}
      </div>
    `;
  }
}
