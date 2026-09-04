import { LitElement, html, css, unsafeCSS } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import { when } from 'lit/directives/when.js';
import { repeat } from 'lit/directives/repeat.js';
import {
  getAIModels,
  getAIModelsOverview,
  updateAIModel,
  deleteAIModel,
} from '../../../api';
import type { AIModel, AIModelOverviewItem } from '../../../types';

import '@shoelace-style/shoelace/dist/components/dialog/dialog.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/card/card.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/option/option.js';
import '@shoelace-style/shoelace/dist/components/select/select.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '../../../components/add-ai-model-modal';
import '../../../components/list-toolbar';
import { unifiedWebSocketManager } from '../../../services/unified-websocket-manager';
import { formatRelativeTime } from '../../../utils/date';
import {
  effectiveViewMode,
  loadViewMode,
  saveViewMode,
  subscribeNarrowViewport,
  type ListViewMode,
  type NarrowViewportSubscription,
} from '../../../utils/view-mode';
import consoleStyles from '../../../styles/console-styles.css?inline';
import { consoleDialogStyles } from '../../../styles/console-dialog';

const VIEW_MODE_KEY = 'preloop.models.view_mode';

export function isGatewayEnabled(model: AIModel): boolean {
  const gateway = model.meta_data?.gateway;
  if (!gateway || typeof gateway !== 'object') {
    return true;
  }
  return (gateway as { enabled?: boolean }).enabled !== false;
}

export function filterModels(
  models: AIModel[],
  search: string,
  provider: string,
  status: string
): AIModel[] {
  const query = search.trim().toLowerCase();
  return models.filter((model) => {
    if (provider && model.provider_name !== provider) {
      return false;
    }
    if (status === 'enabled' && !isGatewayEnabled(model)) {
      return false;
    }
    if (status === 'disabled' && isGatewayEnabled(model)) {
      return false;
    }
    if (!query) {
      return true;
    }
    const haystack = [model.name, model.provider_name].join(' ').toLowerCase();
    return haystack.includes(query);
  });
}

@customElement('ai-models-view')
export class AIModelsView extends LitElement {
  private static readonly FLEET_WINDOW_DAYS = 30;

  private readonly INFO_ALERT_DISMISSED_KEY =
    'preloop-models-info-alert-dismissed';

  @state()
  private _isInfoAlertOpen = false;

  @state()
  private models: AIModel[] = [];

  @state()
  private isLoading = true;

  @state()
  private error: string | null = null;

  @state()
  private isModalOpen = false;

  @state()
  private editingModel: AIModel | null = null;

  @state()
  private isDeleteConfirmOpen = false;

  @state()
  private modelToDelete: AIModel | null = null;

  @state()
  private modelOverview = new Map<string, AIModelOverviewItem>();

  @state()
  private search = '';

  @state()
  private providerFilter = '';

  @state()
  private statusFilter = '';

  @state()
  private currentView: ListViewMode = loadViewMode(VIEW_MODE_KEY);

  @state()
  private narrowViewport = false;

  private unsubscribeRealtime?: () => void;
  private refreshTimer: number | null = null;
  private refreshInFlight = false;
  private narrowViewportSubscription: NarrowViewportSubscription | null = null;

