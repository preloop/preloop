import { LitElement, html, css } from 'lit';
import { customElement, property } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import { humanizePermission } from '../permissions';

/**
 * Friendly empty state shown when the current user lacks a required permission.
 * Keeps the page chrome intact so the console does not feel broken.
 */
@customElement('permission-denied')
export class PermissionDenied extends LitElement {
  @property({ type: String })
  title = 'Permission required';

  @property({ type: String })
  message =
    'Your role does not include access to this area. Ask an account admin to grant the required permission, or switch to a page you can use.';

  @property({ type: String, attribute: 'required-permission' })
  requiredPermission = '';

  static styles = css`
    :host {
      display: block;
    }

    .panel {
      display: flex;
      flex-direction: column;
      align-items: flex-start;
      gap: 0.75rem;
      max-width: 40rem;
      margin: 2rem auto;
      padding: 1.5rem 1.75rem;
      border: 1px solid var(--sl-color-neutral-200);
      border-radius: var(--sl-border-radius-large);
      background: var(--sl-color-neutral-0);
    }

    .icon-row {
      display: flex;
      align-items: center;
      gap: 0.75rem;
    }

    sl-icon {
      font-size: 1.75rem;
      color: var(--sl-color-warning-600);
    }

    h2 {
      margin: 0;
      font-size: 1.25rem;
      font-weight: 600;
      color: var(--sl-color-neutral-900);
    }

    p {
      margin: 0;
      color: var(--sl-color-neutral-700);
      line-height: 1.5;
    }

    .permission {
      display: inline-block;
      margin-top: 0.25rem;
      padding: 0.15rem 0.5rem;
      border-radius: var(--sl-border-radius-medium);
      background: var(--sl-color-neutral-100);
      color: var(--sl-color-neutral-800);
      font-family: var(--sl-font-mono);
      font-size: 0.85rem;
    }

    .actions {
      display: flex;
      gap: 0.5rem;
      margin-top: 0.5rem;
    }
  `;

  render() {
    const permissionLabel = this.requiredPermission
      ? humanizePermission(this.requiredPermission.split(',')[0].trim())
      : '';

    return html`
      <div class="panel" role="status">
        <div class="icon-row">
          <sl-icon name="shield-lock" label="Permission required"></sl-icon>
          <h2>${this.title}</h2>
        </div>
        <p>${this.message}</p>
        ${
          this.requiredPermission
            ? html`<span class="permission"
                >Required: ${permissionLabel || this.requiredPermission}</span
              >`
            : ''
        }
        <div class="actions">
          <sl-button href="/console" variant="default" size="small">
            Back to Overview
          </sl-button>
        </div>
      </div>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'permission-denied': PermissionDenied;
  }
}
