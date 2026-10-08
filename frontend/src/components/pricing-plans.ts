import { LitElement, html, css, type TemplateResult } from 'lit';
import { customElement, property } from 'lit/decorators.js';
import './pricing-card';

/**
 * One priced card, as the public pricing page and the console plan page both
 * describe it. The shape comes from the brand's pricing content, not from the
 * billing catalog, so the two pages cannot print different numbers for the
 * same plan.
 */
export interface PricingPlan {
  id: string;
  name: string;
  /** Small line under the name; only the plans that declare one get it. */
  subtitle?: string;
  price_monthly: number | null;
  price_annually: number | null;
  features: string[];
  badge?: string;
  highlight?: boolean;
  cta_text?: string;
  cta_url?: string;
  description?: string;
  price_label?: string;
  price_note?: string;
  price_note_annual?: string;
  tagline?: string;
  /** Which tab the plan belongs to; anything untagged is a cloud plan. */
  deployment?: 'cloud' | 'dedicated';
}

export interface ComparisonRow {
  label: string;
  values: Record<string, string | boolean>;
}

export interface ComparisonGroup {
  title: string;
  rows: ComparisonRow[];
}

export interface Comparison {
  title?: string;
  note?: string;
  note_link?: { label: string; url: string };
  groups: ComparisonGroup[];
}

/** What a card's button says and does, for a page that knows more than price. */
export interface PlanCta {
  label: string;
  disabled?: boolean;
  /** One short line under the button: when a change applies, or why not. */
  note?: string;
}

/**
 * The card row and the comparison table, shared by the public pricing page and
 * the console plan page.
 *
 * Both render into the LIGHT DOM on purpose. The two pages own the surrounding
 * layout and the table's styles, and a shadow root here would cut those styles
 * off and hide the cards from the page's own queries. Nothing in here has
 * state of its own: it is markup, so that "the same cards" means the same
 * markup rather than two copies that drift.
 */
@customElement('pricing-plan-cards')
export class PricingPlanCards extends LitElement {
  @property({ attribute: false }) plans: PricingPlan[] = [];
  @property({ type: String }) interval: 'month' | 'year' = 'year';
  @property({ type: Boolean }) dark = false;
  /**
   * What each card's button should say, where the page knows. The public page
   * passes nothing and every card keeps its "Get <plan>" default; the console
   * page knows which plan the account is on and says so.
   */
  @property({ attribute: false }) ctaFor?: (
    plan: PricingPlan
  ) => PlanCta | null;
  /**
   * The card to draw attention to, where the page has a reason to. The console
   * marks the plan a reader arrived asking about (the one that unlocks the
   * feature they were refused); the public page marks nothing, so its own
   * "most popular" card keeps the only emphasis on the row.
   */
  @property({ type: String }) highlightId = '';

  protected createRenderRoot() {
    return this;
  }

  render() {
    if (!this.plans.length) return html``;
    return html`
      <div class="plans-grid">
        ${this.plans.map((plan) => {
          const cta = this.ctaFor?.(plan) ?? null;
          return html`
            <pricing-card
              class=${plan.id === this.highlightId ? 'requested' : ''}
              .plan=${plan}
              .interval=${this.interval}
              .dark=${this.dark}
              .ctaLabel=${cta?.label ?? ''}
              .ctaDisabled=${cta?.disabled === true}
              .ctaNote=${cta?.note ?? ''}
            ></pricing-card>
          `;
        })}
      </div>
    `;
  }
}

/**
 * The below-the-fold comparison table: quotas, retention and the feature
 * split, so every card stays one number plus one line.
 */
@customElement('pricing-plan-comparison')
export class PricingPlanComparison extends LitElement {
  @property({ attribute: false }) comparison: Comparison | null = null;
  @property({ attribute: false }) plans: PricingPlan[] = [];
  @property({ type: String }) fallbackTitle = '';
  /** The column to mark as the account's own plan. Empty on the public page. */
  @property({ type: String }) currentPlanId = '';

  protected createRenderRoot() {
    return this;
  }

  /** Render one cell: booleans become marks, text prints as it arrived. */
  private cell(value: string | boolean | undefined): TemplateResult | string {
    if (value === true) {
      return html`<span class="check-mark"
        ><sl-icon name="check-lg" label="Included"></sl-icon
      ></span>`;
    }
    if (value === false) {
      return html`<span class="cross-mark"
        ><sl-icon name="x-lg" label="Not included"></sl-icon
      ></span>`;
    }
    // Undefined renders empty rather than as a dash or a "no": an unstated
    // limit is not a claim that the plan lacks the capability.
    return value ?? '';
  }

