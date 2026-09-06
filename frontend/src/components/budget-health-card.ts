import { LitElement, css, html, nothing } from 'lit';
import { customElement, property } from 'lit/decorators.js';
import type { BudgetPolicy } from '../api';
import type {
  AccountGatewayUsageSummaryResponse,
  ManagedAgentSummary,
} from '../types';
import { budgetTrackStyles } from '../styles/budget-track';
import { budgetForecast, forecastEndLabel } from '../utils/budget-forecast';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/card/card.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';

type BudgetPolicyUsage = {
  policy: BudgetPolicy;
  spend: number;
  hardLimit: number;
  softLimit: number;
  maxLimit: number;
  percent: number;
};

@customElement('budget-health-card')
export class BudgetHealthCard extends LitElement {
  @property({ type: Object })
  summary: AccountGatewayUsageSummaryResponse | null = null;
  @property({ type: Array }) policies: BudgetPolicy[] = [];
  @property({ type: Boolean }) configurable = false;
  @property({ type: Array }) agents: ManagedAgentSummary[] = [];
  @property({ type: Boolean }) loading = false;
  @property({ type: String }) timeRange = 'month';
  @property({ type: Boolean }) showRangeSelector = false;

  static styles = [
    budgetTrackStyles,
    css`
      :host {
        display: block;
        width: 100%;
      }

      .content-card {
        width: 100%;
      }

      .content-card::part(base) {
        width: 100%;
      }

      .header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        gap: var(--sl-spacing-small);
      }

      .title {
        display: flex;
        align-items: center;
        gap: var(--sl-spacing-2x-small);
        font-weight: 600;
      }

      select {
        background: transparent;
        border: none;
        color: var(--sl-color-neutral-600);
        cursor: pointer;
        font-size: var(--sl-font-size-small);
        outline: none;
      }

      .content {
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-medium);
      }

      .rows {
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-small);
      }

      .budget-row {
        display: flex;
        flex-direction: column;
        gap: 4px;
      }

      .row-header,
      .row-footer {
        display: flex;
        justify-content: space-between;
        align-items: center;
        gap: var(--sl-spacing-small);
      }

      .row-header {
        font-size: var(--sl-font-size-small);
      }

      .row-label {
        display: flex;
        align-items: center;
        gap: 4px;
        min-width: 0;
        color: var(--sl-color-neutral-800);
      }

      .row-value {
        font-weight: 500;
        text-align: right;
        white-space: nowrap;
      }

      .row-footer {
        color: var(--sl-color-neutral-500);
        font-size: var(--sl-font-size-x-small);
      }

      .title.exceeded {
        color: var(--sl-color-danger-700);
      }

      .row-value.exceeded {
        color: var(--sl-color-danger-700);
      }

      /* The forecast sentence, in the Overview's type and tones. */
      .budget-forecast {
        color: var(--sl-color-neutral-500);
        font-size: var(--sl-font-size-x-small);
        font-variant-numeric: tabular-nums;
      }

      .budget-forecast.warning {
        color: var(--sl-color-warning-700);
      }

      .budget-forecast.danger {
        color: var(--sl-color-danger-700);
      }

      .row-footer .limit-status {
        color: var(--sl-color-danger-700);
        font-weight: var(--sl-font-weight-semibold);
      }

      .empty {
        color: var(--sl-color-neutral-600);
        font-size: var(--sl-font-size-small);
      }

      sl-button {
        width: 100%;
      }
    `,
  ];

  private formatCurrency(value?: number | null): string {
    const amount = Number(value || 0);
    if (amount === 0) return '$0.00';
    return amount >= 0.01 ? `$${amount.toFixed(2)}` : `$${amount.toFixed(4)}`;
  }

  private formatBudgetPeriod(period: string): string {
    if (period === 'hourly') return '1h';
    if (period === 'daily') return '24h';
    if (period === 'weekly') return '7d';
    if (period === 'monthly' || period === 'month') return '30d';
    if (period === 'yearly' || period === 'year') return '1y';
    if (period === 'all_time') return 'all time';
    return period;
  }

