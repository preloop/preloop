import {
  html,
  css,
  unsafeCSS,
  nothing,
  type PropertyValues,
  type TemplateResult,
} from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import {
  AuthedElement,
  getWorkItemCosts,
  type WorkItemCostReport,
  type WorkItemGroup,
  type WorkItemRef,
  type WorkItemSlice,
} from '../../api';
import consoleStyles from '../../styles/console-styles.css?inline';
import { tableScrollStyles } from '../../styles/table-scroll';
import { resolveTimeRange } from '../../utils/time-range';
import { formatUsd } from '../../utils/money';
import { formatCount } from '../../utils/count-format';
import '../../components/view-header.ts';
import '../../components/time-range-select.ts';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/card/card.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';

const RANGE_OPTIONS = [
  { value: 'last-7', label: '7d' },
  { value: 'last-30', label: '30d' },
  { value: 'last-90', label: '90d' },
];

export const WORK_ITEM_PAGE_SIZE = 50;

/** "$1.23 est." with the console's sub-cent rule; blank for nothing. */
export function formatWorkItemCost(value: number | null | undefined): string {
  if (value === null || value === undefined) return '';
  return `${formatUsd(value)} est.`;
}

/** Short label for a work item kind. */
export function workItemKindLabel(kind: WorkItemRef['kind']): string {
  return kind === 'pull_request' ? 'PR' : 'Issue';
}

/** Share of a group's spend, as a whole percent for a bar width. */
export function sliceShare(slice: WorkItemSlice, total: number): number {
  if (!total || total <= 0) return 0;
  return Math.max(
    0,
    Math.min(100, Math.round((slice.estimated_cost / total) * 100))
  );
}

/** "Issue + 2 PRs", the group's makeup in a few words. */
export function groupMakeup(group: WorkItemGroup): string {
  const issues = group.items.filter((i) => i.kind === 'issue').length;
  const pulls = group.items.length - issues;
  const parts: string[] = [];
  if (issues) parts.push(issues === 1 ? '1 issue' : `${issues} issues`);
  if (pulls) parts.push(pulls === 1 ? '1 PR' : `${pulls} PRs`);
  return parts.join(' + ');
}

@customElement('work-item-cost-view')
export class WorkItemCostView extends AuthedElement {
  @state() report: WorkItemCostReport | null = null;
  @state() loading = false;
  @state() error: string | null = null;
  @state() range = 'last-30';
  /**
   * Rendered inside the Spend page's main column: no page header, no range
   * selector of its own (one range per page) and no links back to Spend.
   */
  @property({ type: Boolean, reflect: true }) embedded = false;
  /** The Spend page's window; when set it replaces the local range. */
  @property({ attribute: false }) period: {
    startDate: string;
    endDate: string;
  } | null = null;
  @state() offset = 0;
  @state() expanded = new Set<string>();
  /** Bumped per fetch; an older response landing late is dropped. */
  private request = 0;

  static styles = [
    tableScrollStyles,
    unsafeCSS(consoleStyles),
    css`
      :host {
        display: block;
      }
      .page {
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-large);
      }
      .toolbar {
        display: flex;
        flex-wrap: wrap;
        gap: var(--sl-spacing-small);
        align-items: center;
      }
      .toolbar .spacer {
        flex: 1;
      }
      .totals {
        display: flex;
        flex-wrap: wrap;
        gap: var(--sl-spacing-x-large);
        align-items: baseline;
      }
      .stat-value {
        font-size: var(--console-stat-value, 22px);
        font-weight: 600;
      }
      .stat-label,
      .muted {
        color: var(--console-meta-color);
        font-size: var(--console-text-meta, 13px);
      }
      table {
        width: 100%;
        border-collapse: collapse;
        font-size: var(--console-text-body, 14px);
      }
      th,
      td {
        padding: var(--sl-spacing-x-small) var(--sl-spacing-small);
        text-align: left;
        border-bottom: 1px solid
          var(--console-border-color, var(--sl-color-neutral-200));
        vertical-align: top;
      }
      th {
        font-weight: 600;
        color: var(--console-meta-color);
        font-size: var(--console-text-meta, 13px);
      }
      .num {
        text-align: right;
        white-space: nowrap;
        font-variant-numeric: tabular-nums;
      }
      .expand {
        background: none;
        border: none;
        cursor: pointer;
        color: var(--console-meta-color);
        padding: var(--sl-spacing-2x-small);
        border-radius: var(--sl-border-radius-small);
        display: inline-flex;
      }
      .expand:hover {
        background: var(--console-hover-tint);
        color: inherit;
      }
      .item-title {
        display: block;
        max-width: 32rem;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .detail {
        display: grid;
        gap: var(--sl-spacing-medium);
        padding: var(--sl-spacing-small) 0 var(--sl-spacing-medium);
      }
      .dimensions {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(14rem, 1fr));
        gap: var(--sl-spacing-large);
      }
      .dimension h4 {
        margin: 0 0 var(--sl-spacing-2x-small);
        font-size: var(--console-text-meta, 13px);
        color: var(--console-meta-color);
        font-weight: 600;
      }
      .slice {
        display: grid;
        grid-template-columns: 1fr auto;
        gap: 0 var(--sl-spacing-small);
        font-size: var(--console-text-meta, 13px);
        padding: 2px 0;
      }
      .slice .bar {
        grid-column: 1 / -1;
        height: 3px;
        border-radius: 2px;
        background: var(--sl-color-neutral-200);
        overflow: hidden;
      }
      .slice .bar span {
        display: block;
        height: 100%;
        background: var(--sl-color-primary-600);
      }
      .members {
        list-style: none;
        margin: 0;
        padding: 0;
        display: grid;
        gap: var(--sl-spacing-2x-small);
      }
      .members li {
        display: grid;
        grid-template-columns: 3.5rem minmax(0, 1fr) auto auto;
        gap: var(--sl-spacing-small);
        align-items: baseline;
        font-size: var(--console-text-meta, 13px);
      }
      .sr-only {
        position: absolute;
        width: 1px;
        height: 1px;
        overflow: hidden;
        clip: rect(0 0 0 0);
        white-space: nowrap;
      }
      .empty {
        min-height: 72px;
        display: flex;
        align-items: center;
        justify-content: center;
        color: var(--console-meta-color);
      }
      .pager {
        display: flex;
        gap: var(--sl-spacing-small);
        align-items: center;
        justify-content: flex-end;
      }
    `,
  ];

