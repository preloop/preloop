import { LitElement, html, PropertyValues } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import { parseUTCDate } from '../utils/date';

/** How often the label re-reads the clock. */
const TICK_MS = 30000;

/**
 * "3m ago", kept honest by its own timer.
 *
 * The Overview used to age this label by bumping a state field on the view
 * itself, which re-rendered the whole page, cards and tables included, every
 * thirty seconds. The clock belongs to the text that shows it, so the timer
 * lives here and nothing above this element renders when it ticks.
 *
 * Renders into the light DOM so the surrounding text stays one readable
 * string for the host and for anything reading `textContent`.
 */
@customElement('relative-time-label')
export class RelativeTimeLabel extends LitElement {
  /** ISO timestamp; when absent the fallback is shown and no timer runs. */
  @property({ type: String }) timestamp: string | null = null;
  /** What to show when there is no timestamp yet. */
  @property({ type: String }) fallback = 'Never';

  @state() private tick = 0;

  private timer: number | null = null;

  protected createRenderRoot() {
    return this;
  }

  connectedCallback(): void {
    super.connectedCallback();
    this.syncTimer();
  }

  disconnectedCallback(): void {
    this.clearTimer();
    super.disconnectedCallback();
  }

  protected updated(changed: PropertyValues): void {
    if (changed.has('timestamp')) {
      this.syncTimer();
    }
  }

  private syncTimer(): void {
    if (this.timestamp) {
      if (this.timer === null) {
        this.timer = window.setInterval(() => {
          this.tick += 1;
        }, TICK_MS);
      }
      return;
    }
    this.clearTimer();
  }

  private clearTimer(): void {
    if (this.timer !== null) {
      window.clearInterval(this.timer);
      this.timer = null;
    }
  }

  render() {
    return html`${formatRelativeTime(this.timestamp, this.fallback)}`;
  }
}

/** Minutes, then hours, then days. Coarse on purpose: it is a freshness hint. */
export function formatRelativeTime(
  value: string | null | undefined,
  fallback = 'Never'
): string {
  if (!value) {
    return fallback;
  }
  const timestamp = parseUTCDate(value).getTime();
  const deltaMinutes = Math.round((Date.now() - timestamp) / 60000);
  if (deltaMinutes < 1) {
    return 'just now';
  }
  if (deltaMinutes < 60) {
    return `${deltaMinutes}m ago`;
  }
  const deltaHours = Math.round(deltaMinutes / 60);
  if (deltaHours < 24) {
    return `${deltaHours}h ago`;
  }
  return `${Math.round(deltaHours / 24)}d ago`;
}

declare global {
  interface HTMLElementTagNameMap {
    'relative-time-label': RelativeTimeLabel;
  }
}
