import { formatUsd, formatUsdExact } from '../utils/money';
import { parseUTCDate } from '../utils/date';
import { LitElement, html, css, nothing } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import { fetchWithAuth, getFeatures } from '../api';
import { Router } from '../router';
import { PLAN_PAGE_PATH } from '../utils/premium-features';
import { billingStatusLabel } from '../utils/billing-status';
import type {
  BillingSummary,
  SeatSummary,
  IngestionQuota,
  Plan,
} from '../types/billing-summary';
import './hosted-allowance';

/** Subscription details on Plan, retaining the existing allowance/quota contracts. */
@customElement('billing-subscription-details')
export class BillingSubscriptionDetails extends LitElement {
  @property({ attribute: false }) summary: BillingSummary | null = null;
  @property({ attribute: false }) plans: Plan[] = [];
  @property({ type: Boolean }) canManageBilling = false;
  @state() private _error: string | null = null;
  @state() private _loading = true;
  async connectedCallback(): Promise<void> {
    super.connectedCallback();
    if (this.summary) {
      this._loading = false;
      return;
    }
    try {
      if ((await getFeatures()).features.billing !== true) return;
      const paths = [
        '/api/v1/billing/summary',
        '/api/v1/billing/plans',
        '/api/v1/billing/custom-plans',
        '/api/v1/billing/plan-change-options',
      ];
      const responses = await Promise.all(
        paths.map((path) => fetchWithAuth(path))
      );
      if (responses.some((response) => !response.ok))
        throw new Error('Could not load subscription details.');
      const [summary, plans, custom, options] = await Promise.all(
        responses.map((response) => response.json())
      );
      this.summary = summary;
      this.plans = [...custom, ...plans];
      this.canManageBilling = options.can_manage_billing === true;
    } catch (error) {
      this._error =
        error instanceof Error
          ? error.message
          : 'Could not load subscription details.';
    } finally {
      this._loading = false;
    }
  }

  private get _billingSummary() {
    return this.summary;
  }
  private get subscription() {
    return this.summary?.subscription ?? null;
  }
  private get _canManageBilling() {
    return this.canManageBilling;
  }
  private _handleChoosePlan() {
    if (!Router.go(PLAN_PAGE_PATH)) this._navigate(PLAN_PAGE_PATH);
  }
  private _navigate(url: string): void {
    window.location.assign(url);
  }

  /** Token counts, compacted: 10000000 becomes "10M". */
  private _formatTokens(value: number): string {
    if (value === -1) return 'Unlimited';
    return new Intl.NumberFormat('en-US', {
      notation: 'compact',
      compactDisplay: 'short',
      maximumFractionDigits: 1,
    }).format(value);
  }

  /**
   * What happens past the allowance, today.
   *
   * There is no opt-in for extra usage: nothing in the product sells, grants
   * or meters credit beyond the allowance, so every sentence that offered one
   * ("billed at cost only when you opt in") promised a feature that does not
   * exist. Until prepaid credits ship, the only true statement is that usage
   * stops. `extra_credit_price_per_usd` is deliberately not rendered: a price
   * for something no one can buy is a quote, not a fact.
   */
  private get _extraCreditsLabel() {
    return 'Usage stops at the allowance.';
  }

  /** Unverified ledger history must not be presented as zero spending. */
  private _formatUsageSoFar(hosted: BillingSummary['hosted_models']) {
    return hosted.current_usage_usd == null
      ? 'Not verified'
      : hosted.current_usage_usd == null
        ? 'Not configured'
        : formatUsd(hosted.current_usage_usd);
  }

  /** Remaining credit includes holds; a missing value is not the full cap. */
  private _formatRemainingBeforeCap(hosted: BillingSummary['hosted_models']) {
    if (hosted.remaining_limit_usd == null)
      return this._hasConfiguredLimit(hosted)
        ? 'Not verified'
        : 'Not configured';
    return hosted.remaining_limit_usd == null
      ? 'Not configured'
      : formatUsd(hosted.remaining_limit_usd);
  }

