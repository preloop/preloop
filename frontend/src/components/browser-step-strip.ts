import { LitElement, css, html, nothing } from 'lit';
import { customElement, property } from 'lit/decorators.js';
import type { RuntimeSessionActivityItem } from '../types';
import {
  browserStepKey,
  browserStepMetadata,
  browserStepNumber,
} from '../utils/session-artifacts';
import './browser-step-thumbnail';

/**
 * Session-header strip with one entry per browser step, for scrubbing.
 * Clicking an entry emits `browser-step-scrub` ({ key }) so the host can
 * scroll the matching timeline row into view. Steps without a screenshot
 * still get a numbered chip so every step is reachable.
 */
@customElement('browser-step-strip')
export class BrowserStepStrip extends LitElement {
  /** Browser steps in time order. */
  @property({ attribute: false }) steps: RuntimeSessionActivityItem[] = [];
  @property({ type: String }) sessionId = '';

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
    browser-step-thumbnail {
      pointer-events: none;
    }
  `;

  private scrub(item: RuntimeSessionActivityItem): void {
    this.dispatchEvent(
      new CustomEvent('browser-step-scrub', {
        detail: { key: browserStepKey(item) },
        bubbles: true,
        composed: true,
      })
    );
  }

  render() {
    if (!this.steps.length) return nothing;
    const withShots = this.steps.filter(
      (item) => browserStepMetadata(item).screenshot?.artifact_id
    ).length;
    return html`
      <div class="wrap" data-testid="browser-step-strip">
        <span class="label">
          ${this.steps.length} browser
          step${this.steps.length === 1 ? '' : 's'}, ${withShots} with
          screenshots. Click a step to jump to it.
        </span>
        <div class="strip">
          ${this.steps.map((item, position) => {
            const meta = browserStepMetadata(item);
            const action = String(meta.action || 'other');
            const shot = meta.screenshot;
            const number = browserStepNumber(item, position);
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
