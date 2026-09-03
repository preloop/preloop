import { LitElement, css, html, nothing, unsafeCSS } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import { repeat } from 'lit/directives/repeat.js';

import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/card/card.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/option/option.js';
import '@shoelace-style/shoelace/dist/components/select/select.js';
import '@shoelace-style/shoelace/dist/components/skeleton/skeleton.js';
import '@shoelace-style/shoelace/dist/components/tab/tab.js';
import '@shoelace-style/shoelace/dist/components/tab-group/tab-group.js';
import '@shoelace-style/shoelace/dist/components/tooltip/tooltip.js';

import consoleStyles from '../styles/console-styles.css?inline';
import { parseUTCDate, formatRelativeTime } from '../utils/date';
import { executionDurationText } from '../utils/execution';
import {
  executionSubjectCss,
  renderExecutionSubject,
  type ExecutionSubjectSource,
} from '../utils/execution-subject';
import { renderAgentIcon } from '../utils/agent-icons';
import type { AgentStatusChip } from '../utils/agent-display';

/** Which inventory the box is showing. */
export type InventoryTab = 'agents' | 'flows' | 'models' | 'tools';

export const INVENTORY_TAB_STORAGE_KEY = 'overview_inventory_tab';

/** The last run of a flow, as the executions list returns it. */
export interface InventoryFlowRun extends ExecutionSubjectSource {
  id: string;
  status: string;
  start_time: string;
  end_time?: string | null;
}

export interface InventoryAgentRow {
  id: string;
  name: string;
  /** Agent kind, for the row icon (`claude_code`, `hermes`, ...). */
  kind: string | null;
  status: AgentStatusChip;
  modelAlias: string | null;
  requests: number;
  tokens: number;
  cost: number;
  lastSeenAt: string | null;
}

export interface InventoryFlowRow {
  id: string;
  name: string;
  lastRun: InventoryFlowRun | null;
  runs: number;
  failed: number;
  cost: number;
}

export interface InventoryModelRow {
  id: string | null;
  alias: string;
  provider: string;
  requests: number;
  tokens: number;
  cost: number;
  failed: number;
  /** Why the failure count is a sample, when it is one. */
  failedNote?: string;
}

export interface InventoryToolRow {
  name: string;
  server: string;
  calls: number;
  failed: number;
}

interface SortOption {
  value: string;
  label: string;
}

/** Sort menus, per tab. The first entry is the default for that tab. */
const SORT_OPTIONS: Record<InventoryTab, SortOption[]> = {
  agents: [
    { value: 'last-active', label: 'Last active' },
    { value: 'requests', label: 'Requests' },
    { value: 'spend', label: 'Spend' },
  ],
  flows: [
    { value: 'last-run', label: 'Last run' },
    { value: 'runs', label: 'Runs' },
    { value: 'spend', label: 'Spend' },
    { value: 'failures', label: 'Failures' },
  ],
  models: [
    { value: 'spend', label: 'Spend' },
    { value: 'requests', label: 'Requests' },
    { value: 'failures', label: 'Failures' },
  ],
  tools: [
    { value: 'calls', label: 'Calls' },
    { value: 'failures', label: 'Failures' },
  ],
};

const TAB_ORDER: InventoryTab[] = ['agents', 'flows', 'models', 'tools'];

const TAB_LABELS: Record<InventoryTab, string> = {
  agents: 'Agents',
  flows: 'Flows',
  models: 'Models',
  tools: 'Tools',
};

/**
 * What a tab says when it has nothing, and the one thing to do about it.
 * A tab with no rows is not an error, so it gets a line, not a panel.
 */
const EMPTY_STATES: Record<
  InventoryTab,
  { text: string; href?: string; linkText?: string }
