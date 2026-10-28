import { relativeTime } from '../../../utils/relative-time';
import {
  formatFutureRelativeTime,
  formatAbsoluteTime,
  parseUTCDate,
} from '../../../utils/date';
import { ConsoleStatus } from '../../../controllers/console-status';
import '@shoelace-style/shoelace/dist/components/tooltip/tooltip.js';
import { EditPermissions } from '../../../controllers/edit-permissions';
import { LitElement, html, css, nothing, unsafeCSS } from 'lit';
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
import '@shoelace-style/shoelace/dist/components/divider/divider.js';
import '@shoelace-style/shoelace/dist/components/icon-button/icon-button.js';
import '@shoelace-style/shoelace/dist/components/dropdown/dropdown.js';
import '@shoelace-style/shoelace/dist/components/menu/menu.js';
import '@shoelace-style/shoelace/dist/components/menu-item/menu-item.js';
import '@shoelace-style/shoelace/dist/components/tab-group/tab-group.js';
import '@shoelace-style/shoelace/dist/components/tab/tab.js';
import '@shoelace-style/shoelace/dist/components/tab-panel/tab-panel.js';
import '@shoelace-style/shoelace/dist/components/select/select.js';
import '@shoelace-style/shoelace/dist/components/option/option.js';
import '../../../components/preloop-invite-dialog';
import '../../../components/view-header.ts';
import { confirmDialog, showToast } from '../../../components/confirm-dialog';
import consoleStyles from '../../../styles/console-styles.css?inline';

@customElement('invitation-management-view')
export class InvitationManagementView extends LitElement {
  private readonly accessibilityStatus = new ConsoleStatus(this);
  private readonly editPermissions = new EditPermissions(this);
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
      /* No page padding here: the shell pays the side inset
         (styles/console-styles.css, "The page box"). */
      :host {
        display: block;
      }

      /* The console table pattern (DESIGN.md "Tables and views"). */
      .invitations-table td {
        vertical-align: middle;
      }

      .invitation-email {
        font-weight: 600;
      }

      .date-cell {
        color: var(--console-meta-color);
        font-size: var(--console-text-meta);
        white-space: nowrap;
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
    if (!this.editPermissions.allows('invite_users')) return;
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
    if (!this.editPermissions.allows('invite_users')) return;
    try {
      await resendInvitation(invitation.id);
      showToast(`Invitation resent to ${invitation.email}.`, 'success');
    } catch (error) {
      this.error =
        error instanceof Error ? error.message : 'Failed to resend invitation';
    }
  }

