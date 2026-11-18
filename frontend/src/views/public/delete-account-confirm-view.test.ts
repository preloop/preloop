import { html, fixture, expect } from '@open-wc/testing';
import sinon from 'sinon';
import './delete-account-confirm-view';
import {
  CONFIRM_LINK_ERROR,
  DeleteAccountConfirmView,
} from './delete-account-confirm-view';

const BRAND_CONFIG: any = {
  name: 'Test Brand',
  domain: 'test.example.com',
  company: { legal_name: 'Test Co', address: '123 Test', city: 'Test' },
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

const tick = (ms = 50) => new Promise((r) => setTimeout(r, ms));

describe('DeleteAccountConfirmView', () => {
  let fetchStub: sinon.SinonStub;

  beforeEach(() => {
    (window as any).BRAND_CONFIG = BRAND_CONFIG;
    fetchStub = sinon.stub(window, 'fetch');
    window.history.replaceState({}, '', '/delete-account/confirm?token=t0k');
  });

  afterEach(() => {
    fetchStub.restore();
    delete (window as any).BRAND_CONFIG;
    window.history.replaceState({}, '', '/');
  });

  it('sends nothing until the button is pressed', async () => {
    await fixture(
      html`<delete-account-confirm-view></delete-account-confirm-view>`
    );
    await tick();
    expect(fetchStub.called).to.be.false;
  });

  it('confirms with the token from the link', async () => {
    fetchStub.callsFake(
      async () =>
        new Response(
          JSON.stringify({ status: 'received', message: 'Received.' }),
          { status: 200 }
        )
    );
    const el = (await fixture(
      html`<delete-account-confirm-view></delete-account-confirm-view>`
    )) as DeleteAccountConfirmView;
    (el as any)._reason = 'moving on';
    await (el as any)._confirm();
    await el.updateComplete;
    const call = fetchStub.getCall(0);
    expect(String(call.args[0])).to.contain(
      '/account/deletion-request/confirm'
    );
    expect(JSON.parse(call.args[1].body)).to.deep.equal({
      token: 't0k',
      reason: 'moving on',
    });
    expect(el.shadowRoot?.textContent).to.contain('Received.');
  });

  it('explains an invalid or used link', async () => {
    fetchStub.callsFake(
      async () =>
        new Response(JSON.stringify({ detail: 'nope' }), { status: 400 })
    );
    const el = (await fixture(
      html`<delete-account-confirm-view></delete-account-confirm-view>`
    )) as DeleteAccountConfirmView;
    await (el as any)._confirm();
    await el.updateComplete;
    expect(el.shadowRoot?.textContent).to.contain(CONFIRM_LINK_ERROR);
  });
});
