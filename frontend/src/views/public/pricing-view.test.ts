import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';
import './pricing-view';
import { PublicPricingView } from './pricing-view';

const tick = (ms = 150) => new Promise((r) => setTimeout(r, ms));

const CONTENT = {
  pricing: {
    title: 'Simple Pricing',
    lead: 'Pick a plan that fits.',
    billing_toggle: true,
    plans: [
      {
        id: 'teams',
        name: 'Teams',
        price_monthly: 20,
        price_annually: 200,
        features: ['Feature A', 'Feature B'],
        badge: 'Popular',
      },
      {
        id: 'enterprise',
        name: 'Enterprise',
        price_monthly: null,
        price_annually: null,
        features: ['Everything'],
      },
    ],
    faqs: [{ q: 'Is there a trial?', a: 'Yes, 14 days.' }],
  },
};

/**
 * The 2026 ladder as the page now presents it: four cloud plans and their
 * comparison table on the Cloud tab, three editions and their own comparison
 * table on the Dedicated tab. Same layout on both, different columns.
 */
const LADDER_CONTENT = {
  pricing: {
    title: 'Pricing',
    lead: 'Choose Cloud for hosted subscriptions or Dedicated for self-managed options.',
    billing_toggle: true,
    plans: [
      {
        id: 'free',
        name: 'Free',
        price_monthly: 0,
        price_annually: 0,
        deployment: 'cloud',
        tagline: 'Try Preloop with your own keys.',
        features: [],
      },
      {
        id: 'pro',
        name: 'Pro',
        price_monthly: 12,
        price_annually: 120,
        deployment: 'cloud',
        tagline: 'You and all your agents.',
        features: [],
      },
      {
        id: 'team',
        name: 'Team',
        price_monthly: 120,
        price_annually: 1200,
        deployment: 'cloud',
        tagline: 'Up to 5 people, every agent governed.',
        features: [],
      },
      {
        id: 'business',
        name: 'Business',
        price_monthly: 350,
        price_annually: 3500,
        deployment: 'cloud',
        tagline: 'Up to 20 people, scale usage.',
        features: [],
      },
    ],
    comparison: {
      title: 'Compare cloud plans',
      note: 'Governance never stops.',
      groups: [
        {
          title: 'Plan',
          rows: [
            {
              label: 'Agents',
              values: {
                free: '3',
                pro: 'Unlimited',
                team: 'Unlimited',
                business: 'Unlimited',
              },
            },
          ],
        },
        {
          title: 'Optimize',
          rows: [
            {
              label: 'AI session optimization',
              values: { free: false, pro: true, team: true, business: true },
            },
          ],
        },
      ],
    },
    dedicated: {
      label: 'Dedicated',
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
        note: 'Editions are quoted, not bought through checkout.',
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
            title: 'Additional capabilities',
            rows: [
              {
                label: 'Role-based access control',
                values: {
                  opensource: false,
                  'business-selfhosted': true,
                  enterprise: true,
                },
              },
            ],
          },
        ],
      },
    },
    faqs: [],
  },
};

/** Click the Cloud/Dedicated tab and let the view settle. */
async function selectTab(
  el: PublicPricingView,
  tab: 'cloud' | 'dedicated'
): Promise<void> {
  const toggle = el.shadowRoot?.querySelector('deployment-toggle') as
    HTMLElement | undefined;
  const button = toggle?.shadowRoot?.querySelector(
    `.tab-${tab}`
  ) as HTMLElement | null;
  button?.click();
  await el.updateComplete;
}

function stubLadderFetch() {
  return sinon
    .stub(window, 'fetch')
    .callsFake(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/landing-content.json')) {
        return new Response(JSON.stringify(LADDER_CONTENT), { status: 200 });
      }
      return new Response(JSON.stringify({ features: {} }), { status: 200 });
    });
}

function stubFetch(features: Record<string, boolean> = { billing: false }) {
  return sinon
    .stub(window, 'fetch')
    .callsFake(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/landing-content.json')) {
        return new Response(JSON.stringify(CONTENT), { status: 200 });
      }
      if (url.includes('/api/v1/features')) {
        return new Response(JSON.stringify({ features }), { status: 200 });
      }
      return new Response('{}', { status: 200 });
    });
}

