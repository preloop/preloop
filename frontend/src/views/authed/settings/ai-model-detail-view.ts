import { LitElement, html, css, unsafeCSS } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import { Router } from '@vaadin/router';

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
import '@shoelace-style/shoelace/dist/components/textarea/textarea.js';
import '../../../components/view-header.ts';
import '../../../components/resource-actions.ts';
import '../../../components/budget-policy-editor.ts';
import '../../../components/preloop-session-observer.ts';
import '../../../components/add-ai-model-modal';
import {
  deleteAIModel,
  extractErrorMessage,
  fetchWithAuth,
  getAIModel,
  getAIModelGatewayUsageSearch,
  getAIModelGatewayUsageSummary,
  getAIModelRuntimeSessions,
  updateAIModel,
  type GatewayUsageSummaryParams,
} from '../../../api';
import type {
  AIModel,
  AIModelGatewayUsageSearchResponse,
  AIModelGatewayUsageSummaryResponse,
  AIModelRuntimeSessionListResponse,
  GatewayUsageByDay,
  GatewayUsageSearchResultItem,
  RuntimeSessionSummary,
} from '../../../types';
import { unifiedWebSocketManager } from '../../../services/unified-websocket-manager';
import consoleStyles from '../../../styles/console-styles.css?inline';
import { consoleDialogStyles } from '../../../styles/console-dialog';

type DateRangePreset = 'last-7' | 'last-30' | 'last-90' | 'all' | 'custom';

@customElement('ai-model-detail-view')
export class AIModelDetailView extends LitElement {
  @property({ type: String })
  modelId = '';

  @state()
  private model: AIModel | null = null;

  @state()
  private summary: AIModelGatewayUsageSummaryResponse | null = null;

  @state()
  private sessions: AIModelRuntimeSessionListResponse | null = null;

  @state()
  private interactions: AIModelGatewayUsageSearchResponse | null = null;

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
  private interactionQuery = '';

  @state()
  private validationPrompt =
    'Welcome to Preloop. Reply with a short acknowledgement.';

  @state()
  private validationResponse = '';

  @state()
  private validationError: string | null = null;

  @state()
  private validationInFlight = false;

  @state()
  private gatewayEnableInFlight = false;

  @state()
  private isEditModalOpen = false;

  @state()
  private isDeleteConfirmOpen = false;

  private initialized = false;
  private unsubscribeRealtime?: () => void;
  private refreshTimer: number | null = null;
  private refreshInFlight = false;

