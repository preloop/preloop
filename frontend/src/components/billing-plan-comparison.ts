import {
  formatCurrencyAmount,
  formatCurrencyAmountExact,
  formatCurrencyCents,
  formatCurrencyCentsExact,
  formatUsdExact,
} from '../utils/money';
import { parseUTCDate } from '../utils/date';
import { LitElement, css, html, nothing } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import { fetchWithAuth } from '../api';
import { cheapestPlanUnlocking } from '../utils/premium-features';
import type {
  BillingMonth,
  BillingNotice,
  BillingPlan,
  PlanAssessment,
  PlanChangeOptions,
  PlanChangePreview,
  PlanChangeResult,
  PlanEligibility,
} from '../types/billing';

const CAPABILITIES: Record<string, string> = {
  ai_optimization: 'Built-in model optimization',
  value_reviews: 'Value reviews',
  rbac: 'Role-based access control',
  team_approvals: 'Team approval workflows',
  price_overrides: 'Model price overrides',
  reconciliation: 'Provider billing reconciliation',
};
const COVERAGE_REASONS: Record<string, string> = {
  historical_collection_not_proven:
    'Complete historical collection has not been established.',
  account_created_during_period: 'The account was created during this month.',
  account_created_after_period: 'The account did not exist during this month.',
  retention_gap: 'Some records are outside the available history.',
  current_partial_month: 'This month is still in progress.',
  current_partial_month_not_projected:
    'This month is still in progress; totals are not projected.',
  before_account_creation: 'The account did not exist during this month.',
  unpriced_hosted_usage: 'Some built-in model requests have no verified cost.',
};

const SWITCHING_DISABLED = html`Plan changes from the console are not available
  yet. Manage in Stripe or
  <a href="mailto:sales@preloop.ai">contact support</a>.`;

/**
 * Authenticated plan selection. Loading this component never changes a
 * subscription.
 *
 * The section is progressive: collapsed it states the plan you are on and
 * offers one action. Everything else (the plan picker, the limits table, the
 * recorded usage table, the provider warnings) appears only once the reader
 * asks for it. A settings page that opens with two warnings and four tables
 * tells a reader who came to check their plan nothing about their plan.
 */
@customElement('billing-plan-comparison')
export class BillingPlanComparison extends LitElement {
  @state() private options: PlanChangeOptions | null = null;
  @state() private loading = true;
  /** Step 2: the reader asked to change plan. */
  @state() private changing = false;
  @state() private showComparison = false;
  @state() private showUsage = false;
  @state() private busy: 'preview' | 'confirm' | 'checkout' | null = null;
  @state() private error = '';
  @state() private selectedPlan = '';
  /**
   * Annual by default, like the public pricing page.
   *
   * Both surfaces sell the same catalog, so opening one on "Monthly" and the
   * other on "Annually" made the same plan look like two prices. `?interval=`
   * still wins, and a target with no period at all (Free) is normalised back
   * to month in `refresh`, where the period question does not arise.
   */
  @state() private interval: 'month' | 'year' = 'year';
  @state() private preview: PlanChangePreview | null = null;
  @state() private accepted = false;
  @state() private now = Date.now();
  @state() private result: PlanChangeResult | null = null;
  @state() private refreshRequired = false;
  @state() private pendingConfirmation: string | null = null;
  @state() private operatorRecovery = false;
  private recoveryKey: string | null = null;
  /** A plan asked for from outside while the options were still loading. */
  private pendingSelection: {
    plan: string;
    interval: 'month' | 'year';
  } | null = null;
  private revision = 0;
  private expiryTimer?: number;

  connectedCallback(): void {
    super.connectedCallback();
    void this.refresh();
  }

  /**
   * Open the picker on one named plan: the plan page's card buttons.
   *
   * The cards state the offer and nothing else. The price, the consequences
   * and the confirmation stay here, in the one place that holds a server
   * quote, so a card click selects a plan rather than starting a change.
   * Calling it before the options have loaded is harmless: the plan is kept
   * and `refresh` discards it only if the account cannot move there.
   */
  startChange(planId: string, interval?: 'month' | 'year'): void {
    this.changing = true;
    const wanted = interval ?? this.interval;
    if (this.loading) {
      // The options are still arriving. Selecting now would cancel that load
      // in flight, so record the request and let `refresh` apply it once it
      // knows whether this account can move to that plan at all.
      this.pendingSelection = { plan: planId, interval: wanted };
      return;
    }
    this.choose(planId, wanted);
  }

  disconnectedCallback(): void {
    super.disconnectedCallback();
    this.revision++;
    window.clearTimeout(this.expiryTimer);
  }

  private isLegacy(plan: BillingPlan | null | undefined): boolean {
    return plan?.is_legacy ?? plan?.legacy ?? false;
  }

  /** The server's verdict on one plan, absent on an older server. */
  private verdict(planId: string): PlanEligibility | undefined {
    return this.options?.plan_eligibility?.find((e) => e.plan_id === planId);
  }

  /**
   * Whether a plan can be a checkout or switch target at all.
   *
   * A quote-only plan (Enterprise: no self-serve price, ``purchasable``
   * false) is not one. It used to sit in the picker beside Pro and Team, so
   * choosing it replaced the price and the action with a sales paragraph: a
   * dead end dressed as a choice. It is now named outside the picker, with
   * the contact link the server supplies. The account's own plan stays in the
   * list even when it is quote-only, because a reader has to see what they
   * are on.
   */
  private isTarget(plan: BillingPlan): boolean {
    if (this.isLegacy(plan)) return false;
    const verdict = this.verdict(plan.id);
    if (verdict) return verdict.purchasable || verdict.is_current;
    return (
      plan.purchasable !== false || plan.id === this.options?.current_plan?.id
    );
  }

  /** Candidate plans, in catalog order, quote-only entries removed. */
  private get targets(): BillingPlan[] {
    return (this.options?.plans ?? []).filter((p) => this.isTarget(p));
  }

  /**
   * What "Change to" may offer: the candidates minus the plan the account is
   * already entitled to.
   *
   * Listing the current plan in a list of changes offers a change that is not
   * one, and it was the first option in the list, so a Free account opened the
   * picker on "Free". The plan stays visible either way: the line above the
   * picker names it, and the comparison table keeps its column. The one
   * exception is a paid subscription that can move between monthly and annual
   * billing on the same plan: that is a real change with a real quote.
   */
  private get changeTargets(): BillingPlan[] {
    return this.targets.filter((p) => this.isChangeTarget(p));
  }

  /** Whether choosing `plan` in the picker would change anything at all. */
  private isChangeTarget(
    plan: BillingPlan,
    options: PlanChangeOptions | null = this.options
  ): boolean {
    if (!options) return false;
    if (plan.id !== this.effectiveCurrentPlan(options)?.id) return true;
    return this.intervalSwitchable(plan, options);
  }

  /**
   * Whether the account could bill the same plan on the other period.
   *
   * Only a live paid subscription has a period to move. Free has no period
   * and an expired trial has no subscription to re-time, so for those the
   * current plan is not a change target at all.
   */
  private intervalSwitchable(
    plan: BillingPlan,
    options: PlanChangeOptions
  ): boolean {
    if (!options.current_subscription || this.trialExpired(options))
      return false;
    return (
      plan.price_monthly != null &&
      plan.price_annually != null &&
      (plan.price_monthly > 0 || plan.price_annually > 0)
    );
  }

