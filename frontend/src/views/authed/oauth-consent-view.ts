import { LitElement, html, css, nothing } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import { Router } from '@vaadin/router';
import { fetchWithAuth } from '../../api';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/divider/divider.js';

@customElement('oauth-consent-view')
export class OAuthConsentView extends LitElement {
  @state() private clientId = '';
  @state() private clientName = '';
  @state() private redirectUri = '';
  @state() private codeChallenge = '';
  @state() private stateParam = '';
  @state() private scopes = '';
  @state() private resource = '';
  @state() private redirectUriProvidedExplicitly = 'true';
  @state() private error = '';
  @state() private manualCode = '';
  @state() private loading = false;
  @state() private signupRequested = false;

  static styles = css`
    :host {
      display: flex;
      justify-content: center;
      align-items: center;
      height: 100%;
      width: 100%;
      padding: 1rem;
    }

    .container {
      background: var(--sl-color-neutral-0);
      border: 1px solid var(--sl-color-neutral-200);
      border-radius: var(--sl-border-radius-large);
      padding: 2.5rem;
      width: 100%;
      max-width: 480px;
      box-shadow: var(--sl-shadow-large);
      text-align: center;
    }

    .dark-theme .container {
      background: var(--sl-color-neutral-100);
      border-color: var(--sl-color-neutral-300);
    }

    .header-icon {
      font-size: 3rem;
      color: var(--sl-color-primary-600);
      margin-bottom: 1rem;
    }

    h1 {
      font-size: var(--sl-font-size-x-large);
      margin: 0 0 0.5rem 0;
      color: var(--sl-color-neutral-900);
    }

    p {
      color: var(--sl-color-neutral-600);
      line-height: var(--sl-line-height-normal);
      margin-bottom: 1.5rem;
      font-size: var(--sl-font-size-medium);
    }

    .actions {
      display: flex;
      flex-direction: column;
      gap: 1rem;
      margin-top: 2rem;
    }

    sl-button {
      width: 100%;
    }

    .manual-code-box {
      margin: 1.5rem 0;
      padding: 1rem;
      border-radius: var(--sl-border-radius-medium);
      background: var(--sl-color-neutral-50);
      border: 1px solid var(--sl-color-neutral-200);
      font-family: var(--sl-font-mono);
      font-size: var(--sl-font-size-large);
      color: var(--sl-color-neutral-900);
      word-break: break-all;
    }

    .error-box {
      background: var(--sl-color-danger-50);
      border: 1px solid var(--sl-color-danger-200);
      color: var(--sl-color-danger-700);
      padding: 1rem;
      border-radius: var(--sl-border-radius-medium);
      margin-bottom: 1.5rem;
      text-align: left;
    }
  `;

  connectedCallback() {
    super.connectedCallback();
    const urlParams = new URLSearchParams(window.location.search);
    this.clientId = urlParams.get('client_id') || 'cli';
    this.clientName = urlParams.get('client_name') || 'Preloop CLI';
    this.redirectUri = urlParams.get('redirect_uri') || '';
    this.codeChallenge = urlParams.get('code_challenge') || '';
    this.stateParam = urlParams.get('state') || '';
    this.scopes = urlParams.get('scopes') || '';
    this.resource = urlParams.get('resource') || '';
    this.redirectUriProvidedExplicitly =
      urlParams.get('redirect_uri_provided_explicitly') || 'true';
    this.signupRequested = urlParams.get('signup') === '1';

    // If the user is not authenticated, route them to the right entry point
    // (sign-up if the CLI requested signup, otherwise sign-in) and remember
    // this URL so they end up back on the consent page once they're logged
    // in. Without this the user lands on a "Authorize" button they can't use
    // and there is no obvious path forward for new users.
    if (typeof window !== 'undefined' && !localStorage.getItem('accessToken')) {
      try {
        localStorage.setItem(
          'loginRedirect',
          window.location.pathname +
            window.location.search +
            window.location.hash
        );
      } catch (e) {
        // Ignore localStorage failures - worst case the user has to navigate back manually.
      }
      const target = this.signupRequested ? '/register' : '/login';
      Router.go(target);
    }
  }

  private async handleAuthorize() {
    this.error = '';
    this.loading = true;

    try {
      const response = await fetchWithAuth('/api/v1/auth/oauth-consent', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          client_id: this.clientId,
          redirect_uri: this.redirectUri,
          code_challenge: this.codeChallenge,
          state: this.stateParam,
          scopes: this.scopes,
          redirect_uri_provided_explicitly: this.redirectUriProvidedExplicitly,
          resource: this.resource,
        }),
      });

      if (!response.ok) {
        let errorMessage = 'Failed to authorize client.';
        try {
          const errorData = await response.json();
          if (errorData.detail) {
            errorMessage =
              typeof errorData.detail === 'string'
                ? errorData.detail
                : JSON.stringify(errorData.detail);
          }
        } catch (e) {}
        throw new Error(errorMessage);
      }

      const resp = await response.json();

      if (resp.action === 'manual') {
        this.manualCode = resp.code;
      } else if (resp.action === 'redirect' && resp.redirect_url) {
        window.location.href = resp.redirect_url;
      }
    } catch (e: any) {
      console.error(e);
      this.error = e.message || 'Failed to authorize client.';
    } finally {
      this.loading = false;
    }
  }

  private handleCancel() {
    Router.go('/console');
  }

  render() {
    if (this.manualCode) {
      return html`
        <div class="container">
          <sl-icon
            name="check-circle"
            class="header-icon"
            style="color: var(--sl-color-success-600);"
          ></sl-icon>
          <h1>Authorization Successful</h1>
          <p><strong>${this.clientName}</strong> is ready to continue.</p>
          <p>
            Copy this one-time code and paste it back into your application.
          </p>
          <div class="manual-code-box">${this.manualCode}</div>
          <div class="actions">
            <sl-button
              variant="primary"
              size="large"
              @click=${() => Router.go('/console/agents?cli=connected')}
            >
              Continue to your agents
            </sl-button>
          </div>
          <p style="font-size: var(--sl-font-size-small); margin-top: 1.5rem;">
            You can also securely close this window.
          </p>
        </div>
      `;
    }

    return html`
      <div class="container">
        <sl-icon name="shield-lock" class="header-icon"></sl-icon>
        <h1>Authorize access?</h1>
        <p>
          <strong>${this.clientName}</strong> wants to connect to your Preloop
          account.
        </p>

        ${
          this.error
            ? html`<div class="error-box">
                <sl-icon
                  name="exclamation-triangle"
                  style="margin-right: 0.5rem;"
                ></sl-icon>
                ${this.error}
              </div>`
            : nothing
        }

        <sl-divider></sl-divider>
        <p
          style="text-align: left; font-size: var(--sl-font-size-small); margin: 1rem 0;"
        >
          This will allow ${this.clientName} to perform operations on your
          behalf, governed by your workspace policies.
        </p>

        <div class="actions">
          <sl-button
            variant="primary"
            size="large"
            ?loading=${this.loading}
            @click=${this.handleAuthorize}
          >
            Authorize Application
          </sl-button>
          <sl-button
            variant="default"
            size="large"
            ?disabled=${this.loading}
            @click=${this.handleCancel}
          >
            Cancel
          </sl-button>
        </div>
      </div>
    `;
  }
}
