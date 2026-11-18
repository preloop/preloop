import { REST_API_PREFIX } from '../../api-path';
import { LitElement, html } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import { formStyles } from '../../styles/form-styles';
import { ApiError, post } from '../../api';

import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/textarea/textarea.js';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '../../components/logo-component';

export const CONFIRM_LINK_ERROR = 'This link is invalid or has expired.';

/**
 * Landing page of the emailed deletion confirmation link.
 *
 * Nothing is sent on load: mail scanners open links, so the request is
 * confirmed only when the person presses the button.
 */
@customElement('delete-account-confirm-view')
export class DeleteAccountConfirmView extends LitElement {
  @state() private _token = '';
  @state() private _reason = '';
  @state() private _submitting = false;
  @state() private _error = '';
  @state() private _done: { status: string; message: string } | null = null;

  static styles = [formStyles];

  connectedCallback() {
    super.connectedCallback();
    this._token =
      new URLSearchParams(window.location.search).get('token') || '';
    if (!this._token) this._error = CONFIRM_LINK_ERROR;
  }

  private async _confirm() {
    this._submitting = true;
    this._error = '';
    try {
      this._done = await post(
        REST_API_PREFIX + '/account/deletion-request/confirm',
        { token: this._token, reason: this._reason }
      );
    } catch (error) {
      this._error =
        error instanceof ApiError && error.status === 400
          ? CONFIRM_LINK_ERROR
          : 'Could not confirm the request. Try again in a moment.';
    } finally {
      this._submitting = false;
    }
  }

  render() {
    return html`
      <div class="container">
        <div class="logo">
          <a href="/"><logo-component></logo-component></a>
        </div>
        <div class="form-container">
          <h2>Confirm account deletion</h2>
          ${
            this._done
              ? html`<sl-alert
                  variant=${this._done.status === 'received' ? 'success' : 'warning'}
                  open
                  >${this._done.message}</sl-alert
                >`
              : html`
                  <p>
                    Press the button to confirm that you want your account
                    deleted. Our team then processes the request within 30 days
                    and confirms by email.
                  </p>
                  <sl-textarea
                    label="Reason for leaving (optional)"
                    resize="auto"
                    maxlength="1000"
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
                    variant="danger"
                    ?disabled=${!this._token}
                    ?loading=${this._submitting}
                    @click=${this._confirm}
                    >Confirm deletion request</sl-button
                  >
                `
          }
        </div>
      </div>
    `;
  }
}
