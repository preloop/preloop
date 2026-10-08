import { LitElement, html, css, nothing, unsafeCSS } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/card/card.js';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import '../../../components/view-header.ts';
import consoleStyles from '../../../styles/console-styles.css?inline';
import { confirmDialog } from '../../../components/confirm-dialog';
import { isCapabilityOff } from '../../../capabilities';
import {
  createSubaccount,
  currentAccountId,
  deleteSubaccount,
  detachSubaccount,
  getMemberships,
  listSubaccounts,
  switchAccount,
  updateSubaccount,
  type Subaccount,
  isNotFound,
} from '../../../hierarchy-api';
import { formatTags, parseTags } from './tags';

const HIERARCHY_DOCS_URL =
  'https://docs.preloop.ai/guide/accounts-and-profiles';

/**
 * The subaccounts of the current account (capability `account_hierarchy`):
 * create, open, rename, tag, detach and delete.
 *
 * Settings > Account renders it as a card (`embedded`). The card shows only
 * on a root account: a subaccount creates subaccounts of its own only when
 * its parent allowed it, which the console cannot read, so it does not
 * offer what the server would refuse. `/console/settings/subaccounts` still
 * renders the full page for deep links, with no nav entry pointing at it.
 */
@customElement('subaccounts-view')
export class SubaccountsView extends LitElement {
  static styles = [
    unsafeCSS(consoleStyles),
    css`
      table {
        width: 100%;
        border-collapse: collapse;
      }
      th,
      td {
        text-align: left;
        padding: var(--sl-spacing-x-small) var(--sl-spacing-small);
        border-bottom: 1px solid var(--sl-color-neutral-200);
        vertical-align: middle;
      }
      .create {
        display: flex;
        gap: var(--sl-spacing-small);
        align-items: flex-end;
        margin-bottom: var(--sl-spacing-medium);
        flex-wrap: wrap;
      }
      .actions {
        display: flex;
        gap: var(--sl-spacing-2x-small);
        justify-content: flex-end;
      }
      .error {
        color: var(--sl-color-danger-700);
      }
      .off-state,
      .hint {
        color: var(--console-meta-color, var(--sl-color-neutral-600));
      }
      .table-scroll {
        overflow-x: auto;
      }
      .subaccounts-card {
        display: block;
        margin-bottom: 2rem;
      }
    `,
  ];

  /** Render as the Subaccounts card of the Account page. */
  @property({ type: Boolean }) embedded = false;
  /** For tests: where "Open" goes after the switch. */
  @property({ attribute: false }) navigate?: (url: string) => void;

  @state() private accountId = '';
  /** The current account sits below another one (embedded card hides). */
  @state() private nonRoot = false;
  @state() private subaccounts: Subaccount[] = [];
  @state() private loading = true;
  /** The endpoint is missing: the capability is off on this server. */
  @state() private off = false;
  @state() private error = '';
  @state() private editingId: string | null = null;
  @state() private busy = false;

  connectedCallback() {
    super.connectedCallback();
    void this.load();
  }

  private async load() {
    this.loading = true;
    this.error = '';
    try {
      this.accountId = await currentAccountId();
      if (this.embedded) {
        // Memberships name the current account's parent. Without the
        // multi-account capability the list is unavailable; the account is
        // then treated as a root and the server has the last word.
        const memberships = await getMemberships().catch(() => []);
        const current = memberships.find(
          (m) => m.account_id === this.accountId
        );
        this.nonRoot = !!current?.parent_account_id;
        if (this.nonRoot) return;
      }
      this.subaccounts = await listSubaccounts(this.accountId);
    } catch (error) {
      if (isCapabilityOff(error)) {
        this.off = true;
      } else {
        this.error =
          error instanceof Error ? error.message : 'Could not load subaccounts';
      }
    } finally {
      this.loading = false;
    }
  }

  private field(id: string): string {
    const input = this.renderRoot.querySelector<HTMLInputElement>(`#${id}`);
    return (input?.value ?? '').trim();
  }

  private async run(action: () => Promise<void>) {
    this.busy = true;
    this.error = '';
    try {
      await action();
      this.subaccounts = await listSubaccounts(this.accountId);
    } catch (error) {
      if (isCapabilityOff(error)) {
        this.off = true;
      } else if (isNotFound(error)) {
        // Gone, or never in this account: say so and show the fresh list.
        this.error = 'That subaccount is not in this account.';
        this.editingId = null;
        this.subaccounts = await listSubaccounts(this.accountId).catch(
          () => this.subaccounts
        );
      } else {
        this.error = error instanceof Error ? error.message : 'Request failed';
      }
    } finally {
      this.busy = false;
    }
  }

  private create = () => {
    const name = this.field('new-name');
    const { tags, errors } = parseTags(this.field('new-tags'));
    if (!name) {
      this.error = 'Give the subaccount a name.';
      return;
    }
    if (errors.length) {
      this.error = errors.join('. ');
      return;
    }
    void this.run(async () => {
      await createSubaccount(this.accountId, { name, tags });
      for (const id of ['new-name', 'new-tags']) {
        const input = this.renderRoot.querySelector<HTMLInputElement>(`#${id}`);
        if (input) input.value = '';
      }
    });
  };

  private save(sub: Subaccount) {
    const name = this.field(`name-${sub.id}`);
    const { tags, errors } = parseTags(this.field(`tags-${sub.id}`));
    if (!name) {
      this.error = 'A subaccount needs a name.';
      return;
    }
    if (errors.length) {
      this.error = errors.join('. ');
      return;
    }
    void this.run(async () => {
      await updateSubaccount(this.accountId, sub.id, { name, tags });
      this.editingId = null;
    });
  }

