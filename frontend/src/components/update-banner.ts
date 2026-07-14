import { LitElement, html, css } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import { fetchWithAuth } from '../api';

interface VersionStatus {
  version: string;
  update_available: boolean;
  latest_version: string | null;
  update_url: string | null;
  changelog_url: string | null;
  checked_at: string | null;
  telemetry_enabled: boolean;
}

const DISMISS_KEY = 'updateBannerDismissed';

/**
 * Update-available banner for self-hosted administrators.
 *
 * Renders only for superusers when the instance's daily version check (see
 * backend GET /api/v1/version/status) reports a newer Preloop release.
 * Dismissal is per-version: the banner returns for the next release.
 * Instances with telemetry disabled never see it (no check result exists).
 */
@customElement('update-banner')
export class UpdateBanner extends LitElement {
  @state() private _status: VersionStatus | null = null;
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
      background: var(--sl-color-primary-600, #4a4bd6);
      color: #fff;
      font-size: 0.875rem;
    }
    .banner a {
      color: #fff;
      font-weight: 600;
      text-decoration: underline;
    }
    .dismiss {
      background: none;
      border: none;
      color: #fff;
      cursor: pointer;
      font-size: 1rem;
      line-height: 1;
      padding: 0.25rem;
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

  private async _check() {
    if (!localStorage.getItem('accessToken')) {
      this._status = null;
      return;
    }
    try {
      const response = await fetchWithAuth('/api/v1/version/status');
      if (!response.ok) {
        // Non-superusers (403) and older backends (404) simply get no banner.
        this._status = null;
        return;
      }
      const status: VersionStatus = await response.json();
      this._status = status;
      this._dismissed =
        localStorage.getItem(DISMISS_KEY) === status.latest_version;
    } catch {
      this._status = null;
    }
  }

  private _dismiss() {
    if (this._status?.latest_version) {
      localStorage.setItem(DISMISS_KEY, this._status.latest_version);
    }
    this._dismissed = true;
  }

  render() {
    const status = this._status;
    if (!status || !status.update_available || this._dismissed) {
      return html``;
    }
    const url =
      status.update_url ||
      status.changelog_url ||
      'https://docs.preloop.ai/upgrade';
    return html`
      <div class="banner" role="status">
        <span>
          Preloop ${status.latest_version} is available — you are running
          ${status.version}.
        </span>
        <a href=${url} target="_blank" rel="noopener">View upgrade guide</a>
        <button
          class="dismiss"
          aria-label="Dismiss update notice"
          @click=${this._dismiss}
        >
          ✕
        </button>
      </div>
    `;
  }
}
