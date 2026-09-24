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
import { Router } from '../../../router';
import type { SessionArtifactUsage } from '../../../types';
import { PLAN_PAGE_PATH } from '../../../utils/premium-features';
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

/**
 * BYOK ingestion quota, from GET /api/v1/billing/summary (`ingestion_quota`).
 *
 * Exhausting this NEVER stops anything: the gateway keeps proxying and the
 * firewall, approvals and budgets keep enforcing. Only the detail of derived
 * analytics thins out. Any copy here that implies agents stop is a
 * product-safety bug, not a wording preference.
 */
interface IngestionQuota {
  plan_id: string;
  quota_tokens: number;
  used_tokens: number;
  remaining_tokens: number | null;
  is_unlimited: boolean;
  over_quota: boolean;
  degraded_analytics: boolean;
  usage_ratio: number;
  approaching_limit: boolean;
  period_start: string;
  period_end: string;
}

/** Seat usage against the plan's included bracket. */
interface SeatSummary {
  active_users: number;
  included_users: number | null;
  max_users: number | null;
  over_included: boolean;
  seat_addon: {
    price_per_user_monthly: number;
    price_per_user_annually: number;
    max_users: number;
  } | null;
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
  /**
   * What the account is entitled to right now. An ended trial leaves a stale
   * subscription row behind, so `subscription` alone never answers "what plan
   * am I on?". Older servers omit these fields; the view falls back to the
   * subscription status and period end.
   */
  effective_plan_id?: string | null;
  effective_plan?: { id: string; name: string } | null;
  trial: {
    is_trialing: boolean;
    days: number;
    requires_payment_method: boolean;
    hosted_model_hard_cap_usd: number | null;
    is_expired?: boolean;
    ended_at?: string | null;
  };
  hosted_models: {
    billing_period_start: string;
    billing_period_end: string;
    included_limit_usd: number | null;
    active_limit_usd: number | null;
    /** null when the server has no verified spend figure, not zero spend. */
    current_usage_usd: number | null;
    remaining_limit_usd: number | null;
    /**
     * Sent by the server, deliberately not rendered: there is no way to buy
     * extra usage yet, so the price would advertise a feature that does not
     * exist. Kept here because it describes the payload.
     */
    extra_credit_price_per_usd: number;
    models: HostedModelUsageRow[];
    /** One-time credit granted to card-free free accounts. Never resets. */
    one_time_credit_usd?: number | null;
    lifetime_usage_usd?: number | null;
  };
  ingestion_quota?: IngestionQuota | null;
  seats?: SeatSummary | null;
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
  @state() private _canManageBilling = false;
  @state() private _sessionArtifactUsage: SessionArtifactUsage | null = null;

  // The 2026 ladder's limits, in the order a buyer weighs them. The legacy
  // keys (api_calls_monthly, ai_calls_monthly, issues_ingested_monthly,
  // custom_*_enabled) still exist on the plan rows because the shared
  // PlanFeatures schema requires them, but they are unlimited everywhere and
  // describe nothing, so they are deliberately not listed.
  private _featureOrder = [
    'max_users',
    'max_agents',
    'byok_ingest_tokens_monthly',
    'hosted_models_monthly_limit_usd',
    'retention_days',
  ];

  private _featureLabels: Record<string, string> = {
    max_users: 'Users included',
    max_agents: 'Agents',
    byok_ingest_tokens_monthly: 'Analysis quota',
    hosted_models_monthly_limit_usd: 'Built-in model allowance',
    retention_days: 'Analytics history',
  };

  // Bare numbers are ambiguous once units are mixed: 90 could be dollars,
  // days, or seats. Each limit states its own unit.
  private _featureFormatters: Record<string, (value: any) => string | null> = {
    max_users: (v) => (v === -1 ? 'Unlimited' : v ? `${v}` : null),
    max_agents: (v) => (v === -1 ? 'Unlimited' : v ? `${v}` : null),
    byok_ingest_tokens_monthly: (v) =>
      v === -1 ? 'Unlimited' : v ? `${this._formatTokens(v)} / month` : null,
    hosted_models_monthly_limit_usd: (v) =>
      v === null || v === undefined ? null : `${this._formatUsd(v)} / month`,
    retention_days: (v) =>
      v === -1 ? 'Custom' : v === 365 ? '1 year' : v ? `${v} days` : null,
  };

