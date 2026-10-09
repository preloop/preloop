import { billingStatusLabel } from '../../../utils/billing-status';
import { LitElement, html, css, unsafeCSS } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import {
  fetchWithAuth,
  getAccountOrganization,
  updateAccountOrganization,
  AccountOrganization,
  getFeatures,
  FeaturesResponse,
  BILLING_SUBSCRIPTION_CHANGED,
} from '../../../api';
import '@shoelace-style/shoelace/dist/components/details/details.js';
import consoleStyles from '../../../styles/console-styles.css?inline';
import pricingStyles from '../../../styles/pricing-styles.css?inline';
import type {
  SessionArtifactSettings,
  SessionArtifactUsage,
} from '../../../types';
import { PLAN_PAGE_PATH } from '../../../utils/premium-features';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/switch/switch.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/card/card.js';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import '../../../components/view-header';
import { hasCapability } from '../../../capabilities';
import '../hierarchy/subaccounts-view';

import type {
  BillingSummary,
  Subscription,
} from '../../../types/billing-summary';

@customElement('account-view')
export class AccountView extends LitElement {
  @state() private accountOrganization: AccountOrganization | null = null;
  @state() private features: FeaturesResponse | null = null;
  @state() private organizationName: string = '';
  @state() private isSavingOrg = false;
  @state() private orgSuccessMessage = '';
  @state() private orgErrorMessage = '';
  @state() private subscription: Subscription | null = null;
  @state() private _billingSummary: BillingSummary | null = null;
  @state() private _loading = true;
  @state() private _error: string | null = null;
  @state() private _sessionArtifactUsage: SessionArtifactUsage | null = null;
  @state() private _artifactSettings: SessionArtifactSettings | null = null;
  @state() private _audioRetentionDraft = '';
  @state() private _artifactSettingsSaving = false;
  @state() private _artifactSettingsError = '';

  private _handleSubscriptionChanged = (event: Event) => {
    if (event.target !== window) return;
    void this._refreshBillingSummary();
  };

  async connectedCallback() {
    super.connectedCallback();
    window.addEventListener(
      BILLING_SUBSCRIPTION_CHANGED,
      this._handleSubscriptionChanged
    );
    await this._fetchData();
  }

  disconnectedCallback() {
    window.removeEventListener(
      BILLING_SUBSCRIPTION_CHANGED,
      this._handleSubscriptionChanged
    );
    super.disconnectedCallback();
  }

  private async _fetchData() {
    this._loading = true;
    try {
      // Fetch account details and features
      const [accountOrganization, features] = await Promise.all([
        getAccountOrganization(),
        getFeatures(),
      ]);

      this.accountOrganization = accountOrganization;
      this.features = features;
      this.organizationName = accountOrganization.organization_name || '';

      try {
        const usageRes = await fetchWithAuth(
          '/api/v1/account/session-artifacts/usage'
        );
        if (usageRes.ok) {
          const body = await usageRes.json();
          const byKind = body?.by_kind;
          if (
            typeof body?.used_bytes === 'number' &&
            typeof body?.budget_bytes === 'number' &&
            typeof byKind?.screenshot === 'number' &&
            typeof byKind?.recording === 'number'
          ) {
            this._sessionArtifactUsage = body;
            this._scrollToArtifactStorageWhenLinked();
          }
        }
      } catch {
        this._sessionArtifactUsage = null;
      }
      await this._loadArtifactSettings();

      // Only fetch billing data for proprietary version
      const isProprietary = features.features['billing'] === true;

      if (isProprietary) {
        const summaryRes = await fetchWithAuth('/api/v1/billing/summary');
        if (!summaryRes.ok) throw new Error('Failed to load billing summary.');
        this._billingSummary = await summaryRes.json();
        this.subscription = this._billingSummary?.subscription ?? null;
      }
    } catch (error) {
      this._error = (error as Error).message;
      console.error(error);
    } finally {
      this._loading = false;
    }
  }

  private async _handleSaveOrganization() {
    this.isSavingOrg = true;
    this.orgSuccessMessage = '';
    this.orgErrorMessage = '';

    try {
      const updated = await updateAccountOrganization({
        organization_name: this.organizationName || null,
      });

      this.accountOrganization = updated;
      this.orgSuccessMessage = 'Account name saved successfully';
      setTimeout(() => (this.orgSuccessMessage = ''), 3000);
    } catch (error) {
      this.orgErrorMessage = (error as Error).message;
    } finally {
      this.isSavingOrg = false;
    }
  }

