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
    sessionStorage.clear();
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

  // protects=social callback reaches the same second step, including CLI delivery;
  // fails_when=raw challenge remains in URL or tokens are stored before proof; no_claim=server is stubbed.
  it('completes a social CLI challenge after the code', async () => {
    const originalUrl = location.href;
    history.replaceState(
      {},
      '',
      '/login#mfa_challenge=social-challenge&cli_redirect_uri=http%3A%2F%2F127.0.0.1%3A4321%2Fcallback'
    );
    fetchStub.callsFake(async (input) =>
      String(input).endsWith('/features')
        ? json({ features: {} })
        : json({
            access_token: 'cli-access',
            refresh_token: 'cli-refresh',
            cli_redirect_uri: 'http://127.0.0.1:4321/callback',
          })
    );
    try {
      const element = await fixture<LoginView>(html`<login-view></login-view>`);
      expect(element.shadowRoot?.textContent).to.contain(
        'Confirm your sign-in'
      );
      expect(location.hash).to.equal('');
      expect(localStorage.getItem('accessToken')).to.equal(null);
      const deliver = sinon.stub(element as any, '_completeCliMfa');
      element
        .shadowRoot!.querySelector('form')!
        .dispatchEvent(new Event('submit', { cancelable: true }));
      await waitUntil(() => deliver.called);
      const callback = new URL(deliver.firstCall.args[0]);
      expect(callback.origin).to.equal('http://127.0.0.1:4321');
      expect(callback.searchParams.get('access_token')).to.equal('cli-access');
      expect(localStorage.getItem('accessToken')).to.equal(null);
    } finally {
      history.replaceState({}, '', originalUrl);
    }
  });

  // protects=fragment callback is never a delivery authority; fails_when=unsigned address receives tokens; no_claim=server endpoint binding is tested separately.
  it('refuses CLI delivery without a server-verified callback', async () => {
    const originalUrl = location.href;
    history.replaceState(
      {},
      '',
      '/login#mfa_challenge=challenge&cli_redirect_uri=http%3A%2F%2F127.0.0.1%3A9999%2Fcallback'
    );
    fetchStub.callsFake(async (input) =>
      String(input).endsWith('/features')
        ? json({ features: {} })
        : json({ access_token: 'access', refresh_token: 'refresh' })
    );
    try {
      const element = await fixture<LoginView>(html`<login-view></login-view>`);
      const deliver = sinon.stub(element as any, '_completeCliMfa');
      element
        .shadowRoot!.querySelector('form')!
        .dispatchEvent(new Event('submit', { cancelable: true }));
      await waitUntil(() =>
        element.shadowRoot!.textContent!.includes(
          'CLI sign-in could not be verified'
        )
      );
      expect(deliver.called).to.equal(false);
      expect(localStorage.getItem('accessToken')).to.equal(null);
      const verify = fetchStub
        .getCalls()
        .find((call) => String(call.args[0]).endsWith('/auth/mfa/verify'))!;
      expect(JSON.parse(verify.args[1].body).cli_redirect_uri).to.equal(
        'http://127.0.0.1:9999/callback'
      );
    } finally {
      history.replaceState({}, '', originalUrl);
    }
  });

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
      // Only the fixed restart-notice name may be kept, never the challenge.
      expect(Object.keys(sessionStorage)).to.deep.equal([
        'preloop.signInNotice',
      ]);
      expect(sessionStorage.getItem('preloop.signInNotice')).to.equal(
        'mfa_restarted'
      );
      expect(location.href).not.to.contain('synthetic-challenge');
    } finally {
      history.replaceState({}, '', originalUrl);
    }
  });

  // protects=a factor this server cannot decrypt is explained and the
  // recovery path is offered; fails_when=generic error or code field stays;
  // no_claim=server is stubbed.
  it('explains an unreadable authenticator and switches to recovery', async () => {
    const element = await secondStep();
    fetchStub.callsFake(async () =>
      json(
        {
          detail: {
            code: 'factor_unreadable',
            message: 'server message',
          },
        },
        409
      )
    );
    element.shadowRoot
      ?.querySelector('form')
      ?.dispatchEvent(new Event('submit', { cancelable: true }));
    await waitUntil(() =>
      element.shadowRoot
        ?.querySelector('[role="alert"]')
        ?.textContent?.includes('encryption key changed')
    );
    const alert = element.shadowRoot!.querySelector('[role="alert"]')!;
    expect(alert.textContent).to.contain('recovery code');
    expect(alert.textContent).to.contain('administrator');
    expect(
      element.shadowRoot?.querySelector('sl-input')?.getAttribute('label')
    ).to.equal('Recovery code');
  });

  // protects=the sign-in page says why an administrator ended the session;
  // fails_when=plain sign-in form with no reason; no_claim=server is stubbed.
  it('shows why an administrator ended the session', async () => {
    sessionStorage.setItem('sessionEndReason', 'sign_in_reset');
    const element = await fixture<LoginView>(html`<login-view></login-view>`);
    await element.updateComplete;
    expect(element.shadowRoot?.textContent).to.contain(
      'An administrator reset your sign-in.'
    );
    expect(sessionStorage.getItem('sessionEndReason')).to.equal(null);
  });

  for (const [code, message] of Object.entries({
    invalid_code: 'already been used',
    challenge_expired: 'sign-in has expired',
    challenge_locked: 'Too many incorrect codes for this sign-in. Start again.',
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

  // protects=a reload on the code step explains the restart (L1-2); fails_when=
  // the password form reappears silently or the notice persists; no_claim=server is stubbed.
  it('explains a restarted sign-in after reloading the code step', async () => {
    const element = await secondStep();
    await element.updateComplete;
    element.remove();
    const again = await fixture<LoginView>(html`<login-view></login-view>`);
    await again.updateComplete;
    expect(again.shadowRoot?.textContent).to.contain(
      'Sign-in was restarted. Enter your password again.'
    );
    const third = await fixture<LoginView>(html`<login-view></login-view>`);
    await third.updateComplete;
    expect(third.shadowRoot?.textContent).not.to.contain(
      'Sign-in was restarted'
    );
  });

  // protects=the session ended by a password change is explained (L1-3).
  it('says the password was changed', async () => {
    sessionStorage.setItem('preloop.signInNotice', 'password_changed');
    const element = await fixture<LoginView>(html`<login-view></login-view>`);
    await element.updateComplete;
    expect(element.shadowRoot?.textContent).to.contain(
      'Your password was changed. Sign in again.'
    );
    expect(sessionStorage.getItem('preloop.signInNotice')).to.equal(null);
  });

  // protects=the two lockouts read differently (L1-1).
  it('shows how long the per-user lock lasts', async () => {
    const element = await secondStep();
    fetchStub.callsFake(async () =>
      json(
        { detail: { code: 'mfa_user_locked', retry_after_seconds: 1500 } },
        429
      )
    );
    element.shadowRoot
      ?.querySelector('form')
      ?.dispatchEvent(new Event('submit', { cancelable: true }));
    await waitUntil(() =>
      element.shadowRoot
        ?.querySelector('[role="alert"]')
        ?.textContent?.includes('Try again in 25 minutes')
    );
  });
});
