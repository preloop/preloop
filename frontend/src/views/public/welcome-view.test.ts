import { html, fixture, expect } from '@open-wc/testing';
import sinon from 'sinon';
import './welcome-view';
import { WelcomeView } from './welcome-view';

const BRAND_CONFIG: any = {
  name: 'Test Brand',
  domain: 'test.example.com',
  edition: 'saas',
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

describe('WelcomeView', () => {
  let fetchStub: sinon.SinonStub;

  beforeEach(() => {
    (window as any).BRAND_CONFIG = BRAND_CONFIG;
    localStorage.clear();
    fetchStub = sinon.stub(window, 'fetch');
  });

  afterEach(() => {
    fetchStub.restore();
    delete (window as any).BRAND_CONFIG;
    localStorage.clear();
  });

  async function mount(): Promise<WelcomeView> {
    return (await fixture(html`<welcome-view></welcome-view>`)) as WelcomeView;
  }

  it('shows an error when no account details are present in the URL', async () => {
    const el = await mount();
    await el.updateComplete;
    expect(el.shadowRoot?.querySelector('.error-message')).to.exist;
    expect(el.shadowRoot?.textContent).to.contain(
      'Could not retrieve your details'
    );
  });

  it('renders the password onboarding form for a new user', async () => {
    const el = await mount();
    (el as any)._username = 'bob';
    (el as any)._email = 'bob@example.com';
    (el as any)._claimToken = 'claim-token-abc';
    (el as any)._error = '';
    await el.updateComplete;
    expect(el.shadowRoot?.querySelector('#password')).to.exist;
    expect(el.shadowRoot?.textContent).to.contain('Welcome to Test Brand');
  });

  it('says account, not organization, on both onboarding steps', async () => {
    const el = await mount();
    (el as any)._username = 'bob';
    (el as any)._email = 'bob@example.com';
    (el as any)._claimToken = 'claim-token-abc';
    (el as any)._error = '';
    await el.updateComplete;
    const labels = () =>
      [...el.shadowRoot!.querySelectorAll('sl-input')].map((i) =>
        i.getAttribute('label')
      );
    expect(labels()).to.include('Account name');
    expect(el.shadowRoot!.textContent).not.to.match(/organi[sz]ation/i);
    expect(labels().join(' ')).not.to.match(/organi[sz]ation/i);

    (el as any)._needsPassword = false;
    await el.updateComplete;
    expect(labels()).to.include('Account name');
    expect(el.shadowRoot!.textContent).not.to.match(/organi[sz]ation/i);
  });

  it('rejects a password shorter than 8 characters without calling the API', async () => {
    const el = await mount();
    (el as any)._username = 'bob';
    (el as any)._email = 'bob@example.com';
    (el as any)._claimToken = 'claim-token-abc';
    (el as any)._error = '';
    await el.updateComplete;
    const pw = el.shadowRoot?.querySelector('#password') as any;
    pw.value = 'short';
    await (el as any)._handleOnboardingSubmit(new Event('submit'));
    await el.updateComplete;
    expect((el as any)._error).to.contain('at least 8 characters');
    expect(
      fetchStub
        .getCalls()
        .some((c) => String(c.args[0]).includes('complete-onboarding'))
    ).to.be.false;
  });

  it('completes onboarding and stores tokens on success', async () => {
    fetchStub.callsFake(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('complete-onboarding')) {
        return new Response(
          JSON.stringify({
            access_token: 'acc-123',
            refresh_token: 'ref-123',
          }),
          { status: 200 }
        );
      }
      return new Response(JSON.stringify({ organization_name: 'Acme' }), {
        status: 200,
      });
    });
    const el = await mount();
    (el as any)._username = 'bob';
    (el as any)._email = 'bob@example.com';
    (el as any)._claimToken = 'claim-token-abc';
    (el as any)._error = '';
    await el.updateComplete;
    const pw = el.shadowRoot?.querySelector('#password') as any;
    pw.value = 'longenough1';
    await (el as any)._handleOnboardingSubmit(new Event('submit'));
    await tick();
    await el.updateComplete;
    expect(localStorage.getItem('accessToken')).to.equal('acc-123');
    expect((el as any)._error).to.equal('');
    expect((el as any)._needsPassword).to.be.false;
  });

  it('collects the name and sends it with the password', async () => {
    // The Stripe-first signup asks for two things here and only two: a name
    // to address the person by and a password to sign in with. The email came
    // from the completed checkout and is not editable.
    fetchStub.callsFake(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('complete-onboarding')) {
        return new Response(
          JSON.stringify({ access_token: 'acc-1', refresh_token: 'ref-1' }),
          { status: 200 }
        );
      }
      return new Response(JSON.stringify({}), { status: 200 });
    });
    const el = await mount();
    (el as any)._username = 'bob';
    (el as any)._email = 'bob@example.com';
    (el as any)._claimToken = 'claim-token-abc';
    (el as any)._error = '';
    await el.updateComplete;
    const nameInput = el.shadowRoot?.querySelector('#full-name') as any;
    expect(nameInput, 'name field').to.exist;
    (el as any)._fullName = 'Bobbie Tables';
    const pw = el.shadowRoot?.querySelector('#password') as any;
    pw.value = 'longenough1';
    await (el as any)._handleOnboardingSubmit(new Event('submit'));
    await tick();

    const call = fetchStub
      .getCalls()
      .find((c) => String(c.args[0]).includes('complete-onboarding'));
    expect(
      JSON.parse(String((call?.args[1] as RequestInit).body))
    ).to.deep.equal({
      email: 'bob@example.com',
      username: 'bob',
      password: 'longenough1',
      full_name: 'Bobbie Tables',
      claim_token: 'claim-token-abc',
    });
  });

  it('prefills the name Stripe already collected', async () => {
    const original = window.location.pathname + window.location.search;
    history.replaceState(
      {},
      '',
      '/welcome?username=bob&email=bob%40example.com&full_name=Bobbie%20Tables&needs_password=true&claim_token=claim-token-abc'
    );
    try {
      const el = await mount();
      await el.updateComplete;
      expect((el as any)._fullName).to.equal('Bobbie Tables');
      const nameInput = el.shadowRoot?.querySelector('#full-name') as any;
      expect(nameInput?.value).to.equal('Bobbie Tables');
    } finally {
      history.replaceState({}, '', original);
    }
  });
  it('carries the claim token from the query string into the request', async () => {
    // The token is the credential. It arrives in the welcome link minted by
    // checkout success and has to reach the server unchanged, or the claim is
    // refused.
    fetchStub.callsFake(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('complete-onboarding')) {
        return new Response(
          JSON.stringify({ access_token: 'acc-1', refresh_token: 'ref-1' }),
          { status: 200 }
        );
      }
      return new Response(JSON.stringify({}), { status: 200 });
    });
    const original = window.location.pathname + window.location.search;
    history.replaceState(
      {},
      '',
      '/welcome?username=bob&email=bob%40example.com&needs_password=true&claim_token=tok.en.123'
    );
    try {
      const el = await mount();
      await el.updateComplete;
      expect((el as any)._claimToken).to.equal('tok.en.123');
      expect((el as any)._error).to.equal('');
      const pw = el.shadowRoot?.querySelector('#password') as any;
      pw.value = 'longenough1';
      await (el as any)._handleOnboardingSubmit(new Event('submit'));
      await tick();

      const call = fetchStub
        .getCalls()
        .find((c) => String(c.args[0]).includes('complete-onboarding'));
      expect(
        JSON.parse(String((call?.args[1] as RequestInit).body)).claim_token
      ).to.equal('tok.en.123');
    } finally {
      history.replaceState({}, '', original);
    }
  });

  it('refuses a welcome link with no claim token and never posts', async () => {
    // Knowing the address is not a credential. A link without a token is an
    // expired or hand-made one, and the recovery is the password reset email.
    const original = window.location.pathname + window.location.search;
    history.replaceState(
      {},
      '',
      '/welcome?username=bob&email=bob%40example.com&needs_password=true'
    );
    try {
      const el = await mount();
      await el.updateComplete;
      expect((el as any)._error).to.contain('no longer valid');
      expect((el as any)._error).to.contain('Forgot password');

      const pw = el.shadowRoot?.querySelector('#password') as any;
      pw.value = 'longenough1';
      await (el as any)._handleOnboardingSubmit(new Event('submit'));
      await tick();

      expect(
        fetchStub
          .getCalls()
          .some((c) => String(c.args[0]).includes('complete-onboarding'))
      ).to.be.false;
    } finally {
      history.replaceState({}, '', original);
    }
  });
});
