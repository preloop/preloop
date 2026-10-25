import { LitElement, html, css, unsafeCSS, nothing } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import consoleStyles from '../../styles/console-styles.css?inline';
import {
  listDeliveryReports,
  type DeliveryReportRow,
} from '../../delivery-reports-api';
import { PermissionError } from '../../permissions';
import { formatRelativeTime, parseUTCDate } from '../../utils/date';
import { formatUsd } from '../../utils/money';
import { periodLabel, runLabel } from '../../utils/delivery-report';
import '../../components/view-header';
import '../../components/permission-denied';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/option/option.js';
import '@shoelace-style/shoelace/dist/components/select/select.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';

export const PAGE_SIZE = 25;
export const VIEW_MODE_KEY = 'preloop.delivery_reports.view_mode';

const RANGES: { value: string; label: string; days: number | null }[] = [
  { value: '30', label: 'Last 30 days', days: 30 },
  { value: '90', label: 'Last 90 days', days: 90 },
  { value: '365', label: 'Last year', days: 365 },
  { value: 'all', label: 'All time', days: null },
];

type ViewMode = 'list' | 'cards';

function storedViewMode(): ViewMode {
  try {
    return localStorage.getItem(VIEW_MODE_KEY) === 'cards' ? 'cards' : 'list';
  } catch {
    return 'list';
  }
}

/** Money as the console prints it; unknown spend is a dash, never $0.00. */
export function spendLabel(value: number | null | undefined): string {
  return typeof value === 'number' ? `${formatUsd(value)} est.` : '—';
}

/**
 * `/console/delivery-reports`: one row per Delivery Report run, newest first.
 *
 * A filter bar, an honest count and a table (cards on a phone). Paging uses
 * the server's total; search and the project filter narrow the page in view
 * and the count says so.
 */
@customElement('delivery-reports-view')
export class DeliveryReportsView extends LitElement {
  static styles = [
    unsafeCSS(consoleStyles),
    css`
      .filter-bar {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        gap: 8px;
        margin-bottom: 12px;
      }
      .filter-bar sl-input {
        flex: 1 1 240px;
        max-width: 280px;
      }
      .filter-bar sl-select {
        width: 160px;
      }
      /* Named for a screen reader; the filter bar has no room to print it. */
      .filter-bar sl-input::part(form-control-label),
      .filter-bar sl-select::part(form-control-label) {
        position: absolute;
        width: 1px;
        height: 1px;
        padding: 0;
        margin: -1px;
        overflow: hidden;
        clip: rect(0 0 0 0);
        white-space: nowrap;
        border: 0;
      }
      .count {
        margin-left: auto;
        color: var(--console-meta-color);
        font-size: var(--console-text-meta);
        font-variant-numeric: tabular-nums;
      }
      table {
        width: 100%;
        border-collapse: collapse;
      }
      th,
      td {
        padding: 10px 16px;
        text-align: left;
        border-bottom: 1px solid var(--console-hairline);
        font-size: var(--console-text-body);
        vertical-align: top;
      }
      th {
        color: var(--console-meta-color);
        font-weight: 500;
      }
      .num {
        text-align: right;
        font-variant-numeric: tabular-nums;
      }
      .meta {
        color: var(--console-meta-color);
      }
      a {
        color: var(--sl-color-primary-700);
        text-decoration: none;
      }
      a:hover,
      a:focus-visible {
        text-decoration: underline;
      }
      .cards {
        list-style: none;
        margin: 0;
        padding: 0;
      }
      .cards li {
        padding: 12px 16px;
        border-bottom: 1px solid var(--console-hairline);
      }
      .cards .facts {
        margin-top: 4px;
        color: var(--console-meta-color);
        font-size: var(--console-text-meta);
        font-variant-numeric: tabular-nums;
      }
      .empty {
        display: flex;
        align-items: center;
        justify-content: center;
        min-height: 72px;
        color: var(--console-meta-color);
      }
      .pager {
        display: flex;
        justify-content: flex-end;
        align-items: center;
        gap: 8px;
        margin-top: 12px;
        font-variant-numeric: tabular-nums;
      }
      .view-switch {
        display: inline-flex;
      }
      @media (max-width: 640px) {
        .view-switch {
          display: none;
        }
        .filter-bar sl-input,
        .filter-bar sl-select {
          max-width: none;
          width: 100%;
        }
      }
    `,
  ];

  @state() private rows: DeliveryReportRow[] = [];
  @state() private total = 0;
  @state() private offset = 0;
  @state() private range = '30';
  @state() private search = '';
  @state() private project = '';
  @state() private loading = true;
  @state() private error: string | null = null;
  @state() private permissionError: PermissionError | null = null;
  @state() private viewMode: ViewMode = storedViewMode();
  @state() private narrow = false;

