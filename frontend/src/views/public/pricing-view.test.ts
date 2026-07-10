import { html, fixture, expect } from '@open-wc/testing';
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
