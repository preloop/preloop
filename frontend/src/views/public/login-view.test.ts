import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';
import './login-view';
import { LoginView } from './login-view';
import { invalidateApiCaches } from '../../api';

describe('LoginView', () => {
  let element: LoginView;
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

    element = await fixture(html`<login-view></login-view>`);
    // Clear localStorage before each test
    localStorage.clear();
    // Stub fetch before each test
    fetchStub = sinon.stub(window, 'fetch');
  });

  afterEach(() => {
    // Restore fetch after each test
    fetchStub.restore();
    // Clean up BRAND_CONFIG
    delete (window as any).BRAND_CONFIG;
    invalidateApiCaches();
  });

  it('should render the login form', () => {
    const form = element.shadowRoot?.querySelector('form');
    expect(form).to.exist;
    const usernameInput = element.shadowRoot?.querySelector('#username');
    expect(usernameInput).to.exist;
    const passwordInput = element.shadowRoot?.querySelector('#password');
    expect(passwordInput).to.exist;
    const loginButton = element.shadowRoot?.querySelector(
      'sl-button[type="submit"]'
    );
    expect(loginButton).to.exist;
  });

  it('should show an error message on failed login', async () => {
    // Stub fetch to simulate a failed login with error detail
    fetchStub.resolves(
      new Response(JSON.stringify({ detail: 'Invalid credentials' }), {
        status: 401,
      })
    );

    // Fill in the form fields
    const usernameInput = element.shadowRoot?.querySelector<any>('#username');
    const passwordInput = element.shadowRoot?.querySelector<any>('#password');
    usernameInput.value = 'testuser';
    passwordInput.value = 'wrongpassword';

    const form = element.shadowRoot?.querySelector('form') as HTMLFormElement;
    const submitEvent = new SubmitEvent('submit', {
      bubbles: true,
      cancelable: true,
    });
    form.dispatchEvent(submitEvent);

    // Wait until the error message appears in the DOM
    await waitUntil(
      () => element.shadowRoot?.querySelector('.error-message'),
      'Error message did not appear'
    );

    const errorMessage = element.shadowRoot?.querySelector('.error-message');
    expect(errorMessage).to.exist;
    expect(errorMessage?.textContent).to.contain('Invalid credentials');
    expect(fetchStub).to.have.been.calledOnce;
  });

  it('should not show an error message on successful login', async () => {
    // Stub fetch to simulate a successful login
    fetchStub.resolves(
      new Response(JSON.stringify({ access_token: 'test_token' }), {
        status: 200,
      })
    );

    // Fill in the form fields
    const usernameInput = element.shadowRoot?.querySelector<any>('#username');
    const passwordInput = element.shadowRoot?.querySelector<any>('#password');
    usernameInput.value = 'testuser';
    passwordInput.value = 'correctpassword';

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

    // Verify fetch was called
    expect(fetchStub).to.have.been.calledOnce;

    // Verify token was stored in localStorage
    expect(localStorage.getItem('accessToken')).to.equal('test_token');
  });

  async function submitLogin(username = 'testuser', password = 'correct') {
    const usernameInput = element.shadowRoot?.querySelector<any>('#username');
    const passwordInput = element.shadowRoot?.querySelector<any>('#password');
    usernameInput.value = username;
    passwordInput.value = password;
    const form = element.shadowRoot?.querySelector('form') as HTMLFormElement;
    form.dispatchEvent(
      new SubmitEvent('submit', { bubbles: true, cancelable: true })
    );
    await new Promise((resolve) => setTimeout(resolve, 100));
    await element.updateComplete;
  }

  it('offers a resend when the address is not verified yet', async () => {
    // The password was right. Printing only the refusal would leave the
    // person retyping a password that already works, so the one action that
    // fixes it has to be on screen.
    fetchStub.resolves(
      new Response(
        JSON.stringify({
          detail: {
            code: 'email_not_verified',
            message: 'Verify your email address to finish signing in.',
            email: 'bob@example.com',
          },
        }),
        { status: 403, headers: { 'Content-Type': 'application/json' } }
      )
    );

    await submitLogin('bob', 'correct');

    const error = element.shadowRoot?.querySelector('.error-message');
    expect(error?.textContent).to.contain('Verify your email address');
    // The envelope must never be printed raw.
    expect(error?.textContent).to.not.contain('email_not_verified');
    expect(element.shadowRoot?.querySelector('#resend-verification')).to.exist;
  });

  it('shows no resend action for an ordinary wrong password', async () => {
    // The default instance does not require verification at all, so this
    // action must not appear on a normal failed sign-in.
    fetchStub.resolves(
      new Response(JSON.stringify({ detail: 'Invalid credentials' }), {
        status: 401,
      })
    );

    await submitLogin('bob', 'wrong');

    expect(element.shadowRoot?.querySelector('.error-message')).to.exist;
    expect(element.shadowRoot?.querySelector('#resend-verification')).to.not
      .exist;
  });

  it('sends the resend to the address the server named', async () => {
    fetchStub.onFirstCall().resolves(
      new Response(
        JSON.stringify({
          detail: {
            code: 'email_not_verified',
            message: 'Verify your email address to finish signing in.',
            email: 'bob@example.com',
          },
        }),
        { status: 403, headers: { 'Content-Type': 'application/json' } }
      )
    );
    fetchStub.onSecondCall().resolves(
      new Response(
        JSON.stringify({
          message: 'A new verification email is on its way.',
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } }
      )
    );

    await submitLogin('bob', 'correct');
    const resend = element.shadowRoot?.querySelector(
      '#resend-verification'
    ) as HTMLElement;
    resend.click();
    await new Promise((resolve) => setTimeout(resolve, 100));
    await element.updateComplete;

    const call = fetchStub.getCalls()[1];
    expect(String(call.args[0])).to.contain('/auth/resend-verification');
    expect(JSON.parse(String(call.args[1].body))).to.deep.equal({
      email: 'bob@example.com',
    });
    expect(
      element.shadowRoot?.querySelector('.success-message')?.textContent
    ).to.contain('on its way');
  });

  it('shows the rate limit in the server words', async () => {
    fetchStub.onFirstCall().resolves(
      new Response(
        JSON.stringify({
          detail: {
            code: 'email_not_verified',
            message: 'Verify your email address to finish signing in.',
            email: 'bob@example.com',
          },
        }),
        { status: 403, headers: { 'Content-Type': 'application/json' } }
      )
    );
    fetchStub.onSecondCall().resolves(
      new Response(
        JSON.stringify({
          detail:
            'Too many verification emails requested. Wait a few minutes and try again.',
        }),
        { status: 429, headers: { 'Content-Type': 'application/json' } }
      )
    );

    await submitLogin('bob', 'correct');
    (
      element.shadowRoot?.querySelector('#resend-verification') as HTMLElement
    ).click();
    await new Promise((resolve) => setTimeout(resolve, 100));
    await element.updateComplete;

    expect(
      element.shadowRoot?.querySelector('.error-message')?.textContent
    ).to.contain('Too many verification emails');
  });

  it('announces a failed sign-in to screen readers', async () => {
    fetchStub.resolves(
      new Response(JSON.stringify({ detail: 'Invalid credentials' }), {
        status: 401,
      })
    );
    await submitLogin('bob', 'wrong');
    expect(
      element.shadowRoot?.querySelector('.error-message')?.getAttribute('role')
    ).to.equal('alert');
  });

  it('gives password managers the fields they look for', () => {
    expect(
      element.shadowRoot
        ?.querySelector('#username')
        ?.getAttribute('autocomplete')
    ).to.equal('username');
    expect(
      element.shadowRoot
        ?.querySelector('#password')
        ?.getAttribute('autocomplete')
    ).to.equal('current-password');
  });

  it('shows the request in flight and sends it only once', async () => {
    let release: (response: Response) => void = () => {};
    fetchStub.callsFake(
      () =>
        new Promise<Response>((resolve) => {
          release = resolve;
        })
    );
    const usernameInput = element.shadowRoot?.querySelector<any>('#username');
    const passwordInput = element.shadowRoot?.querySelector<any>('#password');
    usernameInput.value = 'bob';
    passwordInput.value = 'correct';
    const form = element.shadowRoot?.querySelector('form') as HTMLFormElement;
    form.dispatchEvent(
      new SubmitEvent('submit', { bubbles: true, cancelable: true })
    );
    await element.updateComplete;
    const button = element.shadowRoot?.querySelector(
      'sl-button[type="submit"]'
    );
    expect(button?.hasAttribute('loading')).to.be.true;

    // A second Enter on a slow server must not send a second request.
    form.dispatchEvent(
      new SubmitEvent('submit', { bubbles: true, cancelable: true })
    );
    expect(fetchStub).to.have.been.calledOnce;

    release(
      new Response(JSON.stringify({ detail: 'Invalid credentials' }), {
        status: 401,
      })
    );
    await waitUntil(() => !button?.hasAttribute('loading'));
  });

  it('never promises an email sign-in the backend does not accept', async () => {
    // The divider only renders next to passkey or OAuth buttons.
    (element as any).oauthProviders = ['github'];
    element.requestUpdate();
    await element.updateComplete;
    const divider = element.shadowRoot?.querySelector('.divider');
    expect(divider?.textContent).to.equal('or sign in with your username');
    expect(
      element.shadowRoot?.querySelector('#username')?.getAttribute('label')
    ).to.equal('Username');
  });

  it('should have links for password reset and registration', () => {
    const forgotPasswordLink = element.shadowRoot?.querySelector(
      'a[href="/forgot-password"]'
    );
    expect(forgotPasswordLink).to.exist;
    const signUpLink = element.shadowRoot?.querySelector('a[href="/register"]');
    expect(signUpLink).to.exist;
  });
});
