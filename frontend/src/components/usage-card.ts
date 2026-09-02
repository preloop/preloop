import { LitElement, css, html, nothing } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';

import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/card/card.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/radio-button/radio-button.js';
import '@shoelace-style/shoelace/dist/components/radio-group/radio-group.js';
import '@shoelace-style/shoelace/dist/components/skeleton/skeleton.js';
import '@shoelace-style/shoelace/dist/components/tooltip/tooltip.js';
import './time-range-select.ts';

import type { BudgetPolicy } from '../api';
import type { AccountGatewayUsageSummaryResponse } from '../types';
import { budgetTrackStyles, renderBudgetTrack } from '../styles/budget-track';

export type UsageUnit = 'tokens' | 'dollars';

export const USAGE_UNIT_STORAGE_KEY = 'overview_usage_unit';

const PERIOD_ORDER = [
  'hourly',
  'daily',
  'weekly',
  'monthly',
  'yearly',
  'all_time',
];

const SUBJECT_TYPE_LABELS: Record<string, string> = {
  managed_agent: 'agents',
  ai_model: 'models',
  user: 'users',
  team: 'teams',
  flow: 'flows',
  api_key: 'API keys',
};

/**
 * Usage first, budgets second: tokens are the primary currency of the
 * gateway, dollars are always an estimate. Replaces Budget health on the
 * Overview (cost-view still uses `<budget-health-card>`).
 */
@customElement('usage-card')
export class UsageCard extends LitElement {
  @property({ type: Object })
  summary: AccountGatewayUsageSummaryResponse | null = null;
  @property({ type: Array }) policies: BudgetPolicy[] = [];
  @property({ type: Boolean }) loading = false;
  @property({ type: String }) error: string | null = null;
  /** 'day' | 'week' | 'month' | 'year', shared with the rest of the page. */
  @property({ type: String }) timeRange = 'month';
  @property({ type: Number }) toolCallsCount = 0;

  @state() private unit: UsageUnit = 'tokens';

  static styles = [
    budgetTrackStyles,
    css`
      :host {
        display: block;
        width: 100%;
      }

      .content-card,
      .content-card::part(base) {
        width: 100%;
      }

      .content-card::part(base) {
        border: 1px solid var(--sl-color-neutral-200);
        border-radius: var(--sl-border-radius-medium);
        box-shadow: var(--sl-shadow-x-small);
      }

      .content-card::part(header) {
        background-color: transparent;
        border-bottom: 1px solid var(--sl-color-neutral-200);
        padding: var(--sl-spacing-medium);
      }

      .header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: var(--sl-spacing-small);
      }

      /* Unit toggle and range live together in the header: they are the two
         controls that change what every number below them means. */
      .header-controls {
        align-items: center;
        display: flex;
        gap: var(--sl-spacing-small);
      }

      .title {
        font-size: 0.9375rem; /* console card title */
        font-weight: 600;
        color: var(--sl-color-neutral-900);
      }

      .body {
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-medium);
      }

      .unit-toggle {
        display: flex;
      }

      /* The control is self-explanatory next to the numbers it switches, so
         the label is for screen readers only. */
      .unit-toggle sl-radio-group::part(form-control-label) {
        clip: rect(0 0 0 0);
        height: 1px;
        overflow: hidden;
        position: absolute;
        white-space: nowrap;
        width: 1px;
      }

      .primary-value {
        font-size: 2rem;
        font-weight: 700;
        line-height: 1.1;
        color: var(--sl-color-neutral-900);
        font-variant-numeric: tabular-nums;
      }

      .primary-label {
        display: flex;
        align-items: center;
        gap: var(--sl-spacing-2x-small);
        color: var(--sl-color-neutral-500);
        font-size: 0.8125rem; /* console meta */
        margin-top: var(--sl-spacing-2x-small);
      }

      .secondary-line {
        color: var(--sl-color-neutral-500);
        font-size: 0.8125rem;
        font-variant-numeric: tabular-nums;
      }

      .sparkline {
        display: block;
        width: 100%;
        height: 40px;
      }

      /* One deliberate motion on the Overview: the trend line draws itself
         once, on the paint that first has data. Nothing loops, nothing
         pulses. Under prefers-reduced-motion the line is simply there. */
      @media (prefers-reduced-motion: no-preference) {
        .sparkline-line {
          animation: sparkline-draw 300ms ease-out forwards;
          stroke-dasharray: 1;
          stroke-dashoffset: 1;
        }

        .sparkline-area {
          animation: sparkline-fade 300ms ease-out forwards;
          opacity: 0;
        }
      }

      @keyframes sparkline-draw {
        to {
          stroke-dashoffset: 0;
        }
      }

      @keyframes sparkline-fade {
        to {
          opacity: 0.12;
        }
      }

      .budgets {
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-small);
      }

      .budget-row {
        display: flex;
        flex-direction: column;
        gap: 4px;
      }

      .budget-row-header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: var(--sl-spacing-small);
        font-size: var(--sl-font-size-small);
      }

      .budget-row-label {
        color: var(--sl-color-neutral-800);
      }

      .budget-row-value {
        font-weight: 500;
        white-space: nowrap;
        font-variant-numeric: tabular-nums;
      }

      .muted {
        color: var(--sl-color-neutral-500);
        font-size: 0.8125rem;
      }

      .more-limits {
        align-self: flex-start;
      }

      .footer {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: var(--sl-spacing-small);
      }

      .footer a {
        color: var(--sl-color-primary-600);
        font-size: var(--sl-font-size-small);
        text-decoration: none;
      }

      .footer a:hover {
        text-decoration: underline;
      }

      sl-radio-button::part(button) {
        font-size: var(--sl-font-size-x-small);
      }
    `,
  ];

