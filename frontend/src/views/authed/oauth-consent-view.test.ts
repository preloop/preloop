import { expect, fixture, html, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';
import './oauth-consent-view';
import type { OAuthConsentView } from './oauth-consent-view';

describe('OAuth login guidance', () => {
  const originalUrl = window.location.href;
  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-token');
  });
  afterEach(() => {
    sinon.restore();
    localStorage.clear();
    history.replaceState({}, '', originalUrl);
  });
  async function mount(
    redirect = 'urn:ietf:wg:oauth:2.0:oob',
    client = 'cli'
  ): Promise<OAuthConsentView> {
    history.replaceState(
      {},
      '',
      `/console/authorize?client_id=${client}&redirect_uri=${encodeURIComponent(redirect)}&state=example-state`
    );
    return fixture<OAuthConsentView>(
      html`<oauth-consent-view></oauth-consent-view>`
    );
  }
  it('explains manual approval and keeps the code as the primary next step', async () => {
    const fetchStub = sinon.stub(window, 'fetch').callsFake(
      async () =>
        new Response(
          JSON.stringify({
            action: 'manual',
            code: 'SYNTHETIC-ONE-TIME-CODE',
          })
        )
    );
    const el = await mount();
    expect(el.shadowRoot!.textContent).to.contain('paste');
    expect(el.shadowRoot!.textContent).to.contain(
      'Successfully authenticated!'
    );
    (el.shadowRoot!.querySelector('sl-button') as HTMLElement).click();
    await waitUntil(() => !!el.shadowRoot!.querySelector('.manual-code-box'));
    expect(el.shadowRoot!.textContent).to.contain(
      'Finish login in your terminal'
    );
    expect(el.shadowRoot!.textContent).not.to.contain(
      'Authorization Successful'
    );
    expect(el.shadowRoot!.textContent).not.to.contain(
      'Continue to your agents'
    );
    const copy = el.shadowRoot!.querySelector('sl-copy-button') as any;
    expect(copy.value).to.equal('SYNTHETIC-ONE-TIME-CODE');
    expect(el.shadowRoot!.textContent).to.contain('before closing this tab');
    const payload = JSON.parse(
      fetchStub.getCalls().find((call) => call.args[1]?.method === 'POST')!
        .args[1].body
    );
    expect(payload.state).to.equal('example-state');
    expect(payload.redirect_uri).to.equal('urn:ietf:wg:oauth:2.0:oob');
  });
  it('explains that loopback login completes automatically in the waiting terminal', async () => {
    const el = await mount('http://127.0.0.1:12345/callback');
    expect(el.shadowRoot!.textContent).to.contain('automatically');
    expect(el.shadowRoot!.textContent).to.contain(
      'Successfully authenticated!'
    );
    expect(el.shadowRoot!.textContent).not.to.contain('paste');
  });
  it('does not give CLI instructions to other OAuth clients', async () => {
    const el = await mount('https://example.com/callback', 'other-client');
    expect(el.shadowRoot!.textContent).not.to.contain(
      'Successfully authenticated!'
    );
    expect(el.shadowRoot!.textContent).to.contain('Authorize Application');
  });
  it('keeps manual authorization instructions generic for another client', async () => {
    sinon
      .stub(window, 'fetch')
      .callsFake(
        async () =>
          new Response(
            JSON.stringify({ action: 'manual', code: 'SYNTHETIC-CODE' })
          )
      );
    const el = await mount('urn:ietf:wg:oauth:2.0:oob', 'other-client');
    (el.shadowRoot!.querySelector('sl-button') as HTMLElement).click();
    await waitUntil(() => !!el.shadowRoot!.querySelector('.manual-code-box'));
    expect(el.shadowRoot!.textContent).to.contain(
      'Finish authorization in your application'
    );
    expect(el.shadowRoot!.textContent).not.to.contain('preloop login');
    expect(el.shadowRoot!.textContent).not.to.contain(
      'Successfully authenticated!'
    );
  });

  it('keeps consent errors visible without showing a code', async () => {
    sinon
      .stub(window, 'fetch')
      .resolves(
        new Response(
          JSON.stringify({ detail: 'Request expired. Start again.' }),
          { status: 400 }
        )
      );
    const el = await mount();
    (el.shadowRoot!.querySelector('sl-button') as HTMLElement).click();
    await waitUntil(() =>
      el.shadowRoot?.textContent?.includes('Request expired. Start again.')
    );
    expect(el.shadowRoot!.querySelector('.manual-code-box')).to.equal(null);
    expect((el.shadowRoot!.querySelector('sl-button') as any).loading).to.equal(
      false
    );
  });
});