  private media: MediaQueryList | null = null;
  private readonly onMedia = () => {
    this.narrow = Boolean(this.media?.matches);
  };

  connectedCallback(): void {
    super.connectedCallback();
    if (typeof window.matchMedia === 'function') {
      this.media = window.matchMedia('(max-width: 640px)');
      this.narrow = this.media.matches;
      this.media.addEventListener?.('change', this.onMedia);
    }
    void this.load();
  }

  disconnectedCallback(): void {
    this.media?.removeEventListener?.('change', this.onMedia);
    super.disconnectedCallback();
  }

  private since(): string | null {
    const days = RANGES.find((r) => r.value === this.range)?.days;
    if (!days) return null;
    return new Date(Date.now() - days * 86400000).toISOString();
  }

  async load(): Promise<void> {
    this.loading = true;
    this.error = null;
    try {
      const page = await listDeliveryReports({
        limit: PAGE_SIZE,
        offset: this.offset,
        since: this.since(),
      });
      this.rows = page.items;
      this.total = page.total;
    } catch (error) {
      if (error instanceof PermissionError) {
        this.permissionError = error;
      } else {
        this.error = 'Could not load delivery reports.';
      }
    } finally {
      this.loading = false;
    }
  }

  private visibleRows(): DeliveryReportRow[] {
    const needle = this.search.trim().toLowerCase();
    return this.rows.filter((row) => {
      if (this.project && !row.projects.includes(this.project)) return false;
      if (!needle) return true;
      const haystack = [
        runLabel(row.window, row.started_at),
        periodLabel(row.window),
        row.flow_name ?? '',
        ...row.projects,
      ]
        .join(' ')
        .toLowerCase();
      return haystack.includes(needle);
    });
  }

  private setViewMode(mode: ViewMode): void {
    this.viewMode = mode;
    try {
      localStorage.setItem(VIEW_MODE_KEY, mode);
    } catch {
      /* storage unavailable: the choice lasts for this visit */
    }
  }

  private countLabel(visible: number): string {
    const noun = this.total === 1 ? 'report' : 'reports';
    const filtered = Boolean(this.search.trim() || this.project);
    if (!filtered) return `${this.total} ${noun}`;
    // Search and the project filter narrow the page in view, not the
    // whole range, so the count names both.
    return `${visible} of ${this.rows.length} on this page · ${this.total} ${noun}`;
  }

  private renderTime(value: string | null) {
    if (!value) return html`<span class="meta">—</span>`;
    return html`<span class="meta" title=${parseUTCDate(value).toLocaleString()}
      >${formatRelativeTime(value, new Date(), { maxRelativeDays: 3650 })}</span
    >`;
  }

  private renderTable(rows: DeliveryReportRow[]) {
    return html`<div class="table-card">
      <table data-testid="reports-table">
        <thead>
          <tr>
            <th scope="col" aria-sort="descending">Report</th>
            <th scope="col">Period</th>
            <th scope="col">Projects</th>
            <th scope="col" class="num">Shipped</th>
            <th
              scope="col"
              class="num"
              title="Closed issues that are partial or have an evidence gap"
            >
              Unverified closes
            </th>
            <th scope="col" class="num">Proposals</th>
            <th scope="col" class="num">Spend</th>
            <th scope="col">Ran</th>
          </tr>
        </thead>
        <tbody>
          ${rows.map(
            (row) =>
              html`<tr data-testid="report-row">
                <td>
                  <a href="/console/delivery-reports/${row.id}"
                    >${runLabel(row.window, row.started_at)}</a
                  >
                  ${this.renderStatus(row)}
                </td>
                <td>${periodLabel(row.window)}</td>
                <td>
                  ${row.projects.join(', ') || html`<span class="meta">—</span>`}
                </td>
                <td class="num">${row.shipped}</td>
                <td class="num">${row.closed_unverified}</td>
                <td class="num">${row.proposals}</td>
                <td class="num">${spendLabel(row.cost_usd)}</td>
                <td>${this.renderTime(row.started_at)}</td>
              </tr>`
          )}
        </tbody>
      </table>
    </div>`;
  }

  private renderStatus(row: DeliveryReportRow) {
    if (row.status === 'SUCCEEDED') return nothing;
    const failed = row.status === 'FAILED' || row.report_status === 'error';
    return html`<sl-badge pill variant=${failed ? 'danger' : 'neutral'}
      >${row.status}</sl-badge
    >`;
  }