  /** Format a byte count with binary units. */
  private _formatBytes(value: number): string {
    const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
    let size = value;
    let unit = 0;
    while (size >= 1024 && unit < units.length - 1) {
      size /= 1024;
      unit += 1;
    }
    const text = Number.isInteger(size) ? String(size) : size.toFixed(1);
    return `${text} ${units[unit]}`;
  }

  /**
   * Timeline placeholders for evicted or expired screenshots link here with
   * `#session-artifact-storage`; bring the card into view once it renders.
   */
  private _scrollToArtifactStorageWhenLinked(): void {
    if (window.location.hash !== '#session-artifact-storage') return;
    void this.updateComplete.then(() => {
      this.renderRoot
        .querySelector('#session-artifact-storage')
        ?.scrollIntoView({ block: 'start' });
    });
  }

  /**
   * Kinds other than screenshot and recording that hold bytes. Shown with the
   * raw kind name until display labels land (#1083); unknown future kinds
   * render the same way instead of breaking the card.
   */
  private _otherArtifactKinds(usage: SessionArtifactUsage): [string, number][] {
    return Object.entries(usage.by_kind).filter(
      ([kind, bytes]) =>
        kind !== 'screenshot' &&
        kind !== 'recording' &&
        typeof bytes === 'number' &&
        bytes > 0
    );
  }

  /** ``generated_file`` becomes ``Generated file``. */
  private _kindLabel(kind: string): string {
    const words = kind.replace(/_/g, ' ');
    return words.charAt(0).toUpperCase() + words.slice(1);
  }

  private async _loadArtifactSettings(): Promise<void> {
    try {
      const res = await fetchWithAuth(
        '/api/v1/account/session-artifacts/settings'
      );
      if (!res.ok) return;
      const body = await res.json();
      if (typeof body?.audio_storage_enabled === 'boolean') {
        this._artifactSettings = body;
        this._audioRetentionDraft = String(body.audio_retention_days);
      }
    } catch {
      this._artifactSettings = null;
    }
  }

  /** Saves the change; resolves true when the server accepted it. */
  private async _saveArtifactSettings(
    change: Partial<
      Pick<
        SessionArtifactSettings,
        'audio_storage_enabled' | 'audio_retention_days'
      >
    >
  ): Promise<boolean> {
    this._artifactSettingsSaving = true;
    this._artifactSettingsError = '';
    try {
      const res = await fetchWithAuth(
        '/api/v1/account/session-artifacts/settings',
        {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(change),
        }
      );
      if (res.ok) {
        const body = await res.json();
        this._artifactSettings = body;
        // A toggle must not clobber a retention the user is still typing.
        if (change.audio_retention_days !== undefined) {
          this._audioRetentionDraft = String(body.audio_retention_days);
        }
        return true;
      } else if (res.status === 403) {
        this._artifactSettingsError =
          'Only an account admin can change audio storage.';
      } else if (res.status === 422) {
        this._artifactSettingsError = `Retention must be between 1 and ${
          this._artifactSettings?.audio_retention_max_days ??
          'the session retention'
        } days.`;
      } else {
        this._artifactSettingsError = 'Could not save audio storage settings.';
      }
    } catch {
      this._artifactSettingsError = 'Could not save audio storage settings.';
    } finally {
      this._artifactSettingsSaving = false;
    }
    return false;
  }

  private async _onAudioToggle(event: Event): Promise<void> {
    const toggle = event.target as HTMLInputElement;
    const saved = await this._saveArtifactSettings({
      audio_storage_enabled: toggle.checked,
    });
    if (!saved) {
      // Lit skips an unchanged ?checked binding, so put the control back to
      // what the server still has; it must never claim a state not saved.
      toggle.checked = this._artifactSettings?.audio_storage_enabled ?? false;
    }
  }

  private _onAudioRetentionSave(): void {
    const days = Number(this._audioRetentionDraft);
    if (!Number.isInteger(days)) {
      this._artifactSettingsError = 'Retention must be a whole number of days.';
      return;
    }
    void this._saveArtifactSettings({ audio_retention_days: days });
  }