  async handleCancelInvitation(invitation: UserInvitation) {
    if (!this.editPermissions.allows('invite_users')) return;
    const confirmed = await confirmDialog({
      title: 'Cancel invitation?',
      message: `Cancel the invitation to ${invitation.email}?`,
      detail:
        'The link in their email stops working. You can send a new invitation later.',
      confirmLabel: 'Cancel invitation',
      cancelLabel: 'Keep invitation',
      variant: 'danger',
    });
    if (!confirmed) {
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
      <view-header headerText="Invitations" width="narrow">
        <div slot="main-column">
          <sl-tooltip
            content=${!this.editPermissions.allows('invite_users') ? 'Requires invite_users' : ''}
            ><sl-button
              ?disabled=${!this.editPermissions.allows('invite_users')}
              variant="primary"
              @click=${() => (this.isCreateModalOpen = true)}
            >
              <sl-icon slot="prefix" name="envelope-plus"></sl-icon>
              Send invitation
            </sl-button></sl-tooltip
          >
        </div>
      </view-header>

      ${
        this.error
          ? html`<div class="error" role="alert">${this.error}</div>`
          : ''
      }

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
        @invitations-sent=${(e: CustomEvent) => {
          if (!e.detail?.partial) {
            this.isCreateModalOpen = false;
          }
          this.fetchInvitations();
        }}
      ></preloop-invite-dialog>
    `;
  }

  /**
   * The invitations the active tab is about. The list endpoint is asked for
   * the tab's status, but a server that ignores the filter returns all of
   * them, and the Pending tab must never call a cancelled one pending.
   */
  private get visibleInvitations(): UserInvitation[] {
    if (this.activeTab === 'all') return this.invitations;
    return this.invitations.filter(
      (invitation) =>
        String(invitation.status || '').toLowerCase() === this.activeTab
    );
  }

  renderInvitations() {
    const invitations = this.visibleInvitations;
    if (invitations.length === 0) {
      const empty =
        this.activeTab === 'all'
          ? 'No invitations yet.'
          : `No ${this.activeTab} invitations.`;
      return html`
        <div class="empty-state">
          <sl-icon
            name="envelope"
            style="font-size: 3rem; margin-bottom: 1rem;"
          ></sl-icon>
          <p>${empty}</p>
        </div>
      `;
    }

    return html`
      <sl-card class="table-card">
        <div class="table-scroll">
          <table
            class="styled-table invitations-table"
            aria-label="Invitations"
          >
            <thead>
              <tr>
                <th scope="col">Email</th>
                <th scope="col">Status</th>
                <th scope="col">Sent</th>
                <th scope="col">Expires</th>
                <th scope="col">Accepted</th>
                <th scope="col"><span class="sr-only">Actions</span></th>
              </tr>
            </thead>
            <tbody>
              ${repeat(
                invitations,
                (invitation) => invitation.id,
                (invitation) => this.renderInvitationRow(invitation)
              )}
            </tbody>
          </table>
        </div>
      </sl-card>
    `;
  }

  /**
   * When a pending invitation stops working, as a countdown ("in 7d"). A
   * column of ages beside a bare date reads as two kinds of fact.
   */
  static expiresLabel(value: string, now: Date = new Date()): string {
    const remaining = parseUTCDate(value).getTime() - now.getTime();
    if (remaining <= 0) return 'Expired';
    if (remaining < 86400000) return formatFutureRelativeTime(value, now);
    return `in ${Math.ceil(remaining / 86400000)}d`;
  }

  /** A date cell: relative, with the absolute in the tooltip. */
  private renderDate(value: string | null | undefined, future = false) {
    if (!value) return nothing;
    if (!future) return relativeTime(value);
    const label = InvitationManagementView.expiresLabel(value);
    return html`<span title=${formatAbsoluteTime(value)}>${label}</span>`;
  }

  private renderInvitationRow(invitation: UserInvitation) {
    const pending = invitation.status === 'pending';
    const canInvite = this.editPermissions.allows('invite_users');
    return html`
      <tr data-invitation-id=${invitation.id}>
        <td class="invitation-email">${invitation.email}</td>
        <td>
          <sl-badge
            class="status-chip"
            pill
            variant=${this.getStatusVariant(invitation.status)}
            >${InvitationManagementView.statusLabel(invitation.status)}</sl-badge
          >
        </td>
        <td class="date-cell">${this.renderDate(invitation.created_at)}</td>
        <td class="date-cell">
          ${pending ? this.renderDate(invitation.expires_at, true) : nothing}
        </td>
        <td class="date-cell">${this.renderDate(invitation.accepted_at)}</td>
        <td class="actions-cell">
          ${
            pending
              ? html`<sl-dropdown class="row-menu" placement="bottom-end" hoist>
                  <sl-icon-button
                    slot="trigger"
                    name="three-dots-vertical"
                    label="Actions for ${invitation.email}"
                  ></sl-icon-button>
                  <sl-menu>
                    <sl-menu-item
                      data-action="resend"
                      ?disabled=${!canInvite}
                      title=${canInvite ? '' : 'Requires invite_users'}
                      @click=${() => this.handleResendInvitation(invitation)}
                    >
                      <sl-icon slot="prefix" name="arrow-repeat"></sl-icon>
                      Resend invitation
                    </sl-menu-item>
                    <sl-divider></sl-divider>
                    <sl-menu-item
                      class="danger-item"
                      data-action="cancel"
                      ?disabled=${!canInvite}
                      title=${canInvite ? '' : 'Requires invite_users'}
                      @click=${() => this.handleCancelInvitation(invitation)}
                    >
                      <sl-icon slot="prefix" name="x-lg"></sl-icon>
                      Cancel invitation
                    </sl-menu-item>
                  </sl-menu>
                </sl-dropdown>`
              : nothing
          }
        </td>
      </tr>
    `;
  }
}
