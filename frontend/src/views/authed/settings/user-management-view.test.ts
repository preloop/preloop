import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import { invalidateApiCaches } from '../../../api';
import './user-management-view';
import type { UserManagementView } from './user-management-view';

describe('UserManagementView', () => {
  let fetchStub: sinon.SinonStub;

  function json(data: unknown, status = 200) {
    return new Response(JSON.stringify(data), {
      status,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  function createFetchStub(
    opts: {
      featureEnabled?: boolean;
      users?: unknown[];
      usersFail?: boolean;
    } = {}
  ) {
    const featureEnabled = opts.featureEnabled !== false;
    return sinon
      .stub(window, 'fetch')
      .callsFake(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();
        const method = (init?.method || 'GET').toUpperCase();

        if (url.includes('/api/v1/features')) {
          return json({
            plugins: [],
            features: { user_management: featureEnabled },
          });
        }

        if (url.includes('/api/v1/users') && method === 'GET') {
          if (opts.usersFail) {
            return json({ detail: 'boom' }, 500);
          }
          return json({
            users: opts.users ?? [],
            total: (opts.users ?? []).length,
          });
        }

        if (url.includes('/api/v1/users') && method === 'POST') {
          return json({ id: 'user-new', username: 'newuser' });
        }

        if (url.includes('/api/v1/roles')) {
          return json({
            roles: [{ id: 'role-1', name: 'admin', description: 'Admin role' }],
          });
        }

        return json({ detail: `Unhandled: ${method} ${url}` }, 500);
      });
  }

  const sampleUser = {
    id: 'user-1',
    username: 'alice',
    email: 'alice@example.com',
    full_name: 'Alice Example',
    is_active: true,
    user_source: 'local',
    email_verified: true,
  };

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

  it('shows the not-available message when feature is disabled', async () => {
    fetchStub = createFetchStub({ featureEnabled: false });
    const element = (await fixture(
      html`<user-management-view></user-management-view>`
    )) as UserManagementView;

    await waitUntil(() => !(element as any).isLoading, 'still loading');
    await element.updateComplete;

    expect(element.shadowRoot?.textContent).to.contain(
      'not available in this edition'
    );
  });

  it('renders the user list when users exist', async () => {
    fetchStub = createFetchStub({ users: [sampleUser] });
    const element = (await fixture(
      html`<user-management-view></user-management-view>`
    )) as UserManagementView;

    await waitUntil(
      () => (element as any).users?.length === 1,
      'users did not load'
    );
    await element.updateComplete;

    expect(element.shadowRoot?.textContent).to.contain('User Management');
    expect(element.shadowRoot?.textContent).to.contain('Alice Example');
    expect(element.shadowRoot?.textContent).to.contain('alice@example.com');
  });

  it('renders an empty grid when there are no users', async () => {
    fetchStub = createFetchStub({ users: [] });
    const element = (await fixture(
      html`<user-management-view></user-management-view>`
    )) as UserManagementView;

    await waitUntil(() => !(element as any).isLoading, 'still loading');
    await element.updateComplete;

    const cards = element.shadowRoot?.querySelectorAll('.users-grid sl-card');
    expect(cards?.length).to.equal(0);
  });

  it('shows an error when user loading fails', async () => {
    fetchStub = createFetchStub({ usersFail: true });
    const element = (await fixture(
      html`<user-management-view></user-management-view>`
    )) as UserManagementView;

    await waitUntil(
      () => (element as any).error !== null,
      'error did not appear'
    );
    await element.updateComplete;

    const errorEl = element.shadowRoot?.querySelector('.error');
    expect(errorEl).to.exist;
  });

  it('creates a new user', async () => {
    fetchStub = createFetchStub({ users: [] });
    const element = (await fixture(
      html`<user-management-view></user-management-view>`
    )) as UserManagementView;

    await waitUntil(() => !(element as any).isLoading, 'still loading');

    (element as any).newUser = {
      username: 'bob',
      email: 'bob@example.com',
      password: 'password123',
    };
    await (element as any).handleCreateUser();
    await element.updateComplete;

    const postCall = fetchStub
      .getCalls()
      .find(
        (c) =>
          String(c.args[0]).includes('/api/v1/users') &&
          (c.args[1]?.method || 'GET').toUpperCase() === 'POST'
      );
    expect(postCall, 'expected a POST to /api/v1/users').to.exist;
    expect((element as any).isCreateModalOpen).to.be.false;
  });
});
