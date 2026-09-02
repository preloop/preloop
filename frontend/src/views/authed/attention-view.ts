import { css, html, nothing, unsafeCSS } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import { repeat } from 'lit/directives/repeat.js';

import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/card/card.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import '../../components/budget-limits-dialog.ts';
import '../../components/view-header.ts';

import {
  AuthedElement,
  getAccountAgents,
  getAccountGatewayUsageSearch,
  getAccountGatewayUsageSummary,
  getAccountRuntimeSessions,
  getBudgetPolicies,
  getFeatures,
  getFlowExecutions,
  listApprovalRequests,
  type BudgetPolicy,
} from '../../api';
import { unifiedWebSocketManager } from '../../services/unified-websocket-manager';
import consoleStyles from '../../styles/console-styles.css?inline';
import type {
  AccountGatewayUsageSummaryResponse,
  GatewayUsageSearchResultItem,
  ManagedAgentSummary,
  RuntimeSessionSummary,
} from '../../types';
import {
  ATTENTION_KIND_META,
  ATTENTION_KIND_ORDER,
  attentionKindChipLabel,
  deriveAttentionItems,
  groupAttentionItems,
  type AttentionApproval,
  type AttentionFlowExecution,
  type AttentionItem,
  type AttentionKind,
} from '../../utils/attention';
import { formatRelativeTime } from '../../utils/date';

const DAY_MS = 24 * 60 * 60 * 1000;

/**
 * One page for everything waiting on a human or degraded right now. The
 * Overview shows the first five of exactly this list; this view is where the
 * count in the hero metric leads.
 */
@customElement('attention-view')
export class AttentionView extends AuthedElement {
  @state() private loading = true;
  @state() private approvals: AttentionApproval[] = [];
  @state() private agents: ManagedAgentSummary[] = [];
  @state() private sessions: RuntimeSessionSummary[] = [];
  @state() private executions: AttentionFlowExecution[] = [];
  @state() private gatewayFailures: GatewayUsageSearchResultItem[] = [];
  @state() private budgetPolicies: BudgetPolicy[] = [];
  @state() private usageSummary: AccountGatewayUsageSummaryResponse | null =
    null;
  @state() private lastUpdatedAt: string | null = null;
  @state() private billingEnabled = false;
  @state() private showLimitsDialog = false;

  private unsubscribeRealtime?: () => void;
  private refreshTimer: number | null = null;

  static styles = [
    unsafeCSS(consoleStyles),
    css`
      :host {
        display: block;
      }

      .chip-strip {
        display: flex;
        flex-wrap: wrap;
        gap: var(--sl-spacing-x-small);
        margin-bottom: var(--sl-spacing-large);
      }

      .chip {
        align-items: center;
        background: var(--sl-color-neutral-0);
        border: 1px solid var(--sl-color-neutral-200);
        border-radius: var(--sl-border-radius-pill);
        color: var(--sl-color-neutral-800);
        cursor: pointer;
        display: inline-flex;
        font-size: var(--sl-font-size-small);
        font-variant-numeric: tabular-nums;
        gap: var(--sl-spacing-2x-small);
        padding: var(--sl-spacing-2x-small) var(--sl-spacing-small);
      }

      .chip.empty {
        color: var(--sl-color-neutral-500);
        cursor: default;
      }

      .chip sl-icon {
        color: var(--sl-color-neutral-500);
      }

      .sections {
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-large);
      }

      .section-title {
        align-items: center;
        display: flex;
        font-weight: 600;
        gap: var(--sl-spacing-2x-small);
      }

      .attention-row {
        align-items: center;
        display: flex;
        gap: var(--sl-spacing-small);
        padding: var(--sl-spacing-x-small) 0;
      }

      .attention-row + .attention-row {
        border-top: 1px solid var(--sl-color-neutral-100);
      }

      .severity-dot {
        background: var(--sl-color-warning-600);
        border-radius: 50%;
        flex-shrink: 0;
        height: 8px;
        width: 8px;
      }

      .severity-dot.critical {
        background: var(--sl-color-danger-600);
      }

      .row-body {
        display: flex;
        flex-direction: column;
        gap: 2px;
        min-width: 0;
      }

      .row-title {
        color: var(--sl-color-neutral-900);
        text-decoration: none;
      }

      .row-title:hover {
        text-decoration: underline;
      }

      .row-detail {
        color: var(--sl-color-neutral-500);
        font-size: var(--sl-font-size-small);
        font-variant-numeric: tabular-nums;
      }

      .row-action {
        margin-left: auto;
      }

      .all-clear {
        align-items: center;
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-small);
        padding: var(--sl-spacing-2x-large);
        text-align: center;
      }

      .all-clear sl-icon {
        color: var(--sl-color-success-600);
        font-size: 2.5rem;
      }

      .updated-at {
        color: var(--sl-color-neutral-500);
        font-size: var(--sl-font-size-small);
      }

      .loading-container {
        display: flex;
        justify-content: center;
        padding: var(--sl-spacing-2x-large);
      }
    `,
  ];

