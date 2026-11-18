import { expect, fixture, html, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';
import { invalidateApiCaches } from '../api';
import './sign-in-policy-card';
import { SignInPolicyCard } from './sign-in-policy-card';

const coverage = {
  policy: 'all',
  step_up_window_minutes: 15,
  members_total: 12,
  members_pending_total: 4,
  members_offset: 0,
  members_limit: 50,
  members_with_factor: 6,
  members_with_totp: 4,
  members_sso: 2,
  members_pending: Array.from({ length: 4 }),
  members: [
    {
      user_id: 'member',
      email: 'member@example.com',
      role: 'member',
      passkey: false,
      totp: true,
      sso: false,
      reset_allowed: true,
    },
  ],
};

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('Account sign-in policy', () => {
  let fetchStub: sinon.SinonStub;
  beforeEach(() => {
    invalidateApiCaches();
    localStorage.setItem('accessToken', 'synthetic-access');
    fetchStub = sinon.stub(window, 'fetch').callsFake(async (input, init) => {
      const url = String(input);
      if (url.endsWith('/features'))
        return json({ features: { strong_sign_in_policy: true } });
      if (url.endsWith('/users/me'))
        return json({ account_id: 'account', id: 'admin' });
      if (init?.method === 'PUT')
        return json({
          ...coverage,
          ...JSON.parse(init.body as string),
          access_token: 'new-access',
          refresh_token: 'new-refresh',
        });
      return json(coverage);
    });
  });
  afterEach(() => {
    sinon.restore();
    invalidateApiCaches();
    localStorage.clear();
  });

  // protects=OSS has no enforcement UI or policy fetch; fails_when=capability ignored; no_claim=server is stubbed.
  it('is inert without the edition capability', async () => {
    fetchStub.callsFake(async () => json({ features: {} }));
    const element = await fixture<SignInPolicyCard>(
      html`<sign-in-policy-card></sign-in-policy-card>`
    );
    await element.updateComplete;
    expect(element.shadowRoot?.querySelector('section')).to.equal(null);
    expect(fetchStub.callCount).to.equal(1);
  });

  async function card(): Promise<SignInPolicyCard> {
    const element = await fixture<SignInPolicyCard>(
      html`<sign-in-policy-card></sign-in-policy-card>`
    );
    await waitUntil(() => !!element.shadowRoot?.querySelector('table'));
    return element;
  }

  // protects=coverage and channel limitation are visible together; fails_when=counts or caveat missing; no_claim=server supplies counts.
  it('shows factor coverage and the mobile/watch/chat limitation', async () => {
    const element = await card();
    const text = element.shadowRoot!.textContent!.replace(/\s+/g, ' ');
    expect(text).to.contain('6 of 12');
    expect(text).to.contain('4 use an authenticator app');
    expect(text).to.contain('Mobile, watch, Slack, and Mattermost');
    expect(text).to.contain('member@example.com');
    expect(text).to.contain('Authenticator app');
  });

  // protects=an authenticator this server cannot decrypt is shown as
  // unusable; fails_when=it reads as a working factor; no_claim=server is stubbed.
  it('shows an unreadable authenticator as unusable', async () => {
    fetchStub.callsFake(async (input) => {
      const url = String(input);
      if (url.endsWith('/features'))
        return json({ features: { strong_sign_in_policy: true } });
      return json({
        ...coverage,
        members: [{ ...coverage.members[0], totp: false, totp_unusable: true }],
      });
    });
    const element = await card();
    const cell = element.shadowRoot!.querySelector('tbody td:nth-child(3)')!;
    expect(cell.textContent!.replace(/\s+/g, ' ')).to.contain(
      'Authenticator app (unusable, needs to be set up again)'
    );
  });

  // protects=policy disable clears step-up and replaces revoked tokens; fails_when=old credentials retained; no_claim=server auth is stubbed.
  it('turns step-up off with policy and stores replacement tokens', async () => {
    const element = await card();
    const select = element.shadowRoot!.querySelector('sl-select') as any;
    select.value = 'off';
    select.dispatchEvent(new Event('sl-change'));
    element
      .shadowRoot!.querySelector('form')!
      .dispatchEvent(new Event('submit', { cancelable: true }));
    await waitUntil(() => localStorage.getItem('accessToken') === 'new-access');
    const call = fetchStub
      .getCalls()
      .find((call) => call.args[1]?.method === 'PUT')!;
    expect(JSON.parse(call.args[1].body)).to.deep.equal({
      policy: 'off',
      step_up_window_minutes: 0,
    });
  });

  // protects=reset requires a reason before confirmation; fails_when=empty reason submits; no_claim=confirmation and backend proof tested separately.
  it('requires a reset reason', async () => {
    const element = await card();
    element.shadowRoot!.querySelector<HTMLElement>('tbody sl-button')!.click();
    await element.updateComplete;
    const button = Array.from(
      element.shadowRoot!.querySelectorAll<any>('sl-button')
    ).find((button) => button.textContent.includes('Reset member sign-in'));
    expect(button.disabled).to.equal(true);
    expect(
      element.shadowRoot!.querySelector('sl-input[label="Reason for reset"]')
    ).not.to.equal(null);
  });

  // protects=invalid windows do not reach API; fails_when=3-minute setting is sent; no_claim=server repeats validation.
  it('rejects an invalid window', async () => {
    const element = await card();
    const input = element.shadowRoot!.querySelector(
      'sl-input[type="number"]'
    ) as any;
    input.value = '3';
    input.dispatchEvent(new Event('sl-input'));
    element
      .shadowRoot!.querySelector('form')!
      .dispatchEvent(new Event('submit', { cancelable: true }));
    await element.updateComplete;
    expect(element.shadowRoot!.textContent).to.contain('5 to 720');
    expect(
      fetchStub.getCalls().some((call) => call.args[1]?.method === 'PUT')
    ).to.equal(false);
  });
  // protects=bounded coverage can reach later members while totals remain global; fails_when=next page missing or first-page pending count used; no_claim=network responses are mocked.
  it('loads another coverage page and keeps the account totals', async () => {
    fetchStub.callsFake(async (input) => {
      const url = String(input);
      if (url.endsWith('/features'))
        return json({ features: { strong_sign_in_policy: true } });
      if (url.endsWith('/users/me'))
        return json({ account_id: 'account', id: 'admin' });
      const offset = Number(
        new URL(url, window.location.origin).searchParams.get('offset') || 0
      );
      return json({
        ...coverage,
        members_total: 121,
        members_pending_total: 120,
        members_offset: offset,
        members: [
          {
            ...coverage.members[0],
            email: offset ? 'later@example.com' : 'first@example.com',
          },
        ],
      });
    });
    const element = await card();
    expect(element.shadowRoot!.textContent!.replace(/\s+/g, ' ')).to.contain(
      '120 have no factor'
    );
    const next = Array.from(
      element.shadowRoot!.querySelectorAll<any>('sl-button')
    ).find((button) => button.textContent.trim() === 'Next');
    expect(next).not.to.equal(undefined);
    next.click();
    await waitUntil(() =>
      element.shadowRoot!.textContent!.includes('later@example.com')
    );
    expect(
      fetchStub
        .getCalls()
        .some((call) => String(call.args[0]).includes('offset=50'))
    ).to.equal(true);
  });
  for (const empty of [false, true]) {
    // protects=partial success cannot corrupt browser credentials; fails_when=undefined tokens replace session; no_claim=server schema is tested separately.
    it(`preserves session on ${empty ? '204' : 'token-less 200'} policy save`, async () => {
      const element = await card();
      localStorage.setItem('refreshToken', 'existing-refresh');
      fetchStub.callsFake(async (input, init) => {
        if (init?.method === 'PUT')
          return empty ? new Response(null, { status: 204 }) : json(coverage);
        if (String(input).endsWith('/features'))
          return json({ features: { strong_sign_in_policy: true } });
        if (String(input).endsWith('/users/me'))
          return json({ account_id: 'account', id: 'admin' });
        return json(coverage);
      });
      element
        .shadowRoot!.querySelector('form')!
        .dispatchEvent(new Event('submit', { cancelable: true }));
      await waitUntil(
        () =>
          !!element.shadowRoot!.querySelector('[role="status"]')?.textContent
      );
      expect(localStorage.getItem('accessToken')).to.equal('synthetic-access');
      expect(localStorage.getItem('refreshToken')).to.equal('existing-refresh');
      expect(element.shadowRoot!.textContent).to.contain(
        'Account sign-in policy saved.'
      );
    });
  }
});
