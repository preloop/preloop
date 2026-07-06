import { LitElement, html, css, unsafeCSS } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import { changePassword } from '../../../api';
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
    `,
  ];
}
