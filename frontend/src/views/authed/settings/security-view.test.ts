import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import '../../../components/view-header.ts';
import { resetConfirmDialogForTests } from '../../../components/confirm-dialog';
import { invalidateApiCaches } from '../../../api';
import './security-view';
import type { SecurityView } from './security-view';

describe('SecurityView', () => {
  let fetchStub: sinon.SinonStub;

  const cliSessions = [
    {
      id: 'aaaaaaaa-0000-0000-0000-000000000001',
      created_at: '2026-09-01T10:00:00',
      last_seen_at: '2026-09-27T08:00:00+00:00',
      user_agent: 'preloop-cli/0.17.0',
      hostname: 'laptop.example.com',
      current: false,
    },
    {
      id: 'aaaaaaaa-0000-0000-0000-000000000002',
      created_at: '2026-09-02T10:00:00',
      last_seen_at: null,
      user_agent: null,
      hostname: 'build-host',
      current: false,
    },
  ];

  function createFetchStub(
    opts: { changeFails?: boolean; withCliSessions?: boolean } = {}
  ) {
    return sinon
      .stub(window, 'fetch')
      .callsFake(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();
        const method = (init?.method || 'GET').toUpperCase();

        if (
          url.includes('/api/v1/auth/sessions/revoke-all') &&
          method === 'POST'
        ) {
          return new Response(JSON.stringify({ auth_generation: 1 }), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        }

        if (
          opts.withCliSessions &&
          url.endsWith('/api/v1/auth/sessions/cli') &&
          method === 'GET'
        ) {
          return new Response(JSON.stringify(cliSessions), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        }

        if (
          opts.withCliSessions &&
          url.includes('/api/v1/auth/sessions/cli/') &&
          method === 'DELETE'
        ) {
          return new Response(null, { status: 204 });
        }

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
    resetConfirmDialogForTests();
  });

  it('renders the change password form', async () => {
    fetchStub = createFetchStub();
    const element = (await fixture(
      html`<security-view></security-view>`
    )) as SecurityView;
    await element.updateComplete;

    const header = element.shadowRoot?.querySelector('view-header');
    expect(header?.getAttribute('headerText')).to.equal('Security');
    expect(element.shadowRoot?.textContent).to.contain('Change password');
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

  it('signs out everywhere after confirmation', async () => {
    fetchStub = createFetchStub();
    const element = (await fixture(
      html`<security-view></security-view>`
    )) as SecurityView;
    await element.updateComplete;
    const navigate = sinon.stub(element as any, '_navigate');

    (
      element.shadowRoot?.querySelector(
        '[data-testid="sign-out-everywhere"]'
      ) as HTMLElement
    ).click();

    await waitUntil(
      () => !!document.querySelector('confirm-dialog'),
      'confirm dialog'
    );
    const dialog = document.querySelector('confirm-dialog')!;
    const confirmBtn = dialog.shadowRoot?.querySelector(
      '[data-testid="confirm-dialog-confirm"]'
    ) as HTMLElement;
    expect(confirmBtn, 'expected the danger confirm control').to.exist;
    confirmBtn.click();

    await waitUntil(() => navigate.called, 'local sign-out');

    const call = fetchStub
      .getCalls()
      .find((c) => String(c.args[0]).includes('/auth/sessions/revoke-all'));
    expect(call, 'expected revoke-all POST').to.exist;
    expect((call?.args[1]?.method || '').toUpperCase()).to.equal('POST');
    // The revoke already ended the session; no server sign out follows.
    expect(
      fetchStub
        .getCalls()
        .some((c) => String(c.args[0]) === '/api/v1/auth/logout')
    ).to.equal(false);
    expect(localStorage.getItem('accessToken')).to.equal(null);
    expect(localStorage.getItem('refreshToken')).to.equal(null);
    expect(navigate).to.have.been.calledWith('/');
  });

  it('hides the CLI logins card when the list cannot be loaded', async () => {
    fetchStub = createFetchStub();
    const element = (await fixture(
      html`<security-view></security-view>`
    )) as SecurityView;
    await element.updateComplete;

    expect(element.shadowRoot?.querySelector('[data-testid="cli-sessions"]')).to
      .not.exist;
  });

  it('lists CLI logins and revokes one', async () => {
    fetchStub = createFetchStub({ withCliSessions: true });
    const element = (await fixture(
      html`<security-view></security-view>`
    )) as SecurityView;
    await waitUntil(
      () => element.shadowRoot?.querySelector('[data-testid="cli-sessions"]'),
      'CLI logins card'
    );

    const card = element.shadowRoot!.querySelector(
      '[data-testid="cli-sessions"]'
    )!;
    expect(card.textContent).to.contain('laptop.example.com');
    expect(card.textContent).to.contain('build-host');

    const buttons = card.querySelectorAll('[data-testid="revoke-cli-session"]');
    expect(buttons.length).to.equal(2);
    (buttons[1] as HTMLElement).click();

    await waitUntil(
      () => !!document.querySelector('confirm-dialog'),
      'confirm dialog'
    );
    const dialog = document.querySelector('confirm-dialog')!;
    await waitUntil(
      () => dialog.shadowRoot?.textContent?.includes('build-host'),
      'dialog names the host'
    );
    (
      dialog.shadowRoot?.querySelector(
        '[data-testid="confirm-dialog-confirm"]'
      ) as HTMLElement
    ).click();

    await waitUntil(
      () =>
        !element.shadowRoot
          ?.querySelector('[data-testid="cli-sessions"]')
          ?.textContent?.includes('build-host'),
      'revoked row removed'
    );
    const call = fetchStub
      .getCalls()
      .find(
        (c) =>
          String(c.args[0]).includes(
            '/api/v1/auth/sessions/cli/aaaaaaaa-0000-0000-0000-000000000002'
          ) && (c.args[1]?.method || '').toUpperCase() === 'DELETE'
      );
    expect(call, 'expected DELETE for the revoked session').to.exist;
    expect(
      element.shadowRoot?.querySelector('[data-testid="cli-sessions"]')
        ?.textContent
    ).to.contain('laptop.example.com');
  });

  it('keeps the CLI login when the revoke is cancelled', async () => {
    fetchStub = createFetchStub({ withCliSessions: true });
    const element = (await fixture(
      html`<security-view></security-view>`
    )) as SecurityView;
    await waitUntil(
      () => element.shadowRoot?.querySelector('[data-testid="cli-sessions"]'),
      'CLI logins card'
    );

    (
      element.shadowRoot!.querySelectorAll(
        '[data-testid="revoke-cli-session"]'
      )[1] as HTMLElement
    ).click();
    await waitUntil(
      () => !!document.querySelector('confirm-dialog'),
      'confirm dialog'
    );
    const dialog = document.querySelector('confirm-dialog')!;
    await waitUntil(
      () => dialog.shadowRoot?.querySelector('sl-button:not([data-testid])'),
      'cancel control'
    );
    (
      dialog.shadowRoot!.querySelector(
        'sl-button:not([data-testid])'
      ) as HTMLElement
    ).click();
    await new Promise((resolve) => setTimeout(resolve, 50));

    const deletes = fetchStub
      .getCalls()
      .filter((c) => (c.args[1]?.method || '').toUpperCase() === 'DELETE');
    expect(deletes).to.have.length(0);
    expect(
      element.shadowRoot?.querySelector('[data-testid="cli-sessions"]')
        ?.textContent
    ).to.contain('build-host');
  });
});

describe('SecurityView passkey-only switch', () => {
  let fetchStub: sinon.SinonStub;
  let putBodies: unknown[];

  function token(claims: Record<string, unknown>): string {
    const body = btoa(JSON.stringify({ sub: 'u1', ...claims }))
      .replace(/\+/g, '-')
      .replace(/\//g, '_')
      .replace(/=+$/, '');
    return `header.${body}.signature`;
  }

  function stubServer(enabled: boolean) {
    putBodies = [];
    fetchStub = sinon
      .stub(window, 'fetch')
      .callsFake(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();
        const method = (init?.method || 'GET').toUpperCase();
        if (url.endsWith('/features')) {
          return Response.json({ features: { passkeys: true } });
        }
        if (url.endsWith('/auth/webauthn/credentials')) {
          return Response.json([
            { id: 'p1', name: 'Laptop', created_at: '2026-11-01T10:00:00Z' },
          ]);
        }
        if (url.endsWith('/auth/webauthn/passkey-only') && method === 'GET') {
          return Response.json({ enabled, passkey_count: 1 });
        }
        if (url.endsWith('/auth/webauthn/passkey-only') && method === 'PUT') {
          putBodies.push(JSON.parse(String(init?.body)));
          return Response.json({
            enabled: true,
            access_token: 'new-access',
            refresh_token: 'new-refresh',
            token_type: 'bearer',
            expires_in: 1800,
          });
        }
        return Response.json({ detail: `Unhandled: ${url}` }, { status: 500 });
      });
  }

  async function render(): Promise<SecurityView> {
    const element = (await fixture(
      html`<security-view></security-view>`
    )) as SecurityView;
    await waitUntil(
      () => switchOf(element),
      'the passkey-only switch never rendered'
    );
    return element;
  }

  function switchOf(element: SecurityView): any {
    return element.shadowRoot?.querySelector(
      '[data-testid="passkey-only-switch"]'
    );
  }

  afterEach(() => {
    fetchStub?.restore();
    localStorage.clear();
    invalidateApiCaches();
  });

  // protects=the switch shows what the server says; fails_when=it renders
  // unchecked for an enabled account or is disabled in a fresh passkey
  // session; no_claim=the server is a fetch stub.
  it('reflects the server state in a fresh passkey session', async () => {
    localStorage.setItem(
      'accessToken',
      token({ amr: ['passkey'], auth_time: Math.floor(Date.now() / 1000) })
    );
    stubServer(true);
    const element = await render();

    const control = switchOf(element);
    expect(control).to.exist;
    expect(control.checked).to.equal(true);
    expect(control.disabled).to.equal(false);
    expect(
      element.shadowRoot?.querySelector('[data-testid="passkey-only"]')
        ?.textContent
    ).to.contain('If you lose every passkey');
  });

  // protects=a password session cannot flip the setting from the console;
  // fails_when=the switch is enabled or no reason is shown; no_claim=the
  // server enforces the same rule (tested in the backend suite).
  it('is disabled with an explanation in a password session', async () => {
    localStorage.setItem(
      'accessToken',
      token({ amr: ['password'], auth_time: Math.floor(Date.now() / 1000) })
    );
    stubServer(false);
    const element = await render();

    expect(switchOf(element).checked).to.equal(false);
    expect(switchOf(element).disabled).to.equal(true);
    expect(
      element.shadowRoot?.querySelector('[data-testid="passkey-only-reason"]')
        ?.textContent
    ).to.contain('Sign in with a passkey');
  });

  // protects=enabling replaces this session's tokens with the returned pair;
  // fails_when=no PUT is sent, or the old tokens stay in storage (they stop
  // working once the setting is on); no_claim=the server is a fetch stub.
  it('enables and stores the fresh token pair', async () => {
    localStorage.setItem(
      'accessToken',
      token({ amr: ['passkey'], auth_time: Math.floor(Date.now() / 1000) })
    );
    stubServer(false);
    const element = await render();

    const control = switchOf(element);
    control.checked = true;
    control.dispatchEvent(new Event('sl-change'));
    await waitUntil(() => (element as any).passkeyOnly === true);

    expect(putBodies).to.deep.equal([{ enabled: true }]);
    expect(localStorage.getItem('accessToken')).to.equal('new-access');
    expect(localStorage.getItem('refreshToken')).to.equal('new-refresh');
  });
});