  static styles = [
    consoleDialogStyles,
    unsafeCSS(consoleStyles),
    css`
      :host {
        display: block;
      }

      .page,
      .stack,
      .daily-list,
      .session-list,
      .interaction-list {
        display: flex;
        flex-direction: column;
      }

      .page,
      .stack {
        gap: var(--sl-spacing-large);
      }

      .filters-grid,
      .interaction-toolbar {
        display: flex;
        gap: var(--sl-spacing-medium);
        flex-wrap: wrap;
        align-items: end;
      }

      .filters-grid sl-select,
      .filters-grid sl-input,
      .interaction-toolbar sl-input {
        min-width: 180px;
      }

      .interaction-toolbar sl-input {
        min-width: 280px;
      }

      .filters-actions {
        display: flex;
        gap: var(--sl-spacing-small);
        margin-left: auto;
      }

      .period-caption,
      .meta-line,
      .session-meta,
      .interaction-meta,
      .interaction-excerpt,
      .stat-detail {
        color: var(--sl-color-neutral-600);
        font-size: var(--sl-font-size-small);
        overflow-wrap: anywhere;
      }

      .period-caption {
        margin-top: var(--sl-spacing-small);
      }

      .model-heading {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: var(--sl-spacing-medium);
        flex-wrap: wrap;
      }

      .model-title {
        font-size: 1.2rem;
        font-weight: 700;
        color: var(--sl-color-neutral-900);
      }

      .badge-row,
      .model-metadata {
        display: flex;
        gap: var(--sl-spacing-small);
        flex-wrap: wrap;
      }

      .metadata-stack,
      .validation-stack {
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-small);
      }

      .validation-toolbar {
        display: flex;
        gap: var(--sl-spacing-small);
        align-items: center;
        flex-wrap: wrap;
      }

      .validation-output {
        white-space: pre-wrap;
        font-family: var(--sl-font-mono);
        font-size: var(--sl-font-size-small);
        background: var(--sl-color-neutral-50);
        border: 1px solid var(--sl-color-neutral-200);
        border-radius: var(--sl-border-radius-medium);
        padding: var(--sl-spacing-medium);
      }

      .summary-grid {
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
        color: var(--sl-color-neutral-600);
        font-size: var(--sl-font-size-small);
        margin-bottom: var(--sl-spacing-2x-small);
      }

      .stat-value {
        font-size: 1.5rem;
        line-height: 1.2;
        font-weight: 700;
        color: var(--sl-color-neutral-900);
      }

      .daily-row,
      .session-row {
        display: grid;
        gap: var(--sl-spacing-small);
        align-items: center;
        padding: var(--sl-spacing-small) 0;
        border-bottom: 1px solid var(--sl-color-neutral-200);
      }

      .daily-row {
        grid-template-columns: minmax(110px, 140px) minmax(0, 1fr) 90px 120px;
      }

      .session-row {
        grid-template-columns: minmax(0, 2fr) 90px 120px 100px 170px;
      }

      .daily-row:last-child,
      .session-row:last-child,
      .interaction-row:last-child {
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

      .session-primary {
        min-width: 0;
      }

      .session-title,
      .interaction-title {
        font-weight: 600;
        color: var(--sl-color-neutral-900);
        overflow-wrap: anywhere;
      }

      .session-link {
        color: var(--sl-color-primary-700);
        text-decoration: none;
      }

      .session-link:hover {
        text-decoration: underline;
      }

      .interaction-row {
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-x-small);
        padding: var(--sl-spacing-medium) 0;
        border-bottom: 1px solid var(--sl-color-neutral-200);
      }

      .interaction-header {
        display: flex;
        align-items: flex-start;
        justify-content: space-between;
        gap: var(--sl-spacing-small);
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

      @media (max-width: 720px) {
        .filters-actions {
          margin-left: 0;
          width: 100%;
        }

        .daily-row,
        .session-row {
          grid-template-columns: 1fr;
        }

        .cell-numeric {
          text-align: left;
        }
      }
    `,
  ];

  onBeforeEnter(location: { params: { modelId?: string } }) {
    const nextModelId = location.params.modelId ?? '';
    const changed = this.modelId !== nextModelId;
    this.modelId = nextModelId;

    if (this.initialized && changed) {
      void this.loadData();
    }
  }

  connectedCallback() {
    super.connectedCallback();
    this.connectRealtime();

    if (!this.initialized) {
      if (this.selectedRange !== 'custom') {
        this.applyPresetDates(this.selectedRange);
      }
      this.initialized = true;
      if (this.modelId) {
        void this.loadData();
      }
    }
  }

  disconnectedCallback(): void {
    super.disconnectedCallback();
    this.unsubscribeRealtime?.();
    if (this.refreshTimer !== null) {
      window.clearTimeout(this.refreshTimer);
      this.refreshTimer = null;
    }
  }

  private connectRealtime(): void {
    const scheduleRefresh = () => this.scheduleRefresh();
    const unsubscribers = [
      unifiedWebSocketManager.subscribe(
        'gateway_activity',
        scheduleRefresh,
        (message) => message?.payload?.ai_model_id === this.modelId
      ),
      unifiedWebSocketManager.subscribe(
        'budget_health',
        scheduleRefresh,
        (message) => message?.payload?.ai_model_id === this.modelId
      ),
      unifiedWebSocketManager.subscribe(
        'runtime_sessions',
        scheduleRefresh,
        (message) => this.shouldRefreshForRuntimeSession(message)
      ),
      unifiedWebSocketManager.subscribe(
        'system',
        scheduleRefresh,
        (message) => message?.type === 'authenticated'
      ),
    ];
    this.unsubscribeRealtime = () => {
      for (const unsubscribe of unsubscribers) {
        unsubscribe();
      }
    };
    void unifiedWebSocketManager.connect();
  }

