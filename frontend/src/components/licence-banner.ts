import { REST_API_PREFIX } from '../api-path';
import { LitElement, html, css } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import { fetchWithAuth, getFeatures, onApiCachesInvalidated } from '../api';
import { editionOf } from '../capabilities';

export interface LicenceStatus {
  status: 'missing' | 'valid' | 'grace' | 'expired' | 'invalid';
  licence_id: string | null;
  grace_ends: string | null;
  banner: string | null;
}

const DISMISS_KEY = 'licenceBannerDismissed';

/**
 * Set once the endpoint answers 404: the route is not mounted on this server,
 * so asking again on the next navigation can only produce the same 404.
 */
let licenceRouteMissing = false;
onApiCachesInvalidated(() => {
  licenceRouteMissing = false;
});

/**
 * Only a self-hosted Enterprise build mounts the licence route. Cloud and the
 * open-source edition never do, so the console does not ask there at all.
 */
async function licenceRouteExpected(): Promise<boolean> {
  if (licenceRouteMissing) return false;
  try {
    return editionOf(await getFeatures()) === 'enterprise';
  } catch {
    return false;
  }
}

/**
 * Enterprise licence banner for self-hosted administrators.
 *
 * Reads GET /api/v1/licence/status, which only an Enterprise build mounts and
 * only superusers may read; a 403 or 404 renders nothing, so the open-source
 * console is unchanged. Other editions skip the request entirely and a 404 is
 * remembered for the session, so it is not logged on every navigation. The server writes the copy. A licence in its grace
 * period can be dismissed until the licence changes; an expired or
 * unverifiable licence cannot, because Enterprise features are already off.
 */
@customElement('licence-banner')
export class LicenceBanner extends LitElement {
  @state() private _status: LicenceStatus | null = null;
  @state() private _dismissed = false;

  static styles = css`
    :host {
      display: block;
    }
    .banner {
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 0.75rem;
      padding: 0.5rem 1rem;
      background: color-mix(
        in srgb,
        var(--sl-color-warning-500) 10%,
        var(--console-surface, transparent)
      );
      border-bottom: 1px solid
        color-mix(in srgb, var(--sl-color-warning-500) 35%, transparent);
      color: var(--sl-color-warning-800);
      font-size: 0.875rem;
    }
    .dismiss {
      background: none;
      border: none;
      color: inherit;
      cursor: pointer;
      font-size: 1rem;
      line-height: 1;
      min-width: 44px;
      min-height: 44px;
    }
  `;

  connectedCallback() {
    super.connectedCallback();
    this._check();
    window.addEventListener('auth-change', this._onAuthChange);
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    window.removeEventListener('auth-change', this._onAuthChange);
  }

  private _onAuthChange = () => {
    this._check();
  };

  private _dismissKey(status: LicenceStatus): string {
    return `${status.licence_id ?? ''}:${status.grace_ends ?? ''}`;
  }

  private async _check() {
    if (!localStorage.getItem('accessToken')) {
      this._status = null;
      return;
    }
    if (!(await licenceRouteExpected())) {
      this._status = null;
      return;
    }
    try {
      // Passive: a background read must not raise the 429 toast or 402 modal.
      const response = await fetchWithAuth(
        REST_API_PREFIX + '/licence/status',
        {
          passive: true,
        }
      );
      if (response.status === 404) {
        licenceRouteMissing = true;
      }
      if (!response.ok) {
        // Non-superusers (403) and community builds (404) get no banner.
        this._status = null;
        return;
      }
      const status: LicenceStatus = await response.json();
      this._status = status;
      this._dismissed =
        status.status === 'grace' &&
        localStorage.getItem(DISMISS_KEY) === this._dismissKey(status);
    } catch {
      this._status = null;
    }
  }

  private _dismiss() {
    if (this._status) {
      localStorage.setItem(DISMISS_KEY, this._dismissKey(this._status));
    }
    this._dismissed = true;
  }

  render() {
    const status = this._status;
    if (!status || !status.banner || this._dismissed) {
      return html``;
    }
    return html`
      <div class="banner" role="status">
        <span>${status.banner}</span>
        ${
          status.status === 'grace'
            ? html`<button
                class="dismiss"
                aria-label="Dismiss licence notice"
                @click=${this._dismiss}
              >
                ✕
              </button>`
            : ''
        }
      </div>
    `;
  }
}
