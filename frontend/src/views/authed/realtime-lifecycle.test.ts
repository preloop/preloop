import { expect, waitUntil, aTimeout } from '@open-wc/testing';
import sinon from 'sinon';
import './approvals-view';
import './flow-execution-view';
import { unifiedWebSocketManager } from '../../services/unified-websocket-manager';
const json = (data: unknown) =>
  new Response(JSON.stringify(data), { status: 200 });

describe('realtime lifecycle regressions', () => {
  let mounted: HTMLElement[] = [];
  beforeEach(() => {
    localStorage.setItem('accessToken', 'synthetic');
  });
  afterEach(() => {
    mounted.forEach((e) => e.remove());
    mounted = [];
    sinon.restore();
    localStorage.clear();
  });
  it('cleans up approvals subscriptions across 30 leave-before-response visits', async () => {
    const subscriptions = new Set<(m: any) => void>();
    sinon.stub(unifiedWebSocketManager, 'subscribe').callsFake((_topic, cb) => {
      subscriptions.add(cb);
      return () => subscriptions.delete(cb);
    });
    let finish: ((r: Response) => void) | undefined;
    sinon.stub(window, 'fetch').callsFake(async (input) => {
      const url = String(input);
      if (url.includes('/approval-requests?'))
        return new Promise<Response>((resolve) => {
          finish = resolve;
        });
      return json({ total: 0, tools: [] });
    });
    const counts = [];
    for (let i = 0; i < 30; i++) {
      finish = undefined;
      const el: any = document.createElement('approvals-view');
      mounted.push(el);
      document.body.append(el);
      await waitUntil(() => !!finish);
      el.remove();
      finish!(
        json([
          {
            id: 'stale',
            status: 'pending',
            requested_at: new Date().toISOString(),
            expires_at: new Date(Date.now() + 60000).toISOString(),
            tool_name: 'example',
            tool_args: {},
          },
        ])
      );
      await aTimeout(10);
      counts.push(subscriptions.size);
      expect(el.approvalRequests).to.have.length(0);
      expect(el.tickTimer).to.equal(undefined);
    }
    document.body.dataset.approvalSubscriptionCounts = JSON.stringify(counts);
    expect(
      subscriptions.size,
      'Detached views must not remain subscribed after late responses'
    ).to.equal(0);
  });
  it('keeps execution logs near live after a 30-second stream at 50 lines/second', async () => {
    sinon.stub(unifiedWebSocketManager, 'subscribe').returns(() => {});
    sinon.stub(unifiedWebSocketManager, 'onStateChange').returns(() => {});
    sinon.stub(window, 'fetch').resolves(json({}));
    const el: any = document.createElement('flow-execution-view');
    mounted.push(el);
    document.body.append(el);
    await el.updateComplete;
    // No executionId property change: exercise actual event/flush/render with a synthetic running row.
    el.execution = {
      id: 'stream',
      status: 'RUNNING',
      start_time: new Date().toISOString(),
    };
    Object.defineProperty(el, 'executionId', {
      value: 'stream',
      writable: true,
    });
    el.activeTab = 'logs';
    el.isAutoScroll = false;
    el.startBufferFlush();
    const samples = [];
    let sent = 0;
    for (let tick = 0; tick < 60; tick++) {
      for (let j = 0; j < 25; j++)
        el.handleWebSocketMessage({
          execution_id: 'stream',
          timestamp: new Date().toISOString(),
          type: 'agent_log_line',
          log_id: `row-${sent + 1}`,
          payload: { line: `Synthetic line ${++sent}` },
        });
      await aTimeout(500);
      expect(el.logBuffer.length).to.be.at.most(25);
      if ((tick + 1) % 20 === 0)
        samples.push({
          elapsedSeconds: (tick + 1) / 2,
          sent,
          rendered: el.logs.length,
          queued: el.logBuffer.length,
        });
    }
    document.body.dataset.executionStream = JSON.stringify(samples);
    await waitUntil(() => el.logBuffer.length === 0);
    expect(el.logs.length).to.equal(1500);
    expect(new Set(el.logs.map((log: any) => log.log_id)).size).to.equal(1500);
    await el.updateComplete;
    expect(
      el.shadowRoot.querySelectorAll('.log-container .log-entry').length
    ).to.equal(1000);
  });
});

