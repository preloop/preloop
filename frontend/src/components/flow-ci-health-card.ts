import { LitElement, css, html, nothing, svg, unsafeCSS } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';

import '@shoelace-style/shoelace/dist/components/card/card.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';

import {
  getFlowCiHealth,
  type CiHealthFlake,
  type CiHealthPoint,
  type CiHealthValue,
  type FlowCiHealth,
} from '../api';
import consoleStyles from '../styles/console-styles.css?inline';
import { formatCount, formatCountExact } from '../utils/count-format';
import { formatRelativeTime } from '../utils/date';
import { formatUsd } from '../utils/money';

/** "4m 10s", "1h 02m", "45s"; an em dash when unknown. */
export function formatSeconds(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return '—';
  }
  const seconds = Math.round(value);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60)
    return `${minutes}m ${String(seconds % 60).padStart(2, '0')}s`;
  return `${Math.floor(minutes / 60)}h ${String(minutes % 60).padStart(2, '0')}m`;
}

/** A 0..1 rate as a whole percent. */
export function formatRate(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return '—';
  }
  return `${Math.round(value * 100)}%`;
}

/**
 * The grey delta line DESIGN.md "Numbers" asks for: the arrow carries the
 * direction, colour is reserved for states.
 */
export function rateDelta(
  latest: number | null | undefined,
  prior: number | null | undefined,
  days: number
): string {
  if (latest == null || prior == null) return '';
  const points = Math.round((latest - prior) * 100);
  if (points === 0) return `No change vs ${days}d ago`;
  return `${points > 0 ? '▲' : '▼'} ${Math.abs(points)} pts vs ${days}d ago`;
}

/** Seconds delta, e.g. "▼ 3m 00s vs 7d ago". */
export function secondsDelta(
  latest: number | null | undefined,
  prior: number | null | undefined,
  days: number
): string {
  if (latest == null || prior == null) return '';
  const diff = latest - prior;
  if (Math.round(diff) === 0) return `No change vs ${days}d ago`;
  return `${diff > 0 ? '▲' : '▼'} ${formatSeconds(Math.abs(diff))} vs ${days}d ago`;
}

