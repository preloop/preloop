import { LitElement, css, html, nothing } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import type { RuntimeSessionRequestItem } from '../types';
import { formatCost, formatNumber } from '../utils/session-observer';

export type RequestTimelineSort =
  | 'recent'
  | 'oldest'
  | 'costliest'
  | 'cheapest'
  | 'type';

type ThresholdMode = 'tokens' | 'usd';

/**
 * Unified per-request session timeline.
 *
 * Renders ALL gateway requests for a session as one sorted stream sourced from
 * `api_usage` rows (one event per request). Each event shows its tokens and
 * estimated spend plus the tools it carried with their per-tool schema token
 * cost. Provides sort, a cost/token threshold filter, and a failed-only
 * toggle. This replaces the sparse captured-event timeline.
 */
@customElement('session-request-timeline')
export class SessionRequestTimeline extends LitElement {
  @property({ type: Array })
  requests: RuntimeSessionRequestItem[] = [];

  @property({ type: Number })
  total = 0;

  @property({ type: Number })
  failedCount = 0;

  @property({ type: Boolean })
  loading = false;

  @property({ type: Boolean })
  hasMore = false;

  @property({ type: Boolean })
  failedOnly = false;

  @state()
  private sort: RequestTimelineSort = 'recent';

  @state()
  private threshold = 0;

  @state()
  private thresholdMode: ThresholdMode = 'usd';

  static styles = css`
    :host {
      display: block;
    }

    .toolbar {
      align-items: end;
      display: flex;
      flex-wrap: wrap;
      gap: var(--sl-spacing-small);
      margin-bottom: var(--sl-spacing-small);
    }

    .control {
      display: grid;
      gap: 2px;
    }

    .control-label {
      color: var(--sl-color-neutral-600);
      font-size: var(--sl-font-size-x-small);
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }

    select,
    input[type='number'] {
      border: 1px solid var(--sl-color-neutral-300);
      border-radius: var(--sl-border-radius-medium);
      font-size: var(--sl-font-size-small);
      padding: 4px 8px;
    }

    .count {
      color: var(--sl-color-neutral-600);
      font-size: var(--sl-font-size-small);
      margin-left: auto;
    }

    .stream {
      display: flex;
      flex-direction: column;
      gap: var(--sl-spacing-x-small);
    }

    .request-row {
      align-items: center;
      background: var(--sl-color-neutral-0);
      border: 1px solid var(--sl-color-neutral-200);
      border-radius: var(--sl-border-radius-medium);
      display: grid;
      gap: var(--sl-spacing-small);
      grid-template-columns: auto 1fr auto;
      padding: var(--sl-spacing-x-small) var(--sl-spacing-small);
    }

    .request-row.error {
      border-color: var(--sl-color-danger-300);
      background: var(--sl-color-danger-50);
    }

    .request-main {
      display: grid;
      gap: 2px;
      min-width: 0;
    }

    .request-title {
      font-weight: 600;
      font-size: var(--sl-font-size-small);
    }

    .request-meta,
    .tools {
      color: var(--sl-color-neutral-600);
      font-size: var(--sl-font-size-x-small);
    }

    .tool-chip {
      background: var(--sl-color-neutral-100);
      border-radius: 999px;
      display: inline-block;
      margin: 1px 2px;
      padding: 1px 6px;
    }

    .tool-chip.stripped {
      opacity: 0.55;
      text-decoration: line-through;
    }

    .request-cost {
      text-align: right;
      white-space: nowrap;
    }

    .cost-value {
      font-weight: 700;
    }

    .empty {
      color: var(--sl-color-neutral-600);
      padding: var(--sl-spacing-large);
      text-align: center;
    }
  `;

  private get filteredSorted(): RuntimeSessionRequestItem[] {
    let rows = [...this.requests];
    if (this.threshold > 0) {
      rows = rows.filter((row) =>
        this.thresholdMode === 'usd'
          ? row.estimated_cost >= this.threshold
          : row.total_tokens >= this.threshold
      );
    }
    const time = (row: RuntimeSessionRequestItem): number =>
      row.timestamp ? new Date(row.timestamp).getTime() : 0;
    switch (this.sort) {
      case 'oldest':
        rows.sort((a, b) => time(a) - time(b));
        break;
      case 'costliest':
        rows.sort((a, b) => b.estimated_cost - a.estimated_cost);
        break;
      case 'cheapest':
        rows.sort((a, b) => a.estimated_cost - b.estimated_cost);
        break;
      case 'type':
        rows.sort((a, b) => {
          const keyA = a.is_error ? 'error' : a.model_alias || '';
          const keyB = b.is_error ? 'error' : b.model_alias || '';
          return keyA.localeCompare(keyB) || time(b) - time(a);
        });
        break;
      case 'recent':
      default:
        rows.sort((a, b) => time(b) - time(a));
        break;
    }
    return rows;
  }