  /**
   * Re-read the summary when something outside this view changed the
   * subscription.
   *
   * The plan comparison's own event reaches `_refreshBillingSummary` through
   * the template binding below; it is composed, so it also arrives here after
   * bubbling out of the shadow root. Ignoring anything that did not originate
   * on `window` keeps that one change to one fetch, and leaves this listener
   * for the module-level dispatch in `api.ts`, which has no element to bubble
   * from.
   */
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
          }
        }
      } catch {
        this._sessionArtifactUsage = null;
      }

      // Only fetch billing data for proprietary version
      const isProprietary = features.features['billing'] === true;

      if (isProprietary) {
        const [summaryRes, publicPlansRes, customPlansRes, optionsRes] =
          await Promise.all([
            fetchWithAuth('/api/v1/billing/summary'),
            fetchWithAuth('/api/v1/billing/plans'),
            fetchWithAuth('/api/v1/billing/custom-plans'),
            fetchWithAuth('/api/v1/billing/plan-change-options'),
          ]);

        // Whether the reader may open the provider portal at all. The plan
        // picker used to answer this, from this same endpoint, while it still
        // lived on this page. Asking here keeps the request count the same
        // and keeps "Manage in Stripe" from offering an action the account's
        // role cannot take. A failure leaves it disabled, as before.
        if (optionsRes.ok) {
          const options = await optionsRes.json();
          this._canManageBilling = options?.can_manage_billing === true;
        }

        if (summaryRes.ok) {
          this._billingSummary = await summaryRes.json();
          this.subscription = this._billingSummary?.subscription ?? null;
        } else {
          throw new Error('Failed to load billing summary.');
        }

        if (publicPlansRes.ok) {
          const allPlans = await publicPlansRes.json();
          // Hide only the $0 plan: it is what you already have when you have
          // no subscription, and it is not something you can check out. A
          // null price means sales-led (Enterprise), which must stay visible
          // so the card can offer a contact route.
          this._publicPlans = allPlans.filter(
            (p: Plan) => p.price_monthly === null || p.price_monthly > 0
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

  /**
   * Usage so far, where an absent figure on a capped plan means nothing was
   * spent yet. `_formatUsd` alone prints "Not configured" for null, which
   * reads as a broken plan on a brand new account that simply has not run
   * anything. Zero is only claimed when the plan does carry a limit; a plan
   * with neither an allowance nor a cap still says "Not configured", because
   * there the null describes the plan and not the spend.
   */
  private _formatUsageSoFar(hosted: BillingSummary['hosted_models']) {
    const spent = hosted.current_usage_usd;
    if (spent === null || spent === undefined) {
      return this._hasConfiguredLimit(hosted)
        ? this._formatUsd(0)
        : 'Not configured';
    }
    return this._formatUsd(spent);
  }

  /**
   * Room left before the cap. The server leaves this null when it has no cap
   * to subtract from, and also on a capped plan that has recorded no spend at
   * all; in the second case the whole cap is what remains, so print it rather
   * than "Not configured". Nothing is inferred when usage is already non-zero:
   * that subtraction belongs to the server, which knows about holds.
   */
  private _formatRemainingBeforeCap(hosted: BillingSummary['hosted_models']) {
    const remaining = hosted.remaining_limit_usd;
    const cap = hosted.active_limit_usd;
    if (remaining === null || remaining === undefined) {
      const unspent = !hosted.current_usage_usd;
      if (cap !== null && cap !== undefined && unspent) {
        return this._formatUsd(cap);
      }
      return 'Not configured';
    }
    return this._formatUsd(remaining);
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
    const date = new Date(value);
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
    const date = new Date(value);
    return !Number.isNaN(date.getTime()) && date.getTime() < Date.now();
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

  /**
   * "Choose a plan" and "View plans": the plan page.
   *
   * The plans used to be a section at the bottom of this page, under the
   * organisation name, the quota and the usage table, which is why an account
   * with no subscription had to scroll past everything it was not looking for
   * to find the only action it could take. They now have a page of their own,
   * and this page keeps the summary and the route to it.
   */
  private _handleChoosePlan() {
    if (!Router.go(PLAN_PAGE_PATH)) this._navigate(PLAN_PAGE_PATH);
  }

  /** A full page load, for the case where no router claimed the path. */
  private _navigate(url: string): void {
    window.location.assign(url);
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

  /**
   * Seats used against the plan's included bracket.
   *
   * Bracket pricing means the price does not move with the seat count, so
   * this is a capacity readout, never a running bill. Business is the one
   * plan that can buy past its bracket, so it is the one plan that gets an
   * add-on price quoted here.
   */
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
                >Extra users are $${addon.price_per_user_monthly} each per
                month, up to ${addon.max_users}.</span
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
  /** Used and budget bytes for session screenshots and recordings. */
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

  private _renderSessionArtifactUsage() {
    const usage = this._sessionArtifactUsage;
    if (!usage) return '';
    return html`
      <div class="card" data-testid="session-artifact-usage">
        <div class="current-row">
          <span class="plan-name">Session artifact storage</span>
        </div>
        <div class="usage-grid">
          <div class="usage-metric">
            <div class="usage-label">Used</div>
            <div class="usage-value">
              ${this._formatBytes(usage.used_bytes)} /
              ${this._formatBytes(usage.budget_bytes)}
            </div>
          </div>
          <div class="usage-metric">
            <div class="usage-label">Screenshots</div>
            <div class="usage-value">
              ${this._formatBytes(usage.by_kind.screenshot)}
            </div>
          </div>
          <div class="usage-metric">
            <div class="usage-label">Recordings</div>
            <div class="usage-value">
              ${this._formatBytes(usage.by_kind.recording)}
            </div>
          </div>
        </div>
      </div>
    `;
  }

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
                ${new Date(quota.period_end).toLocaleDateString()}. Upgrade to
                restore full detail sooner.`
              : quota.approaching_limit
                ? html`You have used most of this month's analysis quota. Agents
                  and policies are unaffected either way. Upgrade for a larger
                  quota.`
                : html`Tokens we analyze from your own provider keys. Resets
                  ${new Date(quota.period_end).toLocaleDateString()}. Your
                  provider tokens are never billed or marked up by Preloop.`
          }
        </div>
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

          ${this._renderSessionArtifactUsage()}
          ${
            isProprietary
              ? html`
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
                            ? this.subscription.status ===
                              'pending_cancellation'
                              ? 'Pending cancellation'
                              : this.subscription.status
                            : 'Free'
                        }
                      </span>
                    </div>
                    ${
                      trialExpired
                        ? html`<div class="date" data-testid="trial-ended">
                            Your
                            ${trialedPlanName ? `${trialedPlanName} ` : ''}trial
                            ended${trialEndedOn ? ` on ${trialEndedOn}` : ''}.
                            You are on the Free plan.
                          </div>`
                        : this.subscription
                          ? html`
                              <div class="date">
                                ${periodEndLabel}
                                ${this._formatDate(
                                  this.subscription.current_period_end
                                )}
                              </div>
                            `
                          : html`<div class="date">
                              You are on the Free plan. It does not expire and
                              needs no card.
                            </div>`
                    }
                    ${this._renderSeats(seats)}
                    ${
                      trialSummary?.is_trialing && !trialExpired
                        ? html`
                            <div class="date">
                              Trial cap for built-in models:
                              ${this._formatUsd(
                                trialSummary?.hosted_model_hard_cap_usd
                              )}
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
                              <span class="plan-name"
                                >Built-in model usage</span
                              >
                            </div>
                            <div class="date">
                              ${
                                this._isPast(hostedSummary.billing_period_end)
                                  ? 'Billing period ended'
                                  : 'Current billing period ends'
                              }
                              ${this._formatDate(
                                hostedSummary.billing_period_end
                              )}
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
                                          ${this._formatUsd(
                                            onFreePlan
                                              ? (hostedSummary.one_time_credit_usd ??
                                                  hostedSummary.included_limit_usd)
                                              : hostedSummary.included_limit_usd
                                          )}
                                        </div>
                                      </div>
                                      <div class="usage-metric">
                                        <div class="usage-label">
                                          Current active cap
                                        </div>
                                        <div class="usage-value">
                                          ${this._formatUsd(
                                            hostedSummary.active_limit_usd
                                          )}
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
                                        <div class="usage-label">
                                          Remaining before cap
                                        </div>
                                        <div class="usage-value">
                                          ${this._formatRemainingBeforeCap(
                                            hostedSummary
                                          )}
                                        </div>
                                      </div>
                                    `
                              }
                              <div class="usage-metric">
                                <div class="usage-label">Extra credits</div>
                                <div class="usage-value">
                                  ${this._extraCreditsLabel}
                                </div>
                              </div>
                            </div>
                            <div class="usage-note">
                              ${
                                staleTrialFigures
                                  ? html`<div style="margin-bottom: 0.75rem;">
                                      Allowances from the ended trial are not
                                      shown: they no longer describe what this
                                      account can spend.
                                    </div>`
                                  : ''
                              }
                              ${
                                onFreePlan
                                  ? html`Built-in models are Preloop-managed
                                    hosted models. The free credit is a one-time
                                    grant, not a monthly allowance, so it does
                                    not reset. When it runs out you can add your
                                    own provider key and keep going, or upgrade
                                    for a monthly allowance. Your own keys are
                                    never metered or billed here.`
                                  : html`Built-in models are Preloop-managed
                                    hosted models. Usage on your own provider
                                    keys is never billed and is not counted
                                    here.`
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
                `
              : ''
          }
        </div>
      </div>
    `;
  }
}
