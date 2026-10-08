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
    // Delete is outline and last, after the gap.
    const del = element.shadowRoot?.querySelector(
      '.team-actions sl-button[variant="danger"]'
    );
    expect(del?.hasAttribute('outline')).to.equal(true);
    expect(element.shadowRoot?.textContent).to.contain('Platform');
    expect(element.shadowRoot?.textContent).to.contain(
      'Platform engineering team'
    );
  });

  it('explains teams and offers to create one when there are none', async () => {
    fetchStub = createFetchStub({ teams: [] });
    const element = (await fixture(
      html`<team-management-view></team-management-view>`
    )) as TeamManagementView;

    await waitUntil(() => !(element as any).isLoading, 'still loading');
    await element.updateComplete;

    const cards = element.shadowRoot?.querySelectorAll('.teams-grid sl-card');
    expect(cards?.length).to.equal(0);
    const empty = element.shadowRoot?.querySelector('.empty-state');
    expect(empty?.textContent).to.contain('No teams yet');
    const create = empty?.querySelector('sl-button') as HTMLElement;
    create.click();
    await element.updateComplete;
    expect((element as any).isCreateModalOpen).to.equal(true);
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

  it('names every icon-only action for assistive tech', async () => {
    fetchStub = createFetchStub({ teams: [sampleTeam] });
    const element = (await fixture(
      html`<team-management-view></team-management-view>`
    )) as TeamManagementView;
    await waitUntil(() => (element as any).teams?.length === 1);
    await element.updateComplete;
    const labels = [
      ...element.shadowRoot!.querySelectorAll('.team-actions sl-icon'),
    ].map((icon) => icon.getAttribute('label'));
    expect(labels).to.deep.equal([
      'Manage roles',
      'Members',
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
      '.team-actions sl-button[variant="danger"]'
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
      () => el.shadowRoot!.querySelector('[data-id], sl-card'),
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
