import { LitElement, html, css, nothing, unsafeCSS } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/checkbox/checkbox.js';
import '@shoelace-style/shoelace/dist/components/option/option.js';
import '@shoelace-style/shoelace/dist/components/radio/radio.js';
import '@shoelace-style/shoelace/dist/components/radio-group/radio-group.js';
import '@shoelace-style/shoelace/dist/components/select/select.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import '../../../components/view-header.ts';
import { confirmDialog } from '../../../components/confirm-dialog';
import consoleStyles from '../../../styles/console-styles.css?inline';
import { getTeams, getUsers } from '../../../api';
import { isCapabilityOff } from '../../../capabilities';
import {
  createAccessGrant,
  currentAccountId,
  deleteAccessGrant,
  listAccessGrants,
  listSubaccounts,
  type AccessGrant,
  type GrantLevel,
  type Subaccount,
  isNotFound,
} from '../../../hierarchy-api';
import { GRANT_LEVELS } from './grant-levels';

interface SubjectOption {
  type: 'user' | 'team';
  id: string;
  label: string;
}

const HIERARCHY_DOCS_URL =
  'https://docs.preloop.ai/guide/accounts-and-profiles';

const LEVELS = GRANT_LEVELS;

/**
 * Settings > Access grants (capability `account_hierarchy`): give a user or
 * team of this account a level of access in all subaccounts (including ones
 * created later) or in selected ones.
 */
