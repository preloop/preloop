import { html, css } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import { AuthedElement, getUserProfile } from '../../api';
import { unifiedWebSocketManager } from '../../services/unified-websocket-manager';
import '@shoelace-style/shoelace/dist/components/card/card.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/switch/switch.js';
import '@shoelace-style/shoelace/dist/components/select/select.js';
import '@shoelace-style/shoelace/dist/components/option/option.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import '@shoelace-style/shoelace/dist/components/qr-code/qr-code.js';
import '@shoelace-style/shoelace/dist/components/dialog/dialog.js';
import { consoleDialogStyles } from '../../styles/console-dialog';

interface NotificationPreferences {
  id: string;
  preferred_channel: string;
  enable_email: boolean;
  enable_mobile_push: boolean;
  stagger_email?: boolean;
  mobile_device_tokens?: Array<{
    platform: string;
    token: string;
    registered_at: string;
  }>;
}

interface QRCodeData {
  token: string;
  qr_data: string;
  expires_at: string;
  expires_in_seconds: number;
}

/** Per-device outcome of an admin diagnostic test send. */
interface TestPushDeviceResult {
  platform: string;
  token: string;
  transport: string;
  success: boolean;
  error?: string | null;
  error_reason?: string | null;
  remediation?: string | null;
  project_id?: string | null;
  status_code?: number | null;
}

interface TestPushResult {
  kind: string;
  request_id: string;
  sent: number;
  failed: number;
  results: TestPushDeviceResult[];
}

@customElement('notification-preferences-view')
export class NotificationPreferencesView extends AuthedElement {
  @state()
  private preferences: NotificationPreferences | null = null;

  @state()
  private isLoading = true;

  @state()
  private isSaving = false;

  @state()
  private errorMessage = '';

  @state()
  private successMessage = '';

  @state()
  private showQRDialog = false;

  @state()
  private qrCodeData: QRCodeData | null = null;

  @state()
  private qrExpiry: number = 0;

  @state()
  private isAdmin = false;

  @state()
  private isSendingTest: 'approval' | 'question' | null = null;

  @state()
  private testResult: TestPushResult | null = null;

  @state()
  private testError = '';

  private qrExpiryInterval: any = null;
  private unsubscribe?: () => void;
  private unsubscribeStateChange?: () => void;

