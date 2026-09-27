import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';
import './login-view';
import { LoginView } from './login-view';
import type { Capability } from '../../capabilities';
import { mockApi, type MockApi } from '../../test-helpers/capability-api';

const MEMBERSHIPS = '/api/v1/me/memberships';
const TOKEN = '/api/v1/auth/token/json';
const SWITCH = '/api/v1/auth/switch-account';

const membership = (
  id: string,
  name: string,
  parent: string | null = null
) => ({
  account_id: id,
  account_name: name,
  slug: id,
  parent_account_id: parent,
  last_used_at: null,
});

async function signIn(
  capabilities: Capability[],
  memberships: unknown[],
  login: Record<string, unknown> = {}
) {
  const api = mockApi({
    capabilities,
    routes: [
      {
        method: 'POST',
        path: TOKEN,
        body: {
          access_token: 'first-access',
          refresh_token: 'first-refresh',
          ...login,
        },
      },
      { path: MEMBERSHIPS, body: { items: memberships } },
      {
        method: 'POST',
        path: SWITCH,
        body: { access_token: 'acct-access', refresh_token: 'acct-refresh' },
      },
    ],
  });
  const navigate = sinon.stub(
    LoginView.prototype as unknown as { _navigateAfterLogin: () => void },
    '_navigateAfterLogin'
  );
  const el = await fixture<LoginView>(html`<login-view></login-view>`);
  await waitUntil(() => api.callsTo('/api/v1/features').length > 0);
  await new Promise((r) => setTimeout(r, 0));
  el.shadowRoot!.querySelector<HTMLInputElement>('#username')!.value = 'ada';
  el.shadowRoot!.querySelector<HTMLInputElement>('#password')!.value = 'pw';
  el.shadowRoot!.querySelector('form')!.dispatchEvent(
    new SubmitEvent('submit', { bubbles: true, cancelable: true })
  );
  await waitUntil(() => api.callsTo(TOKEN, 'POST').length === 1);
  return { api, el, navigate };
}

describe('login-view account chooser', () => {
  let api: MockApi | undefined;
  let navigate: sinon.SinonStub | undefined;

  beforeEach(() => {
    (window as any).BRAND_CONFIG = {
      name: 'Test Brand',
      domain: 'test.example.com',
      company: { legal_name: 'Test Co', address: '1 Test', city: 'Test' },
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
    localStorage.clear();
  });

  afterEach(() => {
    api?.restore();
    navigate?.restore();
    localStorage.clear();
    delete (window as any).BRAND_CONFIG;
  });

  it('asks which account to open when there are several and none was last used', async () => {
    const r = await signIn(
      ['multi_account'],
      [membership('acc-a', 'Alpha'), membership('acc-b', 'Beta')]
    );
    ({ api, navigate } = r);
    const el = r.el;
    await waitUntil(() =>
      el.shadowRoot!.querySelector('account-login-chooser')
    );
    const chooser = el.shadowRoot!.querySelector('account-login-chooser')!;
    await chooser.updateComplete;
    expect(el.shadowRoot!.querySelector('form')).to.be.null;
    expect(navigate!.called).to.equal(false);

    (
      chooser.shadowRoot!.querySelector(
        'sl-button[data-account="acc-b"]'
      ) as HTMLElement
    ).click();
    await waitUntil(() => navigate!.called, 'did not continue after choosing');
    expect(api!.callsTo(SWITCH, 'POST')[0].body).to.eql({
      account_id: 'acc-b',
    });
    expect(api!.callsTo(SWITCH, 'POST')[0].authorization).to.equal(
      'Bearer first-access'
    );
    expect(localStorage.getItem('accessToken')).to.equal('acct-access');
    expect(localStorage.getItem('refreshToken')).to.equal('acct-refresh');
  });

  it('goes straight in with a last used account', async () => {
    const r = await signIn(
      ['multi_account'],
      [membership('acc-a', 'Alpha'), membership('acc-b', 'Beta')],
      { last_active_account_id: 'acc-a' }
    );
    ({ api, navigate } = r);
    await waitUntil(() => navigate!.called);
    expect(api.callsTo(MEMBERSHIPS)).to.have.length(0);
    expect(r.el.shadowRoot!.querySelector('account-login-chooser')).to.be.null;
  });

  it('goes straight in with a single account', async () => {
    const r = await signIn(['multi_account'], [membership('acc-a', 'Alpha')]);
    ({ api, navigate } = r);
    await waitUntil(() => navigate!.called);
    expect(api.callsTo(MEMBERSHIPS)).to.have.length(1);
    expect(r.el.shadowRoot!.querySelector('account-login-chooser')).to.be.null;
  });

  it('never asks for memberships without the capability', async () => {
    const r = await signIn([], [membership('a', 'A'), membership('b', 'B')]);
    ({ api, navigate } = r);
    await waitUntil(() => navigate!.called);
    expect(api.callsTo(MEMBERSHIPS)).to.have.length(0);
  });
});
