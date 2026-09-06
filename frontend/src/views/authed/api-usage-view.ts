import { LitElement, html, css, nothing, unsafeCSS } from 'lit';
import { customElement, state } from 'lit/decorators.js';

import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/card/card.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/option/option.js';
import '@shoelace-style/shoelace/dist/components/progress-bar/progress-bar.js';
import '@shoelace-style/shoelace/dist/components/select/select.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import '../../components/view-header.ts';
import '../../components/token-figures.ts';
import '../../components/time-range-select.ts';
import {
  getAccountGatewayUsageSearch,
  getAccountGatewayUsageSummary,
  getAccountRateLimitReport,
  type GatewayUsageSummaryParams,
} from '../../api';
import type {
  AccountGatewayUsageSearchResponse,
  AccountGatewayUsageSummaryResponse,
  AccountRateLimitReportResponse,
  RateLimitSnapshotItem,
  GatewayBudgetSummary,
  GatewayTokenUsage,
  GatewayUsageSearchResultItem,
  GatewayUsageByDay,
  GatewayUsageByFlow,
  GatewayUsageByModel,
  GatewayUsageBySession,
} from '../../types';
import consoleStyles from '../../styles/console-styles.css?inline';
import { shortExecutionId } from '../../utils/execution-subject';
import {
  formatTimeRangeWindow,
  resolvePreviousTimeRange,
  resolveTimeRange,
  timeRangeShortLabel,
  type TimeRangeKey,
} from '../../utils/time-range';

// The page carries one range control, the shared `time-range-select`, with the
// same vocabulary as the Overview and Cost. The window itself comes from
// `utils/time-range`, so "30d" here is the same 30 days Cost and the model
// detail page ask the server for.
const DATE_RANGE_OPTIONS: Array<{ value: TimeRangeKey; label: string }> = [
  { value: 'last-24h', label: '24h' },
  { value: 'last-7', label: '7d' },
  { value: 'last-30', label: '30d' },
  { value: 'last-90', label: '90d' },
  { value: 'last-365', label: '1y' },
  // The window with no bounds. The console lost it when the Filters card
  // went; the shared util has always been able to resolve it.
  { value: 'all', label: 'All time' },
];

@customElement('api-usage-view')
export class ApiUsageView extends LitElement {
  @state()
  private summary: AccountGatewayUsageSummaryResponse | null = null;

  // The window before the selected one, so spend can say "vs prior 30d"
  // instead of a bare dollar figure.
  @state()
  private previousSummary: AccountGatewayUsageSummaryResponse | null = null;

  @state()
  private searchResults: AccountGatewayUsageSearchResponse | null = null;

  @state()
  private rateLimitReport: AccountRateLimitReportResponse | null = null;

  @state()
  private loading = true;

  @state()
  private error: string | null = null;

  @state()
  private selectedRange: TimeRangeKey = 'last-30';

  @state()
  private searchQuery = '';

  // The search is its own request against its own card, so it has its own
  // busy flag and its own error rather than blanking the page's numbers.
  @state()
  private searchLoading = false;

  @state()
  private searchError: string | null = null;

  private initialized = false;

  private searchDebounce?: ReturnType<typeof setTimeout>;

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

      .summary-card,
      .breakdown-card {
        overflow: hidden;
      }

      /* One range control, then what it resolved to, then the search that
         narrows the captured interactions. No Apply: the page answers as the
         controls change. */
      .toolbar {
        display: flex;
        gap: var(--sl-spacing-medium);
        align-items: center;
        flex-wrap: wrap;
      }

      .toolbar time-range-select {
        --time-range-select-width: 110px;
      }

      /* The window the numbers cover, restated beside the control that chose
         it, because the sibling pages used to disagree about "30 days". */
      .range-window {
        color: var(--sl-color-neutral-600);
        font-size: var(--sl-font-size-small);
        font-variant-numeric: tabular-nums;
      }

      .usage-search {
        flex: 1 1 260px;
        min-width: 220px;
        margin-left: auto;
      }

      /* Names the search field for assistive tech without a visible label. */
      .usage-search::part(form-control-label) {
        position: absolute;
        width: 1px;
        height: 1px;
        padding: 0;
        margin: -1px;
        overflow: hidden;
        clip: rect(0 0 0 0);
        white-space: nowrap;
        border: 0;
      }