  static styles = [
    consoleDialogStyles,
    css`
      :host {
        display: block;
        padding: var(--sl-spacing-large);
        max-width: 900px;
        margin: 0 auto;
      }

      .header {
        margin-bottom: var(--sl-spacing-large);
      }

      .header h1 {
        margin: 0 0 var(--sl-spacing-x-small) 0;
        font-size: var(--sl-font-size-2x-large);
        font-weight: var(--sl-font-weight-semibold);
        color: var(--sl-color-neutral-900);
      }

      .header p {
        margin: 0;
        color: var(--sl-color-neutral-600);
        font-size: var(--sl-font-size-medium);
      }

      .content {
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-large);
      }

      .section-title {
        font-size: var(--sl-font-size-large);
        font-weight: var(--sl-font-weight-semibold);
        margin: 0 0 var(--sl-spacing-medium) 0;
        color: var(--sl-color-neutral-900);
      }

      .preference-row {
        display: flex;
        justify-content: space-between;
        align-items: center;
        padding: var(--sl-spacing-medium);
        border: 1px solid var(--sl-color-neutral-200);
        border-radius: var(--sl-border-radius-medium);
      }

      .preference-label {
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-2x-small);
      }

      .preference-title {
        font-weight: var(--sl-font-weight-semibold);
        color: var(--sl-color-neutral-900);
      }

      .preference-description {
        font-size: var(--sl-font-size-small);
        color: var(--sl-color-neutral-600);
      }

      .devices-list {
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-small);
      }

      .device-item {
        display: flex;
        justify-content: space-between;
        align-items: center;
        padding: var(--sl-spacing-medium);
        border: 1px solid var(--sl-color-neutral-200);
        border-radius: var(--sl-border-radius-medium);
        background: var(--sl-color-neutral-50);
      }

      .device-info {
        display: flex;
        align-items: center;
        gap: var(--sl-spacing-medium);
      }

      .device-platform {
        display: flex;
        align-items: center;
        gap: var(--sl-spacing-x-small);
      }

      .device-details {
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-2x-small);
      }

      .device-token {
        font-family: var(--sl-font-mono);
        font-size: var(--sl-font-size-x-small);
        color: var(--sl-color-neutral-600);
      }

      .device-date {
        font-size: var(--sl-font-size-x-small);
        color: var(--sl-color-neutral-500);
      }

      .qr-container {
        display: flex;
        flex-direction: column;
        align-items: center;
        gap: var(--sl-spacing-medium);
        padding: var(--sl-spacing-large);
      }

      .qr-code-wrapper {
        padding: var(--sl-spacing-large);
        background: white;
        border-radius: var(--sl-border-radius-medium);
        box-shadow: var(--sl-shadow-large);
      }

      .qr-instructions {
        text-align: center;
        max-width: 400px;
      }

      .qr-instructions h4 {
        margin: 0 0 var(--sl-spacing-small) 0;
        font-size: var(--sl-font-size-large);
        font-weight: var(--sl-font-weight-semibold);
      }

      .qr-instructions p {
        margin: 0;
        color: var(--sl-color-neutral-600);
        line-height: 1.5;
      }

      .qr-expiry {
        font-size: var(--sl-font-size-small);
        color: var(--sl-color-warning-700);
        font-weight: var(--sl-font-weight-semibold);
      }

      .app-store-links {
        display: flex;
        justify-content: center;
        gap: var(--sl-spacing-medium);
        margin-top: var(--sl-spacing-large);
      }

      .app-store-button {
        display: inline-flex;
        align-items: center;
        gap: var(--sl-spacing-small);
        padding: var(--sl-spacing-medium) var(--sl-spacing-large);
        background: var(--sl-color-primary-600);
        color: var(--sl-color-neutral-0);
        text-decoration: none;
        border-radius: var(--sl-border-radius-medium);
        transition: all 0.2s ease;
        font-weight: var(--sl-font-weight-semibold);
        box-shadow: var(--sl-shadow-small);
      }

      .app-store-button:hover {
        background: var(--sl-color-primary-700);
        transform: translateY(-1px);
        box-shadow: var(--sl-shadow-medium);
      }

      .app-store-button sl-icon {
        font-size: 1.5rem;
      }

      .loading {
        display: flex;
        justify-content: center;
        align-items: center;
        min-height: 400px;
      }

      /* Admin test-send: a diagnostic panel, kept quiet until it has something
       to say. Colour is reserved for the pass/fail verdict. */
      .test-send-card {
        margin-bottom: var(--sl-spacing-large);
      }

      .test-send-header {
        margin-bottom: var(--sl-spacing-medium);
      }

      .test-send-actions {
        display: flex;
        flex-wrap: wrap;
        gap: var(--sl-spacing-small);
      }

      .test-send-hint,
      .test-send-alert {
        margin-top: var(--sl-spacing-small);
        font-size: var(--sl-font-size-small);
        color: var(--sl-color-neutral-600);
      }

      .test-results {
        margin-top: var(--sl-spacing-medium);
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-small);
      }

      .test-results-summary {
        font-size: var(--sl-font-size-small);
        font-weight: var(--sl-font-weight-semibold);
        color: var(--sl-color-neutral-600);
      }

      .test-result-item {
        border: 1px solid var(--sl-color-neutral-200);
        border-left-width: 3px;
        border-radius: var(--sl-border-radius-medium);
        padding: var(--sl-spacing-small);
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-2x-small);
      }

      .test-result-item.ok {
        border-left-color: var(--sl-color-success-600);
      }

      .test-result-item.failed {
        border-left-color: var(--sl-color-danger-600);
      }

      .test-result-head {
        display: flex;
        align-items: center;
        flex-wrap: wrap;
        gap: var(--sl-spacing-x-small);
      }

      .test-result-meta {
        font-size: var(--sl-font-size-x-small);
        color: var(--sl-color-neutral-500);
      }

      .test-result-meta code,
      .test-result-error code {
        font-family: var(--sl-font-mono);
      }

      /* The verbatim provider error is the point of this panel — make it
       readable and selectable rather than truncating it. */
      .test-result-error {
        font-family: var(--sl-font-mono);
        font-size: var(--sl-font-size-x-small);
        color: var(--sl-color-danger-700);
        word-break: break-word;
        user-select: text;
      }

      .test-result-remediation {
        font-size: var(--sl-font-size-small);
        color: var(--sl-color-neutral-600);
      }
    `,
  ];

