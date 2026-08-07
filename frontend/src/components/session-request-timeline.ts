import { LitElement, css, html, nothing } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import type {
  RuntimeSessionCacheSummary,
  RuntimeSessionRequestItem,
} from '../types';
import { formatCost, formatNumber } from '../utils/session-observer';

export type RequestTimelineSort =
  'recent' | 'oldest' | 'costliest' | 'cheapest' | 'type';

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

  /**
   * Whole-session prompt-cache rollup. Undefined when the backend did not
   * send one (older API); the summary block is then simply not rendered.
   */
  @property({ type: Object })
  cacheSummary?: RuntimeSessionCacheSummary;

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

    /* Per-call cache line: quiet, one line, never competes with the title. */
    .cache-line {
      color: var(--sl-color-neutral-600);
      font-size: var(--sl-font-size-x-small);
    }

    .cache-line .not-reported {
      font-style: italic;
      opacity: 0.75;
    }

    .cache-line .derived::after {
      content: '~';
      font-size: 0.85em;
      vertical-align: super;
    }

    /* Session rollup: one compact block above the stream. */
    .cache-summary {
      background: var(--sl-color-neutral-50);
      border: 1px solid var(--sl-color-neutral-200);
      border-radius: var(--sl-border-radius-medium);
      display: flex;
      flex-wrap: wrap;
      gap: var(--sl-spacing-medium);
      margin-bottom: var(--sl-spacing-small);
      padding: var(--sl-spacing-x-small) var(--sl-spacing-small);
    }

    .cache-stat {
      display: grid;
      gap: 1px;
    }

    .cache-stat-label {
      color: var(--sl-color-neutral-600);
      font-size: var(--sl-font-size-x-small);
      letter-spacing: 0.04em;
      text-transform: uppercase;
    }

    .cache-stat-value {
      font-size: var(--sl-font-size-small);
      font-weight: 600;
    }

    .cache-stat-value.muted {
      font-style: italic;
      font-weight: 400;
      opacity: 0.75;
    }

    .cache-coverage {
      align-self: center;
      color: var(--sl-color-neutral-600);
      font-size: var(--sl-font-size-x-small);
      margin-left: auto;
      max-width: 22rem;
      text-align: right;
    }
  `;

  /** Human label for a token count that may legitimately be absent. */
  private static tokens(value: number | null | undefined) {
    return value === null || value === undefined
      ? html`<span class="not-reported">not reported</span>`
      : html`${formatNumber(value)}`;
  }

  /**
   * Format a cache-savings amount without rounding it away.
   *
   * The shared `formatCost` snaps anything >= $0.01 to two decimals, which
   * turns $0.0125 of measured savings into "$0.01" — a 20% understatement of a
   * figure whose whole point is precision. Sub-dollar savings therefore keep
   * four decimals here, and a non-zero amount below the last displayed digit
   * is shown as a "<" bound rather than as $0.00.
   */
  private static savings(value: number): string {
    if (value >= 1) return `$${value.toFixed(2)}`;
    if (value >= 0.0001) return `$${value.toFixed(4)}`;
    return value > 0 ? '<$0.0001' : '$0.00';
  }

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

  /**
   * One quiet line of cache accounting under an existing request row.
   *
   * Suppressed entirely when the provider reported no cache data for the call:
   * a row of three "not reported" values would be noise, and the session
   * summary already states how many requests are uncovered. When cache data
   * IS present, every field is shown with its honest status — an absent write
   * count reads "not reported", never 0, and a miss carries a marker when it
   * was derived rather than reported by the provider.
   */
  private renderCacheLine(row: RuntimeSessionRequestItem) {
    const cache = row.cache;
    if (!cache || !cache.has_cache_data) return nothing;
    const derived = cache.cache_miss_source === 'derived';
    const missTitle =
      cache.cache_miss_source === 'reported'
        ? 'Cache miss tokens reported directly by the provider'
        : derived
          ? 'Cache miss derived as prompt - cache read - cache write'
          : 'Provider reported no cache miss count';
    return html`
      <div class="cache-line" data-testid="request-cache-line">
        Cache: read ${SessionRequestTimeline.tokens(cache.cache_read_tokens)} ·
        write ${SessionRequestTimeline.tokens(cache.cache_creation_tokens)} ·
        <span class=${derived ? 'derived' : ''} title=${missTitle}
          >miss ${SessionRequestTimeline.tokens(cache.cache_miss_tokens)}</span
        >
        ${
          cache.usage_source && cache.usage_source !== 'provider'
            ? html` ·
                <span class="not-reported">tokens ${cache.usage_source}</span>`
            : nothing
        }
      </div>
    `;
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
            ${
              row.is_retry
                ? html`<sl-badge variant="warning" pill>retry</sl-badge>`
                : nothing
            }
          </div>
          <div class="request-meta">
            ${ts ? ts.toLocaleString() : 'no timestamp'} ·
            ${formatNumber(row.total_tokens)} tokens
            ${row.finish_reason ? html` · ${row.finish_reason}` : nothing}
          </div>
          ${
            row.tools.length
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
              : nothing
          }
          ${this.renderCacheLine(row)}
        </div>
        <div class="request-cost">
          <div class="cost-value">${formatCost(row.estimated_cost)}</div>
        </div>
      </div>
    `;
  }

  /**
   * Session-level cache rollup.
   *
   * Ratio and cached/uncached totals are over the covered requests only, and
   * the coverage note says so whenever any request lacked a provider cache
   * split. Savings are rendered only when the backend supplied an exact
   * catalog-priced figure; otherwise the reason is shown instead of a number.
   */
  private renderCacheSummary() {
    const summary = this.cacheSummary;
    if (!summary || !summary.requests_with_cache_data) return nothing;
    const ratio =
      summary.cache_hit_ratio === null
        ? null
        : Math.round(summary.cache_hit_ratio * 1000) / 10;
    const savingsOmittedText =
      summary.savings_omitted_reason === 'no_catalog_cache_price'
        ? 'no exact catalog price'
        : summary.savings_omitted_reason === 'no_cache_reads'
          ? 'no cache reads'
          : 'unavailable';
    return html`
      <div
        class="cache-summary"
        data-testid="session-cache-summary"
        role="group"
        aria-label="Prompt cache summary"
      >
        <div class="cache-stat">
          <span class="cache-stat-label">Cache hit ratio</span>
          <span class="cache-stat-value" data-testid="cache-hit-ratio">
            ${ratio === null ? 'not reported' : `${ratio}%`}
          </span>
        </div>
        <div class="cache-stat">
          <span class="cache-stat-label">Cached prompt</span>
          <span class="cache-stat-value"
            >${formatNumber(summary.cached_prompt_tokens)}</span
          >
        </div>
        <div class="cache-stat">
          <span class="cache-stat-label">Uncached prompt</span>
          <span class="cache-stat-value"
            >${formatNumber(summary.uncached_prompt_tokens)}</span
          >
        </div>
        <div class="cache-stat">
          <span class="cache-stat-label">Cache writes</span>
          <span
            class="cache-stat-value ${
              summary.cache_write_tokens === null ? 'muted' : ''
            }"
            data-testid="cache-write-tokens"
            title=${
              summary.cache_write_tokens === null
                ? 'No provider used in this session reports cache-write tokens'
                : 'Prompt tokens (re)written into the provider cache'
            }
          >
            ${
              summary.cache_write_tokens === null
                ? 'not reported'
                : formatNumber(summary.cache_write_tokens)
            }
          </span>
        </div>
        <div class="cache-stat">
          <span class="cache-stat-label">Est. cache savings</span>
          <span
            class="cache-stat-value ${
              summary.estimated_cache_savings_usd === null ? 'muted' : ''
            }"
            data-testid="cache-savings"
            title=${
              summary.estimated_cache_savings_usd === null
                ? 'Omitted: a savings figure is only shown when the price catalog supports it exactly'
                : 'Input price minus cache-read price over the tokens served from cache'
            }
          >
            ${
              summary.estimated_cache_savings_usd === null
                ? savingsOmittedText
                : SessionRequestTimeline.savings(
                    summary.estimated_cache_savings_usd
                  )
            }
          </span>
        </div>
        ${
          summary.requests_without_cache_data
            ? html`<span class="cache-coverage" data-testid="cache-coverage">
                Based on ${formatNumber(summary.requests_with_cache_data)} of
                ${formatNumber(summary.requests_total)} requests;
                ${formatNumber(summary.requests_without_cache_data)} reported no
                cache data (${formatNumber(summary.uncovered_prompt_tokens)}
                prompt tokens excluded, not counted as misses).
              </span>`
            : nothing
        }
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
      ${this.renderCacheSummary()}
      ${
        this.loading && !rows.length
          ? html`<div class="empty"><sl-spinner></sl-spinner></div>`
          : rows.length
            ? html`<div class="stream">
                ${rows.map((row) => this.renderRequest(row))}
              </div>`
            : html`<div class="empty">
                No requests match the current filter.
              </div>`
      }
      ${
        this.hasMore
          ? html`
              <div
                style="text-align:center;margin-top:var(--sl-spacing-small);"
              >
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
          : nothing
      }
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'session-request-timeline': SessionRequestTimeline;
  }
}