> = {
  agents: {
    text: 'No agents yet.',
    href: '/console/agents',
    linkText: 'Onboard an agent',
  },
  flows: {
    text: 'No flows yet.',
    href: '/console/flows/new',
    linkText: 'Create a flow',
  },
  models: {
    text: 'No models yet.',
    href: '/console/ai-models',
    linkText: 'Add a model',
  },
  tools: {
    text: 'No tools yet.',
    href: '/console/tools',
    linkText: 'Connect a server',
  },
};

const ALL_LINKS: Record<InventoryTab, { href: string; noun: string }> = {
  agents: { href: '/console/agents', noun: 'agents' },
  flows: { href: '/console/flows', noun: 'flows' },
  models: { href: '/console/ai-models', noun: 'models' },
  tools: { href: '/console/tools', noun: 'tools' },
};

/** A taller window shows two more rows; nothing else changes. */
const TALL_VIEWPORT = 1000;
const ROWS_DEFAULT = 6;
const ROWS_TALL = 8;

function timeValue(value: string | null | undefined): number {
  if (!value) return 0;
  const parsed = parseUTCDate(value).getTime();
  return Number.isNaN(parsed) ? 0 : parsed;
}

/**
 * What the account owns, in one box.
 *
 * Four counts used to sit above the page as a stat strip and three cards
 * below it listed the same things in three different shapes. The counts are
 * now the tab labels, and one table shape serves all four inventories: the
 * question "what do I have, and which of it is busy" is answered in one
 * place, at one depth. The range comes from the Usage card, because a page
 * with two time ranges has two truths.
 */
@customElement('inventory-card')
export class InventoryCard extends LitElement {
  @property({ type: Array }) agentRows: InventoryAgentRow[] = [];
  @property({ type: Array }) flowRows: InventoryFlowRow[] = [];
  @property({ type: Array }) modelRows: InventoryModelRow[] = [];
  @property({ type: Array }) toolRows: InventoryToolRow[] = [];

  /** Totals for the tab labels and the footer link: everything, not the page. */
  @property({ type: Number }) agentsTotal = 0;
  @property({ type: Number }) flowsTotal = 0;
  @property({ type: Number }) modelsTotal = 0;
  @property({ type: Number }) toolsTotal = 0;

  @property({ type: Boolean }) loading = false;
  /** The page range, already worded ("30d"), shown but not editable here. */
  @property({ type: String }) rangeLabel = '30d';

  @state() private tab: InventoryTab = 'agents';
  @state() private sorts: Record<InventoryTab, string> = {
    agents: SORT_OPTIONS.agents[0].value,
    flows: SORT_OPTIONS.flows[0].value,
    models: SORT_OPTIONS.models[0].value,
    tools: SORT_OPTIONS.tools[0].value,
  };
  @state() private rowLimit = ROWS_DEFAULT;

  private onResize = () => {
    const limit = window.innerHeight > TALL_VIEWPORT ? ROWS_TALL : ROWS_DEFAULT;
    if (limit !== this.rowLimit) {
      this.rowLimit = limit;
    }
  };

