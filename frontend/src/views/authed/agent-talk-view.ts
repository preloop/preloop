import { ConsoleStatus } from '../../controllers/console-status';
import { parseUTCDate } from '../../utils/date';
/**
 * The talk page: one agent, one session, one composer.
 *
 * This is the whole reason wave 5 exists. Talking to an agent used to happen
 * in a dialog stacked on top of whatever page the operator was reading, which
 * meant the conversation was the smallest thing on screen and disappeared the
 * moment anything else needed attention. A route can be a popup window, a tab
 * or a phone page, and in all three the layout is the same three rows: who you
 * are talking to, what has been said, and the box you type in.
 *
 * Layout rules that are load bearing:
 * - The page never scrolls. The thread scrolls inside itself, so the composer
 *   stays where the operator's hands are.
 * - `100dvh`, not `100vh`: mobile browsers shrink the visual viewport when the
 *   keyboard opens, and `vh` would push the composer under it.
 */
import { LitElement, css, html, nothing, unsafeCSS } from 'lit';
import { customElement, query, state } from 'lit/decorators.js';

import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import '../../components/session-chat-view';
import '../../components/talk-composer';
import '../../components/talk-session-picker';
import {
  TALK_SESSION_NEW_EVENT,
  TALK_SESSION_SELECT_EVENT,
} from '../../components/talk-session-picker';

import {
  getAccountAgent,
  getAccountRuntimeSessionActivityTimeline,
  getRuntimeSessionGatewayEvents,
  listApprovalRequests,
} from '../../api';
import {
  ConnectionState,
  unifiedWebSocketManager,
} from '../../services/unified-websocket-manager';
import consoleStyles from '../../styles/console-styles.css?inline';
import type {
  AgentControlCommandResponse,
  FlowGatewayEvent,
  ManagedAgentSummary,
  RuntimeSessionActivityItem,
  RuntimeSessionSummary,
} from '../../types';
import {
  getAgentStatusChip,
  type AgentStatusChip,
} from '../../utils/agent-display';
import { agentStateChip, turnInfoFromSession } from '../../utils/agent-status';

/** Agent chips that say what it is doing, as opposed to its lifecycle. */
const ACTIVITY_CHIP_LABELS = new Set([
  'Working',
  'Needs approval',
  'Your turn',
  'Idle',
  'Ended',
]);
import { renderAgentIcon } from '../../utils/agent-icons';
import { normalizeObservedSession } from '../../utils/session-observer';
import {
  TALK_HEARTBEAT_MS,
  openTalkChannel,
  postTalkChannelMessage,
} from '../../utils/talk-channel';
import {
  saveTalkWindowGeometry,
  talkRoutePath,
  talkWindowTitle,
} from '../../utils/talk-window';
import type { PendingTalkMessage } from '../../components/session-chat-view';
import { TALK_RETRY_EVENT } from '../../components/session-chat-view';
import type { TalkComposer } from '../../components/talk-composer';
import '../../components/artifact-image-viewer';
import {
  browserStepKey,
  browserStepViewerImages,
  sortBrowserSteps,
} from '../../utils/session-artifacts';
import {
  TALK_MESSAGE_SENT_EVENT,
  TALK_NEW_SESSION_EVENT,
  TALK_PENDING_CHANGED_EVENT,
  TALK_SWITCH_SESSION_EVENT,
} from '../../components/talk-composer';

const EVENT_PAGE_SIZE = 50;
const TURN_HISTORY_WINDOW = 40;

@customElement('agent-talk-view')
export class AgentTalkView extends LitElement {
  private readonly accessibilityStatus = new ConsoleStatus(this);
  @state() private agentId = '';
  @state() private agent: ManagedAgentSummary | null = null;
  @state() private sessions: RuntimeSessionSummary[] = [];
  @state() private sessionId: string | null = null;
  /** Browser step shown full size in the viewer, or -1. */
  @state() private browserStepViewerIndex = -1;
  /**
   * A session named in the URL. Only the first resolution reads it: after
   * that every window is pinned to whatever session it shows, and only the
   * operator moves it (picker, notice, ended-session guard).
   */
  @state() private pinnedSessionId: string | null = null;
  /**
   * Set by New session: the window waits for the session its next send
   * creates and opens that one, instead of resolving the most recent.
   */
  @state() private awaitingNewSession = false;
  /** Set once the window has resolved its session; nothing re-picks after. */
  private sessionResolved = false;
  /** Bumped on every switch so late responses for an old session are dropped. */
  private sessionGeneration = 0;
  /** Another session of this agent that reported activity, offered not followed. */
  @state() private noticeSessionId: string | null = null;
  @state() private windowMode = false;
  /** The entry point that opened this window, for `requested_from`. */
  @state() private sourceContext: string | null = null;
  @state() private loading = true;
  @state() private loadingEvents = false;
  @state() private loadingMoreEvents = false;
  @state() private hasMoreEvents = false;
  @state() private error: string | null = null;
  /** The agent or the open session's conversation could not be loaded. */
  @state() private sessionLoadFailed = false;
  @state() private events: FlowGatewayEvent[] = [];
  @state() private activity: RuntimeSessionActivityItem[] = [];
  @state() private pending: PendingTalkMessage[] = [];
  /** Pending approvals for the followed session only, never account-wide. */
  @state()
  private pendingApprovals: Array<{
    id: string;
    status: string;
    requested_at?: string;
  }> = [];
  /** Socket health; null until the manager reports its first state. */
  @state() private realtimeConnected: boolean | null = null;

