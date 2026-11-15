import { LitElement, html, css } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';

/**
 * The notices a link may ask for with `?message=<key>`. Only these keys are
 * shown, as this fixed copy: free text from the URL never reaches the
 * console banner, where a crafted link could put any words in Preloop's
 * mouth (01-16).
 */
export const GLOBAL_NOTICE_MESSAGES: Record<string, string> = {
  // Nothing in Preloop links here with a message today. Add a key and its
  // copy when a redirect needs one.
};

@customElement('global-notice')
export class GlobalNotice extends LitElement {
  @state() private _message = '';

  connectedCallback() {
    super.connectedCallback();
    this._checkUrlForMessage();
  }

  private _checkUrlForMessage() {
    const url = new URL(window.location.href);
    const key = url.searchParams.get('message');
    if (key === null) return;
    const message = Object.prototype.hasOwnProperty.call(
      GLOBAL_NOTICE_MESSAGES,
      key
    )
      ? GLOBAL_NOTICE_MESSAGES[key]
      : '';
    if (!message) return;
    this._message = message;
    // Drop only the parameter this read; the page's own filters stay.
    url.searchParams.delete('message');
    window.history.replaceState(
      {},
      '',
      `${url.pathname}${url.search}${url.hash}`
    );
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
