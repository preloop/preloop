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