  connectedCallback(): void {
    super.connectedCallback();
    try {
      const stored = localStorage.getItem(USAGE_UNIT_STORAGE_KEY);
      if (stored === 'tokens' || stored === 'dollars') {
        this.unit = stored;
      }
    } catch {
      // Private mode or a blocked storage partition: keep the default.
    }
  }

  private get rangeLabel(): string {
    if (this.timeRange === 'day') return '24h';
    if (this.timeRange === 'week') return '7d';
    if (this.timeRange === 'year') return '1y';
    return '30d';
  }

  private setUnit(unit: UsageUnit): void {
    if (unit === this.unit) return;
    this.unit = unit;
    try {
      localStorage.setItem(USAGE_UNIT_STORAGE_KEY, unit);
    } catch {
      // Non-fatal: the toggle still works for this session.
    }
  }

  private formatCompactNumber(value: number | null | undefined): string {
    const amount = Number(value || 0);
    if (amount < 1000) {
      return String(Math.round(amount));
    }
    return new Intl.NumberFormat(undefined, {
      notation: 'compact',
      maximumFractionDigits: 1,
    }).format(amount);
  }

  private formatCurrency(value: number | null | undefined): string {
    const amount = Number(value || 0);
    if (amount > 0 && amount < 0.01) {
      return `$${amount.toFixed(4)}`;
    }
    return `$${amount.toFixed(2)}`;
  }

  private handleRangeChange(event: CustomEvent<{ value: string }>) {
    event.stopPropagation();
    this.dispatchEvent(
      new CustomEvent('range-change', {
        detail: { value: event.detail.value },
        bubbles: true,
        composed: true,
      })
    );
  }

  private emit(name: string) {
    this.dispatchEvent(
      new CustomEvent(name, { bubbles: true, composed: true })
    );
  }

  private renderUnitToggle() {
    return html`
      <div class="unit-toggle">
        <sl-radio-group
          size="small"
          label="Usage unit"
          value=${this.unit}
          @sl-change=${(event: Event) =>
            this.setUnit((event.target as HTMLInputElement).value as UsageUnit)}
        >
          <sl-radio-button value="tokens">Tokens</sl-radio-button>
          <sl-radio-button value="dollars">$ est.</sl-radio-button>
        </sl-radio-group>
      </div>
    `;
  }

  private renderPrimary() {
    if (this.loading && !this.summary) {
      return html`
        <div>
          <sl-skeleton
            effect="sheen"
            style="width: 55%; height: 2rem;"
          ></sl-skeleton>
          <sl-skeleton
            effect="sheen"
            style="width: 80%; height: 1rem; margin-top: var(--sl-spacing-small);"
          ></sl-skeleton>
        </div>
      `;
    }

    const tokens = this.summary?.token_usage?.total_tokens || 0;
    const cost = this.summary?.estimated_cost || 0;

    if (this.unit === 'dollars') {
      return html`
        <div>
          <div class="primary-value">${this.formatCurrency(cost)}</div>
          <div class="primary-label">
            <span>est. spend · ${this.rangeLabel}</span>
            <sl-tooltip
              content="Estimated from provider list prices and your plan. See Cost for reconciliation."
            >
              <sl-icon
                name="info-circle"
                label="About estimated spend"
              ></sl-icon>
            </sl-tooltip>
          </div>
        </div>
      `;
    }

    return html`
      <div>
        <div class="primary-value">${this.formatCompactNumber(tokens)}</div>
        <div class="primary-label">
          <span>tokens · ${this.rangeLabel}</span>
        </div>
      </div>
    `;
  }

