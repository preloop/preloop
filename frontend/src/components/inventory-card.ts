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
import './user-avatar';

import consoleStyles from '../styles/console-styles.css?inline';
import { parseUTCDate, formatRelativeTime } from '../utils/date';
import { executionDurationText } from '../utils/execution';
import {
  executionSubjectCss,
  renderExecutionSubject,
  type ExecutionSubjectSource,
} from '../utils/execution-subject';
import { renderFailureCategoryChip } from '../utils/failure-category';
import { renderAgentIcon } from '../utils/agent-icons';
import type { AgentStatusChip } from '../utils/agent-display';
import type { GatewayTokenUsage } from '../types';
import './token-figures';

/** Which inventory the box is showing. */
export type InventoryTab = 'agents' | 'flows' | 'models' | 'tools' | 'users';

export const INVENTORY_TAB_STORAGE_KEY = 'overview_inventory_tab';

/** The last run of a flow, as the executions list returns it. */
export interface InventoryFlowRun extends ExecutionSubjectSource {
  id: string;
  status: string;
  start_time: string;
  end_time?: string | null;
  /** Which layer broke the run (#361); absent unless the run failed. */
  failure_category?: string | null;
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
  /** The in/out/cache split behind `tokens`, or null when unmeasured. */
  tokenUsage?: GatewayTokenUsage | null;
  cost: number;
  lastSeenAt: string | null;
}

export interface InventoryFlowRow {
  id: string;
  name: string;
  lastRun: InventoryFlowRun | null;
  runs: number;
  failed: number;
  tokenUsage?: GatewayTokenUsage | null;
  cost: number;
}

export interface InventoryModelRow {
  id: string | null;
  alias: string;
  provider: string;
  requests: number;
  tokens: number;
  tokenUsage?: GatewayTokenUsage | null;
  cost: number;
}

/*
 * Why the Models tab has no "failed" column.
 *
 * The spec asked for one. When this card was written nothing the console
 * could call counted failures per model, so the column would have printed a
 * sample of one page as if it were the month. The batch models overview does
 * count them now, but this card is the spend inventory: five columns of
 * counts, sorted by requests or cost. Failures are told where they are the
 * subject, per model on the Models page and per account on the Gateway card
 * and the failures card under it.
 */

/**
 * The model alias without the provider it is already sitting next to.
 *
 * Gateway aliases are conventionally `provider/model`, and the table prints
 * the provider in the very next column, so the prefix costs half the width
 * of the name it qualifies: at 1440 the cell read "deepseek/deepse...".
 * Only an exact provider match is dropped, and only on a separator, so
 * "anthropic/claude-sonnet-4" served by OpenRouter keeps its prefix.
 */
export function modelAliasLabel(alias: string, provider: string): string {
  const name = (alias || '').trim();
  const match = name.match(/^([^/:]+)[/:](.+)$/);
  if (!match) return name;
  const key = (value: string) => value.toLowerCase().replace(/[^a-z0-9]/g, '');
  const prefix = key(match[1]);
  if (!prefix || prefix !== key(provider || '')) return name;
  return match[2];
}

/**
 * One teammate, with what they own and what they spent in the range.
 *
 * There is no "flows owned" column. Flows carry no owner in the data model,
 * so the number would be a zero that looks like a fact. Agents do carry one.
 */
export interface InventoryUserRow {
  id: string;
  name: string;
  /** Role names, already joined; empty when the account has no roles set. */
  role: string;
  lastLoginAt: string | null;
  agentsOwned: number;
  tokens: number;
  tokenUsage?: GatewayTokenUsage | null;
  cost: number;
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
  ],
  tools: [
    { value: 'calls', label: 'Calls' },
    { value: 'failures', label: 'Failures' },
  ],
  users: [
    { value: 'last-login', label: 'Last login' },
    { value: 'spend', label: 'Spend' },
  ],
};

