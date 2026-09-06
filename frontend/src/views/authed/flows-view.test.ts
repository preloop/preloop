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

    /**
     * `execution_stats` as the server answers it for a window: runs, failed
     * and cost measured over the same days the header names. Nightly has the
     * shape that used to print a contradiction, spend with no run in range.
     */
    const windowStats = (
      runs: number,
      failed: number,
      cost: number,
      lastRunAt: string | null = null
    ) => ({
      since: new Date(Date.now() - 30 * 24 * 3600_000).toISOString(),
      runs,
      failed,
      cost,
      last_run_at: lastRunAt,
      total_execs: runs,
      running_execs: 0,
      last_seen_at: lastRunAt,
      estimated_cost: cost,
    });

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
        execution_stats: windowStats(1, 0, 1.2345, minutesAgo(10)),
      },
      {
        id: 'flow-nightly',
        name: 'Nightly Report',
        description: 'Sends the nightly digest',
        is_enabled: false,
        execution_stats: windowStats(0, 0, 0.33),
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
        execution_stats: windowStats(2, 1, 0.5, minutesAgo(120)),
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
        element.shadowRoot
          ?.querySelector('list-toolbar [slot="count"]')
          ?.textContent?.trim()
      ).to.equal('3 flows');
    });

    it('filters by the search box and says how many of how many are left', async () => {
      const element = await renderFlows();
      const toolbar = element.shadowRoot?.querySelector('list-toolbar');
      toolbar?.dispatchEvent(
        new CustomEvent('search-change', {
          detail: { value: 'nightly' },
          bubbles: true,
          composed: true,
        })
      );
      await element.updateComplete;

      expect(rowNames(element)).to.deep.equal(['Nightly Report']);
      expect(
        element.shadowRoot
          ?.querySelector('list-toolbar [slot="count"]')
          ?.textContent?.trim()
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
      const toolbar = element.shadowRoot?.querySelector('list-toolbar');
      toolbar?.dispatchEvent(
        new CustomEvent('search-change', {
          detail: { value: 'no such flow' },
          bubbles: true,
          composed: true,
        })
      );
      await element.updateComplete;

      expect(
        element.shadowRoot?.querySelector('.empty-state')?.textContent
      ).to.contain('No flows match these filters.');
    });

    it('switches to cards and remembers the choice for the next visit', async () => {
      const element = await renderFlows();
      const toolbar = element.shadowRoot?.querySelector('list-toolbar');
      const cardsButton = toolbar?.shadowRoot?.querySelector(
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

    it('asks the server for the counts of the range it names', async () => {
      await renderFlows();
      const flowsCall = fetchStub
        .getCalls()
        .map((call) => String(call.args[0]))
        .find(
          (url) =>
            url.includes('/api/v1/flows') &&
            !url.includes('presets') &&
            !url.includes('executions')
        );
      expect(flowsCall, 'the flows request names the window').to.contain(
        'stats_since='
      );
    });

    it('states no spend beside a flow with no run in the range', async () => {
      const element = await renderFlows();
      const nightly = element.rows.find(
        (row: FlowListRow) => row.id === 'flow-nightly'
      );
      // The server reported spend against the flow but zero runs started in
      // the window. One period, one story: the cell says nothing rather than
      // printing money next to "No run in the last 30d".
      expect(nightly?.runs).to.equal(0);
      expect(nightly?.cost).to.equal(0);

      const row = [
        ...(element.shadowRoot?.querySelectorAll('tbody tr.flow-row') || []),
      ].find((tr) => (tr.textContent || '').includes('Nightly Report'));
      const text = (row?.textContent || '').replace(/\s+/g, ' ');
      expect(text).to.contain('No run in the last 30d');
      expect(text).to.not.contain('$0.33');
      expect(text).to.not.contain('$0.00');
    });

    it('states no spend when the server did not measure this window', async () => {
      // A server without `stats_since` answers lifetime stats only, so the
      // runs are counted from the executions sample and there is no figure
      // for this window. "-" says that; "$0.00" would claim the flow spent
      // nothing, which the page has no way of knowing.
      fetchStub = sinon
        .stub(window, 'fetch')
        .callsFake(async (input: RequestInfo | URL, init?: RequestInit) => {
          const url = typeof input === 'string' ? input : input.toString();
          const method = (init?.method || 'GET').toUpperCase();
          const json = (data: unknown) =>
            new Response(JSON.stringify(data), {
              status: 200,
              headers: { 'Content-Type': 'application/json' },
            });
          if (url.includes('/api/v1/flows/presets')) return json([]);
          if (url.includes('/api/v1/flows/executions')) {
            return json(url.includes('status=') ? [] : EXECUTIONS);
          }
          if (url.includes('/api/v1/flows') && method === 'GET') {
            return json([
              {
                id: 'flow-review',
                name: 'Pull Request Reviewer',
                is_enabled: true,
                trigger_event_source: 'webhook',
                execution_stats: {
                  total_execs: 9,
                  running_execs: 0,
                  estimated_cost: 4.5,
                },
              },
            ]);
          }
          if (url.includes('/api/v1/trackers')) return json([]);
          return json({ detail: `Unhandled: ${method} ${url}` });
        });
      const element = (await fixture(
        html`<flows-view></flows-view>`
      )) as FlowsView;
      await waitUntil(
        () => !(element as any).isLoading,
        'Flows view did not finish loading'
      );
      await element.updateComplete;

      const row = element.rows.find(
        (candidate: FlowListRow) => candidate.id === 'flow-review'
      );
      expect(row?.countsFromServer).to.equal(false);
      expect(row?.runs).to.be.greaterThan(0);

      const cells = [
        ...(element.shadowRoot?.querySelectorAll(
          'tbody tr.flow-row td.numeric'
        ) || []),
      ];
      const cost = cells[cells.length - 1];
      expect((cost?.textContent || '').trim()).to.equal('-');
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
        lastRunAt: null,
        runs: 0,
        failed: 0,
        cost: 0,
        countsFromServer: true,
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

  describe('wave 7 review fixes', () => {
    const FLOWS = [
      {
        id: 'flow-review',
        name: 'Pull Request Reviewer',
        is_enabled: true,
        trigger_event_source: 'webhook',
        trigger_event_types: ['pull_request_updated'],
      },
    ];

    /** A backend timestamp without a zone, which is UTC. */
    const naive = (ms: number) =>
      new Date(ms).toISOString().replace('Z', '').replace('.000', '');

    function stubApi(options: { executions: unknown[]; flowsStatus?: number }) {
      return sinon
        .stub(window, 'fetch')
        .callsFake(async (input: RequestInfo | URL, init?: RequestInit) => {
          const url = typeof input === 'string' ? input : input.toString();
          const method = (init?.method || 'GET').toUpperCase();
          const json = (data: unknown, status = 200) =>
            new Response(JSON.stringify(data), {
              status,
              headers: { 'Content-Type': 'application/json' },
            });

          if (url.includes('/api/v1/flows/presets')) return json([]);
          if (url.includes('/api/v1/flows/executions')) {
            return json(url.includes('status=') ? [] : options.executions);
          }
          if (url.includes('/api/v1/flows') && method === 'GET') {
            const status = options.flowsStatus ?? 200;
            return status === 200
              ? json(FLOWS)
              : json({ detail: 'Gateway timeout' }, status);
          }
          if (url.includes('/api/v1/account/gateway-usage/summary')) {
            return json({ usage_by_flow: [] });
          }
          if (url.includes('/api/v1/trackers')) return json([]);
          return json({ detail: `Unhandled: ${method} ${url}` });
        });
    }

    async function renderFlows(options: {
      executions: unknown[];
      flowsStatus?: number;
    }): Promise<FlowsView> {
      fetchStub = stubApi(options);
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

    it('reads a zoneless run timestamp as UTC when it ranks the last run', async () => {
      const now = Date.now();
      const element = await renderFlows({
        executions: [
          {
            id: 'exec-zoneless-older',
            flow_id: 'flow-review',
            status: 'SUCCEEDED',
            // No zone: UTC, two hours back. Read as local time this run
            // jumps by the browser offset and can outrank the newer one.
            start_time: naive(now - 2 * 60 * 60 * 1000),
          },
          {
            id: 'exec-newer',
            flow_id: 'flow-review',
            status: 'SUCCEEDED',
            start_time: new Date(now - 60 * 60 * 1000).toISOString(),
          },
        ],
      });

      const row = element.rows.find(
        (candidate: FlowListRow) => candidate.id === 'flow-review'
      );
      expect(row?.runs).to.equal(2);
      expect(row?.lastRun?.id).to.equal('exec-newer');
    });

    it('keeps a zoneless run inside the window it belongs to', async () => {
      const element = await renderFlows({
        executions: [
          {
            id: 'exec-23h',
            flow_id: 'flow-review',
            status: 'SUCCEEDED',
            start_time: naive(Date.now() - 23 * 60 * 60 * 1000),
          },
        ],
      });
      (element as unknown as { range: string }).range = 'day';
      await element.updateComplete;

      const row = element.rows.find(
        (candidate: FlowListRow) => candidate.id === 'flow-review'
      );
      expect(row?.runs, '23h back is inside 24h in any timezone').to.equal(1);
    });

    it('cuts 30d at thirty days, not at a calendar month', async () => {
      const day = 24 * 60 * 60 * 1000;
      const element = await renderFlows({
        executions: [
          {
            id: 'exec-29d',
            flow_id: 'flow-review',
            status: 'SUCCEEDED',
            start_time: new Date(Date.now() - 29 * day).toISOString(),
          },
          {
            id: 'exec-30d-and-a-half',
            flow_id: 'flow-review',
            status: 'SUCCEEDED',
            start_time: new Date(Date.now() - 30.5 * day).toISOString(),
          },
        ],
      });

      const row = element.rows.find(
        (candidate: FlowListRow) => candidate.id === 'flow-review'
      );
      // A 31 day month used to stretch "30d" to 31 days.
      expect(row?.runs).to.equal(1);
      expect(row?.lastRun?.id).to.equal('exec-29d');
    });

    it('says which window the failures filter counts', async () => {
      const element = await renderFlows({ executions: [] });
      const option = [
        ...(element.shadowRoot?.querySelectorAll('sl-option') || []),
      ].find((candidate) => candidate.getAttribute('value') === 'failing');

      expect((option?.textContent || '').replace(/\s+/g, ' ').trim()).to.equal(
        'Failed in the last 30d'
      );
    });

    it('says the flows fetch failed instead of showing an empty account', async () => {
      const element = await renderFlows({
        executions: [],
        flowsStatus: 500,
      });

      const text = (element.shadowRoot?.textContent || '').replace(/\s+/g, ' ');
      expect(text).to.contain('Could not load your flows');
      expect(text).to.not.contain('No flows yet');
      // The failure no longer escapes loadData, so the rest of the page ran.
      expect((element as any).isLoading).to.be.false;
    });

    it('reloads the flows when the failure card is retried', async () => {
      const element = await renderFlows({
        executions: [],
        flowsStatus: 500,
      });
      fetchStub.restore();
      fetchStub = stubApi({ executions: [] });
      invalidateApiCaches();

      const retry = element.shadowRoot?.querySelector(
        '.empty-card .empty-cta-btn'
      ) as HTMLElement;
      expect(retry, 'the failure card offers a retry').to.exist;
      retry.click();
      await waitUntil(
        () => !(element as any).isLoading,
        'Flows view did not finish reloading'
      );
      await element.updateComplete;

      expect(element.shadowRoot?.textContent).to.not.contain(
        'Could not load your flows'
      );
      expect(element.shadowRoot?.querySelector('table.flows-table')).to.exist;
    });
  });

  describe('card footer', () => {
    async function renderCards(flows: unknown[], executions: unknown[]) {
      localStorage.setItem('preloop.flows.view_mode', 'cards');
      fetchStub = sinon
        .stub(window, 'fetch')
        .callsFake(async (input: RequestInfo | URL) => {
          const url = typeof input === 'string' ? input : input.toString();
          const body = url.includes('/api/v1/flows/executions')
            ? executions
            : url.includes('/api/v1/flows/presets')
              ? []
              : flows;
          return new Response(JSON.stringify(body), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        });
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

    const FLOWS = [{ id: 'flow-1', name: 'Nightly Sync', is_enabled: true }];

    it('keeps one primary button on the page when cards are shown', async () => {
      const element = await renderCards(FLOWS, []);

      const cards = element.shadowRoot!.querySelectorAll('.flow-card');
      expect(cards.length).to.equal(1);
      // Every card carrying a filled Run now made the grid all-primary; the
      // page's single primary is Create flow in the header.
      const primaries = Array.from(
        element.shadowRoot!.querySelectorAll('sl-button[variant="primary"]')
      ).filter((button) => !button.closest('sl-dialog'));
      expect(primaries.length).to.equal(1);
      expect((primaries[0].textContent || '').trim()).to.contain('Create flow');
      const runNow = Array.from(
        element.shadowRoot!.querySelectorAll('.flow-card sl-button')
      ).find((button) => (button.textContent || '').includes('Run now'))!;
      expect(runNow.hasAttribute('outline')).to.be.true;
      expect(runNow.getAttribute('variant')).to.not.equal('primary');
    });

    it('shows the last run outcome and subject in the card footer', async () => {
      const element = await renderCards(FLOWS, [
        {
          id: 'exec-1',
          flow_id: 'flow-1',
          flow_name: 'Nightly Sync',
          status: 'SUCCEEDED',
          start_time: new Date(Date.now() - 31 * 60 * 1000).toISOString(),
          end_time: new Date(Date.now() - 30 * 60 * 1000).toISOString(),
          trigger_subject: 'preloop/preloop #138',
        },
      ]);

      const footer = element.shadowRoot!.querySelector('.card-last-run')!;
      expect(footer).to.exist;
      expect(footer.textContent).to.contain('Succeeded');
      expect(footer.textContent).to.contain('preloop/preloop #138');
      expect(footer.getAttribute('href')).to.contain(
        '/console/flows/executions/exec-1'
      );
    });

    it('says a flow has never run rather than inventing an outcome', async () => {
      const element = await renderCards(FLOWS, []);

      const footer = element.shadowRoot!.querySelector('.card-last-run')!;
      expect((footer.textContent || '').trim()).to.equal('No run yet');
    });
  });

  it('labels the preset card action Use preset', async () => {
    fetchStub = createFetchStub(
      [],
      [{ id: 'preset-1', name: 'Office Reviewer' }]
    );
    const element = (await fixture(
      html`<flows-view></flows-view>`
    )) as FlowsView;

    await waitUntil(
      () => !(element as any).isLoading,
      'Flows view did not finish loading'
    );
    await waitUntil(
      () => (element as any).presets?.length === 1,
      'Presets did not load'
    );
    await element.updateComplete;

    const buttons = Array.from(
      element.shadowRoot!.querySelectorAll('.flow-card sl-button')
    );
    expect(
      buttons.some((button) =>
        (button.textContent || '').includes('Use preset')
      )
    ).to.be.true;
    expect(element.shadowRoot!.textContent).to.not.include('Use template');
  });
});
