import { LitElement, css, html, nothing } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import { getUserProfile, hasPermission } from '../api';
import { getAuditChainStatus, type ChainStatus } from '../records-api';
import { CHAIN_HONESTY } from '../utils/records-format';

/**
 * One line on the audit timeline. The verify shortcut opens Records.
 * Sequence range is not derived from a time filter: the verify API takes
 * sequences, and this page's filter is a time range.
 */
@customElement('audit-integrity-strip')
export class AuditIntegrityStrip extends LitElement {
  @state() private status: ChainStatus | null = null;
  @state() private unavailable = false;

  static styles = css`
    :host {
      display: block;
      margin-bottom: var(--sl-spacing-medium);
    }
    .strip {
      display: flex;
      flex-wrap: wrap;
      gap: var(--sl-spacing-2x-small);
      align-items: baseline;
      font-size: var(--sl-font-size-small);
    }
    .chip {
      font-size: 12px;
      font-weight: 500;
      padding: 2px 8px;
      border-radius: var(--sl-border-radius-pill);
      background: color-mix(
        in srgb,
        var(--sl-color-success-500) 16%,
        transparent
      );
      color: var(--sl-color-success-800);
      cursor: help;
    }
    .meta {
      color: var(--console-meta-color);
    }
    a {
      color: var(--console-link-color);
    }
  `;

  connectedCallback(): void {
    super.connectedCallback();
    void this.load();
  }

  private async load(): Promise<void> {
    try {
      const profile = await getUserProfile();
      if (!hasPermission(profile.permissions, 'view_audit_logs')) {
        this.unavailable = true;
        return;
      }
      this.status = await getAuditChainStatus();
    } catch {
      this.unavailable = true;
    }
  }

  render() {
    if (this.unavailable || !this.status) {
      return nothing;
    }
    const status = this.status;
    if (!status.enabled) {
      return html`
        <div class="strip" data-testid="audit-integrity-strip">
          <span>Audit chain is disabled on this deployment.</span>
        </div>
      `;
    }
    const checkpoint = status.latest_checkpoint;
    // One compact chip (#1693). The detail that used to take two lines
    // stays one hover or focus away, and in full on Records.
    const detail = [
      `Sealed through seq ${status.head_seq}` +
        (status.last_sealed_at ? ` at ${status.last_sealed_at}` : '') +
        '.',
      `${status.unsealed_rows} unsealed, seal lag ${status.seal_lag_seconds}s (the sealer runs behind the writes).`,
      `Last checkpoint ${checkpoint ? `seq ${checkpoint.seq}` : 'none yet'}.`,
      CHAIN_HONESTY,
    ].join(' ');
    return html`
      <div class="strip" data-testid="audit-integrity-strip">
        <span
          class="chip"
          data-testid="audit-seal-chip"
          title=${detail}
          aria-label=${detail}
          tabindex="0"
          >Sealed</span
        >
        <span class="meta" aria-hidden="true">·</span>
        <a href="/console/settings/records#audit-integrity">Verify</a>
      </div>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'audit-integrity-strip': AuditIntegrityStrip;
  }
}
