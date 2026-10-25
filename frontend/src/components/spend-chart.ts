import { LitElement, css, html, nothing, svg } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import {
  getCostTimeseries,
  type CostGranularity,
  type CostTimeseriesResponse,
} from '../api';
import type { GatewayUsageByDay } from '../types';
import { formatUsd } from '../utils/money';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/button-group/button-group.js';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/option/option.js';
import '@shoelace-style/shoelace/dist/components/select/select.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';

const DAY_MS = 86_400_000;
/** The server refuses longer windows; the custom range is clamped to it. */
export const MAX_WINDOW_DAYS = 366;

export type SpendPreset = 'page' | '7d' | '30d' | '90d' | '12m' | 'custom';

const PRESETS: { value: SpendPreset; label: string; days: number }[] = [
  { value: '7d', label: 'Last 7 days', days: 7 },
  { value: '30d', label: 'Last 30 days', days: 30 },
  { value: '90d', label: 'Last 90 days', days: 90 },
  { value: '12m', label: 'Last 12 months', days: 365 },
];

/** UTC midnight of the day containing `date`. */
function startOfUtcDay(date: Date): Date {
  return new Date(
    Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), date.getUTCDate())
  );
}

function isoDay(date: Date): string {
  return date.toISOString().slice(0, 10);
}

/** Bucket starts covering [start, end), so empty buckets still draw as 0. */
export function bucketStarts(
  start: Date,
  end: Date,
  granularity: CostGranularity
): string[] {
  const out: string[] = [];
  let cursor = startOfUtcDay(start);
  if (granularity === 'week') {
    // ISO weeks start on Monday, like Postgres date_trunc('week').
    const weekday = (cursor.getUTCDay() + 6) % 7;
    cursor = new Date(cursor.getTime() - weekday * DAY_MS);
  } else if (granularity === 'month') {
    cursor = new Date(
      Date.UTC(cursor.getUTCFullYear(), cursor.getUTCMonth(), 1)
    );
  }
  while (cursor < end && out.length < 400) {
    out.push(isoDay(cursor));
    if (granularity === 'day') cursor = new Date(cursor.getTime() + DAY_MS);
    else if (granularity === 'week')
      cursor = new Date(cursor.getTime() + 7 * DAY_MS);
    else
      cursor = new Date(
        Date.UTC(cursor.getUTCFullYear(), cursor.getUTCMonth() + 1, 1)
      );
  }
  return out;
}

/** A round axis maximum (1, 2, 2.5, 5 x 10^n) at or above `value`. */
export function niceMax(value: number): number {
  if (value <= 0) return 1;
  const exponent = Math.floor(Math.log10(value));
  const base = 10 ** exponent;
  for (const step of [1, 2, 2.5, 5, 10]) {
    if (step * base >= value) return step * base;
  }
  return 10 * base;
}

/**
 * Spend over time: a full-width bar chart. Reads GET /cost/timeseries, which
 * is bounded to a year per request.
 *
 * Given a `period`, the chart belongs to a page that already owns the range
 * (one range per page, DESIGN.md "Time range and units"): it draws exactly
 * that window, restates it as a muted label and offers only the bucket size.
 * Without one it carries its own range (presets or a custom pair of dates)
 * and back/forward stepping by one window.
 */
@customElement('spend-chart')
export class SpendChart extends LitElement {
  /** Injected clock for tests. */
  @property({ attribute: false }) now: () => Date = () => new Date();
  /**
   * The window the rest of the page covers. The chart draws exactly this
   * window, so its total matches the cards, and has no range of its own.
   */
  @property({ attribute: false }) period: {
    startDate: string;
    endDate: string;
  } | null = null;
  @state() private preset: SpendPreset = '30d';
  @state() private granularity: CostGranularity = 'day';
  /** Window as [start, end): end is exclusive, UTC midnight. */
  @state() private start!: Date;
  @state() private end!: Date;
  @state() private data: CostTimeseriesResponse | null = null;
  @state() private loading = false;
  @state() private error = '';
  @state() private hover: number | null = null;
  private generation = 0;

