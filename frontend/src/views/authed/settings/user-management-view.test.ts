import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import { invalidateApiCaches } from '../../../api';
import './user-management-view';
import { UserManagementView } from './user-management-view';

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

    // The page is called what the sidebar calls it.
    expect(element.shadowRoot?.querySelector('h1')?.textContent).to.equal(
      'Users'
    );
    expect(element.shadowRoot?.textContent).to.contain('Alice Example');
    expect(element.shadowRoot?.textContent).to.contain('alice@example.com');
  });

  it('labels the sign-in source in words and keeps delete quiet', async () => {
    fetchStub = createFetchStub({
      users: [
        { ...sampleUser, user_source: 'oauth_google', email_verified: false },
      ],
    });
    const element = (await fixture(
      html`<user-management-view></user-management-view>`
    )) as UserManagementView;

    await waitUntil(
      () => (element as any).users?.length === 1,
      'users did not load'
    );
    await element.updateComplete;

    const chips = Array.from(
      element.shadowRoot?.querySelectorAll('.user-meta sl-badge') || []
    );
    const labels = chips.map((chip) => (chip.textContent || '').trim());
    // A raw enum value is not a label a reader can act on.
    expect(labels).to.include('Google');
    expect(labels).to.not.include('oauth_google');
    // Chips are tints, and no chip is a solid paint.
    chips.forEach((chip) => {
      expect(chip.classList.contains('chip')).to.equal(true);
      expect(chip.classList.contains('solid')).to.equal(false);
    });

    const del = element.shadowRoot?.querySelector(
      '.user-actions sl-button[variant="danger"]'
    );
    expect(del?.hasAttribute('outline')).to.equal(true);
    expect(del?.classList.contains('danger-action')).to.equal(true);
    // Last in the row, after the gap.
    const actions = Array.from(
      element.shadowRoot?.querySelectorAll('.user-actions sl-button') || []
    );
    expect(actions[actions.length - 1]).to.equal(del);
  });

  it('names sign-in sources and roles without exposing the schema', () => {
    expect(UserManagementView.userSourceLabel('local')).to.equal('Password');
    expect(UserManagementView.userSourceLabel('oauth_google')).to.equal(
      'Google'
    );
    expect(UserManagementView.userSourceLabel('oauth_github')).to.equal(
      'GitHub'
    );
    expect(UserManagementView.userSourceLabel('')).to.equal('Unknown');
    expect(UserManagementView.roleLabel('owner')).to.equal('Owner');
    expect(UserManagementView.roleLabel('team_admin')).to.equal('Team admin');
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
