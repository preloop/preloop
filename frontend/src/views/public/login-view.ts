import { LitElement, html, css, nothing } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import { Router } from '../../router';
import {
  ApiError,
  post,
  getFeatures,
  resendVerificationEmail,
} from '../../api';
import { formStyles } from '../../styles/form-styles';
import { hasCapability } from '../../capabilities';
import type { Membership } from '../../hierarchy-api';
import { getBrandConfig } from '../../brand-config';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '../../components/logo-component';
import { trackGoal } from '../../services/web-analytics';
import { passkeysSupported, signInWithPasskey } from '../../services/passkeys';

const OAUTH_PROVIDER_CONFIG: Record<string, { label: string; icon: string }> = {
  github: { label: 'GitHub', icon: 'github' },
  google: { label: 'Google', icon: 'google' },
  gitlab: { label: 'GitLab', icon: 'gitlab' },
};

@customElement('login-view')
export class LoginView extends LitElement {
  @state()
  private error = '';

  @state()
  private successMessage = '';

  @state()
  private oauthProviders: string[] = [];

  @state()
  private registrationEnabled = true;

  @state()
  private passkeysEnabled = false;

  /**
   * The address to resend a verification link to, set only when the server
   * refused this sign-in with `email_not_verified`. Empty the rest of the
   * time, so the resend action does not exist on an instance that does not
   * require verification (that is every instance by default).
   */
  @state()
  private unverifiedEmail = '';

  @state()
  private resending = false;

  /** Capability `multi_account` from /features (off in OSS). */
  private multiAccount = false;

  /**
   * Memberships to choose from after sign-in, set only when the person has
   * several accounts and none was used last. Null renders the sign-in form.
   */
  @state()
  private chooserMemberships: Membership[] | null = null;

  static styles = [
    formStyles,
    css`
      .success-message {
        background-color: #d4edda;
        color: #155724;
        border: 1px solid #c3e6cb;
        padding: 0.75rem 1.25rem;
        margin-bottom: 1rem;
        border-radius: 0.25rem;
        text-align: center;
      }

      /* The resend action sits inside the refusal it answers, so there is no
         question about which message it belongs to. */
      .resend-row {
        margin-top: 0.75rem;
      }

      .oauth-section {
        display: flex;
        flex-direction: column;
        gap: 0.75rem;
      }

      .oauth-button {
        width: 100%;
      }

      .oauth-button::part(base) {
        width: 100%;
        display: flex;
        align-items: center;
        justify-content: center;
        gap: 0.5rem;
      }

      .divider {
        display: flex;
        align-items: center;
        margin: 1.5rem 0;
        color: var(--sl-color-neutral-500);
        font-size: var(--sl-font-size-small);
      }

      .divider::before,
      .divider::after {
        content: '';
        flex: 1;
        border-bottom: 1px solid var(--sl-color-neutral-300);
      }

      .divider::before {
        margin-right: 0.75rem;
      }

      .divider::after {
        margin-left: 0.75rem;
      }
    `,
  ];

  connectedCallback() {
    super.connectedCallback();
    const urlParams = new URLSearchParams(window.location.search);
    if (urlParams.has('registered')) {
      this.successMessage =
        'Your account has been created successfully. Please sign in.';
      const url = new URL(window.location.href);
      url.searchParams.delete('registered');
      window.history.replaceState({}, document.title, url.pathname);
    }
    this._checkFeatures();
  }

  private async _checkFeatures() {
    try {
      const features = await getFeatures();
      const providers = features.features['oauth_providers'];
      this.oauthProviders = Array.isArray(providers) ? providers : [];
      this.registrationEnabled = features.features['registration'] !== false;
      this.passkeysEnabled =
        features.features['passkeys'] !== false && passkeysSupported();
      this.multiAccount = hasCapability(features.features, 'multi_account');
    } catch (error) {
      this.oauthProviders = [];
      // Fail open, matching the /register route guard.
      this.registrationEnabled = true;
      this.passkeysEnabled = false;
    }
  }

  private _navigateAfterLogin() {
    const redirectPath = localStorage.getItem('loginRedirect');
    if (redirectPath) {
      localStorage.removeItem('loginRedirect');
      if (redirectPath.startsWith('/admin')) {
        // The admin dashboard is a separate SPA that the console's
        // client-side router cannot reach; do a hard navigation.
        window.location.href = redirectPath;
      } else {
        Router.go(redirectPath);
      }
    } else {
      Router.go('/console');
    }
  }

  /**
   * After tokens are stored: open the account chooser when the person has
   * several accounts and no last used one, otherwise continue as before.
   * Any failure here falls back to the normal redirect.
   */
  private async _continueAfterSignIn(data: object) {
    const lastActive = (data as { last_active_account_id?: unknown })
      .last_active_account_id;
    if (this.multiAccount && !lastActive) {
      try {
        const [{ getMemberships }] = await Promise.all([
          import('../../hierarchy-api'),
          import('../authed/hierarchy/account-login-chooser'),
        ]);
        const memberships = await getMemberships();
        if (memberships.length > 1) {
          this.chooserMemberships = memberships;
          return;
        }
      } catch {
        // No memberships endpoint: a single account, carry on.
      }
    }
    this._navigateAfterLogin();
  }

  private async handlePasskeySignIn() {
    this.error = '';
    try {
      const data = await signInWithPasskey();
      localStorage.setItem('accessToken', data.access_token);
      if (data.refresh_token) {
        localStorage.setItem('refreshToken', data.refresh_token);
      }
      trackGoal('Login');
      window.dispatchEvent(
        new CustomEvent('auth-change', { bubbles: true, composed: true })
      );
      await this._continueAfterSignIn(data);
    } catch (error) {
      // A cancelled ceremony (user dismissed the prompt) is not an error
      // worth showing.
      const message =
        error instanceof Error ? error.message : 'Passkey sign-in failed';
      if (!message.includes('cancelled')) {
        this.error = message;
      }
      console.error('Passkey sign in failed', error);
    }
  }