  @query('talk-composer') private composer!: TalkComposer;

  private previousDocumentTitle = document.title;
  private nextEventOffset: number | null = null;
  private unsubscribeRealtime: (() => void) | null = null;
  private unsubscribeRealtimeState: (() => void) | null = null;
  private channel: BroadcastChannel | null = null;
  private heartbeatTimer: number | null = null;
  private reloadTimer: number | null = null;
  /**
   * The open turn's reply as it streams in (`session_progress` deltas).
   * Cleared once the stored reply is read after the turn ends.
   */
  @state() private liveReply = '';
  private liveItemId: string | null = null;
  private liveTurnEnded = false;
  /** Last `seq` seen for the open session; a jump means a missed event. */
  private lastProgressSeq: number | null = null;
  private geometryTimer: number | null = null;

  static styles = [
    unsafeCSS(consoleStyles),
    css`
      :host {
        display: flex;
        flex-direction: column;
        /* dvh, not vh: the phone keyboard shrinks the visual viewport. */
        height: 100dvh;
        max-height: 100dvh;
        min-height: 0;
        overflow: hidden;
        width: 100%;
      }

      .talk-header {
        align-items: center;
        background: var(--console-surface);
        border-bottom: 1px solid var(--console-hairline);
        display: flex;
        flex-shrink: 0;
        gap: var(--sl-spacing-small);
        padding: var(--sl-spacing-small) var(--sl-spacing-medium);
        padding-top: calc(
          var(--sl-spacing-small) + env(safe-area-inset-top, 0px)
        );
      }

      .avatar {
        align-items: center;
        background: var(--console-page);
        border: 1px solid var(--console-hairline);
        border-radius: 8px;
        display: flex;
        flex-shrink: 0;
        font-size: 18px;
        height: 34px;
        justify-content: center;
        width: 34px;
      }

      .identity {
        display: flex;
        flex-direction: column;
        gap: 2px;
        min-width: 0;
      }

      .name-row {
        align-items: center;
        display: flex;
        gap: var(--sl-spacing-x-small);
        min-width: 0;
      }

      .name {
        margin: 0;
        font-size: inherit;
        font-weight: var(--sl-font-weight-semibold);
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }

      .subject {
        color: var(--console-meta-color);
        font-size: var(--console-text-meta);
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }

      .spacer {
        flex: 1;
      }

      .open-console {
        color: var(--console-link-color);
        font-size: var(--console-text-meta);
        white-space: nowrap;
      }

      .talk-error {
        background: var(--console-surface);
        border-bottom: 1px solid var(--console-hairline);
        color: var(--sl-color-danger-700);
        flex-shrink: 0;
        font-size: var(--console-text-meta);
        padding: var(--sl-spacing-2x-small) var(--sl-spacing-medium);
      }

      .session-notice button {
        background: none;
        border: 0;
        color: var(--console-link-color);
        cursor: pointer;
        font: inherit;
        padding: 0;
        text-decoration: underline;
      }

      talk-session-picker .talk-session-option-title {
        display: block;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }

      talk-session-picker .talk-session-option-meta {
        color: var(--console-meta-color);
        display: flex;
        font-size: var(--console-text-meta);
        gap: var(--sl-spacing-x-small);
      }

      talk-session-picker .talk-session-live {
        color: var(--sl-color-success-700);
        font-weight: var(--sl-font-weight-semibold);
      }

      .session-notice.has-notice {
        background: var(--console-surface-raised, var(--sl-color-neutral-50));
        border-bottom: 1px solid var(--console-hairline);
        color: var(--console-meta-color);
        flex-shrink: 0;
        font-size: var(--console-text-meta);
        padding: var(--sl-spacing-2x-small) var(--sl-spacing-medium);
      }

      session-chat-view {
        flex: 1;
        min-height: 0;
      }

      talk-composer {
        flex-shrink: 0;
      }

      .state {
        align-items: center;
        color: var(--console-meta-color);
        display: flex;
        flex: 1;
        flex-direction: column;
        gap: var(--sl-spacing-small);
        justify-content: center;
        padding: var(--sl-spacing-x-large);
        text-align: center;
      }
    `,
  ];

  onBeforeEnter(location: {
    params: { agentId?: string };
    search?: string;
  }): void {
    this.agentId = location.params.agentId ?? '';
    const search = new URLSearchParams(
      location.search ?? window.location.search
    );
    this.pinnedSessionId = search.get('session');
    this.sessionId = this.pinnedSessionId;
    this.windowMode = search.get('window') === '1';
    this.sourceContext = search.get('source');
  }

  /**
   * What the composer reports as `requested_from`: the entry point that opened
   * this window when there was one, otherwise the shape of the page itself.
   */
  private get composerSourceContext(): string {
    if (this.sourceContext) return this.sourceContext;
    return this.windowMode ? 'talk-window' : 'talk-page';
  }

