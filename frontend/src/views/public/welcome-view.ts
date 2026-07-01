import { LitElement, html, css } from 'lit';
import { customElement, state, query } from 'lit/decorators.js';
import { Router } from '@vaadin/router';
import * as api from '../../api';
import { formStyles } from '../../styles/form-styles';
import { getBrandConfig } from '../../brand-config';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '../../components/logo-component';

@customElement('welcome-view')
export class WelcomeView extends LitElement {
  @state() private _username = '';
  @state() private _email = '';
  @state() private _orgName = '';
  @state() private _needsPassword = true;
  @state() private _error = '';
  @state() private _loading = false;
  @query('#password') private _passwordInput?: HTMLInputElement;

  connectedCallback() {
    super.connectedCallback();
    const urlParams = new URLSearchParams(window.location.search);
    this._username = urlParams.get('username') || '';
    this._email = urlParams.get('email') || '';
    this._needsPassword = urlParams.get('needs_password') !== 'false';

    if (!this._email) {
      this._error = 'Could not retrieve your details. Please contact support.';
    }

    if (!this._needsPassword) {
      this._loadAccountDetails();
    }
  }

  private async _loadAccountDetails() {
    try {
      const token = localStorage.getItem('accessToken');
      if (token) {
        const details = await api.getAccountDetails();
        if (details && details.organization_name) {
          this._orgName = details.organization_name;
        }
      }
    } catch (error) {
      console.warn('Failed to load initial account details:', error);
    }
  }

  private async _saveAccountDetailsAndProceed(setupGithub: boolean) {
    this._loading = true;
    try {
      const token = localStorage.getItem('accessToken');
      if (token && this._orgName.trim()) {
        const updateAccountResponse = await api.fetchPublic(
          '/api/v1/account/details',
          {
            method: 'PATCH',
            headers: {
              'Content-Type': 'application/json',
              Authorization: `Bearer ${token}`,
            },
            body: JSON.stringify({
              organization_name: this._orgName.trim(),
            }),
          }
        );

        if (!updateAccountResponse.ok) {
          console.error(
            'Failed to update organization name:',
            await updateAccountResponse.text()
          );
        }
      }
    } catch (error) {
      console.error('Error updating organization name:', error);
    }

    if (setupGithub) {
      try {
        const { authorization_url, state } = await api.getGitHubAuthUrl();
        sessionStorage.setItem('github_oauth_state', state);
        sessionStorage.setItem('github_oauth_from_welcome', 'true');
        window.location.href = authorization_url;
        return; // Don't reset loading, let the redirect happen
      } catch (error: any) {
        this._error = error.message || 'Failed to start GitHub OAuth';
        this._loading = false;
        return;
      }
    } else {
      this._loading = false;
      Router.go('/console');
    }
  }

  private async _handleOnboardingSubmit(e?: Event) {
    if (e) {
      e.preventDefault();
    }

    // We only need to run the full onboarding flow if they need a password
    // Otherwise it's just saving the org name and going to the console
    if (!this._needsPassword) {
      return; // Handle via specific buttons below
    }

    this._loading = true;
    this._error = '';

    const password = this._passwordInput?.value;
    if (!password || password.length < 8) {
      this._error = 'Password must be at least 8 characters long.';
      this._loading = false;
      return;
    }

    try {
      const response = await api.fetchPublic(
        '/api/v1/auth/complete-onboarding',
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            email: this._email,
            username: this._username,
            password: password,
          }),
        }
      );

      if (!response.ok) {
        const errorData = await response.json();
        throw new Error(errorData.detail || 'Failed to complete onboarding.');
      }

      const data = await response.json();
      localStorage.setItem('accessToken', data.access_token);
      if (data.refresh_token) {
        localStorage.setItem('refreshToken', data.refresh_token);
      }

      this._loading = false;
      // After a successful password set, we just want to load the org name if it exists somehow
      // But typically this is a new user so they don't have one set yet.

      // Update UI to show the next step.
      this._needsPassword = false;
      await this._loadAccountDetails();
    } catch (error: any) {
      this._error = error.message;
      this._loading = false;
      return;
    }
  }

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
      }
    `,
  ];

  render() {
    if (this._error && !this._username) {
      return html`
        <div class="container">
          <div class="logo">
            <a href="/">
              <logo-component></logo-component>
            </a>
          </div>
          <div class="form-container">
            <div class="error-message">${this._error}</div>
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
          <h2>Welcome to ${getBrandConfig().name}!</h2>
          <p style="text-align: center; margin-bottom: 1.5rem;">
            ${
              this._needsPassword
                ? 'Your account has been created. Please set your password to continue.'
                : 'Your account is ready! Review your organization name and optionally connect GitHub to enable PR reviews and automation.'
            }
          </p>
          ${
            this._error
              ? html`<div class="error-message">${this._error}</div>`
              : ''
          }
          <form @submit=${this._handleOnboardingSubmit}>
            ${
              this._needsPassword
                ? html`
                    <div class="form-group">
                      <sl-input
                        label="Email"
                        value=${this._email}
                        readonly
                        disabled
                      ></sl-input>
                    </div>
                    <div class="form-group">
                      <sl-input
                        label="Organization"
                        value=${this._orgName}
                        @sl-change=${(e: any) => (this._orgName = e.target.value)}
                        required
                      ></sl-input>
                    </div>
                    <div class="form-group">
                      <sl-input
                        label="Username"
                        value=${this._username}
                        @sl-change=${(e: any) =>
                          (this._username = e.target.value)}
                      ></sl-input>
                    </div>
                    <div class="form-group">
                      <sl-input
                        id="password"
                        type="password"
                        label="Password"
                        required
                        password-toggle
                      ></sl-input>
                    </div>
                  `
                : html`
                    <div class="form-group">
                      <sl-input
                        label="Organization Name"
                        value=${this._orgName}
                        @sl-change=${(e: any) => (this._orgName = e.target.value)}
                        required
                        help-text="Set up your workspace name"
                      ></sl-input>
                    </div>
                  `
            }

            <div
              class="form-actions"
              style=${
                this._needsPassword
                  ? ''
                  : 'display: flex; gap: 1rem; flex-direction: column;'
              }
            >
              ${
                this._needsPassword
                  ? html`
                      <sl-button
                        type="submit"
                        variant="primary"
                        ?loading=${this._loading}
                        style="width: 100%;"
                      >
                        Complete Registration
                      </sl-button>
                    `
                  : html`
                      <sl-button
                        variant="primary"
                        @click=${() => this._saveAccountDetailsAndProceed(true)}
                        ?loading=${this._loading}
                        style="width: 100%;"
                      >
                        <sl-icon slot="prefix" name="github"></sl-icon>
                        Connect with GitHub
                      </sl-button>
                      <sl-button
                        variant="default"
                        @click=${() => this._saveAccountDetailsAndProceed(false)}
                        ?disabled=${this._loading}
                        style="width: 100%;"
                      >
                        Next
                      </sl-button>
                    `
              }
            </div>
          </form>
        </div>
      </div>
    `;
  }
}