describe('PublicPricingView', () => {
  let fetchStub: sinon.SinonStub;

  afterEach(() => {
    fetchStub.restore();
  });

  it('preserves nested SSR copy for both tabs without refetching', async () => {
    fetchStub = stubFetch();
    const el = await fixture<PublicPricingView>(
      html`<public-pricing-view
        ><article>
          <header
            slot="pricing-heading"
            data-title="Cloud pricing"
            data-lead="USD, excluding tax. Self-hosted is separate."
            data-billing-toggle="false"
          ></header>
          <section>
            <div
              slot="plan-0"
              data-plan-id="free"
              data-plan-name="Free"
              data-price-monthly="0"
              data-price-annually="0"
            ></div>
          </section>
          <section>
            <div
              slot="dedicated-plan-0"
              data-plan-id="business-selfhosted"
              data-plan-name="Business Self-Hosted"
              data-price-monthly=""
              data-price-annually=""
              data-price-label="Contact us"
              data-tagline="Pilot by agreement"
              data-cta-text="Contact us"
              data-cta-url="/request-demo"
              data-deployment="dedicated"
            ></div>
          </section></article
      ></public-pricing-view>`
    );
    await waitUntil(() => (el as any)._loaded);
    await el.updateComplete;
    expect(el.shadowRoot?.textContent)
      .to.include('Cloud pricing')
      .and.include('USD, excluding tax. Self-hosted is separate.');
    await selectTab(el, 'dedicated');
    const card = el.shadowRoot?.querySelector('pricing-card') as HTMLElement;
    expect(card.shadowRoot?.textContent)
      .to.include('Business Self-Hosted')
      .and.include('Pilot by agreement');
    expect(
      fetchStub
        .getCalls()
        .some((c) => String(c.args[0]).includes('landing-content.json'))
    ).to.equal(false);
  });

  it('hydrates a dedicated-only slotted page without refetching JSON', async () => {
    fetchStub = stubFetch();
    const el = await fixture<PublicPricingView>(
      html`<public-pricing-view
        ><article>
          <header
            slot="pricing-heading"
            data-title="Dedicated only"
            data-lead="Self-hosted editions."
          ></header>
          <section>
            <div
              slot="dedicated-plan-0"
              data-plan-id="opensource"
              data-plan-name="Open Source"
              data-price-monthly="0"
              data-price-annually="0"
              data-price-label="$0"
              data-tagline="Run it yourself."
              data-cta-text="Explore"
              data-cta-url="https://github.com/preloop/preloop"
              data-deployment="dedicated"
            ></div>
          </section></article
      ></public-pricing-view>`
    );
    await waitUntil(() => (el as any)._loaded);
    await el.updateComplete;
    expect(el.shadowRoot?.textContent)
      .to.include('Dedicated only')
      .and.include('Self-hosted editions.');
    const card = el.shadowRoot?.querySelector('pricing-card') as HTMLElement;
    expect(card, 'dedicated card hydrates from dedicated-plan slots').to.exist;
    expect(card.shadowRoot?.textContent)
      .to.include('Open Source')
      .and.include('Run it yourself.');
    expect(
      fetchStub
        .getCalls()
        .some((c) => String(c.args[0]).includes('landing-content.json'))
    ).to.equal(false);
  });

  it('loads plans from JSON and renders the hero copy and cards', async () => {
    fetchStub = stubFetch();
    const el = (await fixture(
      html`<public-pricing-view></public-pricing-view>`
    )) as PublicPricingView;
    await tick();
    await el.updateComplete;
    expect(el.shadowRoot?.textContent).to.contain('Simple Pricing');
    expect(el.shadowRoot?.textContent).to.contain('Pick a plan that fits.');
    expect(el.shadowRoot?.querySelectorAll('pricing-card').length).to.equal(2);
  });

  it('renders the FAQ section from JSON', async () => {
    fetchStub = stubFetch();
    const el = (await fixture(
      html`<public-pricing-view></public-pricing-view>`
    )) as PublicPricingView;
    await tick();
    await el.updateComplete;
    expect(el.shadowRoot?.querySelector('.pricing-faq')).to.exist;
    expect(el.shadowRoot?.textContent).to.contain('Is there a trial?');
  });

  it('sends logged-out visitors to /register for the teams plan (card-free signup)', async () => {
    fetchStub = stubFetch({ billing: true, oauth_signin: true });
    localStorage.removeItem('accessToken');
    const el = (await fixture(
      html`<public-pricing-view></public-pricing-view>`
    )) as PublicPricingView;
    await tick();
    await el.updateComplete;
    const navStub = sinon.stub(el as any, '_navigate');
    // The teams CTA never creates a checkout session for anonymous visitors:
    // signup is card-free; checkout is the in-product upgrade door.
    await (el as any)._handleSignUp('teams');
    const checkoutCalls = fetchStub
      .getCalls()
      .filter((c) => String(c.args[0]).includes('create-checkout-session'));
    expect(checkoutCalls.length).to.equal(0);
    expect(navStub.calledOnceWith('/register')).to.be.true;
  });

  it('opens on Cloud with four cards and a four-column comparison table', async () => {
    fetchStub = stubLadderFetch();
    const el = (await fixture(
      html`<public-pricing-view></public-pricing-view>`
    )) as PublicPricingView;
    await tick();
    await el.updateComplete;

    const cards = Array.from(
      el.shadowRoot?.querySelectorAll('pricing-card') || []
    );
    expect(cards.length).to.equal(4);
    expect(cards.map((c) => (c as any).plan.id)).to.deep.equal([
      'free',
      'pro',
      'team',
      'business',
    ]);
    const toggle = el.shadowRoot?.querySelector('deployment-toggle') as
      HTMLElement | undefined;
    const cloudBtn = toggle?.shadowRoot?.querySelector(
      '.tab-cloud'
    ) as HTMLButtonElement | null;
    expect(cloudBtn?.tagName).to.equal('BUTTON');
    expect(cloudBtn?.getAttribute('aria-pressed')).to.equal('true');

    const table = el.shadowRoot?.querySelector('.comparison-table');
    expect(table, 'comparison table renders').to.exist;
    // One header column per cloud plan plus the leading label column.
    const headers = Array.from(table?.querySelectorAll('thead th') || []);
    expect(headers.length).to.equal(5);
    expect(headers.map((h) => h.textContent?.trim())).to.deep.equal([
      '',
      'Free',
      'Pro',
      'Team',
      'Business',
    ]);
    expect(table?.textContent).to.contain('Agents');
    expect(table?.textContent).to.contain('Unlimited');
    expect(el.shadowRoot?.textContent).to.contain('Governance never stops.');
  });

  it('lets the period change the prices only, never the plans or the rows', async () => {
    fetchStub = stubLadderFetch();
    const el = (await fixture(
      html`<public-pricing-view></public-pricing-view>`
    )) as PublicPricingView;
    await tick();
    await el.updateComplete;

    const snapshot = () => {
      const cards = Array.from(
        el.shadowRoot?.querySelectorAll('pricing-card') || []
      );
      const table = el.shadowRoot?.querySelector('.comparison-table');
      return {
        ids: cards.map((c) => (c as any).plan.id),
        rows: Array.from(table?.querySelectorAll('tbody th') || []).map((th) =>
          th.textContent?.trim()
        ),
        prices: cards.map(
          (c) =>
            (c as HTMLElement).shadowRoot
              ?.querySelector('.price-main')
              ?.textContent?.trim() || ''
        ),
      };
    };

    // The fixture opens on Yearly: Pro's 120/yr divides exactly, so the card
    // leads with the effective monthly rate.
    const yearly = snapshot();
    expect(yearly.prices).to.deep.equal(['$0', '$10', '$100', '$3,500']);

    const toggle = el.shadowRoot?.querySelector('billing-toggle') as
      HTMLElement | undefined;
    const monthlyBtn = toggle?.shadowRoot?.querySelectorAll('sl-button')[0] as
      HTMLElement | undefined;
    monthlyBtn?.click();
    await el.updateComplete;
    await Promise.all(
      Array.from(el.shadowRoot?.querySelectorAll('pricing-card') || []).map(
        (c) => (c as any).updateComplete
      )
    );

    const monthly = snapshot();
    expect(monthly.prices).to.deep.equal(['$0', '$12', '$120', '$350']);
    // Everything except the numbers is untouched by the period.
    expect(monthly.ids).to.deep.equal(yearly.ids);
    expect(monthly.rows).to.deep.equal(yearly.rows);
  });

  it('keeps the quoted Enterprise plan out of the cloud cards and columns', async () => {
    fetchStub = stubLadderFetch();
    const el = (await fixture(
      html`<public-pricing-view></public-pricing-view>`
    )) as PublicPricingView;
    await tick();
    await el.updateComplete;

    const table = el.shadowRoot?.querySelector('.comparison-table');
    expect(table?.textContent).to.not.contain('Enterprise');
    const cards = Array.from(
      el.shadowRoot?.querySelectorAll('pricing-card') || []
    );
    expect(cards.some((c) => (c as any).plan.id === 'enterprise')).to.equal(
      false
    );
    // Every body row has exactly one cell per cloud plan, so no column is
    // left empty by a plan that has no quota to state.
    const bodyRows = Array.from(
      table?.querySelectorAll('tbody tr:not(.group-row)') || []
    );
    expect(bodyRows.length).to.be.greaterThan(0);
    for (const row of bodyRows) {
      expect(row.querySelectorAll('td').length).to.equal(4);
    }
  });

  it('switches to Dedicated: same layout, different columns, no period toggle', async () => {
    fetchStub = stubLadderFetch();
    const el = (await fixture(
      html`<public-pricing-view></public-pricing-view>`
    )) as PublicPricingView;
    await tick();
    await el.updateComplete;
    expect(el.shadowRoot?.querySelector('billing-toggle'), 'cloud has a period')
      .to.exist;

    await selectTab(el, 'dedicated');

    // Same card row as Cloud, three editions instead of four subscriptions.
    const cards = Array.from(
      el.shadowRoot?.querySelectorAll('pricing-card') || []
    );
    expect(cards.map((c) => (c as any).plan.id)).to.deep.equal([
      'opensource',
      'business-selfhosted',
      'enterprise',
    ]);
    expect((cards[0] as HTMLElement).shadowRoot?.textContent).to.contain('$0');
    expect((cards[2] as HTMLElement).shadowRoot?.textContent).to.contain(
      'from $30k/yr'
    );

    // Same comparison table, with the edition columns.
    const table = el.shadowRoot?.querySelector('.comparison-table');
    expect(table, 'dedicated table renders in the same format').to.exist;
    const headers = Array.from(table?.querySelectorAll('thead th') || []);
    expect(headers.map((h) => h.textContent?.trim())).to.deep.equal([
      '',
      'Open Source',
      'Business',
      'Enterprise',
    ]);
    expect(el.shadowRoot?.textContent).to.contain('Compare dedicated editions');
    expect(table?.textContent).to.contain('Self-hosted, Apache 2.0');
    expect(table?.querySelector('.cross-mark'), 'excluded mark').to.exist;

    // Nothing on this tab is priced per month, so the period toggle goes.
    expect(el.shadowRoot?.querySelector('billing-toggle')).to.not.exist;

    const toggle = el.shadowRoot?.querySelector('deployment-toggle') as
      HTMLElement | undefined;
    const cloudBtn = toggle?.shadowRoot?.querySelector(
      '.tab-cloud'
    ) as HTMLButtonElement | null;
    const dedicatedBtn = toggle?.shadowRoot?.querySelector(
      '.tab-dedicated'
    ) as HTMLButtonElement | null;
    expect(cloudBtn?.getAttribute('aria-pressed')).to.equal('false');
    expect(dedicatedBtn?.getAttribute('aria-pressed')).to.equal('true');
  });

  it('sends a dedicated card CTA to its own link, never to checkout', async () => {
    fetchStub = stubLadderFetch();
    localStorage.setItem('accessToken', 'token');
    const el = (await fixture(
      html`<public-pricing-view></public-pricing-view>`
    )) as PublicPricingView;
    await tick();
    await el.updateComplete;
    const navStub = sinon.stub(el as any, '_navigate');
    const openStub = sinon.stub(window, 'open');
    try {
      await (el as any)._handleSignUp('business-selfhosted');
      expect(navStub.calledOnceWith('/request-demo')).to.be.true;
      await (el as any)._handleSignUp('opensource');
      expect(
        openStub.calledOnceWith(
          'https://github.com/preloop/preloop',
          '_blank',
          'noopener,noreferrer'
        )
      ).to.be.true;
      expect(
        fetchStub
          .getCalls()
          .some((c) => String(c.args[0]).includes('create-checkout-session'))
      ).to.equal(false);
    } finally {
      openStub.restore();
      localStorage.removeItem('accessToken');
    }
  });

  it('switches back to Cloud and restores the cards and the period toggle', async () => {
    fetchStub = stubLadderFetch();
    const el = (await fixture(
      html`<public-pricing-view></public-pricing-view>`
    )) as PublicPricingView;
    await tick();
    await el.updateComplete;

    await selectTab(el, 'dedicated');
    await selectTab(el, 'cloud');

    expect(el.shadowRoot?.querySelectorAll('pricing-card').length).to.equal(4);
    expect(el.shadowRoot?.querySelector('billing-toggle')).to.exist;
    expect(el.shadowRoot?.textContent).to.contain('Compare cloud plans');
    expect(el.shadowRoot?.textContent).to.not.contain(
      'Compare dedicated editions'
    );
  });

  it('renders booleans in the comparison table as marks, not as text', async () => {
    fetchStub = stubLadderFetch();
    const el = (await fixture(
      html`<public-pricing-view></public-pricing-view>`
    )) as PublicPricingView;
    await tick();
    await el.updateComplete;

    const table = el.shadowRoot?.querySelector('.comparison-table');
    expect(table?.querySelector('.check-mark'), 'included mark').to.exist;
    expect(table?.querySelector('.cross-mark'), 'excluded mark').to.exist;
    // "true"/"false" must never leak into the rendered copy.
    expect(table?.textContent).to.not.contain('true');
    expect(table?.textContent).to.not.contain('false');
  });

  it('never advertises a per-user unit: pricing is per bracket', async () => {
    fetchStub = stubLadderFetch();
    const el = (await fixture(
      html`<public-pricing-view></public-pricing-view>`
    )) as PublicPricingView;
    await tick();
    await el.updateComplete;

    const cards = Array.from(
      el.shadowRoot?.querySelectorAll('pricing-card') || []
    );
    for (const card of cards) {
      const text = (card as HTMLElement).shadowRoot?.textContent || '';
      expect(text, 'no per-seat unit on the card').to.not.contain('/user');
    }
  });

  it('routes existing accounts to comparison before any paid-plan mutation', async () => {
    fetchStub = stubLadderFetch();
    const el = await fixture<PublicPricingView>(
      html`<public-pricing-view></public-pricing-view>`
    );
    await waitUntil(() => (el as any)._loaded);
    localStorage.setItem('accessToken', 'test-token');
    const navigate = sinon.stub(el as any, '_navigate');
    await (el as any)._handleSignUp('pro');
    expect(
      navigate.calledOnceWith(
        '/console/settings/account?plan=pro&interval=year'
      )
    ).to.equal(true);
    expect(
      fetchStub
        .getCalls()
        .some((c) => String(c.args[0]).includes('create-checkout-session'))
    ).to.equal(false);
    localStorage.removeItem('accessToken');
  });

  it('routes an existing account considering Free to comparison, never checkout', async () => {
    fetchStub = stubLadderFetch();
    localStorage.setItem('accessToken', 'token');
    const el = (await fixture(
      html`<public-pricing-view></public-pricing-view>`
    )) as PublicPricingView;
    await tick();
    await el.updateComplete;
    const navStub = sinon.stub(el as any, '_navigate');
    try {
      await (el as any)._handleSignUp('free');
      const checkoutCalls = fetchStub
        .getCalls()
        .filter((c) => String(c.args[0]).includes('create-checkout-session'));
      expect(checkoutCalls.length).to.equal(0);
      expect(
        navStub.calledOnceWith(
          '/console/settings/account?plan=free&interval=year'
        )
      ).to.be.true;
    } finally {
      localStorage.removeItem('accessToken');
    }
  });

  it('opens an external enterprise CTA in a new tab with noopener', async () => {
    const content = structuredClone(LADDER_CONTENT);
    content.pricing.dedicated.plans[2].cta_url = 'https://example.com/contact';
    fetchStub = sinon
      .stub(window, 'fetch')
      .callsFake(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes('/landing-content.json')) {
          return new Response(JSON.stringify(content), { status: 200 });
        }
        return new Response(JSON.stringify({ features: {} }), { status: 200 });
      });
    const el = (await fixture(
      html`<public-pricing-view></public-pricing-view>`
    )) as PublicPricingView;
    await tick();
    await el.updateComplete;
    const navStub = sinon.stub(el as any, '_navigate');
    const openStub = sinon.stub(window, 'open');
    try {
      await (el as any)._handleSignUp('enterprise');
      expect(navStub.called).to.equal(false);
      expect(
        openStub.calledOnceWith(
          'https://example.com/contact',
          '_blank',
          'noopener,noreferrer'
        )
      ).to.be.true;
    } finally {
      openStub.restore();
    }
  });

  it('prints the SSR comparison fallback titles when the brand omits title', async () => {
    const content = structuredClone(LADDER_CONTENT);
    delete (content.pricing.comparison as { title?: string }).title;
    delete (content.pricing.dedicated.comparison as { title?: string }).title;
    fetchStub = sinon
      .stub(window, 'fetch')
      .callsFake(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes('/landing-content.json')) {
          return new Response(JSON.stringify(content), { status: 200 });
        }
        return new Response(JSON.stringify({ features: {} }), { status: 200 });
      });
    const el = (await fixture(
      html`<public-pricing-view></public-pricing-view>`
    )) as PublicPricingView;
    await tick();
    await el.updateComplete;
    expect(el.shadowRoot?.textContent).to.contain('Compare cloud plans');
    expect(el.shadowRoot?.textContent).to.not.contain('Compare plans');
    await selectTab(el, 'dedicated');
    expect(el.shadowRoot?.textContent).to.contain('Compare dedicated editions');
  });

  it('sends enterprise to the contact route, never to checkout', async () => {
    fetchStub = stubLadderFetch();
    localStorage.setItem('accessToken', 'token');
    const el = (await fixture(
      html`<public-pricing-view></public-pricing-view>`
    )) as PublicPricingView;
    await tick();
    await el.updateComplete;
    const navStub = sinon.stub(el as any, '_navigate');
    try {
      await (el as any)._handleSignUp('enterprise');
      const checkoutCalls = fetchStub
        .getCalls()
        .filter((c) => String(c.args[0]).includes('create-checkout-session'));
      expect(checkoutCalls.length).to.equal(0);
      expect(navStub.calledOnceWith('/request-demo')).to.be.true;
    } finally {
      localStorage.removeItem('accessToken');
    }
  });

  it('rehydrates the comparison table from SSR slotted markup', async () => {
    fetchStub = stubLadderFetch();
    const payload = JSON.stringify(LADDER_CONTENT.pricing.comparison);
    const el = (await fixture(html`
      <public-pricing-view>
        <div
          slot="plan-0"
          data-plan-id="pro"
          data-plan-name="Pro"
          data-price-monthly="10"
          data-price-annually="100"
          data-tagline="You and all your agents."
          data-features=""
        ></div>
        <section slot="comparison" data-comparison=${payload}></section>
      </public-pricing-view>
    `)) as PublicPricingView;
    await tick();
    await el.updateComplete;

    // Slotted content wins over the JSON fetch, so the crawler-visible copy
    // is exactly what the visitor ends up interacting with.
    expect((el as any)._plans.length).to.equal(1);
    expect(el.shadowRoot?.querySelector('.comparison-table')).to.exist;
    expect(el.shadowRoot?.textContent).to.contain('AI session optimization');
  });

  it('reads the tab each plan belongs to from the SSR markup', async () => {
    fetchStub = stubLadderFetch();
    const el = (await fixture(html`
      <public-pricing-view>
        <div
          slot="plan-0"
          data-plan-id="pro"
          data-plan-name="Pro"
          data-price-monthly="10"
          data-price-annually="100"
          data-deployment="cloud"
          data-features=""
        ></div>
        <div
          slot="plan-1"
          data-plan-id="enterprise"
          data-plan-name="Enterprise"
          data-price-monthly=""
          data-price-annually=""
          data-price-label="from $30k/yr"
          data-tagline="Dedicated or self-hosted, up to 100 users."
          data-deployment="dedicated"
          data-features=""
        ></div>
      </public-pricing-view>
    `)) as PublicPricingView;
    await tick();
    await el.updateComplete;

    // The server-rendered page and the hydrated one must show the same split.
    expect(el.shadowRoot?.querySelectorAll('pricing-card').length).to.equal(1);
    await selectTab(el, 'dedicated');
    const cards = Array.from(
      el.shadowRoot?.querySelectorAll('pricing-card') || []
    );
    expect(cards.map((c) => (c as any).plan.id)).to.deep.equal(['enterprise']);
    expect((cards[0] as HTMLElement).shadowRoot?.textContent).to.contain(
      'from $30k/yr'
    );
  });

  it('survives a malformed comparison payload without losing the cards', async () => {
    fetchStub = stubLadderFetch();
    const el = (await fixture(html`
      <public-pricing-view>
        <div
          slot="plan-0"
          data-plan-id="pro"
          data-plan-name="Pro"
          data-price-monthly="10"
          data-price-annually="100"
          data-features=""
        ></div>
        <section slot="comparison" data-comparison="{not json"></section>
      </public-pricing-view>
    `)) as PublicPricingView;
    await tick();
    await el.updateComplete;

    expect((el as any)._comparison).to.equal(null);
    expect(el.shadowRoot?.querySelectorAll('pricing-card').length).to.equal(1);
  });

  it('falls back to Dedicated when the cloud list is empty', async () => {
    fetchStub = sinon
      .stub(window, 'fetch')
      .callsFake(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes('/landing-content.json')) {
          return new Response(
            JSON.stringify({
              pricing: {
                title: 'Pricing',
                lead: 'Dedicated only.',
                billing_toggle: true,
                plans: [],
                dedicated: {
                  plans: [
                    {
                      id: 'opensource',
                      name: 'Open Source',
                      price_monthly: 0,
                      price_annually: 0,
                      price_label: '$0',
                      tagline: 'Run it yourself.',
                      cta_text: 'Explore',
                      cta_url: 'https://github.com/preloop/preloop',
                      features: [],
                    },
                  ],
                },
                faqs: [],
              },
            }),
            { status: 200 }
          );
        }
        return new Response(JSON.stringify({ features: {} }), { status: 200 });
      });
    const el = (await fixture(
      html`<public-pricing-view></public-pricing-view>`
    )) as PublicPricingView;
    await tick();
    await el.updateComplete;

    expect(el.shadowRoot?.querySelectorAll('pricing-card').length).to.equal(1);
    expect(el.shadowRoot?.querySelector('billing-toggle')).to.not.exist;
    const toggle = el.shadowRoot?.querySelector('deployment-toggle') as
      HTMLElement | undefined;
    const dedicatedBtn = toggle?.shadowRoot?.querySelector(
      '.tab-dedicated'
    ) as HTMLButtonElement | null;
    expect(dedicatedBtn?.getAttribute('aria-pressed')).to.equal('true');
  });

  it('falls back gracefully when content fails to load (no plans)', async () => {
    fetchStub = sinon
      .stub(window, 'fetch')
      .callsFake(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes('/landing-content.json')) {
          return new Response('nope', { status: 500 });
        }
        return new Response(JSON.stringify({ features: {} }), { status: 200 });
      });
    const el = (await fixture(
      html`<public-pricing-view></public-pricing-view>`
    )) as PublicPricingView;
    await tick();
    await el.updateComplete;
    expect((el as any)._plans.length).to.equal(0);
    expect(el.shadowRoot?.querySelector('pricing-card')).to.not.exist;
  });
});
