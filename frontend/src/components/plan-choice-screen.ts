import { LitElement, html, css, unsafeCSS, nothing } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import pricingStyles from '../styles/pricing-styles.css?inline';
import './billing-toggle';
import './pricing-plans';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import {
  pricingPlansStyles,
  type Comparison,
  type PlanCta,
  type PricingPlan,
} from './pricing-plans';
import { cloudPlans, loadPricingContent } from '../utils/pricing-content';
import { recordFreePlanChoice, startCheckout, signOut } from '../api';
import { getBrandConfig, hasBrandConfig } from '../brand-config';
import './logo-component';

/**
 * Where checkout returns to, for both outcomes.
 *
 * Completing the checkout stamps the choice server-side, so the console the
 * buyer lands in has nothing left to ask. Backing out at Stripe records
 * nothing, so the same path lands back on this screen, which is the founder
 * decision: a cancelled checkout is not a choice.
 */
const RETURN_TO = '/console';

/**
 * The first-login plan choice.
 *
 * Shown once, to somebody who signed up from the plain signup page without
 * picking a plan first. It is full screen with no console chrome, no dismiss
 * and no "later", because a plan is not an optional detail of an account and
 * a dialog that can be closed is how the previous version of this ended up
 * being ignored by everybody it was written for.
 *
 * The cards are the SAME cards as the public pricing page and the console
 * plan page, from the same published content, for the same reason: a reader
 * who compared plans before signing up must not be shown a different ladder
 * afterwards.
 *
 * Nothing here decides eligibility. The shell only mounts this element after
 * the billing plugin said to, and the plugin is the only thing that can see
 * a subscription or a member's billing rights.
 */
@customElement('plan-choice-screen')
export class PlanChoiceScreen extends LitElement {
  /**
   * The configured trial length, from the same endpoint that said to show
   * this. Zero means no trial is configured, and the copy says so rather
   * than promising "0 days free".
   */
  @property({ type: Number }) trialDays = 0;
  @property({ type: String }) email = '';
  @property({ type: Boolean }) checking = false;
  private loadStarted = false;

  @state() private _loading = true;
  @state() private _plans: PricingPlan[] = [];
  @state() private _comparison: Comparison | null = null;
  @state() private _comparisonTitle = 'Compare plans';
  /** Annual by default, like the public page and the console plan page. */
  @state() private _interval: 'month' | 'year' = 'year';
  @state() private _busyPlan = '';
  @state() private _error = '';

  async connectedCallback() {
    super.connectedCallback();
    if (!this.checking) await this.startLoad();
  }

  protected updated(changed: Map<string, unknown>): void {
    if (changed.has('checking') && !this.checking) void this.startLoad();
  }

  private async startLoad(): Promise<void> {
    if (this.loadStarted) return;
    this.loadStarted = true;
    await this._load();
  }

  private identityHeader() {
    let docs = 'https://docs.preloop.ai';
    try {
      docs = getBrandConfig().docs_url || docs;
    } catch {
      /* older build */
    }
    return html`<header class="identity-header">
      ${hasBrandConfig() ? html`<logo-component></logo-component>` : html`<span>Preloop</span>`}
      <span>Signed in as ${this.email}</span>
      <button
        type="button"
        @click=${() => void signOut({ navigate: (url) => this._navigate(url), assign: (url) => this._navigate(url) })}
      >
        Sign out
      </button>
      <a href=${docs} target="_blank" rel="noopener">Help</a>
    </header>`;
  }

  private async _load(): Promise<void> {
    this._loading = true;
    try {
      const content = await loadPricingContent();
      this._plans = cloudPlans(content);
      this._comparison = content.comparison;
      if (content.comparison?.title) {
        this._comparisonTitle = content.comparison.title;
      }
    } catch {
      // The screen cannot list plans it could not read. Say so and offer the
      // one choice that needs no price list, rather than block the console
      // behind a blank page.
      this._error =
        'The plan list could not be loaded. You can start on Free and choose a paid plan later from Settings.';
    } finally {
      this._loading = false;
    }
  }

  /**
   * A plan nobody can self-serve into: both intervals are unpriced, which is
   * how the catalog says "quoted". Shape, not plan id, so a brand whose
   * sales-led tier has another name still reaches its own contact route.
   * Same rule as the public pricing page, deliberately.
   */
  private _isQuoted(plan: PricingPlan): boolean {
    return plan.price_monthly === null && plan.price_annually === null;
  }

  /** A plan with nothing to charge for. There is no checkout to open. */
  private _isFree(plan: PricingPlan): boolean {
    return (
      (plan.price_monthly ?? 0) === 0 &&
      (plan.price_annually ?? 0) === 0 &&
      !this._isQuoted(plan)
    );
  }

  /** The trial sentence, or the plain checkout one when no trial is set. */
  private get _paidNote(): string {
    return this.trialDays > 0
      ? `Free for ${this.trialDays} days. A card is required to start, and nothing is charged until the trial ends.`
      : 'Secure checkout shows the final amount and any taxes.';
  }

