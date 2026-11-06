import { LitElement, css, html, nothing, svg } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import { getCostTimeseries, type BudgetPolicy } from '../api';
import type {
  AccountGatewayUsageSummaryResponse,
  GatewayUsageByDay,
  ManagedAgentSummary,
} from '../types';
import { budgetTrackStyles } from '../styles/budget-track';
import { formatUsd, formatUsdExact } from '../utils/money';
import {
  budgetPeriodLabel,
  budgetPeriodWindow,
  periodEndFor,
  periodStartFor,
  budgetForecastStyles,
  renderBudgetForecast,
} from '../utils/budget-forecast';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/card/card.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';

type BudgetPolicyUsage = {
  policy: BudgetPolicy;
  spend: number | null;
  hardLimit: number;
  softLimit: number;
  maxLimit: number;
  percent: number;
};

/** Chart geometry, in viewBox units; the svg stretches to the card. */
const CHART_WIDTH = 100;
const CHART_HEIGHT = 40;

export interface BudgetSpendChart {
  /** Polyline points of cumulative spend, one per day. */
  points: string;
  /** y of the hard (else soft) limit line, or null when off the chart. */
  limitY: number | null;
  /** y of the soft limit line when a hard limit is drawn too. */
  softY: number | null;
  /** Cumulative spend at the last day. */
  total: number;
}

/**
 * Cumulative estimated spend per day against the budget's limits, scaled so
 * the higher of spend and limit fills the chart. Fewer than two days is no
 * trend, so it returns null.
 */
export function budgetSpendChart(
  rows: GatewayUsageByDay[] | null | undefined,
  softLimit: number,
  hardLimit: number,
  slots = 0
): BudgetSpendChart | null {
  if (!rows || rows.length < 2) return null;
  const ordered = [...rows].sort((a, b) => a.date.localeCompare(b.date));
  let running = 0;
  const cumulative = ordered.map((row) => {
    running += Number(row.estimated_cost || 0);
    return running;
  });
  const limit = hardLimit > 0 ? hardLimit : softLimit > 0 ? softLimit : 0;
  const max = Math.max(running, limit) || 1;
  const y = (value: number) =>
    Number((CHART_HEIGHT - (value / max) * (CHART_HEIGHT - 4) - 2).toFixed(2));
  // With the period's bucket count the line stops where today is, so the
  // empty rest of the chart is the part of the period still to come.
  const step = CHART_WIDTH / (Math.max(slots, cumulative.length) - 1);
  return {
    points: cumulative
      .map((value, index) => `${(index * step).toFixed(2)},${y(value)}`)
      .join(' '),
    limitY: limit > 0 ? y(limit) : null,
    softY:
      hardLimit > 0 && softLimit > 0 && softLimit < hardLimit
        ? y(softLimit)
        : null,
    total: running,
  };
}

@customElement('budget-health-card')
export class BudgetHealthCard extends LitElement {
  @property({ type: Object })
  summary: AccountGatewayUsageSummaryResponse | null = null;
  @property({ type: Array }) policies: BudgetPolicy[] = [];
  @property({ type: Boolean }) configurable = false;
  @property({ type: Array }) agents: ManagedAgentSummary[] = [];
  /** Team id to name, for `team` rows (set when team budgets are enabled). */
  @property({ attribute: false }) teamNames: Record<string, string> = {};
  @property({ type: Boolean }) loading = false;
  @property({ type: String }) timeRange = 'month';
  @property({ type: Boolean }) showRangeSelector = false;
  /**
   * Fetch the budget period's daily spend for the chart. Off by default so
   * a host that already has a series (or a test) makes no request.
   */
  @property({ type: Boolean }) loadSeries = false;
  /** Spend per bucket of the current budget period, when loaded. */
  @state() series: GatewayUsageByDay[] | null = null;
  @state() private seriesSlots = 0;
  private seriesKey = '';

