/**
 * Brokered live desktop viewer (#870).
 *
 * Opens a short-lived view on a desktop endpoint, connects the viewer socket
 * and renders the remote display with the vendored noVNC in view-only mode.
 * The server enforces view-only by dropping input messages; `viewOnly` here
 * only keeps noVNC from sending them. Nothing is recorded: frames live in the
 * canvas and nowhere else.
 *
 * Used by the agent page's Desktop tab and the session view's side panel.
 */
import { LitElement, css, html, nothing } from 'lit';
import { customElement, property, query, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import type RFB from '@novnc/novnc';
import {
  DesktopApiError,
  type DesktopView,
  type DesktopViewHolder,
  createDesktopView,
  desktopViewerSocketUrl,
  endDesktopView,
  getDesktopView,
  releaseDesktopControl,
  requestDesktopControl,
} from '../desktop-api';

export type DesktopViewerState =
  | 'idle'
  | 'connecting'
  | 'live'
  | 'unreachable'
  | 'gone'
  | 'ended'
  | 'busy'
  | 'forbidden'
  | 'error';

/** User-facing copy, kept together so wording reviews touch one place. */
export const DESKTOP_VIEW_STRINGS = {
  open: 'Open live view',
  end: 'End',
  reopen: 'Open a new view',
  requestControl: 'Request control',
  releaseControl: 'Release control',
  controlReasonLabel: 'Why do you need control?',
  controlConfirm: 'Take control',
  cancel: 'Cancel',
  bannerViewOnly: 'View only. Nothing is recorded.',
  bannerControl: 'You have control of this desktop. Nothing is recorded.',
  waitingForApproval: 'Waiting for another admin to approve control',
  controlNotApproved: 'Control was not approved. The view stays view only.',
  connecting: 'Connecting to the desktop...',
  unreachable:
    'The desktop host did not answer. Check that preloop-desktop-tunnel.service is running on the host.',
  displayUnavailable:
    'The host is connected but its display is not available (x11vnc is not running).',
  gone: 'The desktop host disconnected.',
  forbidden: 'You do not have permission to view this desktop.',
  busy: (holder: DesktopViewHolder | null) =>
    holder
      ? `${holder.username || 'Another user'} is viewing this desktop (since ${formatSince(holder.since)}). One viewer at a time.`
      : 'Someone else is viewing this desktop. One viewer at a time.',
  ended: (reason: string | null) =>
    `View ended${reason ? `: ${CLOSED_REASON_TEXT[reason] ?? reason}` : ''}.`,
  remaining: (text: string) => `${text} left`,
};

const CLOSED_REASON_TEXT: Record<string, string> = {
  user_closed: 'closed',
  expired: 'time limit reached',
  idle: 'idle for too long',
  agent_disconnected: 'host disconnected',
  viewer_disconnected: 'connection lost',
  desktop_agent_unreachable: 'host unreachable',
  display_unavailable: 'display unavailable',
  server_restart: 'server restarted',
};

/** Viewer socket close codes from the #870 contract. */
const CLOSE_CODE_REASON: Record<number, string> = {
  1000: 'user_closed',
  4400: 'viewer_disconnected',
  4404: 'user_closed',
  4408: 'idle',
  4410: 'agent_disconnected',
  4411: 'expired',
  4412: 'user_closed',
  4413: 'display_unavailable',
  4415: 'desktop_agent_unreachable',
  4499: 'server_restart',
};

/**
 * Viewer state and closed reason for a socket close code, or null when the
 * code says nothing specific (the view row is then asked for its reason).
 */
export function outcomeForCloseCode(
  code: number
): { state: DesktopViewerState; reason: string | null } | null {
  if (code === 4403) return { state: 'forbidden', reason: null };
  if (code === 4409) return { state: 'busy', reason: null };
  const reason = CLOSE_CODE_REASON[code];
  if (!reason) return null;
  return outcomeForClosedReason(reason);
}

/** Viewer state for a view's `closed_reason`. */
export function outcomeForClosedReason(reason: string | null): {
  state: DesktopViewerState;
  reason: string | null;
} {
  if (
    reason === 'desktop_agent_unreachable' ||
    reason === 'display_unavailable'
  )
    return { state: 'unreachable', reason };
  if (reason === 'agent_disconnected') return { state: 'gone', reason };
  return { state: 'ended', reason };
}

/** `m:ss` (or `h:mm:ss`) until `expiresAt`, floored at zero. */
export function formatRemaining(expiresAt: string, now = Date.now()): string {
  const ms = Date.parse(expiresAt) - now;
  const total = Number.isFinite(ms) ? Math.max(0, Math.floor(ms / 1000)) : 0;
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = String(total % 60).padStart(2, '0');
  return hours > 0
    ? `${hours}:${String(minutes).padStart(2, '0')}:${seconds}`
    : `${minutes}:${seconds}`;
}

function formatSince(iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleTimeString();
}

type RFBConstructor = typeof RFB;

/** Loads noVNC on first use so pages without a desktop never fetch it. */
let rfbModule: Promise<RFBConstructor> | null = null;
function loadRFB(): Promise<RFBConstructor> {
  rfbModule ??= import('@novnc/novnc').then((module) => module.default);
  return rfbModule;
}

@customElement('desktop-view')
export class DesktopViewElement extends LitElement {
  /** Endpoint to open (`id` or host `endpoint_id`). */
  @property({ type: String }) endpointId = '';

  /** Session the view is opened from, recorded in the audit event. */
  @property({ type: String }) runtimeSessionId = '';

  /** Whether the caller may take control; hides "Request control" if not. */
  @property({ type: Boolean }) canControl = false;

  /** Opens a view as soon as the element connects (the side panel case). */
  @property({ type: Boolean }) autoStart = false;

  /** How often a pending control approval is re-checked. */
  @property({ attribute: false }) controlPollMs = 3000;

  @state() viewerState: DesktopViewerState = 'idle';
  @state() private view: DesktopView | null = null;
  @state() private closedReason: string | null = null;
  @state() private holder: DesktopViewHolder | null = null;
  @state() private errorText = '';
  @state() private remaining = '';
  @state() private askingReason = false;
  @state() private controlBusy = false;
  /** Neutral note after a control request was not approved. */
  @state() private controlNote = '';

  @query('.screen') private screen?: HTMLDivElement;

  private rfb: RFB | null = null;
  private socket: WebSocket | null = null;
  private ticker: number | null = null;
  private controlPoll: number | null = null;
  /** Bumped on every open/teardown so late callbacks from an old view no-op. */
  private generation = 0;

  static styles = css`
    :host {
      display: flex;
      flex-direction: column;
      gap: var(--sl-spacing-small);
      min-width: 0;
    }
    .bar {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--sl-spacing-x-small);
    }
    .banner {
      flex: 1 1 auto;
      font-size: var(--sl-font-size-small);
      color: var(--sl-color-neutral-700);
    }
    .control-waiting {
      display: inline-flex;
      align-items: center;
      gap: var(--sl-spacing-2x-small);
      font-size: var(--sl-font-size-small);
      color: var(--sl-color-neutral-700);
    }
    .countdown {
      font-variant-numeric: tabular-nums;
      font-size: var(--sl-font-size-small);
      color: var(--sl-color-neutral-600);
    }
    .screen {
      position: relative;
      width: 100%;
      aspect-ratio: 16 / 10;
      background: var(--sl-color-neutral-100);
      border: 1px solid var(--sl-color-neutral-200);
      border-radius: var(--sl-border-radius-medium);
      overflow: hidden;
    }
    .screen[hidden] {
      display: none;
    }
    .status {
      display: flex;
      align-items: center;
      gap: var(--sl-spacing-small);
      color: var(--sl-color-neutral-700);
    }
    .reason {
      display: flex;
      gap: var(--sl-spacing-x-small);
      align-items: flex-end;
    }
    .reason sl-input {
      flex: 1 1 auto;
    }
  `;

  connectedCallback(): void {
    super.connectedCallback();
    if (this.autoStart && this.viewerState === 'idle') {
      void this.open();
    }
  }

  disconnectedCallback(): void {
    super.disconnectedCallback();
    // Leaving the page ends the view: a view nobody is watching should not
    // hold the one viewer slot until it expires.
    void this.end();
  }

  /** Creates a view and connects to it. */
  async open(): Promise<void> {
    if (!this.endpointId) return;
    this.teardown();
    const generation = ++this.generation;
    this.viewerState = 'connecting';
    this.closedReason = null;
    this.holder = null;
    this.errorText = '';
    let view: DesktopView;
    try {
      view = await createDesktopView(this.endpointId, {
        runtimeSessionId: this.runtimeSessionId || undefined,
      });
    } catch (error) {
      if (generation !== this.generation) return;
      this.applyCreateError(error);
      return;
    }
    if (generation !== this.generation) {
      // Ended or unmounted while the view was being created: `end()` saw no
      // view to delete, so this one is ours to close or it would hold the
      // viewer slot until it expires.
      void endDesktopView(view.view_id).catch(() => undefined);
      return;
    }
    this.view = view;
    this.startTicker();
    await this.updateComplete;
    await this.connectViewer(view, generation);
  }

  /** Ends the current view, if any. */
  async end(): Promise<void> {
    const view = this.view;
    const wasActive =
      this.viewerState === 'connecting' || this.viewerState === 'live';
    this.generation++;
    this.teardown();
    if (wasActive) {
      this.viewerState = 'ended';
      this.closedReason = 'user_closed';
    }
    if (view && view.state !== 'closed') {
      try {
        await endDesktopView(view.view_id);
      } catch {
        // The view expires server-side anyway; nothing more to do here.
      }
    }
  }

  private applyCreateError(error: unknown): void {
    if (error instanceof DesktopApiError) {
      if (error.code === 'desktop_view_busy') {
        this.viewerState = 'busy';
        this.holder = error.holder;
        return;
      }
      if (error.status === 403) {
        this.viewerState = 'forbidden';
        return;
      }
      if (
        error.code === 'desktop_not_available' ||
        error.code === 'desktop_endpoint_not_found'
      ) {
        this.viewerState = 'gone';
        return;
      }
      this.viewerState = 'error';
      this.errorText = error.message;
      return;
    }
    this.viewerState = 'error';
    this.errorText = error instanceof Error ? error.message : String(error);
  }

  private async connectViewer(
    view: DesktopView,
    generation: number
  ): Promise<void> {
    let RFBClass: RFBConstructor;
    try {
      RFBClass = await loadRFB();
    } catch (error) {
      if (generation !== this.generation) return;
      this.viewerState = 'error';
      this.errorText = 'The desktop viewer failed to load.';
      return;
    }
    if (generation !== this.generation || !this.screen) return;
    const token = localStorage.getItem('accessToken') || '';
    let socketUrl: string;
    try {
      socketUrl = desktopViewerSocketUrl(view.ws_url, token);
    } catch (error) {
      this.teardown();
      this.viewerState = 'error';
      this.errorText = error instanceof Error ? error.message : String(error);
      void endDesktopView(view.view_id).catch(() => undefined);
      return;
    }
    // Our own socket rather than a URL: noVNC reports a close as clean or
    // not, and the contract's close code is what tells the states apart.
    const socket = new WebSocket(socketUrl, ['binary']);
    socket.binaryType = 'arraybuffer';
    socket.addEventListener('close', (event) => {
      if (generation !== this.generation) return;
      void this.handleSocketClose(event.code, view.view_id, generation);
    });
    this.socket = socket;
    const rfb = new RFBClass(this.screen, socket, { shared: true });
    rfb.viewOnly = view.mode !== 'control';
    rfb.scaleViewport = true;
    rfb.resizeSession = false;
    rfb.focusOnClick = false;
    rfb.background = 'transparent';
    rfb.addEventListener('connect', () => {
      if (generation !== this.generation) return;
      this.viewerState = 'live';
    });
    this.rfb = rfb;
  }

  private async handleSocketClose(
    code: number,
    viewId: string,
    generation: number
  ): Promise<void> {
    this.teardown();
    let outcome = outcomeForCloseCode(code);
    if (!outcome) {
      try {
        const latest = await getDesktopView(viewId);
        if (generation !== this.generation) return;
        this.view = latest;
        outcome = outcomeForClosedReason(
          latest.closed_reason ?? 'viewer_disconnected'
        );
      } catch {
        outcome = outcomeForClosedReason('viewer_disconnected');
      }
    }
    if (generation !== this.generation) return;
    if (this.view) this.view = { ...this.view, state: 'closed' };
    this.viewerState = outcome.state;
    this.closedReason = outcome.reason;
  }

  private teardown(): void {
    if (this.ticker !== null) {
      window.clearInterval(this.ticker);
      this.ticker = null;
    }
    this.stopControlPoll();
    const rfb = this.rfb;
    this.rfb = null;
    this.socket = null;
    if (rfb) {
      try {
        rfb.disconnect();
      } catch {
        // Already disconnected.
      }
    }
    this.askingReason = false;
  }

  private startTicker(): void {
    const tick = () => {
      const expires = this.view?.expires_at;
      this.remaining = expires ? formatRemaining(expires) : '';
    };
    tick();
    this.ticker = window.setInterval(tick, 1000);
  }

  private get isControl(): boolean {
    return this.view?.mode === 'control';
  }

  private get awaitingApproval(): boolean {
    return !this.isControl && !!this.view?.control_request;
  }

  /** Applies a fresh view row: mode, input gate and the approval wait. */
  private applyView(view: DesktopView): void {
    const wasWaiting = this.awaitingApproval;
    this.view = view;
    if (this.rfb) this.rfb.viewOnly = view.mode !== 'control';
    if (this.awaitingApproval) {
      this.startControlPoll();
      return;
    }
    this.stopControlPoll();
    this.controlNote =
      wasWaiting && view.mode !== 'control'
        ? DESKTOP_VIEW_STRINGS.controlNotApproved
        : '';
  }

  private startControlPoll(): void {
    if (this.controlPoll !== null) return;
    const generation = this.generation;
    this.controlPoll = window.setInterval(async () => {
      if (!this.view || generation !== this.generation) return;
      try {
        const view = await getDesktopView(this.view.view_id);
        if (generation === this.generation) this.applyView(view);
      } catch {
        // Transient: the next tick asks again.
      }
    }, this.controlPollMs);
  }

  private stopControlPoll(): void {
    if (this.controlPoll !== null) {
      window.clearInterval(this.controlPoll);
      this.controlPoll = null;
    }
  }

  private async submitControlRequest(): Promise<void> {
    const input = this.renderRoot.querySelector<HTMLInputElement>(
      'sl-input.control-reason'
    );
    const reason = (input?.value || '').trim();
    if (!this.view || reason.length < 3) {
      input?.focus();
      return;
    }
    this.controlBusy = true;
    try {
      const view = await requestDesktopControl(this.view.view_id, reason);
      this.errorText = '';
      this.controlNote = '';
      this.applyView(view);
      this.askingReason = false;
    } catch (error) {
      this.errorText =
        error instanceof Error ? error.message : 'Control request failed.';
    } finally {
      this.controlBusy = false;
    }
  }

  private async releaseControl(): Promise<void> {
    if (!this.view) return;
    this.controlBusy = true;
    try {
      await releaseDesktopControl(this.view.view_id);
      this.view = { ...this.view, mode: 'view_only', control_expires_at: null };
      if (this.rfb) this.rfb.viewOnly = true;
    } catch (error) {
      this.errorText =
        error instanceof Error ? error.message : 'Release failed.';
    } finally {
      this.controlBusy = false;
    }
  }

  private renderStatus() {
    const s = DESKTOP_VIEW_STRINGS;
    switch (this.viewerState) {
      case 'connecting':
        return html`<div class="status" data-state="connecting">
          <sl-spinner></sl-spinner>${s.connecting}
        </div>`;
      case 'unreachable':
        return html`<sl-alert variant="warning" open data-state="unreachable">
          <sl-icon slot="icon" name="plug"></sl-icon>
          ${
            this.closedReason === 'display_unavailable'
              ? s.displayUnavailable
              : s.unreachable
          }
        </sl-alert>`;
      case 'gone':
        return html`<sl-alert variant="warning" open data-state="gone">
          <sl-icon slot="icon" name="pc-display"></sl-icon>${s.gone}
        </sl-alert>`;
      case 'busy':
        return html`<sl-alert variant="neutral" open data-state="busy">
          <sl-icon slot="icon" name="person"></sl-icon>${s.busy(this.holder)}
        </sl-alert>`;
      case 'forbidden':
        return html`<sl-alert variant="danger" open data-state="forbidden">
          <sl-icon slot="icon" name="shield-lock"></sl-icon>${s.forbidden}
        </sl-alert>`;
      case 'ended':
        return html`<div class="status" data-state="ended">
          ${s.ended(this.closedReason)}
        </div>`;
      case 'error':
        return html`<sl-alert variant="danger" open data-state="error">
          <sl-icon slot="icon" name="exclamation-triangle"></sl-icon>
          ${this.errorText}
        </sl-alert>`;
      default:
        return nothing;
    }
  }

  private renderControls() {
    const s = DESKTOP_VIEW_STRINGS;
    const active =
      this.viewerState === 'connecting' || this.viewerState === 'live';
    if (!active) {
      return html`<div class="bar">
        <sl-button
          class="open"
          variant="primary"
          size="small"
          @click=${() => void this.open()}
        >
          <sl-icon slot="prefix" name="display"></sl-icon>
          ${this.viewerState === 'idle' ? s.open : s.reopen}
        </sl-button>
      </div>`;
    }
    return html`
      <div class="bar">
        <span class="banner" data-testid="desktop-banner"
          >${this.isControl ? s.bannerControl : s.bannerViewOnly}</span
        >
        ${
          this.remaining
            ? html`<span class="countdown" data-testid="desktop-countdown"
                >${s.remaining(this.remaining)}</span
              >`
            : nothing
        }
        ${
          this.canControl && this.viewerState === 'live'
            ? this.isControl
              ? html`<sl-button
                  class="release-control"
                  size="small"
                  ?loading=${this.controlBusy}
                  @click=${() => void this.releaseControl()}
                  >${s.releaseControl}</sl-button
                >`
              : this.awaitingApproval
                ? html`<span
                    class="control-waiting"
                    data-testid="desktop-control-waiting"
                    role="status"
                    ><sl-spinner></sl-spinner>${s.waitingForApproval}</span
                  >`
                : html`<sl-button
                    class="request-control"
                    size="small"
                    ?disabled=${this.askingReason}
                    @click=${() => (this.askingReason = true)}
                    >${s.requestControl}</sl-button
                  >`
            : nothing
        }
        <sl-button
          class="end"
          size="small"
          variant="danger"
          outline
          @click=${() => void this.end()}
          >${s.end}</sl-button
        >
      </div>
      ${
        this.askingReason
          ? html`<div class="reason">
              <sl-input
                class="control-reason"
                size="small"
                label=${s.controlReasonLabel}
                minlength="3"
                maxlength="500"
              ></sl-input>
              <sl-button
                size="small"
                variant="primary"
                ?loading=${this.controlBusy}
                @click=${() => void this.submitControlRequest()}
                >${s.controlConfirm}</sl-button
              >
              <sl-button
                size="small"
                @click=${() => (this.askingReason = false)}
                >${s.cancel}</sl-button
              >
            </div>`
          : nothing
      }
      ${
        this.controlNote && this.viewerState === 'live'
          ? html`<span class="control-waiting" role="status"
              >${this.controlNote}</span
            >`
          : nothing
      }
      ${
        this.errorText && this.viewerState === 'live'
          ? html`<sl-alert variant="danger" open>${this.errorText}</sl-alert>`
          : nothing
      }
    `;
  }

  render() {
    const active =
      this.viewerState === 'connecting' || this.viewerState === 'live';
    return html`
      ${this.renderControls()} ${this.renderStatus()}
      <div
        class="screen"
        data-state=${this.viewerState}
        ?hidden=${!active}
        aria-label="Live desktop"
      ></div>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'desktop-view': DesktopViewElement;
  }
}
