import { LitElement, css, html, nothing } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import type { RuntimeSessionActivityItem } from '../types';
import {
  ARTIFACT_EXCERPT_BYTES,
  ARTIFACT_EXCERPT_LINES,
  ARTIFACT_EXPANDED_LINES,
  ARTIFACT_KIND_ICONS,
  ARTIFACT_STORAGE_SETTINGS_HREF,
  acquireSessionArtifact,
  artifactUnavailableReason,
  downloadSessionArtifact,
  formatArtifactBytes,
  orderedArtifactLabels,
  readSessionArtifactText,
  releaseSessionArtifact,
  type ArtifactTextLoad,
  type ArtifactView,
  type SessionArtifactLoad,
} from '../utils/session-artifacts';
import './browser-step-thumbnail';

/** Upper bound for "Show more": enough bytes for 200 ordinary lines. */
const EXPANDED_BYTES = 256 * 1024;

/**
 * One `artifact` activity in the session timeline (preloop/preloop#1083).
 *
 * Images show a lazy thumbnail and open the shared full-size viewer; text
 * kinds show the first lines of a 4 KB read, expandable to 200 lines; audio
 * loads only when the user presses play; everything else shows its facts and
 * a download. Bytes are never fetched for an artifact known to be evicted or
 * expired, and a 410 from the byte route turns the row grey with the reason.
 *
 * The host is focusable; Enter opens the artifact (viewer for images, expand
 * for text, play for audio, download otherwise).
 *
 * Events (bubbling, composed): `artifact-open` ({ artifactId }) for images,
 * `artifact-scrub` ({ artifactId }) for the lineage link.
 */
@customElement('session-artifact-row')
export class SessionArtifactRow extends LitElement {
  @property({ attribute: false }) item: RuntimeSessionActivityItem | null =
    null;
  @property({ attribute: false }) artifact: ArtifactView | null = null;
  @property({ type: String }) sessionId = '';
  /** Name of the parent artifact when it is in this session. */
  @property({ type: String }) parentName = '';
  @property({ type: Boolean, reflect: true }) highlighted = false;

  @state() private text: ArtifactTextLoad | null = null;
  @state() private expanded = false;
  @state() private expanding = false;
  @state() private audio: SessionArtifactLoad | null = null;
  @state() private audioLoading = false;
  /** Availability learnt from a 410 after the row was rendered. */
  @state() private goneAs: string | null = null;
  @state() private copied = false;

  private observer: IntersectionObserver | null = null;
  private audioHeld: { sessionId: string; artifactId: string } | null = null;

  static styles = css`
    :host {
      display: block;
      border-radius: 8px;
    }
    :host(:focus-visible) {
      outline: 2px solid var(--sl-color-primary-400, #60a5fa);
      outline-offset: 2px;
    }
    .row {
      display: flex;
      gap: 0.75rem;
      align-items: flex-start;
      padding: 0.6rem 0.75rem;
      border: 1px solid var(--sl-color-neutral-200, #e5e7eb);
      border-left: 3px solid var(--sl-color-violet-500, #8b5cf6);
      border-radius: 8px;
      background: var(--sl-color-neutral-0, #fff);
    }
    :host([highlighted]) .row {
      border-left-color: var(--sl-color-warning-500, #f59e0b);
      box-shadow: 0 0 0 2px var(--sl-color-warning-400, #fbbf24);
    }
    .row.gone {
      border-left-color: var(--sl-color-neutral-400, #9ca3af);
      background: var(--sl-color-neutral-50, #f9fafb);
      color: var(--sl-color-neutral-600, #4b5563);
    }
    .icon {
      flex: none;
      font-size: 1.1rem;
      color: var(--sl-color-violet-600, #7c3aed);
      margin-top: 0.1rem;
    }
    .gone .icon {
      color: var(--sl-color-neutral-500, #6b7280);
    }
    .main {
      flex: 1;
      min-width: 0;
      display: flex;
      flex-direction: column;
      gap: 0.3rem;
    }
    .headline {
      display: flex;
      flex-wrap: wrap;
      align-items: baseline;
      gap: 0.4rem;
    }
    .name {
      font-weight: 600;
      overflow-wrap: anywhere;
    }
    .kind {
      font-size: 0.7rem;
      text-transform: uppercase;
      letter-spacing: 0.03em;
      color: var(--sl-color-neutral-500, #6b7280);
    }
    .chips {
      display: flex;
      flex-wrap: wrap;
      gap: 0.25rem;
    }
    .chip {
      font-size: 0.7rem;
      padding: 0.05rem 0.45rem;
      border-radius: 999px;
      background: var(--sl-color-neutral-100, #f3f4f6);
      color: var(--sl-color-neutral-700, #374151);
      border: 1px solid var(--sl-color-neutral-200, #e5e7eb);
    }
    .chip.primary {
      background: var(--sl-color-primary-50, #eff6ff);
      border-color: var(--sl-color-primary-200, #bfdbfe);
      color: var(--sl-color-primary-800, #1e40af);
    }
    .meta {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 0.35rem;
      font-size: 0.75rem;
      color: var(--sl-color-neutral-500, #6b7280);
    }
    .sha {
      font-family: var(--sl-font-mono, monospace);
    }
    button.link {
      border: none;
      background: none;
      padding: 0;
      font: inherit;
      color: var(--sl-color-primary-600, #2563eb);
      cursor: pointer;
      text-decoration: underline;
    }
    pre.excerpt {
      margin: 0;
      padding: 0.4rem 0.5rem;
      font-family: var(--sl-font-mono, monospace);
      font-size: 0.78rem;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      background: var(--sl-color-neutral-50, #f9fafb);
      border: 1px solid var(--sl-color-neutral-200, #e5e7eb);
      border-radius: 6px;
      max-height: 32rem;
      overflow: auto;
      color: var(--sl-color-neutral-800, #1f2937);
    }
    .actions {
      display: flex;
      flex-wrap: wrap;
      gap: 0.75rem;
      font-size: 0.8rem;
    }
    .reason {
      font-size: 0.8rem;
    }
    .reason a {
      color: var(--sl-color-primary-600, #2563eb);
    }
    audio {
      width: min(100%, 360px);
      height: 36px;
    }
    .error {
      font-size: 0.8rem;
      color: var(--sl-color-danger-700, #b91c1c);
    }
  `;

