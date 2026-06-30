import { html, fixture, expect } from '@open-wc/testing';
import sinon from 'sinon';
import './delete-account-view';
import { DeleteAccountView } from './delete-account-view';

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

const tick = (ms = 150) => new Promise((r) => setTimeout(r, ms));

describe('DeleteAccountView', () => {
  let el: DeleteAccountView;
  let fetchStub: sinon.SinonStub;

  beforeEach(async () => {
    (window as any).BRAND_CONFIG = BRAND_CONFIG;
    fetchStub = sinon.stub(window, 'fetch');
    el = (await fixture(
      html`<delete-account-view></delete-account-view>`
    )) as DeleteAccountView;
  });

  afterEach(() => {
    fetchStub.restore();
    delete (window as any).BRAND_CONFIG;
  });

  const calledDeletion = () =>
    fetchStub
      .getCalls()
      .some((c) =>
        String(c.args[0]).includes('/api/v1/account/deletion-request')
      );

  it('renders the deletion request form and data-retention info', () => {
    expect(el.shadowRoot?.querySelector('form')).to.exist;
    expect(el.shadowRoot?.querySelector('sl-button[variant="danger"]')).to
      .exist;
    expect(el.shadowRoot?.textContent).to.contain(
      'What happens when you delete your account'
    );
  });

  it('submits a deletion request and shows a success state', async () => {
    fetchStub.callsFake(
      async () => new Response(JSON.stringify({}), { status: 200 })
    );
    (el as any)._email = 'user@example.com';
    (el as any)._username = 'user';
    await (el as any)._handleSubmit(new Event('submit'));
    await tick();
    await el.updateComplete;
    expect((el as any)._success).to.be.true;
    expect(el.shadowRoot?.textContent).to.contain('Request Received');
    expect(calledDeletion()).to.be.true;
  });

  it('shows an error when the request fails', async () => {
    fetchStub.callsFake(async () => new Response('nope', { status: 500 }));
    (el as any)._email = 'user@example.com';
    (el as any)._username = 'user';
    await (el as any)._handleSubmit(new Event('submit'));
    await tick();
    await el.updateComplete;
    expect((el as any)._success).to.be.false;
    expect((el as any)._error).to.contain('Failed to submit request');
    const alert = el.shadowRoot?.querySelector('sl-alert[variant="danger"]');
    expect(alert).to.exist;
  });
});
