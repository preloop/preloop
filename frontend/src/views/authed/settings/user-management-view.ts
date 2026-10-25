import { ConsoleStatus } from '../../../controllers/console-status';
import '@shoelace-style/shoelace/dist/components/tooltip/tooltip.js';
import { EditPermissions } from '../../../controllers/edit-permissions';
import { LitElement, html, css, nothing, unsafeCSS } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import { repeat } from 'lit/directives/repeat.js';
import {
  getUsers,
  createUser,
  updateUser,
  deactivateUser,
  getRoles,
  getUserRoles,
  assignUserRole,
  removeUserRole,
  getFeatures,
} from '../../../api';
import type { User, UserCreate, UserUpdate, Role } from '../../../types';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/dialog/dialog.js';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/card/card.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/dropdown/dropdown.js';
import '@shoelace-style/shoelace/dist/components/menu/menu.js';
import '@shoelace-style/shoelace/dist/components/menu-item/menu-item.js';
import '@shoelace-style/shoelace/dist/components/checkbox/checkbox.js';
import '@shoelace-style/shoelace/dist/components/divider/divider.js';
import '@shoelace-style/shoelace/dist/components/icon-button/icon-button.js';
import '../../../components/view-header.ts';
import consoleStyles from '../../../styles/console-styles.css?inline';
import { hasCapability } from '../../../capabilities';
import type { Subaccount } from '../../../hierarchy-api';
import type { SubaccountAccessDialog } from '../hierarchy/subaccount-access-dialog';
import { consoleDialogStyles } from '../../../styles/console-dialog';
import { confirmDialog } from '../../../components/confirm-dialog';
import { roleLabel } from '../../../utils/role-label';
import { userHref } from '../../../utils/user-href';
import { formatLocalDateTime, formatRelativeTime } from '../../../utils/date';

@customElement('user-management-view')
export class UserManagementView extends LitElement {
  private readonly accessibilityStatus = new ConsoleStatus(this);
  private readonly editPermissions = new EditPermissions(this);
  @state()
  private users: User[] = [];

  @state()
  private roles: Role[] = [];

  @state()
  private isLoading = true;

  @state()
  private error: string | null = null;

  @state()
  private isCreateModalOpen = false;

  @state()
  private isEditModalOpen = false;

  @state()
  private isRoleModalOpen = false;

  @state()
  private selectedUser: User | null = null;

  @state()
  private userRoles: Role[] = [];

  @state()
  private newUser: Partial<UserCreate> = {};

  @state()
  private editUser: Partial<UserUpdate> = {};

  /**
   * The failure of an action taken inside an open dialog. It renders in that
   * dialog: a page-level message would sit behind the modal that caused it.
   */
  @state()
  private dialogError: string | null = null;

  /**
   * How the account was created, in words. The stored values are enum names
   * (`local`, `oauth_google`); printing them verbatim asked the reader to
   * know the schema to learn that someone signs in with Google.
   */
  static userSourceLabel(source: string | null | undefined): string {
    const value = String(source || '').toLowerCase();
    if (value === 'local' || value === 'password') return 'Password';
    if (value === 'oauth_google' || value === 'google') return 'Google';
    if (value === 'oauth_github' || value === 'github') return 'GitHub';
    if (value === 'oauth_microsoft' || value === 'microsoft') {
      return 'Microsoft';
    }
    if (value === 'saml' || value === 'sso') return 'SSO';
    if (value === 'invitation') return 'Invitation';
    if (!value) return 'Unknown';
    return value
      .replace(/^oauth_/, '')
      .replace(/_/g, ' ')
      .replace(/^\w/, (character) => character.toUpperCase());
  }

  /** Role names are stored lower case (`owner`); the chip says "Owner". */
  static roleLabel(name: string | null | undefined): string {
    return roleLabel(name);
  }

