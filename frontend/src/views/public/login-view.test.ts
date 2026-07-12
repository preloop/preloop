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

  it('should have links for password reset and registration', () => {
    const forgotPasswordLink = element.shadowRoot?.querySelector(
      'a[href="/forgot-password"]'
    );
    expect(forgotPasswordLink).to.exist;
    const signUpLink = element.shadowRoot?.querySelector('a[href="/register"]');
    expect(signUpLink).to.exist;
  });
});