  /**
   * Which window a budget row is about, in the Overview's words. "Global
   * spend · 30d" was read as a rolling total; "Monthly budget · Sep" says the
   * number resets, and says it the same way on both pages.
   */
  private periodWindow(period: string, now: Date = new Date()): string {
    if (period === 'hourly') return 'this hour';
    if (period === 'daily') return 'today';
    if (period === 'weekly') return 'this week';
    if (period === 'monthly' || period === 'month') {
      return now.toLocaleDateString(undefined, { month: 'short' });
    }
    if (period === 'yearly' || period === 'year')
      return String(now.getFullYear());
    return '';
  }

  private budgetPeriodLabel(period: string, now: Date = new Date()): string {
    const normalized =
      period === 'month' ? 'monthly' : period === 'year' ? 'yearly' : period;
    const base =
      normalized === 'all_time'
        ? 'All time budget'
        : `${normalized.charAt(0).toUpperCase()}${normalized.slice(1)} budget`;
    const window = this.periodWindow(normalized, now);
    return window ? `${base} · ${window}` : base;
  }

  private periodForTimeRange(): BudgetPolicy['period'] {
    if (this.timeRange === 'day') return 'daily';
    if (this.timeRange === 'week') return 'weekly';
    if (this.timeRange === 'year') return 'yearly';
    return 'monthly';
  }

  private getManagedAgentBySourceId(
    sourceId: string | null | undefined
  ): any | undefined {
    if (!sourceId) {
      return undefined;
    }
    return this.agents.find(
      (agent) => agent.id === sourceId || agent.session_source_id === sourceId
    );
  }

  private policyDisplayName(policy: BudgetPolicy): string {
    const period =
      this.periodWindow(policy.period) ||
      this.formatBudgetPeriod(policy.period);
    if (policy.subject_type === 'global' || policy.subject_type === 'account') {
      return this.budgetPeriodLabel(policy.period);
    }
    if (policy.subject_type === 'managed_agent') {
      const agentName =
        this.getManagedAgentBySourceId(policy.subject_id)?.display_name ||
        'Managed agent';
      return `${agentName} · ${period}`;
    }
    if (policy.subject_type === 'ai_model') {
      return `${policy.model_alias || 'Model'} · ${period}`;
    }
    return `${policy.subject_type.replace(/_/g, ' ')} · ${period}`;
  }

  private policyIcon(policy: BudgetPolicy): string {
    if (policy.subject_type === 'global' || policy.subject_type === 'account') {
      return 'globe';
    }
    if (policy.subject_type === 'managed_agent') return 'robot';
    if (policy.subject_type === 'ai_model') return 'cpu';
    return 'sliders';
  }

  private spendForPolicy(policy: BudgetPolicy): number {
    // Prefer the server-computed, PERIOD-ALIGNED spend (today for a daily
    // policy, this month for a monthly one). The summary-derived fallbacks
    // below use the Cost view's date range, which ignores the policy period —
    // that made a daily and a monthly policy show identical spend.
    if (typeof policy.current_spend_usd === 'number') {
      return policy.current_spend_usd;
    }
    if (!this.summary) return 0;
    if (policy.subject_type === 'global' || policy.subject_type === 'account') {
      return (
        this.summary.budget?.current_spend_usd ||
        this.summary.estimated_cost ||
        0
      );
    }
    if (policy.subject_type === 'ai_model') {
      return this.summary.usage_by_model
        .filter(
          (model) =>
            model.ai_model_id === policy.subject_id ||
            model.model_alias === policy.model_alias
        )
        .reduce((total, model) => total + model.estimated_cost, 0);
    }
    if (policy.subject_type === 'flow') {
      return this.summary.usage_by_flow
        .filter((flow) => flow.flow_id === policy.subject_id)
        .reduce((total, flow) => total + flow.estimated_cost, 0);
    }
    if (policy.subject_type === 'managed_agent') {
      const agent = this.getManagedAgentBySourceId(policy.subject_id);
      const agentIds = new Set(
        [policy.subject_id, agent?.id, agent?.session_source_id].filter(
          Boolean
        ) as string[]
      );
      return this.summary.usage_by_session
        .filter(
          (s) =>
            agentIds.has(s.session_source_id || '') ||
            agentIds.has(s.runtime_principal_id || '')
        )
        .reduce((acc, s) => acc + s.estimated_cost, 0);
    }
    return this.summary.usage_by_session
      .filter(
        (session) =>
          session.session_source_id === policy.subject_id ||
          session.runtime_principal_id === policy.subject_id
      )
      .reduce((total, session) => total + session.estimated_cost, 0);
  }

