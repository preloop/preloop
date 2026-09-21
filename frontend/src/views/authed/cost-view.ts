import { html, css, unsafeCSS, nothing } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import {
  AuthedElement,
  createModelPriceOverride,
  createProviderBillingConnection,
  deleteModelPriceOverride,
  updateModelPriceOverride,
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
  type CostUsageBreakdown,
} from '../../api';
import type {
  AIModel,
  CostAnalyticsSummaryResponse,
  CostReconciliationResponse,
  CostReconciliationRow,
  GatewayTokenUsage,
  GatewayUsageBySession,
  GatewayUsageByTool,
  ImportedUsageByConversation,
  ImportedUsageByModel,
  ModelPriceOverride,
  ModelPriceOverrideCreate,
  ProviderBillingConnection,
} from '../../types';
import consoleStyles from '../../styles/console-styles.css?inline';
import {
  resolvePreviousTimeRange,
  resolveTimeRange,
  timeRangeShortLabel,
} from '../../utils/time-range';
import '../../components/view-header.ts';
import { formatProviderLookupSummary } from '../../components/reprice-job-status';
import '../../components/time-range-select.ts';
import '../../components/budget-policy-editor.ts';
import '../../components/budget-health-card.ts';
import '../../components/tool-cost-flags-panel.ts';
import '../../components/token-figures.ts';
import { sumTokenUsage } from '../../components/token-figures';
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

// Aggregated row for the Agents tab (agent sessions or full flow totals).
type AgentGroupRow = {
  key: string;
  name: string;
  agentId: string | null;
  flowId: string | null;
  requests: number;
  totalTokens: number;
  /** The in/out/cache split behind `totalTokens`, or null when unmeasured. */
  tokenUsage: GatewayTokenUsage | null;
  cost: number;
};