  async connectedCallback() {
    super.connectedCallback();
    await this.loadPreferences();
    void this.fetchAdminStatus();

    // Connect to WebSocket for real-time device registration updates
    try {
      console.debug(
        '[NotificationPrefs] Setting up device_registered subscription'
      );

      this.unsubscribe = unifiedWebSocketManager.subscribe(
        'device_registered',
        (message: any) => {
          console.debug(
            '[NotificationPrefs] Received device_registered event:',
            message
          );

          // Close QR dialog if open
          if (this.showQRDialog) {
            console.debug('[NotificationPrefs] Closing QR dialog');
            this.handleCloseQRDialog();
          }

          // Reload preferences to show new device
          this.loadPreferences();

          // Show success message
          this.successMessage = `${message.platform === 'ios' ? 'iOS' : 'Android'} device registered successfully`;
          setTimeout(() => (this.successMessage = ''), 5000);
        }
      );

      // Track connection state changes
      this.unsubscribeStateChange = unifiedWebSocketManager.onStateChange(
        (state) => {
          console.debug('[NotificationPrefs] WebSocket state:', state);
        }
      );

      console.debug('[NotificationPrefs] Subscription setup complete');
    } catch (error) {
      console.error('Failed to setup WebSocket subscription:', error);
    }
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    if (this.qrExpiryInterval) {
      clearInterval(this.qrExpiryInterval);
    }

    // Disconnect from WebSocket
    this.unsubscribe?.();
    this.unsubscribeStateChange?.();
  }

  /** Test sends are admin-only for now; hide the controls for everyone else. */
  private async fetchAdminStatus() {
    try {
      const user = await getUserProfile();
      this.isAdmin = user?.is_superuser || false;
    } catch {
      this.isAdmin = false;
    }
  }

  private get hasRegisteredDevices(): boolean {
    return (this.preferences?.mobile_device_tokens?.length ?? 0) > 0;
  }