  connectedCallback(): void {
    super.connectedCallback();
    // A talk page is the whole viewport whether it is a popup or a phone.
    this.dispatchEvent(
      new CustomEvent('request-full-bleed', {
        detail: true,
        bubbles: true,
        composed: true,
      })
    );
    void this.load();
    this.connectRealtime();
    if (this.windowMode) {
      // DESIGN "Awareness lives in the header": one chip per open talk
      // *window*. The in-page form is the page the operator is reading, so a
      // chip for it would point at the current tab and say nothing.
      this.openChannel();
      // The operator who drags this window to the right size and place has
      // said where they want it; the next open should land there.
      window.addEventListener('resize', this.handleWindowResize);
      window.addEventListener('beforeunload', this.persistGeometry);
    }
  }

  private handleWindowResize = (): void => {
    if (this.geometryTimer !== null) window.clearTimeout(this.geometryTimer);
    // Resize fires per frame while dragging; one write at the end is enough.
    this.geometryTimer = window.setTimeout(() => {
      this.geometryTimer = null;
      this.persistGeometry();
    }, 400);
  };

  private persistGeometry = (): void => {
    if (!this.windowMode || !this.agentId) return;
    saveTalkWindowGeometry(this.agentId, {
      width: window.outerWidth,
      height: window.outerHeight,
      left: window.screenX,
      top: window.screenY,
    });
  };

  disconnectedCallback(): void {
    this.sessionGeneration += 1;
    this.eventsAbort?.abort();
    this.eventsRequest = null;
    this.eventsAbort = null;
    this.eventsRefreshQueued = false;
    this.eventsMoreQueued = false;
    this.unsubscribeRealtime?.();
    this.unsubscribeRealtime = null;
    if (this.heartbeatTimer !== null) window.clearInterval(this.heartbeatTimer);
    if (this.reloadTimer !== null) window.clearTimeout(this.reloadTimer);
    this.reloadTimer = null;
    this.postChannel('close');
    this.channel?.close();
    this.channel = null;
    window.removeEventListener('pagehide', this.handlePageHide);
    window.removeEventListener('resize', this.handleWindowResize);
    window.removeEventListener('beforeunload', this.persistGeometry);
    if (this.geometryTimer !== null) window.clearTimeout(this.geometryTimer);
    document.title = this.previousDocumentTitle;
    super.disconnectedCallback();
  }

  private handlePageHide = (): void => {
    this.postChannel('close');
  };

  // -- awareness -----------------------------------------------------------

  private openChannel(): void {
    this.channel = openTalkChannel();
    if (!this.channel) return;
    window.addEventListener('pagehide', this.handlePageHide);
    // The first beat announces the window; the rest keep it from being pruned
    // when the window is left open with nothing being typed.
    this.heartbeatTimer = window.setInterval(
      () => this.postChannel('open'),
      TALK_HEARTBEAT_MS
    );
  }

  /**
   * The unread dot means "the agent said something you have not seen", so it
   * is posted from realtime activity, never from the operator's own send, and
   * only while this window is not the one being looked at. `document.hidden`
   * covers a minimised or backgrounded window, `hasFocus()` the far more
   * common case of a visible popup sitting behind the console.
   */
  private isUnattended(): boolean {
    if (document.hidden) return true;
    return typeof document.hasFocus === 'function'
      ? !document.hasFocus()
      : false;
  }

  private noteAgentTurn(): void {
    if (!this.isUnattended()) return;
    this.postChannel('message');
  }

  private postChannel(type: 'open' | 'message' | 'close'): void {
    if (!this.channel || !this.agentId) return;
    postTalkChannelMessage(this.channel, {
      type,
      agentId: this.agentId,
      agentName: this.agent?.display_name || this.agentId,
      sessionId: this.sessionId,
      at: Date.now(),
    });
  }

  // -- data ----------------------------------------------------------------

  private get sessionSubject(): string | null {
    return this.subjectOf(this.sessionId);
  }

  private subjectOf(sessionId: string | null): string | null {
    const session = this.sessions.find((item) => item.id === sessionId);
    if (!session) return null;
    return normalizeObservedSession(session).title;
  }

  private get currentSession(): RuntimeSessionSummary | undefined {
    return this.sessions.find((session) => session.id === this.sessionId);
  }

  /**
   * Lifecycle and onboarding chips come from the agent; what it is doing
   * comes from the session this window is on (D35).
   */
  private headerChip(agent: ManagedAgentSummary): AgentStatusChip {
    const chip = getAgentStatusChip(agent);
    const session = this.currentSession;
    if (!session || !ACTIVITY_CHIP_LABELS.has(chip.label)) return chip;
    return agentStateChip(session);
  }

  /** The live session the ended-session guard offers, newest first. */
  private get liveAlternative(): { id: string; subject: string } | null {
    const live = this.sessions
      .filter(
        (session) =>
          session.id !== this.sessionId &&
          !session.ended_at &&
          session.is_active_now
      )
      .sort((left, right) =>
        (right.last_activity_at || right.started_at || '').localeCompare(
          left.last_activity_at || left.started_at || ''
        )
      )[0];
    if (!live) return null;
    return { id: live.id, subject: normalizeObservedSession(live).title };
  }

