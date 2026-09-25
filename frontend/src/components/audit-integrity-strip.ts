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
      gap: var(--sl-spacing-small);
      align-items: baseline;
      padding-bottom: var(--sl-spacing-small);
      border-bottom: 1px solid var(--console-hairline);
      font-size: var(--sl-font-size-small);
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
    const checkpoint = status.latest_checkpoint;
    return html`
      <div class="strip" data-testid="audit-integrity-strip">
        <span>
          Sealed through seq ${status.head_seq}
          ${status.last_sealed_at ? html`at ${status.last_sealed_at}` : nothing}.
        </span>
        <span class="meta">
          ${status.unsealed_rows} unsealed, seal lag ${status.seal_lag_seconds}s
          (the sealer runs behind the writes).
        </span>
        <span class="meta">
          Last checkpoint ${checkpoint ? `seq ${checkpoint.seq}` : 'none yet'}.
        </span>
        <a href="/console/settings/records#audit-integrity">Verify</a>
        <span class="meta">${CHAIN_HONESTY}</span>
      </div>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'audit-integrity-strip': AuditIntegrityStrip;
  }
}
