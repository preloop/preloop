import { LitElement, html } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import { Router } from '../../router';
import { formStyles } from '../../styles/form-styles';
import { post } from '../../api';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import '../../components/logo-component';

@customElement('verify-email-view')
export class VerifyEmailView extends LitElement {
  @state()
  private isLoading = true;

  @state()
  private error = '';

  /** True when the link also signed the user in, so the console is one click. */
  @state()
  private signedIn = false;

  static styles = [formStyles];

  async firstUpdated() {
    const params = new URLSearchParams(window.location.search);
    const token = params.get('token');

    if (!token) {
      this.error = 'No verification token found in the URL.';
      this.isLoading = false;
      return;
    }

    try {
      const data = await post('/api/v1/auth/verify-email', { token });
      // The link is the only thing an unverified user can act on, so it is
      // also the sign-in. Storing the tokens the endpoint returns turns
      // "verify, then go and find the login page" into one click. An older
      // server returns only a message, and then this stays a plain
      // confirmation.
      if (data?.access_token) {
        localStorage.setItem('accessToken', data.access_token);
        if (data.refresh_token) {
          localStorage.setItem('refreshToken', data.refresh_token);
        }
        this.signedIn = true;
        window.dispatchEvent(
          new CustomEvent('auth-change', { bubbles: true, composed: true })
        );
      }
      this.isLoading = false;
    } catch (error) {
      this.error = 'Invalid or expired verification token.';
      this.isLoading = false;
      console.error('Email verification failed', error);
    }
  }

  private _goToConsole(event: Event) {
    event.preventDefault();
    Router.go('/console');
  }

  render() {
    if (this.isLoading) {
      return html`
        <div class="container">
          <div class="logo">
            <a href="/">
              <logo-component></logo-component>
            </a>
          </div>
          <div class="form-container">
            <h2>Verifying Email</h2>
            <sl-spinner style="font-size: 3rem;"></sl-spinner>
          </div>
        </div>
      `;
    }

    if (this.error) {
      return html`
        <div class="container">
          <div class="logo">
            <a href="/">
              <logo-component></logo-component>
            </a>
          </div>
          <div class="form-container">
            <h2>Email Verification Failed</h2>
            <sl-alert variant="danger" open>
              <sl-icon slot="icon" name="exclamation-octagon"></sl-icon>
              ${this.error}
            </sl-alert>
            <div class="form-links">
              <a href="/login">Back to Sign in</a>
            </div>
          </div>
        </div>
      `;
    }

    return html`
      <div class="container">
        <div class="logo">
          <a href="/">
            <logo-component></logo-component>
          </a>
        </div>
        <div class="form-container">
          <h2>Email Verified</h2>
          <sl-alert variant="success" open>
            <sl-icon slot="icon" name="check-circle"></sl-icon>
            ${
              this.signedIn
                ? 'Your email is verified and you are signed in.'
                : 'Your email has been successfully verified. You can now sign in.'
            }
          </sl-alert>
          <div class="form-links">
            ${
              this.signedIn
                ? html`<a
                    href="/console"
                    @click=${this._goToConsole}
                    id="go-to-console"
                    >Go to the console</a
                  >`
                : html`<a href="/login">Proceed to Sign in</a>`
            }
          </div>
        </div>
      </div>
    `;
  }
}