  /**
   * Send a clearly-labelled TEST notification over the real push path.
   *
   * The backend creates no approval request, so nothing is gated on the
   * result and approving or declining it on the device has no effect.
   */
  private async handleSendTest(kind: 'approval' | 'question') {
    this.isSendingTest = kind;
    this.testResult = null;
    this.testError = '';

    try {
      const data = await this.fetchData(
        '/api/v1/notification-preferences/me/test-push',
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ kind }),
        }
      );

      if (!data) {
        throw new Error('Test send failed');
      }

      this.testResult = data as TestPushResult;
    } catch (error: any) {
      this.testError = error?.message || 'Failed to send test notification';
    } finally {
      this.isSendingTest = null;
    }
  }

  private renderTestSendSection() {
    if (!this.isAdmin) {
      return '';
    }

    return html`
      <sl-card class="test-send-card">
        <div class="test-send-header">
          <div>
            <h2 class="section-title" style="margin: 0;">
              Test push delivery
              <sl-badge variant="neutral" pill>Admin</sl-badge>
            </h2>
            <div class="preference-description">
              Sends a clearly labelled test over the real delivery path and
              reports exactly what the push provider said. Nothing is gated on
              it &mdash; no approval request is created, and responding to it on
              the device has no effect.
            </div>
          </div>
        </div>

        <div class="test-send-actions">
          <sl-button
            size="small"
            variant="primary"
            ?disabled=${
              !this.hasRegisteredDevices || this.isSendingTest !== null
            }
            ?loading=${this.isSendingTest === 'approval'}
            @click=${() => this.handleSendTest('approval')}
          >
            <sl-icon slot="prefix" name="shield-check"></sl-icon>
            Send test approval
          </sl-button>
          <sl-button
            size="small"
            ?disabled=${
              !this.hasRegisteredDevices || this.isSendingTest !== null
            }
            ?loading=${this.isSendingTest === 'question'}
            @click=${() => this.handleSendTest('question')}
          >
            <sl-icon slot="prefix" name="chat-dots"></sl-icon>
            Send test question
          </sl-button>
        </div>

        ${
          !this.hasRegisteredDevices
            ? html`<div class="test-send-hint">
                Register a device below to enable test sends.
              </div>`
            : ''
        }
        ${
          this.testError
            ? html`<sl-alert variant="danger" open class="test-send-alert">
                <sl-icon slot="icon" name="exclamation-octagon"></sl-icon>
                ${this.testError}
              </sl-alert>`
            : ''
        }
        ${this.testResult ? this.renderTestResult(this.testResult) : ''}
      </sl-card>
    `;
  }

  private renderTestResult(result: TestPushResult) {
    return html`
      <div class="test-results">
        <div class="test-results-summary">
          ${result.sent} delivered &middot; ${result.failed} failed
        </div>
        ${result.results.map(
          (device) => html`
            <div class="test-result-item ${device.success ? 'ok' : 'failed'}">
              <div class="test-result-head">
                <sl-badge variant=${device.success ? 'success' : 'danger'}>
                  ${device.success ? 'Delivered' : 'Failed'}
                </sl-badge>
                <span class="test-result-meta">
                  ${device.platform === 'ios' ? 'iOS' : 'Android'} &middot;
                  ${device.transport} &middot;
                  <code>${device.token}</code>
                </span>
              </div>
              ${
                device.error
                  ? html`<div class="test-result-error">
                      ${
                        device.error_reason
                          ? html`<strong>${device.error_reason}</strong>
                              &mdash; `
                          : ''
                      }${device.error}
                    </div>`
                  : ''
              }
              ${
                device.remediation
                  ? html`<div class="test-result-remediation">
                      ${device.remediation}
                    </div>`
                  : ''
              }
              ${
                device.project_id
                  ? html`<div class="test-result-meta">
                      Server Firebase project:
                      <code>${device.project_id}</code>
                    </div>`
                  : ''
              }
            </div>
          `
        )}
      </div>
    `;
  }

  private async loadPreferences() {
    try {
      this.isLoading = true;
      const data = await this.fetchData('/api/v1/notification-preferences/me');

      if (!data) {
        throw new Error('Failed to load preferences');
      }

      this.preferences = data;
      this.errorMessage = '';
    } catch (error: any) {
      this.errorMessage =
        error.message || 'Failed to load notification preferences';
    } finally {
      this.isLoading = false;
    }
  }

  /** Persist a single preference field via PUT /notification-preferences/me. */
  private async updatePreference(
    field: string,
    value: string | boolean,
    label: string
  ) {
    if (!this.preferences) return;

    try {
      this.isSaving = true;
      const data = await this.fetchData('/api/v1/notification-preferences/me', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ [field]: value }),
      });

      if (!data) {
        throw new Error(`Failed to update ${label}`);
      }

      this.preferences = data;
      this.successMessage = 'Preference updated successfully';
      setTimeout(() => (this.successMessage = ''), 3000);
    } catch (error: any) {
      this.errorMessage = error.message || 'Failed to update preference';
    } finally {
      this.isSaving = false;
    }
  }

  private async handleChannelChange(e: any) {
    await this.updatePreference(
      'preferred_channel',
      e.target.value,
      'channel preference'
    );
  }

  private async handleToggleEmail(e: any) {
    await this.updatePreference(
      'enable_email',
      e.target.checked,
      'email preference'
    );
  }

  private async handleToggleMobilePush(e: any) {
    await this.updatePreference(
      'enable_mobile_push',
      e.target.checked,
      'mobile push preference'
    );
  }

  private async handleToggleStaggerEmail(e: any) {
    await this.updatePreference(
      'stagger_email',
      e.target.checked,
      'quiet-alerts preference'
    );
  }

  private get showStaggerToggle(): boolean {
    return Boolean(
      this.preferences?.enable_email && this.preferences?.enable_mobile_push
    );
  }

  private async handleShowQRCode() {
    try {
      const data = await this.fetchData(
        '/api/v1/notification-preferences/me/qr-code'
      );

      if (!data) {
        throw new Error('Failed to generate QR code');
      }

      this.qrCodeData = data;
      this.qrExpiry = this.qrCodeData!.expires_in_seconds;
      this.showQRDialog = true;

      // Start countdown
      this.qrExpiryInterval = setInterval(() => {
        this.qrExpiry--;
        if (this.qrExpiry <= 0) {
          clearInterval(this.qrExpiryInterval);
          this.handleCloseQRDialog();
        }
      }, 1000);
    } catch (error: any) {
      this.errorMessage = error.message || 'Failed to generate QR code';
    }
  }

  private handleCloseQRDialog() {
    this.showQRDialog = false;
    this.qrCodeData = null;
    if (this.qrExpiryInterval) {
      clearInterval(this.qrExpiryInterval);
      this.qrExpiryInterval = null;
    }
  }

  private async handleUnregisterDevice(token: string) {
    if (!confirm('Are you sure you want to unregister this device?')) {
      return;
    }

    try {
      const data = await this.fetchData(
        `/api/v1/notification-preferences/me/device/${encodeURIComponent(token)}`,
        {
          method: 'DELETE',
        }
      );

      if (!data) {
        throw new Error('Failed to unregister device');
      }

      this.preferences = data;
      this.successMessage = 'Device unregistered successfully';
      setTimeout(() => (this.successMessage = ''), 3000);
    } catch (error: any) {
      this.errorMessage = error.message || 'Failed to unregister device';
    }
  }

  private formatDate(dateString: string): string {
    const date = new Date(dateString);
    return date.toLocaleDateString() + ' ' + date.toLocaleTimeString();
  }

  private formatExpiry(seconds: number): string {
    const minutes = Math.floor(seconds / 60);
    const secs = seconds % 60;
    return `${minutes}:${secs.toString().padStart(2, '0')}`;
  }

  render() {
    if (this.isLoading) {
      return html`
        <div class="loading">
          <sl-spinner style="font-size: 3rem;"></sl-spinner>
        </div>
      `;
    }

    return html`
      <div class="header">
        <h1>Notification Preferences</h1>
        <p>Manage how you receive approval request notifications</p>
      </div>

      ${
        this.successMessage
          ? html`
              <sl-alert variant="success" open closable>
                <sl-icon slot="icon" name="check-circle"></sl-icon>
                ${this.successMessage}
              </sl-alert>
            `
          : ''
      }
      ${
        this.errorMessage
          ? html`
              <sl-alert variant="danger" open closable>
                <sl-icon slot="icon" name="exclamation-triangle"></sl-icon>
                ${this.errorMessage}
              </sl-alert>
            `
          : ''
      }

      <div class="content">
        <sl-card>
          <h2 class="section-title">Notification Channels</h2>

          <div class="preference-row">
            <div class="preference-label">
              <div class="preference-title">Email Notifications</div>
              <div class="preference-description">
                Receive approval requests via email
              </div>
            </div>
            <sl-switch
              ?checked=${this.preferences?.enable_email}
              @sl-change=${this.handleToggleEmail}
              ?disabled=${this.isSaving}
            ></sl-switch>
          </div>

          <div
            class="preference-row"
            style="margin-top: var(--sl-spacing-small);"
          >
            <div class="preference-label">
              <div class="preference-title">Mobile Push Notifications</div>
              <div class="preference-description">
                Receive approval requests on your mobile device
              </div>
            </div>
            <sl-switch
              ?checked=${this.preferences?.enable_mobile_push}
              @sl-change=${this.handleToggleMobilePush}
              ?disabled=${this.isSaving}
            ></sl-switch>
          </div>

          ${
            this.showStaggerToggle
              ? html`
                  <div
                    class="preference-row"
                    style="margin-top: var(--sl-spacing-small);"
                    data-testid="stagger-email-toggle"
                  >
                    <div class="preference-label">
                      <div class="preference-title">Quiet duplicate alerts</div>
                      <div class="preference-description">
                        When push is enabled, email me only if an approval is
                        still waiting after a minute.
                      </div>
                    </div>
                    <sl-switch
                      ?checked=${this.preferences?.stagger_email !== false}
                      @sl-change=${this.handleToggleStaggerEmail}
                      ?disabled=${this.isSaving}
                    ></sl-switch>
                  </div>
                `
              : ''
          }
        </sl-card>

        <sl-card>
          <div
            style="display: flex; justify-content: space-between; align-items: center; margin-bottom: var(--sl-spacing-medium);"
          >
            <h2 class="section-title" style="margin: 0;">Mobile Devices</h2>
            <sl-button size="small" @click=${this.handleShowQRCode}>
              <sl-icon slot="prefix" name="qr-code"></sl-icon>
              Register New Device
            </sl-button>
          </div>

          ${
            this.preferences?.mobile_device_tokens &&
            this.preferences.mobile_device_tokens.length > 0
              ? html`
                  <div class="devices-list">
                    ${this.preferences.mobile_device_tokens.map(
                      (device) => html`
                        <div class="device-item">
                          <div class="device-info">
                            <div class="device-platform">
                              <sl-icon
                                name=${
                                  device.platform === 'ios'
                                    ? 'phone'
                                    : 'phone-fill'
                                }
                                style="font-size: 1.5rem;"
                              ></sl-icon>
                              <sl-badge
                                variant=${
                                  device.platform === 'ios'
                                    ? 'primary'
                                    : 'success'
                                }
                              >
                                ${device.platform === 'ios' ? 'iOS' : 'Android'}
                              </sl-badge>
                            </div>
                            <div class="device-details">
                              <div class="device-token">
                                ${device.token.substring(0, 20)}...
                              </div>
                              <div class="device-date">
                                Registered:
                                ${this.formatDate(device.registered_at)}
                              </div>
                            </div>
                          </div>
                          <sl-button
                            size="small"
                            variant="danger"
                            @click=${() =>
                              this.handleUnregisterDevice(device.token)}
                          >
                            <sl-icon slot="prefix" name="trash"></sl-icon>
                            Unregister
                          </sl-button>
                        </div>
                      `
                    )}
                  </div>
                `
              : html`
                  <div class="empty-state">
                    <sl-icon name="phone"></sl-icon>
                    <p>No mobile devices registered</p>
                    <p
                      style="font-size: var(--sl-font-size-small); margin-top: var(--sl-spacing-small);"
                    >
                      Scan a QR code with your mobile app to register your
                      device
                    </p>
                  </div>
                `
          }
        </sl-card>

        ${this.renderTestSendSection()}

        <div class="app-store-links">
          <a
            href="https://apps.apple.com/app/preloop/id6757803021"
            target="_blank"
            rel="noopener noreferrer"
            class="app-store-button"
            title="Download on the App Store"
          >
            <sl-icon name="apple"></sl-icon>
            <span>App Store</span>
          </a>
          <a
            href="https://play.google.com/store/apps/details?id=ai.spacecode.preloop&pli=1"
            target="_blank"
            rel="noopener noreferrer"
            class="app-store-button"
            title="Get it on Google Play"
          >
            <sl-icon name="google-play"></sl-icon>
            <span>Google Play</span>
          </a>
        </div>
      </div>

      <sl-dialog
        label="Register Mobile Device"
        ?open=${this.showQRDialog}
        @sl-request-close=${this.handleCloseQRDialog}
        style="--width: 600px;"
      >
        ${
          this.qrCodeData
            ? html`
                <div class="qr-container">
                  <div class="qr-instructions">
                    <h4>Scan with your mobile app</h4>
                    <p>
                      Open the Preloop mobile app and scan this QR code to
                      register your device for push notifications.
                    </p>
                  </div>

                  <div class="qr-code-wrapper">
                    <sl-qr-code
                      value=${this.qrCodeData.qr_data}
                      size="300"
                    ></sl-qr-code>
                  </div>

                  <div class="qr-expiry">
                    <sl-icon name="clock"></sl-icon>
                    Expires in: ${this.formatExpiry(this.qrExpiry)}
                  </div>
                </div>
              `
            : html`<sl-spinner></sl-spinner>`
        }

        <sl-button slot="footer" @click=${this.handleCloseQRDialog}>
          Close
        </sl-button>
      </sl-dialog>
    `;
  }
}