  /**
   * The window title is how an operator finds the right popup in a stack of
   * them, so it names the agent and the session, not the product.
   */
  private syncDocumentTitle(): void {
    document.title = talkWindowTitle(
      this.agent?.display_name || 'Agent',
      this.sessionSubject
    );
  }

  private pickSession(sessions: RuntimeSessionSummary[]): string | null {
    if (this.pinnedSessionId) return this.pinnedSessionId;
    const ranked = [...sessions].sort((left, right) => {
      if (left.is_active_now !== right.is_active_now) {
        return left.is_active_now ? -1 : 1;
      }
      const leftAt = left.last_activity_at || left.started_at || '';
      const rightAt = right.last_activity_at || right.started_at || '';
      return rightAt.localeCompare(leftAt);
    });
    return ranked[0]?.id ?? null;
  }

  /**
   * Read the agent and its sessions, then the open session's thread.
   *
   * The session is resolved once: an unpinned open takes the most recently
   * active session and from then on the window stays there. Reloads refresh
   * the session list (for the picker and the notice) without re-picking.
   */
  private async load(): Promise<void> {
    if (!this.agentId) return;
    this.loading = !this.agent;
    this.error = null;
    const generation = this.sessionGeneration;
    try {
      // Sessions only: usage by model and activity are for the agent page
      // and made every reload of a talk window several times heavier.
      const [detail, sessionPage] = await Promise.all([
        getAccountAgent(this.agentId, { include: [], lightweight: true }),
        getAccountAgent(this.agentId, {
          include: ['sessions'],
          lightweight: true,
        }),
      ]);
      if (generation !== this.sessionGeneration) return;
      this.agent = detail.agent;
      this.sessions = sessionPage.sessions || [];
      if (!this.sessionResolved) {
        this.sessionResolved = true;
        if (!this.sessionId && !this.awaitingNewSession) {
          this.sessionId = this.pickSession(this.sessions);
        }
      }
      this.syncDocumentTitle();
      this.postChannel('open');
      if (this.sessionId) await this.loadEvents(this.sessionId);
    } catch (error) {
      if (generation !== this.sessionGeneration) return;
      this.sessionLoadFailed = true;
      this.error =
        error instanceof Error ? error.message : 'Failed to load agent';
    } finally {
      if (generation === this.sessionGeneration) this.loading = false;
    }
  }

  /**
   * Echoed operator turns in the thread, by command id: a sent bubble is
   * dropped only once its own echo is on screen.
   */
  private echoedCommandIds(
    activity: RuntimeSessionActivityItem[]
  ): Set<string> {
    const ids = new Set<string>();
    for (const item of activity) {
      const metadata = (item.metadata || {}) as Record<string, unknown>;
      if (typeof metadata.command_id === 'string') ids.add(metadata.command_id);
      // A delivered note is echoed by its note id.
      if (typeof metadata.note_id === 'string') ids.add(metadata.note_id);
    }
    return ids;
  }

  private eventsRequest: Promise<void> | null = null;
  private eventsAbort: AbortController | null = null;
  private eventsRefreshQueued = false;
  @state() private eventsMoreQueued = false;

  private async loadEvents(sessionId: string): Promise<void> {
    if (this.eventsRequest) {
      this.eventsRefreshQueued = true;
      await this.eventsRequest;
      return;
    }
    await this.runEventsRead(() => this.readEvents(sessionId));
  }

  /** Serialize first/older pages; a burst requests one trailing current page. */
  private async runEventsRead(read: () => Promise<void>): Promise<void> {
    this.eventsAbort = new AbortController();
    const request = read();
    this.eventsRequest = request;
    try {
      await request;
    } finally {
      if (this.eventsRequest !== request) return;
      this.eventsRequest = null;
      this.eventsAbort = null;
      if (this.eventsMoreQueued) {
        this.eventsMoreQueued = false;
        if (this.isConnected && this.sessionId) void this.loadMoreEvents();
      } else if (this.eventsRefreshQueued) {
        this.eventsRefreshQueued = false;
        if (this.isConnected && this.sessionId)
          void this.loadEvents(this.sessionId);
      }
    }
  }

