import { LitElement, html, css } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import { formStyles } from '../../styles/form-styles';

import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/textarea/textarea.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/details/details.js';
import '../../components/logo-component';

@customElement('delete-account-view')
export class DeleteAccountView extends LitElement {
  @state() private _email = '';
  @state() private _username = '';
  @state() private _accountId = '';
  @state() private _orgName = '';
  @state() private _reason = '';
  @state() private _submitting = false;
  @state() private _error: string | null = null;
  @state() private _success = false;

  connectedCallback() {
    super.connectedCallback();

    // Parse URL parameters
    const params = new URLSearchParams(window.location.search);
    this._email = params.get('email') || '';
    this._username = params.get('username') || '';
    this._accountId = params.get('account_id') || '';
    this._orgName = params.get('org_name') || '';
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
    this._success = false;

    try {
      const response = await fetch('/api/v1/account/deletion-request', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          email: this._email,
          username: this._username,
          account_id: this._accountId,
          org_name: this._orgName,
          reason: this._reason,
        }),
      });

      if (!response.ok) {
        throw new Error('Network response was not ok');
      }

      this._submitting = false;
      this._success = true;
    } catch (e) {
      this._submitting = false;
      this._error =
        'Failed to submit request. Please try again or contact support@preloop.ai';
    }
  }

  render() {
    if (this._success) {
      return html`
        <div class="container">
          <sl-alert variant="success" open>
            <sl-icon slot="icon" name="check2-circle"></sl-icon>
            <strong>Request Received</strong><br />
            Your account deletion request has been submitted. We will process
            your request within 30 days and send a confirmation to your email
            address.
          </sl-alert>
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
            <sl-input
              label="Email Address"
              type="email"
              required
              help-text="The email address associated with your Preloop account"
              .value=${this._email}
              @sl-input=${(e: any) => (this._email = e.target.value)}
            ></sl-input>

            <sl-input
              label="Username"
              required
              help-text="Your Preloop username"
              .value=${this._username}
              @sl-input=${(e: any) => (this._username = e.target.value)}
            ></sl-input>

            ${
              this._orgName
                ? html`<sl-input
                    label="Organization"
                    readonly
                    .value=${this._orgName}
                  ></sl-input>`
                : ''
            }

            <sl-textarea
              label="Reason for leaving (optional)"
              resize="auto"
              help-text="Help us improve by sharing why you're deleting your account"
              .value=${this._reason}
              @sl-input=${(e: any) => (this._reason = e.target.value)}
            ></sl-textarea>

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
