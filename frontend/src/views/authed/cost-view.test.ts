import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';
import '../../components/view-header.ts';
import './cost-view.ts';
import type { CostView } from './cost-view';

describe('CostView', () => {
  let fetchStub: sinon.SinonStub;

  const summary = {
    period_start: '2026-03-01T00:00:00Z',
    period_end: '2026-03-31T00:00:00Z',
    total_requests: 12,
    successful_requests: 11,
    failed_requests: 1,
    token_usage: {
      prompt_tokens: 200,
      completion_tokens: 100,
      total_tokens: 300,
    },
    estimated_cost: 8.5,
    budget: {
      monthly_limit_usd: 100,
      soft_limit_usd: 80,
      current_spend_usd: 8.5,
      soft_limit_exceeded: false,
      hard_limit_exceeded: false,
    },
    requests_by_day: [],
    usage_by_model: [
      {
        ai_model_id: 'model-1',
        model_alias: 'gpt-test',
        provider_name: 'openai',
        request_count: 5,
        token_usage: {
          prompt_tokens: 100,
          completion_tokens: 50,
          total_tokens: 150,
        },
        estimated_cost: 8.5,
      },
    ],
    usage_by_flow: [],
    usage_by_session: [
      {
        runtime_session_id: 'runtime-session-1',
        session_source_type: 'managed_agent',
        session_source_id: 'agent-1',
        agent_id: 'agent-1',
        agent_name: 'Ops Agent',
        title: 'Agent Session',
        flow_execution_id: null,
        flow_id: null,
        flow_name: null,
        session_reference: 'Agent Session',
        model_alias: 'gpt-test',
        provider_name: 'openai',
        request_count: 5,
        token_usage: {
          prompt_tokens: 100,
          completion_tokens: 50,
          total_tokens: 150,
        },
        estimated_cost: 8.5,
        last_request_at: '2026-03-07T10:00:00Z',
      },
    ],
  };

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
    fetchStub = sinon.stub(window, 'fetch');
    fetchStub.callsFake(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString();

      if (url.includes('/api/v1/cost/summary')) {
        return new Response(JSON.stringify(summary), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      if (url.includes('/api/v1/ai-models')) {
        return new Response(JSON.stringify([]), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      if (url.includes('/api/v1/features')) {
        return new Response(JSON.stringify({ features: { billing: true } }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      if (url.includes('/api/v1/budget/policies')) {
        return new Response(JSON.stringify([]), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      return new Response('{}', { status: 200 });
    });
  });

  afterEach(() => {
    fetchStub.restore();
    localStorage.clear();
  });

  it('renders accessible cost metrics and tables after load', async () => {
    const element = (await fixture(html`<cost-view></cost-view>`)) as CostView;
    // `loading` is a private reactive state field; the cast keeps tsc happy
    // without widening the component's public API.
    await waitUntil(
      () => (element as unknown as { loading: boolean }).loading === false
    );

    const metrics = element.shadowRoot?.querySelector(
      '[aria-label="Cost summary metrics"]'
    );
    expect(metrics).to.exist;

    const agentTable = element.shadowRoot?.querySelector(
      'table[aria-label="Spend by agent"]'
    );
    expect(agentTable).to.exist;
    expect(agentTable?.querySelector('th[scope="col"]')).to.exist;
  });

  it('exposes loading status while analytics are fetched', async () => {
    const element = (await fixture(html`<cost-view></cost-view>`)) as CostView;
    await element.updateComplete;

    const loading = element.shadowRoot?.querySelector(
      '[role="status"][aria-busy="true"]'
    );
    expect(loading).to.exist;
  });
});
