import { LitElement, html, css, unsafeCSS } from 'lit';
import { unsafeHTML } from 'lit/directives/unsafe-html.js';
import { customElement, state } from 'lit/decorators.js';
import { fetchWithAuth, fetchPublic, startCheckout } from '../../api';
import landingStyles from '../../styles/landing.css?inline';
import pricingStyles from '../../styles/pricing-styles.css?inline';
import '../../components/billing-toggle';
import '../../components/pricing-card';

interface Plan {
  id: string;
  name: string;
  price_monthly: number | null;
  price_annually: number | null;
  features: string[];
  badge?: string;
  highlight?: boolean;
  cta_text?: string;
  cta_url?: string;
  description?: string;
  price_label?: string;
}

interface PricingFaq {
  q: string;
  a: string;
}

@customElement('public-pricing-view')
export class PublicPricingView extends LitElement {
  @state() private _interval: 'month' | 'year' = 'year';

  @state() private _plans: Plan[] = [];
  @state() private _faqs: PricingFaq[] = [];
  @state() private _title = 'Pricing';
  @state() private _lead = 'Start your 14-day free trial today.';
  @state() private _billingToggle = true;
  @state() private _loaded = false;

  async connectedCallback() {
    super.connectedCallback();
    await this._loadContent();
  }

  private async _loadContent() {
    try {
      // Prefer slotted SEO content (SSR-injected light DOM) so we keep
      // whatever was actually served to the user / crawler.
      const children = Array.from(this.children);
      const hasSlottedPlan = children.some((el) =>
        el.getAttribute('slot')?.startsWith('plan-')
      );

      if (hasSlottedPlan) {
        this._loadFromSlots(children);
      } else {
        await this._loadFromJson();
      }
    } catch (err) {
      console.error('[pricing-view] Failed to load pricing content:', err);
      // Best-effort fallback: try JSON if slots failed.
      try {
        await this._loadFromJson();
      } catch (err2) {
        console.error('[pricing-view] Failed to load pricing JSON:', err2);
      }
    } finally {
      this._loaded = true;
    }
  }

  private _loadFromSlots(children: Element[]) {
    const plans: Plan[] = [];
    for (let i = 0; i < 20; i++) {
      const el = children.find(
        (c) => c.getAttribute('slot') === `plan-${i}`
      ) as HTMLElement | undefined;
      if (!el) break;
      const featuresAttr = el.getAttribute('data-features') || '';
      const priceMonthlyRaw = el.getAttribute('data-price-monthly');
      const priceAnnuallyRaw = el.getAttribute('data-price-annually');
      const priceMonthly =
        priceMonthlyRaw === '' || priceMonthlyRaw === null
          ? null
          : Number(priceMonthlyRaw);
      const priceAnnually =
        priceAnnuallyRaw === '' || priceAnnuallyRaw === null
          ? null
          : Number(priceAnnuallyRaw);
      plans.push({
        id: el.getAttribute('data-plan-id') || `plan-${i}`,
        name: el.getAttribute('data-plan-name') || '',
        price_monthly: Number.isFinite(priceMonthly)
          ? (priceMonthly as number)
          : null,
        price_annually: Number.isFinite(priceAnnually)
          ? (priceAnnually as number)
          : null,
        price_label: el.getAttribute('data-price-label') || undefined,
        badge: el.getAttribute('data-badge') || undefined,
        highlight: el.getAttribute('data-highlight') === 'true',
        cta_text: el.getAttribute('data-cta-text') || undefined,
        cta_url: el.getAttribute('data-cta-url') || undefined,
        description: el.getAttribute('data-description') || undefined,
        features: featuresAttr ? featuresAttr.split('|').filter(Boolean) : [],
      });
    }

    const faqs: PricingFaq[] = [];
    for (let i = 0; i < 30; i++) {
      const el = children.find((c) => c.getAttribute('slot') === `faq-${i}`) as
        HTMLElement | undefined;
      if (!el) break;
      const q = el.getAttribute('data-q') || '';
      const a = el.getAttribute('data-a') || '';
      if (q && a) faqs.push({ q, a });
    }

    if (plans.length) this._plans = plans;
    if (faqs.length) this._faqs = faqs;
  }

