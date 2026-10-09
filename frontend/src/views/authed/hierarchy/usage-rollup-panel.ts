import { tableScrollStyles } from '../../../styles/table-scroll';
import { formatUsd, formatUsdExact } from '../../../utils/money';
import { LitElement, html, css, nothing, type PropertyValues } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/card/card.js';
import '@shoelace-style/shoelace/dist/components/option/option.js';
import '@shoelace-style/shoelace/dist/components/select/select.js';
import { isCapabilityOff } from '../../../capabilities';
import { resolveTimeRange } from '../../../utils/time-range';
import {
  currentAccountId,
  getAttentionRollup,
  getUsageRollup,
  type AttentionRollupRow,
  type UsageRollupRow,
} from '../../../hierarchy-api';

const ALL = '__all__';

/**
 * Usage by subaccount on the Usage page (capability `account_hierarchy`):
 * a subaccount column and filter over the rollup endpoint, "All subaccounts"
 * by default, and open attention per subaccount. Only parent admins get an
 * answer; for everyone else the panel stays empty.
 */
@customElement('usage-rollup-panel')
export class UsageRollupPanel extends LitElement {
  static styles = [
    tableScrollStyles,
    css`
      :host {
        display: block;
        margin-bottom: var(--sl-spacing-large);
      }
      table {
        width: 100%;
        border-collapse: collapse;
      }
      th,
      td {
        text-align: left;
        padding: var(--sl-spacing-2x-small) var(--sl-spacing-small);
        border-bottom: 1px solid var(--sl-color-neutral-200);
      }
      td.num,
      th.num {
        text-align: right;
      }
      .head {
        display: flex;
        justify-content: space-between;
        align-items: flex-end;
        gap: var(--sl-spacing-medium);
        margin-bottom: var(--sl-spacing-small);
      }
      h3 {
        margin: 0;
        font-size: var(--sl-font-size-medium);
      }
    `,
  ];

  /** `range`: the page's time range key, so both tables show one window. */
  @property({ attribute: false }) context: Record<string, unknown> = {};

  @state() private rows: UsageRollupRow[] = [];
  @state() private attention: AttentionRollupRow[] = [];
  @state() private filter = ALL;
  @state() private ready = false;

  private accountId = '';
  private generation = 0;
  /**
   * Every subaccount seen so far, so the filter keeps offering the others
   * after the server has narrowed the rows to one.
   */
  private known = new Map<string, string>();

  protected willUpdate(changed: PropertyValues) {
    if (changed.has('context')) void this.load();
  }

  private async load() {
    const generation = ++this.generation;
    try {
      this.accountId ||= await currentAccountId();
      const [usage, attention] = await Promise.all([
        getUsageRollup(this.accountId, {
          ...this.window(),
          subaccount_id: this.filter === ALL ? undefined : this.filter,
        }),
        getAttentionRollup(this.accountId).catch(() => []),
      ]);
      if (generation !== this.generation) return;
      this.rows = usage.rows;
      this.attention = attention;
      for (const row of [...usage.rows, ...attention]) {
        this.known.set(row.subaccount_id, row.subaccount_name);
      }
      this.ready = true;
    } catch (error) {
      if (generation !== this.generation) return;
      this.ready = false;
      if (isCapabilityOff(error)) {
        this.dispatchEvent(
          new CustomEvent('capability-off', { bubbles: true })
        );
      }
    }
  }

  private window(): { start?: string; end?: string } {
    const key = this.context.range;
    if (typeof key !== 'string' || !key) return {};
    const { startDate, endDate } = resolveTimeRange(key);
    return { start: startDate ?? undefined, end: endDate ?? undefined };
  }

  private subaccountOptions(): Array<[string, string]> {
    return [...this.known.entries()].sort((a, b) => a[1].localeCompare(b[1]));
  }

  private visibleRows(): UsageRollupRow[] {
    return this.filter === ALL
      ? this.rows
      : this.rows.filter((row) => row.subaccount_id === this.filter);
  }

  render() {
    if (!this.ready) return nothing;
    const rows = this.visibleRows();
    const total = rows.reduce((sum, row) => sum + (row.cost_usd || 0), 0);
    return html`<sl-card>
      <div class="head">
        <h3>By subaccount</h3>
        <sl-select
          size="small"
          label="Subaccount"
          data-testid="subaccount-filter"
          .value=${this.filter}
          @sl-change=${(e: Event) => {
            this.filter = (e.target as HTMLSelectElement).value || ALL;
            void this.load();
          }}
        >
          <sl-option value=${ALL}>All subaccounts</sl-option>
          ${this.subaccountOptions().map(
            ([id, name]) => html`<sl-option value=${id}>${name}</sl-option>`
          )}
        </sl-select>
      </div>
      <div class="table-scroll">
        <table data-testid="rollup-table">
          <thead>
            <tr>
              <th>Subaccount</th>
              <th>Model</th>
              <th>Day</th>
              <th class="num">Requests</th>
              <th class="num">Cost</th>
            </tr>
          </thead>
          <tbody>
            ${rows.map(
              (row) =>
                html`<tr data-subaccount=${row.subaccount_id}>
                  <td>${row.subaccount_name}</td>
                  <td>${row.model ?? ''}</td>
                  <td>${row.day ?? ''}</td>
                  <td class="num">${row.requests}</td>
                  <td class="num">
                    ${html`<span title=${formatUsdExact(row.cost_usd)}>${formatUsd(row.cost_usd)}</span>`}
                  </td>
                </tr>`
            )}
          </tbody>
          <tfoot>
            <tr>
              <th colspan="4">Total</th>
              <th class="num" data-testid="rollup-total">
                ${html`<span title=${formatUsdExact(total)}>${formatUsd(total)}</span>`}
              </th>
            </tr>
          </tfoot>
        </table>
      </div>
      ${
        this.attention.length
          ? html`<h3>Needs attention</h3>
              <div class="table-scroll">
                <table data-testid="attention-rollup">
                  <tbody>
                    ${this.attention
                      .filter(
                        (row) =>
                          this.filter === ALL ||
                          row.subaccount_id === this.filter
                      )
                      .map(
                        (row) =>
                          html`<tr>
                            <td>${row.subaccount_name}</td>
                            <td class="num">${row.count}</td>
                          </tr>`
                      )}
                  </tbody>
                </table>
              </div>`
          : nothing
      }
    </sl-card>`;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'usage-rollup-panel': UsageRollupPanel;
  }
}
