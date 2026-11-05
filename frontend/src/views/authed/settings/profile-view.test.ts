import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import '../../../components/view-header.ts';
import { invalidateApiCaches } from '../../../api';
import './profile-view';
import type { ProfileView } from './profile-view';

describe('ProfileView', () => {
  let fetchStub: sinon.SinonStub;

  function jsonResponse(data: unknown, status = 200) {
    return new Response(JSON.stringify(data), {
      status,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  function createFetchStub(
    opts: {
      profileFails?: boolean;
      verified?: boolean;
      resendStatus?: number;
    } = {}
  ) {
    return sinon
      .stub(window, 'fetch')
      .callsFake(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();
        const method = (init?.method || 'GET').toUpperCase();

        if (
          url.includes('/api/v1/auth/resend-verification') &&
          method === 'POST'
        ) {
          return jsonResponse(
            opts.resendStatus === 429
              ? {
                  detail:
                    'Too many verification emails requested. Wait a few minutes and try again.',
                }
              : {
                  message:
                    'If that address needs verifying, a new verification email is on its way.',
                },
            opts.resendStatus || 200
          );
        }

        if (url.includes('/api/v1/auth/users/me') && method === 'GET') {
          if (opts.profileFails) {
            return jsonResponse({ detail: 'boom' }, 500);
          }
          return jsonResponse({
            email_verified: opts.verified ?? false,
            username: 'alice',
            email: 'alice@example.com',
            full_name: 'Alice Example',
          });
        }

        if (url.includes('/api/v1/auth/users/me') && method === 'PUT') {
          return jsonResponse({
            email_verified: opts.verified ?? false,
            username: 'alice',
            email: 'alice@example.com',
            full_name: 'Updated Name',
          });
        }

        return jsonResponse({ detail: `Unhandled: ${method} ${url}` }, 500);
      });
  }

  beforeEach(() => {
    invalidateApiCaches();
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
  });

  afterEach(() => {
    fetchStub?.restore();
    localStorage.clear();
    invalidateApiCaches();
  });

  it('renders the profile header', async () => {
    fetchStub = createFetchStub();
    const element = (await fixture(
      html`<profile-view></profile-view>`
    )) as ProfileView;
    await element.updateComplete;

    const header = element.shadowRoot?.querySelector('view-header');
    expect(header).to.exist;
    expect(header?.getAttribute('headerText')).to.equal('Profile');
  });

  it('loads and displays user details', async () => {
    fetchStub = createFetchStub();
    const element = (await fixture(
      html`<profile-view></profile-view>`
    )) as ProfileView;

    await waitUntil(
      () => (element as any).user !== null,
      'Profile did not load'
    );
    await element.updateComplete;

    expect((element as any).user.username).to.equal('alice');
    expect((element as any).fullName).to.equal('Alice Example');
    const inputs = element.shadowRoot?.querySelectorAll('sl-input');
    expect(inputs && inputs.length).to.be.greaterThan(0);
  });

  it('shows an error message when loading fails', async () => {
    fetchStub = createFetchStub({ profileFails: true });
    const element = (await fixture(
      html`<profile-view></profile-view>`
    )) as ProfileView;

    await waitUntil(
      () => (element as any).updateProfileMessage !== '',
      'Error message did not appear'
    );
    await element.updateComplete;

    expect((element as any).updateProfileMessage).to.contain(
      'Failed to load account details'
    );
  });

  it('updates the profile on submit', async () => {
    fetchStub = createFetchStub();
    const element = (await fixture(
      html`<profile-view></profile-view>`
    )) as ProfileView;

    await waitUntil(() => (element as any).user !== null, 'Profile not loaded');

    (element as any).fullName = 'Updated Name';
    await (element as any).handleUpdateProfile(new Event('submit'));
    await element.updateComplete;

    expect(
      element.shadowRoot!.querySelector('#profile-update-status')!.textContent
    ).to.contain('Profile updated successfully');
    const putCall = fetchStub
      .getCalls()
      .find((c) => (c.args[1]?.method || 'GET').toUpperCase() === 'PUT');
    expect(putCall, 'expected a PUT request').to.exist;
  });

  it('reports a failed update', async () => {
    fetchStub = createFetchStub();
    const element = (await fixture(
      html`<profile-view></profile-view>`
    )) as ProfileView;
    await waitUntil(() => (element as any).user !== null, 'Profile not loaded');

    // Make the next PUT fail.
    fetchStub.restore();
    fetchStub = sinon
      .stub(window, 'fetch')
      .callsFake(
        async () =>
          new Response(JSON.stringify({ detail: 'nope' }), { status: 500 })
      );

    await (element as any).handleUpdateProfile(new Event('submit'));
    await element.updateComplete;

    expect((element as any).updateProfileMessage).to.contain('nope');
  });
  it('announces pending and repeated save failures without losing input or focus', async () => {
    fetchStub = createFetchStub();
    const element = await fixture<ProfileView>(
      html`<profile-view></profile-view>`
    );
    await waitUntil(() => !!(element as any).user);
    await element.updateComplete;
    const status = element.shadowRoot!.querySelector('#profile-update-status')!;
    expect(status.getAttribute('role')).to.equal('status');
    expect(status.getAttribute('aria-live')).to.equal('polite');
    expect(status.getAttribute('aria-atomic')).to.equal('true');
    const input = element.shadowRoot!.querySelector('sl-input') as any;
    input.value = 'already.used';
    input.dispatchEvent(new CustomEvent('sl-input', { bubbles: true }));
    input.focus();
    for (let attempt = 0; attempt < 2; attempt++) {
      let resolve!: (response: Response) => void;
      fetchStub
        .withArgs('/api/v1/auth/users/me', sinon.match({ method: 'PUT' }))
        .returns(
          new Promise<Response>((done) => {
            resolve = done;
          })
        );
      const submit = element.handleUpdateProfile(new Event('submit'));
      await waitUntil(() => status.textContent!.includes('Updating profile'));
      expect(
        element
          .shadowRoot!.querySelector('sl-button[type="submit"]')!
          .hasAttribute('disabled')
      ).to.equal(true);
      resolve(
        jsonResponse({ detail: 'That username is already in use.' }, 409)
      );
      await submit;
      await element.updateComplete;
      expect(
        element.shadowRoot!.querySelector('#profile-update-status')
      ).to.equal(status);
      expect(status.textContent).to.contain('That username is already in use.');
      expect(input.value).to.equal('already.used');
      expect(element.shadowRoot!.activeElement).to.equal(input);
    }
  });

  it('shows verified status without resend controls', async () => {
    fetchStub = createFetchStub({ verified: true });
    const element = await fixture<ProfileView>(
      html`<profile-view></profile-view>`
    );
    await waitUntil(
      () => !!element.shadowRoot?.querySelector('.email-verification')
    );
    expect(
      element.shadowRoot?.querySelector('.email-verification')?.textContent
    ).to.contain('Verified');
    expect(
      element.shadowRoot?.querySelector('[data-testid="resend-verification"]')
    ).not.to.exist;
  });

  it('resends verification to the profile email and shows confirmation', async () => {
    fetchStub = createFetchStub();
    const element = await fixture<ProfileView>(
      html`<profile-view></profile-view>`
    );
    await waitUntil(
      () =>
        !!element.shadowRoot?.querySelector(
          '[data-testid="resend-verification"]'
        )
    );
    expect(
      element.shadowRoot?.querySelector('.email-verification')?.textContent
    ).to.contain('Not verified');
    (
      element.shadowRoot?.querySelector(
        '[data-testid="resend-verification"]'
      ) as HTMLElement
    ).click();
    await waitUntil(
      () =>
        !!element.shadowRoot?.textContent?.includes(
          'a new verification email is on its way'
        )
    );
    const call = fetchStub
      .getCalls()
      .find((c) => String(c.args[0]).includes('/resend-verification'));
    expect(JSON.parse(call?.args[1]?.body as string)).to.deep.equal({
      email: 'alice@example.com',
    });
  });

  it('shows rate limit errors and allows retrying', async () => {
    fetchStub = createFetchStub({ resendStatus: 429 });
    const element = await fixture<ProfileView>(
      html`<profile-view></profile-view>`
    );
    await waitUntil(
      () =>
        !!element.shadowRoot?.querySelector(
          '[data-testid="resend-verification"]'
        )
    );
    (
      element.shadowRoot?.querySelector(
        '[data-testid="resend-verification"]'
      ) as HTMLElement
    ).click();
    await waitUntil(
      () =>
        !!element.shadowRoot?.textContent?.includes(
          'Too many verification emails'
        )
    );
    expect(
      element.shadowRoot
        ?.querySelector('[data-testid="resend-verification"]')
        ?.hasAttribute('disabled')
    ).to.be.false;
  });

  it('refreshes verification status after following the email link', async () => {
    fetchStub = createFetchStub();
    const element = await fixture<ProfileView>(
      html`<profile-view></profile-view>`
    );
    await waitUntil(
      () =>
        !!element.shadowRoot?.querySelector(
          '[data-testid="refresh-verification"]'
        )
    );
    fetchStub.restore();
    fetchStub = createFetchStub({ verified: true });
    (
      element.shadowRoot?.querySelector(
        '[data-testid="refresh-verification"]'
      ) as HTMLElement
    ).click();
    await waitUntil(
      () =>
        !!element.shadowRoot?.textContent?.includes(
          'Your email address is verified.'
        )
    );
    expect(
      element.shadowRoot?.querySelector('[data-testid="resend-verification"]')
    ).not.to.exist;
    expect(fetchStub.called).to.be.true;
  });
  it('reports when the email still awaits verification after refreshing', async () => {
    fetchStub = createFetchStub();
    const element = await fixture<ProfileView>(
      html`<profile-view></profile-view>`
    );
    await waitUntil(
      () =>
        !!element.shadowRoot?.querySelector(
          '[data-testid="refresh-verification"]'
        )
    );
    (
      element.shadowRoot?.querySelector(
        '[data-testid="refresh-verification"]'
      ) as HTMLElement
    ).click();
    await waitUntil(
      () =>
        !!element.shadowRoot?.textContent?.includes(
          'still awaiting verification'
        )
    );
    expect(
      element.shadowRoot?.querySelector('[data-testid="resend-verification"]')
    ).to.exist;
  });
});

describe('shared identity profile edits', () => {
  let fetchStub: sinon.SinonStub;
  beforeEach(() => {
    invalidateApiCaches();
    localStorage.setItem('accessToken', 'test-token');
    localStorage.setItem('refreshToken', 'test-refresh');
    fetchStub = sinon.stub(window, 'fetch').callsFake(async (_input, init) => {
      const changes =
        init?.method === 'PUT' ? JSON.parse(String(init.body)) : {};
      return new Response(
        JSON.stringify({
          id: 'child-membership',
          person_id: 'person-example',
          account_id: 'child-account',
          username: 'chosen-person',
          email: 'jane@example.com',
          full_name: 'Jane Example',
          email_verified: !changes.email,
          ...changes,
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } }
      );
    });
  });
  afterEach(() => {
    fetchStub.restore();
    localStorage.clear();
    invalidateApiCaches();
  });

  it('edits the canonical username and notifies existing header listeners', async () => {
    const element = await fixture<ProfileView>(
      html`<profile-view></profile-view>`
    );
    await waitUntil(() => !!(element as any).user);
    const listener = sinon.spy();
    window.addEventListener('auth-change', listener);
    try {
      (element as any).username = 'chosen-updated';
      await element.handleUpdateProfile(new Event('submit'));
      const call = fetchStub
        .getCalls()
        .find((call) => call.args[1]?.method === 'PUT')!;
      expect(JSON.parse(String(call.args[1].body))).to.deep.equal({
        full_name: 'Jane Example',
        username: 'chosen-updated',
      });
      expect((element as any).user.person_id).to.equal('person-example');
      expect(listener.calledOnce).to.equal(true);
    } finally {
      window.removeEventListener('auth-change', listener);
    }
  });

  it('clears credentials and auth state while showing email verification guidance', async () => {
    const element = await fixture<ProfileView>(
      html`<profile-view></profile-view>`
    );
    await waitUntil(() => !!(element as any).user);
    const listener = sinon.spy();
    window.addEventListener('auth-change', listener);
    try {
      (element as any).email = 'updated@example.com';
      await element.handleUpdateProfile(new Event('submit'));
      await element.updateComplete;
      expect(localStorage.getItem('accessToken')).to.equal(null);
      expect(localStorage.getItem('refreshToken')).to.equal(null);
      expect(listener.calledOnce).to.equal(true);
      expect(element.shadowRoot!.textContent).to.contain(
        'verify your new address'
      );
      expect(element.shadowRoot!.querySelector('a[href="/login"]')).to.exist;
      expect(
        element
          .shadowRoot!.querySelector('sl-button[type="submit"]')!
          .hasAttribute('disabled')
      ).to.equal(true);
    } finally {
      window.removeEventListener('auth-change', listener);
    }
  });
});
