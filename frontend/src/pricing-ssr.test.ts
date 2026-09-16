import { expect } from '@open-wc/testing';
import { generatePricingSlottedContent } from './pricing-ssr';
import type { BrandConfig } from './brand-config';

/**
 * The crawler contract.
 *
 * Everything a search engine needs from /pricing has to be in the LIGHT DOM
 * of the served HTML with JavaScript disabled: both tabs, every card, both
 * comparison tables, and the prices for both billing periods. These tests
 * parse the server-side render exactly as a crawler would and assert on the
 * visible text, not on the data-* payloads the hydrated component reads.
 */

const CONFIG = {
  name: 'Preloop',
  landing: {
    pricing: {
      title: 'Pricing',
      lead: 'USD prices exclude tax; annual plans are prepaid.',
      billing_toggle: true,
      plans: [
        {
          id: 'free',
          name: 'Free',
          price_monthly: 0,
          price_annually: 0,
          tagline: 'Try Preloop with your own keys.',
          cta_text: 'Start free',
          cta_url: '/register',
          features: [],
        },
        {
          id: 'pro',
          name: 'Pro',
          price_monthly: 12,
          price_annually: 120,
          tagline: 'You and all your agents.',
          cta_text: 'Get Pro',
          cta_url: '/register',
          features: [],
        },
        {
          id: 'team',
          name: 'Team',
          price_monthly: 120,
          price_annually: 1200,
          tagline: 'Up to 5 people, every agent governed.',
          cta_text: 'Get Team',
          cta_url: '/register',
          features: [],
        },
        {
          id: 'business',
          name: 'Business',
          price_monthly: 350,
          price_annually: 3500,
          tagline: 'Up to 20 people, scale usage.',
          cta_text: 'Get Business',
          cta_url: '/register',
          features: [],
        },
      ],
      comparison: {
        title: 'Compare cloud plans',
        note: 'Quotas never stop the gateway.',
        groups: [
          {
            title: 'Plan limits',
            rows: [
              {
                label: 'Users included',
                values: {
                  free: '1',
                  pro: '1',
                  team: 'Up to 5',
                  business: 'Up to 20',
                },
              },
            ],
          },
          {
            title: 'Additional capabilities',
            rows: [
              {
                label: 'Role-based access control',
                values: {
                  free: false,
                  pro: false,
                  team: true,
                  business: true,
                },
              },
            ],
          },
        ],
      },
      dedicated: {
        label: 'Dedicated',
        lead: 'Self-hosted and dedicated editions.',
        plans: [
          {
            id: 'opensource',
            name: 'Open Source',
            price_monthly: 0,
            price_annually: 0,
            price_label: '$0',
            tagline: 'Run the Apache 2.0 edition on your own infrastructure.',
            cta_text: 'Explore the open-source edition',
            cta_url: 'https://github.com/preloop/preloop',
            features: [],
          },
          {
            id: 'business-selfhosted',
            name: 'Business',
            price_monthly: null,
            price_annually: null,
            price_label: 'Contact us',
            tagline: 'A limited introduction for teams that self-operate.',
            cta_text: 'Contact us',
            cta_url: '/request-demo',
            features: [],
          },
          {
            id: 'enterprise',
            name: 'Enterprise',
            price_monthly: null,
            price_annually: null,
            price_label: 'from $30k/yr',
            tagline: 'Dedicated or self-hosted, up to 100 users.',
            cta_text: 'Contact us',
            cta_url: '/request-demo',
            features: [],
          },
        ],
        comparison: {
          title: 'Compare dedicated editions',
          note: 'Preloop core is Apache 2.0 and free to self-host.',
          groups: [
            {
              title: 'Edition',
              rows: [
                {
                  label: 'Deployment',
                  values: {
                    opensource: 'Self-hosted, Apache 2.0',
                    'business-selfhosted': 'Self-hosted',
                    enterprise: 'Dedicated or self-hosted',
                  },
                },
              ],
            },
            {
              title: 'Operating the deployment',
              rows: [
                {
                  label: 'Price',
                  values: {
                    opensource: 'Free',
                    'business-selfhosted': 'Contact us',
                    enterprise: 'From $30,000 per year',
                  },
                },
              ],
            },
          ],
        },
      },
      faqs: [{ q: 'Is it per seat or per plan?', a: 'Per plan.' }],
    },
  },
} as unknown as BrandConfig;

/** Parse the SSR output the way a crawler with no JavaScript would. */
function render(config: BrandConfig = CONFIG) {
  const html = generatePricingSlottedContent(config);
  const doc = new DOMParser().parseFromString(
    `<!doctype html><html><body>${html}</body></html>`,
    'text/html'
  );
  return {
    html,
    doc,
    text: doc.body.textContent?.replace(/\s+/g, ' ').trim() || '',
  };
}