  static styles = [
    unsafeCSS(consoleStyles),
    consoleDialogStyles,
    css`
      /* No page padding here: the shell pays the side inset
         (styles/console-styles.css, "The page box"). */
      :host {
        display: block;
      }

      .dialog-error {
        margin-bottom: var(--sl-spacing-medium);
      }

      /* The console table pattern (DESIGN.md "Tables and views"):
         hairline rows, the name a real link, chips as tints and the row
         actions behind one kebab with the destructive one last. */
      .users-table td {
        vertical-align: middle;
      }

      .users-table tr.linked td {
        box-shadow: inset 0 -2px 0 var(--sl-color-primary-600);
      }

      .user-name {
        font-weight: 600;
      }

      .user-name a {
        color: var(--sl-color-primary-600);
        text-decoration: none;
      }

      .user-name a:hover {
        text-decoration: underline;
      }

      .identity-note,
      .user-email,
      .teams-cell,
      .last-login {
        color: var(--console-meta-color);
        font-size: var(--console-text-meta);
      }

      .identity-note {
        margin: 2px 0 0;
      }

      .chips {
        display: flex;
        gap: var(--sl-spacing-2x-small);
        flex-wrap: wrap;
      }

      .actions-cell {
        width: 1%;
        white-space: nowrap;
      }

      .sr-only {
        position: absolute;
        width: 1px;
        height: 1px;
        overflow: hidden;
        clip: rect(0 0 0 0);
        white-space: nowrap;
      }

      .empty-row {
        text-align: center;
        color: var(--console-meta-color);
        padding: var(--sl-spacing-x-large);
      }

      .form-grid {
        display: grid;
        gap: 1rem;
      }

      .role-list {
        display: flex;
        flex-direction: column;
        gap: 0.5rem;
        max-height: 300px;
        overflow-y: auto;
      }

      .role-item {
        display: flex;
        align-items: center;
        gap: 0.5rem;
        padding: 0.5rem;
        border-radius: 4px;
        background: var(--sl-color-neutral-50);
      }

      .error {
        color: var(--sl-color-danger-600);
        background: var(--sl-color-danger-50);
        padding: 1rem;
        border-radius: 4px;
        margin-bottom: 1rem;
      }

      .loading {
        display: flex;
        justify-content: center;
        align-items: center;
        padding: 4rem;
      }
    `,
  ];

  @state()
  private featureEnabled = true;

  /**
   * The account's subaccounts (capability `account_hierarchy`). With at
   * least one, each row offers "Subaccount access".
   */
  @state()
  private subaccounts: Subaccount[] = [];

  async connectedCallback() {
    super.connectedCallback();
    try {
      const featuresResponse = await getFeatures();
      if (!featuresResponse.features?.['user_management']) {
        this.featureEnabled = false;
        this.isLoading = false;
        return;
      }
      if (hasCapability(featuresResponse.features, 'account_hierarchy')) {
        // Loaded on demand, so the gated hierarchy code stays out of this
        // chunk where the capability is off.
        void import('../hierarchy/subaccount-access-dialog')
          .then((module) => module.subaccountsOfCurrentAccount())
          .then((subaccounts) => (this.subaccounts = subaccounts))
          .catch(() => undefined);
      }
    } catch {
      // If features endpoint fails, proceed optimistically
    }
    await Promise.all([this.fetchUsers(), this.fetchRoles()]);
  }

  async fetchUsers() {
    this.isLoading = true;
    this.error = null;
    try {
      const response = await getUsers();
      this.users = response.users;
    } catch (error) {
      this.error =
        error instanceof Error ? error.message : 'Failed to fetch users';
    } finally {
      this.isLoading = false;
    }
  }

  async fetchRoles() {
    try {
      const response = await getRoles();
      this.roles = response.roles;
    } catch (error) {
      console.error('Failed to fetch roles:', error);
    }
  }