  private _renderAudioStorageSettings() {
    const audio = this._artifactSettings;
    if (!audio) return '';
    return html`
      <div class="audio-storage" data-testid="audio-storage-settings">
        <sl-switch
          data-testid="audio-storage-toggle"
          ?checked=${audio.audio_storage_enabled}
          ?disabled=${this._artifactSettingsSaving}
          @sl-change=${this._onAudioToggle}
          >Store raw audio</sl-switch
        >
        <div class="usage-note" data-testid="audio-storage-copy">
          Store raw audio deposited by agents. Off by default. Transcripts are
          stored either way.
        </div>
        <div class="audio-retention">
          <sl-input
            data-testid="audio-retention-days"
            type="number"
            min="1"
            max=${audio.audio_retention_max_days}
            label="Keep raw audio for (days)"
            value=${this._audioRetentionDraft}
            ?disabled=${this._artifactSettingsSaving}
            @sl-input=${(e: Event) =>
              (this._audioRetentionDraft = (
                e.target as HTMLInputElement
              ).value)}
          >
            <span slot="help-text">
              Older audio is expired: its bytes are dropped and the artifact row
              stays. At most ${audio.audio_retention_max_days} days (the session
              retention).
            </span>
          </sl-input>
          <sl-button
            size="small"
            data-testid="audio-retention-save"
            ?disabled=${
              this._artifactSettingsSaving ||
              this._audioRetentionDraft === String(audio.audio_retention_days)
            }
            @click=${this._onAudioRetentionSave}
            >Save retention</sl-button
          >
        </div>
        ${
          this._artifactSettingsError
            ? html`<sl-alert
                variant="danger"
                open
                data-testid="audio-storage-error"
                >${this._artifactSettingsError}</sl-alert
              >`
            : ''
        }
      </div>
    `;
  }

  /** Artifacts page filtered to one kind (#1087). */
  private _artifactsHref(kind?: string): string {
    return kind
      ? `/console/artifacts?kind=${encodeURIComponent(kind)}`
      : '/console/artifacts';
  }

  /** A usage cell whose label opens the Artifacts page for that kind. */
  private _kindUsageMetric(kind: string, label: string, bytes: number) {
    return html`
      <div class="usage-metric" data-kind=${kind}>
        <div class="usage-label">
          <a
            href=${this._artifactsHref(kind)}
            data-testid="artifact-kind-link"
            title="Browse ${label.toLowerCase()}"
            >${label}</a
          >
        </div>
        <div class="usage-value">${this._formatBytes(bytes)}</div>
      </div>
    `;
  }

  private _renderSessionArtifactUsage() {
    const usage = this._sessionArtifactUsage;
    if (!usage) return '';
    return html`
      <div
        class="card"
        id="session-artifact-storage"
        data-testid="session-artifact-usage"
      >
        <div class="current-row">
          <span class="plan-name">Session artifact storage</span>
          <a href=${this._artifactsHref()} data-testid="browse-artifacts-link"
            >Browse artifacts</a
          >
        </div>
        <div class="usage-grid">
          <div class="usage-metric">
            <div class="usage-label">Used</div>
            <div class="usage-value">
              ${this._formatBytes(usage.used_bytes)} /
              ${this._formatBytes(usage.budget_bytes)}
            </div>
          </div>
          ${this._kindUsageMetric(
            'screenshot',
            'Screenshots',
            usage.by_kind.screenshot
          )}
          ${this._kindUsageMetric(
            'recording',
            'Recordings',
            usage.by_kind.recording
          )}
          ${this._otherArtifactKinds(usage).map(([kind, bytes]) =>
            this._kindUsageMetric(kind, this._kindLabel(kind), bytes)
          )}
        </div>
        ${this._renderAudioStorageSettings()}
      </div>
    `;
  }

  private async _refreshBillingSummary(): Promise<void> {
    try {
      const response = await fetchWithAuth('/api/v1/billing/summary', {
        cache: 'no-store',
      });
      if (response.ok) {
        this._billingSummary = await response.json();
        this.subscription = this._billingSummary?.subscription ?? null;
      }
    } catch {
      // The comparison keeps its explicit result and offers a status refresh.
    }
  }

