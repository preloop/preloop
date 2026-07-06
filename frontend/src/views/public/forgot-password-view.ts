import { LitElement, html } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import { formStyles } from '../../styles/form-styles';
import { post } from '../../api';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '../../components/logo-component';

@customElement('forgot-password-view')
export class ForgotPasswordView extends LitElement {
  @state()
  private message = '';

  @state()
  private error = '';

  static styles = [formStyles];

  private async handleForgotPassword(event: SubmitEvent) {
    event.preventDefault();
    const form = event.target as HTMLFormElement;
    const formData = new FormData(form);
    const email = formData.get('email') as string;

    try {
      await post('/api/v1/auth/forgot-password', { email });
      this.message =
        'If an account with that email exists, a password reset link has been sent.';
      this.error = '';
    } catch (error) {
      // Extract the error message from the Error object
      if (error instanceof Error) {
        this.error = error.message;
      } else {
        this.error = 'An unexpected error occurred. Please try again.';
      }
      console.error('Forgot password failed', error);
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
          <h2>Forgot Password</h2>
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
          <form @submit=${this.handleForgotPassword}>
            <sl-input
              type="email"
              label="Email"
              name="email"
              required
            ></sl-input>
            <div class="form-actions">
              <sl-button type="submit" variant="primary"
                >Send Reset Link</sl-button
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