  /** Quote-only plans, named with their contact link instead of a price. */
  private get contactPlans(): BillingPlan[] {
    return (this.options?.plans ?? []).filter(
      (p) => !this.isLegacy(p) && !this.isTarget(p)
    );
  }

  private eligible(plan: BillingPlan | null | undefined): boolean {
    if (!plan) return false;
    return this.verdict(plan.id)?.eligible !== false;
  }

  /**
   * Whether the target has a billing period to choose.
   *
   * Free is priced zero monthly and zero annually, so asking a reader to pick
   * between "Monthly" and "Annually" for it offers a choice between two
   * identical nothings, and the confirmation that followed quoted a period
   * that does not exist.
   */
  private get showPeriod(): boolean {
    const target = this.target;
    if (!target) return false;
    const verdict = this.verdict(target.id);
    if (verdict) return verdict.requires_period;
    return !!target.price_monthly || !!target.price_annually;
  }

  private permissionChanged(allowed: boolean): void {
    this.dispatchEvent(
      new CustomEvent('billing-permission-changed', {
        detail: { canManageBilling: allowed },
        bubbles: true,
        composed: true,
      })
    );
  }

  /**
   * "Refresh subscription status" must actually re-read the provider.
   *
   * Plain reloads of this component only re-fetched our own database row, so
   * a subscription whose state changed at Stripe without a delivered webhook
   * (a finished trial, most visibly) looked identical after every click. The
   * explicit button now asks the server to reconcile with the provider first;
   * that request is permission-checked and rate limited server side, so a
   * user holding the button down cannot turn it into a provider hammer.
   * Automatic loads stay read-only.
   */
  private refreshFromProvider(): void {
    void this.refresh({ reconcile: true });
  }

  async refresh(intent?: { reconcile?: boolean }): Promise<void> {
    if (this.busy === 'confirm' || this.busy === 'checkout') return;
    const revision = ++this.revision;
    this.loading = true;
    this.preview = null;
    window.clearTimeout(this.expiryTimer);
    this.accepted = false;
    this.error = '';
    this.permissionChanged(false);
    try {
      const response = await fetchWithAuth(
        intent?.reconcile
          ? '/api/v1/billing/plan-change-options?reconcile=true'
          : '/api/v1/billing/plan-change-options',
        { cache: 'no-store' }
      );
      if (!response.ok) throw new Error(await this.responseError(response));
      const options = (await response.json()) as PlanChangeOptions;
      if (revision !== this.revision || !this.isConnected) return;
      const firstLoad = !this.options;
      this.options = options;
      if (firstLoad && options.current_subscription) {
        try {
          const raw = localStorage.getItem('accessToken')?.split('.')[1];
          const actor = raw
            ? JSON.parse(atob(raw.replace(/-/g, '+').replace(/_/g, '/'))).sub
            : null;
          if (typeof actor === 'string') {
            this.recoveryKey = `preloop:billing-change:${actor}:${options.current_subscription.id}`;
            const saved = JSON.parse(
              sessionStorage.getItem(this.recoveryKey) || 'null'
            );
            if (typeof saved?.preview_id === 'string') {
              this.pendingConfirmation = saved.preview_id;
              this.operatorRecovery = saved.operator_recovery === true;
            }
          }
        } catch {
          /* Recovery remains in memory if browser storage is unavailable. */
        }
      }
      if (firstLoad) {
        const params = new URLSearchParams(window.location.search);
        const requested = options.plans.find(
          (p) =>
            p.id === params.get('plan') &&
            this.isTarget(p) &&
            this.isChangeTarget(p, options)
        );
        const feature = params.get('feature') || '';
        if (requested) this.selectedPlan = requested.id;
        else if (feature) {
          // Arrived from the upgrade dialog. The reader was refused one named
          // thing, so open on the cheapest plan that includes it rather than
          // on the next rung of the ladder, which may not include it at all.
          const unlocking = this.cheapestUnlocking(options, feature);
          if (unlocking) this.selectedPlan = unlocking;
        }
        if (
          params.get('interval') === 'year' ||
          params.get('interval') === 'month'
        ) {
          this.interval = params.get('interval') as 'month' | 'year';
        }
        // A request that names a plan, an interval or a refused feature came
        // from somewhere that already asked the question, so answer it:
        // opening on "Change plan" would make the reader click again.
        if (requested || feature || params.get('interval'))
          this.changing = true;
      }
      this.refreshRequired = false;
      if (this.pendingSelection) {
        this.selectedPlan = this.pendingSelection.plan;
        this.interval = this.pendingSelection.interval;
        this.pendingSelection = null;
      }
      if (
        !options.plans.some(
          (p) =>
            p.id === this.selectedPlan &&
            this.isTarget(p) &&
            this.isChangeTarget(p, options)
        )
      )
        this.selectedPlan = this.defaultSelection(options);
      if (!this.showPeriod) this.interval = 'month';
      this.permissionChanged(options.can_manage_billing === true);
    } catch (error) {
      if (revision !== this.revision) return;
      this.options = null;
      this.error =
        error instanceof Error
          ? error.message
          : 'Could not load plan comparison.';
    } finally {
      if (revision === this.revision) {
        this.loading = false;
        this.busy = null;
      }
    }
  }

  private async responseError(response: Response): Promise<string> {
    const body = await response.json().catch(() => ({}));
    const detail = body.detail;
    if (typeof detail === 'string') return detail;
    if (typeof detail?.message === 'string') return detail.message;
    return response.status === 403
      ? 'Only a billing owner or account administrator can change this subscription.'
      : 'Could not verify the billing request. Refresh and try again.';
  }

  /**
   * Same fallback as account-view: a trialing row whose period has ended is
   * Free. plan-change-options still reports the subscription row until the
   * entitlement demotion lands.
   */
  private trialExpired(options: PlanChangeOptions): boolean {
    const subscription = options.current_subscription;
    if (subscription?.status !== 'trialing') return false;
    if (!subscription.current_period_end) return false;
    const date = parseUTCDate(subscription.current_period_end);
    return !Number.isNaN(date.getTime()) && date.getTime() < Date.now();
  }

  /** Entitled plan for the collapsed line and the picker ladder floor. */
  private effectiveCurrentPlan(options: PlanChangeOptions): BillingPlan | null {
    if (this.trialExpired(options)) {
      return (
        options.plans.find((p) => p.id === 'free') ?? {
          id: 'free',
          name: 'Free',
          features: {},
        }
      );
    }
    return options.current_plan;
  }