  connectedCallback(): void {
    super.connectedCallback();
    if (!this.hasAttribute('tabindex')) this.tabIndex = 0;
    this.addEventListener('keydown', this.onKeydown);
    this.observeForExcerpt();
  }

  disconnectedCallback(): void {
    this.removeEventListener('keydown', this.onKeydown);
    this.observer?.disconnect();
    this.observer = null;
    this.releaseAudio();
    super.disconnectedCallback();
  }

  updated(changed: Map<string, unknown>): void {
    if (changed.has('artifact') || changed.has('sessionId')) {
      const previous = changed.get('artifact') as ArtifactView | undefined;
      if (previous && previous.id !== this.artifact?.id) {
        this.text = null;
        this.expanded = false;
        this.goneAs = null;
        this.releaseAudio();
      }
      this.observeForExcerpt();
    }
  }

  private get availability(): string {
    return this.goneAs || this.artifact?.availability || 'available';
  }

  private get isGone(): boolean {
    return this.availability !== 'available';
  }

  private get isText(): boolean {
    const group = this.artifact?.group;
    return group === 'transcript' || group === 'document';
  }

  /** Read the excerpt once the row nears the viewport, never earlier. */
  private observeForExcerpt(): void {
    if (!this.isConnected || !this.isText || this.isGone) return;
    if (this.text || this.observer || !this.sessionId) return;
    if (typeof IntersectionObserver === 'undefined') {
      void this.loadText(ARTIFACT_EXCERPT_BYTES);
      return;
    }
    this.observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          this.observer?.disconnect();
          this.observer = null;
          void this.loadText(ARTIFACT_EXCERPT_BYTES);
        }
      },
      { rootMargin: '200px' }
    );
    this.observer.observe(this);
  }

  private async loadText(maxBytes: number): Promise<void> {
    const artifact = this.artifact;
    if (!artifact) return;
    const result = await readSessionArtifactText(
      this.sessionId,
      artifact.id,
      maxBytes
    );
    if (this.artifact?.id !== artifact.id) return;
    if (result.status === 'gone') this.goneAs = result.availability;
    this.text = result;
  }

  private async toggleExpand(): Promise<void> {
    if (this.expanded) {
      this.expanded = false;
      return;
    }
    const current = this.text;
    if (current?.status === 'ok' && current.truncated) {
      this.expanding = true;
      await this.loadText(EXPANDED_BYTES);
      this.expanding = false;
    }
    this.expanded = true;
  }

  private async playAudio(): Promise<void> {
    const artifact = this.artifact;
    if (!artifact || this.audioLoading || this.audio?.status === 'ok') return;
    this.audioLoading = true;
    this.audioHeld = { sessionId: this.sessionId, artifactId: artifact.id };
    const result = await acquireSessionArtifact(this.sessionId, artifact.id);
    this.audioLoading = false;
    if (result.status === 'error') {
      // Drop the failed load from the shared cache so Play can retry it.
      this.releaseAudio();
      this.audio = result;
      return;
    }
    if (result.status === 'gone') this.goneAs = result.availability;
    this.audio = result;
    await this.updateComplete;
    const player = this.renderRoot.querySelector('audio');
    void player?.play?.().catch(() => undefined);
  }

  private releaseAudio(): void {
    this.audio = null;
    if (!this.audioHeld) return;
    releaseSessionArtifact(this.audioHeld.sessionId, this.audioHeld.artifactId);
    this.audioHeld = null;
  }

  private async download(): Promise<void> {
    const artifact = this.artifact;
    if (!artifact) return;
    const result = await downloadSessionArtifact(
      this.sessionId,
      artifact.id,
      artifact.name
    );
    if (result.status === 'gone') this.goneAs = result.availability;
  }

  /** Open a text artifact in a new tab (as text, never as active content). */
  private async openText(): Promise<void> {
    const artifact = this.artifact;
    if (!artifact) return;
    const tab = window.open('', '_blank');
    const result = await readSessionArtifactText(
      this.sessionId,
      artifact.id,
      EXPANDED_BYTES * 4
    );
    if (result.status !== 'ok') {
      tab?.close();
      if (result.status === 'gone') this.goneAs = result.availability;
      return;
    }
    if (!tab) return;
    const url = URL.createObjectURL(
      new Blob([result.text], { type: 'text/plain;charset=utf-8' })
    );
    tab.location.href = url;
    setTimeout(() => URL.revokeObjectURL(url), 60_000);
  }

  private emit(name: string): void {
    if (!this.artifact) return;
    this.dispatchEvent(
      new CustomEvent(name, {
        detail: {
          artifactId:
            name === 'artifact-scrub'
              ? this.artifact.parentArtifactId
              : this.artifact.id,
        },
        bubbles: true,
        composed: true,
      })
    );
  }

  /** Enter on the focused row performs the row's primary action. */
  open(): void {
    const artifact = this.artifact;
    if (!artifact || this.isGone) return;
    if (artifact.group === 'screenshot') this.emit('artifact-open');
    else if (this.isText) void this.toggleExpand();
    else if (artifact.group === 'audio') void this.playAudio();
    else void this.download();
  }

  private onKeydown = (event: KeyboardEvent): void => {
    if (event.key !== 'Enter' || event.defaultPrevented) return;
    // Buttons and links inside the row handle their own Enter.
    const origin = event.composedPath()[0] as HTMLElement | undefined;
    if (origin && origin !== this && origin.closest?.('button, a, audio')) {
      return;
    }
    event.preventDefault();
    this.open();
  };

  private async copySha(): Promise<void> {
    const sha = this.artifact?.sha256;
    if (!sha) return;
    try {
      await navigator.clipboard.writeText(sha);
      this.copied = true;
      setTimeout(() => (this.copied = false), 1500);
    } catch {
      // Clipboard denied; the full hash is in the title attribute.
    }
  }

  private renderText() {
    const load = this.text;
    if (!load) {
      return html`<pre class="excerpt" data-testid="artifact-excerpt">
Loading...</pre>`;
    }
    if (load.status === 'error') {
      return html`<div class="error">${load.message}</div>`;
    }
    if (load.status === 'gone') return nothing;
    const lines = load.text.split(/\r?\n/);
    const limit = this.expanded
      ? ARTIFACT_EXPANDED_LINES
      : ARTIFACT_EXCERPT_LINES;
    const shown = lines.slice(0, limit).join('\n');
    const more = lines.length > limit || load.truncated;
    const canExpand =
      !this.expanded &&
      (lines.length > ARTIFACT_EXCERPT_LINES || load.truncated);
    return html`
      <pre class="excerpt" data-testid="artifact-excerpt">
${shown}${more ? '\n...' : ''}</pre>
      <div class="actions">
        ${
          canExpand || this.expanded
            ? html`<button
                class="link"
                data-testid="artifact-show-more"
                aria-expanded=${this.expanded ? 'true' : 'false'}
                ?disabled=${this.expanding}
                @click=${() => this.toggleExpand()}
              >
                ${this.expanded ? 'Show less' : 'Show more'}
              </button>`
            : nothing
        }
        <button class="link" @click=${() => this.openText()}>Open</button>
        <button
          class="link"
          data-testid="artifact-download"
          @click=${() => this.download()}
        >
          Download
        </button>
      </div>
    `;
  }

  private renderAudio() {
    const load = this.audio;
    if (load?.status === 'ok') {
      return html`<audio
        controls
        preload="none"
        src=${load.url}
        data-testid="artifact-audio"
      ></audio>`;
    }
    return html`<div class="actions">
        <button
          class="link"
          data-testid="artifact-play"
          ?disabled=${this.audioLoading}
          @click=${() => this.playAudio()}
        >
          ${this.audioLoading ? 'Loading audio...' : 'Play audio'}
        </button>
        <button class="link" @click=${() => this.download()}>Download</button>
      </div>
      ${
        load?.status === 'error'
          ? html`<div class="error" data-testid="artifact-audio-error">
              ${load.message}
            </div>`
          : nothing
      }`;
  }

  private renderBody(artifact: ArtifactView) {
    if (this.isGone) {
      return html`<div class="reason" data-testid="artifact-unavailable">
        ${artifactUnavailableReason(this.availability)}
        <a href=${ARTIFACT_STORAGE_SETTINGS_HREF}>Storage budget</a>
      </div>`;
    }
    if (this.isText) return this.renderText();
    if (artifact.group === 'audio') return this.renderAudio();
    if (artifact.group === 'screenshot') return nothing;
    return html`<div class="actions">
      <button
        class="link"
        data-testid="artifact-download"
        @click=${() => this.download()}
      >
        Download
      </button>
    </div>`;
  }

  render() {
    const artifact = this.artifact;
    if (!artifact) return nothing;
    const labels = orderedArtifactLabels(artifact.labels);
    const time = this.item?.timestamp
      ? new Date(this.item.timestamp).toLocaleTimeString()
      : '';
    const size = formatArtifactBytes(artifact.sizeBytes);
    return html`
      <div
        class="row ${this.isGone ? 'gone' : ''}"
        data-testid="artifact-row"
        data-kind=${artifact.group}
        data-availability=${this.availability}
      >
        <sl-icon
          class="icon"
          name=${ARTIFACT_KIND_ICONS[artifact.group]}
          label=${artifact.kind || artifact.group}
        ></sl-icon>
        <div class="main">
          <div class="headline">
            <span class="name">${artifact.name}</span>
            <span class="kind">${artifact.kind}</span>
          </div>
          ${
            labels.length
              ? html`<div class="chips" data-testid="artifact-labels">
                  ${labels.map(
                    ([key, value]) =>
                      html`<span
                        class="chip ${
                          key === 'site' || key === 'consent_basis'
                            ? 'primary'
                            : ''
                        }"
                        data-label=${key}
                        >${key === 'tags' ? value : `${key}: ${value}`}</span
                      >`
                  )}
                </div>`
              : nothing
          }
          ${this.renderBody(artifact)}
          <div class="meta">
            ${artifact.producer ? html`<span>${artifact.producer.replace(/_/g, ' ')}</span>` : nothing}
            ${artifact.toolName ? html`<span>· ${artifact.toolName}</span>` : nothing}
            ${size ? html`<span>· ${size}</span>` : nothing}
            <span>· ${artifact.contentType}</span>
            ${
              artifact.sha256
                ? html`<span>·</span
                    ><button
                      class="link sha"
                      title=${`sha256 ${artifact.sha256}. Click to copy.`}
                      aria-label="Copy sha256"
                      data-testid="artifact-sha"
                      @click=${() => this.copySha()}
                    >
                      ${this.copied ? 'copied' : `sha256 ${artifact.sha256.slice(0, 12)}`}
                    </button>`
                : nothing
            }
            ${time ? html`<span>· ${time}</span>` : nothing}
          </div>
          ${
            artifact.parentArtifactId
              ? html`<div class="meta">
                  <button
                    class="link"
                    data-testid="artifact-lineage"
                    @click=${() => this.emit('artifact-scrub')}
                  >
                    derived from ${this.parentName || 'an earlier artifact'}
                  </button>
                </div>`
              : nothing
          }
        </div>
        ${
          artifact.group === 'screenshot' && !this.isGone
            ? html`<browser-step-thumbnail
                .sessionId=${this.sessionId}
                .artifactId=${artifact.id}
                .availability=${this.availability}
                .label=${artifact.name}
                @thumbnail-open=${() => this.emit('artifact-open')}
              ></browser-step-thumbnail>`
            : nothing
        }
      </div>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'session-artifact-row': SessionArtifactRow;
  }
}
