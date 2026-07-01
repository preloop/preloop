import { LitElement, html, css } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import { getTeams, getRoles, createInvitation } from '../api';
import type { Team, Role } from '../types';
import '@shoelace-style/shoelace/dist/components/dialog/dialog.js';
import '@shoelace-style/shoelace/dist/components/textarea/textarea.js';
import '@shoelace-style/shoelace/dist/components/select/select.js';
import '@shoelace-style/shoelace/dist/components/option/option.js';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';

@customElement('preloop-invite-dialog')
export class PreloopInviteDialog extends LitElement {
  static styles = css`
    :host {
      display: block;
    }

    .form-grid {
      display: grid;
      gap: 1.25rem;
      margin-top: 0.5rem;
    }

    .error-alert {
      margin-top: 1rem;
    }

    .success-alert {
      margin-top: 1rem;
    }

    sl-select::part(combobox) {
      max-height: 120px;
      overflow-y: auto;
    }
  `;

  @property({ type: Boolean })
  open = false;

  @state()
  private emailsText = '';

  @state()
  private selectedRoleIds: string[] = [];

  @state()
  private selectedTeamIds: string[] = [];

  @state()
  private teams: Team[] = [];

  @state()
  private roles: Role[] = [];

  @state()
  private isLoading = false;

  @state()
  private isSending = false;

  @state()
  private error: string | null = null;

  @state()
  private successMessage: string | null = null;

  async connectedCallback() {
    super.connectedCallback();
    await this.loadData();
  }

  async loadData() {
    this.isLoading = true;
    try {
      const [teamsRes, rolesRes] = await Promise.all([
        getTeams(0, 100).catch(() => ({ teams: [] })),
        getRoles().catch(() => ({ roles: [] })),
      ]);
      this.teams = teamsRes.teams || [];
      this.roles = rolesRes.roles || [];
    } catch (e) {
      console.error('Failed to load data for invitation dialog:', e);
    } finally {
      this.isLoading = false;
    }
  }

  get showPermissionWarning(): boolean {
    if (this.selectedRoleIds.length === 0) return false;
    const selectedRoles = this.roles.filter((r) =>
      this.selectedRoleIds.includes(r.id)
    );
    const hasSufficient = selectedRoles.some(
      (r) =>
        (r.permissions || []).includes('create_flows') ||
        (r.permissions || []).includes('execute_flows')
    );
    return !hasSufficient;
  }

  private parseEmails(): string[] {
    if (!this.emailsText) return [];
    return this.emailsText
      .split(/[\s,\n]+/)
      .map((e) => e.trim())
      .filter((e) => /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(e));
  }

  async handleSendInvitations() {
    this.error = null;
    this.successMessage = null;

    const emails = this.parseEmails();
    if (emails.length === 0) {
      this.error = 'Please enter at least one valid email address.';
      return;
    }

    if (this.selectedRoleIds.length === 0) {
      this.error = 'Please select at least one role for the invitees.';
      return;
    }

    this.isSending = true;
    try {
      const promises = emails.map((email) =>
        createInvitation({
          email,
          role_ids: this.selectedRoleIds,
          team_ids:
            this.selectedTeamIds.length > 0 ? this.selectedTeamIds : undefined,
        })
      );
      await Promise.all(promises);

      this.successMessage = `Successfully sent ${emails.length} invitation${emails.length > 1 ? 's' : ''}!`;
      this.emailsText = '';
      this.selectedRoleIds = [];
      this.selectedTeamIds = [];

      this.dispatchEvent(
        new CustomEvent('invitations-sent', {
          bubbles: true,
          composed: true,
        })
      );

      // Auto close after a short delay so user sees the success state
      setTimeout(() => {
        this.open = false;
        this.successMessage = null;
        this.dispatchEvent(
          new CustomEvent('close', { bubbles: true, composed: true })
        );
      }, 1500);
    } catch (e) {
      this.error =
        e instanceof Error ? e.message : 'Failed to send invitations.';
    } finally {
      this.isSending = false;
    }
  }

  private handleClose() {
    this.open = false;
    this.error = null;
    this.successMessage = null;
    this.dispatchEvent(
      new CustomEvent('close', { bubbles: true, composed: true })
    );
  }

  render() {
    return html`
      <sl-dialog
        label="Invite Team Members"
        ?open=${this.open}
        @sl-request-close=${this.handleClose}
        style="--width: 32rem;"
      >
        <div class="form-grid">
          <sl-textarea
            label="Email Addresses"
            placeholder="Enter email addresses (separated by comma, space, or newline)"
            rows="3"
            .value=${this.emailsText}
            @sl-input=${(e: any) => {
              this.emailsText = e.target.value;
            }}
            ?disabled=${this.isSending}
          ></sl-textarea>

          <sl-select
            label="Assign Roles"
            placeholder="Select roles for the invited users"
            multiple
            clearable
            .value=${this.selectedRoleIds}
            @sl-change=${(e: any) => {
              this.selectedRoleIds = e.target.value;
            }}
            ?disabled=${this.isSending || this.isLoading}
          >
            ${this.roles.map(
              (role) => html`
                <sl-option .value=${role.id} title=${role.description || ''}>
                  ${role.name}
                </sl-option>
              `
            )}
          </sl-select>

          ${
            this.showPermissionWarning
              ? html`
                  <sl-alert variant="warning" open class="warning-alert">
                    <sl-icon slot="icon" name="exclamation-triangle"></sl-icon>
                    <strong>Insufficient Permissions Warning:</strong><br />
                    The selected roles do not have sufficient permissions to
                    create or execute flows, which are required for onboarding
                    or adding agents.
                  </sl-alert>
                `
              : ''
          }

          <sl-select
            label="Assign Teams (Optional)"
            placeholder="Select teams for the invited users"
            multiple
            clearable
            .value=${this.selectedTeamIds}
            @sl-change=${(e: any) => {
              this.selectedTeamIds = e.target.value;
            }}
            ?disabled=${
              this.isSending || this.isLoading || this.teams.length === 0
            }
          >
            ${this.teams.map(
              (team) => html`
                <sl-option .value=${team.id}>${team.name}</sl-option>
              `
            )}
          </sl-select>

          ${
            this.error
              ? html`
                  <sl-alert variant="danger" open class="error-alert">
                    <sl-icon slot="icon" name="exclamation-octagon"></sl-icon>
                    ${this.error}
                  </sl-alert>
                `
              : ''
          }
          ${
            this.successMessage
              ? html`
                  <sl-alert variant="success" open class="success-alert">
                    <sl-icon slot="icon" name="check-circle"></sl-icon>
                    ${this.successMessage}
                  </sl-alert>
                `
              : ''
          }
        </div>

        <sl-button
          slot="footer"
          variant="primary"
          @click=${this.handleSendInvitations}
          ?loading=${this.isSending}
          ?disabled=${this.isLoading}
        >
          Send Invitation(s)
        </sl-button>
        <sl-button
          slot="footer"
          variant="default"
          @click=${this.handleClose}
          ?disabled=${this.isSending}
        >
          Cancel
        </sl-button>
      </sl-dialog>
    `;
  }
}
