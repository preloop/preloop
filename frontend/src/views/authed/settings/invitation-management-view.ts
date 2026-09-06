import { LitElement, html, css, unsafeCSS } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import { repeat } from 'lit/directives/repeat.js';
import {
  getInvitations,
  createInvitation,
  resendInvitation,
  cancelInvitation,
  getTeams,
  getFeatures,
} from '../../../api';
import type { UserInvitation, InvitationCreate, Team } from '../../../types';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/dialog/dialog.js';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/card/card.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/tab-group/tab-group.js';
import '@shoelace-style/shoelace/dist/components/tab/tab.js';
import '@shoelace-style/shoelace/dist/components/tab-panel/tab-panel.js';
import '@shoelace-style/shoelace/dist/components/select/select.js';
import '@shoelace-style/shoelace/dist/components/option/option.js';
import '../../../components/preloop-invite-dialog';
import consoleStyles from '../../../styles/console-styles.css?inline';

@customElement('invitation-management-view')
export class InvitationManagementView extends LitElement {
  @state()
  private invitations: UserInvitation[] = [];

  @state()
  private isLoading = true;

  @state()
  private error: string | null = null;

  @state()
  private isCreateModalOpen = false;

  @state()
  private newInvitation: Partial<InvitationCreate> = {};

  @state()
  private activeTab: 'pending' | 'accepted' | 'all' = 'pending';

  @state()
  private teams: Team[] = [];

  @state()
  private isLoadingTeams = false;