  private shouldRefreshForRuntimeSession(message: any): boolean {
    const runtimeSessionId = message?.payload?.runtime_session_id;
    if (!runtimeSessionId) {
      return false;
    }
    if (this.selectedSessionId === runtimeSessionId) {
      return true;
    }
    return (
      this.sessions?.items?.some(
        (session) => session.id === runtimeSessionId
      ) ?? false
    );
  }

  private scheduleRefresh(): void {
    if (!this.modelId) {
      return;
    }
    if (this.refreshTimer !== null) {
      window.clearTimeout(this.refreshTimer);
    }
    this.refreshTimer = window.setTimeout(() => {
      this.refreshTimer = null;
      void this.loadData({ preserveLoadingState: true });
    }, 250);
  }

  private async loadData(options: { preserveLoadingState?: boolean } = {}) {
    if (!this.modelId) {
      this.error = 'Missing AI model id.';
      this.loading = false;
      return;
    }

    if (this.refreshInFlight) {
      return;
    }
    this.refreshInFlight = true;
    if (!options.preserveLoadingState) {
      this.loading = true;
    }
    this.error = null;

    try {
      this.model = await getAIModel(this.modelId);
    } catch (error) {
      this.error =
        error instanceof Error ? error.message : 'Failed to fetch AI model';
      this.model = null;
      this.summary = null;
      this.sessions = null;
      this.interactions = null;
      this.loading = false;
      return;
    }

    try {
      const params = this.buildSummaryParams();
      const [summary, sessions, interactions] = await Promise.all([
        getAIModelGatewayUsageSummary(this.modelId, params),
        getAIModelRuntimeSessions(this.modelId, {
          ...params,
          limit: 10,
          status: 'all',
        }),
        getAIModelGatewayUsageSearch(this.modelId, {
          ...params,
          query: this.interactionQuery.trim() || undefined,
          limit: 10,
        }),
      ]);
      this.summary = summary;
      this.sessions = sessions;
      this.interactions = interactions;
    } catch (error) {
      this.error =
        error instanceof Error
          ? error.message
          : 'Failed to fetch AI model observability data';
      this.summary = null;
      this.sessions = null;
      this.interactions = null;
    } finally {
      this.loading = false;
      this.refreshInFlight = false;
    }
  }

  private buildSummaryParams(): GatewayUsageSummaryParams {
    const params: GatewayUsageSummaryParams = {};

    if (this.startDate) {
      params.startDate = new Date(`${this.startDate}T00:00:00`).toISOString();
    }
    if (this.endDate) {
      params.endDate = new Date(`${this.endDate}T23:59:59.999`).toISOString();
    }

    return params;
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
    const startDate = new Date(today);
    const days = range === 'last-7' ? 7 : range === 'last-30' ? 30 : 90;
    startDate.setDate(startDate.getDate() - (days - 1));

    this.startDate = this.getLocalDateString(startDate);
    this.endDate = this.getLocalDateString(today);
  }