  async handleCreateUser() {
    if (!this.editPermissions.allows('manage_users')) return;
    const missing = [
      !this.newUser.username ? 'a username' : null,
      !this.newUser.email ? 'an email' : null,
      !this.newUser.password ? 'a password' : null,
    ].filter(Boolean);
    if (missing.length > 0) {
      this.dialogError = `Enter ${missing.join(', ').replace(/, ([^,]*)$/, ' and $1')}.`;
      return;
    }

    this.dialogError = null;
    try {
      await createUser(this.newUser as UserCreate);
      this.isCreateModalOpen = false;
      this.newUser = {};
      await this.fetchUsers();
    } catch (error) {
      this.dialogError =
        error instanceof Error ? error.message : 'Failed to create user';
    }
  }

  async handleEditUser() {
    if (!this.editPermissions.allows('manage_users')) return;
    if (!this.selectedUser) return;

    this.dialogError = null;
    try {
      await updateUser(this.selectedUser.id, this.editUser);
      this.isEditModalOpen = false;
      this.selectedUser = null;
      this.editUser = {};
      await this.fetchUsers();
    } catch (error) {
      this.dialogError =
        error instanceof Error ? error.message : 'Failed to update user';
    }
  }

  async handleDeactivateUser(user: User) {
    if (!this.editPermissions.allows('manage_users')) return;
    const confirmed = await confirmDialog({
      title: 'Deactivate account membership?',
      message: `Deactivate ${user.full_name || user.username || 'Identity unavailable'}${user.email ? ` (${user.email})` : ''}? Membership: ${user.id}.`,
      detail:
        'They lose access to this account. Other account memberships and their shared identity are retained. Reactivate access from Edit account membership.',
      confirmLabel: 'Deactivate',
      variant: 'danger',
    });
    if (!confirmed) {
      return;
    }

    try {
      await deactivateUser(user.id);
      await this.fetchUsers();
    } catch (error) {
      this.error =
        error instanceof Error ? error.message : 'Failed to deactivate user';
    }
  }

  async openRoleModal(user: User) {
    if (!this.editPermissions.allows('assign_roles')) return;
    this.selectedUser = user;
    this.dialogError = null;
    this.isRoleModalOpen = true;
    try {
      this.userRoles = await getUserRoles(user.id);
    } catch (error) {
      console.error('Failed to fetch user roles:', error);
      this.userRoles = [];
    }
  }

  async handleToggleRole(roleId: string, isChecked: boolean) {
    if (!this.editPermissions.allows('assign_roles')) return;
    if (!this.selectedUser) return;

    try {
      if (isChecked) {
        await assignUserRole(this.selectedUser.id, roleId);
      } else {
        await removeUserRole(this.selectedUser.id, roleId);
      }
      // Refresh user roles
      this.userRoles = await getUserRoles(this.selectedUser.id);
      this.dialogError = null;
    } catch (error) {
      this.dialogError =
        error instanceof Error ? error.message : 'Failed to update role';
    }
  }

  openEditModal(user: User) {
    if (!this.editPermissions.allows('manage_users')) return;
    this.selectedUser = user;
    this.editUser = {
      is_active: user.is_active,
    };
    this.dialogError = null;
    this.isEditModalOpen = true;
  }

  private openCreateModal() {
    if (!this.editPermissions.allows('manage_users')) return;
    this.dialogError = null;
    this.isCreateModalOpen = true;
  }

  private renderDialogError() {
    return this.dialogError
      ? html`<sl-alert class="dialog-error" variant="danger" open role="alert">
          <sl-icon slot="icon" name="exclamation-octagon"></sl-icon>
          ${this.dialogError}
        </sl-alert>`
      : '';
  }

  private async openSubaccountAccess(subject: {
    type: 'user' | 'team';
    id: string;
    label: string;
  }) {
    const dialog = this.renderRoot.querySelector(
      'subaccount-access-dialog'
    ) as SubaccountAccessDialog | null;
    await dialog?.show(subject);
  }

  /**
   * `?user=<id>` from a link elsewhere (a device's people, say). The router constructs
   * the view before it writes the destination URL and reuses it between
   * queries, so the id comes from the route, not from construction time.
   */
  @state() private linkedUserId = '';
  private linkedUserShown = false;

  onBeforeEnter(context: { search: string }): void {
    this.linkedUserId = new URLSearchParams(context.search).get('user') || '';
    this.linkedUserShown = false;
  }

