import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';
import './register-view';
import {
  RegisterView,
  humanizeRegisterError,
  parseBootstrapFragment,
} from './register-view';
import { invalidateApiCaches } from '../../api';

describe('RegisterView', () => {
  let element: RegisterView;
  let fetchStub: any;

  beforeEach(async () => {
    // Set up minimal BRAND_CONFIG for getBrandConfig()
    (window as any).BRAND_CONFIG = {
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

    element = await fixture(html`<register-view></register-view>`);
    // Stub fetch after initial render so startup requests don't wedge WTR.
    fetchStub = sinon.stub(window, 'fetch');
  });

  afterEach(() => {
    // Restore fetch after each test
    fetchStub.restore();
    // Clean up BRAND_CONFIG
    delete (window as any).BRAND_CONFIG;
    invalidateApiCaches();
  });

  it('should render the registration form', () => {
    const form = element.shadowRoot?.querySelector('form');
    expect(form).to.exist;
    const usernameInput = element.shadowRoot?.querySelector('#username');
    expect(usernameInput).to.exist;
    const emailInput = element.shadowRoot?.querySelector('#email');
    expect(emailInput).to.exist;
    const passwordInput = element.shadowRoot?.querySelector('#password');
    expect(passwordInput).to.exist;
    const registerButton = element.shadowRoot?.querySelector(
      'sl-button[type="submit"]'
    );
    expect(registerButton).to.exist;
  });

  it('should show an error message on failed registration', async () => {
    // Stub fetch to simulate a failed registration with error detail
    fetchStub.resolves(
      new Response(JSON.stringify({ detail: 'Email already registered' }), {
        status: 400,
      })
    );

    // Fill in the form fields
    const usernameInput = element.shadowRoot?.querySelector<any>('#username');
    const emailInput = element.shadowRoot?.querySelector<any>('#email');
    const passwordInput = element.shadowRoot?.querySelector<any>('#password');
    usernameInput.value = 'testuser';
    emailInput.value = 'test@example.com';
    passwordInput.value = 'password123';

    const form = element.shadowRoot?.querySelector('form') as HTMLFormElement;
    const submitEvent = new SubmitEvent('submit', {
      bubbles: true,
      cancelable: true,
    });
    form.dispatchEvent(submitEvent);

    // Wait until the error message appears
    await waitUntil(
      () => element.shadowRoot?.querySelector('.error-message'),
      'Error message did not appear'
    );

    const errorMessage = element.shadowRoot?.querySelector('.error-message');
    expect(errorMessage).to.exist;
    expect(errorMessage?.textContent).to.contain('Email already registered');
    expect(fetchStub).to.have.been.calledOnce;
  });

  it('should not show an error message on successful registration', async () => {
    // Stub fetch to simulate a successful registration AND a successful
    // auto-login. After registering, the view tries to log the user in
    // automatically so the CLI OAuth consent flow (and any other pending
    // loginRedirect) can continue without bouncing through the sign-in page.
    fetchStub.callsFake(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString();
      if (url.includes('/api/v1/auth/token/json')) {
        return new Response(
          JSON.stringify({
            access_token: 'test-token',
            refresh_token: 'test-refresh',
          }),
          { status: 200 }
        );
      }
      return new Response(JSON.stringify({}), { status: 200 });
    });

    // Fill in the form fields
    const usernameInput = element.shadowRoot?.querySelector<any>('#username');
    const emailInput = element.shadowRoot?.querySelector<any>('#email');
    const passwordInput = element.shadowRoot?.querySelector<any>('#password');
    usernameInput.value = 'testuser';
    emailInput.value = 'test@example.com';
    passwordInput.value = 'password123';

    const form = element.shadowRoot?.querySelector('form') as HTMLFormElement;
    const submitEvent = new SubmitEvent('submit', {
      bubbles: true,
      cancelable: true,
    });
    form.dispatchEvent(submitEvent);

    // Wait for the async operation to complete
    await new Promise((resolve) => setTimeout(resolve, 100));
    await element.updateComplete;

    // Check that no error message appears
    const errorMessage = element.shadowRoot?.querySelector('.error-message');
    expect(errorMessage).to.not.exist;

    // Both /register and /token/json should have been called.
    expect(fetchStub).to.have.been.calledTwice;
    expect(localStorage.getItem('accessToken')).to.equal('test-token');
    localStorage.removeItem('accessToken');
    localStorage.removeItem('refreshToken');
  });

  it('should have a link to the login page', () => {
    const loginLink = element.shadowRoot?.querySelector('a[href="/login"]');
    expect(loginLink).to.exist;
    expect(loginLink?.textContent).to.contain(
      'Already have an account? Sign In'
    );
  });

  it('shows a persistent password rule under the password field', () => {
    const passwordInput = element.shadowRoot?.querySelector<any>('#password');
    expect(passwordInput?.getAttribute('help-text')).to.equal(
      'At least 8 characters.'
    );
  });

  it('humanizes raw pydantic email validation errors', async () => {
    // FastAPI 422 shape: detail is a list of {msg} items. The raw prose must
    // never reach the user.
    fetchStub.resolves(
      new Response(
        JSON.stringify({
          detail: [
            {
              msg: 'value is not a valid email address: The part after the @-sign is a special-use or reserved name that cannot be used with email.',
            },
          ],
        }),
        { status: 422 }
      )
    );

    const usernameInput = element.shadowRoot?.querySelector<any>('#username');
    const emailInput = element.shadowRoot?.querySelector<any>('#email');
    const passwordInput = element.shadowRoot?.querySelector<any>('#password');
    usernameInput.value = 'testuser';
    emailInput.value = 'name@preloop.test';
    passwordInput.value = 'password123';

    const form = element.shadowRoot?.querySelector('form') as HTMLFormElement;
    form.dispatchEvent(
      new SubmitEvent('submit', { bubbles: true, cancelable: true })
    );

    await waitUntil(
      () => element.shadowRoot?.querySelector('.error-message'),
      'Error message did not appear'
    );
    const errorMessage = element.shadowRoot?.querySelector('.error-message');
    expect(errorMessage?.textContent).to.contain(
      "That email address doesn't look deliverable — check the part after the @."
    );
    expect(errorMessage?.textContent).to.not.contain('special-use');
  });

  describe('humanizeRegisterError', () => {
    it('maps pydantic email prose to the first-class string', () => {
      expect(
        humanizeRegisterError(
          'value is not a valid email address: The part after the @-sign is a special-use or reserved name that cannot be used with email.'
        )
      ).to.equal(
        "That email address doesn't look deliverable — check the part after the @."
      );
    });

    it('maps password length errors to the first-class string', () => {
      expect(
        humanizeRegisterError('String should have at least 8 characters')
      ).to.equal('Passwords need at least 8 characters.');
      expect(
        humanizeRegisterError('password must be at least 8 characters')
      ).to.equal('Passwords need at least 8 characters.');
    });

    it('does not treat unrelated length messages as password errors', () => {
      expect(
        humanizeRegisterError('Name should have at least 8 characters')
      ).to.equal("That didn't work: Name should have at least 8 characters");
    });

    it('wraps unmapped errors in plain language', () => {
      expect(humanizeRegisterError('Username already registered')).to.equal(
        "That didn't work: Username already registered"
      );
    });

    it('falls back on empty messages', () => {
      expect(humanizeRegisterError('')).to.equal('Failed to create an account');
    });
  });

  describe('first-account context', () => {
    it('renders the first-account note when the instance has no users yet', async () => {
      fetchStub.callsFake(async (input: RequestInfo | URL) => {
        const url = typeof input === 'string' ? input : input.toString();
        if (url.includes('/api/v1/features')) {
          return new Response(
            JSON.stringify({
              plugins: [],
              features: { registration: true, first_account_pending: true },
            }),
            { status: 200, headers: { 'Content-Type': 'application/json' } }
          );
        }
        return new Response(JSON.stringify({}), { status: 200 });
      });
      invalidateApiCaches();

      const el = (await fixture(
        html`<register-view></register-view>`
      )) as RegisterView;
      await waitUntil(
        () => el.shadowRoot?.querySelector('.first-account-note'),
        'first-account note did not appear'
      );
      const note = el.shadowRoot?.querySelector('.first-account-note');
      expect(note?.textContent?.replace(/\s+/g, ' ')).to.contain(
        "You're creating the first account on this instance — it becomes the admin account."
      );
    });

    it('omits the note once users exist', async () => {
      fetchStub.callsFake(async (input: RequestInfo | URL) => {
        const url = typeof input === 'string' ? input : input.toString();
        if (url.includes('/api/v1/features')) {
          return new Response(
            JSON.stringify({
              plugins: [],
              features: { registration: true, first_account_pending: false },
            }),
            { status: 200, headers: { 'Content-Type': 'application/json' } }
          );
        }
        return new Response(JSON.stringify({}), { status: 200 });
      });
      invalidateApiCaches();

      const el = (await fixture(
        html`<register-view></register-view>`
      )) as RegisterView;
      await new Promise((resolve) => setTimeout(resolve, 100));
      await el.updateComplete;
      expect(el.shadowRoot?.querySelector('.first-account-note')).to.not.exist;
    });
  });

  describe('parseBootstrapFragment', () => {
    it('extracts the token from a #bootstrap fragment', () => {
      expect(parseBootstrapFragment('#bootstrap=abc123')).to.equal('abc123');
    });

    it('decodes URI-encoded tokens', () => {
      expect(parseBootstrapFragment('#bootstrap=a%2Bb')).to.equal('a+b');
    });

    it('returns empty for other fragments and empty hashes', () => {
      expect(parseBootstrapFragment('')).to.equal('');
      expect(parseBootstrapFragment('#other=1')).to.equal('');
      expect(parseBootstrapFragment('#bootstrap=')).to.equal('');
    });
  });

  describe('bootstrap setup link', () => {
    const featuresResponse = (bootstrapPending: boolean) =>
      new Response(
        JSON.stringify({
          plugins: [],
          features: {
            registration: true,
            first_account_pending: true,
            registration_bootstrap_pending: bootstrapPending,
          },
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } }
      );

    afterEach(() => {
      // Never leak a test fragment into the next test.
      history.replaceState(null, '', window.location.pathname);
      localStorage.removeItem('accessToken');
      localStorage.removeItem('refreshToken');
    });

    it('reads the fragment token, strips it from the URL, and submits it', async () => {
      window.location.hash = '#bootstrap=tok1234567890';
      fetchStub.callsFake(async (input: RequestInfo | URL) => {
        const url = typeof input === 'string' ? input : input.toString();
        if (url.includes('/api/v1/features')) {
          return featuresResponse(true);
        }
        if (url.includes('/api/v1/auth/token/json')) {
          return new Response(
            JSON.stringify({
              access_token: 'test-token',
              refresh_token: 'test-refresh',
            }),
            { status: 200 }
          );
        }
        return new Response(JSON.stringify({}), { status: 200 });
      });
      invalidateApiCaches();

      const el = (await fixture(
        html`<register-view></register-view>`
      )) as RegisterView;

      // The fragment is stripped immediately on load.
      expect(window.location.hash).to.equal('');

      const usernameInput = el.shadowRoot?.querySelector<any>('#username');
      const emailInput = el.shadowRoot?.querySelector<any>('#email');
      const passwordInput = el.shadowRoot?.querySelector<any>('#password');
      usernameInput.value = 'firstadmin';
      emailInput.value = 'admin@example.com';
      passwordInput.value = 'password123';

      const form = el.shadowRoot?.querySelector('form') as HTMLFormElement;
      form.dispatchEvent(
        new SubmitEvent('submit', { bubbles: true, cancelable: true })
      );
      await new Promise((resolve) => setTimeout(resolve, 100));
      await el.updateComplete;

      const registerCall = fetchStub
        .getCalls()
        .find((call: any) => String(call.args[0]).includes('/auth/register'));
      expect(registerCall).to.exist;
      const body = JSON.parse(registerCall.args[1].body);
      expect(body.bootstrap_token).to.equal('tok1234567890');
    });

    it('shows no notice when a valid token is present (form looks normal)', async () => {
      window.location.hash = '#bootstrap=tok1234567890';
      fetchStub.callsFake(async (input: RequestInfo | URL) => {
        const url = typeof input === 'string' ? input : input.toString();
        if (url.includes('/api/v1/features')) {
          return featuresResponse(true);
        }
        return new Response(JSON.stringify({}), { status: 200 });
      });
      invalidateApiCaches();

      const el = (await fixture(
        html`<register-view></register-view>`
      )) as RegisterView;
      await new Promise((resolve) => setTimeout(resolve, 100));
      await el.updateComplete;

      expect(el.shadowRoot?.querySelector('.bootstrap-notice')).to.not.exist;
      expect(el.shadowRoot?.querySelector('form')).to.exist;
    });

    it('shows the amber setup-link notice when bootstrap is pending and no token is present', async () => {
      fetchStub.callsFake(async (input: RequestInfo | URL) => {
        const url = typeof input === 'string' ? input : input.toString();
        if (url.includes('/api/v1/features')) {
          return featuresResponse(true);
        }
        return new Response(JSON.stringify({}), { status: 200 });
      });
      invalidateApiCaches();

      const el = (await fixture(
        html`<register-view></register-view>`
      )) as RegisterView;
      await waitUntil(
        () => el.shadowRoot?.querySelector('.bootstrap-notice'),
        'setup-link notice did not appear'
      );

      const notice = el.shadowRoot?.querySelector('.bootstrap-notice');
      expect(notice?.classList.contains('claimed')).to.be.false;
      expect(notice?.getAttribute('aria-live')).to.equal('assertive');
      expect(notice?.textContent?.replace(/\s+/g, ' ')).to.contain(
        'Setup link required'
      );
      expect(notice?.textContent?.replace(/\s+/g, ' ')).to.contain(
        'Use the link printed at the end of the install, or run create_first_user.py on the server.'
      );
      expect(
        el.shadowRoot?.querySelector('form')?.getAttribute('aria-describedby')
      ).to.equal('bootstrap-notice');
    });

    it('shows the error-red already-claimed notice on a 403 with a token', async () => {
      window.location.hash = '#bootstrap=staletoken123';
      fetchStub.callsFake(async (input: RequestInfo | URL) => {
        const url = typeof input === 'string' ? input : input.toString();
        if (url.includes('/api/v1/features')) {
          return featuresResponse(false);
        }
        if (url.includes('/auth/register')) {
          return new Response(
            JSON.stringify({
              detail:
                'Registration is disabled. Please contact an administrator for an invitation.',
            }),
            { status: 403 }
          );
        }
        return new Response(JSON.stringify({}), { status: 200 });
      });
      invalidateApiCaches();

      const el = (await fixture(
        html`<register-view></register-view>`
      )) as RegisterView;

      const usernameInput = el.shadowRoot?.querySelector<any>('#username');
      const emailInput = el.shadowRoot?.querySelector<any>('#email');
      const passwordInput = el.shadowRoot?.querySelector<any>('#password');
      usernameInput.value = 'latecomer';
      emailInput.value = 'late@example.com';
      passwordInput.value = 'password123';

      const form = el.shadowRoot?.querySelector('form') as HTMLFormElement;
      form.dispatchEvent(
        new SubmitEvent('submit', { bubbles: true, cancelable: true })
      );
      await waitUntil(
        () => el.shadowRoot?.querySelector('.bootstrap-notice.claimed'),
        'already-claimed notice did not appear'
      );

      const notice = el.shadowRoot?.querySelector('.bootstrap-notice.claimed');
      expect(notice?.getAttribute('aria-live')).to.equal('assertive');
      expect(notice?.textContent?.replace(/\s+/g, ' ')).to.contain(
        'This instance has already been claimed.'
      );
      expect(notice?.textContent?.replace(/\s+/g, ' ')).to.contain(
        'An admin account exists — ask them to invite you, or sign in'
      );
      // The generic error banner stays hidden for this state.
      expect(el.shadowRoot?.querySelector('.error-message')).to.not.exist;
    });

    it('falls back to the amber notice when the backend demands the setup link', async () => {
      window.location.hash = '#bootstrap=corruptedtoken';
      fetchStub.callsFake(async (input: RequestInfo | URL) => {
        const url = typeof input === 'string' ? input : input.toString();
        if (url.includes('/api/v1/features')) {
          return featuresResponse(true);
        }
        if (url.includes('/auth/register')) {
          return new Response(
            JSON.stringify({
              detail:
                'Setup link required. Use the link printed at the end of the install, or run create_first_user.py on the server.',
            }),
            { status: 403 }
          );
        }
        return new Response(JSON.stringify({}), { status: 200 });
      });
      invalidateApiCaches();

      const el = (await fixture(
        html`<register-view></register-view>`
      )) as RegisterView;

      const usernameInput = el.shadowRoot?.querySelector<any>('#username');
      const emailInput = el.shadowRoot?.querySelector<any>('#email');
      const passwordInput = el.shadowRoot?.querySelector<any>('#password');
      usernameInput.value = 'badlink';
      emailInput.value = 'bad@example.com';
      passwordInput.value = 'password123';

      const form = el.shadowRoot?.querySelector('form') as HTMLFormElement;
      form.dispatchEvent(
        new SubmitEvent('submit', { bubbles: true, cancelable: true })
      );
      await waitUntil(
        () =>
          el.shadowRoot?.querySelector('.bootstrap-notice:not(.claimed)') &&
          !el.shadowRoot?.querySelector('.error-message'),
        'setup-link notice did not appear after the 403'
      );

      const notice = el.shadowRoot?.querySelector('.bootstrap-notice');
      expect(notice?.textContent?.replace(/\s+/g, ' ')).to.contain(
        'Setup link required'
      );
    });
  });
});
