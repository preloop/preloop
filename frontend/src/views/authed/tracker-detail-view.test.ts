import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';
import '../../components/view-header.ts';
import { resetRunPresetDialogForTests } from '../../components/run-preset-dialog';
import { resetConfirmDialogForTests } from '../../components/confirm-dialog';
import { invalidateApiCaches } from '../../api';
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
  openCounts?: Record<string, number>;
  trackerType?: string;
  trackerExtra?: Record<string, unknown>;
  pullRequests?: unknown[];
  prHasMore?: boolean;
  prTotal?: number;
  prSupported?: boolean;
  pullRequestHandler?: (url: string) => Response | null;
}

function stubFetch(opts: StubOpts = {}) {
  const {
    issues = [],
    total = issues.length,
    openCounts,
    trackerType = 'github',
    trackerExtra = {},
    pullRequests = [],
    prHasMore = false,
    prTotal,
    prSupported = true,
    pullRequestHandler,
  } = opts;
  return sinon
    .stub(window, 'fetch')
    .callsFake(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const json = (data: unknown, status = 200) =>
        new Response(JSON.stringify(data), { status });
      if (url.includes('/azure-devops/webhook')) {
        return json({
          callback_url: null,
          callback_configured: false,
          required_events: [],
          signature: 'missing_secret',
          registration: null,
          instructions: '',
          header_name: 'X-Preloop-Webhook-Secret',
        });
      }
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
          ...trackerExtra,
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
        const override = pullRequestHandler?.(url);
        if (override) return override;
        return json({
          items: pullRequests,
          page: 1,
          limit: 20,
          has_more: prHasMore,
          supported: prSupported,
          fetched_at: '2026-01-03T00:00:00Z',
          ...(prTotal !== undefined ? { total: prTotal } : {}),
        });
      }
      if (url.includes('/api/v1/projects')) {
        return json([projectA, projectB]);
      }
      if (url.includes('/api/v1/issues?')) {
        const parsed = new URL(url, 'http://localhost');
        const projectId = parsed.searchParams.get('project_id') || '';
        if (openCounts && parsed.searchParams.get('limit') === '1') {
          return json({
            items: [],
            total: openCounts[projectId] ?? 0,
            skip: 0,
            limit: 1,
          });
        }
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
    sessionStorage.clear();
    window.history.replaceState({}, '', '/');
  });

  it('explains zero Azure repositories when the inclusion scope is empty', async () => {
    fetchStub = stubFetch({
      trackerType: 'azure_devops',
      trackerExtra: { scope_rules: [] },
    });
    const el = await mountView();
    (el as any)._projects = [];
    await el.updateComplete;
    const diagnostic = el.shadowRoot!.querySelector('.empty-repository-scope');
    expect(diagnostic?.textContent).to.contain('0 repositories imported');
    expect(diagnostic?.textContent).to.contain('No organization is included');
  });

  it('defaults to the project with the most open issues', async () => {
    window.history.replaceState(
      {},
      '',
      `/console/trackers/${trackerId}?tab=issues`
    );
    fetchStub = stubFetch({
      issues: [],
      openCounts: { [projectA.id]: 0, [projectB.id]: 7 },
    });
    const el = await mountView();
    expect(
      (el as unknown as { _selectedProjectId: string })._selectedProjectId
    ).to.equal(projectB.id);
    expect(window.location.search).to.contain(`project=${projectB.id}`);
  });

  it('reopens on the project read last in this session', async () => {
    sessionStorage.setItem(`preloop.tracker.project.${trackerId}`, projectA.id);
    window.history.replaceState(
      {},
      '',
      `/console/trackers/${trackerId}?tab=issues`
    );
    fetchStub = stubFetch({
      issues: [],
      openCounts: { [projectA.id]: 0, [projectB.id]: 7 },
    });
    const el = await mountView();
    expect(
      (el as unknown as { _selectedProjectId: string })._selectedProjectId
    ).to.equal(projectA.id);
  });

  it('remembers the project a link named', async () => {
    fetchStub = stubFetch({ issues: [] });
    await mountView();
    expect(
      sessionStorage.getItem(`preloop.tracker.project.${trackerId}`)
    ).to.equal(projectA.id);
  });

  it('skips the open issue probe when it cannot change the answer', async () => {
    const probeCalls = () =>
      fetchStub
        .getCalls()
        .map((call) => String(call.args[0]))
        .filter(
          (url) => url.includes('/api/v1/issues?') && url.includes('limit=1')
        );

    // Session memory already names the project.
    sessionStorage.setItem(`preloop.tracker.project.${trackerId}`, projectB.id);
    window.history.replaceState(
      {},
      '',
      `/console/trackers/${trackerId}?tab=issues`
    );
    fetchStub = stubFetch({
      issues: [],
      openCounts: { [projectA.id]: 0, [projectB.id]: 7 },
    });
    await mountView();
    expect(probeCalls(), 'remembered project needs no probe').to.deep.equal([]);
    fetchStub.restore();

    // A tab that does not list issues does not need the default either.
    sessionStorage.clear();
    window.history.replaceState(
      {},
      '',
      `/console/trackers/${trackerId}?tab=pull-requests`
    );
    fetchStub = stubFetch({
      issues: [],
      openCounts: { [projectA.id]: 0, [projectB.id]: 7 },
    });
    await mountView();
    expect(probeCalls(), 'pull requests tab needs no probe').to.deep.equal([]);
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
    const hiddenHeaders = [
      ...(el.shadowRoot?.querySelectorAll('th .visually-hidden') || []),
    ].map((node) => node.textContent?.trim());
    expect(hiddenHeaders).to.include('Actions');
    expect(hiddenHeaders).to.include('Select issues');
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

  // Wave 3 (C10): the Issues tab is a collection pane, not a card. It spans
  // the page whether it has rows or not, the tab is its title, and the
  // filters are the bar the Flows list established.
  it('renders the Issues tab as a full-width pane when it is empty', async () => {
    fetchStub = stubFetch({ issues: [], total: 0 });
    const el = await mountView();
    (el as unknown as { _selectedProjectId: string })._selectedProjectId =
      projectA.id;
    await (
      el as unknown as { _loadIssues: (reset: boolean) => Promise<void> }
    )._loadIssues(true);
    await el.updateComplete;

    const panel = el.shadowRoot?.querySelector(
      'sl-tab-panel[name="issues"] .collection-pane'
    ) as HTMLElement | null;
    expect(panel, 'the issues tab renders a pane').to.exist;
    expect(panel?.querySelector('sl-card'), 'no card around the collection').to
      .not.exist;

    // Spanning the page: the empty pane is as wide as its tab panel.
    const panelWidth = panel!.getBoundingClientRect().width;
    const hostWidth = el
      .shadowRoot!.querySelector('sl-tab-panel[name="issues"]')!
      .getBoundingClientRect().width;
    expect(panelWidth).to.be.greaterThan(0);
    expect(hostWidth - panelWidth).to.be.lessThan(2);

    // The bar carries search, the project, the status and the count.
    const toolbar = panel?.querySelector('list-toolbar');
    expect(toolbar?.getAttribute('searchPlaceholder')).to.equal(
      'Search key or title'
    );
    expect(panel?.querySelectorAll('sl-select').length).to.equal(2);
    expect(
      panel?.querySelector('[slot="count"]')?.textContent?.trim()
    ).to.equal('0 issues');

    // One line where the table would be, not a collapsed card.
    const empty = panel?.querySelector('.issues-empty') as HTMLElement | null;
    expect(empty?.textContent).to.contain('No open issues in Alpha');
    expect(empty!.getBoundingClientRect().height).to.be.at.least(72);
  });

  // A dropdown reading "Alpha" names nothing: each filter says what it
  // filters, on screen and to assistive tech.
  it('names the Issues pane filters on screen, not only to a screen reader', async () => {
    fetchStub = stubFetch({ issues: [], total: 0 });
    const el = await mountView();
    await el.updateComplete;

    const selects = Array.from(
      el.shadowRoot?.querySelectorAll(
        'sl-tab-panel[name="issues"] .collection-pane sl-select'
      ) || []
    );
    expect(selects.length).to.equal(2);
    expect(
      selects.map((select) =>
        select.querySelector('[slot="prefix"]')?.textContent?.trim()
      )
    ).to.deep.equal(['Project', 'Status']);
    // The accessible name survives too.
    expect(selects.map((select) => select.getAttribute('label'))).to.deep.equal(
      ['Project', 'Status']
    );

    const prList = el.shadowRoot?.querySelector(
      'sl-tab-panel[name="pull-requests"] .collection-pane sl-select [slot="prefix"]'
    );
    expect(prList?.textContent?.trim()).to.equal('Project');
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
    // Search is debounced; wait for that load instead of a fixed tick so
    // CI does not assert the empty line while the previous issues response
    // is still on screen.
    await tick(300);
    await (
      el as unknown as { _loadIssues: (reset: boolean) => Promise<void> }
    )._loadIssues(true);
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
    // The tab is the title, so the pane no longer repeats it as a card
    // heading; the count on the bar says what is on screen.
    expect(text).to.not.contain('Open merge requests');
    expect(text).to.contain('1 open merge request');
    expect(text).to.contain('Live from GitLab, refreshed every minute.');
    expect(text).to.contain('Fix login');
    expect(text).to.contain('#7');
    const actionsHeader = el.shadowRoot?.querySelector('th .visually-hidden');
    expect(actionsHeader?.textContent?.trim()).to.equal('Actions');
    const branches = el.shadowRoot?.querySelector('.pr-branches');
    expect(branches?.getAttribute('aria-label')).to.equal('feature to main');
    expect(
      branches?.querySelector('[aria-hidden="true"]')?.textContent
    ).to.contain('->');
    expect(
      branches?.querySelector('.visually-hidden')?.textContent?.trim()
    ).to.equal('to');
  });

  it('does not report a paged PR list as the total', async () => {
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
      prHasMore: true,
    });
    const el = await mountView();
    await (
      el as unknown as { _loadPullRequests: (reset: boolean) => Promise<void> }
    )._loadPullRequests(true);
    await el.updateComplete;
    const count = el.shadowRoot
      ?.querySelector('sl-tab-panel[name="pull-requests"] [slot="count"]')
      ?.textContent?.trim();
    expect(count).to.equal('showing 1 pull request');
    expect(count).to.not.contain('open pull request');
  });

  it('uses the API total when the PR list reports one', async () => {
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
      prHasMore: true,
      prTotal: 40,
    });
    const el = await mountView();
    await (
      el as unknown as { _loadPullRequests: (reset: boolean) => Promise<void> }
    )._loadPullRequests(true);
    await el.updateComplete;
    expect(
      el.shadowRoot
        ?.querySelector('sl-tab-panel[name="pull-requests"] [slot="count"]')
        ?.textContent?.trim()
    ).to.equal('1 of 40 open pull requests');
  });

  it('leaves the Branches cell empty when a branch is missing', async () => {
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
          source_branch: '',
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
    const branches = el.shadowRoot?.querySelector('.pr-branches');
    expect(branches?.textContent?.trim()).to.equal('');
    expect(el.shadowRoot?.textContent).to.not.contain('? ->');
  });

  for (const scenario of [
    {
      status: 500,
      detail: 'Configuration error for tracker: invalid settings',
      message: 'GitLab configuration error',
    },
    {
      status: 502,
      detail: 'Tracker request failed',
      message: 'GitLab request failed',
    },
    {
      status: 500,
      detail: 'Unexpected internal error',
      message: 'Could not load merge requests from GitLab',
    },
  ]) {
    it(`shows an honest count and actionable ${scenario.status} error for ${scenario.detail}`, async () => {
      history.replaceState(
        {},
        '',
        `/console/trackers/${trackerId}?tab=pull-requests&project=${projectA.id}`
      );
      let failed = true;
      fetchStub = stubFetch({
        trackerType: 'gitlab',
        pullRequestHandler: () =>
          failed
            ? new Response(JSON.stringify({ detail: scenario.detail }), {
                status: scenario.status,
              })
            : null,
      });
      const el = await mountView();
      await (el as any)._loadPullRequests(true);
      await el.updateComplete;
      const panel = el.shadowRoot!.querySelector(
        'sl-tab-panel[name="pull-requests"]'
      )!;
      expect(panel.textContent).to.contain(scenario.message);
      expect(panel.textContent).not.to.contain('0 open merge requests');
      expect(panel.textContent).not.to.contain(scenario.detail);
      expect(
        panel.querySelector('[slot="count"]')!.textContent!.trim()
      ).to.equal('Count unavailable');
      if (scenario.detail.startsWith('Configuration')) {
        expect(panel.querySelector('.prs-error')!.textContent).to.contain(
          'Edit tracker'
        );
        expect(panel.textContent).not.to.contain('Could not reach');
      }
      failed = false;
      (
        panel.querySelector('.prs-error sl-button:last-child') as HTMLElement
      ).click();
      await waitUntil(() => !panel.querySelector('.prs-error'));
      expect(panel.textContent).to.contain('0 open merge requests');
    });
  }

  it('keeps rows when Load more fails', async () => {
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
      prHasMore: true,
      pullRequestHandler: (url) => {
        if (url.includes('page=2')) {
          return new Response(
            JSON.stringify({ detail: 'Tracker request failed' }),
            {
              status: 502,
            }
          );
        }
        return null;
      },
    });
    const el = await mountView();
    await (
      el as unknown as { _loadPullRequests: (reset: boolean) => Promise<void> }
    )._loadPullRequests(true);
    await el.updateComplete;
    expect(el.shadowRoot?.textContent).to.contain('Add login');
    (
      el as unknown as { _loadMorePullRequests: () => void }
    )._loadMorePullRequests();
    await tick(50);
    await el.updateComplete;
    expect(el.shadowRoot?.textContent).to.contain('Add login');
    expect(el.shadowRoot?.textContent).to.contain('GitHub request failed.');
    expect(el.shadowRoot?.querySelector('table.styled-table')).to.exist;
  });

  for (const [status, body, message] of [
    [
      400,
      JSON.stringify({
        detail: 'GitLab rejected the access token. Edit the tracker.',
      }),
      'GitLab rejected the access token. Edit the tracker.',
    ],
    [
      403,
      JSON.stringify({
        detail: "Check the token's api scope and project access.",
      }),
      "Check the token's api scope and project access.",
    ],
    [502, '<html>Bad gateway</html>', 'GitLab request failed.'],
    [
      422,
      JSON.stringify({ detail: [{ msg: 'validation error' }] }),
      'Could not load merge requests from GitLab',
    ],
    [
      400,
      JSON.stringify({ detail: 'Invalid <img src=x onerror=alert(1)> token' }),
      'Invalid <img src=x onerror=alert(1)> token',
    ],
  ] as const) {
    it(`shows a safe MR error and recovers on retry (${status}: ${message})`, async () => {
      window.history.replaceState(
        {},
        '',
        `/console/trackers/${trackerId}?tab=pull-requests&project=${projectA.id}`
      );
      let failing = true;
      fetchStub = stubFetch({
        trackerType: 'gitlab',
        pullRequestHandler: () =>
          failing ? new Response(body, { status }) : null,
      });
      const el = await mountView();
      await (
        el as unknown as {
          _loadPullRequests: (reset: boolean) => Promise<void>;
        }
      )._loadPullRequests(true);
      await el.updateComplete;
      const error = el.shadowRoot?.querySelector('.prs-error');
      expect(error?.textContent).to.contain(message);
      expect(error?.querySelector('img')).to.equal(null);
      expect(
        el.shadowRoot
          ?.querySelector('sl-tab-panel[name="pull-requests"] [slot="count"]')
          ?.textContent?.trim()
      ).to.equal('Count unavailable');
      if (status === 400 || status === 403) {
        expect(error?.textContent).to.contain('Edit tracker');
      }
      failing = false;
      const retry = Array.from(error?.querySelectorAll('sl-button') || []).find(
        (button) => button.textContent?.trim() === 'Retry'
      ) as HTMLElement;
      retry.click();
      await waitUntil(() => !el.shadowRoot?.querySelector('.prs-error'));
      expect(
        el.shadowRoot?.querySelector(
          'sl-tab-panel[name="pull-requests"] [slot="count"]'
        )?.textContent
      ).to.contain('0 open merge requests');
    });
  }

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

  for (const trackerType of ['github', 'azure_devops']) {
    it(`Run reviewer sends pull_request target for ${trackerType}`, async () => {
      window.history.replaceState(
        {},
        '',
        `/console/trackers/${trackerId}?tab=pull-requests&project=${projectA.id}`
      );
      fetchStub = stubFetch({
        trackerType,
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
        el as unknown as {
          _loadPullRequests: (reset: boolean) => Promise<void>;
        }
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
  }

  it('Run triage on selected posts a capped targets list', async () => {
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
        {
          id: 'issue-2',
          key: 'ALP-2',
          title: 'Fix search',
          status: 'open',
          updated_at: '2026-01-03T00:00:00Z',
          project: 'Alpha',
          project_id: projectA.id,
          url: 'https://example.com/2',
        },
      ],
      total: 2,
    });
    const el = await mountView();
    await (
      el as unknown as { _loadIssues: (reset: boolean) => Promise<void> }
    )._loadIssues(true);
    await el.updateComplete;
    (
      el as unknown as {
        _toggleIssueSelection: (id: string, checked: boolean) => void;
      }
    )._toggleIssueSelection('issue-1', true);
    (
      el as unknown as {
        _toggleIssueSelection: (id: string, checked: boolean) => void;
      }
    )._toggleIssueSelection('issue-2', true);
    await el.updateComplete;
    expect(el.shadowRoot?.textContent).to.contain('Run triage on selected');
    (
      el as unknown as { _runTriageOnSelected: () => void }
    )._runTriageOnSelected();
    await tick(50);
    const runCalls = fetchStub
      .getCalls()
      .filter((call) =>
        String(call.args[0]).includes('/api/v1/flows/run-preset')
      );
    expect(runCalls.length).to.be.greaterThan(0);
    const init = runCalls[0].args[1] as RequestInit;
    const body = JSON.parse(String(init.body));
    expect(body.preset_slug).to.equal('issue-triage-assistant');
    expect(body.target).to.equal(undefined);
    expect(body.targets).to.deep.equal([
      { kind: 'issue', issue_id: 'issue-1' },
      { kind: 'issue', issue_id: 'issue-2' },
    ]);
  });
});

