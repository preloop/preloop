import { html, css, nothing } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import {
  AuthedElement,
  dismissToolCostFlag,
  getToolCostFlags,
  refreshToolCostFlags,
  type ToolCostFlag,
} from '../api';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/card/card.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import '@shoelace-style/shoelace/dist/components/tooltip/tooltip.js';

/**
 * Dashboard panel listing evidence-grounded "tool cost flags" — Preloop's
 * findings that an agent's tool definitions are wasting money. This is a
 * read + dismiss surface only; one-click disable is a deferred phase and is
 * never rendered here.
 */
@customElement('tool-cost-flags-panel')
export class ToolCostFlagsPanel extends AuthedElement {
  @state() private flags: ToolCostFlag[] = [];
  @state() private loading = true;
  @state() private refreshing = false;
  @state() private error: string | null = null;
  @state() private actionError: string | null = null;
  // Tracks whether the optional refresh endpoint is available (404 → hide button).
  @state() private refreshAvailable = true;
  // Per-flag dismiss-in-flight set so individual buttons can show pending state.
  @state() private dismissing: Set<string> = new Set();

  static styles = css`
    :host {
      display: block;
      width: 100%;
    }

    .content-card {
      width: 100%;
    }

    .content-card::part(base) {
      width: 100%;
    }

    .header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: var(--sl-spacing-small);
    }

    .title {
      display: flex;
      align-items: center;
      gap: var(--sl-spacing-2x-small);
      font-weight: 600;
    }

    .content {
      display: flex;
      flex-direction: column;
      gap: var(--sl-spacing-medium);
    }

    .flag-list {
      display: flex;
      flex-direction: column;
      gap: var(--sl-spacing-small);
    }

    .flag-row {
      display: flex;
      flex-direction: column;
      gap: 4px;
      padding: var(--sl-spacing-small) 0;
      border-bottom: 1px solid var(--sl-color-neutral-100);
    }

    .flag-row:last-child {
      border-bottom: none;
    }

    .flag-top {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: var(--sl-spacing-small);
    }

    .flag-name {
      display: flex;
      align-items: center;
      gap: var(--sl-spacing-2x-small);
      font-weight: 600;
      color: var(--sl-color-neutral-900);
      min-width: 0;
      word-break: break-word;
    }

    .source-badge {
      cursor: help;
    }

    .flag-cost {
      font-weight: 600;
      white-space: nowrap;
      color: var(--sl-color-neutral-900);
    }

    .flag-claim {
      color: var(--sl-color-neutral-700);
      font-size: var(--sl-font-size-small);
      line-height: 1.4;
    }

    .flag-footer {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: var(--sl-spacing-small);
    }

    .muted-note {
      color: var(--sl-color-neutral-500);
      font-size: var(--sl-font-size-x-small);
    }

    .empty {
      color: var(--sl-color-neutral-600);
      font-size: var(--sl-font-size-small);
      padding: var(--sl-spacing-small) 0;
    }

    .loading-state {
      display: flex;
      align-items: center;
      gap: var(--sl-spacing-small);
      color: var(--sl-color-neutral-600);
      padding: var(--sl-spacing-small) 0;
    }

    .error-state {
      display: flex;
      flex-direction: column;
      gap: var(--sl-spacing-small);
      align-items: flex-start;
    }
  `;

  connectedCallback() {
    super.connectedCallback();
    void this.load();
  }

  private async load() {
    this.loading = true;
    this.error = null;
    try {
      this.flags = await getToolCostFlags();
    } catch (error) {
      this.error =
        error instanceof Error
          ? error.message
          : 'Failed to load tool cost flags';
    } finally {
      this.loading = false;
    }
  }

  private async handleRefresh() {
    this.refreshing = true;
    this.actionError = null;
    this.error = null;
    try {
      this.flags = await refreshToolCostFlags();
    } catch (error) {
      // The refresh endpoint is optional. If it 404s, hide the button and fall
      // back to a plain reload of the existing flags.
      const message =
        error instanceof Error ? error.message : 'Failed to refresh';
      if (/404/.test(message)) {
        this.refreshAvailable = false;
        await this.load();
      } else {
        this.actionError = message;
      }
    } finally {
      this.refreshing = false;
    }
  }

  private async handleDismiss(flag: ToolCostFlag) {
    this.actionError = null;
    // Optimistically remove the row, remembering its index for restore-on-error.
    const index = this.flags.findIndex((f) => f.id === flag.id);
    const previous = this.flags;
    this.flags = this.flags.filter((f) => f.id !== flag.id);
    this.dismissing = new Set(this.dismissing).add(flag.id);
    try {
      await dismissToolCostFlag(flag.id);
    } catch (error) {
      // Restore the row at its original position and surface the error.
      const restored = [...this.flags];
      restored.splice(index < 0 ? restored.length : index, 0, flag);
      this.flags = restored;
      this.actionError =
        error instanceof Error
          ? error.message
          : 'Failed to dismiss tool cost flag';
      // `previous` retained only for clarity; restored ordering matches it.
      void previous;
    } finally {
      const next = new Set(this.dismissing);
      next.delete(flag.id);
      this.dismissing = next;
    }
  }

