import { LitElement, html, css } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';

@customElement('global-notice')
export class GlobalNotice extends LitElement {
  @state() private _message = '';

  connectedCallback() {
    super.connectedCallback();
    this._checkUrlForMessage();
  }

  private _checkUrlForMessage() {
    const urlParams = new URLSearchParams(window.location.search);
    const message = urlParams.get('message');
    if (message) {
      this._message = message.replace(/_/g, ' '); // Replace underscores with spaces
      // Clean the URL after we have captured the message
      const newUrl = `${window.location.pathname}`;
      window.history.replaceState({}, '', newUrl);
    }
  }

  private _handleClose() {
    this._message = '';
  }

  static styles = css`
    .notice {
      position: fixed;
      top: 1rem;
      right: 1rem;
      z-index: 1000;
    }
  `;

  render() {
    if (!this._message) {
      return html``;
    }

    return html`
      <sl-alert
        class="notice"
        variant="primary"
        closable
        open
        @sl-hide=${this._handleClose}
      >
        <sl-icon slot="icon" name="info-circle"></sl-icon>
        ${this._message}
      </sl-alert>
    `;
  }
}
