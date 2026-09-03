import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import '../../components/view-header.ts';
import './flows-view';
import type { FlowsView } from './flows-view';
import {
  filterFlowRows,
  flowStatusOf,
  flowTriggerSummary,
  loadInitialFlowsViewMode,
  sortFlowListRows,
  type FlowListRow,
} from './flows-view';
import { invalidateApiCaches } from '../../api';
import { loadShoelaceTokens } from '../../utils/test-shoelace-theme';

describe('FlowsView', () => {
  let fetchStub: sinon.SinonStub;

  function createFetchStub(flows: unknown[] = [], presets: unknown[] = []) {
    return sinon
      .stub(window, 'fetch')
      .callsFake(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();
        const method = (init?.method || 'GET').toUpperCase();

        const json = (data: unknown) =>
          new Response(JSON.stringify(data), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });

        if (
          url.includes('/api/v1/flows') &&
          !url.includes('presets') &&
          !url.includes('executions') &&
          method === 'GET'
        ) {
          return json(flows);
        }
        if (url.includes('/api/v1/flows/presets') && method === 'GET') {
          return json(presets);
        }
        if (url.includes('/api/v1/flows/executions') && method === 'GET') {
          return json([]);
        }

        return json({ detail: `Unhandled: ${method} ${url}` });
      });
  }

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
  });

  afterEach(() => {
    fetchStub?.restore();
    localStorage.clear();
    invalidateApiCaches();
  });

  it('renders the flow list view', async () => {
    fetchStub = createFetchStub([], []);
    const element = (await fixture(
      html`<flows-view></flows-view>`
    )) as FlowsView;

    await waitUntil(
      () => !(element as any).isLoading,
      'Flows view did not finish loading'
    );
    await element.updateComplete;

    const header = element.shadowRoot?.querySelector('view-header');
    expect(header).to.exist;
    expect(header?.getAttribute('headerText')).to.equal('Flows');

    // The section description renders via the shared view-header prop, not an
    // inline banner (keeps Flows consistent with the other console views).
    expect(header?.getAttribute('description')).to.contain(
      'Event-driven agent runs.'
    );
    expect(element.shadowRoot?.querySelector('.proxy-notice')).to.not.exist;
  });

  it('shows empty state when no flows', async () => {
    fetchStub = createFetchStub([], []);
    const element = (await fixture(
      html`<flows-view></flows-view>`
    )) as FlowsView;

    await waitUntil(
      () => !(element as any).isLoading,
      'Flows view did not finish loading'
    );
    await element.updateComplete;

    const emptyState = element.shadowRoot?.querySelector('.empty-state');
    expect(emptyState).to.exist;
    expect(emptyState?.textContent).to.include('No flows yet');
  });

  it('shows flow cards when flows exist', async () => {
    const mockFlows = [
      { id: 'flow-1', name: 'Test Flow', description: 'A test flow' },
    ];
    // Cards are no longer the default view, so this asks for them explicitly.
    localStorage.setItem('preloop.flows.view_mode', 'cards');
    fetchStub = createFetchStub(mockFlows, []);
    const element = (await fixture(
      html`<flows-view></flows-view>`
    )) as FlowsView;

    await waitUntil(
      () => (element as any).flows?.length === 1,
      'Flows did not load'
    );
    await element.updateComplete;

    const flowsGrid = element.shadowRoot?.querySelector('.flows-grid');
    expect(flowsGrid).to.exist;
    const flowCards = element.shadowRoot?.querySelectorAll('.flow-card');
    expect(flowCards?.length).to.equal(1);
  });

  it('does not fetch presets on initial load when flows already exist', async () => {
    const mockFlows = [
      { id: 'flow-1', name: 'Test Flow', description: 'A test flow' },
    ];
    fetchStub = createFetchStub(mockFlows, [
      { id: 'preset-1', name: 'Preset' },
    ]);
    const element = (await fixture(
      html`<flows-view></flows-view>`
    )) as FlowsView;

    await waitUntil(
      () => !(element as any).isLoading,
      'Flows view did not finish loading'
    );
    await element.updateComplete;

    const urls = fetchStub.getCalls().map((c) => String(c.args[0]));
    expect(urls.some((u) => u.includes('/api/v1/flows/presets'))).to.be.false;
  });

  it('stubs fetch for flows API', async () => {
    fetchStub = createFetchStub([], []);
    const element = (await fixture(
      html`<flows-view></flows-view>`
    )) as FlowsView;

    await waitUntil(
      () => !(element as any).isLoading,
      'Flows view did not finish loading'
    );

    expect(fetchStub).to.have.been.called;
    const urls = fetchStub.getCalls().map((c) => String(c.args[0]));
    expect(urls.some((u) => u.includes('/api/v1/flows'))).to.be.true;
  });

  describe('schedule indicators', () => {
    async function renderFlows(flows: unknown[]) {
      // The next-run line lives on the card; the table says it in the
      // Trigger cell's title, where there is no room for a second line.
      localStorage.setItem('preloop.flows.view_mode', 'cards');
      fetchStub = createFetchStub(flows, []);
      const element = (await fixture(
        html`<flows-view></flows-view>`
      )) as FlowsView;
      await waitUntil(
        () => !(element as any).isLoading,
        'Flows view did not finish loading'
      );
      await element.updateComplete;
      return element;
    }

    it('shows the next run time for scheduled flows', async () => {
      const element = await renderFlows([
        {
          id: 'flow-sched',
          name: 'Nightly Report',
          trigger_event_source: 'schedule',
          is_enabled: true,
          schedule_state: {
            active: true,
            type: 'daily',
            description: 'Daily at 09:00 (Europe/Athens)',
            timezone: 'Europe/Athens',
            next_run_at: '2026-08-17T06:00:00+00:00',
          },
        },
      ]);

      const card = element.shadowRoot?.querySelector('.flow-card');
      expect(card?.textContent).to.contain('Next run');
      expect(card?.textContent).to.not.contain('Schedule paused');
    });

    it('shows a paused badge when a scheduled flow is disabled', async () => {
      const element = await renderFlows([
        {
          id: 'flow-paused',
          name: 'Paused Report',
          trigger_event_source: 'schedule',
          is_enabled: false,
          schedule_state: {
            active: false,
            type: 'daily',
            description: 'Daily at 09:00 (Europe/Athens)',
            timezone: 'Europe/Athens',
            next_run_at: null,
          },
        },
      ]);

      const card = element.shadowRoot?.querySelector('.flow-card');
      expect(card?.textContent).to.contain('Schedule paused');
      expect(card?.textContent).to.not.contain('Next run');
    });

    it('shows no schedule indicator for non-scheduled flows', async () => {
      const element = await renderFlows([
        {
          id: 'flow-hook',
          name: 'Webhook Flow',
          trigger_event_source: 'webhook',
        },
      ]);

      const card = element.shadowRoot?.querySelector('.flow-card');
      expect(card?.textContent).to.not.contain('Next run');
      expect(card?.textContent).to.not.contain('Schedule paused');
    });
  });

  describe('recent execution duration', () => {
    async function renderItem(exec: Record<string, unknown>) {
      fetchStub = createFetchStub([], []);
      const element = (await fixture(
        html`<flows-view></flows-view>`
      )) as FlowsView;
      await waitUntil(
        () => !(element as any).isLoading,
        'Flows view did not finish loading'
      );

      const item = await fixture(
        (element as any).renderExecutionItem(exec) as any
      );
      return (item.textContent || '').replace(/\s+/g, ' ').trim();
    }

    it('appends the duration to the started timestamp when the run finished', async () => {
      const text = await renderItem({
        id: 'exec-done',
        flow_id: 'flow-1',
        status: 'SUCCEEDED',
        start_time: '2026-03-09T10:00:00Z',
        end_time: '2026-03-09T10:04:32Z',
      });

      expect(text).to.contain('Started');
      expect(text).to.contain('· 4m 32s');
    });

    it('shows the live elapsed time for a running execution', async () => {
      const text = await renderItem({
        id: 'exec-running',
        flow_id: 'flow-1',
        status: 'RUNNING',
        start_time: new Date(Date.now() - 65_000).toISOString(),
      });

      expect(text).to.match(/Started .*· Running · \d+m \d+s/);
    });

    it('appends nothing for a legacy terminal execution without an end time', async () => {
      const text = await renderItem({
        id: 'exec-legacy',
        flow_id: 'flow-1',
        status: 'FAILED',
        start_time: '2026-03-09T10:00:00Z',
      });

      expect(text).to.contain('Started');
      expect(text).to.not.contain('·');
    });
  });

  describe('filter bar, view switcher and list', () => {
    const minutesAgo = (minutes: number) =>
      new Date(Date.now() - minutes * 60_000).toISOString();

    const FLOWS = [
      {
        id: 'flow-review',
        name: 'Pull Request Reviewer',
        description: 'Reviews pull requests',
        icon: 'diagram-3',
        is_enabled: true,
        source_preset_id: 'preset-review',
        trigger_event_source: 'webhook',
        trigger_event_types: ['pull_request_updated'],
      },
      {
        id: 'flow-nightly',
        name: 'Nightly Report',
        description: 'Sends the nightly digest',
        is_enabled: false,
        trigger_event_source: 'schedule',
        schedule_state: {
          active: false,
          type: 'daily',
          description: 'Daily at 09:00 (Europe/Athens)',
          timezone: 'Europe/Athens',
          next_run_at: null,
        },
      },
      {
        id: 'flow-triage',
        name: 'Issue Triage',
        description: 'Triages new issues',
        is_enabled: true,
        trigger_event_source: 'webhook',
        trigger_event_types: ['issue_created'],
      },
    ];

    const EXECUTIONS = [
      {
        id: 'exec-newest',
        flow_id: 'flow-review',
        status: 'SUCCEEDED',
        start_time: minutesAgo(10),
        end_time: minutesAgo(9),
        trigger_subject: 'PR #12 Fix the parser',
      },
      {
        id: 'exec-failed',
        flow_id: 'flow-triage',
        status: 'FAILED',
        start_time: minutesAgo(120),
        end_time: minutesAgo(119),
      },
      {
        id: 'exec-old',
        flow_id: 'flow-triage',
        status: 'SUCCEEDED',
        start_time: minutesAgo(300),
        end_time: minutesAgo(299),
      },
    ];

    function stubFlowsApi() {
      return sinon
        .stub(window, 'fetch')
        .callsFake(async (input: RequestInfo | URL, init?: RequestInit) => {
          const url = typeof input === 'string' ? input : input.toString();
          const method = (init?.method || 'GET').toUpperCase();
          const json = (data: unknown) =>
            new Response(JSON.stringify(data), {
              status: 200,
              headers: { 'Content-Type': 'application/json' },
            });

          if (url.includes('/api/v1/flows/presets')) {
            return json([{ id: 'preset-review', name: 'PR Review' }]);
          }
          if (url.includes('/api/v1/flows/executions')) {
            // The second call asks for the runs in flight; nothing is running.
            return json(url.includes('status=') ? [] : EXECUTIONS);
          }
          if (url.includes('/api/v1/flows') && method === 'GET') {
            return json(FLOWS);
          }
          if (url.includes('/api/v1/account/gateway-usage/summary')) {
            return json({
              usage_by_flow: [
                { flow_id: 'flow-review', estimated_cost: 1.2345 },
                { flow_id: 'flow-triage', estimated_cost: 0.5 },
              ],
            });
          }
          if (url.includes('/api/v1/trackers')) {
            return json([]);
          }
          return json({ detail: `Unhandled: ${method} ${url}` });
        });
    }

    async function renderFlows(): Promise<FlowsView> {
      fetchStub = stubFlowsApi();
      const element = (await fixture(
        html`<flows-view></flows-view>`
      )) as FlowsView;
      await waitUntil(
        () => !(element as any).isLoading,
        'Flows view did not finish loading'
      );
      await element.updateComplete;
      return element;
    }

    const rowNames = (element: FlowsView) =>
      [
        ...(element.shadowRoot?.querySelectorAll(
          'tbody .flow-cell .row-link'
        ) || []),
      ].map((link) => (link.textContent || '').trim());

    it('lists the flows in a table by default and counts them', async () => {
      const element = await renderFlows();

      expect(element.shadowRoot?.querySelector('table.flows-table')).to.exist;
      expect(element.shadowRoot?.querySelector('.flows-grid')).to.not.exist;
      expect(rowNames(element)).to.have.lengthOf(3);
      expect(
        element.shadowRoot?.querySelector('.results-count')?.textContent?.trim()
      ).to.equal('3 flows');
    });

    it('filters by the search box and says how many of how many are left', async () => {
      const element = await renderFlows();
      const search = element.shadowRoot?.querySelector(
        '.search-input'
      ) as HTMLInputElement & { value: string };
      search.value = 'nightly';
      search.dispatchEvent(new CustomEvent('sl-input', { bubbles: true }));
      await element.updateComplete;

      expect(rowNames(element)).to.deep.equal(['Nightly Report']);
      expect(
        element.shadowRoot?.querySelector('.results-count')?.textContent?.trim()
      ).to.equal('1 of 3 flows');
    });

    it('filters by status', async () => {
      const element = await renderFlows();
      const status = element.shadowRoot?.querySelector(
        '.status-filter'
      ) as HTMLElement & { value: string[] };
      status.value = ['paused'];
      status.dispatchEvent(new CustomEvent('sl-change', { bubbles: true }));
      await element.updateComplete;

      expect(rowNames(element)).to.deep.equal(['Nightly Report']);
    });

    it('filters by the preset a flow was cloned from', async () => {
      const element = await renderFlows();
      await waitUntil(
        () =>
          element.rows.some(
            (row: FlowListRow) => row.presetLabel === 'PR Review'
          ),
        'Preset names did not load'
      );
      const kind = element.shadowRoot?.querySelector(
        '.preset-filter'
      ) as HTMLElement & { value: string[] };
      kind.value = ['preset-review'];
      kind.dispatchEvent(new CustomEvent('sl-change', { bubbles: true }));
      await element.updateComplete;

      expect(rowNames(element)).to.deep.equal(['Pull Request Reviewer']);
    });

    it('says so when the filters match nothing', async () => {
      const element = await renderFlows();
      const search = element.shadowRoot?.querySelector(
        '.search-input'
      ) as HTMLInputElement & { value: string };
      search.value = 'no such flow';
      search.dispatchEvent(new CustomEvent('sl-input', { bubbles: true }));
      await element.updateComplete;

      expect(
        element.shadowRoot?.querySelector('.empty-state')?.textContent
      ).to.contain('No flows match these filters.');
    });

    it('switches to cards and remembers the choice for the next visit', async () => {
      const element = await renderFlows();
      const cardsButton = element.shadowRoot?.querySelector(
        'sl-button[data-view="cards"]'
      ) as HTMLElement;
      cardsButton.click();
      await element.updateComplete;

      expect(element.shadowRoot?.querySelector('.flows-grid')).to.exist;
      expect(element.shadowRoot?.querySelector('table.flows-table')).to.not
        .exist;
      expect(localStorage.getItem('preloop.flows.view_mode')).to.equal('cards');

      fetchStub.restore();
      const revisited = await renderFlows();
      expect(revisited.shadowRoot?.querySelector('.flows-grid')).to.exist;
    });

    it('renders cards on a phone even though list is the stored choice', async () => {
      localStorage.setItem('preloop.flows.view_mode', 'list');
      const matchMedia = sinon.stub(window, 'matchMedia').callsFake(
        (query: string) =>
          ({
            matches: query.includes('max-width: 640px'),
            media: query,
            addEventListener: () => {},
            removeEventListener: () => {},
            addListener: () => {},
            removeListener: () => {},
            onchange: null,
            dispatchEvent: () => false,
          }) as unknown as MediaQueryList
      );
      try {
        const element = await renderFlows();
        expect(element.shadowRoot?.querySelector('.flows-grid')).to.exist;
        expect(element.shadowRoot?.querySelector('table.flows-table')).to.not
          .exist;
        // The phone borrowed the view; it did not overwrite the preference.
        expect(localStorage.getItem('preloop.flows.view_mode')).to.equal(
          'list'
        );
      } finally {
        matchMedia.restore();
      }
    });

    it('sorts by last run first and marks the sorted header', async () => {
      const element = await renderFlows();

      const headers = [
        ...(element.shadowRoot?.querySelectorAll('thead th') || []),
      ];
      const lastRun = headers.find((th) =>
        (th.textContent || '').includes('Last run')
      );
      expect(lastRun?.getAttribute('aria-sort')).to.equal('descending');
      // Newest run first, and the flow that never ran last.
      expect(rowNames(element)).to.deep.equal([
        'Pull Request Reviewer',
        'Issue Triage',
        'Nightly Report',
      ]);
    });

    it('sorts by name when the Flow header is clicked, and flips on a second click', async () => {
      const element = await renderFlows();
      const flowSort = element.shadowRoot?.querySelector(
        'button[data-sort-key="flow"]'
      ) as HTMLElement;

      flowSort.click();
      await element.updateComplete;
      expect(rowNames(element)).to.deep.equal([
        'Issue Triage',
        'Nightly Report',
        'Pull Request Reviewer',
      ]);
      expect(flowSort.closest('th')?.getAttribute('aria-sort')).to.equal(
        'ascending'
      );

      flowSort.click();
      await element.updateComplete;
      expect(rowNames(element)).to.deep.equal([
        'Pull Request Reviewer',
        'Nightly Report',
        'Issue Triage',
      ]);
      expect(flowSort.closest('th')?.getAttribute('aria-sort')).to.equal(
        'descending'
      );
    });

    it('gives every row a real link to its flow', async () => {
      const element = await renderFlows();
      const links = [
        ...(element.shadowRoot?.querySelectorAll<HTMLAnchorElement>(
          'tbody .flow-cell .row-link'
        ) || []),
      ];
      expect(links.map((link) => link.getAttribute('href'))).to.deep.equal([
        '/console/flows/flow-review',
        '/console/flows/flow-triage',
        '/console/flows/flow-nightly',
      ]);
    });

    it('shows the last run, the counts in range and the estimated spend', async () => {
      const element = await renderFlows();
      await waitUntil(
        () => element.rows.some((row: FlowListRow) => row.cost > 0),
        'Costs did not load'
      );
      await element.updateComplete;

      const firstRow = element.shadowRoot?.querySelector('tbody tr.flow-row');
      const text = (firstRow?.textContent || '').replace(/\s+/g, ' ');
      expect(text).to.contain('Succeeded');
      expect(text).to.contain('PR #12');
      // Above a cent, two decimals (DESIGN.md, "Money").
      expect(text).to.contain('$1.23');

      const triage = element.rows.find(
        (row: FlowListRow) => row.id === 'flow-triage'
      );
      expect(triage?.runs).to.equal(2);
      expect(triage?.failed).to.equal(1);
    });

    it('offers open, run, edit, pause and a separated danger delete in the kebab', async () => {
      const element = await renderFlows();
      const actions = element.shadowRoot?.querySelector(
        'tbody tr.flow-row resource-actions'
      ) as HTMLElement & { updateComplete: Promise<unknown> };
      await (actions as any).updateComplete;
      const items = [
        ...(actions.shadowRoot?.querySelectorAll('sl-menu-item') || []),
      ];
      const labels = items.map((item) => (item.textContent || '').trim());
      expect(labels).to.deep.equal([
        'Open',
        'Run now',
        'Edit',
        'Pause',
        'Delete',
      ]);
      const del = items[items.length - 1];
      expect(del.classList.contains('danger-item'), 'Delete is the danger item')
        .to.be.true;
    });

    it('offers Resume, not Pause, on a paused flow and will not run it', async () => {
      const element = await renderFlows();
      const paused = element.rows.find(
        (row: FlowListRow) => row.id === 'flow-nightly'
      )!;
      const actions = (element as any).getRowActions(paused);
      expect(actions.map((action: any) => action.label)).to.include('Resume');
      expect(actions.find((action: any) => action.id === 'run-now').disabled).to
        .be.true;
    });

    it('keeps the kebab button inside its own cell', async () => {
      await loadShoelaceTokens();
      const element = await renderFlows();
      const cell = element.shadowRoot?.querySelector<HTMLElement>(
        'table.flows-table tbody td.actions-cell'
      );
      expect(cell, 'the actions cell renders').to.exist;
      const kebab = cell
        ?.querySelector('resource-actions')
        ?.shadowRoot?.querySelector<HTMLElement>('sl-dropdown > sl-button');
      expect(kebab, 'the kebab trigger renders').to.exist;

      const cellBox = cell!.getBoundingClientRect();
      const buttonBox = kebab!.getBoundingClientRect();
      expect(buttonBox.width, 'the kebab has its real width').to.be.greaterThan(
        30
      );
      expect(
        buttonBox.left,
        'the kebab is not cut off the left edge of its cell'
      ).to.be.at.least(cellBox.left);
      expect(
        buttonBox.right,
        'the kebab is not cut off the right edge of its cell'
      ).to.be.at.most(cellBox.right);
    });
  });

  describe('helpers', () => {
    function makeRow(overrides: Partial<FlowListRow> = {}): FlowListRow {
      return {
        id: 'flow-1',
        name: 'Alpha',
        presetLabel: 'Custom flow',
        presetValue: 'custom',
        icon: 'diagram-3',
        description: '',
        detailUrl: '/console/flows/flow-1',
        triggerLabel: 'Webhook',
        triggerTitle: 'Webhook',
        status: 'enabled',
        statusLabel: 'Enabled',
        lastRun: null,
        runs: 0,
        failed: 0,
        cost: 0,
        source: { id: 'flow-1', name: 'Alpha' } as any,
        ...overrides,
      };
    }

    it('defaults to the list and only trusts a value it knows', () => {
      expect(loadInitialFlowsViewMode()).to.equal('list');
      localStorage.setItem('preloop.flows.view_mode', 'cards');
      expect(loadInitialFlowsViewMode()).to.equal('cards');
      localStorage.setItem('preloop.flows.view_mode', 'carousel');
      expect(loadInitialFlowsViewMode()).to.equal('list');
    });

    it('reads a flow status from what can start it', () => {
      expect(flowStatusOf({ is_enabled: false } as any)).to.equal('paused');
      expect(
        flowStatusOf({
          is_enabled: true,
          trigger_event_source: 'schedule',
          schedule_state: { active: false },
        } as any)
      ).to.equal('paused');
      expect(flowStatusOf({ is_enabled: true } as any)).to.equal('draft');
      expect(
        flowStatusOf({
          is_enabled: true,
          execution_stats: { total_execs: 3 },
        } as any)
      ).to.equal('enabled');
      expect(
        flowStatusOf({
          is_enabled: true,
          trigger_event_source: 'webhook',
        } as any)
      ).to.equal('enabled');
    });

    it('summarises a trigger on one line and never shows a raw id', () => {
      expect(
        flowTriggerSummary({
          trigger_event_source: 'webhook',
          trigger_event_types: ['pull_request_updated'],
        } as any).label
      ).to.equal('Webhook · Pull request updated');
      expect(
        flowTriggerSummary(
          {
            trigger_event_source: 'tracker-uuid',
            trigger_event_types: ['issue_created'],
          } as any,
          { 'tracker-uuid': 'Acme Jira' }
        ).label
      ).to.equal('Acme Jira · Issue created');
      expect(flowTriggerSummary({} as any).label).to.equal('Manual');
      expect(
        flowTriggerSummary({
          trigger_event_source: 'schedule',
          schedule_state: { active: true, description: 'Daily at 09:00' },
        } as any).label
      ).to.equal('Daily at 09:00');
    });

    it('sorts by a column and falls back to the name so rows never shuffle', () => {
      const rows = [
        makeRow({ id: 'b', name: 'Beta', runs: 2 }),
        makeRow({ id: 'a', name: 'Alpha', runs: 2 }),
        makeRow({ id: 'c', name: 'Gamma', runs: 9 }),
      ];
      expect(
        sortFlowListRows(rows, 'runs', 'desc').map((row) => row.name)
      ).to.deep.equal(['Gamma', 'Alpha', 'Beta']);
      expect(
        sortFlowListRows(rows, 'flow', 'asc').map((row) => row.name)
      ).to.deep.equal(['Alpha', 'Beta', 'Gamma']);
    });

    it('matches the search against everything the row shows', () => {
      const rows = [
        makeRow({ id: 'a', name: 'Alpha', presetLabel: 'PR Review' }),
        makeRow({ id: 'b', name: 'Beta', triggerLabel: 'Daily at 09:00' }),
      ];
      const names = (query: string) =>
        filterFlowRows(rows, {
          query,
          presets: [],
          statuses: [],
        }).map((row) => row.name);
      expect(names('pr rev')).to.deep.equal(['Alpha']);
      expect(names('daily')).to.deep.equal(['Beta']);
      expect(names('')).to.deep.equal(['Alpha', 'Beta']);
    });

    it('treats "has failures" as its own status filter', () => {
      const rows = [
        makeRow({ id: 'a', name: 'Alpha', failed: 2 }),
        makeRow({ id: 'b', name: 'Beta', status: 'paused' }),
      ];
      expect(
        filterFlowRows(rows, {
          query: '',
          presets: [],
          statuses: ['failing'],
        }).map((row) => row.name)
      ).to.deep.equal(['Alpha']);
    });
  });
});
