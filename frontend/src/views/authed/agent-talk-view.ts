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

import {
  getAccountAgent,
  getAccountRuntimeSessionActivityTimeline,
  getRuntimeSessionGatewayEvents,
} from '../../api';
import { unifiedWebSocketManager } from '../../services/unified-websocket-manager';
import consoleStyles from '../../styles/console-styles.css?inline';
import type {
  FlowGatewayEvent,
  ManagedAgentSummary,
  RuntimeSessionActivityItem,
  RuntimeSessionSummary,
} from '../../types';
import { getAgentStatusChip } from '../../utils/agent-display';
import { renderAgentIcon } from '../../utils/agent-icons';
import { normalizeObservedSession } from '../../utils/session-observer';
import {
  TALK_HEARTBEAT_MS,
  openTalkChannel,
  postTalkChannelMessage,
} from '../../utils/talk-channel';
import { saveTalkWindowGeometry, talkRoutePath } from '../../utils/talk-window';
import type { PendingTalkMessage } from '../../components/session-chat-view';
import { TALK_RETRY_EVENT } from '../../components/session-chat-view';
import type { TalkComposer } from '../../components/talk-composer';
import {
  TALK_MESSAGE_SENT_EVENT,
  TALK_PENDING_CHANGED_EVENT,
} from '../../components/talk-composer';

const EVENT_PAGE_SIZE = 50;

@customElement('agent-talk-view')
export class AgentTalkView extends LitElement {
  @state() private agentId = '';
  @state() private agent: ManagedAgentSummary | null = null;
  @state() private sessions: RuntimeSessionSummary[] = [];
  @state() private sessionId: string | null = null;
  /** A session named in the URL is pinned: live traffic never moves it. */
  @state() private pinnedSessionId: string | null = null;
  @state() private windowMode = false;
  /** The entry point that opened this window, for `requested_from`. */
  @state() private sourceContext: string | null = null;
  @state() private loading = true;
  @state() private loadingEvents = false;
  @state() private loadingMoreEvents = false;
  @state() private hasMoreEvents = false;
  @state() private error: string | null = null;
  @state() private events: FlowGatewayEvent[] = [];
  @state() private activity: RuntimeSessionActivityItem[] = [];
  @state() private pending: PendingTalkMessage[] = [];
  @state() private followBanner: string | null = null;

  @query('talk-composer') private composer!: TalkComposer;

  private previousDocumentTitle = document.title;
  private nextEventOffset: number | null = null;
  private unsubscribeRealtime: (() => void) | null = null;
  private channel: BroadcastChannel | null = null;
  private heartbeatTimer: number | null = null;
  private reloadTimer: number | null = null;
  private followBannerTimer: number | null = null;
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

