import { expect, fixture, html, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';
import './login-view';
import { LoginView } from './login-view';
import { invalidateApiCaches } from '../../api';
import { Router } from '../../router';
import './verify-email-view';

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('Login second step', () => {
  let fetchStub: sinon.SinonStub;
  beforeEach(() => {
    (window as any).BRAND_CONFIG = {
      name: 'Preloop',
      domain: 'example.com',
      company: { legal_name: 'Example', address: '', city: '' },
      branding: {
        logo_light: '/logo.svg',
        logo_dark: '/logo.svg',
        favicon: '/favicon.ico',
        primary_color: '#000',
        gradient_product: '',
        gradient_ai: '',
      },
      social: {},
    };
    localStorage.clear();
    invalidateApiCaches();
    fetchStub = sinon.stub(window, 'fetch').callsFake(async (input) => {
      if (String(input).endsWith('/features')) return json({ features: {} });
      return json(
        {
          detail: {
            code: 'mfa_required',
            challenge_token: 'synthetic-challenge',
            methods: ['totp', 'recovery_code'],
          },
        },
        401
      );
    });
  });
  afterEach(() => {
    delete (window as any).BRAND_CONFIG;
    sinon.restore();
    invalidateApiCaches();
    localStorage.clear();
  });

  async function secondStep(): Promise<LoginView> {
    const element = await fixture<LoginView>(html`<login-view></login-view>`);
    await element.updateComplete;
    const form = element.shadowRoot?.querySelector('form');
    form?.dispatchEvent(new Event('submit', { cancelable: true }));
    await waitUntil(() =>
      element.shadowRoot?.textContent?.includes('Confirm your sign-in')
    );
    return element;
  }

  // protects=password challenge has no token persistence and permits recovery;
  // fails_when=password fields remain or tokens are stored; no_claim=server is stubbed.
  it('switches to a code field and offers recovery', async () => {
    const element = await secondStep();
    expect(element.shadowRoot?.querySelector('#password')).to.equal(null);
    expect(
      element.shadowRoot?.querySelector('sl-input')?.getAttribute('inputmode')
    ).to.equal('numeric');
    Array.from(element.shadowRoot!.querySelectorAll<HTMLElement>('sl-button'))
      .find((button) => button.textContent?.includes('Use a recovery code'))
      ?.click();
    await element.updateComplete;
    expect(
      element.shadowRoot?.querySelector('sl-input')?.getAttribute('label')
    ).to.equal('Recovery code');
    expect(localStorage.getItem('accessToken')).to.equal(null);
  });

  // protects=email-link MFA reaches the same second step without storing the
  // challenge; fails_when=valid email proof is reported invalid; no_claim=server is stubbed.
  it('continues an email-link challenge in the login form', async () => {
    const originalUrl = location.href;
    const url = new URL(originalUrl);
    url.searchParams.set('token', 'synthetic-email-link');
    history.replaceState({}, '', url);
    const navigate = sinon.stub(Router, 'go');
    try {
      await fixture(html`<verify-email-view></verify-email-view>`);
      await waitUntil(() => navigate.called);
      expect(navigate.firstCall.args[0]).to.equal('/login');
      const login = await fixture<LoginView>(html`<login-view></login-view>`);
      await login.updateComplete;
      expect(login.shadowRoot?.textContent).to.contain('Confirm your sign-in');
      expect(localStorage.length).to.equal(0);
      expect(sessionStorage.length).to.equal(0);
      expect(location.href).not.to.contain('synthetic-challenge');
    } finally {
      history.replaceState({}, '', originalUrl);
    }
  });

  for (const [code, message] of Object.entries({
    invalid_code: 'already been used',
    challenge_expired: 'sign-in has expired',
    challenge_locked: 'Too many incorrect codes',
  })) {
    // protects=stable actionable second-step errors; fails_when=refusal loses
    // its message or challenge; no_claim=server is stubbed.
    it(`shows ${code}`, async () => {
      const element = await secondStep();
      fetchStub.callsFake(async () =>
        json({ detail: { code } }, code === 'challenge_locked' ? 429 : 401)
      );
      element.shadowRoot
        ?.querySelector('form')
        ?.dispatchEvent(new Event('submit', { cancelable: true }));
      await waitUntil(() =>
        element.shadowRoot
          ?.querySelector('[role="alert"]')
          ?.textContent?.includes(message)
      );
      expect(element.shadowRoot?.textContent).to.contain('Start again');
    });
  }
});
