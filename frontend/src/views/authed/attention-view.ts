import { css, html, nothing, unsafeCSS } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import { repeat } from 'lit/directives/repeat.js';

import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/card/card.js';
import '@shoelace-style/shoelace/dist/components/dropdown/dropdown.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/icon-button/icon-button.js';
import '@shoelace-style/shoelace/dist/components/menu/menu.js';
import '@shoelace-style/shoelace/dist/components/menu-item/menu-item.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import '@shoelace-style/shoelace/dist/components/tooltip/tooltip.js';
import '../../components/budget-limits-dialog.ts';
import '../../components/view-header.ts';

import {
  AuthedElement,
  dismissAttentionItem,
  getFeatures,
  getUserProfile,
  hasPermission,
  removeAccountAgent,
  restoreAttentionItem,
  type AttentionDismissal,
  type BudgetPolicy,
  type UserPermissions,
} from '../../api';
import { confirmDialog } from '../../components/confirm-dialog';
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
  errorHeadline,
  groupAttentionItems,
  type AttentionApproval,
  type AttentionFlowExecution,
  type AttentionItem,
  type AttentionKind,
  type AttentionPriceOverride,
  type DismissedAttentionItem,
} from '../../utils/attention';
import { REMOVE_AGENT_CONSEQUENCE } from '../../utils/agent-display';
import { loadAttentionInputs } from '../../utils/attention-data';
import { formatLocalDateTime, formatRelativeTime } from '../../utils/date';
import { renderFailureCategoryChip } from '../../utils/failure-category';
import {
  executionSubjectCss,
  renderExecutionSubject,
} from '../../utils/execution-subject';

/** Rows in a section this small are open on arrival; longer lists start shut. */
const AUTO_EXPAND_MAX_ROWS = 3;

/** Long enough to notice after a jump, short enough not to look like state. */
const HIGHLIGHT_MS = 2400;

const DISMISS_REASON_LABELS: Record<string, string> = {
  expected: 'Expected',
  snoozed: 'Snoozed',
  fixed: 'Marked fixed',
};