  private calculatePolicyUsages(): BudgetPolicyUsage[] {
    return this.policies
      .map((policy) => {
        const spend = this.spendForPolicy(policy);
        const hardLimit = policy.hard_limit_usd || 0;
        const softLimit = policy.soft_limit_usd || 0;
        const maxLimit = hardLimit || softLimit;
        const percent =
          maxLimit > 0
            ? Math.min(100, Math.round((spend / maxLimit) * 100))
            : 0;
        return { policy, spend, hardLimit, softLimit, maxLimit, percent };
      })
      .sort((a, b) => {
        const aGlobal =
          a.policy.subject_type === 'global' ||
          a.policy.subject_type === 'account';
        const bGlobal =
          b.policy.subject_type === 'global' ||
          b.policy.subject_type === 'account';
        if (aGlobal !== bGlobal) return aGlobal ? -1 : 1;
        return b.percent - a.percent;
      });
  }

  private selectedGlobalUsage(
    usages: BudgetPolicyUsage[]
  ): BudgetPolicyUsage | undefined {
    const selectedPeriod = this.periodForTimeRange();
    return usages.find(
      (usage) =>
        (usage.policy.subject_type === 'global' ||
          usage.policy.subject_type === 'account') &&
        usage.policy.period === selectedPeriod
    );
  }

  private isHardLimitExceeded(spend: number, hardLimit: number): boolean {
    return hardLimit > 0 && spend >= hardLimit;
  }

  private isSoftOnlyLimitExceeded(
    spend: number,
    softLimit: number,
    hardLimit: number
  ): boolean {
    return softLimit > 0 && hardLimit <= 0 && spend >= softLimit;
  }

  private isLimitExceeded(
    spend: number,
    softLimit: number,
    hardLimit: number
  ): boolean {
    return (
      this.isHardLimitExceeded(spend, hardLimit) ||
      this.isSoftOnlyLimitExceeded(spend, softLimit, hardLimit)
    );
  }

  private isSoftLimitWarning(
    spend: number,
    softLimit: number,
    hardLimit: number
  ): boolean {
    return (
      softLimit > 0 && hardLimit > 0 && spend >= softLimit && spend < hardLimit
    );
  }

  private limitStatusLabel(
    spend: number,
    softLimit: number,
    hardLimit: number
  ): string | null {
    if (this.isHardLimitExceeded(spend, hardLimit)) {
      return spend > hardLimit ? 'Hard limit exceeded' : 'Hard limit reached';
    }
    if (this.isSoftOnlyLimitExceeded(spend, softLimit, hardLimit)) {
      return spend > softLimit ? 'Soft limit exceeded' : 'Soft limit reached';
    }
    if (this.isSoftLimitWarning(spend, softLimit, hardLimit)) {
      return 'Soft limit reached';
    }
    return null;
  }

  /**
   * Straight-line projection for a budget row, the same sentence the Overview
   * prints. "$12 of $300" on the 3rd reads as comfortable whether the account
   * lands at $120 or $1,200; this says which.
   */
  private renderForecast(input: {
    period: string;
    spend: number;
    softLimit: number;
    hardLimit: number;
    periodStart?: string | null;
    periodEnd?: string | null;
  }) {
    const forecast = budgetForecast(input);
    if (!forecast) return nothing;
    const percent = Math.round(forecast.elapsedFraction * 100);
    return html`
      <div
        class="budget-forecast ${forecast.tone}"
        title=${`Straight line from ${this.formatCurrency(
          input.spend
        )} spent in the first ${percent}% of the period.`}
      >
        On track for ${this.formatCurrency(forecast.amount)} by
        ${forecastEndLabel(input.period, forecast.end, forecast.endBasis)}
      </div>
    `;
  }