  connectedCallback(): void {
    super.connectedCallback();
    void this.fetchAll();
    this.connectRealtime();
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
      unifiedWebSocketManager.subscribe('approvals', scheduleRefresh),
      unifiedWebSocketManager.subscribe('managed_agents', scheduleRefresh),
      unifiedWebSocketManager.subscribe('flow_executions', scheduleRefresh),
      unifiedWebSocketManager.subscribe('gateway_activity', scheduleRefresh),
      unifiedWebSocketManager.subscribe('budget_health', scheduleRefresh),
    ];
    this.unsubscribeRealtime = () => {
      for (const unsubscribe of unsubscribers) {
        unsubscribe();
      }
    };
    void unifiedWebSocketManager.connect();
  }

  /** Bursts of websocket events collapse into one refetch. */
  private scheduleRefresh(): void {
    if (this.refreshTimer !== null) {
      window.clearTimeout(this.refreshTimer);
    }
    this.refreshTimer = window.setTimeout(() => {
      this.refreshTimer = null;
      void this.fetchAll();
    }, 1500);
  }

  private async fetchAll(): Promise<void> {
    const sessionsStart = new Date(Date.now() - 7 * DAY_MS).toISOString();
    const usageStart = new Date(Date.now() - 30 * DAY_MS).toISOString();

    const [
      approvals,
      agents,
      sessions,
      executions,
      failures,
      policies,
      summary,
      features,
    ] = await Promise.allSettled([
      listApprovalRequests({ status: 'pending', limit: 100 }),
      getAccountAgents({ status: 'all', limit: 200 }),
      getAccountRuntimeSessions({
        status: 'all',
        limit: 100,
        startDate: sessionsStart,
      }),
      getFlowExecutions({ status: 'FAILED', limit: 25 }),
      getAccountGatewayUsageSearch({ limit: 12 }),
      getBudgetPolicies(),
      getAccountGatewayUsageSummary({
        startDate: usageStart,
        includeBreakdown: false,
      }),
      getFeatures(),
    ]);

    // A rejected call (usually a 403 for a permission this operator lacks)
    // simply drops its section rather than failing the page.
    if (approvals.status === 'fulfilled') {
      this.approvals = (approvals.value || []) as AttentionApproval[];
    }
    if (agents.status === 'fulfilled') {
      this.agents = agents.value.items || [];
    }
    if (sessions.status === 'fulfilled') {
      this.sessions = sessions.value.items || [];
    }
    if (executions.status === 'fulfilled') {
      this.executions = (executions.value || []) as AttentionFlowExecution[];
    }
    if (failures.status === 'fulfilled') {
      this.gatewayFailures = (failures.value.items || []).filter(
        (item) => item.outcome !== 'success'
      );
    }
    if (policies.status === 'fulfilled') {
      this.budgetPolicies = policies.value || [];
    }
    if (summary.status === 'fulfilled') {
      this.usageSummary = summary.value;
    }
    if (features.status === 'fulfilled') {
      this.billingEnabled = features.value?.features?.billing === true;
    }

    this.lastUpdatedAt = new Date().toISOString();
    this.loading = false;
  }

  private get items(): AttentionItem[] {
    return deriveAttentionItems({
      approvals: this.approvals,
      agents: this.agents,
      sessions: this.sessions,
      executions: this.executions,
      gatewayFailures: this.gatewayFailures,
      budgetPolicies: this.budgetPolicies,
      usageSummary: this.usageSummary,
    });
  }

  /** `approval` -> `approvals`, `pricing` -> `pricing`. */
  private sectionId(kind: AttentionKind): string {
    return ATTENTION_KIND_META[kind].plural.toLowerCase();
  }

  private scrollToSection(kind: AttentionKind): void {
    const section = this.renderRoot.querySelector(`#${this.sectionId(kind)}`);
    section?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  private renderChipStrip(grouped: Map<AttentionKind, AttentionItem[]>) {
    return html`
      <div class="chip-strip">
        ${ATTENTION_KIND_ORDER.map((kind) => {
          const count = grouped.get(kind)?.length || 0;
          return html`
            <button
              class="chip ${count === 0 ? 'empty' : ''}"
              ?disabled=${count === 0}
              @click=${() => this.scrollToSection(kind)}
            >
              <sl-icon name=${ATTENTION_KIND_META[kind].icon}></sl-icon>
              ${attentionKindChipLabel(kind, count)}
            </button>
          `;
        })}
      </div>
    `;
  }

  private renderAction(item: AttentionItem) {
    if (item.action?.event === 'configure-limits') {
      return html`
        <sl-button
          class="row-action"
          size="small"
          @click=${() => (this.showLimitsDialog = true)}
        >
          ${item.action.label}
        </sl-button>
      `;
    }
    if (item.action?.href) {
      return html`
        <sl-button class="row-action" size="small" href=${item.action.href}>
          ${item.action.label}
        </sl-button>
      `;
    }
    return html`
      <sl-button class="row-action" size="small" href=${item.href}>
        ${item.kind === 'approval' ? 'Review' : 'Open'}
      </sl-button>
    `;
  }

  private renderSection(kind: AttentionKind, items: AttentionItem[]) {
    if (items.length === 0) {
      return nothing;
    }
    const meta = ATTENTION_KIND_META[kind];
    return html`
      <sl-card class="content-card" id=${this.sectionId(kind)}>
        <div slot="header" class="card-header-with-action">
          <div class="section-title">
            <sl-icon name=${meta.icon}></sl-icon>
            ${meta.plural}
            <sl-badge variant="neutral" pill>${items.length}</sl-badge>
          </div>
        </div>
        <div class="list">
          ${repeat(
            items,
            (item) => item.id,
            (item) => html`
              <div class="attention-row">
                <span
                  class="severity-dot ${item.severity}"
                  aria-hidden="true"
                ></span>
                <div class="row-body">
                  <a class="row-title" href=${item.href}>${item.title}</a>
                  <span class="row-detail">${item.detail}</span>
                </div>
                ${this.renderAction(item)}
              </div>
            `
          )}
        </div>
      </sl-card>
    `;
  }

  private renderAllClear() {
    return html`
      <sl-card class="content-card">
        <div class="all-clear">
          <sl-icon name="check-circle"></sl-icon>
          <div>Nothing needs you right now.</div>
          <a href="/console">Back to Overview</a>
        </div>
      </sl-card>
    `;
  }

  render() {
    const items = this.items;
    const grouped = groupAttentionItems(items);

    return html`
      <view-header headerText="Needs attention" width="wide">
        <div slot="description">
          Everything waiting on you or degraded right now: approvals, agents,
          flows, models, and budgets.
          ${
            this.lastUpdatedAt
              ? html`<span class="updated-at"
                  >Updated ${formatRelativeTime(this.lastUpdatedAt)}</span
                >`
              : nothing
          }
        </div>
      </view-header>

      <div class="column-layout wide">
        <div class="main-column">
          ${
            this.loading
              ? html`<div class="loading-container">
                  <sl-spinner style="font-size: 2rem;"></sl-spinner>
                </div>`
              : html`
                  ${this.renderChipStrip(grouped)}
                  <div class="sections">
                    ${
                      items.length === 0
                        ? this.renderAllClear()
                        : ATTENTION_KIND_ORDER.map((kind) =>
                            this.renderSection(kind, grouped.get(kind) || [])
                          )
                    }
                  </div>
                `
          }
        </div>
      </div>

      <budget-limits-dialog
        ?open=${this.showLimitsDialog}
        .billingEnabled=${this.billingEnabled}
        @sl-hide=${() => (this.showLimitsDialog = false)}
        @budget-policies-changed=${() => void this.fetchAll()}
      ></budget-limits-dialog>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'attention-view': AttentionView;
  }
}
