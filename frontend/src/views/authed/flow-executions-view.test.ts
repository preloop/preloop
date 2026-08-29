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
      // Flow Name, Subject, Status, Start Time, Duration, Tool Calls, Details
      return (cells?.[4]?.textContent || '').replace(/\s+/g, ' ').trim();
    };

    it('replaces the End Time header with Duration', async () => {
      const el = await renderRows(EXECUTIONS);
      const headers = Array.from(
        el.shadowRoot?.querySelectorAll('thead th') || []
      ).map((th) => (th.textContent || '').trim());

      expect(headers).to.contain('Duration');
      expect(headers).to.contain('Start Time');
      expect(headers).to.not.contain('End Time');
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
});