  protected willUpdate(changed: PropertyValues<this>): void {
    const previous = changed.get('period') as
      { startDate: string; endDate: string } | null | undefined;
    if (
      changed.has('period') &&
      previous !== undefined &&
      (previous?.startDate !== this.period?.startDate ||
        previous?.endDate !== this.period?.endDate)
    ) {
      this.onPeriodChanged();
    }
  }

  connectedCallback(): void {
    super.connectedCallback();
    void this.load();
  }

  async load(): Promise<void> {
    const request = ++this.request;
    this.loading = true;
    this.error = null;
    const window = this.period ?? resolveTimeRange(this.range);
    try {
      const report = await getWorkItemCosts({
        startDate: window.startDate,
        endDate: window.endDate,
        limit: WORK_ITEM_PAGE_SIZE,
        offset: this.offset,
      });
      if (request !== this.request) return;
      this.report = report;
    } catch (error) {
      if (request !== this.request) return;
      this.error =
        error instanceof Error
          ? error.message
          : 'Failed to load work item cost';
    } finally {
      if (request === this.request) this.loading = false;
    }
  }

  private onPeriodChanged(): void {
    this.offset = 0;
    this.expanded = new Set();
    void this.load();
  }

  private onRange = (event: Event) => {
    const value = (event as CustomEvent<{ value?: string }>).detail?.value;
    if (!value || value === this.range) return;
    this.range = value;
    this.offset = 0;
    this.expanded = new Set();
    void this.load();
  };

  private toggle(key: string): void {
    const next = new Set(this.expanded);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    this.expanded = next;
  }

  private page(delta: number): void {
    this.offset = Math.max(0, this.offset + delta * WORK_ITEM_PAGE_SIZE);
    this.expanded = new Set();
    void this.load();
  }

  private renderLink(
    item: { key: string; url: string | null },
    title: string | null
  ): TemplateResult {
    const label = html`<strong>${item.key}</strong>`;
    return html`${
      item.url
        ? html`<a href=${item.url} target="_blank" rel="noopener">${label}</a>`
        : label
    }
    ${
      title
        ? html`<span class="item-title muted" title=${title}>${title}</span>`
        : nothing
    }`;
  }

  private renderSlices(
    heading: string,
    slices: WorkItemSlice[],
    total: number
  ): TemplateResult {
    return html`<div class="dimension">
      <h4>${heading}</h4>
      ${
        slices.length
          ? slices.map(
              (slice) =>
                html`<div
                  class="slice"
                  title=${`${formatCount(slice.total_tokens)} tokens`}
                >
                  <span>${slice.label}</span>
                  <span class="num">${formatUsd(slice.estimated_cost)}</span>
                  <div class="bar">
                    <span style=${`width:${sliceShare(slice, total)}%`}></span>
                  </div>
                </div>`
            )
          : html`<span class="muted">None</span>`
      }
    </div>`;
  }

  private renderDetail(group: WorkItemGroup): TemplateResult {
    return html`<div class="detail">
      <ul class="members" aria-label="Issues and pull requests">
        ${group.items.map(
          (item) =>
            html`<li>
              <span class="kind muted">${workItemKindLabel(item.kind)}</span>
              <span>${this.renderLink(item, item.title)}</span>
              <span
                >${
                  item.state
                    ? item.state === 'merged'
                      ? html`<sl-badge pill variant="success">merged</sl-badge>`
                      : html`<sl-badge pill variant="neutral"
                          >${item.state}</sl-badge
                        >`
                    : nothing
                }</span
              >
              <span class="num"
                >${formatWorkItemCost(item.estimated_cost)}</span
              >
            </li>`
        )}
      </ul>
      <div class="dimensions">
        ${this.renderSlices('Agents', group.agents, group.estimated_cost)}
        ${this.renderSlices('Flows', group.flows, group.estimated_cost)}
        ${this.renderSlices('Models', group.models, group.estimated_cost)}
        ${this.renderSlices('People', group.users, group.estimated_cost)}
      </div>
    </div>`;
  }