      .results {
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-large);
      }

      /* A range change never blanks answers the page already has: they stay
         readable at 60% until the new ones arrive. A search does the same to
         the one card it changes. */
      .results.is-updating,
      .breakdown-card.is-updating {
        opacity: 0.6;
        pointer-events: none;
      }

      .stats-grid {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
        gap: var(--sl-spacing-medium);
      }

      .stat-card {
        border: 1px solid var(--sl-color-neutral-200);
        border-radius: var(--sl-border-radius-medium);
        padding: var(--sl-spacing-medium);
        background: var(--sl-color-neutral-0);
      }

      .stat-label {
        display: flex;
        align-items: center;
        gap: var(--sl-spacing-x-small);
        color: var(--sl-color-neutral-600);
        font-size: var(--sl-font-size-small);
        margin-bottom: var(--sl-spacing-2x-small);
      }

      .stat-value {
        font-size: 1.6rem;
        font-weight: 700;
        color: var(--sl-color-neutral-900);
        line-height: 1.2;
      }

      .stat-detail {
        margin-top: var(--sl-spacing-2x-small);
        color: var(--sl-color-neutral-600);
        font-size: var(--sl-font-size-small);
      }

      .content-grid {
        display: grid;
        grid-template-columns: minmax(0, 1.5fr) minmax(320px, 1fr);
        gap: var(--sl-spacing-large);
      }

      .stack {
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-large);
      }

      .section-header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: var(--sl-spacing-small);
      }

      .section-title {
        display: flex;
        align-items: center;
        gap: var(--sl-spacing-small);
      }

      .section-subtitle {
        color: var(--sl-color-neutral-600);
        font-size: var(--sl-font-size-small);
      }

      .budget-summary {
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-medium);
      }

      .budget-meta {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
        gap: var(--sl-spacing-small);
      }

      .budget-meta-item {
        padding: var(--sl-spacing-small);
        border-radius: var(--sl-border-radius-medium);
        background: var(--sl-color-neutral-50);
        border: 1px solid var(--sl-color-neutral-200);
      }

      .budget-meta-label {
        color: var(--sl-color-neutral-600);
        font-size: var(--sl-font-size-x-small);
        text-transform: uppercase;
        letter-spacing: 0.04em;
        margin-bottom: 0.2rem;
      }

      .budget-meta-value {
        font-weight: 600;
        color: var(--sl-color-neutral-900);
      }

      .daily-list,
      .breakdown-list {
        display: flex;
        flex-direction: column;
      }

      .daily-row,
      .breakdown-row,
      .breakdown-header {
        display: grid;
        gap: var(--sl-spacing-small);
        align-items: center;
      }

      .daily-row {
        grid-template-columns: minmax(110px, 140px) minmax(0, 1fr) 90px 120px;
        padding: var(--sl-spacing-small) 0;
        border-bottom: 1px solid var(--sl-color-neutral-200);
      }

      .daily-row:last-child,
      .breakdown-row:last-child {
        border-bottom: none;
      }

      .trend-bar {
        height: 10px;
        border-radius: 999px;
        background: var(--sl-color-neutral-100);
        overflow: hidden;
      }

      .trend-bar-fill {
        height: 100%;
        background: linear-gradient(
          90deg,
          var(--sl-color-primary-400),
          var(--sl-color-primary-600)
        );
        border-radius: 999px;
      }

      .breakdown-header,
      .breakdown-row {
        grid-template-columns: minmax(0, 2fr) 110px 120px 110px;
        padding: var(--sl-spacing-small) 0;
        border-bottom: 1px solid var(--sl-color-neutral-200);
      }

      .breakdown-header {
        color: var(--sl-color-neutral-600);
        font-size: var(--sl-font-size-x-small);
        text-transform: uppercase;
        letter-spacing: 0.04em;
        font-weight: 600;
      }

      .breakdown-primary {
        min-width: 0;
      }

      .breakdown-name {
        font-weight: 600;
        color: var(--sl-color-neutral-900);
        overflow-wrap: anywhere;
      }

      .breakdown-secondary {
        color: var(--sl-color-neutral-600);
        font-size: var(--sl-font-size-small);
        margin-top: 0.15rem;
      }

      .session-list {
        display: flex;
        flex-direction: column;
      }

      .session-header,
      .session-row {
        display: grid;
        grid-template-columns: minmax(0, 2.2fr) 90px 120px 100px 170px;
        gap: var(--sl-spacing-small);
        align-items: center;
        padding: var(--sl-spacing-small) 0;
        border-bottom: 1px solid var(--sl-color-neutral-200);
      }

      .session-header {
        color: var(--sl-color-neutral-600);
        font-size: var(--sl-font-size-x-small);
        text-transform: uppercase;
        letter-spacing: 0.04em;
        font-weight: 600;
      }

      .session-row:last-child {
        border-bottom: none;
      }

      .session-primary {
        min-width: 0;
      }

      .session-meta {
        color: var(--sl-color-neutral-600);
        font-size: var(--sl-font-size-x-small);
        margin-top: 0.2rem;
        overflow-wrap: anywhere;
      }

      .session-meta code {
        font-size: inherit;
      }

      .session-link {
        color: var(--sl-color-primary-700);
        text-decoration: none;
      }

      .session-link:hover {
        text-decoration: underline;
      }

      .search-list {
        display: flex;
        flex-direction: column;
      }

      .search-row {
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-x-small);
        padding: var(--sl-spacing-medium) 0;
        border-bottom: 1px solid var(--sl-color-neutral-200);
      }

      .search-row:last-child {
        border-bottom: none;
      }

      .search-header {
        display: flex;
        gap: var(--sl-spacing-small);
        justify-content: space-between;
        align-items: flex-start;
      }

      .search-title {
        font-weight: 600;
        color: var(--sl-color-neutral-900);
        overflow-wrap: anywhere;
      }

      .search-meta,
      .search-excerpt {
        color: var(--sl-color-neutral-600);
        font-size: var(--sl-font-size-small);
        overflow-wrap: anywhere;
      }

      .search-excerpt {
        color: var(--sl-color-neutral-800);
      }

      .cell-numeric {
        text-align: right;
        font-variant-numeric: tabular-nums;
      }

      .empty-state,
      .loading-state {
        text-align: center;
        padding: var(--sl-spacing-x-large);
        color: var(--sl-color-neutral-600);
      }

      .empty-state sl-icon,
      .loading-state sl-spinner {
        font-size: 2rem;
        margin-bottom: var(--sl-spacing-small);
      }

      @media (max-width: 1100px) {
        .content-grid {
          grid-template-columns: 1fr;
        }
      }

      @media (max-width: 720px) {
        .filters-actions {
          margin-left: 0;
          width: 100%;
        }

        .daily-row {
          grid-template-columns: 1fr;
        }

        .breakdown-header {
          display: none;
        }

        .breakdown-row {
          grid-template-columns: 1fr;
          gap: var(--sl-spacing-2x-small);
        }

        .session-header {
          display: none;
        }

        .session-row {
          grid-template-columns: 1fr;
          gap: var(--sl-spacing-2x-small);
        }

        .cell-numeric {
          text-align: left;
        }
      }
    `,
  ];

  connectedCallback() {
    super.connectedCallback();

    if (!this.initialized) {
      this.initialized = true;
      void this.loadSummary();
    }
  }

  disconnectedCallback() {
    if (this.searchDebounce) {
      clearTimeout(this.searchDebounce);
      this.searchDebounce = undefined;
    }
    super.disconnectedCallback();
  }

  /** The selected window, in the shape the gateway endpoints take. */
  private rangeParams(): GatewayUsageSummaryParams {
    const range = resolveTimeRange(this.selectedRange);
    const params: GatewayUsageSummaryParams = {};

    if (range.startDate) {
      params.startDate = range.startDate;
    }

    if (range.endDate) {
      params.endDate = range.endDate;
    }

    return params;
  }

  private async loadSummary() {
    this.loading = true;
    this.error = null;

    const previousRange = resolvePreviousTimeRange(this.selectedRange);

    try {
      const params = this.rangeParams();
      const searchQuery = this.searchQuery.trim();
      const [summary, searchResults, rateLimitReport, previousSummary] =
        await Promise.all([
          getAccountGatewayUsageSummary({
            ...params,
            // This view renders model/flow/session/day breakdowns.
            includeBreakdown: true,
          }),
          getAccountGatewayUsageSearch({
            ...params,
            query: searchQuery || undefined,
            limit: 10,
          }),
          // Rate-limit telemetry is supplementary; a failure here must not
          // blank the whole usage view.
          getAccountRateLimitReport(params).catch((error: unknown) => {
            console.error('Failed to load rate limit report:', error);
            return null;
          }),
          // The comparison window is a garnish on one stat: if it fails, the
          // stat says it has no comparison rather than the page failing. All
          // time has nothing before it, so it costs no request.
          previousRange.startDate
            ? getAccountGatewayUsageSummary({
                startDate: previousRange.startDate,
                endDate: previousRange.endDate ?? undefined,
              }).catch(() => null)
            : Promise.resolve(null),
        ]);
      this.summary = summary;
      this.searchResults = searchResults;
      this.searchError = null;
      this.rateLimitReport = rateLimitReport;
      this.previousSummary = previousSummary;
    } catch (error) {
      console.error('Failed to load account gateway usage summary:', error);
      this.error =
        error instanceof Error
          ? error.message
          : 'Failed to load gateway usage summary';
      this.summary = null;
      this.searchResults = null;
      this.rateLimitReport = null;
      this.previousSummary = null;
    } finally {
      this.loading = false;
    }
  }

  private handleRangeChange(event: Event) {
    const value = (event as CustomEvent<{ value: string }>).detail
      ?.value as TimeRangeKey;
    if (!value || value === this.selectedRange) {
      return;
    }
    this.selectedRange = value;
    void this.loadSummary();
  }

  /**
   * Only the captured interactions depend on the query, so a pause in typing
   * is one request. Reloading the page would cost four (summary with
   * breakdowns, search, the rate-limit report and the comparison window) to
   * change one list.
   */
  private async loadSearchResults() {
    this.searchLoading = true;

    try {
      this.searchResults = await getAccountGatewayUsageSearch({
        ...this.rangeParams(),
        query: this.searchQuery.trim() || undefined,
        limit: 10,
      });
      this.searchError = null;
    } catch (error) {
      console.error('Failed to search captured gateway interactions:', error);
      // A failed search says so where the results would be; the numbers above
      // it are still true and stay on screen.
      this.searchError =
        error instanceof Error
          ? error.message
          : 'Failed to search captured interactions';
      this.searchResults = null;
    } finally {
      this.searchLoading = false;
    }
  }

  private handleSearchQueryChange(event: Event) {
    this.searchQuery = (
      event.target as HTMLInputElement & { value: string }
    ).value;
    // The search hits the server, so a keystroke is not a request: the page
    // waits for a pause in typing.
    if (this.searchDebounce) {
      clearTimeout(this.searchDebounce);
    }
    this.searchDebounce = setTimeout(() => {
      this.searchDebounce = undefined;
      void this.loadSearchResults();
    }, 300);
  }

  /** The short form of the selected range, for stat labels ("$ est. · 30d"). */
  private rangeChipLabel(): string {
    return timeRangeShortLabel(this.selectedRange);
  }

  /**
   * Which days the numbers cover, restated beside the control that chose
   * them. The server's own window wins over the client's preset: what the
   * page prints is what it was actually given.
   */
  private rangeWindowLabel(): string {
    const requested = resolveTimeRange(this.selectedRange);
    return formatTimeRangeWindow({
      startDate: this.summary?.period_start ?? requested.startDate,
      endDate: this.summary?.period_end ?? requested.endDate,
    });
  }

  /**
   * A delta in the Overview's form: an arrow, a percentage, and the window it
   * is measured against. A dollar difference alone says nothing about whether
   * spend doubled or moved a percent.
   */
  private spendComparisonDetail(): string {
    // "All time" has no window before it to compare against.
    if (!resolvePreviousTimeRange(this.selectedRange).startDate) {
      return 'All recorded gateway spend';
    }
    const previousLabel = `prior ${this.rangeChipLabel()}`;
    const current = this.summary?.estimated_cost ?? 0;
    const previous = this.previousSummary?.estimated_cost ?? 0;
    if (!previous || previous <= 0) {
      return `No comparison for ${previousLabel}`;
    }
    const change = ((current - previous) / previous) * 100;
    if (Math.abs(change) < 0.5) {
      return `No change vs ${previousLabel}`;
    }
    const arrow = change > 0 ? '\u25b2' : '\u25bc';
    return `${arrow} ${Math.abs(Math.round(change))}% vs ${previousLabel}`;
  }

  private formatNumber(value: number | null | undefined): string {
    return typeof value === 'number' ? value.toLocaleString() : '0';
  }

  /**
   * Counts big enough to lose their shape are compact, as on the Overview and
   * Cost ("821.3M"), with the exact figure kept in a title for anyone who
   * needs every digit.
   */
  private formatCompactNumber(value: number | null | undefined): string {
    const amount = Number(value || 0);
    if (amount < 1000) return String(Math.round(amount));
    return new Intl.NumberFormat(undefined, {
      notation: 'compact',
      maximumFractionDigits: 1,
    }).format(amount);
  }

  private formatPercent(value: number): string {
    return `${value.toFixed(1)}%`;
  }

  private formatCost(value: number | null | undefined): string {
    if (typeof value !== 'number' || Number.isNaN(value)) {
      return '$0.00';
    }

    if (value === 0) {
      return '$0.00';
    }

    return value >= 0.01 ? `$${value.toFixed(2)}` : `$${value.toFixed(4)}`;
  }

  private formatDateLabel(value: string): string {
    return new Intl.DateTimeFormat(undefined, {
      month: 'short',
      day: 'numeric',
    }).format(new Date(value));
  }

  private formatDateTimeLabel(value: string): string {
    return new Intl.DateTimeFormat(undefined, {
      month: 'short',
      day: 'numeric',
      year: 'numeric',
      hour: 'numeric',
      minute: '2-digit',
    }).format(new Date(value));
  }

  private getSessionSourceType(session: GatewayUsageBySession): string | null {
    return (
      session.session_source_type ??
      (session.flow_execution_id ? 'flow_execution' : null)
    );
  }

  private getSessionSourceId(session: GatewayUsageBySession): string | null {
    return (
      session.session_source_id ??
      session.runtime_session_id ??
      session.flow_execution_id ??
      null
    );
  }

  private getSessionLastActivity(
    session: GatewayUsageBySession
  ): string | null {
    return session.last_activity_at ?? session.last_request_at ?? null;
  }

  private getSessionSourceLabel(sourceType: string | null): string {
    if (!sourceType) {
      return 'Runtime session';
    }

    if (sourceType === 'flow_execution') {
      return 'Flow execution';
    }

    return sourceType
      .split(/[_-]+/g)
      .filter(Boolean)
      .map((segment) => segment.charAt(0).toUpperCase() + segment.slice(1))
      .join(' ');
  }

  private getSessionDisplayName(session: GatewayUsageBySession): string {
    return (
      session.runtime_session_name ??
      session.runtime_principal_name ??
      session.flow_name ??
      session.session_reference ??
      (this.getSessionSourceId(session)
        ? `${this.getSessionSourceLabel(this.getSessionSourceType(session))} ${this.getSessionSourceId(session)}`
        : null) ??
      'Unnamed runtime session'
    );
  }

  private isFlowBackedSession(session: GatewayUsageBySession): boolean {
    return (
      this.getSessionSourceType(session) === 'flow_execution' &&
      Boolean(this.getSessionSourceId(session))
    );
  }

  /**
   * A run id is a link, not a paragraph: a uuid shows its first 8 characters
   * with the whole id in the title. An id that is already a short handle (a
   * codex run key) is left alone, because truncating it would lose meaning.
   */
  private shortSourceId(id: string): string {
    return /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(
      id
    )
      ? shortExecutionId(id)
      : id;
  }

  private getRuntimeSessionHref(session: GatewayUsageBySession): string | null {
    return session.runtime_session_id
      ? `/console/runtime-sessions?sessionId=${session.runtime_session_id}`
      : null;
  }

  private getSuccessRate(summary: AccountGatewayUsageSummaryResponse): number {
    if (summary.total_requests === 0) {
      return 0;
    }

    return (summary.successful_requests / summary.total_requests) * 100;
  }

  private getBudgetLimit(budget: GatewayBudgetSummary): number | null {
    return budget.soft_limit_usd ?? budget.monthly_limit_usd;
  }

  private getBudgetProgress(budget: GatewayBudgetSummary): number {
    const limit = this.getBudgetLimit(budget);
    if (!limit || limit <= 0) {
      return 0;
    }

    return Math.min((budget.current_spend_usd / limit) * 100, 100);
  }

  private renderStatCard(
    label: string,
    value: string,
    detail: unknown,
    icon: string,
    exact?: string
  ) {
    return html`
      <div class="stat-card">
        <div class="stat-label">
          <sl-icon name=${icon}></sl-icon>
          <span>${label}</span>
        </div>
        <div class="stat-value" title=${exact ?? nothing}>${value}</div>
        <div class="stat-detail">${detail}</div>
      </div>
    `;
  }

  private renderBudgetCard(summary: AccountGatewayUsageSummaryResponse) {
    const budget = summary.budget;
    const limit = this.getBudgetLimit(budget);
    const progress = this.getBudgetProgress(budget);

    return html`
      <sl-card class="summary-card">
        <div slot="header" class="section-header">
          <div class="section-title">
            <sl-icon name="cash-stack"></sl-icon>
            <span>Budget snapshot</span>
          </div>
          ${
            budget.hard_limit_exceeded
              ? html`<sl-badge variant="danger">Hard limit exceeded</sl-badge>`
              : budget.soft_limit_exceeded
                ? html`<sl-badge variant="warning"
                    >Soft limit exceeded</sl-badge
                  >`
                : html`<sl-badge variant="success">Within limits</sl-badge>`
          }
        </div>

        <div class="budget-summary">
          <div class="budget-meta">
            <div class="budget-meta-item">
              <div class="budget-meta-label">Current Spend</div>
              <div class="budget-meta-value">
                ${this.formatCost(budget.current_spend_usd)}
              </div>
            </div>
            <div class="budget-meta-item">
              <div class="budget-meta-label">Soft Limit</div>
              <div class="budget-meta-value">
                ${
                  budget.soft_limit_usd === null
                    ? 'Not set'
                    : this.formatCost(budget.soft_limit_usd)
                }
              </div>
            </div>
            <div class="budget-meta-item">
              <div class="budget-meta-label">Monthly Limit</div>
              <div class="budget-meta-value">
                ${
                  budget.monthly_limit_usd === null
                    ? 'Not set'
                    : this.formatCost(budget.monthly_limit_usd)
                }
              </div>
            </div>
          </div>

          ${
            limit
              ? html`
                  <div>
                    <div class="section-subtitle">
                      ${this.formatPercent(progress)} of
                      ${budget.soft_limit_usd !== null ? 'soft' : 'monthly'}
                      limit used
                    </div>
                    <sl-progress-bar
                      value=${progress}
                      style="margin-top: var(--sl-spacing-small);"
                    ></sl-progress-bar>
                  </div>
                `
              : html`
                  <div class="section-subtitle">
                    No account limit is configured yet, but gateway usage is
                    being tracked.
                  </div>
                `
          }
        </div>
      </sl-card>
    `;
  }

  private formatBlockedDuration(blockedMs: number): string {
    if (blockedMs <= 0) {
      return '0s';
    }
    const totalSeconds = Math.round(blockedMs / 1000);
    if (totalSeconds < 60) {
      return `${totalSeconds}s`;
    }
    const minutes = Math.floor(totalSeconds / 60);
    const seconds = totalSeconds % 60;
    return seconds > 0 ? `${minutes}m ${seconds}s` : `${minutes}m`;
  }

  private describeSnapshotHeadroom(snapshot: RateLimitSnapshotItem): string {
    const data = snapshot.rate_limit;
    const parts: string[] = [];
    if (typeof data.requests_remaining === 'number') {
      const limit =
        typeof data.requests_limit === 'number'
          ? ` / ${this.formatNumber(data.requests_limit)}`
          : '';
      parts.push(
        `${this.formatNumber(data.requests_remaining)}${limit} requests left`
      );
    }
    if (typeof data.tokens_remaining === 'number') {
      const limit =
        typeof data.tokens_limit === 'number'
          ? ` / ${this.formatNumber(data.tokens_limit)}`
          : '';
      parts.push(
        `${this.formatNumber(data.tokens_remaining)}${limit} tokens left`
      );
    }
    if (parts.length === 0) {
      const headerCount = Object.keys(data.headers ?? {}).length;
      parts.push(
        `${headerCount} rate-limit header${headerCount === 1 ? '' : 's'} captured`
      );
    }
    return parts.join(' · ');
  }

  private renderRateLimitCard(report: AccountRateLimitReportResponse) {
    const totals = report.totals;
    const snapshots = report.latest_snapshots;
    const hasHits = totals.rate_limited_requests > 0;

    return html`
      <sl-card class="breakdown-card">
        <div slot="header" class="section-header">
          <div class="section-title">
            <sl-icon name="speedometer2"></sl-icon>
            <span>Rate limits and headroom</span>
          </div>
          ${
            hasHits
              ? html`<sl-badge variant="warning">
                  ${this.formatNumber(totals.rate_limited_requests)}
                  hit${totals.rate_limited_requests === 1 ? '' : 's'}
                </sl-badge>`
              : html`<sl-badge variant="success">No 429s</sl-badge>`
          }
        </div>

        <div class="budget-summary">
          <div class="budget-meta">
            <div class="budget-meta-item">
              <div class="budget-meta-label">Rate-Limited Requests</div>
              <div class="budget-meta-value">
                ${this.formatNumber(totals.rate_limited_requests)}
              </div>
            </div>
            <div class="budget-meta-item">
              <div class="budget-meta-label">Time Blocked</div>
              <div class="budget-meta-value">
                ${this.formatBlockedDuration(totals.blocked_ms)}
              </div>
            </div>
            <div class="budget-meta-item">
              <div class="budget-meta-label">Last Hit</div>
              <div class="budget-meta-value">
                ${
                  totals.last_rate_limited_at
                    ? this.formatDateTimeLabel(totals.last_rate_limited_at)
                    : 'Never'
                }
              </div>
            </div>
          </div>

          ${
            hasHits
              ? html`
                  <div class="section-subtitle">
                    ${this.formatNumber(totals.quota_exhausted_count)} quota
                    exhausted · ${this.formatNumber(totals.transient_count)}
                    transient overload
                  </div>
                `
              : ''
          }
          ${
            snapshots.length > 0
              ? html`
                  <div class="breakdown-list">
                    ${snapshots.map(
                      (snapshot) => html`
                        <div class="breakdown-row">
                          <div class="breakdown-primary">
                            <div class="breakdown-name">
                              ${snapshot.model_alias || 'Unknown model'}
                            </div>
                            <div class="breakdown-secondary">
                              ${snapshot.provider_name || 'Unknown provider'}
                              ${
                                snapshot.upstream_credential_type === 'oauth'
                                  ? html`· subscription`
                                  : ''
                              }
                            </div>
                          </div>
                          <div class="breakdown-secondary">
                            ${this.describeSnapshotHeadroom(snapshot)}
                            <br />
                            observed
                            ${this.formatDateTimeLabel(snapshot.observed_at)}
                          </div>
                        </div>
                      `
                    )}
                  </div>
                `
              : html`
                  <div class="section-subtitle">
                    No rate-limit headers observed from providers yet.
                  </div>
                `
          }
          <div class="section-subtitle">
            All figures are read directly from provider response headers. Time
            blocked sums provider Retry-After hints on 429s.
          </div>
        </div>
      </sl-card>
    `;
  }

  private renderDailyUsage(days: GatewayUsageByDay[]) {
    if (days.length === 0) {
      return html`
        <div class="empty-state">
          <sl-icon name="bar-chart"></sl-icon>
          <div>No gateway activity recorded for the selected period.</div>
        </div>
      `;
    }

    const maxRequests = Math.max(...days.map((day) => day.request_count), 1);

    return html`
      <div class="daily-list">
        ${days.map(
          (day) => html`
            <div class="daily-row">
              <div>${this.formatDateLabel(day.date)}</div>
              <div class="trend-bar">
                <div
                  class="trend-bar-fill"
                  style=${`width: ${(day.request_count / maxRequests) * 100}%`}
                ></div>
              </div>
              <div class="cell-numeric">
                ${this.formatNumber(day.request_count)} req
              </div>
              <div class="cell-numeric">
                ${this.formatCost(day.estimated_cost)}
              </div>
            </div>
          `
        )}
      </div>
    `;
  }

  private renderModelBreakdown(models: GatewayUsageByModel[]) {
    if (models.length === 0) {
      return html`
        <div class="empty-state">
          <sl-icon name="cpu"></sl-icon>
          <div>No model-level usage is available for this period.</div>
        </div>
      `;
    }

    const sortedModels = [...models].sort(
      (left, right) =>
        right.estimated_cost - left.estimated_cost ||
        right.request_count - left.request_count
    );

    return html`
      <div class="breakdown-list">
        <div class="breakdown-header">
          <div>Model</div>
          <div class="cell-numeric">Requests</div>
          <div class="cell-numeric">Tokens</div>
          <div class="cell-numeric">Cost</div>
        </div>
        ${sortedModels.map(
          (model) => html`
            <div class="breakdown-row">
              <div class="breakdown-primary">
                <div class="breakdown-name">
                  ${model.model_alias || 'Unnamed model'}
                </div>
                <div class="breakdown-secondary">
                  ${model.provider_name || 'Unknown provider'}
                </div>
              </div>
              <div class="cell-numeric">
                ${this.formatNumber(model.request_count)}
              </div>
              <div class="cell-numeric">
                <token-figures
                  .usage=${model.token_usage}
                  expanded
                ></token-figures>
              </div>
              <div class="cell-numeric">
                ${this.formatCost(model.estimated_cost)}
              </div>
            </div>
          `
        )}
      </div>
    `;
  }

  private renderFlowBreakdown(flows: GatewayUsageByFlow[]) {
    if (flows.length === 0) {
      return html`
        <div class="empty-state">
          <sl-icon name="diagram-3"></sl-icon>
          <div>No flow-level breakdown is available for this period.</div>
        </div>
      `;
    }

    const sortedFlows = [...flows].sort(
      (left, right) =>
        right.request_count - left.request_count ||
        right.estimated_cost - left.estimated_cost
    );

    return html`
      <div class="breakdown-list">
        <div class="breakdown-header">
          <div>Flow</div>
          <div class="cell-numeric">Requests</div>
          <div class="cell-numeric">Tokens</div>
          <div class="cell-numeric">Cost</div>
        </div>
        ${sortedFlows.map(
          (flow) => html`
            <div class="breakdown-row">
              <div class="breakdown-primary">
                <div class="breakdown-name">
                  ${flow.flow_name || 'Unnamed flow'}
                </div>
                <div class="breakdown-secondary">
                  ${flow.flow_id || 'No flow id'}
                </div>
              </div>
              <div class="cell-numeric">
                ${this.formatNumber(flow.request_count)}
              </div>
              <div class="cell-numeric">
                <token-figures
                  .usage=${flow.token_usage}
                  expanded
                ></token-figures>
              </div>
              <div class="cell-numeric">
                ${this.formatCost(flow.estimated_cost)}
              </div>
            </div>
          `
        )}
      </div>
    `;
  }

  private renderSessionBreakdown(sessions: GatewayUsageBySession[]) {
    if (sessions.length === 0) {
      return html`
        <div class="empty-state">
          <sl-icon name="collection"></sl-icon>
          <div>No runtime session usage is available for this period.</div>
        </div>
      `;
    }

    const sortedSessions = [...sessions].sort(
      (left, right) =>
        new Date(this.getSessionLastActivity(right) || 0).getTime() -
          new Date(this.getSessionLastActivity(left) || 0).getTime() ||
        right.request_count - left.request_count
    );

    return html`
      <div class="session-list">
        <div class="session-header">
          <div>Runtime session</div>
          <div class="cell-numeric">Requests</div>
          <div class="cell-numeric">Tokens</div>
          <div class="cell-numeric">Cost</div>
          <div>Last activity</div>
        </div>
        ${sortedSessions.map((session) => {
          const sourceType = this.getSessionSourceType(session);
          const sourceId = this.getSessionSourceId(session);
          const lastActivity = this.getSessionLastActivity(session);
          const flowBacked = this.isFlowBackedSession(session);
          const sessionHref = this.getRuntimeSessionHref(session);

          return html`
            <div class="session-row">
              <div class="session-primary">
                <div class="breakdown-name">
                  ${
                    sessionHref
                      ? html`<a class="session-link" href=${sessionHref}
                          >${this.getSessionDisplayName(session)}</a
                        >`
                      : this.getSessionDisplayName(session)
                  }
                </div>
                <div class="breakdown-secondary">
                  ${session.model_alias || 'Unknown model'}
                  ${session.provider_name ? `\u00b7 ${session.provider_name}` : ''}
                </div>
                ${
                  sourceId
                    ? html`
                        <div class="session-meta">
                          ${
                            flowBacked
                              ? html`Flow execution:
                                  <a
                                    class="session-link"
                                    href=${`/console/flows/executions/${sourceId}`}
                                    title=${sourceId}
                                    >${this.shortSourceId(sourceId)}</a
                                  >`
                              : html`${this.getSessionSourceLabel(sourceType)}
                                  <code title=${sourceId}
                                    >${this.shortSourceId(sourceId)}</code
                                  >`
                          }
                        </div>
                      `
                    : ''
                }
              </div>
              <div
                class="cell-numeric"
                title=${this.formatNumber(session.request_count)}
              >
                ${this.formatCompactNumber(session.request_count)}
              </div>
              <div class="cell-numeric">
                <token-figures
                  .usage=${session.token_usage}
                  expanded
                ></token-figures>
              </div>
              <div class="cell-numeric">
                ${this.formatCost(session.estimated_cost)}
              </div>
              <div>
                ${
                  lastActivity
                    ? this.formatDateTimeLabel(lastActivity)
                    : 'Unknown'
                }
              </div>
            </div>
          `;
        })}
      </div>
    `;
  }

  private renderSearchResults(
    results: AccountGatewayUsageSearchResponse | null
  ) {
    if (this.searchError) {
      return html`
        <div class="empty-state" role="alert">
          <sl-icon name="exclamation-triangle"></sl-icon>
          <div>${this.searchError}</div>
        </div>
      `;
    }

    if (!results || results.items.length === 0) {
      return html`
        <div class="empty-state">
          <sl-icon name="search"></sl-icon>
          <div>
            ${
              this.searchQuery.trim()
                ? 'No captured gateway interactions matched this search.'
                : 'No captured gateway interactions are available yet.'
            }
          </div>
        </div>
      `;
    }

    return html`
      <div class="search-list">
        ${results.items.map((item) => this.renderSearchResult(item))}
      </div>
    `;
  }

  private renderSearchResult(item: GatewayUsageSearchResultItem) {
    const sourceLabel = this.getSessionSourceLabel(item.session_source_type);

    return html`
      <div class="search-row">
        <div class="search-header">
          <div>
            <div class="search-title">
              ${item.model_alias || 'Unknown model'}
              ${item.provider_name ? html`· ${item.provider_name}` : ''}
            </div>
            <div class="search-meta">
              ${item.method} ${item.endpoint} · ${sourceLabel}
              ${
                item.session_reference
                  ? html` · Session <code>${item.session_reference}</code>`
                  : ''
              }
            </div>
          </div>
          <sl-badge variant=${item.outcome === 'error' ? 'danger' : 'success'}>
            ${item.outcome}
          </sl-badge>
        </div>
        <div class="search-excerpt">${item.excerpt}</div>
        <div class="search-meta">
          ${this.formatDateTimeLabel(item.timestamp)} ·
          <token-figures .usage=${item.token_usage}></token-figures> ·
          ${this.formatCost(item.estimated_cost)}
          ${item.flow_name ? html` · ${item.flow_name}` : ''}
          ${
            item.runtime_principal_name
              ? html` · Principal ${item.runtime_principal_name}`
              : ''
          }
        </div>
      </div>
    `;
  }

  private renderSummary(summary: AccountGatewayUsageSummaryResponse) {
    const successRate = this.getSuccessRate(summary);
    const tokenUsage: GatewayTokenUsage = summary.token_usage;
    const range = this.rangeChipLabel();

    return html`
      <div class="stats-grid" role="region" aria-label="Gateway usage totals">
        ${this.renderStatCard(
          `Requests \u00b7 ${range}`,
          this.formatCompactNumber(summary.total_requests),
          `${this.formatNumber(summary.successful_requests)} succeeded, ${this.formatNumber(summary.failed_requests)} failed`,
          'activity',
          this.formatNumber(summary.total_requests)
        )}
        ${this.renderStatCard(
          `$ est. \u00b7 ${range}`,
          this.formatCost(summary.estimated_cost),
          this.spendComparisonDetail(),
          'cash'
        )}
        ${this.renderStatCard(
          `Tokens \u00b7 ${range}`,
          this.formatCompactNumber(tokenUsage.total_tokens),
          html`<token-figures .usage=${tokenUsage} expanded></token-figures>`,
          'cpu',
          this.formatNumber(tokenUsage.total_tokens)
        )}
        ${this.renderStatCard(
          `Success rate \u00b7 ${range}`,
          this.formatPercent(successRate),
          `${this.formatNumber(summary.failed_requests)} failed`,
          'check2-circle'
        )}
      </div>
    `;
  }

  render() {
    return html`
      <view-header headerText="API usage" width="extra-wide"></view-header>
      <div class="column-layout dashboard extra-wide">
        <div class="main-column">
          <div class="page">
            <div class="toolbar">
              <time-range-select
                ariaLabel="Gateway usage range"
                .value=${this.selectedRange}
                .options=${DATE_RANGE_OPTIONS}
                @range-change=${this.handleRangeChange}
              ></time-range-select>
              <span class="range-window">${this.rangeWindowLabel()}</span>
              <sl-input
                class="usage-search"
                label="Search captured interactions"
                placeholder="Search prompts, outputs, or metadata"
                clearable
                .value=${this.searchQuery}
                @sl-input=${this.handleSearchQueryChange}
                @sl-clear=${this.handleSearchQueryChange}
              >
                <sl-icon name="search" slot="prefix"></sl-icon>
              </sl-input>
            </div>

            ${
              this.error
                ? html`
                    <sl-alert variant="danger" open>
                      <sl-icon
                        slot="icon"
                        name="exclamation-triangle"
                      ></sl-icon>
                      ${this.error}
                    </sl-alert>
                  `
                : ''
            }
            ${
              this.loading && !this.summary
                ? html`
                    <sl-card>
                      <div
                        class="loading-state"
                        role="status"
                        aria-live="polite"
                        aria-busy="true"
                      >
                        <sl-spinner></sl-spinner>
                        <div>Loading gateway usage summary...</div>
                      </div>
                    </sl-card>
                  `
                : this.summary
                  ? html`
                      <div
                        class="results ${this.loading ? 'is-updating' : ''}"
                        aria-busy=${this.loading ? 'true' : 'false'}
                      >
                        ${this.renderSummary(this.summary)}

                        <sl-card class="breakdown-card">
                          <div slot="header" class="section-header">
                            <div class="section-title">
                              <sl-icon name="collection"></sl-icon>
                              <span>Recent runtime sessions</span>
                            </div>
                            <span class="section-subtitle">
                              Recent gateway activity grouped by runtime session
                            </span>
                          </div>
                          ${this.renderSessionBreakdown(
                            this.summary.usage_by_session
                          )}
                        </sl-card>

                        <sl-card
                          class="breakdown-card ${
                            this.searchLoading ? 'is-updating' : ''
                          }"
                          aria-busy=${this.searchLoading ? 'true' : 'false'}
                        >
                          <div slot="header" class="section-header">
                            <div class="section-title">
                              <sl-icon name="search"></sl-icon>
                              <span>Captured interactions</span>
                            </div>
                            <span class="section-subtitle">
                              ${
                                this.searchQuery.trim()
                                  ? 'Search results from the indexed gateway corpus'
                                  : 'Recent indexed gateway interactions'
                              }
                            </span>
                          </div>
                          ${this.renderSearchResults(this.searchResults)}
                        </sl-card>

                        <div class="content-grid">
                          <div class="stack">
                            <sl-card class="breakdown-card">
                              <div slot="header" class="section-header">
                                <div class="section-title">
                                  <sl-icon name="bar-chart"></sl-icon>
                                  <span>Daily activity</span>
                                </div>
                                <span class="section-subtitle">
                                  Requests and spend over time
                                </span>
                              </div>
                              ${this.renderDailyUsage(this.summary.requests_by_day)}
                            </sl-card>

                            <sl-card class="breakdown-card">
                              <div slot="header" class="section-header">
                                <div class="section-title">
                                  <sl-icon name="cpu"></sl-icon>
                                  <span>Usage by model</span>
                                </div>
                                <span class="section-subtitle">
                                  Top models by cost and volume
                                </span>
                              </div>
                              ${this.renderModelBreakdown(
                                this.summary.usage_by_model
                              )}
                            </sl-card>
                          </div>

                          <div class="stack">
                            ${this.renderBudgetCard(this.summary)}
                            ${
                              this.rateLimitReport
                                ? this.renderRateLimitCard(this.rateLimitReport)
                                : ''
                            }

                            <sl-card class="breakdown-card">
                              <div slot="header" class="section-header">
                                <div class="section-title">
                                  <sl-icon name="diagram-3"></sl-icon>
                                  <span>Usage by flow</span>
                                </div>
                                <span class="section-subtitle">
                                  Flow-level gateway consumption
                                </span>
                              </div>
                              ${this.renderFlowBreakdown(
                                this.summary.usage_by_flow
                              )}
                            </sl-card>
                          </div>
                        </div>
                      </div>
                    `
                  : html`
                      <sl-card>
                        <div class="empty-state">
                          <sl-icon name="inbox"></sl-icon>
                          <div>No gateway usage summary is available yet.</div>
                        </div>
                      </sl-card>
                    `
            }
          </div>
        </div>
      </div>
    `;
  }
}
