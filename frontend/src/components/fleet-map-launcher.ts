import { LitElement, css, html, nothing, unsafeCSS } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import './fleet-map';
import { getFleetMap, type FleetMapGraph } from '../fleet-map-api';
import {
  windowMinutes,
  type FleetMapWindow,
} from '../utils/fleet-map-activity';
import { normalizeTimeRangeKey } from '../utils/time-range';
import consoleStyles from '../styles/console-styles.css?inline';

/**
 * The map window that covers a page range. The map's windows end now and
 * stop at 90 days, so a calendar range takes the smallest window that holds
 * it and anything longer than 90 days takes 90 days.
 */
export function fleetMapWindowForRange(
  range: string | null | undefined
): FleetMapWindow {
  switch (normalizeTimeRangeKey(range)) {
    case 'today':
    case 'last-24h':
      return '24h';
    case 'this-week':
    case 'last-7':
      return '7d';
    case 'this-month':
    case 'last-30':
      return '30d';
    case 'all':
      return 'any';
    case 'last-month':
    case 'last-90':
    case 'this-year':
    case 'last-365':
      return '90d';
    default:
      return '30d';
  }
}

/**
 * The Overview's "Fleet map" button and the map it opens. The map is a full
 * screen surface only: nothing is fetched until the button is pressed, and
 * leaving full screen (Esc, the exit button) unmounts it and puts focus
 * back on the button. It opens on the page's period with idle resources and
 * agents that are not onboarded hidden; a change inside the map stays in
 * the map and never writes back to the page range.
 *
 * Every member may open it: the server returns only what the viewer may
 * see, and says so when that is nothing.
 */
@customElement('fleet-map-launcher')
export class FleetMapLauncher extends LitElement {
  /** The page range key (the Usage card's), mapped onto a map window. */
  @property({ attribute: 'time-range' }) timeRange = 'last-30';
  /** Whose saved positions and filters the map reads. */
  @property({ attribute: 'user-id' }) userId = '';
  /** Instance administrators see account-wide realtime events as they come. */
  @property({ type: Boolean, attribute: 'instance-admin' })
  instanceAdmin = false;
  @state() private open = false;
  @state() private graph: FleetMapGraph | null = null;
  @state() private error = '';
  @state() private mapWindow: FleetMapWindow = '30d';
  @state() private includeIdle = false;
  @state() private users: string[] = [];
  private userIds: string[] = [];
  private controller: AbortController | null = null;
  private previousOverflow: string | null = null;

  static styles = [
    unsafeCSS(consoleStyles),
    css`
      :host {
        display: inline-flex;
      }
      .cover {
        position: fixed;
        inset: 0;
        z-index: 1000;
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        gap: var(--sl-spacing-medium);
        background: var(--console-page);
        color: var(--console-body-color);
        font-size: var(--console-text-body);
      }
      /* The button hides on a phone; a map already open stays open. */
      @media (max-width: 640px) {
        [data-testid='fleet-map-open'] {
          display: none;
        }
      }
    `,
  ];

  disconnectedCallback(): void {
    this.controller?.abort();
    // After the map inside has disconnected and put back what it locked.
    queueMicrotask(() => this.releaseScroll());
    window.removeEventListener('keydown', this.onKey);
    super.disconnectedCallback();
  }

  /** Open the map full screen on the page's period and fetch its graph. */
  async show(): Promise<void> {
    if (this.open) return;
    this.mapWindow = fleetMapWindowForRange(this.timeRange);
    this.includeIdle = false;
    this.users = [];
    this.userIds = [];
    this.graph = null;
    this.error = '';
    this.open = true;
    this.previousOverflow ??= document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    window.addEventListener('keydown', this.onKey);
    await this.load();
  }

  /** Unmount the map and give focus back to the button. */
  close(): void {
    if (!this.open) return;
    this.controller?.abort();
    this.open = false;
    this.graph = null;
    this.error = '';
    window.removeEventListener('keydown', this.onKey);
    void this.updateComplete.then(() => {
      // After the map has unmounted and put back what it had locked.
      this.releaseScroll();
      this.renderRoot
        .querySelector<HTMLElement & { focus(): void }>(
          '[data-testid="fleet-map-open"]'
        )
        ?.focus();
    });
  }

  private releaseScroll(): void {
    if (this.previousOverflow === null) return;
    document.body.style.overflow = this.previousOverflow;
    this.previousOverflow = null;
  }

  /** Esc while the graph loads; once drawn, the map owns Esc itself. */
  private onKey = (event: KeyboardEvent): void => {
    if (event.key !== 'Escape' || event.defaultPrevented || this.graph) return;
    event.preventDefault();
    this.close();
  };

  /** ``quiet``: a background refresh keeps the current map on failure. */
  private async load(quiet = false): Promise<void> {
    this.controller?.abort();
    const controller = (this.controller = new AbortController());
    if (!quiet) this.error = '';
    try {
      const graph = await getFleetMap(
        controller.signal,
        windowMinutes(this.mapWindow),
        this.includeIdle,
        this.userIds
      );
      if (!controller.signal.aborted && this.open) this.graph = graph;
    } catch (error) {
      if (!controller.signal.aborted && !(quiet && this.graph))
        this.error =
          error instanceof Error
            ? error.message
            : 'The fleet map could not be loaded.';
    }
  }

  private renderMap() {
    if (this.error)
      return html`<div class="cover" role="alertdialog" aria-label="Fleet map">
        <p>${this.error}</p>
        <div>
          <sl-button size="small" @click=${() => void this.load()}
            >Retry</sl-button
          >
          <sl-button size="small" @click=${() => this.close()}>Close</sl-button>
        </div>
      </div>`;
    if (!this.graph)
      return html`<div
        class="cover"
        role="status"
        data-testid="fleet-map-loading"
      >
        <sl-spinner></sl-spinner>
        Mapping people, devices, agents and providers...
        <sl-button size="small" @click=${() => this.close()}>Close</sl-button>
      </div>`;
    return html`<fleet-map
      fullscreen
      wheel-mode="pan"
      .graph=${this.graph}
      .activityWindow=${this.mapWindow}
      .includeIdle=${this.includeIdle}
      .users=${this.users}
      live
      ?instance-admin=${this.instanceAdmin}
      default-arrangement="spatial"
      positions-scope="inventory"
      default-onboarded-only
      .persistWindow=${false}
      user-id=${this.userId}
      @fleet-map-fullscreen=${(event: CustomEvent<{ fullscreen: boolean }>) => {
        if (!event.detail.fullscreen) this.close();
      }}
      @fleet-map-window-change=${(
        event: CustomEvent<{ window: FleetMapWindow }>
      ) => {
        this.mapWindow = event.detail.window;
        void this.load();
      }}
      @fleet-map-stale=${() => void this.load(true)}
      @fleet-map-users-change=${(
        event: CustomEvent<{ users: string[]; userIds: string[] }>
      ) => {
        this.users = event.detail.users;
        this.userIds = event.detail.userIds;
        void this.load();
      }}
      @fleet-map-idle-change=${(
        event: CustomEvent<{ includeIdle: boolean }>
      ) => {
        this.includeIdle = event.detail.includeIdle;
        void this.load();
      }}
    ></fleet-map>`;
  }

  render() {
    return html`<sl-button
        size="small"
        data-testid="fleet-map-open"
        aria-haspopup="dialog"
        @click=${() => void this.show()}
        ><sl-icon slot="prefix" name="diagram-3"></sl-icon>Fleet map</sl-button
      >${this.open ? this.renderMap() : nothing}`;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'fleet-map-launcher': FleetMapLauncher;
  }
}
