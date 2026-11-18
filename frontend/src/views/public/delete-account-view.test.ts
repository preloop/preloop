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

  it('signed out: sends only the address and reason, shows check-your-email', async () => {
    fetchStub.callsFake(
      async () =>
        new Response(
          JSON.stringify({
            status: 'confirmation_sent',
            message: 'If that address belongs to a verified account...',
          }),
          { status: 200 }
        )
    );
    (el as any)._email = 'user@example.com';
    (el as any)._reason = 'done';
    await (el as any)._handleSubmit(new Event('submit'));
    await tick();
    await el.updateComplete;
    expect(calledDeletion()).to.be.true;
    const call = fetchStub
      .getCalls()
      .find((c) => String(c.args[0]).includes('/account/deletion-request'))!;
    expect(JSON.parse(call.args[1].body)).to.deep.equal({
      email: 'user@example.com',
      reason: 'done',
    });
    expect(el.shadowRoot?.textContent).to.contain('Check your email');
  });

  it('signed out on a server without email: says to sign in', async () => {
    fetchStub.callsFake(
      async () =>
        new Response(
          JSON.stringify({
            status: 'sign_in_required',
            message: 'Sign in to request deletion of your account.',
          }),
          { status: 200 }
        )
    );
    (el as any)._email = 'user@example.com';
    await (el as any)._handleSubmit(new Event('submit'));
    await tick();
    await el.updateComplete;
    expect(el.shadowRoot?.textContent).to.contain('Sign in required');
    expect(el.shadowRoot?.querySelector('a[href="/login"]')).to.exist;
  });

  it('signed in: hides the address field and sends the session', async () => {
    localStorage.setItem('accessToken', 'synthetic-access');
    try {
      const signedIn = (await fixture(
        html`<delete-account-view></delete-account-view>`
      )) as DeleteAccountView;
      expect(signedIn.shadowRoot?.querySelector('sl-input[type="email"]')).to
        .not.exist;
      fetchStub.callsFake(
        async () =>
          new Response(
            JSON.stringify({ status: 'received', message: 'Received.' }),
            { status: 200 }
          )
      );
      await (signedIn as any)._handleSubmit(new Event('submit'));
      await tick();
      await signedIn.updateComplete;
      const call = fetchStub
        .getCalls()
        .find((c) => String(c.args[0]).includes('/account/deletion-request'))!;
      const headers = new Headers(call.args[1].headers);
      expect(headers.get('Authorization')).to.equal('Bearer synthetic-access');
      expect(JSON.parse(call.args[1].body)).to.not.have.property('email');
      expect(signedIn.shadowRoot?.textContent).to.contain('Request Received');
    } finally {
      localStorage.removeItem('accessToken');
    }
  });

  it('shows an error when the request fails', async () => {
    fetchStub.callsFake(async () => new Response('nope', { status: 500 }));
    (el as any)._email = 'user@example.com';
    await (el as any)._handleSubmit(new Event('submit'));
    await tick();
    await el.updateComplete;
    expect((el as any)._outcome).to.equal(null);
    expect((el as any)._error).to.contain('Failed to submit request');
    const alert = el.shadowRoot?.querySelector('sl-alert[variant="danger"]');
    expect(alert).to.exist;
  });
});