  private formatCurrency(value?: number | null): string {
    const amount = Number(value || 0);
    if (amount === 0) return '$0.00';
    return amount >= 0.01 ? `$${amount.toFixed(2)}` : `$${amount.toFixed(4)}`;
  }

  private flagClaim(flag: ToolCostFlag): string {
    const claim = flag.evidence?.claim;
    if (typeof claim === 'string' && claim.trim()) {
      return claim;
    }
    // Fall back to a constructed string when the backend omits an explicit claim.
    return `Estimated ${this.formatCurrency(
      flag.estimated_weekly_cost
    )}/week attributed to this tool definition.`;
  }

  private sourceTooltip(flag: ToolCostFlag): string {
    return `Source "${flag.tool_source}" is a heuristic/provisional label and is not authoritative.`;
  }

  private renderFlag(flag: ToolCostFlag) {
    const isDismissing = this.dismissing.has(flag.id);
    return html`
      <div class="flag-row">
        <div class="flag-top">
          <span class="flag-name">
            ${flag.tool_name}
            <sl-tooltip content=${this.sourceTooltip(flag)}>
              <sl-badge class="source-badge" variant="neutral" pill>
                ${flag.tool_source}
              </sl-badge>
            </sl-tooltip>
          </span>
          <span class="flag-cost"
            >${this.formatCurrency(flag.estimated_weekly_cost)}/wk</span
          >
        </div>
        <div class="flag-claim">${this.flagClaim(flag)}</div>
        <div class="flag-footer">
          ${
            flag.disable_eligible
              ? nothing
              : html`<span class="muted-note">
                  One-click disable unavailable for this tool (ambiguous name).
                </span>`
          }
          <sl-button
            size="small"
            variant="default"
            aria-label=${`Dismiss flag for ${flag.tool_name}`}
            ?loading=${isDismissing}
            @click=${() => this.handleDismiss(flag)}
          >
            Dismiss
          </sl-button>
        </div>
      </div>
    `;
  }

  render() {
    return html`
      <sl-card class="content-card">
        <div slot="header" class="header">
          <div class="title" id="tool-cost-flags-title">
            <sl-icon name="exclamation-triangle" aria-hidden="true"></sl-icon>
            Tool cost flags
            ${
              this.loading || this.refreshing
                ? html`<sl-spinner style="font-size: 1rem;"></sl-spinner>`
                : nothing
            }
          </div>
          ${
            this.refreshAvailable
              ? html`<sl-button
                  size="small"
                  variant="default"
                  ?loading=${this.refreshing}
                  @click=${this.handleRefresh}
                >
                  <sl-icon
                    slot="prefix"
                    name="arrow-clockwise"
                    aria-hidden="true"
                  ></sl-icon>
                  Refresh
                </sl-button>`
              : nothing
          }
        </div>
        <div
          class="content"
          role="region"
          aria-labelledby="tool-cost-flags-title"
        >
          ${
            this.actionError
              ? html`<sl-alert
                  variant="danger"
                  open
                  closable
                  role="alert"
                  aria-live="assertive"
                  @sl-after-hide=${() => (this.actionError = null)}
                  >${this.actionError}</sl-alert
                >`
              : nothing
          }
          ${this.renderBody()}
        </div>
      </sl-card>
    `;
  }

  private renderBody() {
    if (this.loading) {
      return html`<div
        class="loading-state"
        role="status"
        aria-live="polite"
        aria-busy="true"
      >
        <sl-spinner></sl-spinner>
        <span>Loading tool cost flags...</span>
      </div>`;
    }
    if (this.error) {
      return html`<div class="error-state">
        <sl-alert variant="danger" open role="alert" aria-live="assertive">
          <sl-icon
            slot="icon"
            name="exclamation-octagon"
            aria-hidden="true"
          ></sl-icon>
          ${this.error}
        </sl-alert>
        <sl-button size="small" variant="default" @click=${this.load}>
          Retry
        </sl-button>
      </div>`;
    }
    if (!this.flags.length) {
      return html`<div class="empty">
        No tool cost flags — your agents' tool definitions look efficient.
      </div>`;
    }
    return html`<div class="flag-list">
      ${this.flags.map((flag) => this.renderFlag(flag))}
    </div>`;
  }
}
