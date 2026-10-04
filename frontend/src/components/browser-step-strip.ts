import { LitElement, css, html, nothing } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import type { RuntimeSessionActivityItem } from '../types';
import {
  browserStepKey,
  browserStepMetadata,
  browserStepNumber,
} from '../utils/session-artifacts';
import './browser-step-thumbnail';

/**
 * Most entries the header strip renders. A long browser session can carry
 * thousands of steps; rendering every thumbnail would mount thousands of
 * lazy loaders, so the strip caps at this many and shows a `+N` remainder.
 */
export const BROWSER_STEP_STRIP_MAX = 200;

/**
 * Session-header strip with one entry per browser step, for scrubbing.
 * Clicking an entry emits `browser-step-scrub` ({ key }) so the host can
 * scroll the matching timeline row into view. Steps without a screenshot
 * still get a numbered chip.
 *
 * Rendering every entry would mount a lazy thumbnail loader per step, so a
 * long session shows only the most recent {@link BROWSER_STEP_STRIP_MAX}
 * steps: those are the ones an operator is usually scrubbing for. Older steps
 * fold behind an interactive `+N` button that reveals the previous chunk, so
 * every step stays reachable without mounting the whole history at once.
 */
@customElement('browser-step-strip')
export class BrowserStepStrip extends LitElement {
  /** Browser steps in time order. */
  @property({ attribute: false }) steps: RuntimeSessionActivityItem[] = [];
  @property({ type: String }) sessionId = '';

  /**
   * How many steps the window currently covers, counted back from the newest.
   * Starts at the cap; each `+N` click adds another chunk.
   */
  @state() private revealed = BROWSER_STEP_STRIP_MAX;

  static styles = css`
    :host {
      display: block;
    }
    .wrap {
      display: flex;
      flex-direction: column;
      gap: 0.35rem;
      padding: 0.5rem 0.75rem;
      border: 1px solid var(--sl-color-neutral-200, #e5e7eb);
      border-radius: 8px;
      background: var(--sl-color-neutral-50, #f9fafb);
    }
    .label {
      font-size: 0.75rem;
      color: var(--sl-color-neutral-600, #4b5563);
    }
    .strip {
      display: flex;
      gap: 0.4rem;
      overflow-x: auto;
      padding-bottom: 0.25rem;
    }
    button {
      flex: none;
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 0.15rem;
      padding: 0.2rem;
      border: 1px solid transparent;
      border-radius: 6px;
      background: transparent;
      cursor: pointer;
      color: inherit;
      font: inherit;
    }
    button:hover,
    button:focus-visible {
      border-color: var(--sl-color-primary-400, #60a5fa);
      outline: none;
    }
    .chip {
      width: 72px;
      height: 46px;
      display: flex;
      align-items: center;
      justify-content: center;
      border-radius: 6px;
      border: 1px dashed var(--sl-color-neutral-300, #d1d5db);
      font-size: 0.7rem;
      color: var(--sl-color-neutral-600, #4b5563);
      text-transform: capitalize;
    }
    .index {
      font-size: 0.65rem;
      color: var(--sl-color-neutral-500, #6b7280);
    }
    .more {
      flex: none;
      display: flex;
      align-items: center;
      justify-content: center;
      width: 72px;
      min-height: 46px;
      border-radius: 6px;
      border: 1px dashed var(--sl-color-neutral-300, #d1d5db);
      color: var(--sl-color-neutral-600, #4b5563);
      font-size: 0.8rem;
      font-weight: 600;
    }
    browser-step-thumbnail {
      pointer-events: none;
    }
  `;

  protected willUpdate(changed: Map<PropertyKey, unknown>): void {
    // The host reuses one strip element across sessions and only swaps
    // `steps`/`sessionId`, so an expanded window would otherwise carry over
    // and bypass the thumbnail cap on the next session. Reset only when the
    // session changes: a live session appends to `steps` continuously, and
    // resetting on every step would immediately undo a revealed chunk.
    if (changed.has('sessionId')) {
      this.revealed = BROWSER_STEP_STRIP_MAX;
    }
  }

  private scrub(item: RuntimeSessionActivityItem): void {
    this.dispatchEvent(
      new CustomEvent('browser-step-scrub', {
        detail: { key: browserStepKey(item) },
        bubbles: true,
        composed: true,
      })
    );
  }

  private revealEarlier(): void {
    this.revealed = Math.min(
      this.steps.length,
      this.revealed + BROWSER_STEP_STRIP_MAX
    );
  }

  render() {
    if (!this.steps.length) return nothing;
    const withShots = this.steps.filter(
      (item) => browserStepMetadata(item).screenshot?.artifact_id
    ).length;
    const windowSize = Math.max(BROWSER_STEP_STRIP_MAX, this.revealed);
    const shown = this.steps.slice(Math.max(0, this.steps.length - windowSize));
    // Position within `this.steps`, not within the slice: `browserStepNumber`
    // falls back to it for steps that do not report a `step_index`.
    const offset = this.steps.length - shown.length;
    const hidden = offset;
    // One click grows the window by a chunk; name the button for what the
    // click does, not the total still hidden behind it.
    const revealCount = Math.min(hidden, BROWSER_STEP_STRIP_MAX);
    return html`
      <div class="wrap" data-testid="browser-step-strip">
        <span class="label">
          ${this.steps.length} browser
          step${this.steps.length === 1 ? '' : 's'}, ${withShots} with
          screenshots. Click a step to jump to it.
        </span>
        <div class="strip">
          ${shown.map((item, position) => {
            const meta = browserStepMetadata(item);
            const action = String(meta.action || 'other');
            const shot = meta.screenshot;
            const number = browserStepNumber(item, position + offset);
            return html`<button
              type="button"
              data-step-key=${browserStepKey(item)}
              aria-label=${`Jump to step #${number}: ${action}`}
              @click=${() => this.scrub(item)}
            >
              ${
                shot?.artifact_id
                  ? html`<browser-step-thumbnail
                      compact
                      .sessionId=${this.sessionId}
                      .artifactId=${shot.artifact_id}
                      .availability=${String(shot.availability || 'available')}
                      .label=${`Step #${number} ${action}`}
                    ></browser-step-thumbnail>`
                  : html`<span class="chip">${action}</span>`
              }
              <span class="index">#${number}</span>
            </button>`;
          })}
          ${
            hidden > 0
              ? html`<button
                  type="button"
                  class="more"
                  data-testid="browser-step-overflow"
                  aria-label=${`Reveal earlier steps (${hidden} hidden)`}
                  title=${`Reveal the previous ${revealCount} of ${hidden} hidden step${
                    hidden === 1 ? '' : 's'
                  }`}
                  @click=${() => this.revealEarlier()}
                >
                  +${hidden}
                </button>`
              : nothing
          }
        </div>
      </div>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'browser-step-strip': BrowserStepStrip;
  }
}