  private renderSecondaryLine() {
    if (this.loading && !this.summary) {
      return html`<sl-skeleton
        effect="sheen"
        style="width: 90%; height: 0.85rem;"
      ></sl-skeleton>`;
    }

    const usage = this.summary?.token_usage;
    const requests = this.summary?.total_requests || 0;
    const parts =
      this.unit === 'dollars'
        ? [
            `${this.formatCompactNumber(usage?.total_tokens || 0)} tokens`,
            `${this.formatCompactNumber(requests)} requests`,
          ]
        : [
            `${this.formatCompactNumber(usage?.prompt_tokens || 0)} prompt`,
            `${this.formatCompactNumber(usage?.completion_tokens || 0)} completion`,
            `${this.formatCompactNumber(requests)} requests`,
          ];
    if (this.toolCallsCount > 0) {
      parts.push(`${this.formatCompactNumber(this.toolCallsCount)} tool calls`);
    }
    return html`<div class="secondary-line">${parts.join(' · ')}</div>`;
  }

  /** Inline SVG: a trend line is worth more here than a charting library. */
  private renderSparkline() {
    const days = this.summary?.requests_by_day || [];
    if (days.length < 3) {
      return nothing;
    }
    const values = days.map((day) =>
      this.unit === 'dollars'
        ? Number(day.estimated_cost || 0)
        : Number(day.total_tokens || 0)
    );
    const max = Math.max(...values);
    const min = Math.min(...values);
    const span = max - min || 1;
    const width = 100;
    const height = 40;
    const step = values.length > 1 ? width / (values.length - 1) : width;
    const points = values.map((value, index) => {
      const x = index * step;
      const y = height - ((value - min) / span) * (height - 4) - 2;
      return `${x.toFixed(2)},${y.toFixed(2)}`;
    });
    const areaPoints = `0,${height} ${points.join(' ')} ${width},${height}`;

    return html`
      <svg
        class="sparkline"
        viewBox="0 0 ${width} ${height}"
        preserveAspectRatio="none"
        role="img"
        aria-label=${
          this.unit === 'dollars'
            ? `Estimated spend per day over the last ${this.rangeLabel}`
            : `Tokens per day over the last ${this.rangeLabel}`
        }
      >
        <polygon
          class="sparkline-area"
          points=${areaPoints}
          fill="var(--sl-color-primary-500)"
          opacity="0.12"
        ></polygon>
        <polyline
          class="sparkline-line"
          points=${points.join(' ')}
          pathLength="1"
          fill="none"
          stroke="var(--sl-color-primary-500)"
          stroke-width="1.5"
          vector-effect="non-scaling-stroke"
        ></polyline>
      </svg>
    `;
  }

  private get globalPolicies(): BudgetPolicy[] {
    return this.policies
      .filter(
        (policy) =>
          policy.subject_type === 'global' || policy.subject_type === 'account'
      )
      .sort(
        (left, right) =>
          PERIOD_ORDER.indexOf(left.period) - PERIOD_ORDER.indexOf(right.period)
      );
  }

  private get scopedPolicies(): BudgetPolicy[] {
    return this.policies.filter(
      (policy) =>
        policy.subject_type !== 'global' && policy.subject_type !== 'account'
    );
  }

  private get legacyMonthlyLimit(): number {
    return this.summary?.budget?.monthly_limit_usd || 0;
  }

  /**
   * Which window a budget row is about. "Monthly budget: $12 / $50" was read
   * as a running total; "Monthly budget - Sep" says the number resets.
   */
  private periodWindow(period: string, now: Date): string {
    if (period === 'hourly') return 'this hour';
    if (period === 'daily') return 'today';
    if (period === 'weekly') return 'this week';
    if (period === 'monthly') {
      return now.toLocaleDateString(undefined, { month: 'short' });
    }
    if (period === 'yearly') return String(now.getFullYear());
    return '';
  }

