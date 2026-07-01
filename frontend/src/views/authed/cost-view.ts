import { html, css, unsafeCSS, nothing } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import {
  AuthedElement,
  createModelPriceOverride,
  getAccountAgents,
  getAIModels,
  getBudgetPolicies,
  getCostAnalyticsSummary,
  getFeatures,
  getUsers,
  getModelPriceOverrides,
  getToolCostFlags,
  type BudgetPolicy,
} from '../../api';
import type {
  AIModel,
  CostAnalyticsSummaryResponse,
  GatewayUsageBySession,
  GatewayUsageByTool,
  ModelPriceOverride,
  ModelPriceOverrideCreate,
} from '../../types';
import consoleStyles from '../../styles/console-styles.css?inline';
import '../../components/view-header.ts';
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

@customElement('cost-view')
export class CostView extends AuthedElement {
  @state() private summary: CostAnalyticsSummaryResponse | null = null;
  @state() private previousRangeSummary: CostAnalyticsSummaryResponse | null =
    null;
  @state() private budgetPolicies: BudgetPolicy[] = [];
  @state() private aiModels: AIModel[] = [];
  @state() private pricingOverrides: ModelPriceOverride[] = [];
  @state() private featureFlags: Record<string, boolean | string[]> = {};
  @state() private loading = true;
  @state() private saving = false;
  @state() private error: string | null = null;
  @state() private selectedRange: DateRangePreset = 'today';
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

  private get modelPriceOverridesEnabled(): boolean {
    return this.featureFlags.model_price_overrides === true;
  }

  private get billingEnabled(): boolean {
    return this.featureFlags.billing === true;
  }

