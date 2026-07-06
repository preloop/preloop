import { LitElement, html, css, unsafeCSS, render } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import {
  fetchWithAuth,
  getAccountOrganization,
  updateAccountOrganization,
  AccountOrganization,
  getFeatures,
  FeaturesResponse,
} from '../../../api';
import consoleStyles from '../../../styles/console-styles.css?inline';
import pricingStyles from '../../../styles/pricing-styles.css?inline';
import '../../../components/billing-toggle';
import '../../../components/pricing-card';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/card/card.js';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';

interface Plan {
  id: string;
  name: string;
  price_monthly: number | null;
  price_annually: number | null;
  features: { [key: string]: any };
}

interface Subscription {
  plan_id: string;
  status: string;
  current_period_end: string;
}

interface HostedModelUsageRow {
  ai_model_id: string | null;
  model_name: string;
  model_alias: string | null;
  tier: string | null;
  provider_name: string | null;
  request_count: number;
  total_tokens: number;
  estimated_cost: number;
}

interface BillingSummary {
  subscription: Subscription | null;
  plan: Plan | null;
  trial: {
    is_trialing: boolean;
    days: number;
    requires_payment_method: boolean;
    hosted_model_hard_cap_usd: number | null;
  };
  hosted_models: {
    billing_period_start: string;
    billing_period_end: string;
    included_limit_usd: number | null;
    active_limit_usd: number | null;
    current_usage_usd: number;
    remaining_limit_usd: number | null;
    extra_credit_price_per_usd: number;
    models: HostedModelUsageRow[];
  };
}

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
  @state() private _publicPlans: Plan[] = [];
  @state() private _customPlans: Plan[] = [];
  @state() private _loading = true;
  @state() private _error: string | null = null;
  @state() private _interval: 'month' | 'year' = 'month';

  private _featureOrder = [
    'api_calls_monthly',
    'ai_calls_monthly',
    'issues_ingested_monthly',
    'custom_ai_models_enabled',
    'custom_compliance_metrics_enabled',
  ];

  // Human-readable labels for common feature keys
  private _featureLabels: Record<string, string> = {
    api_calls_monthly: 'API calls / month',
    ai_calls_monthly: 'AI calls / month',
    issues_ingested_monthly: 'Issues ingested / month',
    custom_ai_models_enabled: 'Custom AI models',
    custom_compliance_metrics_enabled: 'Custom compliance metrics',
  };

  async connectedCallback() {
    super.connectedCallback();
    await this._fetchData();
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

      // Only fetch billing data for proprietary version
      const isProprietary = features.features['billing'] === true;

      if (isProprietary) {
        await fetchWithAuth('/api/v1/billing/sync-subscription', {
          method: 'POST',
        });

        const [summaryRes, publicPlansRes, customPlansRes] = await Promise.all([
          fetchWithAuth('/api/v1/billing/summary'),
          fetchWithAuth('/api/v1/billing/plans'),
          fetchWithAuth('/api/v1/billing/custom-plans'),
        ]);

        if (summaryRes.ok) {
          this._billingSummary = await summaryRes.json();
          this.subscription = this._billingSummary?.subscription ?? null;
        } else {
          throw new Error('Failed to load billing summary.');
        }

        if (publicPlansRes.ok) {
          const allPlans = await publicPlansRes.json();
          this._publicPlans = allPlans.filter(
            (p: Plan) => p.price_monthly !== null && p.price_monthly > 0
          );
        } else {
          throw new Error('Failed to load public plans.');
        }

        if (customPlansRes.ok) {
          this._customPlans = await customPlansRes.json();
        } else {
          throw new Error('Failed to load custom plans.');
        }
      }
    } catch (error) {
      this._error = (error as Error).message;
      console.error(error);
    } finally {
      this._loading = false;
    }
  }

  private _formatUsd(value: number | null | undefined) {
    if (value === null || value === undefined) {
      return 'Not configured';
    }
    return new Intl.NumberFormat(undefined, {
      style: 'currency',
      currency: 'USD',
      minimumFractionDigits: value < 10 ? 2 : 0,
      maximumFractionDigits: 2,
    }).format(value);
  }

  private _formatExtraCreditPrice(value: number | null | undefined) {
    if (value === null || value === undefined) {
      return 'Not configured';
    }
    return `${this._formatUsd(value)} per additional $1.00 of built-in model usage`;
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
      this.orgSuccessMessage = 'Organization name saved successfully';
      setTimeout(() => (this.orgSuccessMessage = ''), 3000);
    } catch (error) {
      this.orgErrorMessage = (error as Error).message;
    } finally {
      this.isSavingOrg = false;
    }
  }

  private async _handleManageSubscription() {
    this._error = null;
    try {
      const response = await fetchWithAuth(
        '/api/v1/billing/create-portal-session',
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ return_url: window.location.href }),
        }
      );

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({
          detail:
            'Failed to create portal session. Please check configuration and try again.',
        }));
        throw new Error(errorData.detail);
      }

      const { url } = await response.json();
      if (url) {
        window.location.href = url;
      } else {
        throw new Error('Could not retrieve the subscription management URL.');
      }
    } catch (error) {
      this._error = (error as Error).message;
      console.error('Failed to create portal session:', error);
    }
  }

  private _handleUpgradeRequest(e: CustomEvent) {
    this._handleUpgrade(e.detail.planId);
  }

  private async _handleUpgrade(planId: string) {
    this._error = null;
    try {
      const response = await fetchWithAuth(
        '/api/v1/billing/create-checkout-session',
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            plan_id: planId,
            interval: this._interval,
          }),
        }
      );

      if (!response.ok) {
        const errorData = await response
          .json()
          .catch(() => ({ detail: 'Failed to process subscription change.' }));
        throw new Error(errorData.detail);
      }

      const result = await response.json();

      if (result.action === 'redirect') {
        window.location.href = result.url;
      } else if (result.action === 'refresh') {
        await this._fetchData();
      }
    } catch (error) {
      this._error = (error as Error).message;
      console.error('Failed to change subscription:', error);
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

      .usage-grid {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
        gap: 0.75rem;
        margin-top: 1rem;
      }

      .usage-metric {
        padding: 0.875rem;
        border-radius: 12px;
        border: 1px solid var(--sl-color-neutral-200);
        background: var(--sl-color-neutral-0);
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

    const isProprietary = this.features?.features['billing'] === true;
    const availablePlans = [...this._customPlans, ...this._publicPlans];
    const currentPlanName = this._billingSummary?.plan?.name
      ? this._billingSummary.plan.name
      : this.subscription?.plan_id
        ? (availablePlans.find((p) => p.id === this.subscription?.plan_id)
            ?.name ?? 'Free')
        : 'Free';
    const hostedSummary = this._billingSummary?.hosted_models;
    const trialSummary = this._billingSummary?.trial;

    return html`
      <view-header headerText="Account" width="narrow"></view-header>
      <div class="column-layout narrow">
        <div class="main-column">
          <!-- Organization Details Section -->
          <sl-card style="margin-bottom: 2rem;">
            <h2 slot="header" style="margin: 0; font-size: 1.25rem;">
              Organization Details
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
                label="Organization Name"
                placeholder="Enter your organization name"
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
                  Save Organization Name
                </sl-button>
              </div>
            </div>
          </sl-card>

          ${
            isProprietary
              ? html`
                  <!-- Subscription Section (Proprietary Only) -->
                  <div class="card current-plan">
                    <div class="current-row">
                      <span class="plan-name">${currentPlanName}</span>
                      <span
                        class="status-chip ${
                          this.subscription?.status === 'pending_cancellation'
                            ? 'pending'
                            : ''
                        }"
                      >
                        ${
                          this.subscription
                            ? this.subscription.status ===
                              'pending_cancellation'
                              ? 'Pending cancellation'
                              : this.subscription.status
                            : 'Free'
                        }
                      </span>
                    </div>
                    ${
                      this.subscription
                        ? html`
                            <div class="date">
                              ${
                                this.subscription.status ===
                                'pending_cancellation'
                                  ? 'Cancels on'
                                  : 'Renews on'
                              }
                              ${new Date(
                                this.subscription.current_period_end
                              ).toLocaleDateString()}
                            </div>
                          `
                        : html`<div class="date">
                            You are currently on the Free plan.
                          </div>`
                    }
                    ${
                      trialSummary?.is_trialing
                        ? html`
                            <div class="date">
                              Trial cap for built-in models:
                              ${this._formatUsd(
                                trialSummary.hosted_model_hard_cap_usd
                              )}
                            </div>
                          `
                        : ''
                    }
                    <div class="actions">
                      <sl-button
                        size="medium"
                        variant="primary"
                        ?disabled=${!this.subscription}
                        @click=${this._handleManageSubscription}
                      >
                        Manage in Stripe
                      </sl-button>
                    </div>
                  </div>

                  ${
                    hostedSummary
                      ? html`
                          <div class="card">
                            <div class="current-row">
                              <span class="plan-name"
                                >Built-in model usage</span
                              >
                            </div>
                            <div class="date">
                              Current billing period ends
                              ${new Date(
                                hostedSummary.billing_period_end
                              ).toLocaleDateString()}
                            </div>
                            <div class="usage-grid">
                              <div class="usage-metric">
                                <div class="usage-label">Plan limit</div>
                                <div class="usage-value">
                                  ${this._formatUsd(
                                    hostedSummary.included_limit_usd
                                  )}
                                </div>
                              </div>
                              <div class="usage-metric">
                                <div class="usage-label">
                                  Current active cap
                                </div>
                                <div class="usage-value">
                                  ${this._formatUsd(hostedSummary.active_limit_usd)}
                                </div>
                              </div>
                              <div class="usage-metric">
                                <div class="usage-label">Usage so far</div>
                                <div class="usage-value">
                                  ${this._formatUsd(
                                    hostedSummary.current_usage_usd
                                  )}
                                </div>
                              </div>
                              <div class="usage-metric">
                                <div class="usage-label">
                                  Remaining before cap
                                </div>
                                <div class="usage-value">
                                  ${this._formatUsd(
                                    hostedSummary.remaining_limit_usd
                                  )}
                                </div>
                              </div>
                              <div class="usage-metric">
                                <div class="usage-label">Extra credits</div>
                                <div class="usage-value">
                                  ${this._formatExtraCreditPrice(
                                    hostedSummary.extra_credit_price_per_usd
                                  )}
                                </div>
                              </div>
                            </div>
                            <div class="usage-note">
                              Built-in models are Preloop-managed hosted models.
                              BYOK usage is not counted here.
                            </div>
                            <div class="usage-models">
                              ${
                                hostedSummary.models.length > 0
                                  ? hostedSummary.models.map(
                                      (model) => html`
                                        <div class="usage-model-row">
                                          <div>
                                            <div class="usage-model-name">
                                              ${model.model_name}
                                            </div>
                                            <div class="usage-model-meta">
                                              ${model.request_count}
                                              request${
                                                model.request_count === 1
                                                  ? ''
                                                  : 's'
                                              }
                                              ·
                                              ${model.total_tokens.toLocaleString()}
                                              tokens
                                              ${model.tier ? `· ${model.tier}` : ''}
                                            </div>
                                          </div>
                                          <div class="usage-model-cost">
                                            ${this._formatUsd(model.estimated_cost)}
                                          </div>
                                        </div>
                                      `
                                    )
                                  : html`
                                      <div class="usage-note">
                                        No built-in model usage recorded in this
                                        billing period yet.
                                      </div>
                                    `
                              }
                            </div>
                          </div>
                        `
                      : ''
                  }

                  <div>
                    <billing-toggle
                      .interval=${this._interval}
                      @interval-change=${(e: CustomEvent) =>
                        (this._interval = e.detail.value)}
                    ></billing-toggle>

                    <div
                      class="plans-grid"
                      @signup-requested=${this._handleUpgradeRequest}
                    >
                      ${availablePlans
                        .filter((p) => p.id !== this.subscription?.plan_id)
                        .map(
                          (plan) => html`
                            <pricing-card
                              .plan=${plan}
                              .interval=${this._interval}
                              .featureOrder=${this._featureOrder}
                              .featureLabels=${this._featureLabels}
                            ></pricing-card>
                          `
                        )}
                    </div>
                  </div>
                `
              : ''
          }
        </div>
      </div>
    `;
  }
}