  private _ctaFor = (plan: PricingPlan): PlanCta | null => {
    if (this._busyPlan === plan.id) {
      return { label: 'Opening checkout', disabled: true };
    }
    if (this._isQuoted(plan)) {
      return {
        label: plan.cta_text || 'Contact us',
        note: 'Priced per deployment. We will get back to you.',
      };
    }
    if (this._isFree(plan)) {
      return {
        label: 'Start on Free',
        note: 'No card required. The Free plan does not expire.',
      };
    }
    return { label: `Choose ${plan.name}`, note: this._paidNote };
  };

  /**
   * Rebuilt per render on purpose: `_ctaFor` reads state the card row cannot
   * see (the in-flight checkout), and a stable function identity leaves Lit
   * with nothing changed to notice, so the row keeps its first answer.
   */
  private get _cardCta(): (plan: PricingPlan) => PlanCta | null {
    return (plan: PricingPlan) => this._ctaFor(plan);
  }

  private _handleCardAction = (event: CustomEvent) => {
    const planId = String(event.detail?.planId ?? '');
    const plan = this._plans.find((p) => p.id === planId);
    if (!plan || this._busyPlan) return;
    this._error = '';
    if (this._isQuoted(plan)) {
      // Contact is not a choice, so nothing is recorded: whoever comes back
      // still has a plan to pick.
      this._navigate(plan.cta_url || '/request-demo');
      return;
    }
    if (this._isFree(plan)) {
      void this._chooseFree();
      return;
    }
    void this._checkout(planId);
  };

  private async _chooseFree(): Promise<void> {
    this._busyPlan = 'free';
    try {
      await recordFreePlanChoice();
      this._done();
    } catch (error) {
      this._error =
        error instanceof Error
          ? error.message
          : 'Could not record your choice. Try again.';
    } finally {
      this._busyPlan = '';
    }
  }

  private async _checkout(planId: string): Promise<void> {
    this._busyPlan = planId;
    try {
      const outcome = await startCheckout(planId, this._interval, RETURN_TO);
      // `redirect` has already navigated away. Anything else is the server
      // explaining itself, in its own words: a retired plan, a catalog that
      // is not synced, a role that cannot buy.
      if (outcome && outcome.action !== 'redirect') {
        this._error = outcome.message;
      }
      if (outcome?.action === 'refresh') {
        // The server can see a subscription this screen cannot. The question
        // is already settled, so stop asking it.
        this._done();
      }
    } catch (error) {
      this._error =
        error instanceof Error
          ? error.message
          : 'Checkout could not be started. Try again in a moment.';
    } finally {
      this._busyPlan = '';
    }
  }

  /** Split out so a test can watch where a contact card sends the reader. */
  private _navigate(url: string): void {
    window.location.assign(url);
  }

  private _done(): void {
    this.dispatchEvent(
      new CustomEvent('plan-choice-made', { bubbles: true, composed: true })
    );
  }

  render() {
    if (this.checking || this._loading) {
      return html`
        ${this.identityHeader()}
        <div class="choice-loading" role="status">
          <sl-spinner
            label="Checking your account"
            style="font-size: 3rem;"
          ></sl-spinner>
          <p>${this.checking ? 'Checking your account…' : 'Loading plans…'}</p>
        </div>
      `;
    }
    return html`
      <div class="choice-page">
        ${this.identityHeader()}
        <header class="choice-head">
          <h1>Choose your plan</h1>
          <p class="lead">
            One step left. Pick the plan this account runs on. You can change it
            at any time from Settings.
          </p>
        </header>
        ${
          this._error
            ? html`<sl-alert
                variant="danger"
                open
                data-testid="plan-choice-error"
              >
                <sl-icon slot="icon" name="exclamation-octagon"></sl-icon>
                ${this._error}
              </sl-alert>`
            : nothing
        }
        ${
          this._plans.length
            ? html`
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
                  ></pricing-plan-cards>
                </div>
                <pricing-plan-comparison
                  .comparison=${this._comparison}
                  .plans=${this._plans}
                  .fallbackTitle=${this._comparisonTitle}
                ></pricing-plan-comparison>
              `
            : html`<sl-button
                data-testid="plan-choice-free-fallback"
                variant="primary"
                ?loading=${this._busyPlan === 'free'}
                @click=${() => void this._chooseFree()}
                >Start on Free</sl-button
              >`
        }
      </div>
    `;
  }

  static styles = [
    unsafeCSS(pricingStyles),
    pricingPlansStyles,
    css`
      .identity-header {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        gap: 1rem;
        padding: 1.5rem;
      }
      .identity-header logo-component {
        margin-right: auto;
      }
      :host {
        display: block;
        min-height: 100vh;
        background: var(--sl-color-neutral-0);
      }

      .choice-loading {
        display: flex;
        justify-content: center;
        align-items: center;
        min-height: 100vh;
      }

      .choice-page {
        max-width: 1100px;
        margin: 0 auto;
        padding: 3rem 1.5rem 4rem 1.5rem;
      }

      .choice-head {
        text-align: center;
        margin-bottom: 1.5rem;
      }

      .choice-head h1 {
        margin: 0 0 0.5rem 0;
      }

      .lead {
        margin: 0 auto;
        max-width: 46rem;
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

      sl-alert {
        margin-bottom: 1.5rem;
      }
    `,
  ];
}

declare global {
  interface HTMLElementTagNameMap {
    'plan-choice-screen': PlanChoiceScreen;
  }
}