  static styles = [
    unsafeCSS(consoleStyles),
    unsafeCSS(executionSubjectCss),
    css`
      :host {
        display: block;
        width: 100%;
      }

      .content-card,
      .content-card::part(base) {
        width: 100%;
      }

      .content-card::part(body) {
        padding: 0;
      }

      .header {
        align-items: center;
        display: flex;
        gap: var(--sl-spacing-small);
        justify-content: space-between;
      }

      .title {
        font-size: var(--console-text-card-title);
        font-weight: 600;
      }

      .header-controls {
        align-items: center;
        display: flex;
        gap: var(--sl-spacing-small);
      }

      /* The sort is read next to the column it orders, so its label is for
         screen readers only. */
      .sort-select::part(form-control-label) {
        clip: rect(0 0 0 0);
        height: 1px;
        overflow: hidden;
        position: absolute;
        white-space: nowrap;
        width: 1px;
      }

      .sort-select {
        width: 9.5rem;
      }

      .sort-select::part(combobox) {
        font-size: var(--console-text-meta);
        min-height: 2rem;
        padding-block: 0;
      }

      /* The range is a fact about the numbers below, not a control: the one
         control lives on the Usage card. */
      .range-label {
        color: var(--console-meta-color);
        font-size: var(--console-text-meta);
        font-variant-numeric: tabular-nums;
        white-space: nowrap;
      }

      .tabs {
        border-bottom: 1px solid var(--console-hairline);
      }

      sl-tab-group::part(nav) {
        padding: 0 var(--sl-spacing-medium);
      }

      sl-tab-group::part(tabs) {
        border-bottom: none;
      }

      sl-tab::part(base) {
        font-size: var(--console-text-body);
        padding: var(--sl-spacing-small) var(--sl-spacing-medium);
      }

      .tab-count {
        color: var(--console-meta-color);
        font-variant-numeric: tabular-nums;
        margin-left: 6px;
      }

      sl-tab[active] .tab-count {
        color: inherit;
      }

      table.inventory-table {
        border-collapse: collapse;
        font-size: var(--console-text-body);
        table-layout: fixed;
        width: 100%;
      }

      .inventory-table th,
      .inventory-table td {
        border-bottom: 1px solid var(--console-hairline);
        overflow: hidden;
        padding: var(--sl-spacing-x-small) var(--sl-spacing-medium);
        text-align: left;
        text-overflow: ellipsis;
        white-space: nowrap;
      }

      .inventory-table th {
        color: var(--console-meta-color);
        font-size: var(--console-text-meta);
        font-weight: 600;
      }

      .inventory-table tbody tr:last-child td {
        border-bottom: none;
      }

      .inventory-table tbody tr:hover {
        background-color: var(--console-hover-tint);
      }

      .inventory-table th.num,
      .inventory-table td.num {
        font-variant-numeric: tabular-nums;
        text-align: right;
      }

      .identity {
        align-items: center;
        display: flex;
        gap: var(--sl-spacing-x-small);
        min-width: 0;
      }

      .identity sl-icon {
        color: var(--console-meta-color);
        flex-shrink: 0;
        font-size: 15px;
      }

      a.row-name {
        color: var(--console-link-color);
        overflow: hidden;
        text-decoration: none;
        text-overflow: ellipsis;
        white-space: nowrap;
      }

      a.row-name:hover,
      a.row-name:focus-visible {
        text-decoration: underline;
      }

      .muted {
        color: var(--console-meta-color);
      }

      .last-run {
        align-items: center;
        display: flex;
        gap: var(--sl-spacing-x-small);
        min-width: 0;
      }

      .last-run .meta {
        color: var(--console-meta-color);
        font-size: var(--console-text-meta);
        white-space: nowrap;
      }

      .skeleton-row td {
        padding: var(--sl-spacing-small) var(--sl-spacing-medium);
      }

      sl-skeleton {
        --border-radius: var(--sl-border-radius-small);
        height: 0.75rem;
      }

      .empty {
        color: var(--console-meta-color);
        padding: var(--sl-spacing-large) var(--sl-spacing-medium);
        text-align: center;
      }

      .empty a {
        color: var(--console-link-color);
        text-decoration: none;
      }

      .empty a:hover {
        text-decoration: underline;
      }

      .footer {
        border-top: 1px solid var(--console-hairline);
        padding: var(--sl-spacing-small) var(--sl-spacing-medium);
      }

      .footer a {
        color: var(--console-link-color);
        font-size: var(--console-text-meta);
        text-decoration: none;
      }

      .footer a:hover {
        text-decoration: underline;
      }

      sl-badge.chip::part(base) {
        font-size: 0.6875rem;
      }

      /* Phones read a row as two lines: who it is, then its numbers. The
         header row has nothing to align to once the cells stack, so it goes. */
      @media (max-width: 640px) {
        .inventory-table {
          table-layout: auto;
        }

        .inventory-table thead {
          display: none;
        }

        .inventory-table tr {
          display: flex;
          flex-wrap: wrap;
          gap: 0 var(--sl-spacing-small);
          padding: var(--sl-spacing-x-small) var(--sl-spacing-medium);
        }

        .inventory-table td {
          border-bottom: none;
          padding: 0;
        }

        .inventory-table tbody tr {
          border-bottom: 1px solid var(--console-hairline);
        }

        .inventory-table td.identity-cell {
          flex: 0 0 100%;
        }

        .inventory-table td.num,
        .inventory-table td.secondary {
          color: var(--console-meta-color);
          font-size: var(--console-text-meta);
          text-align: left;
        }
      }
    `,
  ];

