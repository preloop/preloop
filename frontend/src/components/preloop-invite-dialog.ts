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
import { consoleDialogStyles } from '../styles/console-dialog';
import { roleLabel } from '../utils/role-label';

const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

/** One address the last send could not invite, and why. */
interface FailedInvite {
  email: string;
  reason: string;
}

@customElement('preloop-invite-dialog')
export class PreloopInviteDialog extends LitElement {
  static styles = [
    consoleDialogStyles,
    css`
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

      .failure-list {
        margin: var(--sl-spacing-x-small) 0 0;
        padding-left: var(--sl-spacing-large);
      }

      sl-select::part(combobox) {
        max-height: 120px;
        overflow-y: auto;
      }
    `,
  ];

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

  /** Addresses the last send could not invite, kept for a retry. */
  @state()
  private failedInvites: FailedInvite[] = [];

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

  /**
   * Split the textarea into addresses. Anything that does not look like an
   * email is returned as invalid rather than dropped, so "jane@example.com,
   * bob" is reported instead of quietly inviting one person.
   */
  private parseEmails(): { valid: string[]; invalid: string[] } {
    const valid: string[] = [];
    const invalid: string[] = [];
    const seen = new Set<string>();
    for (const raw of (this.emailsText || '').split(/[\s,;]+/)) {
      const entry = raw.trim();
      if (!entry) continue;
      const key = entry.toLowerCase();
      if (seen.has(key)) continue;
      seen.add(key);
      (EMAIL_PATTERN.test(entry) ? valid : invalid).push(entry);
    }
    return { valid, invalid };
  }

  async handleSendInvitations() {
    this.error = null;
    this.successMessage = null;
    this.failedInvites = [];

    const { valid: emails, invalid } = this.parseEmails();
    if (invalid.length > 0) {
      this.error = `${invalid.length === 1 ? 'This is' : 'These are'} not ${invalid.length === 1 ? 'an email address' : 'email addresses'}: ${invalid.join(', ')}. Fix or remove ${invalid.length === 1 ? 'it' : 'them'}, then send again.`;
      return;
    }
    if (emails.length === 0) {
      this.error = 'Enter at least one email address.';
      return;
    }

    if (this.selectedRoleIds.length === 0) {
      this.error = 'Choose at least one role for the people you invite.';
      return;
    }

    this.isSending = true;
    try {
      const results = await Promise.allSettled(
        emails.map((email) =>
          createInvitation({
            email,
            role_ids: this.selectedRoleIds,
            team_ids:
              this.selectedTeamIds.length > 0
                ? this.selectedTeamIds
                : undefined,
          })
        )
      );
      const failed: FailedInvite[] = [];
      results.forEach((result, index) => {
        if (result.status === 'rejected') {
          failed.push({
            email: emails[index],
            reason:
              result.reason instanceof Error && result.reason.message
                ? result.reason.message
                : 'Failed to send',
          });
        }
      });
      const sent = emails.length - failed.length;

      if (sent > 0) {
        this.dispatchEvent(
          new CustomEvent('invitations-sent', {
            bubbles: true,
            composed: true,
            detail: { partial: failed.length > 0 },
          })
        );
      }

      if (failed.length > 0) {
        // Keep only the failed addresses, so a retry does not invite the
        // others twice.
        this.emailsText = failed.map((entry) => entry.email).join('\n');
        this.failedInvites = failed;
        this.error =
          sent > 0
            ? `Sent ${sent} · ${failed.length} failed. The failed ${failed.length === 1 ? 'address is' : 'addresses are'} left above to retry.`
            : `No invitations were sent.`;
        return;
      }

      this.successMessage = `Sent ${sent} invitation${sent === 1 ? '' : 's'}.`;
      this.emailsText = '';
      this.selectedRoleIds = [];
      this.selectedTeamIds = [];

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
    this.failedInvites = [];
    this.dispatchEvent(
      new CustomEvent('close', { bubbles: true, composed: true })
    );
  }

  render() {
    return html`
      <sl-dialog
        label="Invite team members"
        ?open=${this.open}
        @sl-request-close=${this.handleClose}
        style="--width: 32rem;"
      >
        <div class="form-grid">
          <sl-textarea
            label="Email addresses"
            placeholder="jane@example.com, john@example.com"
            help-text="Separate addresses with commas, spaces or new lines."
            rows="3"
            .value=${this.emailsText}
            @sl-input=${(e: any) => {
              this.emailsText = e.target.value;
            }}
            ?disabled=${this.isSending}
          ></sl-textarea>

          <sl-select
            label="Roles"
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
                  ${roleLabel(role.name)}
                </sl-option>
              `
            )}
          </sl-select>

          ${
            this.showPermissionWarning
              ? html`
                  <sl-alert variant="neutral" open class="warning-alert">
                    <sl-icon slot="icon" name="info-circle"></sl-icon>
                    These roles can't create or run flows, so the people you
                    invite won't be able to onboard or add agents.
                  </sl-alert>
                `
              : ''
          }

          <sl-select
            label="Teams (optional)"
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
                  <sl-alert
                    variant="danger"
                    open
                    class="error-alert"
                    role="alert"
                  >
                    <sl-icon slot="icon" name="exclamation-octagon"></sl-icon>
                    ${this.error}
                    ${
                      this.failedInvites.length > 0
                        ? html`<ul class="failure-list">
                            ${this.failedInvites.map(
                              (entry) =>
                                html`<li>${entry.email}: ${entry.reason}</li>`
                            )}
                          </ul>`
                        : ''
                    }
                  </sl-alert>
                `
              : ''
          }
          ${
            this.successMessage
              ? html`
                  <sl-alert
                    variant="success"
                    open
                    class="success-alert"
                    role="status"
                  >
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
          Send invitations
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
