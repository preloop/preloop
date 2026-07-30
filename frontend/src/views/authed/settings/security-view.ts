import { LitElement, html, css, unsafeCSS } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import { changePassword, getFeatures } from '../../../api';
import {
  PasskeySummary,
  deletePasskey,
  listPasskeys,
  passkeysSupported,
  registerPasskey,
} from '../../../services/passkeys';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import consoleStyles from '../../../styles/console-styles.css?inline';

@customElement('security-view')
export class SecurityView extends LitElement {
  @state()
  private currentPassword = '';

  @state()
  private newPassword = '';

  @state()
  private confirmNewPassword = '';

  @state()
  private changePasswordMessage = '';

  @state()
  private passkeys: PasskeySummary[] = [];

  @state()
  private passkeysEnabled = false;

  @state()
  private passkeyMessage = '';

  @state()
  private passkeyBusy = false;

  connectedCallback() {
    super.connectedCallback();
    void this.loadPasskeys();
  }

  private async loadPasskeys() {
    try {
      const features = await getFeatures();
      this.passkeysEnabled =
        features.features['passkeys'] !== false && passkeysSupported();
      if (this.passkeysEnabled) {
        this.passkeys = await listPasskeys();
      }
    } catch {
      // Passkey list failure must not break the security page.
      this.passkeysEnabled = false;
    }
  }

  private async handleAddPasskey() {
    this.passkeyMessage = '';
    this.passkeyBusy = true;
    try {
      await registerPasskey();
      this.passkeys = await listPasskeys();
      this.passkeyMessage = 'Passkey registered.';
    } catch (error) {
      this.passkeyMessage =
        error instanceof Error ? error.message : 'Failed to register passkey.';
    } finally {
      this.passkeyBusy = false;
    }
  }

  private async handleDeletePasskey(id: string) {
    this.passkeyMessage = '';
    try {
      await deletePasskey(id);
      this.passkeys = this.passkeys.filter((p) => p.id !== id);
      this.passkeyMessage = 'Passkey removed.';
    } catch (error) {
      this.passkeyMessage =
        error instanceof Error ? error.message : 'Failed to remove passkey.';
    }
  }

  async handleChangePassword(event: Event) {
    event.preventDefault();

    if (this.newPassword.length < 8) {
      this.changePasswordMessage =
        'New password must be at least 8 characters.';
      return;
    }

    if (this.newPassword !== this.confirmNewPassword) {
      this.changePasswordMessage = 'New passwords do not match.';
      return;
    }
    try {
      await changePassword({
        current_password: this.currentPassword,
        new_password: this.newPassword,
      });
      this.changePasswordMessage = 'Password changed successfully.';
      this.currentPassword = '';
      this.newPassword = '';
      this.confirmNewPassword = '';
    } catch (error) {
      const errorMessage =
        error instanceof Error ? error.message : 'An unknown error occurred.';
      this.changePasswordMessage = `Failed to change password: ${errorMessage}`;
    }
  }

  render() {
    return html`
      <view-header headerText="Security" width="narrow"> </view-header>
      <div class="column-layout narrow">
        <div class="main-column">
          <div class="card">
            <div class="card-header">
              <h3>Change Password</h3>
            </div>
            <div class="card-body">
              <form @submit="${this.handleChangePassword}">
                <sl-input
                  type="password"
                  label="Current Password"
                  .value="${this.currentPassword}"
                  @sl-input="${(e: Event) =>
                    (this.currentPassword = (
                      e.target as HTMLInputElement
                    ).value)}"
                  required
                  password-toggle
                ></sl-input>
                <sl-input
                  type="password"
                  label="New Password"
                  .value="${this.newPassword}"
                  @sl-input="${(e: Event) =>
                    (this.newPassword = (e.target as HTMLInputElement).value)}"
                  required
                  minlength="8"
                  password-toggle
                ></sl-input>
                <sl-input
                  type="password"
                  label="Confirm New Password"
                  .value="${this.confirmNewPassword}"
                  @sl-input="${(e: Event) =>
                    (this.confirmNewPassword = (
                      e.target as HTMLInputElement
                    ).value)}"
                  required
                  password-toggle
                ></sl-input>
                <sl-button variant="primary" type="submit"
                  >Change Password</sl-button
                >
                ${
                  this.changePasswordMessage
                    ? html`<p>${this.changePasswordMessage}</p>`
                    : ''
                }
              </form>
            </div>
          </div>
          ${this.passkeysEnabled
            ? html`
                <div class="card">
                  <div class="card-header">
                    <h3>Passkeys</h3>
                  </div>
                  <div class="card-body">
                    <p>
                      Sign in without a password using your device's screen
                      lock, fingerprint, or security key.
                    </p>
                    ${this.passkeys.length > 0
                      ? html`
                          <ul class="passkey-list">
                            ${this.passkeys.map(
                              (passkey) => html`
                                <li class="passkey-item">
                                  <span>
                                    ${passkey.name}
                                    <small>
                                      added
                                      ${new Date(
                                        passkey.created_at
                                      ).toLocaleDateString()}
                                    </small>
                                  </span>
                                  <sl-button
                                    size="small"
                                    variant="danger"
                                    outline
                                    @click="${() =>
                                      this.handleDeletePasskey(passkey.id)}"
                                    >Remove</sl-button
                                  >
                                </li>
                              `
                            )}
                          </ul>
                        `
                      : html`<p><em>No passkeys registered yet.</em></p>`}
                    <sl-button
                      variant="primary"
                      ?loading="${this.passkeyBusy}"
                      @click="${this.handleAddPasskey}"
                      >Add Passkey</sl-button
                    >
                    ${this.passkeyMessage
                      ? html`<p>${this.passkeyMessage}</p>`
                      : ''}
                  </div>
                </div>
              `
            : ''}
        </div>
        <div class="side-column"></div>
      </div>
    `;
  }

  static styles = [
    unsafeCSS(consoleStyles),
    css`
      .card-header {
        padding-bottom: 1rem;
        margin-bottom: 1rem;
      }
      h2,
      h3 {
        margin: 0;
      }
      form {
        display: flex;
        flex-direction: column;
        gap: 1rem;
      }

      sl-button {
        width: 12em;
      }

      .passkey-list {
        list-style: none;
        margin: 0 0 1rem 0;
        padding: 0;
      }

      .passkey-item {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 0.5rem 0;
        border-bottom: 1px solid var(--sl-color-neutral-200);
      }

      .passkey-item sl-button {
        width: auto;
      }

      .passkey-item small {
        color: var(--sl-color-neutral-500);
        margin-left: 0.5rem;
      }
    `,
  ];
}