  private async _loadFromJson() {
    const response = await fetch('/landing-content.json');
    if (!response.ok) {
      throw new Error(`Failed to load pricing content: ${response.statusText}`);
    }
    const content = await response.json();
    const pricing = content.pricing || {};
    if (pricing.title) this._title = pricing.title;
    if (pricing.lead) this._lead = pricing.lead;
    if (typeof pricing.billing_toggle === 'boolean') {
      this._billingToggle = pricing.billing_toggle;
    }
    if (Array.isArray(pricing.plans)) {
      this._plans = pricing.plans.map((p: any) => ({
        id: p.id,
        name: p.name,
        price_monthly: p.price_monthly ?? null,
        price_annually: p.price_annually ?? null,
        price_label: p.price_label,
        badge: p.badge,
        highlight: p.highlight,
        cta_text: p.cta_text,
        cta_url: p.cta_url,
        description: p.description,
        features: p.features || [],
      }));
    }
    if (Array.isArray(pricing.faqs)) {
      this._faqs = pricing.faqs;
    }
  }

  /** Seam for tests: window.location is not stubbable in the runner. */
  private _navigate(url: string) {
    window.location.href = url;
  }

  private _handleSignUpRequest(e: CustomEvent) {
    this._handleSignUp(e.detail.planId);
  }

  private _planById(id: string): Plan | undefined {
    return this._plans.find((p) => p.id === id);
  }

  private async _handleSignUp(planId: string) {
    const plan = this._planById(planId);

    if (planId === 'opensource') {
      const url = plan?.cta_url || 'https://github.com/preloop/preloop';
      if (/^https?:\/\//.test(url)) {
        window.open(url, '_blank');
      } else {
        this._navigate(url);
      }
      return;
    }

    if (planId === 'enterprise') {
      this._navigate(plan?.cta_url || '/request-demo');
      return;
    }

    if (planId === 'teams') {
      // Signup is card-free (T2 paywall move): logged-out visitors register
      // first and upgrade in-product. Logged-in users on /pricing are here to
      // upgrade — the shared checkout helper is the one deliberate paid door.
      const authed = !!localStorage.getItem('accessToken');
      if (!authed) {
        this._navigate('/register');
        return;
      }
      try {
        await startCheckout('teams', this._interval, '/console');
      } catch (error) {
        console.error('Checkout error:', error);
        this._navigate('/register');
      }
    }
  }

