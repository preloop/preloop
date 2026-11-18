import { expect, fixture, html, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';
import './mfa-card';
import { MfaCard } from './mfa-card';
import { invalidateApiCaches } from '../api';
import { answerConfirmDialog } from '../utils/test-confirm-dialog';

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('Authenticator enrollment', () => {
  let fetchStub: sinon.SinonStub;
  beforeEach(() => {
    localStorage.setItem('accessToken', 'session');
    invalidateApiCaches();
    fetchStub = sinon.stub(window, 'fetch').callsFake(async (input) => {
      const url = String(input);
      if (url.endsWith('/features')) return json({ features: {} });
      if (url.endsWith('/status'))
        return json({ totp_enabled: false, recovery_codes_remaining: 0 });
      if (url.endsWith('/enroll'))
        return json({
          secret: 'SYNTHETICSECRET',
          otpauth_uri:
            'otpauth://totp/Preloop:person@example.com?secret=JBSWY3DPEHPK3PXP&issuer=Preloop',
        });
      if (url.endsWith('/confirm'))
        return json({
          access_token: 'upgraded',
          refresh_token: 'refresh',
          recovery_codes: Array.from(
            { length: 10 },
            (_, i) => `abcde-abcd${i}`
          ),
        });
      return json({});
    });
  });
  afterEach(() => {
    sinon.restore();
    localStorage.clear();
    invalidateApiCaches();
  });

  async function card(): Promise<MfaCard> {
    const element = await fixture<MfaCard>(html`<mfa-card></mfa-card>`);
    await waitUntil(() =>
      element.shadowRoot?.textContent?.includes('Set up authenticator app')
    );
    return element;
  }
  async function start(element: MfaCard): Promise<void> {
    element.shadowRoot?.querySelector<HTMLElement>('sl-button')?.click();
    await waitUntil(() => !!element.shadowRoot?.querySelector('img'));
  }

  // protects=off and enrolling states; fails_when=secret or QR missing;
  // no_claim=server and authenticator hardware are replaced by fetch fixtures.
  it('shows an off card, then generates the QR locally', async () => {
    const element = await card();
    await start(element);
    expect(element.shadowRoot?.querySelector('img')?.src).to.match(
      /^data:image/
    );
    expect(element.shadowRoot?.textContent).to.contain('SYNTHETICSECRET');
    expect(
      element.shadowRoot?.querySelector('sl-input')?.getAttribute('label')
    ).to.equal('Authenticator code');
    expect(
      fetchStub
        .getCalls()
        .every((call) => !String(call.args[0]).includes('otpauth'))
    ).to.equal(true);
  });

  // protects=recovery confirmation gate and secret removal; fails_when=Done
  // works before saving or the enrollment secret remains; no_claim=server is stubbed.
  it('requires saving recovery codes before returning to the on state', async () => {
    const element = await card();
    await start(element);
    const input = element.shadowRoot?.querySelector<any>('sl-input');
    input.value = '123456';
    input.dispatchEvent(new Event('sl-input'));
    element.shadowRoot
      ?.querySelector('form')
      ?.dispatchEvent(new Event('submit', { cancelable: true }));
    await waitUntil(() => !!element.shadowRoot?.querySelector('sl-checkbox'));
    const done = Array.from(
      element.shadowRoot!.querySelectorAll<any>('sl-button')
    ).find((button) => button.textContent.includes('Done'));
    expect(done.disabled).to.equal(true);
    expect(element.shadowRoot?.querySelector('img')).to.equal(null);
    expect(element.shadowRoot?.textContent).not.to.contain('SYNTHETICSECRET');
    const saved = element.shadowRoot?.querySelector<any>('sl-checkbox');
    saved.checked = true;
    saved.dispatchEvent(new Event('sl-change'));
    await element.updateComplete;
    expect(done.disabled).to.equal(false);
    done.click();
    await element.updateComplete;
    expect(element.shadowRoot?.textContent?.replace(/\s+/g, ' ')).to.contain(
      '10 recovery codes remaining'
    );
    expect(element.shadowRoot?.querySelector('ul')).to.equal(null);
    expect(localStorage.getItem('accessToken')).to.equal('upgraded');
    expect(localStorage.length).to.equal(2);
  });

  // protects=enrollment error recovery; fails_when=invalid proof hides the
  // enrollment or exposes new recovery codes; no_claim=proof validation is server-side.
  it('keeps enrollment open on an invalid code', async () => {
    const element = await card();
    await start(element);
    fetchStub.callsFake(async () =>
      json({ detail: { code: 'invalid_code' } }, 401)
    );
    element.shadowRoot
      ?.querySelector('form')
      ?.dispatchEvent(new Event('submit', { cancelable: true }));
    await waitUntil(() =>
      element.shadowRoot?.textContent?.includes('already been used')
    );
    expect(element.shadowRoot?.querySelector('img')).to.exist;
    expect(element.shadowRoot?.querySelector('sl-checkbox')).to.equal(null);
  });

  // protects=an unreadable factor the policy still requires is not offered for
  // removal; fails_when=Remove is shown and the server refuses it;
  // no_claim=server is stubbed.
  it('does not offer removal of an unreadable factor the policy requires', async () => {
    fetchStub.callsFake(async (input) => {
      const url = String(input);
      if (url.endsWith('/features')) return json({ features: {} });
      if (url.endsWith('/status'))
        return json({
          totp_enabled: true,
          totp_needs_setup: true,
          totp_removal_blocked: true,
          recovery_codes_remaining: 7,
        });
      return json({});
    });
    const element = await fixture<MfaCard>(html`<mfa-card></mfa-card>`);
    await waitUntil(() =>
      element.shadowRoot?.textContent?.includes('needs to be set up again')
    );
    const text = element.shadowRoot!.textContent!.replace(/\s+/g, ' ');
    expect(text).to.contain('Your account requires a second sign-in step.');
    expect(text).to.contain('administrator to reset your sign-in');
    expect(
      Array.from(element.shadowRoot!.querySelectorAll('sl-button')).some(
        (button) => button.textContent?.includes('Remove')
      )
    ).to.equal(false);
    expect(element.shadowRoot!.querySelector('sl-input')).to.equal(null);
  });

  // protects=policy-required authenticator is not offered for removal and a
  // server refusal is shown; fails_when=Turn off is offered or a 403 is
  // silent; no_claim=server is stubbed.
  it('explains instead of offering Turn off when policy requires the factor', async () => {
    let blocked = true;
    fetchStub.callsFake(async (input) => {
      const url = String(input);
      if (url.endsWith('/features')) return json({ features: {} });
      if (url.endsWith('/status'))
        return json({
          totp_enabled: true,
          totp_removal_blocked: blocked,
          recovery_codes_remaining: 10,
        });
      if (url.endsWith('/disable'))
        return json({ detail: { code: 'mfa_required_by_policy' } }, 403);
      return json({});
    });
    const blockedText =
      'Your account requires a second sign-in step. Add another factor before removing this one.';
    let element = await fixture<MfaCard>(html`<mfa-card></mfa-card>`);
    await waitUntil(() =>
      element.shadowRoot?.textContent?.includes('Authenticator app is on')
    );
    const buttons = () =>
      Array.from(element.shadowRoot!.querySelectorAll('sl-button')).map(
        (button) => button.textContent?.trim()
      );
    expect(element.shadowRoot!.textContent!.replace(/\s+/g, ' ')).to.contain(
      blockedText
    );
    expect(buttons()).not.to.include('Turn off');
    // Policy changed after the page loaded: the server refusal is shown.
    blocked = false;
    element = await fixture<MfaCard>(html`<mfa-card></mfa-card>`);
    await waitUntil(() => buttons().includes('Turn off'));
    const input = element.shadowRoot!.querySelector('sl-input')!;
    (input as unknown as HTMLInputElement).value = '123456';
    input.dispatchEvent(new Event('sl-input'));
    await element.updateComplete;
    Array.from(element.shadowRoot!.querySelectorAll<HTMLElement>('sl-button'))
      .find((button) => button.textContent?.trim() === 'Turn off')!
      .click();
    await answerConfirmDialog(true);
    await waitUntil(() =>
      element.shadowRoot
        ?.querySelector('[role="status"]')
        ?.textContent?.includes(blockedText)
    );
  });

  // protects=a factor this server cannot decrypt reads as needing setup and
  // can be removed with a recovery code; fails_when=it still reads as on or
  // removal asks for an authenticator code; no_claim=server is stubbed.
  it('shows an unreadable authenticator as needing setup again', async () => {
    let removed: unknown;
    fetchStub.callsFake(async (input, init) => {
      const url = String(input);
      if (url.endsWith('/features')) return json({ features: {} });
      if (url.endsWith('/status'))
        return json({
          totp_enabled: true,
          totp_needs_setup: true,
          recovery_codes_remaining: 7,
        });
      if (url.endsWith('/disable')) {
        removed = JSON.parse(init!.body as string);
        return new Response(null, { status: 204 });
      }
      return json({});
    });
    const element = await fixture<MfaCard>(html`<mfa-card></mfa-card>`);
    await waitUntil(() =>
      element.shadowRoot?.textContent?.includes('needs to be set up again')
    );
    const text = element.shadowRoot!.textContent!;
    expect(text).not.to.contain('Authenticator app is on');
    expect(
      element.shadowRoot?.querySelector('sl-input')?.getAttribute('label')
    ).to.equal('Recovery code');
    const input = element.shadowRoot!.querySelector('sl-input')!;
    (input as unknown as HTMLInputElement).value = 'abcde-fghij';
    input.dispatchEvent(new Event('sl-input'));
    await element.updateComplete;
    Array.from(element.shadowRoot!.querySelectorAll<HTMLElement>('sl-button'))
      .find((button) => button.textContent?.includes('Remove'))!
      .click();
    await answerConfirmDialog(true);
    await waitUntil(() =>
      element.shadowRoot?.textContent?.includes('Set up authenticator app')
    );
    expect(removed).to.deep.equal({ recovery_code: 'abcde-fghij' });
  });

  // protects=one-time material erased on detach; fails_when=secret remains
  // in a reusable component; no_claim=browser heap zeroization.
  it('clears enrollment material when disconnected', async () => {
    const element = await card();
    await start(element);
    element.remove();
    await element.updateComplete;
    expect(element.shadowRoot?.textContent).not.to.contain('SYNTHETICSECRET');
    expect(element.shadowRoot?.querySelector('img')).to.equal(null);
  });
});