describe('Server-rendered pricing (light DOM)', () => {
  it('names every plan on both tabs', () => {
    const { text } = render();
    for (const name of [
      'Free',
      'Pro',
      'Team',
      'Business',
      'Open Source',
      'Enterprise',
    ]) {
      expect(text, name).to.contain(name);
    }
    expect(text).to.contain('Cloud');
    expect(text).to.contain('Dedicated');
  });

  it('prints both billing periods so neither price needs a click', () => {
    const { text } = render();
    // Monthly price, effective monthly rate, and the annual total.
    expect(text).to.contain('$12');
    expect(text).to.contain('$10');
    expect(text).to.contain('$120');
    expect(text).to.contain('$1,200');
    expect(text).to.contain('$3,500');
    expect(text).to.contain('billed monthly');
    expect(text).to.contain('billed annually ($120/yr)');
    expect(text).to.contain('about $292/mo, billed annually');
  });

  it('renders both comparison tables as real tables', () => {
    const { doc, text } = render();
    expect(text).to.contain('Compare cloud plans');
    expect(text).to.contain('Compare dedicated editions');

    const tables = Array.from(doc.querySelectorAll('table'));
    expect(tables.length).to.equal(2);

    const headers = tables.map((t) =>
      Array.from(t.querySelectorAll('thead th')).map((th) =>
        th.textContent?.trim()
      )
    );
    expect(headers[0]).to.deep.equal(['', 'Free', 'Pro', 'Team', 'Business']);
    expect(headers[1]).to.deep.equal([
      '',
      'Open Source',
      'Business',
      'Enterprise',
    ]);
  });

  it('carries at least one row label from every group of both tables', () => {
    const { text } = render();
    for (const label of [
      'Plan limits',
      'Users included',
      'Additional capabilities',
      'Role-based access control',
      'Edition',
      'Deployment',
      'Operating the deployment',
      'Price',
    ]) {
      expect(text, label).to.contain(label);
    }
    // Booleans become words, never "true"/"false".
    expect(text).to.contain('Included');
    expect(text).to.contain('Not included');
  });

  it('keeps the Cloud section before the Dedicated section', () => {
    const { html } = render();
    expect(html.indexOf('pricing-tab-cloud')).to.be.greaterThan(-1);
    expect(html.indexOf('pricing-tab-cloud')).to.be.lessThan(
      html.indexOf('pricing-tab-dedicated')
    );
  });

  it('links every card CTA with a real href', () => {
    const { doc } = render();
    const ctas = Array.from(doc.querySelectorAll('a.plan-cta'));
    expect(ctas.length).to.equal(7);
    const github = ctas.find(
      (a) => a.getAttribute('href') === 'https://github.com/preloop/preloop'
    );
    expect(github, 'open-source CTA').to.exist;
    expect(github?.getAttribute('rel')).to.equal('noopener noreferrer');
    expect(
      ctas.filter((a) => a.getAttribute('href') === '/request-demo').length
    ).to.equal(2);
  });

  it('tags each card with the tab it belongs to so hydration keeps the split', () => {
    const { doc } = render();
    const dedicated = Array.from(
      doc.querySelectorAll('[slot^="dedicated-plan-"]')
    );
    expect(dedicated.length).to.equal(3);
    for (const el of dedicated) {
      expect(el.getAttribute('data-deployment')).to.equal('dedicated');
    }
    expect(doc.querySelectorAll('[slot^="plan-"]').length).to.equal(4);
  });

  it('never prints the same price sentence twice for a quoted edition', () => {
    const { doc } = render();
    const enterprise = doc.querySelector('[data-plan-id="enterprise"]');
    const periods = enterprise?.querySelectorAll('.price-period') || [];
    expect(periods.length).to.equal(1);
    expect(periods[0].textContent?.trim()).to.equal('from $30k/yr');
  });

  it('falls back to the tagged plans when a brand ships no dedicated block', () => {
    const pricing = (CONFIG as any).landing.pricing;
    const fallback = {
      ...CONFIG,
      landing: {
        pricing: {
          ...pricing,
          dedicated: undefined,
          plans: [
            ...pricing.plans,
            { ...pricing.dedicated.plans[2], deployment: 'dedicated' },
          ],
        },
      },
    } as unknown as BrandConfig;
    const { doc, text } = render(fallback);
    expect(doc.querySelectorAll('[slot^="dedicated-plan-"]').length).to.equal(
      1
    );
    expect(text).to.contain('Enterprise');
    expect(text).to.contain('from $30k/yr');
  });

  it('falls back to the same table titles the hydrated view uses when title is omitted', () => {
    const pricing = (CONFIG as any).landing.pricing;
    const untitled = {
      ...CONFIG,
      landing: {
        pricing: {
          ...pricing,
          comparison: { groups: pricing.comparison.groups },
          dedicated: {
            ...pricing.dedicated,
            comparison: { groups: pricing.dedicated.comparison.groups },
          },
        },
      },
    } as unknown as BrandConfig;
    const { text } = render(untitled);
    expect(text).to.contain('Compare cloud plans');
    expect(text).to.contain('Compare dedicated editions');
  });

  it('renders nothing dedicated for a cloud-only brand', () => {
    const pricing = (CONFIG as any).landing.pricing;
    const cloudOnly = {
      ...CONFIG,
      landing: { pricing: { ...pricing, dedicated: undefined } },
    } as unknown as BrandConfig;
    const { doc, text } = render(cloudOnly);
    expect(doc.querySelectorAll('table').length).to.equal(1);
    expect(text).to.not.contain('Compare dedicated editions');
  });
});