  static styles = [
    unsafeCSS(pricingStyles),
    unsafeCSS(landingStyles),
    css`
      .loading,
      .error {
        text-align: center;
        margin: 2rem 0;
      }
      .error {
        color: var(--sl-color-danger-600);
      }

      .pricing-table {
        display: none; /* Hidden by default on mobile */
        width: 80%;
        margin: 0 auto;
        table-layout: fixed;
        border-collapse: separate;
        border-spacing: 0;
        border-radius: 16px;
        background-color: #21262f; /* Dark background from landing page */
        color: #e6edf3; /* Light text color from landing page */
        border: 1px solid #161b22; /* Subtle border from landing page */
        box-shadow: 0 8px 24px rgba(0, 0, 0, 0.1);
      }

      .pricing-table th,
      .pricing-table td {
        font-size: 1rem;
        padding: 1rem;
        text-align: center;
        vertical-align: middle;
      }

      .pricing-table tr:last-child td {
        border-bottom: none; /* Remove border for the last row */
      }

      .pricing-table th,
      .pricing-table td {
        width: 33.33%;
      }

      .pricing-table th {
        padding: 2rem 0 0.5rem 0;
        font-size: 1.1rem;
        font-weight: 600;
        border-bottom: 2px solid #58a6ff; /* Header underline inspired by landing page */
      }

      .pricing-table td:first-child {
        text-align: left;
        font-weight: 500;
      }

      .pricing-table th {
        font-weight: 600;
        font-size: 1.1rem;
      }

      .pricing-table .price {
        font-size: 2.4rem;
        font-weight: 700;
        text-align: center;
      }

      /* Popular column styles */
      .popular {
        position: relative;
        background: linear-gradient(
          90deg,
          hsl(220, 60%, 40%),
          hsl(260, 65%, 38%)
        );
        color: white;
      }

      .pricing-table th.popular {
        border-bottom-color: #a777ff;
      }

      /* Rounded corners for table cells */
      .pricing-table th:first-child {
        border-top-left-radius: 12px;
      }

      .pricing-table th:last-child {
        border-top-right-radius: 12px;
      }

      .pricing-table tr:last-child td:first-child {
        border-bottom-left-radius: 12px;
      }

      .pricing-table tr:last-child td:last-child {
        border-bottom-right-radius: 12px;
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
        box-shadow: 0 4px 12px rgba(0, 0, 0, 0.2);
        z-index: 1;
      }

      /* Default outlined button style */
      .pricing-button::part(base) {
        background-color: transparent;
        border: 1px solid #58a6ff;
        color: #58a6ff;
        font-size: 1rem;
        font-weight: 600;
        transition: all 0.2s ease-in-out;
      }

      .pricing-button::part(base):hover {
        background-color: #58a6ff;
        color: white;
      }

      /* Solid, gradient button for the popular plan */
      .popular .pricing-button::part(base) {
        background: linear-gradient(45deg, #a777ff, #f777ff);
        border: none;
        color: white;
        font-weight: 600;
      }

      .popular .pricing-button::part(base):hover {
        filter: brightness(1.1);
      }

      .check-mark sl-icon {
        color: #58a6ff; /* Icon color from landing page */
        font-size: 1.2rem;
      }

      .popular .check-mark sl-icon {
        color: white;
      }

      .hero-content .lead {
        margin: 0 auto;
        text-align: center;
      }

      /* Desktop view */
      @media (min-width: 860px) {
        .plans-grid {
          display: none;
        }
        .pricing-table {
          display: table;
        }
      }
    `,
  ];

  private _renderCards() {
    return html`
      <div class="plans-grid" @signup-requested=${this._handleSignUpRequest}>
        ${this._plans.map(
          (plan) => html`
            <pricing-card
              .plan=${plan}
              .interval=${this._interval}
              .dark=${true}
            ></pricing-card>
          `
        )}
      </div>
    `;
  }