  connectedCallback(): void {
    super.connectedCallback();
    try {
      const stored = localStorage.getItem(INVENTORY_TAB_STORAGE_KEY);
      if (stored && (TAB_ORDER as string[]).includes(stored)) {
        this.tab = stored as InventoryTab;
      }
    } catch {
      // Private mode or a blocked storage partition: keep Agents.
    }
    this.onResize();
    window.addEventListener('resize', this.onResize);
  }

  disconnectedCallback(): void {
    window.removeEventListener('resize', this.onResize);
    super.disconnectedCallback();
  }

  private countFor(tab: InventoryTab): number {
    if (tab === 'agents') return this.agentsTotal;
    if (tab === 'flows') return this.flowsTotal;
    if (tab === 'models') return this.modelsTotal;
    return this.toolsTotal;
  }

  private selectTab(tab: InventoryTab): void {
    if (tab === this.tab) return;
    this.tab = tab;
    try {
      localStorage.setItem(INVENTORY_TAB_STORAGE_KEY, tab);
    } catch {
      // Non-fatal: the tab still switches for this session.
    }
  }

  private setSort(value: string): void {
    this.sorts = { ...this.sorts, [this.tab]: value };
  }

  private formatCompactNumber(value: number | null | undefined): string {
    const amount = Number(value || 0);
    if (amount < 1000) return String(Math.round(amount));
    return new Intl.NumberFormat(undefined, {
      notation: 'compact',
      maximumFractionDigits: 1,
    }).format(amount);
  }

  private formatCurrency(value: number | null | undefined): string {
    const amount = Number(value || 0);
    if (amount > 0 && amount < 0.01) return `$${amount.toFixed(4)}`;
    return `$${amount.toFixed(2)}`;
  }

  private absolute(value: string | null | undefined): string {
    if (!value) return 'Never';
    const date = parseUTCDate(value);
    return Number.isNaN(date.getTime()) ? 'Never' : date.toLocaleString();
  }

  private relative(value: string | null | undefined): string {
    if (!value) return 'Never';
    return formatRelativeTime(value);
  }

  /** Green for a finished run, red for a failed one, neutral while it runs. */
  private statusVariant(status: string): 'success' | 'danger' | 'neutral' {
    const normalized = (status || '').toLowerCase();
    if (['succeeded', 'completed', 'success'].includes(normalized)) {
      return 'success';
    }
    if (['failed', 'error', 'timeout'].includes(normalized)) {
      return 'danger';
    }
    return 'neutral';
  }

  private get sortedAgents(): InventoryAgentRow[] {
    const rows = [...this.agentRows];
    const sort = this.sorts.agents;
    if (sort === 'requests') {
      rows.sort((a, b) => b.requests - a.requests);
    } else if (sort === 'spend') {
      rows.sort((a, b) => b.cost - a.cost);
    } else {
      rows.sort((a, b) => timeValue(b.lastSeenAt) - timeValue(a.lastSeenAt));
    }
    return rows;
  }

