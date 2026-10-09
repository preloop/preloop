import { ConsoleStatus } from '../../../controllers/console-status';
import { parseUTCDate } from '../../../utils/date';
import '../../../components/billing-subscription-details';
import type { BillingSummary } from '../../../types/billing-summary';
import { LitElement, html, css, unsafeCSS, nothing } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import consoleStyles from '../../../styles/console-styles.css?inline';
import pricingStyles from '../../../styles/pricing-styles.css?inline';
import '../../../components/view-header.ts';
import '../../../components/billing-toggle';
import '../../../components/pricing-plans';
import '../../../components/billing-plan-comparison';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import {
  pricingPlansStyles,
  type Comparison,
  type PlanCta,
  type PricingPlan,
} from '../../../components/pricing-plans';
import type { BillingPlanComparison } from '../../../components/billing-plan-comparison';
import { cloudPlans, loadPricingContent } from '../../../utils/pricing-content';
import {
  BILLING_SUBSCRIPTION_CHANGED,
  fetchWithAuth,
  getFeatures,
  startCheckout,
} from '../../../api';
import type {
  BillingPlan,
  PlanChangeOptions,
  PlanEligibility,
} from '../../../types/billing';
import {
  PLAN_PAGE_PATH,
  capabilityForFeature,
  cheapestPlanUnlocking,
  premiumFeatureLabel,
} from '../../../utils/premium-features';

/**
 * Where checkout returns to, so a bought plan lands back on this page.
 *
 * The route string itself lives in `premium-features`, which declares itself
 * the one place to edit when the page moves; a second literal here would keep
 * sending buyers to the old path long after the first one changed.
 */
const PLAN_PATH = PLAN_PAGE_PATH;

/**
 * The console plan page.
 *
 * It renders the SAME cards and the SAME comparison table as the public
 * pricing page, from the same published content, because a reader who saw the
 * public ladder and then signed in must not be shown a different one. What it
 * adds is everything only a signed-in account knows: which plan is current,
 * what each card's button would actually do, and what a change costs.
 *
 * The page states the offer; it never performs a change on its own. A card
 * button either opens checkout (an account with no subscription has nothing
 * to switch, so there is nothing to quote) or hands the chosen plan to the
 * plan change section below, which quotes the price and the effective date
 * from the server and asks for confirmation there.
 */
@customElement('plan-view')
export class PlanView extends LitElement {
  private readonly accessibilityStatus = new ConsoleStatus(this);
  @state() private _loading = true;
  @state() private _error = '';
  /** Without the billing plugin this deployment sells nothing. */
  @state() private _billingEnabled = false;
  @state() private _plans: PricingPlan[] = [];
  @state() private _comparison: Comparison | null = null;
  @state() private _comparisonTitle = 'Compare plans';
  /**
   * Annual by default, like the public page and the plan change section: the
   * same catalog opened on two different periods looked like two prices.
   */
  @state() private _interval: 'month' | 'year' = 'year';
  @state() private _options: PlanChangeOptions | null = null;
  /** The plan the reader arrived asking about, from `?plan=` or `?feature=`. */
  @state() private _requestedPlan = '';
  /** The refused feature behind `?feature=`, exactly as the 402 named it. */
  @state() private _requestedFeature = '';
  /** The plan capability that feature maps to, matched against each plan. */
  @state() private _requestedCapability = '';
  /**
   * What to call that capability in a sentence.
   *
   * The name the reader was refused ("AI session optimization") and the
   * capability a plan sells ("analysis with built-in models") are not the
   * same word, so the card matches on the capability and speaks the refusal.
   */
  @state() private _requestedLabel = '';
  @state() private _checkoutPlan = '';
  @state() private _notice = '';
  @state() private _summary: BillingSummary | null = null;

  /**
   * Re-read the current plan when something changed the subscription.
   *
   * The plan change section's own event reaches `_reloadOptions` through the
   * template binding on the element; it is composed, so it also arrives here
   * after bubbling out of the shadow root. Ignoring anything that did not
   * originate on `window` keeps one change to one fetch, and leaves this
   * listener for the module-level dispatch in `api.ts`, which has no element
   * to bubble from.
   */
  private _refreshOnChange = (event: Event) => {
    if (event.target !== window) return;
    void this._reloadOptions();
  };

