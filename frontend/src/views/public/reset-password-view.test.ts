import { html, fixture, expect } from '@open-wc/testing';
import sinon from 'sinon';
import './reset-password-view';
import { ResetPasswordView } from './reset-password-view';

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

// Build a synthetic submit event backed by a real native <form> so the
// component's `new FormData(event.target)` reads the values reliably (shoelace
// inputs do not always surface their value through FormData in the test DOM).
function submitEvent(password: string, confirmPassword: string) {
  const form = document.createElement('form');
  const p = document.createElement('input');
  p.name = 'password';
  p.value = password;
  const c = document.createElement('input');
  c.name = 'confirmPassword';
  c.value = confirmPassword;
  form.append(p, c);
  return { preventDefault() {}, target: form } as unknown as SubmitEvent;
}

describe('ResetPasswordView', () => {
  let el: ResetPasswordView;
  let fetchStub: sinon.SinonStub;

  beforeEach(async () => {
    (window as any).BRAND_CONFIG = BRAND_CONFIG;
    fetchStub = sinon.stub(window, 'fetch');
    el = (await fixture(
      html`<reset-password-view></reset-password-view>`
    )) as ResetPasswordView;
  });

  afterEach(() => {
    fetchStub.restore();
    delete (window as any).BRAND_CONFIG;
  });

  it('renders the new-password and confirm-password fields', () => {
    expect(el.shadowRoot?.querySelector('form')).to.exist;
    expect(el.shadowRoot?.querySelectorAll('sl-input').length).to.equal(2);
    expect(el.shadowRoot?.querySelector('a[href="/login"]')).to.exist;
  });

  const calledReset = () =>
    fetchStub
      .getCalls()
      .some((c) => String(c.args[0]).includes('/api/v1/auth/reset-password'));

  it('validates that passwords match before calling the API', async () => {
    await (el as any).handleResetPassword(
      submitEvent('password1', 'password2')
    );
    await el.updateComplete;
    expect((el as any).error).to.contain('Passwords do not match');
    expect(calledReset()).to.be.false;
  });

  it('shows a success message after resetting the password', async () => {
    fetchStub.callsFake(
      async () => new Response(JSON.stringify({}), { status: 200 })
    );
    await (el as any).handleResetPassword(
      submitEvent('password1', 'password1')
    );
    await el.updateComplete;
    expect((el as any).message).to.contain('reset successfully');
    expect(calledReset()).to.be.true;
  });

  it('shows an error when the token is invalid', async () => {
    fetchStub.callsFake(
      async () => new Response(JSON.stringify({}), { status: 400 })
    );
    await (el as any).handleResetPassword(
      submitEvent('password1', 'password1')
    );
    await el.updateComplete;
    expect((el as any).error).to.contain('Invalid or expired');
  });
});
