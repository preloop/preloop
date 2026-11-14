import { expect, fixture, html, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';
import './mfa-card';
import { MfaCard } from './mfa-card';
import { invalidateApiCaches } from '../api';

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
