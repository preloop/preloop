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
import '../../../components/time-range-select.ts';
import '../../../components/resource-actions.ts';
import '../../../components/budget-policy-editor.ts';
import '../../../components/preloop-session-observer.ts';
import '../../../components/add-ai-model-modal';
import {
  createModelPriceOverride,
  deleteAIModel,
  extractErrorMessage,
  fetchAIModelPricingFromProvider,
  fetchWithAuth,
  getAIModel,
  getAIModelPricing,
  getAIModelGatewayUsageSearch,
  getAIModelGatewayUsageSummary,
  getAIModelRuntimeSessions,
  getFeatures,
  repriceCost,
  updateAIModel,
  updateModelPriceOverride,
  type GatewayUsageSummaryParams,
} from '../../../api';
import type {
  AIModel,
  AIModelGatewayUsageSearchResponse,
  AIModelPriceQuote,
  AIModelPricingResponse,
  AIModelGatewayUsageSummaryResponse,
  AIModelRuntimeSessionListResponse,
  GatewayUsageByDay,
  GatewayUsageSearchResultItem,
  RuntimeSessionSummary,
} from '../../../types';
import { unifiedWebSocketManager } from '../../../services/unified-websocket-manager';
import consoleStyles from '../../../styles/console-styles.css?inline';
import {
  formatTimeRangeWindow,
  resolveTimeRange,
  type TimeRangeKey,
} from '../../../utils/time-range';
import { consoleDialogStyles } from '../../../styles/console-dialog';

// The one range control, with the same vocabulary as the Overview, Cost and
// API usage, and the window resolved by the same shared math so "30d" means
// the same 30 days on all four pages.
const DATE_RANGE_OPTIONS: Array<{ value: TimeRangeKey; label: string }> = [
  { value: 'last-24h', label: '24h' },
  { value: 'last-7', label: '7d' },
  { value: 'last-30', label: '30d' },
  { value: 'last-365', label: '1y' },
];

type PriceField =
  'input' | 'output' | 'cachedInput' | 'request' | 'effectiveFrom';

/** How a price got its numbers, said in the words the console uses elsewhere. */
const PRICING_SOURCE_LABEL: Record<string, string> = {
  override: 'Account override',
  model_config: 'Set on this model',
  catalog: 'Provider catalog',
  none: 'No price',
};

