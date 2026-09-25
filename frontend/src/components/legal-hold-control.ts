import { LitElement, css, html, nothing } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/dialog/dialog.js';
import '@shoelace-style/shoelace/dist/components/textarea/textarea.js';
import { getUserProfile, hasPermission } from '../api';
import {
  createLegalHold,
  listLegalHolds,
  releaseLegalHold,
  type LegalHold,
} from '../records-api';
import { showToast } from './confirm-dialog';
import {
  HOLD_DOES,
  HOLD_DOES_NOT,
  MAX_HOLD_REASON,
  MIN_HOLD_REASON,
} from '../utils/records-format';

/**
 * Place or release the active hold on one resource.
 *
 * The held state updates from the response, before the parent refetches.
 */
@customElement('legal-hold-control')
export class LegalHoldControl extends LitElement {
  @property({ attribute: 'resource-type' })
  resourceType = 'execution';

  @property({ attribute: 'resource-id' })
  resourceId = '';

  /** When the parent already knows the row is frozen. */
  @property({ type: Boolean, attribute: 'known-held' })
  knownHeld = false;

  @state() private hold: LegalHold | null = null;
  @state() private canView = false;
  @state() private canManage = false;
  @state() private ready = false;
  @state() private dialog: 'place' | 'release' | null = null;
  @state() private reason = '';
  @state() private busy = false;
  @state() private error: string | null = null;

  static styles = css`
    :host {
      display: inline-flex;
      align-items: center;
      gap: var(--sl-spacing-small);
      flex-wrap: wrap;
    }
    .note {
      color: var(--sl-color-neutral-600);
      font-size: var(--sl-font-size-small);
    }
    .actions {
      display: flex;
      gap: var(--sl-spacing-small);
      justify-content: flex-end;
    }
  `;

  connectedCallback(): void {
    super.connectedCallback();
    void this.refresh();
  }

  updated(changed: Map<string, unknown>): void {
    if (
      (changed.has('resourceId') || changed.has('resourceType')) &&
      this.ready
    ) {
      void this.loadHold();
    }
  }

  private async refresh(): Promise<void> {
    try {
      const profile = await getUserProfile();
      this.canView = hasPermission(profile.permissions, 'view_policies');
      this.canManage = hasPermission(profile.permissions, 'manage_policies');
    } catch {
      this.canView = false;
      this.canManage = false;
    }
    this.ready = true;
    if (this.canView && this.resourceId) {
      await this.loadHold();
    }
  }

  private async loadHold(): Promise<void> {
    if (!this.resourceId) {
      this.hold = null;
      return;
    }
    try {
      const rows = await listLegalHolds({
        activeOnly: true,
        resourceType: this.resourceType,
        resourceId: this.resourceId,
        limit: 5,
      });
      this.hold =
        rows.find(
          (row) =>
            row.resource_id === this.resourceId &&
            row.resource_type === this.resourceType &&
            row.active
        ) ?? null;
    } catch {
      this.hold = null;
    }
  }

  private get held(): boolean {
    return this.hold?.active === true || this.knownHeld;
  }

  private open(kind: 'place' | 'release'): void {
    this.reason = '';
    this.error = null;
    this.dialog = kind;
  }

  private async confirm(): Promise<void> {
    const reason = this.reason.trim();
    if (reason.length < MIN_HOLD_REASON) {
      this.error = `A reason of at least ${MIN_HOLD_REASON} characters is required.`;
      return;
    }
    this.busy = true;
    this.error = null;
    try {
      if (this.dialog === 'place') {
        this.hold = await createLegalHold({
          resource_type: this.resourceType,
          resource_id: this.resourceId,
          reason,
        });
        showToast('Legal hold placed.', 'success');
      } else if (this.hold) {
        this.hold = await releaseLegalHold(this.hold.id, reason);
        showToast('Legal hold released.', 'success');
      }
      this.dialog = null;
      this.dispatchEvent(
        new CustomEvent('records-hold-changed', {
          bubbles: true,
          composed: true,
          detail: { held: this.hold?.active === true },
        })
      );
    } catch (err) {
      this.error =
        err instanceof Error ? err.message : 'Could not update the hold';
    } finally {
      this.busy = false;
    }
  }

  render() {
    if (!this.ready) {
      return nothing;
    }
    if (!this.canView && !this.knownHeld) {
      return nothing;
    }
    const held = this.held;
    return html`
      ${
        held
          ? html`<sl-badge
              class="chip"
              variant="warning"
              pill
              data-testid="hold-badge"
              >Legal hold</sl-badge
            >`
          : nothing
      }
      ${
        this.canManage && this.resourceId
          ? html`<sl-button
              size="small"
              variant=${held && this.hold ? 'danger' : 'default'}
              ?outline=${held && !!this.hold}
              data-testid=${held && this.hold ? 'release-hold' : 'place-hold'}
              @click=${() => this.open(held && this.hold ? 'release' : 'place')}
            >
              ${held && this.hold ? 'Release hold' : 'Place legal hold'}
            </sl-button>`
          : nothing
      }
      ${
        held && this.canManage && !this.hold
          ? html`<span class="note"
              >Held. Open Settings, Records to release a hold this page could
              not match.</span
            >`
          : nothing
      }
      <sl-dialog
        label=${this.dialog === 'release' ? 'Release legal hold' : 'Place legal hold'}
        ?open=${this.dialog !== null}
        @sl-after-hide=${() => {
          if (!this.busy) this.dialog = null;
        }}
      >
        <p>
          ${
            this.dialog === 'release'
              ? 'Releasing the hold lets the purge and the evidence janitor remove this record again once retention says so. The release reason is written to the audit log.'
              : `${HOLD_DOES} ${HOLD_DOES_NOT}`
          }
        </p>
        <sl-textarea
          label="Reason"
          required
          data-testid="hold-reason"
          .value=${this.reason}
          maxlength=${MAX_HOLD_REASON}
          help-text=${`At least ${MIN_HOLD_REASON} characters. Written to the audit log.`}
          @sl-input=${(event: Event) => {
            this.reason = (event.target as HTMLInputElement).value;
          }}
        ></sl-textarea>
        ${this.error ? html`<p class="note">${this.error}</p>` : nothing}
        <div class="actions" slot="footer">
          <sl-button
            @click=${() => {
              this.dialog = null;
            }}
            >Cancel</sl-button
          >
          <sl-button
            variant=${this.dialog === 'release' ? 'danger' : 'primary'}
            ?outline=${this.dialog === 'release'}
            ?loading=${this.busy}
            data-testid="hold-confirm"
            @click=${() => this.confirm()}
          >
            ${this.dialog === 'release' ? 'Release hold' : 'Place hold'}
          </sl-button>
        </div>
      </sl-dialog>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'legal-hold-control': LegalHoldControl;
  }
}
