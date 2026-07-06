import { LitElement, html, css, nothing } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import { Router } from '@vaadin/router';
import { post, getFeatures } from '../../api';
import { formStyles } from '../../styles/form-styles';
import { getBrandConfig } from '../../brand-config';

import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '../../components/logo-component';

const OAUTH_PROVIDER_CONFIG: Record<string, { label: string; icon: string }> = {
  github: { label: 'GitHub', icon: 'github' },
  google: { label: 'Google', icon: 'google' },
  gitlab: { label: 'GitLab', icon: 'gitlab' },
};

@customElement('register-view')
export class RegisterView extends LitElement {
  @state()
  private error = '';

  @state()
  private _loading = false;

  @state()
  private oauthProviders: string[] = [];

  @state()
  private _billingEnabled = false;

  static styles = [
    formStyles,
    css`
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
    this._checkFeatures();
  }

  private async _checkFeatures() {
    try {
      const features = await getFeatures();
      const providers = features.features['oauth_providers'];
      this.oauthProviders = Array.isArray(providers) ? providers : [];
      this._billingEnabled = features.features['billing'] === true;
    } catch (error) {
      this.oauthProviders = [];
      this._billingEnabled = false;
    }
  }

  private async handleRegister(event: SubmitEvent) {
    event.preventDefault();
    this._loading = true;
    const form = event.target as HTMLFormElement;
    const formData = new FormData(form);
    const username = formData.get('username') as string;
    const email = formData.get('email') as string;
    const password = formData.get('password') as string;

    try {
      const registerResult = await post('/api/v1/auth/register', {
        username,
        email,
        password,
      });

      // If the backend returns an error in the payload instead of throwing an HTTP error
      if (registerResult && registerResult.error) {
        throw new Error(registerResult.error);
      }

      if (this._billingEnabled) {
        try {
          const authData = await post('/api/v1/auth/token/json', {
            username,
            password,
          });

          if (authData && authData.access_token) {
            localStorage.setItem('accessToken', authData.access_token);

            const response = await fetch(
              '/api/v1/billing/create-checkout-session',
              {
                method: 'POST',
                headers: {
                  'Content-Type': 'application/json',
                  Authorization: `Bearer ${authData.access_token}`,
                },
                body: JSON.stringify({
                  plan_id: 'teams',
                  interval: 'month',
                }),
              }
            );

            if (response.ok) {
              const result = await response.json();
              if (result.action === 'redirect' && result.url) {
                window.location.href = result.url;
                return;
              }
            }
          }
        } catch (checkoutError) {
          console.error(
            'Failed to create checkout session after registration',
            checkoutError
          );
        }
      }

      // Try to auto-log-in the user using the credentials they just submitted
      // and continue any pending flow (eg. CLI OAuth consent at
      // /console/authorize) instead of bouncing them back to the sign-in
      // page. This is the seamless path for new users running
      // `curl ... | sh` -> `preloop signup` -> sign up -> back to CLI.
      try {
        const authData = await post('/api/v1/auth/token/json', {
          username,
          password,
        });
        if (authData && authData.access_token) {
          localStorage.setItem('accessToken', authData.access_token);
          if (authData.refresh_token) {
            localStorage.setItem('refreshToken', authData.refresh_token);
          }
          window.dispatchEvent(
            new CustomEvent('auth-change', { bubbles: true, composed: true })
          );
          this._loading = false;
          const redirectPath = localStorage.getItem('loginRedirect');
          if (redirectPath) {
            localStorage.removeItem('loginRedirect');
            Router.go(redirectPath);
          } else {
            Router.go('/console');
          }
          return;
        }
      } catch (autoLoginError) {
        // Silently fall back to the sign-in page below.
        console.warn(
          'Auto-login after registration failed, falling back to sign-in page',
          autoLoginError
        );
      }

      // Fallback: send the user to the sign-in page (loginRedirect, if set,
      // is preserved across this navigation).
      this._loading = false;
      Router.go('/login?registered=true');
    } catch (error) {
      this._loading = false;
      if (error instanceof Error) {
        this.error = error.message;
      } else {
        this.error = 'Failed to create an account';
      }
      console.error('Create account failed', error);
      // Ensure we don't proceed with checkout if registration failed
      return;
    }
  }

  private _renderOAuthButtons() {
    if (this.oauthProviders.length === 0) return nothing;

    return html`
      <div class="oauth-section">
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
              Sign up with ${config.label}
            </sl-button>
          `;
        })}
      </div>
      <div class="divider">or sign up with email</div>
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
          <h2>Create a ${getBrandConfig().name} account</h2>
          ${
            this.error
              ? html`<div class="error-message">${this.error}</div>`
              : ''
          }
          ${this._renderOAuthButtons()}
          <form @submit=${this.handleRegister}>
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
                type="email"
                label="Email"
                id="email"
                name="email"
                required
              ></sl-input>
            </div>
            <div class="form-group">
              <sl-input
                type="password"
                label="Password"
                id="password"
                name="password"
                minlength="8"
                required
                password-toggle
              ></sl-input>
            </div>
            <div class="form-actions">
              <sl-button
                type="submit"
                variant="primary"
                ?loading=${this._loading}
                >Create account</sl-button
              >
            </div>
            <div class="form-links">
              <a href="/login">Already have an account? Sign In</a>
            </div>
          </form>
        </div>
      </div>
    `;
  }
}
