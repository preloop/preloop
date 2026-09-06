import { html, css, unsafeCSS, nothing } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import {
  AuthedElement,
  createModelPriceOverride,
  createProviderBillingConnection,
  getAccountAgents,
  getAIModels,
  getBudgetPolicies,
  getCostAnalyticsSummary,
  getCostReconciliation,
  getFeatures,
  getProviderBillingConnections,
  getUsers,
  getModelPriceOverrides,
  getToolCostFlags,
  repriceCost,
  syncProviderBillingConnection,
  type BudgetPolicy,
} from '../../api';
import type {
  AIModel,
  CostAnalyticsSummaryResponse,
  CostReconciliationResponse,
  CostReconciliationRow,
  GatewayUsageBySession,
  GatewayUsageByTool,
  ImportedUsageByConversation,
  ImportedUsageByModel,
  ModelPriceOverride,
  ModelPriceOverrideCreate,
  ProviderBillingConnection,
} from '../../types';
import consoleStyles from '../../styles/console-styles.css?inline';
import '../../components/view-header.ts';
import '../../components/time-range-select.ts';
import '../../components/budget-policy-editor.ts';
import '../../components/budget-health-card.ts';
import '../../components/tool-cost-flags-panel.ts';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/card/card.js';
import '@shoelace-style/shoelace/dist/components/dialog/dialog.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/option/option.js';
import '@shoelace-style/shoelace/dist/components/select/select.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import '@shoelace-style/shoelace/dist/components/tab/tab.js';
import '@shoelace-style/shoelace/dist/components/tab-group/tab-group.js';
import '@shoelace-style/shoelace/dist/components/tab-panel/tab-panel.js';
import { consoleDialogStyles } from '../../styles/console-dialog';

type DateRangePreset =
  | 'today'
  | 'this-week'
  | 'this-month'
  | 'last-month'
  | 'last-7'
  | 'last-30'
  | 'last-90';

type DateRangeParams = {
  startDate: string;
  endDate: string;
};

type SortDirection = 'asc' | 'desc';

type SortState = {
  key: string;
  dir: SortDirection;
};

// A single sortable column. `value` extracts the sort key from a row; `numeric`
// selects numeric vs. locale-string comparison.
type SortColumn<T> = {
  key: string;
  label: string;
  numeric: boolean;
  value: (row: T) => number | string;
};

// Aggregated row for the Agents tab (grouped sessions by agent or flow).
type AgentGroupRow = {
  key: string;
  name: string;
  agentId: string | null;
  flowId: string | null;
  requests: number;
  totalTokens: number;
  cost: number;
};

// Aggregated row for the Users tab (grouped sessions by resolved owner).
type UserGroupRow = {
  username: string;
  requests: number;
  cost: number;
};

// One imported-usage thread: a conversation plus the subagent conversations
// spawned from it (rows whose parent_conversation_id points at it).
type ImportedConversationThread = {
  row: ImportedUsageByConversation;
  children: ImportedUsageByConversation[];
};

const COST_DATE_RANGE_STORAGE_KEY = 'preloop.cost.dateRange';
const DATE_RANGE_PRESETS: DateRangePreset[] = [
  'today',
  'this-week',
  'this-month',
  'last-month',
  'last-7',
  'last-30',
  'last-90',
];

// The page carries one range control, the shared `time-range-select`, so Cost
// speaks the same range vocabulary as the Overview instead of a bare labelled
// select. The calendar presets stay: "This month" and "Today" are what the
// Month-to-date and Projected cards are computed from.
const DATE_RANGE_OPTIONS: { value: DateRangePreset; label: string }[] = [
  { value: 'today', label: 'Today' },
  { value: 'this-week', label: 'This week' },
  { value: 'this-month', label: 'This month' },
  { value: 'last-month', label: 'Last month' },
  { value: 'last-7', label: '7d' },
  { value: 'last-30', label: '30d' },
  { value: 'last-90', label: '90d' },
];

@customElement('cost-view')
export class CostView extends AuthedElement {
  // Async reprice follow-up: check the summary every 5s for up to about a
  // minute, then stop and say so instead of polling forever.
  private static readonly REPRICE_POLL_INTERVAL_MS = 5000;
  private static readonly REPRICE_POLL_MAX_ATTEMPTS = 12;

  @state() private summary: CostAnalyticsSummaryResponse | null = null;
  @state() private previousRangeSummary: CostAnalyticsSummaryResponse | null =
    null;
  // When the numbers on screen were fetched. The page dropped its Refresh
  // button, so it says how fresh it is instead of offering a manual poll.
  @state() private loadedAt: string | null = null;
  @state() private budgetPolicies: BudgetPolicy[] = [];
  @state() private aiModels: AIModel[] = [];
  @state() private pricingOverrides: ModelPriceOverride[] = [];
  @state() private featureFlags: Record<string, boolean | string[]> = {};
  @state() private loading = true;
  @state() private saving = false;
  @state() private error: string | null = null;
  @state() private selectedRange: DateRangePreset = 'last-30';
  @state() private budgetDialogOpen = false;
  @state() private priceDialogOpen = false;
  @state() private priceMode:
    | 'custom_token_price'
    | 'fixed_request_price'
    | 'discount'
    | 'prepaid_tokens'
    | 'prepaid_credit' = 'custom_token_price';
  @state() private priceModelAlias = '';
  @state() private priceProvider = '';
  @state() private priceInput = '';
  @state() private priceOutput = '';
  @state() private pricePer1k = '';
  @state() private requestPrice = '';
  @state() private discountPercent = '';
  @state() private prepaidTokens = '';
  @state() private prepaidCredit = '';
  @state() private priceCurrency = 'USD';
  @state() private priceFxRate = '';
  // Reprice action (billing flag): re-derives cost for unpriced rows in the
  // selected window from stored tokens and current prices.
  @state() private repricing = false;
  @state() private repriceNotice: string | null = null;
  // Reconciliation tab (provider_billing_reconciliation flag).
  @state() private reconciliation: CostReconciliationResponse | null = null;
  @state() private reconciliationError: string | null = null;
  @state() private reconciliationLoading = false;
  @state() private providerConnections: ProviderBillingConnection[] = [];
  @state() private connectionProvider = 'openai';
  @state() private connectionAdminKey = '';
  @state() private connectionSaving = false;
  // Owner attribution for the Users tab: agent_id -> owner username. Populated
  // from the account's managed agents; empty when the lookup fails.
  @state() private agentOwnerMap = new Map<string, string>();
  @state() private agentOwnerBySource: Array<{
    sourceId: string;
    owner: string;
  }> = [];
  // For single-user accounts, unowned sessions (e.g. traffic from an agent kind
  // with no registered managed agent) are attributed to the sole user rather
  // than "Unattributed", since all account spend is that user's.
  @state() private fallbackOwner: string | null = null;
  @state() private ownerAttributionAvailable = true;
  // Tool cost flags fetched here so the Tools tab can choose between the full
  // <tool-cost-flags-panel> (when flags exist) and a small inline notice.
  @state() private toolFlagCount = 0;
  // Tracks which tabs have already triggered their lazy auxiliary fetch, so we
  // fetch per-tab data only when a tab is first opened (see D). Reset on every
  // core reload (e.g. date-range change) so the aux data re-fetches for the new
  // range when its tab is next shown.
  @state() private loadedTabs = new Set<string>();
  // Per-tab sort state. Keys reference the column identifiers used in each tab.
  @state() private agentSort: SortState = { key: 'cost', dir: 'desc' };
  @state() private toolSort: SortState = {
    key: 'total_cost',
    dir: 'desc',
  };
  @state() private sessionSort: SortState = { key: 'cost', dir: 'desc' };
  @state() private userSort: SortState = { key: 'cost', dir: 'desc' };
  @state() private importedSort: SortState = { key: 'cost', dir: 'desc' };

  private get modelPriceOverridesEnabled(): boolean {
    return this.featureFlags.model_price_overrides === true;
  }

  private get billingEnabled(): boolean {
    return this.featureFlags.billing === true;
  }

  private get reconciliationEnabled(): boolean {
    return this.featureFlags.provider_billing_reconciliation === true;
  }

  static styles = [
    consoleDialogStyles,
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

      .analytics-stack,
      .actions-stack {
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-large);
      }

      .toolbar {
        display: flex;
        gap: var(--sl-spacing-medium);
        align-items: center;
        flex-wrap: wrap;
      }

      /* Wide enough for "Last month"; the shared control is 96px by default,
         which is sized for 24h/7d/30d chips. */
      .toolbar time-range-select {
        --time-range-select-width: 140px;
      }

      /* The window the numbers cover, restated beside the control that chose
         it, plus how fresh they are now that there is no Refresh button. */
      .range-window {
        color: var(--sl-color-neutral-600);
        font-size: var(--sl-font-size-small);
        font-variant-numeric: tabular-nums;
      }

      /* It opens a dialog, so it is a button. As an anchor it pointed at a
         fragment that cannot resolve inside a shadow root, and told assistive
         tech it was a link. */
      .catalog-action {
        appearance: none;
        background: none;
        border: none;
        padding: 0;
        font: inherit;
        color: var(--sl-color-primary-600);
        cursor: pointer;
      }

      .catalog-action:hover {
        text-decoration: underline;
      }

      .metric-grid {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
        gap: var(--sl-spacing-medium);
      }

      .metric-card {
        padding: var(--sl-spacing-medium);
        border: 1px solid var(--sl-color-neutral-200);
        border-radius: var(--sl-border-radius-medium);
        background: var(--sl-color-neutral-0);
      }

      .metric-label {
        color: var(--sl-color-neutral-600);
        font-size: var(--sl-font-size-small);
      }

      .metric-value {
        margin-top: var(--sl-spacing-x-small);
        font-size: 1.6rem;
        font-weight: 700;
        color: var(--sl-color-neutral-950);
      }

      .metric-detail {
        margin-top: var(--sl-spacing-2x-small);
        color: var(--sl-color-neutral-600);
        font-size: var(--sl-font-size-small);
      }

      .section-header {
        display: flex;
        justify-content: space-between;
        align-items: flex-start;
        gap: var(--sl-spacing-small);
      }

      .section-title {
        display: flex;
        align-items: center;
        gap: var(--sl-spacing-small);
        font-weight: 700;
        color: var(--sl-color-neutral-900);
      }

      .tool-cost-flags-section {
        display: flex;
        flex-direction: column;
        width: 100%;
      }

      sl-tab-group::part(base) {
        --track-color: var(--sl-color-neutral-200);
      }

