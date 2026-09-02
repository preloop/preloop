import { LitElement, css, html, nothing } from 'lit';
import { customElement, state } from 'lit/decorators.js';

import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/dialog/dialog.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import { consoleDialogStyles } from '../styles/console-dialog';

export interface ConfirmDialogOptions {
  title: string;
  message: string;
  /** Extra muted line under the message, for consequences worth spelling out. */
  detail?: string;
  confirmLabel?: string;
  cancelLabel?: string;
  variant?: 'danger' | 'primary';
}

/**
 * The console's replacement for `window.confirm`.
 *
 * A native confirm cannot be styled, cannot be themed for dark mode, blocks the
 * whole tab and reads as a browser warning rather than as part of the product.
 * One singleton `sl-dialog` lives at the end of `document.body` and every call
 * site awaits a promise instead.
 */
@customElement('confirm-dialog')
export class ConfirmDialog extends LitElement {
  @state() private options: ConfirmDialogOptions | null = null;
  @state() private isOpen = false;

  private resolver: ((confirmed: boolean) => void) | null = null;
  /**
   * Set while the confirm button settles the promise, so the `sl-after-hide`
   * that Shoelace fires on the way out is not read as a cancel.
   */
  private settled = false;

  static styles = [
    consoleDialogStyles,
    css`
      :host {
        display: contents;
      }
      .message {
        color: var(--sl-color-neutral-800);
        white-space: pre-line;
      }
      .detail {
        margin-top: var(--sl-spacing-small);
        color: var(--sl-color-neutral-600);
        font-size: var(--sl-font-size-small);
        white-space: pre-line;
      }
    `,
  ];

  /** Opens the dialog and resolves once the operator answers. */
  ask(options: ConfirmDialogOptions): Promise<boolean> {
    // A second ask while one is pending cancels the first rather than
    // stranding its caller in a promise that never settles.
    this.settle(false);
    this.options = options;
    this.settled = false;
    this.isOpen = true;
    return new Promise<boolean>((resolve) => {
      this.resolver = resolve;
    });
  }

  private settle(confirmed: boolean): void {
    const resolver = this.resolver;
    this.resolver = null;
    resolver?.(confirmed);
  }

  private confirm(): void {
    this.settled = true;
    this.isOpen = false;
    this.settle(true);
  }

  private cancel(): void {
    this.settled = true;
    this.isOpen = false;
    this.settle(false);
  }

  render() {
    const options = this.options;
    if (!options) return nothing;
    const variant = options.variant || 'primary';
    return html`
      <sl-dialog
        label=${options.title}
        ?open=${this.isOpen}
        @sl-after-hide=${(event: Event) => {
          if (event.target !== event.currentTarget) return;
          this.isOpen = false;
          // Dismissed with Escape, the overlay or the close button.
          if (!this.settled) this.settle(false);
        }}
      >
        <div class="message">${options.message}</div>
        ${
          options.detail
            ? html`<div class="detail">${options.detail}</div>`
            : nothing
        }
        <sl-button slot="footer" @click=${() => this.cancel()}>
          ${options.cancelLabel || 'Cancel'}
        </sl-button>
        <sl-button
          slot="footer"
          variant=${variant}
          ?outline=${variant === 'danger'}
          data-testid="confirm-dialog-confirm"
          @click=${() => this.confirm()}
        >
          ${options.confirmLabel || 'Confirm'}
        </sl-button>
      </sl-dialog>
    `;
  }
}

let singleton: ConfirmDialog | null = null;

function getConfirmDialog(): ConfirmDialog {
  if (singleton?.isConnected) return singleton;
  singleton = document.createElement('confirm-dialog') as ConfirmDialog;
  document.body.append(singleton);
  return singleton;
}

/**
 * Asks the operator to confirm an action. Resolves `true` only when they press
 * the confirm button; dismissing the dialog any other way resolves `false`.
 */
export function confirmDialog(options: ConfirmDialogOptions): Promise<boolean> {
  return getConfirmDialog().ask(options);
}

/** Test seam: drops the singleton so each case starts from a clean dialog. */
export function resetConfirmDialogForTests(): void {
  singleton?.remove();
  singleton = null;
}

/**
 * The console's replacement for `window.alert`: a Shoelace toast, so a
 * non-blocking message never freezes the tab.
 */
export function showToast(
  message: string,
  variant: 'primary' | 'success' | 'neutral' | 'warning' | 'danger' = 'primary'
): void {
  const icon =
    variant === 'danger' || variant === 'warning'
      ? 'exclamation-triangle'
      : variant === 'success'
        ? 'check2-circle'
        : 'info-circle';
  const alert = Object.assign(document.createElement('sl-alert'), {
    variant,
    duration: 4000,
    closable: true,
  });
  alert.innerHTML = `<sl-icon slot="icon" name="${icon}"></sl-icon>`;
  alert.append(document.createTextNode(message));
  document.body.append(alert);
  void (alert as unknown as { toast: () => Promise<void> }).toast();
}

declare global {
  interface HTMLElementTagNameMap {
    'confirm-dialog': ConfirmDialog;
  }
}
