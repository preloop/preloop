import { fixture, html, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import './flow-execution-view';
import type { FlowExecutionView } from './flow-execution-view';

describe('FlowExecutionView', () => {
  let fetchStub: sinon.SinonStub;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');

    fetchStub = sinon.stub(window, 'fetch');
    fetchStub.callsFake(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();
        const method = (init?.method || 'GET').toUpperCase();

        if (
          url.includes('/api/v1/flows/executions/exec-1/gateway-events') &&
          method === 'GET'
        ) {
          return new Response(
            JSON.stringify({
              logs: [
                {
                  execution_id: 'exec-1',
                  timestamp: '2026-03-09T10:01:00Z',
                  type: 'model_gateway_call',
                  payload: {
                    api_usage_id: 'usage-1',
                    model_alias: 'openai/gpt-5',
                    provider_name: 'OpenAI',
                    outcome: 'success',
                    estimated_cost: 0.1,
                    total_tokens: 1234,
                    prompt_tokens: 1000,
                    completion_tokens: 234,
                    duration_ms: 820,
                    status_code: 200,
                    method: 'POST',
                    endpoint: '/v1/responses',
                    endpoint_kind: 'responses',
                    finish_reason: 'stop',
                    upstream_request_id: 'req_123',
                    capture_policy: {
                      content_capture_enabled: true,
                      max_preview_chars: 120,
                      sensitive_fields_redacted: true,
                      content_redacted: false,
                      content_truncated: false,
                      conversation_preview_available: true,
                    },
                    conversation_preview: {
                      messages: [
                        {
                          source: 'request',
                          role: 'user',
                          text: 'Summarize this issue',
                          redacted: false,
                          truncated: false,
                          original_length: 20,
                        },
                        {
                          source: 'response',
                          role: 'assistant',
                          text: 'Done',
                          redacted: false,
                          truncated: false,
                          original_length: 4,
                        },
                      ],
                      metadata: {
                        message_count: 2,
                        request_message_count: 1,
                        response_message_count: 1,
                        has_redacted_content: false,
                        has_truncated_content: false,
                      },
                    },
                    request: { model: 'gpt-5', input: 'Summarize this issue' },
                    response: { id: 'resp_123', output_text: 'Done' },
                  },
                },
              ],
              source: 'database',
            }),
            {
              status: 200,
              headers: { 'Content-Type': 'application/json' },
            }
          );
        }

        if (
          url.includes('/api/v1/flows/executions/exec-1/logs') &&
          method === 'GET'
        ) {
          return new Response(
            JSON.stringify({
              logs: [],
              source: 'database',
            }),
            {
              status: 200,
              headers: { 'Content-Type': 'application/json' },
            }
          );
        }

        if (
          url.endsWith('/api/v1/flows/executions/exec-1/metrics') &&
          method === 'GET'
        ) {
          return new Response(
            JSON.stringify({
              tool_calls: 0,
              api_requests: 1,
              token_usage: {
                total_tokens: 1234,
                input_tokens: 1000,
                output_tokens: 234,
              },
              estimated_cost: 0.1,
              has_pricing: true,
            }),
            {
              status: 200,
              headers: { 'Content-Type': 'application/json' },
            }
          );
        }

        if (
          url.endsWith('/api/v1/flows/executions/exec-1') &&
          method === 'GET'
        ) {
          return new Response(
            JSON.stringify({
              id: 'exec-1',
              flow_id: 'flow-1',
              status: 'COMPLETED',
              start_time: '2026-03-09T10:00:00Z',
              end_time: '2026-03-09T10:02:00Z',
              trigger_event_details: {
                source: 'github',
                type: 'issue_comment',
              },
            }),
            {
              status: 200,
              headers: { 'Content-Type': 'application/json' },
            }
          );
        }

        if (
          url.includes(
            '/api/v1/flows/executions/exec-running/gateway-events'
          ) &&
          method === 'GET'
        ) {
          return new Response(
            JSON.stringify({
              logs: [],
              source: 'database',
            }),
            {
              status: 200,
              headers: { 'Content-Type': 'application/json' },
            }
          );
        }

        if (
          url.includes('/api/v1/flows/executions/exec-running/logs') &&
          method === 'GET'
        ) {
          return new Response(
            JSON.stringify({
              logs: [],
              source: 'database',
            }),
            {
              status: 200,
              headers: { 'Content-Type': 'application/json' },
            }
          );
        }

        if (
          url.endsWith('/api/v1/flows/executions/exec-running') &&
          method === 'GET'
        ) {
          return new Response(
            JSON.stringify({
              id: 'exec-running',
              flow_id: 'flow-running',
              status: 'RUNNING',
              start_time: '2026-03-09T10:00:00Z',
              trigger_event_details: {
                source: 'github',
                type: 'issue_comment',
              },
              tool_calls_count: 3,
              mcp_usage_logs: [
                {
                  timestamp: '2026-03-09T10:00:10Z',
                  tool_name: 'search_issues',
                },
                {
                  timestamp: '2026-03-09T10:00:20Z',
                  tool_name: 'get_issue',
                },
              ],
            }),
            {
              status: 200,
              headers: { 'Content-Type': 'application/json' },
            }
          );
        }

        if (url.endsWith('/api/v1/flows/flow-1') && method === 'GET') {
          return new Response(
            JSON.stringify({
              id: 'flow-1',
              name: 'Gateway Demo',
              agent_type: 'codex',
              trigger_event_source: 'github',
              trigger_event_type: 'issue_comment',
            }),
            {
              status: 200,
              headers: { 'Content-Type': 'application/json' },
            }
          );
        }

        if (url.endsWith('/api/v1/flows/flow-running') && method === 'GET') {
          return new Response(
            JSON.stringify({
              id: 'flow-running',
              name: 'Running Flow',
              agent_type: 'codex',
              trigger_event_source: 'github',
              trigger_event_type: 'issue_comment',
            }),
            {
              status: 200,
              headers: { 'Content-Type': 'application/json' },
            }
          );
        }

        return new Response(
          JSON.stringify({ detail: `Unhandled request: ${method} ${url}` }),
          {
            status: 500,
            headers: { 'Content-Type': 'application/json' },
          }
        );
      }
    );
  });

  afterEach(() => {
    fetchStub.restore();
    localStorage.clear();
  });

  it('renders execution-scoped gateway events with payload details', async () => {
    const element = (await fixture(
      html`<flow-execution-view></flow-execution-view>`
    )) as FlowExecutionView;

    element.executionId = 'exec-1';
    await element.updateComplete;

    await waitUntil(
      () =>
        (element as any).execution?.id === 'exec-1' &&
        !(element as any).isLoading,
      'Execution view did not finish loading'
    );
    await element.updateComplete;

    (element as any).loadGatewayEvents();
    await waitUntil(
      () => (element as any).gatewayEvents?.length === 1,
      'Gateway events did not load'
    );
    await element.updateComplete;

    const gatewayEvents = Array.from(
      element.shadowRoot?.querySelectorAll('preloop-gateway-event') || []
    );
    const gatewayContent = gatewayEvents
      .map((el) => el.shadowRoot?.textContent || '')
      .join(' ');

    const content = (
      (element.shadowRoot?.textContent || '') +
      ' ' +
      gatewayContent
    ).replace(/\s+/g, ' ');

    expect(content).to.contain('Gateway Events');
    expect(content).to.contain('openai/gpt-5');
    expect(content).to.contain('OpenAI');
    expect(content).to.contain('Success');
    expect(content).to.contain('$0.10');
    expect(content).to.contain('1,234');
    expect(content).to.contain('Capture Policy');
    expect(content).to.contain('Conversation Preview');
    expect(content).to.contain('Preview captured');
    expect(content).to.contain('Request User');
    expect(content).to.contain('Response Assistant');
    expect(content).to.contain('120 chars');
    expect(content).to.contain('"upstream_request_id": "req_123"');

    const gatewayEventsCalls = fetchStub
      .getCalls()
      .filter((call) =>
        String(call.args[0]).endsWith(
          '/api/v1/flows/executions/exec-1/gateway-events'
        )
      );
    expect(gatewayEventsCalls.length).to.equal(1);
  });

  it('updates execution metrics from live gateway events', async () => {
    const element = (await fixture(
      html`<flow-execution-view></flow-execution-view>`
    )) as FlowExecutionView;

    element.executionId = 'exec-1';
    await element.updateComplete;
    await waitUntil(
      () =>
        (element as any).execution?.id === 'exec-1' &&
        !(element as any).isLoading,
      'Execution view did not finish loading'
    );

    (element as any).loadGatewayEvents();
    await waitUntil(
      () => (element as any).gatewayEvents?.length === 1,
      'Gateway events did not load'
    );

    (element as any).handleWebSocketMessage({
      execution_id: 'exec-1',
      timestamp: '2026-03-09T10:03:00Z',
      type: 'model_gateway_call',
      payload: {
        api_usage_id: 'usage-live-1',
        total_tokens: 4321,
        estimated_cost: 0.245,
        prompt_tokens: 4000,
        completion_tokens: 321,
        outcome: 'success',
      },
    });

    await element.updateComplete;

    expect((element as any).gatewayEvents).to.have.length(2);
    expect((element as any).totalTokens).to.equal(5555);
    expect((element as any).budgetUsed).to.equal(0.345);
    expect((element as any).hasPricing).to.equal(true);
  });

  it('hydrates tool call metrics from the execution record on reload', async () => {
    const element = (await fixture(
      html`<flow-execution-view></flow-execution-view>`
    )) as FlowExecutionView;

    element.executionId = 'exec-running';
    await element.updateComplete;

    await waitUntil(
      () =>
        (element as any).execution?.id === 'exec-running' &&
        !(element as any).isLoading,
      'Running execution view did not finish loading'
    );

    expect((element as any).toolCalls).to.equal(3);
    expect(
      (element as any).logs.some((log: any) => log.type === 'mcp_call')
    ).to.equal(true);
    const content = (element.shadowRoot?.textContent || '').replace(
      /\s+/g,
      ' '
    );
    expect(content).to.contain('Tool Activity');
    expect(content).to.contain('search_issues');
    expect(content).to.contain('get_issue');
  });

  it('does not claim pricing when a stored cost is a placeholder zero', async () => {
    // Customer-reported: OpenRouter-routed flows metered 28M tokens but the
    // model was unpriceable, so estimated_cost persisted as 0 and the UI
    // announced "$0.00" for real spend. A zero cost with tokens spent must
    // fall back to the token display instead.
    const element = await fixture<FlowExecutionView>(
      html`<flow-execution-view></flow-execution-view>`
    );
    (element as any).execution = {
      id: 'exec-unpriced',
      total_tokens: 28553143,
      estimated_cost: 0,
      tool_calls_count: 3,
      mcp_usage_logs: [],
    };
    (element as any).gatewayEvents = [];

    (element as any).hydrateMetricsFromExecution();

    expect((element as any).hasPricing).to.equal(false);
    expect((element as any).totalTokens).to.equal(28553143);
  });

  it('treats a null cost with tokens as unpriced, not free', async () => {
    const element = await fixture<FlowExecutionView>(
      html`<flow-execution-view></flow-execution-view>`
    );
    (element as any).execution = {
      id: 'exec-null-cost',
      total_tokens: 1000,
      estimated_cost: null,
      tool_calls_count: 0,
      mcp_usage_logs: [],
    };
    (element as any).gatewayEvents = [];

    (element as any).hydrateMetricsFromExecution();

    expect((element as any).hasPricing).to.equal(false);
  });

  describe('timing card duration', () => {
    const timingSubtext = (element: FlowExecutionView) => {
      const cards = Array.from(
        element.shadowRoot?.querySelectorAll('sl-card') || []
      );
      const timing = cards.find((card) =>
        (card.textContent || '').includes('Timing')
      );
      return (timing?.querySelector('.summary-subtext')?.textContent || '')
        .replace(/\s+/g, ' ')
        .trim();
    };

    async function load(executionId: string) {
      const element = (await fixture(
        html`<flow-execution-view></flow-execution-view>`
      )) as FlowExecutionView;
      element.executionId = executionId;
      await element.updateComplete;
      await waitUntil(
        () =>
          (element as any).execution?.id === executionId &&
          !(element as any).isLoading,
        `Execution view did not finish loading ${executionId}`
      );
      await element.updateComplete;
      return element;
    }

    it('shows the finished duration for a completed execution', async () => {
      const element = await load('exec-1');

      expect(timingSubtext(element)).to.equal('2m 0s');
    });

    it('shows a live elapsed duration for a running execution', async () => {
      const element = await load('exec-running');

      expect(timingSubtext(element)).to.match(/^Running · /);
    });

    it('ticks the elapsed duration while the execution runs', async () => {
      const clock = sinon.useFakeTimers({
        now: new Date('2026-03-09T10:00:10Z'),
        toFake: ['setInterval', 'clearInterval', 'Date'],
      });

      try {
        const element = await load('exec-running');
        expect(timingSubtext(element)).to.equal('Running · 10s');

        clock.tick(2000);
        await element.updateComplete;

        expect(timingSubtext(element)).to.equal('Running · 12s');
      } finally {
        clock.restore();
      }
    });

    it('clears the tick interval when disconnected', async () => {
      const element = await load('exec-running');
      const intervalId = (element as any).durationTickIntervalId;
      expect(intervalId).to.be.a('number');

      const clearSpy = sinon.spy(window, 'clearInterval');
      try {
        element.remove();
        await element.updateComplete;

        expect(clearSpy.calledWith(intervalId)).to.equal(true);
        expect((element as any).durationTickIntervalId).to.equal(undefined);
      } finally {
        clearSpy.restore();
      }
    });
  });

  it('does not treat a zero-cost gateway event as priced', async () => {
    const element = await fixture<FlowExecutionView>(
      html`<flow-execution-view></flow-execution-view>`
    );
    (element as any).gatewayEvents = [
      {
        execution_id: 'exec-1',
        timestamp: '2026-08-08T10:00:00Z',
        type: 'model_gateway_call',
        payload: {
          api_usage_id: 'usage-unpriced',
          total_tokens: 5000,
          estimated_cost: 0,
          outcome: 'success',
        },
      },
    ];

    (element as any).applyGatewayMetricsFromEvents();

    expect((element as any).totalTokens).to.equal(5000);
    expect((element as any).hasPricing).to.equal(false);
  });
});
