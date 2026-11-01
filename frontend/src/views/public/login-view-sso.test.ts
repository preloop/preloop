import { fixture, html, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';
import { invalidateApiCaches } from '../../api';
import './login-view';
import type { LoginView } from './login-view';

describe('Email-first SSO sign-in', () => {
  let fetchStub: sinon.SinonStub;
  let enabled: boolean;
  let pluginOn: boolean;
  let discovery: Record<string, unknown>;
  let discoveryStatus: number;
  let element: LoginView;
  beforeEach(() => {
    enabled = true;
    pluginOn = true;
    discovery = { sso_url: '/api/v1/auth/sso/connection-a/start' };
    discoveryStatus = 200;
    (window as any).BRAND_CONFIG = {
      name: 'Example Console',
      domain: 'example.com',
      company: { legal_name: 'Example', address: 'Example', city: 'Example' },
      branding: { logo_light: '/logo.svg', logo_dark: '/logo.svg' },
      social: {},
    };
    invalidateApiCaches();
    localStorage.clear();
    fetchStub = sinon.stub(window, 'fetch').callsFake(async (input) => {
      const path = String(input);
      const body = path.endsWith('/features')
        ? {
            features: {
              enterprise_sso: pluginOn,
              sso_login: enabled,
              passkeys: false,
              registration: true,
            },
            plugins: [],
          }
        : discovery;
      return new Response(JSON.stringify(body), {
        status: path.endsWith('/features') ? 200 : discoveryStatus,
        headers: { 'Content-Type': 'application/json' },
      });
    });
  });
  afterEach(() => {
    window.history.replaceState({}, '', '/');
    sinon.restore();
    invalidateApiCaches();
    localStorage.clear();
    delete (window as any).BRAND_CONFIG;
  });
  async function mount() {
    element = await fixture<LoginView>(html`<login-view></login-view>`);
    await waitUntil(() => fetchStub.called);
    await new Promise((resolve) => setTimeout(resolve, 0));
    await element.updateComplete;
    return element.shadowRoot!;
  }
  async function discover(email = 'person@example.com') {
    const input =
      element.shadowRoot!.querySelector<HTMLInputElement>('[name=email]')!;
    input.value = email;
    element
      .shadowRoot!.querySelector('#sso-discovery-form')!
      .dispatchEvent(
        new SubmitEvent('submit', { bubbles: true, cancelable: true })
      );
    await waitUntil(() =>
      fetchStub
        .getCalls()
        .some((call) => String(call.args[0]).endsWith('/sso/discover'))
    );
    await new Promise((resolve) => setTimeout(resolve, 0));
    await element.updateComplete;
  }
  it('shows the generic SSO failure and removes the error URL marker', async () => {
    window.history.replaceState({}, '', '/login?sso_error=sign_in_failed');
    const root = await mount();
    expect(root.querySelector('[role=alert]')!.textContent).to.contain(
      'Single sign-on failed'
    );
    expect(location.search).to.equal('');
    expect(localStorage.getItem('accessToken')).to.equal(null);
  });

  it('binds a fresh password sign-in to the selected recovery account', async () => {
    window.history.replaceState(
      {},
      '',
      '/login?account_id=11111111-1111-4111-8111-111111111111'
    );
    const root = await mount();
    (root.querySelector('#other-sign-in-methods') as HTMLElement).click();
    await element.updateComplete;
    root.querySelector<HTMLInputElement>('#username')!.value =
      'owner@example.com';
    root.querySelector<HTMLInputElement>('#password')!.value =
      'synthetic-password';
    fetchStub.callsFake(
      async (input) =>
        new Response(JSON.stringify({ detail: 'Invalid account selection' }), {
          status: 401,
        })
    );
    root
      .querySelector('form')!
      .dispatchEvent(
        new SubmitEvent('submit', { bubbles: true, cancelable: true })
      );
    await waitUntil(() =>
      fetchStub
        .getCalls()
        .some((call) => String(call.args[0]).endsWith('/auth/token/json'))
    );
    const call = fetchStub
      .getCalls()
      .find((call) => String(call.args[0]).endsWith('/auth/token/json'))!;
    expect(JSON.parse(String(call.args[1]!.body))).to.include({
      account_id: '11111111-1111-4111-8111-111111111111',
    });
    expect(localStorage.getItem('accessToken')).to.equal(null);
  });

  it('asks for an email first and offers the discovered SSO start URL', async () => {
    const root = await mount();
    expect(root.querySelector('#password')).to.be.null;
    await discover();
    const button = root.querySelector('#continue-sso')!;
    expect(button.textContent).to.contain('Continue with SSO');
    expect(button.getAttribute('href')).to.equal(
      `${location.origin}/api/v1/auth/sso/connection-a/start`
    );
    await (button as HTMLElement & { updateComplete: Promise<unknown> })
      .updateComplete;
    expect(button.shadowRoot!.querySelector('a')!.rel).not.to.contain(
      'noreferrer'
    );
    const call = fetchStub
      .getCalls()
      .find((call) => String(call.args[0]).endsWith('/sso/discover'))!;
    expect(JSON.parse(String(call.args[1]!.body))).to.deep.equal({
      email: 'person@example.com',
    });
    expect(localStorage.getItem('accessToken')).to.equal(null);
  });
  it('returns to existing methods for domains without SSO', async () => {
    discovery = { sso_url: null };
    const root = await mount();
    await discover('unknown@example.org');
    expect(root.querySelector('#username')).to.exist;
    expect(root.querySelector('#password')).to.exist;
    expect(root.querySelector('#continue-sso')).to.be.null;
    expect(root.textContent).not.to.contain('domain not found');
  });
  it('keeps the original login when the feature is absent', async () => {
    enabled = false;
    const root = await mount();
    expect(root.querySelector('#username')).to.exist;
    expect(root.querySelector('#password')).to.exist;
    expect(root.querySelector('#sso-discovery-form')).to.be.null;
    expect(fetchStub.callCount).to.equal(1);
  });
  it('keeps owner recovery and other sign-in methods accessible without discovery', async () => {
    const root = await mount();
    (root.querySelector('#other-sign-in-methods') as HTMLElement).click();
    await element.updateComplete;
    expect(root.querySelector('#password')).to.exist;
    expect(fetchStub.callCount).to.equal(1);
  });
  it('rejects foreign redirect URLs from discovery', async () => {
    discovery = {
      sso_url: 'https://other.example/api/v1/auth/sso/connection-a/start',
    };
    const root = await mount();
    await discover();
    expect(root.querySelector('#continue-sso')).to.be.null;
    expect(root.querySelector('#password')).to.exist;
  });
  it('shows a retryable error on a throttled request without losing alternative methods', async () => {
    discoveryStatus = 429;
    const root = await mount();
    await discover();
    expect(root.querySelector('[role=alert]')!.textContent).to.contain(
      'Try again'
    );
    expect(root.querySelector('#other-sign-in-methods')).to.exist;
    expect(root.querySelector('#continue-sso')).to.be.null;
  });
  it('clears the previous SSO route when the email changes and preserves a pending login destination', async () => {
    localStorage.setItem('loginRedirect', '/console/agents');
    const root = await mount();
    await discover();
    const input = root.querySelector<HTMLInputElement>('[name=email]')!;
    input.value = 'other@example.org';
    input.dispatchEvent(new Event('input'));
    await element.updateComplete;
    expect(root.querySelector('#continue-sso')).to.be.null;
    expect(localStorage.getItem('loginRedirect')).to.equal('/console/agents');
  });
  it('shows the password form directly when no SSO connection is configured', async () => {
    // The founder's case: plugin loaded, no connection anywhere.
    enabled = false;
    pluginOn = true;
    const root = await mount();
    expect(root.querySelector('#sso-discovery-form')).to.be.null;
    expect(root.querySelector('#username')).to.exist;
    expect(root.querySelector('#password')).to.exist;
    expect(root.textContent).not.to.contain('Use single sign-on');
    expect(fetchStub.callCount).to.equal(1);
  });
  it('prefills the password form for a domain without SSO and does not loop', async () => {
    discovery = { sso_url: null };
    const root = await mount();
    await discover('user@other.com');
    const username = root.querySelector<HTMLInputElement>('#username')!;
    expect(username.value).to.equal('user@other.com');
    expect(root.querySelector('#sso-discovery-form')).to.be.null;
    const discoverCalls = () =>
      fetchStub
        .getCalls()
        .filter((call) => String(call.args[0]).endsWith('/sso/discover'))
        .length;
    expect(discoverCalls()).to.equal(1);
    // The SSO link returns to the email step, which keeps the email and
    // leads to SSO for a configured domain rather than a dead end.
    const back = Array.from(root.querySelectorAll('sl-button')).find((b) =>
      b.textContent?.includes('Use single sign-on')
    ) as HTMLElement;
    back.click();
    await element.updateComplete;
    const email = root.querySelector<HTMLInputElement>('[name=email]')!;
    expect(email.value).to.equal('user@other.com');
    discovery = { sso_url: '/api/v1/auth/sso/connection-a/start' };
    await discover('user@example.com');
    expect(root.querySelector('#continue-sso')).to.exist;
  });
});