/**
 * One page for everything waiting on a human or degraded right now. The
 * Overview shows the first five of exactly this list; this view is where the
 * count in the hero metric leads.
 *
 * Every row can be opened to show the evidence behind it (which runs failed,
 * which models have no price) and dismissed with a reason. A dismissal is
 * pinned to a fingerprint, so the item comes back by itself when the situation
 * changes rather than staying hidden forever.
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
  @state() private priceOverrides: AttentionPriceOverride[] = [];

  @state() private usageSummary: AccountGatewayUsageSummaryResponse | null =
    null;
  @state() private dismissals: AttentionDismissal[] = [];
  /** False on a server without the endpoint: no dismiss controls at all. */
  @state() private dismissalsSupported = false;
  /**
   * `null` when RBAC is off (the permission helpers then allow everything);
   * an allow-list otherwise. Writing a dismissal needs `manage_agents`.
   */
  @state() private permissions: UserPermissions = null;
  @state() private lastUpdatedAt: string | null = null;
  @state() private billingEnabled = false;
  @state() private showLimitsDialog = false;
  @state() private showDismissed = false;
  @state() private highlightedId: string | null = null;
  @state() private busyItemId: string | null = null;
  @state() private dismissError: string | null = null;
  /** Explicit open/close per row; sections of 3 or fewer default to open. */
  @state() private rowOverrides: Record<string, boolean> = {};

  private unsubscribeRealtime?: () => void;
  private refreshTimer: number | null = null;
  private highlightTimer: number | null = null;
  private handledHash: string | null = null;

  static styles = [
    unsafeCSS(consoleStyles),
    unsafeCSS(executionSubjectCss),
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
        background: var(--console-surface);
        border: 1px solid var(--console-hairline);
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
        color: var(--console-meta-color);
        cursor: default;
      }

      .chip sl-icon {
        color: var(--console-meta-color);
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
        border-radius: var(--sl-border-radius-medium);
        padding: var(--sl-spacing-x-small) 0;
      }

      .attention-row + .attention-row {
        border-top: 1px solid var(--console-hairline);
      }

      /* A jump from the Overview strip has to land somewhere visible. The tint
         fades out on its own: it says "here", it is not a state. */
      .attention-row.highlighted {
        animation: row-highlight ${unsafeCSS(HIGHLIGHT_MS)}ms ease-out;
      }

      @keyframes row-highlight {
        0% {
          background: color-mix(
            in srgb,
            var(--sl-color-primary-500) 18%,
            transparent
          );
        }
        100% {
          background: transparent;
        }
      }

      @media (prefers-reduced-motion: reduce) {
        .attention-row.highlighted {
          animation: none;
          background: color-mix(
            in srgb,
            var(--sl-color-primary-500) 12%,
            transparent
          );
        }
      }

      .row-head {
        align-items: center;
        display: flex;
        gap: var(--sl-spacing-small);
      }

      .row-toggle {
        align-items: center;
        background: none;
        border: none;
        color: var(--console-meta-color);
        cursor: pointer;
        display: flex;
        flex-shrink: 0;
        padding: 2px;
      }

      .row-toggle sl-icon {
        transition: transform 0.15s ease;
      }

      .row-toggle[aria-expanded='true'] sl-icon {
        transform: rotate(90deg);
      }

      .row-spacer {
        flex-shrink: 0;
        width: 20px;
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

      /* Low tone: worth naming once, not worth an amber dot. */
      .severity-dot.low {
        background: var(--sl-color-neutral-400);
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
        color: var(--console-meta-color);
        font-size: var(--console-text-meta);
        font-variant-numeric: tabular-nums;
      }

      .row-actions {
        align-items: center;
        display: flex;
        gap: var(--sl-spacing-2x-small);
        margin-left: auto;
      }

      .row-evidence {
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-x-small);
        padding: var(--sl-spacing-x-small) 0 var(--sl-spacing-small) 40px;
      }

      .evidence-line {
        color: var(--sl-color-neutral-700);
        font-size: var(--console-text-meta);
      }

      .evidence-line .mono,
      .evidence-table .mono {
        font-family: var(--sl-font-mono);
        font-size: 12px;
      }

      .evidence-table {
        border-collapse: collapse;
        font-size: var(--console-text-meta);
        table-layout: fixed;
        width: 100%;
      }

      .evidence-table th {
        color: var(--console-meta-color);
        font-weight: 500;
        padding: 2px var(--sl-spacing-x-small) 2px 0;
        text-align: left;
      }

      .evidence-table td {
        color: var(--sl-color-neutral-700);
        font-variant-numeric: tabular-nums;
        overflow: hidden;
        padding: 3px var(--sl-spacing-x-small) 3px 0;
        text-overflow: ellipsis;
        white-space: nowrap;
      }

      .evidence-table td a {
        color: var(--console-link-color);
        text-decoration: none;
      }

      .evidence-table .numeric {
        text-align: right;
      }

      /* The subject is what the reader scans this table for, so it gets the
         body colour while the timings stay in the -700 register. */
      .evidence-table .subject-cell {
        color: var(--console-body-color);
      }

      .runs-table .subject-cell {
        width: 30%;
      }
      .runs-table .started-cell {
        width: 18%;
      }
      .runs-table .duration-cell {
        width: 12%;
      }
      .runs-table .open-cell {
        width: 60px;
      }

      /* The category column takes its width from the subject, which has the
         most to give: a chip needs a fixed amount and a truncated chip says
         nothing. Overflow stays visible so the tooltip's chip is not clipped. */
      .runs-table .category-cell {
        overflow: visible;
        width: 18%;
      }
      .runs-table.has-category .subject-cell {
        width: 22%;
      }
      .runs-table.has-category .started-cell {
        width: 15%;
      }
      .runs-table.has-category .duration-cell {
        width: 10%;
      }

      /* On a phone the five columns collide and every one of them is cut off
         mid-word. Duration and the error text are the ones to drop: the error
         is repeated in full above the table as "most common", and the run is
         one tap away. Subject and when it ran stay, and the subject takes the
         width the other two gave up. */
      @media (max-width: 700px) {
        .runs-table .duration-cell,
        .runs-table .error-cell {
          display: none;
        }
        .runs-table .subject-cell {
          width: 58%;
        }
        .runs-table .started-cell {
          width: 22%;
        }
        /* The category survives the narrow layout: it is the shortest
           statement of why the run failed, and it is what the error column
           would have said in more words. */
        .runs-table.has-category .subject-cell {
          width: 40%;
        }
        .runs-table.has-category .started-cell {
          width: 20%;
        }
        .runs-table.has-category .category-cell {
          width: 34%;
        }
      }

      /* At 390px even those three columns collide: the category chip is wider
         than 34% of the table and overprints "Open run". Started goes (the
         subject link keeps the exact time in its title), the chip takes the
         width it needs, and Open keeps a column of its own. */
      @media (max-width: 480px) {
        .runs-table.has-category .started-cell {
          display: none;
        }
        .runs-table.has-category .subject-cell {
          width: 38%;
        }
        .runs-table.has-category .category-cell {
          width: 42%;
        }
        .runs-table.has-category .open-cell {
          width: 20%;
        }
        .runs-table.has-category .category-cell sl-badge.chip::part(base) {
          font-size: 11px;
          padding: 1px 6px;
        }
      }

      /* Pricing evidence: the same widths the columns carried inline, now in
         the stylesheet so the phone layout below can change them. */
      .pricing-table .provider-cell {
        width: 18%;
      }
      .pricing-table .requests-cell,
      .pricing-table .tokens-cell {
        width: 12%;
      }
      .pricing-table .last-request-cell {
        width: 16%;
      }
      .pricing-table .price-cell {
        width: 80px;
      }
      .pricing-table .stacked-provider {
        display: none;
      }

      /* On a phone six columns cannot hold six headers, and a fixed table
         lets them overprint each other ("Model" over "Provider"). Tokens and
         the last request go (both are on the model page), the provider moves
         under the alias, and every header clips to its own column. */
      @media (max-width: 700px) {
        .pricing-table .tokens-cell,
        .pricing-table .last-request-cell,
        .pricing-table .provider-cell {
          display: none;
        }
        .pricing-table .model-cell {
          width: 52%;
        }
        .pricing-table .requests-cell {
          width: 22%;
        }
        .pricing-table .price-cell {
          width: 26%;
        }
        .pricing-table .stacked-provider {
          color: var(--console-meta-color);
          display: block;
          font-family: var(--sl-font-sans);
          overflow: hidden;
          text-overflow: ellipsis;
        }
        .pricing-table th {
          overflow: hidden;
          text-overflow: ellipsis;
          white-space: nowrap;
        }
      }

      /* The one filled block allowed inside a card body: a command to copy,
         on the page colour, no border. */
      .evidence-command {
        align-items: center;
        background: var(--console-page);
        border: none;
        border-radius: var(--sl-border-radius-medium);
        display: flex;
        gap: var(--sl-spacing-x-small);
        max-width: 100%;
        overflow: hidden;
        padding: 2px var(--sl-spacing-2x-small) 2px var(--sl-spacing-x-small);
      }

      .evidence-command code {
        font-family: var(--sl-font-mono);
        font-size: 12px;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }

      .evidence-reason {
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-2x-small);
      }

      .evidence-actions {
        display: flex;
        flex-wrap: wrap;
        gap: var(--sl-spacing-x-small);
      }

      .dismissed-toggle {
        align-items: center;
        background: none;
        border: none;
        color: var(--sl-color-neutral-700);
        cursor: pointer;
        display: flex;
        font-family: inherit;
        font-size: var(--console-text-card-title);
        font-weight: 600;
        gap: var(--sl-spacing-2x-small);
        padding: 0;
      }

      .dismissed-row {
        align-items: center;
        display: flex;
        gap: var(--sl-spacing-small);
        padding: var(--sl-spacing-x-small) 0;
      }

      .dismissed-row + .dismissed-row {
        border-top: 1px solid var(--console-hairline);
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
        color: var(--console-meta-color);
        font-size: var(--sl-font-size-small);
      }

      .loading-container {
        display: flex;
        justify-content: center;
        padding: var(--sl-spacing-2x-large);
      }

      @media (max-width: 640px) {
        .row-head {
          flex-wrap: wrap;
        }

        .row-actions {
          margin-left: 28px;
        }

        .row-evidence {
          padding-left: var(--sl-spacing-x-small);
        }
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
    if (this.highlightTimer !== null) {
      window.clearTimeout(this.highlightTimer);
      this.highlightTimer = null;
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
    // Exactly the same loader the Overview uses, so the hero count and this
    // page can never be computed from differently shaped data.
    const [inputs, features, profile] = await Promise.all([
      loadAttentionInputs(),
      getFeatures().catch(() => null),
      getUserProfile().catch(() => null),
    ]);

    this.approvals = (inputs.approvals || []) as AttentionApproval[];
    this.agents = inputs.agents || [];
    this.sessions = inputs.sessions || [];
    this.executions = (inputs.executions || []) as AttentionFlowExecution[];
    this.gatewayFailures = inputs.gatewayFailures || [];
    this.budgetPolicies = inputs.budgetPolicies || [];
    this.usageSummary = inputs.usageSummary || null;
    this.priceOverrides = inputs.priceOverrides || [];
    this.dismissals = (inputs.dismissals || []) as AttentionDismissal[];
    this.dismissalsSupported = inputs.dismissalsSupported;
    this.permissions = profile?.permissions ?? null;
    this.billingEnabled = features?.features?.billing === true;

    this.lastUpdatedAt = new Date().toISOString();
    this.loading = false;
  }

  private get derived() {
    return deriveAttentionItems({
      approvals: this.approvals,
      agents: this.agents,
      sessions: this.sessions,
      executions: this.executions,
      gatewayFailures: this.gatewayFailures,
      budgetPolicies: this.budgetPolicies,
      usageSummary: this.usageSummary,
      priceOverrides: this.priceOverrides,
      dismissals: this.dismissals,
    });
  }

  private get items(): AttentionItem[] {
    return this.derived.items;
  }

  /** `approval` -> `approvals`, `pricing` -> `pricing`. */
  private sectionId(kind: AttentionKind): string {
    return ATTENTION_KIND_META[kind].plural.toLowerCase();
  }

  private scrollToSection(kind: AttentionKind): void {
    const section = this.renderRoot.querySelector(`#${this.sectionId(kind)}`);
    section?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  protected updated(): void {
    if (!this.loading) {
      this.applyHashTarget();
    }
  }

  /**
   * `/console/attention#item-<id>` comes from the Overview strip chips: open
   * that row, scroll it into view and tint it once so the jump lands.
   */
  private applyHashTarget(): void {
    const hash = window.location.hash;
    if (!hash.startsWith('#item-') || hash === this.handledHash) {
      return;
    }
    const itemId = decodeURIComponent(hash.slice('#item-'.length));
    const row = this.renderRoot.querySelector(
      `[data-item-id="${CSS.escape(itemId)}"]`
    );
    if (!row) {
      return;
    }
    this.handledHash = hash;
    this.rowOverrides = { ...this.rowOverrides, [itemId]: true };
    row.scrollIntoView({ behavior: 'smooth', block: 'center' });
    this.highlightedId = itemId;
    if (this.highlightTimer !== null) {
      window.clearTimeout(this.highlightTimer);
    }
    this.highlightTimer = window.setTimeout(() => {
      this.highlightTimer = null;
      this.highlightedId = null;
    }, HIGHLIGHT_MS);
  }

  private isExpanded(item: AttentionItem, sectionSize: number): boolean {
    const override = this.rowOverrides[item.id];
    if (override !== undefined) {
      return override;
    }
    return sectionSize <= AUTO_EXPAND_MAX_ROWS;
  }

  private toggleRow(item: AttentionItem, sectionSize: number): void {
    this.rowOverrides = {
      ...this.rowOverrides,
      [item.id]: !this.isExpanded(item, sectionSize),
    };
  }

  private async dismiss(
    item: AttentionItem,
    reason: 'expected' | 'snoozed' | 'fixed'
  ): Promise<void> {
    this.busyItemId = item.id;
    this.dismissError = null;
    try {
      await dismissAttentionItem(item.id, {
        fingerprint: item.fingerprint,
        reason,
        snooze_days: reason === 'snoozed' ? 7 : undefined,
      });
      await this.fetchAll();
    } catch {
      this.dismissError = 'Could not dismiss that item. Try again.';
    } finally {
      this.busyItemId = null;
    }
  }

  private async restore(itemId: string): Promise<void> {
    this.busyItemId = itemId;
    this.dismissError = null;
    try {
      await restoreAttentionItem(itemId);
      await this.fetchAll();
    } catch {
      this.dismissError = 'Could not restore that item. Try again.';
    } finally {
      this.busyItemId = null;
    }
  }

  private async copyCommand(command: string): Promise<void> {
    try {
      await navigator.clipboard.writeText(command);
    } catch {
      // Clipboard permission denied: the text is on screen and selectable.
    }
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

  /**
   * Writing a dismissal needs `manage_agents`, the permission the backend
   * checks on `PUT/DELETE /attention/dismissals`; any member can read the
   * inbox. Without it the controls are absent rather than present and
   * answering with a 403.
   */
  private get canWriteDismissals(): boolean {
    return (
      this.dismissalsSupported &&
      hasPermission(this.permissions, 'manage_agents')
    );
  }

  /** Removing an agent is the same permission the agent page checks. */
  private get canRemoveAgents(): boolean {
    return hasPermission(this.permissions, 'manage_agents');
  }

  /**
   * The Remove offered next to "start it where it runs": one confirmation
   * naming the agent and its consequence, then the same DELETE the agent page
   * uses, then a refetch so the row leaves the inbox.
   */
  private async removeAgent(
    item: AttentionItem,
    agent: { id: string; name: string }
  ): Promise<void> {
    const confirmed = await confirmDialog({
      title: 'Remove agent',
      message: `Remove ${agent.name} from the managed agents list?`,
      detail: REMOVE_AGENT_CONSEQUENCE,
      confirmLabel: 'Remove agent',
      variant: 'danger',
    });
    if (!confirmed) {
      return;
    }
    this.busyItemId = item.id;
    this.dismissError = null;
    try {
      await removeAccountAgent(agent.id);
      await this.fetchAll();
    } catch {
      this.dismissError = 'Could not remove that agent. Try again.';
    } finally {
      this.busyItemId = null;
    }
  }

  /**
   * Approvals are never dismissable, and a server without the endpoint gets no
   * controls at all rather than buttons that fail.
   */
  private renderDismiss(item: AttentionItem) {
    if (!this.canWriteDismissals || !item.dismissable) {
      return nothing;
    }
    // Where the answer is only ever "yes, that is expected", a menu is one
    // click too many.
    if (item.quickDismiss) {
      return html`
        <sl-button
          size="small"
          data-testid="quick-dismiss"
          ?loading=${this.busyItemId === item.id}
          @click=${() =>
            void this.dismiss(item, item.quickDismiss?.reason || 'expected')}
          >${item.quickDismiss.label}</sl-button
        >
      `;
    }
    return html`
      <sl-dropdown class="dismiss-dropdown" hoist>
        <sl-button
          slot="trigger"
          size="small"
          caret
          ?loading=${this.busyItemId === item.id}
          >Dismiss</sl-button
        >
        <sl-menu
          @sl-select=${(event: CustomEvent<{ item: { value: string } }>) =>
            void this.dismiss(
              item,
              event.detail.item.value as 'expected' | 'snoozed' | 'fixed'
            )}
        >
          <sl-menu-item value="expected"
            >Expected, keep quiet until it changes</sl-menu-item
          >
          <sl-menu-item value="snoozed">Snooze 7 days</sl-menu-item>
          <sl-menu-item value="fixed">Fixed</sl-menu-item>
        </sl-menu>
      </sl-dropdown>
    `;
  }

  private renderCommand(command: string) {
    return html`
      <div class="evidence-command">
        <code>${command}</code>
        <sl-icon-button
          name="clipboard"
          label="Copy command"
          @click=${() => void this.copyCommand(command)}
        ></sl-icon-button>
      </div>
    `;
  }

  private renderFlowEvidence(item: AttentionItem) {
    const runs = item.evidence?.failedRuns || [];
    const common = item.evidence?.mostCommonError;
    const flowId = item.evidence?.flowId;
    // The column exists only when the server categorises failures. A column of
    // blanks against an older server would say "we do not know" five times.
    const showCategory = runs.some((run) => Boolean(run.failureCategory));
    return html`
      ${
        common
          ? html`<div class="evidence-line">
              Most common: <span class="mono">${common.message}</span>
              (${common.count} of ${common.total})
            </div>`
          : nothing
      }
      <table
        class="evidence-table runs-table ${showCategory ? 'has-category' : ''}"
      >
        <thead>
          <tr>
            <!-- Subject first: six failures of one flow differ by what they
                 ran on, not by when they ran. Column widths live in the
                 stylesheet, not in style attributes, so the narrow layout
                 below can widen the subject. -->
            <th class="subject-cell">Subject</th>
            <th class="started-cell">Started</th>
            <th class="duration-cell">Duration</th>
            ${
              showCategory
                ? html`<th class="category-cell">Category</th>`
                : nothing
            }
            <th class="error-cell">Error</th>
            <th class="open-cell"></th>
          </tr>
        </thead>
        <tbody>
          ${runs.map(
            (run) => html`
              <tr>
                <td class="subject-cell">
                  ${renderExecutionSubject({
                    id: run.id,
                    trigger_subject: run.subject,
                    trigger_subject_url: run.subjectUrl,
                  })}
                </td>
                <td
                  class="started-cell"
                  title=${
                    run.startedAt ? formatLocalDateTime(run.startedAt) : nothing
                  }
                >
                  ${run.startedAt ? formatRelativeTime(run.startedAt) : 'n/a'}
                </td>
                <td class="duration-cell">${run.durationText || 'n/a'}</td>
                ${
                  showCategory
                    ? html`<td class="category-cell">
                        ${renderFailureCategoryChip(run.failureCategory)}
                      </td>`
                    : nothing
                }
                <td
                  class="mono error-cell"
                  title=${run.errorMessage || nothing}
                >
                  ${
                    errorHeadline(run.errorMessage) ||
                    'No error message recorded'
                  }
                </td>
                <td class="open-cell">
                  <a href="/console/flows/executions/${run.id}">Open run</a>
                </td>
              </tr>
            `
          )}
        </tbody>
      </table>
      <div class="evidence-actions">
        ${
          flowId
            ? html`<sl-button
                size="small"
                href="/console/flows/executions?flow_id=${encodeURIComponent(
                  flowId
                )}&status=FAILED"
                >Open runs</sl-button
              >`
            : nothing
        }
        ${
          flowId
            ? html`<sl-button size="small" href="/console/flows/${flowId}"
                >Open flow</sl-button
              >`
            : nothing
        }
      </div>
    `;
  }

  private renderAgentEvidence(item: AttentionItem) {
    const reasons = item.evidence?.agentReasons || [];
    return html`
      ${reasons.map(
        (reason) => html`
          <div class="evidence-reason">
            <div class="evidence-line">${reason.text}</div>
            ${reason.command ? this.renderCommand(reason.command) : nothing}
            ${
              reason.action || reason.removeAgent
                ? html`<div class="evidence-actions">
                    ${
                      reason.action
                        ? html`<sl-button
                            size="small"
                            href=${reason.action.href}
                            >${reason.action.label}</sl-button
                          >`
                        : nothing
                    }
                    ${
                      reason.removeAgent && this.canRemoveAgents
                        ? html`<sl-button
                            size="small"
                            variant="danger"
                            outline
                            data-testid="remove-agent"
                            ?loading=${this.busyItemId === item.id}
                            @click=${() =>
                              this.removeAgent(item, reason.removeAgent!)}
                            >Remove</sl-button
                          >`
                        : nothing
                    }
                  </div>`
                : nothing
            }
          </div>
        `
      )}
    `;
  }

  private renderModelEvidence(item: AttentionItem) {
    const failures = item.evidence?.modelFailures || [];
    return html`
      <table class="evidence-table">
        <thead>
          <tr>
            <th style="width: 22%">When</th>
            <th style="width: 12%">Status</th>
            <th>Error</th>
            <th style="width: 90px"></th>
          </tr>
        </thead>
        <tbody>
          ${failures.map(
            (failure) => html`
              <tr>
                <td
                  title=${failure.at ? formatLocalDateTime(failure.at) : nothing}
                >
                  ${failure.at ? formatRelativeTime(failure.at) : 'n/a'}
                </td>
                <td>${failure.statusCode ?? 'n/a'}</td>
                <td class="mono" title=${failure.excerpt || nothing}>
                  ${failure.excerpt || 'No response body recorded'}
                </td>
                <td>
                  ${
                    failure.sessionId
                      ? html`<a
                          href="/console/runtime-sessions?sessionId=${failure.sessionId}"
                          >Open session</a
                        >`
                      : nothing
                  }
                </td>
              </tr>
            `
          )}
        </tbody>
      </table>
      <div class="evidence-actions">
        <sl-button size="small" href=${item.href}>Open model</sl-button>
      </div>
    `;
  }

  /**
   * Whole under a thousand, compact above it, with the exact figure in the
   * cell's title: the console's number rule, and 95,592,073 tokens in a table
   * cell is a width, not a fact.
   */
  private formatCount(value: number): string {
    if (value < 1000) return String(Math.round(value));
    return new Intl.NumberFormat(undefined, {
      notation: 'compact',
      maximumFractionDigits: 1,
    }).format(value);
  }

  private renderPricingEvidence(item: AttentionItem) {
    const zeroPriced = item.evidence?.zeroPricedModels || [];
    const models = zeroPriced.length
      ? zeroPriced
      : item.evidence?.unpricedModels || [];
    const catalogMissing = item.evidence?.catalogMissing === true;
    // A column of "n/a" is a column of nothing: the gateway summary only
    // carries a last-request time for some breakdowns.
    const anyLastRequest = models.some((model) => Boolean(model.lastRequestAt));
    return html`
      ${
        catalogMissing
          ? html`<div class="evidence-line">
              Load a catalog or add price overrides on the Cost page, and past
              usage is costed from then on.
            </div>`
          : nothing
      }
      ${
        zeroPriced.length > 0
          ? html`<div class="evidence-line">
              These models have a price and it is $0, so their usage adds
              nothing to spend. Promo or mistake?
            </div>`
          : nothing
      }
      ${
        models.length > 0
          ? html`
              <table class="evidence-table pricing-table">
                <thead>
                  <tr>
                    <th class="model-cell">Model</th>
                    <th class="provider-cell">Provider</th>
                    <th class="requests-cell numeric">Requests</th>
                    <th class="tokens-cell numeric">Tokens</th>
                    ${
                      anyLastRequest
                        ? html`<th class="last-request-cell">Last request</th>`
                        : nothing
                    }
                    <th class="price-cell"></th>
                  </tr>
                </thead>
                <tbody>
                  ${models.map(
                    (model) => html`
                      <tr>
                        <td class="mono model-cell" title=${model.alias}>
                          ${model.alias}
                          <!-- The provider column is dropped on a phone, so
                               the provider rides under the alias there. -->
                          <span class="stacked-provider"
                            >${model.provider || 'n/a'}</span
                          >
                        </td>
                        <td class="provider-cell">
                          ${model.provider || 'n/a'}
                        </td>
                        <td
                          class="numeric requests-cell"
                          title=${model.requests.toLocaleString()}
                        >
                          ${this.formatCount(model.requests)}
                        </td>
                        <td
                          class="numeric tokens-cell"
                          title=${model.tokens.toLocaleString()}
                        >
                          ${this.formatCount(model.tokens)}
                        </td>
                        ${
                          anyLastRequest
                            ? html`<td
                                class="last-request-cell"
                                title=${
                                  model.lastRequestAt
                                    ? formatLocalDateTime(model.lastRequestAt)
                                    : nothing
                                }
                              >
                                ${
                                  model.lastRequestAt
                                    ? formatRelativeTime(model.lastRequestAt)
                                    : ''
                                }
                              </td>`
                            : nothing
                        }
                        <td class="price-cell">
                          <a
                            href=${
                              model.aiModelId
                                ? `/console/ai-models/${model.aiModelId}?pricing=edit`
                                : '/console/cost?panel=pricing'
                            }
                            >${zeroPriced.length > 0 ? 'Edit price' : 'Set price'}</a
                          >
                        </td>
                      </tr>
                    `
                  )}
                </tbody>
              </table>
            `
          : nothing
      }
      <div class="evidence-actions">
        <sl-button size="small" href="/console/cost?panel=pricing"
          >Open pricing</sl-button
        >
      </div>
    `;
  }

  private renderBudgetEvidence(item: AttentionItem) {
    const budget = item.evidence?.budget;
    if (!budget) {
      return nothing;
    }
    const money = (value: number) => `$${value.toFixed(2)}`;
    return html`
      <table class="evidence-table">
        <tbody>
          <tr>
            <th style="width: 40%">Spend this ${budget.period} period</th>
            <td>${money(budget.spendUsd)}</td>
          </tr>
          <tr>
            <th>Soft limit</th>
            <td>
              ${budget.softLimitUsd ? money(budget.softLimitUsd) : 'none'}
            </td>
          </tr>
          <tr>
            <th>Hard limit</th>
            <td>
              ${budget.hardLimitUsd ? money(budget.hardLimitUsd) : 'none'}
            </td>
          </tr>
        </tbody>
      </table>
      <div class="evidence-actions">
        <sl-button size="small" @click=${() => (this.showLimitsDialog = true)}
          >Configure limits</sl-button
        >
      </div>
    `;
  }

  private renderEvidence(item: AttentionItem) {
    switch (item.kind) {
      case 'flow':
        return this.renderFlowEvidence(item);
      case 'agent':
        return this.renderAgentEvidence(item);
      case 'model':
        return this.renderModelEvidence(item);
      case 'pricing':
        return this.renderPricingEvidence(item);
      case 'budget':
        return this.renderBudgetEvidence(item);
      default:
        return nothing;
    }
  }

  private hasEvidence(item: AttentionItem): boolean {
    const evidence = item.evidence;
    if (!evidence) return false;
    return Boolean(
      evidence.failedRuns?.length ||
      evidence.agentReasons?.length ||
      evidence.modelFailures?.length ||
      evidence.unpricedModels?.length ||
      evidence.zeroPricedModels?.length ||
      evidence.catalogMissing ||
      evidence.budget
    );
  }

  private renderRow(item: AttentionItem, sectionSize: number) {
    const expandable = this.hasEvidence(item);
    const expanded = expandable && this.isExpanded(item, sectionSize);
    return html`
      <div
        class="attention-row ${
          this.highlightedId === item.id ? 'highlighted' : ''
        }"
        data-item-id=${item.id}
      >
        <div class="row-head">
          ${
            expandable
              ? html`<button
                  class="row-toggle"
                  aria-expanded=${expanded ? 'true' : 'false'}
                  aria-label=${expanded ? 'Hide details' : 'Show details'}
                  @click=${() => this.toggleRow(item, sectionSize)}
                >
                  <sl-icon name="chevron-right"></sl-icon>
                </button>`
              : html`<span class="row-spacer"></span>`
          }
          <span class="severity-dot ${item.severity}" aria-hidden="true"></span>
          <div class="row-body">
            <a class="row-title" href=${item.href}>${item.title}</a>
            <span
              class="row-detail"
              title=${item.at ? formatLocalDateTime(item.at) : nothing}
              >${item.detail}</span
            >
          </div>
          <div class="row-actions">
            ${this.renderAction(item)} ${this.renderDismiss(item)}
          </div>
        </div>
        ${
          expanded
            ? html`<div class="row-evidence">${this.renderEvidence(item)}</div>`
            : nothing
        }
      </div>
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
            <!-- A section count stays a solid pill (wave 4): it is a
                 badge on a heading, not a state on a row. The page's own
                 .chip class is the filter strip, so it is not used here. -->
            <sl-badge variant="neutral" pill>${items.length}</sl-badge>
          </div>
        </div>
        <div class="list">
          ${repeat(
            items,
            (item) => item.id,
            (item) => this.renderRow(item, items.length)
          )}
        </div>
      </sl-card>
    `;
  }

  private renderDismissedSection(dismissed: DismissedAttentionItem[]) {
    if (dismissed.length === 0) {
      return nothing;
    }
    return html`
      <sl-card class="content-card" id="dismissed">
        <div slot="header" class="card-header-with-action">
          <button
            class="dismissed-toggle"
            aria-expanded=${this.showDismissed ? 'true' : 'false'}
            @click=${() => (this.showDismissed = !this.showDismissed)}
          >
            <sl-icon
              name=${this.showDismissed ? 'chevron-down' : 'chevron-right'}
            ></sl-icon>
            Dismissed (${dismissed.length})
          </button>
        </div>
        ${
          this.showDismissed
            ? html`<div class="list">
                ${dismissed.map(
                  ({ item, dismissal }) => html`
                    <div class="dismissed-row">
                      <div class="row-body">
                        <a class="row-title" href=${item.href}>${item.title}</a>
                        <span class="row-detail">
                          ${
                            DISMISS_REASON_LABELS[dismissal.reason] ||
                            dismissal.reason
                          }
                          ${
                            dismissal.snooze_until
                              ? ` until ${formatLocalDateTime(
                                  dismissal.snooze_until
                                )}`
                              : ''
                          }
                          ${
                            dismissal.dismissed_by_username
                              ? ` · by ${dismissal.dismissed_by_username}`
                              : ''
                          }
                          ${
                            dismissal.created_at
                              ? ` · ${formatRelativeTime(dismissal.created_at)}`
                              : ''
                          }
                        </span>
                      </div>
                      ${
                        this.canWriteDismissals
                          ? html`<sl-button
                              class="row-action"
                              size="small"
                              ?loading=${this.busyItemId === item.id}
                              @click=${() => void this.restore(item.id)}
                              >Restore</sl-button
                            >`
                          : nothing
                      }
                    </div>
                  `
                )}
              </div>`
            : nothing
        }
      </sl-card>
    `;
  }

  private renderAllClear(dismissedCount: number) {
    return html`
      <sl-card class="content-card">
        <div class="all-clear">
          <sl-icon name="check-circle"></sl-icon>
          <div>Nothing needs you right now.</div>
          ${
            dismissedCount > 0
              ? html`<div class="row-detail">
                  ${dismissedCount} dismissed
                  ${dismissedCount === 1 ? 'item is' : 'items are'} hidden
                  below.
                </div>`
              : nothing
          }
          <a href="/console">Back to Overview</a>
        </div>
      </sl-card>
    `;
  }

  render() {
    const { items, dismissed } = this.derived;
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
                  ${
                    this.dismissError
                      ? html`<div class="row-detail" role="alert">
                          ${this.dismissError}
                        </div>`
                      : nothing
                  }
                  <div class="sections">
                    ${
                      items.length === 0
                        ? this.renderAllClear(dismissed.length)
                        : ATTENTION_KIND_ORDER.map((kind) =>
                            this.renderSection(kind, grouped.get(kind) || [])
                          )
                    }
                    ${this.renderDismissedSection(dismissed)}
                  </div>
                `
          }
        </div>
      </div>

      <budget-limits-dialog
        ?open=${this.showLimitsDialog}
        .billingEnabled=${this.billingEnabled}
        @budget-limits-hide=${() => (this.showLimitsDialog = false)}
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