  /**
   * Reload the plan state after a change, and say so if the reload fails.
   *
   * `_loadOptions` throws on a bad response, so calling it from an event
   * handler without this would turn a failed refresh into an unhandled
   * rejection: cards frozen on the plan the account no longer holds, and
   * nothing on screen admitting it.
   */
  private async _reloadOptions(): Promise<void> {
    try {
      await Promise.all([this._loadOptions(), this._loadSummary()]);
      this._error = '';
    } catch (error) {
      this._error =
        error instanceof Error
          ? error.message
          : 'Could not reload your current plan.';
    }
  }

  async connectedCallback() {
    super.connectedCallback();
    window.addEventListener(
      BILLING_SUBSCRIPTION_CHANGED,
      this._refreshOnChange
    );
    this._readQuery();
    await this._load();
  }

  disconnectedCallback() {
    window.removeEventListener(
      BILLING_SUBSCRIPTION_CHANGED,
      this._refreshOnChange
    );
    super.disconnectedCallback();
  }

  /** `?plan=`, `?interval=` and `?feature=`, as the upgrade dialog sends them. */
  private _readQuery(): void {
    const params = new URLSearchParams(window.location.search);
    const plan = params.get('plan');
    if (plan) this._requestedPlan = plan;
    const feature = params.get('feature');
    if (feature) {
      this._requestedFeature = feature;
      this._requestedCapability = capabilityForFeature(feature);
      this._requestedLabel = premiumFeatureLabel(feature);
    }
    const interval = params.get('interval');
    if (interval === 'month' || interval === 'year') this._interval = interval;
  }

  private async _load(): Promise<void> {
    this._loading = true;
    try {
      const features = await getFeatures();
      this._billingEnabled = features.features['billing'] === true;
      if (!this._billingEnabled) return;
      // The published content is the price list; the account's options say
      // what this account may do with it. Neither one alone can draw the page.
      const [content] = await Promise.all([
        loadPricingContent(),
        this._loadOptions(),
        this._loadSummary(),
      ]);
      this._plans = cloudPlans(content);
      this._comparison = content.comparison;
      if (content.comparison?.title) {
        this._comparisonTitle = content.comparison.title;
      }
    } catch (error) {
      this._error =
        error instanceof Error ? error.message : 'Could not load the plans.';
    } finally {
      this._loading = false;
    }
  }

  private async _loadSummary(): Promise<void> {
    const response = await fetchWithAuth('/api/v1/billing/summary', {
      cache: 'no-store',
    });
    if (!response.ok)
      throw new Error('Could not load your subscription and usage.');
    this._summary = await response.json();
  }

  /** Current plan, eligibility and permission, from the billing plugin. */
  private async _loadOptions(): Promise<void> {
    const response = await fetchWithAuth(
      '/api/v1/billing/plan-change-options',
      { cache: 'no-store' }
    );
    if (!response.ok) throw new Error('Could not load your current plan.');
    const options = (await response.json()) as PlanChangeOptions;
    this._options = options;
    if (!this._requestedPlan && this._requestedFeature) {
      const unlocking = this._cheapestUnlocking(
        options,
        this._requestedFeature
      );
      if (unlocking) this._requestedPlan = unlocking;
    }
  }

  /**
   * A trialing subscription whose period has ended entitles nothing, so the
   * account is on Free. Same rule as the account page and the plan change
   * section: the provider row outlives the entitlement.
   */
  private _trialExpired(options: PlanChangeOptions): boolean {
    const subscription = options.current_subscription;
    if (subscription?.status !== 'trialing') return false;
    if (!subscription.current_period_end) return false;
    const date = parseUTCDate(subscription.current_period_end);
    return !Number.isNaN(date.getTime()) && date.getTime() < Date.now();
  }

  /** The plan the account is entitled to right now. */
  private _currentPlanId(): string {
    const options = this._options;
    if (!options) return '';
    if (this._trialExpired(options)) return 'free';
    return options.current_plan?.id ?? 'free';
  }

