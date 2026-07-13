import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';
import './register-view';
import { RegisterView } from './register-view';
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

    // Stub fetch before each test
    fetchStub = sinon.stub(window, 'fetch');
    element = await fixture(html`<register-view></register-view>`);
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
});
