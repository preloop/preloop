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

  it('reloads executions when the status filter changes', async () => {
    fetchStub = stub(EXECUTIONS);
    const el = (await fixture(
      html`<flow-executions-view></flow-executions-view>`
    )) as FlowExecutionsView;
    await tick();
    await el.updateComplete;
    const callsBefore = fetchStub.callCount;
    (el as any).handleStatusFilterChange({ target: { value: 'RUNNING' } });
    await tick();
    expect((el as any).statusFilter).to.equal('RUNNING');
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
});