  private renderBudgetLimitRow(
    label: string,
    icon: string,
    spend: number,
    softLimit: number,
    hardLimit: number,
    forecast: {
      period: string;
      periodStart?: string | null;
      periodEnd?: string | null;
    } | null = null
  ) {
    const maxLimit = hardLimit || softLimit;
    const fillPercent =
      maxLimit > 0 ? Math.min(100, (spend / maxLimit) * 100) : 0;
    const softPercent =
      softLimit > 0 && maxLimit > 0
        ? Math.min(100, (softLimit / maxLimit) * 100)
        : 0;
    const limitExceeded = this.isLimitExceeded(spend, softLimit, hardLimit);
    const softLimitWarning = this.isSoftLimitWarning(
      spend,
      softLimit,
      hardLimit
    );
    const statusLabel = this.limitStatusLabel(spend, softLimit, hardLimit);
    const successFillPercent = limitExceeded
      ? 0
      : softLimit > 0
        ? Math.min(fillPercent, softPercent)
        : fillPercent;
    const warningFillPercent =
      !limitExceeded && softLimitWarning ? fillPercent - softPercent : 0;
    const dangerFillPercent = limitExceeded ? fillPercent : 0;

    const ariaLabel = `${label} budget usage`;

    return html`
      <div class="budget-row">
        <div class="row-header">
          <span class="row-label">
            <sl-icon name=${icon} aria-hidden="true"></sl-icon>
            ${label}
          </span>
          <span class="row-value${limitExceeded ? ' exceeded' : ''}">
            ${this.formatCurrency(spend)}
            ${
              maxLimit > 0
                ? html` / ${this.formatCurrency(maxLimit)}`
                : html`<span style="color: var(--sl-color-neutral-500);">
                    spent</span
                  >`
            }
          </span>
        </div>
        ${
          maxLimit > 0
            ? html`
                <div
                  class="budget-track"
                  role="progressbar"
                  aria-label=${ariaLabel}
                  aria-valuemin="0"
                  aria-valuemax="100"
                  aria-valuenow=${Math.round(fillPercent)}
                >
                  ${
                    successFillPercent > 0
                      ? html`<div
                          class="budget-track-fill"
                          style="--budget-fill-width: ${successFillPercent}%;"
                        ></div>`
                      : nothing
                  }
                  ${
                    warningFillPercent > 0
                      ? html`<div
                          class="budget-track-fill warning"
                          style="--budget-fill-left: ${softPercent}%; --budget-fill-width: ${warningFillPercent}%;"
                        ></div>`
                      : nothing
                  }
                  ${
                    dangerFillPercent > 0
                      ? html`<div
                          class="budget-track-fill danger"
                          style="--budget-fill-width: ${dangerFillPercent}%;"
                        ></div>`
                      : nothing
                  }
                  ${
                    softLimit > 0 && hardLimit > 0 && softLimit < hardLimit
                      ? html`<div
                          class="budget-soft-marker"
                          title=${`Soft limit ${this.formatCurrency(softLimit)}`}
                          style="--budget-soft-position: ${softPercent}%;"
                        ></div>`
                      : nothing
                  }
                  ${
                    hardLimit > 0
                      ? html`<div
                          class="budget-hard-marker"
                          title=${`Hard limit ${this.formatCurrency(hardLimit)}`}
                        ></div>`
                      : nothing
                  }
                </div>
                <div class="row-footer">
                  <span>
                    ${
                      statusLabel
                        ? html`<span class="limit-status">${statusLabel}</span>`
                        : softLimit > 0
                          ? html`Soft ${this.formatCurrency(softLimit)}`
                          : nothing
                    }
                  </span>
                  <span>
                    ${
                      hardLimit > 0
                        ? html`Hard ${this.formatCurrency(hardLimit)}`
                        : nothing
                    }
                  </span>
                </div>
              `
            : nothing
        }
        ${
          forecast
            ? this.renderForecast({
                period: forecast.period,
                spend,
                softLimit,
                hardLimit,
                periodStart: forecast.periodStart,
                periodEnd: forecast.periodEnd,
              })
            : nothing
        }
      </div>
    `;
  }