  updated(): void {
    if (!this.linkedUserId || this.linkedUserShown) return;
    const row = this.shadowRoot?.querySelector(
      `tr[data-user-id="${CSS.escape(this.linkedUserId)}"]`
    );
    if (!row) return;
    this.linkedUserShown = true;
    row.scrollIntoView?.({ block: 'center' });
  }

  /** The team names a person holds roles through, once each. */
  static teamNames(user: User): string[] {
    const inherited = ((user as any).inherited_roles || []) as Array<{
      team_name?: string;
    }>;
    return [
      ...new Set(
        inherited
          .map((role) => role.team_name || '')
          .filter((name) => name.length > 0)
      ),
    ];
  }

  private displayName(user: User): string {
    return (
      user.full_name || user.username || user.email || 'Identity unavailable'
    );
  }

  private renderUserRow(user: User) {
    const linked = user.id === this.linkedUserId;
    const roles = ((user as any).roles || []) as Array<{ name: string }>;
    // A role held directly and through a team is one role: the team copy
    // would only repeat the chip (the Teams column says where it came from).
    const direct = new Set(roles.map((role) => role.name));
    const inherited = (
      ((user as any).inherited_roles || []) as Array<{
        name: string;
        team_name?: string;
      }>
    ).filter(
      (role, index, all) =>
        !direct.has(role.name) &&
        all.findIndex((other) => other.name === role.name) === index
    );
    const teams = UserManagementView.teamNames(user);
    const canManage = this.editPermissions.allows('manage_users');
    const canAssign = this.editPermissions.allows('assign_roles');
    return html`
      <tr
        data-user-id=${user.id}
        class=${linked ? 'linked' : ''}
        aria-current=${linked ? 'true' : 'false'}
      >
        <td>
          <div class="user-name">
            <a href=${userHref(user.id)}
              >${user.full_name || user.username || 'Identity unavailable'}</a
            >
          </div>
          ${
            user.identity_status === 'quarantined'
              ? html`<p class="identity-note">
                  Identity requires migration conflict resolution by a platform
                  administrator.
                </p>`
              : nothing
          }
        </td>
        <td class="user-email">${user.email || ''}</td>
        <td>
          <div class="chips">
            <sl-badge
              class="chip"
              pill
              variant=${user.is_active ? 'success' : 'neutral'}
              >${user.is_active ? 'Active' : 'Inactive'}</sl-badge
            >
            ${
              user.email_verified
                ? nothing
                : html`<sl-badge
                    class="chip"
                    pill
                    variant="warning"
                    title="This email address has not been confirmed"
                    >Unverified</sl-badge
                  >`
            }
          </div>
        </td>
        <td>
          <sl-badge class="chip" pill variant="neutral"
            >${UserManagementView.userSourceLabel(user.user_source)}</sl-badge
          >
        </td>
        <td class="roles-cell">
          <div class="chips">
            ${roles.map(
              (role) =>
                html`<sl-badge class="chip" pill variant="neutral"
                  >${UserManagementView.roleLabel(role.name)}</sl-badge
                >`
            )}
            ${inherited.map(
              (role) =>
                html`<sl-badge
                  class="chip"
                  pill
                  variant="neutral"
                  title="From team: ${role.team_name || ''}"
                  >${UserManagementView.roleLabel(role.name)}</sl-badge
                >`
            )}
          </div>
        </td>
        <td class="teams-cell">${teams.join(', ')}</td>
        <td class="last-login">
          ${
            user.last_login
              ? html`<span title=${formatLocalDateTime(user.last_login)}
                  >${formatRelativeTime(user.last_login, new Date(), {
                    maxRelativeDays: Number.POSITIVE_INFINITY,
                  })}</span
                >`
              : 'Never'
          }
        </td>
        <td class="actions-cell">
          <sl-dropdown class="row-menu" placement="bottom-end" hoist>
            <sl-icon-button
              slot="trigger"
              name="three-dots-vertical"
              label="Actions for ${this.displayName(user)}"
            ></sl-icon-button>
            <sl-menu>
              <sl-menu-item
                data-action="roles"
                ?disabled=${!canAssign}
                title=${canAssign ? '' : 'Requires assign_roles'}
                @click=${() => this.openRoleModal(user)}
              >
                <sl-icon slot="prefix" name="shield-check"></sl-icon>
                Manage roles
              </sl-menu-item>
              <sl-menu-item
                data-action="edit"
                ?disabled=${!canManage}
                title=${canManage ? '' : 'Requires manage_users'}
                @click=${() => this.openEditModal(user)}
              >
                <sl-icon slot="prefix" name="pencil"></sl-icon>
                Edit account membership
              </sl-menu-item>
              ${
                this.subaccounts.length > 0
                  ? html`<sl-menu-item
                      data-action="subaccount-access"
                      data-testid="subaccount-access"
                      @click=${() =>
                        this.openSubaccountAccess({
                          type: 'user',
                          id: user.id,
                          label: this.displayName(user),
                        })}
                    >
                      <sl-icon slot="prefix" name="diagram-3"></sl-icon>
                      Subaccount access
                    </sl-menu-item>`
                  : nothing
              }
              <sl-divider></sl-divider>
              <sl-menu-item
                class="danger-item"
                data-action="deactivate"
                ?disabled=${!canManage}
                title=${canManage ? '' : 'Requires manage_users'}
                @click=${() => this.handleDeactivateUser(user)}
              >
                <sl-icon slot="prefix" name="person-dash"></sl-icon>
                Deactivate account membership
              </sl-menu-item>
            </sl-menu>
          </sl-dropdown>
        </td>
      </tr>
    `;
  }

