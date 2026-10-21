import { tableScrollStyles } from '../styles/table-scroll';
import { formatUsd, formatUsdExact } from '../utils/money';
import {
  renderWithoutSubscriptionsLine,
  type SubscriptionEstimate,
} from '../utils/subscription-estimate';
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
import './time-range-select';
import './fleet-map';
import { getFleetMap, type FleetMapGraph } from '../fleet-map-api';
import {
  loadFleetMapWindow,
  windowMinutes,
  type FleetMapWindow,
} from '../utils/fleet-map-activity';
import { DEFAULT_USAGE_RANGE, USAGE_RANGE_OPTIONS } from './usage-card';
import { userHref } from '../utils/user-href';

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
import {
  ListTable,
  dataTableStyles,
  listTableStyles,
  type ListColumn,
} from '../table';
import '../table/data-table';
import '../table/column-picker';

/** Which inventory the box is showing. */
export type InventoryTab =
  'agents' | 'devices' | 'flows' | 'models' | 'tools' | 'users';

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
  /** Spend plus the API-equivalent cost of subscription requests (D-05). */
  withoutSubscriptions?: SubscriptionEstimate | null;
  /** Subscription-routed requests behind that estimate; 0 without any. */
  subscriptionRequests?: number;
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
  /** Spend plus the API-equivalent cost of their agents' subscription use. */
  withoutSubscriptions?: SubscriptionEstimate | null;
  subscriptionRequests?: number;
}

/** One machine from the device fleet, as the fleet page names it. */
export interface InventoryDeviceRow {
  key: string;
  name: string;
  /** Platform as people write it ("macOS"), from the fleet page's helper. */
  os: string;
  href: string;
  people: { id: string | null; label: string; href: string | null }[];
  runnerState: string;
  runnerLabel: string;
  runnerLive: boolean;
  agentsGoverned: number;
  agentsUngoverned: number;
  lastSeenAt: string | null;
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
  devices: [
    { value: 'last-seen', label: 'Last seen' },
    { value: 'agents', label: 'Agents' },
  ],
};

const TAB_ORDER: InventoryTab[] = [
  'agents',
  'flows',
  'models',
  'tools',
  'users',
  'devices',
];

