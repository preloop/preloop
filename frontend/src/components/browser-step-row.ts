import { parseUTCDate } from '../utils/date';
import { LitElement, css, html, nothing } from 'lit';
import { customElement, property } from 'lit/decorators.js';
import type { RuntimeSessionActivityItem } from '../types';
import {
  browserActionIcon,
  browserStepKey,
  browserStepMetadata,
} from '../utils/session-artifacts';
import './browser-step-thumbnail';

/**
 * One `browser_step` activity in the session timeline: action icon, action
 * and URL, target, collapsible reasoning, step index and a lazy screenshot
 * thumbnail. The row is an observation of what the agent reported doing, not
 * an approval or proof of page state. Emits `browser-step-open` ({ key }) when
 * the thumbnail is clicked.
 */
@customElement('browser-step-row')
export class BrowserStepRow extends LitElement {
  @property({ attribute: false }) item: RuntimeSessionActivityItem | null =
    null;
  @property({ type: String }) sessionId = '';

  static styles = css`
    :host {
      display: block;
    }
    .row {
      display: flex;
      gap: 0.75rem;
      align-items: flex-start;
      padding: 0.6rem 0.75rem;
      border: 1px solid var(--sl-color-neutral-200, #e5e7eb);
      border-left: 3px solid var(--sl-color-sky-500, #0ea5e9);
      border-radius: 8px;
      background: var(--sl-color-neutral-0, #fff);
    }
    .row.failed {
      border-left-color: var(--sl-color-danger-500, #ef4444);
    }
    .icon {
      flex: none;
      font-size: 1.1rem;
      color: var(--sl-color-sky-600, #0284c7);
      margin-top: 0.1rem;
    }
    .main {
      flex: 1;
      min-width: 0;
      display: flex;
      flex-direction: column;
      gap: 0.25rem;
    }
    .headline {
      display: flex;
      flex-wrap: wrap;
      align-items: baseline;
      gap: 0.4rem;
    }
    .action {
      font-weight: 600;
      text-transform: capitalize;
    }
    .url {
      font-family: var(--sl-font-mono, monospace);
      font-size: 0.8rem;
      overflow-wrap: anywhere;
      color: var(--sl-color-neutral-700, #374151);
    }
    .meta {
      font-size: 0.75rem;
      color: var(--sl-color-neutral-500, #6b7280);
    }
    .target {
      font-size: 0.8rem;
      color: var(--sl-color-neutral-700, #374151);
      overflow-wrap: anywhere;
    }
    .target code {
      font-size: 0.75rem;
    }
    details summary {
      cursor: pointer;
      font-size: 0.75rem;
      color: var(--sl-color-neutral-600, #4b5563);
    }
    details p {
      margin: 0.25rem 0 0;
      font-size: 0.8rem;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }
    .badge {
      font-size: 0.7rem;
      padding: 0 0.4rem;
      border-radius: 999px;
      background: var(--sl-color-neutral-100, #f3f4f6);
      color: var(--sl-color-neutral-700, #374151);
    }
    .badge.failed {
      background: var(--sl-color-danger-100, #fee2e2);
      color: var(--sl-color-danger-700, #b91c1c);
    }
  `;

  private open(): void {
    if (!this.item) return;
    this.dispatchEvent(
      new CustomEvent('browser-step-open', {
        detail: { key: browserStepKey(this.item) },
        bubbles: true,
        composed: true,
      })
    );
  }

  render() {
    const item = this.item;
    if (!item) return nothing;
    const meta = browserStepMetadata(item);
    const action = String(meta.action || 'other');
    const failed = String(item.status || '').toLowerCase() === 'failed';
    const time = item.timestamp
      ? parseUTCDate(item.timestamp).toLocaleTimeString()
      : '';
    const shot = meta.screenshot;
    return html`
      <div class="row ${failed ? 'failed' : ''}" data-testid="browser-step-row">
        <sl-icon
          class="icon"
          name=${browserActionIcon(action)}
          label=${action}
        ></sl-icon>
        <div class="main">
          <div class="headline">
            <span class="action">${action}</span>
            ${meta.url ? html`<span class="url">${meta.url}</span>` : nothing}
          </div>
          ${
            meta.target
              ? html`<div class="target">
                  Target: <code>${meta.target}</code>
                </div>`
              : nothing
          }
          ${
            meta.reasoning
              ? html`<details>
                  <summary>Agent reasoning</summary>
                  <p>${meta.reasoning}</p>
                </details>`
              : nothing
          }
          <div class="meta">
            Browser step
            ${typeof meta.step_index === 'number' ? html`#${meta.step_index}` : nothing}
            ${meta.source ? html`· ${meta.source.replace(/_/g, ' ')}` : nothing}
            ${time ? html`· ${time}` : nothing}
            ${failed ? html`<span class="badge failed">failed</span>` : nothing}
          </div>
        </div>
        ${
          shot && shot.artifact_id
            ? html`<browser-step-thumbnail
                .sessionId=${this.sessionId}
                .artifactId=${shot.artifact_id}
                .availability=${String(shot.availability || 'available')}
                .label=${`Screenshot after ${action}`}
                @thumbnail-open=${() => this.open()}
              ></browser-step-thumbnail>`
            : nothing
        }
      </div>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'browser-step-row': BrowserStepRow;
  }
}