  render() {
    if (this.isLoading) {
      return html`
        <div class="loading">
          <sl-spinner style="font-size: 3rem;"></sl-spinner>
        </div>
      `;
    }

    if (!this.featureEnabled) {
      return html`
        <div class="loading">
          <p>User management is not available in this edition.</p>
        </div>
      `;
    }

    return html`
      <view-header headerText="Users" width="narrow">
        <div slot="main-column">
          <sl-tooltip
            content=${!this.editPermissions.allows('manage_users') ? 'Requires manage_users' : ''}
            ><sl-button
              ?disabled=${!this.editPermissions.allows('manage_users')}
              variant="primary"
              @click=${this.openCreateModal}
            >
              <sl-icon slot="prefix" name="person-plus"></sl-icon>
              Add user
            </sl-button></sl-tooltip
          >
        </div>
      </view-header>

      ${
        this.error
          ? html`<div class="error" role="alert">${this.error}</div>`
          : ''
      }

      <sl-card class="table-card">
        <div class="table-scroll">
          <table class="styled-table users-table" aria-label="Users">
            <thead>
              <tr>
                <th scope="col">Name</th>
                <th scope="col">Email</th>
                <th scope="col">Status</th>
                <th scope="col">Sign-in</th>
                <th scope="col">Roles</th>
                <th scope="col">Teams</th>
                <th scope="col">Last login</th>
                <th scope="col"><span class="sr-only">Actions</span></th>
              </tr>
            </thead>
            <tbody>
              ${
                this.users.length === 0
                  ? html`<tr>
                      <td class="empty-row" colspan="8">No users yet.</td>
                    </tr>`
                  : repeat(
                      this.users,
                      (user) => user.id,
                      (user) => this.renderUserRow(user)
                    )
              }
            </tbody>
          </table>
        </div>
      </sl-card>

      ${
        this.subaccounts.length > 0
          ? html`<subaccount-access-dialog></subaccount-access-dialog>`
          : ''
      }

      <!-- Create user modal -->
      <sl-dialog
        label="Create user"
        ?open=${this.isCreateModalOpen}
        @sl-request-close=${() => (this.isCreateModalOpen = false)}
      >
        ${this.renderDialogError()}
        <div class="form-grid">
          <sl-input
            label="Username"
            required
            placeholder="Enter username"
            value=${this.newUser.username || ''}
            @sl-input=${(e: any) => (this.newUser.username = e.target.value)}
          ></sl-input>
          <sl-input
            label="Email"
            type="email"
            required
            placeholder="Enter email"
            value=${this.newUser.email || ''}
            @sl-input=${(e: any) => (this.newUser.email = e.target.value)}
          ></sl-input>
          <sl-input
            label="Full name"
            placeholder="Enter full name (optional)"
            value=${this.newUser.full_name || ''}
            @sl-input=${(e: any) => (this.newUser.full_name = e.target.value)}
          ></sl-input>
          <sl-input
            label="Password"
            type="password"
            required
            placeholder="Enter password"
            value=${this.newUser.password || ''}
            @sl-input=${(e: any) => (this.newUser.password = e.target.value)}
            password-toggle
          ></sl-input>
        </div>
        <sl-tooltip
          slot="footer"
          content=${!this.editPermissions.allows('manage_users') ? 'Requires manage_users' : ''}
          ><sl-button
            ?disabled=${!this.editPermissions.allows('manage_users')}
            variant="primary"
            @click=${this.handleCreateUser}
          >
            Create user
          </sl-button></sl-tooltip
        >
        <sl-button
          slot="footer"
          variant="default"
          @click=${() => (this.isCreateModalOpen = false)}
        >
          Cancel
        </sl-button>
      </sl-dialog>

      <!-- Edit user modal -->
      <sl-dialog
        label="Edit account membership"
        ?open=${this.isEditModalOpen}
        @sl-request-close=${() => (this.isEditModalOpen = false)}
      >
        ${this.renderDialogError()}
        <div class="form-grid">
          <p>
            ${this.selectedUser?.full_name || this.selectedUser?.username || 'Identity unavailable'}
            ${this.selectedUser?.email ? `(${this.selectedUser.email})` : ''}.
            Membership: ${this.selectedUser?.id}
          </p>
          <p>
            Shared identity and credentials are managed by the person in
            Profile. These changes affect access to this account.
          </p>
          <sl-checkbox
            ?disabled=${!this.editPermissions.allows('manage_users')}
            ?checked=${this.editUser.is_active}
            @sl-change=${(e: any) =>
              (this.editUser.is_active = e.target.checked)}
          >
            Active account membership
          </sl-checkbox>
        </div>
        <sl-tooltip
          slot="footer"
          content=${!this.editPermissions.allows('manage_users') ? 'Requires manage_users' : ''}
          ><sl-button
            ?disabled=${!this.editPermissions.allows('manage_users')}
            variant="primary"
            @click=${this.handleEditUser}
          >
            Save changes
          </sl-button></sl-tooltip
        >
        <sl-button
          slot="footer"
          variant="default"
          @click=${() => (this.isEditModalOpen = false)}
        >
          Cancel
        </sl-button>
      </sl-dialog>

      <!-- Manage roles modal -->
      <sl-dialog
        label="Manage roles"
        ?open=${this.isRoleModalOpen}
        @sl-request-close=${() => (this.isRoleModalOpen = false)}
      >
        ${this.renderDialogError()}
        <div class="role-list">
          ${this.roles.map((role) => {
            const isAssigned = this.userRoles.some((r) => r.id === role.id);
            return html`
              <div class="role-item">
                <sl-checkbox
                  ?disabled=${!this.editPermissions.allows('assign_roles')}
                  ?checked=${isAssigned}
                  @sl-change=${(e: any) =>
                    this.handleToggleRole(role.id, e.target.checked)}
                >
                  ${roleLabel(role.name)}
                </sl-checkbox>
                ${
                  role.description
                    ? html`<span
                        style="font-size: 0.875rem; color: var(--sl-color-neutral-600);"
                      >
                        ${role.description}
                      </span>`
                    : ''
                }
              </div>
            `;
          })}
        </div>
        <sl-button
          slot="footer"
          variant="primary"
          @click=${() => (this.isRoleModalOpen = false)}
        >
          Close
        </sl-button>
      </sl-dialog>
    `;
  }
}
