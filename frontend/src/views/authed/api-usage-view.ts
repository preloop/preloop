import { LitElement, html, css, unsafeCSS } from 'lit';
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
import {
  getAccountGatewayUsageSearch,
  getAccountGatewayUsageSummary,
  type GatewayUsageSummaryParams,
} from '../../api';
import type {
  AccountGatewayUsageSearchResponse,
  AccountGatewayUsageSummaryResponse,
  GatewayBudgetSummary,
  GatewayTokenUsage,
  GatewayUsageSearchResultItem,
  GatewayUsageByDay,
  GatewayUsageByFlow,
  GatewayUsageByModel,
  GatewayUsageBySession,
} from '../../types';
import consoleStyles from '../../styles/console-styles.css?inline';

type DateRangePreset = 'last-7' | 'last-30' | 'last-90' | 'all' | 'custom';

@customElement('api-usage-view')
export class ApiUsageView extends LitElement {
  @state()
  private summary: AccountGatewayUsageSummaryResponse | null = null;

  @state()
  private searchResults: AccountGatewayUsageSearchResponse | null = null;

  @state()
  private loading = true;

  @state()
  private error: string | null = null;

  @state()
  private selectedRange: DateRangePreset = 'last-30';

  @state()
  private startDate = '';

  @state()
  private endDate = '';

  @state()
  private searchQuery = '';

  private initialized = false;

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

      .filters-card,
      .summary-card,
      .breakdown-card {
        overflow: hidden;
      }

      .filters-grid {
        display: flex;
        gap: var(--sl-spacing-medium);
        flex-wrap: wrap;
        align-items: end;
      }

      .filters-grid sl-select,
      .filters-grid sl-input {
        min-width: 180px;
      }

      .filters-actions {
        display: flex;
        gap: var(--sl-spacing-small);
        align-items: center;
        margin-left: auto;
      }

