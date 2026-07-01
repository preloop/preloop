import { LitElement, html, css, unsafeCSS } from 'lit';
import { customElement, property } from 'lit/decorators.js';

interface Plan {
  id: string;
  name: string;
  price_monthly: number | null;
  price_annually: number | null;
  features: { [key: string]: any } | string[];
  badge?: string;
}

@customElement('pricing-card')
export class PricingCard extends LitElement {
  @property({ type: Object }) plan!: Plan;
  @property({ type: String }) interval: 'month' | 'year' = 'month';
  @property({ type: Array }) featureOrder: string[] = [];
  @property({ type: Object }) featureLabels: Record<string, string> = {};
  @property({ type: Boolean }) dark = false;

  private formatPrice(plan: Plan) {
    if (plan.id === 'enterprise') {
      return html`<div class="price-main">Custom</div>`;
    }
    const isMonthly = this.interval === 'month';
    const amount = isMonthly ? plan.price_monthly : plan.price_annually;
    const unit = isMonthly ? '/user/month' : '/user/year';

    if (amount === null) {
      return html`<div class="price-main">Custom</div>`;
    }

    if (amount === 0) {
      return html`<div class="price-main">Free</div>`;
    }

    const perMo =
      !isMonthly && typeof plan.price_annually === 'number'
        ? Math.round((plan.price_annually as number) / 12)
        : null;

    return html`
      <div class="price-main">$${amount}</div>
      <div class="unit">${unit}</div>
      ${
        !isMonthly && perMo !== null
          ? html`<div class="price-sub">~$${perMo}/mo billed annually</div>`
          : null
      }
    `;
  }

  private _formatNumber(num: number): string {
    if (num === -1) return 'Unlimited';
    if (num < 1000) return num.toString();
    return new Intl.NumberFormat('en-US', {
      notation: 'compact',
      compactDisplay: 'short',
    }).format(num);
  }

  private renderFeature(value: any, key: string) {
    const label = this.featureLabels[key] ?? key.replace(/_/g, ' ');
    let included = false;
    let displayValue: string | null = null;

    if (value === true) {
      included = true;
    } else if (value === false) {
      included = false;
    } else if (value === -1) {
      included = true;
      displayValue = 'Unlimited';
    } else if (typeof value === 'number') {
      included = true;
      displayValue = this._formatNumber(value);
    }

    return html`
      <li class=${included ? 'feature included' : 'feature excluded'}>
        <span class="feat-icon"
          >${
            included
              ? html`<sl-icon name="check-lg"></sl-icon>`
              : html`<sl-icon name="x-lg"></sl-icon>`
          }</span
        >
        <span class="feat-text">
          ${label}${
            displayValue
              ? html`<span class="feat-value">: ${displayValue}</span>`
              : ''
          }
        </span>
      </li>
    `;
  }

  private _handleSignUp() {
    this.dispatchEvent(
      new CustomEvent('signup-requested', {
        detail: { planId: this.plan.id, interval: this.interval },
        bubbles: true,
        composed: true,
      })
    );
  }

