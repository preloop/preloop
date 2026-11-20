import { fixture, html, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import './flow-execution-view';
import type { FlowExecutionView } from './flow-execution-view';

/**
 * The strip shows the reasoning effort that reached the provider, read from
 * effective_requests, never the effort the flow asked for.
 */
describe('FlowExecutionView effort sent', () => {
  let fetchStub: sinon.SinonStub;
  let detail: Record<string, unknown>;

  const json = (body: unknown) =>
    new Response(JSON.stringify(body), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });

  const BASE = {
    id: 'exec-effort',
    flow_id: 'flow-effort',
    flow_name: 'Issue fixer',
    status: 'SUCCEEDED',
    start_time: '2026-09-01T10:00:00Z',
    end_time: '2026-09-01T10:10:00Z',
    model_alias: 'model-a',
    trigger_event_details: {
      source: 'manual',
      type: 'manual',
      _model_routing: { reasoning_effort: 'high' },
    },
  };

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    fetchStub = sinon.stub(window, 'fetch');
    fetchStub.callsFake(async (input: RequestInfo | URL) => {
      const path = new URL(String(input), window.location.origin).pathname;
      if (path === '/api/v1/flows/executions/exec-effort') return json(detail);
      if (path.endsWith('/logs') || path.endsWith('/gateway-events'))
        return json({ logs: [], source: 'database', has_more: false });
      if (path === '/api/v1/flows/flow-effort')
        return json({
          id: 'flow-effort',
          name: 'Issue fixer',
          agent_config: { reasoning_effort: 'high' },
        });
      if (path.endsWith('/metrics'))
        return json({ token_usage: { total_tokens: 0 }, tool_calls: 0 });
      return json({});
    });
  });

  afterEach(() => {
    fetchStub.restore();
    localStorage.clear();
  });

  async function mount() {
    const element = (await fixture(
      html`<flow-execution-view></flow-execution-view>`
    )) as FlowExecutionView;
    element.executionId = 'exec-effort';
    await waitUntil(
      () => !(element as any).isLoading && !!(element as any).execution,
      'The page did not paint'
    );
    await element.updateComplete;
    return element.shadowRoot!;
  }

  const group = (
    model: string,
    effort: string | null,
    count: number,
    maxOutput: number | null = null
  ) => ({
    model,
    reasoning_effort: effort,
    max_output_tokens: maxOutput,
    request_count: count,
  });

  it('shows the effort sent for one group next to the model', async () => {
    detail = { ...BASE, effective_requests: [group('model-a', 'low', 4)] };
    const root = await mount();
    const item = root.querySelector('[data-testid="strip-effort"]')!;
    expect(item.textContent!.trim()).to.equal('Low');
    const model = root.querySelector('[data-testid="strip-model"]')!;
    expect(model.closest('.strip-item')!.nextElementSibling).to.equal(
      item.closest('.strip-item')
    );
    expect(item.closest('.strip-item')!.textContent).to.include('Effort sent');
  });

  it('shows each group with its request count when they differ', async () => {
    detail = {
      ...BASE,
      effective_requests: [
        group('model-a', 'low', 3, 1000),
        group('model-a', 'low', 2, 2000),
        group('model-b', 'high', 1),
      ],
    };
    const root = await mount();
    const text = root
      .querySelector('[data-testid="strip-effort"]')!
      .textContent!.replace(/\s+/g, ' ')
      .trim();
    expect(text).to.equal(
      'Low · model-a (5 requests), High · model-b (1 request)'
    );
  });

  it('names a group that sent no effort among groups that did', async () => {
    detail = {
      ...BASE,
      effective_requests: [
        group('model-a', 'low', 2),
        group('model-b', null, 1),
      ],
    };
    const root = await mount();
    const text = root
      .querySelector('[data-testid="strip-effort"]')!
      .textContent!.replace(/\s+/g, ' ')
      .trim();
    expect(text).to.equal(
      'Low · model-a (2 requests), None sent · model-b (1 request)'
    );
  });

  for (const [name, value] of [
    ['absent', undefined],
    ['null', null],
    ['empty', []],
    ['without any effort', [group('model-a', null, 3)]],
  ] as const) {
    it(`shows no effort sent when effective_requests is ${name}`, async () => {
      detail = { ...BASE };
      if (value !== undefined) detail.effective_requests = value;
      const root = await mount();
      expect(root.querySelector('[data-testid="strip-effort"]')).to.equal(null);
      expect(root.textContent).to.not.include('Effort sent');
    });
  }
});
