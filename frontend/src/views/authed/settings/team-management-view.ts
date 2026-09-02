import { LitElement, html, css } from 'lit';
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
import '@shoelace-style/shoelace/dist/components/select/select.js';
import '@shoelace-style/shoelace/dist/components/option/option.js';
import '@shoelace-style/shoelace/dist/components/checkbox/checkbox.js';
import { consoleDialogStyles } from '../../../styles/console-dialog';

@customElement('team-management-view')
export class TeamManagementView extends LitElement {
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

  static styles = [
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

      .teams-grid {
        display: grid;
        gap: 1rem;
      }

      sl-card {
        width: 100%;
      }

      .team-card-content {
        display: grid;
        grid-template-columns: auto 1fr auto;
        gap: 1rem;
        align-items: center;
      }

      .team-icon {
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

      .team-details {
        flex: 1;
      }

      .team-name {
        font-weight: 600;
        font-size: 1rem;
        margin: 0 0 0.25rem 0;
      }

      .team-description {
        color: var(--sl-color-neutral-600);
        font-size: 0.875rem;
        margin: 0 0 0.5rem 0;
      }

      .team-roles {
        display: flex;
        gap: 0.5rem;
        align-items: center;
        flex-wrap: wrap;
        margin-top: 0.5rem;
      }

      .team-roles strong {
        font-size: 0.875rem;
        color: var(--sl-color-neutral-600);
      }

      .team-actions {
        display: flex;
        gap: 0.5rem;
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
    if (!this.newTeam.name) {
      return;
    }

    try {
      await createTeam(this.newTeam as TeamCreate);
      this.isCreateModalOpen = false;
      this.newTeam = {};
      await this.fetchTeams();
    } catch (error) {
      this.error =
        error instanceof Error ? error.message : 'Failed to create team';
    }
  }

  async handleEditTeam() {
    if (!this.selectedTeam) return;

    try {
      await updateTeam(this.selectedTeam.id, this.editTeam);
      this.isEditModalOpen = false;
      this.selectedTeam = null;
      this.editTeam = {};
      await this.fetchTeams();
    } catch (error) {
      this.error =
        error instanceof Error ? error.message : 'Failed to update team';
    }
  }

  async handleDeleteTeam(team: Team) {
    if (!confirm(`Are you sure you want to delete team "${team.name}"?`)) {
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
    this.isMembersModalOpen = true;
    try {
      this.teamMembers = await getTeamMembers(team.id);
    } catch (error) {
      console.error('Failed to fetch team members:', error);
      this.teamMembers = [];
    }
  }

  async handleAddMember() {
    if (!this.selectedTeam || !this.selectedUserId) return;

    try {
      await addTeamMember(this.selectedTeam.id, this.selectedUserId);
      this.selectedUserId = '';
      this.teamMembers = await getTeamMembers(this.selectedTeam.id);
    } catch (error) {
      this.error =
        error instanceof Error ? error.message : 'Failed to add team member';
    }
  }

  async handleRemoveMember(userId: string) {
    if (!this.selectedTeam) return;

    if (!confirm('Are you sure you want to remove this member?')) {
      return;
    }

    try {
      await removeTeamMember(this.selectedTeam.id, userId);
      this.teamMembers = await getTeamMembers(this.selectedTeam.id);
    } catch (error) {
      this.error =
        error instanceof Error ? error.message : 'Failed to remove team member';
    }
  }

  openEditModal(team: Team) {
    this.selectedTeam = team;
    this.editTeam = {
      name: team.name,
      description: team.description || undefined,
    };
    this.isEditModalOpen = true;
  }

  async openRoleModal(team: Team) {
    this.selectedTeam = team;
    this.isRoleModalOpen = true;
    try {
      this.teamRoles = await getTeamRoles(team.id);
    } catch (error) {
      console.error('Failed to fetch team roles:', error);
      this.teamRoles = [];
    }
  }

  async handleToggleRole(roleId: string, isChecked: boolean) {
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
    } catch (error) {
      this.error =
        error instanceof Error ? error.message : 'Failed to update role';
    }
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
      <div class="header">
        <h1>Team Management</h1>
        <sl-button
          variant="primary"
          @click=${() => (this.isCreateModalOpen = true)}
        >
          <sl-icon slot="prefix" name="people-fill"></sl-icon>
          Create Team
        </sl-button>
      </div>

      ${this.error ? html`<div class="error">${this.error}</div>` : ''}

      <div class="teams-grid">
        ${repeat(
          this.teams,
          (team) => team.id,
          (team) => html`
            <sl-card>
              <div class="team-card-content">
                <div class="team-icon">
                  <sl-icon name="people-fill"></sl-icon>
                </div>
                <div class="team-details">
                  <h3 class="team-name">${team.name}</h3>
                  ${
                    team.description
                      ? html`<p class="team-description">
                          ${team.description}
                        </p>`
                      : ''
                  }
                  ${
                    (team as any).roles && (team as any).roles.length > 0
                      ? html`
                          <div class="team-roles">
                            <strong>Roles:</strong>
                            ${(team as any).roles.map(
                              (role: any) =>
                                html`<sl-badge variant="primary"
                                  >${role.name}</sl-badge
                                >`
                            )}
                          </div>
                        `
                      : ''
                  }
                </div>
                <div class="team-actions">
                  <sl-button
                    size="small"
                    @click=${() => this.openRoleModal(team)}
                  >
                    <sl-icon name="shield-check"></sl-icon>
                  </sl-button>
                  <sl-button
                    size="small"
                    @click=${() => this.openMembersModal(team)}
                  >
                    <sl-icon name="person-lines-fill"></sl-icon>
                  </sl-button>
                  <sl-button
                    size="small"
                    @click=${() => this.openEditModal(team)}
                  >
                    <sl-icon name="pencil"></sl-icon>
                  </sl-button>
                  <sl-button
                    size="small"
                    variant="danger"
                    @click=${() => this.handleDeleteTeam(team)}
                  >
                    <sl-icon name="trash"></sl-icon>
                  </sl-button>
                </div>
              </div>
            </sl-card>
          `
        )}
      </div>

      <!-- Create Team Modal -->
      <sl-dialog
        label="Create Team"
        ?open=${this.isCreateModalOpen}
        @sl-request-close=${() => (this.isCreateModalOpen = false)}
      >
        <div class="form-grid">
          <sl-input
            label="Team Name"
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
        <sl-button
          slot="footer"
          variant="primary"
          @click=${this.handleCreateTeam}
        >
          Create Team
        </sl-button>
        <sl-button
          slot="footer"
          variant="default"
          @click=${() => (this.isCreateModalOpen = false)}
        >
          Cancel
        </sl-button>
      </sl-dialog>

      <!-- Edit Team Modal -->
      <sl-dialog
        label="Edit Team"
        ?open=${this.isEditModalOpen}
        @sl-request-close=${() => (this.isEditModalOpen = false)}
      >
        <div class="form-grid">
          <sl-input
            label="Team Name"
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
        <sl-button
          slot="footer"
          variant="primary"
          @click=${this.handleEditTeam}
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

      <!-- Team Members Modal -->
      <sl-dialog
        label="Team Members"
        ?open=${this.isMembersModalOpen}
        @sl-request-close=${() => (this.isMembersModalOpen = false)}
      >
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
                          ${user?.full_name || user?.username || 'Unknown User'}
                        </span>
                        <span class="member-email">${user?.email || ''}</span>
                      </div>
                      <sl-button
                        size="small"
                        variant="danger"
                        @click=${() => this.handleRemoveMember(member.user_id)}
                      >
                        <sl-icon name="x-lg"></sl-icon>
                      </sl-button>
                    </div>
                  `;
                })
          }
        </div>

        <div class="add-member-section">
          <h4>Add Member</h4>
          <div class="add-member-form">
            <sl-select
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
                      ${user.full_name || user.username} (${user.email})
                    </sl-option>
                  `
                )}
            </sl-select>
            <sl-button @click=${this.handleAddMember}>Add</sl-button>
          </div>
        </div>

        <sl-button
          slot="footer"
          variant="primary"
          @click=${() => (this.isMembersModalOpen = false)}
        >
          Done
        </sl-button>
      </sl-dialog>

      <!-- Manage Team Roles Modal -->
      <sl-dialog
        label="Manage Team Roles"
        ?open=${this.isRoleModalOpen}
        @sl-request-close=${() => (this.isRoleModalOpen = false)}
      >
        <div class="role-list">
          ${this.roles.map((role) => {
            const isAssigned = this.teamRoles.some((r) => r.id === role.id);
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