  private get sortedFlows(): InventoryFlowRow[] {
    const rows = [...this.flowRows];
    const sort = this.sorts.flows;
    if (sort === 'runs') {
      rows.sort((a, b) => b.runs - a.runs);
    } else if (sort === 'spend') {
      rows.sort((a, b) => b.cost - a.cost);
    } else if (sort === 'failures') {
      rows.sort((a, b) => b.failed - a.failed);
    } else {
      rows.sort(
        (a, b) =>
          timeValue(b.lastRun?.start_time) - timeValue(a.lastRun?.start_time)
      );
    }
    return rows;
  }

  private get sortedModels(): InventoryModelRow[] {
    const rows = [...this.modelRows];
    const sort = this.sorts.models;
    if (sort === 'requests') {
      rows.sort((a, b) => b.requests - a.requests);
    } else if (sort === 'failures') {
      rows.sort((a, b) => b.failed - a.failed);
    } else {
      rows.sort((a, b) => b.cost - a.cost);
    }
    return rows;
  }

  private get sortedTools(): InventoryToolRow[] {
    const rows = [...this.toolRows];
    const sort = this.sorts.tools;
    if (sort === 'failures') {
      rows.sort((a, b) => b.failed - a.failed);
    } else {
      rows.sort((a, b) => b.calls - a.calls);
    }
    return rows;
  }

  private renderHeader() {
    const options = SORT_OPTIONS[this.tab];
    return html`
      <div slot="header" class="header">
        <span class="title">Inventory</span>
        <div class="header-controls">
          <sl-select
            class="sort-select"
            label="Sort ${TAB_LABELS[this.tab].toLowerCase()} by"
            size="small"
            hoist
            value=${this.sorts[this.tab]}
            @sl-change=${(event: Event) => {
              const select = event.target as HTMLElement & { value: string };
              this.setSort(select.value);
            }}
          >
            ${options.map(
              (option) =>
                html`<sl-option value=${option.value}
                  >${option.label}</sl-option
                >`
            )}
          </sl-select>
          <span class="range-label" title="Range set on the Usage card"
            >${this.rangeLabel}</span
          >
        </div>
      </div>
    `;
  }

  private renderTabs() {
    return html`
      <div class="tabs">
        <sl-tab-group
          @sl-tab-show=${(event: CustomEvent<{ name: string }>) => {
            const name = event.detail.name as InventoryTab;
            if ((TAB_ORDER as string[]).includes(name)) {
              this.selectTab(name);
            }
          }}
        >
          ${TAB_ORDER.map(
            (tab) => html`
              <sl-tab slot="nav" panel=${tab} ?active=${this.tab === tab}>
                ${TAB_LABELS[tab]}
                <span class="tab-count">${this.countFor(tab)}</span>
              </sl-tab>
            `
          )}
        </sl-tab-group>
      </div>
    `;
  }

  private renderSkeleton(columns: number) {
    return html`
      <tbody>
        ${Array.from(
          { length: this.rowLimit },
          () => html`
            <tr class="skeleton-row">
              ${Array.from(
                { length: columns },
                () => html`<td><sl-skeleton effect="none"></sl-skeleton></td>`
              )}
            </tr>
          `
        )}
      </tbody>
    `;
  }

  private renderEmpty() {
    const empty = EMPTY_STATES[this.tab];
    return html`
      <div class="empty">
        ${empty.text}
        ${
          empty.href
            ? html` <a href=${empty.href}>${empty.linkText}</a>`
            : nothing
        }
      </div>
    `;
  }