  private async readEvents(sessionId: string): Promise<void> {
    const generation = this.sessionGeneration;
    this.loadingEvents = true;
    // Approvals are read with the transcript, not after it: whether the agent
    // is blocked is the first thing an operator looks for on a live session.
    void this.loadApprovals();
    try {
      const [events, activity] = await Promise.all([
        getRuntimeSessionGatewayEvents(sessionId, {
          limit: EVENT_PAGE_SIZE,
          offset: 0,
          history: 'delta',
          historyWindow: TURN_HISTORY_WINDOW,
          signal: this.eventsAbort?.signal,
        }),
        getAccountRuntimeSessionActivityTimeline(sessionId).catch(() => ({
          items: [] as RuntimeSessionActivityItem[],
        })),
      ]);
      if (
        sessionId !== this.sessionId ||
        generation !== this.sessionGeneration
      ) {
        return;
      }
      // Keep older pages the operator already opened when the live page moves.
      const byId = new Map(this.events.map((event) => [event.id, event]));
      for (const event of events.logs || []) byId.set(event.id, event);
      this.events = this.sortEvents(Array.from(byId.values()));
      this.activity = activity.items || [];
      this.hasMoreEvents =
        events.pagination?.has_more ??
        (events.logs || []).length >= EVENT_PAGE_SIZE;
      // Resume from the fresh page's cursor even when older pages remain
      // mounted. A burst can add more than one page; retained row count is
      // not an offset and would skip the gap between new and retained rows.
      this.nextEventOffset =
        events.pagination?.next_offset ?? (events.logs || []).length;
      // Anything the server has now echoed back is in the thread already.
      this.composer?.clearSentPending(this.echoedCommandIds(this.activity));
      // The ended turn's reply is now in the stored thread.
      if (this.liveTurnEnded) this.resetLiveReply();
      this.sessionLoadFailed = false;
    } catch (error) {
      if (generation !== this.sessionGeneration) return;
      this.sessionLoadFailed = true;
      this.error =
        error instanceof Error ? error.message : 'Failed to load conversation';
    } finally {
      if (generation === this.sessionGeneration) this.loadingEvents = false;
    }
  }

  private async loadMoreEvents(): Promise<void> {
    if (this.loadingMoreEvents) return;
    if (this.eventsRequest) {
      this.eventsMoreQueued = true;
      return;
    }
    await this.runEventsRead(() => this.readMoreEvents());
  }

  private async readMoreEvents(): Promise<void> {
    if (
      !this.sessionId ||
      this.loadingMoreEvents ||
      !this.hasMoreEvents ||
      this.nextEventOffset === null
    ) {
      return;
    }
    this.loadingMoreEvents = true;
    const sessionId = this.sessionId;
    const generation = this.sessionGeneration;
    try {
      const page = await getRuntimeSessionGatewayEvents(sessionId, {
        limit: EVENT_PAGE_SIZE,
        offset: this.nextEventOffset,
        history: 'delta',
        historyWindow: TURN_HISTORY_WINDOW,
        signal: this.eventsAbort?.signal,
      });
      // An older page of a session the operator has left is not this thread.
      if (
        sessionId !== this.sessionId ||
        generation !== this.sessionGeneration
      ) {
        return;
      }
      const byId = new Map(this.events.map((event) => [event.id, event]));
      for (const event of page.logs || []) byId.set(event.id, event);
      this.events = this.sortEvents(Array.from(byId.values()));
      this.hasMoreEvents = page.pagination?.has_more ?? false;
      this.nextEventOffset = page.pagination?.next_offset ?? null;
    } catch (error) {
      if (generation !== this.sessionGeneration) return;
      this.error =
        error instanceof Error ? error.message : 'Failed to load more events';
    } finally {
      if (generation === this.sessionGeneration) this.loadingMoreEvents = false;
    }
  }

  private sortEvents(events: FlowGatewayEvent[]): FlowGatewayEvent[] {
    return [...events].sort(
      (left, right) =>
        parseUTCDate(right.timestamp || '1970-01-01T00:00:00Z').getTime() -
        parseUTCDate(left.timestamp || '1970-01-01T00:00:00Z').getTime()
    );
  }

  private connectRealtime(): void {
    const unsubscribers = [
      unifiedWebSocketManager.subscribe('gateway_activity', (message) =>
        this.handleActivity(message)
      ),
      unifiedWebSocketManager.subscribe('runtime_sessions', (message) =>
        this.handleActivity(message)
      ),
      unifiedWebSocketManager.subscribe('agent_control', (message) =>
        this.handleActivity(message)
      ),
      // Live reply deltas: appended to the open turn, never a refetch.
      unifiedWebSocketManager.subscribe('session_progress', (message) =>
        this.handleSessionProgress(message)
      ),
      // Approvals have their own topic: a session blocked on a decision emits
      // no gateway or session traffic at all, so without this the operator is
      // not told until they happen to reload.
      unifiedWebSocketManager.subscribe('approvals', (message) =>
        this.handleApprovalActivity(message)
      ),
    ];
    this.unsubscribeRealtime = () => {
      for (const unsubscribe of unsubscribers) unsubscribe();
      this.unsubscribeRealtimeState?.();
      this.unsubscribeRealtimeState = null;
    };
    this.unsubscribeRealtimeState = unifiedWebSocketManager.onStateChange(
      (state) => {
        const connected = state === ConnectionState.CONNECTED;
        // Whatever happened while the socket was down was not pushed here, so
        // coming back reads the open session again instead of trusting it.
        const reconnected = connected && this.realtimeConnected === false;
        this.realtimeConnected = connected;
        if (reconnected) void this.load();
      }
    );
    this.realtimeConnected =
      unifiedWebSocketManager.getState() === ConnectionState.CONNECTED;
    void unifiedWebSocketManager.connect();
  }