/** Polyline points for a 0..1 series in a width x height box. */
export function sparklinePoints(
  values: Array<number | null>,
  width = 96,
  height = 20
): string {
  const known = values
    .map((value, index) => ({ value, index }))
    .filter((p): p is { value: number; index: number } => p.value !== null);
  if (known.length < 2) return '';
  const last = values.length - 1 || 1;
  return known
    .map(({ value, index }) => {
      const x = (index / last) * width;
      const y = height - Math.max(0, Math.min(1, value)) * height;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(' ');
}

/** Whether a response has the shape the card renders (an older or proxied
 * server may answer this path with something else). */
export function isCiHealthView(value: unknown): value is FlowCiHealth {
  const view = value as FlowCiHealth | null;
  return (
    !!view &&
    typeof view === 'object' &&
    typeof view.days === 'number' &&
    !!view.repositories &&
    typeof view.repositories === 'object' &&
    Array.isArray(view.top_flakes) &&
    !!view.week?.runs &&
    !!view.prior_week?.runs
  );
}

/**
 * The CI health card on a flow page: per-repository health with its trend,
 * the failures worth a look, and what the flow delivered this week. Renders
 * nothing for a flow that never ran the ci_health_report collector.
 */
@customElement('flow-ci-health-card')
export class FlowCiHealthCard extends LitElement {
  @property({ type: String }) flowId = '';
  @property({ type: Number }) days = 7;
  @state() private view: FlowCiHealth | null = null;
  @state() private failed = false;

  static styles = [
    unsafeCSS(consoleStyles),
    css`
      :host {
        display: block;
      }
      :host([hidden]) {
        display: none;
      }
      .header {
        display: flex;
        align-items: center;
        gap: var(--sl-spacing-x-small);
      }
      .range {
        margin-left: auto;
        color: var(--console-meta-color);
        font-size: var(--console-text-meta);
        font-weight: 400;
      }
      h3 {
        font-size: var(--console-text-meta);
        font-weight: 600;
        color: var(--console-meta-color);
        margin: var(--sl-spacing-medium) 0 var(--sl-spacing-2x-small);
      }
      h3:first-of-type {
        margin-top: 0;
      }
      table {
        width: 100%;
        border-collapse: collapse;
        font-size: var(--console-text-body);
        font-variant-numeric: tabular-nums;
      }
      th {
        text-align: left;
        font-weight: 500;
        font-size: var(--console-text-meta);
        color: var(--console-meta-color);
        padding: var(--sl-spacing-2x-small) var(--sl-spacing-x-small);
        border-bottom: 1px solid var(--console-hairline);
      }
      td {
        padding: var(--sl-spacing-x-small);
        border-bottom: 1px solid var(--console-hairline);
        vertical-align: top;
      }
      tr:last-child td {
        border-bottom: none;
      }
      th.num,
      td.num {
        text-align: right;
      }
      .meta {
        display: block;
        color: var(--console-meta-color);
        font-size: var(--console-text-meta);
      }
      .test {
        overflow-wrap: anywhere;
      }
      .spark polyline {
        fill: none;
        stroke: var(--sl-color-primary-600);
        stroke-width: 1.5;
      }
      dl.value {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
        gap: var(--sl-spacing-small) var(--sl-spacing-large);
        margin: 0;
        font-variant-numeric: tabular-nums;
      }
      dl.value dt {
        color: var(--console-meta-color);
        font-size: var(--console-text-meta);
      }
      dl.value dd {
        margin: 0;
        font-size: var(--console-text-card-title);
        font-weight: 600;
      }
      a {
        color: var(--console-link-color);
      }
      .empty {
        color: var(--console-meta-color);
        font-size: var(--console-text-meta);
        margin: 0;
      }
    `,
  ];

  updated(changed: Map<string, unknown>): void {
    if ((changed.has('flowId') || changed.has('days')) && this.flowId) {
      void this.load();
    }
    this.hidden = this.view === null;
  }

  private requestKey = '';

  private async load(): Promise<void> {
    // Another flow's card must never show, even for a moment, and a slow
    // answer for a previous flow must not replace the current one.
    const key = `${this.flowId}:${this.days}`;
    this.requestKey = key;
    this.view = null;
    try {
      const view = await getFlowCiHealth(this.flowId, this.days);
      if (this.requestKey !== key) return;
      this.view = isCiHealthView(view) ? view : null;
      this.failed = false;
    } catch {
      if (this.requestKey !== key) return;
      // A card that cannot load says nothing rather than a false zero; the
      // flow page above it is still the authority on the runs.
      this.view = null;
      this.failed = true;
    }
  }

  private renderRepositories(view: FlowCiHealth) {
    const rows = Object.entries(view.repositories);
    if (rows.length === 0) {
      return html`<p class="empty">No CI data collected yet.</p>`;
    }
    return html`<table data-testid="ci-health-repositories">
      <thead>
        <tr>
          <th>Repository</th>
          <th class="num">Success</th>
          <th class="num">Queue p95</th>
          <th>Slowest gate</th>
          <th class="num">Runner min</th>
          <th>Trend</th>
        </tr>
      </thead>
      <tbody>
        ${rows.map(([repo, data]) => {
          const latest: Partial<CiHealthPoint> = data.latest ?? {};
          const prior: Partial<CiHealthPoint> = data.prior ?? {};
          const points = sparklinePoints(
            data.series.map((p) => p.success_rate)
          );
          return html`<tr>
            <td>${repo.replace(/^[a-z]+:/, '')}</td>
            <td class="num">
              ${formatRate(latest.success_rate)}
              <span class="meta"
                >${rateDelta(latest.success_rate, prior.success_rate, view.days)}</span
              >
            </td>
            <td class="num">
              ${formatSeconds(latest.queue_p95_s)}
              <span class="meta"
                >${secondsDelta(latest.queue_p95_s, prior.queue_p95_s, view.days)}</span
              >
            </td>
            <td>
              ${latest.slowest_gate ?? '—'}
              <span class="meta"
                >${formatSeconds(latest.slowest_gate_p95_s)} p95</span
              >
            </td>
            <td class="num">${formatCount(latest.runner_minutes)}</td>
            <td>
              ${
                points
                  ? html`<svg
                      class="spark"
                      width="96"
                      height="20"
                      viewBox="0 0 96 20"
                      role="img"
                      aria-label="Success rate over the last ${
                        data.series.length
                      } runs"
                    >
                      ${svg`<polyline points=${points}></polyline>`}
                    </svg>`
                  : nothing
              }
            </td>
          </tr>`;
        })}
      </tbody>
    </table>`;
  }

  private renderFlakes(view: FlowCiHealth) {
    if (view.top_flakes.length === 0) {
      return html`<p class="empty">No open failures in the ledger.</p>`;
    }
    const now = new Date();
    return html`<table data-testid="ci-health-flakes">
      <thead>
        <tr>
          <th>Failure</th>
          <th class="num">Seen</th>
          <th>Last</th>
          <th>Status</th>
        </tr>
      </thead>
      <tbody>
        ${view.top_flakes.map(
          (flake) =>
            html`<tr>
              <td class="test">
                ${flake.test ?? flake.id}
                <span class="meta"
                  >${[
                    flake.framework,
                    flake.error,
                    (flake.jobs ?? []).join(', '),
                  ]
                    .filter(Boolean)
                    .join(' · ')}</span
                >
                ${(flake.linked ?? [])
                  .slice(-1)
                  .map(
                    (url) =>
                      html`<a
                        class="meta"
                        href=${url}
                        target="_blank"
                        rel="noopener"
                        >${url.replace(/^https?:\/\//, '')}</a
                      >`
                  )}
              </td>
              <td class="num">${formatCount(flake.count)}</td>
              <td title=${flake.last_seen ?? ''}>
                ${flake.last_seen ? formatRelativeTime(flake.last_seen, now) : '—'}
              </td>
              <td>
                ${
                  flake.status === 'regressed'
                    ? html`<sl-badge class="chip" pill variant="warning"
                        >Regressed</sl-badge
                      >`
                    : html`<sl-badge class="chip" pill variant="neutral"
                        >Open</sl-badge
                      >`
                }
                ${
                  flake.flaky
                    ? html`<span class="tag-chip meta" data-testid="flaky-tag"
                        >flaky</span
                      >`
                    : nothing
                }
              </td>
            </tr>`
        )}
      </tbody>
    </table>`;
  }

  private renderValue(view: FlowCiHealth) {
    const week: CiHealthValue = view.week;
    const prior: CiHealthValue = view.prior_week;
    const priorTitle = (value: string) => `Prior ${view.days}d: ${value}`;
    return html`<dl class="value" data-testid="ci-health-value">
      <div>
        <dt>Runs</dt>
        <dd title=${priorTitle(String(prior.runs.total))}>
          ${formatCount(week.runs.total)}
        </dd>
        <span class="meta"
          >${week.runs.skipped ? `${formatCount(week.runs.skipped)} skipped · ` : ''}${formatCount(
            week.runs.failed
          )}
          failed</span
        >
      </div>
      <div>
        <dt>Pull requests</dt>
        <dd title=${priorTitle(`${prior.pull_requests_merged} merged`)}>
          ${week.pull_requests_merged} merged
        </dd>
        <span class="meta">${week.pull_requests_opened} opened</span>
      </div>
      <div>
        <dt>Failures fixed</dt>
        <dd>${week.failures_fixed}</dd>
        <span class="meta"
          >${
            week.failures_regressed
              ? `${week.failures_regressed} regressed`
              : 'none regressed'
          }</span
        >
      </div>
      <div>
        <dt>Runner min saved / wk</dt>
        <dd
          title="Claimed by the merged changes, not measured on CI"
          data-testid="minutes-saved"
        >
          ${
            week.runner_minutes_saved_per_week_est == null
              ? '—'
              : `${formatCount(week.runner_minutes_saved_per_week_est)} est.`
          }
        </dd>
      </div>
      <div>
        <dt>Tokens</dt>
        <dd title=${formatCountExact(week.tokens)}>
          ${formatCount(week.tokens)}
        </dd>
        <span class="meta">${formatUsd(week.cost_usd_est)} est.</span>
      </div>
      <div>
        <dt>Per merged PR</dt>
        <dd data-testid="cost-per-pr">
          ${
            week.cost_per_merged_pull_request_usd_est == null
              ? '—'
              : `${formatUsd(week.cost_per_merged_pull_request_usd_est)} est.`
          }
        </dd>
        <span class="meta"
          >${
            week.tokens_per_merged_pull_request == null
              ? 'no merged PR yet'
              : `${formatCount(week.tokens_per_merged_pull_request)} tokens`
          }</span
        >
      </div>
    </dl>`;
  }

  render() {
    const view = this.view;
    if (!view) return nothing;
    return html`<sl-card data-testid="ci-health-card">
      <div slot="header" class="header">
        <sl-icon name="speedometer2"></sl-icon>
        CI health
        <span class="range">${view.days}d</span>
      </div>
      <h3>Repositories</h3>
      ${this.renderRepositories(view)}
      <h3>Worth a look</h3>
      ${this.renderFlakes(view)}
      <h3>Value, last ${view.days}d</h3>
      ${this.renderValue(view)}
    </sl-card>`;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'flow-ci-health-card': FlowCiHealthCard;
  }
}
