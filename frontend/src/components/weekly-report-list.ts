import { html, css, nothing, unsafeCSS } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import { AuthedElement, fetchWithAuth } from '../api';
import { REST_API_PREFIX } from '../api-path';
import { parseDigestLink } from '../utils/digest-period';
import { LOCATION_CHANGED } from '../router';
import consoleStyles from '../styles/console-styles.css?inline';
import './view-header';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';

interface ReportRow {
  label: string;
  value: string;
  meta: string;
  detail: string;
  url: string | null;
  links?: ReportRow[];
}

interface ReportItems {
  title: string;
  items: ReportRow[];
  total: number;
  notes: string[];
}

const filters = [
  'kind',
  'account_id',
  'start_date',
  'end_date',
  'user_id',
  'model',
  'agent',
  'flow',
  'status',
  'item',
  'offset',
];

export function withoutReportFilters(href: string): string {
  const url = new URL(href, window.location.origin);
  for (const key of filters) url.searchParams.delete(key);
  return url.pathname + url.search + url.hash;
}

/** The weekly report's counted rows, inside an existing console page. */
@customElement('weekly-report-list')
export class WeeklyReportList extends AuthedElement {
  @state() private data: ReportItems | null = null;
  @state() private error = '';
  @state() private loading = false;
  private generation = 0;
  private controller: AbortController | null = null;
  private locationKey = '';
  private readonly restore = (): void => {
    if (this.locationKey !== window.location.pathname + window.location.search)
      void this.load();
  };

  static styles = [
    unsafeCSS(consoleStyles),
    css`
      :host {
        display: block;
      }
      .filters {
        display: flex;
        flex-wrap: wrap;
        gap: var(--sl-spacing-small);
        align-items: center;
        margin-block: var(--sl-spacing-medium);
      }
      .table-scroll {
        overflow-x: auto;
      }
      table {
        font-size: var(--console-text-body);
      }
      th,
      td {
        vertical-align: top;
      }
      sl-badge::part(base) {
        white-space: normal;
        overflow-wrap: anywhere;
      }
      td p {
        margin-block: var(--sl-spacing-x-small);
      }

      .pagination {
        display: flex;
        gap: var(--sl-spacing-medium);
        align-items: center;
        margin-block: var(--sl-spacing-medium);
      }
    `,
  ];

  connectedCallback(): void {
    super.connectedCallback();
    window.addEventListener('popstate', this.restore);
    window.addEventListener(LOCATION_CHANGED, this.restore);
    void this.load();
  }

  disconnectedCallback(): void {
    ++this.generation;
    this.controller?.abort();
    window.removeEventListener('popstate', this.restore);
    window.removeEventListener(LOCATION_CHANGED, this.restore);
    super.disconnectedCallback();
  }

  private async load(): Promise<void> {
    this.locationKey = window.location.pathname + window.location.search;
    const generation = ++this.generation;
    this.controller?.abort();
    this.controller = new AbortController();
    this.data = null;
    this.error = '';
    const params = new URLSearchParams(window.location.search);
    const link = parseDigestLink(window.location.search);
    if (
      link.error ||
      !link.period ||
      !link.accountId ||
      filters.some((key) => params.getAll(key).length > 1)
    ) {
      this.error =
        'Invalid report filters. Open the complete link from your report.';
      this.loading = false;
      return;
    }
    this.loading = true;
    const query = new URLSearchParams();
    for (const key of filters) {
      const value = params.get(key);
      if (value !== null) query.set(key, value);
    }
    try {
      const response = await fetchWithAuth(
        `${REST_API_PREFIX}/cost/report-items?${query}`,
        { signal: this.controller.signal }
      );
      if (generation !== this.generation) return;
      if (!response.ok) {
        this.error =
          response.status === 409
            ? 'This report belongs to a different account. Switch accounts using the account menu, then reopen this link.'
            : response.status === 403
              ? 'You do not have permission to view these report items.'
              : response.status === 404
                ? 'Weekly report lists are not available on this instance.'
                : 'Could not load the report items. Try again.';
        return;
      }
      const data = (await response.json()) as ReportItems;
      if (generation === this.generation) this.data = data;
    } catch (error) {
      if (
        generation === this.generation &&
        !(error instanceof DOMException && error.name === 'AbortError')
      )
        this.error = 'Could not load the report items. Try again.';
    } finally {
      if (generation === this.generation) this.loading = false;
    }
  }