  private handleActivity(message: {
    payload?: Record<string, unknown>;
    managed_agent_id?: string;
    agent_id?: string;
    runtime_session_id?: string;
  }): void {
    const payload = message?.payload ?? {};
    const agentId =
      (payload.managed_agent_id as string) ||
      (payload.agent_id as string) ||
      message.managed_agent_id ||
      message.agent_id;
    if (agentId && agentId !== this.agentId) return;
    const sessionId = (payload.runtime_session_id ??
      message.runtime_session_id) as string | undefined;
    if (!agentId && !sessionId) return;
    if (!agentId && sessionId && sessionId !== this.sessionId) return;
    if (sessionId && sessionId !== this.sessionId && agentId === this.agentId) {
      // Any other session of the same agent is offered, never followed, also
      // while New session waits for its first send (only that send's own
      // response opens the session it created): an
      // agent often runs several at once, and following each one's traffic
      // flipped the window between them and sent turns to the wrong one.
      this.offerSession(sessionId);
      return;
    }
    // The composer marks a note read, or re-reads how this session is routed.
    this.composer?.handleRealtime(message);
    this.noteAgentTurn();
    this.scheduleReload();
  }

  /**
   * One `session_progress` event. Only the open session's events count;
   * the server already scopes them to this account. Reply deltas append to
   * the live bubble; a gap in `seq` (a throttled or lost event) or the end
   * of the turn re-reads the stored thread instead. Sidecars that send no
   * progress keep the reload-on-activity path above.
   */
  private handleSessionProgress(message: {
    payload?: Record<string, unknown>;
    managed_agent_id?: string;
    runtime_session_id?: string;
  }): void {
    const payload = message?.payload ?? {};
    const sessionId =
      (payload.runtime_session_id as string | undefined) ??
      message?.runtime_session_id;
    if (!sessionId || sessionId !== this.sessionId) return;
    if (message.managed_agent_id && message.managed_agent_id !== this.agentId)
      return;
    const seq = typeof payload.seq === 'number' ? payload.seq : null;
    if (seq !== null) {
      const gap =
        this.lastProgressSeq !== null && seq !== this.lastProgressSeq + 1;
      this.lastProgressSeq = seq;
      if (gap) {
        // Text is missing in the middle: the stored thread is the truth.
        this.resetLiveReply();
        this.scheduleReload();
        return;
      }
    }
    switch (payload.kind) {
      case 'turn_started':
        this.resetLiveReply();
        return;
      case 'assistant_delta': {
        const delta = typeof payload.delta === 'string' ? payload.delta : '';
        if (!delta) return;
        if (this.liveTurnEnded) this.resetLiveReply();
        const itemId =
          typeof payload.item_id === 'string' ? payload.item_id : null;
        const separator =
          this.liveReply && itemId !== this.liveItemId ? '\n\n' : '';
        this.liveItemId = itemId;
        this.liveReply = `${this.liveReply}${separator}${delta}`.slice(
          -20000
        );
        this.noteAgentTurn();
        return;
      }
      case 'turn_completed':
        this.liveTurnEnded = true;
        this.scheduleReload();
        return;
      default:
        // Tools, titles and usage drive the status line, not the thread.
        return;
    }
  }

  private resetLiveReply(): void {
    this.liveReply = '';
    this.liveItemId = null;
    this.liveTurnEnded = false;
  }

  /** Test seam: feed one `session_progress` message without a socket. */
  public receiveSessionProgress(message: {
    payload?: Record<string, unknown>;
    managed_agent_id?: string;
    runtime_session_id?: string;
  }): void {
    this.handleSessionProgress(message);
  }

  /** Show the one-line "New session" notice for a session not on screen. */
  private offerSession(sessionId: string): void {
    if (this.noticeSessionId === sessionId) return;
    this.noticeSessionId = sessionId;
    // The notice names the session, and a brand new one is not in the list
    // yet: refresh the list only, never the open thread.
    if (!this.sessions.some((session) => session.id === sessionId)) {
      void this.refreshSessions();
    }
  }

  private async refreshSessions(): Promise<void> {
    const generation = this.sessionGeneration;
    try {
      const detail = await getAccountAgent(this.agentId, {
        include: ['sessions'],
        lightweight: true,
      });
      if (generation !== this.sessionGeneration) return;
      this.sessions = detail.sessions || [];
    } catch (error) {
      // The notice still offers the switch, under a generic name.
      console.error('Failed to refresh talk sessions:', error);
    }
  }

  /**
   * An approval was created or resolved for a session this page may be showing.
   *
   * Scoped to the followed session and re-read from the server rather than
   * patched in place: the realtime payload deliberately carries no tool
   * arguments, so the client cannot decide a row's status on its own.
   */
  private handleApprovalActivity(message: {
    payload?: Record<string, unknown>;
    runtime_session_id?: string;
  }): void {
    const payload = message?.payload ?? {};
    const sessionId =
      (payload.runtime_session_id as string | undefined) ??
      message?.runtime_session_id;
    if (!sessionId || sessionId !== this.sessionId) return;
    void this.loadApprovals();
  }

