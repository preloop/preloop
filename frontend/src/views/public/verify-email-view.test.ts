import { html, fixture, expect } from '@open-wc/testing';
import sinon from 'sinon';
import './verify-email-view';
import { VerifyEmailView } from './verify-email-view';

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

// Append params to the current URL while preserving the existing query string
// (which carries the Web Test Runner session id) so the page does not reload.
function withSearch(params: Record<string, string>): () => void {
  const orig = window.location.search;
  const sp = new URLSearchParams(orig);
  for (const [k, v] of Object.entries(params)) sp.set(k, v);
  window.history.replaceState(
    {},
    '',
    `${window.location.pathname}?${sp.toString()}`
  );
  return () =>
    window.history.replaceState({}, '', `${window.location.pathname}${orig}`);
}

describe('VerifyEmailView', () => {
  let fetchStub: sinon.SinonStub;
  let restore: () => void = () => {};

  beforeEach(() => {
    (window as any).BRAND_CONFIG = BRAND_CONFIG;
    fetchStub = sinon.stub(window, 'fetch');
  });

  afterEach(() => {
    fetchStub.restore();
    delete (window as any).BRAND_CONFIG;
    restore();
  });

  it('shows an error when there is no token in the URL', async () => {
    const el = (await fixture(
      html`<verify-email-view></verify-email-view>`
    )) as VerifyEmailView;
    await tick();
    await el.updateComplete;
    expect(el.shadowRoot?.textContent).to.contain('Email Verification Failed');
    expect((el as any).error).to.contain('No verification token');
  });

  it('shows a success state when the token verifies', async () => {
    fetchStub.callsFake(
      async () => new Response(JSON.stringify({}), { status: 200 })
    );
    restore = withSearch({ token: 'good-token' });
    const el = (await fixture(
      html`<verify-email-view></verify-email-view>`
    )) as VerifyEmailView;
    await tick();
    await el.updateComplete;
    expect(el.shadowRoot?.textContent).to.contain('Email Verified');
    expect(
      fetchStub
        .getCalls()
        .some((c) => String(c.args[0]).includes('/api/v1/auth/verify-email'))
    ).to.be.true;
  });

  it('shows a failure state when the token is invalid', async () => {
    fetchStub.callsFake(
      async () => new Response(JSON.stringify({}), { status: 400 })
    );
    restore = withSearch({ token: 'bad-token' });
    const el = (await fixture(
      html`<verify-email-view></verify-email-view>`
    )) as VerifyEmailView;
    await tick();
    await el.updateComplete;
    expect(el.shadowRoot?.textContent).to.contain('Email Verification Failed');
    expect((el as any).error).to.contain('Invalid or expired');
  });

  it('links back to sign in', async () => {
    const el = (await fixture(
      html`<verify-email-view></verify-email-view>`
    )) as VerifyEmailView;
    await tick();
    await el.updateComplete;
    expect(el.shadowRoot?.querySelector('a[href="/login"]')).to.exist;
  });
});
