import { html, fixture, expect } from '@open-wc/testing';
import sinon from 'sinon';
import './landing-view';
import { LandingView } from './landing-view';

const BRAND_CONFIG: any = {
  name: 'Test Brand',
  domain: 'test.example.com',
  edition: 'saas',
  company: { legal_name: 'Test Co', address: '123 Test', city: 'Test' },
  branding: {
    logo_light: '/logo.svg',
    logo_dark: '/logo-dark.svg',
    favicon: '/favicon.ico',
    primary_color: '#000',
    gradient_product: '',
    gradient_ai: '',
  },
  social: { twitter: '', linkedin: '', instagram: '' },
};

const tick = (ms = 150) => new Promise((r) => setTimeout(r, ms));

const JSON_CONTENT = {
  hero: {
    title: 'JSON Hero Title',
    lead: 'JSON hero lead copy.',
    cta_primary: 'Get Started',
  },
  features: [{ title: 'Feature One', text: 'Does things.' }],
  faqs: [{ q: 'Question?', a: 'Answer.' }],
};

function stubFetch(content: unknown = JSON_CONTENT) {
  return sinon
    .stub(window, 'fetch')
    .callsFake(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/landing-content.json')) {
        return new Response(JSON.stringify(content), { status: 200 });
      }
      if (url.includes('/api/v1/features')) {
        return new Response(JSON.stringify({ features: {} }), { status: 200 });
      }
      return new Response('{}', { status: 200 });
    });
}

describe('LandingView', () => {
  let fetchStub: sinon.SinonStub;

  beforeEach(() => {
    (window as any).BRAND_CONFIG = BRAND_CONFIG;
  });

  afterEach(() => {
    fetchStub.restore();
    delete (window as any).BRAND_CONFIG;
  });

  it('keeps slotted hero marketing copy in the light DOM (crawlable)', async () => {
    fetchStub = stubFetch();
    const el = (await fixture(html`
      <landing-view>
        <h1 slot="hero-title">Govern Every AI Agent</h1>
        <p slot="hero-lead">The MCP governance layer for teams.</p>
      </landing-view>
    `)) as LandingView;
    await tick();
    await el.updateComplete;

    // Slotted content remains in the light DOM where crawlers can read it.
    const lightTitle = el.querySelector('[slot="hero-title"]');
    expect(lightTitle, 'hero title in light DOM').to.exist;
    expect(lightTitle?.textContent).to.contain('Govern Every AI Agent');
    // And the component projects it into its rendered hero.
    expect((el as any)._heroTitle).to.contain('Govern Every AI Agent');
  });

  it('renders the default get-started marketing section', async () => {
    fetchStub = stubFetch();
    const el = (await fixture(
      html`<landing-view></landing-view>`
    )) as LandingView;
    await tick();
    await el.updateComplete;
    expect(el.shadowRoot?.querySelector('#get-started')).to.exist;
    expect(el.shadowRoot?.textContent).to.contain(
      'Turbocharge your AI Workflow with MCP'
    );
  });

  it('loads hero and feature content from JSON on client navigation', async () => {
    fetchStub = stubFetch();
    const el = (await fixture(
      html`<landing-view></landing-view>`
    )) as LandingView;
    await tick();
    await el.updateComplete;
    expect((el as any)._heroTitle).to.equal('JSON Hero Title');
    expect((el as any)._featureSlides.length).to.equal(1);
    expect(
      fetchStub
        .getCalls()
        .some((c) => String(c.args[0]).includes('/landing-content.json'))
    ).to.be.true;
  });

  it('handles empty content without rendering feature cards', async () => {
    fetchStub = stubFetch({ hero: {}, features: [], faqs: [] });
    const el = (await fixture(
      html`<landing-view></landing-view>`
    )) as LandingView;
    await tick();
    await el.updateComplete;
    expect((el as any)._featureSlides.length).to.equal(0);
    expect((el as any)._faqs.length).to.equal(0);
  });
});