      .period-caption {
        margin-top: var(--sl-spacing-small);
        color: var(--sl-color-neutral-600);
        font-size: var(--sl-font-size-small);
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
      if (this.selectedRange !== 'custom') {
        this.applyPresetDates(this.selectedRange);
      }
      this.initialized = true;
      void this.loadSummary();
    }
  }

  private async loadSummary() {
    this.loading = true;
    this.error = null;

    try {
      const params: GatewayUsageSummaryParams = {};

      if (this.startDate) {
        params.startDate = new Date(`${this.startDate}T00:00:00`).toISOString();
      }

      if (this.endDate) {
        params.endDate = new Date(`${this.endDate}T23:59:59.999`).toISOString();
      }

      const searchQuery = this.searchQuery.trim();
      const [summary, searchResults] = await Promise.all([
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
      ]);
      this.summary = summary;
      this.searchResults = searchResults;
    } catch (error) {
      console.error('Failed to load account gateway usage summary:', error);
      this.error =
        error instanceof Error
          ? error.message
          : 'Failed to load gateway usage summary';
      this.summary = null;
      this.searchResults = null;
    } finally {
      this.loading = false;
    }
  }

  private getLocalDateString(date: Date): string {
    const year = date.getFullYear();
    const month = `${date.getMonth() + 1}`.padStart(2, '0');
    const day = `${date.getDate()}`.padStart(2, '0');
    return `${year}-${month}-${day}`;
  }

  private applyPresetDates(range: Exclude<DateRangePreset, 'custom'>) {
    if (range === 'all') {
      this.startDate = '';
      this.endDate = '';
      return;
    }

    const today = new Date();
    const endDate = new Date(today);
    const startDate = new Date(today);
    const days = range === 'last-7' ? 7 : range === 'last-30' ? 30 : 90;

    startDate.setDate(startDate.getDate() - (days - 1));

    this.startDate = this.getLocalDateString(startDate);
    this.endDate = this.getLocalDateString(endDate);
  }

  private handleRangeChange(event: Event) {
    const value = (event.target as HTMLInputElement & { value: string })
      .value as DateRangePreset;

    this.selectedRange = value;

    if (value !== 'custom') {
      this.applyPresetDates(value);
      void this.loadSummary();
    }
  }

  private handleStartDateChange(event: Event) {
    this.startDate = (
      event.target as HTMLInputElement & { value: string }
    ).value;
    this.selectedRange = 'custom';
  }

  private handleEndDateChange(event: Event) {
    this.endDate = (event.target as HTMLInputElement & { value: string }).value;
    this.selectedRange = 'custom';
  }

  private handleSearchQueryChange(event: Event) {
    this.searchQuery = (
      event.target as HTMLInputElement & { value: string }
    ).value;
  }

  private async applyCustomFilters() {
    if (this.startDate && this.endDate && this.startDate > this.endDate) {
      this.error = 'Start date must be earlier than end date.';
      return;
    }

    await this.loadSummary();
  }

  private async clearFilters() {
    this.selectedRange = 'all';
    this.startDate = '';
    this.endDate = '';
    await this.loadSummary();
  }

  private formatNumber(value: number | null | undefined): string {
    return typeof value === 'number' ? value.toLocaleString() : '0';
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
    detail: string,
    icon: string
  ) {
    return html`
      <div class="stat-card">
        <div class="stat-label">
          <sl-icon name=${icon}></sl-icon>
          <span>${label}</span>
        </div>
        <div class="stat-value">${value}</div>
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
            <span>Budget Snapshot</span>
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
                ${this.formatNumber(model.token_usage.total_tokens)}
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
                ${this.formatNumber(flow.token_usage.total_tokens)}
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
          <div>Runtime Session</div>
          <div class="cell-numeric">Requests</div>
          <div class="cell-numeric">Tokens</div>
          <div class="cell-numeric">Cost</div>
          <div>Last Activity</div>
        </div>
        ${sortedSessions.map((session) => {
          const sourceType = this.getSessionSourceType(session);
          const sourceId = this.getSessionSourceId(session);
          const lastActivity = this.getSessionLastActivity(session);
          const flowBacked = this.isFlowBackedSession(session);

          return html`
            <div class="session-row">
              <div class="session-primary">
                <div class="breakdown-name">
                  ${
                    this.getRuntimeSessionHref(session)
                      ? html`
                          <a
                            class="session-link"
                            href=${this.getRuntimeSessionHref(session)!}
                            >${this.getSessionDisplayName(session)}</a
                          >
                        `
                      : this.getSessionDisplayName(session)
                  }
                </div>
                <div class="breakdown-secondary">
                  ${session.model_alias || 'Unknown model'}
                  ${
                    session.provider_name
                      ? html`· ${session.provider_name}`
                      : ''
                  }
                </div>
                <div class="session-meta">
                  Source: ${this.getSessionSourceLabel(sourceType)}
                </div>
                ${
                  sourceId
                    ? html`
                        <div class="session-meta">
                          ${
                            flowBacked
                              ? html`
                                  Flow execution:
                                  <a
                                    class="session-link"
                                    href=${`/console/flows/executions/${sourceId}`}
                                    >${sourceId}</a
                                  >
                                `
                              : html` Source ID: <code>${sourceId}</code> `
                          }
                        </div>
                      `
                    : ''
                }
                ${
                  session.session_reference
                    ? html`
                        <div class="session-meta">
                          Session reference:
                          <code>${session.session_reference}</code>
                        </div>
                      `
                    : ''
                }
              </div>
              <div class="cell-numeric">
                ${this.formatNumber(session.request_count)}
              </div>
              <div class="cell-numeric">
                ${this.formatNumber(session.token_usage.total_tokens)}
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
          ${this.formatNumber(item.token_usage.total_tokens)} tokens ·
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

    return html`
      <div class="stats-grid">
        ${this.renderStatCard(
          'Requests',
          this.formatNumber(summary.total_requests),
          `${this.formatNumber(summary.successful_requests)} succeeded, ${this.formatNumber(summary.failed_requests)} failed`,
          'activity'
        )}
        ${this.renderStatCard(
          'Estimated Cost',
          this.formatCost(summary.estimated_cost),
          `${this.formatCost(summary.budget.current_spend_usd)} current spend`,
          'cash'
        )}
        ${this.renderStatCard(
          'Total Tokens',
          this.formatNumber(tokenUsage.total_tokens),
          `${this.formatNumber(tokenUsage.prompt_tokens)} prompt, ${this.formatNumber(tokenUsage.completion_tokens)} completion`,
          'cpu'
        )}
        ${this.renderStatCard(
          'Success Rate',
          this.formatPercent(successRate),
          `${this.formatDateTimeLabel(summary.period_start)} to ${this.formatDateTimeLabel(summary.period_end)}`,
          'check2-circle'
        )}
      </div>
    `;
  }

  render() {
    return html`
      <view-header headerText="API Usage" width="extra-wide"></view-header>
      <div class="column-layout dashboard extra-wide">
        <div class="main-column">
          <div class="page">
            <sl-card class="filters-card">
              <div slot="header" class="section-header">
                <div class="section-title">
                  <sl-icon name="funnel"></sl-icon>
                  <span>Gateway Usage Filters</span>
                </div>
              </div>

              <div class="filters-grid">
                <sl-select
                  label="Date range"
                  value=${this.selectedRange}
                  @sl-change=${this.handleRangeChange}
                >
                  <sl-option value="last-7">Last 7 days</sl-option>
                  <sl-option value="last-30">Last 30 days</sl-option>
                  <sl-option value="last-90">Last 90 days</sl-option>
                  <sl-option value="all">All time</sl-option>
                  <sl-option value="custom">Custom</sl-option>
                </sl-select>

                <sl-input
                  type="date"
                  label="Start date"
                  .value=${this.startDate}
                  @sl-change=${this.handleStartDateChange}
                ></sl-input>

                <sl-input
                  type="date"
                  label="End date"
                  .value=${this.endDate}
                  @sl-change=${this.handleEndDateChange}
                ></sl-input>

                <sl-input
                  label="Conversation search"
                  placeholder="Search captured prompts, outputs, or metadata"
                  .value=${this.searchQuery}
                  @sl-input=${this.handleSearchQueryChange}
                ></sl-input>

                <div class="filters-actions">
                  <sl-button
                    variant="primary"
                    @click=${this.applyCustomFilters}
                  >
                    Apply
                  </sl-button>
                  <sl-button variant="default" @click=${this.clearFilters}>
                    Reset
                  </sl-button>
                </div>
              </div>

              ${
                this.summary
                  ? html`
                      <div class="period-caption">
                        Showing gateway usage from
                        ${this.formatDateTimeLabel(this.summary.period_start)}
                        to ${this.formatDateTimeLabel(this.summary.period_end)}.
                      </div>
                    `
                  : ''
              }
            </sl-card>

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
              this.loading
                ? html`
                    <sl-card>
                      <div class="loading-state">
                        <sl-spinner></sl-spinner>
                        <div>Loading gateway usage summary...</div>
                      </div>
                    </sl-card>
                  `
                : this.summary
                  ? html`
                      ${this.renderSummary(this.summary)}

                      <sl-card class="breakdown-card">
                        <div slot="header" class="section-header">
                          <div class="section-title">
                            <sl-icon name="collection"></sl-icon>
                            <span>Recent Runtime Sessions</span>
                          </div>
                          <span class="section-subtitle">
                            Recent gateway activity grouped by runtime session
                          </span>
                        </div>
                        ${this.renderSessionBreakdown(
                          this.summary.usage_by_session
                        )}
                      </sl-card>

                      <sl-card class="breakdown-card">
                        <div slot="header" class="section-header">
                          <div class="section-title">
                            <sl-icon name="search"></sl-icon>
                            <span>Captured Interactions</span>
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
                                <span>Daily Activity</span>
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
                                <span>Usage By Model</span>
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

                          <sl-card class="breakdown-card">
                            <div slot="header" class="section-header">
                              <div class="section-title">
                                <sl-icon name="diagram-3"></sl-icon>
                                <span>Usage By Flow</span>
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