  connectedCallback(): void {
    super.connectedCallback();
    if (this.period) this.adoptPeriod();
    else this.applyPreset('30d');
  }

  protected willUpdate(changed: Map<PropertyKey, unknown>): void {
    // connectedCallback already handled the initial period.
    if (!this.hasUpdated) return;
    if (!changed.has('period') || !this.period || !this.start) return;
    const before = changed.get('period') as SpendChart['period'];
    // Compare instants, not strings: the page hands over the window it asked
    // for, then the one the server echoed back in another spelling.
    const instant = (value: string | undefined) =>
      value ? new Date(value).getTime() : Number.NaN;
    const same =
      instant(before?.startDate) === instant(this.period.startDate) &&
      instant(before?.endDate) === instant(this.period.endDate);
    if (!same) this.adoptPeriod();
  }

  private adoptPeriod() {
    if (!this.period) return;
    const start = new Date(this.period.startDate);
    const end = new Date(this.period.endDate);
    if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime())) return;
    if (end <= start) return;
    this.preset = 'page';
    this.start = start;
    this.end =
      end.getTime() - start.getTime() > MAX_WINDOW_DAYS * DAY_MS
        ? new Date(start.getTime() + MAX_WINDOW_DAYS * DAY_MS)
        : end;
    const days = (this.end.getTime() - start.getTime()) / DAY_MS;
    if (days > 120 && this.granularity === 'day') this.granularity = 'month';
    void this.load();
  }

  private applyPreset(preset: SpendPreset) {
    this.preset = preset;
    const found = PRESETS.find((option) => option.value === preset);
    if (!found) return;
    const end = new Date(startOfUtcDay(this.now()).getTime() + DAY_MS);
    this.end = end;
    this.start = new Date(end.getTime() - found.days * DAY_MS);
    // A year of days is 365 bars; months read better there.
    if (found.days > 120 && this.granularity === 'day')
      this.granularity = 'month';
    if (found.days <= 30 && this.granularity === 'month')
      this.granularity = 'day';
    void this.load();
  }

  private get windowMs(): number {
    return this.end.getTime() - this.start.getTime();
  }

  private get atPresent(): boolean {
    return this.end.getTime() >= startOfUtcDay(this.now()).getTime() + DAY_MS;
  }

  private step(direction: -1 | 1) {
    const shift = this.windowMs * direction;
    let end = new Date(this.end.getTime() + shift);
    const ceiling = new Date(startOfUtcDay(this.now()).getTime() + DAY_MS);
    if (end > ceiling) end = ceiling;
    this.start = new Date(end.getTime() - this.windowMs);
    this.end = end;
    this.preset = 'custom';
    void this.load();
  }

  private setCustom(which: 'start' | 'end', value: string) {
    if (!value) return;
    const day = new Date(`${value}T00:00:00Z`);
    if (Number.isNaN(day.getTime())) return;
    let start = this.start;
    let end = this.end;
    if (which === 'start') start = day;
    else end = new Date(day.getTime() + DAY_MS); // inclusive end date
    if (end <= start) end = new Date(start.getTime() + DAY_MS);
    if (end.getTime() - start.getTime() > MAX_WINDOW_DAYS * DAY_MS) {
      if (which === 'start')
        end = new Date(start.getTime() + MAX_WINDOW_DAYS * DAY_MS);
      else start = new Date(end.getTime() - MAX_WINDOW_DAYS * DAY_MS);
    }
    this.start = start;
    this.end = end;
    this.preset = 'custom';
    void this.load();
  }

  private async load() {
    const generation = ++this.generation;
    this.loading = true;
    this.error = '';
    try {
      const data = await getCostTimeseries({
        startDate: this.start.toISOString(),
        endDate: this.end.toISOString(),
        granularity: this.granularity,
      });
      if (generation !== this.generation) return;
      this.data = data;
    } catch (error) {
      if (generation !== this.generation) return;
      this.error =
        error instanceof Error ? error.message : 'Failed to load spend';
    } finally {
      if (generation === this.generation) this.loading = false;
    }
  }

  /** Every bucket in the window, zero-filled. */
  private series(): GatewayUsageByDay[] {
    const byDate = new Map(
      (this.data?.buckets ?? []).map((bucket) => [bucket.date, bucket])
    );
    return bucketStarts(this.start, this.end, this.granularity).map(
      (date) =>
        byDate.get(date) ?? {
          date,
          request_count: 0,
          estimated_cost: 0,
          total_tokens: 0,
          input_tokens: 0,
          output_tokens: 0,
          cache_read_tokens: 0,
          cache_write_tokens: 0,
        }
    );
  }

  private bucketLabel(date: string, long = false): string {
    const day = new Date(`${date}T00:00:00Z`);
    if (this.granularity === 'month')
      return day.toLocaleDateString(undefined, {
        month: 'short',
        year: long ? 'numeric' : '2-digit',
        timeZone: 'UTC',
      });
    const label = day.toLocaleDateString(undefined, {
      month: 'short',
      day: 'numeric',
      timeZone: 'UTC',
      ...(long ? { year: 'numeric' } : {}),
    });
    return this.granularity === 'week' && long ? `Week of ${label}` : label;
  }

  private rangeLabel(): string {
    // A midnight end is exclusive; a mid-day end (the page's rolling
    // window ends now) is the last day itself.
    const last =
      this.end.getTime() % DAY_MS === 0
        ? new Date(this.end.getTime() - DAY_MS)
        : this.end;
    const fmt = (date: Date) =>
      date.toLocaleDateString(undefined, {
        month: 'short',
        day: 'numeric',
        year: 'numeric',
        timeZone: 'UTC',
      });
    return `${fmt(this.start)} to ${fmt(last)} (UTC)`;
  }

  private renderChart() {
    const rows = this.series();
    const width = 1000;
    const height = 240;
    const pad = { left: 56, right: 8, top: 12, bottom: 28 };
    const plotW = width - pad.left - pad.right;
    const plotH = height - pad.top - pad.bottom;
    const max = niceMax(Math.max(0, ...rows.map((row) => row.estimated_cost)));
    const slot = plotW / Math.max(rows.length, 1);
    const barW = Math.max(1, slot * 0.7);
    const ticks = [0, 0.25, 0.5, 0.75, 1].map((ratio) => ratio * max);
    const labelEvery = Math.ceil(rows.length / 12);
    const total = rows.reduce((sum, row) => sum + row.estimated_cost, 0);
    const requests = rows.reduce((sum, row) => sum + row.request_count, 0);
    const hovered = this.hover === null ? null : rows[this.hover];
    return html`
      <p class="totals">
        <strong>${formatUsd(total)}</strong> across ${requests.toLocaleString()}
        ${requests === 1 ? 'request' : 'requests'}
        <span class="meta">${this.rangeLabel()}</span>
      </p>
      <div class="plot" @mouseleave=${() => (this.hover = null)}>
        <svg
          viewBox="0 0 ${width} ${height}"
          preserveAspectRatio="none"
          role="img"
          aria-label="Spend per ${this.granularity}, ${this.rangeLabel()}: ${formatUsd(total)} total"
        >
          ${ticks.map((tick) => {
            const y = pad.top + plotH - (tick / max) * plotH;
            return svg`<line class="grid" x1=${pad.left} x2=${width - pad.right} y1=${y} y2=${y}></line>`;
          })}
          ${rows.map((row, index) => {
            const h = (row.estimated_cost / max) * plotH;
            const x = pad.left + index * slot + (slot - barW) / 2;
            return svg`<rect class="hit" x=${pad.left + index * slot} y=${pad.top} width=${slot} height=${plotH}
                @mouseenter=${() => (this.hover = index)}></rect>
              <rect class="bar ${this.hover === index ? 'active' : ''}" x=${x}
                y=${pad.top + plotH - h} width=${barW} height=${Math.max(h, row.estimated_cost > 0 ? 1 : 0)}></rect>`;
          })}
        </svg>
        <div class="y-axis" aria-hidden="true">
          ${ticks.map(
            (tick) =>
              html`<span
                style="top: ${pad.top + plotH - (tick / max) * plotH}px"
                >${formatUsd(tick)}</span
              >`
          )}
        </div>
        <div class="x-axis" aria-hidden="true">
          ${rows.map((row, index) =>
            index % labelEvery === 0
              ? html`<span
                  style="left: ${((pad.left + (index + 0.5) * slot) / width) * 100}%"
                  >${this.bucketLabel(row.date)}</span
                >`
              : nothing
          )}
        </div>
        ${
          hovered
            ? html`<div
                class="tooltip"
                role="status"
                style="left: ${((pad.left + ((this.hover ?? 0) + 0.5) * slot) / width) * 100}%"
              >
                <strong>${this.bucketLabel(hovered.date, true)}</strong>
                <span>${formatUsd(hovered.estimated_cost)}</span>
                <span
                  >${hovered.request_count.toLocaleString()}
                  ${hovered.request_count === 1 ? 'request' : 'requests'}</span
                >
              </div>`
            : nothing
        }
      </div>
      <table class="sr-only">
        <caption>
          Spend per ${this.granularity}
        </caption>
        <tbody>
          ${rows.map(
            (row) =>
              html`<tr>
                <th scope="row">${this.bucketLabel(row.date, true)}</th>
                <td>${formatUsd(row.estimated_cost)}</td>
                <td>${row.request_count}</td>
              </tr>`
          )}
        </tbody>
      </table>
    `;
  }

  /** The chart's own range, only when no page owns one. */
  private renderRangeControls() {
    return html`<sl-select
        size="small"
        label="Range"
        class="preset"
        value=${this.preset}
        @sl-change=${(event: Event) => {
          const value = (event.target as HTMLSelectElement)
            .value as SpendPreset;
          if (value !== 'custom') this.applyPreset(value);
          else this.preset = 'custom';
        }}
      >
        ${PRESETS.map(
          (option) =>
            html`<sl-option value=${option.value}>${option.label}</sl-option>`
        )}
        <sl-option value="custom">Custom range</sl-option>
      </sl-select>
      ${
        this.preset === 'custom'
          ? html`<sl-input
                size="small"
                type="date"
                label="From"
                class="date"
                value=${isoDay(this.start)}
                @sl-change=${(event: Event) =>
                  this.setCustom(
                    'start',
                    (event.target as HTMLInputElement).value
                  )}
              ></sl-input>
              <sl-input
                size="small"
                type="date"
                label="To"
                class="date"
                value=${isoDay(new Date(this.end.getTime() - DAY_MS))}
                @sl-change=${(event: Event) =>
                  this.setCustom(
                    'end',
                    (event.target as HTMLInputElement).value
                  )}
              ></sl-input>`
          : nothing
      }`;
  }

  private renderStepControls() {
    return html`<sl-button-group label="Step through time">
      <sl-button
        size="small"
        class="step-back"
        aria-label="Previous period"
        @click=${() => this.step(-1)}
        >‹</sl-button
      >
      <sl-button
        size="small"
        class="step-forward"
        aria-label="Next period"
        ?disabled=${this.atPresent}
        @click=${() => this.step(1)}
        >›</sl-button
      >
    </sl-button-group>`;
  }

  render() {
    return html`<section aria-labelledby="spend-chart-title">
      <header>
        <h2 id="spend-chart-title">Spend over time</h2>
        <div class="controls">
          ${this.period ? nothing : this.renderRangeControls()}
          <sl-button-group label="Bucket size">
            ${(['day', 'week', 'month'] as CostGranularity[]).map(
              (value) =>
                html`<sl-button
                  size="small"
                  variant=${this.granularity === value ? 'primary' : 'default'}
                  aria-pressed=${this.granularity === value ? 'true' : 'false'}
                  @click=${() => {
                    this.granularity = value;
                    void this.load();
                  }}
                  >${value[0].toUpperCase() + value.slice(1)}</sl-button
                >`
            )}
          </sl-button-group>
          ${this.period ? nothing : this.renderStepControls()}
        </div>
      </header>
      ${
        this.error
          ? html`<p class="error" role="alert">${this.error}</p>`
          : this.data === null
            ? html`<div class="loading" role="status">
                <sl-spinner></sl-spinner> Loading spend...
              </div>`
            : html`<div class=${this.loading ? 'updating' : ''}>
                ${this.renderChart()}
              </div>`
      }
    </section>`;
  }

  static styles = css`
    :host {
      display: block;
    }
    section {
      padding: var(--sl-spacing-medium);
      border: 1px solid var(--console-hairline, var(--sl-color-neutral-200));
      border-radius: var(--sl-border-radius-large);
      background: var(--console-surface, var(--sl-color-neutral-0));
    }
    header {
      display: flex;
      flex-wrap: wrap;
      align-items: flex-end;
      justify-content: space-between;
      gap: var(--sl-spacing-small);
    }
    h2 {
      margin: 0;
      font-size: var(--console-text-section, var(--sl-font-size-medium));
    }
    .controls {
      display: flex;
      flex-wrap: wrap;
      align-items: flex-end;
      gap: var(--sl-spacing-x-small);
    }
    .controls sl-select::part(form-control-label),
    .controls sl-input::part(form-control-label) {
      font-size: var(--sl-font-size-x-small);
    }
    .preset {
      width: 150px;
    }
    .date {
      width: 150px;
    }
    .totals {
      margin: var(--sl-spacing-small) 0 var(--sl-spacing-x-small);
      display: flex;
      gap: var(--sl-spacing-x-small);
      align-items: baseline;
      flex-wrap: wrap;
    }
    .meta {
      color: var(--console-meta-color, var(--sl-color-neutral-600));
      font-size: var(--console-text-caption, var(--sl-font-size-x-small));
    }
    .plot {
      position: relative;
      height: 260px;
      padding-bottom: 4px;
    }
    svg {
      width: 100%;
      height: 240px;
      display: block;
    }
    .grid {
      stroke: var(--console-hairline, var(--sl-color-neutral-200));
      stroke-width: 1;
      vector-effect: non-scaling-stroke;
    }
    .bar {
      fill: var(--sl-color-primary-500);
      pointer-events: none;
    }
    .bar.active {
      fill: var(--sl-color-primary-700);
    }
    .hit {
      fill: transparent;
    }
    .y-axis span {
      position: absolute;
      left: 0;
      width: 50px;
      text-align: right;
      transform: translateY(-50%);
      font-size: var(--sl-font-size-x-small);
      color: var(--console-meta-color, var(--sl-color-neutral-600));
      font-variant-numeric: tabular-nums;
    }
    .x-axis span {
      position: absolute;
      top: 216px;
      transform: translateX(-50%);
      font-size: var(--sl-font-size-x-small);
      color: var(--console-meta-color, var(--sl-color-neutral-600));
      white-space: nowrap;
    }
    .tooltip {
      position: absolute;
      top: 0;
      transform: translateX(-50%);
      display: grid;
      gap: 2px;
      padding: var(--sl-spacing-2x-small) var(--sl-spacing-x-small);
      border-radius: var(--sl-border-radius-medium);
      background: var(--sl-color-neutral-900);
      color: var(--sl-color-neutral-0);
      font-size: var(--sl-font-size-x-small);
      pointer-events: none;
      white-space: nowrap;
      font-variant-numeric: tabular-nums;
    }
    .updating {
      opacity: 0.6;
    }
    .loading {
      display: flex;
      gap: var(--sl-spacing-x-small);
      align-items: center;
      padding: var(--sl-spacing-large);
    }
    .error {
      color: var(--sl-color-danger-700);
    }
    .sr-only {
      position: absolute;
      width: 1px;
      height: 1px;
      overflow: hidden;
      clip: rect(0 0 0 0);
      white-space: nowrap;
    }
  `;
}
