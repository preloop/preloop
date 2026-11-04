import { aTimeout, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';
import './flow-executions-view';
import type { FlowExecutionsView } from './flow-executions-view';
import {
  ConnectionState,
  unifiedWebSocketManager,
} from '../../services/unified-websocket-manager';

const row = (id: string, status = 'SUCCEEDED') => ({
  id,
  flow_id: 'flow-1',
  flow_name: id,
  status,
  start_time: '2026-10-01T10:00:00Z',
});
const json = (rows: unknown[]) =>
  new Response(JSON.stringify(rows), { status: 200 });

describe('execution request lifecycle', () => {
  let el: FlowExecutionsView;
  let reads: Array<{ url: URL; finish: (response: Response) => void }>;
  let listeners: Set<(state: ConnectionState) => void>;
  let subscriptions: Set<(message: unknown) => void>;

  beforeEach(() => {
    localStorage.clear();
    localStorage.setItem('accessToken', 'test-token');
    window.history.replaceState({}, '', '/console/flows/executions');
    reads = [];
    listeners = new Set();
    subscriptions = new Set();
    sinon
      .stub(unifiedWebSocketManager, 'getState')
      .returns(ConnectionState.CONNECTED);
    sinon.stub(unifiedWebSocketManager, 'subscribe').callsFake((_topic, cb) => {
      subscriptions.add(cb);
      return () => subscriptions.delete(cb);
    });
    sinon.stub(unifiedWebSocketManager, 'onStateChange').callsFake((cb) => {
      listeners.add(cb);
      return () => listeners.delete(cb);
    });
    sinon.stub(window, 'fetch').callsFake(async (input, init) => {
      const url = new URL(String(input), window.location.origin);
      if (url.pathname.endsWith('/flows/executions')) {
        // Ignore cancellation to exercise responses already buffered on abort.
        return new Promise<Response>((finish) =>
          reads.push({ url, signal: init?.signal, finish })
        );
      }
      return json([]);
    });
    el = document.createElement('flow-executions-view') as FlowExecutionsView;
    document.body.append(el);
  });

  afterEach(() => {
    el.remove();
    sinon.restore();
    localStorage.clear();
  });

  async function loaded() {
    await waitUntil(() => reads.length === 1);
    reads[0].finish(json([row('initial')]));
    await waitUntil(() => el.filteredExecutions[0]?.id === 'initial');
  }

  it('does not attach listeners or restart duration ticking after unmount during initial load', async () => {
    await waitUntil(() => reads.length === 1);
    el.remove();
    expect(reads[0].signal?.aborted).to.equal(true);
    reads[0].finish(json([row('late', 'RUNNING')]));
    await aTimeout(50);
    expect(subscriptions.size).to.equal(0);
    expect(listeners.size).to.equal(0);
    expect((el as any).durationTickIntervalId).to.equal(undefined);
    expect(el.filteredExecutions).to.deep.equal([]);
  });

  it('keeps the newest filter/page when responses complete out of order', async () => {
    await loaded();
    el.setStatusFilter('FAILED');
    await waitUntil(() => reads.length === 2);
    el.goToPage(2);
    await waitUntil(() => reads.length === 3);
    expect(reads[1].signal?.aborted).to.equal(true);
    expect(reads[2].url.searchParams.get('skip')).to.equal('25');
    expect(reads[2].url.searchParams.get('status')).to.equal('FAILED');
    reads[2].finish(json([row('page-2', 'FAILED')]));
    await waitUntil(() => el.filteredExecutions[0]?.id === 'page-2');
    reads[1].finish(json([row('page-1', 'FAILED')]));
    await aTimeout(30);
    expect(el.filteredExecutions[0].id).to.equal('page-2');
  });

  it('coalesces live bursts and retains one trailing refresh during a read', async () => {
    await loaded();
    const burst = () => {
      for (let i = 0; i < 20; i++)
        el.handleWebSocketMessage({
          type: 'execution_started',
          payload: { id: `new-${i}` },
        });
    };
    burst();
    await waitUntil(() => reads.length >= 2);
    expect(reads.length).to.equal(2);
    burst();
    await aTimeout(250);
    expect(reads.length).to.equal(2);
    reads[1].finish(json([row('first-refresh')]));
    await waitUntil(() => reads.length === 3);
    reads[2].finish(json([row('trailing-refresh')]));
    await waitUntil(() => el.filteredExecutions[0]?.id === 'trailing-refresh');
    await aTimeout(250);
    expect(reads.length).to.equal(3);
  });

  it('reconciles status changes against the active filter', async () => {
    await loaded();
    el.setStatusFilter('RUNNING');
    await waitUntil(() => reads.length === 2);
    reads[1].finish(json([row('running', 'RUNNING')]));
    await waitUntil(() => el.filteredExecutions[0]?.id === 'running');
    el.handleWebSocketMessage({
      type: 'status_update',
      execution_id: 'running',
      payload: { status: 'SUCCEEDED' },
    });
    await waitUntil(() => reads.length === 3);
    expect(reads[2].url.searchParams.getAll('status')).to.include('RUNNING');
    reads[2].finish(json([]));
    await waitUntil(() => el.filteredExecutions.length === 0);
  });

  it('refreshes after reconnect and cancels a scheduled refresh on navigation', async () => {
    await loaded();
    for (const listener of listeners) listener(ConnectionState.DISCONNECTED);
    for (const listener of listeners) listener(ConnectionState.CONNECTED);
    await waitUntil(() => reads.length === 2);
    reads[1].finish(json([row('reconnected')]));
    await waitUntil(() => el.filteredExecutions[0]?.id === 'reconnected');
    el.handleWebSocketMessage({ type: 'execution_started', payload: {} });
    el.remove();
    await aTimeout(250);
    expect(reads.length).to.equal(2);
    expect(listeners.size).to.equal(0);
    expect(subscriptions.size).to.equal(0);
  });
});