  /**
   * True when the plan carries a spendable limit of any kind: the recurring
   * cap, the allowance it comes from, or the Free tier's one-time credit.
   * Only a plan with none of the three has nothing to measure spend against.
   */
  private _hasConfiguredLimit(hosted: BillingSummary['hosted_models']) {
    return [
      hosted.active_limit_usd,
      hosted.included_limit_usd,
      hosted.one_time_credit_usd,
    ].some((limit) => limit !== null && limit !== undefined);
  }

  /** "Jul 27", or "Jul 27, 2025" when the year is not the current one. */
  private _formatDate(value: string | null | undefined) {
    if (!value) {
      return 'Unknown';
    }
    const date = parseUTCDate(value);
    if (Number.isNaN(date.getTime())) {
      return 'Unknown';
    }
    const sameYear = date.getFullYear() === new Date().getFullYear();
    return date.toLocaleDateString(undefined, {
      month: 'short',
      day: 'numeric',
      ...(sameYear ? {} : { year: 'numeric' }),
    });
  }

  /** A confirmed date, or null when the value would print as "Unknown". */
  private _knownDate(value: string | null | undefined) {
    if (!value) {
      return null;
    }
    const formatted = this._formatDate(value);
    return formatted === 'Unknown' ? null : formatted;
  }

  private _isPast(value: string | null | undefined) {
    if (!value) {
      return false;
    }
    const date = parseUTCDate(value);
    return !Number.isNaN(date.getTime()) && date.getTime() < Date.now();
  }

  private _renderSeats(seats: SeatSummary | null) {
    if (!seats || seats.included_users === null) return '';
    const addon = seats.seat_addon;
    const agentLimit = this._billingSummary?.plan?.features?.max_agents;
    return html`
      <div class="date">
        ${seats.active_users} of ${seats.included_users} included
        ${seats.included_users === 1 ? 'user' : 'users'} in use.
        ${agentLimit === -1 ? 'Agents are unlimited.' : typeof agentLimit === 'number' ? `Agent allowance: ${agentLimit}.` : ''}
        ${
          seats.over_included && addon
            ? html`<span class="seat-warning"
                >Extra users are
                ${html`<span title=${formatUsdExact(addon.price_per_user_monthly)}>${formatUsd(addon.price_per_user_monthly)}</span>`}
                each per month, up to ${addon.max_users}.</span
              >`
            : seats.over_included
              ? html`<span class="seat-warning"
                  >You are over the included seats. Upgrade to add more.</span
                >`
              : ''
        }
      </div>
    `;
  }

  /**
   * BYOK analysis quota meter.
   *
   * The copy must never suggest that agents stop. They do not: over quota,
   * the gateway keeps proxying and every policy keeps enforcing, and the only
   * consequence is thinner analytics detail. This is a product-safety rule
   * from the canonical pricing spec, not a tone preference.
   */
  private _renderIngestionQuota(quota: IngestionQuota | null) {
    if (!quota || quota.is_unlimited) return '';
    const percent = Math.min(Math.round(quota.usage_ratio * 100), 100);
    return html`
      <div class="card">
        <div class="current-row">
          <span class="plan-name">Analysis quota</span>
          <span class="quota-figures">
            ${this._formatTokens(quota.used_tokens)} of
            ${this._formatTokens(quota.quota_tokens)} tokens
          </span>
        </div>
        <div
          class="quota-bar"
          role="progressbar"
          aria-valuenow=${percent}
          aria-valuemin="0"
          aria-valuemax="100"
          aria-label="Analysis quota used"
        >
          <div
            class="quota-fill ${
              quota.over_quota ? 'over' : quota.approaching_limit ? 'warn' : ''
            }"
            style="width: ${percent}%"
          ></div>
        </div>
        <div class="usage-note">
          ${
            quota.over_quota
              ? html`You have used this month's analysis quota. Your agents keep
                running and every policy still applies. New traffic is recorded
                with less analysis detail until the quota resets on
                ${parseUTCDate(quota.period_end).toLocaleDateString()}. Upgrade
                to restore full detail sooner.`
              : quota.approaching_limit
                ? html`You have used most of this month's analysis quota. Agents
                  and policies are unaffected either way. Upgrade for a larger
                  quota.`
                : html`Tokens we analyze from your own provider keys. Resets
                  ${parseUTCDate(quota.period_end).toLocaleDateString()}. Your
                  provider tokens are never billed or marked up by Preloop.`
          }
        </div>
      </div>
    `;
  }

