import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import { invalidateApiCaches } from '../../../api';
import { resetConfirmDialogForTests } from '../../../components/confirm-dialog';
import { answerConfirmDialog } from '../../../utils/test-confirm-dialog';
import './team-management-view';
import type { TeamManagementView } from './team-management-view';
import {
  mockApi,
  signInForTest,
  type MockApi,
} from '../../../test-helpers/capability-api';
import type { Capability } from '../../../capabilities';
import type { SubaccountAccessDialog } from '../hierarchy/subaccount-access-dialog';

describe('TeamManagementView', () => {
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
      teams?: unknown[];
      teamsFail?: boolean;
      advancedRoles?: boolean;
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
            features: {
              user_management: featureEnabled,
              rbac: opts.advancedRoles === true,
            },
          });
        }

        if (url.includes('/api/v1/teams') && method === 'GET') {
          if (opts.teamsFail) {
            return json({ detail: 'boom' }, 500);
          }
          return json({
            teams: opts.teams ?? [],
            total: (opts.teams ?? []).length,
          });
        }

        if (url.includes('/api/v1/teams/') && method === 'DELETE') {
          return new Response(null, { status: 204 });
        }

        if (url.includes('/api/v1/teams') && method === 'POST') {
          return json({ id: 'team-new', name: 'New Team' });
        }

        if (url.includes('/api/v1/users')) {
          return json({ users: [], total: 0 });
        }

        if (url.includes('/api/v1/roles')) {
          return json({ roles: [] });
        }

        return json({ detail: `Unhandled: ${method} ${url}` }, 500);
      });
  }

  const sampleTeam = {
    id: 'team-1',
    name: 'Platform',
    description: 'Platform engineering team',
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
      html`<team-management-view></team-management-view>`
    )) as TeamManagementView;

    await waitUntil(() => !(element as any).isLoading, 'still loading');
    await element.updateComplete;

    expect(element.shadowRoot?.textContent).to.contain(
      'not available in this edition'
    );
  });

  it('renders the team list when teams exist', async () => {
    fetchStub = createFetchStub({ teams: [sampleTeam] });
    const element = (await fixture(
      html`<team-management-view></team-management-view>`
    )) as TeamManagementView;

    await waitUntil(
      () => (element as any).teams?.length === 1,
      'teams did not load'
    );
    await element.updateComplete;

    // The page is called what the sidebar calls it, in the shared header.
    expect(
      (element.shadowRoot?.querySelector('view-header') as any)?.headerText
    ).to.equal('Teams');
    // A console table, not a card per team.
    const root = element.shadowRoot!;
    expect(root.querySelector('.teams-grid sl-card')).to.equal(null);
    const headers = [
      ...root.querySelectorAll('table.styled-table thead th'),
    ].map((th) => (th.textContent || '').trim());
    expect(headers).to.deep.equal(['Name', 'Description', 'Roles', 'Actions']);
    expect(root.querySelectorAll('tbody tr[data-team-id]').length).to.equal(1);
    root.querySelectorAll('tbody sl-badge').forEach((chip) => {
      expect(chip.hasAttribute('pill')).to.equal(true);
      expect(chip.getAttribute('variant')).to.equal('neutral');
    });
    // No red button on the row: Delete is the kebab's last item.
    expect(root.querySelector('tbody sl-button[variant="danger"]')).to.equal(
      null
    );
    const items = [...root.querySelectorAll('.row-menu sl-menu-item')];
    const del = items[items.length - 1];
    expect(del.getAttribute('data-action')).to.equal('delete');
    expect(del.classList.contains('danger-item')).to.equal(true);
    expect(del.previousElementSibling?.tagName).to.equal('SL-DIVIDER');
    expect(element.shadowRoot?.textContent).to.contain('Platform');
    expect(element.shadowRoot?.textContent).to.contain(
      'Platform engineering team'
    );
  });

  it('links each team member to their person page', async () => {
    fetchStub = createFetchStub({ teams: [sampleTeam] });
    const element = (await fixture(
      html`<team-management-view></team-management-view>`
    )) as TeamManagementView;
    await waitUntil(
      () => (element as any).teams?.length === 1,
      'teams did not load'
    );
    (element as any).users = [
      {
        id: 'user-1',
        username: 'jane',
        full_name: 'Jane Doe',
        email: 'jane@example.com',
      },
    ];
    (element as any).teamMembers = [
      { id: 'member-1', team_id: 'team-1', user_id: 'user-1' },
    ];
    (element as any).isMembersModalOpen = true;
    await element.updateComplete;

    const link = element.shadowRoot!.querySelector(
      '.member-name a'
    ) as HTMLAnchorElement;
    expect(link.getAttribute('href')).to.equal(
      '/console/settings/users/user-1'
    );
    expect(link.textContent!.trim()).to.equal('Jane Doe');
  });

  it('explains teams and offers to create one when there are none', async () => {
    fetchStub = createFetchStub({ teams: [] });
    const element = (await fixture(
      html`<team-management-view></team-management-view>`
    )) as TeamManagementView;

    await waitUntil(() => !(element as any).isLoading, 'still loading');
    await element.updateComplete;

    expect(element.shadowRoot?.querySelector('table')).to.equal(null);
    const empty = element.shadowRoot?.querySelector('.empty-state');
    expect(empty?.textContent).to.contain('No teams yet');
    const create = empty?.querySelector('sl-button') as HTMLElement;
    create.click();
    await element.updateComplete;
    expect((element as any).isCreateModalOpen).to.equal(true);
  });

  it('offers an upgrade for team role inheritance while basic teams work in OSS', async () => {
    fetchStub = createFetchStub({ teams: [sampleTeam] });
    const element = await fixture<TeamManagementView>(
      html`<team-management-view></team-management-view>`
    );
    await waitUntil(() => (element as any).teams?.length === 1);
    await waitUntil(() =>
      (element as any).editPermissions.allows('manage_teams')
    );
    const upgrade = sinon.spy();
    window.addEventListener('show-upgrade-modal', upgrade);
    try {
      await element.openRoleModal(sampleTeam as any);
      expect(upgrade).to.have.been.calledOnce;
      expect(upgrade.firstCall.args[0].detail.feature).to.equal('rbac');
      expect((element as any).isRoleModalOpen).to.equal(false);
      expect(
        fetchStub
          .getCalls()
          .some(
            (call) =>
              String(call.args[0]).includes('/teams/team-1/roles') ||
              String(call.args[0]).includes('/api/v1/roles')
          )
      ).to.equal(false);
    } finally {
      window.removeEventListener('show-upgrade-modal', upgrade);
    }
  });

  it('opens team role inheritance when the advanced capability is enabled', async () => {
    fetchStub = createFetchStub({ teams: [sampleTeam], advancedRoles: true });
    const element = await fixture<TeamManagementView>(
      html`<team-management-view></team-management-view>`
    );
    await waitUntil(() => (element as any).teams?.length === 1);
    await waitUntil(() =>
      (element as any).editPermissions.allows('manage_teams')
    );
    await element.openRoleModal(sampleTeam as any);
    expect((element as any).isRoleModalOpen).to.equal(true);
  });

  it('shows an error when team loading fails', async () => {
    fetchStub = createFetchStub({ teamsFail: true });
    const element = (await fixture(
      html`<team-management-view></team-management-view>`
    )) as TeamManagementView;

    await waitUntil(
      () => (element as any).error !== null,
      'error did not appear'
    );
    await element.updateComplete;

    expect(element.shadowRoot?.querySelector('.error')).to.exist;
  });

  it('creates a new team', async () => {
    fetchStub = createFetchStub({ teams: [] });
    const element = (await fixture(
      html`<team-management-view></team-management-view>`
    )) as TeamManagementView;

    await waitUntil(() => !(element as any).isLoading, 'still loading');

    (element as any).newTeam = { name: 'New Team' };
    await (element as any).handleCreateTeam();
    await element.updateComplete;

    const postCall = fetchStub
      .getCalls()
      .find(
        (c) =>
          String(c.args[0]).includes('/api/v1/teams') &&
          (c.args[1]?.method || 'GET').toUpperCase() === 'POST'
      );
    expect(postCall, 'expected a POST to /api/v1/teams').to.exist;
    expect((element as any).isCreateModalOpen).to.be.false;
  });

  it('names the row menu and every action in it', async () => {
    fetchStub = createFetchStub({ teams: [sampleTeam] });
    const element = (await fixture(
      html`<team-management-view></team-management-view>`
    )) as TeamManagementView;
    await waitUntil(() => (element as any).teams?.length === 1);
    await element.updateComplete;
    expect(
      element
        .shadowRoot!.querySelector('.row-menu sl-icon-button[slot="trigger"]')!
        .getAttribute('label')
    ).to.equal('Actions for Platform');
    const labels = [
      ...element.shadowRoot!.querySelectorAll('.row-menu sl-menu-item'),
    ].map((item) => (item.textContent || '').trim());
    expect(labels).to.deep.equal([
      'Members',
      'Manage roles',
      'Edit team',
      'Delete team',
    ]);
  });

  it('asks before deleting a team and says what members lose', async () => {
    fetchStub = createFetchStub({ teams: [sampleTeam] });
    const element = (await fixture(
      html`<team-management-view></team-management-view>`
    )) as TeamManagementView;
    await waitUntil(() => (element as any).teams?.length === 1);
    await element.updateComplete;
    const deletes = () =>
      fetchStub.getCalls().filter((call) => call.args[1]?.method === 'DELETE');
    const del = element.shadowRoot!.querySelector(
      '.row-menu sl-menu-item[data-action="delete"]'
    ) as HTMLElement;

    del.click();
    const prompt = await answerConfirmDialog(false);
    expect(prompt).to.contain('Platform');
    expect(prompt).to.contain('lose any roles');
    expect(deletes()).to.have.length(0);

    del.click();
    await answerConfirmDialog(true);
    await waitUntil(() => deletes().length === 1);
    expect(String(deletes()[0].args[0])).to.contain('/api/v1/teams/team-1');
  });

  it('asks for a team name inside the create dialog', async () => {
    fetchStub = createFetchStub({ teams: [] });
    const element = (await fixture(
      html`<team-management-view></team-management-view>`
    )) as TeamManagementView;
    await waitUntil(() => !(element as any).isLoading, 'still loading');
    (element as any).openCreateModal();
    await (element as any).handleCreateTeam();
    await element.updateComplete;
    const dialog = element.shadowRoot!.querySelector(
      'sl-dialog[label="Create team"]'
    )!;
    expect(dialog.querySelector('sl-alert')?.textContent).to.contain(
      'Enter a team name.'
    );
  });
});

describe('TeamManagementView subaccount access', () => {
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
          path: '/api/v1/teams',
          body: {
            teams: [
              {
                id: 'team-1',
                name: 'Platform',
                description: '',
                member_count: 0,
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
    const el = await fixture<TeamManagementView>(
      html`<team-management-view></team-management-view>`
    );
    await waitUntil(
      () => el.shadowRoot!.querySelector('tr[data-team-id]'),
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
          subject_type: 'team',
          subject_id: 'team-1',
          level: 'operate',
          target: 'selected',
          subaccount_ids: ['sub-a'],
        },
        {
          id: 'grant-other',
          subject_type: 'team',
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
    // Only this team's grants, named by subaccount.
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
      subject_type: 'team',
      subject_id: 'team-1',
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
