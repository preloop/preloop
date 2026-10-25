import { ConsoleStatus } from '../../../controllers/console-status';
import '@shoelace-style/shoelace/dist/components/tooltip/tooltip.js';
import { EditPermissions } from '../../../controllers/edit-permissions';
import { LitElement, html, css, nothing, unsafeCSS } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import { repeat } from 'lit/directives/repeat.js';
import {
  getTeams,
  createTeam,
  updateTeam,
  deleteTeam,
  getTeamMembers,
  addTeamMember,
  removeTeamMember,
  getUsers,
  getRoles,
  getTeamRoles,
  assignTeamRole,
  removeTeamRole,
  getFeatures,
} from '../../../api';
import type {
  Team,
  TeamCreate,
  TeamUpdate,
  TeamMember,
  User,
  Role,
} from '../../../types';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/dialog/dialog.js';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/textarea/textarea.js';
import '@shoelace-style/shoelace/dist/components/card/card.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/divider/divider.js';
import '@shoelace-style/shoelace/dist/components/icon-button/icon-button.js';
import '@shoelace-style/shoelace/dist/components/dropdown/dropdown.js';
import '@shoelace-style/shoelace/dist/components/menu/menu.js';
import '@shoelace-style/shoelace/dist/components/menu-item/menu-item.js';
import '@shoelace-style/shoelace/dist/components/select/select.js';
import '@shoelace-style/shoelace/dist/components/option/option.js';
import '@shoelace-style/shoelace/dist/components/checkbox/checkbox.js';
import '../../../components/view-header.ts';
import consoleStyles from '../../../styles/console-styles.css?inline';
import { hasCapability } from '../../../capabilities';
import type { Subaccount } from '../../../hierarchy-api';
import type { SubaccountAccessDialog } from '../hierarchy/subaccount-access-dialog';
import { consoleDialogStyles } from '../../../styles/console-dialog';
import { confirmDialog } from '../../../components/confirm-dialog';
import { roleLabel } from '../../../utils/role-label';
import { userHref } from '../../../utils/user-href';

@customElement('team-management-view')
export class TeamManagementView extends LitElement {
  private readonly accessibilityStatus = new ConsoleStatus(this);
  private readonly editPermissions = new EditPermissions(this);
  @state()
  private teams: Team[] = [];

  @state()
  private users: User[] = [];

  @state()
  private isLoading = true;

  @state()
  private error: string | null = null;

  @state()
  private isCreateModalOpen = false;

  @state()
  private isEditModalOpen = false;

  @state()
  private isMembersModalOpen = false;

  @state()
  private selectedTeam: Team | null = null;

  @state()
  private teamMembers: TeamMember[] = [];

  @state()
  private newTeam: Partial<TeamCreate> = {};

  @state()
  private editTeam: Partial<TeamUpdate> = {};

  @state()
  private selectedUserId = '';

  @state()
  private roles: Role[] = [];

  @state()
  private isRoleModalOpen = false;

  @state()
  private teamRoles: Role[] = [];

  /** Role names are stored lower case (`owner`); the chip says "Owner". */
  static roleLabel(name: string | null | undefined): string {
    return roleLabel(name);
  }

  /**
   * The failure of an action taken inside an open dialog. It renders in that
   * dialog: a page-level message would sit behind the modal that caused it.
   */
  @state()
  private dialogError: string | null = null;

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

      .empty-state {
        display: flex;
        flex-direction: column;
        align-items: center;
        gap: var(--sl-spacing-small);
        padding: var(--sl-spacing-2x-large) var(--sl-spacing-large);
        text-align: center;
        color: var(--console-meta-color, var(--sl-color-neutral-600));
      }

      .empty-state p {
        margin: 0;
      }

      /* The console table pattern (DESIGN.md "Tables and views"). */
      .teams-table td {
        vertical-align: middle;
      }

      .team-name {
        font-weight: 600;
      }

