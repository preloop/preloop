import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import { invalidateApiCaches } from '../../../api';
import { resetConfirmDialogForTests } from '../../../components/confirm-dialog';
import { answerConfirmDialog } from '../../../utils/test-confirm-dialog';
import './user-management-view';
import { UserManagementView } from './user-management-view';
import {
  mockApi,
  signInForTest,
  type MockApi,
} from '../../../test-helpers/capability-api';
import type { Capability } from '../../../capabilities';
import type { SubaccountAccessDialog } from '../hierarchy/subaccount-access-dialog';

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
      createFails?: boolean;
    } = {}
  ) {
    const featureEnabled = opts.featureEnabled !== false;
    return sinon
      .stub(window, 'fetch')
      .callsFake(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();
        if (url.includes('/auth/users/me')) {
          return new Response(JSON.stringify({ permissions: null }), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        }
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

        if (url.includes('/deactivate') && method === 'POST') {
          return json({ ...opts.users?.[0], is_active: false });
        }

        if (url.includes('/api/v1/users') && method === 'POST') {
          if (opts.createFails) {
            return json({ detail: 'Username already taken' }, 400);
          }
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
    resetConfirmDialogForTests();
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

  it('marks the person a device page linked to, from the route', async () => {
    fetchStub = createFetchStub({ users: [sampleUser] });
    // Built before the router writes the destination URL.
    const element = document.createElement(
      'user-management-view'
    ) as UserManagementView;
    element.onBeforeEnter({ search: `?user=${sampleUser.id}` });
    document.body.append(element);
    try {
      await waitUntil(() => (element as any).users?.length === 1);
      await element.updateComplete;
      const row = element.shadowRoot!.querySelector(
        `tr[data-user-id="${sampleUser.id}"]`
      )!;
      expect(row.getAttribute('aria-current')).to.equal('true');
    } finally {
      element.remove();
    }
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

    // The page is called what the sidebar calls it, in the shared header.
    const header = element.shadowRoot?.querySelector('view-header') as any;
    expect(header?.headerText).to.equal('Users');
    expect(header?.querySelector('[slot="main-column"] sl-button')).to.exist;
    expect(element.shadowRoot?.textContent).to.contain('Alice Example');
    expect(element.shadowRoot?.textContent).to.contain('alice@example.com');
  });

  it('links each name to the person page and keeps the row actions', async () => {
    fetchStub = createFetchStub({ users: [sampleUser] });
    const element = (await fixture(
      html`<user-management-view></user-management-view>`
    )) as UserManagementView;
    await waitUntil(
      () => (element as any).users?.length === 1,
      'users did not load'
    );
    await element.updateComplete;

    const link = element.shadowRoot!.querySelector(
      '.user-name a'
    ) as HTMLAnchorElement;
    expect(link.getAttribute('href')).to.equal(
      '/console/settings/users/user-1'
    );
    expect(link.textContent!.trim()).to.equal('Alice Example');
    expect(
      element.shadowRoot!.querySelectorAll('.row-menu sl-menu-item').length
    ).to.be.greaterThan(1);
  });

  it('lays the people out as a console table, one row per person', async () => {
    fetchStub = createFetchStub({
      users: [
        {
          ...sampleUser,
          last_login: new Date(Date.now() - 3 * 86400000).toISOString(),
          roles: [{ id: 'r-1', name: 'owner' }],
          inherited_roles: [
            { id: 'r-2', name: 'team_admin', team_name: 'Founders' },
            // Held directly as well: one chip, not two.
            { id: 'r-1', name: 'owner', team_name: 'Founders' },
          ],
        },
        { ...sampleUser, id: 'user-2', email: 'bob@example.com' },
      ],
    });
    const element = (await fixture(
      html`<user-management-view></user-management-view>`
    )) as UserManagementView;
    await waitUntil(() => (element as any).users?.length === 2);
    await element.updateComplete;

    const root = element.shadowRoot!;
    expect(root.querySelector('sl-card[data-user-id]')).to.equal(null);
    const headers = [
      ...root.querySelectorAll('table.styled-table thead th'),
    ].map((th) => (th.textContent || '').trim());
    expect(headers).to.deep.equal([
      'Name',
      'Email',
      'Status',
      'Sign-in',
      'Roles',
      'Teams',
      'Last login',
      'Actions',
    ]);
    const rows = root.querySelectorAll('table.styled-table tbody tr');
    expect(rows.length).to.equal(2);
    const first = rows[0] as HTMLTableRowElement;
    // The team is a column of its own, not micro-text glued to the role.
    expect(first.querySelector('.teams-cell')!.textContent!.trim()).to.equal(
      'Founders'
    );
    expect(first.querySelector('.roles-cell')!.textContent).to.not.contain('(');
    const roleChips = [...first.querySelectorAll('.roles-cell sl-badge')];
    expect(roleChips.map((chip) => chip.textContent!.trim())).to.deep.equal([
      'Owner',
      'Team admin',
    ]);
    expect(roleChips[1].getAttribute('title')).to.equal('From team: Founders');
    expect(first.querySelector('.last-login')!.textContent!.trim()).to.equal(
      '3d ago'
    );
    expect(
      (rows[1] as HTMLElement).querySelector('.last-login')!.textContent!.trim()
    ).to.equal('Never');
    // Every chip is the shared tinted pill.
    root.querySelectorAll('tbody sl-badge').forEach((chip) => {
      expect(chip.hasAttribute('pill'), chip.textContent || '').to.equal(true);
      expect(chip.classList.contains('solid')).to.equal(false);
      expect(chip.getAttribute('variant')).to.not.equal('primary');
    });
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
      element.shadowRoot?.querySelectorAll('tbody tr sl-badge') || []
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

    // No red button on the row: the destructive action is the last item
    // of the row's kebab menu, after a divider.
    expect(
      element.shadowRoot?.querySelector('tbody sl-button[variant="danger"]')
    ).to.equal(null);
    const items = Array.from(
      element.shadowRoot?.querySelectorAll('.row-menu sl-menu-item') || []
    );
    const del = items[items.length - 1];
    expect(del.getAttribute('data-action')).to.equal('deactivate');
    expect(del.classList.contains('danger-item')).to.equal(true);
    expect(del.previousElementSibling?.tagName).to.equal('SL-DIVIDER');
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

  it('renders no rows when there are no users', async () => {
    fetchStub = createFetchStub({ users: [] });
    const element = (await fixture(
      html`<user-management-view></user-management-view>`
    )) as UserManagementView;

    await waitUntil(() => !(element as any).isLoading, 'still loading');
    await element.updateComplete;

    const rows = element.shadowRoot?.querySelectorAll('tbody tr[data-user-id]');
    expect(rows?.length).to.equal(0);
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

  async function mountWithUser() {
    const element = (await fixture(
      html`<user-management-view></user-management-view>`
    )) as UserManagementView;
    await waitUntil(
      () => (element as any).users?.length === 1,
      'users did not load'
    );
    await element.updateComplete;
    return element;
  }

  const deactivateCalls = () =>
    fetchStub
      .getCalls()
      .filter((call) => String(call.args[0]).includes('/deactivate'));

  it('asks in the console dialog before deactivating, and explains why', async () => {
    fetchStub = createFetchStub({ users: [sampleUser] });
    const nativeConfirm = sinon.stub(window, 'confirm');
    try {
      const element = await mountWithUser();
      const deactivate = element.shadowRoot!.querySelector(
        '.row-menu sl-menu-item[data-action="deactivate"]'
      ) as HTMLElement;

      deactivate.click();
      const prompt = await answerConfirmDialog(false);
      expect(prompt).to.contain('alice@example.com');
      expect(prompt).to.contain('access to this account');
      expect(prompt).to.contain('Other account memberships');
      await element.updateComplete;
      expect(deactivateCalls()).to.have.length(0);

      deactivate.click();
      await answerConfirmDialog(true);
      await waitUntil(() => deactivateCalls().length === 1);
      expect(nativeConfirm.called).to.equal(false);
    } finally {
      nativeConfirm.restore();
    }
  });

  it('names the row menu and every action in it', async () => {
    fetchStub = createFetchStub({ users: [sampleUser] });
    const element = await mountWithUser();
    const trigger = element.shadowRoot!.querySelector(
      '.row-menu sl-icon-button[slot="trigger"]'
    )!;
    expect(trigger.getAttribute('label')).to.equal('Actions for Alice Example');
    const labels = [
      ...element.shadowRoot!.querySelectorAll('.row-menu sl-menu-item'),
    ].map((item) => (item.textContent || '').trim());
    expect(labels).to.deep.equal([
      'Manage roles',
      'Edit account membership',
      'Deactivate account membership',
    ]);
  });

  it('says which fields are missing inside the create dialog', async () => {
    fetchStub = createFetchStub({ users: [] });
    const element = (await fixture(
      html`<user-management-view></user-management-view>`
    )) as UserManagementView;
    await waitUntil(() => !(element as any).isLoading, 'still loading');
    (element as any).openCreateModal();
    (element as any).newUser = { username: 'bob' };
    await (element as any).handleCreateUser();
    await element.updateComplete;

    const dialog = element.shadowRoot!.querySelector(
      'sl-dialog[label="Create user"]'
    )!;
    const alert = dialog.querySelector('sl-alert[role="alert"]');
    expect(alert?.textContent).to.contain('Enter an email and a password.');
    expect(element.shadowRoot!.querySelector('div.error')).to.not.exist;
  });

  it('shows a failed create inside the open dialog, not behind it', async () => {
    fetchStub = createFetchStub({ users: [], createFails: true });
    const element = (await fixture(
      html`<user-management-view></user-management-view>`
    )) as UserManagementView;
    await waitUntil(() => !(element as any).isLoading, 'still loading');
    (element as any).openCreateModal();
    (element as any).newUser = {
      username: 'jane',
      email: 'jane@example.com',
      password: 'password123',
    };
    await (element as any).handleCreateUser();
    await element.updateComplete;

    expect((element as any).isCreateModalOpen).to.equal(true);
    const dialog = element.shadowRoot!.querySelector(
      'sl-dialog[label="Create user"]'
    )!;
    expect(dialog.querySelector('sl-alert')?.textContent).to.contain(
      'Username already taken'
    );
    expect(element.shadowRoot!.querySelector('div.error')).to.not.exist;
  });

  it('lists roles by their display names in the roles dialog', async () => {
    fetchStub = createFetchStub({ users: [sampleUser] });
    const element = await mountWithUser();
    await waitUntil(() => (element as any).roles.length === 1);
    await element.updateComplete;
    const box = element.shadowRoot!.querySelector(
      'sl-dialog[label="Manage roles"] sl-checkbox'
    );
    expect(box?.textContent?.trim()).to.equal('Admin');
  });
});

describe('UserManagementView subaccount access', () => {
  let api: MockApi | undefined;
  beforeEach(() => signInForTest());
  afterEach(() => {
    api?.restore();
    api = undefined;
    localStorage.clear();
  });

  const SUBS = '/api/v1/accounts/acc-root/subaccounts';
  const GRANTS = '/api/v1/accounts/acc-root/access-grants';

  async function renderView(subaccounts: unknown[], grants: unknown[] = []) {
    api = mockApi({
      capabilities: ['account_hierarchy', 'user_management'] as Capability[],
      routes: [
        { path: SUBS, body: { items: subaccounts } },
        { path: GRANTS, body: { items: grants } },
        {
          method: 'POST',
          path: GRANTS,
          status: 201,
          body: (call) => ({ id: 'grant-new', ...(call.body as object) }),
        },
        {
          method: 'DELETE',
          path: /^\/api\/v1\/accounts\/acc-root\/access-grants\//,
          status: 204,
        },
        {
          path: '/api/v1/users',
          body: {
            users: [
              {
                id: 'user-1',
                username: 'alice',
                email: 'alice@example.com',
                full_name: 'Alice Example',
                is_active: true,
                user_source: 'local',
                email_verified: true,
              },
            ],
            total: 1,
          },
        },
        { path: /^\/api\/v1\/(users|teams)$/, body: { users: [], total: 0 } },
        { path: /^\/api\/v1\/roles/, body: { roles: [] } },
      ],
      fallback: { status: 200, body: [] },
    });
    const el = await fixture<UserManagementView>(
      html`<user-management-view></user-management-view>`
    );
    await waitUntil(
      () => el.shadowRoot!.querySelector('tr[data-user-id]'),
      'rows did not render'
    );
    await waitUntil(() => api!.callsTo(SUBS).length > 0, 'no subaccount read');
    await new Promise((r) => setTimeout(r, 0));
    await el.updateComplete;
    return el;
  }

  it('offers no Subaccount access action without subaccounts', async () => {
    const el = await renderView([]);
    expect(
      el.shadowRoot!.querySelector('[data-testid="subaccount-access"]')
    ).to.equal(null);
    expect(el.shadowRoot!.querySelector('subaccount-access-dialog')).to.equal(
      null
    );
  });

  it('grants and revokes access to a subaccount from the row', async () => {
    const el = await renderView(
      [{ id: 'sub-a', name: 'North' }],
      [
        {
          id: 'grant-1',
          subject_type: 'user',
          subject_id: 'user-1',
          level: 'operate',
          target: 'selected',
          subaccount_ids: ['sub-a'],
        },
        {
          id: 'grant-other',
          subject_type: 'user',
          subject_id: 'someone-else',
          level: 'admin',
          target: 'all',
        },
      ]
    );
    const action = el.shadowRoot!.querySelector(
      '[data-testid="subaccount-access"]'
    ) as HTMLElement;
    expect(action).to.exist;
    action.click();
    const dialog = el.shadowRoot!.querySelector(
      'subaccount-access-dialog'
    ) as SubaccountAccessDialog;
    await waitUntil(
      () => dialog.shadowRoot?.querySelector('[data-testid="subject-grants"]'),
      'grants did not load'
    );
    const listed = [
      ...dialog.shadowRoot!.querySelectorAll(
        '[data-testid="subject-grants"] li'
      ),
    ];
    // Only this user's grants, named by subaccount.
    expect(listed.map((li) => li.getAttribute('data-id'))).to.eql(['grant-1']);
    expect(listed[0].textContent).to.contain('North');

    const grant = [...dialog.shadowRoot!.querySelectorAll('sl-button')].find(
      (b) => b.textContent?.trim() === 'Grant access'
    ) as HTMLElement;
    grant.click();
    await waitUntil(
      () => api!.calls.some((c) => c.method === 'POST' && c.path === GRANTS),
      'no grant created'
    );
    const post = api!.calls.find(
      (c) => c.method === 'POST' && c.path === GRANTS
    )!;
    expect(post.body).to.eql({
      subject_type: 'user',
      subject_id: 'user-1',
      level: 'read',
      target: 'all',
    });

    await waitUntil(() => !(dialog as any).busy, 'grant did not settle');
    await dialog.updateComplete;
    const revoke = [...dialog.shadowRoot!.querySelectorAll('sl-button')].find(
      (b) => b.textContent?.trim() === 'Revoke'
    ) as HTMLElement;
    revoke.click();
    // Revoking asks first, naming who and where, as Access grants does.
    const asked = await answerConfirmDialog(true);
    expect(asked).to.contain('Revoke');
    expect(asked).to.contain('North');
    await waitUntil(
      () =>
        api!.calls.some(
          (c) => c.method === 'DELETE' && c.path === `${GRANTS}/grant-1`
        ),
      'no grant revoked'
    );
    expect(
      dialog.shadowRoot!.querySelector(
        'a[href="/console/settings/access-grants"]'
      )
    ).to.exist;
  });
});

describe('membership editing scope', () => {
  it('shows an explicit unavailable state for a quarantined identity', async () => {
    invalidateApiCaches();
    localStorage.setItem('accessToken', 'test-token');
    const fetchStub = sinon.stub(window, 'fetch').callsFake(
      async (input) =>
        new Response(
          JSON.stringify(
            String(input).includes('features')
              ? { features: { user_management: true }, plugins: [] }
              : String(input).includes('/auth/')
                ? { permissions: null }
                : String(input).includes('/roles')
                  ? { roles: [] }
                  : {
                      users: [
                        {
                          id: 'membership-child',
                          account_id: 'account-child',
                          person_id: 'person-example',
                          username: null,
                          email: null,
                          full_name: null,
                          identity_status: 'quarantined',
                          is_active: true,
                        },
                      ],
                      total: 1,
                    }
          ),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        )
    );
    try {
      const element = await fixture<UserManagementView>(
        html`<user-management-view></user-management-view>`
      );
      await waitUntil(() => !(element as any).isLoading);
      await element.updateComplete;
      expect(element.shadowRoot!.textContent).to.contain(
        'Identity unavailable'
      );
      expect(element.shadowRoot!.textContent).to.contain(
        'migration conflict resolution'
      );
      element.openEditModal((element as any).users[0]);
      await element.updateComplete;
      expect((element as any).editUser).to.deep.equal({ is_active: true });
      const dialog = element.shadowRoot!.querySelector(
        'sl-dialog[label="Edit account membership"]'
      )!;
      expect(dialog.querySelector('sl-input')).to.equal(null);
      expect(dialog.textContent).to.contain('Identity unavailable');
      expect(dialog.textContent).to.contain('membership-child');
      const pending = element.handleDeactivateUser((element as any).users[0]);
      const prompt = await answerConfirmDialog(false);
      await pending;
      expect(prompt).to.contain('Identity unavailable');
      expect(prompt).to.contain('membership-child');
      expect(prompt).not.to.contain('null');
    } finally {
      fetchStub.restore();
      localStorage.clear();
      invalidateApiCaches();
    }
  });
});
