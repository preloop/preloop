import { LitElement, css, html, nothing } from 'lit';
import { customElement, property } from 'lit/decorators.js';

import '@shoelace-style/shoelace/dist/components/dialog/dialog.js';
import './budget-policy-editor.ts';
import { consoleDialogStyles } from '../styles/console-dialog';

/**
 * One dialog for spending limits, shared by the Overview usage card and the
 * attention page, so the limits editor is never re-wrapped per view.
 */
@customElement('budget-limits-dialog')
export class BudgetLimitsDialog extends LitElement {
  @property({ type: Boolean, reflect: true }) open = false;
  @property({ type: Boolean }) billingEnabled = false;
  /** Optional scoping, forwarded to the editor (agent/model embeds). */
  @property({ type: String }) subjectType?: string;
  @property({ type: String }) subjectId?: string;

  static styles = [
    consoleDialogStyles,
    css`
      sl-dialog::part(body) {
        padding-top: var(--sl-spacing-small);
      }

      .description {
        color: var(--sl-color-neutral-600);
        font-size: var(--sl-font-size-small);
        margin-bottom: var(--sl-spacing-medium);
      }
    `,
  ];

  /**
   * Overlay clicks are too easy to hit while filling the form. Hoisted
   * selects (notify recipients, period, scope) portal outside the panel, so
   * a click on an option looks like an overlay click and used to close the
   * dialog. Only the close button and Escape dismiss it.
   */
  private handleRequestClose = (event: CustomEvent<{ source?: string }>) => {
    if (event.detail?.source === 'overlay') {
      event.preventDefault();
    }
  };

  private handleHide(event: Event) {
    if (event.target !== event.currentTarget) return;
    this.open = false;
    this.dispatchEvent(
      new CustomEvent('sl-hide', { bubbles: true, composed: true })
    );
  }

  private handlePoliciesChanged(event: CustomEvent) {
    event.stopPropagation();
    this.dispatchEvent(
      new CustomEvent('budget-policies-changed', {
        bubbles: true,
        composed: true,
        detail: event.detail,
      })
    );
  }

  render() {
    return html`
      <sl-dialog
        label="Spending limits"
        style="--width: 720px;"
        ?open=${this.open}
        @sl-request-close=${this.handleRequestClose}
        @sl-hide=${this.handleHide}
      >
        <div class="description">
          Soft limits notify. Hard limits stop model calls through the gateway.
        </div>
        ${
          // The editor loads policies and subject lists on connect; keep it out
          // of the tree until the operator actually opens the dialog.
          this.open
            ? html`<budget-policy-editor
                .billingEnabled=${this.billingEnabled}
                .subjectType=${this.subjectType}
                .subjectId=${this.subjectId}
                @budget-policies-changed=${this.handlePoliciesChanged}
              ></budget-policy-editor>`
            : nothing
        }
      </sl-dialog>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'budget-limits-dialog': BudgetLimitsDialog;
  }
}
