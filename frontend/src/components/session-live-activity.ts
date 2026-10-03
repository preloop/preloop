import '@shoelace-style/shoelace/dist/components/button/button.js';
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
  @state() private now = Date.now();
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
  `;
  connectedCallback(): void {
    super.connectedCallback();
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
      this.now = Date.now();
      if (
        this.requests.some(
          (r) =>
            r.status === 'pending' && !isUnexpiredPendingRequest(r, this.now)
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
    super.disconnectedCallback();
  }
  protected updated(changed: Map<string | number | symbol, unknown>): void {
    if (changed.has('sessionId')) void this.open();
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
          now: this.now,
        },
        bubbles: true,
        composed: true,
      })
    );
  }
  render() {
    const pending = this.requests.filter((r) =>
      isUnexpiredPendingRequest(r, this.now)
    );
    const events = [...this.events, ...this.transient];
    const work = sessionWork(events, this.ended, this.now);
    const tools = sessionTools(events, this.activity);
    const requested = tools.filter((t) => t.phase === 'requested');
    const runningEvidence = tools.filter((t) => t.phase === 'running');
    const running = runningEvidence.filter(
      (t) => this.now - Date.parse(t.timestamp || '') < 120000
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
      ${!this.ended && this.connected && !pending.length && work.startedAt ? html`<span>${work.model} · ${Math.max(0, Math.floor((this.now - work.startedAt) / 1000))}s${(work.count || 0) > 1 ? ` · ${work.count} requests` : ''}</span>` : nothing}
      ${pending.length ? html`<sl-button variant="neutral" size="small" @click=${() => this.dispatchEvent(new CustomEvent('session-approval-jump', { detail: { id: pending[0].id }, bubbles: true, composed: true }))}>Next pending approval</sl-button>` : nothing}
      ${!this.connected ? html`<span class="transport">Reconnecting · last update ${new Date(this.lastUpdate).toLocaleTimeString()}</span>` : nothing}
      ${truncated ? html`<span class="transport">Additional tool history omitted by capture limits.</span>` : nothing}
      ${this.error ? html`<span>${this.error}</span><sl-button variant="neutral" size="small" @click=${() => this.refresh()}>Retry approvals</sl-button>` : nothing}
    </div>`;
  }
}
