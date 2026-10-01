import { LitElement, css, html, nothing } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import {
  ARTIFACT_STORAGE_SETTINGS_HREF,
  acquireSessionArtifact,
  releaseSessionArtifact,
  unavailableReason,
  type SessionArtifactLoad,
} from '../utils/session-artifacts';

/**
 * Lazy screenshot thumbnail for one session artifact.
 *
 * Bytes are fetched with the user's token only once the thumbnail scrolls
 * near the viewport, held as an object URL and released (revoked when no one
 * else holds it) on disconnect. A 410 from the byte route renders a grey
 * placeholder with the reason and, unless `compact`, a link to the Settings
 * storage card. Clicking an available image emits `thumbnail-open`.
 */
@customElement('browser-step-thumbnail')
export class BrowserStepThumbnail extends LitElement {
  @property({ type: String }) sessionId = '';
  @property({ type: String }) artifactId = '';
  /** Availability from step metadata; non-available skips the fetch. */
  @property({ type: String }) availability = 'available';
  @property({ type: String }) label = 'Screenshot';
  /** Small strip variant: no link, shorter reason. */
  @property({ type: Boolean, reflect: true }) compact = false;

  @state() private load: SessionArtifactLoad | null = null;
  private held: { sessionId: string; artifactId: string } | null = null;
  private observer: IntersectionObserver | null = null;
  private visible = false;

  static styles = css`
    :host {
      display: inline-block;
      width: 160px;
      height: 100px;
      flex: none;
    }
    :host([compact]) {
      width: 72px;
      height: 46px;
    }
    .box {
      width: 100%;
      height: 100%;
      border-radius: 6px;
      overflow: hidden;
      border: 1px solid var(--sl-color-neutral-300, #d1d5db);
      background: var(--sl-color-neutral-100, #f3f4f6);
      display: flex;
      align-items: center;
      justify-content: center;
      box-sizing: border-box;
    }
    button.box {
      padding: 0;
      cursor: zoom-in;
    }
    button.box:focus-visible {
      outline: 2px solid var(--sl-color-primary-500, #3b82f6);
      outline-offset: 2px;
    }
    img {
      width: 100%;
      height: 100%;
      object-fit: cover;
      object-position: top left;
      display: block;
    }
    .gone {
      flex-direction: column;
      gap: 0.25rem;
      padding: 0.35rem;
      background: var(--sl-color-neutral-200, #e5e7eb);
      color: var(--sl-color-neutral-700, #374151);
      font-size: 0.7rem;
      line-height: 1.2;
      text-align: center;
    }
    :host([compact]) .gone {
      font-size: 0.6rem;
    }
    .gone a {
      color: var(--sl-color-primary-600, #2563eb);
    }
    .muted {
      color: var(--sl-color-neutral-500, #6b7280);
      font-size: 0.7rem;
    }
  `;

  connectedCallback(): void {
    super.connectedCallback();
    if (typeof IntersectionObserver === 'undefined') {
      this.visible = true;
      this.sync();
      return;
    }
    this.observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          this.visible = true;
          this.observer?.disconnect();
          this.observer = null;
          this.sync();
        }
      },
      { rootMargin: '200px' }
    );
    this.observer.observe(this);
  }

  disconnectedCallback(): void {
    this.observer?.disconnect();
    this.observer = null;
    this.release();
    super.disconnectedCallback();
  }

  protected updated(changed: Map<string, unknown>): void {
    if (
      changed.has('sessionId') ||
      changed.has('artifactId') ||
      changed.has('availability')
    ) {
      this.sync();
    }
  }

  private sync(): void {
    const wanted =
      this.visible &&
      this.sessionId &&
      this.artifactId &&
      this.availability === 'available'
        ? { sessionId: this.sessionId, artifactId: this.artifactId }
        : null;
    if (
      wanted &&
      this.held &&
      wanted.sessionId === this.held.sessionId &&
      wanted.artifactId === this.held.artifactId
    ) {
      return;
    }
    this.release();
    this.load = null;
    if (!wanted) return;
    this.held = wanted;
    void acquireSessionArtifact(wanted.sessionId, wanted.artifactId).then(
      (result) => {
        if (this.held === wanted) this.load = result;
      }
    );
  }

  private release(): void {
    if (this.held) {
      releaseSessionArtifact(this.held.sessionId, this.held.artifactId);
    }
    this.held = null;
  }

  private open(): void {
    this.dispatchEvent(
      new CustomEvent('thumbnail-open', { bubbles: true, composed: true })
    );
  }

  render() {
    const gone =
      this.availability !== 'available'
        ? this.availability
        : this.load?.status === 'gone'
          ? this.load.availability
          : null;
    if (gone) {
      return html`<div
        class="box gone"
        data-testid="screenshot-unavailable"
        data-availability=${gone}
        title=${unavailableReason(gone)}
      >
        <span
          >${this.compact ? `Screenshot ${gone}` : unavailableReason(gone)}</span
        >
        ${
          this.compact
            ? nothing
            : html`<a href=${ARTIFACT_STORAGE_SETTINGS_HREF}
                >Storage settings</a
              >`
        }
      </div>`;
    }
    if (this.load?.status === 'ok' && this.compact) {
      // The strip wraps the thumbnail in its own button; no nested control.
      return html`<div class="box">
        <img
          data-testid="screenshot-thumbnail"
          src=${this.load.url}
          alt=${this.label}
        />
      </div>`;
    }
    if (this.load?.status === 'ok') {
      return html`<button
        class="box"
        type="button"
        aria-label=${`Open full-size ${this.label}`}
        @click=${() => this.open()}
      >
        <img
          data-testid="screenshot-thumbnail"
          src=${this.load.url}
          alt=${this.label}
        />
      </button>`;
    }
    if (this.load?.status === 'error') {
      return html`<div class="box muted" title=${this.load.message}>
        Image failed to load
      </div>`;
    }
    return html`<div class="box muted" aria-busy="true">Loading...</div>`;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'browser-step-thumbnail': BrowserStepThumbnail;
  }
}