  static styles = [
    unsafeCSS(consoleStyles),
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

      .invitations-grid {
        display: grid;
        gap: 1rem;
      }

      sl-card {
        width: 100%;
      }

      .invitation-card-content {
        display: grid;
        grid-template-columns: auto 1fr auto;
        gap: 1rem;
        align-items: center;
      }

      .invitation-icon {
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

      .invitation-details {
        flex: 1;
      }

      .invitation-email {
        font-weight: 600;
        font-size: 1rem;
        margin: 0 0 0.25rem 0;
      }

      .invitation-date {
        color: var(--sl-color-neutral-600);
        font-size: 0.875rem;
        margin: 0 0 0.5rem 0;
      }

      .invitation-meta {
        display: flex;
        gap: 0.5rem;
        flex-wrap: wrap;
      }

      .invitation-actions {
        display: flex;
        gap: 0.5rem;
      }

      .invitation-actions .danger-action {
        margin-left: var(--sl-spacing-large);
      }

      .form-grid {
        display: grid;
        gap: 1rem;
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

      sl-tab-group {
        margin-bottom: 2rem;
      }
    `,
  ];

  /** Statuses are stored lower case; the chip says "Pending". */
  static statusLabel(status: string | null | undefined): string {
    const value = String(status || '').trim();
    if (!value) return 'Unknown';
    return value
      .replace(/_/g, ' ')
      .replace(/^\w/, (character) => character.toUpperCase());
  }

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
    await Promise.all([this.fetchInvitations(), this.fetchTeams()]);
  }

  async fetchTeams() {
    this.isLoadingTeams = true;
    try {
      const response = await getTeams(0, 100);
      this.teams = response.teams;
    } catch (error) {
      console.error('Failed to fetch teams:', error);
      // Don't set a global error for teams, just log it
    } finally {
      this.isLoadingTeams = false;
    }
  }

  async fetchInvitations() {
    this.isLoading = true;
    this.error = null;
    try {
      const status = this.activeTab === 'all' ? undefined : this.activeTab;
      const response = await getInvitations(0, 100, status);
      this.invitations = response.invitations;
    } catch (error) {
      this.error =
        error instanceof Error ? error.message : 'Failed to fetch invitations';
    } finally {
      this.isLoading = false;
    }
  }

  async handleCreateInvitation() {
    if (!this.newInvitation.email) {
      return;
    }

    try {
      await createInvitation(this.newInvitation as InvitationCreate);
      this.isCreateModalOpen = false;
      this.newInvitation = {};
      await this.fetchInvitations();
    } catch (error) {
      this.error =
        error instanceof Error ? error.message : 'Failed to create invitation';
    }
  }

  async handleResendInvitation(invitation: UserInvitation) {
    try {
      await resendInvitation(invitation.id);
      alert('Invitation resent successfully');
    } catch (error) {
      this.error =
        error instanceof Error ? error.message : 'Failed to resend invitation';
    }
  }

  async handleCancelInvitation(invitation: UserInvitation) {
    if (
      !confirm(
        `Are you sure you want to cancel the invitation to ${invitation.email}?`
      )
    ) {
      return;
    }

    try {
      await cancelInvitation(invitation.id);
      await this.fetchInvitations();
    } catch (error) {
      this.error =
        error instanceof Error ? error.message : 'Failed to cancel invitation';
    }
  }

  formatDate(dateString: string): string {
    const date = new Date(dateString);
    return (
      date.toLocaleDateString() +
      ' ' +
      date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
    );
  }

  getStatusVariant(
    status: string
  ): 'success' | 'warning' | 'neutral' | 'danger' {
    switch (status) {
      case 'accepted':
        return 'success';
      case 'pending':
        return 'warning';
      case 'expired':
        return 'danger';
      case 'cancelled':
        return 'neutral';
      default:
        return 'neutral';
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
          <p>Invitation management is not available in this edition.</p>
        </div>
      `;
    }

    return html`
      <div class="header">
        <h1>Invitations</h1>
        <sl-button
          variant="primary"
          @click=${() => (this.isCreateModalOpen = true)}
        >
          <sl-icon slot="prefix" name="envelope-plus"></sl-icon>
          Send invitation
        </sl-button>
      </div>

      ${this.error ? html`<div class="error">${this.error}</div>` : ''}

      <sl-tab-group
        @sl-tab-show=${(e: CustomEvent) => {
          this.activeTab = e.detail.name as 'pending' | 'accepted' | 'all';
          this.fetchInvitations();
        }}
      >
        <sl-tab
          slot="nav"
          panel="pending"
          ?active=${this.activeTab === 'pending'}
        >
          Pending
        </sl-tab>
        <sl-tab slot="nav" panel="accepted">Accepted</sl-tab>
        <sl-tab slot="nav" panel="all">All</sl-tab>

        <sl-tab-panel name="pending">
          ${this.renderInvitations()}
        </sl-tab-panel>
        <sl-tab-panel name="accepted">
          ${this.renderInvitations()}
        </sl-tab-panel>
        <sl-tab-panel name="all"> ${this.renderInvitations()} </sl-tab-panel>
      </sl-tab-group>

      <!-- Create Invitation Modal -->
      <preloop-invite-dialog
        ?open=${this.isCreateModalOpen}
        @close=${() => {
          this.isCreateModalOpen = false;
        }}
        @invitations-sent=${() => {
          this.isCreateModalOpen = false;
          this.fetchInvitations();
        }}
      ></preloop-invite-dialog>
    `;
  }

  renderInvitations() {
    if (this.invitations.length === 0) {
      return html`
        <div class="empty-state">
          <sl-icon
            name="envelope"
            style="font-size: 3rem; margin-bottom: 1rem;"
          ></sl-icon>
          <p>No invitations found.</p>
        </div>
      `;
    }

    return html`
      <div class="invitations-grid">
        ${repeat(
          this.invitations,
          (invitation) => invitation.id,
          (invitation) => html`
            <sl-card>
              <div class="invitation-card-content">
                <div class="invitation-icon">
                  <sl-icon name="envelope-fill"></sl-icon>
                </div>
                <div class="invitation-details">
                  <h3 class="invitation-email">${invitation.email}</h3>
                  <p class="invitation-date">
                    Sent: ${this.formatDate(invitation.created_at)}
                  </p>
                  <div class="invitation-meta">
                    <sl-badge
                      class="status-chip"
                      variant="${this.getStatusVariant(invitation.status)}"
                    >
                      ${InvitationManagementView.statusLabel(invitation.status)}
                    </sl-badge>
                    ${
                      invitation.status === 'pending'
                        ? html`
                            <sl-badge class="chip" variant="neutral">
                              Expires ${this.formatDate(invitation.expires_at)}
                            </sl-badge>
                          `
                        : ''
                    }
                    ${
                      invitation.accepted_at
                        ? html`
                            <sl-badge class="chip" variant="success">
                              Accepted
                              ${this.formatDate(invitation.accepted_at)}
                            </sl-badge>
                          `
                        : ''
                    }
                  </div>
                </div>
                <div class="invitation-actions">
                  ${
                    invitation.status === 'pending'
                      ? html`
                          <sl-button
                            size="small"
                            @click=${() =>
                              this.handleResendInvitation(invitation)}
                            title="Resend invitation"
                          >
                            <sl-icon name="arrow-repeat"></sl-icon>
                          </sl-button>
                          <!-- Outline, last, after a gap (DESIGN.md
                               "Destructive actions"). -->
                          <sl-button
                            class="danger-action"
                            size="small"
                            variant="danger"
                            outline
                            @click=${() =>
                              this.handleCancelInvitation(invitation)}
                            title="Cancel invitation"
                          >
                            <sl-icon name="x-lg"></sl-icon>
                          </sl-button>
                        `
                      : ''
                  }
                </div>
              </div>
            </sl-card>
          `
        )}
      </div>
    `;
  }
}
