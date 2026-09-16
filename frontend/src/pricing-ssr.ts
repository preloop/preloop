import type {
  PricingComparison,
  PricingConfig,
  PricingPlan,
} from './brand-config';
import { formatPlanPriceText } from './pricing-format';

/**
 * Server-side render of the whole pricing page into the LIGHT DOM.
 *
 * Everything a search engine needs has to be readable with JavaScript
 * disabled: both tabs, every card, both comparison tables, and the prices for
 * both billing periods. So this emits two plain sections, Cloud then
 * Dedicated, as ordinary `<h2>/<table>/<p>` markup. `<public-pricing-view>`
 * projects the same data through named slots once it hydrates and hides the
 * inactive tab; nothing here is shadow-DOM-only.
 *
 * Kept out of vite-plugin-brand.ts so it imports no Node built-ins and can be
 * unit tested in the browser test runner against a real DOM parse.
 */

function escapeHtml(value: string | number | null | undefined): string {
  if (value === null || value === undefined) return '';
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

const escapeAttr = escapeHtml;

/** Heading when a brand ships comparison groups but no `comparison.title`. */
export const CLOUD_COMPARISON_FALLBACK_TITLE = 'Compare cloud plans';
/** Heading when a brand ships dedicated comparison groups but no title. */
export const DEDICATED_COMPARISON_FALLBACK_TITLE = 'Compare dedicated editions';

interface PricingSsrBrand {
  name?: string;
  landing?: { pricing?: PricingConfig };
}

/**
 * One card. The price line carries BOTH periods: the monthly figure and the
 * yearly figure are each a full sentence, so a crawler that never runs the
 * period toggle still indexes both numbers. The hydrated component replaces
 * this block wholesale with the period the visitor selected.
 */
function planBlock(plan: PricingPlan, slot: string): string {
  const monthlyText = formatPlanPriceText(plan, 'month');
  const yearlyText = formatPlanPriceText(plan, 'year');
  const cta = plan.cta_text || 'Learn more';
  const ctaUrl = plan.cta_url || '/register';
  const isExternal = typeof ctaUrl === 'string' && ctaUrl.startsWith('http');
  const target = isExternal ? ' target="_blank" rel="noopener noreferrer"' : '';
  const featureItems = (plan.features || [])
    .map((f: string) => `<li>${escapeHtml(f)}</li>`)
    .join('\n            ');
  const description = plan.description
    ? `<p class="plan-description">${escapeHtml(plan.description)}</p>`
    : '';
  const tagline = plan.tagline
    ? `<p class="plan-tagline">${escapeHtml(plan.tagline)}</p>`
    : '';
  const badge = plan.badge
    ? `<span class="badge">${escapeHtml(plan.badge)}</span>`
    : '';
  // Identical strings when a plan is quoted or free: printing the same
  // sentence twice would read as a duplicated price rather than two periods.
  const priceLines =
    monthlyText === yearlyText
      ? `<span class="price-period" data-interval="month year">${escapeHtml(monthlyText)}</span>`
      : `<span class="price-period" data-interval="month">Monthly: ${escapeHtml(monthlyText)}</span>
          <span class="price-period" data-interval="year">Yearly: ${escapeHtml(yearlyText)}</span>`;

  return `
        <div slot="${escapeAttr(slot)}"
             class="plan plan-${escapeAttr(plan.id)}${plan.highlight ? ' highlight' : ''}"
             data-plan-id="${escapeAttr(plan.id)}"
             data-plan-name="${escapeAttr(plan.name)}"
             data-price-monthly="${escapeAttr(plan.price_monthly)}"
             data-price-annually="${escapeAttr(plan.price_annually)}"
             data-price-label="${escapeAttr(plan.price_label || '')}"
             data-price-note="${escapeAttr(plan.price_note || '')}"
             data-price-note-annual="${escapeAttr(plan.price_note_annual || '')}"
             data-tagline="${escapeAttr(plan.tagline || '')}"
             data-badge="${escapeAttr(plan.badge || '')}"
             data-highlight="${plan.highlight ? 'true' : 'false'}"
             data-cta-text="${escapeAttr(cta)}"
             data-cta-url="${escapeAttr(ctaUrl)}"
             data-deployment="${escapeAttr(plan.deployment === 'dedicated' ? 'dedicated' : 'cloud')}"
             data-description="${escapeAttr(plan.description || '')}"
             data-features="${escapeAttr((plan.features || []).join('|'))}">
          ${badge}
          <h3>${escapeHtml(plan.name)}</h3>
          <p class="price">
          ${priceLines}
          </p>
          ${tagline}
          ${description}
          <ul>
            ${featureItems}
          </ul>
          <a class="plan-cta" href="${escapeAttr(ctaUrl)}"${target}>${escapeHtml(cta)}</a>
        </div>`;
}

/**
 * Render one comparison table into the light DOM.
 *
 * The table is emitted twice over: once as real `<table>` markup so crawlers
 * and no-JS visitors see the rows, and once as a JSON payload on
 * `data-comparison` so `<public-pricing-view>` can rehydrate it without
 * re-parsing HTML. Returns an empty string when nothing is configured, which
 * keeps brands that only want cards unaffected.
 */
export function generatePricingComparisonBlock(
  comparison: PricingComparison | undefined,
  plans: PricingPlan[],
  slot: string,
  fallbackHeading: string
): string {
  const groups = comparison?.groups || [];
  if (!groups.length || !plans.length) return '';

  const heading = comparison?.title || fallbackHeading;
  const planIds = plans.map((p) => p.id);

  const cell = (value: unknown): string => {
    if (value === true) return 'Included';
    if (value === false) return 'Not included';
    if (value === null || value === undefined) return '';
    return escapeHtml(String(value));
  };

  const headerCells = plans
    .map((p) => `<th scope="col">${escapeHtml(p.name)}</th>`)
    .join('');

  const bodyRows = groups
    .map((group) => {
      const groupHeader = `
            <tr class="group-row">
              <th scope="colgroup" colspan="${planIds.length + 1}">${escapeHtml(
                group.title || ''
              )}</th>
            </tr>`;
      const rows = (group.rows || [])
        .map((row) => {
          const cells = planIds
            .map((id) => `<td>${cell(row.values?.[id])}</td>`)
            .join('');
          return `
            <tr>
              <th scope="row">${escapeHtml(row.label || '')}</th>
              ${cells}
            </tr>`;
        })
        .join('');
      return groupHeader + rows;
    })
    .join('');

  const note = comparison?.note
    ? `<p class="comparison-note">${escapeHtml(comparison.note)}</p>`
    : '';

  return `
      <section slot="${escapeAttr(slot)}"
               class="pricing-comparison"
               data-comparison="${escapeAttr(JSON.stringify(comparison))}">
        <h2>${escapeHtml(heading)}</h2>
        <table>
          <thead>
            <tr><th scope="col"></th>${headerCells}</tr>
          </thead>
          <tbody>${bodyRows}
          </tbody>
        </table>
        ${note}
      </section>`;
}

export function generatePricingSlottedContent(config: PricingSsrBrand): string {
  const pricing = (config.landing?.pricing || {}) as PricingConfig;
  const plans = pricing.plans || [];
  const faqs = pricing.faqs || [];
  const title = pricing.title || `Pricing - ${config.name || 'Preloop'}`;
  const lead = pricing.lead || 'Choose the plan that fits your team.';

  // Hosted subscriptions. A plan the catalog or the brand tagged `dedicated`
  // belongs to the other tab and is never counted twice.
  const cloudPlans = plans.filter((plan) => plan.deployment !== 'dedicated');
  // Self-managed and quoted editions. The brand's `dedicated` block wins when
  // present; otherwise the tagged plans are the fallback, so a brand that only
  // marks a plan `dedicated` still gets a second tab.
  const dedicatedPlans =
    pricing.dedicated?.plans && pricing.dedicated.plans.length
      ? pricing.dedicated.plans
      : plans.filter((plan) => plan.deployment === 'dedicated');

  const cloudCards = cloudPlans
    .map((plan, idx) => planBlock(plan, `plan-${idx}`))
    .join('\n');
  const dedicatedCards = dedicatedPlans
    .map((plan, idx) =>
      planBlock({ ...plan, deployment: 'dedicated' }, `dedicated-plan-${idx}`)
    )
    .join('\n');

  const cloudComparison = generatePricingComparisonBlock(
    pricing.comparison,
    cloudPlans,
    'comparison',
    CLOUD_COMPARISON_FALLBACK_TITLE
  );
  const dedicatedComparison = generatePricingComparisonBlock(
    pricing.dedicated?.comparison,
    dedicatedPlans,
    'dedicated-comparison',
    DEDICATED_COMPARISON_FALLBACK_TITLE
  );

  const cloudLabel = pricing.cloud_label || 'Cloud';
  const dedicatedLabel = pricing.dedicated?.label || 'Dedicated';

  const faqBlocks = faqs
    .map(
      (faq, idx) => `
        <div slot="faq-${idx}"
             class="faq-item"
             data-q="${escapeAttr(faq.q)}"
             data-a="${escapeAttr(faq.a)}">
          <h3>${escapeHtml(faq.q)}</h3>
          <p>${escapeHtml(faq.a)}</p>
        </div>`
    )
    .join('\n');

  const dedicatedSection = dedicatedPlans.length
    ? `
      <section class="pricing-tab pricing-tab-dedicated"
               slot="dedicated-tab"
               data-deployment="dedicated"
               data-label="${escapeAttr(dedicatedLabel)}">
        <h2>${escapeHtml(dedicatedLabel)}</h2>
        ${pricing.dedicated?.lead ? `<p class="lead">${escapeHtml(pricing.dedicated.lead)}</p>` : ''}
        ${dedicatedCards}
        ${dedicatedComparison}
      </section>`
    : '';

  return `
    <article class="pricing-content">
      <header class="pricing-header" slot="pricing-heading" data-title="${escapeAttr(title)}" data-lead="${escapeAttr(lead)}" data-billing-toggle="${pricing.billing_toggle !== false}" data-cloud-label="${escapeAttr(cloudLabel)}" data-dedicated-label="${escapeAttr(dedicatedLabel)}">
        <h1>${escapeHtml(title)}</h1>
        <p class="lead">${escapeHtml(lead)}</p>
      </header>

      <section class="pricing-tab pricing-tab-cloud" data-deployment="cloud" data-label="${escapeAttr(cloudLabel)}">
        <h2>${escapeHtml(cloudLabel)}</h2>
        ${cloudCards}
        ${cloudComparison}
      </section>

      ${dedicatedSection}

      ${
        faqs.length > 0
          ? `
      <section class="pricing-faq">
        <h2>Frequently Asked Questions</h2>
        ${faqBlocks}
      </section>`
          : ''
      }
    </article>
  `;
}
