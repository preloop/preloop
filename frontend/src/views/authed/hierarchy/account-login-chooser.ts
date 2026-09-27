import { LitElement, html, css, nothing } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import { switchAccount, type Membership } from '../../../hierarchy-api';
import { groupMemberships } from './membership-order';

/**
 * Shown after sign-in (capability `multi_account`) when the person belongs
 * to several accounts and none was used last. Picking one switches to it.
 */
@customElement('account-login-chooser')
export class AccountLoginChooser extends LitElement {
  static styles = css`
    ul {
      list-style: none;
      padding: 0;
      margin: 0;
      display: grid;
      gap: var(--sl-spacing-x-small);
    }
    sl-button {
      width: 100%;
    }
    .error {
      color: var(--sl-color-danger-700);
    }
  `;

  @property({ attribute: false }) memberships: Membership[] = [];
  /** Runs after the switch instead of a page load. */
  @property({ attribute: false }) navigate?: (url: string) => void;

  @state() private busyId = '';
  @state() private error = '';

  private async pick(accountId: string) {
    this.busyId = accountId;
    this.error = '';
    try {
      await switchAccount(accountId, this.navigate);
    } catch (error) {
      this.error =
        error instanceof Error ? error.message : 'Could not open the account';
    } finally {
      this.busyId = '';
    }
  }

  private entry(m: Membership, depth: number) {
    return html`<li
      class=${depth > 0 ? 'child' : 'root'}
      style=${depth > 0 ? `padding-left: ${depth * 1.25}rem` : ''}
    >
      <sl-button
        data-account=${m.account_id}
        ?loading=${this.busyId === m.account_id}
        @click=${() => this.pick(m.account_id)}
        >${m.account_name}</sl-button
      >
    </li>`;
  }

  render() {
    return html`<p>Choose the account to open.</p>
      <ul>
        ${groupMemberships(this.memberships).map(
          (group) =>
            html`${this.entry(group.root, 0)}${group.children.map((m) =>
              this.entry(m, group.depth.get(m.account_id) ?? 1)
            )}`
        )}
      </ul>
      ${this.error ? html`<p class="error">${this.error}</p>` : nothing}`;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'account-login-chooser': AccountLoginChooser;
  }
}