  /**
   * The plan the picker opens on: the cheapest plan above what the account is
   * entitled to today that the account can actually hold. Opening on Free
   * would offer a downgrade to someone who clicked "Change plan", and opening
   * on a plan the account is already too large for opens the flow on a
   * refusal.
   */
  private defaultSelection(options: PlanChangeOptions): string {
    const price = (plan: BillingPlan): number =>
      typeof plan.price_monthly === 'number'
        ? plan.price_monthly
        : Number.POSITIVE_INFINITY;
    const currentId = this.effectiveCurrentPlan(options)?.id;
    const targets = options.plans.filter(
      (p) => this.isTarget(p) && p.id !== currentId
    );
    // A plan the server blocked is never the default, not even as a last
    // resort: the picker has just labelled it "(not available)" and the action
    // under it is disabled, so opening on it offers a choice that cannot be
    // taken. When nothing fits, the selection stays empty and the reasons
    // under the picker are the whole screen.
    const selectable = targets.filter((p) => this.eligible(p));
    const currentPlan = this.effectiveCurrentPlan(options);
    const ladder = selectable
      .filter(
        (p) =>
          p.id !== 'free' &&
          p.id !== currentId &&
          Number.isFinite(price(p)) &&
          p.purchasable !== false
      )
      .sort((a, b) => price(a) - price(b));
    const current = this.trialExpired(options)
      ? 0
      : currentPlan
        ? price(currentPlan)
        : 0;
    return (
      ladder.find((p) => price(p) > current)?.id ??
      ladder[0]?.id ??
      selectable.find((p) => p.id !== currentId && p.id !== 'free')?.id ??
      // The server blocked every paid candidate, so Free is the only plan
      // this account can move to. Opening on it is a poor offer, but an empty
      // picker is worse: it hides the one plan that is left. With nothing
      // blocked, Free stays off the default as before.
      (targets.some((p) => !this.eligible(p))
        ? selectable.find((p) => p.id !== currentId)?.id
        : undefined) ??
      ''
    );
  }

  /**
   * The cheapest plan this account can move to that unlocks `feature`.
   *
   * `feature` is the name the 402 upgrade contract refused, mapped to a plan
   * capability by the shared selector. The rule (published capability, sold,
   * priced, not legacy) lives in `premium-features` so the plan page's card
   * highlight and this panel's preselection cannot name two different plans;
   * what stays here is what only this panel knows: which plans are targets
   * and which ones the server says this account may take. A feature no
   * eligible plan unlocks returns nothing and the caller falls back to the
   * ordinary default.
   */
  private cheapestUnlocking(
    options: PlanChangeOptions,
    feature: string
  ): string | undefined {
    const currentId = this.effectiveCurrentPlan(options)?.id;
    return (
      cheapestPlanUnlocking(
        options.plans,
        feature,
        (plan) =>
          this.isTarget(plan as BillingPlan) &&
          plan.id !== currentId &&
          this.eligible(plan as BillingPlan)
      ) || undefined
    );
  }

  /** One line of what the plan includes, for the collapsed state. */
  private tagline(plan: BillingPlan | null | undefined): string {
    const users = plan?.features?.max_users;
    const seats =
      users === -1
        ? 'Unlimited users'
        : typeof users === 'number'
          ? `${this.count(users)} ${users === 1 ? 'user' : 'users'}`
          : null;
    const priced =
      typeof plan?.features?.hosted_models_monthly_limit_usd === 'number' ||
      typeof plan?.features?.hosted_credit_one_time_usd === 'number';
    const hosted = priced
      ? `${this.feature(plan, 'hosted_models_monthly_limit_usd')} for built-in models`
      : null;
    const parts = [seats, hosted].filter((part) => part !== null);
    return parts.length
      ? `${parts.join(', ')}.`
      : 'Your included limits are shown when you compare plans.';
  }

  private choose(plan: string, interval = this.interval): void {
    if (
      this.busy === 'confirm' ||
      this.busy === 'checkout' ||
      this.pendingConfirmation
    )
      return;
    this.revision++;
    this.selectedPlan = plan;
    this.interval = interval;
    // A plan with nothing to bill has no period to choose, so the annual
    // default must not follow the reader onto it and quote a year of zero.
    if (!this.showPeriod) this.interval = 'month';
    this.preview = null;
    window.clearTimeout(this.expiryTimer);
    this.accepted = false;
    this.busy = null;
    this.error = '';
    this.result = null;
  }

  private get target(): BillingPlan | undefined {
    return this.options?.plans.find((p) => p.id === this.selectedPlan);
  }

  private get salesLed(): boolean {
    return (
      this.target?.id === 'enterprise' ||
      (this.target?.id !== 'free' && this.target?.purchasable === false) ||
      (this.target?.price_monthly === null &&
        this.target?.price_annually === null)
    );
  }

  private get canAct(): boolean {
    return (
      !!this.options?.can_manage_billing &&
      !!this.options?.switching_enabled &&
      !this.loading &&
      !this.busy &&
      !this.refreshRequired &&
      !this.pendingConfirmation &&
      !!this.target &&
      !this.salesLed &&
      // The per-plan verdict decides, with no exemption for a cancellation
      // down to Free. The console used to let a Free downgrade through on the
      // theory that the caps of the plan being left cannot trap it, but the
      // server blocks Free on the account's own member and agent counts and
      // the picker says "(not available)" about it. Two answers on one screen
      // are worse than one strict answer, so the exemption, if it is wanted,
      // belongs in ``evaluate_plan`` where the sentence the reader reads is
      // written.
      this.eligible(this.target) &&
      // The older fit assessment keeps the exemption it shipped with. It is
      // an opinion formed from observed months, not a measurement of the
      // account as it is now, and the confirm endpoint accepts a cancellation
      // to Free that the assessment calls blocked.
      (!this.assessment()?.blockers?.length ||
        (!!this.options.current_subscription &&
          this.selectedPlan === 'free')) &&
      (!this.options.current_subscription ||
        (!!this.options.current_subscription.revision &&
          Number.isFinite(
            this.options.current_subscription.total_amount_cents
          ))) &&
      !this.options?.current_subscription?.pending_change &&
      !this.options?.current_subscription?.cancel_at_period_end &&
      !(
        this.options?.current_subscription?.plan_id === this.selectedPlan &&
        this.options.current_subscription.interval === this.interval
      )
    );
  }

  private get expired(): boolean {
    const expires = Date.parse(this.preview?.expires_at ?? '');
    return (
      !Number.isFinite(expires) || expires <= Math.max(this.now, Date.now())
    );
  }

  private get validQuote(): boolean {
    const p = this.preview;
    return (
      !!p &&
      p.target.plan_id === this.selectedPlan &&
      p.target.interval === this.interval &&
      Number.isFinite(Date.parse(p.effective_at)) &&
      Number.isFinite(p.amount_due_now_cents) &&
      (p.proration_amount_cents === null ||
        Number.isFinite(p.proration_amount_cents)) &&
      Number.isFinite(p.target.total_amount_cents) &&
      Number.isFinite(p.current.total_amount_cents) &&
      p.amount_due_now_cents! >= 0 &&
      p.target.total_amount_cents! >= 0 &&
      /^[a-z]{3}$/i.test(p.currency) &&
      p.target.currency?.toLowerCase() === p.currency.toLowerCase() &&
      p.current.currency?.toLowerCase() === p.currency.toLowerCase() &&
      !!p.preview_id &&
      (p.timing === 'immediate' || p.timing === 'period_end')
    );
  }

