import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import '../../../components/view-header.ts';
import './security-view';
import type { SecurityView } from './security-view';

describe('SecurityView', () => {
  let fetchStub: sinon.SinonStub;

  function createFetchStub(opts: { changeFails?: boolean } = {}) {
    return sinon
      .stub(window, 'fetch')
      .callsFake(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();
        const method = (init?.method || 'GET').toUpperCase();

        if (
          url.includes('/api/v1/auth/users/me/password') &&
          method === 'PUT'
        ) {
          if (opts.changeFails) {
            return new Response(JSON.stringify({ detail: 'Wrong password' }), {
              status: 400,
              headers: { 'Content-Type': 'application/json' },
            });
          }
          return new Response(null, { status: 204 });
        }

        return new Response(JSON.stringify({ detail: `Unhandled: ${url}` }), {
          status: 500,
          headers: { 'Content-Type': 'application/json' },
        });
      });
  }

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
  });

  afterEach(() => {
    fetchStub?.restore();
    localStorage.clear();
  });

  it('renders the change password form', async () => {
    fetchStub = createFetchStub();
    const element = (await fixture(
      html`<security-view></security-view>`
    )) as SecurityView;
    await element.updateComplete;

    const header = element.shadowRoot?.querySelector('view-header');
    expect(header?.getAttribute('headerText')).to.equal('Security');
    expect(element.shadowRoot?.textContent).to.contain('Change Password');
    const inputs = element.shadowRoot?.querySelectorAll('sl-input');
    expect(inputs?.length).to.equal(3);
  });

  it('validates minimum new password length', async () => {
    fetchStub = createFetchStub();
    const element = (await fixture(
      html`<security-view></security-view>`
    )) as SecurityView;

    (element as any).currentPassword = 'oldpassword';
    (element as any).newPassword = 'short';
    (element as any).confirmNewPassword = 'short';
    await (element as any).handleChangePassword(new Event('submit'));
    await element.updateComplete;

    expect((element as any).changePasswordMessage).to.contain(
      'at least 8 characters'
    );
    // Validation should short-circuit before any password change request.
    const pwCall = fetchStub
      .getCalls()
      .find((c) => String(c.args[0]).includes('/users/me/password'));
    expect(pwCall, 'no password request should be made').to.not.exist;
  });

  it('validates matching new passwords', async () => {
    fetchStub = createFetchStub();
    const element = (await fixture(
      html`<security-view></security-view>`
    )) as SecurityView;

    (element as any).currentPassword = 'oldpassword';
    (element as any).newPassword = 'newpassword1';
    (element as any).confirmNewPassword = 'newpassword2';
    await (element as any).handleChangePassword(new Event('submit'));
    await element.updateComplete;

    expect((element as any).changePasswordMessage).to.contain('do not match');
  });

  it('changes the password successfully', async () => {
    fetchStub = createFetchStub();
    const element = (await fixture(
      html`<security-view></security-view>`
    )) as SecurityView;

    (element as any).currentPassword = 'oldpassword';
    (element as any).newPassword = 'newpassword1';
    (element as any).confirmNewPassword = 'newpassword1';
    await (element as any).handleChangePassword(new Event('submit'));
    await element.updateComplete;

    expect((element as any).changePasswordMessage).to.contain(
      'Password changed successfully'
    );
    expect((element as any).newPassword).to.equal('');
  });

  it('reports a failed password change', async () => {
    fetchStub = createFetchStub({ changeFails: true });
    const element = (await fixture(
      html`<security-view></security-view>`
    )) as SecurityView;

    (element as any).currentPassword = 'oldpassword';
    (element as any).newPassword = 'newpassword1';
    (element as any).confirmNewPassword = 'newpassword1';
    await (element as any).handleChangePassword(new Event('submit'));
    await element.updateComplete;

    expect((element as any).changePasswordMessage).to.contain(
      'Failed to change password'
    );
  });
});
