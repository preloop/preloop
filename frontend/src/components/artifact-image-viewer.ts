import { LitElement, css, html, nothing } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import {
  ARTIFACT_STORAGE_SETTINGS_HREF,
  acquireSessionArtifact,
  releaseSessionArtifact,
  unavailableReason,
  type SessionArtifactLoad,
} from '../utils/session-artifacts';

/** One image the viewer can page to. */
export interface ArtifactViewerImage {
  /** Stable id of the item the image belongs to (e.g. a browser step key). */
  key: string;
  artifactId: string | null;
  availability?: string | null;
  title: string;
  caption?: string | null;
}

/**
 * Full-size viewer for session image artifacts (browser-step screenshots
 * today, other image artifacts later).
 *
 * The viewer only reads stored bytes through the account artifact route; it
 * never sees the ingest payload. Escape closes, ArrowLeft/ArrowRight page.
 * Emits `viewer-close` and `viewer-navigate` ({ index, key }).
 */
@customElement('artifact-image-viewer')
export class ArtifactImageViewer extends LitElement {
  @property({ type: String }) sessionId = '';
  @property({ type: Array }) images: ArtifactViewerImage[] = [];
  /** Index into `images`; `-1` means closed. */
  @property({ type: Number }) index = -1;

  @state() private load: SessionArtifactLoad | null = null;
  private heldArtifactId: string | null = null;
  private previouslyFocused: Element | null = null;

  static styles = css`
    :host {
      display: contents;
    }
    .backdrop {
      position: fixed;
      inset: 0;
      z-index: 1000;
      background: rgb(0 0 0 / 0.78);
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      gap: 0.75rem;
      padding: 1.5rem;
    }
    .frame {
      position: relative;
      display: flex;
      align-items: center;
      justify-content: center;
      max-width: 100%;
      max-height: calc(100vh - 8rem);
    }
    img {
      max-width: min(1400px, calc(100vw - 8rem));
      max-height: calc(100vh - 8rem);
      object-fit: contain;
      border-radius: 6px;
      background: #fff;
      box-shadow: 0 10px 40px rgb(0 0 0 / 0.5);
    }
    .placeholder {
      min-width: 320px;
      min-height: 200px;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      gap: 0.5rem;
      padding: 1.5rem;
      border-radius: 6px;
      background: var(--sl-color-neutral-200, #e5e7eb);
      color: var(--sl-color-neutral-700, #374151);
      text-align: center;
    }
    .placeholder a {
      color: var(--sl-color-primary-600, #2563eb);
    }
    .caption {
      color: #f3f4f6;
      max-width: min(1000px, 90vw);
      text-align: center;
      font-size: 0.875rem;
      overflow-wrap: anywhere;
    }
    .caption .title {
      font-weight: 600;
    }
    button {
      border: 0;
      border-radius: 999px;
      width: 2.5rem;
      height: 2.5rem;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      background: rgb(255 255 255 / 0.15);
      color: #fff;
      cursor: pointer;
      font-size: 1.1rem;
    }
    button:hover:not([disabled]),
    button:focus-visible {
      background: rgb(255 255 255 / 0.3);
      outline: 2px solid #fff;
    }
    button[disabled] {
      opacity: 0.35;
      cursor: default;
    }
    .nav {
      display: flex;
      align-items: center;
      gap: 1rem;
    }
    .close {
      position: absolute;
      top: 1rem;
      right: 1rem;
    }
    .counter {
      color: #e5e7eb;
      font-size: 0.8rem;
      min-width: 4rem;
      text-align: center;
    }
  `;

  get open(): boolean {
    return this.index >= 0 && this.index < this.images.length;
  }

  connectedCallback(): void {
    super.connectedCallback();
    window.addEventListener('keydown', this.handleKeydown);
  }

  disconnectedCallback(): void {
    window.removeEventListener('keydown', this.handleKeydown);
    this.releaseHeld();
    super.disconnectedCallback();
  }

  protected willUpdate(changed: Map<string, unknown>): void {
    if (
      changed.has('index') ||
      changed.has('images') ||
      changed.has('sessionId')
    ) {
      const wasOpen =
        changed.has('index') && (changed.get('index') as number) >= 0;
      if (this.open && !wasOpen && changed.has('index')) {
        this.previouslyFocused = document.activeElement;
      }
      this.syncArtifact();
    }
  }

