import { LitElement, html, css, nothing, unsafeCSS } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
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
  listSubaccounts,
  updateSubaccount,
  type Subaccount,
  isNotFound,
} from '../../../hierarchy-api';
import { formatTags, parseTags } from './tags';

const HIERARCHY_DOCS_URL =
  'https://docs.preloop.ai/guide/accounts-and-profiles';

/**
 * Settings > Subaccounts (capability `account_hierarchy`): create, rename,
 * tag, detach and delete the subaccounts of the current account.
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
    `,
  ];

  @state() private accountId = '';
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

  render() {
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
}

declare global {
  interface HTMLElementTagNameMap {
    'subaccounts-view': SubaccountsView;
  }
}
