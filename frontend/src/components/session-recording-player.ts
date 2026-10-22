import { LitElement, css, html, nothing } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import { keyed } from 'lit/directives/keyed.js';
import type { RecordingSync, RuntimeSessionArtifactDescriptor } from '../types';
import {
  acquireSessionArtifact,
  releaseSessionArtifact,
  artifactUnavailableReason,
  type SessionArtifactLoad,
} from '../utils/session-artifacts';
import {
  adjacentMediaStep,
  currentMediaStep,
  mediaOffsetInRange,
} from '../utils/media-sync';

/** A docked, demand-loaded player using the existing authorized content cache. */
@customElement('session-recording-player')
export class SessionRecordingPlayer extends LitElement {
  @property({ attribute: false })
  recordings: RuntimeSessionArtifactDescriptor[] = [];
  @property({ attribute: false }) sync: RecordingSync[] = [];
  @property() sessionId = '';
  @property() selectedId = '';
  @state() private collapsed = false;
  @state() private load: SessionArtifactLoad | null = null;
  @state() private loading = false;
  @state() private notice = '';
  @state() private currentTime = 0;
  private held: { sessionId: string; artifactId: string } | null = null;
  private generation = 0;
  private lastUpdate = -Infinity;
  private pendingSeek: number | null = null;
  private lastActivity: string | null = null;

  static styles = css`
    :host {
      display: block;
      position: sticky;
      top: 0;
      z-index: 3;
    }
    section {
      padding: 0.6rem;
      border: 1px solid var(--sl-color-neutral-300, #ccc);
      border-radius: 8px;
      background: var(--sl-color-neutral-0, white);
    }
    header {
      display: flex;
      gap: 0.5rem;
      align-items: center;
      flex-wrap: wrap;
    }
    select {
      min-width: 0;
      max-width: 75%;
      padding: 0.3rem;
      font: inherit;
      color: inherit;
      background: inherit;
    }
    button {
      font: inherit;
      cursor: pointer;
      padding: 0.25rem 0.5rem;
    }
    video,
    img {
      display: block;
      width: 100%;
      max-height: 280px;
      object-fit: contain;
      margin-top: 0.5rem;
    }
    .notice {
      font-size: 0.8rem;
      margin: 0.4rem 0 0;
    }
  `;

  get selected(): RuntimeSessionArtifactDescriptor | undefined {
    return (
      this.recordings.find((item) => item.id === this.selectedId) ??
      this.recordings[0]
    );
  }
  get mapping(): RecordingSync | undefined {
    return this.sync.find((item) => item.artifact_id === this.selected?.id);
  }
  get seekable(): boolean {
    return (
      this.load?.status !== 'gone' &&
      this.selected?.availability === 'available' &&
      this.selected?.content_type !== 'image/gif' &&
      !!this.selected?.media_timing
    );
  }