  private periodLabel(period: string, now: Date = new Date()): string {
    const base =
      period === 'all_time'
        ? 'All time budget'
        : `${period.charAt(0).toUpperCase()}${period.slice(1)} budget`;
    const window = this.periodWindow(period, now);
    return window ? `${base} · ${window}` : base;
  }

  private renderBudgetRow(
    label: string,
    spend: number,
    softLimit: number,
    hardLimit: number
  ) {
    const denominator = hardLimit || softLimit;
    return html`
      <div class="budget-row">
        <div class="budget-row-header">
          <span class="budget-row-label">${label}</span>
          <span class="budget-row-value">
            ${this.formatCurrency(spend)}
            ${
              denominator > 0
                ? html`/ ${this.formatCurrency(denominator)}`
                : html`<span class="muted">spent</span>`
            }
          </span>
        </div>
        ${
          denominator > 0
            ? renderBudgetTrack({
                spend,
                softLimit,
                hardLimit,
                label,
              })
            : nothing
        }
      </div>
    `;
  }

  private renderBudgets() {
    const globals = this.globalPolicies;
    const scoped = this.scopedPolicies;
    const legacyLimit = this.legacyMonthlyLimit;
    const hasGlobalMonthly = globals.some(
      (policy) => policy.period === 'monthly'
    );

    if (globals.length === 0 && legacyLimit <= 0 && scoped.length === 0) {
      return html`<div class="muted">No budget set.</div>`;
    }

    const rows = globals.map((policy) =>
      this.renderBudgetRow(
        this.periodLabel(policy.period),
        policy.current_spend_usd || 0,
        policy.soft_limit_usd || 0,
        policy.hard_limit_usd || 0
      )
    );

    if (!hasGlobalMonthly && legacyLimit > 0) {
      rows.unshift(
        this.renderBudgetRow(
          this.periodLabel('monthly'),
          this.summary?.budget?.current_spend_usd || 0,
          this.summary?.budget?.soft_limit_usd || 0,
          legacyLimit
        )
      );
    }

    return html`
      <div class="budgets">
        ${rows}
        ${
          scoped.length > 0
            ? html`
                <sl-button
                  class="more-limits"
                  size="small"
                  variant="text"
                  @click=${() => this.emit('configure-limits')}
                >
                  ${`+ ${scoped.length} more limit${
                    scoped.length === 1 ? '' : 's'
                  } (${this.scopedSubjectSummary(scoped)})`}
                </sl-button>
              `
            : nothing
        }
      </div>
    `;
  }

  private scopedSubjectSummary(policies: BudgetPolicy[]): string {
    const seen: string[] = [];
    for (const policy of policies) {
      const label =
        SUBJECT_TYPE_LABELS[policy.subject_type] ||
        policy.subject_type.replace(/_/g, ' ');
      if (!seen.includes(label)) {
        seen.push(label);
      }
    }
    return seen.join(', ');
  }

  render() {
    return html`
      <sl-card class="content-card">
        <div slot="header" class="header">
          <div class="title">Usage</div>
          <div class="header-controls">
            ${this.renderUnitToggle()}
            <time-range-select
              ariaLabel="Usage time range"
              .value=${this.timeRange}
              .options=${[
                { value: 'day', label: '24h' },
                { value: 'week', label: '7d' },
                { value: 'month', label: '30d' },
                { value: 'year', label: '1y' },
              ]}
              @range-change=${this.handleRangeChange}
            ></time-range-select>
          </div>
        </div>

        <div class="body">
          ${
            this.error
              ? html`<sl-alert variant="danger" open>
                  <sl-icon slot="icon" name="exclamation-octagon"></sl-icon>
                  ${this.error}
                </sl-alert>`
              : nothing
          }
          ${this.renderPrimary()} ${this.renderSecondaryLine()}
          ${this.renderSparkline()} ${this.renderBudgets()}
          <div class="footer">
            <sl-button
              size="small"
              variant="text"
              @click=${() => this.emit('configure-limits')}
            >
              <sl-icon slot="prefix" name="gear"></sl-icon>
              Configure limits
            </sl-button>
            <a href="/console/cost">Cost details</a>
          </div>
        </div>
      </sl-card>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'usage-card': UsageCard;
  }
}