/** Stored overrides are per 1,000 tokens; providers publish per 1,000,000. */
const PER_1K_TO_PER_1M = 1000;

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

  /**
   * A reload the operator asked for (a new range, a new search) dims the
   * answers it is about to replace instead of blanking the page; the 250 ms
   * realtime refresh does neither, because a page that dims itself twice a
   * second is unreadable.
   */
  @state()
  private updating = false;

  @state()
  private error: string | null = null;

  @state()
  private selectedRange: TimeRangeKey = 'last-30';

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
  private pricing: AIModelPricingResponse | null = null;

  @state()
  private pricingEditOpen = false;

  @state()
  private pricingSaving = false;

  @state()
  private pricingFetching = false;

  @state()
  private pricingError: string | null = null;

  @state()
  private pricingNotice: string | null = null;

  /**
   * The effective date of the price just saved, or null when nothing was
   * saved this visit. A new price only costs new requests, so the card
   * offers to recost what is already recorded rather than doing it quietly.
   */
  @state()
  private repriceSince: string | null = null;

  @state()
  private repricing = false;

  @state()
  private repriceNotice: string | null = null;

  @state()
  private repriceError: string | null = null;

  /** The form's own values, in USD per million tokens, as typed. */
  @state()
  private priceDraft: Record<PriceField, string> = {
    input: '',
    output: '',
    cachedInput: '',
    request: '',
    effectiveFrom: '',
  };

  @state()
  private priceOverridesEnabled = false;

  @state()
  private isEditModalOpen = false;

  @state()
  private isDeleteConfirmOpen = false;

  /** Set when the URL asked for the price editor before the price arrived. */
  private pendingPricingEdit = false;

  private initialized = false;
  private unsubscribeRealtime?: () => void;
  private refreshTimer: number | null = null;
  private refreshInFlight = false;
  private interactionSearchDebounce?: ReturnType<typeof setTimeout>;

  static styles = [
    consoleDialogStyles,
    unsafeCSS(consoleStyles),
    css`
      :host {
        display: block;
      }

      .page,
      .stack,
      .results,
      .daily-list,
      .session-list,
      .interaction-list {
        display: flex;
        flex-direction: column;
      }

      .page,
      .stack,
      .results {
        gap: var(--sl-spacing-large);
      }

      /* A range change never blanks answers the page already has: they stay
         readable at 60% until the new ones arrive, the way API usage and Cost
         behave. Only the very first load shows a spinner. */
      .results.is-updating {
        opacity: 0.6;
        pointer-events: none;
      }

      .price-grid {
        display: grid;
        gap: var(--sl-spacing-medium);
        grid-template-columns: repeat(4, minmax(0, 1fr));
      }

      .price-cell-label {
        color: var(--sl-color-neutral-600);
        font-size: var(--sl-font-size-small);
      }

      .price-cell-value {
        font-size: var(--sl-font-size-large);
        font-variant-numeric: tabular-nums;
        font-weight: 600;
      }

      .price-cell-value.unknown {
        color: var(--sl-color-neutral-500);
        font-weight: 400;
      }

      .price-cell-unit {
        color: var(--sl-color-neutral-500);
        font-size: var(--sl-font-size-x-small);
      }

      .price-actions {
        display: flex;
        flex-wrap: wrap;
        gap: var(--sl-spacing-small);
        margin-top: var(--sl-spacing-medium);
      }

      .price-form {
        border-top: 1px solid var(--sl-color-neutral-200);
        margin-top: var(--sl-spacing-medium);
        padding-top: var(--sl-spacing-medium);
      }

      .price-form-grid {
        display: grid;
        gap: var(--sl-spacing-small);
        grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
        margin-bottom: var(--sl-spacing-small);
      }

      .price-notice,
      .price-error {
        font-size: var(--sl-font-size-small);
        margin-top: var(--sl-spacing-small);
      }

      .price-notice {
        color: var(--sl-color-neutral-700);
      }

      .price-error {
        color: var(--sl-color-danger-700);
      }

      .reprice-offer {
        border-top: 1px solid
          var(--console-hairline, var(--sl-color-neutral-200));
        margin-top: var(--sl-spacing-medium);
        padding-top: var(--sl-spacing-medium);
      }

      .reprice-offer sl-button {
        margin-top: var(--sl-spacing-small);
      }

      .interaction-toolbar {
        display: flex;
        gap: var(--sl-spacing-medium);
        flex-wrap: wrap;
        align-items: end;
      }

      .interaction-toolbar sl-input {
        min-width: 280px;
      }

      /* One range control, the window it resolved to, and the search that
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
         it, because the sibling usage pages used to disagree about "30 days". */
      .range-window {
        color: var(--sl-color-neutral-600);
        font-size: var(--sl-font-size-small);
        font-variant-numeric: tabular-nums;
      }

      .interaction-search {
        flex: 1 1 260px;
        min-width: 220px;
        margin-left: auto;
      }

      /* Names the search field for assistive tech without a visible label. */
      .interaction-search::part(form-control-label) {
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

      /* One hairline row of facts on the card surface. Nothing inside a card
         gets a filled box of its own (DESIGN.md "Depth limit: two"); what
         separates the facts is a hairline. */
      .summary-strip {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
        gap: var(--sl-spacing-medium);
        border-bottom: 1px solid var(--console-hairline);
        padding-bottom: var(--sl-spacing-medium);
        /* Clips the rule of whichever stat starts a row: see below. */
        overflow: hidden;
      }

      /* The separator lives in the gap to the stat's left, not on its border:
         a border follows DOM order, and once this grid wraps the first stat of
         the second row would keep a rule with nothing beside it. A rule in the
         gap falls outside the grid's box for a row's first stat and is
         clipped. */
      .stat-item {
        position: relative;
      }

      .stat-item::before {
        content: '';
        position: absolute;
        top: 0;
        bottom: 0;
        left: calc(-1 * var(--sl-spacing-medium) / 2);
        border-left: 1px solid var(--console-hairline);
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
        .interaction-search {
          margin-left: 0;
          width: 100%;
        }

        .daily-row,
        .session-row {
          grid-template-columns: 1fr;
        }

        .price-grid {
          grid-template-columns: repeat(2, minmax(0, 1fr));
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
    // The attention list links here with ?pricing=edit, so "Set price" lands
    // on the form rather than on a page with a form somewhere down it.
    this.pendingPricingEdit =
      new URLSearchParams(window.location.search).get('pricing') === 'edit';

    if (!this.initialized) {
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
    if (this.interactionSearchDebounce) {
      clearTimeout(this.interactionSearchDebounce);
      this.interactionSearchDebounce = undefined;
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

  private async loadData(
    options: { preserveLoadingState?: boolean; markUpdating?: boolean } = {}
  ) {
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
    if (options.markUpdating) {
      this.updating = true;
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
      this.updating = false;
      this.refreshInFlight = false;
      return;
    }

    void this.loadPricing();

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
      this.updating = false;
      this.refreshInFlight = false;
    }
  }

  private buildSummaryParams(): GatewayUsageSummaryParams {
    const params: GatewayUsageSummaryParams = {};
    const range = resolveTimeRange(this.selectedRange);

    if (range.startDate) {
      params.startDate = range.startDate;
    }
    if (range.endDate) {
      params.endDate = range.endDate;
    }

    return params;
  }

  private getLocalDateString(date: Date): string {
    const year = date.getFullYear();
    const month = `${date.getMonth() + 1}`.padStart(2, '0');
    const day = `${date.getDate()}`.padStart(2, '0');
    return `${year}-${month}-${day}`;
  }

  private handleRangeChange(event: Event) {
    const value = (event as CustomEvent<{ value: string }>).detail
      ?.value as TimeRangeKey;
    if (!value || value === this.selectedRange) {
      return;
    }
    this.selectedRange = value;
    // The numbers on screen stay readable while the new window loads; only a
    // page that has nothing to show yet gets a spinner.
    void this.loadData({ preserveLoadingState: true, markUpdating: true });
  }

  private handleInteractionQueryChange(event: Event) {
    this.interactionQuery = (
      event.target as HTMLInputElement & { value: string }
    ).value;
    // The search runs on the server, so a keystroke is not a request: the
    // page waits for a pause in typing instead of an Apply button.
    if (this.interactionSearchDebounce) {
      clearTimeout(this.interactionSearchDebounce);
    }
    this.interactionSearchDebounce = setTimeout(() => {
      this.interactionSearchDebounce = undefined;
      void this.loadData({ preserveLoadingState: true, markUpdating: true });
    }, 300);
  }

  /**
   * Which days the numbers cover, restated under the control that chose them.
   * The server's own window wins over the client's preset.
   */
  private rangeWindowLabel(): string {
    const requested = resolveTimeRange(this.selectedRange);
    return formatTimeRangeWindow({
      startDate: this.summary?.period_start ?? requested.startDate,
      endDate: this.summary?.period_end ?? requested.endDate,
    });
  }

  private formatNumber(value: number | null | undefined): string {
    return typeof value === 'number' ? value.toLocaleString() : '0';
  }

  /**
   * The price is loaded on its own: it comes from a different endpoint than
   * the usage numbers, and a model with no price is still worth reading about.
   */
  private async loadPricing(): Promise<void> {
    if (!this.modelId) {
      return;
    }
    try {
      const [pricing, features] = await Promise.all([
        getAIModelPricing(this.modelId),
        getFeatures().catch(() => ({ features: {} })),
      ]);
      this.pricing = pricing;
      this.priceOverridesEnabled =
        (features.features || {}).model_price_overrides === true;
      if (!this.pricingEditOpen) {
        this.resetPriceDraft();
      }
      if (this.pendingPricingEdit) {
        this.pendingPricingEdit = false;
        if (this.canEditPrice) {
          this.openPriceEditor();
        }
      }
    } catch {
      // A missing price is not an error worth a banner: the card says so.
      this.pricing = null;
    }
  }

  private get canEditPrice(): boolean {
    return this.priceOverridesEnabled;
  }

  /** Fill the form from the price in force, so editing starts from today. */
  private resetPriceDraft(): void {
    const price = this.pricing?.price;
    this.priceDraft = {
      input: this.priceInputValue(price?.input_per_1m),
      output: this.priceInputValue(price?.output_per_1m),
      cachedInput: this.priceInputValue(price?.cached_input_per_1m),
      request: this.priceInputValue(price?.request_price),
      effectiveFrom: this.getLocalDateString(new Date()),
    };
  }

  private priceInputValue(value: number | null | undefined): string {
    return typeof value === 'number' ? String(value) : '';
  }

  private openPriceEditor = () => {
    this.resetPriceDraft();
    this.pricingError = null;
    this.pricingNotice = null;
    this.pricingEditOpen = true;
  };

  private closePriceEditor = () => {
    this.pricingEditOpen = false;
    this.pricingError = null;
  };

  private setPriceField(field: PriceField, value: string): void {
    this.priceDraft = { ...this.priceDraft, [field]: value };
  }

  /**
   * Read one typed price. Empty means "say nothing about this", which is not
   * the same as zero, so it comes back as null.
   */
  private parsePrice(value: string): number | null | undefined {
    const text = value.trim();
    if (!text) {
      return null;
    }
    const parsed = Number(text);
    if (!Number.isFinite(parsed) || parsed < 0) {
      return undefined;
    }
    return parsed;
  }

  /** Ask the provider what it charges. The answer fills the form, unsaved. */
  private async fetchProviderPrice(): Promise<void> {
    if (!this.modelId) {
      return;
    }
    this.pricingFetching = true;
    this.pricingError = null;
    this.pricingNotice = null;
    try {
      const quote: AIModelPriceQuote = await fetchAIModelPricingFromProvider(
        this.modelId
      );
      if (!this.pricingEditOpen) {
        this.resetPriceDraft();
        this.pricingEditOpen = true;
      }
      this.priceDraft = {
        ...this.priceDraft,
        input: this.priceInputValue(quote.price.input_per_1m),
        output: this.priceInputValue(quote.price.output_per_1m),
        cachedInput: this.priceInputValue(quote.price.cached_input_per_1m),
        request: this.priceInputValue(quote.price.request_price),
      };
      const label =
        this.pricing?.fetch_provider_label ||
        quote.provider_name ||
        'the provider';
      this.pricingNotice = `${label} lists ${quote.model_key}. Check the numbers and save to use them.`;
    } catch (error) {
      this.pricingError =
        error instanceof Error
          ? error.message
          : 'Could not read a price from the provider.';
    } finally {
      this.pricingFetching = false;
    }
  }

  /**
   * Save the form as an account price override. An override already in force
   * is updated in place; anything else creates one, so history is not rewritten
   * by accident.
   */
  private async savePrice(): Promise<void> {
    const input = this.parsePrice(this.priceDraft.input);
    const output = this.parsePrice(this.priceDraft.output);
    const cached = this.parsePrice(this.priceDraft.cachedInput);
    const request = this.parsePrice(this.priceDraft.request);
    if (
      input === undefined ||
      output === undefined ||
      cached === undefined ||
      request === undefined
    ) {
      this.pricingError = 'Prices must be zero or more.';
      return;
    }
    if (input === null && output === null && request === null) {
      this.pricingError =
        'Enter at least an input price, an output price or a price per request.';
      return;
    }
    const effectiveFrom = this.priceDraft.effectiveFrom.trim();
    if (!effectiveFrom) {
      this.pricingError = 'Pick the date this price starts.';
      return;
    }
    const effectiveDate = new Date(`${effectiveFrom}T00:00:00`);
    if (Number.isNaN(effectiveDate.getTime())) {
      this.pricingError = 'That is not a date Preloop can read.';
      return;
    }
    const modelAlias =
      this.pricing?.model_alias || this.gatewayModelAlias || this.model?.name;
    if (!modelAlias) {
      this.pricingError = 'This model has no gateway alias to price.';
      return;
    }

    const perThousand = (value: number | null): number | null =>
      value === null ? null : value / PER_1K_TO_PER_1M;

    this.pricingSaving = true;
    this.pricingError = null;
    try {
      const payload = {
        ai_model_id: this.modelId,
        provider_name: this.model?.provider_name || null,
        model_alias: modelAlias,
        currency: this.pricing?.currency || 'USD',
        input_price_per_1k: perThousand(input),
        output_price_per_1k: perThousand(output),
        cache_read_input_price_per_1k: perThousand(cached),
        cache_creation_input_price_per_1k: null,
        price_per_1k: null,
        request_price: request,
        discount_percent: null,
        prepaid_token_balance: null,
        prepaid_credit_balance_usd: null,
        effective_from: effectiveDate.toISOString(),
        effective_until: null,
        is_active: true,
        notes: null,
      };
      if (this.pricing?.override_id) {
        await updateModelPriceOverride(this.pricing.override_id, payload);
      } else {
        await createModelPriceOverride(payload);
      }
      this.pricingEditOpen = false;
      this.pricingNotice = 'Price saved. New requests are costed with it.';
      // Only offer a backfill when the new price already covers some past
      // window. A future effective_from would make start_date > end_date.
      this.repriceSince =
        effectiveDate.getTime() <= Date.now()
          ? effectiveDate.toISOString()
          : null;
      this.repriceNotice = null;
      this.repriceError = null;
      await this.loadPricing();
    } catch (error) {
      this.pricingError =
        error instanceof Error ? error.message : 'Failed to save the price.';
    } finally {
      this.pricingSaving = false;
    }
  }

  /**
   * Recost usage already recorded, from the date the saved price starts.
   *
   * `only_unpriced: false` because the point is retroactive application: a
   * row that was costed with the old price has a cost, and leaving it alone
   * would make the offer a lie.
   */
  private async applyToPastUsage(): Promise<void> {
    const since = this.repriceSince;
    if (!since || this.repricing) {
      return;
    }
    this.repricing = true;
    this.repriceError = null;
    this.repriceNotice = null;
    try {
      const result = await repriceCost({
        start_date: since,
        end_date: new Date().toISOString(),
        only_unpriced: false,
      });
      if (result.submitted_async) {
        this.repriceNotice =
          'Repricing is running in the background. Costs update as it works through the window.';
      } else {
        const updated = Number(result.rows_updated || 0).toLocaleString();
        const examined = Number(result.rows_examined || 0).toLocaleString();
        this.repriceNotice = `Repriced ${updated} of ${examined} rows since ${this.formatDate(
          since
        )}.`;
      }
    } catch (error) {
      this.repriceError =
        error instanceof Error ? error.message : 'Failed to reprice usage.';
    } finally {
      this.repricing = false;
    }
  }

  private renderRepriceOffer() {
    if (!this.repriceSince || !this.canEditPrice) {
      return '';
    }
    const since = this.formatDate(this.repriceSince);
    return html`
      <div class="reprice-offer" data-testid="reprice-offer">
        <div class="meta-line">
          Repricing recosts every gateway row since ${since} against current
          prices, not this model alone.
        </div>
        <sl-button
          size="small"
          data-testid="apply-past-usage"
          ?loading=${this.repricing}
          @click=${() => void this.applyToPastUsage()}
          >Apply to past usage since ${since}</sl-button
        >
        ${
          this.repriceNotice
            ? html`<div
                class="price-notice"
                role="status"
                data-testid="reprice-result"
              >
                ${this.repriceNotice}
              </div>`
            : ''
        }
        ${
          this.repriceError
            ? html`<div class="price-error" role="alert">
                ${this.repriceError}
              </div>`
            : ''
        }
      </div>
    `;
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

  private formatDate(value: string | null | undefined): string {
    if (!value) {
      return 'Unknown';
    }
    return new Intl.DateTimeFormat(undefined, {
      month: 'short',
      day: 'numeric',
      year: 'numeric',
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

  private renderStat(label: string, value: string, detail: string) {
    return html`
      <div class="stat-item">
        <div class="stat-label">${label}</div>
        <div class="stat-value">${value}</div>
        <div class="stat-detail">${detail}</div>
      </div>
    `;
  }

  /**
   * The window the usage numbers cover, for the card header. "Sep 5, 2026,
   * 11:59 PM" was rendered as a fourth big number labelled "Tracked Period";
   * a period is context for the other three, not a stat of its own.
   */
  private get trackedPeriodLabel(): string | null {
    if (!this.summary) {
      return null;
    }
    const start = new Date(this.summary.period_start);
    const end = new Date(this.summary.period_end);
    if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime())) {
      return null;
    }
    const days = Math.max(
      1,
      Math.round((end.getTime() - start.getTime()) / (24 * 60 * 60 * 1000))
    );
    return `${days} days · ${this.formatDate(this.summary.period_start)} to ${this.formatDate(this.summary.period_end)}`;
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
        <!-- A hairline strip, not four filled boxes inside a card
             (DESIGN.md "Depth limit: two"). The tracked period moved into the
             card header: a date-time is not a stat. -->
        <div class="summary-strip">
          ${this.renderStat(
            'Requests',
            this.formatNumber(this.summary.total_requests),
            `${this.formatNumber(this.summary.successful_requests)} succeeded, ${this.formatNumber(this.summary.failed_requests)} failed`
          )}
          ${this.renderStat(
            '$ est.',
            this.formatCost(this.summary.estimated_cost),
            `${this.formatPercent(this.summary.successful_requests, this.summary.total_requests)} success rate`
          )}
          ${this.renderStat(
            'Tokens',
            this.formatNumber(this.summary.token_usage.total_tokens),
            `${this.formatNumber(this.summary.token_usage.prompt_tokens)} prompt, ${this.formatNumber(this.summary.token_usage.completion_tokens)} completion`
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

  /**
   * What this model costs, where that number comes from, and the two ways to
   * change it. Every other page reports spend; this is the only place the
   * price behind it can be read.
   */
  private renderPricingCard() {
    const pricing = this.pricing;
    const price = pricing?.price;
    const source = pricing?.source || 'none';
    const providerLabel =
      pricing?.fetch_provider_label || this.model?.provider_name || 'provider';
    return html`
      <sl-card id="pricing">
        <div slot="header" class="model-heading">
          <div class="model-title">Pricing</div>
          <div class="badge-row">
            <sl-badge
              class="chip"
              pill
              variant=${source === 'none' ? 'warning' : 'neutral'}
            >
              ${PRICING_SOURCE_LABEL[source] || source}
            </sl-badge>
          </div>
        </div>
        ${
          pricing
            ? html`
                <div class="price-grid">
                  ${this.renderPriceCell('Input', price?.input_per_1m)}
                  ${this.renderPriceCell('Output', price?.output_per_1m)}
                  ${this.renderPriceCell(
                    'Cached input',
                    price?.cached_input_per_1m
                  )}
                  ${this.renderPriceCell(
                    'Per request',
                    price?.request_price,
                    true
                  )}
                </div>
                <div class="meta-line">${this.pricingProvenance()}</div>
                <div class="meta-line" data-testid="pricing-history-note">
                  A price applies to new requests. Usage already recorded keeps
                  the cost it was given until it is repriced.
                </div>
              `
            : html`
                <div class="meta-line">
                  Pricing is not available for this model.
                </div>
              `
        }
        ${
          this.pricingNotice
            ? html`<div class="price-notice" role="status">
                ${this.pricingNotice}
              </div>`
            : ''
        }
        ${
          this.pricingError
            ? html`<div class="price-error" role="alert">
                ${this.pricingError}
              </div>`
            : ''
        }
        ${this.pricingEditOpen ? this.renderPriceForm() : ''}
        ${this.renderRepriceOffer()}
        <div class="price-actions">
          ${
            this.canEditPrice && !this.pricingEditOpen
              ? html`<sl-button
                  size="small"
                  data-testid="edit-price"
                  @click=${this.openPriceEditor}
                  >Edit price</sl-button
                >`
              : ''
          }
          ${
            pricing?.fetch_supported
              ? html`<sl-button
                  size="small"
                  data-testid="fetch-price"
                  ?loading=${this.pricingFetching}
                  @click=${() => void this.fetchProviderPrice()}
                  >Fetch from provider</sl-button
                >`
              : html`<sl-button
                  size="small"
                  disabled
                  data-testid="fetch-price"
                  title=${`Not offered by ${providerLabel}`}
                  >Not offered by ${providerLabel}</sl-button
                >`
          }
        </div>
        ${
          this.canEditPrice
            ? ''
            : html`<div class="meta-line">
                Price overrides are part of Preloop Cloud and Enterprise. The
                price above comes from the provider catalog.
              </div>`
        }
      </sl-card>
    `;
  }

  private renderPriceCell(
    label: string,
    value: number | null | undefined,
    perRequest = false
  ) {
    const known = typeof value === 'number';
    return html`
      <div class="price-cell">
        <div class="price-cell-label">${label}</div>
        <div class="price-cell-value ${known ? '' : 'unknown'}">
          ${known ? this.formatPrice(value as number) : 'Not priced'}
        </div>
        <div class="price-cell-unit">
          ${perRequest ? 'per request' : 'per 1M tokens'}
        </div>
      </div>
    `;
  }

  /** Prices run from $0.02 to $75 per million, so two decimals is not enough. */
  private formatPrice(value: number): string {
    if (value === 0) {
      return '$0';
    }
    if (value >= 1) {
      return `$${value.toFixed(2)}`;
    }
    return `$${value.toFixed(4).replace(/0+$/, '').replace(/\.$/, '')}`;
  }

  private pricingProvenance(): string {
    const pricing = this.pricing;
    if (!pricing) {
      return '';
    }
    if (pricing.source === 'none') {
      return 'Nothing prices this model, so its requests are recorded without a cost.';
    }
    const parts: string[] = [];
    if (pricing.source === 'catalog' && pricing.catalog_key) {
      parts.push(`Catalog entry ${pricing.catalog_key}`);
    }
    if (pricing.source === 'override') {
      parts.push('Set by an account price override');
    }
    if (pricing.source === 'model_config') {
      parts.push('Set on the model itself');
    }
    if (pricing.effective_from) {
      parts.push(
        `in force since ${this.formatDateTime(pricing.effective_from)}`
      );
    }
    if (pricing.effective_until) {
      parts.push(`until ${this.formatDateTime(pricing.effective_until)}`);
    }
    return `${parts.join(', ')}.`;
  }

  private renderPriceForm() {
    return html`
      <div class="price-form" data-testid="price-form">
        <div class="price-form-grid">
          <sl-input
            label="Input per 1M tokens"
            inputmode="decimal"
            data-testid="price-input"
            value=${this.priceDraft.input}
            @sl-input=${(event: Event) =>
              this.setPriceField(
                'input',
                (event.target as HTMLInputElement).value
              )}
          ></sl-input>
          <sl-input
            label="Output per 1M tokens"
            inputmode="decimal"
            data-testid="price-output"
            value=${this.priceDraft.output}
            @sl-input=${(event: Event) =>
              this.setPriceField(
                'output',
                (event.target as HTMLInputElement).value
              )}
          ></sl-input>
          <sl-input
            label="Cached input per 1M tokens"
            inputmode="decimal"
            data-testid="price-cached"
            value=${this.priceDraft.cachedInput}
            @sl-input=${(event: Event) =>
              this.setPriceField(
                'cachedInput',
                (event.target as HTMLInputElement).value
              )}
          ></sl-input>
          <sl-input
            label="Per request"
            inputmode="decimal"
            data-testid="price-request"
            value=${this.priceDraft.request}
            @sl-input=${(event: Event) =>
              this.setPriceField(
                'request',
                (event.target as HTMLInputElement).value
              )}
          ></sl-input>
          <sl-input
            type="date"
            label="In force from"
            data-testid="price-effective-from"
            value=${this.priceDraft.effectiveFrom}
            @sl-input=${(event: Event) =>
              this.setPriceField(
                'effectiveFrom',
                (event.target as HTMLInputElement).value
              )}
          ></sl-input>
        </div>
        <div class="meta-line">
          Leave a field empty to say nothing about it. Empty is not $0.
        </div>
        <div class="price-actions">
          <sl-button
            variant="primary"
            size="small"
            data-testid="save-price"
            ?loading=${this.pricingSaving}
            @click=${() => void this.savePrice()}
            >Save price</sl-button
          >
          <sl-button size="small" @click=${this.closePriceEditor}
            >Cancel</sl-button
          >
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
            Back to models
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
              // Danger outline, last, after a gap: DESIGN.md "Destructive
              // actions". Solid red next to Edit invited the click it should
              // be hardest to make by accident.
              {
                id: 'delete',
                label: 'Delete',
                icon: 'trash',
                variant: 'danger',
                outline: true,
                separated: true,
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
                <!-- This card holds a name, an identifier, an alias,
                     credentials and a managed agent. That is "Details";
                     "Observability" promised telemetry it never carried. -->
                <div class="model-title">Details</div>
                <div class="badge-row">
                  ${
                    this.model?.provider_name
                      ? html`
                          <sl-badge class="tag-chip" variant="neutral">
                            ${this.model.provider_name}
                          </sl-badge>
                        `
                      : ''
                  }
                  ${
                    this.model?.is_default
                      ? html`<sl-badge class="chip" variant="success" pill
                          >Default</sl-badge
                        >`
                      : ''
                  }
                  ${
                    this.model?.model_kind
                      ? html`
                          <sl-badge class="tag-chip" variant="neutral">
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
                            ${
                              // "Unknown" on a model that was never edited
                              // reads as a lookup that failed.
                              this.model.updated_at
                                ? this.formatDateTime(this.model.updated_at)
                                : 'Never'
                            }
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

            ${this.renderPricingCard()} ${this.renderGatewayValidation()}

            <div class="toolbar">
              <time-range-select
                ariaLabel="Model usage range"
                .value=${this.selectedRange}
                .options=${DATE_RANGE_OPTIONS}
                @range-change=${this.handleRangeChange}
              ></time-range-select>
              <span class="range-window">${this.rangeWindowLabel()}</span>
              <sl-input
                class="interaction-search"
                label="Search captured interactions"
                placeholder="Search prompts, outputs, or metadata"
                clearable
                .value=${this.interactionQuery}
                @sl-input=${this.handleInteractionQueryChange}
                @sl-clear=${this.handleInteractionQueryChange}
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
                        <div>Loading AI model observability...</div>
                      </div>
                    </sl-card>
                  `
                : html`
                    <div
                      class="results ${this.updating ? 'is-updating' : ''}"
                      aria-busy=${this.updating ? 'true' : 'false'}
                    >
                      <sl-card>
                        <div slot="header" class="model-heading">
                          <div class="model-title">Usage summary</div>
                          ${
                            this.trackedPeriodLabel
                              ? html`<div class="meta-line">
                                  ${this.trackedPeriodLabel}
                                </div>`
                              : ''
                          }
                        </div>
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
                    </div>
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
        label="Delete model"
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
