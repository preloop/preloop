import { LitElement, html } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import { formStyles } from '../../styles/form-styles';
import { ApiError, post } from '../../api';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '../../components/logo-component';

/** Shown when the link itself is the problem: expired, used, or unknown. */
export const RESET_TOKEN_ERROR = 'Invalid or expired reset token.';

/**
 * The sentence for a failed reset. Only a refusal of the token (400 from the
 * token check, 404 for an account that no longer exists) blames the link; a
 * password the server rejects (422) or a server fault says what it was, so
 * nobody throws away a good link over a too-short password.
 */
export function resetErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 400 || error.status === 404) return RESET_TOKEN_ERROR;
    if (error.message && !error.message.startsWith('HTTP error!')) {
      return error.message;
    }
  }
  return 'Could not reset your password. Try again in a moment.';
}

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
        'Your password has been reset successfully. You can now sign in.';
      this.error = '';
    } catch (error) {
      this.error = resetErrorMessage(error);
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
          <h2>Reset password</h2>
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
              label="New password"
              name="password"
              autocomplete="new-password"
              minlength="8"
              required
              password-toggle
              help-text="At least 8 characters."
            ></sl-input>
            <sl-input
              type="password"
              label="Confirm new password"
              name="confirmPassword"
              autocomplete="new-password"
              minlength="8"
              required
              password-toggle
            ></sl-input>
            <div class="form-actions">
              <sl-button type="submit" variant="primary"
                >Reset password</sl-button
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
