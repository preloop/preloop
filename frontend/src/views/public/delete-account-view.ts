import { REST_API_PREFIX } from '../../api-path';
import { LitElement, html, css } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import { formStyles } from '../../styles/form-styles';
import { fetchWithAuth } from '../../api';

import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/textarea/textarea.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/details/details.js';
import '../../components/logo-component';

/** Longest reason the server accepts. */
export const REASON_MAX_LENGTH = 1000;

const DELETION_URL = REST_API_PREFIX + '/account/deletion-request';

type DeletionStatus = 'received' | 'confirmation_sent' | 'sign_in_required';

/**
 * Public "delete my account" page (also linked from app store listings).
 *
 * Signed in, the request goes with the session and the server takes the
 * account from it. Signed out, the page asks for the account's address; the
 * server mails a confirmation link to it and the request reaches the team
 * only once that link is followed.
 */
@customElement('delete-account-view')
export class DeleteAccountView extends LitElement {
  @state() private _signedIn = false;
  @state() private _email = '';
  @state() private _reason = '';
  @state() private _submitting = false;
  @state() private _error: string | null = null;
  @state() private _outcome: DeletionStatus | null = null;
  @state() private _message = '';

  connectedCallback() {
    super.connectedCallback();
    this._signedIn = Boolean(localStorage.getItem('accessToken'));
    const params = new URLSearchParams(window.location.search);
    this._email = params.get('email') || '';
  }

  static styles = [
    formStyles,
    css`
      h2 {
        text-align: center;
        margin-bottom: 1rem;
      }
      .subtitle {
        text-align: center;
        color: var(--sl-color-neutral-600);
        margin-bottom: 2rem;
      }
      sl-input,
      sl-textarea {
        margin: 1rem 0;
      }
      sl-button {
        width: 100%;
        margin-top: 2rem;
      }
      .info-section {
        margin: 2rem 0;
        padding: 1.5rem;
        background: var(--sl-color-neutral-50);
        border-radius: var(--sl-border-radius-medium);
      }
      .info-section h3 {
        margin-top: 0;
        font-size: 1rem;
        color: var(--sl-color-neutral-700);
      }
      .info-section ul {
        margin: 0.5rem 0;
        padding-left: 1.5rem;
      }
      .info-section li {
        margin: 0.5rem 0;
        color: var(--sl-color-neutral-600);
      }
      sl-details {
        margin: 1rem 0;
      }
      sl-details::part(summary) {
        font-weight: 600;
      }
      .warning {
        color: var(--sl-color-danger-600);
        font-weight: 500;
      }
    `,
  ];

  private async _handleSubmit(event: Event) {
    event.preventDefault();
    this._submitting = true;
    this._error = null;

    const body = JSON.stringify(
      this._signedIn ? { reason: this._reason } : { email: this._email }
    );
    const init: RequestInit = {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body,
    };

    try {
      const response = this._signedIn
        ? await fetchWithAuth(DELETION_URL, init)
        : await fetch(DELETION_URL, init);
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        const detail = typeof data?.detail === 'string' ? data.detail : null;
        throw new Error(detail || 'request failed');
      }
      this._outcome = data.status as DeletionStatus;
      this._message = data.message || '';
    } catch (e) {
      const detail = e instanceof Error ? e.message : '';
      this._error =
        detail && detail !== 'request failed'
          ? detail
          : 'Failed to submit request. Please try again or contact support@preloop.ai';
    } finally {
      this._submitting = false;
    }
  }

  private _renderOutcome() {
    if (this._outcome === 'received') {
      return html`
        <sl-alert variant="success" open>
          <sl-icon slot="icon" name="check2-circle"></sl-icon>
          <strong>Request Received</strong><br />
          ${this._message}
        </sl-alert>
      `;
    }
    if (this._outcome === 'confirmation_sent') {
      return html`
        <sl-alert variant="primary" open>
          <sl-icon slot="icon" name="envelope"></sl-icon>
          <strong>Check your email</strong><br />
          ${this._message}
        </sl-alert>
      `;
    }
    return html`
      <sl-alert variant="warning" open>
        <sl-icon slot="icon" name="box-arrow-in-right"></sl-icon>
        <strong>Sign in required</strong><br />
        ${this._message}
        <p><a href="/login">Sign in</a>, then open this page again.</p>
      </sl-alert>
    `;
  }

  render() {
    if (this._outcome) {
      return html`<div class="container">${this._renderOutcome()}</div>`;
    }

    return html`
      <div class="container">
        <div class="logo">
          <a href="/">
            <logo-component></logo-component>
          </a>
        </div>
        <div class="form-container">
          <h2>Delete Your Account</h2>
          <p class="subtitle">
            Request deletion of your Preloop account and associated data
          </p>

          <div class="info-section">
            <h3>What happens when you delete your account:</h3>
            <ul>
              <li>Your user profile and authentication credentials</li>
              <li>
                Your mobile device registrations and push notification settings
              </li>
              <li>Your approval request history and decisions</li>
              <li>
                Your organization membership (if sole owner, org data may be
                retained for other members)
              </li>
            </ul>

            <sl-details summary="Data retention information">
              <p>Upon account deletion:</p>
              <ul>
                <li>
                  <strong>Immediately deleted:</strong> Login credentials,
                  device tokens, personal preferences
                </li>
                <li>
                  <strong>Deleted within 30 days:</strong> Approval history,
                  activity logs
                </li>
                <li>
                  <strong>Retained for compliance:</strong> Billing records may
                  be retained for up to 7 years as required by law
                </li>
              </ul>
            </sl-details>

            <p class="warning">
              ⚠️ This action cannot be undone. You will need to create a new
              account if you wish to use Preloop again.
            </p>
          </div>

          <form @submit=${this._handleSubmit}>
            ${
              this._signedIn
                ? html`<p class="signed-in">
                    You are signed in. The request is made for the account you
                    are signed in to; only an account owner can make it.
                  </p>`
                : html`<sl-input
                    label="Email Address"
                    type="email"
                    required
                    help-text="The verified email address of your Preloop account. We will send a link to it to confirm the request."
                    .value=${this._email}
                    @sl-input=${(e: any) => (this._email = e.target.value)}
                  ></sl-input>`
            }
            ${
              this._signedIn
                ? html`<sl-textarea
                    label="Reason for leaving (optional)"
                    resize="auto"
                    maxlength=${REASON_MAX_LENGTH}
                    help-text="Help us improve by sharing why you're deleting your account"
                    .value=${this._reason}
                    @sl-input=${(e: any) => (this._reason = e.target.value)}
                  ></sl-textarea>`
                : html`<p class="reason-later">
                    You can tell us why you are leaving on the confirmation
                    page.
                  </p>`
            }
            ${
              this._error
                ? html`<sl-alert variant="danger" open
                    >${this._error}</sl-alert
                  >`
                : ''
            }

            <sl-button
              type="submit"
              variant="danger"
              ?loading=${this._submitting}
            >
              Request Account Deletion
            </sl-button>
          </form>
        </div>
      </div>
    `;
  }
}