  private handleRangeChange(event: Event) {
    const value = (event.target as HTMLInputElement & { value: string })
      .value as DateRangePreset;
    this.selectedRange = value;

    if (value !== 'custom') {
      this.applyPresetDates(value);
      void this.loadData();
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

  private handleInteractionQueryChange(event: Event) {
    this.interactionQuery = (
      event.target as HTMLInputElement & { value: string }
    ).value;
  }

  private async applyFilters() {
    if (this.startDate && this.endDate && this.startDate > this.endDate) {
      this.error = 'Start date must be earlier than end date.';
      return;
    }

    await this.loadData();
  }

  private async clearFilters() {
    this.selectedRange = 'last-30';
    this.applyPresetDates('last-30');
    this.interactionQuery = '';
    await this.loadData();
  }

  private formatNumber(value: number | null | undefined): string {
    return typeof value === 'number' ? value.toLocaleString() : '0';
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

  private formatPercent(numerator: number, denominator: number): string {
    if (denominator === 0) {
      return '0.0%';
    }
    return `${((numerator / denominator) * 100).toFixed(1)}%`;
  }

  private formatDateLabel(value: string): string {
    return new Intl.DateTimeFormat(undefined, {
      month: 'short',
      day: 'numeric',
    }).format(new Date(value));
  }

  private formatDateTime(value: string | null | undefined): string {
    if (!value) {
      return 'Unknown';
    }
    return new Intl.DateTimeFormat(undefined, {
      month: 'short',
      day: 'numeric',
      year: 'numeric',
      hour: 'numeric',
      minute: '2-digit',
    }).format(new Date(value));
  }

  private getSourceLabel(sourceType: string | null | undefined): string {
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

  private getSessionDisplayName(session: RuntimeSessionSummary): string {
    return (
      session.runtime_principal_name ??
      session.flow_name ??
      session.session_reference ??
      `${this.getSourceLabel(session.session_source_type)} ${session.session_source_id}`
    );
  }

  private getGatewayConfig(): {
    enabled?: boolean;
    model_alias?: string;
    url?: string;
  } | null {
    const gateway = this.model?.meta_data?.gateway;
    return gateway && typeof gateway === 'object'
      ? (gateway as { enabled?: boolean; model_alias?: string; url?: string })
      : null;
  }

  private get gatewayModelAlias(): string {
    if (this.model?.model_kind && this.model.model_kind !== 'llm') {
      return '';
    }
    const gatewayAlias = this.getGatewayConfig()?.model_alias?.trim();
    if (gatewayAlias) {
      return gatewayAlias;
    }
    if (!this.model) {
      return '';
    }
    return `${String(this.model.provider_name || '').toLowerCase()}/${this.model.model_identifier}`;
  }

  private get gatewayEnabled(): boolean {
    return Boolean(this.getGatewayConfig()?.enabled);
  }

  private openEditModal = () => {
    if (!this.model) {
      return;
    }
    this.isEditModalOpen = true;
  };

  private closeEditModal = () => {
    this.isEditModalOpen = false;
  };

  private async handleModelUpdated() {
    this.closeEditModal();
    await this.loadData({ preserveLoadingState: true });
  }

  private openDeleteConfirm = () => {
    if (!this.model) {
      return;
    }
    this.isDeleteConfirmOpen = true;
  };

  private async confirmDelete() {
    if (!this.model) {
      return;
    }
    try {
      await deleteAIModel(this.model.id);
      this.isDeleteConfirmOpen = false;
      Router.go('/console/ai-models');
    } catch (error) {
      this.isDeleteConfirmOpen = false;
      this.error =
        error instanceof Error ? error.message : 'Failed to delete model';
    }
  }

  private async enableGatewayRouting() {
    if (!this.model?.id || !this.model.has_api_key) {
      this.validationError =
        'Add upstream API credentials on this model before enabling gateway routing.';
      return;
    }
    this.gatewayEnableInFlight = true;
    this.validationError = null;
    try {
      const meta: Record<string, unknown> = {
        ...(this.model.meta_data && typeof this.model.meta_data === 'object'
          ? this.model.meta_data
          : {}),
      };
      const provider = String(this.model.provider_name || '').toLowerCase();
      const mid = this.model.model_identifier;
      meta.gateway = {
        enabled: true,
        provider_adapter: 'preloop',
        model_alias: `${provider}/${mid}`,
      };
      this.model = await updateAIModel(this.model.id, { meta_data: meta });
      await this.loadData({ preserveLoadingState: true });
    } catch (error) {
      this.validationError =
        error instanceof Error
          ? error.message
          : 'Failed to enable gateway routing';
    } finally {
      this.gatewayEnableInFlight = false;
    }
  }

  private get managedAgentDisplayName(): string | null {
    const value = this.model?.meta_data?.managed_agent_display_name;
    return typeof value === 'string' && value.trim() ? value.trim() : null;
  }

  private get managedAgentId(): string | null {
    const value = this.model?.meta_data?.managed_agent_id;
    return typeof value === 'string' && value.trim() ? value.trim() : null;
  }

  private get managedAgentRuntimePrincipalId(): string | null {
    const value = this.model?.meta_data?.managed_agent_runtime_principal_id;
    return typeof value === 'string' && value.trim() ? value.trim() : null;
  }

  private async runValidationPrompt() {
    if (
      !this.gatewayEnabled ||
      !this.gatewayModelAlias ||
      !this.validationPrompt.trim()
    ) {
      this.validationError =
        'This model is not gateway-enabled or the prompt is empty.';
      return;
    }

    this.validationInFlight = true;
    this.validationError = null;
    this.validationResponse = '';

    try {
      const response = await fetchWithAuth('/openai/v1/responses', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          model: this.gatewayModelAlias,
          input: this.validationPrompt.trim(),
        }),
      });
      const responseData = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(
          extractErrorMessage(responseData, 'Failed to run model request')
        );
      }
      const outputText = Array.isArray(responseData?.output)
        ? responseData.output
            .flatMap((item: any) =>
              Array.isArray(item?.content)
                ? item.content
                    .map((contentItem: any) =>
                      typeof contentItem?.text === 'string'
                        ? contentItem.text
                        : null
                    )
                    .filter(Boolean)
                : []
            )
            .join('\n')
        : '';
      this.validationResponse =
        outputText ||
        responseData?.output_text ||
        JSON.stringify(responseData, null, 2);
      await this.loadData({ preserveLoadingState: true });
    } catch (error) {
      this.validationError =
        error instanceof Error ? error.message : 'Failed to run model request';
    } finally {
      this.validationInFlight = false;
    }
  }

