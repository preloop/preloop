import { html, fixture, expect } from '@open-wc/testing';
import sinon from 'sinon';
import '../../components/view-header.ts';
import { resetRunPresetDialogForTests } from '../../components/run-preset-dialog';
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
  trackerType?: string;
  pullRequests?: unknown[];
  prHasMore?: boolean;
  prSupported?: boolean;
}

function stubFetch(opts: StubOpts = {}) {
  const {
    issues = [],
    total = issues.length,
    trackerType = 'github',
    pullRequests = [],
    prHasMore = false,
    prSupported = true,
  } = opts;
  return sinon
    .stub(window, 'fetch')
    .callsFake(async (input: RequestInfo | URL, init?: RequestInit) => {
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
          tracker_type: trackerType,
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
      if (url.includes('/pull-requests')) {
        return json({
          items: pullRequests,
          page: 1,
          limit: 20,
          has_more: prHasMore,
          supported: prSupported,
          fetched_at: '2026-01-03T00:00:00Z',
        });
      }
      if (url.includes('/api/v1/projects')) {
        return json([projectA, projectB]);
      }
      if (url.includes('/api/v1/issues?')) {
        const parsed = new URL(url, 'http://localhost');
        const q = parsed.searchParams.get('q') || '';
        const items = q ? [] : issues;
        const listed = q ? 0 : total;
        return json({
          items,
          total: listed,
          skip: 0,
          limit: 20,
        });
      }
      if (url.includes('/api/v1/flows/run-preset')) {
        const body = init?.body ? JSON.parse(String(init.body)) : {};
        if (!body.confirm_create) {
          return json(
            {
              detail: {
                code: 'flow_missing',
                flow_name: 'Pull Request Reviewer',
              },
            },
            409
          );
        }
        return json({
          execution_id: 'exec-1',
          flow_id: 'flow-1',
          flow_name: 'Pull Request Reviewer',
          flow_created: true,
          execution_url: '/console/flows/executions/exec-1',
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
    resetRunPresetDialogForTests();
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
    const actionsHeader = el.shadowRoot?.querySelector('th .visually-hidden');
    expect(actionsHeader?.textContent?.trim()).to.equal('Actions');
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

  it('search with no matches uses its own empty line', async () => {
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
    (
      el as unknown as { _onIssueSearch: (event: Event) => void }
    )._onIssueSearch({
      target: { value: 'zzzz-no-match' },
    } as unknown as Event);
    await tick(300);
    await el.updateComplete;
    const searchCall = fetchStub
      .getCalls()
      .map((call) => String(call.args[0]))
      .find((url) => url.includes('q=zzzz-no-match'));
    expect(searchCall).to.exist;
    expect(el.shadowRoot?.textContent).to.contain(
      "No issues match 'zzzz-no-match'."
    );
    expect(el.shadowRoot?.textContent).to.not.contain(
      'Switch the status filter to see closed issues.'
    );
  });

  it('drops a slower issues response after a newer project switch', async () => {
    fetchStub = stubFetch({ issues: [] });
    const el = await mountView();
    let releaseFirst: (() => void) | undefined;
    const firstGate = new Promise<void>((resolve) => {
      releaseFirst = resolve;
    });
    let issueCalls = 0;
    fetchStub.restore();
    fetchStub = sinon.stub(window, 'fetch').callsFake(async (input) => {
      const url = String(input);
      const json = (data: unknown) =>
        new Response(JSON.stringify(data), { status: 200 });
      if (!url.includes('/api/v1/issues?')) {
        return json({});
      }
      issueCalls += 1;
      const parsed = new URL(url, 'http://localhost');
      const project = parsed.searchParams.get('project_id');
      if (issueCalls === 1) {
        await firstGate;
        return json({
          items: [
            {
              id: 'stale',
              key: 'OLD-1',
              title: 'Stale row',
              status: 'open',
              updated_at: '2026-01-03T00:00:00Z',
              project: 'Alpha',
              project_id: projectA.id,
              url: 'https://example.com/old',
            },
          ],
          total: 1,
          skip: 0,
          limit: 20,
        });
      }
      return json({
        items: [
          {
            id: 'fresh',
            key: 'NEW-1',
            title: 'Fresh row',
            status: 'open',
            updated_at: '2026-01-04T00:00:00Z',
            project: 'Beta',
            project_id: project,
            url: 'https://example.com/new',
          },
        ],
        total: 1,
        skip: 0,
        limit: 20,
      });
    });
    const firstLoad = (
      el as unknown as { _loadIssues: (reset: boolean) => Promise<void> }
    )._loadIssues(true);
    (
      el as unknown as { _onProjectFilter: (event: Event) => void }
    )._onProjectFilter({
      target: { value: projectB.id },
    } as unknown as Event);
    await tick(20);
    releaseFirst?.();
    await firstLoad;
    await tick(50);
    await el.updateComplete;
    expect(el.shadowRoot?.textContent).to.contain('NEW-1');
    expect(el.shadowRoot?.textContent).to.not.contain('OLD-1');
  });

  it('Load more is a button with a loading state', async () => {
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
      total: 40,
    });
    const el = await mountView();
    await (
      el as unknown as { _loadIssues: (reset: boolean) => Promise<void> }
    )._loadIssues(true);
    await el.updateComplete;
    const loadMore = el.shadowRoot?.querySelector('sl-button.load-more');
    expect(loadMore).to.exist;
    expect(loadMore?.textContent).to.contain('Load more');
    expect(el.shadowRoot?.querySelector('a.load-more')).to.not.exist;
  });

  it('renders MR label for gitlab trackers', async () => {
    window.history.replaceState(
      {},
      '',
      `/console/trackers/${trackerId}?tab=pull-requests&project=${projectA.id}`
    );
    fetchStub = stubFetch({
      trackerType: 'gitlab',
      pullRequests: [
        {
          number: 7,
          iid: 7,
          title: 'Fix login',
          url: 'https://gitlab.example.com/group/project/-/merge_requests/7',
          author: 'janedoe',
          source_branch: 'feature',
          target_branch: 'main',
          state: 'open',
          draft: false,
          updated_at: '2026-01-03T00:00:00Z',
        },
      ],
    });
    const el = await mountView();
    await (
      el as unknown as { _loadPullRequests: (reset: boolean) => Promise<void> }
    )._loadPullRequests(true);
    await el.updateComplete;
    const text = el.shadowRoot?.textContent || '';
    expect(text).to.contain('Merge requests');
    expect(text).to.contain('Open merge requests');
    expect(text).to.contain('Live from GitLab, refreshed every minute.');
    expect(text).to.contain('Fix login');
    expect(text).to.contain('#7');
  });

  it('hides tab for jira', async () => {
    window.history.replaceState({}, '', `/console/trackers/${trackerId}`);
    fetchStub = stubFetch({ trackerType: 'jira' });
    const el = await mountView();
    await el.updateComplete;
    const tabs = Array.from(
      el.shadowRoot?.querySelectorAll('sl-tab') || []
    ).map((tab) => tab.textContent?.trim());
    expect(tabs).to.include('Projects');
    expect(tabs).to.include('Issues');
    expect(tabs).to.not.include('Pull requests');
    expect(tabs).to.not.include('Merge requests');
    expect(el.shadowRoot?.textContent).to.not.contain('Run reviewer');
  });

  it('Run reviewer sends pull_request target', async () => {
    window.history.replaceState(
      {},
      '',
      `/console/trackers/${trackerId}?tab=pull-requests&project=${projectA.id}`
    );
    fetchStub = stubFetch({
      pullRequests: [
        {
          number: 12,
          iid: 12,
          title: 'Add login',
          url: 'https://github.com/acme/widgets/pull/12',
          author: 'janedoe',
          source_branch: 'feature',
          target_branch: 'main',
          state: 'open',
          draft: false,
          updated_at: '2026-01-03T00:00:00Z',
        },
      ],
    });
    const el = await mountView();
    (el as unknown as { _selectedProjectId: string })._selectedProjectId =
      projectA.id;
    await (
      el as unknown as { _loadPullRequests: (reset: boolean) => Promise<void> }
    )._loadPullRequests(true);
    await el.updateComplete;
    (
      el as unknown as {
        _runReviewer: (pr: { number: number }) => void;
      }
    )._runReviewer({ number: 12 });
    await tick(50);
    const runCalls = fetchStub
      .getCalls()
      .filter((call) =>
        String(call.args[0]).includes('/api/v1/flows/run-preset')
      );
    expect(runCalls.length).to.be.greaterThan(0);
    const init = runCalls[0].args[1] as RequestInit;
    const body = JSON.parse(String(init.body));
    expect(body.preset_slug).to.equal('pull-request-reviewer');
    expect(body.target).to.deep.equal({
      kind: 'pull_request',
      project_id: projectA.id,
      number: 12,
    });
  });
});
