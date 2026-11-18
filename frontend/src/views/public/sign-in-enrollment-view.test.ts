import { expect, fixture, html, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';
import { invalidateApiCaches } from '../../api';
import { Router } from '../../router';
import './sign-in-enrollment-view';

function json(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('Required sign-in enrollment', () => {
  beforeEach(() => {
    localStorage.setItem('accessToken', 'synthetic');
    invalidateApiCaches();
  });
  afterEach(() => {
    sinon.restore();
    localStorage.clear();
    invalidateApiCaches();
  });

  // protects=restricted members can reach authenticator enrollment; fails_when=screen redirects before proof; no_claim=API and browser hardware are stubs.
  it('shows authenticator enrollment without ordinary console content', async () => {
    sinon.stub(window, 'fetch').callsFake(async (input) => {
      if (String(input).endsWith('/features'))
        return json({ features: { passkeys: false } });
      if (String(input).endsWith('/status'))
        return json({ totp_enabled: false, recovery_codes_remaining: 0 });
      return json({ account_id: 'account', enroll_only: true });
    });
    const navigate = sinon.stub(Router, 'go');
    const element = await fixture(
      html`<sign-in-enrollment-view></sign-in-enrollment-view>`
    );
    await waitUntil(() => !!element.shadowRoot?.querySelector('mfa-card'));
    expect(element.shadowRoot?.textContent).to.contain('Protect your sign-in');
    expect(element.shadowRoot?.textContent).to.contain(
      'ask an account administrator'
    );
    expect(navigate.called).to.equal(false);
  });

  // protects=completed enrollment resumes console only after codes saved; fails_when=Done cannot leave enrollment; no_claim=recovery-code gate has existing component tests.
  it('continues when the authenticator card confirms completion', async () => {
    sinon.stub(window, 'fetch').callsFake(async (input) =>
      json(
        String(input).endsWith('/features')
          ? { features: {} }
          : {
              account_id: 'account',
              enroll_only: true,
              totp_enabled: false,
              recovery_codes_remaining: 0,
            }
      )
    );
    const navigate = sinon.stub(Router, 'go');
    const element = await fixture(
      html`<sign-in-enrollment-view></sign-in-enrollment-view>`
    );
    element.shadowRoot!.querySelector('mfa-card')!.dispatchEvent(
      new CustomEvent('mfa-enrollment-complete', {
        bubbles: true,
        composed: true,
      })
    );
    expect(navigate.calledWith('/console')).to.equal(true);
  });
});