describe('realtime remount, filter races and burst recovery', () => {
  let el: any;
  beforeEach(() => localStorage.setItem('accessToken', 'synthetic'));
  afterEach(() => {
    el?.remove();
    sinon.restore();
    localStorage.clear();
  });
  const row = (id: string) => ({
    id,
    status: 'pending',
    requested_at: new Date().toISOString(),
    expires_at: new Date(Date.now() + 60000).toISOString(),
    tool_name: 'example',
    tool_args: {},
  });
  it('keeps one listener on remount and ignores superseded responses and stats', async () => {
    const subscribers = new Set<(message: any) => void>();
    sinon.stub(unifiedWebSocketManager, 'subscribe').callsFake((_topic, cb) => {
      subscribers.add(cb);
      return () => {
        subscribers.delete(cb);
      };
    });
    const requests: {
      url: string;
      signal?: AbortSignal | null;
      resolve: (response: Response) => void;
    }[] = [];
    sinon.stub(window, 'fetch').callsFake(async (input, init) =>
      String(input).includes('/approval-requests')
        ? new Promise<Response>((resolve) =>
            requests.push({
              url: String(input),
              signal: init?.signal,
              resolve,
            })
          )
        : json({})
    );
    el = document.createElement('approvals-view');
    document.body.append(el);
    await waitUntil(() => requests.length === 2);
    el.remove();
    expect(requests.every((r) => r.signal?.aborted)).to.equal(true);
    document.body.append(el);
    await waitUntil(() => requests.length === 4);
    requests[3].resolve(json([row('current')]));
    requests[2].resolve(json({}));
    await waitUntil(() => subscribers.size === 1);
    requests[1].resolve(json([row('stale')]));
    requests[0].resolve(json({}));
    await aTimeout(0);
    expect(el.approvalRequests.map((r: any) => r.id)).to.deep.equal([
      'current',
    ]);
    el.searchQuery = 'old';
    void el.loadApprovalRequests();
    await waitUntil(() => requests.length === 6);
    el.searchQuery = 'new';
    void el.loadApprovalRequests();
    await waitUntil(() => requests.length === 8);
    requests[7].resolve(json([row('new')]));
    requests[6].resolve(json({}));
    await aTimeout(0);
    requests[5].resolve(json([row('old')]));
    requests[4].resolve(json({}));
    await aTimeout(0);
    expect(el.approvalRequests.map((r: any) => r.id)).to.deep.equal(['new']);
    expect(el.tickTimer).not.to.equal(undefined);
    el.searchQuery = '';
    [...subscribers][0]({
      type: 'approval_created',
      approval_request_id: 'live',
      tool_name: 'example',
      expires_at: new Date(Date.now() + 60000).toISOString(),
    });
    expect(el.approvalRequests.some((r: any) => r.id === 'live')).to.equal(
      true
    );
    el.remove();
    expect(subscribers.size).to.equal(0);
    expect(el.tickTimer).to.equal(undefined);
    expect(el.statsTimer).to.equal(null);
  });
  it('yields during bursts and bounds rendered history without losing lines or a paused reading position', async () => {
    sinon.stub(unifiedWebSocketManager, 'subscribe').returns(() => {});
    sinon.stub(unifiedWebSocketManager, 'onStateChange').returns(() => {});
    sinon.stub(window, 'fetch').resolves(json({}));
    el = document.createElement('flow-execution-view');
    document.body.append(el);
    await el.updateComplete;
    el.execution = {
      id: 'stream',
      status: 'RUNNING',
      start_time: new Date().toISOString(),
    };
    Object.defineProperty(el, 'executionId', {
      value: 'stream',
      writable: true,
    });
    el.activeTab = 'logs';
    el.isAutoScroll = false;
    const line = (index: number) => ({
      execution_id: 'stream',
      log_id: `row-${index}`,
      timestamp: '2026-01-01T00:00:00Z',
      type: 'agent_log_line',
      payload: { line: `Synthetic line ${index}` },
    });
    el.logs = [line(0)];
    el.logBuffer = [
      line(0),
      ...Array.from({ length: 5999 }, (_, i) => line(i + 1)),
    ];
    el.flushLogBuffer();
    expect(el.logs.length).to.be.at.most(251);
    expect(el.logBuffer.length).to.be.greaterThan(0);
    await waitUntil(() => el.logBuffer.length === 0, 'burst catchup', {
      timeout: 10000,
    });
    await el.updateComplete;
    expect(el.logs.map((log: any) => log.log_id)).to.deep.equal(
      Array.from({ length: 6000 }, (_, i) => `row-${i}`)
    );
    const container = el.shadowRoot.querySelector(
      '.log-container'
    ) as HTMLElement;
    expect(container.querySelectorAll('.log-entry').length).to.equal(1000);
    container.scrollTop = 100;
    const first = container.querySelector('.log-entry')?.textContent;
    el.logBuffer = [line(6000)];
    el.flushLogBuffer();
    await el.updateComplete;
    expect(container.scrollTop).to.equal(100);
    expect(container.querySelector('.log-entry')?.textContent).to.equal(first);
    const copy = sinon.stub(navigator.clipboard, 'writeText').resolves();
    await el.copyAllLogs();
    expect(copy.firstCall.args[0].split('\n')).to.have.length(6001);
    el.logSearchQuery = 'Synthetic line 5999';
    await el.updateComplete;
    expect(container.textContent).to.contain('Synthetic line 5999');
    el.logSearchQuery = '';
    el.isAutoScroll = true;
    await el.updateComplete;
    expect(container.textContent).to.contain('Synthetic line 6000');
    const earlier = [...el.shadowRoot.querySelectorAll('sl-button')].find(
      (button: any) => button.textContent.includes('Earlier lines')
    ) as HTMLElement;
    earlier.click();
    await el.updateComplete;
    expect(el.isAutoScroll).to.equal(false);
    expect(el.logWindowStart).to.be.lessThan(5001);
    const pageStart = el.logWindowStart;
    const anchor = container.querySelector('.log-entry')?.textContent;
    el.hasMoreLogs = true;
    el.logsSkip = 6001;
    (window.fetch as sinon.SinonStub).resolves(
      json({ logs: [line(-1)], has_more: false })
    );
    await el.loadPreviousLogs();
    await el.updateComplete;
    expect(el.logWindowStart).to.equal(pageStart + 1);
    expect(container.querySelector('.log-entry')?.textContent).to.equal(anchor);

    el.logBuffer = Array.from({ length: 2000 }, (_, i) => line(i + 6001));
    el.handleWebSocketMessage({
      execution_id: 'stream',
      timestamp: '2026-01-01T00:01:00Z',
      type: 'status_update',
      payload: { status: 'SUCCEEDED', model_output_summary: 'Finished' },
    });
    expect(el.logBuffer.length).to.be.greaterThan(0);
    await waitUntil(() => el.logBuffer.length === 0);
    expect(el.logs.at(-1).type).to.equal('model_output');
    el.logBuffer = Array.from({ length: 2000 }, (_, i) => line(i + 9001));
    el.stopBufferFlush();
    expect(el.logBuffer.length).to.be.greaterThan(0);
    el.remove();
    const retained = el.logs.length;
    await aTimeout(20);
    expect(el.logs.length).to.equal(retained);
    expect(el.logBuffer).to.have.length(0);
    expect(el.bufferDrainTimer).to.equal(undefined);
    expect(el.bufferFlushInterval).to.equal(undefined);
  });
});
