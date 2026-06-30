import { html, fixture, expect } from '@open-wc/testing';
import sinon from 'sinon';
import './welcome-view';
import { WelcomeView } from './welcome-view';

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

describe('WelcomeView', () => {
  let fetchStub: sinon.SinonStub;

  beforeEach(() => {
    (window as any).BRAND_CONFIG = BRAND_CONFIG;
    localStorage.clear();
    fetchStub = sinon.stub(window, 'fetch');
  });

  afterEach(() => {
    fetchStub.restore();
    delete (window as any).BRAND_CONFIG;
    localStorage.clear();
  });

  async function mount(): Promise<WelcomeView> {
    return (await fixture(html`<welcome-view></welcome-view>`)) as WelcomeView;
  }

  it('shows an error when no account details are present in the URL', async () => {
    const el = await mount();
    await el.updateComplete;
    expect(el.shadowRoot?.querySelector('.error-message')).to.exist;
    expect(el.shadowRoot?.textContent).to.contain(
      'Could not retrieve your details'
    );
  });

  it('renders the password onboarding form for a new user', async () => {
    const el = await mount();
    (el as any)._username = 'bob';
    (el as any)._email = 'bob@example.com';
    (el as any)._error = '';
    await el.updateComplete;
    expect(el.shadowRoot?.querySelector('#password')).to.exist;
    expect(el.shadowRoot?.textContent).to.contain('Welcome to Test Brand');
  });

  it('rejects a password shorter than 8 characters without calling the API', async () => {
    const el = await mount();
    (el as any)._username = 'bob';
    (el as any)._email = 'bob@example.com';
    (el as any)._error = '';
    await el.updateComplete;
    const pw = el.shadowRoot?.querySelector('#password') as any;
    pw.value = 'short';
    await (el as any)._handleOnboardingSubmit(new Event('submit'));
    await el.updateComplete;
    expect((el as any)._error).to.contain('at least 8 characters');
    expect(
      fetchStub
        .getCalls()
        .some((c) => String(c.args[0]).includes('complete-onboarding'))
    ).to.be.false;
  });

  it('completes onboarding and stores tokens on success', async () => {
    fetchStub.callsFake(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('complete-onboarding')) {
        return new Response(
          JSON.stringify({
            access_token: 'acc-123',
            refresh_token: 'ref-123',
          }),
          { status: 200 }
        );
      }
      return new Response(JSON.stringify({ organization_name: 'Acme' }), {
        status: 200,
      });
    });
    const el = await mount();
    (el as any)._username = 'bob';
    (el as any)._email = 'bob@example.com';
    (el as any)._error = '';
    await el.updateComplete;
    const pw = el.shadowRoot?.querySelector('#password') as any;
    pw.value = 'longenough1';
    await (el as any)._handleOnboardingSubmit(new Event('submit'));
    await tick();
    await el.updateComplete;
    expect(localStorage.getItem('accessToken')).to.equal('acc-123');
    expect((el as any)._error).to.equal('');
    expect((el as any)._needsPassword).to.be.false;
  });
});
