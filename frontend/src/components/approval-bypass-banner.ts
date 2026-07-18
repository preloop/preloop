import { LitElement, html, css } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import { getApprovalBypassStatus, revokeAllApprovalBypasses } from '../api';
import type { ApprovalBypassStatus } from '../types';

/**
 * Persistent warning banner shown whenever approval gating is relaxed.
 *
 * Design intent: a bypass must be impossible to forget. The banner is
 * unmissable, states plainly what is switched off, counts down to expiry, and
 * carries a one-click "Restore approvals" action so re-tightening is never
 * more than a single tap away.
 *
 * Colors follow DESIGN.md semantic states: pending/warning amber `#F2A93B`
 * for a muted-but-still-gating bypass, denied/error red `#FF5D5D` accents when
 * approvals are actually being skipped. A bypass is a warning condition and is
 * never rendered in neutral chrome.
 */
@customElement('approval-bypass-banner')
export class ApprovalBypassBanner extends LitElement {
  @state()
  private status: ApprovalBypassStatus | null = null;

  @state()
  private revoking = false;

  /** Ticks once a second so the countdown stays honest. */
  @state()
  private now = Date.now();

  private pollTimer?: number;
  private tickTimer?: number;

  static styles = css`
    :host {
      display: block;
    }

    .banner {
      display: flex;
      align-items: center;
      gap: 12px;
      padding: 12px 16px;
      border-radius: 4px;
      border-left: 4px solid #f2a93b;
      background: rgba(242, 169, 59, 0.12);
      color: #e6edf3;
      font-size: 14px;
      line-height: 1.4;
    }

    /* Approvals are actually being skipped - the more severe state. */
    .banner.bypassing {
      border-left-color: #ff5d5d;
      background: rgba(255, 93, 93, 0.12);
    }

    .icon {
      flex-shrink: 0;
      font-size: 18px;
      line-height: 1;
    }

    .text {
      flex: 1;
      min-width: 0;
    }

    .title {
      font-weight: 600;
    }

    .detail {
      opacity: 0.85;
      font-size: 13px;
    }

    button {
      flex-shrink: 0;
      background: #0284c7;
      color: #fff;
      border: none;
      border-radius: 4px;
      padding: 8px 14px;
      font-size: 13px;
      font-weight: 500;
      cursor: pointer;
      transition: opacity 150ms ease-out;
    }

    button:hover:not(:disabled) {
      opacity: 0.9;
    }

    button:disabled {
      opacity: 0.6;
      cursor: default;
    }

    @media (prefers-color-scheme: light) {
      .banner {
        color: #1c2128;
      }
    }
  `;

  connectedCallback() {
    super.connectedCallback();
    void this.refresh();
    // Poll so a bypass opened from the phone shows up in an open console tab.
    this.pollTimer = window.setInterval(() => void this.refresh(), 30000);
    this.tickTimer = window.setInterval(() => {
      this.now = Date.now();
    }, 1000);
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    if (this.pollTimer) window.clearInterval(this.pollTimer);
    if (this.tickTimer) window.clearInterval(this.tickTimer);
  }

  /** Reload bypass status, failing silently (the banner is advisory chrome). */
  private async refresh() {
    try {
      this.status = await getApprovalBypassStatus();
    } catch {
      this.status = null;
    }
  }

  private async handleRestore() {
    this.revoking = true;
    try {
      await revokeAllApprovalBypasses();
      await this.refresh();
      this.dispatchEvent(
        new CustomEvent('bypasses-revoked', {
          bubbles: true,
          composed: true,
        })
      );
    } catch {
      // Leave the banner up; the bypass is still active and must stay visible.
    } finally {
      this.revoking = false;
    }
  }

  /** Human-readable time remaining until the soonest expiry. */
  private countdown(): string {
    const expiry = this.status?.soonest_expiry;
    if (!expiry) return '';
    const msLeft = new Date(`${expiry}Z`).getTime() - this.now;
    if (msLeft <= 0) return 'expiring now';
    const minutes = Math.floor(msLeft / 60000);
    if (minutes < 1) return 'less than a minute left';
    if (minutes < 60) return `${minutes} min left`;
    const hours = Math.floor(minutes / 60);
    const remainder = minutes % 60;
    return remainder > 0 ? `${hours}h ${remainder}m left` : `${hours}h left`;
  }

  render() {
    if (!this.status?.active) return html``;

    const bypassing = this.status.auto_approve_active;
    const autoApproved = this.status.bypasses.reduce(
      (sum, b) => sum + (b.auto_approved_count ?? 0),
      0
    );

    return html`
      <div class="banner ${bypassing ? 'bypassing' : ''}" role="alert">
        <span class="icon" aria-hidden="true">${bypassing ? '⚠' : '🔕'}</span>
        <div class="text">
          <div class="title">
            ${
              bypassing
                ? 'Approvals are being auto-approved without review'
                : 'Approval notifications are muted'
            }
          </div>
          <div class="detail">
            ${
              bypassing
                ? html`${autoApproved} tool
                  ${autoApproved === 1 ? 'call has' : 'calls have'} run
                  unsupervised. ${this.countdown()}.`
                : html`Agents are still blocked waiting for you.
                  ${this.countdown()}.`
            }
          </div>
        </div>
        <button
          @click=${this.handleRestore}
          ?disabled=${this.revoking}
          part="restore"
        >
          ${this.revoking ? 'Restoring…' : 'Restore approvals'}
        </button>
      </div>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'approval-bypass-banner': ApprovalBypassBanner;
  }
}