  private emitFailedOnly(failedOnly: boolean): void {
    this.dispatchEvent(
      new CustomEvent('request-timeline-failed-only', {
        detail: { failedOnly },
        bubbles: true,
        composed: true,
      })
    );
  }

  private renderRequest(row: RuntimeSessionRequestItem) {
    const model = row.model_alias || row.provider_name || 'request';
    const ts = row.timestamp ? new Date(row.timestamp) : null;
    return html`
      <div class="request-row ${row.is_error ? 'error' : ''}">
        <sl-badge variant=${row.is_error ? 'danger' : 'success'} pill>
          ${row.status_code || '—'}
        </sl-badge>
        <div class="request-main">
          <div class="request-title">
            ${model}
            ${row.is_retry
              ? html`<sl-badge variant="warning" pill>retry</sl-badge>`
              : nothing}
          </div>
          <div class="request-meta">
            ${ts ? ts.toLocaleString() : 'no timestamp'} ·
            ${formatNumber(row.total_tokens)} tokens
            ${row.finish_reason ? html` · ${row.finish_reason}` : nothing}
          </div>
          ${row.tools.length
            ? html`
                <div class="tools">
                  Tools (${formatNumber(row.tools_total_schema_tokens)} schema
                  tokens):
                  ${row.tools.map(
                    (tool) => html`
                      <span
                        class="tool-chip ${tool.stripped ? 'stripped' : ''}"
                      >
                        ${tool.name || 'tool'} ·
                        ${formatNumber(tool.schema_tokens_estimate)}t
                      </span>
                    `
                  )}
                </div>
              `
            : nothing}
        </div>
        <div class="request-cost">
          <div class="cost-value">${formatCost(row.estimated_cost)}</div>
        </div>
      </div>
    `;
  }

  render() {
    const rows = this.filteredSorted;
    return html`
      <div class="toolbar">
        <label class="control">
          <span class="control-label">Sort</span>
          <select
            @change=${(event: Event) => {
              this.sort = (event.target as HTMLSelectElement)
                .value as RequestTimelineSort;
            }}
          >
            <option value="recent">Recent first</option>
            <option value="oldest">Oldest first</option>
            <option value="costliest">Costliest first</option>
            <option value="cheapest">Cheapest first</option>
            <option value="type">Group by type</option>
          </select>
        </label>
        <label class="control">
          <span class="control-label">Hide below</span>
          <input
            type="number"
            min="0"
            step="any"
            .value=${String(this.threshold)}
            @input=${(event: Event) => {
              this.threshold = Number(
                (event.target as HTMLInputElement).value || 0
              );
            }}
          />
        </label>
        <label class="control">
          <span class="control-label">Threshold unit</span>
          <select
            @change=${(event: Event) => {
              this.thresholdMode = (event.target as HTMLSelectElement)
                .value as ThresholdMode;
            }}
          >
            <option value="usd">$ spend</option>
            <option value="tokens">tokens</option>
          </select>
        </label>
        <sl-button
          size="small"
          variant=${this.failedOnly ? 'danger' : 'default'}
          @click=${() => this.emitFailedOnly(!this.failedOnly)}
        >
          ${this.failedOnly ? 'Showing failed only' : 'Failed requests only'}
          (${formatNumber(this.failedCount)})
        </sl-button>
        <span class="count">
          ${formatNumber(rows.length)} shown · ${formatNumber(this.total)} total
          requests
        </span>
      </div>
      ${this.loading && !rows.length
        ? html`<div class="empty"><sl-spinner></sl-spinner></div>`
        : rows.length
          ? html`<div class="stream">
              ${rows.map((row) => this.renderRequest(row))}
            </div>`
          : html`<div class="empty">
              No requests match the current filter.
            </div>`}
      ${this.hasMore
        ? html`
            <div style="text-align:center;margin-top:var(--sl-spacing-small);">
              <sl-button
                size="small"
                ?loading=${this.loading}
                @click=${() =>
                  this.dispatchEvent(
                    new CustomEvent('request-timeline-load-more', {
                      bubbles: true,
                      composed: true,
                    })
                  )}
              >
                Load more requests
              </sl-button>
            </div>
          `
        : nothing}
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'session-request-timeline': SessionRequestTimeline;
  }
}