  private async requestPreview(): Promise<void> {
    if (!this.canAct || !this.options?.current_subscription) return;
    const revision = ++this.revision;
    this.busy = 'preview';
    this.preview = null;
    this.accepted = false;
    this.error = '';
    this.result = null;
    try {
      const response = await fetchWithAuth(
        '/api/v1/billing/plan-change-preview',
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            target_plan_id: this.selectedPlan,
            interval: this.interval,
          }),
        }
      );
      if (!response.ok) throw new Error(await this.responseError(response));
      const preview = (await response.json()) as PlanChangePreview;
      if (revision !== this.revision || !this.isConnected) return;
      this.preview = preview;
      this.now = Date.now();
      window.clearTimeout(this.expiryTimer);
      const delay = Date.parse(preview.expires_at) - this.now;
      if (Number.isFinite(delay) && delay > 0) {
        this.expiryTimer = window.setTimeout(
          () => {
            this.now = Date.now();
          },
          Math.min(delay + 10, 2147483647)
        );
      }
    } catch (error) {
      if (revision === this.revision)
        this.error =
          error instanceof Error ? error.message : 'Preview unavailable.';
    } finally {
      if (revision === this.revision) this.busy = null;
    }
  }

  private async confirm(): Promise<void> {
    if (
      !this.canAct ||
      !this.preview ||
      this.expired ||
      !this.validQuote ||
      !this.accepted ||
      this.preview.blockers.length
    )
      return;
    this.pendingConfirmation = this.preview.preview_id;
    this.persistRecovery();
    await this.sendConfirmation();
  }

  private persistRecovery(): void {
    if (!this.recoveryKey) return;
    try {
      if (this.pendingConfirmation)
        sessionStorage.setItem(
          this.recoveryKey,
          JSON.stringify({
            preview_id: this.pendingConfirmation,
            operator_recovery: this.operatorRecovery,
          })
        );
      else sessionStorage.removeItem(this.recoveryKey);
    } catch {
      /* The current page still retains the original command. */
    }
  }

  private async retryConfirmation(): Promise<void> {
    if (
      !this.pendingConfirmation ||
      this.busy ||
      !this.options?.can_manage_billing ||
      this.operatorRecovery
    )
      return;
    await this.sendConfirmation();
  }

  private async sendConfirmation(): Promise<void> {
    const previewId = this.pendingConfirmation!;
    this.busy = 'confirm';
    this.error = '';
    try {
      const response = await fetchWithAuth(
        '/api/v1/billing/plan-change-confirm',
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ preview_id: previewId }),
        }
      );
      if (!response.ok) {
        const body = await response
          .clone()
          .json()
          .catch(() => ({}));
        const code = body.detail?.code;
        if (body.detail?.operation_started === false)
          this.pendingConfirmation = null;
        this.operatorRecovery = code === 'recovery_required';
        throw new Error(await this.responseError(response));
      }
      const result = (await response.json()) as PlanChangeResult;
      if (
        !['scheduled', 'applied'].includes(result.status) ||
        !result.operation_id
      )
        throw new Error('The change outcome could not be verified.');
      this.result = result;
      this.pendingConfirmation = null;
      this.operatorRecovery = false;
      this.dispatchEvent(
        new CustomEvent('billing-subscription-changed', {
          bubbles: true,
          composed: true,
        })
      );
    } catch (error) {
      this.error =
        error instanceof Error ? error.message : 'Could not verify the change.';
      if (!this.pendingConfirmation)
        this.error +=
          ' Refresh subscription status before requesting another preview.';
    } finally {
      this.persistRecovery();
      this.refreshRequired = true;
      this.preview = null;
      this.accepted = false;
      this.busy = null;
    }
    if (this.result && !this.error) await this.refresh();
  }

  private navigate(url: string): void {
    window.location.href = url;
  }

  private async checkout(): Promise<void> {
    if (
      !this.canAct ||
      this.options?.current_subscription ||
      this.target?.id === 'free'
    )
      return;
    this.busy = 'checkout';
    this.error = '';
    try {
      const response = await fetchWithAuth(
        '/api/v1/billing/create-checkout-session',
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            plan_id: this.selectedPlan,
            interval: this.interval,
          }),
        }
      );
      if (!response.ok) throw new Error(await this.responseError(response));
      const result = await response.json();
      if (result.action !== 'redirect' || typeof result.url !== 'string') {
        throw new Error(
          'Checkout is not available. Refresh subscription status.'
        );
      }
      this.navigate(result.url);
    } catch (error) {
      this.error =
        error instanceof Error ? error.message : 'Could not open checkout.';
    } finally {
      this.busy = null;
    }
  }

  private count(value: number | null | undefined): string {
    if (value == null || !Number.isFinite(value)) return 'Unknown';
    return new Intl.NumberFormat(undefined).format(value);
  }
  private date(value: string): string {
    const date = parseUTCDate(value);
    return Number.isNaN(date.getTime())
      ? 'Unknown'
      : date.toLocaleString(undefined, {
          dateStyle: 'medium',
          timeStyle: 'short',
        });
  }
  private month(value: string): string {
    const date = parseUTCDate(value);
    return Number.isNaN(date.getTime())
      ? 'Unknown month'
      : date.toLocaleDateString(undefined, {
          month: 'long',
          year: 'numeric',
          timeZone: 'UTC',
        });
  }
  private notice(value: BillingNotice | string): string {
    return typeof value === 'string' ? value : value.message;
  }
  private feature(plan: BillingPlan | null | undefined, key: string): string {
    if (
      key === 'hosted_models_monthly_limit_usd' &&
      plan?.features?.hosted_credit_one_time_usd != null
    ) {
      const credit = plan.features.hosted_credit_one_time_usd;
      return typeof credit === 'number'
        ? `${formatCurrencyCents(credit * 100, 'USD')} one-time credit`
        : 'One-time credit';
    }
    const value = plan?.features?.[key];
    if (value === undefined || value === null) return 'Not specified';
    if (value === -1) return key === 'retention_days' ? 'Custom' : 'Unlimited';
    if (typeof value !== 'number') return String(value);
    if (key === 'hosted_models_monthly_limit_usd')
      return `${formatCurrencyCents(value * 100, 'USD')} / month`;
    if (key === 'byok_ingest_tokens_monthly')
      return `${this.count(value)} tokens / month`;
    if (key === 'retention_days')
      return value === 183
        ? '6 months (183 days)'
        : value === 365
          ? '1 year'
          : value === 730
            ? '2 years'
            : `${this.count(value)} days`;
    return this.count(value);
  }
  private assessment(): PlanAssessment | undefined {
    return this.options?.assessments.find(
      (a) => a.plan_id === this.selectedPlan
    );
  }
  /**
   * Step 3: what choosing this plan would actually do, and nothing else.
   *
   * There used to be a summary line on every plan. When it could not prove a
   * fit it said "Not enough evidence to confirm a fit", which is a statement
   * about our records rather than about the reader's account, and it appeared
   * for every account whose three month history was not complete, which is
   * most of them. A reader cannot act on it. It is gone. What is left is the
   * server's list: a refusal with both numbers, a consequence with its date,
   * or nothing at all.
   */
  private renderConsequences() {
    const verdict = this.verdict(this.selectedPlan);
    const observed = (this.assessment()?.advisories ?? []).map((n) =>
      this.notice(n)
    );
    const blockers = (verdict?.blockers ?? []).map((b) => b.message);
    const warnings = [
      ...(verdict?.warnings ?? []).map((w) => w.message),
      ...observed,
    ];
    const benefit = verdict?.retention.benefit_message;
    const protectedNote = verdict?.retention.protected_by_floor
      ? verdict.retention.message
      : null;
    return html`${
        blockers.length
          ? html`<ul class="warning" data-testid="plan-blockers">
              ${blockers.map((message) => html`<li>${message}</li>`)}
            </ul>`
          : nothing
      }
      ${
        warnings.length
          ? html`<ul class="warning" data-testid="plan-warnings">
              ${warnings.map((message) => html`<li>${message}</li>`)}
            </ul>`
          : nothing
      }
      ${benefit ? html`<p data-testid="plan-benefit">${benefit}</p>` : nothing}
      ${protectedNote ? html`<p data-testid="retention-protected">${protectedNote}</p>` : nothing}
      <button
        class="link"
        type="button"
        data-testid="show-usage"
        aria-expanded=${this.showUsage ? 'true' : 'false'}
        @click=${() => {
          this.showUsage = !this.showUsage;
        }}
      >
        ${this.showUsage ? 'Hide recorded usage' : 'Show recorded usage'}
      </button>
      ${this.showUsage ? this.renderHistory() : nothing}`;
  }

  private renderHistory() {
    const o = this.options!;
    const assessment = this.assessment();
    return html`
      ${
        o.hosted_credit
          ? html`
              <h3>Current built-in model balance</h3>
              <p>
                ${o.hosted_credit.coverage === 'unknown' ? 'Your historical hosted balance has not been verified. An unavailable balance does not mean fresh credit.' : html`Available ${o.hosted_credit.one_time_credit_usd != null ? 'one-time credit' : 'this UTC calendar month'}: ${formatCurrencyCents((o.hosted_credit.one_time_credit_usd != null ? o.hosted_credit.remaining_credit_usd : o.hosted_credit.month_remaining_usd) == null ? null : (o.hosted_credit.one_time_credit_usd != null ? o.hosted_credit.remaining_credit_usd! : o.hosted_credit.month_remaining_usd!) * 100, 'USD')}.`}
              </p>
              ${(o.hosted_credit.lifetime_reserved_usd ?? 0) > 0 ? html`<p>${html`<span title=${formatCurrencyCentsExact(o.hosted_credit.lifetime_reserved_usd! * 100, 'USD')}>${formatCurrencyCents(o.hosted_credit.lifetime_reserved_usd! * 100, 'USD')}</span>`} is reserved for calls in progress or awaiting verified provider charges. Reserved credit is not available for another call; it is released or settled when the charge is confirmed.</p>` : nothing}
              ${o.hosted_credit.extra_spending_enabled === false ? html`<p>Extra spending is off. When the included balance is exhausted, built-in model calls stop. Calls using your own provider keys continue.</p>` : nothing}
            `
          : nothing
      }
      <h3>Would this plan cover your usage?</h3>
      <p>
        The previous three completed calendar months are shown separately from
        the current partial month. Missing records are not zero usage.
        Observations do not predict future usage.
      </p>
      <div
        class="table-scroll"
        tabindex="0"
        role="region"
        aria-label="Monthly usage comparison"
      >
        <table>
          <caption>
            Recorded usage and selected plan limits. Months use UTC.
          </caption>
          <thead>
            <tr>
              <th scope="col">Month</th>
              <th scope="col">Available history</th>
              <th scope="col">BYOK analysis tokens</th>
              <th scope="col">Built-in model cost</th>
            </tr>
          </thead>
          <tbody>
            ${o.monthly_usage.map((m) => this.renderMonth(m))}
          </tbody>
        </table>
      </div>
      <p>
        Selected plan:
        ${this.feature(this.target, 'byok_ingest_tokens_monthly')} for BYOK
        analysis;
        ${this.feature(this.target, 'hosted_models_monthly_limit_usd')} for
        built-in models. Provider charges on your own keys are separate and are
        never marked up by Preloop.
      </p>
      ${this.target?.features.hosted_credit_one_time_usd != null ? html`<p>The Free hosted credit is granted once across the account lifetime. It does not reset each month or when you change plans. Remaining lifetime credit: ${html`<span title=${formatCurrencyCentsExact(o.hosted_credit?.remaining_credit_usd == null ? null : o.hosted_credit.remaining_credit_usd * 100, 'USD')}>${formatCurrencyCents(o.hosted_credit?.remaining_credit_usd == null ? null : o.hosted_credit.remaining_credit_usd * 100, 'USD')}</span>`}.</p>` : nothing}
      <p>
        Current users:
        <strong>${this.count(o.current_usage.active_users)}</strong>; pending
        invitations:
        <strong>${this.count(o.current_usage.pending_invitations)}</strong>;
        current agents:
        <strong>${this.count(o.current_usage.active_agents)}</strong>.
      </p>
      ${
        assessment?.blockers?.length
          ? html`<ul class="warning">
              ${assessment.blockers.map((n) => html`<li>${this.notice(n)}</li>`)}
            </ul>`
          : nothing
      }
      ${
        assessment?.advisories?.length
          ? html`<ul>
              ${assessment.advisories.map((n) => html`<li>${this.notice(n)}</li>`)}
            </ul>`
          : nothing
      }
    `;
  }
  private observedLimit(m: BillingMonth, hosted: boolean): string {
    if (hosted && this.target?.features.hosted_credit_one_time_usd != null)
      return 'Lifetime credit, not a monthly allowance';
    const observed = hosted
      ? m.observed_hosted_cost_usd
      : m.observed_byok_tokens;
    const limit =
      this.target?.features[
        hosted
          ? 'hosted_models_monthly_limit_usd'
          : 'byok_ingest_tokens_monthly'
      ];
    if (limit === -1) return 'No plan quota';
    if (typeof limit !== 'number' || observed == null) return 'Cannot assess';
    if (observed > limit)
      return hosted ? 'Above included allowance' : 'Above selected quota';
    return m.coverage === 'complete'
      ? 'Within observed limit'
      : 'Cannot confirm full-month fit';
  }
  private renderMonth(m: BillingMonth) {
    const reason = (m.coverage_reasons ?? [])
      .map(
        (r) =>
          COVERAGE_REASONS[r] ??
          'Complete coverage is not established for this month.'
      )
      .join(' ');
    return html`<tr>
      <th scope="row">
        ${this.month(m.period_start)}${m.is_partial ? html`<br /><span class="muted">Current partial month</span>` : nothing}
      </th>
      <td>
        ${m.coverage === 'complete' ? 'Complete' : m.coverage === 'partial' ? 'Incomplete' : m.coverage === 'not_applicable' ? 'Account did not exist' : 'Unknown'}${reason ? html`<br /><small>${reason}</small>` : nothing}
      </td>
      <td>
        ${this.count(m.observed_byok_tokens)}<br /><small
          >${this.observedLimit(m, false)}</small
        >${m.coverage !== 'complete' && m.observed_byok_tokens != null ? html`<br /><small>Observed only</small>` : nothing}
      </td>
      <td>
        ${html`<span title=${formatCurrencyCentsExact(m.observed_hosted_cost_usd == null ? null : m.observed_hosted_cost_usd * 100, 'USD')}>${formatCurrencyCents(m.observed_hosted_cost_usd == null ? null : m.observed_hosted_cost_usd * 100, 'USD')}</span>`}<br /><small
          >${this.observedLimit(m, true)}</small
        >${m.coverage !== 'complete' && m.observed_hosted_cost_usd != null ? html`<br /><small>Observed only</small>` : nothing}
      </td>
    </tr>`;
  }
  private renderLimits() {
    const current = this.options!.current_plan;
    const target = this.target;
    const rows = [
      ['max_users', 'Users'],
      ['max_agents', 'Agents'],
      ['byok_ingest_tokens_monthly', 'BYOK analysis'],
      ['hosted_models_monthly_limit_usd', 'Built-in model allowance'],
      ['retention_days', 'Analytics history'],
    ];
    const capabilities = Object.keys(CAPABILITIES).filter(
      (key) =>
        current?.capabilities?.includes(key) ||
        target?.capabilities?.includes(key)
    );
    return html`<div
        class="table-scroll"
        tabindex="0"
        role="region"
        aria-label="Changed plan limits"
      >
        <table>
          <caption>
            What changes
          </caption>
          <thead>
            <tr>
              <th scope="col">Included</th>
              <th scope="col">${current?.name ?? 'Current plan'}</th>
              <th scope="col">${target?.name ?? 'Selected plan'}</th>
            </tr>
          </thead>
          <tbody>
            ${rows.map(
              ([key, label]) =>
                html`<tr>
                  <th scope="row">${label}</th>
                  <td>${this.feature(current, key)}</td>
                  <td>${this.feature(target, key)}</td>
                </tr>`
            )}
            ${capabilities.map(
              (key) =>
                html`<tr>
                  <th scope="row">${CAPABILITIES[key]}</th>
                  <td>
                    ${current?.capabilities?.includes(key) ? 'Included' : 'Not included'}
                  </td>
                  <td>
                    ${target?.capabilities?.includes(key) ? 'Included' : 'Not included'}
                  </td>
                </tr>`
            )}
          </tbody>
        </table>
      </div>
      ${target?.seat_addon ? html`<p>Extra users: ${html`<span title=${formatCurrencyCentsExact(target.seat_addon.price_per_user_monthly * 100, 'USD')}>${formatCurrencyCents(target.seat_addon.price_per_user_monthly * 100, 'USD')}</span>`} per user monthly, or ${html`<span title=${formatCurrencyCentsExact(target.seat_addon.price_per_user_annually * 100, 'USD')}>${formatCurrencyCents(target.seat_addon.price_per_user_annually * 100, 'USD')}</span>`} annually, up to ${target.seat_addon.max_users} users. The quote includes any required extra users.</p>` : nothing}
      <p>
        Exhausting a BYOK analysis quota reduces analytics detail. The gateway,
        firewall, approvals and budgets continue enforcing your policies.
        Built-in model spend limits are separate.
      </p>
      <p>
        Analytics history controls access and storage for usage and
        runtime-session analytics. Older analytics are periodically removed;
        longer grandfathered commitments remain protected. Audit and evidence
        retention is managed
        separately.${this.options!.storage_retention?.minimum_days != null ? html` Stored audit and evidence records have a minimum retention of ${this.options!.storage_retention.minimum_days} days under your account policy.` : nothing}${this.options!.storage_retention?.legal_holds_override ? ' Legal holds can retain records longer.' : ''}
      </p>`;
  }
  private renderPreview() {
    const p = this.preview;
    if (!p) return nothing;
    return html`<section
      class="preview"
      aria-label="Confirm plan change"
      aria-live="polite"
    >
      <h3>Review before changing your subscription</h3>
      <dl>
        <dt>Current recurring subtotal</dt>
        <dd>
          ${p.current.name}:
          ${html`<span title=${formatCurrencyCentsExact(p.current.total_amount_cents, p.current.currency)}>${formatCurrencyCents(p.current.total_amount_cents, p.current.currency)}</span>`}
          /
          ${p.current.interval}${p.current.quantity > 1 ? ` (${p.current.quantity} users)` : ''}
        </dd>
        <dt>New recurring subtotal</dt>
        <dd>
          ${p.target.name}:
          ${html`<span title=${formatCurrencyCentsExact(p.target.total_amount_cents, p.target.currency)}>${formatCurrencyCents(p.target.total_amount_cents, p.target.currency)}</span>`}
          /
          ${p.target.interval}${p.target.addon_quantity ? ` (includes ${p.target.addon_quantity} extra users)` : ''}
        </dd>
        <dt>Effective date</dt>
        <dd>
          ${this.date(p.effective_at)}
          (${p.timing === 'period_end' ? 'at the end of your current billing period' : 'immediately after confirmation'})
        </dd>
        <dt>Proration</dt>
        <dd>
          ${p.proration_amount_cents === null ? 'Not separately itemized; included in the verified amount due now.' : html`<span title=${formatCurrencyCentsExact(p.proration_amount_cents, p.currency)}>${formatCurrencyCents(p.proration_amount_cents, p.currency)}</span>`}
        </dd>
        <dt>Due now</dt>
        <dd>
          ${html`<span title=${formatCurrencyCentsExact(p.amount_due_now_cents, p.currency)}>${formatCurrencyCents(p.amount_due_now_cents, p.currency)}</span>`}
        </dd>
        <dt>Quote expires</dt>
        <dd>${this.date(p.expires_at)}</dd>
      </dl>
      <p>
        Recurring subtotals exclude discounts and tax. Due now is the amount
        returned in the billing preview.
      </p>
      <p>
        Your current subscription continues until the effective date. Confirming
        a change does not cancel your account or require signing up again.
      </p>
      ${this.isLegacy(this.options?.current_plan) ? html`<p class="warning">You are choosing to leave Legacy Teams pricing. Returning to this withdrawn plan is not offered through self-service.</p>` : nothing}
      ${
        p.blockers.length
          ? html`<ul class="warning">
              ${p.blockers.map((n) => html`<li>${this.notice(n)}</li>`)}
            </ul>`
          : nothing
      }
      ${
        p.advisories.length
          ? html`<ul>
              ${p.advisories.map((n) => html`<li>${this.notice(n)}</li>`)}
            </ul>`
          : nothing
      }
      ${!this.validQuote ? html`<p role="alert">A complete price and effective date could not be verified. Request a new preview.</p>` : nothing}
      ${this.expired ? html`<p role="alert">This preview has expired. Request a new preview before confirming.</p>` : nothing}
      <label class="consent"
        ><input
          type="checkbox"
          data-testid="consent"
          .checked=${this.accepted}
          ?disabled=${!!this.busy || this.expired || !this.validQuote || !!p.blockers.length}
          @change=${(e: Event) => {
            this.accepted = (e.target as HTMLInputElement).checked;
          }}
        />
        I have reviewed the price, changed limits, available usage history and
        effective date, and I want to make this change.</label
      >
      <button
        data-testid="confirm"
        ?disabled=${!this.canAct || !this.accepted || this.expired || !this.validQuote || !!p.blockers.length}
        @click=${this.confirm}
      >
        ${this.busy === 'confirm' ? 'Confirming…' : p.timing === 'period_end' ? 'Confirm scheduled change' : 'Confirm plan change'}
      </button>
    </section>`;
  }

  /**
   * The current subscription in one sentence.
   *
   * Amounts print only when the provider has actually confirmed them.
   * "Unavailable per user, Unknown users" is not a price; it is placeholder
   * text where a price should be, and it reads as a billing fault.
   */
  private renderCurrentSubscription() {
    const o = this.options!;
    const subscription = o.current_subscription;
    if (!subscription)
      return html`<p>
        You are comparing cloud plans. The free open-source self-hosted edition
        has its own terms and is not subject to these cloud plan limits.
      </p>`;
    if (
      !Number.isFinite(subscription.total_amount_cents) ||
      !subscription.interval
    )
      return html`<p data-testid="amount-unverified">
        Current subscription amount: not yet verified with the payment provider.
      </p>`;
    const legacy = this.isLegacy(o.current_plan);
    const perUser =
      legacy &&
      Number.isFinite(subscription.unit_amount_cents) &&
      Number.isFinite(subscription.quantity);
    return html`<p>
      <strong>${o.current_plan?.name ?? 'Current plan'}</strong
      >${legacy ? ' (grandfathered)' : ''}. Current recurring subtotal before
      discounts and tax:
      <strong
        >${html`<span title=${formatCurrencyCentsExact(subscription.total_amount_cents, subscription.currency)}>${formatCurrencyCents(subscription.total_amount_cents, subscription.currency)}</span>`}
        / ${subscription.interval}</strong
      >${perUser ? html` (${formatCurrencyCents(subscription.unit_amount_cents, subscription.currency)} per user, ${this.count(subscription.quantity)} users). Your grandfathered per-user rate stays until you choose to change plans.` : '.'}
    </p>`;
  }

  /** Step 1: what you are on, and the one control that opens the rest. */
  private renderCollapsed() {
    const o = this.options!;
    const blocked = !o.switching_enabled;
    const current = this.effectiveCurrentPlan(o);
    return html`
      <p data-testid="current-plan">
        <strong>${current?.name ?? 'Free'}</strong
        >${this.isLegacy(current) ? ' (grandfathered)' : ''}.
        ${this.tagline(current)}
      </p>
      <button
        data-testid="change-plan"
        type="button"
        ?disabled=${blocked || this.loading}
        @click=${() => {
          this.changing = true;
        }}
      >
        Change plan
      </button>
      ${
        blocked
          ? html`<p class="muted" data-testid="switching-disabled">
              ${SWITCHING_DISABLED}
            </p>`
          : nothing
      }
    `;
  }

  /**
   * The plans this account cannot switch to, and the arithmetic that says so.
   *
   * Disabled options carry "(not available)" in the picker, which is where a
   * reader looks, but an option's text is not the place for a sentence and a
   * disabled option cannot be opened to read one. So each refusal is repeated
   * here in full: the count, the cap, and what would have to change.
   */
  private renderUnavailable() {
    const blocked = this.changeTargets
      .filter((p) => !this.eligible(p))
      .map((p) => ({ plan: p, verdict: this.verdict(p.id)! }));
    if (!blocked.length) return nothing;
    return html`<div class="warning" data-testid="unavailable-plans">
      <p>Not available for this account:</p>
      <ul>
        ${blocked.map(
          ({ plan, verdict }) =>
            html`<li>
              <strong>${plan.name}</strong>:
              ${verdict.blockers.map((b) => b.message).join(' ')}
            </li>`
        )}
      </ul>
    </div>`;
  }

  /**
   * Where a contact link may point.
   *
   * `contact_url` reaches an `href`, and lit escapes an attribute value but
   * does not judge its scheme: `javascript:` and `data:` in an `href` execute
   * on click. The field comes from the plugin catalog today, and a catalog
   * entry is the kind of thing an operator is given to edit tomorrow, so the
   * console accepts only a same-origin path or an absolute http(s) URL and
   * falls back to the demo request page for anything else. Control characters
   * and spaces are dropped first, because a browser drops them before it
   * reads the scheme and "jav\tascript:alert(1)" would otherwise pass.
   */
  private contactHref(url: string | null | undefined): string {
    const candidate = (url ?? '').replace(/[\u0000-\u0020\u007f]/g, '');
    if (!candidate) return '/request-demo';
    // "//host" is another origin written as a path, not a path.
    if (candidate.startsWith('//')) return '/request-demo';
    if (candidate.startsWith('/')) return candidate;
    return /^https?:\/\//i.test(candidate) ? candidate : '/request-demo';
  }

  /**
   * One contact line: the server's sentence when it sends one, the console's
   * fallback when it does not, and the link, composed in one place so the two
   * callers cannot drift apart.
   */
  private renderContactLine(plan: BillingPlan, fallback: string) {
    const verdict = this.verdict(plan.id);
    const sentence = verdict?.contact_message?.trim() || fallback;
    return html`${sentence}
      <a class="contact" href=${this.contactHref(verdict?.contact_url)}
        >Contact us about ${plan.name}</a
      >`;
  }

  /** Quote-only plans: named, with the contact link, never a checkout. */
  private renderContactPlans() {
    const plans = this.contactPlans;
    if (!plans.length) return nothing;
    return html`<p data-testid="contact-plans">
      ${plans.map(
        (p) =>
          html`${this.renderContactLine(
            p,
            `${p.name} is priced per deployment.`
          )}.`
      )}
    </p>`;
  }

  /** Step 2: the picker, the price, the action, and what they imply. */
  private renderChange() {
    const o = this.options!;
    const target = this.target;
    return html`
      ${this.renderCurrentSubscription()}
      <div class="selectors">
        <label
          >Change to<select
            data-testid="plan"
            .value=${this.selectedPlan}
            ?disabled=${this.busy === 'confirm' || this.busy === 'checkout' || !!this.pendingConfirmation}
            @change=${(e: Event) => this.choose((e.target as HTMLSelectElement).value)}
          >
            ${this.changeTargets.map((p) => html`<option value=${p.id} ?selected=${p.id === this.selectedPlan} ?disabled=${!this.eligible(p)}>${p.name}${this.eligible(p) ? '' : ' (not available)'}</option>`)}
          </select></label
        >
        ${
          this.showPeriod
            ? html`<label
                >Billing period<select
                  data-testid="interval"
                  .value=${this.interval}
                  ?disabled=${this.busy === 'confirm' || this.busy === 'checkout' || !!this.pendingConfirmation}
                  @change=${(e: Event) => this.choose(this.selectedPlan, (e.target as HTMLSelectElement).value as 'month' | 'year')}
                >
                  <option value="month">Monthly</option>
                  <option value="year">Annually</option>
                </select></label
              >`
            : nothing
        }
      </div>
      ${this.renderUnavailable()}${this.renderContactPlans()}
      ${
        target
          ? html`
              ${
                this.salesLed
                  ? html`<p data-testid="sales-led">
                      ${this.renderContactLine(
                        target,
                        `${target.name} is priced per deployment and is not bought from the console.`
                      )}
                    </p>`
                  : html`
                      <p data-testid="price">
                        ${target.name}:
                        ${html`<span title=${formatCurrencyCentsExact((this.interval === 'year' ? target.price_annually : target.price_monthly) == null ? null : (this.interval === 'year' ? target.price_annually! : target.price_monthly!) * 100, 'USD')}>${formatCurrencyCents((this.interval === 'year' ? target.price_annually : target.price_monthly) == null ? null : (this.interval === 'year' ? target.price_annually! : target.price_monthly!) * 100, 'USD')}</span>`}${this.showPeriod ? html` / ${this.interval}` : nothing}.${o.current_subscription ? ' The preview shows the exact amount and effective date before anything changes.' : ' Secure checkout shows the final amount and any taxes before you subscribe.'}
                      </p>
                      ${this.renderActionNotices()}
                      ${
                        o.current_subscription
                          ? html`<button
                              data-testid="preview"
                              ?disabled=${!this.canAct}
                              @click=${this.requestPreview}
                            >
                              ${this.busy === 'preview' ? 'Preparing price preview…' : 'Preview price and effective date'}
                            </button>`
                          : html`<button
                              data-testid="checkout"
                              ?disabled=${!this.canAct || target.id === 'free'}
                              @click=${this.checkout}
                            >
                              Continue to secure checkout
                            </button>`
                      }
                    `
              }
              <button
                class="link"
                type="button"
                data-testid="show-comparison"
                aria-expanded=${this.showComparison ? 'true' : 'false'}
                @click=${() => {
                  this.showComparison = !this.showComparison;
                }}
              >
                ${this.showComparison ? 'Hide detailed comparison' : 'Show detailed comparison'}
              </button>
              ${this.showComparison ? this.renderLimits() : nothing}
              ${this.renderConsequences()}${this.renderPreview()}
            `
          : html`<p>
              No other cloud plans are available.
              <a href="/request-demo">Contact us</a> for a custom deployment.
            </p>`
      }
    `;
  }

  /** Conditions that change what the action does, shown beside the action. */
  private renderActionNotices() {
    const o = this.options!;
    return html`
      ${!o.can_manage_billing ? html`<p class="warning">Only a billing owner or account administrator can change this subscription. You can review the comparison.</p>` : nothing}
      ${
        !o.switching_enabled
          ? html`<p class="warning" data-testid="switching-disabled">
              ${SWITCHING_DISABLED}
            </p>`
          : nothing
      }
      ${o.current_subscription?.pending_change || o.current_subscription?.cancel_at_period_end ? html`<p class="warning">A subscription change is already scheduled. Review it before choosing another change.</p>` : nothing}
      ${
        o.warnings?.length
          ? html`<ul class="warning" data-testid="warnings">
              ${o.warnings.map((n) => html`<li>${this.notice(n)}</li>`)}
            </ul>`
          : nothing
      }
    `;
  }

  render() {
    const o = this.options;
    return html`<section
      aria-labelledby="compare-title"
      aria-busy=${this.loading || !!this.busy}
    >
      <div class="heading">
        <h2 id="compare-title">Cloud plan</h2>
        <button
          class="secondary"
          data-testid="refresh"
          @click=${this.refreshFromProvider}
          ?disabled=${this.loading || this.busy === 'confirm' || this.busy === 'checkout'}
        >
          Refresh subscription status
        </button>
      </div>
      ${this.error ? html`<p class="warning" role="alert">${this.error}</p>` : nothing}
      ${
        this.pendingConfirmation
          ? html`<section
              class="warning"
              aria-label="Unconfirmed billing operation"
            >
              <p>
                The outcome of your confirmed change is not yet known. Your
                original confirmation is retained; refreshing prices does not
                resolve it.
              </p>
              ${
                this.operatorRecovery
                  ? html`<p>
                      Billing reconciliation is required.
                      <a href="mailto:support@preloop.ai">Contact support</a>
                      before attempting another change.
                    </p>`
                  : html`<button
                        data-testid="recover"
                        ?disabled=${!!this.busy || !o?.can_manage_billing}
                        @click=${this.retryConfirmation}
                      >
                        Check or complete the original change
                      </button>
                      <p>
                        This sends the same confirmation again, using its
                        original operation identifier.
                      </p>`
              }
            </section>`
          : nothing
      }
      ${this.result ? html`<p class="success" role="status">${this.result.status === 'scheduled' ? 'Plan change scheduled' : 'Plan changed'}: ${this.options?.plans.find((plan) => plan.id === this.result?.plan_id)?.name || this.target?.name || 'Your plan'}, effective ${this.date(this.result.effective_at)}.</p>` : nothing}
      ${this.loading ? html`<p role="status">Loading current prices and usage coverage…</p>` : nothing}
      ${!this.loading && o ? (this.changing ? this.renderChange() : this.renderCollapsed()) : nothing}
    </section>`;
  }

  static styles = css`
    :host {
      display: block;
      color: var(--sl-color-neutral-900);
    }
    section {
      display: grid;
      gap: 1rem;
    }
    h2,
    h3,
    p {
      margin: 0;
    }
    p,
    li {
      line-height: 1.6;
    }
    h2 {
      font-size: 1.2rem;
    }
    h3 {
      font-size: 1.05rem;
    }
    .heading {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 1rem;
      flex-wrap: wrap;
    }
    .selectors {
      display: flex;
      gap: 1rem;
      flex-wrap: wrap;
    }
    label {
      display: grid;
      gap: 0.5rem;
      font-weight: 500;
    }
    select,
    button {
      font: inherit;
      min-height: 44px;
      border-radius: 0.4rem;
      padding: 0.55rem 0.8rem;
    }
    select {
      border: 1px solid var(--sl-color-neutral-300);
      color: inherit;
      background: var(--sl-color-neutral-0);
    }
    button {
      cursor: pointer;
      color: var(--sl-color-neutral-0);
      background: var(--sl-color-primary-600);
      border: 1px solid var(--sl-color-primary-600);
      justify-self: start;
    }
    button.secondary {
      background: transparent;
      color: inherit;
      border-color: var(--sl-color-neutral-300);
    }
    /* A disclosure control is a link in everything but tag name: it reveals
       text on the same page and never writes anything. */
    button.link {
      background: transparent;
      border: none;
      color: var(--sl-color-primary-700);
      text-decoration: underline;
      padding: 0.55rem 0;
      justify-self: start;
    }
    button:disabled {
      opacity: 0.55;
      cursor: not-allowed;
    }
    :focus-visible {
      outline: 2px solid var(--sl-color-primary-600);
      outline-offset: 3px;
    }
    a {
      color: var(--sl-color-primary-700);
    }
    .contact {
      padding: 0.7rem 0;
    }
    .table-scroll {
      overflow-x: auto;
      max-width: 100%;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      text-align: left;
      font-size: 0.9rem;
    }
    caption {
      text-align: left;
      font-weight: 600;
      margin-bottom: 0.6rem;
    }
    th,
    td {
      padding: 0.65rem;
      border-bottom: 1px solid var(--sl-color-neutral-200);
      vertical-align: top;
      min-width: 8rem;
    }
    small,
    .muted {
      color: var(--sl-color-neutral-600);
    }
    small {
      display: inline-block;
      max-width: 24rem;
    }
    .warning {
      padding: 0.8rem;
      background: var(--sl-color-warning-50);
      border-left: 3px solid var(--sl-color-warning-600);
    }
    ul.warning {
      padding-left: 2rem;
    }
    .success {
      padding: 0.8rem;
      background: var(--sl-color-success-50);
    }
    .fit {
      font-weight: 600;
    }
    .preview {
      border: 1px solid var(--sl-color-neutral-300);
      border-radius: 0.5rem;
      padding: 1rem;
    }
    dl {
      display: grid;
      grid-template-columns: minmax(8rem, 1fr) 2fr;
      gap: 0.6rem;
      margin: 0;
    }
    dt {
      font-weight: 600;
    }
    dd {
      margin: 0;
    }
    .consent {
      display: flex;
      align-items: flex-start;
      font-weight: 400;
      line-height: 1.6;
    }
    input[type='checkbox'] {
      width: 20px;
      height: 20px;
      margin-top: 0.2rem;
      flex-shrink: 0;
    }
    @media (max-width: 560px) {
      dl {
        grid-template-columns: 1fr;
      }
      dd {
        margin-bottom: 0.5rem;
      }
      .selectors label {
        width: 100%;
      }
    }
  `;
}