describe('TrackerDetailView managed Bitbucket connection (issue #1065)', () => {
  let fetchStub: sinon.SinonStub;

  const managedStatus = {
    tracker_id: trackerId,
    name: 'Bitbucket Cloud',
    provider: 'bitbucket',
    managed: true,
    state: 'connected',
    consumer_configured: true,
    workspace: 'ws',
    repository: 'repo',
    actor: { uuid: '{u}', display_name: 'Jane Doe', nickname: 'jane' },
    expires_at: '2026-10-04T13:00:00+00:00',
    rotation_version: 3,
    grant_status: 'active',
    granted_scopes: ['pullrequest:write'],
    capabilities: {
      read_repositories: true,
      push: true,
      register_webhooks: false,
    },
    capabilities_verified: false,
    reconnect_reason: null,
  };

  function stubManaged(opts: {
    flag: boolean;
    authType?: string;
    status?: Record<string, unknown>;
    statusCode?: number;
    isValid?: boolean;
  }) {
    const {
      flag,
      authType = 'managed_oauth',
      status = managedStatus,
      statusCode = 200,
      isValid = true,
    } = opts;
    const calls: string[] = [];
    const stub = sinon
      .stub(window, 'fetch')
      .callsFake(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        calls.push(`${(init?.method || 'GET').toUpperCase()} ${url}`);
        const json = (data: unknown, code = 200) =>
          new Response(JSON.stringify(data), { status: code });
        if (url.includes('/api/v1/features')) {
          return json({
            plugins: [],
            features: flag ? { bitbucket_cloud_oauth: true } : {},
          });
        }
        if (url.includes(`/auth/bitbucket/trackers/${trackerId}/status`)) {
          return json(status, statusCode);
        }
        if (url.includes(`/auth/bitbucket/trackers/${trackerId}/reconnect`)) {
          return json({
            authorization_url:
              'https://bitbucket.org/site/oauth2/authorize?r=1',
            transaction_id: 't',
            expires_at: '2026-10-04T12:10:00+00:00',
            tracker_id: trackerId,
          });
        }
        if (url.includes(`/auth/bitbucket/trackers/${trackerId}/disconnect`)) {
          return new Response(null, { status: 204 });
        }
        if (
          url.includes(`/api/v1/trackers/${trackerId}`) &&
          !url.includes('/sync')
        ) {
          return json({
            id: trackerId,
            name: 'Bitbucket Cloud',
            tracker_type: 'bitbucket',
            auth_type: authType,
            created: '2026-01-01T00:00:00Z',
            last_updated: '2026-01-02T00:00:00Z',
            is_valid: isValid,
            token_expires_at:
              authType === 'managed_oauth' ? null : '2020-01-01',
            token_expiry_status:
              authType === 'managed_oauth' ? null : 'expired',
          });
        }
        if (url.includes('/api/v1/organizations')) {
          return json({ items: [] });
        }
        if (url.includes('/api/v1/projects')) {
          return json([]);
        }
        return json({});
      });
    return { stub, calls };
  }

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    invalidateApiCaches();
    window.history.replaceState({}, '', `/console/trackers/${trackerId}`);
  });

  afterEach(() => {
    fetchStub?.restore();
    invalidateApiCaches();
    localStorage.clear();
    sessionStorage.clear();
    window.history.replaceState({}, '', '/');
  });

  it('explains first sync without claiming it is running', async () => {
    fetchStub = stubManaged({ flag: true, isValid: false }).stub;
    const el = await mountView();
    await waitUntil(() => el.shadowRoot?.textContent?.includes('Authorized'));
    expect(
      el.shadowRoot!.querySelector('.tracker-header')!.textContent
    ).to.contain('Not synced yet');
    expect(
      el.shadowRoot!.querySelector('.initial-sync-help')!.textContent
    ).to.contain('Authorization succeeded');
  });

  it('shows state, actor, server expiry and unknown-aware capabilities', async () => {
    const { stub, calls } = stubManaged({ flag: true });
    fetchStub = stub;
    const el = await mountView();
    const root = el.shadowRoot!;
    const chip = root.querySelector('.managed-connection');
    expect(chip).to.exist;
    expect(chip?.getAttribute('data-state')).to.equal('connected');
    expect(chip?.textContent).to.contain('Authorized');
    // The manual expiry chip never applies to a managed grant.
    expect(root.querySelector('.token-expiry')).to.not.exist;
    const panel = root.querySelector('.managed-connection-panel');
    expect(panel).to.exist;
    expect(panel?.querySelector('.managed-actor')?.textContent).to.contain(
      'Jane Doe'
    );
    expect(panel?.querySelector('.managed-expiry')?.textContent).to.not.contain(
      'none'
    );
    expect(
      panel
        ?.querySelector('[data-capability="push"]')
        ?.getAttribute('data-value')
    ).to.equal('granted');
    expect(
      panel
        ?.querySelector('[data-capability="register_webhooks"]')
        ?.getAttribute('data-value')
    ).to.equal('missing');
    expect(panel?.textContent?.replace(/\s+/g, ' ')).to.contain('not tested');
    expect(panel?.querySelector('.managed-reconnect')).to.exist;
    expect(panel?.querySelector('.managed-disconnect')).to.exist;
    expect(calls.some((c) => c.includes('/auth/bitbucket/trackers/'))).to.equal(
      true
    );
    expect(root.textContent).to.not.match(/token-[a-z]/);
  });

  it('reports unknown capabilities and reconnect reason', async () => {
    const { stub } = stubManaged({
      flag: true,
      status: {
        ...managedStatus,
        state: 'reconnect_required',
        expires_at: null,
        reconnect_reason: 'invalid_grant',
        capabilities: { push: null },
      },
    });
    fetchStub = stub;
    const el = await mountView();
    const root = el.shadowRoot!;
    expect(
      root.querySelector('.managed-connection')?.getAttribute('data-state')
    ).to.equal('reconnect_required');
    expect(
      root.querySelector('[data-capability="push"]')?.getAttribute('data-value')
    ).to.equal('unknown');
    expect(root.querySelector('.managed-reason')?.textContent).to.contain(
      'invalid_grant'
    );
    expect(root.querySelector('.managed-expiry')?.textContent).to.contain(
      'none'
    );
  });

  it('reconnect redirects to the provider and disconnect calls the endpoint', async () => {
    const { stub, calls } = stubManaged({ flag: true });
    fetchStub = stub;
    const el = await mountView();
    const navigate = sinon.stub(el as any, '_navigate');
    (el.shadowRoot?.querySelector('.managed-reconnect') as HTMLElement).click();
    await tick(50);
    expect(navigate).to.have.been.calledOnceWith(
      'https://bitbucket.org/site/oauth2/authorize?r=1'
    );
    expect(
      calls.some(
        (c) =>
          c.startsWith(`POST `) &&
          c.includes(`/trackers/${trackerId}/reconnect`)
      )
    ).to.equal(true);

    // Disconnect asks for confirmation first; nothing is posted before it.
    (
      el.shadowRoot?.querySelector('.managed-disconnect') as HTMLElement
    ).click();
    await waitUntil(
      () => !!document.querySelector('confirm-dialog'),
      'no confirm dialog'
    );
    const dialog = document.querySelector('confirm-dialog')!;
    await (dialog as unknown as { updateComplete: Promise<unknown> })
      .updateComplete;
    expect(dialog.shadowRoot!.textContent).to.contain('Disconnect');
    expect(
      calls.some((c) => c.includes(`/trackers/${trackerId}/disconnect`))
    ).to.equal(false);
    const confirm = dialog.shadowRoot!.querySelector<HTMLElement>(
      '[data-testid="confirm-dialog-confirm"]'
    )!;
    confirm.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    await waitUntil(
      () => calls.some((c) => c.includes(`/trackers/${trackerId}/disconnect`)),
      'disconnect was not requested'
    );
    expect(
      calls.filter((c) => c.includes(`/trackers/${trackerId}/status`)).length
    ).to.be.greaterThan(1);
    resetConfirmDialogForTests();
  });

  it('uses disconnected grant status instead of stale tracker validation in the header', async () => {
    const status = {
      ...managedStatus,
      state: 'disconnected',
      expires_at: null,
    };
    fetchStub = stubManaged({ flag: true, status }).stub;
    const el = await mountView();
    await waitUntil(() =>
      el.shadowRoot?.querySelector(
        '.managed-connection[data-state="disconnected"]'
      )
    );
    const root = el.shadowRoot!;
    expect(root.querySelector('.tracker-header')?.textContent).to.contain(
      'Disconnected'
    );
    expect(root.querySelector('.tracker-header')?.textContent).to.not.contain(
      'Connected'
    );
    expect(
      root.querySelector('.managed-connection-panel')?.textContent
    ).to.not.contain('Discovery succeeded');
    expect(
      root.querySelector('.managed-connection-panel')?.textContent
    ).to.contain('Reconnect before using');
  });

  it('flag off: managed tracker is marked unavailable, no status call, no manual chip', async () => {
    const { stub, calls } = stubManaged({ flag: false });
    fetchStub = stub;
    const el = await mountView();
    const root = el.shadowRoot!;
    const chip = root.querySelector('.managed-connection');
    expect(chip?.getAttribute('data-state')).to.equal('unavailable');
    expect(root.querySelector('.managed-connection-panel')).to.exist;
    expect(root.querySelector('.managed-reconnect')).to.not.exist;
    expect(root.querySelector('.token-expiry')).to.not.exist;
    expect(calls.some((c) => c.includes('/auth/bitbucket/'))).to.equal(false);
  });

  it('pasted-token trackers keep the manual expiry chip and no managed UI', async () => {
    const { stub, calls } = stubManaged({
      flag: true,
      authType: 'oauth_token',
    });
    fetchStub = stub;
    const el = await mountView();
    const root = el.shadowRoot!;
    expect(root.querySelector('.managed-connection')).to.not.exist;
    expect(root.querySelector('.managed-connection-panel')).to.not.exist;
    expect(root.querySelector('.token-expiry')?.textContent).to.contain(
      'Token expired'
    );
    expect(calls.some((c) => c.includes('/auth/bitbucket/'))).to.equal(false);
  });
});

