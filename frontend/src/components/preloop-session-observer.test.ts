import { fixture, html, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';
import { unifiedWebSocketManager } from '../services/unified-websocket-manager';
import './preloop-session-observer';
import type { PreloopSessionObserver } from './preloop-session-observer';

describe('PreloopSessionObserver', () => {
  let fetchStub: sinon.SinonStub;
  let connectStub: sinon.SinonStub;
  let subscribeStub: sinon.SinonStub;

  const session = {
    id: 'runtime-session-1',
    session_source_type: 'claude_code',
    session_source_id: 'workspace-42',
    session_reference: 'claude-session-42',
    runtime_principal_name: 'Claude Workspace',
    started_at: '2026-03-09T18:00:00Z',
    last_activity_at: '2026-03-09T20:00:00Z',
    ended_at: null,
    latest_model_alias: 'anthropic/claude-sonnet-4',
    latest_provider_name: 'Anthropic',
    is_active_now: true,
    activity_status: 'active_now',
    total_requests: 1,
    successful_requests: 1,
    failed_requests: 0,
    token_usage: {
      prompt_tokens: 1200,
      completion_tokens: 100,
      total_tokens: 1300,
    },
    estimated_cost: 0.42,
    last_request_at: '2026-03-09T20:00:00Z',
  };

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
    connectStub = sinon.stub(unifiedWebSocketManager, 'connect').resolves();
    subscribeStub = sinon
      .stub(unifiedWebSocketManager, 'subscribe')
      .returns(() => undefined);
    fetchStub = sinon.stub(window, 'fetch');
    fetchStub.callsFake(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString();
      if (url.includes('/gateway-events/') && url.includes('/summary')) {
        return new Response(
          JSON.stringify({
            event_id: 'event-1',
            title: 'Widget replay request',
            summary:
              'The user asked the agent to build a session replay widget.',
            key_points: ['User wants replay clarity', '1300 tokens'],
            risk_level: 'low',
            next_action: null,
            generated_by: 'model',
            model_name: 'fast-model',
            estimated_summary_cost: 0.001,
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        );
      }
      if (url.includes('/ai-models')) {
        return new Response(
          JSON.stringify([
            {
              id: 'model-1',
              name: 'fast-model',
              provider_name: 'anthropic',
              model_kind: 'llm',
              model_identifier: 'claude-sonnet-4',
              is_default: true,
              created_at: '2026-03-09T00:00:00Z',
              updated_at: '2026-03-09T00:00:00Z',
            },
          ]),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        );
      }
      if (url.includes('/requests')) {
        const failedOnly = url.includes('failed_only=true');
        const items = [
          {
            id: 'req-ok',
            timestamp: '2026-03-09T20:00:00Z',
            model_alias: 'gpt-4o',
            provider_name: 'openai',
            status_code: 200,
            is_error: false,
            finish_reason: 'stop',
            is_retry: false,
            prompt_tokens: 500,
            completion_tokens: 500,
            total_tokens: 1000,
            estimated_cost: 0.02,
            endpoint: '/v1/chat/completions',
            tools: [],
            tools_total_schema_tokens: 0,
          },
          {
            id: 'req-fail',
            timestamp: '2026-03-09T20:05:00Z',
            model_alias: 'gpt-4o',
            provider_name: 'openai',
            status_code: 500,
            is_error: true,
            finish_reason: null,
            is_retry: false,
            prompt_tokens: 10,
            completion_tokens: 0,
            total_tokens: 10,
            estimated_cost: 0.0,
            endpoint: '/v1/chat/completions',
            tools: [],
            tools_total_schema_tokens: 0,
          },
        ];
        const filtered = failedOnly
          ? items.filter((item) => item.is_error)
          : items;
        return new Response(
          JSON.stringify({
            items: filtered,
            total: filtered.length,
            failed_count: 1,
            limit: 25,
            offset: 0,
            next_offset: null,
            has_more: false,
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        );
      }
      if (url.includes('/gateway-events')) {
        return new Response(
          JSON.stringify({
            logs: [
              {
                id: 'event-1',
                timestamp: '2026-03-09T20:00:00Z',
                type: 'model_gateway_call',
                payload: {
                  outcome: 'success',
                  model_alias: 'anthropic/claude-sonnet-4',
                  prompt_tokens: 1200,
                  completion_tokens: 100,
                  total_tokens: 5000,
                  estimated_cost: 0.42,
                  conversation_preview: {
                    messages: [
                      {
                        role: 'user',
                        text: 'Build a widget that replays agent sessions',
                      },
                      {
                        role: 'assistant',
                        text: 'I will inspect the existing session views.',
                      },
                    ],
                  },
                },
              },
            ],
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        );
      }
      if (url.includes('/activity')) {
        return new Response(
          JSON.stringify({
            items: [
              {
                activity_type: 'model_interaction',
                timestamp: '2026-03-09T20:00:00Z',
                title: 'Duplicate model summary',
                summary: 'POST /anthropic/v1/messages',
                status: 'success',
              },
              {
                activity_type: 'model_gateway_call',
                timestamp: '2026-03-09T20:00:00Z',
                title: 'Duplicate stored gateway call',
                status: 'success',
              },
              {
                activity_type: 'session_started',
                timestamp: '2026-03-09T18:00:00Z',
                title: 'Session started',
                summary: 'Claude Workspace',
                status: 'info',
              },
            ],
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        );
      }
      if (url.includes('/summaries')) {
        return new Response(
          JSON.stringify({
            title: 'Widget implementation session',
            description: 'Model summary from the fast-model endpoint.',
            risk_level: 'low',
            highlights: ['1 model request', '1300 total tokens'],
            next_action: null,
            generated_by: 'local',
            fast_model_name: 'fast-model',
            estimated_summary_cost: 0,
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        );
      }
      if (url.includes('/optimizations')) {
        return new Response(
          JSON.stringify({
            generated_by: 'model',
            fast_model_name: 'fast-model',
            model_id: 'model-1',
            model_name: 'fast-model',
            token_usage: {
              prompt_tokens: 120,
              completion_tokens: 20,
              total_tokens: 140,
            },
            estimated_optimization_cost: 0.004,
            suggestions: [
              {
                id: 'trim-context',
                title: 'Trim prompt context',
                description: 'Most tokens were prompt-side.',
                expected_savings_tokens: 300,
                expected_savings_usd: 0.08,
                confidence: 'medium',
                action_label: 'Review context segments',
                evidence: ['1200 prompt tokens'],
              },
            ],
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        );
      }
      return new Response(JSON.stringify({}), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    });
  });

  afterEach(() => {
    fetchStub.restore();
    connectStub.restore();
    subscribeStub.restore();
    localStorage.clear();
  });

  function deepText(el: Element | ShadowRoot | null | undefined): string {
    if (!el) return '';
    let text = el.textContent || '';
    el.querySelectorAll('*').forEach((child) => {
      const shadow = (child as HTMLElement).shadowRoot;
      if (shadow) text += ` ${deepText(shadow)}`;
    });
    return text;
  }

  it('renders normalized sessions and keeps optimizations opt-in', async () => {
    const el = (await fixture(
      html`<preloop-session-observer
        .sessions=${[session]}
      ></preloop-session-observer>`
    )) as PreloopSessionObserver;

    await waitUntil(
      () => deepText(el.shadowRoot).includes('Build a widget'),
      '',
      {
        timeout: 3000,
      }
    );
    const text = deepText(el.shadowRoot);
    expect(text).to.include('Claude Workspace');
    // The unified chat renders the conversation as turns (supporting activity is
    // now inline rather than in a separate "Supporting activity" section).
    expect(text).to.include('Build a widget');
    expect(text).to.not.include('Duplicate model summary');
    expect(text).to.include('Transcript');
    expect(text).to.include('Replay');
    expect(
      fetchStub.calledWithMatch(
        '/api/v1/runtime-sessions/runtime-session-1/optimizations'
      )
    ).to.be.false;
  });

  it('switches replay layout to chat transcript', async () => {
    const el = (await fixture(
      html`<preloop-session-observer
        .sessions=${[session]}
      ></preloop-session-observer>`
    )) as PreloopSessionObserver;

    await waitUntil(
      () => deepText(el.shadowRoot).includes('Build a widget'),
      '',
      {
        timeout: 3000,
      }
    );
    const replayButton = Array.from(
      el.shadowRoot?.querySelectorAll('sl-button') || []
    ).find((button) => button.textContent?.trim() === 'Replay');
    expect(replayButton).to.exist;
    replayButton!.click();
    await el.updateComplete;

    expect(deepText(el.shadowRoot)).to.include(
      'I will inspect the existing session views.'
    );
  });

  it('shows replay view with time controls when replay mode is selected', async () => {
    const el = (await fixture(
      html`<preloop-session-observer
        .sessions=${[session]}
        .features=${{ optimization: true }}
      ></preloop-session-observer>`
    )) as PreloopSessionObserver;

    await waitUntil(() => deepText(el.shadowRoot).includes('Replay'), '', {
      timeout: 3000,
    });
    const replayButton = Array.from(
      el.shadowRoot?.querySelectorAll('sl-button') || []
    ).find((button) => button.textContent?.trim() === 'Replay');
    expect(replayButton).to.exist;
    replayButton!.click();
    await el.updateComplete;

    const replayPanel = el.shadowRoot?.querySelector('session-replay-panel');
    await replayPanel?.updateComplete;

    await waitUntil(
      () => deepText(replayPanel?.shadowRoot).includes('Start'),
      'Replay view did not load controls',
      { timeout: 3000 }
    );

    const replayText = deepText(replayPanel?.shadowRoot);
    expect(
      replayPanel?.shadowRoot?.querySelector('sl-button[title="Jump to start"]')
    ).to.exist;
    expect(
      replayPanel?.shadowRoot?.querySelector(
        'sl-button[title="Previous event"]'
      )
    ).to.exist;
    expect(
      replayPanel?.shadowRoot?.querySelector('sl-button[title="Next event"]')
    ).to.exist;
    expect(
      replayPanel?.shadowRoot?.querySelector('sl-button[title="Jump to end"]')
    ).to.exist;
    expect(replayPanel?.shadowRoot?.querySelector('select.speed-select-native'))
      .to.exist;
    expect(replayText).to.include('Tool call');
    expect(replayText).to.not.include('Loaded');
    expect(replayText).to.not.include('Comic');
    expect(replayText).to.include('1x');
  });

  it('summarizes long visible interactions when enabled', async () => {
    localStorage.setItem(
      'preloop.sessionObserver.summarizeCostAcknowledged',
      'true'
    );
    const el = (await fixture(
      html`<preloop-session-observer
        .sessions=${[session]}
      ></preloop-session-observer>`
    )) as PreloopSessionObserver;

    await waitUntil(
      () => deepText(el.shadowRoot).includes('Build a widget'),
      '',
      {
        timeout: 3000,
      }
    );
    // Programmatically enable summarizeVisibleContent as the toolbar button is removed
    (el as any).summarizeVisibleContent = true;
    await el.updateComplete;

    await waitUntil(
      () => deepText(el.shadowRoot).includes('The user asked the agent'),
      '',
      { timeout: 3000 }
    );
    expect(
      fetchStub.calledWithMatch(
        '/api/v1/runtime-sessions/runtime-session-1/gateway-events/event-1/summary'
      )
    ).to.be.true;
  });

  it('loads the unified request timeline from /requests when Requests is clicked', async () => {
    const el = (await fixture(
      html`<preloop-session-observer
        .sessions=${[session]}
      ></preloop-session-observer>`
    )) as PreloopSessionObserver;

    await waitUntil(
      () => deepText(el.shadowRoot).includes('Build a widget'),
      '',
      {
        timeout: 3000,
      }
    );

    const requestsButton = Array.from(
      el.shadowRoot?.querySelectorAll('sl-button') || []
    ).find((button) => button.textContent?.trim().startsWith('Requests'));
    expect(requestsButton).to.exist;
    requestsButton!.click();

    await waitUntil(
      () =>
        Boolean(el.shadowRoot?.querySelector('session-request-timeline')) &&
        fetchStub.calledWithMatch(
          '/api/v1/runtime-sessions/runtime-session-1/requests'
        ),
      'request timeline did not load',
      { timeout: 3000 }
    );

    const timeline = el.shadowRoot?.querySelector('session-request-timeline');
    expect(timeline).to.exist;
  });

  it('opens the budget creation dialog from a set_budget action', async () => {
    const el = (await fixture(
      html`<preloop-session-observer
        .sessions=${[session]}
      ></preloop-session-observer>`
    )) as PreloopSessionObserver;

    await waitUntil(
      () => deepText(el.shadowRoot).includes('Build a widget'),
      '',
      {
        timeout: 3000,
      }
    );

    el.dispatchEvent(
      new CustomEvent('session-create-budget', {
        detail: {
          action: { type: 'set_budget', params: {} },
          suggestion: { id: 'budget' },
        },
        bubbles: true,
        composed: true,
      })
    );
    await el.updateComplete;

    const dialog = el.shadowRoot?.querySelector('sl-dialog');
    expect(dialog).to.exist;
    expect(deepText(el.shadowRoot)).to.include('Create budget for this agent');
  });

  it('switches to a failed-only request view from an open_events action', async () => {
    const el = (await fixture(
      html`<preloop-session-observer
        .sessions=${[session]}
      ></preloop-session-observer>`
    )) as PreloopSessionObserver;

    await waitUntil(
      () => deepText(el.shadowRoot).includes('Build a widget'),
      '',
      {
        timeout: 3000,
      }
    );

    el.dispatchEvent(
      new CustomEvent('session-inspect-requests', {
        detail: { failedOnly: true, eventIds: [] },
        bubbles: true,
        composed: true,
      })
    );

    await waitUntil(
      () =>
        fetchStub.calledWithMatch(
          '/api/v1/runtime-sessions/runtime-session-1/requests'
        ),
      'failed requests were not loaded',
      { timeout: 3000 }
    );

    const failedCall = fetchStub
      .getCalls()
      .find(
        (call) =>
          typeof call.args[0] === 'string' &&
          call.args[0].includes('/requests') &&
          call.args[0].includes('failed_only=true')
      );
    expect(failedCall, 'expected a failed_only request call').to.exist;
  });

  it('renders a bounded empty state (no spinner) when there are no sessions', async () => {
    const el = (await fixture(
      html`<preloop-session-observer
        .sessions=${[]}
      ></preloop-session-observer>`
    )) as PreloopSessionObserver;
    await el.updateComplete;

    const panel = el.shadowRoot?.querySelector('session-replay-panel');
    expect(panel, 'replay panel renders').to.exist;
    await (panel as any).updateComplete;

    // The replay panel must not spin forever with nothing to load.
    expect((panel as any).loading, 'panel loading flag').to.equal(false);
    expect(panel!.shadowRoot?.querySelector('.loading sl-spinner')).to.not
      .exist;
    expect(deepText(el.shadowRoot)).to.include(
      'Select a session to follow it live or replay it.'
    );
    expect(deepText(el.shadowRoot)).to.not.include('Loading session replay');
  });

  it('explains the first gateway call on an agent scope with no sessions', async () => {
    const el = (await fixture(
      html`<preloop-session-observer
        scope="managed_agent"
        .scopeId=${'agent-1'}
        .sessions=${[]}
      ></preloop-session-observer>`
    )) as PreloopSessionObserver;
    await el.updateComplete;

    const panel = el.shadowRoot?.querySelector('session-replay-panel');
    expect(panel).to.exist;
    await (panel as any).updateComplete;

    expect(deepText(el.shadowRoot)).to.include(
      'No sessions yet for this agent. Its first gateway call will appear here live.'
    );
    expect(panel!.shadowRoot?.querySelector('.loading sl-spinner')).to.not
      .exist;
  });

  describe('Optimize first-use hint', () => {
    beforeEach(() => {
      localStorage.removeItem('optimize_hint_dismissed');
    });

    async function createObserverWithOptimization() {
      return (await fixture(
        html`<preloop-session-observer
          .sessions=${[session]}
          .features=${{ optimization: true }}
        ></preloop-session-observer>`
      )) as PreloopSessionObserver;
    }

    it("shows the hint with the session's real ledger numbers", async () => {
      const el = await createObserverWithOptimization();
      await waitUntil(
        () => el.shadowRoot?.querySelector('.optimize-hint'),
        'hint bar did not render',
        { timeout: 3000 }
      );
      const hint = el.shadowRoot?.querySelector('.optimize-hint');
      const text = (hint?.textContent || '').replace(/\s+/g, ' ');
      expect(text).to.contain('This session used 1,300 tokens ($0.42).');
      expect(text).to.contain(
        'Optimize finds where they went and suggests cuts — you verify each one by replaying the session, without touching your agent.'
      );
      expect(text).to.contain('Try Optimize');
    });

    it('does not show the hint when optimization is disabled', async () => {
      const el = (await fixture(
        html`<preloop-session-observer
          .sessions=${[session]}
        ></preloop-session-observer>`
      )) as PreloopSessionObserver;
      await el.updateComplete;
      expect(el.shadowRoot?.querySelector('.optimize-hint')).to.not.exist;
    });

    it('does not show the hint once dismissed (persisted per user)', async () => {
      localStorage.setItem('optimize_hint_dismissed', 'true');
      const el = await createObserverWithOptimization();
      await el.updateComplete;
      expect(el.shadowRoot?.querySelector('.optimize-hint')).to.not.exist;
    });

    it('dismissing via × persists and removes the bar', async () => {
      const el = await createObserverWithOptimization();
      await waitUntil(() => el.shadowRoot?.querySelector('.optimize-hint'));
      (
        el.shadowRoot?.querySelector(
          '.optimize-hint-dismiss'
        ) as HTMLButtonElement
      ).click();
      await el.updateComplete;
      expect(el.shadowRoot?.querySelector('.optimize-hint')).to.not.exist;
      expect(localStorage.getItem('optimize_hint_dismissed')).to.equal('true');
    });

    it('Try Optimize opens the drawer and retires the hint for good', async () => {
      const el = await createObserverWithOptimization();
      await waitUntil(() => el.shadowRoot?.querySelector('.optimize-hint'));
      (
        el.shadowRoot?.querySelector('.optimize-hint-link') as HTMLElement
      ).click();
      await el.updateComplete;
      expect((el as any).replayMode).to.equal('optimize');
      expect(el.shadowRoot?.querySelector('.optimize-hint')).to.not.exist;
      expect(localStorage.getItem('optimize_hint_dismissed')).to.equal('true');
    });

    it('ships its entry motion behind the reduced-motion guard', () => {
      const styles = ((el: unknown) =>
        (el as { styles: Array<{ cssText: string }> }).styles)(
        customElements.get('preloop-session-observer')
      );
      const text = styles.map((s) => s.cssText).join('\n');
      expect(text).to.contain('@media (prefers-reduced-motion: reduce)');
      expect(text).to.contain('@media (prefers-reduced-motion: no-preference)');
      expect(text).to.contain('optimize-hint-enter');
    });
  });
});