      .team-description {
        color: var(--console-meta-color);
        font-size: var(--console-text-meta);
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

      .form-grid {
        display: grid;
        gap: 1rem;
      }

      .members-list {
        display: flex;
        flex-direction: column;
        gap: 0.5rem;
        max-height: 400px;
        overflow-y: auto;
        margin-bottom: 1rem;
      }

      .member-item {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 0.75rem;
        border-radius: 4px;
        background: var(--sl-color-neutral-50);
      }

      .member-info {
        display: flex;
        flex-direction: column;
        gap: 0.25rem;
      }

      .member-name {
        font-weight: 500;
      }

      .member-email {
        font-size: 0.875rem;
        color: var(--sl-color-neutral-600);
      }

      .add-member-section {
        padding: 1rem;
        background: var(--sl-color-neutral-50);
        border-radius: 4px;
        margin-top: 1rem;
      }

      .add-member-form {
        display: flex;
        gap: 0.5rem;
        align-items: end;
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
    await Promise.all([
      this.fetchTeams(),
      this.fetchUsers(),
      this.fetchRoles(),
    ]);
  }

  async fetchTeams() {
    this.isLoading = true;
    this.error = null;
    try {
      const response = await getTeams();
      this.teams = response.teams;
    } catch (error) {
      this.error =
        error instanceof Error ? error.message : 'Failed to fetch teams';
    } finally {
      this.isLoading = false;
    }
  }

  async fetchUsers() {
    try {
      const response = await getUsers();
      this.users = response.users;
    } catch (error) {
      console.error('Failed to fetch users:', error);
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

  async handleCreateTeam() {
    if (!this.editPermissions.allows('create_teams')) return;
    if (!this.newTeam.name) {
      this.dialogError = 'Enter a team name.';
      return;
    }

    this.dialogError = null;
    try {
      await createTeam(this.newTeam as TeamCreate);
      this.isCreateModalOpen = false;
      this.newTeam = {};
      await this.fetchTeams();
    } catch (error) {
      this.dialogError =
        error instanceof Error ? error.message : 'Failed to create team';
    }
  }

  async handleEditTeam() {
    if (!this.editPermissions.allows('edit_teams')) return;
    if (!this.selectedTeam) return;

    this.dialogError = null;
    try {
      await updateTeam(this.selectedTeam.id, this.editTeam);
      this.isEditModalOpen = false;
      this.selectedTeam = null;
      this.editTeam = {};
      await this.fetchTeams();
    } catch (error) {
      this.dialogError =
        error instanceof Error ? error.message : 'Failed to update team';
    }
  }

  async handleDeleteTeam(team: Team) {
    if (!this.editPermissions.allows('delete_teams')) return;
    const confirmed = await confirmDialog({
      title: 'Delete team?',
      message: `Delete the team "${team.name}"?`,
      detail:
        'Members keep their accounts but lose any roles they had through this team. This cannot be undone.',
      confirmLabel: 'Delete team',
      variant: 'danger',
    });
    if (!confirmed) {
      return;
    }

    try {
      await deleteTeam(team.id);
      await this.fetchTeams();
    } catch (error) {
      this.error =
        error instanceof Error ? error.message : 'Failed to delete team';
    }
  }

  async openMembersModal(team: Team) {
    this.selectedTeam = team;
    this.dialogError = null;
    this.isMembersModalOpen = true;
    try {
      this.teamMembers = await getTeamMembers(team.id);
    } catch (error) {
      console.error('Failed to fetch team members:', error);
      this.teamMembers = [];
    }
  }

  async handleAddMember() {
    if (!this.editPermissions.allows('manage_teams')) return;
    if (!this.selectedTeam || !this.selectedUserId) return;

    try {
      await addTeamMember(this.selectedTeam.id, this.selectedUserId);
      this.selectedUserId = '';
      this.teamMembers = await getTeamMembers(this.selectedTeam.id);
      this.dialogError = null;
    } catch (error) {
      this.dialogError =
        error instanceof Error ? error.message : 'Failed to add team member';
    }
  }

  private memberName(userId: string): string {
    const user = this.users.find((u) => u.id === userId);
    return user?.full_name || user?.username || user?.email || 'this member';
  }

  async handleRemoveMember(userId: string) {
    if (!this.editPermissions.allows('manage_teams')) return;
    if (!this.selectedTeam) return;
    const team = this.selectedTeam;

    const confirmed = await confirmDialog({
      title: 'Remove member?',
      message: `Remove ${this.memberName(userId)} from "${team.name}"?`,
      detail: 'They lose any roles they had through this team.',
      confirmLabel: 'Remove',
      variant: 'danger',
    });
    if (!confirmed) {
      return;
    }

    try {
      await removeTeamMember(team.id, userId);
      this.teamMembers = await getTeamMembers(team.id);
      this.dialogError = null;
    } catch (error) {
      this.dialogError =
        error instanceof Error ? error.message : 'Failed to remove team member';
    }
  }

  openEditModal(team: Team) {
    if (!this.editPermissions.allows('edit_teams')) return;
    this.selectedTeam = team;
    this.editTeam = {
      name: team.name,
      description: team.description || undefined,
    };
    this.dialogError = null;
    this.isEditModalOpen = true;
  }

  private openCreateModal() {
    if (!this.editPermissions.allows('create_teams')) return;
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

  async openRoleModal(team: Team) {
    if (!this.editPermissions.allows('manage_teams')) return;
    this.selectedTeam = team;
    this.dialogError = null;
    this.isRoleModalOpen = true;
    try {
      this.teamRoles = await getTeamRoles(team.id);
    } catch (error) {
      console.error('Failed to fetch team roles:', error);
      this.teamRoles = [];
    }
  }

  async handleToggleRole(roleId: string, isChecked: boolean) {
    if (!this.editPermissions.allows('manage_teams')) return;
    if (!this.selectedTeam) return;

    try {
      if (isChecked) {
        await assignTeamRole(this.selectedTeam.id, roleId);
      } else {
        await removeTeamRole(this.selectedTeam.id, roleId);
      }
      // Refresh team roles
      this.teamRoles = await getTeamRoles(this.selectedTeam.id);
      // Refresh teams to update role display in cards
      await this.fetchTeams();
      this.dialogError = null;
    } catch (error) {
      this.dialogError =
        error instanceof Error ? error.message : 'Failed to update role';
    }
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

  private renderTeamRow(team: Team) {
    const roles = ((team as any).roles || []) as Array<{ name: string }>;
    const canManage = this.editPermissions.allows('manage_teams');
    const canEdit = this.editPermissions.allows('edit_teams');
    const canDelete = this.editPermissions.allows('delete_teams');
    return html`
      <tr data-team-id=${team.id}>
        <td class="team-name">${team.name}</td>
        <td class="team-description">${team.description || ''}</td>
        <td>
          <div class="chips">
            ${roles.map(
              (role) =>
                html`<sl-badge class="chip" pill variant="neutral"
                  >${TeamManagementView.roleLabel(role.name)}</sl-badge
                >`
            )}
          </div>
        </td>
        <td class="actions-cell">
          <sl-dropdown class="row-menu" placement="bottom-end" hoist>
            <sl-icon-button
              slot="trigger"
              name="three-dots-vertical"
              label="Actions for ${team.name}"
            ></sl-icon-button>
            <sl-menu>
              <sl-menu-item
                data-action="members"
                @click=${() => this.openMembersModal(team)}
              >
                <sl-icon slot="prefix" name="person-lines-fill"></sl-icon>
                Members
              </sl-menu-item>
              <sl-menu-item
                data-action="roles"
                ?disabled=${!canManage}
                title=${canManage ? '' : 'Requires manage_teams'}
                @click=${() => this.openRoleModal(team)}
              >
                <sl-icon slot="prefix" name="shield-check"></sl-icon>
                Manage roles
              </sl-menu-item>
              <sl-menu-item
                data-action="edit"
                ?disabled=${!canEdit}
                title=${canEdit ? '' : 'Requires edit_teams'}
                @click=${() => this.openEditModal(team)}
              >
                <sl-icon slot="prefix" name="pencil"></sl-icon>
                Edit team
              </sl-menu-item>
              ${
                this.subaccounts.length > 0
                  ? html`<sl-menu-item
                      data-action="subaccount-access"
                      data-testid="subaccount-access"
                      @click=${() =>
                        this.openSubaccountAccess({
                          type: 'team',
                          id: team.id,
                          label: team.name,
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
                data-action="delete"
                ?disabled=${!canDelete}
                title=${canDelete ? '' : 'Requires delete_teams'}
                @click=${() => this.handleDeleteTeam(team)}
              >
                <sl-icon slot="prefix" name="trash"></sl-icon>
                Delete team
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
          <p>Team management is not available in this edition.</p>
        </div>
      `;
    }

    return html`
      <view-header headerText="Teams" width="narrow">
        <div slot="main-column">
          <sl-tooltip
            content=${!this.editPermissions.allows('create_teams') ? 'Requires create_teams' : ''}
            ><sl-button
              ?disabled=${!this.editPermissions.allows('create_teams')}
              variant="primary"
              @click=${this.openCreateModal}
            >
              <sl-icon slot="prefix" name="people-fill"></sl-icon>
              Create team
            </sl-button></sl-tooltip
          >
        </div>
      </view-header>

      ${
        this.error
          ? html`<div class="error" role="alert">${this.error}</div>`
          : ''
      }
      ${
        this.teams.length === 0 && !this.error
          ? html`<div class="empty-state">
              <sl-icon
                name="people"
                style="font-size: 2.5rem;"
                aria-hidden="true"
              ></sl-icon>
              <p>
                No teams yet. Teams let several people share the same roles.
              </p>
              <sl-tooltip
                content=${!this.editPermissions.allows('create_teams') ? 'Requires create_teams' : ''}
                ><sl-button
                  ?disabled=${!this.editPermissions.allows('create_teams')}
                  size="small"
                  @click=${this.openCreateModal}
                >
                  Create team
                </sl-button></sl-tooltip
              >
            </div>`
          : ''
      }
      ${
        this.teams.length > 0
          ? html`<sl-card class="table-card">
              <div class="table-scroll">
                <table class="styled-table teams-table" aria-label="Teams">
                  <thead>
                    <tr>
                      <th scope="col">Name</th>
                      <th scope="col">Description</th>
                      <th scope="col">Roles</th>
                      <th scope="col"><span class="sr-only">Actions</span></th>
                    </tr>
                  </thead>
                  <tbody>
                    ${repeat(
                      this.teams,
                      (team) => team.id,
                      (team) => this.renderTeamRow(team)
                    )}
                  </tbody>
                </table>
              </div>
            </sl-card>`
          : nothing
      }
      ${
        this.subaccounts.length > 0
          ? html`<subaccount-access-dialog></subaccount-access-dialog>`
          : ''
      }

      <!-- Create team modal -->
      <sl-dialog
        label="Create team"
        ?open=${this.isCreateModalOpen}
        @sl-request-close=${() => (this.isCreateModalOpen = false)}
      >
        ${this.renderDialogError()}
        <div class="form-grid">
          <sl-input
            label="Team name"
            required
            placeholder="Enter team name"
            value=${this.newTeam.name || ''}
            @sl-input=${(e: any) => (this.newTeam.name = e.target.value)}
          ></sl-input>
          <sl-textarea
            label="Description"
            placeholder="Enter team description (optional)"
            value=${this.newTeam.description || ''}
            @sl-input=${(e: any) => (this.newTeam.description = e.target.value)}
          ></sl-textarea>
        </div>
        <sl-tooltip
          content=${!this.editPermissions.allows('create_teams') ? 'Requires create_teams' : ''}
          ><sl-button
            ?disabled=${!this.editPermissions.allows('create_teams')}
            variant="primary"
            @click=${this.handleCreateTeam}
          >
            Create team
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

      <!-- Edit team modal -->
      <sl-dialog
        label="Edit team"
        ?open=${this.isEditModalOpen}
        @sl-request-close=${() => (this.isEditModalOpen = false)}
      >
        ${this.renderDialogError()}
        <div class="form-grid">
          <sl-input
            label="Team name"
            value=${this.editTeam.name || ''}
            @sl-input=${(e: any) => (this.editTeam.name = e.target.value)}
          ></sl-input>
          <sl-textarea
            label="Description"
            value=${this.editTeam.description || ''}
            @sl-input=${(e: any) =>
              (this.editTeam.description = e.target.value)}
          ></sl-textarea>
        </div>
        <sl-tooltip
          slot="footer"
          content=${!this.editPermissions.allows('edit_teams') ? 'Requires edit_teams' : ''}
          ><sl-button
            ?disabled=${!this.editPermissions.allows('edit_teams')}
            variant="primary"
            @click=${this.handleEditTeam}
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

      <!-- Team members modal -->
      <sl-dialog
        label="Team members"
        ?open=${this.isMembersModalOpen}
        @sl-request-close=${() => (this.isMembersModalOpen = false)}
      >
        ${this.renderDialogError()}
        <div class="members-list">
          ${
            this.teamMembers.length === 0
              ? html`<p>No members in this team yet.</p>`
              : this.teamMembers.map((member) => {
                  const user = this.users.find((u) => u.id === member.user_id);
                  return html`
                    <div class="member-item">
                      <div class="member-info">
                        <span class="member-name">
                          <a href=${userHref(member.user_id)}
                            >${user?.full_name || user?.username || 'Identity unavailable'}</a
                          >
                        </span>
                        <span class="member-email">${user?.email || ''}</span>
                      </div>
                      <sl-tooltip
                        content=${!this.editPermissions.allows('manage_teams') ? 'Requires manage_teams' : ''}
                        ><sl-button
                          ?disabled=${!this.editPermissions.allows('manage_teams')}
                          size="small"
                          variant="danger"
                          outline
                          title="Remove from team"
                          @click=${() => this.handleRemoveMember(member.user_id)}
                        >
                          <sl-icon
                            name="x-lg"
                            label="Remove from team"
                          ></sl-icon> </sl-button
                      ></sl-tooltip>
                    </div>
                  `;
                })
          }
        </div>

        <div class="add-member-section">
          <h4>Add member</h4>
          <div class="add-member-form">
            <sl-select
              aria-label="User to add"
              placeholder="Select user"
              value=${this.selectedUserId}
              @sl-change=${(e: any) => (this.selectedUserId = e.target.value)}
              style="flex: 1;"
            >
              ${this.users
                .filter(
                  (u) => !this.teamMembers.some((m) => m.user_id === u.id)
                )
                .map(
                  (user) => html`
                    <sl-option value=${user.id}>
                      ${user.full_name || user.username || 'Identity unavailable'}
                      ${user.email ? `(${user.email})` : ''}
                    </sl-option>
                  `
                )}
            </sl-select>
            <sl-tooltip
              content=${!this.editPermissions.allows('manage_teams') ? 'Requires manage_teams' : ''}
              ><sl-button
                ?disabled=${!this.editPermissions.allows('manage_teams')}
                @click=${this.handleAddMember}
                >Add</sl-button
              ></sl-tooltip
            >
          </div>
        </div>

        <sl-button
          slot="footer"
          variant="primary"
          @click=${() => (this.isMembersModalOpen = false)}
        >
          Close
        </sl-button>
      </sl-dialog>

      <!-- Manage team roles modal -->
      <sl-dialog
        label="Manage team roles"
        ?open=${this.isRoleModalOpen}
        @sl-request-close=${() => (this.isRoleModalOpen = false)}
      >
        ${this.renderDialogError()}
        <div class="role-list">
          ${this.roles.map((role) => {
            const isAssigned = this.teamRoles.some((r) => r.id === role.id);
            return html`
              <div class="role-item">
                <sl-checkbox
                  ?disabled=${!this.editPermissions.allows('manage_teams')}
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
