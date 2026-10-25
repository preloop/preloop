import { REST_API_PREFIX } from '../api-path';
/** Cached outcome evidence, available when the weekly_work_value capability is on. */
import { LitElement, css, html, nothing } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import { fetchWithAuth } from '../api';
import { formatUsdExact } from '../utils/money';

interface Product {
  url: string;
  title: string;
  additions: number | null;
  deletions: number | null;
  files: number | null;
  merged: boolean;
  credit_share: number;
}
interface Outcome {
  id: string;
  kind: string;
  title: string;
  href: string;
  cost: number | null;
  score: number | null;
  produced: Product[];
  issues_closed: number;
  issues_referenced?: number;
  closed_issue_urls?: string[];
  reviews?: {
    url: string;
    comment_ids: string[];
    href?: string;
    addressed: boolean;
    reason: string | null;
  }[];
  review_comments_posted: number;
  review_comments_addressed: number;
}
interface WeeklyValue {
  coverage_notes?: string[];
  items: Outcome[];
  method: string;
  prs_observed: number;
  prs_considered: number;
  window_start: string;
  window_end: string;
}

@customElement('weekly-value-section')
export class WeeklyValueSection extends LitElement {
  @property() start = '';
  @property() end = '';
  @state() private result: WeeklyValue | null = null;
  @state() private error = '';
  @state() private loading = false;
  private generation = 0;

  static styles = css`
    :host {
      display: block;
      margin: 24px 0;
      color: var(--sl-color-neutral-900);
    }
    h2 {
      font-size: 18px;
      font-weight: 600;
    }
    p,
    li {
      font-size: 14px;
      line-height: 1.5;
    }
    .muted {
      color: var(--sl-color-neutral-600);
    }
    .scroll {
      overflow-x: auto;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 14px;
    }
    th,
    td {
      padding: 12px 8px;
      text-align: left;
      vertical-align: top;
      border-bottom: 1px solid var(--console-hairline);
    }
    .number {
      text-align: right;
      font-variant-numeric: tabular-nums;
      white-space: nowrap;
    }
    a {
      color: var(--sl-color-primary-600);
    }
    ul {
      margin: 4px 0;
      padding-left: 16px;
    }
    button {
      color: var(--sl-color-primary-600);
      background: none;
      border: none;
      cursor: pointer;
      font: inherit;
    }
  `;

  protected willUpdate(changed: Map<string, unknown>): void {
    if (changed.has('start') || changed.has('end')) void this.load();
  }

  private async load(): Promise<void> {
    const generation = ++this.generation;
    this.loading = true;
    this.error = '';
    const params = new URLSearchParams();
    if (this.start) params.set('start', this.start);
    if (this.end) params.set('end', this.end);
    try {
      const response = await fetchWithAuth(
        `${REST_API_PREFIX}/billing/weekly-value?${params}`
      );
      if (!response.ok)
        throw new Error('Recorded outcomes could not be loaded.');
      const result = (await response.json()) as WeeklyValue;
      if (generation === this.generation) this.result = result;
    } catch {
      if (generation === this.generation)
        this.error = 'Recorded outcomes could not be loaded.';
    } finally {
      if (generation === this.generation) this.loading = false;
    }
  }

  render() {
    return html`<section aria-label="Most valuable work: recorded outcomes">
      <h2>Most valuable work: recorded outcomes</h2>
      ${this.loading ? html`<p role="status">Loading recorded outcomes…</p>` : nothing}
      ${this.error ? html`<p role="alert">${this.error} <button @click=${() => void this.load()}>Retry</button></p>` : nothing}
      ${
        this.result && !this.loading && !this.error
          ? html`
              <p class="muted">${this.result.method}</p>
              ${(this.result.coverage_notes ?? []).map((note) => html`<p class="muted">${note}</p>`)}
              <p class="muted">
                ${this.result.window_start.slice(0, 10)} to
                ${this.result.window_end.slice(0, 10)}. Forge evidence available
                for ${this.result.prs_observed} of ${this.result.prs_considered}
                known PRs/MRs. No model calls.
              </p>
              ${
                !this.result.items.length
                  ? html`<p>
                      No attributed repository outcomes in this window.
                    </p>`
                  : html` <div class="scroll">
                      <table>
                        <thead>
                          <tr>
                            <th scope="col">Session or flow</th>
                            <th scope="col">Recorded evidence</th>
                            <th scope="col" class="number">Cost est.</th>
                            <th scope="col" class="number">Points / $</th>
                          </tr>
                        </thead>
                        <tbody>
                          ${this.result.items.map(
                            (row) =>
                              html`<tr>
                                <td>
                                  <a href=${row.href}>${row.title}</a>
                                  <div class="muted">
                                    ${row.kind === 'flow' ? 'Flow execution' : 'Session'}
                                  </div>
                                </td>
                                <td>
                                  <ul>
                                    ${row.produced.map((p) => html`<li><a href=${p.url} target="_blank" rel="noopener noreferrer">${p.title} ↗</a>: ${p.merged ? 'merged' : 'published'}; ${p.additions === null ? 'line counts unavailable' : `+${p.additions} / -${p.deletions} lines`}${p.files === null ? '' : `; ${p.files} files`}${p.credit_share < 1 ? `; ${(p.credit_share * 100).toFixed(0)}% shared credit` : ''}</li>`)}
                                  </ul>
                                  ${row.issues_referenced ? html`<div>${row.issues_referenced} issues referenced</div>` : nothing}
                                  ${row.issues_closed ? html`<div>${row.issues_closed} issues closed</div>` : nothing}
                                  ${
                                    row.review_comments_posted
                                      ? html`<details>
                                          <summary>
                                            ${row.review_comments_addressed}/${row.review_comments_posted}
                                            review comments addressed
                                          </summary>
                                          <ul>
                                            ${(row.reviews ?? []).map((review) => html`<li><a href=${review.href ?? review.url} target="_blank" rel="noopener noreferrer">Comment ${review.comment_ids[0]} ↗</a>: ${review.reason ?? (review.addressed ? 'Addressed' : 'No evidence of being addressed')}</li>`)}
                                          </ul>
                                        </details>`
                                      : nothing
                                  }
                                </td>
                                <td class="number">
                                  ${row.cost === null ? 'Unknown' : formatUsdExact(row.cost)}
                                </td>
                                <td class="number">
                                  ${row.score === null ? 'Unranked' : row.score.toFixed(2)}
                                </td>
                              </tr>`
                          )}
                        </tbody>
                      </table>
                    </div>`
              }
            `
          : nothing
      }
    </section>`;
  }
}
