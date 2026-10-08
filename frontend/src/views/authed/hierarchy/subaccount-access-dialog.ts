import { LitElement, html, css, nothing, unsafeCSS } from 'lit';
import { customElement, query, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/checkbox/checkbox.js';
import '@shoelace-style/shoelace/dist/components/dialog/dialog.js';
import '@shoelace-style/shoelace/dist/components/option/option.js';
import '@shoelace-style/shoelace/dist/components/radio/radio.js';
import '@shoelace-style/shoelace/dist/components/radio-group/radio-group.js';
import '@shoelace-style/shoelace/dist/components/select/select.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import consoleStyles from '../../../styles/console-styles.css?inline';
import { consoleDialogStyles } from '../../../styles/console-dialog';
import { isCapabilityOff } from '../../../capabilities';
import { confirmDialog } from '../../../components/confirm-dialog';
import {
  createAccessGrant,
  currentAccountId,
  deleteAccessGrant,
  listAccessGrants,
  listSubaccounts,
  type AccessGrant,
  type GrantLevel,
  type Subaccount,
} from '../../../hierarchy-api';
import { GRANT_LEVELS } from './grant-levels';

/**
 * The current account's subaccounts, or none when the extension is off or
 * the caller may not list them. The Users and Teams pages show "Subaccount
 * access" only when this is not empty.
 */
export async function subaccountsOfCurrentAccount(): Promise<Subaccount[]> {
  try {
    return await listSubaccounts(await currentAccountId());
  } catch {
    return [];
  }
}

/** The user or team whose subaccount access the dialog edits. */
export interface GrantSubject {
  type: 'user' | 'team';
  id: string;
  label: string;
}

/**
 * Per-row "Subaccount access" on the Users and Teams pages (capability
 * `account_hierarchy`): the access grants of one user or team, with revoke,
 * and a form to grant access in all subaccounts or selected ones. The
 * Access grants page stays the overview of every grant.
 */
@customElement('subaccount-access-dialog')
export class SubaccountAccessDialog extends LitElement {
  static styles = [
    unsafeCSS(consoleStyles),
    consoleDialogStyles,
    css`
      ul {
        list-style: none;
        padding: 0;
        margin: 0 0 var(--sl-spacing-medium);
      }
      li {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: var(--sl-spacing-small);
        padding: var(--sl-spacing-2x-small) 0;
        border-bottom: 1px solid var(--sl-color-neutral-200);
      }
      form {
        display: grid;
        gap: var(--sl-spacing-small);
      }
      .error {
        color: var(--sl-color-danger-700);
      }
      .hint {
        color: var(--console-meta-color, var(--sl-color-neutral-600));
      }
    `,
  ];

  @query('sl-dialog') private dialog!: HTMLElement & {
    show: () => void;
    hide: () => void;
  };

  @state() private subject: GrantSubject | null = null;
  @state() private accountId = '';
  @state() private grants: AccessGrant[] = [];
  @state() private subaccounts: Subaccount[] = [];
  @state() private loading = false;
  @state() private busy = false;
  @state() private error = '';
  @state() private level: GrantLevel = 'read';
  @state() private target: 'all' | 'selected' = 'all';
  @state() private selected = new Set<string>();

  /** Open the dialog for one user or team. */
  async show(subject: GrantSubject) {
    this.subject = subject;
    this.error = '';
    this.level = 'read';
    this.target = 'all';
    this.selected = new Set();
    await this.updateComplete;
    this.dialog.show();
    await this.load();
  }

  private async load() {
    this.loading = true;
    try {
      this.accountId = await currentAccountId();
      const [grants, subaccounts] = await Promise.all([
        listAccessGrants(this.accountId),
        listSubaccounts(this.accountId),
      ]);
      this.grants = grants;
      this.subaccounts = subaccounts;
    } catch (error) {
      this.error = isCapabilityOff(error)
        ? 'Subaccounts are not available on this deployment.'
        : error instanceof Error
          ? error.message
          : 'Could not load access grants';
    } finally {
      this.loading = false;
    }
  }

  private get subjectGrants(): AccessGrant[] {
    const subject = this.subject;
    if (!subject) return [];
    return this.grants.filter(
      (g) => g.subject_type === subject.type && g.subject_id === subject.id
    );
  }

  private where(grant: AccessGrant): string {
    if (grant.target === 'all') return 'All subaccounts, including new ones';
    const names = (grant.subaccount_ids ?? []).map(
      (id) => this.subaccounts.find((s) => s.id === id)?.name ?? id
    );
    return names.join(', ') || 'None';
  }

  private async act(action: () => Promise<unknown>) {
    this.busy = true;
    this.error = '';
    try {
      await action();
      this.grants = await listAccessGrants(this.accountId);
      this.dispatchEvent(
        new CustomEvent('grants-changed', { bubbles: true, composed: true })
      );
    } catch (error) {
      this.error = error instanceof Error ? error.message : 'Request failed';
    } finally {
      this.busy = false;
    }
  }

  /** Revoking is destructive: confirm with who, what and where, as Access grants does. */
  private async confirmRevoke(grant: AccessGrant) {
    const level = (GRANT_LEVELS[grant.level] ?? grant.level).toLowerCase();
    const ok = await confirmDialog({
      title: 'Revoke access?',
      message: `Revoke ${this.subject?.label ?? 'this'}'s ${level} access?`,
      detail: `Where: ${this.where(grant)}. They lose that access right away.`,
      confirmLabel: 'Revoke',
      variant: 'danger',
    });
    if (!ok) return;
    await this.act(() => deleteAccessGrant(this.accountId, grant.id));
  }

  private submit = (event: Event) => {
    event.preventDefault();
    const subject = this.subject;
    if (!subject) return;
    if (this.target === 'selected' && this.selected.size === 0) {
      this.error = 'Choose at least one subaccount.';
      return;
    }
    void this.act(() =>
      createAccessGrant(this.accountId, {
        subject_type: subject.type,
        subject_id: subject.id,
        level: this.level,
        target: this.target,
        ...(this.target === 'selected'
          ? { subaccount_ids: [...this.selected] }
          : {}),
      }).then(() => {
        this.selected = new Set();
      })
    );
  };

  private toggle(id: string, on: boolean) {
    const next = new Set(this.selected);
    if (on) next.add(id);
    else next.delete(id);
    this.selected = next;
  }

  private renderBody() {
    if (this.loading) return html`<sl-spinner></sl-spinner>`;
    const grants = this.subjectGrants;
    return html`
      ${
        grants.length === 0
          ? html`<p class="hint" data-testid="no-grants">
              No access in any subaccount yet.
            </p>`
          : html`<ul data-testid="subject-grants">
              ${grants.map(
                (grant) =>
                  html`<li data-id=${grant.id}>
                    <span
                      >${GRANT_LEVELS[grant.level] ?? grant.level}:
                      ${this.where(grant)}</span
                    >
                    <sl-button
                      size="small"
                      ?disabled=${this.busy}
                      @click=${() => this.confirmRevoke(grant)}
                      >Revoke</sl-button
                    >
                  </li>`
              )}
            </ul>`
      }
      <form @submit=${this.submit}>
        <sl-select
          id="level"
          label="Level"
          size="small"
          .value=${this.level}
          @sl-change=${(e: Event) =>
            (this.level = (e.target as HTMLSelectElement).value as GrantLevel)}
        >
          ${Object.entries(GRANT_LEVELS).map(
            ([value, label]) =>
              html`<sl-option value=${value}>${label}</sl-option>`
          )}
        </sl-select>
        <sl-radio-group
          id="target"
          label="Where"
          size="small"
          .value=${this.target}
          @sl-change=${(e: Event) =>
            (this.target = (e.target as HTMLInputElement).value as
              'all' | 'selected')}
        >
          <sl-radio value="all"
            >All subaccounts, including future ones</sl-radio
          >
          <sl-radio value="selected">Selected subaccounts</sl-radio>
        </sl-radio-group>
        ${
          this.target === 'selected'
            ? this.subaccounts.map(
                (sub) =>
                  html`<sl-checkbox
                    size="small"
                    data-subaccount=${sub.id}
                    ?checked=${this.selected.has(sub.id)}
                    @sl-change=${(e: Event) =>
                      this.toggle(
                        sub.id,
                        (e.target as HTMLInputElement).checked
                      )}
                    >${sub.name}</sl-checkbox
                  >`
              )
            : nothing
        }
        <div>
          <sl-button
            size="small"
            variant="primary"
            type="submit"
            ?loading=${this.busy}
            >Grant access</sl-button
          >
        </div>
      </form>
      ${
        this.error
          ? html`<p class="error" role="alert">${this.error}</p>`
          : nothing
      }
      <p class="hint">
        <a href="/console/settings/access-grants">All access grants</a>
      </p>
    `;
  }

  render() {
    return html`<sl-dialog
      label=${
        this.subject
          ? `Subaccount access: ${this.subject.label}`
          : 'Subaccount access'
      }
    >
      ${this.subject ? this.renderBody() : nothing}
    </sl-dialog>`;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'subaccount-access-dialog': SubaccountAccessDialog;
  }
}
