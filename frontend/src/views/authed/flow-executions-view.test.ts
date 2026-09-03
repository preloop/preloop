import { html, fixture, expect } from '@open-wc/testing';
import sinon from 'sinon';
import '../../components/view-header.ts';
import './flow-executions-view';
import { FlowExecutionsView } from './flow-executions-view';

const tick = (ms = 150) => new Promise((r) => setTimeout(r, ms));

const EXECUTIONS = [
  {
    id: 'exec-aaaaaaaa-1',
    flow_id: 'flow-1',
    flow_name: 'Nightly Sync',
    status: 'SUCCEEDED',
    start_time: '2026-03-09T10:00:00Z',
    end_time: '2026-03-09T10:05:00Z',
    tool_calls_count: 3,
  },
  {
    id: 'exec-bbbbbbbb-2',
    flow_id: 'flow-2',
    flow_name: 'Triage',
    status: 'RUNNING',
    start_time: '2026-03-09T11:00:00Z',
  },
];

describe('FlowExecutionsView', () => {
  let fetchStub: sinon.SinonStub;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
  });

  afterEach(() => {
    fetchStub?.restore();
    localStorage.clear();
  });

  function stub(rows: unknown[]) {
    return sinon
      .stub(window, 'fetch')
      .callsFake(
        async () => new Response(JSON.stringify(rows), { status: 200 })
      );
  }

  it('renders the header and empty state with no executions', async () => {
    fetchStub = stub([]);
    const el = (await fixture(
      html`<flow-executions-view></flow-executions-view>`
    )) as FlowExecutionsView;
    await tick();
    await el.updateComplete;
    const header = el.shadowRoot?.querySelector('view-header');
    expect(header?.getAttribute('headerText')).to.equal('Flow Executions');
    expect(el.shadowRoot?.textContent).to.contain('No executions found');
  });

  it('renders a table row per execution', async () => {
    fetchStub = stub(EXECUTIONS);
    const el = (await fixture(
      html`<flow-executions-view></flow-executions-view>`
    )) as FlowExecutionsView;
    await tick();
    await el.updateComplete;
    const rows = el.shadowRoot?.querySelectorAll('tbody tr');
    expect(rows?.length).to.equal(2);
    expect(el.shadowRoot?.textContent).to.contain('Nightly Sync');
    expect(el.shadowRoot?.textContent).to.contain('Triage');
  });

  it('preselects the status and flow filters from the query string', async () => {
    const requested: string[] = [];
    fetchStub = sinon.stub(window, 'fetch').callsFake(async (input) => {
      requested.push(String(input));
      return new Response(JSON.stringify(EXECUTIONS), { status: 200 });
    });
    const original = window.location.href;
    window.history.replaceState(
      {},
      '',
      '/console/flows/executions?status=FAILED&flow_id=flow-1'
    );

    try {
      const el = (await fixture(
        html`<flow-executions-view></flow-executions-view>`
      )) as FlowExecutionsView;
      await tick();
      await el.updateComplete;

      expect(requested.some((url) => url.includes('status=FAILED'))).to.be.true;
      expect(requested.some((url) => url.includes('flow_id=flow-1'))).to.be
        .true;
      const failedButton = el.shadowRoot?.querySelector(
        'sl-button[data-status="FAILED"]'
      );
      expect(failedButton?.getAttribute('variant')).to.equal('danger');
      expect(el.shadowRoot?.textContent).to.contain('Flow: Nightly Sync');
    } finally {
      window.history.replaceState({}, '', original);
    }
  });

  it('renders filter buttons for all execution statuses', async () => {
    fetchStub = stub(EXECUTIONS);
    const el = (await fixture(
      html`<flow-executions-view></flow-executions-view>`
    )) as FlowExecutionsView;
    await tick();
    await el.updateComplete;

    const statuses = [
      'all',
      'RUNNING',
      'PENDING',
      'SUCCEEDED',
      'FAILED',
      'CANCELLED',
    ];
    for (const status of statuses) {
      const btn = el.shadowRoot?.querySelector(
        `sl-button[data-status="${status}"]`
      );
      expect(btn, `Button for status ${status} should exist`).to.exist;
    }
  });

  it('reloads executions when a status filter button is clicked', async () => {
    fetchStub = stub(EXECUTIONS);
    const el = (await fixture(
      html`<flow-executions-view></flow-executions-view>`
    )) as FlowExecutionsView;
    await tick();
    await el.updateComplete;
    const callsBefore = fetchStub.callCount;

    const runningBtn = el.shadowRoot?.querySelector(
      'sl-button[data-status="RUNNING"]'
    ) as HTMLElement;
    expect(runningBtn).to.exist;
    runningBtn.click();
    await tick();
    await el.updateComplete;

    expect(el.statusFilter).to.equal('RUNNING');
    expect(fetchStub.callCount).to.be.greaterThan(callsBefore);
    expect(
      fetchStub
        .getCalls()
        .some((c) => String(c.args[0]).includes('status=RUNNING'))
    ).to.be.true;
  });

  it('surfaces the flow-executions endpoint with pagination params', async () => {
    fetchStub = stub([]);
    const el = (await fixture(
      html`<flow-executions-view></flow-executions-view>`
    )) as FlowExecutionsView;
    await tick();
    await el.updateComplete;
    expect(
      fetchStub
        .getCalls()
        .some((c) => String(c.args[0]).includes('/api/v1/flows/executions'))
    ).to.be.true;
  });

  describe('duration column', () => {
    async function renderRows(rows: unknown[]) {
      fetchStub = stub(rows);
      const el = (await fixture(
        html`<flow-executions-view></flow-executions-view>`
      )) as FlowExecutionsView;
      await tick();
      await el.updateComplete;
      return el;
    }

    const cellText = (el: FlowExecutionsView, rowIndex: number) => {
      const cells = el.shadowRoot
        ?.querySelectorAll('tbody tr')
        [rowIndex]?.querySelectorAll('td');
      // Flow, Subject, Status, Started, Duration, Model, Tool calls, $ est.
      return (cells?.[4]?.textContent || '').replace(/\s+/g, ' ').trim();
    };

    it('names the time columns Started and Duration', async () => {
      const el = await renderRows(EXECUTIONS);
      const headers = Array.from(
        el.shadowRoot?.querySelectorAll('thead th') || []
      ).map((th) => (th.textContent || '').trim());

      expect(headers).to.contain('Duration');
      expect(headers).to.contain('Started');
      expect(headers).to.not.contain('End Time');
      expect(headers).to.not.contain('Start Time');
    });

    it('shows the completed duration for a finished execution', async () => {
      const el = await renderRows([
        {
          id: 'exec-done',
          flow_id: 'flow-1',
          flow_name: 'Nightly Sync',
          status: 'SUCCEEDED',
          start_time: '2026-03-09T10:00:00Z',
          end_time: '2026-03-09T10:04:32Z',
        },
      ]);

      expect(cellText(el, 0)).to.equal('4m 32s');
    });

    it('shows live elapsed time for a running execution', async () => {
      const el = await renderRows([
        {
          id: 'exec-running',
          flow_id: 'flow-2',
          flow_name: 'Triage',
          status: 'RUNNING',
          start_time: new Date(Date.now() - 65_000).toISOString(),
        },
      ]);

      const text = cellText(el, 0);
      expect(text).to.match(/^Running · \d+m \d+s$/);
    });

    it('shows an em dash for a terminal execution with no end time', async () => {
      const el = await renderRows([
        {
          id: 'exec-legacy',
          flow_id: 'flow-3',
          flow_name: 'Legacy',
          status: 'FAILED',
          start_time: '2026-03-09T10:00:00Z',
        },
      ]);

      expect(cellText(el, 0)).to.equal('—');
    });

    it('shows an em dash when the timestamps are unusable', async () => {
      const el = await renderRows([
        {
          id: 'exec-skew',
          flow_id: 'flow-4',
          flow_name: 'Skewed',
          status: 'SUCCEEDED',
          start_time: '2026-03-09T10:05:00Z',
          end_time: '2026-03-09T10:00:00Z',
        },
      ]);

      expect(cellText(el, 0)).to.equal('—');
    });
  });

  describe('wave 7 columns', () => {
    async function renderRows(rows: unknown[]) {
      fetchStub = stub(rows);
      const el = (await fixture(
        html`<flow-executions-view></flow-executions-view>`
      )) as FlowExecutionsView;
      await tick();
      await el.updateComplete;
      return el;
    }

    it('lists the wave 7 columns in order', async () => {
      const el = await renderRows(EXECUTIONS);
      const headers = Array.from(
        el.shadowRoot?.querySelectorAll('thead th') || []
      ).map((th) => (th.textContent || '').trim());

      expect(headers).to.eql([
        'Flow',
        'Subject',
        'Status',
        'Started',
        'Duration',
        'Model',
        'Tool calls',
        '$ est.',
        '',
      ]);
    });

    it('shows the alias, the muted provider and +N for a multi model run', async () => {
      const el = await renderRows([
        {
          id: 'exec-multi',
          flow_id: 'flow-1',
          flow_name: 'Nightly Sync',
          status: 'SUCCEEDED',
          start_time: '2026-03-09T10:00:00Z',
          end_time: '2026-03-09T10:01:00Z',
          model_alias: 'anthropic/claude-sonnet-4',
          provider_name: 'anthropic',
          models_used: [
            {
              model_alias: 'anthropic/claude-sonnet-4',
              provider_name: 'anthropic',
              request_count: 4,
            },
            {
              model_alias: 'openai/gpt-5',
              provider_name: 'openai',
              request_count: 1,
            },
          ],
        },
      ]);

      const cell = el.shadowRoot?.querySelector('tbody .model-cell');
      expect(
        cell?.querySelector('.execution-model-alias')?.textContent
      ).to.equal('anthropic/claude-sonnet-4');
      expect(
        cell?.querySelector('.execution-model-provider')?.textContent
      ).to.equal('anthropic');
      expect(
        cell?.querySelector('.execution-model-more')?.textContent
      ).to.equal('+1');
      expect(
        cell?.querySelector('.execution-model')?.getAttribute('title')
      ).to.contain('openai/gpt-5');
    });

    it('shows a dash when a run has no attributable model', async () => {
      const el = await renderRows([
        {
          id: 'exec-no-model',
          flow_id: 'flow-1',
          flow_name: 'Nightly Sync',
          status: 'SUCCEEDED',
          start_time: '2026-03-09T10:00:00Z',
          end_time: '2026-03-09T10:01:00Z',
        },
      ]);

      const cell = el.shadowRoot?.querySelector('tbody .model-cell');
      expect((cell?.textContent || '').trim()).to.equal('\u2014');
      expect(cell?.querySelector('.execution-model-more')).to.not.exist;
    });

    it('writes the status as a soft title case chip', async () => {
      const el = await renderRows(EXECUTIONS);
      const chips = Array.from(
        el.shadowRoot?.querySelectorAll('tbody sl-badge') || []
      );

      expect(chips[0]?.textContent?.trim()).to.equal('Succeeded');
      expect(chips[0]?.getAttribute('variant')).to.equal('success');
      expect(chips[0]?.classList.contains('solid')).to.be.false;
      expect(chips[1]?.textContent?.trim()).to.equal('Running');
    });

    it('shows Started as relative time with the absolute time in the title', async () => {
      const el = await renderRows(EXECUTIONS);
      const started = el.shadowRoot?.querySelector('tbody .started-cell');

      expect(started?.getAttribute('title')).to.contain('2026-03-09');
      expect((started?.textContent || '').trim()).to.not.contain('2026-03-09');
    });

    it('shows the estimated cost, dashing an unpriced run', async () => {
      const el = await renderRows([
        {
          id: 'exec-cost',
          flow_id: 'flow-1',
          flow_name: 'Nightly Sync',
          status: 'SUCCEEDED',
          start_time: '2026-03-09T10:00:00Z',
          end_time: '2026-03-09T10:01:00Z',
          estimated_cost: 0.083,
        },
        {
          id: 'exec-unpriced',
          flow_id: 'flow-1',
          flow_name: 'Nightly Sync',
          status: 'SUCCEEDED',
          start_time: '2026-03-09T09:00:00Z',
          end_time: '2026-03-09T09:01:00Z',
          estimated_cost: 0,
        },
      ]);

      const costCells = Array.from(
        el.shadowRoot?.querySelectorAll('tbody tr') || []
      ).map((row) => (row.querySelectorAll('td')[7]?.textContent || '').trim());
      expect(costCells).to.eql(['$0.08', '\u2014']);
    });

    it('anchors the flow name at the execution and makes the row clickable', async () => {
      const el = await renderRows(EXECUTIONS);
      const row = el.shadowRoot?.querySelector('tbody tr');
      const link = row?.querySelector('a.row-link') as HTMLAnchorElement;

      expect(link?.getAttribute('href')).to.contain(
        '/console/flows/executions/exec-aaaaaaaa-1'
      );
      expect(row?.classList.contains('execution-row')).to.be.true;
    });

    it('offers open, open session and the run control in the row menu', async () => {
      const el = await renderRows(EXECUTIONS);
      const menus = Array.from(
        el.shadowRoot?.querySelectorAll('tbody resource-actions') || []
      ) as Array<
        HTMLElement & { actions: Array<{ id: string; href?: string }> }
      >;

      expect(menus.length).to.equal(2);
      const finished = menus[0].actions.map((action) => action.id);
      expect(finished).to.eql(['open', 'open-session']);
      expect(menus[0].actions[1].href).to.contain('?tab=transcript');
      const running = menus[1].actions.map((action) => action.id);
      expect(running).to.contain('cancel');
    });

    it('offers retry on a failed run', async () => {
      const el = await renderRows([
        {
          id: 'exec-failed',
          flow_id: 'flow-1',
          flow_name: 'Nightly Sync',
          status: 'FAILED',
          start_time: '2026-03-09T10:00:00Z',
          end_time: '2026-03-09T10:01:00Z',
        },
      ]);
      const menu = el.shadowRoot?.querySelector(
        'tbody resource-actions'
      ) as HTMLElement & { actions: Array<{ id: string }> };

      expect(menu.actions.map((action) => action.id)).to.contain('retry');
    });

    it('asks for 25 rows a page', async () => {
      await renderRows([]);
      expect(
        fetchStub.getCalls().some((c) => String(c.args[0]).includes('limit=26'))
      ).to.be.true;
    });

    it('says live updates are off before a connection exists, never disconnected', async () => {
      const el = await renderRows(EXECUTIONS);
      const status = el.shadowRoot?.querySelector('.connection-status');

      expect(status?.getAttribute('data-connection')).to.equal('off');
      expect((status?.textContent || '').trim()).to.equal('Live updates off');
      expect(el.shadowRoot?.textContent).to.not.contain('Disconnected');
      expect(el.shadowRoot?.querySelector('.connection-dot.dropped')).to.not
        .exist;
    });

    it('turns the indicator red only after a live connection dropped', async () => {
      const el = await renderRows(EXECUTIONS);
      (el as unknown as { wsConnected: boolean }).wsConnected = true;
      (el as unknown as { wsWasConnected: boolean }).wsWasConnected = true;
      await el.updateComplete;
      expect(
        el.shadowRoot?.querySelector('.connection-status')?.textContent?.trim()
      ).to.equal('Live updates on');

      (el as unknown as { wsConnected: boolean }).wsConnected = false;
      await el.updateComplete;
      const status = el.shadowRoot?.querySelector('.connection-status');
      expect(status?.getAttribute('data-connection')).to.equal('dropped');
      expect((status?.textContent || '').trim()).to.equal('Live updates lost');
    });
  });
});
