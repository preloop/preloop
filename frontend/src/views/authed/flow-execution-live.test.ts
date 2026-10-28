import { fixture, html, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';
import './flow-execution-view';
import type { FlowExecutionView } from './flow-execution-view';
import { unifiedWebSocketManager } from '../../services/unified-websocket-manager';

describe('execution live metadata', () => {
  let view: FlowExecutionView;
  let snapshot: any;
  let detailReads: number;
  let pendingRead: (() => Promise<any>) | undefined;
  const endpoint = '/api/v1/flows/executions/';
  const response = (data: unknown) =>
    new Response(JSON.stringify(data), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  const status = (value: string, extra: Record<string, unknown> = {}) => {
    (view as any).handleWebSocketMessage({
      execution_id: view.executionId,
      timestamp: '2026-01-01T00:02:00Z',
      type: 'status_update',
      payload: { status: value, ...extra },
    });
  };
  const runnerText = () =>
    view.shadowRoot!.querySelector('[data-testid="strip-runner"]')
      ?.textContent || '';
  const settle = () => new Promise((resolve) => setTimeout(resolve, 250));

  beforeEach(async () => {
    localStorage.setItem('accessToken', 'test-token');
    sinon.stub(unifiedWebSocketManager, 'subscribe').returns(() => {});
    sinon.stub(unifiedWebSocketManager, 'onStateChange').returns(() => {});
    detailReads = 0;
    pendingRead = undefined;
    snapshot = {
      id: 'live-one',
      flow_id: 'flow-one',
      status: 'PENDING',
      start_time: '2026-01-01T00:00:00Z',
      runner: { kind: 'unknown', name: 'Not recorded' },
    };
    sinon.stub(window, 'fetch').callsFake(async (input) => {
      const url = String(input);
      if (url === endpoint + 'live-one' || url === endpoint + 'live-two') {
        detailReads++;
        return response(pendingRead ? await pendingRead() : snapshot);
      }
      if (url.endsWith('/metrics'))
        return response({
          tool_calls: 0,
          estimated_cost: 0,
          has_pricing: false,
          token_usage: { total_tokens: 0 },
        });
      if (url.includes('/logs') || url.includes('/gateway-events'))
        return response({ logs: [], source: 'database' });
      return response({});
    });
    view = await fixture<FlowExecutionView>(
      html`<flow-execution-view
        .executionId=${'live-one'}
      ></flow-execution-view>`
    );
    await waitUntil(
      () => !!(view as any).execution && !(view as any).isLoading
    );
  });

  afterEach(() => {
    view.remove();
    sinon.restore();
    localStorage.clear();
  });

  it('does not claim hosted before assignment or for an absent projection', async () => {
    expect(runnerText()).not.to.contain('Preloop hosted');
    (view as any).execution = { ...snapshot, runner: undefined };
    await view.updateComplete;
    expect(runnerText()).not.to.contain('Preloop hosted');
  });

  it('refreshes a private assignment and the terminal reason without reloading logs', async () => {
    // Startup recovery deliberately reloads retained logs after subscribing.
    // Measure status-only refreshes after that independent recovery finishes.
    await waitUntil(
      () =>
        (view as any).realtimeRecoveryTimer === undefined &&
        !(view as any).realtimeRecovery &&
        (view as any).metadataRefreshTimer === undefined &&
        !(view as any).metadataRefreshRunning
    );
    snapshot = {
      ...snapshot,
      status: 'RUNNING',
      runner: { kind: 'private', name: 'Office runner' },
      agent_session_reference: 'runner:example:live-one',
    };
    const logReads = () =>
      (window.fetch as sinon.SinonStub)
        .getCalls()
        .filter((call) => String(call.args[0]).includes('/logs')).length;
    const initialLogReads = logReads();
    status('RUNNING');
    await waitUntil(() => runnerText().includes('Office runner'));
    expect(logReads()).to.equal(initialLogReads);
    snapshot = {
      ...snapshot,
      status: 'FAILED',
      error_message: 'Execution timed out after 5400 seconds',
      end_time: '2026-01-01T00:02:00Z',
      failure_category: 'execution_timeout',
    };
    status('FAILED', {
      error_message: snapshot.error_message,
      end_time: snapshot.end_time,
    });
    await view.updateComplete;
    expect(
      view.shadowRoot!.querySelector('[data-testid="error-line"]')?.textContent
    ).to.contain('5400');
    await waitUntil(
      () => (view as any).execution.end_time === snapshot.end_time
    );
    expect(
      view.shadowRoot!.querySelector('[data-testid="strip-duration"]')
        ?.textContent
    ).to.contain('2m 0s');
    expect(
      view.formatMetadataMessage({
        type: 'status_update',
        payload: { status: 'FAILED', error_message: snapshot.error_message },
      } as any)
    ).to.contain('5400');
  });

  it('coalesces duplicate events and never replaces a newer terminal event with an old response', async () => {
    let release!: (value: any) => void;
    pendingRead = () =>
      new Promise((resolve) => {
        release = resolve;
      });
    status('RUNNING');
    await waitUntil(() => detailReads === 2);
    for (let i = 0; i < 20; i++) status('RUNNING');
    status('FAILED', {
      error_message: 'Timed out',
      end_time: '2026-01-01T00:02:00Z',
    });
    snapshot = {
      ...snapshot,
      status: 'FAILED',
      error_message: 'Timed out',
      end_time: '2026-01-01T00:02:00Z',
    };
    pendingRead = undefined;
    release({
      ...snapshot,
      status: 'RUNNING',
      error_message: null,
      end_time: null,
    });
    await settle();
    expect((view as any).execution.status).to.equal('FAILED');
    expect((view as any).execution.error_message).to.equal('Timed out');
    expect(detailReads).to.equal(3);
  });

  it('keeps terminal event diagnostics when the refresh fails without retrying in a loop', async () => {
    pendingRead = async () => {
      throw new Error('Unavailable');
    };
    status('FAILED', {
      error_message: 'Timed out',
      end_time: '2026-01-01T00:02:00Z',
    });
    await waitUntil(() => detailReads === 2);
    await settle();
    expect((view as any).execution.error_message).to.equal('Timed out');
    expect(detailReads).to.equal(2);
  });

  it('preserves diagnostics added by a later event with the same status', async () => {
    let release!: (value: any) => void;
    pendingRead = () =>
      new Promise((resolve) => {
        release = resolve;
      });
    status('FAILED');
    await waitUntil(() => detailReads === 2);
    status('FAILED', {
      error_message: 'Timed out',
      end_time: '2026-01-01T00:02:00Z',
    });
    snapshot = {
      ...snapshot,
      status: 'FAILED',
      error_message: 'Timed out',
      end_time: '2026-01-01T00:02:00Z',
    };
    pendingRead = undefined;
    release({ ...snapshot, error_message: null, end_time: null });
    await settle();
    expect((view as any).execution.error_message).to.equal('Timed out');
    expect(detailReads).to.equal(3);
  });

  it('ignores a detail response after unmount', async () => {
    let release!: (value: any) => void;
    pendingRead = () =>
      new Promise((resolve) => {
        release = resolve;
      });
    status('RUNNING');
    await waitUntil(() => detailReads === 2);
    view.remove();
    release({ ...snapshot, status: 'FAILED', error_message: 'stale' });
    await settle();
    expect((view as any).execution.error_message).not.to.equal('stale');
  });

  it('ignores the old response when navigating to another execution', async () => {
    let release!: (value: any) => void;
    pendingRead = () =>
      new Promise((resolve) => {
        release = resolve;
      });
    status('RUNNING');
    await waitUntil(() => detailReads === 2);
    pendingRead = undefined;
    snapshot = { ...snapshot, id: 'live-two', status: 'SUCCEEDED' };
    view.executionId = 'live-two';
    await waitUntil(() => (view as any).execution.id === 'live-two');
    release({ ...snapshot, id: 'live-one', status: 'FAILED' });
    await settle();
    expect((view as any).execution.id).to.equal('live-two');
    expect((view as any).execution.status).to.equal('SUCCEEDED');
  });
});
