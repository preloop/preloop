import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

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
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
  });

  afterEach(() => {
    fetchStub?.restore();
    localStorage.clear();
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

    expect(element.shadowRoot?.textContent).to.contain('Team Management');
    expect(element.shadowRoot?.textContent).to.contain('Platform');
    expect(element.shadowRoot?.textContent).to.contain(
      'Platform engineering team'
    );
  });

  it('renders an empty grid when there are no teams', async () => {
    fetchStub = createFetchStub({ teams: [] });
    const element = (await fixture(
      html`<team-management-view></team-management-view>`
    )) as TeamManagementView;

    await waitUntil(() => !(element as any).isLoading, 'still loading');
    await element.updateComplete;

    const cards = element.shadowRoot?.querySelectorAll('.teams-grid sl-card');
    expect(cards?.length).to.equal(0);
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
});