      .tab-panel-body {
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-medium);
      }

      .styled-table th.sortable {
        cursor: pointer;
        user-select: none;
        white-space: nowrap;
      }

      .styled-table th.sortable:hover {
        color: var(--sl-color-primary-600);
      }

      .sort-header {
        display: inline-flex;
        align-items: center;
        gap: var(--sl-spacing-2x-small);
      }

      .sort-header sl-icon {
        font-size: 0.85em;
        color: var(--sl-color-primary-600);
      }

      .cell-subtitle {
        color: var(--sl-color-neutral-500);
        font-size: var(--sl-font-size-x-small);
      }

      .tools-notice {
        margin-bottom: var(--sl-spacing-medium);
      }

      .form-grid {
        display: grid;
        grid-template-columns: 1fr;
        gap: var(--sl-spacing-medium);
      }

      .empty {
        color: var(--sl-color-neutral-600);
        padding: var(--sl-spacing-large);
      }

      .action-card-body,
      .dialog-description {
        color: var(--sl-color-neutral-600);
        font-size: var(--sl-font-size-small);
        line-height: 1.5;
      }

      .action-card-body {
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-medium);
      }

      .action-row {
        display: flex;
        justify-content: space-between;
        gap: var(--sl-spacing-small);
        align-items: center;
      }

      .policy-summary {
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-small);
      }

      .policy-summary-row {
        display: flex;
        justify-content: space-between;
        gap: var(--sl-spacing-small);
      }

      .policy-summary-label {
        color: var(--sl-color-neutral-600);
      }

      .policy-summary-value {
        color: var(--sl-color-neutral-900);
        font-weight: 600;
      }

      .loading-state {
        display: flex;
        align-items: center;
        justify-content: center;
        gap: var(--sl-spacing-small);
        min-height: 160px;
        color: var(--sl-color-neutral-600);
      }

      /* The previous range's answers, on their way out: readable, clearly not
         current, and in exactly the place the new ones will appear. Only the
         answers dim and go inert. The side column (budgets, pricing
         overrides) is not an answer about the range, so it stays live: no
         opacity here on the wrapper, which would group the side column into
         the fade. */
      .results.is-updating > *:not(.column-layout),
      .results.is-updating .main-column {
        opacity: 0.6;
        pointer-events: none;
      }

      .analytics-card::part(body) {
        padding: var(--sl-spacing-large);
      }

      .imported-usage-note {
        margin-bottom: var(--sl-spacing-medium);
        color: var(--sl-color-neutral-600);
        font-size: var(--sl-font-size-small);
        line-height: 1.5;
      }

      .imported-usage-totals {
        display: flex;
        flex-wrap: wrap;
        gap: var(--sl-spacing-large);
        margin-bottom: var(--sl-spacing-medium);
      }

      .imported-usage-total-label {
        color: var(--sl-color-neutral-600);
        font-size: var(--sl-font-size-small);
      }

      .imported-usage-total-value {
        margin-top: var(--sl-spacing-2x-small);
        font-size: 1.25rem;
        font-weight: 700;
        color: var(--sl-color-neutral-950);
      }

      .imported-conversations-title {
        margin: var(--sl-spacing-large) 0 var(--sl-spacing-x-small);
        font-size: var(--sl-font-size-medium);
        font-weight: 600;
        color: var(--sl-color-neutral-950);
      }

      .conversation-child-cell {
        padding-left: var(--sl-spacing-x-large);
      }

      .conversation-child-marker {
        color: var(--sl-color-neutral-500);
        margin-right: var(--sl-spacing-2x-small);
      }

      .conversation-thread-total td {
        font-weight: 600;
        background: var(--sl-color-neutral-50);
      }

      .not-reported {
        color: var(--sl-color-neutral-500);
        font-style: italic;
      }

      .analytics-table-wrap {
        overflow-x: auto;
      }

      .analytics-card .styled-table th {
        background: transparent;
        font-weight: 700;
      }

      .subject-links {
        display: flex;
        flex-wrap: wrap;
        gap: var(--sl-spacing-2x-small);
      }

      .agent-breakdown {
        margin-top: var(--sl-spacing-x-small);
        padding-left: var(--sl-spacing-medium);
        color: var(--sl-color-neutral-600);
        font-size: var(--sl-font-size-x-small);
      }

      .agent-breakdown-row {
        display: flex;
        justify-content: space-between;
        gap: var(--sl-spacing-small);
        padding: 2px 0;
      }

      sl-dialog::part(panel) {
        --width: 640px;
      }
    `,
  ];

  connectedCallback() {
    super.connectedCallback();
    this.selectedRange = this.loadStoredDateRange();
    this.requestedPanel = new URLSearchParams(window.location.search).get(
      'panel'
    );
    void this.load();
  }

  /**
   * `?panel=pricing` is how an attention item about unpriced models lands on
   * the part of this page that fixes it, instead of at the top of a long page
   * with no hint where to look.
   */
  protected updated(): void {
    if (this.loading || !this.requestedPanel) {
      return;
    }
    const panel = this.requestedPanel;
    this.requestedPanel = null;
    const target =
      this.renderRoot.querySelector(`#panel-${panel}`) ||
      this.renderRoot.querySelector('#panel-pricing-catalog');
    target?.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }

  private requestedPanel: string | null = null;

  private loadStoredDateRange(): DateRangePreset {
    const stored = window.localStorage.getItem(COST_DATE_RANGE_STORAGE_KEY);
    return DATE_RANGE_PRESETS.includes(stored as DateRangePreset)
      ? (stored as DateRangePreset)
      : 'last-30';
  }

  private persistDateRange(value: DateRangePreset) {
    window.localStorage.setItem(COST_DATE_RANGE_STORAGE_KEY, value);
  }

  private getDateParams(
    range: DateRangePreset = this.selectedRange
  ): DateRangeParams {
    const now = new Date();
    const start = new Date(now);
    const end = new Date(now);
    if (range === 'today') {
      start.setHours(0, 0, 0, 0);
    } else if (range === 'this-week') {
      const day = start.getDay();
      const daysSinceMonday = (day + 6) % 7;
      start.setDate(start.getDate() - daysSinceMonday);
      start.setHours(0, 0, 0, 0);
    } else if (range === 'this-month') {
      start.setDate(1);
      start.setHours(0, 0, 0, 0);
    } else if (range === 'last-month') {
      start.setMonth(now.getMonth() - 1, 1);
      start.setHours(0, 0, 0, 0);
      end.setDate(1);
      end.setHours(0, 0, 0, 0);
    } else {
      const days = range === 'last-7' ? 7 : range === 'last-90' ? 90 : 30;
      start.setDate(now.getDate() - days);
    }
    return {
      startDate: start.toISOString(),
      endDate: end.toISOString(),
    };
  }

  private getPreviousDateParams(range: DateRangePreset): DateRangeParams {
    const current = this.getDateParams(range);
    const start = new Date(current.startDate);
    const end = new Date(current.endDate);
    if (range === 'today') {
      const durationMs = end.getTime() - start.getTime();
      const previousStart = new Date(start);
      previousStart.setDate(previousStart.getDate() - 1);
      const previousEnd = new Date(previousStart.getTime() + durationMs);
      return {
        startDate: previousStart.toISOString(),
        endDate: previousEnd.toISOString(),
      };
    }
    if (range === 'this-week') {
      const previousEnd = new Date(start);
      const previousStart = new Date(start);
      previousStart.setDate(previousStart.getDate() - 7);
      return {
        startDate: previousStart.toISOString(),
        endDate: previousEnd.toISOString(),
      };
    }
    if (range === 'this-month' || range === 'last-month') {
      const previousStart = new Date(start);
      previousStart.setMonth(previousStart.getMonth() - 1, 1);
      const previousEnd = new Date(start);
      return {
        startDate: previousStart.toISOString(),
        endDate: previousEnd.toISOString(),
      };
    }
    const durationMs = end.getTime() - start.getTime();
    const previousEnd = start;
    const previousStart = new Date(previousEnd.getTime() - durationMs);
    return {
      startDate: previousStart.toISOString(),
      endDate: previousEnd.toISOString(),
    };
  }

  private async loadBudgetPolicies() {
    if (!this.billingEnabled) {
      this.budgetPolicies = [];
      return;
    }
    this.budgetPolicies = await getBudgetPolicies().catch(
      () => [] as BudgetPolicy[]
    );
  }

  private handleBudgetPoliciesChanged(
    event: CustomEvent<{ policies: BudgetPolicy[] }>
  ) {
    this.budgetPolicies = event.detail.policies;
  }

  // Build the agent_id -> owner_username map used by the Users tab. Owner is
  // resolved from the account's managed agents; a failed lookup leaves the map
  // empty and flips `ownerAttributionAvailable` so the tab degrades gracefully.
  private async loadAgentOwners() {
    try {
      const response = await getAccountAgents({ limit: 100 });
      const map = new Map<string, string>();
      const bySource: Array<{ sourceId: string; owner: string }> = [];
      for (const agent of response.items) {
        if (agent.owner_username) {
          map.set(agent.id, agent.owner_username);
          if (agent.session_source_id) {
            bySource.push({
              sourceId: agent.session_source_id,
              owner: agent.owner_username,
            });
          }
        }
      }
      // Longest source id first so the most specific agent wins in prefix match.
      bySource.sort((a, b) => b.sourceId.length - a.sourceId.length);
      this.agentOwnerMap = map;
      this.agentOwnerBySource = bySource;
      this.ownerAttributionAvailable = true;
      // Single-user account → attribute otherwise-unowned spend to that user.
      try {
        const users = await getUsers(0, 100);
        const list = users.users || [];
        this.fallbackOwner =
          list.length === 1 ? list[0].username || list[0].email || null : null;
      } catch {
        this.fallbackOwner = null;
      }
    } catch {
      this.agentOwnerMap = new Map();
      this.agentOwnerBySource = [];
      this.fallbackOwner = null;
      this.ownerAttributionAvailable = false;
    }
  }

  // Resolve a session's owner: prefer the backend-resolved agent_id, else
  // prefix-match the session source id against agent base source ids (per-run
  // ids append ':<id>' or '-<timestamp|uuid>'). Robust to a missing agent_id.
  private resolveSessionOwner(session: GatewayUsageBySession): string {
    if (session.agent_id) {
      const owner = this.agentOwnerMap.get(session.agent_id);
      if (owner) {
        return owner;
      }
    }
    const sourceId = session.session_source_id;
    if (sourceId) {
      for (const { sourceId: base, owner } of this.agentOwnerBySource) {
        if (
          sourceId === base ||
          sourceId.startsWith(`${base}:`) ||
          sourceId.startsWith(`${base}-`)
        ) {
          return owner;
        }
      }
    }
    return this.fallbackOwner || 'Unattributed';
  }

  // Fetch the tool cost flag count so the Tools tab can decide between the full
  // <tool-cost-flags-panel> and a lightweight inline notice.
  private async loadToolFlagCount() {
    try {
      const flags = await getToolCostFlags();
      this.toolFlagCount = flags.length;
    } catch {
      this.toolFlagCount = 0;
    }
  }

  // Date-range selector change handler. Defined as a bound arrow property so
  // `this` is always the element regardless of how Lit invokes the listener.
  // Clears previous-period comparison before reload so a stale delta from
  // another range is never shown (see C).
  private handleRangeChange = (event: Event) => {
    const detail = (event as CustomEvent<{ value?: string }>).detail;
    const value = (detail?.value ??
      (event.target as HTMLSelectElement).value) as DateRangePreset;
    if (!DATE_RANGE_PRESETS.includes(value) || value === this.selectedRange) {
      return;
    }
    this.selectedRange = value;
    this.previousRangeSummary = null;
    this.persistDateRange(value);
    void this.load();
  };

  private async load() {
    this.loading = true;
    this.error = null;
    // A core reload (initial mount or date-range change) invalidates any
    // per-tab auxiliary data, which is scoped to the summary window. Clearing
    // this lets the lazy loaders re-run for the newly loaded range the next
    // time each tab is shown (see D).
    this.loadedTabs = new Set<string>();
    try {
      // Current-period summary drives first paint. Previous-period comparison
      // is secondary and loads in the background (backend has no single-call
      // delta endpoint).
      const [summary, aiModels, features] = await Promise.all([
        getCostAnalyticsSummary(this.getDateParams()),
        getAIModels(),
        getFeatures().catch(() => ({ features: {} })),
      ]);
      this.summary = summary;
      this.loadedAt = new Date().toISOString();
      this.featureFlags = features.features || {};
      await this.loadBudgetPolicies();
      this.aiModels = aiModels;
      if (this.featureFlags.model_price_overrides === true) {
        this.pricingOverrides = await getModelPriceOverrides({
          activeOnly: true,
        }).catch(() => []);
      } else {
        this.pricingOverrides = [];
      }
      void this.loadPreviousRangeSummary();
      // Per-tab auxiliary data (owner map for Users, flag count for Tools) is
      // fetched lazily via handleTabShow when a tab is first opened, not up
      // front, to keep the initial load fast (see D).
    } catch (error) {
      this.error =
        error instanceof Error
          ? error.message
          : 'Failed to load cost analytics';
    } finally {
      this.loading = false;
    }
  }

  private async loadPreviousRangeSummary(): Promise<void> {
    const range = this.selectedRange;
    const cacheKey = `preloop.cost.previous_summary.v1:${range}`;
    try {
      const cached = sessionStorage.getItem(cacheKey);
      if (cached) {
        const parsed = JSON.parse(cached) as {
          cachedAt: number;
          data: CostAnalyticsSummaryResponse;
        };
        if (
          parsed?.data &&
          Date.now() - parsed.cachedAt < 120_000 &&
          !this.previousRangeSummary
        ) {
          this.previousRangeSummary = parsed.data;
        }
      }
    } catch {
      // ignore cache read errors
    }

    try {
      const previousSummary = await getCostAnalyticsSummary(
        this.getPreviousDateParams(range)
      );
      // Ignore if the user changed range while this request was in flight.
      if (this.selectedRange !== range) return;
      this.previousRangeSummary = previousSummary;
      try {
        sessionStorage.setItem(
          cacheKey,
          JSON.stringify({ cachedAt: Date.now(), data: previousSummary })
        );
      } catch {
        // ignore cache write errors
      }
    } catch {
      if (this.selectedRange === range && !this.previousRangeSummary) {
        this.previousRangeSummary = null;
      }
    }
  }

  // Lazily loads a tab's auxiliary data the first time it is shown. Triggered by
  // the sl-tab-group `sl-tab-show` event. The Users tab needs the agent -> owner
  // map (see G); the Tools tab needs the flag count. Other tabs are driven
  // entirely by the core summary and need no extra fetch.
  private async handleTabShow(event: CustomEvent<{ name: string }>) {
    const tab = event.detail?.name;
    if (!tab || this.loadedTabs.has(tab)) {
      return;
    }
    // Mark before awaiting so a rapid re-show does not double-fetch.
    this.loadedTabs = new Set(this.loadedTabs).add(tab);
    if (tab === 'users') {
      await this.loadAgentOwners();
    } else if (tab === 'tools') {
      await this.loadToolFlagCount();
    } else if (tab === 'reconciliation') {
      await this.loadReconciliation();
    }
  }

  // Fetches provider connections and the estimated-vs-actual comparison for
  // the current date range. Only called when the Reconciliation tab is shown.
  private async loadReconciliation() {
    this.reconciliationLoading = true;
    this.reconciliationError = null;
    try {
      const range = this.getDateParams();
      const [connections, reconciliation] = await Promise.all([
        getProviderBillingConnections().catch(() => []),
        getCostReconciliation({
          startDate: range.startDate,
          endDate: range.endDate,
        }),
      ]);
      this.providerConnections = connections;
      this.reconciliation = reconciliation;
    } catch (error) {
      this.reconciliationError =
        error instanceof Error
          ? error.message
          : 'Failed to load reconciliation data';
    } finally {
      this.reconciliationLoading = false;
    }
  }

  private async saveProviderConnection() {
    if (!this.connectionAdminKey.trim()) {
      this.reconciliationError = 'Enter the provider admin API key.';
      return;
    }
    this.connectionSaving = true;
    this.reconciliationError = null;
    try {
      const connection = await createProviderBillingConnection({
        provider: this.connectionProvider,
        admin_api_key: this.connectionAdminKey.trim(),
      });
      this.connectionAdminKey = '';
      // Trigger the first backfill immediately so the table populates.
      await syncProviderBillingConnection(connection.id).catch(() => undefined);
      await this.loadReconciliation();
    } catch (error) {
      this.reconciliationError =
        error instanceof Error ? error.message : 'Failed to save connection';
    } finally {
      this.connectionSaving = false;
    }
  }

  // Re-price unpriced usage rows in the current window from stored tokens and
  // the current price catalog/overrides, then reload the summary.
  private async handleReprice() {
    this.repricing = true;
    this.repriceNotice = null;
    try {
      const range = this.getDateParams();
      const previousUnpriced = this.summary?.unpriced_requests ?? 0;
      const result = await repriceCost({
        start_date: range.startDate,
        end_date: range.endDate,
        only_unpriced: true,
      });
      if (result.submitted_async) {
        this.repriceNotice =
          'Repricing is running in the background. Checking for results...';
        await this.pollRepriceOutcome(previousUnpriced);
        await this.load();
        const remaining = this.summary?.unpriced_requests ?? 0;
        this.repriceNotice = this.repriceOutcomeNotice(
          previousUnpriced,
          remaining
        );
      } else {
        this.repriceNotice =
          `Reprice finished: ${result.rows_updated ?? 0} of ` +
          `${result.rows_examined ?? 0} requests priced.`;
        await this.load();
      }
    } catch (error) {
      this.error =
        error instanceof Error ? error.message : 'Failed to reprice usage';
    } finally {
      this.repricing = false;
    }
  }

  // The async reprice path (windows over 7 days) acknowledges before anything
  // is scanned, so the response carries no counts. Poll the summary on a
  // bounded interval and stop early once the unpriced count moves; the notice
  // is computed from the reloaded summary afterwards, not from this poll.
  private async pollRepriceOutcome(previousUnpriced: number): Promise<void> {
    for (
      let attempt = 0;
      attempt < CostView.REPRICE_POLL_MAX_ATTEMPTS;
      attempt++
    ) {
      await new Promise((resolve) =>
        window.setTimeout(resolve, CostView.REPRICE_POLL_INTERVAL_MS)
      );
      try {
        const summary = await getCostAnalyticsSummary(this.getDateParams());
        if ((summary.unpriced_requests ?? 0) !== previousUnpriced) {
          return;
        }
      } catch {
        // A failed poll is not a failed reprice; keep watching.
      }
    }
  }

  // In a fixed window the unpriced count can only DECREASE via pricing, so a
  // lower remaining count is the precise partial-success signal even when
  // the job finished after the last poll. Live traffic can add unpriced rows
  // during the poll window, so an equal-or-higher count reads as no change
  // (a negative "requests priced" would be nonsense).
  private repriceOutcomeNotice(
    previousUnpriced: number,
    remaining: number
  ): string {
    if (remaining === 0) {
      return 'Reprice finished: every request in this window now has a cost estimate.';
    }
    if (remaining < previousUnpriced) {
      return (
        `Reprice finished: ${this.formatNumber(previousUnpriced - remaining)} ` +
        `requests priced, ${this.formatNumber(remaining)} still unpriced. ` +
        'Set a price override for the models named below, then reprice again.'
      );
    }
    return (
      'No change after checking for about a minute. If the models named ' +
      'below are missing from the price catalog, repricing cannot price ' +
      'them: set a price override (a $0 price is fine for free models), ' +
      'then reprice again.'
    );
  }

  // Route from the unpriced banner into the existing price-override dialog
  // with the highest-volume unpriced model pre-filled. A $0 override is
  // honored by repricing, which is the supported escape hatch for custom
  // models that will never appear in a shared price catalog. The backend
  // coalesces rows with no model alias to "unknown"; pre-filling that
  // placeholder would invite a no-op override, so the field stays empty.
  private openPriceOverrideForUnpriced() {
    const top = this.summary?.unpriced_models?.[0];
    if (top && top.model !== 'unknown') {
      this.priceModelAlias = top.model;
    }
    this.priceDialogOpen = true;
  }

  private formatCurrency(value?: number | null): string {
    const amount = Number(value || 0);
    if (amount === 0) return '$0.00';
    return amount >= 0.01 ? `$${amount.toFixed(2)}` : `$${amount.toFixed(4)}`;
  }

  private formatNumber(value?: number | null): string {
    return Number(value || 0).toLocaleString();
  }

  /**
   * Counts big enough to lose their shape are compact, as on the Overview
   * ("836.5M"), with the exact figure kept in a title attribute for anyone
   * who needs to read every digit.
   */
  private formatCompactNumber(value?: number | null): string {
    const amount = Number(value || 0);
    if (amount < 1000) return String(Math.round(amount));
    return new Intl.NumberFormat(undefined, {
      notation: 'compact',
      maximumFractionDigits: 1,
    }).format(amount);
  }

  /**
   * Which days the numbers cover, restated beside the range control. The
   * server's own window is preferred over the client's presets: the four
   * sibling pages disagree about "30 days", and this one prints what it was
   * actually given.
   */
  /** The short form of the selected range, for stat labels ("$ est. · 30d"). */
  private rangeChipLabel(): string {
    const option = DATE_RANGE_OPTIONS.find(
      (item) => item.value === this.selectedRange
    );
    return (option?.label || '30d').toLowerCase();
  }

  private rangeWindowLabel(): string {
    const params = this.getDateParams();
    const start = new Date(this.summary?.period_start || params.startDate);
    const end = new Date(this.summary?.period_end || params.endDate);
    if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime())) return '';
    const day = (date: Date) =>
      date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
    const window = `${day(start)} to ${day(end)}`;
    // Cost has no websocket subscription and no poll, so the numbers are as
    // old as the last load. A relative "updated just now" painted once would
    // still say "just now" an hour later; a clock time cannot go stale.
    const loaded = this.loadedAt ? new Date(this.loadedAt) : null;
    if (!loaded || Number.isNaN(loaded.getTime())) return window;
    const read = loaded.toLocaleTimeString(undefined, {
      hour: 'numeric',
      minute: '2-digit',
    });
    return `${window} · read ${read}`;
  }

  /** The full timestamp behind "read 14:02", for the label's tooltip. */
  private rangeWindowTitle(): string {
    const loaded = this.loadedAt ? new Date(this.loadedAt) : null;
    if (!loaded || Number.isNaN(loaded.getTime())) return '';
    return `Loaded ${loaded.toLocaleString()}`;
  }

  /**
   * A delta in the Overview's form: an arrow, a percentage, and the window it
   * is measured against. A dollar difference alone ("+$14.30") says nothing
   * about whether spend doubled or moved a percent.
   */
  private percentDelta(current: number, previous: number): string {
    if (!previous || previous <= 0) {
      return `No comparison for ${this.previousRangeLabel()}`;
    }
    const change = ((current - previous) / previous) * 100;
    if (Math.abs(change) < 0.5) {
      return `No change vs ${this.previousRangeLabel()}`;
    }
    const arrow = change > 0 ? '▲' : '▼';
    return `${arrow} ${Math.abs(Math.round(change))}% vs ${this.previousRangeLabel()}`;
  }

  private getProjectedPeriodCost(): number | null {
    if (!this.summary) return null;
    const now = new Date();
    if (this.selectedRange === 'today') {
      const dayStart = new Date(this.getDateParams('today').startDate);
      const elapsedHours = Math.max(
        (now.getTime() - dayStart.getTime()) / (60 * 60 * 1000),
        1
      );
      return (this.summary.estimated_cost / elapsedHours) * 24;
    }
    if (this.selectedRange === 'this-week') {
      const weekStart = new Date(this.getDateParams('this-week').startDate);
      const elapsedDays = Math.max(
        (now.getTime() - weekStart.getTime()) / (24 * 60 * 60 * 1000),
        1
      );
      return (this.summary.estimated_cost / elapsedDays) * 7;
    }
    if (this.selectedRange !== 'this-month') return null;
    const dayOfMonth = Math.max(now.getDate(), 1);
    const daysInMonth = new Date(
      now.getFullYear(),
      now.getMonth() + 1,
      0
    ).getDate();
    return (this.summary.estimated_cost / dayOfMonth) * daysInMonth;
  }

  private projectedPeriodLabel(): string {
    return this.selectedRange === 'this-week'
      ? 'Projected week'
      : this.selectedRange === 'today'
        ? 'Projected today'
        : 'Projected month';
  }

  private projectedPeriodComparisonDetail(projectedCost: number): string {
    const previousCost = this.previousRangeSummary?.estimated_cost;
    if (previousCost === null || previousCost === undefined) {
      return `Compared to ${this.previousRangeLabel()}`;
    }
    return this.percentDelta(projectedCost, previousCost);
  }

  // Named as on the Overview ("vs prior 30d"), not "previous 30 days": the
  // same comparison should read the same on both pages.
  private previousRangeLabel(): string {
    if (this.selectedRange === 'today') return 'same time yesterday';
    if (this.selectedRange === 'this-week') return 'prior week';
    if (this.selectedRange === 'this-month') return 'prior month';
    if (this.selectedRange === 'last-month') return 'month before';
    if (this.selectedRange === 'last-7') return 'prior 7d';
    if (this.selectedRange === 'last-90') return 'prior 90d';
    return 'prior 30d';
  }

  private spendComparisonDetail(): string {
    const previousCost = this.previousRangeSummary?.estimated_cost;
    if (previousCost === null || previousCost === undefined) {
      return `Compared to ${this.previousRangeLabel()}`;
    }
    return this.percentDelta(this.summary?.estimated_cost || 0, previousCost);
  }

  private renderSessionSubjects(row: GatewayUsageBySession) {
    const subjects = [];
    if (row.agent_id) {
      subjects.push(
        html`<a href=${`/console/agents/${encodeURIComponent(row.agent_id)}`}>
          ${row.agent_name || row.runtime_principal_name || 'Agent'}
        </a>`
      );
    } else if (
      row.runtime_principal_type === 'managed_agent' &&
      row.runtime_principal_id
    ) {
      subjects.push(
        html`<span>
          ${row.runtime_principal_name || row.runtime_principal_id}
        </span>`
      );
    }
    if (row.flow_id) {
      subjects.push(
        html`<a href=${`/console/flows/${encodeURIComponent(row.flow_id)}`}>
          ${row.flow_name || 'Flow'}
        </a>`
      );
    }
    return subjects.length
      ? html`<div class="subject-links">${subjects}</div>`
      : html`<span>n/a</span>`;
  }

  private renderSectionHeader(icon: string, title: string, action?: unknown) {
    return html`
      <div slot="header" class="section-header">
        <div>
          <div class="section-title">
            <sl-icon name=${icon}></sl-icon>
            <span>${title}</span>
          </div>
        </div>
        ${action}
      </div>
    `;
  }

  private async savePriceOverride() {
    if (!this.priceModelAlias) {
      this.error = 'Enter a model alias for the price override.';
      return;
    }
    const input = this.priceInput !== '' ? Number(this.priceInput) : null;
    const output = this.priceOutput !== '' ? Number(this.priceOutput) : null;
    const pricePer1k = this.pricePer1k !== '' ? Number(this.pricePer1k) : null;
    const requestPrice =
      this.requestPrice !== '' ? Number(this.requestPrice) : null;
    const discountPercent =
      this.discountPercent !== '' ? Number(this.discountPercent) : null;
    const prepaidTokens =
      this.prepaidTokens !== '' ? Number(this.prepaidTokens) : null;
    const prepaidCredit =
      this.prepaidCredit !== '' ? Number(this.prepaidCredit) : null;
    const hasPricing =
      input !== null ||
      output !== null ||
      pricePer1k !== null ||
      requestPrice !== null ||
      discountPercent !== null ||
      prepaidTokens !== null ||
      prepaidCredit !== null;
    if (!hasPricing) {
      this.error = 'Enter at least one pricing, discount, or prepaid value.';
      return;
    }
    const currency = (this.priceCurrency || 'USD').toUpperCase();
    const fxRate = this.priceFxRate !== '' ? Number(this.priceFxRate) : null;
    if (currency !== 'USD' && (!fxRate || fxRate <= 0)) {
      this.error =
        'Non-USD overrides need an FX rate to USD so costs can be recorded in USD.';
      return;
    }
    this.saving = true;
    this.error = null;
    try {
      const payload: ModelPriceOverrideCreate = {
        ai_model_id: null,
        provider_name: this.priceProvider || null,
        model_alias: this.priceModelAlias,
        currency,
        fx_rate_to_usd: currency !== 'USD' ? fxRate : null,
        input_price_per_1k: input,
        output_price_per_1k: output,
        cache_read_input_price_per_1k: null,
        cache_creation_input_price_per_1k: null,
        price_per_1k: pricePer1k,
        request_price: requestPrice,
        discount_percent: discountPercent,
        prepaid_token_balance: prepaidTokens,
        prepaid_credit_balance_usd: prepaidCredit,
        effective_from: null,
        effective_until: null,
        is_active: true,
        notes: null,
      };
      await createModelPriceOverride(payload);
      this.priceModelAlias = '';
      this.priceProvider = '';
      this.priceInput = '';
      this.priceOutput = '';
      this.pricePer1k = '';
      this.requestPrice = '';
      this.discountPercent = '';
      this.prepaidTokens = '';
      this.prepaidCredit = '';
      await this.load();
    } catch (error) {
      this.error =
        error instanceof Error
          ? error.message
          : 'Failed to save price override';
    } finally {
      this.saving = false;
    }
  }

  private renderMetrics() {
    const summary = this.summary;
    const projectedPeriodCost = this.getProjectedPeriodCost();
    return html`
      <div class="metric-grid" role="region" aria-label="Cost summary metrics">
        <div class="metric-card">
          <div class="metric-label">
            ${
              this.selectedRange === 'this-month'
                ? 'Month to date'
                : `$ est. · ${this.rangeChipLabel()}`
            }
          </div>
          <div class="metric-value">
            ${this.formatCurrency(summary?.estimated_cost)}
          </div>
          <div class="metric-detail">${this.spendComparisonDetail()}</div>
        </div>
        <div class="metric-card">
          <div class="metric-label">Requests · ${this.rangeChipLabel()}</div>
          <div
            class="metric-value"
            title=${this.formatNumber(summary?.total_requests)}
          >
            ${this.formatCompactNumber(summary?.total_requests)}
          </div>
          <div class="metric-detail">
            ${this.formatCompactNumber(summary?.successful_requests)} succeeded
          </div>
        </div>
        <div class="metric-card">
          <div class="metric-label">Tokens · ${this.rangeChipLabel()}</div>
          <div
            class="metric-value"
            title=${this.formatNumber(summary?.token_usage.total_tokens)}
          >
            ${this.formatCompactNumber(summary?.token_usage.total_tokens)}
          </div>
          <div class="metric-detail">
            ${this.formatCompactNumber(summary?.token_usage.prompt_tokens)}
            prompt ·
            ${this.formatCompactNumber(summary?.token_usage.completion_tokens)}
            completion
          </div>
        </div>
        ${
          projectedPeriodCost !== null
            ? html`
                <div class="metric-card">
                  <div class="metric-label">${this.projectedPeriodLabel()}</div>
                  <div class="metric-value">
                    ${this.formatCurrency(projectedPeriodCost)}
                  </div>
                  <div class="metric-detail">
                    ${this.projectedPeriodComparisonDetail(projectedPeriodCost)}
                  </div>
                </div>
              `
            : nothing
        }
      </div>
    `;
  }

  // Warning banner when usage rows carry tokens but no cost (model missing
  // from the price catalog at request time). Repricing re-derives their cost
  // from stored tokens against current prices/overrides (billing flag), and
  // the named models route into the price-override dialog for the models no
  // catalog will ever price.
  private renderUnpricedNotice() {
    const unpricedRequests = this.summary?.unpriced_requests ?? 0;
    if (!unpricedRequests) {
      return this.repriceNotice
        ? html`<sl-alert
            variant="success"
            open
            closable
            role="status"
            @sl-after-hide=${() => (this.repriceNotice = null)}
            >${this.repriceNotice}</sl-alert
          >`
        : nothing;
    }
    const unpricedModels = this.summary?.unpriced_models ?? [];
    return html`
      <sl-alert id="panel-pricing-catalog" variant="warning" open role="alert">
        <sl-icon slot="icon" name="exclamation-triangle"></sl-icon>
        ${this.formatNumber(unpricedRequests)}
        request${unpricedRequests === 1 ? '' : 's'}
        (${this.formatNumber(this.summary?.unpriced_tokens)} tokens) in this
        window have no cost estimate, so total spend is understated.
        ${
          unpricedModels.length
            ? html`<div class="unpriced-models">
                These models are missing from the price catalog, so their cost
                cannot be estimated automatically:
                ${unpricedModels.map(
                  (entry, index) =>
                    html`${index > 0 ? ', ' : ''}<code>${entry.model}</code>
                      (${this.formatNumber(entry.tokens)} tokens)`
                )}.
              </div>`
            : nothing
        }
        ${
          unpricedModels.length && this.modelPriceOverridesEnabled
            ? html`<div>
                Set a price override (a $0 price is fine for free models), then
                reprice.
              </div>`
            : nothing
        }
        ${
          this.billingEnabled
            ? html`<sl-button
                size="small"
                variant="warning"
                outline
                .loading=${this.repricing}
                @click=${() => void this.handleReprice()}
                >Reprice now</sl-button
              >`
            : nothing
        }
        ${
          unpricedModels.length && this.modelPriceOverridesEnabled
            ? html`<sl-button
                size="small"
                variant="warning"
                outline
                @click=${() => this.openPriceOverrideForUnpriced()}
                >Set price override</sl-button
              >`
            : nothing
        }
        ${this.repriceNotice ? html`<div>${this.repriceNotice}</div>` : nothing}
      </sl-alert>
    `;
  }

  // One quiet provenance line: which price snapshot produced the estimates.
  private renderCatalogInfo() {
    const catalog = this.summary?.price_catalog;
    if (!catalog?.fetched_at) return nothing;
    const fetched = new Date(catalog.fetched_at);
    const ageDays = Math.floor(
      (Date.now() - fetched.getTime()) / (24 * 60 * 60 * 1000)
    );
    // The date is spelled out rather than left in US numeric form, the age is
    // stated in days, and a stale catalog ends in something to click. There is
    // no endpoint that re-downloads the catalog on demand, so the action is
    // the one that actually fixes a wrong price: an override for this account.
    return html`
      <div class="metric-detail">
        Price catalog from
        ${fetched.toLocaleDateString(undefined, {
          month: 'short',
          day: 'numeric',
          year: 'numeric',
        })}
        (${catalog.model_count ?? '?'} models), ${ageDays}
        ${ageDays === 1 ? 'day' : 'days'} old.
        ${
          ageDays > 45
            ? html`<button
                type="button"
                class="catalog-action"
                @click=${() => {
                  this.priceDialogOpen = true;
                }}
              >
                Override a price
              </button>`
            : nothing
        }
      </div>
    `;
  }

  // Toggle sort for a tab: clicking the active column flips direction, a new
  // column selects it (defaulting to descending, the useful order for spend).
  private toggleSort(current: SortState, key: string): SortState {
    if (current.key === key) {
      return { key, dir: current.dir === 'asc' ? 'desc' : 'asc' };
    }
    return { key, dir: 'desc' };
  }

  // Generic comparison-based sort. Numeric columns compare numerically; text
  // columns use locale compare. Returns a new array (does not mutate input).
  private sortRows<T>(
    rows: readonly T[],
    columns: readonly SortColumn<T>[],
    sort: SortState
  ): T[] {
    const column = columns.find((col) => col.key === sort.key) || columns[0];
    if (!column) return [...rows];
    const factor = sort.dir === 'asc' ? 1 : -1;
    return [...rows].sort((a, b) => {
      const av = column.value(a);
      const bv = column.value(b);
      if (column.numeric) {
        return (Number(av) - Number(bv)) * factor;
      }
      return String(av).localeCompare(String(bv)) * factor;
    });
  }

  // Render a clickable, sortable column header with an active-column caret.
  private renderSortableHeader<T>(
    column: SortColumn<T>,
    sort: SortState,
    onSort: (key: string) => void
  ) {
    const active = sort.key === column.key;
    const caret = active
      ? html`<sl-icon
          name=${sort.dir === 'asc' ? 'caret-up-fill' : 'caret-down-fill'}
        ></sl-icon>`
      : nothing;
    return html`
      <th
        scope="col"
        class="sortable"
        role="button"
        tabindex="0"
        aria-sort=${
          active ? (sort.dir === 'asc' ? 'ascending' : 'descending') : 'none'
        }
        @click=${() => onSort(column.key)}
        @keydown=${(event: KeyboardEvent) => {
          if (event.key === 'Enter' || event.key === ' ') {
            event.preventDefault();
            onSort(column.key);
          }
        }}
      >
        <span class="sort-header">${column.label} ${caret}</span>
      </th>
    `;
  }

  private getSessionTitle(row: GatewayUsageBySession): string {
    return (
      row.title ||
      row.session_summary ||
      row.session_reference ||
      row.runtime_principal_name ||
      row.runtime_session_id ||
      'Session'
    );
  }

  // Group sessions for the Agents tab. Agent-backed sessions collapse into an
  // agent row (linked when agent_id is present), flow-backed sessions into a
  // flow row, and everything else into a single "Other" row.
  private buildAgentGroups(): AgentGroupRow[] {
    const sessions = this.summary?.usage_by_session || [];
    const groups = new Map<string, AgentGroupRow>();
    for (const session of sessions) {
      let key: string;
      let name: string;
      let agentId: string | null = null;
      let flowId: string | null = null;
      if (session.agent_id || session.agent_name) {
        agentId = session.agent_id || null;
        name = session.agent_name || session.agent_id || 'Agent';
        key = `agent:${session.agent_id || session.agent_name}`;
      } else if (session.flow_id || session.flow_name) {
        flowId = session.flow_id || null;
        name = session.flow_name || session.flow_id || 'Flow';
        key = `flow:${session.flow_id || session.flow_name}`;
      } else {
        name = 'Other';
        key = 'other';
      }
      const existing = groups.get(key) || {
        key,
        name,
        agentId,
        flowId,
        requests: 0,
        totalTokens: 0,
        cost: 0,
      };
      existing.requests += session.request_count || 0;
      existing.totalTokens += session.token_usage?.total_tokens || 0;
      existing.cost += session.estimated_cost || 0;
      groups.set(key, existing);
    }
    return [...groups.values()];
  }

  // Group sessions for the Users tab by resolved owner (via agent -> owner map).
  private buildUserGroups(): UserGroupRow[] {
    const sessions = this.summary?.usage_by_session || [];
    const groups = new Map<string, UserGroupRow>();
    for (const session of sessions) {
      const owner = this.resolveSessionOwner(session);
      const existing = groups.get(owner) || {
        username: owner,
        requests: 0,
        cost: 0,
      };
      existing.requests += session.request_count || 0;
      existing.cost += session.estimated_cost || 0;
      groups.set(owner, existing);
    }
    return [...groups.values()];
  }

  // Imported usage (issue #123): spend ingested from a provider's own export
  // (e.g. Cursor) rather than metered by the gateway. It is rendered as its
  // own section and never folded into the spend metrics, budgets, or the
  // per-agent/session/tool tabs above, which all describe gateway traffic.
  // Hidden entirely when the window holds no imported events.
  private renderImportedUsage() {
    const imported = this.summary?.imported_usage;
    if (!imported || !imported.event_count) return nothing;
    const columns: SortColumn<ImportedUsageByModel>[] = [
      {
        key: 'model',
        label: 'Model',
        numeric: false,
        value: (r) => r.model_alias || 'Unknown',
      },
      {
        key: 'source',
        label: 'Source',
        numeric: false,
        value: (r) => r.source || 'Unknown',
      },
      {
        key: 'requests',
        label: 'Events',
        numeric: true,
        value: (r) => r.request_count,
      },
      {
        key: 'tokens',
        label: 'Tokens',
        numeric: true,
        value: (r) => r.total_tokens,
      },
      {
        key: 'cost',
        label: 'Imported cost',
        numeric: true,
        value: (r) => r.imported_cost,
      },
      {
        key: 'last_event',
        label: 'Last event',
        numeric: true,
        value: (r) =>
          r.last_event_at ? new Date(r.last_event_at).getTime() : 0,
      },
    ];
    const rows = this.sortRows(
      imported.usage_by_model || [],
      columns,
      this.importedSort
    );
    return html`
      <sl-card class="analytics-card">
        ${this.renderSectionHeader(
          'box-arrow-in-down',
          'Imported usage',
          html`<sl-badge variant="neutral" pill>Not gateway metered</sl-badge>`
        )}
        <div class="imported-usage-note">
          Usage imported from a provider's own export. It is reported separately
          and is not counted in the spend metrics, budgets, or breakdowns above,
          which cover gateway-metered traffic only.
        </div>
        <div
          class="imported-usage-totals"
          role="region"
          aria-label="Imported usage totals"
        >
          <div>
            <div class="imported-usage-total-label">Imported events</div>
            <div class="imported-usage-total-value">
              ${this.formatNumber(imported.event_count)}
            </div>
          </div>
          <div>
            <div class="imported-usage-total-label">Imported tokens</div>
            <div class="imported-usage-total-value">
              ${this.formatNumber(imported.total_tokens)}
            </div>
          </div>
          <div>
            <div class="imported-usage-total-label">Imported cost</div>
            <div class="imported-usage-total-value">
              ${this.formatCurrency(imported.imported_cost)}
            </div>
          </div>
        </div>
        ${
          rows.length
            ? html`<div class="analytics-table-wrap">
                <table
                  class="styled-table"
                  aria-label="Imported usage by model"
                >
                  <thead>
                    <tr>
                      ${columns.map((column) =>
                        this.renderSortableHeader(
                          column,
                          this.importedSort,
                          (key) =>
                            (this.importedSort = this.toggleSort(
                              this.importedSort,
                              key
                            ))
                        )
                      )}
                    </tr>
                  </thead>
                  <tbody>
                    ${rows.map(
                      (row) => html`
                        <tr>
                          <td>${row.model_alias || 'Unknown'}</td>
                          <td>${row.source || 'Unknown'}</td>
                          <td>${this.formatNumber(row.request_count)}</td>
                          <td>${this.formatNumber(row.total_tokens)}</td>
                          <td>${this.formatCurrency(row.imported_cost)}</td>
                          <td>
                            ${
                              row.last_event_at
                                ? new Date(row.last_event_at).toLocaleString()
                                : '-'
                            }
                          </td>
                        </tr>
                      `
                    )}
                  </tbody>
                </table>
              </div>`
            : html`<div class="empty">No per-model imported usage yet.</div>`
        }
        ${this.renderImportedConversations(imported.usage_by_conversation ?? [])}
      </sl-card>
    `;
  }

  // Per-conversation rollup of imported usage. Subagent conversations
  // (parent_conversation_id) nest under the thread that spawned them.
  // Honesty rails: estimated and reconciled amounts stay in separate
  // columns — they are NEVER added into one number — and a null quantity
  // renders as "not reported", never as 0 or $0.00.
  private renderImportedConversations(rows: ImportedUsageByConversation[]) {
    if (!rows.length) return nothing;
    const threads = this.buildConversationThreads(rows);
    return html`
      <div class="imported-conversations-title">Conversations</div>
      <div class="imported-usage-note">
        Per-thread rollup of imported usage. Subagent conversations are nested
        under the conversation that spawned them. Estimated amounts (derived
        from hook or transcript data) and reconciled amounts (from a billing
        export) are shown separately and are never summed together.
      </div>
      <div class="analytics-table-wrap">
        <table class="styled-table" aria-label="Imported usage by conversation">
          <thead>
            <tr>
              <th scope="col">Conversation</th>
              <th scope="col">Events</th>
              <th scope="col">Tokens</th>
              <th scope="col">Estimated cost</th>
              <th scope="col">Reconciled cost</th>
              <th scope="col">Last event</th>
            </tr>
          </thead>
          <tbody>
            ${threads.map((thread) => this.renderConversationThread(thread))}
          </tbody>
        </table>
      </div>
    `;
  }

  // Group conversations into threads. A row nests under its parent only
  // when that parent is itself top-level (one nesting level); a deeper
  // descendant is promoted to top-level instead so no conversation can
  // ever disappear from the table.
  private buildConversationThreads(
    rows: ImportedUsageByConversation[]
  ): ImportedConversationThread[] {
    const byId = new Map(rows.map((row) => [row.conversation_id, row]));
    const hasKnownParent = (row: ImportedUsageByConversation): boolean => {
      const parent = row.parent_conversation_id;
      return !!parent && parent !== row.conversation_id && byId.has(parent);
    };
    const isNested = (row: ImportedUsageByConversation): boolean => {
      if (!hasKnownParent(row)) return false;
      const parent = byId.get(row.parent_conversation_id as string);
      return !!parent && !hasKnownParent(parent);
    };
    const childrenByParent = new Map<string, ImportedUsageByConversation[]>();
    const roots: ImportedUsageByConversation[] = [];
    for (const row of rows) {
      if (isNested(row)) {
        const parentId = row.parent_conversation_id as string;
        const list = childrenByParent.get(parentId) ?? [];
        list.push(row);
        childrenByParent.set(parentId, list);
      } else {
        roots.push(row);
      }
    }
    return roots.map((row) => ({
      row,
      children: childrenByParent.get(row.conversation_id) ?? [],
    }));
  }

  private renderConversationThread(thread: ImportedConversationThread) {
    const { row, children } = thread;
    if (!children.length) return this.renderConversationRow(row, false);
    const all = [row, ...children];
    const totalEvents = all.reduce((sum, r) => sum + (r.event_count || 0), 0);
    // Thread totals keep the two cost bases apart: an estimated total and a
    // reconciled total, never one combined figure.
    const totalTokens = this.sumReported(all.map((r) => r.total_tokens));
    const totalEstimated = this.sumReported(all.map((r) => r.estimated_cost));
    const totalReconciled = this.sumReported(all.map((r) => r.reconciled_cost));
    return html`
      ${this.renderConversationRow(row, false)}
      ${children.map((child) => this.renderConversationRow(child, true))}
      <tr class="conversation-thread-total">
        <td>Thread total (${all.length} conversations)</td>
        <td>${this.formatNumber(totalEvents)}</td>
        <td>${this.renderReportedNumber(totalTokens)}</td>
        <td>${this.renderReportedCurrency(totalEstimated)}</td>
        <td>${this.renderReportedCurrency(totalReconciled)}</td>
        <td></td>
      </tr>
    `;
  }

  private renderConversationRow(
    row: ImportedUsageByConversation,
    nested: boolean
  ) {
    return html`
      <tr>
        <td class=${nested ? 'conversation-child-cell' : ''}>
          ${
            nested
              ? html`<span class="conversation-child-marker" aria-hidden="true"
                  >&#8627;</span
                >`
              : nothing
          }
          ${row.conversation_id}
        </td>
        <td>${this.formatNumber(row.event_count)}</td>
        <td>${this.renderReportedNumber(row.total_tokens)}</td>
        <td>${this.renderReportedCurrency(row.estimated_cost)}</td>
        <td>${this.renderReportedCurrency(row.reconciled_cost)}</td>
        <td>
          ${
            row.last_event_at
              ? new Date(row.last_event_at).toLocaleString()
              : html`<span class="not-reported">not reported</span>`
          }
        </td>
      </tr>
    `;
  }

  // Null-preserving sum: null when every input is null ("not reported"),
  // so a missing value can never be laundered into a fabricated zero.
  private sumReported(values: Array<number | null | undefined>): number | null {
    const reported = values.filter(
      (value): value is number => value !== null && value !== undefined
    );
    if (!reported.length) return null;
    return reported.reduce((sum, value) => sum + value, 0);
  }

  private renderReportedNumber(value?: number | null) {
    return value === null || value === undefined
      ? html`<span class="not-reported">not reported</span>`
      : html`${this.formatNumber(value)}`;
  }

  private renderReportedCurrency(value?: number | null) {
    return value === null || value === undefined
      ? html`<span class="not-reported">not reported</span>`
      : html`${this.formatCurrency(value)}`;
  }

  private renderBreakdown() {
    return html`
      <sl-card class="analytics-card">
        <sl-tab-group
          @sl-tab-show=${(event: CustomEvent<{ name: string }>) =>
            void this.handleTabShow(event)}
        >
          <sl-tab slot="nav" panel="agents">Agents</sl-tab>
          <sl-tab slot="nav" panel="tools">Tools</sl-tab>
          <sl-tab slot="nav" panel="sessions">Sessions</sl-tab>
          <sl-tab slot="nav" panel="users">Users</sl-tab>
          ${
            this.reconciliationEnabled
              ? html`<sl-tab slot="nav" panel="reconciliation"
                  >Reconciliation</sl-tab
                >`
              : nothing
          }
          <sl-tab-panel name="agents">${this.renderAgentsTab()}</sl-tab-panel>
          <sl-tab-panel name="tools">${this.renderToolsTab()}</sl-tab-panel>
          <sl-tab-panel name="sessions"
            >${this.renderSessionsTab()}</sl-tab-panel
          >
          <sl-tab-panel name="users">${this.renderUsersTab()}</sl-tab-panel>
          ${
            this.reconciliationEnabled
              ? html`<sl-tab-panel name="reconciliation"
                  >${this.renderReconciliationTab()}</sl-tab-panel
                >`
              : nothing
          }
        </sl-tab-group>
      </sl-card>
    `;
  }

  // Drift badge: green when |drift| < 5% of provider cost, amber < 15%, red
  // beyond — matching the product's semantic state colors.
  private renderDriftBadge(row: CostReconciliationRow) {
    if (row.drift_pct === null) {
      return html`<sl-badge variant="neutral">n/a</sl-badge>`;
    }
    const magnitude = Math.abs(row.drift_pct);
    const variant =
      magnitude < 5 ? 'success' : magnitude < 15 ? 'warning' : 'danger';
    return html`<sl-badge variant=${variant}
      >${row.drift_pct > 0 ? '+' : ''}${row.drift_pct.toFixed(1)}%</sl-badge
    >`;
  }

  private renderConnectionSetup() {
    return html`
      <div class="tab-panel-body">
        <sl-alert variant="neutral" open role="status">
          <sl-icon slot="icon" name="link-45deg"></sl-icon>
          Connect a provider's billing API to compare Preloop's estimated spend
          against the amounts the provider actually reports. Requires an
          organization <strong>admin</strong> API key (not an inference key);
          the key is stored encrypted and never shown again.
        </sl-alert>
        <div class="form-grid" style="margin-top: var(--sl-spacing-medium);">
          <sl-select
            label="Provider"
            .value=${this.connectionProvider}
            @sl-change=${(event: Event) =>
              (this.connectionProvider = (
                event.target as HTMLSelectElement
              ).value)}
          >
            <sl-option value="openai">OpenAI</sl-option>
            <sl-option value="anthropic">Anthropic</sl-option>
          </sl-select>
          <sl-input
            label="Admin API key"
            type="password"
            password-toggle
            .value=${this.connectionAdminKey}
            @sl-input=${(event: Event) =>
              (this.connectionAdminKey = (
                event.target as HTMLInputElement
              ).value)}
          ></sl-input>
          <sl-button
            variant="primary"
            .loading=${this.connectionSaving}
            @click=${() => void this.saveProviderConnection()}
          >
            <sl-icon slot="prefix" name="plus"></sl-icon>
            Connect provider
          </sl-button>
        </div>
      </div>
    `;
  }

  private renderReconciliationTab() {
    if (this.reconciliationLoading) {
      return html`<div class="loading-state" role="status" aria-busy="true">
        <sl-spinner></sl-spinner>
        <span>Loading reconciliation...</span>
      </div>`;
    }
    const errorAlert = this.reconciliationError
      ? html`<sl-alert variant="danger" open role="alert"
          >${this.reconciliationError}</sl-alert
        >`
      : nothing;
    if (!this.providerConnections.length) {
      return html`${errorAlert}${this.renderConnectionSetup()}`;
    }
    const rows = this.reconciliation?.rows ?? [];
    const totals = this.reconciliation;
    return html`
      <div class="tab-panel-body">
        ${errorAlert}
        <div class="policy-summary-row">
          <span class="policy-summary-label">Connected providers</span>
          <span class="policy-summary-value">
            ${this.providerConnections
              .map(
                (connection) =>
                  `${connection.provider}${
                    connection.last_error ? ' (sync error)' : ''
                  }`
              )
              .join(', ')}
          </span>
          <sl-button
            size="small"
            @click=${async () => {
              for (const connection of this.providerConnections) {
                await syncProviderBillingConnection(connection.id).catch(
                  () => undefined
                );
              }
              await this.loadReconciliation();
            }}
          >
            <sl-icon slot="prefix" name="arrow-repeat"></sl-icon>
            Sync now
          </sl-button>
        </div>
        ${
          rows.length
            ? html`
                <div class="analytics-table-wrap">
                  <table
                    class="styled-table"
                    aria-label="Estimated vs provider-reported spend"
                  >
                    <thead>
                      <tr>
                        <th scope="col">Date</th>
                        <th scope="col">Provider</th>
                        <th scope="col">Preloop estimate</th>
                        <th scope="col">Provider actual</th>
                        <th scope="col">Drift</th>
                      </tr>
                    </thead>
                    <tbody>
                      ${rows.map(
                        (row) => html`
                          <tr>
                            <td>${new Date(row.date).toLocaleDateString()}</td>
                            <td>${row.provider}</td>
                            <td>${this.formatCurrency(row.preloop_cost)}</td>
                            <td>${this.formatCurrency(row.provider_cost)}</td>
                            <td>${this.renderDriftBadge(row)}</td>
                          </tr>
                        `
                      )}
                    </tbody>
                    ${
                      totals
                        ? html`<tfoot>
                            <tr>
                              <th scope="row" colspan="2">Total</th>
                              <td>
                                ${this.formatCurrency(
                                  totals.total_preloop_cost
                                )}
                              </td>
                              <td>
                                ${this.formatCurrency(
                                  totals.total_provider_cost
                                )}
                              </td>
                              <td>
                                ${
                                  totals.total_drift_pct !== null
                                    ? `${totals.total_drift_pct > 0 ? '+' : ''}${totals.total_drift_pct.toFixed(1)}%`
                                    : 'n/a'
                                }
                              </td>
                            </tr>
                          </tfoot>`
                        : nothing
                    }
                  </table>
                </div>
                <div class="metric-detail">
                  Positive drift means the provider reported more spend than
                  Preloop estimated (e.g. traffic outside the gateway or
                  unpriced models). Provider buckets can settle up to 24h late.
                </div>
              `
            : html`<div class="empty">
                No provider billing data in this window yet. Run "Sync now" or
                wait for the daily ingestion.
              </div>`
        }
        ${this.renderConnectionSetup()}
      </div>
    `;
  }

  private renderAgentsTab() {
    const columns: SortColumn<AgentGroupRow>[] = [
      { key: 'name', label: 'Agent', numeric: false, value: (r) => r.name },
      {
        key: 'requests',
        label: 'Requests',
        numeric: true,
        value: (r) => r.requests,
      },
      {
        key: 'tokens',
        label: 'Tokens',
        numeric: true,
        value: (r) => r.totalTokens,
      },
      { key: 'cost', label: 'Cost', numeric: true, value: (r) => r.cost },
    ];
    const rows = this.sortRows(
      this.buildAgentGroups(),
      columns,
      this.agentSort
    );
    if (!rows.length) {
      return html`<div class="empty">No agent-attributed usage yet.</div>`;
    }
    return html`
      <div class="tab-panel-body">
        <div class="analytics-table-wrap">
          <table class="styled-table" aria-label="Spend by agent">
            <thead>
              <tr>
                ${columns.map((column) =>
                  this.renderSortableHeader(
                    column,
                    this.agentSort,
                    (key) =>
                      (this.agentSort = this.toggleSort(this.agentSort, key))
                  )
                )}
              </tr>
            </thead>
            <tbody>
              ${rows.map(
                (row) => html`
                  <tr>
                    <td>
                      ${
                        row.agentId
                          ? html`<a
                              href=${`/console/agents/${encodeURIComponent(row.agentId)}`}
                              >${row.name}</a
                            >`
                          : row.flowId
                            ? html`<a
                                href=${`/console/flows/${encodeURIComponent(row.flowId)}`}
                                >${row.name}</a
                              >`
                            : row.name
                      }
                    </td>
                    <td>${this.formatNumber(row.requests)}</td>
                    <td>${this.formatNumber(row.totalTokens)}</td>
                    <td>${this.formatCurrency(row.cost)}</td>
                  </tr>
                `
              )}
            </tbody>
          </table>
        </div>
      </div>
    `;
  }

  // Per-definition (per-injection) schema cost for a tool: the whole tool's
  // schema-injection cost amortized across each time its definition was injected
  // into a request. `estimated_schema_cost` is the tool's Total cost (see E).
  private perDefinitionCost(row: GatewayUsageByTool): number {
    return row.estimated_schema_cost / Math.max(row.schema_injections, 1);
  }

  private renderToolsTab() {
    const columns: SortColumn<GatewayUsageByTool>[] = [
      {
        key: 'tool',
        label: 'Tool',
        numeric: false,
        value: (r) => r.tool_name,
      },
      {
        key: 'invocations',
        label: 'Invocations',
        numeric: true,
        value: (r) => r.invocation_count,
      },
      {
        key: 'failed',
        label: 'Failed',
        numeric: true,
        value: (r) => r.failed_invocations,
      },
      {
        key: 'schema_tokens',
        label: 'Schema tokens',
        numeric: true,
        value: (r) => r.schema_tokens_total,
      },
      {
        key: 'total_cost',
        label: 'Total cost',
        numeric: true,
        value: (r) => r.estimated_schema_cost,
      },
      {
        key: 'definition_cost',
        label: 'Definition cost',
        numeric: true,
        value: (r) => this.perDefinitionCost(r),
      },
      {
        key: 'avg_cost',
        label: 'Avg / invocation',
        numeric: true,
        value: (r) => r.avg_cost_per_invocation,
      },
    ];
    const rows = this.sortRows(
      this.summary?.usage_by_tool || [],
      columns,
      this.toolSort
    );
    return html`
      <div class="tab-panel-body">
        ${this.renderToolCostFlagsNotice()}
        ${
          rows.length
            ? html`
                <div class="analytics-table-wrap">
                  <table class="styled-table" aria-label="Tool schema cost">
                    <thead>
                      <tr>
                        ${columns.map((column) =>
                          this.renderSortableHeader(
                            column,
                            this.toolSort,
                            (key) =>
                              (this.toolSort = this.toggleSort(
                                this.toolSort,
                                key
                              ))
                          )
                        )}
                      </tr>
                    </thead>
                    <tbody>
                      ${rows.map(
                        (row) => html`
                          <tr>
                            <td>
                              <div>${row.tool_name}</div>
                              ${
                                row.server_name
                                  ? html`<div class="cell-subtitle">
                                      ${row.server_name}
                                    </div>`
                                  : nothing
                              }
                            </td>
                            <td>${this.formatNumber(row.invocation_count)}</td>
                            <td>
                              ${this.formatNumber(row.failed_invocations)}
                            </td>
                            <td>
                              ${this.formatNumber(row.schema_tokens_total)}
                            </td>
                            <td>
                              ${this.formatCurrency(row.estimated_schema_cost)}
                            </td>
                            <td>
                              ${this.formatCurrency(this.perDefinitionCost(row))}
                            </td>
                            <td>
                              ${this.formatCurrency(row.avg_cost_per_invocation)}
                            </td>
                          </tr>
                        `
                      )}
                    </tbody>
                  </table>
                </div>
              `
            : html`<div class="empty">No tool usage recorded yet.</div>`
        }
      </div>
    `;
  }

  private renderSessionsTab() {
    const columns: SortColumn<GatewayUsageBySession>[] = [
      {
        key: 'session',
        label: 'Session',
        numeric: false,
        value: (r) => this.getSessionTitle(r),
      },
      {
        key: 'owner',
        label: 'Owner / agent',
        numeric: false,
        value: (r) => r.agent_name || r.flow_name || '—',
      },
      {
        key: 'requests',
        label: 'Requests',
        numeric: true,
        value: (r) => r.request_count,
      },
      {
        key: 'tokens',
        label: 'Tokens',
        numeric: true,
        value: (r) => r.token_usage?.total_tokens || 0,
      },
      {
        key: 'cost',
        label: 'Cost',
        numeric: true,
        value: (r) => r.estimated_cost,
      },
      {
        key: 'last_activity',
        label: 'Last activity',
        numeric: true,
        value: (r) =>
          r.last_activity_at || r.last_request_at
            ? new Date(
                (r.last_activity_at || r.last_request_at) as string
              ).getTime()
            : 0,
      },
    ];
    const rows = this.sortRows(
      this.summary?.usage_by_session || [],
      columns,
      this.sessionSort
    );
    if (!rows.length) {
      return html`<div class="empty">No session-attributed usage yet.</div>`;
    }
    return html`
      <div class="tab-panel-body">
        <div class="analytics-table-wrap">
          <table class="styled-table" aria-label="Session spend">
            <thead>
              <tr>
                ${columns.map((column) =>
                  this.renderSortableHeader(
                    column,
                    this.sessionSort,
                    (key) =>
                      (this.sessionSort = this.toggleSort(
                        this.sessionSort,
                        key
                      ))
                  )
                )}
              </tr>
            </thead>
            <tbody>
              ${rows.map(
                (row) => html`
                  <tr>
                    <td>
                      ${
                        row.runtime_session_id
                          ? html`<a
                              href="/console/runtime-sessions?sessionId=${row.runtime_session_id}"
                              >${this.getSessionTitle(row)}</a
                            >`
                          : this.getSessionTitle(row)
                      }
                    </td>
                    <td>${this.renderSessionSubjects(row)}</td>
                    <td>${this.formatNumber(row.request_count)}</td>
                    <td>${this.formatNumber(row.token_usage?.total_tokens)}</td>
                    <td>${this.formatCurrency(row.estimated_cost)}</td>
                    <td>
                      ${
                        row.last_activity_at || row.last_request_at
                          ? new Date(
                              (row.last_activity_at ||
                                row.last_request_at) as string
                            ).toLocaleString()
                          : '—'
                      }
                    </td>
                  </tr>
                `
              )}
            </tbody>
          </table>
        </div>
      </div>
    `;
  }

  private renderUsersTab() {
    const columns: SortColumn<UserGroupRow>[] = [
      {
        key: 'username',
        label: 'User',
        numeric: false,
        value: (r) => r.username,
      },
      {
        key: 'requests',
        label: 'Requests',
        numeric: true,
        value: (r) => r.requests,
      },
      { key: 'cost', label: 'Cost', numeric: true, value: (r) => r.cost },
    ];
    const rows = this.sortRows(this.buildUserGroups(), columns, this.userSort);
    if (!rows.length) {
      return html`<div class="empty">No user-attributed usage yet.</div>`;
    }
    return html`
      <div class="tab-panel-body">
        ${
          !this.ownerAttributionAvailable
            ? html`<sl-alert
                variant="neutral"
                open
                class="tools-notice"
                role="status"
              >
                <sl-icon slot="icon" name="info-circle"></sl-icon>
                Owner attribution unavailable: sessions are grouped as
                Unattributed.
              </sl-alert>`
            : nothing
        }
        <div class="analytics-table-wrap">
          <table class="styled-table" aria-label="Spend by user">
            <thead>
              <tr>
                ${columns.map((column) =>
                  this.renderSortableHeader(
                    column,
                    this.userSort,
                    (key) =>
                      (this.userSort = this.toggleSort(this.userSort, key))
                  )
                )}
              </tr>
            </thead>
            <tbody>
              ${rows.map(
                (row) => html`
                  <tr>
                    <td>${row.username}</td>
                    <td>${this.formatNumber(row.requests)}</td>
                    <td>${this.formatCurrency(row.cost)}</td>
                  </tr>
                `
              )}
            </tbody>
          </table>
        </div>
      </div>
    `;
  }

  private renderBudgets() {
    return html`
      <budget-health-card
        .summary=${this.summary}
        .policies=${this.budgetPolicies}
        .configurable=${this.billingEnabled}
        .timeRange=${'month'}
        @configure=${() => (this.budgetDialogOpen = true)}
      ></budget-health-card>
    `;
  }

  private renderPricing() {
    const overrides = this.pricingOverrides;
    return html`
      <sl-card id="panel-pricing">
        ${this.renderSectionHeader('tags', 'Pricing overrides')}
        <div class="action-card-body">
          <div>
            Use negotiated rates or credits when the default provider estimate
            is not the price your account actually pays.
          </div>
          <div class="policy-summary-row">
            <span class="policy-summary-label">Active overrides</span>
            <span class="policy-summary-value"
              >${this.formatNumber(overrides.length)}</span
            >
          </div>
          <sl-button
            variant="primary"
            @click=${() => (this.priceDialogOpen = true)}
          >
            <sl-icon slot="prefix" name="plus"></sl-icon>
            Add price override
          </sl-button>
        </div>
      </sl-card>
    `;
  }

  private renderControls() {
    return html`
      <div class="actions-stack">
        ${this.billingEnabled ? this.renderBudgets() : null}
        ${this.modelPriceOverridesEnabled ? this.renderPricing() : null}
      </div>
    `;
  }

  // Expensive-tool-definition insight, shown inside the Tools tab. When there
  // are flags we keep the richer <tool-cost-flags-panel>; when there are none
  // we degrade to a single-line muted notice rather than a full standalone card.
  private renderToolCostFlagsNotice() {
    if (this.toolFlagCount > 0) {
      return html`
        <div class="tool-cost-flags-section">
          <div
            class="section-title"
            style="margin-bottom: var(--sl-spacing-small);"
          >
            <sl-icon name="exclamation-triangle"></sl-icon>
            <span>Expensive tool definitions</span>
          </div>
          <tool-cost-flags-panel></tool-cost-flags-panel>
        </div>
      `;
    }
    return html`
      <sl-alert variant="neutral" open class="tools-notice" role="status">
        <sl-icon slot="icon" name="info-circle"></sl-icon>
        No expensive tool definitions flagged: your agents' tool schemas look
        efficient. Sort by total cost or schema tokens to spot the priciest
        tools.
      </sl-alert>
    `;
  }

  private renderPriceOverrideDialog() {
    return html`
      <sl-dialog
        label="Add price override"
        ?open=${this.priceDialogOpen}
        @sl-after-hide=${(event: Event) => {
          if (event.target === event.currentTarget) {
            this.priceDialogOpen = false;
          }
        }}
      >
        <p class="dialog-description">
          Override model prices when your Enterprise account has negotiated
          rates, credits, or provider-specific billing terms.
        </p>
        <div class="form-grid">
          <sl-input
            label="Model alias"
            placeholder="openai/gpt-4o"
            list="cost-model-aliases"
            .value=${this.priceModelAlias}
            @sl-input=${(event: Event) =>
              (this.priceModelAlias = (event.target as HTMLInputElement).value)}
          ></sl-input>
          <datalist id="cost-model-aliases">
            ${this.aiModels.map((model) => {
              const gateway = model.meta_data?.gateway as
                Record<string, unknown> | undefined;
              const alias =
                typeof gateway?.model_alias === 'string'
                  ? gateway.model_alias
                  : model.model_identifier;
              return html`<option value=${alias}></option>`;
            })}
          </datalist>
          <sl-input
            label="Provider"
            placeholder="openai"
            .value=${this.priceProvider}
            @sl-input=${(event: Event) =>
              (this.priceProvider = (event.target as HTMLInputElement).value)}
          ></sl-input>
          <sl-select
            label="Override type"
            .value=${this.priceMode}
            @sl-change=${(event: Event) =>
              (this.priceMode = (event.target as HTMLSelectElement).value as
                | 'custom_token_price'
                | 'fixed_request_price'
                | 'discount'
                | 'prepaid_tokens'
                | 'prepaid_credit')}
          >
            <sl-option value="custom_token_price">Custom token price</sl-option>
            <sl-option value="fixed_request_price"
              >Fixed request price / free usage</sl-option
            >
            <sl-option value="discount">Discount off list price</sl-option>
            <sl-option value="prepaid_tokens">Prepaid token balance</sl-option>
            <sl-option value="prepaid_credit">Prepaid dollar credit</sl-option>
          </sl-select>
          ${
            this.priceMode === 'custom_token_price'
              ? html`
                  <sl-input
                    label="Input $ / 1K"
                    type="number"
                    min="0"
                    step="0.0001"
                    .value=${this.priceInput}
                    @sl-input=${(event: Event) =>
                      (this.priceInput = (
                        event.target as HTMLInputElement
                      ).value)}
                  ></sl-input>
                  <sl-input
                    label="Output $ / 1K"
                    type="number"
                    min="0"
                    step="0.0001"
                    .value=${this.priceOutput}
                    @sl-input=${(event: Event) =>
                      (this.priceOutput = (
                        event.target as HTMLInputElement
                      ).value)}
                  ></sl-input>
                  <sl-input
                    label="Blended $ / 1K (optional)"
                    type="number"
                    min="0"
                    step="0.0001"
                    .value=${this.pricePer1k}
                    @sl-input=${(event: Event) =>
                      (this.pricePer1k = (
                        event.target as HTMLInputElement
                      ).value)}
                  ></sl-input>
                `
              : nothing
          }
          ${
            this.priceMode === 'fixed_request_price'
              ? html`
                  <sl-input
                    label="Request price"
                    help-text="Use 0 for free usage."
                    type="number"
                    min="0"
                    step="0.0001"
                    .value=${this.requestPrice}
                    @sl-input=${(event: Event) =>
                      (this.requestPrice = (
                        event.target as HTMLInputElement
                      ).value)}
                  ></sl-input>
                `
              : nothing
          }
          ${
            this.priceMode === 'discount'
              ? html`
                  <sl-input
                    label="Discount percent"
                    type="number"
                    min="0"
                    max="100"
                    step="0.01"
                    .value=${this.discountPercent}
                    @sl-input=${(event: Event) =>
                      (this.discountPercent = (
                        event.target as HTMLInputElement
                      ).value)}
                  ></sl-input>
                `
              : nothing
          }
          ${
            this.priceMode === 'prepaid_tokens'
              ? html`
                  <sl-input
                    label="Prepaid token balance"
                    type="number"
                    min="0"
                    step="1"
                    .value=${this.prepaidTokens}
                    @sl-input=${(event: Event) =>
                      (this.prepaidTokens = (
                        event.target as HTMLInputElement
                      ).value)}
                  ></sl-input>
                `
              : nothing
          }
          ${
            this.priceMode === 'prepaid_credit'
              ? html`
                  <sl-input
                    label="Prepaid credit balance"
                    type="number"
                    min="0"
                    step="0.01"
                    .value=${this.prepaidCredit}
                    @sl-input=${(event: Event) =>
                      (this.prepaidCredit = (
                        event.target as HTMLInputElement
                      ).value)}
                  ></sl-input>
                `
              : nothing
          }
          <sl-input
            label="Currency"
            maxlength="3"
            help-text=${
              this.priceCurrency.toUpperCase() !== 'USD'
                ? 'Non-USD prices are converted to USD when recorded.'
                : ''
            }
            .value=${this.priceCurrency}
            @sl-input=${(event: Event) =>
              (this.priceCurrency = (event.target as HTMLInputElement).value)}
          ></sl-input>
          ${
            this.priceCurrency.toUpperCase() !== 'USD'
              ? html`
                  <sl-input
                    label="FX rate to USD"
                    help-text="How many USD one ${this.priceCurrency.toUpperCase()} is worth (contract rate). Required for non-USD overrides."
                    type="number"
                    min="0"
                    step="0.0001"
                    .value=${this.priceFxRate}
                    @sl-input=${(event: Event) =>
                      (this.priceFxRate = (
                        event.target as HTMLInputElement
                      ).value)}
                  ></sl-input>
                `
              : nothing
          }
        </div>
        <div slot="footer">
          <sl-button @click=${() => (this.priceDialogOpen = false)}>
            Cancel
          </sl-button>
          <sl-button
            variant="primary"
            .loading=${this.saving}
            @click=${async () => {
              await this.savePriceOverride();
              if (!this.error) {
                this.priceDialogOpen = false;
              }
            }}
          >
            Save override
          </sl-button>
        </div>
      </sl-dialog>
    `;
  }

  private renderDialogs() {
    return html`
      ${
        this.billingEnabled
          ? html`
              <sl-dialog
                label="Configure budget limits"
                ?open=${this.budgetDialogOpen}
                @sl-after-hide=${(event: Event) => {
                  if (event.target === event.currentTarget) {
                    this.budgetDialogOpen = false;
                  }
                }}
              >
                ${
                  this.budgetDialogOpen
                    ? html`
                        <budget-policy-editor
                          billingEnabled
                          @budget-policies-changed=${this.handleBudgetPoliciesChanged}
                        ></budget-policy-editor>
                      `
                    : nothing
                }
                <div slot="footer">
                  <sl-button @click=${() => (this.budgetDialogOpen = false)}>
                    Close
                  </sl-button>
                </div>
              </sl-dialog>
            `
          : null
      }
      ${
        this.modelPriceOverridesEnabled
          ? this.renderPriceOverrideDialog()
          : null
      }
    `;
  }

  render() {
    // A slower call never blanks a card that already has an answer: the first
    // load gets the spinner, a range change keeps the previous numbers at 60%
    // with aria-busy until the new ones arrive.
    const hasAnswer = this.summary !== null;
    const updating = this.loading && hasAnswer;
    return html`
      <div class="page">
        <view-header
          headerText="Cost"
          description="Understand gateway spend by agent, tool, session and user."
        ></view-header>

        <div class="toolbar">
          <time-range-select
            ariaLabel="Cost date range"
            .value=${this.selectedRange}
            .options=${DATE_RANGE_OPTIONS}
            @range-change=${this.handleRangeChange}
          ></time-range-select>
          <span class="range-window" title=${this.rangeWindowTitle()}
            >${this.rangeWindowLabel()}</span
          >
        </div>

        ${
          this.error
            ? html`<sl-alert
                variant="danger"
                open
                role="alert"
                aria-live="assertive"
                >${this.error}</sl-alert
              >`
            : null
        }
        ${
          this.loading && !hasAnswer
            ? html`<sl-card>
                <div
                  class="loading-state"
                  role="status"
                  aria-live="polite"
                  aria-busy="true"
                >
                  <sl-spinner></sl-spinner>
                  <span>Loading cost analytics...</span>
                </div>
              </sl-card>`
            : html`
                <div
                  class="results ${updating ? 'is-updating' : ''}"
                  aria-busy=${updating ? 'true' : 'false'}
                >
                  ${this.renderMetrics()} ${this.renderCatalogInfo()}
                  ${this.renderUnpricedNotice()}
                  <div class="column-layout dashboard extra-wide">
                    <div class="main-column">
                      ${this.renderBreakdown()} ${this.renderImportedUsage()}
                    </div>
                    <div class="side-column">${this.renderControls()}</div>
                  </div>
                </div>
              `
        }
        ${this.renderDialogs()}
      </div>
    `;
  }
}