  /**
   * Read the followed session's pending approvals.
   *
   * A failure is logged, not surfaced: approvals are one input to the activity
   * line, and losing them must not break the conversation underneath.
   */
  private async loadApprovals(): Promise<void> {
    const sessionId = this.sessionId;
    if (!sessionId) return;
    try {
      const rows = await listApprovalRequests({
        runtime_session_id: sessionId,
        status: 'pending',
        limit: 100,
      });
      if (this.sessionId !== sessionId) return;
      this.pendingApprovals = (rows as Array<Record<string, unknown>>).map(
        (row) => ({
          id: String(row.id),
          status: String(row.status ?? 'pending'),
          requested_at:
            typeof row.requested_at === 'string' ? row.requested_at : undefined,
        })
      );
    } catch (error) {
      // Deliberately not surfaced: the conversation underneath is still
      // readable. The previously known set is kept, so the line can be briefly
      // stale rather than wrongly claiming nothing is waiting.
      console.error('Failed to refresh session approvals:', error);
    }
  }

  /** Test seam: feed one realtime message without a socket. */
  public receiveActivity(message: { payload?: Record<string, unknown> }): void {
    this.handleActivity(message);
  }

  /**
   * Open another session in this window, or none (New session).
   *
   * Everything that belongs to the old session goes: thread, approvals, the
   * next-page cursor and any error. Pending turns stay with the composer,
   * which only shows the ones typed for the session on screen.
   */
  private switchSession(sessionId: string | null): void {
    this.sessionGeneration += 1;
    this.eventsAbort?.abort();
    this.eventsRequest = null;
    this.eventsAbort = null;
    this.eventsRefreshQueued = false;
    this.eventsMoreQueued = false;
    this.sessionResolved = true;
    this.sessionId = sessionId;
    this.events = [];
    this.activity = [];
    this.pendingApprovals = [];
    this.resetLiveReply();
    this.lastProgressSeq = null;
    this.hasMoreEvents = false;
    this.nextEventOffset = null;
    this.loadingEvents = false;
    this.loadingMoreEvents = false;
    this.browserStepViewerIndex = -1;
    this.error = null;
    if (this.noticeSessionId === sessionId) this.noticeSessionId = null;
    if (this.reloadTimer !== null) {
      window.clearTimeout(this.reloadTimer);
      this.reloadTimer = null;
    }
    this.syncDocumentTitle();
    this.postChannel('open');
    if (sessionId) void this.loadEvents(sessionId);
    // The operator switched to talk: the cursor goes back to the box.
    this.composer?.focusInput();
  }

  private startNewSession(): void {
    this.awaitingNewSession = true;
    this.switchSession(null);
  }

  /**
   * Traffic arrives in bursts; reloading per event would refetch the whole
   * page dozens of times for one agent turn.
   */
  private scheduleReload(): void {
    if (this.reloadTimer !== null) return;
    this.reloadTimer = window.setTimeout(() => {
      this.reloadTimer = null;
      if (this.sessionId) void this.loadEvents(this.sessionId);
    }, 1200);
  }

  // -- render --------------------------------------------------------------

  private renderHeader() {
    const agent = this.agent;
    const chip = agent ? this.headerChip(agent) : null;
    const subject = this.sessionSubject;
    return html`
      <div class="talk-header">
        <span class="avatar">
          ${renderAgentIcon(agent?.agent_kind || agent?.session_source_type)}
        </span>
        <div class="identity">
          <div class="name-row">
            <h1
              class="name"
              data-testid="talk-agent-name"
              aria-describedby="talk-subject"
            >
              ${agent?.display_name || 'Agent'}
            </h1>
            ${
              chip
                ? html`<sl-badge class="chip" variant=${chip.variant} pill
                    >${chip.label}</sl-badge
                  >`
                : nothing
            }
          </div>
          <span
            id="talk-subject"
            class="subject"
            data-testid="talk-session-subject"
          >
            ${subject || 'No session yet'}
          </span>
        </div>
        <span class="spacer"></span>
        <talk-session-picker
          .sessions=${this.sessions}
          .currentSessionId=${this.sessionId}
          ?canStartNew=${agent?.supports_new_session !== false}
        ></talk-session-picker>
        <a
          class="open-console"
          data-testid="open-in-console"
          href=${`/console/agents/${this.agentId}${
            this.sessionId ? `?session=${this.sessionId}` : ''
          }`}
          target=${this.windowMode ? '_blank' : '_self'}
          rel="noopener"
        >
          Open in console
        </a>
      </div>
    `;
  }

  private renderSessionNotice() {
    const sessionId = this.noticeSessionId;
    // The region is always there, so a screen reader hears the notice when it
    // fills in; no motion, it does not push or animate the thread.
    return html`<div
      class=${sessionId ? 'session-notice has-notice' : 'session-notice'}
      aria-live="polite"
      data-testid=${sessionId ? 'session-notice' : 'session-notice-empty'}
    >
      ${
        sessionId
          ? html`<span
                >${`New session: ${this.subjectOf(sessionId) || 'untitled session'}.`}</span
              >
              <button
                type="button"
                data-testid="session-notice-switch"
                @click=${() => this.switchSession(sessionId)}
              >
                Switch
              </button>`
          : nothing
      }
    </div>`;
  }