  static styles = [
    budgetTrackStyles,
    budgetForecastStyles,
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

      .spend-chart {
        display: block;
        width: 100%;
        height: 40px;
      }

      .spend-chart-caption {
        color: var(--console-meta-color);
        font-size: 0.75rem;
        font-variant-numeric: tabular-nums;
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
        color: var(--console-meta-color);
        font-size: var(--sl-font-size-x-small);
      }

      .title.exceeded {
        color: var(--sl-color-danger-700);
      }

      .row-value.exceeded {
        color: var(--sl-color-danger-700);
      }

      /* Cost runs one step smaller than the Overview; the sentence, its
         tones and its tabular figures come from budgetForecastStyles. */
      .budget-forecast {
        font-size: var(--sl-font-size-x-small);
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
    return formatUsd(value);
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
      budgetPeriodWindow(policy.period) ||
      this.formatBudgetPeriod(policy.period);
    if (policy.subject_type === 'global' || policy.subject_type === 'account') {
      return budgetPeriodLabel(policy.period);
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
    if (policy.subject_type === 'team') {
      const name = policy.subject_id ? this.teamNames[policy.subject_id] : '';
      return `${name ? `Team ${name}` : 'Team'} · ${period}`;
    }
    return `${policy.subject_type.replace(/_/g, ' ')} · ${period}`;
  }

  private policyIcon(policy: BudgetPolicy): string {
    if (policy.subject_type === 'global' || policy.subject_type === 'account') {
      return 'globe';
    }
    if (policy.subject_type === 'managed_agent') return 'robot';
    if (policy.subject_type === 'ai_model') return 'cpu';
    if (policy.subject_type === 'team') return 'people';
    return 'sliders';
  }

  private spendForPolicy(policy: BudgetPolicy): number | null {
    // Missing projections mean unavailable spend. Analytics windows cannot
    // substitute for a policy's period or subject.
    return policy.current_spend_usd ?? null;
  }

  private calculatePolicyUsages(): BudgetPolicyUsage[] {
    return this.policies
      .map((policy) => {
        const spend = this.spendForPolicy(policy);
        const hardLimit = policy.hard_limit_usd || 0;
        const softLimit = policy.soft_limit_usd || 0;
        const maxLimit = hardLimit || softLimit;
        const percent =
          maxLimit > 0 && spend !== null
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

  private renderBudgetLimitRow(
    label: string,
    icon: string,
    spend: number | null,
    softLimit: number,
    hardLimit: number,
    forecast: {
      period: string;
      periodStart?: string | null;
      periodEnd?: string | null;
    } | null = null
  ) {
    const maxLimit = hardLimit || softLimit;
    if (spend === null) {
      return html`<div class="budget-row">
        <div class="row-header">
          <span class="row-label"
            ><sl-icon name=${icon} aria-hidden="true"></sl-icon>${label}</span
          >
          <span class="row-value"
            >Spend
            unavailable${maxLimit > 0 ? html` / ${this.formatCurrency(maxLimit)}` : nothing}</span
          >
        </div>
      </div>`;
    }
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
          <span
            class="row-value${limitExceeded ? ' exceeded' : ''}"
            title=${formatUsdExact(spend)}
          >
            ${this.formatCurrency(spend)}
            ${
              maxLimit > 0
                ? html` / ${this.formatCurrency(maxLimit)}`
                : html`<span style="color: var(--console-meta-color);">
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
                  aria-valuetext=${`${this.formatCurrency(spend)} of ${this.formatCurrency(maxLimit)}`}
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
            ? renderBudgetForecast(
                {
                  period: forecast.period,
                  spend,
                  softLimit,
                  hardLimit,
                  periodStart: forecast.periodStart,
                  periodEnd: forecast.periodEnd,
                },
                (value) => this.formatCurrency(value)
              )
            : nothing
        }
      </div>
    `;
  }

  protected updated(): void {
    if (!this.loadSeries) return;
    const period = this.periodForTimeRange();
    const now = new Date();
    const start = periodStartFor(period, now);
    const end = periodEndFor(period, now);
    if (!start || !end) return;
    const granularity =
      period === 'daily' || period === 'hourly' ? 'hour' : 'day';
    const key = `${start.toISOString()}|${granularity}`;
    if (key === this.seriesKey) return;
    this.seriesKey = key;
    const bucketMs = granularity === 'hour' ? 3_600_000 : 86_400_000;
    const slots = Math.round((end.getTime() - start.getTime()) / bucketMs);
    void getCostTimeseries({
      startDate: start.toISOString(),
      endDate: now.toISOString(),
      granularity,
    })
      .then((data) => {
        if (key !== this.seriesKey) return;
        this.series = data.buckets;
        this.seriesSlots = slots;
      })
      .catch(() => {
        // The chart is a nicety; the rows above still say everything.
      });
  }

  /** Cumulative spend over the period's buckets, with the limit as a line. */
  private renderSpendChart(softLimit: number, hardLimit: number) {
    const chart = budgetSpendChart(
      this.series ?? this.summary?.requests_by_day,
      softLimit,
      hardLimit,
      this.series ? this.seriesSlots : 0
    );
    if (!chart) return nothing;
    const over = hardLimit > 0 && chart.total >= hardLimit;
    const stroke = over
      ? 'var(--sl-color-danger-500)'
      : 'var(--sl-color-primary-500)';
    const label = `Cumulative estimated spend ${this.formatCurrency(chart.total)}${
      hardLimit > 0 ? ` against a ${this.formatCurrency(hardLimit)} limit` : ''
    }`;
    return html`<div>
      <svg
        class="spend-chart"
        viewBox="0 0 ${CHART_WIDTH} ${CHART_HEIGHT}"
        preserveAspectRatio="none"
        role="img"
        aria-label=${label}
      >
        <polygon
          points=${`0,${CHART_HEIGHT} ${chart.points} ${CHART_WIDTH},${CHART_HEIGHT}`}
          fill=${stroke}
          opacity="0.12"
        ></polygon>
        <polyline
          class="spend-line"
          points=${chart.points}
          fill="none"
          stroke=${stroke}
          stroke-width="1.5"
          vector-effect="non-scaling-stroke"
        ></polyline>
        ${
          chart.softY !== null
            ? svg`<line class="soft-line" x1="0" x2=${CHART_WIDTH} y1=${chart.softY} y2=${chart.softY}
                stroke="var(--sl-color-warning-500)" stroke-width="1" stroke-dasharray="2 2"
                vector-effect="non-scaling-stroke"></line>`
            : nothing
        }
        ${
          chart.limitY !== null
            ? svg`<line class="limit-line" x1="0" x2=${CHART_WIDTH} y1=${chart.limitY} y2=${chart.limitY}
                stroke="var(--sl-color-neutral-500)" stroke-width="1" stroke-dasharray="4 3"
                vector-effect="non-scaling-stroke"></line>`
            : nothing
        }
      </svg>
      <div class="spend-chart-caption">
        Cumulative spend
        ${this.series ? 'this period' : 'in this range'}${
          hardLimit > 0 || softLimit > 0 ? ', dashed line is the limit' : ''
        }
      </div>
    </div>`;
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
    // A policy's spend and limits cover its own period, independently of
    // the Cost page's analytics window. Use the same projection as Overview.
    const globalSpend = selectedGlobalUsage
      ? (selectedGlobalUsage.policy.current_spend_usd ?? null)
      : (this.summary?.budget?.current_spend_usd ?? 0);
    const globalSoftLimit =
      selectedGlobalUsage?.softLimit ??
      this.summary?.budget?.soft_limit_usd ??
      0;
    const globalHardLimit =
      selectedGlobalUsage?.hardLimit ??
      this.summary?.budget?.monthly_limit_usd ??
      0;
    const anyLimitExceeded =
      (!selectedGlobalUsage && this.summary?.budget?.hard_limit_exceeded) ||
      (globalSpend !== null &&
        this.isLimitExceeded(globalSpend, globalSoftLimit, globalHardLimit)) ||
      additionalUsages.some(
        (usage) =>
          usage.spend !== null &&
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
          ${this.renderSpendChart(globalSoftLimit, globalHardLimit)}
          <div class="rows">
            ${this.renderBudgetLimitRow(
              budgetPeriodLabel(selectedPeriod),
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
