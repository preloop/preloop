import { LitElement, html, css, nothing } from 'lit';
import { customElement, property, query, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/dialog/dialog.js';
import '@shoelace-style/shoelace/dist/components/divider/divider.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/dropdown/dropdown.js';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/menu/menu.js';
import '@shoelace-style/shoelace/dist/components/menu-item/menu-item.js';
import { getUserProfile } from '../../../api';
import { isCapabilityOff } from '../../../capabilities';
import { showToast } from '../../../components/confirm-dialog';
import {
  createRootAccount,
  getMemberships,
  lastUsedAccounts,
  switchAccount,
  type Membership,
} from '../../../hierarchy-api';
import { groupMemberships } from './membership-order';

/** Above this many accounts the menu gets a search box. */
export const SEARCH_THRESHOLD = 8;

/**
 * Header account switcher (capability `multi_account`). Lists the person's
 * memberships grouped by tree, last used first, and switches with a fresh
 * token pair for the chosen account.
 *
 * The last item, "New account…", creates a root account owned by the
 * person and switches to it. It lives here, where accounts live, rather
 * than in the user menu (personal) or Settings > Account (the current
 * account). A person with one account still gets the menu, so the entry is
 * always reachable.
 */
@customElement('account-switcher')
export class AccountSwitcher extends LitElement {
  static styles = css`
    :host {
      display: inline-flex;
      align-items: center;
    }
    sl-menu sl-input {
      margin: var(--sl-spacing-2x-small) var(--sl-spacing-x-small);
    }
    .dialog-error {
      color: var(--sl-color-danger-700);
      margin: var(--sl-spacing-small) 0 0;
    }
  `;

  @query('sl-dialog.new-account') private newAccountDialog?: HTMLElement & {
    show: () => void;
    hide: () => void;
  };

  @property({ attribute: false }) context: Record<string, unknown> = {};
  /** Where the page goes after a switch; a full load by default. */
  @property({ attribute: false }) navigate?: (url: string) => void;

  @state() private memberships: Membership[] = [];
  @state() private currentId = '';
  @state() private query = '';
  @state() private loaded = false;
  @state() private newAccountName = '';
  @state() private creating = false;
  @state() private createError = '';
  /**
   * The account this dialog already created. A failed switch after a
   * successful create retries only the switch: running the create again
   * would make a second account and leave the first unseen.
   */
  @state() private createdAccountId = '';

  connectedCallback() {
    super.connectedCallback();
    void this.load();
  }

  private async load() {
    try {
      const [memberships, profile] = await Promise.all([
        getMemberships(),
        getUserProfile(),
      ]);
      this.memberships = memberships;
      this.currentId = profile.account_id;
      this.loaded = true;
    } catch (error) {
      if (isCapabilityOff(error)) {
        this.dispatchEvent(
          new CustomEvent('capability-off', { bubbles: true })
        );
      }
      // Anything else: the header simply shows no switcher.
      this.memberships = [];
      this.loaded = true;
    }
  }

  private async pick(accountId: string) {
    if (accountId === this.currentId) return;
    try {
      await switchAccount(accountId, this.navigate);
    } catch (error) {
      if (isCapabilityOff(error)) {
        this.dispatchEvent(
          new CustomEvent('capability-off', { bubbles: true })
        );
        return;
      }
      showToast(
        error instanceof Error ? error.message : 'Could not switch account',
        'danger'
      );
    }
  }

  private openNewAccount = () => {
    this.newAccountName = '';
    this.createError = '';
    this.createdAccountId = '';
    this.newAccountDialog?.show();
  };

  private createAccount = async (event?: Event) => {
    event?.preventDefault();
    const name = this.newAccountName.trim();
    if (!name && !this.createdAccountId) {
      this.createError = 'Give the account a name.';
      return;
    }
    this.creating = true;
    this.createError = '';
    try {
      if (!this.createdAccountId) {
        const created = await createRootAccount(name);
        this.createdAccountId = created.account.id;
        // Listed right away, so the account is reachable from the menu even
        // if the switch below fails and the dialog is closed.
        this.memberships = [...this.memberships, created.membership];
      }
      // The person owns the new account now; sign in to it the same way a
      // pick from the list does.
      await switchAccount(this.createdAccountId, this.navigate);
      this.newAccountDialog?.hide();
    } catch (error) {
      this.createError = this.createdAccountId
        ? `The account was created, but switching to it failed: ${
            error instanceof Error ? error.message : 'unknown error'
          }. Try again, or pick it from the menu.`
        : isCapabilityOff(error)
          ? 'This server cannot create accounts from the console yet.'
          : error instanceof Error
            ? error.message
            : 'Could not create the account';
    } finally {
      this.creating = false;
    }
  };

  private renderNewAccountDialog() {
    return html`<sl-dialog
      class="new-account"
      label="New account"
      @sl-initial-focus=${(e: Event) => {
        e.preventDefault();
        this.renderRoot
          .querySelector<HTMLElement>('[data-testid="new-account-name"]')
          ?.focus();
      }}
    >
      <form @submit=${this.createAccount}>
        <sl-input
          data-testid="new-account-name"
          label="Account name"
          required
          ?disabled=${!!this.createdAccountId}
          maxlength="255"
          .value=${this.newAccountName}
          @sl-input=${(e: Event) =>
            (this.newAccountName = (e.target as HTMLInputElement).value)}
          help-text="A separate account with its own members and resources. You will be its owner."
        ></sl-input>
        ${
          this.createError
            ? html`<p class="dialog-error" role="alert">${this.createError}</p>`
            : nothing
        }
      </form>
      <sl-button slot="footer" @click=${() => this.newAccountDialog?.hide()}
        >Cancel</sl-button
      >
      <sl-button
        slot="footer"
        variant="primary"
        data-testid="create-account"
        ?loading=${this.creating}
        @click=${() => this.createAccount()}
        >${this.createdAccountId ? 'Switch to it' : 'Create account'}</sl-button
      >
    </sl-dialog>`;
  }

  private matchesSearch(m: Membership): boolean {
    const q = this.query.trim().toLowerCase();
    return (
      !q ||
      m.account_name.toLowerCase().includes(q) ||
      (m.slug ?? '').toLowerCase().includes(q)
    );
  }

  private item(m: Membership, depth: number) {
    return html`<sl-menu-item
      class=${depth > 0 ? 'child' : 'root'}
      style=${depth > 0 ? `padding-left: ${depth * 1.25}rem` : ''}
      data-account=${m.account_id}
      type="checkbox"
      ?checked=${m.account_id === this.currentId}
      @click=${() => this.pick(m.account_id)}
      >${m.account_name}</sl-menu-item
    >`;
  }

  render() {
    if (!this.loaded || this.memberships.length === 0) return nothing;
    const current = this.memberships.find(
      (m) => m.account_id === this.currentId
    );
    const label = current?.account_name ?? 'Account';
    const groups = groupMemberships(this.memberships, lastUsedAccounts());
    return html`<sl-dropdown distance="8" placement="bottom-end">
        <sl-button
          slot="trigger"
          size="small"
          caret
          data-testid="account-trigger"
          >${label}</sl-button
        >
        <sl-menu>
          ${
            this.memberships.length > SEARCH_THRESHOLD
              ? html`<sl-input
                  aria-label="Find an account"
                  size="small"
                  placeholder="Find an account"
                  data-testid="account-search"
                  clearable
                  @sl-input=${(e: Event) =>
                    (this.query = (e.target as HTMLInputElement).value)}
                ></sl-input>`
              : nothing
          }
          ${groups.map((group) => {
            const children = group.children.filter((m) =>
              this.matchesSearch(m)
            );
            const showRoot =
              this.matchesSearch(group.root) || children.length > 0;
            return showRoot
              ? html`${this.item(group.root, 0)}
                ${children.map((m) =>
                  this.item(m, group.depth.get(m.account_id) ?? 1)
                )}`
              : nothing;
          })}
          <sl-divider></sl-divider>
          <sl-menu-item
            class="new-account"
            data-testid="new-account"
            @click=${this.openNewAccount}
          >
            <sl-icon slot="prefix" name="plus-lg"></sl-icon>
            New account…
          </sl-menu-item>
        </sl-menu>
      </sl-dropdown>
      ${this.renderNewAccountDialog()}`;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'account-switcher': AccountSwitcher;
  }
}