  static styles = css`
    :host {
      display: flex;
    }
    .plan-card {
      position: relative;
      display: flex;
      flex-direction: column;
      border-radius: 20px;
      padding: 1.5rem;
      background-color: var(--sl-color-neutral-100);
      width: 100%;
    }

    .plan-card.sl-theme-dark {
      background-color: #21262f; /* Dark background from landing page */
    }

    .plan-card.popular {
      border: none;
      background: linear-gradient(
        90deg,
        hsl(220, 60%, 40%),
        hsl(260, 65%, 38%)
      );
      color: white;
    }

    .badge {
      position: absolute;
      top: -15px;
      left: 50%;
      transform: translateX(-50%);
      background: linear-gradient(45deg, #a777ff, #f777ff);
      color: white;
      padding: 0.4rem 1rem;
      border-radius: 16px;
      font-size: 0.9rem;
      font-weight: 700;
      white-space: nowrap;
      z-index: 1;
    }

    .badge.alt {
      background: var(--sl-color-neutral-700);
      top: 12px; /* Reset position for enterprise badge */
      left: auto;
      right: 12px;
      transform: none;
      box-shadow: none;
      font-size: 0.75rem;
      padding: 0.25rem 0.5rem;
    }

    .plan-name {
      margin: 0 0 0.25rem 0;
      font-size: 1.25rem;
    }

    .price-wrap {
      margin: 0.25rem 0 0.75rem 0;
    }

    .price-main {
      font-size: 2rem;
      font-weight: 800;
    }

    .unit {
      font-size: 0.95rem;
    }

    .plan-name,
    .price-main,
    .unit {
      text-align: center;
    }

    .price-sub {
      color: var(--sl-color-text-secondary);
      font-size: 0.95rem;
      margin-top: 0.25rem;
    }

    .plan-card.popular .price-sub {
      color: var(--sl-color-neutral-300);
    }

    .divider {
      border: none;
      height: 1px;
      background-color: var(--sl-color-neutral-600);
      margin: 1rem 0;
    }

    .plan-card.popular .divider {
      background-color: var(--sl-color-primary-500);
    }

    .features {
      list-style: none;
      padding: 0;
      margin: 0.5rem 0 1rem 0;
      display: flex;
      flex-direction: column;
      gap: 0.5rem;
    }

    .feat-icon {
      color: var(--sl-color-success-600);
    }
    .feature.excluded .feat-icon {
      color: var(--sl-color-neutral-400);
    }

    .feat-text {
      font-size: 0.95rem;
    }

    .feat-value {
      font-size: 0.85rem;
      color: var(--sl-color-text-secondary);
    }

    .cta {
      margin-top: auto;
      width: 100%;
    }

    .cta::part(label) {
      font-weight: 600;
    }

    /* Default outlined button style */
    .cta::part(base) {
      background-color: transparent;
      border: 1px solid #58a6ff;
      color: #58a6ff;
      font-weight: 600;
      transition: all 0.2s ease-in-out;
    }

    .cta::part(base):hover {
      background-color: #58a6ff;
      color: white;
    }

    /* Solid, gradient button for the popular plan */
    .popular .cta::part(base) {
      background: linear-gradient(45deg, #a777ff, #f777ff);
      border: none;
      color: white;
    }

    .popular .cta::part(base):hover {
      filter: brightness(1.1);
    }
  `;

  render() {
    const isPopular = this.plan.id === 'teams' || this.plan.id === 'ultra';
    const hasArrayFeatures = Array.isArray(this.plan.features);

    return html`
      <div
        class="plan-card ${isPopular ? 'popular' : ''} ${
          this.dark ? 'sl-theme-dark' : ''
        }"
      >
        ${
          this.plan.badge
            ? html`<div class="badge">${this.plan.badge}</div>`
            : null
        }
        ${
          this.plan.id === 'enterprise' && !this.plan.badge
            ? html`<div class="badge alt">Enterprise</div>`
            : null
        }
        ${
          this.plan.id !== 'free'
            ? html`<h3 class="plan-name">${this.plan.name}</h3>`
            : ''
        }
        <div class="price-wrap">${this.formatPrice(this.plan)}</div>

        <hr class="divider" />

        <ul class="features">
          ${
            hasArrayFeatures
              ? (this.plan.features as string[]).map(
                  (feature) =>
                    html`<li class="feature included">
                      <span class="feat-icon"
                        ><sl-icon name="check-lg"></sl-icon
                      ></span>
                      <span class="feat-text">${feature}</span>
                    </li>`
                )
              : this.featureOrder.map((key) =>
                  this.renderFeature(
                    (this.plan.features as { [key: string]: any })[key],
                    key
                  )
                )
          }
        </ul>

        <sl-button
          class="cta"
          size="large"
          variant="default"
          @click=${this._handleSignUp}
        >
          ${
            this.plan.id === 'enterprise'
              ? 'Contact Sales'
              : this.plan.id === 'opensource'
                ? 'View on GitHub'
                : this.plan.id === 'free'
                  ? 'Get Free'
                  : this.plan.id === 'teams'
                    ? 'Start Free Trial'
                    : `Get ${this.plan.name}`
          }
        </sl-button>
      </div>
    `;
  }
}