      .follow-banner {
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
    this.openChannel();
    if (this.windowMode) {
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
    this.unsubscribeRealtime?.();
    this.unsubscribeRealtime = null;
    if (this.heartbeatTimer !== null) window.clearInterval(this.heartbeatTimer);
    if (this.reloadTimer !== null) window.clearTimeout(this.reloadTimer);
    if (this.followBannerTimer !== null) {
      window.clearTimeout(this.followBannerTimer);
    }
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
    const session = this.sessions.find((item) => item.id === this.sessionId);
    if (!session) return null;
    return normalizeObservedSession(session).title;
  }

  /**
   * The window title is how an operator finds the right popup in a stack of
   * them, so it names the agent and the session, not the product.
   */
  private syncDocumentTitle(): void {
    const name = this.agent?.display_name || 'Agent';
    const subject = this.sessionSubject;
    document.title = subject ? `${name} · ${subject}` : `${name} · Talk`;
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

  private async load(): Promise<void> {
    if (!this.agentId) return;
    this.loading = true;
    this.error = null;
    try {
      const detail = await getAccountAgent(this.agentId);
      this.agent = detail.agent;
      this.sessions = detail.sessions || [];
      const nextSession = this.pickSession(this.sessions);
      const changed = nextSession !== this.sessionId;
      this.sessionId = nextSession;
      this.syncDocumentTitle();
      this.postChannel('open');
      if (nextSession && (changed || !this.events.length)) {
        await this.loadEvents(nextSession);
      }
    } catch (error) {
      this.error =
        error instanceof Error ? error.message : 'Failed to load agent';
    } finally {
      this.loading = false;
    }
  }

  private async loadEvents(sessionId: string): Promise<void> {
    this.loadingEvents = true;
    try {
      const [events, activity] = await Promise.all([
        getRuntimeSessionGatewayEvents(sessionId, {
          limit: EVENT_PAGE_SIZE,
          offset: 0,
        }),
        getAccountRuntimeSessionActivityTimeline(sessionId).catch(() => ({
          items: [] as RuntimeSessionActivityItem[],
        })),
      ]);
      if (sessionId !== this.sessionId) return;
      this.events = this.sortEvents(events.logs || []);
      this.activity = activity.items || [];
      this.hasMoreEvents =
        events.pagination?.has_more ??
        (events.logs || []).length >= EVENT_PAGE_SIZE;
      this.nextEventOffset =
        events.pagination?.next_offset ?? (events.logs || []).length;
      // Anything the server has now echoed back is in the thread already.
      this.composer?.clearSentPending();
    } catch (error) {
      this.error =
        error instanceof Error ? error.message : 'Failed to load conversation';
    } finally {
      this.loadingEvents = false;
    }
  }

  private async loadMoreEvents(): Promise<void> {
    if (
      !this.sessionId ||
      this.loadingMoreEvents ||
      !this.hasMoreEvents ||
      this.nextEventOffset === null
    ) {
      return;
    }
    this.loadingMoreEvents = true;
    try {
      const page = await getRuntimeSessionGatewayEvents(this.sessionId, {
        limit: EVENT_PAGE_SIZE,
        offset: this.nextEventOffset,
      });
      const byId = new Map(this.events.map((event) => [event.id, event]));
      for (const event of page.logs || []) byId.set(event.id, event);
      this.events = this.sortEvents(Array.from(byId.values()));
      this.hasMoreEvents = page.pagination?.has_more ?? false;
      this.nextEventOffset = page.pagination?.next_offset ?? null;
    } catch (error) {
      this.error =
        error instanceof Error ? error.message : 'Failed to load more events';
    } finally {
      this.loadingMoreEvents = false;
    }
  }

  private sortEvents(events: FlowGatewayEvent[]): FlowGatewayEvent[] {
    return [...events].sort(
      (left, right) =>
        new Date(right.timestamp || 0).getTime() -
        new Date(left.timestamp || 0).getTime()
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
    ];
    this.unsubscribeRealtime = () => {
      for (const unsubscribe of unsubscribers) unsubscribe();
    };
    void unifiedWebSocketManager.connect();
  }

  private handleActivity(message: { payload?: Record<string, unknown> }): void {
    const payload = message?.payload ?? {};
    const agentId =
      (payload.managed_agent_id as string) || (payload.agent_id as string);
    if (agentId && agentId !== this.agentId) return;
    const sessionId = payload.runtime_session_id as string | undefined;
    if (!agentId && sessionId && sessionId !== this.sessionId) return;
    // A new session for the same agent is exactly what the operator wants to
    // see: the agent restarted and their next message must land in the live
    // session, not the dead one. Pinned pages stay where they were put.
    if (
      sessionId &&
      sessionId !== this.sessionId &&
      !this.pinnedSessionId &&
      agentId === this.agentId
    ) {
      this.followSession(sessionId);
      return;
    }
    this.noteAgentTurn();
    this.scheduleReload();
  }

  /** Test seam: feed one realtime message without a socket. */
  public receiveActivity(message: { payload?: Record<string, unknown> }): void {
    this.handleActivity(message);
  }

  private followSession(sessionId: string): void {
    this.sessionId = sessionId;
    this.events = [];
    this.activity = [];
    void this.load().then(() => this.announceFollow());
  }

  private announceFollow(): void {
    const subject = this.sessionSubject || 'a new session';
    this.followBanner = `Now following session ${subject}`;
    this.syncDocumentTitle();
    if (this.followBannerTimer !== null) {
      window.clearTimeout(this.followBannerTimer);
    }
    this.followBannerTimer = window.setTimeout(() => {
      this.followBanner = null;
      this.followBannerTimer = null;
    }, 8000);
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
    const chip = agent ? getAgentStatusChip(agent) : null;
    const subject = this.sessionSubject;
    return html`
      <div class="talk-header">
        <span class="avatar">
          ${renderAgentIcon(agent?.agent_kind || agent?.session_source_type)}
        </span>
        <div class="identity">
          <div class="name-row">
            <span class="name" data-testid="talk-agent-name">
              ${agent?.display_name || 'Agent'}
            </span>
            ${
              chip
                ? html`<sl-badge class="chip" variant=${chip.variant} pill
                    >${chip.label}</sl-badge
                  >`
                : nothing
            }
          </div>
          <span class="subject" data-testid="talk-session-subject">
            ${subject || 'No session yet'}
          </span>
        </div>
        <span class="spacer"></span>
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

  render() {
    if (this.loading && !this.agent) {
      return html`<div class="state">
        <sl-spinner></sl-spinner>
        <div>Loading agent...</div>
      </div>`;
    }
    if (!this.agent) {
      return html`<div class="state">
        <div>${this.error || 'Agent not found.'}</div>
        <sl-button href=${talkRoutePath(this.agentId).replace('/talk', '')}>
          Back to the agent
        </sl-button>
      </div>`;
    }

    return html`
      ${this.renderHeader()}
      ${
        this.followBanner
          ? html`<div
              class="follow-banner"
              role="status"
              data-testid="follow-banner"
            >
              ${this.followBanner}
            </div>`
          : nothing
      }
      <session-chat-view
        scrollable
        followLive
        .events=${this.events}
        .activity=${this.activity}
        .pending=${this.pending}
        .loading=${this.loadingEvents && !this.events.length}
        .hasMoreEvents=${this.hasMoreEvents}
        .loadingMoreEvents=${this.loadingMoreEvents}
        emptyText=${
          this.sessionId
            ? 'Nothing has been said in this session yet.'
            : 'This agent has no session yet. Your first message starts one.'
        }
        @session-events-page-requested=${() => void this.loadMoreEvents()}
      ></session-chat-view>
      <talk-composer
        .agent=${this.agent}
        .sessionId=${this.sessionId}
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
    });
    this.addEventListener(TALK_PENDING_CHANGED_EVENT, (event: Event) => {
      this.pending = [
        ...((event as CustomEvent<{ pending: PendingTalkMessage[] }>).detail
          ?.pending ?? []),
      ];
    });
    // Sending is not news: the turn the operator just typed is already on
    // screen. Only the reload is owed here; the unread dot belongs to
    // `handleActivity`, which is where the agent's answer arrives.
    this.addEventListener(TALK_MESSAGE_SENT_EVENT, () => {
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