@customElement('access-grants-view')
export class AccessGrantsView extends LitElement {
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
      }
      form {
        display: grid;
        gap: var(--sl-spacing-small);
        max-width: 36rem;
        margin-bottom: var(--sl-spacing-large);
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
  @state() private grants: AccessGrant[] = [];
  @state() private subaccounts: Subaccount[] = [];
  @state() private subjects: SubjectOption[] = [];
  @state() private loading = true;
  @state() private off = false;
  @state() private error = '';
  @state() private subjectKey = '';
  @state() private level: GrantLevel = 'read';
  @state() private target: 'all' | 'selected' = 'all';
  @state() private selected = new Set<string>();

  connectedCallback() {
    super.connectedCallback();
    void this.load();
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
      this.subjects = await this.loadSubjects();
    } catch (error) {
      if (isCapabilityOff(error)) this.off = true;
      else
        this.error =
          error instanceof Error ? error.message : 'Could not load grants';
    } finally {
      this.loading = false;
    }
  }

  private async loadSubjects(): Promise<SubjectOption[]> {
    const [users, teams] = await Promise.allSettled([getUsers(), getTeams()]);
    return [
      ...(users.status === 'fulfilled' ? users.value.users : []).map(
        (user) => ({
          type: 'user' as const,
          id: user.id,
          label: user.full_name || user.username || user.email,
        })
      ),
      ...(teams.status === 'fulfilled' ? teams.value.teams : []).map(
        (team) => ({
          type: 'team' as const,
          id: team.id,
          label: `Team: ${team.name}`,
        })
      ),
    ];
  }

  private subjectLabel(grant: AccessGrant): string {
    const match = this.subjects.find(
      (s) => s.type === grant.subject_type && s.id === grant.subject_id
    );
    return grant.subject_name || match?.label || grant.subject_id;
  }

  private targetLabel(grant: AccessGrant): string {
    if (grant.target === 'all') return 'All subaccounts, including new ones';
    const names = (grant.subaccount_ids ?? []).map(
      (id) => this.subaccounts.find((s) => s.id === id)?.name ?? id
    );
    return names.join(', ') || 'None';
  }

  private submit = async (event: Event) => {
    event.preventDefault();
    this.error = '';
    const [type, id] = this.subjectKey.split(':');
    if (!type || !id) {
      this.error = 'Choose a user or team.';
      return;
    }
    if (this.target === 'selected' && this.selected.size === 0) {
      this.error = 'Choose at least one subaccount.';
      return;
    }
    try {
      await createAccessGrant(this.accountId, {
        subject_type: type as 'user' | 'team',
        subject_id: id,
        level: this.level,
        target: this.target,
        ...(this.target === 'selected'
          ? { subaccount_ids: [...this.selected] }
          : {}),
      });
      this.grants = await listAccessGrants(this.accountId);
      this.subjectKey = '';
      this.selected = new Set();
    } catch (error) {
      this.fail(error);
    }
  };

  private fail(error: unknown) {
    if (isCapabilityOff(error)) {
      this.off = true;
    } else if (isNotFound(error)) {
      this.error = 'That grant is not in this account.';
      void listAccessGrants(this.accountId)
        .then((grants) => (this.grants = grants))
        .catch(() => undefined);
    } else {
      this.error = error instanceof Error ? error.message : 'Request failed';
    }
  }

  private async revoke(grant: AccessGrant) {
    const level = (LEVELS[grant.level] ?? grant.level).toLowerCase();
    const ok = await confirmDialog({
      title: 'Revoke access?',
      message: `Revoke ${this.subjectLabel(grant)}'s ${level} access?`,
      detail: `Where: ${this.targetLabel(grant)}. They lose that access right away.`,
      confirmLabel: 'Revoke',
      variant: 'danger',
    });
    if (!ok) return;
    try {
      await deleteAccessGrant(this.accountId, grant.id);
      this.grants = await listAccessGrants(this.accountId);
    } catch (error) {
      this.fail(error);
    }
  }

  private toggle(id: string, on: boolean) {
    const next = new Set(this.selected);
    if (on) next.add(id);
    else next.delete(id);
    this.selected = next;
  }

  render() {
    if (this.off) {
      // A bookmarked link on a server without the extension: say so rather
      // than leave a blank page, and raise no error toast.
      return html`
        <view-header headerText="Access grants"></view-header>
        <p class="off-state">
          Access grants aren't available on this deployment: the server does not
          have the account hierarchy extension enabled.
          <a href=${HIERARCHY_DOCS_URL} target="_blank" rel="noopener"
            >Learn about subaccounts</a
          >
        </p>
      `;
    }
    return html`
      <view-header
        headerText="Access grants"
        description="People and teams of this account who work inside subaccounts. Owners have admin everywhere; everyone else needs a grant."
      ></view-header>
      ${
        this.loading
          ? html`<sl-spinner></sl-spinner>`
          : html`
              <form @submit=${this.submit}>
                <sl-select
                  id="subject"
                  label="User or team"
                  size="small"
                  .value=${this.subjectKey}
                  @sl-change=${(e: Event) =>
                    (this.subjectKey = (e.target as HTMLSelectElement).value)}
                >
                  ${this.subjects.map(
                    (s) =>
                      html`<sl-option value=${`${s.type}:${s.id}`}
                        >${s.label}</sl-option
                      >`
                  )}
                </sl-select>
                <sl-select
                  id="level"
                  label="Level"
                  size="small"
                  .value=${this.level}
                  @sl-change=${(e: Event) =>
                    (this.level = (e.target as HTMLSelectElement)
                      .value as GrantLevel)}
                >
                  ${Object.entries(LEVELS).map(
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
                  this.target === 'selected' && this.subaccounts.length === 0
                    ? html`<p class="hint" data-testid="no-subaccounts">
                        This account has no subaccounts yet.
                        <a href="/console/settings/account"
                          >Create a subaccount first</a
                        >, or grant access in all subaccounts.
                      </p>`
                    : nothing
                }
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
                  <sl-button size="small" variant="primary" type="submit"
                    >Add grant</sl-button
                  >
                </div>
                ${
                  this.error
                    ? html`<p class="error" role="alert">${this.error}</p>`
                    : nothing
                }
              </form>
              ${
                this.grants.length === 0
                  ? html`<p class="empty-state">No grants yet.</p>`
                  : html`<div class="table-scroll">
                      <table>
                        <thead>
                          <tr>
                            <th>Who</th>
                            <th>Level</th>
                            <th>Where</th>
                            <th></th>
                          </tr>
                        </thead>
                        <tbody>
                          ${this.grants.map(
                            (grant) =>
                              html`<tr data-id=${grant.id}>
                                <td>${this.subjectLabel(grant)}</td>
                                <td>${LEVELS[grant.level] ?? grant.level}</td>
                                <td>${this.targetLabel(grant)}</td>
                                <td>
                                  <sl-button
                                    size="small"
                                    @click=${() => this.revoke(grant)}
                                    >Revoke</sl-button
                                  >
                                </td>
                              </tr>`
                          )}
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
    'access-grants-view': AccessGrantsView;
  }
}