  static styles = [
    unsafeCSS(pricingStyles),
    unsafeCSS(consoleStyles),
    css`
      .status-chip {
        display: inline-flex;
        align-items: center;
        gap: 0.5rem;
        padding: 0.25rem 0.5rem;
        border-radius: 999px;
        background: var(--sl-color-neutral-200);
        color: var(--sl-color-neutral-800);
        font-weight: 600;
        font-size: 0.85rem;
      }
      .status-chip.pending {
        background: var(--sl-color-warning-200);
        color: var(--sl-color-warning-800);
      }

      .card {
        border: 1px solid var(--sl-color-neutral-300);
        border-radius: 16px;
        padding: 1rem 1.25rem;
      }

      .plan-name {
        font-weight: 700;
      }

      .actions {
        display: flex;
        gap: 0.5rem;
        flex-wrap: wrap;
        margin-top: 0.5rem;
      }

      .billing-toggle {
        margin-bottom: 1rem;
      }

      .features {
        list-style: none;
        padding: 0;
        margin: 0.5rem 0 1rem 0;
        display: flex;
        flex-direction: column;
        gap: 0.5rem;
      }
      .feature {
        display: flex;
        gap: 0.5rem;
        align-items: baseline;
        color: var(--sl-color-neutral-800);
      }
      .feature.excluded {
        color: var(--sl-color-neutral-500);
      }
      .feat-icon {
        color: var(--sl-color-success-600);
      }
      .feature.excluded .feat-icon {
        color: var(--sl-color-neutral-400);
      }
      .feat-text {
        flex: 1;
      }
      .feat-value {
        color: var(--sl-color-neutral-700);
      }
      .more {
        color: var(--sl-color-neutral-600);
        font-size: 0.95rem;
      }

      .cta {
        margin-top: auto;
        width: 100%;
      }

      .loading,
      .error {
        text-align: center;
        margin: 1rem 0;
        color: var(--sl-color-danger-600);
      }

      /* One hairline row, not five boxes inside a card: DESIGN.md depth
         limit two. The rule between the numbers separates them; a border and
         a fill around each one adds a third layer for no information. */
      .usage-grid {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
        gap: 0.75rem;
        margin-top: 1rem;
        padding-bottom: 0.875rem;
        border-bottom: 1px solid var(--console-hairline);
        /* Clips the rule of whichever metric starts a row: see below. */
        overflow: hidden;
      }

      /* The separator is drawn in the gap to the metric's left rather than on
         its own border, because a border follows DOM order and this grid
         wraps: once it does, the first metric of the second row would carry a
         rule with nothing beside it. Sitting in the gap, that rule falls
         outside the grid's box and is clipped away. */
      .usage-metric {
        position: relative;
      }

      .usage-metric::before {
        content: '';
        position: absolute;
        top: 0;
        bottom: 0;
        left: -0.375rem;
        border-left: 1px solid var(--console-hairline);
      }

      .usage-label {
        color: var(--sl-color-neutral-600);
        font-size: 0.85rem;
        margin-bottom: 0.35rem;
      }

      .usage-value {
        color: var(--sl-color-neutral-900);
        font-size: 1rem;
        font-weight: 700;
      }

      .usage-note {
        margin-top: 1rem;
        color: var(--sl-color-neutral-700);
      }

      .audio-storage {
        margin-top: 1.25rem;
        display: flex;
        flex-direction: column;
        gap: 0.75rem;
      }

      .audio-storage sl-switch {
        /* Only the control and its label are clickable; no dead row. */
        align-self: flex-start;
      }

      .audio-storage .usage-note {
        margin-top: 0;
      }

      .audio-retention {
        display: flex;
        align-items: flex-start;
        gap: 0.75rem;
        max-width: 32rem;
      }

      .audio-retention sl-button {
        /* Line up with the input, below its label. */
        margin-top: calc(var(--sl-input-label-font-size-medium) * 1.8);
      }

      .usage-models {
        margin-top: 1rem;
        display: flex;
        flex-direction: column;
        gap: 0.75rem;
      }

      .usage-model-row {
        display: flex;
        justify-content: space-between;
        gap: 1rem;
        align-items: flex-start;
        padding-top: 0.75rem;
        border-top: 1px solid var(--sl-color-neutral-200);
      }

      .usage-model-row:first-child {
        border-top: none;
        padding-top: 0;
      }

      .usage-model-name {
        font-weight: 600;
        color: var(--sl-color-neutral-900);
      }

      .usage-model-meta {
        color: var(--sl-color-neutral-600);
        font-size: 0.9rem;
      }

      .usage-model-cost {
        font-weight: 700;
        color: var(--sl-color-neutral-900);
        white-space: nowrap;
      }

      .seat-warning {
        color: var(--sl-color-warning-700);
        font-weight: 600;
      }

      .quota-figures {
        color: var(--sl-color-neutral-700);
        font-size: 0.9rem;
        white-space: nowrap;
      }

      .quota-bar {
        height: 8px;
        border-radius: 999px;
        background: var(--sl-color-neutral-200);
        overflow: hidden;
        margin: 0.75rem 0 0.5rem 0;
      }

      .quota-fill {
        height: 100%;
        background: var(--sl-color-primary-600);
        transition: width 0.2s ease-in-out;
      }

      .quota-fill.warn {
        background: var(--sl-color-warning-600);
      }

      /* Over quota is amber, never red: nothing has broken and nothing has
         stopped, so the meter must not read as an outage. */
      .quota-fill.over {
        background: var(--sl-color-warning-700);
      }
    `,
  ];

