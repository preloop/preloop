import { LitElement, html, css, nothing } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/dropdown/dropdown.js';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/menu/menu.js';
import '@shoelace-style/shoelace/dist/components/menu-item/menu-item.js';
import { getUserProfile } from '../../../api';
import { isCapabilityOff } from '../../../capabilities';
import { showToast } from '../../../components/confirm-dialog';
import {
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
 */
@customElement('account-switcher')
export class AccountSwitcher extends LitElement {
  static styles = css`
    :host {
      display: inline-flex;
      align-items: center;
    }
    sl-input {
      margin: var(--sl-spacing-2x-small) var(--sl-spacing-x-small);
    }
  `;

  @property({ attribute: false }) context: Record<string, unknown> = {};
  /** Where the page goes after a switch; a full load by default. */
  @property({ attribute: false }) navigate?: (url: string) => void;

  @state() private memberships: Membership[] = [];
  @state() private currentId = '';
  @state() private query = '';
  @state() private loaded = false;

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
    if (this.memberships.length === 1) {
      return html`<span class="single" data-testid="account-name"
        >${label}</span
      >`;
    }
    const groups = groupMemberships(this.memberships, lastUsedAccounts());
    return html`<sl-dropdown distance="8" placement="bottom-end">
      <sl-button slot="trigger" size="small" caret data-testid="account-trigger"
        >${label}</sl-button
      >
      <sl-menu>
        ${
          this.memberships.length > SEARCH_THRESHOLD
            ? html`<sl-input
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
          const children = group.children.filter((m) => this.matchesSearch(m));
          const showRoot =
            this.matchesSearch(group.root) || children.length > 0;
          return showRoot
            ? html`${this.item(group.root, 0)}
              ${children.map((m) =>
                this.item(m, group.depth.get(m.account_id) ?? 1)
              )}`
            : nothing;
        })}
      </sl-menu>
    </sl-dropdown>`;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'account-switcher': AccountSwitcher;
  }
}