  private page(offset: number): void {
    const url = new URL(window.location.href);
    url.searchParams.set('offset', String(offset));
    window.history.pushState({}, '', url.pathname + url.search + url.hash);
    void this.load();
  }

  render() {
    const params = new URLSearchParams(window.location.search);
    const period = parseDigestLink(window.location.search).period;
    const offset = Number(params.get('offset') || 0);
    return html`
      <view-header
        .headerText=${this.data?.title ?? 'Weekly report items'}
      ></view-header>
      <div class="filters" aria-label="Report filters">
        <sl-badge
          class="tag-chip"
          pill
          title=${period ? `${period.startDate} to ${period.endDate} (UTC, end exclusive)` : 'Invalid window'}
        >
          <sl-icon name="calendar3"></sl-icon> Week:
          ${period ? `${period.startDate} to ${period.endDate} (UTC, end exclusive)` : 'Invalid window'}
        </sl-badge>
        ${['kind', 'model', 'agent', 'flow', 'status', 'item', 'user_id'].map((key) => (params.has(key) ? html`<sl-badge class="tag-chip" pill><sl-icon name="tag"></sl-icon>${key}: ${params.get(key)}</sl-badge>` : nothing))}
        <a href=${withoutReportFilters(window.location.href)}>Clear filters</a>
      </div>
      ${this.loading ? html`<p role="status">Loading report items…</p>` : nothing}
      ${
        this.error
          ? html`<p role="alert">${this.error}</p>
              <sl-button @click=${() => void this.load()}>Retry</sl-button>`
          : nothing
      }
      ${
        this.data
          ? html`
              ${this.data.notes.map((note) => html`<p>${note}</p>`)}
              <p role="status">${this.data.total} items</p>
              ${
                this.data.items.length
                  ? html`<div class="table-scroll">
                      <table class="styled-table" aria-label=${this.data.title}>
                        <thead>
                          <tr>
                            <th scope="col">Item</th>
                            <th scope="col">Measured</th>
                            <th scope="col">Evidence</th>
                          </tr>
                        </thead>
                        <tbody>
                          ${this.data.items.map(
                            (row) =>
                              html`<tr>
                                <td>
                                  ${row.url?.startsWith('/console/') ? html`<a href=${row.url}>${row.label}</a>` : row.label}
                                </td>
                                <td>${row.value}</td>
                                <td>
                                  ${row.meta}${row.detail ? html`<p class="meta">${row.detail}</p>` : nothing}
                                  ${(row.links ?? []).map(
                                    (link) =>
                                      html`<p>
                                        ${link.url?.startsWith('/console/') ? html`<a href=${link.url}>${link.label}</a>` : link.label}
                                        ${link.value}
                                      </p>`
                                  )}
                                </td>
                              </tr>`
                          )}
                        </tbody>
                      </table>
                    </div>`
                  : html`<p>No items match these filters.</p>`
              }
              <div class="pagination">
                <sl-button
                  ?disabled=${offset === 0}
                  @click=${() => this.page(Math.max(0, offset - 50))}
                  >Previous</sl-button
                >
                <span
                  >${this.data.total ? offset + 1 : 0}–${Math.min(offset + this.data.items.length, this.data.total)}
                  of ${this.data.total}</span
                >
                <sl-button
                  ?disabled=${offset + 50 >= this.data.total}
                  @click=${() => this.page(offset + 50)}
                  >Next</sl-button
                >
              </div>
            `
          : nothing
      }
    `;
  }
}
