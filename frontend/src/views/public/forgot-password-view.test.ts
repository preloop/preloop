import { html, fixture, expect } from '@open-wc/testing';
import sinon from 'sinon';
import './forgot-password-view';
import { ForgotPasswordView } from './forgot-password-view';

const BRAND_CONFIG: any = {
  name: 'Test Brand',
  domain: 'test.example.com',
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

describe('ForgotPasswordView', () => {
  let el: ForgotPasswordView;
  let fetchStub: sinon.SinonStub;

  beforeEach(async () => {
    (window as any).BRAND_CONFIG = BRAND_CONFIG;
    el = (await fixture(
      html`<forgot-password-view></forgot-password-view>`
    )) as ForgotPasswordView;
    fetchStub = sinon.stub(window, 'fetch');
  });

  afterEach(() => {
    fetchStub.restore();
    delete (window as any).BRAND_CONFIG;
  });

  async function submit(value: string) {
    const input = el.shadowRoot?.querySelector<any>('sl-input[name="email"]');
    input.value = value;
    await el.updateComplete;
    const form = el.shadowRoot?.querySelector('form') as HTMLFormElement;
    form.dispatchEvent(
      new SubmitEvent('submit', { bubbles: true, cancelable: true })
    );
    await tick();
    await el.updateComplete;
  }

  it('renders the email form with a submit button', () => {
    expect(el.shadowRoot?.querySelector('form')).to.exist;
    expect(el.shadowRoot?.querySelector('sl-input[name="email"]')).to.exist;
    expect(el.shadowRoot?.querySelector('sl-button[type="submit"]')).to.exist;
  });

  it('has a link back to sign in', () => {
    expect(el.shadowRoot?.querySelector('a[href="/login"]')).to.exist;
  });

  it('shows a success alert and posts to the endpoint on submit', async () => {
    fetchStub.resolves(
      new Response(JSON.stringify({ ok: true }), { status: 200 })
    );
    await submit('user@example.com');
    expect(el.shadowRoot?.querySelector('sl-alert[variant="success"]')).to
      .exist;
    expect(String(fetchStub.firstCall.args[0])).to.contain(
      '/api/v1/auth/forgot-password'
    );
  });

  it('shows a danger alert when the request fails', async () => {
    fetchStub.resolves(
      new Response(JSON.stringify({ detail: 'Service unavailable' }), {
        status: 500,
      })
    );
    await submit('user@example.com');
    expect(el.shadowRoot?.querySelector('sl-alert[variant="danger"]')).to.exist;
  });
});