  private renderCards(rows: DeliveryReportRow[]) {
    return html`<ul class="cards table-card" data-testid="reports-cards">
      ${rows.map(
        (row) =>
          html`<li data-testid="report-row">
            <a href="/console/delivery-reports/${row.id}"
              >${runLabel(row.window, row.started_at)}</a
            >
            ${this.renderStatus(row)}
            <div class="facts">
              ${periodLabel(row.window)} · ${row.shipped} shipped ·
              ${row.closed_unverified} unverified closes · ${row.proposals}
              proposals · ${spendLabel(row.cost_usd)}
            </div>
          </li>`
      )}
    </ul>`;
  }

  private renderBody() {
    if (this.loading && this.rows.length === 0) {
      return html`<div class="empty">Loading…</div>`;
    }
    if (this.error) {
      return html`<div class="empty" role="alert">${this.error}</div>`;
    }
    const rows = this.visibleRows();
    if (rows.length === 0) {
      return html`<div class="empty">
        ${
          this.total === 0
            ? html`No delivery reports yet.&nbsp;<a href="/console/flows/new"
                  >Create a Delivery Report flow</a
                >`
            : 'No reports match these filters.'
        }
      </div>`;
    }
    const cards = this.narrow || this.viewMode === 'cards';
    return cards ? this.renderCards(rows) : this.renderTable(rows);
  }

  private renderPager() {
    if (this.total <= PAGE_SIZE) return nothing;
    const first = this.offset + 1;
    const last = Math.min(this.offset + PAGE_SIZE, this.total);
    return html`<div class="pager" data-testid="pager">
      <span class="meta">${first}–${last} of ${this.total}</span>
      <sl-button
        size="small"
        ?disabled=${this.offset === 0}
        @click=${() => {
          this.offset = Math.max(0, this.offset - PAGE_SIZE);
          void this.load();
        }}
        >Previous</sl-button
      >
      <sl-button
        size="small"
        ?disabled=${last >= this.total}
        @click=${() => {
          this.offset += PAGE_SIZE;
          void this.load();
        }}
        >Next</sl-button
      >
    </div>`;
  }

  render() {
    if (this.permissionError) {
      return html`<view-header
          headerText="Delivery reports"
          width="wide"
        ></view-header>
        <div class="column-layout wide">
          <permission-denied
            required-permission=${
              this.permissionError.requiredPermission || 'view_flows'
            }
            message=${this.permissionError.message}
          ></permission-denied>
        </div>`;
    }
    const projects = [...new Set(this.rows.flatMap((r) => r.projects))].sort();
    const visible = this.visibleRows();
    return html`
      <view-header
        headerText="Delivery reports"
        description="What landed in each period, what the available evidence supports against stated criteria, and which gaps deserve investigation."
        width="wide"
      ></view-header>
      <div class="column-layout wide">
        <div class="main-column">
          <div class="filter-bar">
            <sl-input
              size="small"
              placeholder="Search reports"
              label="Search reports"
              clearable
              .value=${this.search}
              @sl-input=${(e: Event) => {
                this.search = (e.target as HTMLInputElement).value;
              }}
            >
              <sl-icon name="search" slot="prefix"></sl-icon>
            </sl-input>
            <sl-select
              size="small"
              label="Project"
              .value=${this.project || 'all'}
              @sl-change=${(e: Event) => {
                const value = (e.target as HTMLSelectElement).value;
                this.project = value === 'all' ? '' : value;
              }}
            >
              <sl-option value="all">All projects</sl-option>
              ${projects.map(
                (p) => html`<sl-option value=${p}>${p}</sl-option>`
              )}
            </sl-select>
            <sl-select
              size="small"
              label="Time range"
              .value=${this.range}
              @sl-change=${(e: Event) => {
                this.range = (e.target as HTMLSelectElement).value;
                this.offset = 0;
                void this.load();
              }}
            >
              ${RANGES.map(
                (r) => html`<sl-option value=${r.value}>${r.label}</sl-option>`
              )}
            </sl-select>
            <span class="count" aria-live="polite" data-testid="report-count"
              >${
                this.loading && this.rows.length === 0
                  ? ''
                  : this.countLabel(visible.length)
              }</span
            >
            <span class="view-switch" role="group" aria-label="View">
              <sl-button
                size="small"
                variant=${this.viewMode === 'list' ? 'primary' : 'default'}
                outline
                aria-pressed=${this.viewMode === 'list'}
                @click=${() => this.setViewMode('list')}
                ><sl-icon name="list-ul" label="List"></sl-icon
              ></sl-button>
              <sl-button
                size="small"
                variant=${this.viewMode === 'cards' ? 'primary' : 'default'}
                outline
                aria-pressed=${this.viewMode === 'cards'}
                @click=${() => this.setViewMode('cards')}
                ><sl-icon name="grid" label="Cards"></sl-icon
              ></sl-button>
            </span>
          </div>
          ${this.renderBody()} ${this.renderPager()}
        </div>
      </div>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'delivery-reports-view': DeliveryReportsView;
  }
}
