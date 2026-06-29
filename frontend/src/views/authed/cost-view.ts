import { html, css, unsafeCSS, nothing } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import {
  AuthedElement,
  createModelPriceOverride,
  getAIModels,
  getBudgetPolicies,
  getCostAnalyticsSummary,
  getFeatures,
  getModelPriceOverrides,
  type BudgetPolicy,
} from '../../api';
import type {
  AIModel,
  CostAnalyticsSummaryResponse,
  GatewayUsageBySession,
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

  private async load() {
    this.loading = true;
    this.error = null;
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
      this.budgetPolicies = this.billingEnabled
        ? await getBudgetPolicies().catch(() => [] as BudgetPolicy[])
        : [];
      this.aiModels = aiModels;
      if (this.featureFlags.model_price_overrides === true) {
        this.pricingOverrides = await getModelPriceOverrides({
          activeOnly: true,
        }).catch(() => []);
      } else {
        this.pricingOverrides = [];
      }
    } catch (error) {
      this.error =
        error instanceof Error
          ? error.message
          : 'Failed to load cost analytics';
    } finally {
      this.loading = false;
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
            ${this.selectedRange === 'this-month'
              ? 'Month-to-date Spend'
              : 'Estimated Spend'}
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
        ${projectedPeriodCost !== null
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
          : nothing}
      </div>
    `;
  }

  private renderBreakdown() {
    const rows = this.summary?.usage_by_model || [];
    return html`
      <sl-card class="analytics-card">
        ${this.renderSectionHeader('cpu', 'Spend by model')}
        ${rows.length
          ? html`
              <div class="analytics-table-wrap">
                <table class="styled-table" aria-label="Spend by model">
                  <thead>
                    <tr>
                      <th scope="col">Model</th>
                      <th scope="col">Provider</th>
                      <th scope="col">Requests</th>
                      <th scope="col">Tokens</th>
                      <th scope="col">Cost</th>
                    </tr>
                  </thead>
                  <tbody>
                    ${rows.map(
                      (row) => html`
                        <tr>
                          <td>
                            ${row.ai_model_id
                              ? html`<a
                                  href=${`/console/ai-models/${row.ai_model_id}`}
                                  >${row.model_alias || 'Unknown model'}</a
                                >`
                              : row.model_alias || 'Unknown model'}
                          </td>
                          <td>${row.provider_name || 'Unknown'}</td>
                          <td>${this.formatNumber(row.request_count)}</td>
                          <td>
                            ${this.formatNumber(row.token_usage.total_tokens)}
                          </td>
                          <td>${this.formatCurrency(row.estimated_cost)}</td>
                        </tr>
                      `
                    )}
                  </tbody>
                </table>
              </div>
            `
          : html`<div class="empty">No model gateway usage yet.</div>`}
      </sl-card>
      <sl-card class="analytics-card">
        ${this.renderSectionHeader('collection', 'Recent session spend')}
        ${this.summary?.usage_by_session?.length
          ? html`
              <div class="analytics-table-wrap">
                <table class="styled-table" aria-label="Recent session spend">
                  <thead>
                    <tr>
                      <th scope="col">Session</th>
                      <th scope="col">Agent</th>
                      <th scope="col">Model</th>
                      <th scope="col">Requests</th>
                      <th scope="col">Cost</th>
                    </tr>
                  </thead>
                  <tbody>
                    ${this.summary.usage_by_session.map(
                      (row) => html`
                        <tr>
                          <td>
                            ${row.runtime_session_id
                              ? html`<a
                                  href="/console/runtime-sessions?sessionId=${row.runtime_session_id}"
                                  >${row.session_summary ||
                                  row.session_reference ||
                                  row.runtime_session_id}</a
                                >`
                              : row.session_reference || 'Legacy execution'}
                          </td>
                          <td>${this.renderSessionSubjects(row)}</td>
                          <td>${row.model_alias || 'Unknown'}</td>
                          <td>${this.formatNumber(row.request_count)}</td>
                          <td>${this.formatCurrency(row.estimated_cost)}</td>
                        </tr>
                      `
                    )}
                  </tbody>
                </table>
              </div>
            `
          : html`<div class="empty">No session-attributed usage yet.</div>`}
      </sl-card>
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
        <tool-cost-flags-panel></tool-cost-flags-panel>
        ${this.modelPriceOverridesEnabled ? this.renderPricing() : null}
      </div>
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
                | Record<string, unknown>
                | undefined;
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
          ${this.priceMode === 'custom_token_price'
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
            : nothing}
          ${this.priceMode === 'fixed_request_price'
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
            : nothing}
          ${this.priceMode === 'discount'
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
            : nothing}
          ${this.priceMode === 'prepaid_tokens'
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
            : nothing}
          ${this.priceMode === 'prepaid_credit'
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
            : nothing}
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
      ${this.billingEnabled
        ? html`
            <sl-dialog
              label="Configure Budget Limits"
              ?open=${this.budgetDialogOpen}
              @sl-after-hide=${(event: Event) => {
                if (event.target === event.currentTarget) {
                  this.budgetDialogOpen = false;
                  void this.load();
                }
              }}
            >
              <budget-policy-editor></budget-policy-editor>
              <div slot="footer">
                <sl-button @click=${() => (this.budgetDialogOpen = false)}>
                  Close
                </sl-button>
              </div>
            </sl-dialog>
          `
        : null}
      ${this.modelPriceOverridesEnabled
        ? this.renderPriceOverrideDialog()
        : null}
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
            @sl-change=${(event: Event) => {
              this.selectedRange = (event.target as HTMLSelectElement)
                .value as DateRangePreset;
              this.persistDateRange(this.selectedRange);
              void this.load();
            }}
          >
            <sl-option value="today">Today</sl-option>
            <sl-option value="this-week">This week</sl-option>
            <sl-option value="this-month">This month</sl-option>
            <sl-option value="last-month">Last month</sl-option>
            <sl-option value="last-7">Last 7 days</sl-option>
            <sl-option value="last-30">Last 30 days</sl-option>
            <sl-option value="last-90">Last 90 days</sl-option>
          </sl-select>
          <sl-button @click=${this.load}>Refresh</sl-button>
        </div>

        ${this.error
          ? html`<sl-alert
              variant="danger"
              open
              role="alert"
              aria-live="assertive"
              >${this.error}</sl-alert
            >`
          : null}
        ${this.loading
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
            `}
        ${this.renderDialogs()}
      </div>
    `;
  }
}