  private _renderTable() {
    const maxFeatures = Math.max(
      ...this._plans.map((p) => (p.features as string[]).length)
    );

    return html`
      <table class="pricing-table">
        <thead>
          <tr>
            ${this._plans.map(
              (plan) =>
                html`<th class="${plan.id === 'teams' ? 'popular' : ''}">
                  <div class="plan-name">${plan.name}</div>
                  ${
                    plan.badge
                      ? html`<div class="badge">${plan.badge}</div>`
                      : ''
                  }
                </th>`
            )}
          </tr>
        </thead>
        <tbody>
          <!-- Price Row -->
          <tr>
            ${this._plans.map((plan) => {
              let priceHtml;
              if (plan.id === 'enterprise') {
                priceHtml = html`<div class="price">Custom</div>`;
              } else if (
                plan.price_monthly !== null &&
                plan.price_annually !== null
              ) {
                const isMonthly = this._interval === 'month';
                const amount = isMonthly
                  ? plan.price_monthly
                  : plan.price_annually;

                const unit = isMonthly ? ' /user/month' : ' /user/year';
                const perMo = !isMonthly ? Math.round(amount / 12) : null;

                priceHtml = html`
                  <div class="price">
                    $${amount}
                    <p style="font-size: 1rem; font-weight: 400;">${unit}</p>
                  </div>
                `;
              }
              return html`<td class="${plan.id === 'teams' ? 'popular' : ''}">
                ${priceHtml}
              </td>`;
            })}
          </tr>

          <!-- Features Rows -->
          ${Array.from({ length: maxFeatures }).map((_, idx) => {
            return html`
              <tr>
                ${this._plans.map((plan) => {
                  const features = plan.features as string[];
                  const feature = features[idx];
                  return html`<td
                    class="${plan.id === 'teams' ? 'popular' : ''}"
                    style="text-align: left; padding-left: 2rem;"
                  >
                    ${
                      feature
                        ? html`<span class="check-mark"
                              ><sl-icon name="check-lg"></sl-icon
                            ></span>
                            ${feature}`
                        : ''
                    }
                  </td>`;
                })}
              </tr>
            `;
          })}

          <!-- Button Row -->
          <tr>
            ${this._plans.map(
              (plan) => html`
                <td class="${plan.id === 'teams' ? 'popular' : ''}">
                  <sl-button
                    class="pricing-button"
                    style="width: 100%;"
                    variant="default"
                    size="large"
                    @click=${() => this._handleSignUp(plan.id)}
                  >
                    ${
                      plan.cta_text
                        ? plan.cta_text
                        : plan.id === 'enterprise'
                          ? 'Contact Sales'
                          : plan.id === 'opensource'
                            ? 'View on GitHub'
                            : 'Start Free Trial'
                    }
                  </sl-button>
                </td>
              `
            )}
          </tr>
        </tbody>
      </table>
    `;
  }

  private _handleFaqClick(e: Event) {
    e.preventDefault();
    const summary = e.currentTarget as HTMLElement;
    const details = summary.parentElement as HTMLDetailsElement;
    const answer = summary.nextElementSibling as HTMLElement | null;
    if (!answer) return;

    if (details.open) {
      answer.style.height = `${answer.scrollHeight}px`;
      requestAnimationFrame(() => {
        answer.style.height = '0px';
      });
      answer.addEventListener(
        'transitionend',
        () => {
          details.removeAttribute('open');
        },
        { once: true }
      );
    } else {
      details.setAttribute('open', '');
      answer.style.height = `${answer.scrollHeight}px`;
      answer.addEventListener(
        'transitionend',
        () => {
          if (details.open) {
            answer.style.height = 'auto';
          }
        },
        { once: true }
      );
    }
  }

  private _renderFaqs() {
    if (!this._faqs.length) return '';
    return html`
      <section class="pricing-faq main-section faq-section">
        <div class="section-container">
          <h2 class="text-center">Frequently Asked Questions</h2>
          <div class="faq-list">
            ${this._faqs.map(
              (faq) => html`
                <details class="faq-item">
                  <summary class="faq-question" @click=${this._handleFaqClick}>
                    <span>${faq.q}</span>
                    <sl-icon name="chevron-down"></sl-icon>
                  </summary>
                  <div class="faq-answer">
                    <div class="faq-answer-content">${unsafeHTML(faq.a)}</div>
                  </div>
                </details>
              `
            )}
          </div>
        </div>
      </section>
    `;
  }

  render() {
    return html`
      <app-header></app-header>
      <main>
        <section class="main-section">
          <div class="section-container hero-inner">
            <div class="hero-content">
              <h1 class="fw-bold">
                <span class="gradient-product">${this._title}</span>
              </h1>
              <p class="lead">${this._lead}</p>
            </div>
          </div>

          <div class="section-container">
            ${
              this._billingToggle
                ? html`<billing-toggle
                    .dark=${true}
                    .interval=${this._interval}
                    @interval-change=${(e: CustomEvent) =>
                      (this._interval = e.detail.value)}
                  ></billing-toggle>`
                : ''
            }
            ${
              this._plans.length
                ? html`${this._renderCards()} ${this._renderTable()}`
                : ''
            }
          </div>
        </section>
        ${this._renderFaqs()}
      </main>
      <app-footer></app-footer>
    `;
  }
}
