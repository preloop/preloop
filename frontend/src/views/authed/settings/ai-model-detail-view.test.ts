import { fixture, html, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import { unifiedWebSocketManager } from '../../../services/unified-websocket-manager';
import '../../../setup-tests';
import './ai-model-detail-view';
import type { AIModelDetailView } from './ai-model-detail-view';

describe('AIModelDetailView', () => {
  let fetchStub: sinon.SinonStub;
  let connectStub: sinon.SinonStub;
  let subscribeStub: sinon.SinonStub;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');

    fetchStub = sinon.stub(window, 'fetch');
    fetchStub.callsFake(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString();

      if (
        url.includes('/api/v1/ai-models/model-1') &&
        !url.includes('/summary') &&
        !url.includes('/runtime-sessions') &&
        !url.includes('/interactions')
      ) {
        return new Response(
          JSON.stringify({
            id: 'model-1',
            name: 'Claude Sonnet Primary',
            provider_name: 'Anthropic',
            model_identifier: 'claude-sonnet-4',
            has_api_key: true,
            meta_data: {
              gateway: {
                enabled: true,
                url: 'https://gateway.example/openai/v1',
                model_alias: 'preloop/anthropic/claude-sonnet-4',
              },
              managed_agent_id: 'agent-1',
              managed_agent_display_name: 'Mini Claw',
              managed_agent_runtime_principal_id: 'mini-claw-123',
            },
            is_default: true,
            created_at: '2026-03-01T10:00:00Z',
            updated_at: '2026-03-09T18:30:00Z',
          }),
          {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          }
        );
      }

      if (url.startsWith('/api/v1/ai-models/model-1/summary')) {
        return new Response(
          JSON.stringify({
            ai_model_id: 'model-1',
            model_name: 'Claude Sonnet Primary',
            provider_name: 'Anthropic',
            model_identifier: 'claude-sonnet-4',
            period_start: '2026-02-08T00:00:00Z',
            period_end: '2026-03-09T23:59:59Z',
            total_requests: 18,
            successful_requests: 16,
            failed_requests: 2,
            token_usage: {
              prompt_tokens: 6400,
              completion_tokens: 2100,
              total_tokens: 8500,
            },
            estimated_cost: 1.42,
            requests_by_day: [
              {
                date: '2026-03-08',
                request_count: 7,
                estimated_cost: 0.51,
                total_tokens: 3200,
              },
              {
                date: '2026-03-09',
                request_count: 11,
                estimated_cost: 0.91,
                total_tokens: 5300,
              },
            ],
            usage_by_session: [],
          }),
          {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          }
        );
      }

      if (url.startsWith('/api/v1/ai-models/model-1/runtime-sessions')) {
        return new Response(
          JSON.stringify({
            period_start: '2026-02-08T00:00:00Z',
            period_end: '2026-03-09T23:59:59Z',
            query: null,
            session_source_type: null,
            status: 'all',
            total: 1,
            limit: 10,
            offset: 0,
            items: [
              {
                id: 'runtime-session-1',
                session_source_type: 'flow_execution',
                session_source_id: 'execution-1',
                session_reference: 'session-abc123',
                runtime_principal_type: 'flow_execution',
                runtime_principal_id: 'execution-1',
                runtime_principal_name: 'Triage Assistant',
                started_at: '2026-03-09T19:00:00Z',
                last_activity_at: '2026-03-09T19:15:00Z',
                ended_at: '2026-03-09T19:20:00Z',
                flow_id: 'flow-1',
                flow_name: 'Triage Assistant',
                flow_execution_id: 'execution-1',
                latest_model_alias: 'anthropic/claude-sonnet-4',
                latest_provider_name: 'Anthropic',
                total_requests: 6,
                successful_requests: 6,
                failed_requests: 0,
                token_usage: {
                  prompt_tokens: 2200,
                  completion_tokens: 700,
                  total_tokens: 2900,
                },
                estimated_cost: 0.48,
                last_request_at: '2026-03-09T19:15:00Z',
              },
            ],
          }),
          {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          }
        );
      }

      if (url.startsWith('/api/v1/ai-models/model-1/interactions')) {
        return new Response(
          JSON.stringify({
            period_start: '2026-02-08T00:00:00Z',
            period_end: '2026-03-09T23:59:59Z',
            query: null,
            total: 1,
            limit: 10,
            offset: 0,
            items: [
              {
                api_usage_id: 'usage-1',
                timestamp: '2026-03-09T19:15:00Z',
                status_code: 200,
                outcome: 'success',
                endpoint: '/anthropic/v1/messages',
                method: 'POST',
                provider_name: 'Anthropic',
                model_alias: 'anthropic/claude-sonnet-4',
                flow_id: 'flow-1',
                flow_name: 'Triage Assistant',
                flow_execution_id: 'execution-1',
                runtime_session_id: 'runtime-session-1',
                session_source_type: 'flow_execution',
                session_source_id: 'execution-1',
                session_reference: 'session-abc123',
                runtime_principal_type: 'flow_execution',
                runtime_principal_id: 'execution-1',
                runtime_principal_name: 'Triage Assistant',
                estimated_cost: 0.12,
                token_usage: {
                  prompt_tokens: 300,
                  completion_tokens: 95,
                  total_tokens: 395,
                },
                excerpt:
                  'request.input: Summarize deployment risk response.output_text: Deployment risk summary completed',
                meta_data: {
                  source: 'gateway_interaction',
                },
              },
            ],
          }),
          {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          }
        );
      }

      if (url.includes('/openai/v1/responses')) {
        return new Response(
          JSON.stringify({
            output: [
              {
                content: [
                  {
                    text: 'Welcome acknowledged.',
                  },
                ],
              },
            ],
          }),
          {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          }
        );
      }

      if (url.endsWith('/api/v1/features')) {
        return new Response(JSON.stringify({ features: {} }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }

      if (url.endsWith('/api/v1/ai-models')) {
        return new Response(JSON.stringify([]), {
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

      if (url.includes('/api/v1/users')) {
        return new Response(
          JSON.stringify({
            users: [],
            total: 0,
            skip: 0,
            limit: 100,
          }),
          {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          }
        );
      }

      if (url.includes('/api/v1/auth/users/me')) {
        return new Response(
          JSON.stringify({
            email: 'test@preloop.ai',
            username: 'test',
          }),
          {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          }
        );
      }

      if (url.includes('/api/v1/agents')) {
        return new Response(
          JSON.stringify({
            items: [],
            total: 0,
          }),
          {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          }
        );
      }

      if (
        url.includes(
          '/api/v1/runtime-sessions/runtime-session-1/gateway-events'
        )
      ) {
        return new Response(JSON.stringify({ logs: [] }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }

      if (url.includes('/api/v1/runtime-sessions/runtime-session-1/activity')) {
        return new Response(JSON.stringify({ items: [] }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }

      return new Response(
        JSON.stringify({ detail: `Unhandled request: ${url}` }),
        {
          status: 500,
          headers: { 'Content-Type': 'application/json' },
        }
      );
    });

    connectStub = sinon.stub(unifiedWebSocketManager, 'connect').resolves();
    subscribeStub = sinon
      .stub(unifiedWebSocketManager, 'subscribe')
      .callsFake(() => () => undefined);
  });

  afterEach(() => {
    fetchStub.restore();
    connectStub.restore();
    subscribeStub.restore();
    localStorage.clear();
  });

  it('renders model observability summary, sessions, and interactions', async () => {
    const element = (await fixture(
      html`<ai-model-detail-view .modelId=${'model-1'}></ai-model-detail-view>`
    )) as AIModelDetailView;

    await waitUntil(
      () => !(element as any).loading,
      'AI model detail view did not finish loading',
      { timeout: 5000 }
    );
    await element.updateComplete;

    const content = element.shadowRoot?.textContent || '';
    expect(content).to.contain('Claude Sonnet Primary');
    expect(content).to.contain('Model Observability');
    expect(content).to.contain('Anthropic');
    expect(content).to.contain('claude-sonnet-4');
    expect(content).to.contain('preloop/anthropic/claude-sonnet-4');
    expect(content).to.contain('Mini Claw');
    expect(content).to.contain('Try Through Gateway');
    expect(content).to.contain('18');
    expect(content).to.contain('$1.42');
    expect(content).to.contain('8,500');
    expect(content).to.contain('Session Observer');

    const observer = element.shadowRoot?.querySelector(
      'preloop-session-observer'
    );
    expect(observer).to.exist;

    const agentLink = element.shadowRoot?.querySelector(
      'a[href="/console/agents/agent-1"]'
    );
    expect(agentLink).to.not.equal(null);

    const summaryCall = fetchStub
      .getCalls()
      .find((call) =>
        String(call.args[0]).startsWith('/api/v1/ai-models/model-1/summary')
      );
    const sessionsCall = fetchStub
      .getCalls()
      .find((call) =>
        String(call.args[0]).startsWith(
          '/api/v1/ai-models/model-1/runtime-sessions'
        )
      );
    const interactionsCall = fetchStub
      .getCalls()
      .find((call) =>
        String(call.args[0]).startsWith(
          '/api/v1/ai-models/model-1/interactions'
        )
      );

    expect(summaryCall).to.not.equal(undefined);
    expect(String(summaryCall?.args[0])).to.contain('start_date=');
    expect(String(summaryCall?.args[0])).to.contain('end_date=');
    expect(sessionsCall).to.not.equal(undefined);
    expect(String(sessionsCall?.args[0])).to.contain('limit=10');
    expect(interactionsCall).to.not.equal(undefined);
    expect(String(interactionsCall?.args[0])).to.contain('limit=10');
    expect(connectStub.callCount).to.be.at.least(1);
    expect(subscribeStub.callCount).to.be.at.least(4);
  });

  it('sends a test request through the gateway', async () => {
    const element = (await fixture(
      html`<ai-model-detail-view .modelId=${'model-1'}></ai-model-detail-view>`
    )) as AIModelDetailView;

    await waitUntil(
      () => !(element as any).loading,
      'AI model detail view did not finish loading',
      { timeout: 5000 }
    );
    await element.updateComplete;

    localStorage.setItem('accessToken', 'test-access-token');
    await (element as any).runValidationPrompt();

    await waitUntil(
      () =>
        fetchStub
          .getCalls()
          .some((call) =>
            String(call.args[0]).includes('/openai/v1/responses')
          ),
      'Gateway request was not sent',
      { timeout: 5000 }
    );
    await element.updateComplete;

    const requestCall = fetchStub
      .getCalls()
      .find((call) => String(call.args[0]).includes('/openai/v1/responses'));
    expect(requestCall).to.not.equal(undefined);
    expect(requestCall?.args[1]).to.deep.include({ method: 'POST' });
    expect(String((requestCall?.args[1] as RequestInit)?.body)).to.contain(
      'preloop/anthropic/claude-sonnet-4'
    );
    expect(element.shadowRoot?.textContent || '').to.contain(
      'Welcome acknowledged.'
    );
  });
});
