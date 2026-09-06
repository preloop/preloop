import { LitElement, html, css, unsafeCSS } from 'lit';
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
import consoleStyles from '../../../styles/console-styles.css?inline';
import { consoleDialogStyles } from '../../../styles/console-dialog';

@customElement('user-management-view')
export class UserManagementView extends LitElement {
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
    const value = String(name || '').trim();
    if (!value) return 'Role';
    return value
      .replace(/_/g, ' ')
      .replace(/^\w/, (character) => character.toUpperCase());
  }

  static styles = [
    unsafeCSS(consoleStyles),
    consoleDialogStyles,
    css`
      :host {
        display: block;
        padding: 2rem;
      }

      .header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        margin-bottom: 2rem;
      }

      h1 {
        margin: 0;
        font-size: 1.5rem;
        font-weight: 600;
      }

      .users-grid {
        display: grid;
        gap: 1rem;
      }

      sl-card {
        width: 100%;
      }

      .user-card-content {
        display: grid;
        grid-template-columns: auto 1fr auto;
        gap: 1rem;
        align-items: center;
      }

      .user-icon {
        font-size: 2rem;
        width: 48px;
        height: 48px;
        display: flex;
        align-items: center;
        justify-content: center;
        background: var(--sl-color-primary-50);
        border-radius: 50%;
        color: var(--sl-color-primary-600);
      }

      .user-details {
        flex: 1;
      }

      .user-name {
        font-weight: 600;
        font-size: 1rem;
        margin: 0 0 0.25rem 0;
      }

      .user-email {
        color: var(--sl-color-neutral-600);
        font-size: 0.875rem;
        margin: 0 0 0.5rem 0;
      }

      .user-meta {
        display: flex;
        gap: 0.5rem;
        flex-wrap: wrap;
      }

      .user-roles {
        display: flex;
        gap: 0.5rem;
        align-items: center;
        flex-wrap: wrap;
        margin-top: 0.5rem;
      }

      .user-roles strong {
        font-size: 0.875rem;
        color: var(--sl-color-neutral-600);
      }

      .user-actions {
        display: flex;
        gap: 0.5rem;
      }

      /* The gap DESIGN.md asks for between the ordinary actions and the
         destructive one, so Deactivate is never hit on the way to Edit. */
      .user-actions .danger-action {
        margin-left: var(--sl-spacing-large);
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

  async connectedCallback() {
    super.connectedCallback();
    try {
      const featuresResponse = await getFeatures();
      if (!featuresResponse.features?.['user_management']) {
        this.featureEnabled = false;
        this.isLoading = false;
        return;
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
    if (
      !this.newUser.username ||
      !this.newUser.email ||
      !this.newUser.password
    ) {
      return;
    }

    try {
      await createUser(this.newUser as UserCreate);
      this.isCreateModalOpen = false;
      this.newUser = {};
      await this.fetchUsers();
    } catch (error) {
      this.error =
        error instanceof Error ? error.message : 'Failed to create user';
    }
  }

  async handleEditUser() {
    if (!this.selectedUser) return;

    try {
      await updateUser(this.selectedUser.id, this.editUser);
      this.isEditModalOpen = false;
      this.selectedUser = null;
      this.editUser = {};
      await this.fetchUsers();
    } catch (error) {
      this.error =
        error instanceof Error ? error.message : 'Failed to update user';
    }
  }

  async handleDeactivateUser(user: User) {
    if (
      !confirm(
        `Are you sure you want to deactivate user "${user.username}"? This will prevent them from logging in.`
      )
    ) {
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
    this.selectedUser = user;
    this.isRoleModalOpen = true;
    try {
      this.userRoles = await getUserRoles(user.id);
    } catch (error) {
      console.error('Failed to fetch user roles:', error);
      this.userRoles = [];
    }
  }

  async handleToggleRole(roleId: string, isChecked: boolean) {
    if (!this.selectedUser) return;

    try {
      if (isChecked) {
        await assignUserRole(this.selectedUser.id, roleId);
      } else {
        await removeUserRole(this.selectedUser.id, roleId);
      }
      // Refresh user roles
      this.userRoles = await getUserRoles(this.selectedUser.id);
    } catch (error) {
      this.error =
        error instanceof Error ? error.message : 'Failed to update role';
    }
  }

  openEditModal(user: User) {
    this.selectedUser = user;
    this.editUser = {
      email: user.email,
      full_name: user.full_name || undefined,
      is_active: user.is_active,
    };
    this.isEditModalOpen = true;
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
      <div class="header">
        <h1>Users</h1>
        <sl-button
          variant="primary"
          @click=${() => (this.isCreateModalOpen = true)}
        >
          <sl-icon slot="prefix" name="person-plus"></sl-icon>
          Add user
        </sl-button>
      </div>

      ${this.error ? html`<div class="error">${this.error}</div>` : ''}

      <div class="users-grid">
        ${repeat(
          this.users,
          (user) => user.id,
          (user) => html`
            <sl-card>
              <div class="user-card-content">
                <div class="user-icon">
                  <sl-icon name="person-circle"></sl-icon>
                </div>
                <div class="user-details">
                  <h3 class="user-name">${user.full_name || user.username}</h3>
                  <p class="user-email">${user.email}</p>
                  <div class="user-meta">
                    <sl-badge
                      class="chip"
                      variant="${user.is_active ? 'success' : 'neutral'}"
                    >
                      ${user.is_active ? 'Active' : 'Inactive'}
                    </sl-badge>
                    <sl-badge class="chip" variant="neutral"
                      >${UserManagementView.userSourceLabel(
                        user.user_source
                      )}</sl-badge
                    >
                    ${
                      user.email_verified
                        ? html`<sl-badge class="chip" variant="success"
                            >Verified</sl-badge
                          >`
                        : html`<sl-badge class="chip" variant="warning"
                            >Unverified</sl-badge
                          >`
                    }
                  </div>
                  ${
                    (user as any).roles && (user as any).roles.length > 0
                      ? html`
                          <div class="user-roles">
                            <strong>Roles:</strong>
                            ${(user as any).roles.map(
                              (role: any) =>
                                html`<sl-badge class="chip" variant="neutral"
                                  >${UserManagementView.roleLabel(
                                    role.name
                                  )}</sl-badge
                                >`
                            )}
                          </div>
                        `
                      : ''
                  }
                  ${
                    (user as any).inherited_roles &&
                    (user as any).inherited_roles.length > 0
                      ? html`
                          <div class="user-roles">
                            <strong>From teams:</strong>
                            ${(user as any).inherited_roles.map(
                              (role: any) =>
                                html`<sl-badge
                                  class="chip"
                                  variant="neutral"
                                  title="From team: ${role.team_name}"
                                  >${UserManagementView.roleLabel(role.name)}
                                  <span style="font-size: 0.7em;"
                                    >(${role.team_name})</span
                                  ></sl-badge
                                >`
                            )}
                          </div>
                        `
                      : ''
                  }
                </div>
                <div class="user-actions">
                  <sl-button
                    size="small"
                    title="Manage roles"
                    @click=${() => this.openRoleModal(user)}
                  >
                    <sl-icon name="shield-check"></sl-icon>
                  </sl-button>
                  <sl-button
                    size="small"
                    title="Edit user"
                    @click=${() => this.openEditModal(user)}
                  >
                    <sl-icon name="pencil"></sl-icon>
                  </sl-button>
                  <!-- Destructive last, outline, after a gap (DESIGN.md
                       "Destructive actions"): a solid red button beside two
                       neutral ones is the loudest thing in the row. -->
                  <sl-button
                    class="danger-action"
                    size="small"
                    variant="danger"
                    outline
                    title="Deactivate user"
                    @click=${() => this.handleDeactivateUser(user)}
                  >
                    <sl-icon name="person-dash"></sl-icon>
                  </sl-button>
                </div>
              </div>
            </sl-card>
          `
        )}
      </div>

      <!-- Create User Modal -->
      <sl-dialog
        label="Create User"
        ?open=${this.isCreateModalOpen}
        @sl-request-close=${() => (this.isCreateModalOpen = false)}
      >
        <div class="form-grid">
          <sl-input
            label="Username"
            placeholder="Enter username"
            value=${this.newUser.username || ''}
            @sl-input=${(e: any) => (this.newUser.username = e.target.value)}
          ></sl-input>
          <sl-input
            label="Email"
            type="email"
            placeholder="Enter email"
            value=${this.newUser.email || ''}
            @sl-input=${(e: any) => (this.newUser.email = e.target.value)}
          ></sl-input>
          <sl-input
            label="Full Name"
            placeholder="Enter full name (optional)"
            value=${this.newUser.full_name || ''}
            @sl-input=${(e: any) => (this.newUser.full_name = e.target.value)}
          ></sl-input>
          <sl-input
            label="Password"
            type="password"
            placeholder="Enter password"
            value=${this.newUser.password || ''}
            @sl-input=${(e: any) => (this.newUser.password = e.target.value)}
            password-toggle
          ></sl-input>
        </div>
        <sl-button
          slot="footer"
          variant="primary"
          @click=${this.handleCreateUser}
        >
          Create User
        </sl-button>
        <sl-button
          slot="footer"
          variant="default"
          @click=${() => (this.isCreateModalOpen = false)}
        >
          Cancel
        </sl-button>
      </sl-dialog>

      <!-- Edit User Modal -->
      <sl-dialog
        label="Edit User"
        ?open=${this.isEditModalOpen}
        @sl-request-close=${() => (this.isEditModalOpen = false)}
      >
        <div class="form-grid">
          <sl-input
            label="Email"
            type="email"
            value=${this.editUser.email || ''}
            @sl-input=${(e: any) => (this.editUser.email = e.target.value)}
          ></sl-input>
          <sl-input
            label="Full Name"
            value=${this.editUser.full_name || ''}
            @sl-input=${(e: any) => (this.editUser.full_name = e.target.value)}
          ></sl-input>
          <sl-checkbox
            ?checked=${this.editUser.is_active}
            @sl-change=${(e: any) =>
              (this.editUser.is_active = e.target.checked)}
          >
            Active
          </sl-checkbox>
        </div>
        <sl-button
          slot="footer"
          variant="primary"
          @click=${this.handleEditUser}
        >
          Save Changes
        </sl-button>
        <sl-button
          slot="footer"
          variant="default"
          @click=${() => (this.isEditModalOpen = false)}
        >
          Cancel
        </sl-button>
      </sl-dialog>

      <!-- Manage Roles Modal -->
      <sl-dialog
        label="Manage User Roles"
        ?open=${this.isRoleModalOpen}
        @sl-request-close=${() => (this.isRoleModalOpen = false)}
      >
        <div class="role-list">
          ${this.roles.map((role) => {
            const isAssigned = this.userRoles.some((r) => r.id === role.id);
            return html`
              <div class="role-item">
                <sl-checkbox
                  ?checked=${isAssigned}
                  @sl-change=${(e: any) =>
                    this.handleToggleRole(role.id, e.target.checked)}
                >
                  ${role.name}
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
          Done
        </sl-button>
      </sl-dialog>
    `;
  }
}