  private async handleLogin(event: SubmitEvent) {
    event.preventDefault();
    const form = event.target as HTMLFormElement;
    const formData = new FormData(form);
    const username = formData.get('username') as string;
    const password = formData.get('password') as string;

    try {
      const data = await post('/api/v1/auth/token/json', {
        username,
        password,
      });

      localStorage.setItem('accessToken', data.access_token);
      // Without the refresh token in storage, refreshToken() cannot renew the
      // session and the user is hard-logged-out when the access token expires.
      if (data.refresh_token) {
        localStorage.setItem('refreshToken', data.refresh_token);
      }
      this.error = '';
      this.successMessage = '';
      // Returning-user conversion: distinguishes sign-ins from new signups.
      trackGoal('Login');
      window.dispatchEvent(
        new CustomEvent('auth-change', { bubbles: true, composed: true })
      );
      await this._continueAfterSignIn(data);
    } catch (error) {
      if (error instanceof Error) {
        this.error = error.message;
      } else {
        this.error = 'Invalid username or password';
      }
      // The password was right; the address is not verified yet. Say so and
      // offer the one action that fixes it, instead of leaving the person
      // retyping a password that already works.
      if (error instanceof ApiError && error.code === 'email_not_verified') {
        this.unverifiedEmail = this._resendTarget(error, username);
      } else {
        this.unverifiedEmail = '';
      }
      console.error('Sign in failed', error);
    }
  }

  /**
   * Where to send the new link.
   *
   * The server echoes the account's address on the refusal, which is the
   * reliable answer: the sign-in field may hold a username. Falling back to
   * what was typed still works when it was an address.
   */
  private _resendTarget(error: ApiError, typed: string): string {
    const fromServer = error.detail?.email;
    if (typeof fromServer === 'string' && fromServer.trim()) {
      return fromServer.trim();
    }
    return typed.includes('@') ? typed : '';
  }

  private async _handleResend() {
    if (!this.unverifiedEmail || this.resending) return;
    this.resending = true;
    try {
      this.successMessage = await resendVerificationEmail(this.unverifiedEmail);
      this.error = '';
      this.unverifiedEmail = '';
    } catch (error) {
      // Includes the rate limit: the server's sentence already says to wait.
      this.error =
        error instanceof Error
          ? error.message
          : 'Could not send another verification email. Try again shortly.';
    } finally {
      this.resending = false;
    }
  }

  private _renderPasskeyButton() {
    if (!this.passkeysEnabled) return nothing;
    return html`
      <sl-button
        class="oauth-button"
        variant="default"
        size="large"
        @click=${this.handlePasskeySignIn}
      >
        <sl-icon name="fingerprint" slot="prefix"></sl-icon>
        Sign in with passkey
      </sl-button>
    `;
  }

  private _renderOAuthButtons() {
    if (this.oauthProviders.length === 0 && !this.passkeysEnabled)
      return nothing;

    return html`
      <div class="oauth-section">
        ${this._renderPasskeyButton()}
        ${this.oauthProviders.map((provider) => {
          const config = OAUTH_PROVIDER_CONFIG[provider];
          if (!config) return nothing;
          return html`
            <sl-button
              class="oauth-button"
              variant="default"
              size="large"
              @click=${() => {
                window.location.href = `/api/v1/auth/oauth/${provider}/authorize`;
              }}
            >
              <sl-icon name="${config.icon}" slot="prefix"></sl-icon>
              Sign in with ${config.label}
            </sl-button>
          `;
        })}
      </div>
      <div class="divider">or sign in with email</div>
    `;
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
          <h2>Sign in to ${getBrandConfig().name}</h2>
          ${
            this.chooserMemberships
              ? html`<account-login-chooser
                  .memberships=${this.chooserMemberships}
                  .navigate=${() => this._navigateAfterLogin()}
                ></account-login-chooser>`
              : this._renderSignInForm()
          }
        </div>
      </div>
    `;
  }

  private _renderSignInForm() {
    return html`
      ${
        this.successMessage
          ? html`<div class="success-message">${this.successMessage}</div>`
          : ''
      }
      ${
        this.error
          ? html`<div class="error-message">
              ${this.error}
              ${
                this.unverifiedEmail
                  ? html`<div class="resend-row">
                      <sl-button
                        id="resend-verification"
                        size="small"
                        variant="default"
                        ?loading=${this.resending}
                        @click=${this._handleResend}
                      >
                        Send a new verification email
                      </sl-button>
                    </div>`
                  : nothing
              }
            </div>`
          : ''
      }
      ${this._renderOAuthButtons()}
      <form @submit=${this.handleLogin}>
        <div class="form-group">
          <sl-input
            label="Username"
            id="username"
            name="username"
            required
          ></sl-input>
        </div>
        <div class="form-group">
          <sl-input
            type="password"
            label="Password"
            id="password"
            name="password"
            required
            password-toggle
          ></sl-input>
        </div>
        <div class="form-actions">
          <sl-button type="submit" variant="primary" style="width: 100%;"
            >Sign in</sl-button
          >
        </div>
        <div class="form-links">
          <a href="/forgot-password">Forgot Password?</a>
          ${
            this.registrationEnabled
              ? html` &middot; <a href="/register">Create Account</a>`
              : nothing
          }
        </div>
      </form>
    `;
  }
}