  static styles = [
    consoleDialogStyles,
    unsafeCSS(consoleStyles),
    css`
      table {
        width: 100%;
        border-collapse: collapse;
      }
      .page {
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-large);
      }
      .toolbar-wrap {
        width: 100%;
      }
      .summary-grid {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
        gap: var(--sl-spacing-medium);
      }
      .summary-card::part(base),
      .table-card::part(base) {
        height: 100%;
      }
      .metric-label {
        color: var(--sl-color-neutral-600);
        font-size: var(--sl-font-size-small);
      }
      .metric-value {
        color: var(--sl-color-neutral-900);
        font-size: 1.6rem;
        font-weight: 700;
        line-height: 1.1;
        margin-top: var(--sl-spacing-2x-small);
      }
      .metric-subtext {
        color: var(--sl-color-neutral-600);
        font-size: var(--sl-font-size-small);
        margin-top: var(--sl-spacing-small);
      }
      .styled-table th,
      .styled-table td {
        padding: var(--sl-spacing-medium);
        text-align: left;
        border-bottom: 1px solid var(--console-hairline);
      }
      .styled-table th {
        background-color: transparent;
        font-weight: var(--sl-font-weight-semibold);
      }
      .styled-table td {
        vertical-align: top;
      }
      .styled-table tr:last-child td {
        border-bottom: none;
      }
      .actions {
        display: flex;
        gap: var(--sl-spacing-x-small);
        justify-content: flex-end;
      }
      .empty-state a {
        color: var(--sl-color-primary-600);
        text-decoration: none;
        cursor: pointer;
      }
      .empty-state-wrapper {
        display: flex;
        justify-content: center;
        width: 100%;
        margin-top: var(--sl-spacing-large);
      }
      .empty-card {
        width: 100%;
        max-width: 580px;
      }
      .empty-card::part(base) {
        border: 1px solid
          color-mix(in srgb, var(--sl-color-primary-600) 35%, transparent);
        box-shadow: var(--sl-shadow-large);
        border-radius: var(--sl-border-radius-large);
        overflow: hidden;
      }
      .empty-card-body {
        display: flex;
        flex-direction: column;
        align-items: center;
        text-align: center;
        padding: var(--sl-spacing-large);
      }
      .empty-icon-circle {
        width: 72px;
        height: 72px;
        border-radius: 50%;
        background: color-mix(
          in srgb,
          var(--sl-color-primary-600) 15%,
          transparent
        );
        color: var(--sl-color-primary-600);
        display: flex;
        align-items: center;
        justify-content: center;
        margin-bottom: var(--sl-spacing-medium);
      }
      .empty-icon-circle sl-icon {
        font-size: 2.5rem;
      }
      .empty-card-title {
        margin: 0 0 var(--sl-spacing-2x-small);
        font-size: 1.25rem;
        font-weight: 700;
        color: var(--sl-color-neutral-900);
      }
      .empty-card-desc {
        margin: 0 0 var(--sl-spacing-large);
        max-width: 440px;
        font-size: 0.95rem;
        line-height: 1.55;
        color: var(--sl-color-neutral-600);
      }
      .empty-cta-btn {
        width: 100%;
        max-width: 280px;
      }
      .model-link {
        color: var(--sl-color-primary-700);
        text-decoration: none;
        font-weight: var(--sl-font-weight-semibold);
      }
      .model-link:hover {
        text-decoration: underline;
      }
      .empty-state a:hover {
        text-decoration: underline;
      }
      .info-header {
        margin-bottom: var(--sl-spacing-large);
      }
      .model-meta {
        color: var(--sl-color-neutral-600);
        font-size: var(--sl-font-size-small);
        margin-top: var(--sl-spacing-2x-small);
        overflow-wrap: anywhere;
      }
      .badge-row {
        display: flex;
        flex-wrap: wrap;
        gap: var(--sl-spacing-2x-small);
      }
      .cell-stack {
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-2x-small);
      }
      .cell-primary {
        color: var(--sl-color-neutral-900);
        font-weight: var(--sl-font-weight-semibold);
      }
      .cell-secondary {
        color: var(--sl-color-neutral-600);
        font-size: var(--sl-font-size-small);
      }
      .filter-empty {
        color: var(--console-meta-color);
        font-size: var(--console-text-body);
        padding: var(--sl-spacing-large) 0;
      }
      .models-grid {
        display: grid;
        grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
        gap: var(--sl-spacing-large);
      }
      .model-card-body {
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-small);
      }
      .model-card-header {
        display: flex;
        align-items: flex-start;
        justify-content: space-between;
        gap: var(--sl-spacing-small);
      }
      .model-card-actions {
        display: flex;
        flex-wrap: wrap;
        gap: var(--sl-spacing-x-small);
        justify-content: flex-end;
      }
    `,
  ];

