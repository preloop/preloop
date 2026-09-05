import { html, fixture, expect } from '@open-wc/testing';
import sinon from 'sinon';
import '../../components/view-header.ts';
import './tracker-detail-view';
import type { TrackerDetailView } from './tracker-detail-view';

const tick = (ms = 200) => new Promise((r) => setTimeout(r, ms));

const trackerId = '11111111-1111-1111-1111-111111111111';
const projectA = {
  id: 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
  name: 'Alpha',
  organization_id: 'org-1',
};
const projectB = {
  id: 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb',
  name: 'Beta',
  organization_id: 'org-1',
};

interface StubOpts {
  issues?: unknown[];
  total?: number;
}

function stubFetch(opts: StubOpts = {}) {
  const { issues = [], total = issues.length } = opts;
  return sinon
    .stub(window, 'fetch')
    .callsFake(async (input: RequestInfo | URL) => {
      const url = String(input);
      const json = (data: unknown, status = 200) =>
        new Response(JSON.stringify(data), { status });
      if (
        url.includes(`/api/v1/trackers/${trackerId}`) &&
        !url.includes('/sync')
      ) {
        return json({
          id: trackerId,
          name: 'Example tracker',
          tracker_type: 'github',
          created: '2026-01-01T00:00:00Z',
          last_updated: '2026-01-02T00:00:00Z',
          is_valid: true,
        });
      }
      if (url.includes('/api/v1/features')) {
        return json({ features: {} });
      }
      if (url.includes('/api/v1/organizations')) {
        return json({
          items: [{ id: 'org-1', name: 'Org', tracker_id: trackerId }],
        });
      }
      if (url.includes('/api/v1/projects')) {
        return json([projectA, projectB]);
      }
      if (url.includes('/api/v1/issues?')) {
        return json({
          items: issues,
          total,
          skip: 0,
          limit: 20,
        });
      }
      return json({});
    });
}

async function mountView() {
  const el = (await fixture(
    html`<tracker-detail-view></tracker-detail-view>`
  )) as TrackerDetailView;
  (el as unknown as { location: { params: { trackerId: string } } }).location =
    {
      params: { trackerId },
    };
  (el as unknown as { _trackerId: string })._trackerId = trackerId;
  await (el as unknown as { _loadData: () => Promise<void> })._loadData();
  await el.updateComplete;
  await tick(50);
  await el.updateComplete;
  return el;
}

describe('TrackerDetailView', () => {
  let fetchStub: sinon.SinonStub;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    window.history.replaceState(
      {},
      '',
      `/console/trackers/${trackerId}?tab=issues&project=${projectA.id}&status=open`
    );
  });

  afterEach(() => {
    fetchStub?.restore();
    localStorage.clear();
    window.history.replaceState({}, '', '/');
  });

  it('renders Issues tab rows from listIssues', async () => {
    fetchStub = stubFetch({
      issues: [
        {
          id: 'issue-1',
          key: 'ALP-1',
          title: 'Fix login',
          status: 'open',
          updated_at: '2026-01-03T00:00:00Z',
          project: 'Alpha',
          project_id: projectA.id,
          url: 'https://example.com/1',
        },
      ],
      total: 1,
    });
    const el = await mountView();
    await (
      el as unknown as { _loadIssues: (reset: boolean) => Promise<void> }
    )._loadIssues(true);
    await el.updateComplete;
    expect(el.shadowRoot?.textContent).to.contain('ALP-1');
    expect(el.shadowRoot?.textContent).to.contain('Fix login');
    const issueCalls = fetchStub
      .getCalls()
      .filter((call) => String(call.args[0]).includes('/api/v1/issues?'));
    expect(issueCalls.length).to.be.greaterThan(0);
  });

  it('switching project updates the URL and refetches', async () => {
    fetchStub = stubFetch({ issues: [] });
    const el = await mountView();
    await (
      el as unknown as { _loadIssues: (reset: boolean) => Promise<void> }
    )._loadIssues(true);
    const before = fetchStub
      .getCalls()
      .filter((call) =>
        String(call.args[0]).includes('/api/v1/issues?')
      ).length;
    (
      el as unknown as { _onProjectFilter: (event: Event) => void }
    )._onProjectFilter({
      target: { value: projectB.id },
    } as unknown as Event);
    await tick(50);
    await el.updateComplete;
    expect(window.location.search).to.contain(`project=${projectB.id}`);
    const after = fetchStub
      .getCalls()
      .filter((call) =>
        String(call.args[0]).includes('/api/v1/issues?')
      ).length;
    expect(after).to.be.greaterThan(before);
  });

  it('empty state copy for project with no open issues', async () => {
    fetchStub = stubFetch({ issues: [], total: 0 });
    const el = await mountView();
    (el as unknown as { _selectedProjectId: string })._selectedProjectId =
      projectA.id;
    await (
      el as unknown as { _loadIssues: (reset: boolean) => Promise<void> }
    )._loadIssues(true);
    await el.updateComplete;
    expect(el.shadowRoot?.textContent).to.contain(
      'No open issues in Alpha. Switch the status filter to see closed issues.'
    );
  });
});
