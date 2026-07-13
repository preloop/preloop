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
});
