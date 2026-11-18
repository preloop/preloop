import { fixture, html, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import './flow-execution-view';
import type { FlowExecutionView } from './flow-execution-view';

/**
 * An OpenRouter-routed run names the upstream provider that served the
 * final answer next to the model, and a native-result failure on a spread
 * or unpinned route carries one sentence pointing at a provider preference.
 */
describe('FlowExecutionView upstream provider', () => {
  let fetchStub: sinon.SinonStub;
  let detail: Record<string, unknown>;

  const json = (body: unknown) =>
    new Response(JSON.stringify(body), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });

  const BASE = {
    id: 'exec-up',
    flow_id: 'flow-up',
    flow_name: 'Native review',
    status: 'FAILED',
    start_time: '2026-09-01T10:00:00Z',
    end_time: '2026-09-01T10:10:00Z',
    trigger_event_details: { source: 'manual', type: 'manual' },
  };

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    fetchStub = sinon.stub(window, 'fetch');
    fetchStub.callsFake(async (input: RequestInfo | URL) => {
      const path = new URL(String(input), window.location.origin).pathname;
      if (path === '/api/v1/flows/executions/exec-up') return json(detail);
      if (path.endsWith('/logs') || path.endsWith('/gateway-events'))
        return json({ logs: [], source: 'database', has_more: false });
      if (path === '/api/v1/flows/flow-up')
        return json({ id: 'flow-up', name: 'Native review' });
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
    element.executionId = 'exec-up';
    await waitUntil(
      () => !(element as any).isLoading && !!(element as any).execution,
      'The page did not paint'
    );
    await element.updateComplete;
    return element.shadowRoot!;
  }

  it('shows the final-answer upstream and the providers used', async () => {
    detail = {
      ...BASE,
      failure_category: 'no_confirmation',
      error_message: 'The run ended without a result.',
      upstream_route: {
        upstream_providers: [
          { provider: 'Provider A', request_count: 3 },
          { provider: 'Provider C', request_count: 1 },
        ],
        final_answer_upstream_provider: 'Provider C',
        upstream_provider_pinned: false,
        upstream_hint:
          'Provider C served the final answer on OpenRouter and this run used 2 upstream providers; a provider preference on the model (only, or order with allow_fallbacks false) can pin one provider: https://docs.preloop.ai/guide/providers/openrouter/#structured-results-and-upstream-providers',
      },
    };
    const root = await mount();
    const strip = root.querySelector('[data-testid="strip-upstream"]')!;
    expect(
      strip.parentElement!.querySelector('.strip-label')!.textContent
    ).to.equal('Served by');
    expect(strip.textContent).to.contain('Provider C');
    expect(strip.textContent).to.contain('+1 more');
    expect(strip.getAttribute('title')).to.contain('Provider A: 3 requests');
    const hint = root.querySelector('[data-testid="upstream-hint"]')!;
    expect(hint.textContent).to.contain('Provider C served the final answer');
    expect(hint.textContent).to.not.contain('https://');
    expect(hint.querySelector('a')!.getAttribute('href')).to.contain(
      '#structured-results-and-upstream-providers'
    );
  });

  it('does not credit a provider when no request finished with stop', async () => {
    detail = {
      ...BASE,
      upstream_route: {
        upstream_providers: [{ provider: 'Provider A', request_count: 2 }],
        final_answer_upstream_provider: null,
      },
    };
    const root = await mount();
    const strip = root.querySelector('[data-testid="strip-upstream"]')!;
    const label = strip.parentElement!.querySelector('.strip-label')!;
    expect(label.textContent).to.equal('Providers');
    expect(strip.getAttribute('title')).to.not.contain('Final answer');
  });

  it('shows nothing for a route with no recorded upstream', async () => {
    detail = { ...BASE, status: 'SUCCEEDED' };
    const root = await mount();
    expect(root.querySelector('[data-testid="strip-upstream"]')).to.not.exist;
    expect(root.querySelector('[data-testid="upstream-hint"]')).to.not.exist;
  });
});
