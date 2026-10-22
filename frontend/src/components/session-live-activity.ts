import { randomUuid } from '../utils/random-uuid';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import { LitElement, html, css, nothing } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import {
  getUserProfile,
  hasPermission,
  listApprovalRequests,
  getApprovalRequest,
} from '../api';
import type {
  ApprovalRequest,
  FlowGatewayEvent,
  RuntimeSessionActivityItem,
} from '../types';
import {
  unifiedWebSocketManager,
  ConnectionState,
} from '../services/unified-websocket-manager';
import { sessionWork, sessionTools } from '../utils/session-live';
import type { LiveSessionState } from '../utils/live-session';
import { formatElapsed } from '../utils/live-session';
import { isUnexpiredPendingRequest } from '../utils/approvals';
import {
  applyTurnEvent,
  deriveAgentStatus,
  runtimeForSource,
  turnOutputTokens,
  utcMs,
  type AgentStatus,
  type AgentTurnInfo,
} from '../utils/agent-status';
import './agent-status-line';

export interface SessionApprovalState {
  requests: ApprovalRequest[];
  canDecide: boolean;
  author: string;
  now: number;
}
/** Keep a pending form's input state across equal authoritative schema snapshots. */
export function retainSessionApprovalForms(
  previous: SessionApprovalState,
  next: SessionApprovalState
): SessionApprovalState {
  const current = new Map(
    previous.requests.map((request) => [request.id, request])
  );
  return {
    ...next,
    requests: next.requests.map((request) => {
      const before = current.get(request.id);
      return before &&
        JSON.stringify(before.question_schema) ===
          JSON.stringify(request.question_schema)
        ? { ...request, question_schema: before.question_schema }
        : request;
    }),
  };
}
/** One session controller shared by Talk, Conversation and Transcript. */
@customElement('session-live-activity')
export class SessionLiveActivity extends LitElement {
  @property({ type: String }) sessionId = '';
  @property({ attribute: false }) events: FlowGatewayEvent[] = [];
  @property({ attribute: false }) activity: RuntimeSessionActivityItem[] = [];
  @property({ type: Boolean }) ended = false;
  @state() private requests: ApprovalRequest[] = [];
  @state() private transient: FlowGatewayEvent[] = [];
  /** Ticking clock for approval expiry and the controller's own elapsed text. */
  @state() private clock = Date.now();
  /**
   * Host clock for the compact status line. Left at 0, that line uses the
   * wall clock. The controller never writes this: a fixed test clock must
   * not be replaced by the approval timer.
   */
  @property({ type: Number }) now = 0;
  /** Compact "what is happening" line. Null hides it; the controller still runs. */
  @property({ attribute: false }) state: LiveSessionState | null = null;
  /**
   * Turn facts of this session (runtime, server turn state and bounds).
   * Realtime prompt, tool-end, turn-end and progress events update a local
   * copy, so the line moves without a reload.
   */
  @property({ attribute: false }) turn: AgentTurnInfo | null = null;
  /** True where Preloop can interrupt the turn; adds the interrupt hint. */
  @property({ type: Boolean }) canInterrupt = false;
  @state() private liveTurn: AgentTurnInfo | null = null;
  @state() private connected = false;
  @state() private error = '';
  private generation = 0;
  private approvalsUnsubscribe?: () => void;
  private revisions = new Map<string, number>();
  private canView = false;
  private canDecide = false;
  private author = '';
  private unsubscribe: Array<() => void> = [];
  private timer?: ReturnType<typeof setInterval>;
  private lastUpdate = Date.now();
  private refreshInFlight: Promise<void> | null = null;
  private refreshDirty = false;
  private controllerStarted = false;
  private announcement = '';
  private lastAnnouncedStatus: string | null = null;
  private tickTimer: number | null = null;
  private tickOffset = 0;
  private lastHostNow: number | null = null;
  static styles = css`
    :host {
      font-family: var(--sl-font-sans, system-ui, sans-serif);
      color: var(--sl-color-neutral-900);
      display: block;
      position: sticky;
      top: 0;
      z-index: 2;
      background: var(--sl-color-neutral-0);
      padding: 0.5rem;
      border-bottom: 1px solid var(--sl-color-neutral-200);
    }
    .row {
      display: flex;
      flex-wrap: wrap;
      gap: 0.5rem;
      align-items: center;
    }
    .transport {
      font-size: 0.8rem;
      color: var(--sl-color-neutral-600);
    }
    button {
      min-height: 2.2rem;
    }
    button:focus-visible {
      outline: 2px solid var(--sl-color-primary-500);
    }
    .line {
      align-items: center;
      color: var(--sl-color-neutral-700, #374151);
      display: flex;
      flex-wrap: wrap;
      font-size: 0.78rem;
      gap: 0.4rem;
      padding: 0.15rem 0;
    }
    .line.ended,
    .line.idle,
    .line.unavailable {
      color: var(--sl-color-neutral-500, #6b7280);
    }
    .line.waiting_for_approval {
      color: var(--sl-color-warning-700, #b45309);
      font-weight: 600;
    }
    .elapsed {
      font-variant-numeric: tabular-nums;
    }
    .turn-footer,
    .turn-marker {
      color: var(--sl-color-neutral-600);
      font-family: var(
        --console-font-mono,
        ui-monospace,
        SFMono-Regular,
        Menlo,
        Consolas,
        monospace
      );
      font-size: 0.78rem;
    }
    .turn-marker + .turn-footer::before {
      content: '· ';
    }
    .jump {
      background: none;
      border: 1px solid currentColor;
      border-radius: 999px;
      color: inherit;
      cursor: pointer;
      font: inherit;
      font-size: 0.72rem;
      min-height: 0;
      padding: 0 0.5rem;
    }
    .jump:focus-visible {
      outline: 2px solid var(--sl-color-primary-500, #0ea5e9);
      outline-offset: 2px;
    }
    .live-region {
      clip: rect(0 0 0 0);
      clip-path: inset(50%);
      height: 1px;
      overflow: hidden;
      position: absolute;
      white-space: nowrap;
      width: 1px;
    }
  `;
  connectedCallback(): void {
    super.connectedCallback();
    this.ensureController();
  }
  /**
   * Websocket and approval polling belong to a real session, not the status
   * line.
   *
   * @returns `true` when this call started the controller, so the caller must
   *   not open again — `ensureController` already did.
   */
  private ensureController(): boolean {
    if (this.controllerStarted || !this.sessionId) return false;
    this.controllerStarted = true;
    if (this.hasUpdated) void this.open();
    this.connected =
      unifiedWebSocketManager.getState() === ConnectionState.CONNECTED;
    this.unsubscribe = [
      unifiedWebSocketManager.onStateChange((state) => {
        this.connected = state === ConnectionState.CONNECTED;
        if (this.connected) {
          this.transient = [];
          void this.refresh();
          this.dispatchEvent(
            new CustomEvent('session-live-reconcile', {
              bubbles: true,
              composed: true,
            })
          );
        }
      }),
      unifiedWebSocketManager.subscribe('gateway_activity', (message) => {
        const payload = message?.payload || {};
        if (
          (payload.runtime_session_id || message.runtime_session_id) !==
          this.sessionId
        )
          return;
        this.lastUpdate = Date.now();
        const event: FlowGatewayEvent = {
          id: message.id || randomUuid(),
          execution_id: '',
          timestamp:
            payload.timestamp || message.timestamp || new Date().toISOString(),
          type: message.type,
          payload,
        };
        this.transient = [
          ...this.transient.filter((e) => e.id !== event.id),
          event,
        ].slice(-256);
      }),
      unifiedWebSocketManager.subscribe('runtime_sessions', (message) =>
        this.handleTurnEvent(message)
      ),
      unifiedWebSocketManager.subscribe('agent_control', (message) => {
        if (message?.type === 'managed_agent_command_progress')
          this.handleTurnEvent(message);
      }),
    ];
    this.timer = setInterval(() => {
      this.clock = Date.now();
      if (
        this.requests.some(
          (r) =>
            r.status === 'pending' && !isUnexpiredPendingRequest(r, this.clock)
        )
      )
        this.notify();
    }, 1000);
    return true;
  }
  private handleTurnEvent(message: {
    type?: string;
    timestamp?: string;
    runtime_session_id?: string;
    payload?: Record<string, unknown>;
  }): void {
    const payload = message?.payload ?? {};
    if (
      (payload.runtime_session_id || message?.runtime_session_id) !==
      this.sessionId
    )
      return;
    this.liveTurn = applyTurnEvent(
      this.liveTurn ?? { ...(this.turn ?? {}) },
      message
    );
  }
  /** Server turn facts, overlaid by realtime events seen since. */
  private get effectiveTurn(): AgentTurnInfo {
    return { ...(this.turn ?? {}), ...(this.liveTurn ?? {}) };
  }
  /**
   * The one status (D35) from what this controller observed: approvals,
   * tools, open model requests and the turn's own bounds.
   */
  private agentStatus(
    live: {
      busyModel: boolean;
      runningTool: string | null;
      queued: boolean;
      since: number | null;
      lastEventAt: number | null;
    },
    pending: Array<{ requested_at?: string }>,
    events: FlowGatewayEvent[],
    now: number
  ): AgentStatus {
    const turn = this.effectiveTurn;
    const ended = this.ended || turn.turnState === 'ended';
    const turnEndedAt = utcMs(turn.turnEndedAt);
    const serverStart = utcMs(turn.turnStartedAt);
    const turnStartedAt = serverStart ?? live.since;
    const pendingSince = pending
      .map((request) => utcMs(request.requested_at))
      .filter((value): value is number => value !== null)
      .sort((left, right) => left - right)[0];
    return deriveAgentStatus({
      runtime: runtimeForSource(turn.sourceType),
      turnId: turn.turnStartedAt || null,
      turnStartedAt,
      turnEndedAt,
      interrupted: turn.turnInterrupted === true,
      ended,
      turnState: turn.turnState ?? null,
      modelBusy: live.busyModel,
      runningTool: live.runningTool || turn.runningTool || null,
      pendingApprovals: pending.length,
      pendingSince: pendingSince ?? null,
      queued: live.queued,
      outputTokens: turnOutputTokens(events, turnStartedAt),
      catchingUp: this.sessionId ? !this.connected : false,
      lastEventAt: live.lastEventAt,
      canInterrupt: this.canInterrupt,
      now,
    });
  }
  private renderTurnEnd(status: AgentStatus) {
    if (!status.marker && !status.footer) return nothing;
    return html`${
      status.marker
        ? html`<span class="turn-marker" data-testid="turn-marker"
            >${status.marker}</span
          >`
        : nothing
    }${
      status.footer
        ? html`<span class="turn-footer" data-testid="turn-footer"
            >${status.footer}</span
          >`
        : nothing
    }`;
  }
  disconnectedCallback(): void {
    ++this.generation;
    this.approvalsUnsubscribe?.();
    for (const unsubscribe of this.unsubscribe) unsubscribe();
    this.unsubscribe = [];
    if (this.timer) clearInterval(this.timer);
    this.timer = undefined;
    this.controllerStarted = false;
    if (this.tickTimer !== null) {
      window.clearInterval(this.tickTimer);
      this.tickTimer = null;
    }
    super.disconnectedCallback();
  }
  protected willUpdate(): void {
    const now = this.effectiveNow;
    const status = this.state?.status ?? null;
    // Keyed on the derived phase and turn end, not the host's status: a
    // realtime turn end changes the footer while the host still says idle.
    const line = status && this.state ? this.lineStatus(this.state) : null;
    const signature = line
      ? `${status}|${line.phase}|${line.marker ?? ''}|${line.footer ?? ''}`
      : null;
    if (line && this.state && signature !== this.lastAnnouncedStatus) {
      this.lastAnnouncedStatus = signature;
      // Announce the words, not the ticking counter: "Working", "Waiting
      // for approval", the footer of a finished turn.
      const words = line.line
        ? line.line.split(' (')[0].replace(/…$/, '')
        : line.marker || line.footer || this.state.label;
      const elapsed = formatElapsed(this.state.since ?? null, now);
      this.announcement =
        elapsed && line.line ? `${words}, ${elapsed} elapsed` : `${words}.`;
    }
    this.syncTicker();
  }
  protected updated(changed: Map<string | number | symbol, unknown>): void {
    // Exactly one open per sessionId change: `ensureController` opens when it
    // starts the controller here, so only a switch to an already-running
    // controller needs a second call. Opening twice refetched the profile and
    // reset approval state before the first open had settled.
    if (changed.has('sessionId') && !this.ensureController()) void this.open();
  }
  private get effectiveNow(): number {
    if (this.now !== this.lastHostNow) {
      this.lastHostNow = this.now;
      this.tickOffset = 0;
    }
    return this.now ? this.now + this.tickOffset : Date.now();
  }
  private syncTicker(): void {
    const busy =
      this.state?.status === 'model_processing' ||
      this.state?.status === 'running_tool' ||
      this.state?.status === 'waiting_for_approval';
    if (!busy) {
      if (this.tickTimer !== null) {
        window.clearInterval(this.tickTimer);
        this.tickTimer = null;
      }
      return;
    }
    if (this.tickTimer !== null) return;
    this.tickTimer = window.setInterval(() => {
      this.tickOffset += 1000;
      this.requestUpdate();
    }, 1000);
  }
  private requestPendingJump(): void {
    this.dispatchEvent(
      new CustomEvent('live-activity-pending-jump', {
        detail: { count: this.state?.pendingApprovalCount ?? 0 },
        bubbles: true,
        composed: true,
      })
    );
  }
  private formatClock(at: number | null): string {
    if (!at) return '';
    return new Date(at).toLocaleTimeString();
  }
  private async open(): Promise<void> {
    const generation = ++this.generation;
    this.approvalsUnsubscribe?.();
    this.approvalsUnsubscribe = undefined;
    this.revisions.clear();
    this.requests = [];
    this.transient = [];
    this.liveTurn = null;
    this.canView = false;
    this.canDecide = false;
    this.author = '';
    this.error = '';
    this.refreshInFlight = null;
    this.refreshDirty = false;
    if (!this.sessionId) {
      this.notify();
      return;
    }
    try {
      const profile = await getUserProfile();
      if (generation !== this.generation) return;
      this.canView = hasPermission(
        profile.permissions ?? null,
        'view_approvals'
      );
      this.canDecide = hasPermission(
        profile.permissions ?? null,
        'decide_approvals'
      );
      this.author = profile.email || profile.username || '';
      if (this.canView) {
        const unsubscribe = unifiedWebSocketManager.subscribe(
          'approvals',
          (message) => {
            if (generation !== this.generation) return;
            const payload = message.payload || message;
            if (payload.runtime_session_id !== this.sessionId) return;
            const id = payload.approval_request_id;
            if (typeof id === 'string') void this.refreshOne(id, generation);
            else void this.refresh();
          }
        );
        this.approvalsUnsubscribe = unsubscribe;
        await this.refresh();
      } else this.notify();
    } catch {
      if (generation === this.generation) {
        this.error = 'Approval status unavailable.';
        this.notify();
      }
    }
  }
  private async refreshOne(id: string, generation: number): Promise<void> {
    const revision = (this.revisions.get(id) || 0) + 1;
    this.revisions.set(id, revision);
    try {
      const request = await getApprovalRequest(id);
      if (
        this.revisions.get(id) !== revision ||
        generation !== this.generation ||
        request.runtime_session_id !== this.sessionId
      )
        return;
      this.updateRequest(request);
    } catch {
      if (generation === this.generation) void this.refresh();
    }
  }
  public updateRequest(request: ApprovalRequest): void {
    if (request.runtime_session_id !== this.sessionId) return;
    const previous = this.requests.find((r) => r.id === request.id);
    if (
      previous &&
      previous.status !== 'pending' &&
      request.status === 'pending'
    )
      return;
    this.requests = [
      ...this.requests.filter((r) => r.id !== request.id),
      request,
    ];
    this.notify();
  }
  public refresh(): Promise<void> {
    if (!this.sessionId) return Promise.resolve();
    if (!this.canView) return this.open();
    if (this.refreshInFlight) {
      this.refreshDirty = true;
      return this.refreshInFlight;
    }
    const generation = this.generation,
      sessionId = this.sessionId;
    const run = async () => {
      try {
        // The history and the first pending page are asked together: the
        // answer arrives sooner, and the pending read joins the identical one
        // the session panel makes when it opens instead of repeating it.
        const pendingPage = (skip: number) =>
          listApprovalRequests({
            runtime_session_id: sessionId,
            status: 'pending',
            limit: 100,
            skip,
          });
        const firstPending = pendingPage(0);
        // Settled here so a failed history read cannot leave it unhandled.
        firstPending.catch(() => undefined);
        const history = await listApprovalRequests({
          runtime_session_id: sessionId,
          limit: 100,
        });
        if (generation !== this.generation) return;
        const pending: ApprovalRequest[] = [];
        for (let skip = 0; ; skip += 100) {
          const page =
            skip === 0 ? await firstPending : await pendingPage(skip);
          if (generation !== this.generation) return;
          pending.push(...page);
          if (page.length < 100) break;
        }
        if (generation !== this.generation) return;
        const merged = new Map(this.requests.map((r) => [r.id, r]));
        for (const request of [...history, ...pending])
          if (request.runtime_session_id === sessionId) {
            const previous = merged.get(request.id);
            if (
              !previous ||
              previous.status === 'pending' ||
              request.status !== 'pending'
            )
              merged.set(request.id, request);
          }
        // Pending rows that disappeared need an authoritative resolved record.
        const ids = new Set(pending.map((r) => r.id));
        for (const request of this.requests)
          if (request.status === 'pending' && !ids.has(request.id)) {
            const latest = await getApprovalRequest(request.id);
            if (generation !== this.generation) return;
            merged.set(latest.id, latest);
          }
        this.requests = [...merged.values()];
        this.error = '';
        this.notify();
      } catch {
        if (generation === this.generation)
          this.error = 'Approval status unavailable. Retry.';
      } finally {
        if (generation === this.generation) {
          this.refreshInFlight = null;
          if (this.refreshDirty) {
            this.refreshDirty = false;
            queueMicrotask(() => {
              if (generation === this.generation && this.connected)
                void this.refresh();
            });
          }
        }
      }
    };
    this.refreshInFlight = run();
    return this.refreshInFlight;
  }
  private notify(): void {
    this.dispatchEvent(
      new CustomEvent<SessionApprovalState>('session-approvals-changed', {
        detail: {
          requests: this.requests,
          canDecide: this.canDecide,
          author: this.author,
          now: this.clock,
        },
        bubbles: true,
        composed: true,
      })
    );
  }
  render() {
    return html`${this.renderStatusLine()}${this.renderControllerBar()}`;
  }
  /** The status line's state from the host's LiveSessionState. */
  private lineStatus(state: LiveSessionState): AgentStatus {
    const now = this.effectiveNow;
    const pending = Array.from({ length: state.pendingApprovalCount }, () => ({
      requested_at: undefined as string | undefined,
    }));
    const status = this.agentStatus(
      {
        busyModel: state.status === 'model_processing',
        runningTool:
          state.status === 'running_tool' ? state.detail || 'tool' : null,
        queued: state.status === 'waiting_for_agent',
        since: state.since,
        lastEventAt: state.lastUpdateAt,
      },
      pending,
      [...this.events, ...this.transient],
      now
    );
    if (status.phase === 'waiting_for_approval' && state.since !== null) {
      return deriveAgentStatus({
        runtime: runtimeForSource(this.effectiveTurn.sourceType),
        pendingApprovals: state.pendingApprovalCount,
        pendingSince: state.since,
        now,
      });
    }
    return status;
  }
  private renderStatusLine() {
    const state = this.state;
    if (!state) return nothing;
    const status = this.lineStatus(state);
    return html`
      <div
        class="line ${state.status}"
        data-testid="live-activity"
        data-status=${state.status}
        data-phase=${status.phase}
        data-transport=${state.transport}
      >
        <span data-testid="live-activity-label"
          >${
            status.line
              ? html`<agent-status-line .status=${status}></agent-status-line>`
              : status.marker || status.footer
                ? this.renderTurnEnd(status)
                : status.phase === 'your_turn'
                  ? status.chip.label
                  : state.label
          }</span
        >
        ${
          state.pendingApprovalCount > 0
            ? html`<button
                class="jump"
                type="button"
                data-testid="live-activity-jump"
                @click=${() => this.requestPendingJump()}
              >
                Jump to pending
              </button>`
            : nothing
        }
        ${
          state.transport === 'reconnecting'
            ? html`<span
                class="transport"
                data-testid="live-activity-transport"
              >
                · Reconnecting · last update
                ${this.formatClock(state.lastUpdateAt)}
              </span>`
            : nothing
        }
        <span class="live-region" aria-live="polite" role="status"
          >${this.announcement}</span
        >
      </div>
    `;
  }
  private renderControllerBar() {
    if (
      !this.sessionId &&
      !this.events.length &&
      !this.activity.length &&
      !this.transient.length &&
      !this.ended &&
      !this.error
    ) {
      return nothing;
    }
    const pending = this.requests.filter((r) =>
      isUnexpiredPendingRequest(r, this.clock)
    );
    const events = [...this.events, ...this.transient];
    const work = sessionWork(events, this.ended, this.clock);
    const tools = sessionTools(events, this.activity);
    const requested = tools.filter((t) => t.phase === 'requested');
    const runningEvidence = tools.filter((t) => t.phase === 'running');
    const running = runningEvidence.filter(
      (t) => this.clock - Date.parse(t.timestamp || '') < 120000
    );
    const status = this.agentStatus(
      {
        // A reply that asked for tools means the agent is running them.
        busyModel:
          work.label === 'Model processing' || work.label === 'Agent working',
        runningTool: running.length
          ? running.map((t) => t.name).join(', ')
          : null,
        queued: false,
        since: work.startedAt ?? null,
        lastEventAt: this.lastUpdate,
      },
      pending,
      events,
      this.clock
    );
    // Words for what the status line cannot say: evidence that aged out,
    // a tool the model asked for, an idle or ended session.
    const fallback = this.ended
      ? 'Ended'
      : runningEvidence.length && !running.length
        ? 'Activity unavailable'
        : work.label === 'Waiting for agent' && requested.length
          ? `Tool requested · ${requested.map((t) => t.name).join(', ')}`
          : status.phase === 'your_turn' || status.phase === 'interrupted'
            ? 'Your turn'
            : work.label;
    const truncated = events.some(
      (event) => event.payload?.tools_metadata_truncated === true
    );
    return html`<div class="row">
      ${
        this.state
          ? nothing
          : html`<span
                role="status"
                aria-live="polite"
                data-phase=${status.phase}
                >${
                  status.line
                    ? html`<agent-status-line
                        .status=${status}
                      ></agent-status-line>`
                    : fallback
                }</span
              >${this.renderTurnEnd(status)}`
      }
      ${!this.state && status.line && work.model && (work.count || 0) > 1 ? html`<span class="transport">${work.model} · ${work.count} requests</span>` : nothing}
      ${pending.length ? html`<sl-button variant="neutral" size="small" @click=${() => this.dispatchEvent(new CustomEvent('session-approval-jump', { detail: { id: pending[0].id }, bubbles: true, composed: true }))}>Next pending approval</sl-button>` : nothing}
      ${!this.connected ? html`<span class="transport">Reconnecting · last update ${new Date(this.lastUpdate).toLocaleTimeString()}</span>` : nothing}
      ${truncated ? html`<span class="transport">Additional tool history omitted by capture limits.</span>` : nothing}
      ${this.error ? html`<span>${this.error}</span><sl-button variant="neutral" size="small" @click=${() => this.refresh()}>Retry approvals</sl-button>` : nothing}
    </div>`;
  }
}