const TAB_ORDER: InventoryTab[] = [
  'agents',
  'flows',
  'models',
  'tools',
  'users',
];

const TAB_LABELS: Record<InventoryTab, string> = {
  agents: 'Agents',
  flows: 'Flows',
  models: 'Models',
  tools: 'Tools',
  users: 'Users',
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
  users: {
    text: 'No teammates yet.',
    href: '/console/settings/invitations',
    linkText: 'Invite a teammate',
  },
};

const ALL_LINKS: Record<InventoryTab, { href: string; noun: string }> = {
  agents: { href: '/console/agents', noun: 'agents' },
  flows: { href: '/console/flows', noun: 'flows' },
  models: { href: '/console/ai-models', noun: 'models' },
  tools: { href: '/console/tools', noun: 'tools' },
  users: { href: '/console/settings/users', noun: 'users' },
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
  @property({ type: Array }) userRows: InventoryUserRow[] = [];

  /** Totals for the tab labels and the footer link: everything, not the page. */
  @property({ type: Number }) agentsTotal = 0;
  @property({ type: Number }) flowsTotal = 0;
  @property({ type: Number }) modelsTotal = 0;
  @property({ type: Number }) toolsTotal = 0;
  @property({ type: Number }) usersTotal = 0;

  /**
   * Off on OSS, where there is one account and no user management: a tab
   * listing one person, always, is furniture.
   */
  @property({ type: Boolean }) showUsers = false;

  @property({ type: Boolean }) loading = false;
  /**
   * Per-tab overrides for {@link loading}.
   *
   * The tabs are filled by different requests that land at different times:
   * agents arrive with the first wave, flows and their runs a moment later,
   * models and tools last. One flag for all five meant the whole card stayed
   * a skeleton until the slowest of them answered, hiding rows it already
   * had. `null` means "no answer yet, use the card-wide flag", so a host that
   * sets nothing behaves exactly as before.
   */
  @property({ type: Boolean }) loadingAgents: boolean | null = null;
  @property({ type: Boolean }) loadingFlows: boolean | null = null;
  @property({ type: Boolean }) loadingModels: boolean | null = null;
  @property({ type: Boolean }) loadingTools: boolean | null = null;
  @property({ type: Boolean }) loadingUsers: boolean | null = null;
  /**
   * The usage breakdown is a second, slower request than the lists it fills.
   * While it is in flight the identity of a row (who, which role, when they
   * were last here, how many agents they own) is already known and is shown;
   * only the columns that come from the breakdown wait, as skeleton cells.
   *
   * A zero standing in for "not fetched yet" is the thing this avoids: an
   * account reading "$0.00" for every teammate for four seconds is a wrong
   * answer, not a slow one.
   */
  @property({ type: Boolean }) usageLoading = false;
  /**
   * Per-tab overrides for {@link usageLoading}. The tabs do not share one
   * usage request: Agents / Users / Tools read the account breakdown, Flows
   * read executions plus `usage_by_flow`, Models read `ai-models/overview`.
   * `null` means "use the shared flag", so a host that sets nothing behaves
   * as it did when every usage column waited on the same breakdown.
   */
  @property({ type: Boolean }) usageLoadingFlows: boolean | null = null;
  @property({ type: Boolean }) usageLoadingFlowCost: boolean | null = null;
  @property({ type: Boolean }) usageLoadingModels: boolean | null = null;
  @property({ type: Boolean }) usageLoadingTools: boolean | null = null;
  /** The page range, already worded ("30d"), shown but not editable here. */
  @property({ type: String }) rangeLabel = '30d';
  /**
   * True when the run counts came off a full page of the most recent 100
   * executions whose oldest row is still inside the range: they are then a
   * floor, not the range's total, and every cell that shows one says so.
   */
  @property({ type: Boolean }) flowRunsCapped = false;

  @state() private tab: InventoryTab = 'agents';
  @state() private sorts: Record<InventoryTab, string> = {
    agents: SORT_OPTIONS.agents[0].value,
    flows: SORT_OPTIONS.flows[0].value,
    models: SORT_OPTIONS.models[0].value,
    tools: SORT_OPTIONS.tools[0].value,
    users: SORT_OPTIONS.users[0].value,
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

      /* Not ".header": the console sheet gives that class a page-header
         min-height and bottom margin, which pads a card header by 40px. */
      .card-head {
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

      /* Numbers pay less for their gutters than words do: the columns are
         narrow, and "Requests" has to fit its own heading. */
      .inventory-table th.num,
      .inventory-table td.num {
        font-variant-numeric: tabular-nums;
        padding-inline: var(--sl-spacing-x-small);
        text-align: right;
      }

      .inventory-table th.num:last-child,
      .inventory-table td.num:last-child {
        padding-right: var(--sl-spacing-medium);
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

      /* The subject is the only part of a run worth reading twice, so it is
         the only part allowed to grow, and the chip and the clock never
         steal from it. */
      .last-run sl-badge,
      .last-run sl-tooltip,
      .last-run .meta {
        flex-shrink: 0;
      }

      .last-run .execution-subject {
        flex: 1 1 auto;
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

      /* The width of the number it stands in for, right where the number
         will be, so nothing moves sideways when the usage arrives. */
      .usage-skeleton {
        display: inline-block;
        vertical-align: middle;
        width: 40px;
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

      .footer-note {
        color: var(--console-meta-color);
        font-size: var(--console-text-meta);
        margin-right: var(--sl-spacing-2x-small);
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
         table stops being a table (a row that is still a table row keeps the
         column widths and squeezes the name into a sliver), the header row
         has nothing to align to once the cells stack, so it goes, and each
         number carries the word its column heading used to supply. */
      @media (max-width: 640px) {
        .inventory-table,
        .inventory-table tbody {
          display: block;
          width: 100%;
        }

        .inventory-table thead {
          display: none;
        }

        .inventory-table tr {
          display: flex;
          flex-wrap: wrap;
          gap: 2px var(--sl-spacing-small);
          padding: var(--sl-spacing-x-small) var(--sl-spacing-medium);
        }

        .inventory-table td {
          border-bottom: none;
          display: block;
          padding: 0;
        }

        .inventory-table tbody tr {
          border-bottom: 1px solid var(--console-hairline);
        }

        .inventory-table tbody tr:last-child {
          border-bottom: none;
        }

        .inventory-table td.identity-cell,
        .inventory-table td.wide-cell {
          flex: 0 0 100%;
          min-width: 0;
        }

        .inventory-table td.num,
        .inventory-table td.secondary {
          color: var(--console-meta-color);
          font-size: var(--console-text-meta);
          padding-inline: 0;
          text-align: left;
        }

        /* One alias, one line: the second line is a summary, not a URL. */
        .inventory-table td.secondary:not(.wide-cell) {
          max-width: 55%;
          overflow: hidden;
          text-overflow: ellipsis;
          white-space: nowrap;
        }

        .inventory-table td[data-label]::before {
          content: attr(data-label) ' ';
        }

        /* The second line is one sentence of numbers, so it is punctuated. */
        .inventory-table td.num:not(:last-child)::after,
        .inventory-table td.secondary:not(.wide-cell)::after {
          color: var(--console-meta-color);
          content: ' ·';
        }

        /* Tokens are the one number a phone can do without: the request
           count and the spend already say how much this row is doing. */
        .inventory-table td[data-label='Tokens'] {
          display: none;
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

  /** The Users tab exists only where user management does. */
  private get visibleTabs(): InventoryTab[] {
    return TAB_ORDER.filter((tab) => tab !== 'users' || this.showUsers);
  }

  /** The tab actually on screen, which is never a tab that is not there. */
  private get activeTab(): InventoryTab {
    return this.visibleTabs.includes(this.tab) ? this.tab : 'agents';
  }

  /** Whether the rows of one tab are still on their way. */
  private isTabLoading(tab: InventoryTab): boolean {
    const perTab =
      tab === 'agents'
        ? this.loadingAgents
        : tab === 'flows'
          ? this.loadingFlows
          : tab === 'models'
            ? this.loadingModels
            : tab === 'users'
              ? this.loadingUsers
              : this.loadingTools;
    return perTab === null || perTab === undefined ? this.loading : perTab;
  }

  /**
   * Whether this tab's usage columns are still on their way. Identity rows
   * stay put; only the number cells wait.
   */
  private isUsageLoading(tab: InventoryTab): boolean {
    const perTab =
      tab === 'flows'
        ? this.usageLoadingFlows
        : tab === 'models'
          ? this.usageLoadingModels
          : tab === 'tools'
            ? this.usageLoadingTools
            : null;
    return perTab === null || perTab === undefined ? this.usageLoading : perTab;
  }

  private isFlowCostLoading(): boolean {
    if (
      this.usageLoadingFlowCost !== null &&
      this.usageLoadingFlowCost !== undefined
    ) {
      return this.usageLoadingFlowCost;
    }
    return this.isUsageLoading('flows');
  }

  private countFor(tab: InventoryTab): number {
    if (tab === 'agents') return this.agentsTotal;
    if (tab === 'flows') return this.flowsTotal;
    if (tab === 'models') return this.modelsTotal;
    if (tab === 'users') return this.usersTotal;
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
    this.sorts = { ...this.sorts, [this.activeTab]: value };
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

  /**
   * Age, always relative.
   *
   * The default cuts over to a date after a week, which in a column this
   * narrow becomes "8/26/2..." and tells nobody anything. "6w ago" is the
   * answer the row is being asked for; the exact date is in the title.
   */
  private relative(value: string | null | undefined): string {
    if (!value) return 'Never';
    return formatRelativeTime(value, new Date(), {
      maxRelativeDays: Infinity,
    });
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

  /** SUCCEEDED shouts; a chip in a dense table speaks. */
  private statusLabel(status: string): string {
    const text = (status || '').toLowerCase();
    return text ? text.charAt(0).toUpperCase() + text.slice(1) : 'Unknown';
  }

  private get sortedAgents(): InventoryAgentRow[] {
    const rows = [...this.agentRows];
    // Sorting by a number nobody has yet would order the table by zero and
    // then reshuffle it under the reader's eyes. Until the usage lands, the
    // identity sort holds the rows still.
    const sort = this.usageLoading ? 'last-active' : this.sorts.agents;
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
    if (this.isUsageLoading('flows')) {
      rows.sort((a, b) => a.name.localeCompare(b.name));
      return rows;
    }
    const sort = this.sorts.flows;
    if (sort === 'spend' && this.isFlowCostLoading()) {
      rows.sort((a, b) => a.name.localeCompare(b.name));
      return rows;
    }
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
    if (this.isUsageLoading('models')) {
      rows.sort((a, b) => a.alias.localeCompare(b.alias));
      return rows;
    }
    const sort = this.sorts.models;
    if (sort === 'requests') {
      rows.sort((a, b) => b.requests - a.requests);
    } else {
      rows.sort((a, b) => b.cost - a.cost);
    }
    return rows;
  }

  private get sortedUsers(): InventoryUserRow[] {
    const rows = [...this.userRows];
    if (!this.usageLoading && this.sorts.users === 'spend') {
      rows.sort((a, b) => b.cost - a.cost);
    } else {
      rows.sort((a, b) => timeValue(b.lastLoginAt) - timeValue(a.lastLoginAt));
    }
    return rows;
  }

  private get sortedTools(): InventoryToolRow[] {
    const rows = [...this.toolRows];
    if (this.isUsageLoading('tools')) {
      rows.sort((a, b) => a.name.localeCompare(b.name));
      return rows;
    }
    const sort = this.sorts.tools;
    if (sort === 'failures') {
      rows.sort((a, b) => b.failed - a.failed);
    } else {
      rows.sort((a, b) => b.calls - a.calls);
    }
    return rows;
  }

  private renderHeader() {
    const options = SORT_OPTIONS[this.activeTab];
    const sortHeld =
      this.activeTab === 'flows'
        ? this.isUsageLoading('flows') || this.isFlowCostLoading()
        : this.isUsageLoading(this.activeTab);
    return html`
      <div slot="header" class="card-head">
        <span class="title">Inventory</span>
        <div class="header-controls">
          <sl-select
            class="sort-select"
            label="Sort ${TAB_LABELS[this.activeTab].toLowerCase()} by"
            size="small"
            hoist
            ?disabled=${sortHeld}
            title=${
              sortHeld
                ? 'Sort is held until usage arrives'
                : `Sort ${TAB_LABELS[this.activeTab].toLowerCase()} by`
            }
            value=${this.sorts[this.activeTab]}
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
          ${this.visibleTabs.map(
            (tab) => html`
              <sl-tab slot="nav" panel=${tab} ?active=${this.activeTab === tab}>
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

  /**
   * One usage number, or the space it will occupy. The skeleton is the width
   * of a number rather than the width of the column, so the cell does not
   * shimmer across half the table while it waits.
   */
  private renderUsageCell(value: unknown, pending = this.usageLoading) {
    if (pending) {
      return html`<sl-skeleton
        class="usage-skeleton"
        effect="none"
      ></sl-skeleton>`;
    }
    return value;
  }

  private renderEmpty() {
    const empty = EMPTY_STATES[this.activeTab];
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
        <!-- Same trade as the Models tab: an alias is 24 characters and a
             count is five, so the counts give up the four points that stop
             "deepseek/deepseek-v4-pro" reading as "deepseek/deepse...". -->
        <colgroup>
          <col style="width: 28%" />
          <col style="width: 27%" />
          <col style="width: 11%" />
          <col style="width: 11%" />
          <col style="width: 10%" />
          <col style="width: 13%" />
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
          this.isTabLoading('agents') && rows.length === 0
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
                        <td class="num" data-label="Requests">
                          ${this.renderUsageCell(
                            this.formatCompactNumber(row.requests)
                          )}
                        </td>
                        <!-- Tokens before cost, split in and out. -->
                        <td class="num" data-label="Tokens">
                          ${this.renderUsageCell(
                            html`<token-figures
                              .usage=${row.tokenUsage}
                            ></token-figures>`
                          )}
                        </td>
                        <td class="num">
                          ${this.renderUsageCell(this.formatCurrency(row.cost))}
                        </td>
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

  /**
   * What a run count means. Uncapped it is the range, full stop; capped it is
   * as much of the range as the last 100 runs of the account reached.
   */
  private get flowCountTitle(): string {
    return this.flowRunsCapped
      ? `In the last ${this.rangeLabel}, from the most recent 100 runs`
      : `In the last ${this.rangeLabel}`;
  }

  private renderLastRun(run: InventoryFlowRun | null) {
    if (!run) {
      if (this.isUsageLoading('flows')) {
        return html`<sl-skeleton
          class="usage-skeleton"
          effect="none"
        ></sl-skeleton>`;
      }
      // "No run in range" is only true when the page saw the whole range.
      return this.flowRunsCapped
        ? html`<span class="muted" title=${this.flowCountTitle}
            >No run in the most recent 100</span
          >`
        : html`<span class="muted">No run in range</span>`;
    }
    const duration = executionDurationText(run);
    return html`
      <span class="last-run">
        <sl-badge class="chip" pill variant=${this.statusVariant(run.status)}
          >${this.statusLabel(run.status)}</sl-badge
        >
        <!-- The category sits with the status it qualifies, before the
             subject, so "Failed · runner conflict" reads as one statement. -->
        ${renderFailureCategoryChip(run.failure_category)}
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
        <!-- The tokens column takes its width from the last run cell, which
             holds a sentence and can lose a few points, rather than from the
             name. -->
        <colgroup>
          <col style="width: 24%" />
          <col style="width: 32%" />
          <col style="width: 8%" />
          <col style="width: 8%" />
          <col style="width: 17%" />
          <col style="width: 11%" />
        </colgroup>
        <thead>
          <tr>
            <th>Flow</th>
            <th>Last run</th>
            <th class="num">Runs</th>
            <th class="num">Failed</th>
            <th class="num">Tokens</th>
            <th class="num">$ est.</th>
          </tr>
        </thead>
        ${
          this.isTabLoading('flows') && rows.length === 0
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
                            <a class="row-name" href="/console/flows/${row.id}"
                              >${row.name}</a
                            >
                          </span>
                        </td>
                        <td class="secondary wide-cell">
                          ${this.renderLastRun(row.lastRun)}
                        </td>
                        <td
                          class="num"
                          data-label="Runs"
                          title=${this.flowCountTitle}
                        >
                          ${
                            this.isUsageLoading('flows')
                              ? this.renderUsageCell(
                                  this.formatCompactNumber(row.runs),
                                  true
                                )
                              : html`<a
                                  class="row-name"
                                  href="/console/flows/executions?flow_id=${row.id}"
                                  >${this.formatCompactNumber(row.runs)}</a
                                >`
                          }
                        </td>
                        <td
                          class="num"
                          data-label="Failed"
                          title=${this.flowCountTitle}
                        >
                          ${this.renderUsageCell(
                            this.formatCompactNumber(row.failed),
                            this.isUsageLoading('flows')
                          )}
                        </td>
                        <!-- Tokens before cost, split in and out: the
                             volume that earned the dollar figure beside
                             it. -->
                        <td class="num" data-label="Tokens">
                          ${this.renderUsageCell(
                            html`<token-figures
                              .usage=${row.tokenUsage}
                            ></token-figures>`,
                            this.isFlowCostLoading()
                          )}
                        </td>
                        <td class="num">
                          ${this.renderUsageCell(
                            this.formatCurrency(row.cost),
                            this.isFlowCostLoading()
                          )}
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

  private renderModels() {
    const rows = this.sortedModels.slice(0, this.rowLimit);
    return html`
      <table class="inventory-table">
        <!-- The name is the row, so it gets the room: three counts of five
             characters each fit in 12% at any width this card is used at,
             and what is left over goes to the alias rather than to the
             gutters beside "1.2k". -->
        <colgroup>
          <col style="width: 42%" />
          <col style="width: 22%" />
          <col style="width: 12%" />
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
          </tr>
        </thead>
        ${
          this.isTabLoading('models') && rows.length === 0
            ? this.renderSkeleton(5)
            : html`
                <tbody>
                  ${repeat(
                    rows,
                    (row) => `${row.id || row.alias}`,
                    (row) => html`
                      <tr>
                        <td class="identity-cell" title=${row.alias}>
                          <span class="identity">
                            ${
                              row.id
                                ? html`<a
                                    class="row-name"
                                    href="/console/ai-models/${row.id}"
                                    >${modelAliasLabel(
                                      row.alias,
                                      row.provider
                                    )}</a
                                  >`
                                : html`<span class="row-name"
                                    >${modelAliasLabel(
                                      row.alias,
                                      row.provider
                                    )}</span
                                  >`
                            }
                          </span>
                        </td>
                        <td class="secondary muted">
                          ${row.provider || 'Unknown'}
                        </td>
                        <td class="num" data-label="Requests">
                          ${this.renderUsageCell(
                            this.formatCompactNumber(row.requests),
                            this.isUsageLoading('models')
                          )}
                        </td>
                        <td class="num" data-label="Tokens">
                          ${this.renderUsageCell(
                            html`<token-figures
                              .usage=${row.tokenUsage}
                            ></token-figures>`,
                            this.isUsageLoading('models')
                          )}
                        </td>
                        <td class="num">
                          ${this.renderUsageCell(
                            this.formatCurrency(row.cost),
                            this.isUsageLoading('models')
                          )}
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

  /**
   * Who is on the account, what they own and what they spent in the range.
   *
   * Spend is attributed through the agents a person owns, which is the only
   * link the gateway records between a request and a person. Someone who owns
   * no agent shows no spend, which is true: their spend is somebody's agent.
   */
  private renderUsers() {
    const rows = this.sortedUsers.slice(0, this.rowLimit);
    return html`
      <table class="inventory-table">
        <colgroup>
          <col style="width: 30%" />
          <col style="width: 18%" />
          <col style="width: 14%" />
          <col style="width: 12%" />
          <col style="width: 12%" />
          <col style="width: 14%" />
        </colgroup>
        <thead>
          <tr>
            <th>User</th>
            <th>Role</th>
            <th class="num">Last login</th>
            <th class="num">Agents</th>
            <th class="num">Tokens</th>
            <th class="num">$ est.</th>
          </tr>
        </thead>
        ${
          this.isTabLoading('users') && rows.length === 0
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
                            <user-avatar
                              .label=${row.name}
                              .seed=${row.id}
                              .size=${20}
                            ></user-avatar>
                            <span class="row-name">${row.name}</span>
                          </span>
                        </td>
                        <td class="secondary muted" title=${row.role || ''}>
                          ${row.role || 'No role'}
                        </td>
                        <td
                          class="num"
                          data-label="Last login"
                          title=${this.absolute(row.lastLoginAt)}
                        >
                          ${this.relative(row.lastLoginAt)}
                        </td>
                        <td class="num" data-label="Agents">
                          ${this.formatCompactNumber(row.agentsOwned)}
                        </td>
                        <td class="num" data-label="Tokens">
                          ${this.renderUsageCell(
                            html`<token-figures
                              .usage=${row.tokenUsage}
                            ></token-figures>`
                          )}
                        </td>
                        <td class="num">
                          ${this.renderUsageCell(this.formatCurrency(row.cost))}
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
          this.isTabLoading('tools') && rows.length === 0
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
                        <td class="num" data-label="Calls">
                          ${this.renderUsageCell(
                            this.formatCompactNumber(row.calls),
                            this.isUsageLoading('tools')
                          )}
                        </td>
                        <td class="num" data-label="Failed">
                          ${this.renderUsageCell(
                            this.formatCompactNumber(row.failed),
                            this.isUsageLoading('tools')
                          )}
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
    if (this.activeTab === 'agents') return this.agentRows.length;
    if (this.activeTab === 'flows') return this.flowRows.length;
    if (this.activeTab === 'models') return this.modelRows.length;
    if (this.activeTab === 'users') return this.userRows.length;
    return this.toolRows.length;
  }

  private renderBody() {
    if (!this.isTabLoading(this.activeTab) && this.currentRowCount() === 0) {
      return this.renderEmpty();
    }
    if (this.activeTab === 'agents') return this.renderAgents();
    if (this.activeTab === 'flows') return this.renderFlows();
    if (this.activeTab === 'models') return this.renderModels();
    if (this.activeTab === 'users') return this.renderUsers();
    return this.renderTools();
  }

  private renderFooter() {
    const tab = this.activeTab;
    const link = ALL_LINKS[tab];
    const count = this.countFor(tab);
    if (count === 0) {
      return nothing;
    }
    // One person on the account is not a list worth opening; it is a hint
    // that nobody else has been asked yet.
    if (tab === 'users' && count === 1) {
      return html`
        <div class="footer">
          <span class="footer-note">Working alone?</span>
          <a href="/console/settings/invitations">Invite a teammate →</a>
        </div>
      `;
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
