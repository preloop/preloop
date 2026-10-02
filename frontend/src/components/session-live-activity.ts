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
  /** Websocket and approval polling belong to a real session, not the status line. */
  private ensureController(): void {
    if (this.controllerStarted || !this.sessionId) return;
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
          id: message.id || crypto.randomUUID(),
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
    if (status && status !== this.lastAnnouncedStatus) {
      this.lastAnnouncedStatus = status;
      const elapsed = formatElapsed(this.state?.since ?? null, now);
      this.announcement = elapsed
        ? `${this.state?.label}, ${elapsed} elapsed`
        : `${this.state?.label}.`;
    }
    this.syncTicker();
  }
  protected updated(changed: Map<string | number | symbol, unknown>): void {
    if (changed.has('sessionId')) {
      this.ensureController();
      void this.open();
    }
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
      this.state?.status === 'running_tool';
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
        const history = await listApprovalRequests({
          runtime_session_id: sessionId,
          limit: 100,
        });
        if (generation !== this.generation) return;
        const pending: ApprovalRequest[] = [];
        for (let skip = 0; ; skip += 100) {
          const page = await listApprovalRequests({
            runtime_session_id: sessionId,
            status: 'pending',
            limit: 100,
            skip,
          });
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
  private renderStatusLine() {
    const state = this.state;
    if (!state) return nothing;
    const elapsed = formatElapsed(state.since, this.effectiveNow);
    const busy =
      state.status === 'model_processing' || state.status === 'running_tool';
    return html`
      <div
        class="line ${state.status}"
        data-testid="live-activity"
        data-status=${state.status}
        data-transport=${state.transport}
      >
        ${
          busy
            ? html`<sl-spinner
                style="--indicator-size: 0.75rem; --width: 2px"
                aria-hidden="true"
              ></sl-spinner>`
            : nothing
        }
        <span data-testid="live-activity-label">${state.label}</span>
        ${state.detail ? html`<span>· ${state.detail}</span>` : nothing}
        ${
          elapsed
            ? html`<span class="elapsed" aria-hidden="true">· ${elapsed}</span>`
            : nothing
        }
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
    const label = this.ended
      ? 'Ended'
      : pending.length
        ? `Waiting for approval · ${pending.length}`
        : this.connected && running.length
          ? `Running ${running.map((t) => t.name).join(', ')}`
          : runningEvidence.length && !running.length
            ? 'Activity unavailable'
            : !this.connected && work.label === 'Model processing'
              ? 'Activity unavailable'
              : work.label === 'Waiting for agent' && requested.length
                ? `Tool requested · ${requested.map((t) => t.name).join(', ')}`
                : work.label;
    const truncated = events.some(
      (event) => event.payload?.tools_metadata_truncated === true
    );
    return html`<div class="row">
      <span role="status" aria-live="polite">${label}</span>
      ${!this.ended && this.connected && !pending.length && work.startedAt ? html`<span>${work.model} · ${Math.max(0, Math.floor((this.clock - work.startedAt) / 1000))}s${(work.count || 0) > 1 ? ` · ${work.count} requests` : ''}</span>` : nothing}
      ${pending.length ? html`<sl-button variant="neutral" size="small" @click=${() => this.dispatchEvent(new CustomEvent('session-approval-jump', { detail: { id: pending[0].id }, bubbles: true, composed: true }))}>Next pending approval</sl-button>` : nothing}
      ${!this.connected ? html`<span class="transport">Reconnecting · last update ${new Date(this.lastUpdate).toLocaleTimeString()}</span>` : nothing}
      ${truncated ? html`<span class="transport">Additional tool history omitted by capture limits.</span>` : nothing}
      ${this.error ? html`<span>${this.error}</span><sl-button variant="neutral" size="small" @click=${() => this.refresh()}>Retry approvals</sl-button>` : nothing}
    </div>`;
  }
}