  private renderRow(group: WorkItemGroup): TemplateResult {
    const open = this.expanded.has(group.key);
    return html`<tr>
        <td>
          <button
            class="expand"
            aria-expanded=${open ? 'true' : 'false'}
            aria-label=${`${open ? 'Hide' : 'Show'} what drove ${group.key}`}
            @click=${() => this.toggle(group.key)}
          >
            <sl-icon name=${open ? 'chevron-down' : 'chevron-right'}></sl-icon>
          </button>
        </td>
        <td>${this.renderLink(group, group.title)}</td>
        <td class="muted">${groupMakeup(group)}</td>
        <td class="num">${formatCount(group.execution_count)}</td>
        <td class="num">${formatCount(group.session_count)}</td>
        <td class="num">${formatCount(group.total_tokens)}</td>
        <td
          class="num"
          title=${
            group.partial
              ? 'Some requests had no price; this is the priced subtotal.'
              : 'Estimated from published model rates.'
          }
        >
          ${formatWorkItemCost(group.estimated_cost)}${group.partial ? '*' : ''}
        </td>
      </tr>
      ${
        open
          ? html`<tr class="detail-row">
              <td></td>
              <td colspan="6">${this.renderDetail(group)}</td>
            </tr>`
          : nothing
      }`;
  }

  private renderTable(report: WorkItemCostReport): TemplateResult {
    if (!report.groups.length) {
      return html`<div class="empty">
        No flow run or agent session worked on an issue or pull request in this
        period.
      </div>`;
    }
    const last = this.offset + report.groups.length;
    return html`<div class="table-scroll">
        <table>
          <thead>
            <tr>
              <th><span class="sr-only">Expand</span></th>
              <th>Work item</th>
              <th>Includes</th>
              <th class="num">Flow runs</th>
              <th class="num">Sessions</th>
              <th class="num">Tokens</th>
              <th class="num">Cost</th>
            </tr>
          </thead>
          <tbody>
            ${report.groups.map((group) => this.renderRow(group))}
          </tbody>
        </table>
      </div>
      ${
        report.total_groups > WORK_ITEM_PAGE_SIZE
          ? html`<div class="pager">
              <span class="muted"
                >${this.offset + 1}–${last} of ${report.total_groups}</span
              >
              <sl-button
                size="small"
                ?disabled=${this.offset === 0}
                @click=${() => this.page(-1)}
                >Previous</sl-button
              >
              <sl-button
                size="small"
                ?disabled=${last >= report.total_groups}
                @click=${() => this.page(1)}
                >Next</sl-button
              >
            </div>`
          : nothing
      }`;
  }

  render() {
    const report = this.report;
    return html`<div class="page">
      ${
        this.embedded
          ? html`<p class="muted">
              What each issue and pull request cost, counting every flow run and
              agent session that worked on it.
              ${this.loading ? html`<sl-spinner></sl-spinner>` : nothing}
            </p>`
          : html`<view-header
                headerText="Cost per work item"
                description="What each issue and pull request cost, counting every flow run and agent session that worked on it."
              ></view-header>
              <div class="toolbar">
                <time-range-select
                  ariaLabel="Work item cost period"
                  .value=${this.range}
                  .options=${RANGE_OPTIONS}
                  @range-change=${this.onRange}
                ></time-range-select>
                ${this.loading ? html`<sl-spinner></sl-spinner>` : nothing}
                <span class="spacer"></span>
                <a href="/console/cost/spend">Spend</a>
                <a href="/console/cost/spend?tab=issues">Cost per issue</a>
              </div>`
      }
      ${
        this.error
          ? html`<sl-alert variant="danger" open role="alert"
              >${this.error}</sl-alert
            >`
          : nothing
      }
      ${
        report
          ? html`<sl-card>
              <div class="totals" slot="header">
                <div>
                  <div class="stat-value">
                    ${formatWorkItemCost(report.estimated_cost)}
                  </div>
                  <div class="stat-label">attributed to work items</div>
                </div>
                <div>
                  <div class="stat-value">
                    ${formatCount(report.total_groups)}
                  </div>
                  <div class="stat-label">work items</div>
                </div>
                <div>
                  <div class="stat-value">
                    ${formatCount(report.total_tokens)}
                  </div>
                  <div class="stat-label">tokens</div>
                </div>
              </div>
              ${this.renderTable(report)}
              <p class="muted">
                ${
                  report.groups.some((g) => g.partial)
                    ? '* Some requests had no price, so the amount is the priced subtotal. '
                    : ''
                }${report.split_rule}${
                  report.truncated
                    ? ' This period has more activity than one report reads; older contributors may be missing.'
                    : ''
                }
              </p>
            </sl-card>`
          : nothing
      }
    </div>`;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'work-item-cost-view': WorkItemCostView;
  }
}
