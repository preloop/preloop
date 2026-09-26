import { fixture, html, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import './flow-execution-view';
import type { FlowExecutionView } from './flow-execution-view';
import { INITIAL_GATEWAY_EVENTS_TAIL } from './flow-execution-view';

/**
 * The execution page paints from the detail row alone and fills in the rest
 * as it lands. Every request here is held until the test releases it, so each
 * test decides exactly what has arrived.
 */
describe('FlowExecutionView first paint', () => {
  type Route = 'detail' | 'logs' | 'flow' | 'metrics' | 'gateway';
  let fetchStub: sinon.SinonStub;
  let gates: Record<Route, { promise: Promise<void>; release: () => void }>;
  let failures: Partial<Record<Route, boolean>>;
  let gatewayResponse: (url: string) => unknown;

  const json = (body: unknown, status = 200) =>
    new Response(JSON.stringify(body), {
      status,
      headers: { 'Content-Type': 'application/json' },
    });

  const gate = () => {
    let release!: () => void;
    const promise = new Promise<void>((resolve) => (release = resolve));
    return { promise, release };
  };

  const call = (index: number, tokens: number, cost: number) => ({
    id: `event-${index}`,
    execution_id: 'exec-fp',
    timestamp: `2026-09-01T10:0${index}:00Z`,
    type: 'model_gateway_call',
    payload: {
      api_usage_id: `usage-${index}`,
      model_alias: 'openai/gpt-5',
      provider_name: 'openai',
      outcome: 'success',
      status_code: 200,
      total_tokens: tokens,
      estimated_cost: cost,
    },
  });

  const DETAIL = {
    id: 'exec-fp',
    flow_id: 'flow-fp',
    flow_name: 'Nightly triage',
    status: 'SUCCEEDED',
    start_time: '2026-09-01T10:00:00Z',
    end_time: '2026-09-01T10:40:00Z',
    total_tokens: 9000,
    estimated_cost: 0.9,
    tool_calls_count: 7,
    trigger_event_details: { source: 'github', type: 'issue_comment' },
  };

  const routeOf = (url: string): Route | null => {
    const path = new URL(url, window.location.origin).pathname;
    if (path === '/api/v1/flows/executions/exec-fp') return 'detail';
    if (path === '/api/v1/flows/executions/exec-fp/logs') return 'logs';
    if (path === '/api/v1/flows/executions/exec-fp/metrics') return 'metrics';
    if (path === '/api/v1/flows/executions/exec-fp/gateway-events')
      return 'gateway';
    if (path === '/api/v1/flows/flow-fp') return 'flow';
    return null;
  };

  const bodies: Record<Route, (url: string) => unknown> = {
    detail: () => DETAIL,
    logs: () => ({
      logs: [
        {
          execution_id: 'exec-fp',
          timestamp: '2026-09-01T10:00:30Z',
          type: 'agent_log_line',
          payload: { line: 'cloning the repository' },
        },
      ],
      source: 'database',
      has_more: false,
    }),
    flow: () => ({ id: 'flow-fp', name: 'Nightly triage (flow)' }),
    metrics: () => ({
      tool_calls: 7,
      api_requests: 3,
      token_usage: { total_tokens: 9000 },
      estimated_cost: 0.9,
      has_pricing: true,
    }),
    gateway: (url) => gatewayResponse(url),
  };

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
    gates = {
      detail: gate(),
      logs: gate(),
      flow: gate(),
      metrics: gate(),
      gateway: gate(),
    };
    failures = {};
    gatewayResponse = () => ({
      logs: [call(1, 3000, 0.3)],
      source: 'database',
      has_more: false,
    });
    fetchStub = sinon.stub(window, 'fetch');
    fetchStub.callsFake(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString();
      const route = routeOf(url);
      if (!route) return json({ detail: 'Not found' }, 404);
      await gates[route].promise;
      if (failures[route]) return json({ detail: 'boom' }, 500);
      return json(bodies[route](url));
    });
  });

  afterEach(() => {
    fetchStub.restore();
    localStorage.clear();
    window.history.replaceState({}, '', window.location.pathname);
  });

  const urlsFor = (route: Route) =>
    fetchStub
      .getCalls()
      .map((c) => String(c.args[0]))
      .filter((url) => routeOf(url) === route);

  const root = (element: FlowExecutionView) => element.shadowRoot!;
  const text = (element: FlowExecutionView) =>
    (root(element).textContent || '').replace(/\s+/g, ' ');

  async function mount() {
    const element = (await fixture(
      html`<flow-execution-view></flow-execution-view>`
    )) as FlowExecutionView;
    element.executionId = 'exec-fp';
    await element.updateComplete;
    return element;
  }

  async function paint(element: FlowExecutionView) {
    gates.detail.release();
    await waitUntil(
      () =>
        !(element as any).isLoading &&
        !!root(element).querySelector('.status-pill sl-badge'),
      'The page did not paint from the detail row'
    );
    await element.updateComplete;
  }

  const releaseAll = () =>
    (['logs', 'flow', 'metrics', 'gateway'] as Route[]).forEach((route) =>
      gates[route].release()
    );

  it('shows a page spinner only until the detail row lands', async () => {
    const element = await mount();
    expect(root(element).querySelector('.page-loading')).to.exist;

    await paint(element);

    expect(root(element).querySelector('.page-loading')).to.not.exist;
    releaseAll();
  });

  it('paints header, status, title and figures from the detail row alone', async () => {
    const element = await mount();
    await paint(element);

    // Logs, flow, metrics and gateway events are all still in flight.
    expect(urlsFor('logs')).to.have.length(1);
    expect(urlsFor('flow')).to.have.length(1);
    expect(urlsFor('metrics')).to.have.length(1);
    expect(urlsFor('gateway')).to.have.length(1);

    const header = root(element).querySelector('view-header')!;
    expect(header.getAttribute('headertext')).to.equal('Nightly triage');
    expect(
      root(element).querySelector('.status-pill sl-badge')!.textContent
    ).to.match(/\S/);
    expect(
      root(element).querySelector('[data-testid="strip-tools"]')!.textContent
    ).to.contain('7');
    expect(
      root(element).querySelector('[data-testid="strip-cost"]')!.textContent
    ).to.contain('0.90');
    // The sections that are still loading say so instead of "nothing".
    expect(root(element).querySelector('[data-testid="timeline-loading"]')).to
      .exist;
    expect(text(element)).to.not.contain('Nothing recorded for this run yet.');
    expect((element as any).isLoadingLogs).to.equal(true);

    releaseAll();
  });

  it('fills each section as its own response lands', async () => {
    const element = await mount();
    await paint(element);

    // Model calls first: the timeline shows them while logs still load.
    gates.gateway.release();
    await waitUntil(
      () => root(element).querySelectorAll('.timeline-gateway').length === 1,
      'The model call did not reach the timeline on its own'
    );
    expect((element as any).isLoadingLogs).to.equal(true);
    expect(root(element).querySelector('[data-testid="timeline-loading"]')).to
      .exist;

    // Then the logs: the loading line goes away.
    gates.logs.release();
    await waitUntil(
      () => !(element as any).isLoadingLogs,
      'The logs did not land'
    );
    await element.updateComplete;
    expect(root(element).querySelector('[data-testid="timeline-loading"]')).to
      .not.exist;
    expect((element as any).logs).to.have.length(1);

    // Flow last: the header takes the flow's own name.
    gates.flow.release();
    await waitUntil(
      () =>
        root(element)
          .querySelector('view-header')!
          .getAttribute('headertext') === 'Nightly triage (flow)',
      'The flow did not update the title'
    );
    gates.metrics.release();
  });

  it('does not let a failed logs read block the page', async () => {
    failures.logs = true;
    const element = await mount();
    await paint(element);
    releaseAll();

    await waitUntil(
      () => !(element as any).isLoadingLogs && !!(element as any).flow,
      'The other sections did not load past the failed logs read'
    );
    await waitUntil(
      () => root(element).querySelectorAll('.timeline-gateway').length === 1,
      'The model calls did not load past the failed logs read'
    );
    expect((element as any).loadingError).to.equal(null);
    expect((element as any).logs).to.deep.equal([]);
  });

  it('asks only for the newest model calls, without payloads, on first paint', async () => {
    const element = await mount();
    await paint(element);
    releaseAll();

    const [url] = urlsFor('gateway');
    expect(url).to.contain(`tail=${INITIAL_GATEWAY_EVENTS_TAIL}`);
    expect(url).to.contain('metadata_only=true');
    expect(url).to.contain('model_calls_only=true');
    await waitUntil(() => (element as any).gatewayEventsLoaded);
  });

  it('asks for full payloads when the page opens on the transcript', async () => {
    window.history.replaceState(
      {},
      '',
      `${window.location.pathname}?tab=transcript`
    );
    const element = await mount();
    await paint(element);
    releaseAll();

    await waitUntil(
      () => (element as any).gatewayEventsFullLoaded,
      'The transcript deep link did not load full payloads'
    );
    expect(urlsFor('gateway')).to.have.length(1);
    const [url] = urlsFor('gateway');
    expect(url).to.not.contain('metadata_only');
    expect(url).to.not.contain('model_calls_only');
    expect(url).to.not.contain('tail=');
  });

  it('never lowers the run totals with a truncated set of calls', async () => {
    // The newest calls only: 3,000 tokens of a run the row says used 9,000.
    gatewayResponse = (url) =>
      url.includes('tail=')
        ? {
            logs: [call(2, 3000, 0.3)],
            source: 'database',
            has_more: true,
          }
        : {
            logs: [call(1, 6000, 0.6), call(2, 3000, 0.3)],
            source: 'database',
            has_more: false,
          };
    const element = await mount();
    await paint(element);
    releaseAll();

    await waitUntil(() => (element as any).gatewayEventsLoaded);
    await element.updateComplete;
    expect((element as any).gatewayEventsTruncated).to.equal(true);
    expect((element as any).totalTokens).to.equal(9000);
    expect((element as any).budgetUsed).to.equal(0.9);
    expect(root(element).querySelector('[data-testid="timeline-truncated"]')).to
      .exist;

    // Asking for the rest reads every model call and drops the notice.
    (
      root(element).querySelector(
        '[data-testid="load-earlier-calls"]'
      ) as HTMLElement
    ).click();
    await waitUntil(
      () => root(element).querySelectorAll('.timeline-gateway').length === 2,
      'The earlier model calls did not load'
    );
    const [, rest] = urlsFor('gateway');
    expect(rest).to.contain('model_calls_only=true');
    expect(rest).to.not.contain('tail=');
    expect((element as any).gatewayEventsTruncated).to.equal(false);
    expect((element as any).totalTokens).to.equal(9000);
    expect(root(element).querySelector('[data-testid="timeline-truncated"]')).to
      .not.exist;
  });

  it('drops a gateway response that lands after navigating away', async () => {
    const element = await mount();
    await paint(element);
    gates.logs.release();
    gates.flow.release();
    gates.metrics.release();

    // Leave while the first execution's model calls are still in flight.
    (element as any).executionGeneration++;
    gates.gateway.release();
    await new Promise((resolve) => setTimeout(resolve, 50));

    expect((element as any).gatewayEvents).to.deep.equal([]);
    expect((element as any).gatewayEventsLoaded).to.equal(false);
  });
});
