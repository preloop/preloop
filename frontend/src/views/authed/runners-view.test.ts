import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import '../../components/view-header.ts';
import './runners-view';
import type { RunnersView } from './runners-view';
import { invalidateApiCaches } from '../../api';
import { unifiedWebSocketManager } from '../../services/unified-websocket-manager';

describe('RunnersView', () => {
  let fetchStub: sinon.SinonStub;
  let onRunnerMessage: ((message: unknown) => void) | undefined;

  function createFetchStub(runners: unknown[] = []) {
    return sinon
      .stub(window, 'fetch')
      .callsFake(async (input: RequestInfo | URL) => {
        const url = typeof input === 'string' ? input : input.toString();
        const json = (data: unknown) =>
          new Response(JSON.stringify(data), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        if (url.includes('/api/v1/runners')) {
          return json(runners);
        }
        return json({ detail: `Unhandled: ${url}` });
      });
  }

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
    onRunnerMessage = undefined;
    sinon
      .stub(unifiedWebSocketManager, 'subscribe')
      .callsFake((_topic: string, callback: (message: unknown) => void) => {
        onRunnerMessage = callback;
        return () => undefined;
      });
  });

  afterEach(() => {
    sinon.restore();
    localStorage.clear();
    invalidateApiCaches();
  });

  it('renders the runners list', async () => {
    fetchStub = createFetchStub([
      {
        id: '11111111-1111-4111-8111-111111111111',
        name: 'office-mac',
        hostname: 'mac.local',
        os: 'darwin',
        arch: 'arm64',
        labels: ['local'],
        status: 'online',
        last_heartbeat: '2026-08-17T10:00:00Z',
        current_execution_id: '22222222-2222-4222-8222-222222222222',
        registered_by_email: 'ops@example.com',
      },
    ]);
    const element = (await fixture(
      html`<runners-view></runners-view>`
    )) as RunnersView;

    await waitUntil(
      () => !(element as unknown as { loading: boolean }).loading,
      'Runners view did not finish loading'
    );
    await element.updateComplete;

    const header = element.shadowRoot?.querySelector('view-header');
    expect(header).to.exist;
    expect(header?.getAttribute('headerText')).to.equal('Runners');
    expect(element.shadowRoot?.textContent).to.contain('office-mac');
    expect(element.shadowRoot?.textContent).to.contain('ops@example.com');
    expect(element.shadowRoot?.textContent).to.contain('local');
  });

  it('shows empty state when no runners', async () => {
    fetchStub = createFetchStub([]);
    const element = (await fixture(
      html`<runners-view></runners-view>`
    )) as RunnersView;
    await waitUntil(
      () => !(element as unknown as { loading: boolean }).loading
    );
    await element.updateComplete;
    expect(element.shadowRoot?.textContent).to.contain('No runners registered');
    expect(element.shadowRoot?.textContent).to.contain(
      'preloop runner fg --labels local'
    );
    const docs = element.shadowRoot?.querySelector('sl-button.empty-docs');
    expect(docs).to.exist;
    expect(docs?.getAttribute('rel')).to.equal('noopener noreferrer');
    expect(docs?.getAttribute('target')).to.equal('_blank');
    expect(element.shadowRoot?.querySelector('.empty-command')).to.exist;
  });

  it('updates status from a runners websocket event without a refetch', async () => {
    fetchStub = createFetchStub([
      {
        id: '11111111-1111-4111-8111-111111111111',
        name: 'office-mac',
        hostname: 'mac.local',
        os: 'darwin',
        arch: 'arm64',
        labels: ['local'],
        status: 'offline',
        last_heartbeat: '2026-08-17T10:00:00Z',
        current_execution_id: null,
        registered_by_email: 'ops@example.com',
      },
    ]);
    const element = (await fixture(
      html`<runners-view></runners-view>`
    )) as RunnersView;
    await waitUntil(
      () => !(element as unknown as { loading: boolean }).loading
    );
    await element.updateComplete;
    expect(element.shadowRoot?.textContent).to.contain('offline');

    onRunnerMessage?.({
      type: 'runner_updated',
      payload: {
        id: '11111111-1111-4111-8111-111111111111',
        name: 'office-mac',
        status: 'online',
        last_heartbeat: '2026-09-03T10:00:00Z',
      },
    });
    await element.updateComplete;
    expect(element.shadowRoot?.textContent).to.contain('online');
    expect(
      fetchStub.getCalls().filter((call) => {
        const url = String(call.args[0]);
        return url.includes('/api/v1/runners');
      })
    ).to.have.lengthOf(1);
  });
});