  protected updated(changed: Map<string, unknown>): void {
    if (changed.has('index') && this.open) {
      this.renderRoot.querySelector<HTMLButtonElement>('.close')?.focus();
    }
  }

  private syncArtifact(): void {
    const image = this.open ? this.images[this.index] : null;
    const wanted =
      image && image.artifactId && this.sessionId ? image.artifactId : null;
    if (wanted === this.heldArtifactId) return;
    this.releaseHeld();
    this.load = null;
    if (!wanted) return;
    this.heldArtifactId = wanted;
    const sessionId = this.sessionId;
    void acquireSessionArtifact(sessionId, wanted).then((result) => {
      if (this.heldArtifactId === wanted) this.load = result;
    });
  }

  private releaseHeld(): void {
    if (this.heldArtifactId && this.sessionId) {
      releaseSessionArtifact(this.sessionId, this.heldArtifactId);
    }
    this.heldArtifactId = null;
  }

  private handleKeydown = (event: KeyboardEvent): void => {
    if (!this.open) return;
    if (event.key === 'Escape') {
      event.preventDefault();
      this.close();
    } else if (event.key === 'ArrowLeft') {
      event.preventDefault();
      this.go(-1);
    } else if (event.key === 'ArrowRight') {
      event.preventDefault();
      this.go(1);
    }
  };

  close(): void {
    if (!this.open) return;
    this.releaseHeld();
    this.load = null;
    this.dispatchEvent(
      new CustomEvent('viewer-close', { bubbles: true, composed: true })
    );
    const target = this.previouslyFocused as HTMLElement | null;
    this.previouslyFocused = null;
    target?.focus?.();
  }

  go(delta: number): void {
    const next = this.index + delta;
    if (next < 0 || next >= this.images.length) return;
    this.dispatchEvent(
      new CustomEvent('viewer-navigate', {
        detail: { index: next, key: this.images[next].key },
        bubbles: true,
        composed: true,
      })
    );
  }

  private renderBody(image: ArtifactViewerImage) {
    const gone =
      this.load?.status === 'gone'
        ? this.load.availability
        : !image.artifactId
          ? 'missing'
          : image.availability && image.availability !== 'available'
            ? image.availability
            : null;
    if (gone) {
      return html`<div class="placeholder" data-testid="viewer-unavailable">
        <strong
          >${gone === 'missing' ? 'No screenshot for this step.' : unavailableReason(gone)}</strong
        >
        ${
          gone === 'missing'
            ? nothing
            : html`<a href=${ARTIFACT_STORAGE_SETTINGS_HREF}
                >Review storage and retention in Settings</a
              >`
        }
      </div>`;
    }
    if (!this.load) {
      return html`<div class="placeholder">Loading screenshot...</div>`;
    }
    if (this.load.status !== 'ok') {
      return html`<div class="placeholder">
        ${this.load.status === 'error' ? this.load.message : 'Screenshot unavailable.'}
      </div>`;
    }
    return html`<img
      data-testid="viewer-image"
      src=${this.load.url}
      alt=${`Full-size screenshot: ${image.title}`}
    />`;
  }

  render() {
    if (!this.open) return nothing;
    const image = this.images[this.index];
    return html`
      <div
        class="backdrop"
        role="dialog"
        aria-modal="true"
        aria-label=${`Screenshot viewer: ${image.title}`}
        @click=${(event: Event) => {
          if (event.target === event.currentTarget) this.close();
        }}
      >
        <button
          class="close"
          aria-label="Close viewer"
          @click=${() => this.close()}
        >
          ✕
        </button>
        <div class="frame">${this.renderBody(image)}</div>
        <div class="caption">
          <div class="title">${image.title}</div>
          ${image.caption ? html`<div>${image.caption}</div>` : nothing}
        </div>
        <div class="nav">
          <button
            aria-label="Previous screenshot"
            ?disabled=${this.index <= 0}
            @click=${() => this.go(-1)}
          >
            ‹
          </button>
          <span class="counter">${this.index + 1} / ${this.images.length}</span>
          <button
            aria-label="Next screenshot"
            ?disabled=${this.index >= this.images.length - 1}
            @click=${() => this.go(1)}
          >
            ›
          </button>
        </div>
      </div>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'artifact-image-viewer': ArtifactImageViewer;
  }
}
