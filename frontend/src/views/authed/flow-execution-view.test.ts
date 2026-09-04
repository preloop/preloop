import { fixture, html, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import './flow-execution-view';
import type { FlowExecutionView } from './flow-execution-view';
import { unifiedWebSocketManager } from '../../services/unified-websocket-manager';

describe('FlowExecutionView', () => {
  let fetchStub: sinon.SinonStub;
  /** Held to keep the metadata-only gateway fetch in flight during a test. */
  let gatewayEventsGate: Promise<void> | null;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
    gatewayEventsGate = null;

    fetchStub = sinon.stub(window, 'fetch');
    fetchStub.callsFake(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();
        const method = (init?.method || 'GET').toUpperCase();

        if (
          url.includes('/api/v1/flows/executions/exec-1/gateway-events') &&
          method === 'GET'
        ) {
          if (url.includes('metadata_only=true') && gatewayEventsGate) {
            await gatewayEventsGate;
          }
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
                // The detail endpoint returns the snapshot, subject included;
                // only the list endpoints project it into its own column.
                _subject: {
                  text: 'preloop/preloop #78 · Pull Request Updated',
                  url: 'https://github.com/preloop/preloop/pull/78',
                },
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
    window.history.replaceState({}, '', window.location.pathname);
  });

  /** Text of one cell of the hairline summary strip. */
  const stripValue = (element: FlowExecutionView, testId: string) =>
    (
      element.shadowRoot?.querySelector(`[data-testid="${testId}"]`)
        ?.textContent || ''
    )
      .replace(/\s+/g, ' ')
      .trim();

  const pageText = (element: FlowExecutionView) =>
    (element.shadowRoot?.textContent || '').replace(/\s+/g, ' ');

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

  it('puts what the run was about under the flow name (wave 4)', async () => {
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

    // The title is the flow name, shared by every run of it; the subject is
    // what this run was about, so it sits under the title and links out.
    const line = element.shadowRoot!.querySelector('.execution-subject-line')!;
    const link = line.querySelector('a')!;
    expect(link.textContent).to.contain(
      'preloop/preloop #78 · Pull Request Updated'
    );
    expect(link.getAttribute('href')).to.equal(
      'https://github.com/preloop/preloop/pull/78'
    );
    expect(link.getAttribute('target')).to.equal('_blank');
    expect(link.getAttribute('rel')).to.equal('noopener noreferrer');
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

    // Wave 7: the Timeline is the default tab and merges gateway requests,
    // so the events load with the page instead of on a tab click.
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

    // The events now sit in the Timeline stream rather than under a tab of
    // their own, so the page no longer names them.
    expect(content).to.not.contain('Gateway Events');
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
        String(call.args[0]).includes(
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
    // Wave 7: tool calls are entries in the Timeline, not a boxed card, and
    // the strip counts them.
    expect(content).to.not.contain('Tool Activity');
    expect(content).to.contain('search_issues');
    expect(content).to.contain('get_issue');
    expect(stripValue(element, 'strip-tools')).to.equal('3');
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

  describe('summary strip duration', () => {
    const timingSubtext = (element: FlowExecutionView) =>
      stripValue(element, 'strip-duration');

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

  describe('wave 7 execution page', () => {
    it('replaces the five stat cards with one hairline summary strip', async () => {
      const element = await load('exec-1');

      // The cards are gone.
      expect(element.shadowRoot!.querySelectorAll('sl-card').length).to.equal(
        0
      );

      const strip = element.shadowRoot!.querySelector(
        '[data-testid="summary-strip"]'
      )!;
      expect(strip).to.exist;
      const labels = Array.from(strip.querySelectorAll('.strip-label')).map(
        (label) => (label.textContent || '').trim()
      );
      expect(labels).to.eql([
        'Started',
        'Duration',
        'Ran on',
        'Model',
        'Tokens',
        '$ est.',
        'Tools',
        'Agent',
        'Session',
        'Execution',
      ]);

      // Nothing the cards carried was dropped: the timing, the cost, the
      // agent and the execution id all have a place in the row.
      expect(stripValue(element, 'strip-duration')).to.equal('2m 0s');
      expect(stripValue(element, 'strip-tokens')).to.equal('1,234');
      expect(stripValue(element, 'strip-cost')).to.equal('$0.10');
      expect(pageText(element)).to.contain('codex');
      expect(
        strip.querySelector('sl-copy-button')?.getAttribute('value')
      ).to.equal('exec-1');
    });

    it('adds a Failure item to the strip only when the run has a category', async () => {
      const element = await load('exec-1');
      const labelsOf = () =>
        Array.from(
          element
            .shadowRoot!.querySelector('[data-testid="summary-strip"]')!
            .querySelectorAll('.strip-label')
        ).map((label) => (label.textContent || '').trim());

      // A run with no category: the strip is the row it always was.
      expect(labelsOf()).to.not.contain('Failure');

      (element as any).execution = {
        ...(element as any).execution,
        status: 'FAILED',
        failure_category: 'model_quota',
      };
      await element.updateComplete;

      expect(labelsOf()).to.contain('Failure');
      const chip = element.shadowRoot!.querySelector(
        '[data-testid="strip-failure-category"] sl-badge'
      )!;
      expect(chip.textContent!.trim()).to.equal('Model quota');
      expect(chip.getAttribute('variant')).to.equal('neutral');
      expect(chip.closest('sl-tooltip')!.getAttribute('content')).to.contain(
        'quota'
      );
    });

    it('names the hosted executor when the run has no private runner', async () => {
      const element = await load('exec-1');

      expect(stripValue(element, 'strip-runner')).to.contain('Preloop hosted');
      const badge = element.shadowRoot!.querySelector(
        '[data-testid="strip-runner"] [data-testid="runner-kind-badge"]'
      )!;
      expect(badge.textContent!.trim()).to.equal('Hosted');
      expect(badge.getAttribute('data-runner-kind')).to.equal('hosted');
      expect(
        element.shadowRoot!.querySelector(
          '[data-testid="strip-runner"] a[href="/console/settings/runners"]'
        )
      ).to.equal(null);
    });

    it('names a private runner, its pool, and links to Runners', async () => {
      const element = await load('exec-1');
      (element as any).execution = {
        ...(element as any).execution,
        runner: {
          kind: 'private',
          id: '11111111-1111-1111-1111-111111111111',
          name: 'Office Mac',
          pool: 'gpu',
        },
      };
      await element.updateComplete;

      expect(stripValue(element, 'strip-runner')).to.contain('Office Mac');
      expect(stripValue(element, 'strip-runner')).to.contain('gpu');
      const badge = element.shadowRoot!.querySelector(
        '[data-testid="strip-runner"] [data-testid="runner-kind-badge"]'
      )!;
      expect(badge.textContent!.trim()).to.equal('Private');
      expect(badge.getAttribute('data-runner-kind')).to.equal('private');
      const link = element.shadowRoot!.querySelector(
        '[data-testid="strip-runner"] a[href="/console/settings/runners"]'
      );
      expect(link).to.exist;
      expect(link!.textContent!.trim()).to.equal('Office Mac');
    });

    it('names the model that served the run in the strip', async () => {
      const element = await load('exec-1');

      // exec-1 predates the projection, so the page falls back to counting
      // the gateway events it already loaded.
      expect(stripValue(element, 'strip-model')).to.contain('openai/gpt-5');
      expect(stripValue(element, 'strip-model')).to.contain('OpenAI');
    });

    it('prefers the API model projection over the gateway events', async () => {
      const element = await load('exec-1');
      (element as any).execution = {
        ...(element as any).execution,
        model_alias: 'deepseek/deepseek-v4-pro',
        provider_name: 'DeepSeek',
        models_used: [
          {
            model_alias: 'deepseek/deepseek-v4-pro',
            provider_name: 'DeepSeek',
            request_count: 9,
          },
          {
            model_alias: 'openai/gpt-5',
            provider_name: 'OpenAI',
            request_count: 2,
          },
        ],
      };
      await element.updateComplete;

      const model = stripValue(element, 'strip-model');
      expect(model).to.contain('deepseek/deepseek-v4-pro');
      expect(model).to.contain('DeepSeek');
      expect(model).to.contain('+1');
    });

    it('shows the status as a soft chip with a live dot while running', async () => {
      const finished = await load('exec-1');
      const finishedChip = finished.shadowRoot!.querySelector(
        '.status-pill sl-badge'
      )!;
      expect(finishedChip.textContent!.trim()).to.equal('Completed');
      expect(finishedChip.classList.contains('solid')).to.equal(false);
      expect(finished.shadowRoot!.querySelector('.status-dot')).to.equal(null);

      const running = await load('exec-running');
      expect(
        running
          .shadowRoot!.querySelector('.status-pill sl-badge')!
          .textContent!.trim()
      ).to.equal('Running');
      expect(running.shadowRoot!.querySelector('.status-dot')).to.exist;
    });

    it('offers the five tabs with Timeline first', async () => {
      const element = await load('exec-1');

      const tabs = Array.from(
        element.shadowRoot!.querySelectorAll('sl-tab')
      ).map((tab) => (tab.textContent || '').trim());
      expect(tabs).to.eql([
        'Timeline',
        'Output',
        'Transcript',
        'Logs',
        'Input',
      ]);
      expect((element as any).activeTab).to.equal('timeline');
    });

    it('opens the tab named in the URL and remembers the last one', async () => {
      window.history.replaceState(
        {},
        '',
        `${window.location.pathname}?tab=logs`
      );

      const element = await load('exec-1');
      expect((element as any).activeTab).to.equal('logs');

      // Switching tabs writes both the URL and the remembered choice, so a
      // reload and a shared link both land where the operator was.
      (element as any).handleTabShow(
        new CustomEvent('sl-tab-show', { detail: { name: 'output' } })
      );
      await element.updateComplete;

      expect(new URLSearchParams(window.location.search).get('tab')).to.equal(
        'output'
      );
      expect(localStorage.getItem('preloop.execution-view.tab')).to.equal(
        'output'
      );

      window.history.replaceState({}, '', window.location.pathname);
      const reopened = await load('exec-1');
      expect((reopened as any).activeTab).to.equal('output');
    });

    it('merges gateway calls, tool calls and status changes into one stream', async () => {
      const element = await load('exec-running');

      const rows = Array.from(
        element.shadowRoot!.querySelectorAll('.timeline-stream .timeline-row')
      );
      const text = rows.map((row) => (row.textContent || '').trim());
      expect(text[0]).to.contain('Run started');
      expect(text.join(' ')).to.contain('search_issues');
      expect(text.join(' ')).to.contain('get_issue');

      // A tool call is one entry, not a tool row plus a log line repeating it.
      expect(
        element.shadowRoot!.querySelectorAll('.log-group-toggle').length
      ).to.equal(0);
    });

    it('folds consecutive log lines into an expandable group', async () => {
      const element = await load('exec-running');
      (element as any).logs = [
        {
          execution_id: 'exec-running',
          timestamp: '2026-03-09T10:00:30Z',
          type: 'agent_log_line',
          payload: { content: 'cloning repository' },
        },
        {
          execution_id: 'exec-running',
          timestamp: '2026-03-09T10:00:31Z',
          type: 'agent_log_line',
          payload: { content: 'installing dependencies' },
        },
      ];
      await element.updateComplete;

      const toggle = element.shadowRoot!.querySelector(
        '.log-group-toggle'
      ) as HTMLButtonElement;
      expect(toggle.textContent!.replace(/\s+/g, ' ')).to.contain(
        '2 log lines'
      );
      expect(element.shadowRoot!.querySelector('.log-group-lines')).to.equal(
        null
      );

      toggle.click();
      await element.updateComplete;

      const lines = element.shadowRoot!.querySelector('.log-group-lines')!;
      expect(lines.textContent).to.contain('cloning repository');
      expect(lines.textContent).to.contain('installing dependencies');
    });

    it('pauses and resumes following the live stream', async () => {
      const element = await load('exec-running');

      const follow = element.shadowRoot!.querySelector(
        '[data-testid="follow-live"]'
      ) as HTMLElement;
      expect(follow.textContent!.trim()).to.equal('Following live');
      expect((element as any).followLive).to.equal(true);

      follow.click();
      await element.updateComplete;

      expect((element as any).followLive).to.equal(false);
      expect(
        element
          .shadowRoot!.querySelector('[data-testid="follow-live"]')!
          .textContent!.trim()
      ).to.equal('Paused');

      (element.shadowRoot!.querySelector(
        '[data-testid="follow-live"]'
      ) as HTMLElement)!.click();
      await element.updateComplete;

      expect((element as any).followLive).to.equal(true);
    });

    it('scrolls the stream inside itself only while the run is live', async () => {
      const running = await load('exec-running');
      expect(
        running
          .shadowRoot!.querySelector('[data-testid="timeline-stream"]')!
          .classList.contains('is-live')
      ).to.equal(true);

      const finished = await load('exec-1');
      expect(
        finished
          .shadowRoot!.querySelector('[data-testid="timeline-stream"]')!
          .classList.contains('is-live')
      ).to.equal(false);
      expect(
        finished.shadowRoot!.querySelector('[data-testid="follow-live"]')
      ).to.equal(null);
    });

    it('offers a jump back to the newest entry after scrolling away', async () => {
      const element = await load('exec-running');
      expect(
        element.shadowRoot!.querySelector('[data-testid="jump-latest"]')
      ).to.equal(null);

      (element as any).handleTimelineScroll({
        currentTarget: { scrollHeight: 2000, scrollTop: 0, clientHeight: 500 },
      });
      await element.updateComplete;

      expect((element as any).followLive).to.equal(false);
      const jump = element.shadowRoot!.querySelector(
        '[data-testid="jump-latest"]'
      ) as HTMLElement;
      expect(jump).to.exist;

      jump.click();
      await element.updateComplete;
      expect((element as any).followLive).to.equal(true);
    });

    it('keeps the trigger payload and resolved prompt on the Input tab', async () => {
      const element = await load('exec-1');
      const input = element.shadowRoot!.querySelector(
        'sl-tab-panel[name="input"]'
      )!;

      expect(input.textContent).to.contain('Trigger event');
      expect(input.querySelector('json-tree')).to.exist;
      // The accordions are gone; the payload lives on its own tab now.
      expect(
        element.shadowRoot!.querySelectorAll('sl-details').length
      ).to.equal(0);
    });

    it('keeps non-model log rows out of the timeline as event cards', async () => {
      // The gateway-events endpoint returns every log row of the run, and the
      // plain ones are the same rows the logs endpoint already returned.
      const element = await load('exec-1');
      (element as any).gatewayEvents = [
        ...(element as any).gatewayEvents,
        {
          id: 'evt-status',
          execution_id: 'exec-1',
          timestamp: '2026-03-09T10:01:30Z',
          type: 'status_update',
          payload: { status: 'RUNNING' },
        },
      ];
      await element.updateComplete;

      expect(
        element.shadowRoot!.querySelectorAll(
          '.timeline-stream preloop-gateway-event'
        ).length
      ).to.equal(1);
    });

    it('loads full gateway payloads when the transcript is opened', async () => {
      const element = await load('exec-1');
      const gatewayCalls = () =>
        fetchStub
          .getCalls()
          .map((call) => String(call.args[0]))
          .filter((url) => url.includes('/gateway-events'));

      // The first paint asks for metadata only: it just feeds the timeline.
      expect(gatewayCalls()).to.have.length(1);
      expect(gatewayCalls()[0]).to.contain('metadata_only=true');

      (element as any).handleTabShow({ detail: { name: 'transcript' } });
      await waitUntil(
        () => gatewayCalls().length === 2,
        'Transcript did not reload the events with full payloads'
      );
      expect(gatewayCalls()[1]).to.not.contain('metadata_only');

      // The upgrade reads as loading, not as "nothing was captured".
      (element as any).isLoadingGatewayEvents = true;
      (element as any).gatewayEventsFullLoaded = false;
      await element.updateComplete;
      expect(
        (element.shadowRoot!.querySelector('session-chat-view') as any).loading
      ).to.equal(true);
      (element as any).isLoadingGatewayEvents = false;
      (element as any).gatewayEventsFullLoaded = true;
      await element.updateComplete;

      // Reopening it does not refetch.
      (element as any).handleTabShow({ detail: { name: 'timeline' } });
      (element as any).handleTabShow({ detail: { name: 'transcript' } });
      await element.updateComplete;
      expect(gatewayCalls()).to.have.length(2);
    });

    it('bounds the model output summary in a scrollable block', async () => {
      const element = await load('exec-1');
      (element as any).execution = {
        ...(element as any).execution,
        model_output_summary: 'line one\nline two\nline three',
      };
      await element.updateComplete;

      const summary = element.shadowRoot!.querySelector(
        'pre[data-testid="output-summary"]'
      )!;
      expect(summary).to.exist;
      expect(summary.textContent).to.contain('line three');
      expect(
        element.shadowRoot!.querySelector(
          'sl-tab-panel[name="output"] sl-copy-button'
        )
      ).to.exist;
    });

    it('reads the conversation through the shared session view', async () => {
      const element = await load('exec-1');
      const transcript = element.shadowRoot!.querySelector(
        'session-chat-view'
      ) as any;

      expect(transcript).to.exist;
      expect(transcript.events).to.have.length(1);
    });

    it('shows the first error line under the strip and the full error in Output', async () => {
      const element = await load('exec-1');
      (element as any).execution = {
        ...(element as any).execution,
        status: 'FAILED',
        error_message:
          'Agent exited with code 1\nTraceback (most recent call last):\n  File "run.py"',
      };
      await element.updateComplete;

      const line = element.shadowRoot!.querySelector(
        '[data-testid="error-line"]'
      )!;
      expect(line.textContent!.trim()).to.equal('Agent exited with code 1');
      expect(line.getAttribute('title')).to.contain('Traceback');

      const output = element.shadowRoot!.querySelector(
        'sl-tab-panel[name="output"]'
      )!;
      expect(output.textContent).to.contain(
        'Traceback (most recent call last)'
      );
    });

    it('searches the raw log lines in place', async () => {
      const element = await load('exec-running');
      (element as any).logs = [
        {
          execution_id: 'exec-running',
          timestamp: '2026-03-09T10:00:30Z',
          type: 'agent_log_line',
          payload: { content: 'cloning repository' },
        },
        {
          execution_id: 'exec-running',
          timestamp: '2026-03-09T10:00:31Z',
          type: 'agent_log_line',
          payload: { content: 'installing dependencies' },
        },
      ];
      (element as any).logSearchQuery = 'cloning';
      await element.updateComplete;

      const container = element.shadowRoot!.querySelector('.log-container')!;
      expect(container.textContent).to.contain('cloning repository');
      expect(container.textContent).to.not.contain('installing dependencies');
    });

    it('copies the execution and session ids from the header kebab', async () => {
      const element = await load('exec-1');
      const items = Array.from(
        element.shadowRoot!.querySelectorAll('.header-actions sl-menu-item')
      ).map((item) => (item.textContent || '').trim());

      // "View flow" is in the menu for the phone layout, where the header
      // has no room for it; CSS hides it on a wide screen.
      expect(items).to.eql([
        'View flow',
        'Copy execution id',
        'Copy session id',
      ]);
      // exec-1 has no session reference, so that item cannot be clicked.
      expect(
        element
          .shadowRoot!.querySelectorAll('.header-actions sl-menu-item')[2]
          .hasAttribute('disabled')
      ).to.equal(true);
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

  describe('wave 7 review fixes', () => {
    it('puts the buffer flush and the scroll follower back after a reconnect', async () => {
      let notifyState: ((state: string) => void) | null = null;
      const stateStub = sinon
        .stub(unifiedWebSocketManager, 'onStateChange')
        .callsFake((callback: (state: any) => void) => {
          notifyState = callback;
          return () => {};
        });

      try {
        const element = await load('exec-running');
        const intervals = () => ({
          buffer: (element as any).bufferFlushInterval,
          scroll: (element as any).autoScrollInterval,
        });

        expect(intervals().buffer, 'buffer flush runs while live').to.not.be
          .undefined;
        expect(intervals().scroll, 'scroll follower runs while live').to.not.be
          .undefined;
        expect(notifyState, 'the view tracks connection state').to.not.be.null;

        notifyState!('disconnected');
        expect(intervals().buffer).to.be.undefined;
        expect(intervals().scroll).to.be.undefined;

        // The drop used to be one way: lines kept arriving into a buffer
        // nothing flushed, and the view stayed frozen for the session.
        notifyState!('connected');
        expect(intervals().buffer, 'buffer flush restarted').to.not.be
          .undefined;
        expect(intervals().scroll, 'scroll follower restarted').to.not.be
          .undefined;
      } finally {
        stateStub.restore();
      }
    });

    it('leaves the buffer alone when the run already finished', async () => {
      let notifyState: ((state: string) => void) | null = null;
      const stateStub = sinon
        .stub(unifiedWebSocketManager, 'onStateChange')
        .callsFake((callback: (state: any) => void) => {
          notifyState = callback;
          return () => {};
        });

      try {
        const element = await load('exec-running');
        (element as any).execution = {
          ...(element as any).execution,
          status: 'COMPLETED',
        };
        notifyState!('disconnected');
        notifyState!('connected');

        expect((element as any).bufferFlushInterval).to.be.undefined;
        expect((element as any).autoScrollInterval).to.be.undefined;
      } finally {
        stateStub.restore();
      }
    });

    it('adds one scroll listener per checker and takes it back off', async () => {
      const element = await load('exec-running');
      const container = element.shadowRoot!.querySelector(
        '.log-container'
      ) as HTMLElement;
      expect(container, 'the logs tab has its container').to.exist;

      // Start from nothing attached so the count below is only what the
      // restarts did.
      (element as any).stopAutoScrollChecker();
      const added = sinon.spy(container, 'addEventListener');
      const removed = sinon.spy(container, 'removeEventListener');
      try {
        (element as any).startAutoScrollChecker();
        (element as any).startAutoScrollChecker();
        (element as any).startAutoScrollChecker();
        (element as any).stopAutoScrollChecker();

        const scrollAdds = added
          .getCalls()
          .filter((call) => call.args[0] === 'scroll');
        const scrollRemoves = removed
          .getCalls()
          .filter((call) => call.args[0] === 'scroll');
        // Every restart used to leave its listener behind, so the handler ran
        // once per reconnect for the life of the page. Now each one is added
        // and taken off in a pair, and the last stop leaves none.
        expect(scrollAdds.length).to.be.at.least(3);
        expect(scrollRemoves.length).to.equal(scrollAdds.length);
        // One bound reference throughout, or nothing could be removed.
        expect(scrollAdds[0].args[1]).to.equal(scrollRemoves[0].args[1]);
        expect((element as any).scrollListenerTarget).to.be.undefined;
      } finally {
        added.restore();
        removed.restore();
      }
    });

    it('drops the connection-state listener when the view goes away', async () => {
      const unsubscribeState = sinon.spy();
      const stateStub = sinon
        .stub(unifiedWebSocketManager, 'onStateChange')
        .returns(unsubscribeState);

      try {
        const element = await load('exec-running');
        expect(unsubscribeState.called).to.be.false;

        element.remove();
        expect(unsubscribeState.calledOnce, 'state listener released').to.be
          .true;
      } finally {
        stateStub.restore();
      }
    });

    it('says the copy failed instead of claiming the logs are on the clipboard', async () => {
      const element = await load('exec-1');
      (element as any).logs = [
        {
          execution_id: 'exec-1',
          timestamp: '2026-03-09T10:00:00Z',
          type: 'agent_log_line',
          payload: { content: 'first line' },
        },
      ];
      const toasts: Array<{ message: string; variant?: string }> = [];
      element.addEventListener('show-toast', (event) => {
        toasts.push((event as CustomEvent).detail);
      });

      const writeText = sinon
        .stub(navigator.clipboard, 'writeText')
        .rejects(new Error('Write permission denied'));
      try {
        await (element as any).copyAllLogs();
        expect(toasts).to.eql([
          {
            message: 'Could not copy the logs to the clipboard.',
            variant: 'danger',
          },
        ]);

        writeText.resolves();
        await (element as any).copyAllLogs();
        expect(toasts[1]).to.eql({ message: 'Logs copied to clipboard!' });
      } finally {
        writeText.restore();
      }
    });

    it('upgrades the events when the transcript is opened mid-fetch', async () => {
      let openGate: () => void = () => {};
      gatewayEventsGate = new Promise<void>((resolve) => {
        openGate = resolve;
      });

      const element = (await fixture(
        html`<flow-execution-view></flow-execution-view>`
      )) as FlowExecutionView;
      element.executionId = 'exec-1';
      await element.updateComplete;

      const gatewayCalls = () =>
        fetchStub
          .getCalls()
          .map((call) => String(call.args[0]))
          .filter((url) => url.includes('/gateway-events'));

      await waitUntil(
        () => gatewayCalls().length === 1,
        'The metadata fetch never started'
      );

      // The switch lands while the metadata fetch is still in flight, which
      // handleTabShow declines to act on.
      (element as any).handleTabShow({ detail: { name: 'transcript' } });
      expect(gatewayCalls()).to.have.length(1);

      openGate();
      await waitUntil(
        () => (element as any).gatewayEventsFullLoaded === true,
        'The transcript was left on metadata-only events'
      );

      const calls = gatewayCalls();
      expect(calls).to.have.length(2);
      expect(calls[0]).to.contain('metadata_only=true');
      expect(calls[1]).to.not.contain('metadata_only');
    });

    it('keeps no debug logging in the execution page', async () => {
      const log = sinon.spy(console, 'log');
      try {
        await load('exec-running');
        expect(log.called, 'no console.log survives on this page').to.be.false;
      } finally {
        log.restore();
      }
    });
  });
});