describe('TrackerDetailView managed Jira connection (issue #1579)', () => {
  let fetchStub: sinon.SinonStub;

  function stubJira(flag: boolean, state = 'connected', isValid = true) {
    const calls: string[] = [];
    const stub = sinon
      .stub(window, 'fetch')
      .callsFake(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        calls.push(`${(init?.method || 'GET').toUpperCase()} ${url}`);
        const json = (data: unknown, code = 200) =>
          new Response(JSON.stringify(data), { status: code });
        if (url.includes('/api/v1/features')) {
          return json({
            plugins: [],
            features: flag ? { jira_cloud_oauth: true } : {},
          });
        }
        if (url.includes(`/auth/jira/trackers/${trackerId}/status`)) {
          return json({
            tracker_id: trackerId,
            name: 'Jira Cloud',
            provider: 'jira',
            managed: true,
            state,
            consumer_configured: true,
            cloud_id: 'cloud-1',
            site_url: 'https://example-site.atlassian.net',
            site_name: 'example-site',
            actor: { account_id: 'a', name: 'Jane Doe' },
            expires_at: '2026-10-12T13:00:00+00:00',
            reconnect_reason: null,
          });
        }
        if (
          url.includes(`/api/v1/trackers/${trackerId}`) &&
          !url.includes('/sync')
        ) {
          return json({
            id: trackerId,
            name: 'Jira Cloud',
            tracker_type: 'jira',
            auth_type: 'managed_oauth',
            created: '2026-01-01T00:00:00Z',
            last_updated: '2026-01-02T00:00:00Z',
            is_valid: isValid,
            token_expires_at: null,
            token_expiry_status: null,
          });
        }
        if (url.includes('/api/v1/organizations')) {
          return json({ items: [] });
        }
        if (url.includes('/api/v1/projects')) {
          return json([]);
        }
        return json({});
      });
    return { stub, calls };
  }

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    invalidateApiCaches();
    window.history.replaceState({}, '', `/console/trackers/${trackerId}`);
  });

  afterEach(() => {
    fetchStub?.restore();
    invalidateApiCaches();
    localStorage.clear();
    window.history.replaceState({}, '', '/');
  });

  it('shows actor, site, server expiry and reconnect without token fields', async () => {
    const { stub, calls } = stubJira(true);
    fetchStub = stub;
    const el = await mountView();
    await waitUntil(
      () => el.shadowRoot?.querySelector('.jira-managed-connection'),
      'jira panel not rendered'
    );
    const panel = el.shadowRoot!.querySelector('.jira-managed-connection')!;
    expect(panel.querySelector('.managed-actor')?.textContent).to.contain(
      'Jane Doe'
    );
    expect(panel.querySelector('.jira-site')?.textContent).to.contain(
      'example-site'
    );
    expect(panel.querySelector('.managed-reconnect')).to.exist;
    expect(el.shadowRoot!.querySelector('.token-expiry')).to.not.exist;
    expect(calls.some((c) => c.includes('/auth/jira/trackers/'))).to.equal(
      true
    );
  });

  it('shows Jira first sync separately from successful authorization', async () => {
    fetchStub = stubJira(true, 'connected', false).stub;
    const el = await mountView();
    await waitUntil(() => el.shadowRoot?.textContent?.includes('Authorized'));
    const header = el.shadowRoot!.querySelector('.tracker-header')!;
    expect(header.textContent).to.contain('Not synced yet');
    expect(header.textContent).to.contain('Authorized');
  });

  it('shows a disconnected grant in the header even if tracker validation is stale', async () => {
    fetchStub = stubJira(true, 'disconnected').stub;
    const el = await mountView();
    await waitUntil(() =>
      el.shadowRoot?.querySelector(
        '.managed-connection[data-state="disconnected"]'
      )
    );
    const header = el.shadowRoot!.querySelector('.tracker-header')!;
    expect(header.textContent).to.contain('Disconnected');
    expect(header.textContent).to.not.contain('Connected');
  });

  it('flag off: warns and never calls the provider', async () => {
    const { stub, calls } = stubJira(false);
    fetchStub = stub;
    const el = await mountView();
    await el.updateComplete;
    expect(el.shadowRoot!.textContent).to.contain(
      'Managed Jira connections are unavailable here'
    );
    expect(calls.some((c) => c.includes('/auth/jira/'))).to.equal(false);
  });
});

