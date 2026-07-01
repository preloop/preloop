import { LitElement, html } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import { formStyles } from '../../styles/form-styles';
import { post } from '../../api';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '../../components/logo-component';

@customElement('reset-password-view')
export class ResetPasswordView extends LitElement {
  @state()
  private token = '';

  @state()
  private message = '';

  @state()
  private error = '';

  static styles = [formStyles];

  firstUpdated() {
    const params = new URLSearchParams(window.location.search);
    this.token = params.get('token') || '';
    if (!this.token) {
      this.error = 'No reset token found in the URL.';
    }
  }

  private async handleResetPassword(event: SubmitEvent) {
    event.preventDefault();
    const form = event.target as HTMLFormElement;
    const formData = new FormData(form);
    const password = formData.get('password') as string;
    const confirmPassword = formData.get('confirmPassword') as string;

    if (password !== confirmPassword) {
      this.error = 'Passwords do not match.';
      return;
    }

    try {
      await post('/api/v1/auth/reset-password', {
        token: this.token,
        new_password: password,
      });
      this.message =
        'Your password has been reset successfully. You can now log in.';
      this.error = '';
    } catch (error) {
      this.error = 'Invalid or expired reset token.';
      console.error('Password reset failed', error);
    }
  }

  render() {
    return html`
      <div class="container">
        <div class="logo">
          <a href="/">
            <logo-component></logo-component>
          </a>
        </div>
        <div class="form-container">
          <h2>Reset Password</h2>
          ${
            this.message
              ? html`<sl-alert
                  variant="success"
                  open
                  closable
                  @sl-after-hide=${() => (this.message = '')}
                >
                  <sl-icon slot="icon" name="check-circle"></sl-icon>
                  ${this.message}
                </sl-alert>`
              : ''
          }
          ${
            this.error
              ? html`<sl-alert
                  variant="danger"
                  open
                  closable
                  @sl-after-hide=${() => (this.error = '')}
                >
                  <sl-icon slot="icon" name="exclamation-octagon"></sl-icon>
                  ${this.error}
                </sl-alert>`
              : ''
          }
          <form @submit=${this.handleResetPassword}>
            <sl-input
              type="password"
              label="New Password"
              name="password"
              required
              password-toggle
            ></sl-input>
            <sl-input
              type="password"
              label="Confirm New Password"
              name="confirmPassword"
              required
              password-toggle
            ></sl-input>
            <div class="form-actions">
              <sl-button type="submit" variant="primary"
                >Reset Password</sl-button
              >
            </div>
            <div class="form-links">
              <a href="/login">Back to Sign in</a>
            </div>
          </form>
        </div>
      </div>
    `;
  }
}