  render() {
    if (this._loading) {
      return html`
        <view-header headerText="Account" width="narrow"></view-header>
        <div class="column-layout narrow">
          <div class="main-column">
            <div class="loading">
              <sl-spinner style="font-size: 3rem;"></sl-spinner>
            </div>
          </div>
        </div>
      `;
    }

    if (this._error) {
      return html`
        <view-header headerText="Account" width="narrow"></view-header>
        <div class="column-layout narrow">
          <div class="main-column">
            <sl-alert variant="danger" open>
              <sl-icon slot="icon" name="exclamation-octagon"></sl-icon>
              ${this._error}
            </sl-alert>
          </div>
        </div>
      `;
    }

    const isProprietary = this.features?.features.billing === true;
    const periodEnd = this.subscription?.current_period_end;
    const trialExpired =
      this._billingSummary?.trial?.is_expired === true ||
      (this.subscription?.status === 'trialing' &&
        !!periodEnd &&
        new Date(periodEnd).getTime() < Date.now());
    const displayPlanName =
      this._billingSummary?.effective_plan?.name ??
      (trialExpired ? 'Free' : (this._billingSummary?.plan?.name ?? 'Free'));

    return html`
      <view-header headerText="Account" width="narrow"></view-header>
      <div class="column-layout narrow">
        <div class="main-column">
          <!-- Account details section -->
          <sl-card style="margin-bottom: 2rem;">
            <h2 slot="header" style="margin: 0; font-size: 1.25rem;">
              Account
            </h2>

            ${
              this.orgSuccessMessage
                ? html`
                    <sl-alert variant="success" open closable>
                      <sl-icon slot="icon" name="check-circle"></sl-icon>
                      ${this.orgSuccessMessage}
                    </sl-alert>
                  `
                : ''
            }
            ${
              this.orgErrorMessage
                ? html`
                    <sl-alert variant="danger" open closable>
                      <sl-icon
                        slot="icon"
                        name="exclamation-triangle"
                      ></sl-icon>
                      ${this.orgErrorMessage}
                    </sl-alert>
                  `
                : ''
            }

            <div style="display: flex; flex-direction: column; gap: 1rem;">
              <sl-input
                label="Account name"
                placeholder="Enter your account name"
                value=${this.organizationName}
                @sl-input=${(e: any) =>
                  (this.organizationName = e.target.value)}
                ?disabled=${this.isSavingOrg}
              >
                <span slot="help-text">
                  This name will be displayed across the application
                </span>
              </sl-input>

              <div>
                <sl-button
                  variant="primary"
                  @click=${this._handleSaveOrganization}
                  ?loading=${this.isSavingOrg}
                >
                  Save account name
                </sl-button>
              </div>
            </div>
          </sl-card>

          ${
            // Subaccounts are created from the account they sit under, so
            // the list lives here rather than behind its own nav entry. The
            // card hides itself on a subaccount.
            hasCapability(this.features?.features, 'account_hierarchy')
              ? html`<subaccounts-view embedded></subaccounts-view>`
              : ''
          }
          ${this._renderSessionArtifactUsage()}
          ${
            isProprietary
              ? html`<p class="current-plan" data-testid="billing-summary">
                  You are on ${displayPlanName} ·
                  ${trialExpired ? 'Free' : billingStatusLabel(this.subscription?.status)}.
                  <a href=${PLAN_PAGE_PATH}>Subscription, usage and seats</a>
                </p>`
              : ''
          }
        </div>
      </div>
    `;
  }
}