  async connectedCallback() {
    super.connectedCallback();
    const isDismissed = localStorage.getItem(this.INFO_ALERT_DISMISSED_KEY);
    this._isInfoAlertOpen = isDismissed !== 'true';
    this.narrowViewportSubscription = subscribeNarrowViewport((narrow) => {
      this.narrowViewport = narrow;
    });
    this.narrowViewport = this.narrowViewportSubscription.matches;
    void this.fetchModels();
    this.connectRealtime();
  }

  disconnectedCallback(): void {
    super.disconnectedCallback();
    this.narrowViewportSubscription?.disconnect();
    this.narrowViewportSubscription = null;
    this.unsubscribeRealtime?.();
    if (this.refreshTimer !== null) {
      window.clearTimeout(this.refreshTimer);
      this.refreshTimer = null;
    }
  }

  private connectRealtime(): void {
    const scheduleRefresh = () => this.scheduleRefresh();
    const unsubscribers = [
      unifiedWebSocketManager.subscribe('gateway_activity', scheduleRefresh),
      unifiedWebSocketManager.subscribe('budget_health', scheduleRefresh),
      unifiedWebSocketManager.subscribe('runtime_sessions', scheduleRefresh),
      unifiedWebSocketManager.subscribe('managed_agents', scheduleRefresh),
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

  private scheduleRefresh(): void {
    if (this.refreshTimer !== null) {
      window.clearTimeout(this.refreshTimer);
    }
    this.refreshTimer = window.setTimeout(() => {
      this.refreshTimer = null;
      void this.fetchModels({ preserveLoadingState: true });
    }, 250);
  }

  async fetchModels(options: { preserveLoadingState?: boolean } = {}) {
    if (this.refreshInFlight) {
      return;
    }
    this.refreshInFlight = true;
    if (!options.preserveLoadingState) {
      this.isLoading = true;
    }
    this.error = null;
    try {
      // One request for the page, whatever the fleet size. The per-model
      // endpoints stay for the detail view: a burst of them is what emptied
      // the API connection pool on 2026-09-03.
      const [models, overview] = await Promise.all([
        getAIModels(),
        getAIModelsOverview(this.getOverviewParams()),
      ]);
      this.models = models;
      this.modelOverview = new Map(
        overview.models.map((item) => [item.ai_model_id, item])
      );
    } catch (error) {
      this.error =
        error instanceof Error ? error.message : 'Failed to fetch AI models';
      this.modelOverview = new Map();
    } finally {
      this.isLoading = false;
      this.refreshInFlight = false;
    }
  }

  private getOverviewParams() {
    const endDate = new Date();
    const startDate = new Date(endDate);
    startDate.setDate(
      startDate.getDate() - (AIModelsView.FLEET_WINDOW_DAYS - 1)
    );
    return {
      startDate: startDate.toISOString(),
      endDate: endDate.toISOString(),
    };
  }

  private get overviewWindowLabel(): string {
    return `Last ${AIModelsView.FLEET_WINDOW_DAYS} days`;
  }

  private get fleetRequestCount(): number {
    return [...this.modelOverview.values()].reduce(
      (total, item) => total + item.total_requests,
      0
    );
  }

  private get fleetSpend(): number {
    return [...this.modelOverview.values()].reduce(
      (total, item) => total + item.estimated_cost,
      0
    );
  }

  private get activeFleetSessions(): number {
    return [...this.modelOverview.values()].reduce(
      (total, item) => total + item.active_session_count,
      0
    );
  }

  private get activeModelsCount(): number {
    return [...this.modelOverview.values()].filter(
      (item) => item.total_requests > 0
    ).length;
  }

  private get modelsNeedingAttentionCount(): number {
    return [...this.modelOverview.values()].filter(
      (item) => item.failed_requests > 0
    ).length;
  }

  private formatCurrency(value: number | null | undefined): string {
    return `$${(value || 0).toFixed(2)}`;
  }

  private formatNumber(value: number | null | undefined): string {
    return Intl.NumberFormat().format(value || 0);
  }

  private getModelOverview(modelId: string) {
    return this.modelOverview.get(modelId);
  }

  private getHealthVariant(modelId: string): 'success' | 'warning' | 'neutral' {
    const overview = this.getModelOverview(modelId);
    if (!overview || overview.total_requests === 0) {
      return 'neutral';
    }
    if (overview.failed_requests > 0) {
      return 'warning';
    }
    return 'success';
  }

  private getHealthLabel(modelId: string): string {
    const overview = this.getModelOverview(modelId);
    if (!overview || overview.total_requests === 0) {
      return 'Idle';
    }
    if (overview.failed_requests > 0) {
      return 'Attention';
    }
    return 'Healthy';
  }

  private getPricingSourceLabel(modelId: string): string {
    switch (this.getModelOverview(modelId)?.pricing_source) {
      case 'override':
        return 'Priced by account override';
      case 'model_config':
        return 'Priced by model config';
      case 'catalog':
        return 'Priced from catalog';
      default:
        return 'No price set';
    }
  }

  private getLastRequestLabel(modelId: string): string | null {
    const lastRequestAt = this.getModelOverview(modelId)?.last_request_at;
    return lastRequestAt
      ? `Last request ${formatRelativeTime(lastRequestAt)}`
      : null;
  }

  private getGatewayAlias(model: AIModel): string | null {
    const gateway = model.meta_data?.gateway;
    if (!gateway || typeof gateway !== 'object') {
      return null;
    }
    const alias = (gateway as { model_alias?: string }).model_alias;
    return typeof alias === 'string' && alias.trim() ? alias.trim() : null;
  }

  private getManagedAgentDisplayName(model: AIModel): string | null {
    const value = model.meta_data?.managed_agent_display_name;
    return typeof value === 'string' && value.trim() ? value.trim() : null;
  }

  private getModelKindLabel(model: AIModel): string {
    switch (model.model_kind || 'llm') {
      case 'stt':
        return 'Speech to text';
      case 'tts':
        return 'Text to speech';
      default:
        return 'Inference';
    }
  }

  private get visibleModels(): AIModel[] {
    return filterModels(
      this.models,
      this.search,
      this.providerFilter,
      this.statusFilter
    );
  }

  private get effectiveView(): ListViewMode {
    return effectiveViewMode(this.currentView, this.narrowViewport);
  }

  private get providerOptions(): string[] {
    return [...new Set(this.models.map((model) => model.provider_name))]
      .filter(Boolean)
      .sort((a, b) => a.localeCompare(b));
  }

  private get resultsLabel(): string {
    const shown = this.visibleModels.length;
    const total = this.models.length;
    const noun = total === 1 ? 'model' : 'models';
    if (shown === total) {
      return `${shown} ${noun}`;
    }
    return `${shown} of ${total} ${noun}`;
  }

  private handleSearchChange(event: CustomEvent<{ value: string }>) {
    this.search = event.detail.value;
  }

  private handleViewChange(event: CustomEvent<{ value: ListViewMode }>) {
    this.currentView = event.detail.value;
    saveViewMode(VIEW_MODE_KEY, event.detail.value);
  }

  private handleProviderChange(event: Event) {
    const select = event.target as HTMLElement & { value: string | string[] };
    const value = Array.isArray(select.value) ? select.value[0] : select.value;
    this.providerFilter = value || '';
  }

  private handleStatusChange(event: Event) {
    const select = event.target as HTMLElement & { value: string | string[] };
    const value = Array.isArray(select.value) ? select.value[0] : select.value;
    this.statusFilter = value || '';
  }

  private renderFleetOverview() {
    return html`
      <div class="summary-grid">
        <sl-card class="summary-card">
          <div class="metric-label">Configured models</div>
          <div class="metric-value">
            ${this.formatNumber(this.models.length)}
          </div>
          <div class="metric-subtext">${this.overviewWindowLabel}</div>
        </sl-card>
        <sl-card class="summary-card">
          <div class="metric-label">Models with traffic</div>
          <div class="metric-value">
            ${this.formatNumber(this.activeModelsCount)}
          </div>
          <div class="metric-subtext">
            ${this.formatNumber(this.modelsNeedingAttentionCount)} need
            attention
          </div>
        </sl-card>
        <sl-card class="summary-card">
          <div class="metric-label">Fleet requests</div>
          <div class="metric-value">
            ${this.formatNumber(this.fleetRequestCount)}
          </div>
          <div class="metric-subtext">
            ${this.formatNumber(this.activeFleetSessions)} active sessions
          </div>
        </sl-card>
        <sl-card class="summary-card">
          <div class="metric-label">Fleet spend</div>
          <div class="metric-value">
            ${this.formatCurrency(this.fleetSpend)}
          </div>
          <div class="metric-subtext">${this.overviewWindowLabel}</div>
        </sl-card>
      </div>
    `;
  }

  render() {
    const renderContent = () => {
      if (this.isLoading) {
        return html`<sl-card
          ><div style="display: flex; justify-content: center; padding: 2rem;">
            <sl-spinner></sl-spinner></div
        ></sl-card>`;
      }

      if (this.error) {
        return html`
          <sl-alert variant="danger" open>
            <sl-icon slot="icon" name="exclamation-octagon"></sl-icon>
            <strong>Error:</strong> ${this.error}
          </sl-alert>
        `;
      }

      return this.renderModelsList();
    };

    return html`
      <view-header
        headerText="AI Models"
        description="The AI models your agents reach through the gateway. Each model gets a gateway alias; every call through it is metered and attributed to an agent and session."
        width="narrow"
      >
        ${
          this.models.length > 0
            ? html`
                <div slot="main-column">
                  <sl-button variant="primary" @click=${this.openAddModelModal}>
                    <sl-icon slot="prefix" name="plus-lg"></sl-icon> Add Model
                  </sl-button>
                </div>
              `
            : ''
        }
      </view-header>
      <div class="column-layout narrow">
        <div class="main-column">
          <div class="page">
            ${
              this.isLoading || this.error || this.models.length === 0
                ? null
                : this.renderFleetOverview()
            }
            ${this.models.length > 0 ? this.renderToolbar() : null}
            ${renderContent()}
          </div>
        </div>
        <div class="side-column"></div>
      </div>
      <add-ai-model-modal
        ?open=${this.isModalOpen}
        .model=${this.editingModel}
        @model-created=${this._handleModelSaved}
        @model-updated=${this._handleModelSaved}
        @close-modal=${this.closeModal}
      ></add-ai-model-modal>
      ${this.renderDeleteConfirm()}
    `;
  }
  private renderToolbar() {
    return html`
      <div class="toolbar-wrap">
        <list-toolbar
          .search=${this.search}
          searchPlaceholder="Search models"
          .view=${this.currentView}
          .views=${['list', 'cards']}
          @search-change=${this.handleSearchChange}
          @view-change=${this.handleViewChange}
        >
          <sl-select
            class="provider-filter"
            clearable
            placeholder="All providers"
            .value=${this.providerFilter}
            @sl-change=${this.handleProviderChange}
          >
            ${this.providerOptions.map(
              (provider) =>
                html`<sl-option value=${provider}>${provider}</sl-option>`
            )}
          </sl-select>
          <sl-select
            class="status-filter"
            clearable
            placeholder="Any status"
            .value=${this.statusFilter}
            @sl-change=${this.handleStatusChange}
          >
            <sl-option value="enabled">Enabled</sl-option>
            <sl-option value="disabled">Disabled</sl-option>
          </sl-select>
          <span slot="count">${this.resultsLabel}</span>
        </list-toolbar>
      </div>
    `;
  }

  renderModelsList() {
    return html`
      ${when(
        this.models.length === 0,
        () => html`
          <div class="empty-state-wrapper">
            <sl-card class="empty-card">
              <div class="empty-card-body">
                <div class="empty-icon-circle">
                  <sl-icon name="cpu"></sl-icon>
                </div>
                <h3 class="empty-card-title">No AI models configured</h3>
                <p class="empty-card-desc">
                  The AI models your agents reach through the gateway. Add your
                  OpenAI, Anthropic, Gemini, or custom model endpoints.
                </p>
                <sl-button
                  class="empty-cta-btn"
                  variant="primary"
                  @click=${this.openAddModelModal}
                >
                  <sl-icon slot="prefix" name="plus-lg"></sl-icon>
                  Add Model
                </sl-button>
              </div>
            </sl-card>
          </div>
        `,
        () => this.renderFilteredModels()
      )}
    `;
  }

  private renderFilteredModels() {
    const models = this.visibleModels;
    if (models.length === 0) {
      return html`<div class="filter-empty">
        No models match these filters.
      </div>`;
    }
    return this.effectiveView === 'cards'
      ? this.renderCardsView(models)
      : this.renderListView(models);
  }

  private renderDefaultControl(model: AIModel) {
    return when(
      model.is_default,
      () => html`<sl-badge variant="success" pill>Default</sl-badge>`,
      () => html`
        <sl-button size="small" @click=${() => this.handleSetDefault(model)}>
          Set as default
        </sl-button>
      `
    );
  }

  private renderModelActions(model: AIModel) {
    return html`
      <div class="actions">
        <sl-button size="small" href=${`/console/ai-models/${model.id}`}>
          View
        </sl-button>
        <sl-button
          size="small"
          circle
          @click=${() => this.openEditModal(model)}
        >
          <sl-icon name="pencil"></sl-icon>
        </sl-button>
        <sl-button
          variant="danger"
          size="small"
          circle
          @click=${() => this.openDeleteConfirm(model)}
        >
          <sl-icon name="trash"></sl-icon>
        </sl-button>
      </div>
    `;
  }

  private renderListView(models: AIModel[]) {
    return html`
      <sl-card class="table-card">
        <table class="styled-table">
          <thead>
            <tr>
              <th>Name</th>
              <th>Provider</th>
              <th>Fleet health</th>
              <th>Usage</th>
              <th>Default</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            ${repeat(
              models,
              (model) => model.id,
              (model) => this.renderModelRow(model)
            )}
          </tbody>
        </table>
      </sl-card>
    `;
  }

  private renderModelRow(model: AIModel) {
    return html`
      <tr class="model-row" data-model-id=${model.id}>
        <td>
          <a class="model-link" href=${`/console/ai-models/${model.id}`}>
            ${model.name}
          </a>
          <div class="model-meta">${model.model_identifier}</div>
          ${
            this.getGatewayAlias(model)
              ? html`
                  <div class="model-meta">
                    Gateway alias:
                    <code>${this.getGatewayAlias(model)}</code>
                  </div>
                `
              : null
          }
          ${
            this.getManagedAgentDisplayName(model)
              ? html`
                  <div class="model-meta">
                    Managed agent: ${this.getManagedAgentDisplayName(model)}
                  </div>
                `
              : null
          }
        </td>
        <td>
          <div class="cell-stack">
            <div class="cell-primary">${model.provider_name}</div>
            <div class="cell-secondary">${this.getModelKindLabel(model)}</div>
            <div class="cell-secondary">
              ${this.getPricingSourceLabel(model.id)}
            </div>
          </div>
        </td>
        <td>
          <div class="cell-stack">
            <div class="badge-row">
              <sl-badge variant=${this.getHealthVariant(model.id)} pill>
                ${this.getHealthLabel(model.id)}
              </sl-badge>
              ${
                this.getModelOverview(model.id)?.active_session_count
                  ? html`
                      <sl-badge variant="primary" pill>
                        ${this.formatNumber(
                          this.getModelOverview(model.id)?.active_session_count
                        )}
                        active sessions
                      </sl-badge>
                    `
                  : null
              }
            </div>
            <div class="cell-secondary">
              ${this.formatNumber(
                this.getModelOverview(model.id)?.successful_requests
              )}
              successful ·
              ${this.formatNumber(
                this.getModelOverview(model.id)?.failed_requests
              )}
              failed
            </div>
          </div>
        </td>
        <td>
          <div class="cell-stack">
            <div class="cell-primary">
              ${this.formatNumber(
                this.getModelOverview(model.id)?.total_requests
              )}
              requests
            </div>
            <div class="cell-secondary">
              ${this.formatNumber(
                this.getModelOverview(model.id)?.token_usage.total_tokens
              )}
              tokens ·
              ${this.formatCurrency(
                this.getModelOverview(model.id)?.estimated_cost
              )}
            </div>
            ${
              this.getLastRequestLabel(model.id)
                ? html`<div class="cell-secondary">
                    ${this.getLastRequestLabel(model.id)}
                  </div>`
                : null
            }
          </div>
        </td>
        <td>${this.renderDefaultControl(model)}</td>
        <td>${this.renderModelActions(model)}</td>
      </tr>
    `;
  }

  private renderCardsView(models: AIModel[]) {
    return html`
      <div class="models-grid">
        ${repeat(
          models,
          (model) => model.id,
          (model) => this.renderModelCard(model)
        )}
      </div>
    `;
  }

  private renderModelCard(model: AIModel) {
    return html`
      <sl-card class="model-card" data-model-id=${model.id}>
        <div class="model-card-body">
          <div class="model-card-header">
            <a class="model-link" href=${`/console/ai-models/${model.id}`}>
              ${model.name}
            </a>
            ${this.renderDefaultControl(model)}
          </div>
          <div class="cell-primary">${model.provider_name}</div>
          <div class="cell-secondary">${this.getModelKindLabel(model)}</div>
          <div class="cell-secondary">
            ${this.getPricingSourceLabel(model.id)}
          </div>
          <div class="badge-row">
            <sl-badge variant=${this.getHealthVariant(model.id)} pill>
              ${this.getHealthLabel(model.id)}
            </sl-badge>
            ${
              isGatewayEnabled(model)
                ? html`<sl-badge variant="neutral" pill>Enabled</sl-badge>`
                : html`<sl-badge variant="neutral" pill>Disabled</sl-badge>`
            }
          </div>
          <div class="cell-secondary">
            ${this.formatNumber(this.getModelOverview(model.id)?.total_requests)}
            requests ·
            ${this.formatCurrency(
              this.getModelOverview(model.id)?.estimated_cost
            )}
          </div>
          <div class="model-card-actions">
            ${this.renderModelActions(model)}
          </div>
        </div>
      </sl-card>
    `;
  }

  renderDeleteConfirm() {
    return html`
      <sl-dialog
        label="Delete Model"
        .open=${this.isDeleteConfirmOpen}
        @sl-hide=${() => (this.isDeleteConfirmOpen = false)}
      >
        Are you sure you want to delete the model "${this.modelToDelete?.name}"?
        <sl-button
          slot="footer"
          @click=${() => (this.isDeleteConfirmOpen = false)}
          >Cancel</sl-button
        >
        <sl-button slot="footer" variant="danger" @click=${this.deleteModel}
          >Delete</sl-button
        >
      </sl-dialog>
    `;
  }

  openAddModelModal() {
    this.editingModel = null;
    this.isModalOpen = true;
  }

  openEditModal(model: AIModel) {
    this.editingModel = model;
    this.isModalOpen = true;
  }

  closeModal() {
    this.isModalOpen = false;
    this.editingModel = null;
  }

  private async _handleModelSaved() {
    this.closeModal();
    await this.fetchModels();
  }

  openDeleteConfirm(model: AIModel) {
    this.modelToDelete = model;
    this.isDeleteConfirmOpen = true;
  }

  async handleSetDefault(model: AIModel) {
    try {
      await updateAIModel(model.id, { is_default: true });
      await this.fetchModels();
    } catch (error) {
      console.error('Failed to set default model:', error);
      this.error =
        error instanceof Error ? error.message : 'Failed to set default model';
    }
  }

  async deleteModel() {
    if (this.modelToDelete) {
      try {
        await deleteAIModel(this.modelToDelete.id);
        await this.fetchModels();
      } catch (error) {
        console.error('Failed to delete model:', error);
        this.error =
          error instanceof Error ? error.message : 'Failed to delete model';
      }
    }
    this.isDeleteConfirmOpen = false;
    this.modelToDelete = null;
  }

  private handleInfoAlertHide() {
    localStorage.setItem(this.INFO_ALERT_DISMISSED_KEY, 'true');
    this._isInfoAlertOpen = false;
  }
}