  static styles = [
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
        align-items: end;
        flex-wrap: wrap;
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

      .analytics-card::part(body) {
        padding: var(--sl-spacing-large);
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
    void this.load();
  }

  private loadStoredDateRange(): DateRangePreset {
    const stored = window.localStorage.getItem(COST_DATE_RANGE_STORAGE_KEY);
    return DATE_RANGE_PRESETS.includes(stored as DateRangePreset)
      ? (stored as DateRangePreset)
      : 'today';
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
  // Updates the selected range, persists it, and re-fetches the summary so the
  // KPIs and every tab reflect the new window (see C).
  private handleRangeChange = (event: Event) => {
    const value = (event.target as HTMLSelectElement).value as DateRangePreset;
    if (!DATE_RANGE_PRESETS.includes(value) || value === this.selectedRange) {
      return;
    }
    this.selectedRange = value;
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
      const [summary, aiModels, features, previousSummary] = await Promise.all([
        getCostAnalyticsSummary(this.getDateParams()),
        getAIModels(),
        getFeatures().catch(() => ({ features: {} })),
        getCostAnalyticsSummary(
          this.getPreviousDateParams(this.selectedRange)
        ).catch(() => null),
      ]);
      this.summary = summary;
      this.previousRangeSummary = previousSummary;
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
      ? 'Projected Week'
      : this.selectedRange === 'today'
        ? 'Projected Today'
        : 'Projected Month';
  }

  private projectedPeriodComparisonDetail(projectedCost: number): string {
    const previousCost = this.previousRangeSummary?.estimated_cost;
    if (previousCost === null || previousCost === undefined) {
      return `Compared to ${this.previousRangeLabel()}`;
    }
    return `${this.formatSignedCurrency(projectedCost - previousCost)} vs ${this.previousRangeLabel()}`;
  }

  private formatSignedCurrency(value: number): string {
    const prefix = value > 0 ? '+' : '';
    return `${prefix}${this.formatCurrency(value)}`;
  }

  private previousRangeLabel(): string {
    if (this.selectedRange === 'today') return 'same time yesterday';
    if (this.selectedRange === 'this-week') return 'previous week';
    if (this.selectedRange === 'this-month') return 'previous month';
    if (this.selectedRange === 'last-month') return 'month before';
    if (this.selectedRange === 'last-7') return 'previous 7 days';
    if (this.selectedRange === 'last-90') return 'previous 90 days';
    return 'previous 30 days';
  }

  private spendComparisonDetail(): string {
    const previousCost = this.previousRangeSummary?.estimated_cost;
    if (previousCost === null || previousCost === undefined) {
      return `Compared to ${this.previousRangeLabel()}`;
    }
    const currentCost = this.summary?.estimated_cost || 0;
    const delta = currentCost - previousCost;
    return `${this.formatSignedCurrency(delta)} vs ${this.previousRangeLabel()}`;
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
    this.saving = true;
    this.error = null;
    try {
      const payload: ModelPriceOverrideCreate = {
        ai_model_id: null,
        provider_name: this.priceProvider || null,
        model_alias: this.priceModelAlias,
        currency: this.priceCurrency || 'USD',
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
                ? 'Month-to-date Spend'
                : 'Estimated Spend'
            }
          </div>
          <div class="metric-value">
            ${this.formatCurrency(summary?.estimated_cost)}
          </div>
          <div class="metric-detail">${this.spendComparisonDetail()}</div>
        </div>
        <div class="metric-card">
          <div class="metric-label">Gateway Requests</div>
          <div class="metric-value">
            ${this.formatNumber(summary?.total_requests)}
          </div>
          <div class="metric-detail">
            ${this.formatNumber(summary?.successful_requests)} succeeded
          </div>
        </div>
        <div class="metric-card">
          <div class="metric-label">Tokens</div>
          <div class="metric-value">
            ${this.formatNumber(summary?.token_usage.total_tokens)}
          </div>
          <div class="metric-detail">
            ${this.formatNumber(summary?.token_usage.prompt_tokens)} prompt,
            ${this.formatNumber(summary?.token_usage.completion_tokens)}
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
          <sl-tab-panel name="agents">${this.renderAgentsTab()}</sl-tab-panel>
          <sl-tab-panel name="tools">${this.renderToolsTab()}</sl-tab-panel>
          <sl-tab-panel name="sessions"
            >${this.renderSessionsTab()}</sl-tab-panel
          >
          <sl-tab-panel name="users">${this.renderUsersTab()}</sl-tab-panel>
        </sl-tab-group>
      </sl-card>
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
                Owner attribution unavailable — sessions are grouped as
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
      <sl-card>
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
        No expensive tool definitions flagged — your agents' tool schemas look
        efficient. Sort by total cost or schema tokens to spot the priciest
        tools.
      </sl-alert>
    `;
  }

  private renderPriceOverrideDialog() {
    return html`
      <sl-dialog
        label="Add Price Override"
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
            .value=${this.priceCurrency}
            @sl-input=${(event: Event) =>
              (this.priceCurrency = (event.target as HTMLInputElement).value)}
          ></sl-input>
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
                label="Configure Budget Limits"
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
    return html`
      <div class="page">
        <view-header
          title="Cost Analytics"
          description="Understand gateway spend by model, agent, session, flow, and API key."
        ></view-header>

        <div class="toolbar">
          <sl-select
            label="Date range"
            .value=${this.selectedRange}
            @sl-change=${this.handleRangeChange}
          >
            <sl-option value="today">Today</sl-option>
            <sl-option value="this-week">This week</sl-option>
            <sl-option value="this-month">This month</sl-option>
            <sl-option value="last-month">Last month</sl-option>
            <sl-option value="last-7">Last 7 days</sl-option>
            <sl-option value="last-30">Last 30 days</sl-option>
            <sl-option value="last-90">Last 90 days</sl-option>
          </sl-select>
          <sl-button @click=${() => void this.load()}>Refresh</sl-button>
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
          this.loading
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
                ${this.renderMetrics()}
                <div class="column-layout dashboard extra-wide">
                  <div class="main-column">${this.renderBreakdown()}</div>
                  <div class="side-column">${this.renderControls()}</div>
                </div>
              `
        }
        ${this.renderDialogs()}
      </div>
    `;
  }
}