// Aggregated row for the Users tab (grouped sessions by resolved owner).
type UserGroupRow = {
  username: string;
  requests: number;
  /** The in/out/cache split behind the row, or null when unmeasured. */
  tokenUsage: GatewayTokenUsage | null;
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
  // The override being edited, or null when the dialog is creating one. The
  // row itself is kept so a save preserves the fields the dialog does not
  // show (cache rates, effective dates, notes) instead of nulling them.
  @state() private priceEditOverride: ModelPriceOverride | null = null;
  // The override a confirm dialog is asking about, and what came of it.
  @state() private overrideRemoveTarget: ModelPriceOverride | null = null;
  @state() private overrideRemoving = false;
  @state() private overrideActionError: string | null = null;
  @state() private overrideRemoved: ModelPriceOverride | null = null;
  // What the override dialog itself has to say: a refusal or a validation
  // complaint belongs in the modal, not in the page banner behind it.
  @state() private priceFormError: string | null = null;
  // Reprice action (billing flag): re-derives cost for unpriced rows in the
  // selected window from stored tokens and current prices.
  @state() private repricing = false;
  @state() private repriceJobId: string | null = null;
  @state() private repricePending = false;
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
  @state() private activeTab = 'agents';
  @state() private sectionStates: Record<
    string,
    'loading' | 'ready' | 'error'
  > = {};
  @state() private sectionErrors: Record<string, string> = {};
  @state() private contextLoading = true;
  @state() private contextError: string | null = null;
  @state() private budgetContextReady = false;
  @state() private pricingContextReady = false;
  private loadGeneration = 0;
  private currentPeriod: DateRangeParams | null = null;
  private readyBreakdowns = new Set<CostUsageBreakdown>();
  private pendingBreakdowns = new Map<CostUsageBreakdown, Promise<void>>();
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

      /* An override that is off or out of its window still belongs in the
         table: it explains a past cost. It is dimmed, not hidden. */
      tr.override-inactive td {
        opacity: 0.6;
      }

      .override-model-meta {
        color: var(--sl-color-neutral-500);
        font-size: var(--sl-font-size-x-small);
        display: flex;
        align-items: center;
        gap: var(--sl-spacing-2x-small);
      }

      .override-model-link {
        color: var(--sl-color-primary-600);
        text-decoration: none;
      }

      .override-model-link:hover {
        text-decoration: underline;
      }

      .override-notes {
        max-width: 18ch;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }

      .override-actions {
        white-space: nowrap;
        display: flex;
        gap: var(--sl-spacing-2x-small);
      }

      .override-empty {
        color: var(--sl-color-neutral-500);
      }

      .override-error {
        color: var(--sl-color-danger-700);
      }

      .override-removed-notice {
        color: var(--sl-color-neutral-700);
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

  disconnectedCallback() {
    ++this.loadGeneration;
    super.disconnectedCallback();
  }

  /**
   * `?panel=pricing` is how an attention item about unpriced models lands on
   * the part of this page that fixes it, instead of at the top of a long page
   * with no hint where to look.
   */
  protected updated(): void {
    if (
      this.loading ||
      !this.requestedPanel ||
      (this.requestedPanel === 'pricing' && !this.pricingContextReady)
    ) {
      return;
    }
    const panel = this.requestedPanel;
    const target =
      this.renderRoot.querySelector(`#panel-${panel}`) ||
      this.renderRoot.querySelector('#panel-pricing-catalog');
    if (!target) return;
    this.requestedPanel = null;
    target.scrollIntoView({ behavior: 'smooth', block: 'center' });
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

  /**
   * Cost asks the shared range math, so its "30d" is the same 30 days the
   * Overview, API usage and the model detail page ask for.
   */
  private getDateParams(
    range: DateRangePreset = this.selectedRange
  ): DateRangeParams {
    const window = resolveTimeRange(range);
    return {
      startDate: window.startDate as string,
      endDate: window.endDate as string,
    };
  }

  private getPreviousDateParams(range: DateRangePreset): DateRangeParams {
    const window = resolvePreviousTimeRange(range);
    return {
      startDate: window.startDate as string,
      endDate: window.endDate as string,
    };
  }

  private handleBudgetPoliciesChanged(
    event: CustomEvent<{ policies: BudgetPolicy[] }>
  ) {
    this.budgetPolicies = event.detail.policies;
  }

  // Build the agent_id -> owner_username map used by the Users tab. Owner is
  // resolved from the account's managed agents; a failed lookup leaves the map
  // empty and flips `ownerAttributionAvailable` so the tab degrades gracefully.
  private async loadAgentOwners(generation: number) {
    const response = await getAccountAgents({ limit: 100 });
    const map = new Map<string, string>();
    const bySource: Array<{ sourceId: string; owner: string }> = [];
    for (const agent of response.items) {
      if (agent.owner_username) {
        map.set(agent.id, agent.owner_username);
        if (agent.session_source_id)
          bySource.push({
            sourceId: agent.session_source_id,
            owner: agent.owner_username,
          });
      }
    }
    bySource.sort((a, b) => b.sourceId.length - a.sourceId.length);
    const users = await getUsers(0, 100).catch(() => null);
    if (generation !== this.loadGeneration) return;
    this.agentOwnerMap = map;
    this.agentOwnerBySource = bySource;
    this.ownerAttributionAvailable = true;
    const list = users?.users || [];
    this.fallbackOwner =
      list.length === 1 ? list[0].username || list[0].email || null : null;
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
  private async loadToolFlagCount(generation: number) {
    const flags = await getToolCostFlags();
    if (generation === this.loadGeneration) this.toolFlagCount = flags.length;
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
    const generation = ++this.loadGeneration;
    const range = this.selectedRange;
    const period = this.getDateParams(range);
    this.loading = true;
    this.error = null;
    this.previousRangeSummary = null;
    this.currentPeriod = null;
    this.loadedTabs = new Set();
    this.sectionStates = {};
    this.sectionErrors = {};
    this.readyBreakdowns = new Set();
    this.pendingBreakdowns = new Map();
    this.budgetContextReady = false;
    this.pricingContextReady = false;
    this.overrideRemoved = null;
    void this.loadContext(generation);
    try {
      const summary = await getCostAnalyticsSummary({
        ...period,
        includeBreakdown: false,
      });
      if (generation !== this.loadGeneration) return;
      this.summary = summary;
      this.currentPeriod = {
        startDate: summary.period_start,
        endDate: summary.period_end,
      };
      this.loadedAt = new Date().toISOString();
      this.loading = false;
      void this.loadPreviousRangeSummary(generation, range);
      void this.loadTab(this.activeTab, generation);
      if (summary.imported_usage?.event_count)
        void this.loadTab('imported', generation);
    } catch (error) {
      if (generation !== this.loadGeneration) return;
      this.summary = null;
      this.error =
        error instanceof Error
          ? error.message
          : 'Failed to load cost analytics';
    } finally {
      if (generation === this.loadGeneration) this.loading = false;
    }
  }

  private async loadContext(generation = this.loadGeneration) {
    this.contextLoading = true;
    this.contextError = null;
    const tasks = [
      {
        label: 'model choices',
        request: getAIModels().then((models) => {
          if (generation === this.loadGeneration) this.aiModels = models;
        }),
      },
      {
        label: 'budget policies',
        request: getBudgetPolicies().then((policies) => {
          if (generation !== this.loadGeneration) return;
          this.budgetPolicies = policies;
          this.budgetContextReady = true;
        }),
      },
      {
        label: 'pricing settings',
        request: getFeatures().then(async (features) => {
          if (generation !== this.loadGeneration) return;
          this.featureFlags = features.features || {};
          // The whole list, not only the active rows: an expired or disabled
          // override is exactly what somebody reading this table came to
          // find, and the summary count still counts the active ones.
          const overrides = this.modelPriceOverridesEnabled
            ? await getModelPriceOverrides({ activeOnly: false, passive: true })
            : [];
          if (generation !== this.loadGeneration) return;
          this.pricingOverrides = overrides;
          this.pricingContextReady = true;
        }),
      },
    ];
    const results = await Promise.allSettled(tasks.map((task) => task.request));
    if (generation !== this.loadGeneration) return;
    const failed = tasks
      .filter((_, index) => results[index].status === 'rejected')
      .map((task) => task.label);
    this.contextError = failed.length
      ? `Could not load ${failed.join(', ')}.`
      : null;
    this.contextLoading = false;
  }

  private async loadPreviousRangeSummary(
    generation: number,
    range: DateRangePreset
  ): Promise<void> {
    try {
      const previous = await getCostAnalyticsSummary({
        ...this.getPreviousDateParams(range),
        includeBreakdown: false,
      });
      if (generation === this.loadGeneration)
        this.previousRangeSummary = previous;
    } catch {
      if (generation === this.loadGeneration) this.previousRangeSummary = null;
    }
  }

  private async loadBreakdowns(
    names: CostUsageBreakdown[],
    generation: number
  ) {
    if (generation !== this.loadGeneration || !this.currentPeriod) return;
    const missing = names.filter(
      (name) =>
        !this.readyBreakdowns.has(name) && !this.pendingBreakdowns.has(name)
    );
    if (missing.length) {
      const request = getCostAnalyticsSummary({
        ...this.currentPeriod,
        breakdowns: missing,
      })
        .then((result) => {
          if (generation !== this.loadGeneration || !this.summary) return;
          const fields = {
            models: 'usage_by_model',
            flows: 'usage_by_flow',
            sessions: 'usage_by_session',
            tools: 'usage_by_tool',
            days: 'requests_by_day',
            imported: 'imported_usage',
          } as const;
          const next = { ...this.summary };
          for (const name of missing) {
            const field = fields[name];
            // Merge only selected sections, never a later request's totals.
            if (name === 'imported' && next.imported_usage) {
              next.imported_usage = {
                ...next.imported_usage,
                usage_by_model: result.imported_usage?.usage_by_model ?? [],
                usage_by_conversation:
                  result.imported_usage?.usage_by_conversation ?? [],
              };
            } else {
              Object.assign(next, { [field]: result[field] });
            }
            this.readyBreakdowns.add(name);
          }
          this.summary = next;
        })
        .finally(() => {
          if (generation !== this.loadGeneration) return;
          for (const name of missing) this.pendingBreakdowns.delete(name);
        });
      for (const name of missing) this.pendingBreakdowns.set(name, request);
    }
    await Promise.all(names.map((name) => this.pendingBreakdowns.get(name)));
  }

  private async loadTab(tab: string, generation = this.loadGeneration) {
    if (
      !this.currentPeriod ||
      generation !== this.loadGeneration ||
      this.loadedTabs.has(tab) ||
      this.sectionStates[tab] === 'loading'
    )
      return;
    const sections: Record<string, CostUsageBreakdown[]> = {
      agents: ['sessions', 'flows'],
      sessions: ['sessions'],
      users: ['sessions'],
      tools: ['tools'],
      imported: ['imported'],
      reconciliation: [],
    };
    if (!(tab in sections)) return;
    this.sectionStates = { ...this.sectionStates, [tab]: 'loading' };
    try {
      await Promise.all([
        this.loadBreakdowns(sections[tab], generation),
        tab === 'users' ? this.loadAgentOwners(generation) : undefined,
        tab === 'tools' ? this.loadToolFlagCount(generation) : undefined,
        tab === 'reconciliation'
          ? this.loadReconciliation(generation)
          : undefined,
      ]);
      if (generation !== this.loadGeneration) return;
      this.loadedTabs = new Set(this.loadedTabs).add(tab);
      this.sectionStates = { ...this.sectionStates, [tab]: 'ready' };
    } catch (error) {
      if (generation !== this.loadGeneration) return;
      this.sectionErrors = {
        ...this.sectionErrors,
        [tab]:
          error instanceof Error
            ? error.message
            : 'Could not load this section.',
      };
      this.sectionStates = { ...this.sectionStates, [tab]: 'error' };
    }
  }

  private async handleTabShow(event: CustomEvent<{ name: string }>) {
    const tab = event.detail?.name;
    if (!tab) return;
    this.activeTab = tab;
    await this.loadTab(tab);
  }

  private renderSectionState(section: string) {
    if (this.sectionStates[section] === 'error') {
      return html`<div data-section=${section} role="alert">
        ${this.sectionErrors[section]}
        <sl-button size="small" @click=${() => void this.loadTab(section)}
          >Retry</sl-button
        >
      </div>`;
    }
    return html`<div
      data-section=${section}
      role="status"
      aria-live="polite"
      aria-busy="true"
    >
      <sl-spinner></sl-spinner> Loading ${section}…
    </div>`;
  }

  private renderTab(tab: string, render: () => unknown) {
    return this.sectionStates[tab] === 'ready'
      ? render()
      : this.renderSectionState(tab);
  }

  // Fetches provider connections and the estimated-vs-actual comparison for
  // the current date range. Only called when the Reconciliation tab is shown.
  private async loadReconciliation(generation = this.loadGeneration) {
    this.reconciliationLoading = true;
    this.reconciliationError = null;
    try {
      const range = this.currentPeriod || this.getDateParams();
      const [connections, reconciliation] = await Promise.all([
        getProviderBillingConnections().catch(() => []),
        getCostReconciliation({
          startDate: range.startDate,
          endDate: range.endDate,
        }),
      ]);
      if (generation !== this.loadGeneration) return;
      this.providerConnections = connections;
      this.reconciliation = reconciliation;
    } catch (error) {
      if (generation !== this.loadGeneration) return;
      this.reconciliationError =
        error instanceof Error
          ? error.message
          : 'Failed to load reconciliation data';
    } finally {
      if (generation === this.loadGeneration)
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
    if (this.repricing || this.repricePending) return;
    this.repricing = true;
    this.repriceNotice = null;
    this.repriceJobId = null;
    try {
      const range = this.getDateParams();
      const result = await repriceCost({
        start_date: range.startDate,
        end_date: range.endDate,
        only_unpriced: true,
      });
      if (result.submitted_async) {
        this.repriceJobId = result.job_id ?? null;
        this.repricePending = Boolean(this.repriceJobId);
        if (!this.repriceJobId) {
          this.repriceNotice =
            'Repricing accepted in the background. This server provides no job status, so completion cannot be confirmed. Refresh the page later to see current costs.';
        }
      } else {
        this.repricePending = false;
        this.repriceNotice =
          `Reprice finished: ${result.rows_updated ?? 0} of ` +
          `${result.rows_examined ?? 0} requests updated. ` +
          formatProviderLookupSummary(result.provider_lookup);
        await this.load();
      }
    } catch (error) {
      this.error =
        error instanceof Error ? error.message : 'Failed to reprice usage';
    } finally {
      this.repricing = false;
    }
  }

  private renderRepriceStatus() {
    return this.repriceJobId
      ? html`<reprice-job-status
          .jobId=${this.repriceJobId}
          @reprice-paused=${() => {
            this.repricePending = false;
          }}
          @reprice-complete=${() => {
            this.repricePending = false;
            void this.load();
          }}
        ></reprice-job-status>`
      : this.repriceNotice;
  }

  // Route from the unpriced banner into the existing price-override dialog
  // with the highest-volume unpriced model pre-filled. A $0 override is
  // honored by repricing, which is the supported escape hatch for custom
  // models that will never appear in a shared price catalog. The backend
  // coalesces rows with no model alias to "unknown"; pre-filling that
  // placeholder would invite a no-op override, so the field stays empty.
  private openPriceOverrideForUnpriced() {
    this.openPriceOverrideEditor(null);
    const top = this.summary?.unpriced_models?.[0];
    if (top && top.model !== 'unknown') {
      this.priceModelAlias = top.model;
    }
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
   * The short form of the selected range, for stat labels ("$ est. · 30d").
   */
  private rangeChipLabel(): string {
    return timeRangeShortLabel(this.selectedRange);
  }

  /**
   * Which days the numbers cover, restated beside the range control. The
   * server's own window is preferred over the client's presets: the four
   * sibling pages disagree about "30 days", and this one prints what it was
   * actually given.
   */
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

  /**
   * Open the override dialog, either empty or filled from an existing row.
   * Editing reuses the create form so there is one place that knows how an
   * override is spelled; the row is remembered so a save updates it in place.
   */
  private openPriceOverrideEditor(override: ModelPriceOverride | null) {
    this.overrideActionError = null;
    this.priceFormError = null;
    this.overrideRemoved = null;
    this.priceEditOverride = override;
    const text = (value: number | null | undefined): string =>
      typeof value === 'number' ? String(value) : '';
    this.priceModelAlias = override?.model_alias ?? '';
    this.priceProvider = override?.provider_name ?? '';
    this.priceInput = text(override?.input_price_per_1k);
    this.priceOutput = text(override?.output_price_per_1k);
    this.pricePer1k = text(override?.price_per_1k);
    this.requestPrice = text(override?.request_price);
    this.discountPercent = text(override?.discount_percent);
    this.prepaidTokens = text(override?.prepaid_token_balance);
    this.prepaidCredit = text(override?.prepaid_credit_balance_usd);
    this.priceCurrency = override?.currency ?? 'USD';
    this.priceFxRate = text(override?.fx_rate_to_usd);
    this.priceMode = this.priceModeFor(override);
    this.priceDialogOpen = true;
    if (override) void this.refreshEditedOverride(override.id);
  }

  /**
   * Re-read the row being edited, without touching what the reader typed.
   *
   * The update is a whole row, including the fields this dialog does not show,
   * and overrides are also managed by API. Re-reading when the dialog opens
   * narrows the window in which a save reverts somebody else's change from
   * "since this page loaded" to "since this dialog opened". A failure here
   * changes nothing: the row captured at load time is still what gets sent.
   */
  private async refreshEditedOverride(id: string): Promise<void> {
    try {
      const overrides = await getModelPriceOverrides({
        activeOnly: false,
        passive: true,
      });
      // Only the read the dialog on screen asked for is allowed to paint: an
      // answer to a cancelled or superseded edit would repaint the table with
      // a list that is already older than the one after it.
      if (this.priceEditOverride?.id !== id) return;
      this.pricingOverrides = overrides;
      const fresh = overrides.find((row) => row.id === id);
      if (fresh) {
        this.priceEditOverride = fresh;
      }
    } catch {
      // Keeping the row we have is better than refusing to edit.
    }
  }

  private priceModeFor(
    override: ModelPriceOverride | null
  ): CostView['priceMode'] {
    if (!override) return 'custom_token_price';
    if (typeof override.discount_percent === 'number') return 'discount';
    if (typeof override.prepaid_token_balance === 'number')
      return 'prepaid_tokens';
    if (typeof override.prepaid_credit_balance_usd === 'number')
      return 'prepaid_credit';
    const hasTokenPrice =
      typeof override.input_price_per_1k === 'number' ||
      typeof override.output_price_per_1k === 'number' ||
      typeof override.price_per_1k === 'number';
    if (!hasTokenPrice && typeof override.request_price === 'number') {
      return 'fixed_request_price';
    }
    return 'custom_token_price';
  }

  /**
   * Delete one override after the reader confirmed it. The list and the
   * pricing context are re-read afterwards, so what is on screen is what the
   * account now has rather than what this view guessed.
   */
  private async removeOverride() {
    const target = this.overrideRemoveTarget;
    if (!target || this.overrideRemoving) return;
    this.overrideRemoving = true;
    this.overrideActionError = null;
    this.overrideRemoved = null;
    try {
      await deleteModelPriceOverride(target.id);
      this.overrideRemoveTarget = null;
      this.overrideRemoved = target;
      await this.loadContext();
    } catch (error) {
      // The row stays on screen: nothing was removed, and saying so where the
      // reader clicked beats a page-level banner.
      this.overrideActionError =
        error instanceof Error
          ? error.message
          : 'Failed to remove price override';
    } finally {
      this.overrideRemoving = false;
    }
  }

  private async savePriceOverride() {
    this.priceFormError = null;
    if (!this.priceModelAlias) {
      this.priceFormError = 'Enter a model alias for the price override.';
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
      this.priceFormError =
        'Enter at least one pricing, discount, or prepaid value.';
      return;
    }
    const currency = (this.priceCurrency || 'USD').toUpperCase();
    const fxRate = this.priceFxRate !== '' ? Number(this.priceFxRate) : null;
    if (currency !== 'USD' && (!fxRate || fxRate <= 0)) {
      this.priceFormError =
        'Non-USD overrides need an FX rate to USD so costs can be recorded in USD.';
      return;
    }
    this.saving = true;
    this.error = null;
    const editing = this.priceEditOverride;
    try {
      const payload: ModelPriceOverrideCreate = {
        // Editing keeps the fields this dialog does not show: the cache rates,
        // the effective window and the notes an override was created with are
        // not the operator's to lose by touching an input price. The model id
        // is the exception: retyped to another alias, the row is about another
        // model, and a stale id would link the table to the wrong page.
        ai_model_id:
          editing && editing.model_alias === this.priceModelAlias
            ? editing.ai_model_id
            : null,
        provider_name: this.priceProvider || null,
        model_alias: this.priceModelAlias,
        currency,
        fx_rate_to_usd: currency !== 'USD' ? fxRate : null,
        input_price_per_1k: input,
        output_price_per_1k: output,
        cache_read_input_price_per_1k:
          editing?.cache_read_input_price_per_1k ?? null,
        cache_creation_input_price_per_1k:
          editing?.cache_creation_input_price_per_1k ?? null,
        price_per_1k: pricePer1k,
        request_price: requestPrice,
        discount_percent: discountPercent,
        prepaid_token_balance: prepaidTokens,
        prepaid_credit_balance_usd: prepaidCredit,
        effective_from: editing?.effective_from ?? null,
        effective_until: editing?.effective_until ?? null,
        is_active: editing ? editing.is_active : true,
        notes: editing?.notes ?? null,
      };
      if (editing) {
        await updateModelPriceOverride(editing.id, payload);
      } else {
        await createModelPriceOverride(payload);
      }
      this.priceEditOverride = null;
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
      // Said inside the dialog the reader is standing in: a page-level banner
      // behind a modal is a message nobody reads.
      this.priceFormError =
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
          <!-- In and out, and how much of the input the cache served. -->
          <div class="metric-detail">
            <token-figures
              .usage=${summary?.token_usage || null}
              expanded
            ></token-figures>
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
      return this.repriceNotice || this.repriceJobId
        ? html`<sl-alert
            variant=${this.repriceNotice?.startsWith('Reprice finished:') ? 'success' : 'primary'}
            open
            closable
            role="status"
            @sl-after-hide=${() => (this.repriceNotice = null)}
            >${this.renderRepriceStatus()}</sl-alert
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
                These models have historical usage without a cost estimate:
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
                Check the prices for these models, then reprice. Set a $0
                override only for models you know are free.
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
                ?disabled=${this.repricePending}
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
        ${this.renderRepriceStatus()}
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
                  this.openPriceOverrideEditor(null);
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

  // Flow rows use complete period aggregates, not the bounded recent-session
  // list. Flow-scoped requests belong to the flow in this mixed table, even
  // when their session also names an agent, so their cost is counted once.
  // Non-flow sessions retain their agent/Other grouping.
  private buildAgentGroups(): AgentGroupRow[] {
    const sessions = this.summary?.usage_by_session || [];
    const groups = new Map<string, AgentGroupRow>();
    for (const flow of this.summary?.usage_by_flow || []) {
      // The null-flow aggregate includes non-flow agents: it is not a flow
      // row and must not be added again beside their session-based groups.
      if (!flow.flow_id) continue;
      const key = `flow:${flow.flow_id}`;
      groups.set(key, {
        key,
        name: flow.flow_name || flow.flow_id,
        agentId: null,
        flowId: flow.flow_id,
        requests: flow.request_count || 0,
        totalTokens: flow.token_usage?.total_tokens || 0,
        tokenUsage: flow.token_usage || null,
        cost: flow.estimated_cost || 0,
      });
    }
    for (const session of sessions) {
      if (session.flow_id) continue;
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
        tokenUsage: null,
        cost: 0,
      };
      existing.requests += session.request_count || 0;
      existing.totalTokens += session.token_usage?.total_tokens || 0;
      // Counts add, the cache rate is recomputed from them.
      existing.tokenUsage = sumTokenUsage([
        existing.tokenUsage,
        session.token_usage,
      ]);
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
        tokenUsage: null,
        cost: 0,
      };
      existing.requests += session.request_count || 0;
      // Tokens sum through the one helper, so the merged cache rate is
      // recomputed from the merged counts rather than averaged.
      existing.tokenUsage = sumTokenUsage([
        existing.tokenUsage,
        session.token_usage,
      ]);
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
          this.sectionStates.imported !== 'ready'
            ? this.renderSectionState('imported')
            : rows.length
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
        ${this.sectionStates.imported === 'ready' ? this.renderImportedConversations(imported.usage_by_conversation ?? []) : nothing}
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
          <sl-tab
            slot="nav"
            panel="agents"
            ?active=${this.activeTab === 'agents'}
            >Agents</sl-tab
          >
          <sl-tab slot="nav" panel="tools" ?active=${this.activeTab === 'tools'}
            >Tools</sl-tab
          >
          <sl-tab
            slot="nav"
            panel="sessions"
            ?active=${this.activeTab === 'sessions'}
            >Sessions</sl-tab
          >
          <sl-tab slot="nav" panel="users" ?active=${this.activeTab === 'users'}
            >Users</sl-tab
          >
          ${
            this.reconciliationEnabled
              ? html`<sl-tab slot="nav" panel="reconciliation"
                  >Reconciliation</sl-tab
                >`
              : nothing
          }
          <sl-tab-panel name="agents"
            >${this.renderTab('agents', () => this.renderAgentsTab())}</sl-tab-panel
          >
          <sl-tab-panel name="tools"
            >${this.renderTab('tools', () => this.renderToolsTab())}</sl-tab-panel
          >
          <sl-tab-panel name="sessions"
            >${this.renderTab('sessions', () => this.renderSessionsTab())}</sl-tab-panel
          >
          <sl-tab-panel name="users"
            >${this.renderTab('users', () => this.renderUsersTab())}</sl-tab-panel
          >
          ${
            this.reconciliationEnabled
              ? html`<sl-tab-panel name="reconciliation"
                  >${this.renderTab('reconciliation', () => this.renderReconciliationTab())}</sl-tab-panel
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
                    <td>
                      <token-figures
                        .usage=${row.tokenUsage}
                        expanded
                      ></token-figures>
                    </td>
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
                    <td>
                      <token-figures
                        .usage=${row.token_usage || null}
                        expanded
                      ></token-figures>
                    </td>
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
      // Tokens before cost here too: this is a spend table, and the volume
      // is what the money was spent on.
      {
        key: 'tokens',
        label: 'Tokens',
        numeric: true,
        value: (r) => r.tokenUsage?.total_tokens || 0,
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
                    <td>
                      <token-figures
                        .usage=${row.tokenUsage}
                        expanded
                      ></token-figures>
                    </td>
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
        .configurable=${true}
        .timeRange=${'month'}
        @configure=${() => (this.budgetDialogOpen = true)}
      ></budget-health-card>
    `;
  }

  /**
   * Is this override pricing requests right now? `is_active` is the operator's
   * switch; the effective window is the calendar. A row that is off or out of
   * its window is still worth showing, greyed, because it explains what the
   * account used to pay.
   */
  private overrideStandingOf(
    override: ModelPriceOverride
  ): 'in-force' | 'disabled' | 'expired' | 'pending' {
    if (!override.is_active) return 'disabled';
    const now = Date.now();
    if (
      override.effective_until &&
      new Date(override.effective_until).getTime() <= now
    ) {
      return 'expired';
    }
    if (
      override.effective_from &&
      new Date(override.effective_from).getTime() > now
    ) {
      return 'pending';
    }
    return 'in-force';
  }

  private isOverrideInForce(override: ModelPriceOverride): boolean {
    return this.overrideStandingOf(override) === 'in-force';
  }

  /**
   * Why a row is not pricing anything. Switched off, run out, and not started
   * yet are three different answers, and a future-dated override that reads
   * "Inactive" invites somebody to recreate a price that is already coming.
   */
  private overrideStandingLabel(override: ModelPriceOverride): string {
    switch (this.overrideStandingOf(override)) {
      case 'disabled':
        return 'Disabled';
      case 'expired':
        return 'Expired';
      case 'pending':
        return `Starts ${this.formatOverrideDate(override.effective_from)}`;
      default:
        return '';
    }
  }

  /** Active rows first, newest first inside each group. */
  private sortedOverrides(): ModelPriceOverride[] {
    return [...this.pricingOverrides].sort((left, right) => {
      const leftActive = this.isOverrideInForce(left) ? 0 : 1;
      const rightActive = this.isOverrideInForce(right) ? 0 : 1;
      if (leftActive !== rightActive) return leftActive - rightActive;
      return (right.created_at || '').localeCompare(left.created_at || '');
    });
  }

  /** Stored rates are per 1,000 tokens; every price on this page is per 1M. */
  private formatPer1m(value: number | null | undefined) {
    if (typeof value !== 'number') return nothing;
    return html`${this.formatCurrency(value * 1000)}`;
  }

  private formatOverrideDate(value: string | null | undefined): string {
    if (!value) return '';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return '';
    return date.toLocaleDateString(undefined, {
      month: 'short',
      day: 'numeric',
      year: 'numeric',
    });
  }

  /** A one-line summary of what an override charges, for confirm dialogs. */
  private describeOverrideRates(override: ModelPriceOverride): string {
    const parts: string[] = [];
    const per1m = (value: number | null) =>
      typeof value === 'number' ? this.formatCurrency(value * 1000) : null;
    const input = per1m(override.input_price_per_1k);
    const output = per1m(override.output_price_per_1k);
    const cached = per1m(override.cache_read_input_price_per_1k);
    const creation = per1m(override.cache_creation_input_price_per_1k);
    const blended = per1m(override.price_per_1k);
    if (input) parts.push(`input ${input} per 1M`);
    if (output) parts.push(`output ${output} per 1M`);
    if (cached) parts.push(`cached input ${cached} per 1M`);
    if (creation) parts.push(`cache creation ${creation} per 1M`);
    if (blended) parts.push(`blended ${blended} per 1M`);
    if (typeof override.request_price === 'number') {
      parts.push(`${this.formatCurrency(override.request_price)} per request`);
    }
    if (typeof override.discount_percent === 'number') {
      parts.push(`${override.discount_percent}% off list`);
    }
    return parts.length ? parts.join(', ') : 'no rates set';
  }

  private renderOverrideRow(override: ModelPriceOverride) {
    const inForce = this.isOverrideInForce(override);
    const notes = override.notes || '';
    return html`
      <tr
        class=${inForce ? '' : 'override-inactive'}
        data-override-id=${override.id}
        data-testid="override-row"
      >
        <td>
          ${
            override.ai_model_id
              ? html`<a
                  class="override-model-link"
                  href=${`/console/ai-models/${override.ai_model_id}`}
                  >${override.model_alias}</a
                >`
              : html`${override.model_alias}`
          }
          <div class="override-model-meta">
            ${override.provider_name || ''}
            ${
              inForce
                ? nothing
                : html`<sl-badge
                    class="chip"
                    pill
                    variant="neutral"
                    data-testid="override-standing"
                    >${this.overrideStandingLabel(override)}</sl-badge
                  >`
            }
          </div>
        </td>
        <td>${this.formatPer1m(override.input_price_per_1k)}</td>
        <td>${this.formatPer1m(override.output_price_per_1k)}</td>
        <td>${this.formatPer1m(override.cache_read_input_price_per_1k)}</td>
        <td>${this.formatPer1m(override.cache_creation_input_price_per_1k)}</td>
        <td>
          ${
            typeof override.request_price === 'number'
              ? this.formatCurrency(override.request_price)
              : nothing
          }
        </td>
        <td>${this.formatOverrideDate(override.effective_from)}</td>
        <td>${this.formatOverrideDate(override.effective_until)}</td>
        <td>${this.formatOverrideDate(override.created_at)}</td>
        <td class="override-notes" title=${notes}>
          ${notes.length > 40 ? `${notes.slice(0, 40)}...` : notes}
        </td>
        <td class="override-actions">
          <sl-button
            size="small"
            data-testid="edit-override"
            @click=${() => this.openPriceOverrideEditor(override)}
            >Edit</sl-button
          >
          <sl-button
            size="small"
            data-testid="remove-override"
            @click=${() => {
              this.overrideActionError = null;
              this.overrideRemoveTarget = override;
            }}
            >Remove</sl-button
          >
        </td>
      </tr>
    `;
  }

  private renderOverridesTable() {
    const overrides = this.sortedOverrides();
    if (!overrides.length) {
      return html`<div class="override-empty">
        No price overrides yet. Every model is costed from the provider catalog.
      </div>`;
    }
    return html`
      <div class="analytics-table-wrap">
        <table class="styled-table" aria-label="Price overrides">
          <thead>
            <tr>
              <th scope="col">Model</th>
              <th scope="col">Input / 1M</th>
              <th scope="col">Output / 1M</th>
              <th scope="col">Cached input / 1M</th>
              <th scope="col">Cache creation / 1M</th>
              <th scope="col">Per request</th>
              <th scope="col">Effective from</th>
              <th scope="col">Effective until</th>
              <th scope="col">Created</th>
              <th scope="col">Notes</th>
              <th scope="col">Actions</th>
            </tr>
          </thead>
          <tbody>
            ${overrides.map((override) => this.renderOverrideRow(override))}
          </tbody>
        </table>
      </div>
    `;
  }

  /**
   * What is left to do after an override is gone: usage already recorded under
   * it keeps the cost it was given. The reprice control itself lives on the
   * model detail page, so this points there rather than duplicating it.
   */
  private renderOverrideRemovedNotice() {
    const removed = this.overrideRemoved;
    if (!removed) return nothing;
    const usage = (this.summary?.usage_by_model ?? []).find(
      (row) => row.model_alias === removed.model_alias
    );
    return html`
      <div
        class="override-removed-notice"
        role="status"
        data-testid="override-removed-notice"
      >
        Override removed for ${removed.model_alias}. New requests are costed
        from the catalog.
        ${
          usage && usage.request_count > 0
            ? html`<span data-testid="override-reprice-pointer">
                Rows recorded under the override keep the old cost until they
                are repriced.
                ${
                  removed.ai_model_id
                    ? html`<a
                        href=${`/console/ai-models/${removed.ai_model_id}`}
                        >Reprice on the model page</a
                      >`
                    : nothing
                }
              </span>`
            : nothing
        }
      </div>
    `;
  }

  private renderPricing() {
    const activeCount = this.pricingOverrides.filter((override) =>
      this.isOverrideInForce(override)
    ).length;
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
              >${this.formatNumber(activeCount)}</span
            >
          </div>
          ${
            this.overrideActionError && !this.overrideRemoveTarget
              ? html`<div
                  class="override-error"
                  role="alert"
                  data-testid="override-action-error"
                >
                  ${this.overrideActionError}
                </div>`
              : nothing
          }
          ${this.renderOverrideRemovedNotice()} ${this.renderOverridesTable()}
          <sl-button
            variant="primary"
            @click=${() => this.openPriceOverrideEditor(null)}
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
        ${this.contextError ? html`<sl-alert variant="warning" open>${this.contextError}<sl-button @click=${() => void this.loadContext()}>Retry</sl-button></sl-alert>` : nothing}
        ${this.contextLoading ? html`<div role="status"><sl-spinner></sl-spinner> Loading cost settings…</div>` : nothing}
        ${this.budgetContextReady ? this.renderBudgets() : nothing}
        ${this.pricingContextReady && this.modelPriceOverridesEnabled ? this.renderPricing() : nothing}
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

  private renderRemoveOverrideDialog() {
    const target = this.overrideRemoveTarget;
    return html`
      <sl-dialog
        label="Remove price override"
        data-testid="remove-override-dialog"
        ?open=${target !== null}
        @sl-after-hide=${(event: Event) => {
          if (event.target === event.currentTarget) {
            this.overrideRemoveTarget = null;
          }
        }}
      >
        ${
          target
            ? html`
                <p class="dialog-description">
                  Remove the override for
                  ${target.model_alias}${
                    target.provider_name ? ` (${target.provider_name})` : ''
                  }?
                  It charges
                  ${this.describeOverrideRates(target)}${
                    target.effective_from
                      ? `, effective from ${this.formatOverrideDate(
                          target.effective_from
                        )}`
                      : ''
                  }.
                  New requests fall back to the provider catalog, and usage
                  already recorded keeps the cost it was given until it is
                  repriced.
                </p>
                ${
                  this.overrideActionError
                    ? html`<div
                        class="override-error"
                        role="alert"
                        data-testid="override-action-error"
                      >
                        ${this.overrideActionError}
                      </div>`
                    : nothing
                }
              `
            : nothing
        }
        <div slot="footer">
          <sl-button
            data-testid="cancel-remove-override"
            @click=${() => (this.overrideRemoveTarget = null)}
          >
            Cancel
          </sl-button>
          <sl-button
            variant="danger"
            data-testid="confirm-remove-override"
            .loading=${this.overrideRemoving}
            @click=${() => void this.removeOverride()}
          >
            Remove override
          </sl-button>
        </div>
      </sl-dialog>
    `;
  }

  private renderPriceOverrideDialog() {
    const editing = this.priceEditOverride !== null;
    return html`
      <sl-dialog
        label=${editing ? 'Edit price override' : 'Add price override'}
        ?open=${this.priceDialogOpen}
        @sl-after-hide=${(event: Event) => {
          if (event.target === event.currentTarget) {
            this.priceDialogOpen = false;
            this.priceEditOverride = null;
          }
        }}
      >
        <p class="dialog-description">
          Override model prices when your Enterprise account has negotiated
          rates, credits, or provider-specific billing terms.
        </p>
        ${
          this.priceFormError
            ? html`<div
                class="override-error"
                role="alert"
                data-testid="override-form-error"
              >
                ${this.priceFormError}
              </div>`
            : nothing
        }
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
          <sl-button
            @click=${() => {
              this.priceDialogOpen = false;
              this.priceEditOverride = null;
            }}
          >
            Cancel
          </sl-button>
          <sl-button
            variant="primary"
            data-testid="save-override"
            .loading=${this.saving}
            @click=${async () => {
              await this.savePriceOverride();
              if (!this.priceFormError) {
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
      ${html`
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
      `}
      ${
        this.modelPriceOverridesEnabled
          ? html`${this.renderPriceOverrideDialog()}
            ${this.renderRemoveOverrideDialog()}`
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
                >${this.error}<sl-button @click=${() => void this.load()}
                  >Retry</sl-button
                ></sl-alert
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
            : hasAnswer
              ? html`
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
              : nothing
        }
        ${this.renderDialogs()}
      </div>
    `;
  }
}