  protected willUpdate(changed: Map<PropertyKey, unknown>): void {
    const selected = this.selected;
    if (
      changed.has('sessionId') ||
      (this.held &&
        (this.held.artifactId !== selected?.id ||
          selected?.availability !== 'available'))
    )
      this.release();
    if (changed.has('selectedId') || changed.has('sessionId')) {
      this.notice = '';
      this.currentTime = 0;
      this.lastActivity = null;
      this.pendingSeek = null;
    }
  }
  disconnectedCallback(): void {
    super.disconnectedCallback();
    this.release();
  }
  private release(): void {
    this.generation++;
    this.renderRoot.querySelector('video')?.pause();
    if (this.held)
      releaseSessionArtifact(this.held.sessionId, this.held.artifactId);
    this.held = null;
    this.load = null;
    this.loading = false;
  }
  private emit(name: string, detail: unknown): void {
    this.dispatchEvent(
      new CustomEvent(name, { detail, bubbles: true, composed: true })
    );
  }
  public selectRecording(id: string): void {
    if (!this.recordings.some((item) => item.id === id)) return;
    this.release();
    this.selectedId = id;
    this.collapsed = false;
    this.emit('recording-selected', { artifactId: id });
  }
  private async loadRecording(): Promise<void> {
    const selected = this.selected;
    if (
      !selected ||
      selected.availability !== 'available' ||
      this.loading ||
      this.load?.status === 'ok'
    )
      return;
    this.release();
    const generation = this.generation;
    this.held = { sessionId: this.sessionId, artifactId: selected.id };
    this.loading = true;
    const load = await acquireSessionArtifact(this.sessionId, selected.id);
    if (generation !== this.generation || !this.isConnected) return;
    this.loading = false;
    this.load = load;
    if (load.status === 'error') {
      this.release();
      this.load = load;
    }
  }
  public async seekActivity(activityId: string): Promise<void> {
    const entry = this.mapping?.mappings.find(
      (item) => item.activity_id === activityId
    );
    if (!this.seekable) return;
    if (
      !entry ||
      !mediaOffsetInRange(
        entry.offset_ms,
        this.selected?.media_timing?.duration_ms
      )
    ) {
      this.notice = 'No recording for this moment';
      return;
    }
    this.notice = entry.approximate ? 'Approximate timing' : '';
    this.collapsed = false;
    this.pendingSeek = entry.offset_ms / 1000;
    await this.loadRecording();
    await this.updateComplete;
    this.applySeek();
  }
  private applySeek(): void {
    const video = this.renderRoot.querySelector('video');
    if (!video || !video.readyState || this.pendingSeek === null) return;
    if (
      !mediaOffsetInRange(
        this.pendingSeek * 1000,
        Number.isFinite(video.duration) ? video.duration * 1000 : null
      )
    ) {
      this.notice = 'No recording for this moment';
      this.pendingSeek = null;
      return;
    }
    video.currentTime = this.pendingSeek;
    this.pendingSeek = null;
    this.updatePlayhead(true);
  }
  private updatePlayhead(force = false): void {
    const now = performance.now();
    if (!force && now - this.lastUpdate < 250) return;
    this.lastUpdate = now;
    const video = this.renderRoot.querySelector('video');
    this.currentTime = video?.currentTime ?? 0;
    const step = currentMediaStep(
      this.mapping?.mappings ?? [],
      this.currentTime * 1000
    );
    if ((step?.activity_id ?? null) !== this.lastActivity) {
      this.lastActivity = step?.activity_id ?? null;
      this.emit('recording-playhead', { activityId: this.lastActivity });
    }
  }
  public handleShortcut(event: KeyboardEvent): void {
    if (
      event.defaultPrevented ||
      event.altKey ||
      event.ctrlKey ||
      event.metaKey ||
      !this.seekable
    )
      return;
    if (
      event
        .composedPath()
        .some(
          (node) =>
            node instanceof HTMLElement &&
            (node.matches('input, textarea, select') || node.isContentEditable)
        )
    )
      return;
    if (event.key !== '[' && event.key !== ']') return;
    const entry = adjacentMediaStep(
      this.mapping?.mappings ?? [],
      this.currentTime * 1000,
      event.key === '[' ? -1 : 1
    );
    if (entry) {
      event.preventDefault();
      void this.seekActivity(entry.activity_id);
    }
  }
  render() {
    const selected = this.selected;
    if (!selected) return nothing;
    const unavailable =
      selected.availability !== 'available'
        ? selected.availability
        : this.load?.status === 'gone'
          ? this.load.availability
          : null;
    return html`<section aria-label="Session recording">
      <header>
        <button
          @click=${() => {
            this.collapsed = !this.collapsed;
            if (this.collapsed) this.renderRoot.querySelector('video')?.pause();
          }}
          aria-expanded=${String(!this.collapsed)}
        >
          ${this.collapsed ? 'Expand' : 'Collapse'} recording
        </button>
        <select
          aria-label="Recording"
          .value=${selected.id}
          @change=${(event: Event) => this.selectRecording((event.target as HTMLSelectElement).value)}
        >
          ${this.recordings.map((item) => html`<option value=${item.id}>${item.name ?? item.id} · ${item.producer ?? item.labels?.tags ?? 'agent'}${item.media_timing ? ` · ${item.media_timing.started_at}` : ''}</option>`)}
        </select>
        <span
          title=${`producer clock offset ${((this.mapping?.skew_ms ?? 0) / 1000).toFixed(1)} s`}
          >Timing</span
        >
      </header>
      ${
        this.collapsed
          ? nothing
          : html`
              ${
                unavailable
                  ? html`<p role="status">
                      ${artifactUnavailableReason(unavailable)}
                    </p>`
                  : this.load?.status === 'ok'
                    ? keyed(
                        `${this.sessionId}:${selected.id}`,
                        selected.content_type === 'image/gif'
                          ? html`<img
                              src=${this.load.url}
                              alt=${selected.name ?? 'Session screencast'}
                            />`
                          : html`<video
                              controls
                              muted
                              playsinline
                              preload="metadata"
                              src=${this.load.url}
                              aria-label="Session recording video"
                              @loadedmetadata=${this.applySeek}
                              @timeupdate=${() => this.updatePlayhead()}
                              @seeked=${() => this.updatePlayhead(true)}
                            ></video>`
                      )
                    : html`<button
                        ?disabled=${this.loading}
                        @click=${this.loadRecording}
                      >
                        ${this.loading ? 'Loading recording…' : 'Load recording'}
                      </button>`
              }
              ${selected.media_timing && !this.mapping ? html`<p class="notice">Timeline synchronization is unavailable</p>` : nothing}
              ${!selected.media_timing ? html`<p class="notice">This recording has no timing data</p>` : nothing}
              ${selected.content_type === 'image/gif' ? html`<p class="notice">Seek not supported for GIF in this browser</p>` : nothing}
              ${this.load?.status === 'error' ? html`<p class="notice" role="alert">${this.load.message}</p>` : nothing}
              ${this.notice ? html`<p class="notice" role="status">${this.notice}</p>` : nothing}
            `
      }
    </section>`;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'session-recording-player': SessionRecordingPlayer;
  }
}