  private async _handleManageSubscription() {
    if (!this._canManageBilling) return;
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

  render() {
    if (!this.summary) return nothing;
    const availablePlans = this.plans;
    const currentPlanName = this._billingSummary?.plan?.name
      ? this._billingSummary.plan.name
      : this.subscription?.plan_id
        ? (availablePlans.find((p) => p.id === this.subscription?.plan_id)
            ?.name ?? 'Free')
        : 'Free';
    const hostedSummary = this._billingSummary?.hosted_models;
    const trialSummary = this._billingSummary?.trial;
    // A renewal date in the past is not a renewal. Say what happened on that
    // date instead of promising a renewal that never came. A trial does not
    // renew either, so it never says "Renews on" in any direction.
    const periodEnded = this._isPast(this.subscription?.current_period_end);
    // An ended trial is not a trial. The provider row can sit at "trialing"
    // long after the trial stopped entitling anything, so the card reports
    // what the account has now, not the status Stripe last wrote.
    const trialExpired =
      trialSummary?.is_expired === true ||
      (trialSummary?.is_expired === undefined &&
        this.subscription?.status === 'trialing' &&
        periodEnded);
    const isTrialing =
      !trialExpired &&
      (this.subscription?.status === 'trialing' ||
        Boolean(trialSummary?.is_trialing));
    const periodEndLabel = periodEnded
      ? isTrialing
        ? 'Trial ended'
        : this.subscription?.status === 'pending_cancellation'
          ? 'Cancelled on'
          : 'Ended'
      : this.subscription?.status === 'pending_cancellation'
        ? 'Cancels on'
        : isTrialing
          ? 'Trial ends on'
          : 'Renews on';
    const quota = this._billingSummary?.ingestion_quota ?? null;
    const seats = this._billingSummary?.seats ?? null;
    // The Free tier carries a ONE-TIME hosted credit rather than a monthly
    // allowance. Entitlement decides that, not the presence of a row: an
    // account whose trial ended keeps its subscription row and is on Free.
    const effectivePlanId =
      this._billingSummary?.effective_plan_id ??
      (!this.subscription || trialExpired ? 'free' : this.subscription.plan_id);
    const onFreePlan = effectivePlanId === 'free';
    // The plan that was trialed, for the sentence that says it ended. Once
    // the server reports the effective plan, `plan` is Free and cannot name
    // the trialed plan, so it is only used when it names something else.
    const summaryPlan = this._billingSummary?.plan ?? null;
    const trialedPlanName =
      availablePlans.find((p) => p.id === this.subscription?.plan_id)?.name ??
      (summaryPlan && summaryPlan.id !== effectivePlanId
        ? summaryPlan.name
        : null);
    const trialEndedOn = this._knownDate(
      trialSummary?.ended_at ?? this.subscription?.current_period_end
    );
    // Trial figures describe a trial that is over. Without the Free fields
    // from the server there is no verified allowance to print, and printing
    // the expired trial's cap as an allowance is the mis-sell to avoid.
    const staleTrialFigures =
      trialExpired && hostedSummary?.one_time_credit_usd == null;
    // `effective_plan.name` is the server's answer, resolved from the plan
    // catalog: the catalog is the only place that knows a plan has been
    // renamed, and the legacy per-seat plan was renamed to "Legacy Teams"
    // when it was withdrawn. The persisted plan row deliberately keeps the
    // name it was sold under so a catalog sync can never rewrite a
    // grandfathered contract, which makes `plan.name` a stale display source
    // and a fallback only, for a server that predates this field.
    const displayPlanName =
      this._billingSummary?.effective_plan?.name ??
      (trialExpired ? 'Free' : currentPlanName);

    return html`
      ${this._error ? html`<sl-alert variant="danger" open>${this._error}</sl-alert>` : nothing}
      <hosted-allowance></hosted-allowance>
      <!-- Subscription Section (Proprietary Only) -->
      <div class="card current-plan">
        <div class="current-row">
          <span class="plan-name">${displayPlanName}</span>
          <span
            class="status-chip ${
              this.subscription?.status === 'pending_cancellation'
                ? 'pending'
                : ''
            }"
          >
            ${
              this.subscription && !trialExpired
                ? this.subscription.status === 'pending_cancellation'
                  ? 'Pending cancellation'
                  : billingStatusLabel(this.subscription.status)
                : 'Free'
            }
          </span>
        </div>
        ${
          trialExpired
            ? html`<div class="date" data-testid="trial-ended">
                Your ${trialedPlanName ? `${trialedPlanName} ` : ''}trial
                ended${trialEndedOn ? ` on ${trialEndedOn}` : ''}. You are on
                the Free plan.
              </div>`
            : this.subscription
              ? html`
                  <div class="date">
                    ${periodEndLabel}
                    ${this._formatDate(this.subscription.current_period_end)}
                  </div>
                `
              : html`<div class="date">
                  You are on the Free plan. It does not expire and needs no
                  card.
                </div>`
        }
        ${this._renderSeats(seats)}
        ${
          trialSummary?.is_trialing && !trialExpired
            ? html`
                <div class="date">
                  Trial cap for built-in models:
                  ${trialSummary?.hosted_model_hard_cap_usd == null ? 'Not configured' : html`<span title=${formatUsdExact(trialSummary?.hosted_model_hard_cap_usd)}>${formatUsd(trialSummary?.hosted_model_hard_cap_usd)}</span>`}
                </div>
              `
            : ''
        }
        <div class="actions">
          ${
            // The provider portal manages a subscription. An
            // account on Free has none, so the button was always
            // disabled there: a dead control where the only useful
            // action belongs. Free gets that action instead.
            this.subscription
              ? html`<sl-button
                    size="medium"
                    variant=${trialExpired ? 'default' : 'primary'}
                    data-testid="manage-in-stripe"
                    ?disabled=${!this._canManageBilling}
                    @click=${this._handleManageSubscription}
                  >
                    Manage in Stripe
                  </sl-button>
                  <sl-button
                    size="medium"
                    variant="default"
                    data-testid="view-plans"
                    @click=${this._handleChoosePlan}
                  >
                    View plans
                  </sl-button>`
              : html`<sl-button
                  size="medium"
                  variant="primary"
                  data-testid="choose-a-plan"
                  @click=${this._handleChoosePlan}
                >
                  Choose a plan
                </sl-button>`
          }
        </div>
      </div>

      ${this._renderIngestionQuota(quota)}
      ${
        hostedSummary
          ? html`
              <div class="card">
                <div class="current-row">
                  <span class="plan-name">Built-in model usage</span>
                </div>
                <div class="date">
                  ${
                    this._isPast(hostedSummary.billing_period_end)
                      ? 'Billing period ended'
                      : 'Current billing period ends'
                  }
                  ${this._formatDate(hostedSummary.billing_period_end)}
                </div>
                <div class="usage-grid">
                  ${
                    staleTrialFigures
                      ? ''
                      : html`
                          <div class="usage-metric">
                            <div class="usage-label">
                              ${
                                onFreePlan
                                  ? 'One-time credit'
                                  : 'Monthly allowance'
                              }
                            </div>
                            <div class="usage-value">
                              ${
                                (onFreePlan
                                  ? (hostedSummary.one_time_credit_usd ??
                                    hostedSummary.included_limit_usd)
                                  : hostedSummary.included_limit_usd) == null
                                  ? 'Not configured'
                                  : html`<span
                                      title=${formatUsdExact(
                                        onFreePlan
                                          ? (hostedSummary.one_time_credit_usd ??
                                              hostedSummary.included_limit_usd)
                                          : hostedSummary.included_limit_usd
                                      )}
                                      >${formatUsd(
                                        onFreePlan
                                          ? (hostedSummary.one_time_credit_usd ??
                                              hostedSummary.included_limit_usd)
                                          : hostedSummary.included_limit_usd
                                      )}</span
                                    >`
                              }
                            </div>
                          </div>
                          <div class="usage-metric">
                            <div class="usage-label">Current active cap</div>
                            <div class="usage-value">
                              ${hostedSummary.active_limit_usd == null ? 'Not configured' : html`<span title=${formatUsdExact(hostedSummary.active_limit_usd)}>${formatUsd(hostedSummary.active_limit_usd)}</span>`}
                            </div>
                          </div>
                        `
                  }
                  <div class="usage-metric">
                    <div class="usage-label">Usage so far</div>
                    <div class="usage-value">
                      ${this._formatUsageSoFar(hostedSummary)}
                    </div>
                  </div>
                  ${
                    staleTrialFigures
                      ? ''
                      : html`
                          <div class="usage-metric">
                            <div class="usage-label">Remaining before cap</div>
                            <div class="usage-value">
                              ${this._formatRemainingBeforeCap(hostedSummary)}
                            </div>
                          </div>
                        `
                  }
                  <div class="usage-metric">
                    <div class="usage-label">Extra credits</div>
                    <div class="usage-value">${this._extraCreditsLabel}</div>
                  </div>
                </div>
                <div class="usage-note">
                  ${
                    staleTrialFigures
                      ? html`<div style="margin-bottom: 0.75rem;">
                          Allowances from the ended trial are not shown: they no
                          longer describe what this account can spend.
                        </div>`
                      : ''
                  }
                  ${
                    onFreePlan
                      ? html`Built-in models are Preloop-managed hosted models.
                        The free credit is a one-time grant, not a monthly
                        allowance, so it does not reset. When it runs out you
                        can add your own provider key and keep going, or upgrade
                        for a monthly allowance. Your own keys are never metered
                        or billed here.`
                      : html`Built-in models are Preloop-managed hosted models.
                        Usage on your own provider keys is never billed and is
                        not counted here.`
                  }
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
                                  request${model.request_count === 1 ? '' : 's'}
                                  · ${model.total_tokens.toLocaleString()}
                                  tokens ${model.tier ? `· ${model.tier}` : ''}
                                </div>
                              </div>
                              <div class="usage-model-cost">
                                ${model.estimated_cost == null ? 'Not configured' : html`<span title=${formatUsdExact(model.estimated_cost)}>${formatUsd(model.estimated_cost)}</span>`}
                              </div>
                            </div>
                          `
                        )
                      : html`
                          <div class="usage-note">
                            No built-in model usage recorded in this billing
                            period yet.
                          </div>
                        `
                  }
                </div>
              </div>
            `
          : ''
      }
    `;
  }
  static styles = css`
    :host {
      display: block;
      margin-bottom: 2rem;
    }
    .card {
      padding: 1.5rem;
      border: 1px solid var(--sl-color-neutral-200);
      border-radius: 12px;
      margin: 1rem 0;
    }
    .current-row,
    .usage-model-row,
    .usage-overview {
      display: flex;
      justify-content: space-between;
      gap: 1rem;
      flex-wrap: wrap;
    }
    .plan-name {
      font-weight: 600;
      font-size: 1.2rem;
    }
    .status-chip {
      padding: 0.25rem 0.5rem;
      border-radius: 1rem;
      background: var(--sl-color-neutral-100);
    }
    .actions {
      display: flex;
      gap: 0.75rem;
      margin-top: 1rem;
    }
    .date,
    .usage-note {
      margin: 0.5rem 0;
      color: var(--sl-color-neutral-600);
    }
    .quota-bar {
      height: 0.5rem;
      background: var(--sl-color-neutral-100);
      border-radius: 1rem;
      overflow: hidden;
    }
    .quota-fill {
      height: 100%;
      background: var(--sl-color-primary-500);
    }
    .quota-fill.over,
    .seat-warning {
      color: var(--sl-color-warning-700);
    }
    .usage-models {
      margin-top: 1rem;
    }
    .usage-model-row {
      padding: 0.5rem 0;
    }
    .usage-meta {
      font-size: 0.9rem;
    }
  `;
}