  private renderAgents() {
    const rows = this.sortedAgents.slice(0, this.rowLimit);
    return html`
      <table class="inventory-table">
        <colgroup>
          <col style="width: 30%" />
          <col style="width: 22%" />
          <col style="width: 12%" />
          <col style="width: 12%" />
          <col style="width: 12%" />
          <col style="width: 12%" />
        </colgroup>
        <thead>
          <tr>
            <th>Agent</th>
            <th>Model</th>
            <th class="num">Requests</th>
            <th class="num">Tokens</th>
            <th class="num">$ est.</th>
            <th class="num">Last seen</th>
          </tr>
        </thead>
        ${
          this.loading && rows.length === 0
            ? this.renderSkeleton(6)
            : html`
                <tbody>
                  ${repeat(
                    rows,
                    (row) => row.id,
                    (row) => html`
                      <tr>
                        <td class="identity-cell">
                          <span class="identity">
                            ${renderAgentIcon(row.kind)}
                            <a class="row-name" href="/console/agents/${row.id}"
                              >${row.name}</a
                            >
                            <sl-badge
                              class="chip"
                              pill
                              variant=${row.status.variant}
                              >${row.status.label}</sl-badge
                            >
                          </span>
                        </td>
                        <td
                          class="secondary muted"
                          title=${row.modelAlias || ''}
                        >
                          ${row.modelAlias || 'No model'}
                        </td>
                        <td class="num">
                          ${this.formatCompactNumber(row.requests)}
                        </td>
                        <td class="num">
                          ${this.formatCompactNumber(row.tokens)}
                        </td>
                        <td class="num">${this.formatCurrency(row.cost)}</td>
                        <td class="num" title=${this.absolute(row.lastSeenAt)}>
                          ${this.relative(row.lastSeenAt)}
                        </td>
                      </tr>
                    `
                  )}
                </tbody>
              `
        }
      </table>
    `;
  }

  private renderLastRun(run: InventoryFlowRun | null) {
    if (!run) {
      return html`<span class="muted">No run in range</span>`;
    }
    const duration = executionDurationText(run);
    return html`
      <span class="last-run">
        <sl-badge class="chip" pill variant=${this.statusVariant(run.status)}
          >${run.status}</sl-badge
        >
        ${renderExecutionSubject(run)}
        <span class="meta" title=${this.absolute(run.start_time)}
          >${this.relative(run.start_time)}${
            duration ? html` · ${duration}` : nothing
          }</span
        >
      </span>
    `;
  }

  private renderFlows() {
    const rows = this.sortedFlows.slice(0, this.rowLimit);
    return html`
      <table class="inventory-table">
        <colgroup>
          <col style="width: 24%" />
          <col style="width: 40%" />
          <col style="width: 12%" />
          <col style="width: 12%" />
          <col style="width: 12%" />
        </colgroup>
        <thead>
          <tr>
            <th>Flow</th>
            <th>Last run</th>
            <th class="num">Runs</th>
            <th class="num">Failed</th>
            <th class="num">$ est.</th>
          </tr>
        </thead>
        ${
          this.loading && rows.length === 0
            ? this.renderSkeleton(5)
            : html`
                <tbody>
                  ${repeat(
                    rows,
                    (row) => row.id,
                    (row) => html`
                      <tr>
                        <td class="identity-cell">
                          <span class="identity">
                            <a class="row-name" href="/console/flows/${row.id}"
                              >${row.name}</a
                            >
                          </span>
                        </td>
                        <td class="secondary">
                          ${this.renderLastRun(row.lastRun)}
                        </td>
                        <td class="num">
                          <a
                            class="row-name"
                            href="/console/flows/executions?flow_id=${row.id}"
                            >${this.formatCompactNumber(row.runs)}</a
                          >
                        </td>
                        <td class="num">
                          ${this.formatCompactNumber(row.failed)}
                        </td>
                        <td class="num">${this.formatCurrency(row.cost)}</td>
                      </tr>
                    `
                  )}
                </tbody>
              `
        }
      </table>
    `;
  }

