import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import '../../components/view-header.ts';
import './flows-view';
import type { FlowsView } from './flows-view';
import { invalidateApiCaches } from '../../api';

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
});