  private handleRangeChange(event: Event) {
    const value = (event.target as HTMLSelectElement).value;
    this.dispatchEvent(
      new CustomEvent('range-change', {
        detail: { value },
        bubbles: true,
        composed: true,
      })
    );
  }

  private handleConfigure() {
    this.dispatchEvent(
      new CustomEvent('configure', { bubbles: true, composed: true })
    );
  }

  render() {
    const policyUsages = this.calculatePolicyUsages();
    const selectedGlobalUsage = this.selectedGlobalUsage(policyUsages);
    const additionalUsages = selectedGlobalUsage
      ? policyUsages.filter(
          (usage) => usage.policy.id !== selectedGlobalUsage.policy.id
        )
      : policyUsages;
    const selectedPeriod = this.periodForTimeRange();
    const globalSpend = this.summary?.budget?.current_spend_usd || 0;
    const globalSoftLimit =
      selectedGlobalUsage?.softLimit ||
      this.summary?.budget?.soft_limit_usd ||
      0;
    const globalHardLimit =
      selectedGlobalUsage?.hardLimit ||
      this.summary?.budget?.monthly_limit_usd ||
      0;
    const anyLimitExceeded =
      this.summary?.budget?.hard_limit_exceeded ||
      this.isLimitExceeded(globalSpend, globalSoftLimit, globalHardLimit) ||
      additionalUsages.some((usage) =>
        this.isLimitExceeded(usage.spend, usage.softLimit, usage.hardLimit)
      );

    return html`
      <sl-card class="content-card">
        <div slot="header" class="header">
          <div
            class="title${anyLimitExceeded ? ' exceeded' : ''}"
            id="budget-health-title"
          >
            Budget health
            ${
              this.loading
                ? html`<sl-spinner style="font-size: 1rem;"></sl-spinner>`
                : nothing
            }
          </div>
          ${
            this.showRangeSelector
              ? html`
                  <select
                    aria-label="Budget time range"
                    .value=${this.timeRange}
                    @change=${this.handleRangeChange}
                  >
                    <option value="day">24h</option>
                    <option value="week">7d</option>
                    <option value="month">30d</option>
                    <option value="year">1y</option>
                  </select>
                `
              : nothing
          }
        </div>
        <div
          class="content"
          role="region"
          aria-labelledby="budget-health-title"
        >
          <div class="rows">
            ${this.renderBudgetLimitRow(
              this.budgetPeriodLabel(selectedPeriod),
              'globe',
              globalSpend,
              globalSoftLimit,
              globalHardLimit,
              {
                period: selectedPeriod,
                periodStart: selectedGlobalUsage?.policy.period_start,
                periodEnd: selectedGlobalUsage?.policy.period_end,
              }
            )}
            ${
              additionalUsages.length
                ? additionalUsages.map((usage) =>
                    this.renderBudgetLimitRow(
                      this.policyDisplayName(usage.policy),
                      this.policyIcon(usage.policy),
                      usage.spend,
                      usage.softLimit,
                      usage.hardLimit,
                      {
                        period: usage.policy.period,
                        periodStart: usage.policy.period_start,
                        periodEnd: usage.policy.period_end,
                      }
                    )
                  )
                : html`<div class="empty">No additional budget policies.</div>`
            }
          </div>
          ${
            this.configurable
              ? html`
                  <sl-button
                    size="small"
                    variant="default"
                    aria-label="Configure budget limits"
                    @click=${this.handleConfigure}
                  >
                    <sl-icon
                      slot="prefix"
                      name="gear"
                      aria-hidden="true"
                    ></sl-icon>
                    Configure limits
                  </sl-button>
                `
              : nothing
          }
        </div>
      </sl-card>
    `;
  }
}
