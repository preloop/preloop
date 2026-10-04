import { html, fixture, expect } from '@open-wc/testing';
import sinon from 'sinon';
import './reset-password-view';
import { RESET_TOKEN_ERROR, ResetPasswordView } from './reset-password-view';

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

  it('says what the server refused instead of blaming the link', async () => {
    // A too-short password is a 422, not a bad token: telling the reader the
    // link is dead sends them to request a new one for nothing.
    fetchStub.callsFake(
      async () =>
        new Response(
          JSON.stringify({
            detail: [
              {
                loc: ['body', 'new_password'],
                msg: 'String should have at least 8 characters',
              },
            ],
          }),
          { status: 422 }
        )
    );
    await (el as any).handleResetPassword(submitEvent('short', 'short'));
    await el.updateComplete;
    expect((el as any).error).to.equal(
      'String should have at least 8 characters'
    );
  });

  it('keeps the token message for a refused or unknown link', async () => {
    fetchStub.callsFake(
      async () =>
        new Response(JSON.stringify({ detail: 'User not found' }), {
          status: 404,
        })
    );
    await (el as any).handleResetPassword(
      submitEvent('password1', 'password1')
    );
    expect((el as any).error).to.equal(RESET_TOKEN_ERROR);
  });

  it('falls back to a plain sentence for a server fault without detail', async () => {
    fetchStub.callsFake(async () => new Response('oops', { status: 500 }));
    await (el as any).handleResetPassword(
      submitEvent('password1', 'password1')
    );
    expect((el as any).error).to.equal(
      'Could not reset your password. Try again in a moment.'
    );
  });

  it('states the password rule and helps password managers', () => {
    const [password, confirm] = Array.from(
      el.shadowRoot!.querySelectorAll('sl-input')
    );
    expect(password.getAttribute('minlength')).to.equal('8');
    expect(password.getAttribute('help-text')).to.equal(
      'At least 8 characters.'
    );
    expect(password.getAttribute('autocomplete')).to.equal('new-password');
    expect(confirm.getAttribute('autocomplete')).to.equal('new-password');
  });
});
