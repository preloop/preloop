import { html, fixture, expect } from '@open-wc/testing';
import sinon from 'sinon';
import './app-footer';
import type { AppFooter } from './app-footer';

const BRAND_CONFIG: Record<string, unknown> = {
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

function stubFetch(): sinon.SinonStub {
  return sinon
    .stub(window, 'fetch')
    .callsFake(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/api/v1/features')) {
        return new Response(JSON.stringify({ features: {} }), { status: 200 });
      }
      return new Response('{}', { status: 200 });
    });
}

describe('AppFooter legal disclaimer', () => {
  let fetchStub: sinon.SinonStub;

  afterEach(() => {
    fetchStub.restore();
    delete (window as unknown as { BRAND_CONFIG?: unknown }).BRAND_CONFIG;
  });

  it('renders p.legal-disclaimer from runtime BRAND_CONFIG when the property is unset', async () => {
    (
      window as unknown as { BRAND_CONFIG: Record<string, unknown> }
    ).BRAND_CONFIG = {
      ...BRAND_CONFIG,
      legal_disclaimer:
        'Preloop is not a law firm and does not provide legal advice.',
    };
    fetchStub = stubFetch();
    const el = (await fixture(html`<app-footer></app-footer>`)) as AppFooter;
    await el.updateComplete;

    const disclaimer = el.shadowRoot?.querySelector('p.legal-disclaimer');
    expect(disclaimer, 'disclaimer from runtime brand config').to.exist;
    expect((disclaimer?.textContent || '').trim()).to.equal(
      'Preloop is not a law firm and does not provide legal advice.'
    );
  });

  it('renders no disclaimer p when neither the property nor BRAND_CONFIG.legal_disclaimer is set', async () => {
    (
      window as unknown as { BRAND_CONFIG: Record<string, unknown> }
    ).BRAND_CONFIG = { ...BRAND_CONFIG };
    fetchStub = stubFetch();
    const el = (await fixture(html`<app-footer></app-footer>`)) as AppFooter;
    await el.updateComplete;

    expect(el.shadowRoot?.querySelector('p.legal-disclaimer')).to.equal(null);
  });
});

describe('AppFooter Compare block', () => {
  let fetchStub: sinon.SinonStub;
  const VS_PAGES = [
    { href: '/vs/aws-agentcore', label: 'vs AWS AgentCore' },
    { href: '/vs/trigger-dev', label: 'vs Trigger.dev' },
  ];

  afterEach(() => {
    fetchStub.restore();
    delete (window as unknown as { BRAND_CONFIG?: unknown }).BRAND_CONFIG;
  });

  async function renderWith(config: Record<string, unknown>) {
    (
      window as unknown as { BRAND_CONFIG: Record<string, unknown> }
    ).BRAND_CONFIG = config;
    fetchStub = stubFetch();
    const el = (await fixture(html`<app-footer></app-footer>`)) as AppFooter;
    await el.updateComplete;
    return el;
  }

  it('renders one link per vs_pages entry on SaaS builds', async () => {
    const el = await renderWith({ ...BRAND_CONFIG, vs_pages: VS_PAGES });
    const links = Array.from(
      el.shadowRoot?.querySelectorAll('nav.footer-compare a') ?? []
    ).map((a) => [a.getAttribute('href'), (a.textContent || '').trim()]);
    expect(links).to.deep.equal([
      ['/vs/aws-agentcore', 'vs AWS AgentCore'],
      ['/vs/trigger-dev', 'vs Trigger.dev'],
    ]);
  });

  it('omits the block when vs_pages is empty or absent', async () => {
    let el = await renderWith({ ...BRAND_CONFIG, vs_pages: [] });
    expect(el.shadowRoot?.querySelector('nav.footer-compare')).to.equal(null);
    fetchStub.restore();
    el = await renderWith({ ...BRAND_CONFIG });
    expect(el.shadowRoot?.querySelector('nav.footer-compare')).to.equal(null);
  });

  it('omits the block on self-hosted builds', async () => {
    const el = await renderWith({
      ...BRAND_CONFIG,
      edition: 'selfhosted',
      vs_pages: VS_PAGES,
    });
    expect(el.shadowRoot?.querySelector('nav.footer-compare')).to.equal(null);
  });
});