  private renderStatCard(label: string, value: string, detail: string) {
    return html`
      <div class="stat-card">
        <div class="stat-label">${label}</div>
        <div class="stat-value">${value}</div>
        <div class="stat-detail">${detail}</div>
      </div>
    `;
  }

  private renderDailyUsage(days: GatewayUsageByDay[]) {
    if (days.length === 0) {
      return html`
        <div class="empty-state">
          <sl-icon name="bar-chart"></sl-icon>
          <div>No model activity was recorded for the selected period.</div>
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

  private renderSessions() {
    if (!this.sessions || this.sessions.items.length === 0) {
      return html`
        <div class="empty-state">
          <sl-icon name="collection"></sl-icon>
          <div>No runtime sessions used this model in the selected period.</div>
        </div>
      `;
    }

    return html`
      <div class="session-list">
        ${this.sessions.items.map(
          (session) => html`
            <div class="session-row">
              <div class="session-primary">
                <div class="session-title">
                  <a
                    class="session-link"
                    href=${`/console/runtime-sessions?sessionId=${session.id}`}
                  >
                    ${this.getSessionDisplayName(session)}
                  </a>
                </div>
                <div class="session-meta">
                  ${this.getSourceLabel(session.session_source_type)}
                  ${
                    session.session_reference
                      ? html` · Session
                          <code>${session.session_reference}</code>`
                      : ''
                  }
                </div>
                ${
                  session.flow_execution_id
                    ? html`
                        <div class="session-meta">
                          Flow execution
                          <a
                            class="session-link"
                            href=${`/console/flows/executions/${session.flow_execution_id}`}
                          >
                            ${session.flow_execution_id}
                          </a>
                        </div>
                      `
                    : ''
                }
              </div>
              <div class="cell-numeric">
                ${this.formatNumber(session.total_requests)}
              </div>
              <div class="cell-numeric">
                ${this.formatNumber(session.token_usage.total_tokens)}
              </div>
              <div class="cell-numeric">
                ${this.formatCost(session.estimated_cost)}
              </div>
              <div>
                ${this.formatDateTime(
                  session.last_request_at || session.last_activity_at
                )}
              </div>
            </div>
          `
        )}
      </div>
    `;
  }

  private renderInteraction(item: GatewayUsageSearchResultItem) {
    return html`
      <div class="interaction-row">
        <div class="interaction-header">
          <div>
            <div class="interaction-title">${item.method} ${item.endpoint}</div>
            <div class="interaction-meta">
              ${this.formatDateTime(item.timestamp)}
              ${
                item.session_reference
                  ? html` · Session <code>${item.session_reference}</code>`
                  : ''
              }
              ${
                item.runtime_session_id
                  ? html`
                      ·
                      <a
                        class="session-link"
                        href=${`/console/runtime-sessions?sessionId=${item.runtime_session_id}`}
                      >
                        Open runtime session
                      </a>
                    `
                  : ''
              }
            </div>
          </div>
          <sl-badge variant=${item.outcome === 'error' ? 'danger' : 'success'}>
            ${item.outcome}
          </sl-badge>
        </div>
        <div class="interaction-excerpt">${item.excerpt}</div>
        <div class="interaction-meta">
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

  private renderInteractions() {
    if (!this.interactions || this.interactions.items.length === 0) {
      return html`
        <div class="empty-state">
          <sl-icon name="search"></sl-icon>
          <div>
            ${
              this.interactionQuery.trim()
                ? 'No captured interactions matched this model search.'
                : 'No captured interactions are available for this model yet.'
            }
          </div>
        </div>
      `;
    }

    return html`
      <div class="interaction-list">
        ${this.interactions.items.map((item) => this.renderInteraction(item))}
      </div>
    `;
  }

  private renderSummarySection() {
    if (!this.summary) {
      return html`
        <div class="empty-state">
          <sl-icon name="cpu"></sl-icon>
          <div>Model-scoped usage summary is not available yet.</div>
        </div>
      `;
    }

    return html`
      <div class="stack">
        <div class="summary-grid">
          ${this.renderStatCard(
            'Requests',
            this.formatNumber(this.summary.total_requests),
            `${this.formatNumber(this.summary.successful_requests)} succeeded, ${this.formatNumber(this.summary.failed_requests)} failed`
          )}
          ${this.renderStatCard(
            'Estimated Cost',
            this.formatCost(this.summary.estimated_cost),
            `${this.formatPercent(this.summary.successful_requests, this.summary.total_requests)} success rate`
          )}
          ${this.renderStatCard(
            'Total Tokens',
            this.formatNumber(this.summary.token_usage.total_tokens),
            `${this.formatNumber(this.summary.token_usage.prompt_tokens)} prompt, ${this.formatNumber(this.summary.token_usage.completion_tokens)} completion`
          )}
          ${this.renderStatCard(
            'Tracked Period',
            this.formatDateTime(this.summary.period_end),
            `${this.formatDateTime(this.summary.period_start)} to ${this.formatDateTime(this.summary.period_end)}`
          )}
        </div>
        <div>
          <div
            class="meta-line"
            style="margin-bottom: var(--sl-spacing-small);"
          >
            Daily requests and spend for this model
          </div>
          ${this.renderDailyUsage(this.summary.requests_by_day)}
        </div>
      </div>
    `;
  }

  private renderGatewayValidation() {
    const gatewayConfig = this.getGatewayConfig();
    return html`
      <sl-card>
        <div slot="header" class="model-title">Try Through Gateway</div>
        <div class="validation-stack">
          <div class="meta-line">
            ${
              this.gatewayEnabled
                ? html`
                    Send a real request through Preloop using
                    <code>${this.gatewayModelAlias}</code>.
                  `
                : 'This model is not currently configured for the Preloop gateway.'
            }
          </div>
          ${
            gatewayConfig?.url
              ? html`
                  <div class="meta-line">
                    Gateway URL: <code>${gatewayConfig.url}</code>
                  </div>
                `
              : ''
          }
          ${
            this.gatewayEnabled
              ? html`
                  <sl-textarea
                    label="Prompt"
                    rows="4"
                    value=${this.validationPrompt}
                    @sl-input=${(event: Event) => {
                      this.validationPrompt = (
                        event.target as HTMLTextAreaElement & { value: string }
                      ).value;
                    }}
                  ></sl-textarea>
                  <div class="validation-toolbar">
                    <sl-button
                      variant="primary"
                      ?loading=${this.validationInFlight}
                      @click=${this.runValidationPrompt}
                    >
                      Send request
                    </sl-button>
                  </div>
                `
              : html`
                  <div class="meta-line">
                    ${
                      this.model?.has_api_key
                        ? html`
                            <sl-button
                              variant="primary"
                              ?loading=${this.gatewayEnableInFlight}
                              @click=${this.enableGatewayRouting}
                            >
                              Enable Preloop gateway routing
                            </sl-button>
                          `
                        : html`
                            Add upstream API credentials
                            <sl-button
                              variant="text"
                              size="small"
                              @click=${this.openEditModal}
                            >
                              (edit this model)
                            </sl-button>
                            before enabling gateway routing.
                          `
                    }
                  </div>
                `
          }
          ${
            this.validationError
              ? html`
                  <sl-alert variant="danger" open>
                    <sl-icon slot="icon" name="exclamation-octagon"></sl-icon>
                    ${this.validationError}
                  </sl-alert>
                `
              : null
          }
          ${
            this.validationResponse
              ? html`
                  <div class="validation-output">
                    ${this.validationResponse}
                  </div>
                `
              : null
          }
        </div>
      </sl-card>
    `;
  }

  render() {
    const headerText = this.model?.name || 'AI Model';

    return html`
      <view-header headerText=${headerText} width="extra-wide">
        <div slot="top" style="margin-bottom: var(--sl-spacing-small);">
          <sl-button
            variant="text"
            size="small"
            href="/console/ai-models"
            style="margin-left: -12px;"
          >
            <sl-icon slot="prefix" name="arrow-left"></sl-icon>
            Back to AI Models
          </sl-button>
        </div>
        <div slot="main-column" class="header-actions">
          <resource-actions
            .collapseOverflow=${false}
            .actions=${[
              {
                id: 'edit',
                label: 'Edit',
                icon: 'pencil',
                onClick: this.openEditModal,
              },
              {
                id: 'delete',
                label: 'Delete',
                icon: 'trash',
                variant: 'danger',
                onClick: this.openDeleteConfirm,
              },
            ]}
          ></resource-actions>
        </div>
      </view-header>
      <div class="dashboard extra-wide">
        <div class="main-column">
          <div class="page">
            <sl-card>
              <div slot="header" class="model-heading">
                <div class="model-title">Model Observability</div>
                <div class="badge-row">
                  ${
                    this.model?.provider_name
                      ? html`
                          <sl-badge variant="neutral">
                            ${this.model.provider_name}
                          </sl-badge>
                        `
                      : ''
                  }
                  ${
                    this.model?.is_default
                      ? html`<sl-badge variant="success">Default</sl-badge>`
                      : ''
                  }
                  ${
                    this.model?.model_kind
                      ? html`
                          <sl-badge variant="neutral">
                            ${
                              this.model.model_kind === 'stt'
                                ? 'Speech to text'
                                : this.model.model_kind === 'tts'
                                  ? 'Text to speech'
                                  : 'Inference'
                            }
                          </sl-badge>
                        `
                      : ''
                  }
                </div>
              </div>
              ${
                this.model
                  ? html`
                      <div class="metadata-stack">
                        <div class="model-metadata">
                          <span><strong>Name:</strong> ${this.model.name}</span>
                          <span>
                            <strong>Identifier:</strong>
                            <code>${this.model.model_identifier}</code>
                          </span>
                          <span>
                            <strong>Updated:</strong>
                            ${this.formatDateTime(this.model.updated_at)}
                          </span>
                        </div>
                        <div class="model-metadata">
                          <span>
                            <strong>Gateway:</strong>
                            ${
                              this.model.model_kind === 'llm'
                                ? this.gatewayEnabled
                                  ? 'Enabled'
                                  : 'Disabled'
                                : 'Not used for audio fallback'
                            }
                          </span>
                          ${
                            this.gatewayModelAlias
                              ? html`
                                  <span>
                                    <strong>Gateway alias:</strong>
                                    <code>${this.gatewayModelAlias}</code>
                                  </span>
                                `
                              : ''
                          }
                          <span>
                            <strong>Upstream credentials:</strong>
                            ${this.model.has_api_key ? 'Configured' : 'Missing'}
                          </span>
                          ${
                            this.managedAgentDisplayName
                              ? html`
                                  <span>
                                    <strong>Managed agent:</strong>
                                    ${
                                      this.managedAgentId
                                        ? html`
                                            <a
                                              class="session-link"
                                              href=${`/console/agents/${encodeURIComponent(this.managedAgentId)}`}
                                            >
                                              ${this.managedAgentDisplayName}
                                            </a>
                                          `
                                        : this.managedAgentDisplayName
                                    }
                                  </span>
                                `
                              : ''
                          }
                          ${
                            this.managedAgentRuntimePrincipalId
                              ? html`
                                  <span>
                                    <strong>Runtime principal:</strong>
                                    <code
                                      >${this.managedAgentRuntimePrincipalId}</code
                                    >
                                  </span>
                                `
                              : ''
                          }
                        </div>
                      </div>
                    `
                  : html`
                      <div class="meta-line">
                        Loading model metadata and observability surfaces.
                      </div>
                    `
              }
            </sl-card>

            <sl-card>
              <div slot="header" class="model-title">Budget Management</div>
              <budget-policy-editor
                subjectType="ai_model"
                .subjectId=${this.modelId}
              ></budget-policy-editor>
            </sl-card>

            ${this.renderGatewayValidation()}

            <sl-card>
              <div slot="header" class="model-title">Filters</div>
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
                  label="Captured interaction search"
                  placeholder="Search prompts, outputs, or metadata"
                  .value=${this.interactionQuery}
                  @sl-input=${this.handleInteractionQueryChange}
                ></sl-input>
                <div class="filters-actions">
                  <sl-button variant="primary" @click=${this.applyFilters}>
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
                        Showing model-scoped activity from
                        ${this.formatDateTime(this.summary.period_start)} to
                        ${this.formatDateTime(this.summary.period_end)}.
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
                        <div>Loading AI model observability...</div>
                      </div>
                    </sl-card>
                  `
                : html`
                    <sl-card>
                      <div slot="header" class="model-title">Usage Summary</div>
                      ${this.renderSummarySection()}
                    </sl-card>

                    <sl-card>
                      <div slot="header" class="model-title">
                        Session Observer
                      </div>
                      <div class="meta-line" style="margin-bottom: 0.75rem;">
                        Recent sessions, replay, cost breakdown, and
                        optimization suggestions scoped to this model.
                      </div>
                      <preloop-session-observer
                        scope="ai_model"
                        .scopeId=${this.modelId}
                        .sessions=${this.sessions?.items || []}
                        layout="embedded"
                        defaultReplayMode="timeline"
                        .features=${{
                          summaries: true,
                          auditLinks: true,
                        }}
                      ></preloop-session-observer>
                    </sl-card>
                  `
            }
          </div>
        </div>
      </div>
      <add-ai-model-modal
        ?open=${this.isEditModalOpen}
        .model=${this.model}
        @model-updated=${this.handleModelUpdated}
        @close-modal=${this.closeEditModal}
      ></add-ai-model-modal>
      <sl-dialog
        label="Delete Model"
        .open=${this.isDeleteConfirmOpen}
        @sl-hide=${() => (this.isDeleteConfirmOpen = false)}
      >
        Are you sure you want to delete the model "${this.model?.name}"?
        <sl-button
          slot="footer"
          @click=${() => (this.isDeleteConfirmOpen = false)}
          >Cancel</sl-button
        >
        <sl-button slot="footer" variant="danger" @click=${this.confirmDelete}
          >Delete</sl-button
        >
      </sl-dialog>
    `;
  }
}