  render() {
    if (this.loading && !this.agent) {
      return html`<div class="state" role="status">
        <sl-spinner></sl-spinner>
        <div>Loading agent...</div>
      </div>`;
    }
    if (!this.agent) {
      return html`<div class="state">
        <div role="alert">${this.error || 'Agent not found.'}</div>
        <sl-button href=${talkRoutePath(this.agentId).replace('/talk', '')}>
          Back to the agent
        </sl-button>
      </div>`;
    }

    return html`
      ${this.renderHeader()} ${this.renderSessionNotice()}
      ${
        this.error
          ? html`<div class="talk-error" role="alert" data-testid="talk-error">
              ${this.error}
            </div>`
          : nothing
      }
      <session-chat-view
        scrollable
        followLive
        @session-live-reload=${() => this.scheduleReload()}
        .sessionId=${this.sessionId || ''}
        .ended=${Boolean(this.currentSession?.ended_at)}
        .turn=${turnInfoFromSession(
          this.currentSession as unknown as Record<string, unknown> | undefined
        )}
        @browser-step-open=${(event: CustomEvent<{ key: string }>) => {
          this.browserStepViewerIndex = sortBrowserSteps(
            this.activity
          ).findIndex((item) => browserStepKey(item) === event.detail.key);
        }}
        .events=${this.events}
        .activity=${this.activity}
        .pendingApprovals=${this.pendingApprovals}
        .connected=${this.realtimeConnected ?? true}
        .pending=${this.pending}
        .liveReply=${this.liveReply}
        .loading=${this.loadingEvents && !this.events.length}
        .hasMoreEvents=${this.hasMoreEvents}
        .loadingMoreEvents=${this.loadingMoreEvents || this.eventsMoreQueued}
        emptyText=${
          this.sessionId
            ? 'Nothing has been said in this session yet.'
            : 'This agent has no session yet. Your first message starts one.'
        }
        @session-events-page-requested=${() => void this.loadMoreEvents()}
      ></session-chat-view>
      ${
        this.sessionId && this.browserStepViewerIndex >= 0
          ? html`<artifact-image-viewer
              .sessionId=${this.sessionId}
              .images=${browserStepViewerImages(sortBrowserSteps(this.activity))}
              .index=${this.browserStepViewerIndex}
              @viewer-close=${() => {
                this.browserStepViewerIndex = -1;
              }}
              @viewer-navigate=${(event: CustomEvent<{ index: number }>) => {
                this.browserStepViewerIndex = event.detail.index;
              }}
            ></artifact-image-viewer>`
          : nothing
      }
      <talk-composer
        .agent=${this.agent}
        .sessionId=${this.sessionId}
        .sessionEnded=${Boolean(this.currentSession?.ended_at)}
        .sessionIdle=${Boolean(
          this.currentSession &&
          !this.currentSession.ended_at &&
          !this.currentSession.is_active_now
        )}
        .sessionLoadFailed=${Boolean(this.sessionId && this.sessionLoadFailed)}
        .liveAlternative=${this.liveAlternative}
        sourceContext=${this.composerSourceContext}
      ></talk-composer>
    `;
  }

  firstUpdated(): void {
    this.composer?.focusInput();
    // Retry bubbles from the failed bubble in the thread to the composer that
    // owns the send.
    this.addEventListener(TALK_RETRY_EVENT, (event: Event) => {
      const id = (event as CustomEvent<{ id: string }>).detail?.id;
      if (id) void this.composer?.retry(id);
      // The Retry link took focus; the operator's next keystroke belongs in
      // the box.
      this.composer?.focusInput();
    });
    this.addEventListener(TALK_SESSION_SELECT_EVENT, (event: Event) => {
      const id = (event as CustomEvent<{ sessionId: string }>).detail
        ?.sessionId;
      if (id && id !== this.sessionId) this.switchSession(id);
    });
    this.addEventListener(TALK_SWITCH_SESSION_EVENT, (event: Event) => {
      const id = (event as CustomEvent<{ sessionId: string }>).detail
        ?.sessionId;
      if (id) this.switchSession(id);
    });
    this.addEventListener(TALK_SESSION_NEW_EVENT, () => this.startNewSession());
    this.addEventListener(TALK_NEW_SESSION_EVENT, () => this.startNewSession());
    this.addEventListener(TALK_PENDING_CHANGED_EVENT, (event: Event) => {
      this.pending = [
        ...((event as CustomEvent<{ pending: PendingTalkMessage[] }>).detail
          ?.pending ?? []),
      ];
    });
    // Sending is not news: the turn the operator just typed is already on
    // screen. Only the reload is owed here; the unread dot belongs to
    // `handleActivity`, which is where the agent's answer arrives.
    this.addEventListener(TALK_MESSAGE_SENT_EVENT, (event: Event) => {
      const response = (
        event as CustomEvent<{ response?: AgentControlCommandResponse }>
      ).detail?.response;
      // A turn sent from New session names the session it created in
      // `target_session_id` (`runtime_session_id` is the existing binding).
      const created =
        typeof response?.target_session_id === 'string'
          ? response.target_session_id
          : null;
      if (this.awaitingNewSession && !this.sessionId && created) {
        this.awaitingNewSession = false;
        this.switchSession(created);
        void this.refreshSessions();
        return;
      }
      this.scheduleReload();
    });
    this.syncDocumentTitle();
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'agent-talk-view': AgentTalkView;
  }
}