describe('TrackerDetailView scoped Jira webhook instructions', () => {
  let fetchStub: sinon.SinonStub;
  const orgId = '22222222-2222-2222-2222-222222222222';

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    window.history.replaceState({}, '', `/console/trackers/${trackerId}`);
  });

  afterEach(() => {
    fetchStub?.restore();
    localStorage.clear();
    window.history.replaceState({}, '', '/');
  });

  it('shows the manual webhook steps with the organization id', async () => {
    fetchStub = stubFetch({
      trackerType: 'jira',
      trackerExtra: {
        auth_type: 'api_token',
        url: 'https://example-site.atlassian.net',
        jira_manual_webhook: {
          organization_id: orgId,
          url: `https://preloop.example.com/api/v1/private/webhooks/jira/${orgId}`,
          secret: 'org-webhook-secret',
          events: [
            'jira:issue_created',
            'jira:issue_updated',
            'comment_created',
            'comment_updated',
          ],
        },
      },
    });
    const el = await mountView();
    const panel = el.shadowRoot!.querySelector('.jira-manual-webhook');
    expect(panel).to.exist;
    const text = panel!.textContent || '';
    expect(text).to.contain('Jira admin');
    expect(text).to.contain('System');
    expect(text).to.contain('WebHooks');
    expect(text).to.contain(orgId);
    expect(text).to.contain('org-webhook-secret');
    expect(text).to.contain('jira:issue_created');
    expect(text).to.contain('jira:issue_updated');
    expect(text).to.contain('comment_created');
    expect(text).to.contain('comment_updated');
    expect(text).to.not.contain('scoped-token');
  });

  it('shows the callback without the secret for a viewer', async () => {
    fetchStub = stubFetch({
      trackerType: 'jira',
      trackerExtra: {
        auth_type: 'api_token',
        url: 'https://example-site.atlassian.net',
        jira_manual_webhook: {
          organization_id: orgId,
          url: `https://preloop.example.com/api/v1/private/webhooks/jira/${orgId}`,
          secret: null,
          events: ['jira:issue_created', 'comment_created'],
        },
      },
    });
    const el = await mountView();
    const panel = el.shadowRoot!.querySelector('.jira-manual-webhook');
    expect(panel).to.exist;
    const text = panel!.textContent || '';
    expect(text).to.contain(orgId);
    expect(text).to.contain('jira:issue_created');
    expect(text).to.contain('manage trackers');
    expect(panel!.querySelector('.jira-manual-webhook-secret code')).to.equal(
      null
    );
  });

  it('hides the manual steps when the token can register webhooks', async () => {
    fetchStub = stubFetch({ trackerType: 'jira' });
    const el = await mountView();
    expect(el.shadowRoot!.querySelector('.jira-manual-webhook')).to.equal(null);
  });
});
