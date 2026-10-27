import { totalTokensOf } from './token-figures';
import { formatUsd, formatUsdExact } from '../utils/money';
import { LitElement, css, html, nothing, svg } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';

import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/card/card.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/radio-button/radio-button.js';
import '@shoelace-style/shoelace/dist/components/radio-group/radio-group.js';
import '@shoelace-style/shoelace/dist/components/skeleton/skeleton.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import '@shoelace-style/shoelace/dist/components/tooltip/tooltip.js';
import './time-range-select.ts';
import './token-figures.ts';

import type { BudgetPolicy } from '../api';
import type {
  AccountGatewayUsageSummaryResponse,
  GatewayUsageByDay,
} from '../types';
import { budgetTrackStyles, renderBudgetTrack } from '../styles/budget-track';
import {
  isCalendarTimeRange,
  normalizeTimeRangeKey,
  timeRangeComparisonLabel,
  timeRangeShortLabel,
  type TimeRangeKey,
} from '../utils/time-range';
import type { TimeRangeOption } from './time-range-select';
import {
  budgetPeriodLabel,
  budgetForecastStyles,
  forecastEndLabel,
  renderBudgetForecast,
} from '../utils/budget-forecast';
import {
  buildUsageProjection,
  isProjectedRange,
  projectionPeriodFor,
  seriesValues,
  type UsageSeriesGranularity,
} from '../utils/usage-projection';

export type UsageUnit = 'tokens' | 'dollars';

let sparklineClipSeq = 0;

export const USAGE_UNIT_STORAGE_KEY = 'overview_usage_unit';
export const USAGE_RANGE_STORAGE_KEY = 'overview_usage_range';

/**
 * The Usage card's ranges: calendar-to-date first, so "This month" can line
 * up with the monthly budget row, then the rolling windows.
 */
export const USAGE_RANGE_OPTIONS: TimeRangeOption[] = [
  { value: 'today', label: 'Today' },
  { value: 'this-week', label: 'This week' },
  { value: 'this-month', label: 'This month' },
  { value: 'this-year', label: 'This year' },
  { value: 'last-24h', label: '24h', dividerBefore: true },
  { value: 'last-7', label: '7d' },
  { value: 'last-30', label: '30d' },
  { value: 'last-365', label: '1y' },
];

const USAGE_RANGE_KEYS = USAGE_RANGE_OPTIONS.map((option) => option.value);

/** The card's range for a fresh browser (#1610). */
export const DEFAULT_USAGE_RANGE: TimeRangeKey = 'this-month';

/** The range saved by the last visit, or This month when none is saved or valid. */
export function readStoredUsageRange(): TimeRangeKey {
  try {
    const stored = normalizeTimeRangeKey(
      localStorage.getItem(USAGE_RANGE_STORAGE_KEY)
    );
    if (stored && USAGE_RANGE_KEYS.includes(stored)) return stored;
  } catch {
    // Storage unavailable: fall through to the default.
  }
  return DEFAULT_USAGE_RANGE;
}

