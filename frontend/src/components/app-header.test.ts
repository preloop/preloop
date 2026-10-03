import { expect, fixture, fixtureCleanup, html } from '@open-wc/testing';
import sinon from 'sinon';

import { LOCATION_CHANGED } from '../router';
import type { AppHeader } from './app-header';
import './app-header';

/** Removed headers must not stay reachable through document-wide events. */
describe('AppHeader event lifecycle', () => {
  let mountedHeader: AppHeader | undefined;

  beforeEach(() => {
    (window as Window & { BRAND_CONFIG?: unknown }).BRAND_CONFIG = {
      name: 'Synthetic',
      edition: 'selfhosted',
      branding: {
        logo_light: '/public/images/flow.svg',
        logo_dark: '/public/images/flow.svg',
      },
    };
    localStorage.clear();
    sinon.stub(window, 'fetch').resolves(
      new Response(JSON.stringify({ features: { registration: true } }), {
        headers: { 'Content-Type': 'application/json' },
      })
    );
  });

  afterEach(() => {
    // The reconnect case moves this element outside the fixture wrapper.
    mountedHeader?.remove();
    mountedHeader = undefined;
    fixtureCleanup();
    sinon.restore();
    delete (window as Window & { BRAND_CONFIG?: unknown }).BRAND_CONFIG;
    localStorage.clear();
  });

  const mount = async () => {
    const header = await fixture<AppHeader>(html`<app-header></app-header>`);
    mountedHeader = header;
    await header.checkBillingEnabled();
    await header.updateComplete;
    return header;
  };

  it('stops reacting to auth and routing events when removed', async () => {
    const header = await mount();
    const auth = sinon.spy(header, 'checkAuth');
    const update = sinon.spy(header, 'requestUpdate');
    header.remove();
    await header.updateComplete;
    auth.resetHistory();
    update.resetHistory();

    window.dispatchEvent(new CustomEvent('auth-change'));
    window.dispatchEvent(new CustomEvent(LOCATION_CHANGED));
    await header.updateComplete;

    expect(auth.callCount).to.equal(0);
    expect(update.callCount).to.equal(0);
  });

  it('responds once per event after repeated reconnections', async () => {
    const header = await mount();
    for (let i = 0; i < 3; i++) {
      header.remove();
      document.body.append(header);
      await header.checkBillingEnabled();
      await header.updateComplete;
    }
    const auth = sinon.spy(header, 'checkAuth');
    const update = sinon.spy(header, 'requestUpdate');

    window.dispatchEvent(new CustomEvent('auth-change'));
    expect(auth.callCount).to.equal(1);
    update.resetHistory();
    window.dispatchEvent(new CustomEvent(LOCATION_CHANGED));
    expect(update.callCount).to.equal(1);
  });
});