const TAB_LABELS: Record<InventoryTab, string> = {
  agents: 'Agents',
  devices: 'Devices',
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
  devices: {
    text: 'No devices yet.',
    href: '/console/settings/device-fleet',
    linkText: 'Set up the device fleet',
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
  devices: { href: '/console/settings/device-fleet', noun: 'devices' },
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
 * place, at one depth. The range select here is the page range, shared with
 * the Usage card, because a page with two time ranges has two truths.
 */
@customElement('inventory-card')
export class InventoryCard extends LitElement {
  @property({ type: Array }) agentRows: InventoryAgentRow[] = [];
  @property({ type: Array }) flowRows: InventoryFlowRow[] = [];
  @property({ type: Array }) modelRows: InventoryModelRow[] = [];
  @property({ type: Array }) toolRows: InventoryToolRow[] = [];
  @property({ type: Array }) userRows: InventoryUserRow[] = [];
  @property({ type: Array }) deviceRows: InventoryDeviceRow[] = [];

  /** Totals for the tab labels and the footer link: everything, not the page. */
  @property({ type: Number }) agentsTotal = 0;
  @property({ type: Number }) flowsTotal = 0;
  @property({ type: Number }) modelsTotal = 0;
  @property({ type: Number }) toolsTotal = 0;
  @property({ type: Number }) usersTotal = 0;
  @property({ type: Number }) devicesTotal = 0;

  /**
   * Off on OSS, where there is one account and no user management: a tab
   * listing one person, always, is furniture.
   */
  @property({ type: Boolean }) showUsers = false;
  /** Only where the device fleet is licensed and this reader may view it. */
  @property({ type: Boolean }) showDevices = false;
  /**
   * Tabs whose list could not be read. Their count and body say so instead
   * of "0" and "No agents yet", which would be a statement about the account
   * when the truth is about one request.
   */
  @property({ type: Array }) unavailableTabs: InventoryTab[] = [];

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
  @property({ type: Boolean }) loadingDevices: boolean | null = null;
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
  /** Whether the usage belongs to the range in the header. */
  @property({ type: Boolean }) usageAvailable = true;
  @property({ type: Boolean }) modelUsageAvailable = true;
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
   * The page range key. The select here and the one on the Usage card both
   * emit `range-change` to the page, which owns the one range for both.
   */
  @property({ type: String }) timeRange: string = DEFAULT_USAGE_RANGE;
  /**
   * True when the run counts came off a full page of the most recent 100
   * executions whose oldest row is still inside the range: they are then a
   * floor, not the range's total, and every cell that shows one says so.
   */
  @property({ type: Boolean }) flowRunsCapped = false;

  /**
   * Instance administrators may swap the table for the spatial fleet map.
   * Everyone else keeps the table and never sees the choice.
   */
  @property({ type: Boolean }) instanceAdmin = false;
  /** Whose inventory view and map positions are stored. */
  @property({ type: String }) userId = '';
  @state() private inventoryView: 'table' | 'map' = 'table';
  @state() private map: FleetMapGraph | null = null;
  @state() private mapError = '';
  @state() private mapWindow: FleetMapWindow = loadFleetMapWindow();
  private mapController: AbortController | null = null;
  @state() private tab: InventoryTab = 'agents';
  @state() private sorts: Record<InventoryTab, string> = {
    agents: SORT_OPTIONS.agents[0].value,
    flows: SORT_OPTIONS.flows[0].value,
    models: SORT_OPTIONS.models[0].value,
    tools: SORT_OPTIONS.tools[0].value,
    users: SORT_OPTIONS.users[0].value,
    devices: SORT_OPTIONS.devices[0].value,
  };
  @state() private rowLimit = ROWS_DEFAULT;

  /**
   * The Agents tab, on the shared console list: resizable headers, a column
   * picker, and widths and visibility kept per browser like every other
   * console list. Rows stay in the card's own sort and row limit, so no
   * column is sortable here; the sort select in the header orders them.
   */
  readonly agentTable = new ListTable<InventoryAgentRow>(this, {
    listId: 'overview-inventory-agents',
    getRowId: (row) => row.id,
    pageSize: 0,
    columns: this.agentColumns(),
  });

  private onResize = () => {
    const limit = window.innerHeight > TALL_VIEWPORT ? ROWS_TALL : ROWS_DEFAULT;
    if (limit !== this.rowLimit) {
      this.rowLimit = limit;
    }
  };

  static styles = [
    tableScrollStyles,
    [
      unsafeCSS(consoleStyles),
      listTableStyles,
      dataTableStyles,
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
        /* Wraps on phones: two selects do not fit next to the title. */
        .card-head {
          align-items: center;
          display: flex;
          flex-wrap: wrap;
          gap: var(--sl-spacing-small);
          justify-content: space-between;
        }

        .title {
          font-size: var(--console-text-card-title);
          font-weight: 600;
        }

        .inventory-view {
          display: inline-flex;
          border: 1px solid var(--console-button-border);
          border-radius: var(--sl-border-radius-medium);
          overflow: hidden;
        }
        .inventory-view button {
          font: inherit;
          font-size: var(--console-text-meta);
          display: inline-flex;
          align-items: center;
          gap: 6px;
          padding: 4px 10px;
          min-height: 30px;
          border: 0;
          background: var(--console-button-face);
          color: var(--console-meta-color);
          cursor: pointer;
        }
        .inventory-view button[aria-pressed='true'] {
          color: var(--console-body-color);
          background: var(--console-hover-tint);
        }
        .inventory-map {
          --fleet-map-height: clamp(420px, calc(100dvh - 360px), 720px);
        }

        .header-controls {
          align-items: center;
          display: flex;
          flex-wrap: wrap;
          gap: var(--sl-spacing-small);
          max-width: 100%;
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

        /* The page range, editable here too: it is the same range as the
         Usage card's, so changing either changes both. */
        /* A fixed width: the host's min(width, 100%) resolves against the
           flex row's max-content and left a gap at the card's edge. */
        .range-select {
          width: 128px;
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
        .inventory-table th.numeric,
        .inventory-table td.num {
          font-variant-numeric: tabular-nums;
          padding-inline: var(--sl-spacing-x-small);
          text-align: right;
        }

        .inventory-table th.num:last-child,
        .inventory-table th.numeric:last-child,
        .inventory-table td.num:last-child {
          padding-right: var(--sl-spacing-medium);
        }

        .identity {
          align-items: center;
          display: flex;
          gap: var(--sl-spacing-x-small);
          min-width: 0;
        }

        .device-os {
          flex: none;
          font-size: var(--console-text-meta);
        }

        .device-people {
          display: block;
          overflow: hidden;
          text-overflow: ellipsis;
          white-space: nowrap;
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
            /* The separators below carry the rhythm now, so the gap only has
             to keep the words apart. */
            gap: 2px 4px;
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

          /* The second line is one sentence of numbers, so it is punctuated.
           The separator leads each value instead of trailing the one before
           it: a trailing dot wraps onto the next line on its own, and hiding
           a cell (Tokens, below) used to leave its dot behind. Cells with no
           label resolve attr(data-label) to nothing and print just the dot. */
          .inventory-table
            :is(td.num, td.secondary:not(.wide-cell))
            ~ :is(td.num, td.secondary:not(.wide-cell))::before {
            content: '· ' attr(data-label) ' ';
          }

          /* Tokens are the one number a phone can do without: the request
           count and the spend already say how much this row is doing. */
          .inventory-table td[data-label='Tokens'] {
            display: none;
          }
        }
      `,
    ],
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
    this.mapController?.abort();
    super.disconnectedCallback();
  }

  /** Users and Devices exist only where their features do. */
  private get visibleTabs(): InventoryTab[] {
    return TAB_ORDER.filter(
      (tab) =>
        (tab !== 'users' || this.showUsers) &&
        (tab !== 'devices' || this.showDevices)
    );
  }

  private isUnavailable(tab: InventoryTab): boolean {
    return this.unavailableTabs.includes(tab);
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
              : tab === 'devices'
                ? this.loadingDevices
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
    if (tab === 'devices') return this.devicesTotal;
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

  private get sortedDevices(): InventoryDeviceRow[] {
    const rows = [...this.deviceRows];
    if (this.sorts.devices === 'agents') {
      rows.sort(
        (a, b) =>
          b.agentsGoverned +
          b.agentsUngoverned -
          (a.agentsGoverned + a.agentsUngoverned)
      );
    } else {
      rows.sort((a, b) => timeValue(b.lastSeenAt) - timeValue(a.lastSeenAt));
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

  private get inventoryViewKey(): string {
    return `preloop:inventory:view:${this.userId || 'anonymous'}`;
  }

  protected willUpdate(changed: Map<string, unknown>): void {
    super.willUpdate?.(changed);
    if (changed.has('userId') || changed.has('instanceAdmin')) {
      let saved: string | null = null;
      try {
        saved = localStorage.getItem(this.inventoryViewKey);
      } catch {
        saved = null;
      }
      this.inventoryView =
        this.instanceAdmin && saved === 'map' ? 'map' : 'table';
      if (this.inventoryView === 'map' && !this.map) void this.loadMap();
    }
  }

  /**
   * Say which view is showing, so the page can give the Map the full row: a
   * topology does not fit the 2fr column a table does (F-04).
   */
  protected updated(changed: Map<string, unknown>): void {
    super.updated?.(changed);
    if (changed.has('inventoryView')) {
      this.dispatchEvent(
        new CustomEvent('inventory-view-change', {
          detail: { view: this.inventoryView },
          bubbles: true,
          composed: true,
        })
      );
    }
  }

  private setInventoryView(view: 'table' | 'map'): void {
    if (!this.instanceAdmin || view === this.inventoryView) return;
    this.inventoryView = view;
    try {
      localStorage.setItem(this.inventoryViewKey, view);
    } catch {
      // A preference, not a requirement.
    }
    if (view === 'map' && !this.map) void this.loadMap();
  }

  /** ``quiet``: a background refresh keeps the current map on failure. */
  private async loadMap(quiet = false): Promise<void> {
    this.mapController?.abort();
    const controller = (this.mapController = new AbortController());
    this.mapError = '';
    try {
      const map = await getFleetMap(
        controller.signal,
        windowMinutes(this.mapWindow)
      );
      if (!controller.signal.aborted) this.map = map;
    } catch (error) {
      if (!controller.signal.aborted && !(quiet && this.map))
        this.mapError =
          error instanceof Error
            ? error.message
            : 'The fleet map could not be loaded.';
    }
  }

  private renderViewChoice() {
    if (!this.instanceAdmin) return nothing;
    return html`<div
      class="inventory-view"
      role="group"
      aria-label="Inventory view"
    >
      ${(
        [
          ['table', 'Table', 'table'],
          ['map', 'Map', 'diagram-3'],
        ] as const
      ).map(
        ([view, label, icon]) =>
          html`<button
            type="button"
            data-inventory-view=${view}
            aria-pressed=${this.inventoryView === view ? 'true' : 'false'}
            @click=${() => this.setInventoryView(view)}
          >
            <sl-icon name=${icon} aria-hidden="true"></sl-icon>${label}
          </button>`
      )}
    </div>`;
  }

  private renderMap() {
    if (this.mapError)
      return html`<div class="empty" role="status">${this.mapError}</div>`;
    if (!this.map)
      return html`<div class="empty" role="status">Mapping the fleet...</div>`;
    return html`<fleet-map
      class="inventory-map"
      wheel-mode="page"
      .graph=${this.map}
      .activityWindow=${this.mapWindow}
      live
      instance-admin
      default-arrangement="spatial"
      positions-scope="inventory"
      user-id=${this.userId}
      @fleet-map-window-change=${(
        event: CustomEvent<{ window: FleetMapWindow }>
      ) => {
        this.mapWindow = event.detail.window;
        void this.loadMap();
      }}
      @fleet-map-stale=${() => void this.loadMap(true)}
    ></fleet-map>`;
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
          ${this.renderViewChoice()}
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
          ${this.activeTab === 'agents' ? this.renderAgentColumnPicker() : nothing}
          <time-range-select
            class="range-select"
            ariaLabel="Inventory time range"
            .value=${this.timeRange}
            .options=${USAGE_RANGE_OPTIONS}
          ></time-range-select>
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
                ${
                  this.isUnavailable(tab)
                    ? nothing
                    : html`<span class="tab-count">${this.countFor(tab)}</span>`
                }
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
  private renderUsageCell(
    value: unknown,
    pending = this.usageLoading,
    available = this.activeTab === 'models'
      ? this.modelUsageAvailable
      : this.usageAvailable
  ) {
    if (pending) {
      return html`<sl-skeleton
        class="usage-skeleton"
        effect="none"
      ></sl-skeleton>`;
    }
    return available
      ? value
      : html`<span title="Usage unavailable for this range">—</span>`;
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

  private agentColumns(): Array<ListColumn<InventoryAgentRow>> {
    return [
      {
        id: 'name',
        header: 'Agent',
        hideable: false,
        // The name is what a reader scans for; it gets the room and keeps
        // at least enough for "claude-code on a laptop" before cutting.
        // Widths are content widths: every cell adds 32px of padding, and
        // at 280 + four numeric columns the table measured 776px in a
        // 735px card at 1440, clipping Last seen and leaving Model at 0px.
        // 240 keeps a name and its status chip on one line and leaves Model
        // about 60px, enough for "No model" and an ellipsised alias.
        // 212, not 240: the spend column took 28px for the line under it
        // that says what the spend would be without subscriptions (D-05).
        width: 212,
        minWidth: 160,
        truncate: true,
        cellClass: 'identity-cell',
        cellTitle: (row) => row.name,
        cell: (row) => html`
          <span class="identity">
            ${renderAgentIcon(row.kind)}
            <a
              class="row-name"
              href="/console/agents/${row.id}"
              title=${row.name}
              >${row.name}</a
            >
            <sl-badge class="chip" pill variant=${row.status.variant}
              >${row.status.label}</sl-badge
            >
          </span>
        `,
      },
      {
        id: 'model',
        header: 'Model',
        // Absorbs what the other columns leave, so it has no handle.
        resizable: false,
        truncate: true,
        cellClass: 'secondary muted',
        cellTitle: (row) => row.modelAlias || '',
        cell: (row) => row.modelAlias || 'No model',
      },
      {
        id: 'requests',
        cellClass: 'num',
        cellLabel: 'Requests',
        header: 'Requests',
        numeric: true,
        width: 64,
        cell: (row) =>
          this.renderUsageCell(this.formatCompactNumber(row.requests)),
      },
      {
        // Tokens before cost, as one compact total (#1647); the split is in
        // the tooltip.
        id: 'tokens',
        cellClass: 'num',
        cellLabel: 'Tokens',
        header: 'Tokens',
        numeric: true,
        width: 72,
        cell: (row) =>
          this.renderUsageCell(
            html`<token-figures
              total-only
              .usage=${row.tokenUsage}
            ></token-figures>`
          ),
      },
      {
        id: 'cost',
        cellClass: 'num',
        cellLabel: 'Spend',
        header: '$ est.',
        headerLabel: 'Estimated spend',
        pickerLabel: 'Estimated spend',
        numeric: true,
        width: 100,
        cell: (row) =>
          this.renderUsageCell(
            html`<span title=${formatUsdExact(row.cost)}
                >${formatUsd(row.cost)}</span
              >${renderWithoutSubscriptionsLine(row.withoutSubscriptions, row.subscriptionRequests)}`
          ),
      },
      {
        id: 'last_seen',
        cellClass: 'num',
        cellLabel: 'Seen',
        header: 'Last seen',
        numeric: true,
        width: 64,
        cellTitle: (row) => this.absolute(row.lastSeenAt),
        cell: (row) => this.relative(row.lastSeenAt),
      },
    ];
  }

  private renderAgentColumnPicker() {
    const table = this.agentTable;
    return html`<column-picker
      class="column-picker"
      .columns=${table.pickerColumns}
      ?can-reset=${table.hasColumnChanges}
      density=${table.density}
      @column-toggle=${(event: CustomEvent) =>
        table.setVisible(event.detail.id, event.detail.visible)}
      @columns-reset=${() => table.resetColumns()}
      @density-change=${(event: CustomEvent) =>
        table.setDensity(event.detail.density)}
    ></column-picker>`;
  }

  private renderAgents() {
    const rows = this.sortedAgents.slice(0, this.rowLimit);
    this.agentTable.data = rows;
    return html`
      <div class="table-scroll">
        <data-table
          class="agent-table"
          table-class="inventory-table"
          .table=${this.agentTable}
          .rows=${this.agentTable.rows}
          .loading=${this.isTabLoading('agents') && rows.length === 0}
          .stickyHeader=${false}
          skeleton-rows="3"
          label="Agents"
          noun="agents"
        ></data-table>
      </div>
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
      <div class="table-scroll">
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
                              <a
                                class="row-name"
                                href="/console/flows/${row.id}"
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
                              this.isUsageLoading('flows'),
                              true
                            )}
                          </td>
                          <!-- Tokens before cost: the volume that earned the
                             dollar figure beside it, as one total (the split
                             is in the tooltip). -->
                          <td class="num" data-label="Tokens">
                            ${this.renderUsageCell(
                              html`<token-figures
                                total-only
                                .usage=${row.tokenUsage}
                              ></token-figures>`,
                              this.isFlowCostLoading()
                            )}
                          </td>
                          <td class="num" data-label="Spend">
                            ${this.renderUsageCell(
                              html`<span title=${formatUsdExact(row.cost)}
                                >${formatUsd(row.cost)}</span
                              >`,
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
      </div>
    `;
  }

  private renderModels() {
    const rows = this.sortedModels.slice(0, this.rowLimit);
    return html`
      <div class="table-scroll">
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
                                total-only
                                .usage=${row.tokenUsage}
                              ></token-figures>`,
                              this.isUsageLoading('models')
                            )}
                          </td>
                          <td class="num" data-label="Spend">
                            ${this.renderUsageCell(
                              html`<span title=${formatUsdExact(row.cost)}
                                >${formatUsd(row.cost)}</span
                              >`,
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
      </div>
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
      <div class="table-scroll">
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
                              <a class="row-name" href=${userHref(row.id)}
                                >${row.name}</a
                              >
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
                                total-only
                                .usage=${row.tokenUsage}
                              ></token-figures>`
                            )}
                          </td>
                          <td class="num" data-label="Spend">
                            ${this.renderUsageCell(html`<span title=${formatUsdExact(row.cost)}>${formatUsd(row.cost)}</span>${renderWithoutSubscriptionsLine(row.withoutSubscriptions, row.subscriptionRequests)}`)}
                          </td>
                        </tr>
                      `
                    )}
                  </tbody>
                `
          }
        </table>
      </div>
    `;
  }

  private renderTools() {
    const rows = this.sortedTools.slice(0, this.rowLimit);
    return html`
      <div class="table-scroll">
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
      </div>
    `;
  }

  private renderDevices() {
    const rows = this.sortedDevices.slice(0, this.rowLimit);
    return html`
      <div class="table-scroll">
        <table class="inventory-table">
          <colgroup>
            <col style="width: 30%" />
            <col style="width: 24%" />
            <col style="width: 18%" />
            <col style="width: 14%" />
            <col style="width: 14%" />
          </colgroup>
          <thead>
            <tr>
              <th>Device</th>
              <th>People</th>
              <th>Runner</th>
              <th class="num">Agents</th>
              <th class="num">Last seen</th>
            </tr>
          </thead>
          ${
            this.isTabLoading('devices') && rows.length === 0
              ? this.renderSkeleton(5)
              : html`
                  <tbody>
                    ${repeat(
                      rows,
                      (row) => row.key,
                      (row) => html`
                        <tr>
                          <td class="identity-cell">
                            <span class="identity">
                              <a class="row-name" href=${row.href}
                                >${row.name}</a
                              >
                              <span class="muted device-os">${row.os}</span>
                            </span>
                          </td>
                          <td class="secondary" data-label="People">
                            ${this.renderDevicePeople(row)}
                          </td>
                          <td class="secondary" data-label="Runner">
                            <sl-badge
                              class="chip"
                              pill
                              variant=${row.runnerLive ? 'success' : 'neutral'}
                              >${row.runnerLabel}</sl-badge
                            >
                          </td>
                          <td
                            class="num"
                            data-label="Agents"
                            title=${this.deviceAgentsTitle(row)}
                          >
                            ${row.agentsGoverned + row.agentsUngoverned}
                          </td>
                          <td
                            class="num"
                            data-label="Last seen"
                            title=${this.absolute(row.lastSeenAt)}
                          >
                            ${this.relative(row.lastSeenAt)}
                          </td>
                        </tr>
                      `
                    )}
                  </tbody>
                `
          }
        </table>
      </div>
    `;
  }

  /** First person named, then "+N" with the rest in the title. */
  private renderDevicePeople(row: InventoryDeviceRow) {
    if (row.people.length === 0) {
      return html`<span class="muted">Unknown</span>`;
    }
    const [first, ...rest] = row.people;
    return html`<span
      class="device-people"
      title=${row.people.map((person) => person.label).join(', ')}
      >${
        first.href
          ? html`<a href=${first.href}>${first.label}</a>`
          : html`<span>${first.label}</span>`
      }${
        rest.length
          ? html`<span class="muted"> +${rest.length}</span>`
          : nothing
      }</span
    >`;
  }

  private deviceAgentsTitle(row: InventoryDeviceRow): string {
    return row.agentsUngoverned
      ? `${row.agentsGoverned} governed, ${row.agentsUngoverned} not onboarded`
      : `${row.agentsGoverned} governed`;
  }

  private currentRowCount(): number {
    if (this.activeTab === 'agents') return this.agentRows.length;
    if (this.activeTab === 'devices') return this.deviceRows.length;
    if (this.activeTab === 'flows') return this.flowRows.length;
    if (this.activeTab === 'models') return this.modelRows.length;
    if (this.activeTab === 'users') return this.userRows.length;
    return this.toolRows.length;
  }

  private renderBody() {
    if (this.isUnavailable(this.activeTab) && this.currentRowCount() === 0) {
      return html`
        <div class="empty" role="status">
          ${TAB_LABELS[this.activeTab]} could not be loaded. The next refresh
          tries again.
        </div>
      `;
    }
    if (!this.isTabLoading(this.activeTab) && this.currentRowCount() === 0) {
      return this.renderEmpty();
    }
    if (this.activeTab === 'agents') return this.renderAgents();
    if (this.activeTab === 'flows') return this.renderFlows();
    if (this.activeTab === 'models') return this.renderModels();
    if (this.activeTab === 'users') return this.renderUsers();
    if (this.activeTab === 'devices') return this.renderDevices();
    return this.renderTools();
  }

  private renderFooter() {
    const tab = this.activeTab;
    const link = ALL_LINKS[tab];
    const count = this.countFor(tab);
    if (count === 0 || this.isUnavailable(tab)) {
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
    if (this.instanceAdmin && this.inventoryView === 'map')
      return html`
        <sl-card class="content-card">
          ${this.renderHeader()} ${this.renderMap()}
        </sl-card>
      `;
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
