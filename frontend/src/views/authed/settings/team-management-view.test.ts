import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import { invalidateApiCaches } from '../../../api';
import { resetConfirmDialogForTests } from '../../../components/confirm-dialog';
import { answerConfirmDialog } from '../../../utils/test-confirm-dialog';
import './team-management-view';
import type { TeamManagementView } from './team-management-view';

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