  private async open(sub: Subaccount) {
    try {
      await switchAccount(sub.id, this.navigate);
    } catch (error) {
      this.error =
        error instanceof Error
          ? error.message
          : 'Could not open the subaccount';
    }
  }

  private async detach(sub: Subaccount) {
    const ok = await confirmDialog({
      title: `Detach ${sub.name}?`,
      message:
        'The subaccount becomes a separate account on the Free plan. Its shares and access grants are revoked.',
      confirmLabel: 'Detach',
      variant: 'danger',
    });
    if (ok) void this.run(() => detachSubaccount(this.accountId, sub.id));
  }

  private async removeSubaccount(sub: Subaccount) {
    const ok = await confirmDialog({
      title: `Delete ${sub.name}?`,
      message: 'The subaccount and everything in it are deleted.',
      confirmLabel: 'Delete',
      variant: 'danger',
    });
    if (ok) void this.run(() => deleteSubaccount(this.accountId, sub.id));
  }

  private renderRow(sub: Subaccount) {
    if (this.editingId === sub.id) {
      return html`<tr data-id=${sub.id}>
        <td>
          <sl-input
            id=${`name-${sub.id}`}
            size="small"
            value=${sub.name}
            label="Name"
          ></sl-input>
        </td>
        <td>
          <sl-input
            id=${`tags-${sub.id}`}
            size="small"
            value=${formatTags(sub.tags)}
            label="Tags"
            help-text="key=value, comma separated"
          ></sl-input>
        </td>
        <td class="actions">
          <sl-button size="small" @click=${() => (this.editingId = null)}
            >Cancel</sl-button
          >
          <sl-button
            size="small"
            variant="primary"
            ?loading=${this.busy}
            @click=${() => this.save(sub)}
            >Save</sl-button
          >
        </td>
      </tr>`;
    }
    return html`<tr data-id=${sub.id}>
      <td>${sub.name}</td>
      <td>
        ${Object.entries(sub.tags ?? {}).map(
          ([key, value]) =>
            html`<sl-badge variant="neutral" pill>${key}=${value}</sl-badge> `
        )}
      </td>
      <td class="actions">
        <sl-button
          size="small"
          data-testid="open-subaccount"
          @click=${() => this.open(sub)}
          >Open</sl-button
        >
        <sl-button size="small" @click=${() => (this.editingId = sub.id)}
          >Edit</sl-button
        >
        <sl-button size="small" @click=${() => this.detach(sub)}
          >Detach</sl-button
        >
        <sl-button
          size="small"
          variant="danger"
          outline
          @click=${() => this.removeSubaccount(sub)}
          >Delete</sl-button
        >
      </td>
    </tr>`;
  }

  private renderBody() {
    return html`
      ${
        this.loading
          ? html`<sl-spinner></sl-spinner>`
          : html`
              <div class="create">
                <sl-input id="new-name" size="small" label="Name"></sl-input>
                <sl-input
                  id="new-tags"
                  size="small"
                  label="Tags"
                  placeholder="site=north, env=prod"
                ></sl-input>
                <sl-button
                  size="small"
                  variant="primary"
                  ?loading=${this.busy}
                  @click=${this.create}
                  >Create subaccount</sl-button
                >
              </div>
              ${
                this.error
                  ? html`<p class="error" role="alert">${this.error}</p>`
                  : nothing
              }
              ${
                this.subaccounts.length === 0
                  ? html`<p class="empty-state">No subaccounts yet.</p>`
                  : html`<div class="table-scroll">
                      <table>
                        <thead>
                          <tr>
                            <th>Name</th>
                            <th>Tags</th>
                            <th></th>
                          </tr>
                        </thead>
                        <tbody>
                          ${this.subaccounts.map((sub) => this.renderRow(sub))}
                        </tbody>
                      </table>
                    </div>`
              }
            `
      }
    `;
  }

  render() {
    if (this.embedded) {
      // On the Account page the card is simply absent where it does not
      // apply: no extension, or an account below another one.
      if (this.off || this.nonRoot || this.loading) return nothing;
      return html`<sl-card id="subaccounts" class="subaccounts-card">
        <h2 slot="header" style="margin: 0; font-size: 1.25rem;">
          Subaccounts
        </h2>
        <p class="hint">
          Accounts below this one. They share your resources only where you
          share them, and never see each other. Give people and teams access
          from the Users and Teams pages, or see every grant on
          <a href="/console/settings/access-grants">Access grants</a>.
        </p>
        ${this.renderBody()}
      </sl-card>`;
    }
    if (this.off) {
      // A bookmarked link on a server without the extension: say so rather
      // than leave a blank page, and raise no error toast.
      return html`
        <view-header headerText="Subaccounts"></view-header>
        <p class="off-state">
          Subaccounts aren't available on this deployment: the server does not
          have the account hierarchy extension enabled.
          <a href=${HIERARCHY_DOCS_URL} target="_blank" rel="noopener"
            >Learn about subaccounts</a
          >
        </p>
      `;
    }
    return html`
      <view-header
        headerText="Subaccounts"
        description="Accounts below this one. They share your resources only where you share them, and never see each other."
      ></view-header>
      ${this.renderBody()}
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'subaccounts-view': SubaccountsView;
  }
}
