import { LitElement, html, css } from 'lit';
import { customElement, property } from 'lit/decorators.js';
import { formatPlanPrice } from '../pricing-format';

interface Plan {
  id: string;
  name: string;
  price_monthly: number | null;
  price_annually: number | null;
  features: { [key: string]: any } | string[];
  badge?: string;
  price_label?: string;
  price_note?: string;
  price_note_annual?: string;
  tagline?: string;
  cta_text?: string;
}

@customElement('pricing-card')
export class PricingCard extends LitElement {
  @property({ type: Object }) plan!: Plan;
  @property({ type: String }) interval: 'month' | 'year' = 'month';
  @property({ type: Array }) featureOrder: string[] = [];
  @property({ type: Object }) featureLabels: Record<string, string> = {};
  /**
   * Per-key value renderers for the object-features list. Raw numbers are
   * ambiguous once plans carry mixed units: `retention_days: 90` and
   * `hosted_models_monthly_limit_usd: 2` both render as a bare number without
   * one of these. Returning `null` marks the row excluded.
   *
   * `attribute: false` because functions cannot round-trip through an HTML
   * attribute.
   */
  @property({ attribute: false }) featureFormatters: Record<
    string,
    (value: any) => string | null
  > = {};
  @property({ type: Boolean }) dark = false;

  /**
   * Render the one headline number.
   *
   * Pricing is per BRACKET, not per seat: a plan costs the same whether one
   * person or the whole bracket uses it, so no `/user` unit is ever printed.
   * Seat counts belong in the comparison table, not in the price.
   *
   * The headline, unit and note all come from `formatPlanPrice`, which the
   * server-side render calls too, so the crawler-visible string and the one
   * the visitor reads are produced by the same rule.
   */
  private formatPrice(plan: Plan) {
    // The highlighted card paints a saturated gradient behind this text, so
    // the secondary line cannot keep the neutral grey it uses on a flat card:
    // grey on purple failed contrast and was unreadable in review.
    const subClass = this._isPopular() ? 'price-sub on-highlight' : 'price-sub';
    const { headline, unit, note } = formatPlanPrice(plan, this.interval);

    return html`
      <div class="price-main">${headline}</div>
      ${unit ? html`<div class="unit">${unit}</div>` : null}
      ${note ? html`<div class=${subClass}>${note}</div>` : null}
    `;
  }

  /**
   * The highlighted card. Config wins first so a brand can move the emphasis
   * without a code change; 'pro' is the recommended plan in the 2026 ladder,
   * and 'teams'/'ultra' keep their highlight only where a config still lists
   * them.
   */
  private _isPopular(): boolean {
    return (
      (this.plan as any)?.highlight === true ||
      this.plan?.id === 'pro' ||
      this.plan?.id === 'teams' ||
      this.plan?.id === 'ultra'
    );
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

    const formatter = this.featureFormatters[key];
    if (formatter) {
      displayValue = formatter(value);
      included = displayValue !== null;
      return this._featureRow(label, included, displayValue);
    }

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

    return this._featureRow(label, included, displayValue);
  }

  private _featureRow(
    label: string,
    included: boolean,
    displayValue: string | null
  ) {
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
      text-align: center;
    }

    /* The single line under the price. Grows to fill so every card's CTA
       sits on the same baseline regardless of how long the line is. */
    .tagline {
      margin: 0.75rem 0 1rem 0;
      text-align: center;
      font-size: 0.95rem;
      line-height: 1.4;
      flex: 1 1 auto;
    }

    /* On the gradient card the secondary line is the card's own foreground at
       85% opacity. The old neutral grey token resolved to a mid grey that sat
       on a saturated blue-purple background and failed contrast. */
    .price-sub.on-highlight,
    .plan-card.popular .price-sub {
      color: rgba(255, 255, 255, 0.85);
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

  /** Default CTA label when the plan config does not supply one. */
  private _ctaLabel(): string {
    if (this.plan.cta_text) return this.plan.cta_text;
    switch (this.plan.id) {
      case 'enterprise':
        return 'Contact us';
      case 'opensource':
        return 'View on GitHub';
      case 'free':
        return 'Start free';
      default:
        return `Get ${this.plan.name}`;
    }
  }

  render() {
    const isPopular = this._isPopular();
    const hasArrayFeatures = Array.isArray(this.plan.features);
    // The approved card shape is one number plus one line. When a plan
    // carries a tagline we print exactly that and nothing else: the quota,
    // retention, and feature split rows live in the comparison table so the
    // cards stay scannable and cannot drift out of sync with the table.
    const featureList = hasArrayFeatures
      ? (this.plan.features as string[])
      : null;
    const showFeatureList =
      !this.plan.tagline && (featureList?.length || !hasArrayFeatures);

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
        <h3 class="plan-name">${this.plan.name}</h3>
        <div class="price-wrap">${this.formatPrice(this.plan)}</div>

        ${
          this.plan.tagline
            ? html`<p class="tagline">${this.plan.tagline}</p>`
            : null
        }
        ${showFeatureList ? html`<hr class="divider" />` : null}
        ${
          showFeatureList
            ? html`<ul class="features">
                ${
                  featureList
                    ? featureList.map(
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
              </ul>`
            : null
        }

        <sl-button
          class="cta"
          size="large"
          variant="default"
          @click=${this._handleSignUp}
        >
          ${this._ctaLabel()}
        </sl-button>
      </div>
    `;
  }
}