  /**
   * Whether this plan is quoted rather than sold, for the label and the click.
   *
   * The server answers it outright when it sends eligibility. When it does
   * not (an older server sends no `plan_eligibility` at all), the catalog's
   * `purchasable` flag decides, and a published card with no price either way
   * is a sales conversation whatever the catalog says: a plan the page itself
   * prints as "Priced per deployment" has no amount to put through checkout.
   */
  private _isQuoteOnly(
    plan: PricingPlan | undefined,
    catalog: BillingPlan | undefined,
    verdict: PlanEligibility | undefined
  ): boolean {
    if (verdict) return verdict.purchasable === false;
    return (
      catalog?.purchasable === false ||
      (!!plan && plan.price_monthly == null && plan.price_annually == null)
    );
  }

  private _catalogPlan(planId: string): BillingPlan | undefined {
    return this._options?.plans.find((p) => p.id === planId);
  }

  /**
   * The cheapest plan this account can hold that unlocks `feature`.
   *
   * The rule lives in `premium-features` and is shared with the quote panel
   * below, which asks the same question about the same reader: when the two
   * answered separately they could name different plans, and the card the
   * page highlighted was then not the plan the panel opened on.
   */
  private _cheapestUnlocking(
    options: PlanChangeOptions,
    feature: string
  ): string {
    const currentId = this._currentPlanId();
    return cheapestPlanUnlocking(
      options.plans,
      feature,
      (plan) =>
        plan.id !== currentId &&
        options.plan_eligibility?.find((e) => e.plan_id === plan.id)
          ?.eligible !== false
    );
  }

  private _monthlyPrice(planId: string): number {
    const price = this._catalogPlan(planId)?.price_monthly;
    return typeof price === 'number' ? price : Number.POSITIVE_INFINITY;
  }

  private _date(value: string | null | undefined): string {
    if (!value) return '';
    const date = parseUTCDate(value);
    return Number.isNaN(date.getTime())
      ? ''
      : date.toLocaleDateString(undefined, {
          year: 'numeric',
          month: 'long',
          day: 'numeric',
        });
  }

  /** When a change that waits for the period end would actually happen. */
  private _periodEnd(): string {
    return this._date(this._options?.current_subscription?.current_period_end);
  }

  /**
   * What one card's button says and does.
   *
   * Every answer here is about a relation between two plans, never about the
   * plan alone: the same card reads "Your plan", "Upgrade", or "Switch"
   * depending on what the account holds today. The founder decisions are
   * stated in the note under the button, before anything is clicked: upgrades
   * apply immediately with credit for the unused time, and downgrades,
   * including a move to Free, take effect at the end of the paid period and
   * never refund.
   */
  private _ctaFor = (plan: PricingPlan): PlanCta | null => {
    const options = this._options;
    if (!options) return { label: 'Loading', disabled: true };
    const catalog = this._catalogPlan(plan.id);
    const currentId = this._currentPlanId();
    const verdict = options.plan_eligibility?.find(
      (e) => e.plan_id === plan.id
    );

    if (plan.id === currentId) {
      const subscription = options.current_subscription;
      const note = !subscription
        ? 'No card required. The Free plan does not expire.'
        : subscription.cancel_at_period_end
          ? `Ends on ${this._periodEnd()}.`
          : `Renews on ${this._periodEnd()}.`;
      return { label: 'Your plan', disabled: true, note };
    }

    // A quote-only plan is not bought from a console. The server supplies the
    // destination when it has one; the card falls back to the demo request.
    if (this._isQuoteOnly(plan, catalog, verdict)) {
      return {
        label: plan.cta_text || 'Contact us',
        note: 'Priced per deployment.',
      };
    }

    if (catalog === undefined) {
      // The content offers a plan this deployment's catalog does not sell.
      return { label: 'Not available', disabled: true };
    }

    if (this._checkoutPlan === plan.id) {
      return { label: 'Opening checkout', disabled: true };
    }

    if (verdict && verdict.eligible === false) {
      const reason = verdict.blockers[0]?.message ?? '';
      return {
        label: `Choose ${plan.name}`,
        disabled: true,
        note: reason || 'This plan is not available for this account.',
      };
    }

    if (options.can_manage_billing === false) {
      return {
        label: `Choose ${plan.name}`,
        disabled: true,
        note: 'Only a billing owner or account administrator can change this subscription.',
      };
    }

    const unlocks =
      this._requestedCapability &&
      (catalog.capabilities ?? []).includes(this._requestedCapability)
        ? `Includes ${this._requestedLabel}. `
        : '';

    if (!options.current_subscription || this._trialExpired(options)) {
      // Nothing to switch: there is no subscription to re-price, so the card
      // goes straight to secure checkout rather than quoting a change.
      return {
        label: `Choose ${plan.name}`,
        note: `${unlocks}Secure checkout shows the final amount and any taxes.`,
      };
    }

    if (plan.id === 'free') {
      return {
        label: 'Switch to Free',
        note: `Takes effect on ${this._periodEnd()}.`,
      };
    }

    const upgrade = this._monthlyPrice(plan.id) > this._monthlyPrice(currentId);
    return upgrade
      ? {
          label: `Upgrade to ${plan.name}`,
          note: `${unlocks}Applies immediately, with credit for the unused time. The next step shows the exact amount.`,
        }
      : {
          label: `Switch to ${plan.name}`,
          note: `Takes effect on ${this._periodEnd()}.`,
        };
  };