  render() {
    const comparison = this.comparison;
    if (!comparison?.groups?.length || !this.plans.length) return html``;
    const planIds = this.plans.map((p) => p.id);
    const colCount = planIds.length + 1;
    return html`
      <section class="comparison-section">
        <div class="section-container">
          <h2 class="text-center">${comparison.title || this.fallbackTitle}</h2>
          <div class="comparison-scroll">
            <table class="comparison-table">
              <thead>
                <tr>
                  <th scope="col" class="row-label"></th>
                  ${this.plans.map(
                    (plan) =>
                      html`<th
                        scope="col"
                        class=${plan.id === this.currentPlanId ? 'current' : ''}
                      >
                        ${plan.name}${
                          plan.id === this.currentPlanId
                            ? html`<span class="current-tag">Your plan</span>`
                            : ''
                        }
                      </th>`
                  )}
                </tr>
              </thead>
              <tbody>
                ${comparison.groups.map(
                  (group) => html`
                    <tr class="group-row">
                      <th scope="colgroup" colspan=${colCount}>
                        ${group.title}
                      </th>
                    </tr>
                    ${group.rows.map(
                      (row) => html`
                        <tr>
                          <th scope="row" class="row-label">${row.label}</th>
                          ${planIds.map(
                            (id) =>
                              html`<td>${this.cell(row.values?.[id])}</td>`
                          )}
                        </tr>
                      `
                    )}
                  `
                )}
              </tbody>
            </table>
          </div>
          ${
            comparison.note
              ? html`<p class="comparison-note">
                  ${comparison.note}${
                    comparison.note_link
                      ? html` <a href=${comparison.note_link.url}
                          >${comparison.note_link.label}</a
                        >`
                      : ''
                  }
                </p>`
              : ''
          }
        </div>
      </section>
    `;
  }
}

/**
 * The styles for the markup above.
 *
 * The elements render into the light DOM, so these rules have to live in the
 * shadow root of whichever page hosts them. Exported as one constant so the
 * public page and the console page cannot style the same table differently.
 */
export const pricingPlansStyles = css`
  /* Four cards need a tighter minimum than the shared 260px grid or the
     ladder wraps to two rows on ordinary laptop widths. */
  .plans-grid {
    grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
  }

  .comparison-section {
    margin-top: 3.5rem;
  }

  /* Narrow screens scroll the table sideways instead of squashing four
     columns into unreadable slivers. */
  .comparison-scroll {
    overflow-x: auto;
    -webkit-overflow-scrolling: touch;
  }

  .comparison-table {
    width: 100%;
    min-width: 720px;
    margin: 0 auto;
    border-collapse: collapse;
    background-color: #21262f;
    color: #e6edf3;
    border-radius: 16px;
    overflow: hidden;
  }

  .comparison-table th,
  .comparison-table td {
    font-size: 0.95rem;
    padding: 0.75rem 1rem;
    text-align: center;
    vertical-align: middle;
    border-bottom: 1px solid rgba(230, 237, 243, 0.12);
  }

  .comparison-table thead th {
    font-size: 1.05rem;
    font-weight: 600;
    border-bottom: 2px solid #58a6ff;
  }

  /* The account's own column, on the console page only. */
  .comparison-table thead th.current {
    color: #58a6ff;
  }

  .comparison-table .current-tag {
    display: block;
    font-size: 0.75rem;
    font-weight: 500;
    letter-spacing: 0.04em;
    text-transform: uppercase;
  }

  .comparison-table .row-label {
    text-align: left;
    font-weight: 500;
    min-width: 220px;
  }

  .comparison-table .group-row th {
    text-align: left;
    font-size: 0.8rem;
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: #8b949e;
    padding-top: 1.5rem;
    border-bottom: 1px solid rgba(230, 237, 243, 0.2);
  }

  .comparison-table tbody tr:last-child th,
  .comparison-table tbody tr:last-child td {
    border-bottom: none;
  }

  .check-mark sl-icon {
    color: #58a6ff;
    font-size: 1.2rem;
  }

  .cross-mark sl-icon {
    color: #6e7681;
    font-size: 1.1rem;
  }

  .comparison-note {
    margin-top: 1rem;
    text-align: center;
    font-size: 0.9rem;
    color: var(--sl-color-text-secondary);
  }
`;

declare global {
  interface HTMLElementTagNameMap {
    'pricing-plan-cards': PricingPlanCards;
    'pricing-plan-comparison': PricingPlanComparison;
  }
}
