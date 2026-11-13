import { expect, fixture, html, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';
import './step-up-dialog';
import type { StepUpDialog } from './step-up-dialog';
import { fetchWithAuth } from '../api';
import { Router } from '../router';

const json = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });

describe('Shared step-up', () => {
  beforeEach(() => {
    localStorage.setItem('accessToken', 'old');
  });
  afterEach(() => {
    document.querySelectorAll('step-up-dialog').forEach((el) => el.remove());
    sinon.restore();
    localStorage.clear();
  });

  // protects=fresh proof replaces tokens; fails_when=dialog completes without
  // server verification; no_claim=server responses are stubbed.
  it('verifies a code and returns fresh tokens', async () => {
    const fetchStub = sinon
      .stub(window, 'fetch')
      .callsFake(async (input) =>
        String(input).endsWith('/step-up')
          ? json({ access_token: 'fresh', refresh_token: 'new-refresh' })
          : json({ features: {} })
      );
    const dialog = await fixture<StepUpDialog>(
      html`<step-up-dialog></step-up-dialog>`
    );
    const result = dialog.ask(['totp']);
    await dialog.updateComplete;
    dialog.shadowRoot
      ?.querySelector('form')
      ?.dispatchEvent(new Event('submit', { cancelable: true }));
    let resolved: boolean | undefined;
    void result.then((value) => {
      resolved = value;
    });
    await waitUntil(
      () => resolved !== undefined,
      'proof did not settle: ' + fetchStub.callCount
    );
    expect(resolved).to.equal(true);
    expect(localStorage.getItem('accessToken')).to.equal('fresh');
    expect(
      fetchStub
        .getCalls()
        .filter((call) => String(call.args[0]).endsWith('/step-up')).length
    ).to.equal(1);
  });

  // protects=one retry and visible second failure; fails_when=recursive prompts
  // or a hidden refusal; no_claim=server responses are stubbed.
  it('retries the original request once and surfaces a second failure', async () => {
    let actions = 0;
    sinon.stub(window, 'fetch').callsFake(async (input) => {
      if (String(input).endsWith('/step-up'))
        return json({ access_token: 'fresh', refresh_token: 'new-refresh' });
      if (String(input) !== '/api/v1/sensitive') return json({ features: {} });
      actions++;
      return json(
        { detail: { code: 'step_up_required', methods: ['totp'] } },
        401
      );
    });
    const request = fetchWithAuth('/api/v1/sensitive', { method: 'DELETE' });
    await waitUntil(() => !!document.querySelector('step-up-dialog'));
    const dialog = document.querySelector<StepUpDialog>('step-up-dialog')!;
    await dialog.updateComplete;
    dialog.shadowRoot
      ?.querySelector('form')
      ?.dispatchEvent(new Event('submit', { cancelable: true }));
    expect((await request).status).to.equal(401);
    expect(actions).to.equal(2);
  });

  // protects=cancel never executes the action again; fails_when=cancel retries;
  // no_claim=server responses are stubbed.
  it('returns the original refusal on cancellation', async () => {
    const fetchStub = sinon
      .stub(window, 'fetch')
      .resolves(
        json({ detail: { code: 'step_up_required', methods: ['enroll'] } }, 401)
      );
    const request = fetchWithAuth('/api/v1/sensitive', { method: 'DELETE' });
    await waitUntil(() => !!document.querySelector('step-up-dialog'));
    const dialog = document.querySelector<StepUpDialog>('step-up-dialog')!;
    await dialog.updateComplete;
    dialog.shadowRoot
      ?.querySelector<HTMLElement>('sl-button[slot="footer"]')
      ?.click();
    expect((await request).status).to.equal(401);
    expect(
      fetchStub
        .getCalls()
        .filter((call) => call.args[0] === '/api/v1/sensitive').length
    ).to.equal(1);
  });
  // protects=first-factor bootstrap has a usable fresh-login path;
  // fails_when=login method leaves no action; no_claim=server is stubbed.
  it('returns to login when the first factor needs fresh authentication', async () => {
    const navigate = sinon.stub(Router, 'go');
    const dialog = await fixture<StepUpDialog>(
      html`<step-up-dialog></step-up-dialog>`
    );
    const result = dialog.ask(['login']);
    await dialog.updateComplete;
    Array.from(dialog.shadowRoot!.querySelectorAll<HTMLElement>('sl-button'))
      .find((button) => button.textContent?.includes('Sign in again'))!
      .click();
    expect(await result).to.equal(false);
    expect(navigate.calledWith('/login')).to.equal(true);
    expect(localStorage.getItem('loginRedirect')).to.equal(
      location.pathname + location.search
    );
  });
});