  /**
   * The card row's CTA callback, rebuilt on every render on purpose.
   *
   * `_ctaFor` reads state the row cannot see (the current plan, the period
   * end, an in-flight checkout), and its identity never changes, so passing
   * the method itself left Lit with nothing changed to notice: the row kept
   * whatever it had rendered the first time. After a confirmed change that
   * meant a stale "Your plan" mark on the plan the account had just left. A
   * new closure is what tells the row to ask again; four cards is a cheap
   * question to re-ask.
   */
  private get _cardCta(): (plan: PricingPlan) => PlanCta | null {
    return (plan: PricingPlan) => this._ctaFor(plan);
  }

  private get _changeSection(): BillingPlanComparison | null {
    return this.renderRoot?.querySelector('billing-plan-comparison') ?? null;
  }

  /** A card's button was pressed. One relation, one destination. */
  private _handleCardAction = (event: CustomEvent) => {
    const planId = String(event.detail?.planId ?? '');
    const options = this._options;
    if (!planId || !options) return;
    this._notice = '';
    const plan = this._plans.find((p) => p.id === planId);
    const catalog = this._catalogPlan(planId);
    const verdict = options.plan_eligibility?.find((e) => e.plan_id === planId);
    // The same question the card's label answered. One predicate, because a
    // button that says "Contact us" and starts a checkout is the drift this
    // page exists to remove.
    if (this._isQuoteOnly(plan, catalog, verdict) || catalog === undefined) {
      this._navigate(verdict?.contact_url || plan?.cta_url || '/request-demo');
      return;
    }
    if (!options.current_subscription || this._trialExpired(options)) {
      void this._checkout(planId);
      return;
    }
    // A live subscription is being re-priced, so the quote, the consequences
    // and the confirmation all belong to the section that owns them.
    const section = this._changeSection;
    section?.startChange(planId, this._interval);
    section?.scrollIntoView?.({ behavior: 'smooth', block: 'start' });
  };

  private async _checkout(planId: string): Promise<void> {
    this._checkoutPlan = planId;
    this._error = '';
    try {
      const outcome = await startCheckout(planId, this._interval, PLAN_PATH);
      // `redirect` has already navigated. Anything else sent a sentence.
      if (outcome && outcome.action !== 'redirect')
        this._notice = outcome.message;
    } catch (error) {
      this._error =
        error instanceof Error
          ? error.message
          : 'Checkout could not be started.';
    } finally {
      this._checkoutPlan = '';
    }
  }

  /** Split out so a test can watch where a contact card sends the reader. */
  private _navigate(url: string): void {
    window.location.assign(url);
  }

  /**
   * The line a reader needs when the plan they hold has no card above.
   *
   * A legacy plan (per seat, no longer sold) is absent from the published
   * ladder, so without this the page would list four plans, mark none of them
   * and leave the account guessing what it is on. Accounts whose plan does
   * have a card get nothing here: that card already says "Your plan".
   */
  private _renderCurrentPlanLine() {
    const current = this._options?.current_plan;
    if (!current || !this._options) return nothing;
    if (this._currentPlanId() === 'free') return nothing;
    if (this._plans.some((plan) => plan.id === current.id)) return nothing;
    const ends = this._options.current_subscription?.cancel_at_period_end;
    const date = this._periodEnd();
    const when = date ? ` It ${ends ? 'ends' : 'renews'} on ${date}.` : '';
    return html`<p class="current-plan" data-testid="current-plan-line">
      You are on ${current.name}, which is not in the list below. It stays
      active until you change it.${when}
    </p>`;
  }