/** How long a wait has to be before the card explains it. */
const LONG_RANGE_HINT_MS = 2000;

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
  /**
   * Same window, shifted back one period. A number on its own says nothing
   * about direction, so the card states the change against the window the
   * user is already looking at.
   */
  @property({ type: Object })
  priorSummary: AccountGatewayUsageSummaryResponse | null = null;
  @property({ type: Array }) policies: BudgetPolicy[] = [];
  @property({ type: Boolean }) loading = false;
  /**
   * A range change is in flight and the data on hand belongs to the range
   * before it.
   *
   * Dimming the old numbers (#1598) went unnoticed, and they could sit under
   * the new range's label. The label now names the new range at once; the
   * headline, delta, secondary line and chart become skeletons (never a
   * zero, so the account never looks idle); budgets, which do not depend on
   * the range, only dim; and the range select shows a spinner in place of
   * its chevron (#1610).
   */
  @property({ type: Boolean }) updating = false;
  @property({ type: String }) error: string | null = null;
  /** A `TimeRangeKey` (any spelling `utils/time-range` reads), shared with the page. */
  @property({ type: String }) timeRange: string = DEFAULT_USAGE_RANGE;
  /**
   * Chart buckets from `/cost/timeseries` for `timeRange`, including the
   * history before the period that the projection's shape reads. Null falls
   * back to the summary's `requests_by_day`.
   */
  @property({ type: Array }) series: GatewayUsageByDay[] | null = null;
  @property({ type: String }) seriesGranularity: UsageSeriesGranularity = 'day';
  /** The chart's request is in flight and nothing is drawn yet. */
  @property({ type: Boolean }) seriesLoading = false;
  /** The clock the projection is cut on; tests pin it. */
  @property({ attribute: false }) now: Date | null = null;
  @property({ type: Number }) toolCallsCount = 0;

  @state() private unit: UsageUnit = 'tokens';
  /**
   * "Long ranges take longer" only once the wait is long enough to need
   * explaining. Said immediately it would be an excuse offered before
   * anything went wrong; a year of usage that comes back in 300ms needs no
   * apology.
   */
  @state() private longRangeHint = false;
  private longRangeTimer: number | null = null;

  static styles = [
    budgetTrackStyles,
    budgetForecastStyles,
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

      /* Flex children default to min-width: auto, which let a zoomed select
         push the header past the card edge. With min-width: 0 the controls
         shrink instead and wrap onto a second line inside the header. */
      .header-controls,
      .header-controls > * {
        min-width: 0;
      }

      /* Unit toggle and range live together in the header: they are the two
         controls that change what every number below them means. */
      .header-controls {
        align-items: center;
        display: flex;
        flex-wrap: wrap;
        gap: var(--sl-spacing-small);
        justify-content: flex-end;
        max-width: 100%;
        position: relative;
        /* Wide enough for "This month", the longest range label, and never
           wider than the header (time-range-select clamps it to 100%). */
        --time-range-select-width: 136px;
      }

      .title {
        align-items: center;
        display: flex;
        font-size: 0.9375rem; /* console card title */
        font-weight: 600;
        gap: var(--sl-spacing-x-small);
        color: var(--sl-color-neutral-900);
      }

      /* The spinner itself is drawn inside the range select (its busy
         state); this is the same news for screen readers. */
      .visually-hidden {
        clip: rect(0 0 0 0);
        height: 1px;
        overflow: hidden;
        position: absolute;
        white-space: nowrap;
        width: 1px;
      }

      /* A range change used to dim the old numbers to 0.6, and nobody
         noticed. The figures that belong to a range now give way to a
         skeleton the moment the range changes; the label already names the
         new range. Budgets do not depend on the range and only dim. */
      .is-updating .budgets {
        opacity: 0.5;
      }

      .long-range-hint {
        color: var(--console-meta-color);
        font-size: 0.8125rem;
        margin-top: var(--sl-spacing-2x-small);
      }

      /* Where the number came from, not what it is: same register as the
         range it qualifies. */
      .provenance {
        color: var(--console-meta-color);
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
        color: var(--console-meta-color);
        font-size: 0.8125rem; /* console meta */
        margin-top: var(--sl-spacing-2x-small);
      }

      /* The delta is information, not an alarm: no red, no green, no arrow
         colouring. Spend going up is not by itself a problem. */
      .delta {
        color: var(--console-meta-color);
        font-size: 0.8125rem;
        font-variant-numeric: tabular-nums;
        margin-top: var(--sl-spacing-2x-small);
      }

      .secondary-line {
        color: var(--console-meta-color);
        font-size: 0.8125rem;
        font-variant-numeric: tabular-nums;
      }

      .sparkline {
        display: block;
        width: 100%;
        height: 40px;
      }

      /* Says which part of the chart is data and which is a guess. */
      .sparkline-legend {
        align-items: center;
        color: var(--console-meta-color);
        display: flex;
        flex-wrap: wrap;
        font-size: 0.75rem;
        gap: var(--sl-spacing-x-small);
        margin-top: var(--sl-spacing-2x-small);
      }

      .legend-item {
        align-items: center;
        display: inline-flex;
        gap: var(--sl-spacing-2x-small);
        white-space: nowrap;
      }

      .legend-total {
        flex-basis: 100%;
      }

      .legend-swatch {
        display: inline-block;
        height: 0;
        vertical-align: middle;
        width: 16px;
        border-top: 2px solid var(--sl-color-primary-500);
      }

      .legend-swatch.dashed {
        border-top-style: dashed;
        border-top-color: var(--sl-color-primary-400);
      }

      /* One deliberate motion on the Overview: the trend reveals itself
         once, left to right, on the paint that first has data. A clip
         rectangle grows across the chart and uncovers the line and the area
         together, so they always end at the same x. Nothing loops, nothing
         pulses. Under prefers-reduced-motion the chart is simply there.

         The reveal used to be a stroke-dasharray draw on the line alone.
         With pathLength="1", vector-effect="non-scaling-stroke" and
         preserveAspectRatio="none" the dash was measured in screen pixels,
         so the finished line showed gaps while the area ran to the end. */
      .sparkline-reveal {
        transform-origin: 0 0;
      }

      @media (prefers-reduced-motion: no-preference) {
        .sparkline-reveal {
          animation: sparkline-reveal 300ms ease-out both;
        }
      }

      @keyframes sparkline-reveal {
        from {
          transform: scaleX(0);
        }
        to {
          transform: scaleX(1);
        }
      }

      .budgets {
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-small);
      }

      .budget-basis {
        margin: 0;
        line-height: 1.5;
      }

      .budget-basis strong {
        display: block;
        color: var(--sl-color-neutral-800);
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
        color: var(--console-meta-color);
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

  disconnectedCallback(): void {
    this.clearLongRangeTimer();
    super.disconnectedCallback();
  }

  // In `willUpdate` so that dropping the hint again lands in the same render
  // pass as the property change that dropped it.
  willUpdate(changed: Map<string, unknown>): void {
    if (!changed.has('updating') && !changed.has('timeRange')) {
      return;
    }
    // Only a year: the shorter ranges are back before anybody would read a
    // sentence about them, and saying it there would be noise.
    const key = normalizeTimeRangeKey(this.timeRange);
    if (this.updating && (key === 'last-365' || key === 'this-year')) {
      if (this.longRangeTimer === null && !this.longRangeHint) {
        this.longRangeTimer = window.setTimeout(() => {
          this.longRangeTimer = null;
          this.longRangeHint = true;
        }, LONG_RANGE_HINT_MS);
      }
      return;
    }
    this.clearLongRangeTimer();
    if (this.longRangeHint) {
      this.longRangeHint = false;
    }
  }

  private clearLongRangeTimer(): void {
    if (this.longRangeTimer !== null) {
      window.clearTimeout(this.longRangeTimer);
      this.longRangeTimer = null;
    }
  }

  /**
   * True when the server says the summary came out of a pre-aggregated
   * rollup. Read defensively: servers that do not roll up send neither
   * field, and the card then claims nothing about where the number is from.
   */
  private get fromRollup(): boolean {
    const summary = this.summary;
    if (!summary) return false;
    return (
      summary.from_rollup === true ||
      (summary.source || '').toLowerCase() === 'rollup'
    );
  }

  /** "· from rollup", or nothing at all. */
  private renderProvenance() {
    if (!this.fromRollup) return nothing;
    return html`<span class="provenance" data-testid="usage-provenance"
      >· from rollup</span
    >`;
  }

  private renderLongRangeHint() {
    if (!this.longRangeHint || !this.updating) return nothing;
    return html`<div class="long-range-hint" data-testid="long-range-hint">
      Long ranges take longer
    </div>`;
  }

  private get rangeLabel(): string {
    return timeRangeShortLabel(this.timeRange);
  }

  /** "over the last 30d" for a rolling key, "this month" for a calendar key. */
  private get rangeSpan(): string {
    return isCalendarTimeRange(this.timeRange)
      ? this.rangeLabel
      : `over the last ${this.rangeLabel}`;
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

    const tokens = totalTokensOf(this.summary?.token_usage);
    const cost = this.summary?.estimated_cost || 0;

    if (this.unit === 'dollars') {
      return html`
        <div>
          ${
            this.updating
              ? this.renderValueSkeleton()
              : html`<div class="primary-value">
                  <span title=${formatUsdExact(cost)}>${formatUsd(cost)}</span>
                </div>`
          }
          <div class="primary-label">
            <span>est. spend · ${this.rangeLabel}</span>
            ${this.renderProvenance()}
            <sl-tooltip
              content="Estimated from provider list prices and your plan. See Cost for reconciliation."
            >
              <sl-icon
                name="info-circle"
                label="About estimated spend"
              ></sl-icon>
            </sl-tooltip>
          </div>
          ${this.renderDelta()} ${this.renderLongRangeHint()}
        </div>
      `;
    }

    return html`
      <div>
        ${
          this.updating
            ? this.renderValueSkeleton()
            : html`<div class="primary-value">
                ${this.formatCompactNumber(tokens)}
              </div>`
        }
        <div class="primary-label">
          <span>tokens · ${this.rangeLabel}</span>
          ${this.renderProvenance()}
        </div>
        ${this.renderDelta()} ${this.renderLongRangeHint()}
      </div>
    `;
  }

  /** Where the headline goes while the new range loads. */
  private renderValueSkeleton() {
    return html`<sl-skeleton
      class="value-skeleton"
      data-testid="usage-value-skeleton"
      effect="sheen"
      style="width: 45%; height: 2.2rem;"
    ></sl-skeleton>`;
  }

  /** The value the delta compares, in whichever unit is showing. */
  private valueFor(summary: AccountGatewayUsageSummaryResponse | null): number {
    if (!summary) return 0;
    return this.unit === 'dollars'
      ? Number(summary.estimated_cost || 0)
      : totalTokensOf(summary.token_usage);
  }

  /**
   * The daily rows carry a total but no cache split, so under cache-exclusive
   * input they are scaled by the same factor as the headline: the line keeps
   * its shape and ends where the number above it says (B-04).
   */
  private scaledSeries(values: Map<string, number>): Map<string, number> {
    if (this.unit === 'dollars') return values;
    const scale = this.tokenSeriesScale;
    if (scale === 1) return values;
    return new Map(
      [...values].map(([key, value]) => [key, value * scale] as const)
    );
  }

  private get tokenSeriesScale(): number {
    const usage = this.summary?.token_usage;
    const reported = Number(usage?.total_tokens || 0);
    if (!usage || reported <= 0) return 1;
    return totalTokensOf(usage) / reported;
  }

  /**
   * Percent change against the previous window of equal length. Returns null
   * when there is no prior period to compare against: "up 100% from nothing"
   * is noise, not news.
   */
  private renderDelta() {
    if (this.loading && !this.summary) {
      return nothing;
    }
    if (this.updating) {
      return html`<sl-skeleton
        effect="sheen"
        style="width: 35%; height: 0.8rem; margin-top: var(--sl-spacing-2x-small);"
      ></sl-skeleton>`;
    }
    const prior = this.valueFor(this.priorSummary);
    if (prior <= 0) {
      return nothing;
    }
    const current = this.valueFor(this.summary);
    const change = ((current - prior) / prior) * 100;
    const rounded = Math.round(change);
    if (rounded === 0) {
      return html`<div class="delta">
        No change vs ${timeRangeComparisonLabel(this.timeRange)}
      </div>`;
    }
    const arrow = rounded > 0 ? '▲' : '▼';
    return html`<div class="delta">
      ${arrow} ${Math.abs(rounded)}% vs
      ${timeRangeComparisonLabel(this.timeRange)}
    </div>`;
  }

  private renderSecondaryLine() {
    if ((this.loading && !this.summary) || this.updating) {
      return html`<sl-skeleton
        effect="sheen"
        style="width: 90%; height: 0.85rem;"
      ></sl-skeleton>`;
    }

    const usage = this.summary?.token_usage || null;
    const requests = this.summary?.total_requests || 0;
    const parts =
      this.unit === 'dollars'
        ? [`${this.formatCompactNumber(totalTokensOf(usage))} tokens`]
        : [];
    parts.push(`${this.formatCompactNumber(requests)} requests`);
    if (this.toolCallsCount > 0) {
      parts.push(`${this.formatCompactNumber(this.toolCallsCount)} tool calls`);
    }
    // In dollars the headline is the money, so tokens lead the detail line;
    // in tokens the headline is the total, so the detail splits it.
    return html`<div class="secondary-line">
      ${
        this.unit === 'dollars'
          ? nothing
          : html`<token-figures .usage=${usage} expanded></token-figures> · `
      }${parts.join(' · ')}
    </div>`;
  }

  /** Unique per card: the clip path id lives in this card's shadow root. */
  private readonly sparklineClipId = `usage-sparkline-clip-${++sparklineClipSeq}`;

  /** Unique per card: the hatch pattern id lives in this card's shadow root. */
  private readonly sparklineHatchId = `usage-sparkline-hatch-${sparklineClipSeq}`;

  private renderSparklineSkeleton() {
    return html`<sl-skeleton
      class="sparkline-skeleton"
      data-testid="usage-sparkline-skeleton"
      effect="sheen"
      style="width: 100%; height: 40px;"
    ></sl-skeleton>`;
  }

  /** The chart's rows and their bucket size, from the timeseries or the summary. */
  private get chartRows(): {
    rows: GatewayUsageByDay[];
    granularity: UsageSeriesGranularity;
  } | null {
    if (this.series) {
      return { rows: this.series, granularity: this.seriesGranularity };
    }
    const fallback = this.summary?.requests_by_day;
    return fallback && fallback.length > 0
      ? { rows: fallback, granularity: 'day' }
      : null;
  }

  /** Inline SVG: a trend line is worth more here than a charting library. */
  private renderSparkline() {
    if (this.updating) return this.renderSparklineSkeleton();
    const source = this.chartRows;
    if (!source) {
      return this.seriesLoading ? this.renderSparklineSkeleton() : nothing;
    }
    if (isProjectedRange(this.timeRange)) {
      return this.renderPeriodSparkline(source.rows, source.granularity);
    }
    const values = source.rows.map((day) =>
      this.unit === 'dollars'
        ? Number(day.estimated_cost || 0)
        : Number(day.total_tokens || 0) * this.tokenSeriesScale
    );
    if (values.length < 3) {
      return nothing;
    }
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

    return this.renderSparklineSvg(
      this.unit === 'dollars'
        ? `Estimated spend per day ${this.rangeSpan}`
        : `Tokens per day ${this.rangeSpan}`,
      svg`<polygon
          class="sparkline-area"
          points=${areaPoints}
          fill="var(--sl-color-primary-500)"
          opacity="0.12"
        ></polygon>
        <polyline
          class="sparkline-line"
          points=${points.join(' ')}
          fill="none"
          stroke="var(--sl-color-primary-500)"
          stroke-width="1.5"
          vector-effect="non-scaling-stroke"
        ></polyline>`
    );
  }

  /** One svg, one clip: the reveal uncovers data and projection together. */
  private renderSparklineSvg(label: string, shapes: unknown) {
    const width = 100;
    const height = 40;
    return html`
      <svg
        class="sparkline"
        viewBox="0 0 ${width} ${height}"
        preserveAspectRatio="none"
        role="img"
        aria-label=${label}
      >
        <defs>
          <clipPath id=${this.sparklineClipId}>
            <!-- A little taller than the viewBox so the stroke is never
                 clipped at the top or bottom edge. -->
            <rect
              class="sparkline-reveal"
              x="0"
              y="-4"
              width=${width}
              height=${height + 8}
            ></rect>
          </clipPath>
          <pattern
            id=${this.sparklineHatchId}
            patternUnits="userSpaceOnUse"
            width="2"
            height="4"
            patternTransform="rotate(45)"
          >
            <rect
              width="0.6"
              height="4"
              fill="var(--sl-color-primary-500)"
              opacity="0.35"
            ></rect>
          </pattern>
        </defs>
        <g class="sparkline-shapes" clip-path="url(#${this.sparklineClipId})">
          ${shapes}
        </g>
      </svg>
    `;
  }

  /**
   * A to-date range: the whole period on the x axis, the data up to now as
   * a solid line and area, then the projection as a dashed line over a
   * hatched area, split by a thin "now" marker, with a legend that says so.
   */
  private renderPeriodSparkline(
    rows: GatewayUsageByDay[],
    granularity: UsageSeriesGranularity
  ) {
    const now = this.now ?? new Date();
    const projection = buildUsageProjection({
      range: this.timeRange,
      totalSoFar: this.valueFor(this.summary),
      values: this.scaledSeries(seriesValues(rows, granularity, this.unit)),
      granularity,
      now,
    });
    if (!projection || projection.slots.length < 2) return nothing;
    const { slots, currentIndex } = projection;
    const actual = slots.slice(0, currentIndex + 1).map((s) => s.actual || 0);
    const projected =
      projection.status === 'projected'
        ? slots.slice(currentIndex).map((s) => s.projected || 0)
        : [];
    if (actual.every((v) => v <= 0) && projected.every((v) => v <= 0)) {
      return nothing;
    }
    const width = 100;
    const height = 40;
    const max = Math.max(...actual, ...projected) || 1;
    const step = width / (slots.length - 1);
    const xy = (value: number, index: number) =>
      `${(index * step).toFixed(2)},${(
        height -
        (value / max) * (height - 4) -
        2
      ).toFixed(2)}`;
    const actualPoints = actual.map((v, i) => xy(v, i));
    const nowX = (currentIndex * step).toFixed(2);
    const projectedPoints = projected.map((v, i) => xy(v, currentIndex + i));
    const bucket =
      granularity === 'hour'
        ? 'hour'
        : granularity === 'month'
          ? 'month'
          : 'day';
    const label = `${
      this.unit === 'dollars' ? 'Estimated spend' : 'Tokens'
    } per ${bucket} ${this.rangeSpan}${
      projection.status === 'projected'
        ? ', with a projection to the end of the period'
        : ''
    }`;

    const chart = this.renderSparklineSvg(
      label,
      svg`<polygon
          class="sparkline-area"
          points=${`0,${height} ${actualPoints.join(' ')} ${nowX},${height}`}
          fill="var(--sl-color-primary-500)"
          opacity="0.12"
        ></polygon>
        <polyline
          class="sparkline-line"
          points=${actualPoints.join(' ')}
          fill="none"
          stroke="var(--sl-color-primary-500)"
          stroke-width="1.5"
          vector-effect="non-scaling-stroke"
        ></polyline>
        ${
          projectedPoints.length > 0
            ? svg`<polygon
                  class="sparkline-projection-area"
                  points=${`${nowX},${height} ${projectedPoints.join(' ')} ${width},${height}`}
                  fill="url(#${this.sparklineHatchId})"
                ></polygon>
                <polyline
                  class="sparkline-projection-line"
                  points=${projectedPoints.join(' ')}
                  fill="none"
                  stroke="var(--sl-color-primary-400)"
                  stroke-width="1.5"
                  stroke-dasharray="4 3"
                  vector-effect="non-scaling-stroke"
                ></polyline>`
            : nothing
        }
        <line
          class="sparkline-now"
          x1=${nowX}
          x2=${nowX}
          y1="0"
          y2=${height}
          stroke="var(--sl-color-neutral-500)"
          stroke-width="1"
          vector-effect="non-scaling-stroke"
        ></line>`
    );
    return html`${chart}${this.renderProjectionLegend(projection)}`;
  }

  private renderProjectionLegend(
    projection: NonNullable<ReturnType<typeof buildUsageProjection>>
  ) {
    const solid = html`<span class="legend-item"
      ><span class="legend-swatch" aria-hidden="true"></span>so far</span
    >`;
    if (projection.status === 'too-early') {
      return html`<div
        class="sparkline-legend"
        data-testid="usage-projection-legend"
      >
        ${solid}<span>· too early to project</span>
      </div>`;
    }
    if (
      projection.status !== 'projected' ||
      projection.projectedTotal == null
    ) {
      return nothing;
    }
    const period = projectionPeriodFor(this.timeRange) ?? 'monthly';
    const total =
      this.unit === 'dollars'
        ? formatUsd(projection.projectedTotal)
        : `${this.formatCompactNumber(projection.projectedTotal)} tokens`;
    const shape =
      projection.shape === 'weekday'
        ? 'spread by weekday pattern'
        : projection.shape === 'hour-of-day'
          ? 'spread by hour of day'
          : 'spread evenly';
    return html`<div
      class="sparkline-legend"
      data-testid="usage-projection-legend"
      title=${`Projection: the rate so far carried to the end of the period, ${shape}.`}
    >
      ${solid}
      <span class="legend-item"
        ><span class="legend-swatch dashed" aria-hidden="true"></span
        >projected</span
      >
      <span class="legend-total"
        >Projected: ${total} by
        ${forecastEndLabel(period, projection.end, 'local')}</span
      >
    </div>`;
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

  private renderBudgetRow(
    label: string,
    period: string,
    spend: number | null,
    softLimit: number,
    hardLimit: number,
    bounds: { start?: string | null; end?: string | null } = {}
  ) {
    const denominator = hardLimit || softLimit;
    if (spend === null) {
      return html`<div class="budget-row">
        <div class="budget-row-header">
          <span class="budget-row-label">${label}</span>
          <span class="budget-row-value"
            >Spend
            unavailable${denominator > 0 ? html` / ${formatUsd(denominator)}` : nothing}</span
          >
        </div>
      </div>`;
    }
    return html`
      <div class="budget-row">
        <div class="budget-row-header">
          <span class="budget-row-label">${label}</span>
          <span class="budget-row-value">
            ${html`<span title=${formatUsdExact(spend)}>${formatUsd(spend)}</span>`}
            ${
              denominator > 0
                ? html`/ ${formatUsd(denominator)}`
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
        ${renderBudgetForecast(
          {
            period,
            spend,
            softLimit,
            hardLimit,
            periodStart: bounds.start,
            periodEnd: bounds.end,
          },
          (value) => formatUsd(value)
        )}
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
        budgetPeriodLabel(policy.period),
        policy.period,
        policy.current_spend_usd ?? null,
        policy.soft_limit_usd || 0,
        policy.hard_limit_usd || 0,
        { start: policy.period_start, end: policy.period_end }
      )
    );

    if (!hasGlobalMonthly && legacyLimit > 0) {
      rows.unshift(
        this.renderBudgetRow(
          budgetPeriodLabel('monthly'),
          'monthly',
          this.summary?.budget?.current_spend_usd || 0,
          this.summary?.budget?.soft_limit_usd || 0,
          legacyLimit
        )
      );
    }

    return html`
      <div class="budgets">
        ${
          globals.length > 0
            ? html`<p class="budget-basis muted">
                <strong>Budget usage</strong>
                Uses request-time prices and UTC periods. Later repricing or
                reconciliation changes usage estimates, not this limit counter.
              </p>`
            : nothing
        }
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
            ${
              this.updating
                ? html`<span
                    class="visually-hidden"
                    data-testid="usage-updating"
                    role="status"
                    >Updating usage</span
                  >`
                : nothing
            }
            ${this.renderUnitToggle()}
            <time-range-select
              ariaLabel="Usage time range"
              ?busy=${this.updating}
              .value=${normalizeTimeRangeKey(this.timeRange) ?? DEFAULT_USAGE_RANGE}
              .options=${USAGE_RANGE_OPTIONS}
              @range-change=${this.handleRangeChange}
            ></time-range-select>
          </div>
        </div>

        <div class="body ${this.updating ? 'is-updating' : ''}">
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
            <a href="/console/cost/spend">Cost details</a>
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