  private renderModels() {
    const rows = this.sortedModels.slice(0, this.rowLimit);
    return html`
      <table class="inventory-table">
        <colgroup>
          <col style="width: 30%" />
          <col style="width: 20%" />
          <col style="width: 12%" />
          <col style="width: 14%" />
          <col style="width: 12%" />
          <col style="width: 12%" />
        </colgroup>
        <thead>
          <tr>
            <th>Model</th>
            <th>Provider</th>
            <th class="num">Requests</th>
            <th class="num">Tokens</th>
            <th class="num">$ est.</th>
            <th class="num">Failed</th>
          </tr>
        </thead>
        ${
          this.loading && rows.length === 0
            ? this.renderSkeleton(6)
            : html`
                <tbody>
                  ${repeat(
                    rows,
                    (row) => `${row.id || row.alias}`,
                    (row) => html`
                      <tr>
                        <td class="identity-cell">
                          <span class="identity">
                            ${
                              row.id
                                ? html`<a
                                    class="row-name"
                                    href="/console/ai-models/${row.id}"
                                    >${row.alias}</a
                                  >`
                                : html`<span class="row-name"
                                    >${row.alias}</span
                                  >`
                            }
                          </span>
                        </td>
                        <td class="secondary muted">
                          ${row.provider || 'Unknown'}
                        </td>
                        <td class="num">
                          ${this.formatCompactNumber(row.requests)}
                        </td>
                        <td class="num">
                          ${this.formatCompactNumber(row.tokens)}
                        </td>
                        <td class="num">${this.formatCurrency(row.cost)}</td>
                        <td class="num" title=${row.failedNote || ''}>
                          ${this.formatCompactNumber(row.failed)}
                        </td>
                      </tr>
                    `
                  )}
                </tbody>
              `
        }
      </table>
    `;
  }

  private renderTools() {
    const rows = this.sortedTools.slice(0, this.rowLimit);
    return html`
      <table class="inventory-table">
        <colgroup>
          <col style="width: 38%" />
          <col style="width: 30%" />
          <col style="width: 16%" />
          <col style="width: 16%" />
        </colgroup>
        <thead>
          <tr>
            <th>Tool</th>
            <th>Server</th>
            <th class="num">Calls</th>
            <th class="num">Failed</th>
          </tr>
        </thead>
        ${
          this.loading && rows.length === 0
            ? this.renderSkeleton(4)
            : html`
                <tbody>
                  ${repeat(
                    rows,
                    (row) => `${row.server}:${row.name}`,
                    (row) => html`
                      <tr>
                        <td class="identity-cell">
                          <span class="identity">
                            <a
                              class="row-name"
                              href="/console/tools#tool=${encodeURIComponent(
                                row.name
                              )}"
                              >${row.name}</a
                            >
                          </span>
                        </td>
                        <td class="secondary muted">
                          ${row.server || 'Unknown'}
                        </td>
                        <td class="num">
                          ${this.formatCompactNumber(row.calls)}
                        </td>
                        <td class="num">
                          ${this.formatCompactNumber(row.failed)}
                        </td>
                      </tr>
                    `
                  )}
                </tbody>
              `
        }
      </table>
    `;
  }

  private currentRowCount(): number {
    if (this.tab === 'agents') return this.agentRows.length;
    if (this.tab === 'flows') return this.flowRows.length;
    if (this.tab === 'models') return this.modelRows.length;
    return this.toolRows.length;
  }

  private renderBody() {
    if (!this.loading && this.currentRowCount() === 0) {
      return this.renderEmpty();
    }
    if (this.tab === 'agents') return this.renderAgents();
    if (this.tab === 'flows') return this.renderFlows();
    if (this.tab === 'models') return this.renderModels();
    return this.renderTools();
  }

  private renderFooter() {
    const link = ALL_LINKS[this.tab];
    const count = this.countFor(this.tab);
    if (count === 0) {
      return nothing;
    }
    return html`
      <div class="footer">
        <a href=${link.href}>View all ${count} ${link.noun} →</a>
      </div>
    `;
  }

  render() {
    return html`
      <sl-card class="content-card">
        ${this.renderHeader()} ${this.renderTabs()} ${this.renderBody()}
        ${this.renderFooter()}
      </sl-card>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'inventory-card': InventoryCard;
  }
}