  private _renderCards() {
    if (!this._plans.length) return nothing;
    return html`
      ${this._renderCurrentPlanLine()}
      <div class="period-row">
        <billing-toggle
          .compact=${true}
          .interval=${this._interval}
          @interval-change=${(e: CustomEvent) =>
            (this._interval = e.detail.value)}
        ></billing-toggle>
      </div>
      <div @signup-requested=${this._handleCardAction}>
        <pricing-plan-cards
          .plans=${this._plans}
          .interval=${this._interval}
          .ctaFor=${this._cardCta}
          .highlightId=${this._requestedPlan}
        ></pricing-plan-cards>
      </div>
    `;
  }

  render() {
    if (this._loading) {
      return html`
        <view-header headerText="Plan" width="wide"></view-header>
        <div class="column-layout wide">
          <div class="main-column">
            <div class="loading">
              <sl-spinner style="font-size: 3rem;"></sl-spinner>
            </div>
          </div>
        </div>
      `;
    }

    return html`
      <view-header headerText="Plan" width="wide"></view-header>
      <div class="column-layout wide">
        <div class="main-column">
          ${
            this._error
              ? html`<sl-alert variant="danger" open data-testid="plan-error">
                  <sl-icon slot="icon" name="exclamation-octagon"></sl-icon>
                  ${this._error}
                </sl-alert>`
              : nothing
          }
          ${
            this._notice
              ? html`<sl-alert variant="primary" open data-testid="plan-notice">
                  <sl-icon slot="icon" name="info-circle"></sl-icon>
                  ${this._notice}
                </sl-alert>`
              : nothing
          }
          ${
            this._billingEnabled
              ? html`
                  <billing-subscription-details
                    .summary=${this._summary}
                    .plans=${this._options?.plans ?? []}
                    .canManageBilling=${this._options?.can_manage_billing === true}
                  ></billing-subscription-details>
                  ${this._renderCards()}
                  <!--
                    The section confirms the change; the cards above still
                    show the plan the account held before it. Without this
                    binding the event never reaches the page (it is retargeted
                    to this host before it reaches window, where the listener
                    ignores it), and the cards kept a stale "Your plan" mark
                    and a stale renewal date until a manual reload.
                  -->
                  <billing-plan-comparison
                    @billing-subscription-changed=${() =>
                      void this._reloadOptions()}
                  ></billing-plan-comparison>
                  <pricing-plan-comparison
                    .comparison=${this._comparison}
                    .plans=${this._plans}
                    .fallbackTitle=${this._comparisonTitle}
                    .currentPlanId=${this._currentPlanId()}
                  ></pricing-plan-comparison>
                `
              : html`<p data-testid="billing-unavailable">
                  This deployment does not manage subscriptions. Every feature
                  of this installation is available without a plan.
                </p>`
          }
        </div>
      </div>
    `;
  }

  static styles = [
    unsafeCSS(consoleStyles),
    unsafeCSS(pricingStyles),
    pricingPlansStyles,
    css`
      :host {
        display: block;
      }

      .loading {
        display: flex;
        justify-content: center;
        padding: 3rem 0;
      }

      .current-plan {
        margin: 0 0 1rem 0;
        color: var(--sl-color-text-secondary);
      }

      /* The period pill sits above the card row, right aligned on a laptop
         and centred where a right edge means nothing. */
      .period-row {
        display: flex;
        align-items: center;
        justify-content: flex-end;
        min-height: 3rem;
        margin: 0 0 0.5rem 0;
      }

      @media (max-width: 640px) {
        .period-row {
          justify-content: center;
        }
      }

      /* The plan the reader arrived asking about, from the upgrade dialog. */
      pricing-card.requested {
        outline: 2px solid var(--sl-color-primary-600);
        outline-offset: 4px;
        border-radius: 16px;
      }

      billing-plan-comparison {
        display: block;
        margin-top: 2.5rem;
      }

      sl-alert {
        margin-bottom: 1.5rem;
      }
    `,
  ];
}

declare global {
  interface HTMLElementTagNameMap {
    'plan-view': PlanView;
  }
}
