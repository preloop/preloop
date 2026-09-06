import { unifiedWebSocketManager } from '../../services/unified-websocket-manager';
import { fixture, html, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import './runtime-sessions-view';
import type { RuntimeSessionsView } from './runtime-sessions-view';

describe('RuntimeSessionsView', () => {
  let fetchStub: sinon.SinonStub;
  let wsStub: sinon.SinonStub;

  function getDeepText(el: Element | ShadowRoot | null | undefined): string {
    if (!el) return '';
    let text = el.textContent || '';
    if (el instanceof Element && el.shadowRoot) {
      text += ' ' + getDeepText(el.shadowRoot);
    }
    const children = Array.from(el.children);
    for (const child of children) {
      text += ' ' + getDeepText(child);
    }
    if (el instanceof Element && el.shadowRoot) {
      const shadowChildren = Array.from(el.shadowRoot.children);
      for (const child of shadowChildren) {
        text += ' ' + getDeepText(child);
      }
    }
    return text;
  }

  beforeEach(() => {
    wsStub = sinon.stub(unifiedWebSocketManager, 'send').returns(true);
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');

    fetchStub = sinon.stub(window, 'fetch');
    fetchStub.callsFake(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString();

      if (url.startsWith('/api/v1/runtime-sessions?')) {
        return new Response(
          JSON.stringify({
            period_start: '2026-02-08T00:00:00Z',
            period_end: '2026-03-09T23:59:59Z',
            query: null,
            session_source_type: null,
            status: 'all',
            total: 2,
            limit: 50,
            offset: 0,
            items: [
              {
                id: 'runtime-session-1',
                session_source_type: 'claude_code',
                session_source_id: 'workspace-42',
                session_reference: 'claude-session-42',
                runtime_principal_type: 'claude_code',
                runtime_principal_id: 'workspace-42',
                runtime_principal_name: 'Claude Workspace',
                started_at: '2026-03-09T18:00:00Z',
                last_activity_at: '2026-03-09T20:00:00Z',
                ended_at: null,
                flow_id: null,
                flow_name: null,
                flow_execution_id: null,
                latest_model_alias: 'anthropic/claude-sonnet-4',
                latest_provider_name: 'Anthropic',
                is_active_now: true,
                activity_status: 'active_now',
                total_requests: 4,
                successful_requests: 3,
                failed_requests: 1,
                token_usage: {
                  prompt_tokens: 1200,
                  completion_tokens: 450,
                  total_tokens: 1650,
                },
                estimated_cost: 0.42,
                last_request_at: '2026-03-09T20:00:00Z',
              },
              {
                id: 'runtime-session-2',
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
                latest_model_alias: 'openai/gpt-5',
                latest_provider_name: 'OpenAI',
                is_active_now: false,
                activity_status: 'ended',
                total_requests: 2,
                successful_requests: 2,
                failed_requests: 0,
                token_usage: {
                  prompt_tokens: 500,
                  completion_tokens: 200,
                  total_tokens: 700,
                },
                estimated_cost: 0.11,
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

      if (
        url.includes('/api/v1/runtime-sessions/runtime-session-1/interactions')
      ) {
        return new Response(
          JSON.stringify({
            items: [
              {
                api_usage_id: 'usage-1',
                timestamp: '2026-03-09T20:00:00Z',
                status_code: 200,
                outcome: 'success',
                endpoint: '/anthropic/v1/messages',
                method: 'POST',
                provider_name: 'Anthropic',
                model_alias: 'anthropic/claude-sonnet-4',
                runtime_session_id: 'runtime-session-1',
                session_source_type: 'claude_code',
                session_source_id: 'workspace-42',
                session_reference: 'claude-session-42',
                runtime_principal_type: 'claude_code',
                runtime_principal_id: 'workspace-42',
                runtime_principal_name: 'Claude Workspace',
                auth_subject_type: 'api_key',
                api_key_id: 'api-key-1',
                api_key_name: 'Claude Workspace Token',
                estimated_cost: 0.12,
                token_usage: {
                  prompt_tokens: 200,
                  completion_tokens: 75,
                  total_tokens: 275,
                },
                excerpt:
                  'request.input: Summarize the deployment risk review response.output_text: Deployment risk review summarized',
                meta_data: {
                  source: 'gateway_interaction',
                },
              },
            ],
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        );
      }

      if (url.includes('/api/v1/runtime-sessions/runtime-session-1/activity')) {
        return new Response(
          JSON.stringify({
            items: [
              {
                activity_type: 'session_started',
                timestamp: '2026-03-09T18:00:00Z',
                title: 'Session started',
                summary: 'claude-session-42',
                status: 'info',
              },
              {
                activity_type: 'tool_call',
                timestamp: '2026-03-09T20:00:01Z',
                title: 'search_issues',
                summary: 'Found similar issues',
                status: 'success',
                tool_name: 'search_issues',
                server_name: 'preloop-mcp',
              },
              {
                activity_type: 'model_interaction',
                timestamp: '2026-03-09T20:00:00Z',
                title: 'anthropic/claude-sonnet-4',
                summary: 'POST /anthropic/v1/messages',
                status: 'success',
                api_usage_id: 'usage-1',
                auth_subject_type: 'api_key',
                api_key_id: 'api-key-1',
                api_key_name: 'Claude Workspace Token',
                estimated_cost: 0.12,
                total_tokens: 275,
              },
              {
                activity_type: 'session_ended',
                timestamp: '2026-03-09T20:30:00Z',
                title: 'Session ended',
                summary: 'claude-session-42',
                status: 'completed',
              },
            ],
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
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

      if (url.includes('/api/v1/runtime-sessions/runtime-session-1')) {
        if (
          String((input as Request).method || 'GET').toUpperCase() === 'PATCH'
        ) {
          return new Response(
            JSON.stringify({
              id: 'runtime-session-1',
              session_source_type: 'claude_code',
              session_source_id: 'workspace-42',
              session_reference: 'claude-session-42',
              runtime_principal_type: 'claude_code',
              runtime_principal_id: 'workspace-42',
              runtime_principal_name: 'Claude Workspace',
              started_at: '2026-03-09T18:00:00Z',
              last_activity_at: '2026-03-09T20:30:00Z',
              ended_at: '2026-03-09T20:30:00Z',
              latest_model_alias: 'anthropic/claude-sonnet-4',
              latest_provider_name: 'Anthropic',
              is_active_now: false,
              activity_status: 'ended',
              total_requests: 4,
              successful_requests: 3,
              failed_requests: 1,
              token_usage: {
                prompt_tokens: 1200,
                completion_tokens: 450,
                total_tokens: 1650,
              },
              estimated_cost: 0.42,
              last_request_at: '2026-03-09T20:00:00Z',
            }),
            {
              status: 200,
              headers: { 'Content-Type': 'application/json' },
            }
          );
        }
        return new Response(
          JSON.stringify({
            period_start: '2026-02-08T00:00:00Z',
            period_end: '2026-03-09T23:59:59Z',
            session: {
              id: 'runtime-session-1',
              session_source_type: 'claude_code',
              session_source_id: 'workspace-42',
              session_reference: 'claude-session-42',
              runtime_principal_type: 'claude_code',
              runtime_principal_id: 'workspace-42',
              runtime_principal_name: 'Claude Workspace',
              started_at: '2026-03-09T18:00:00Z',
              last_activity_at: '2026-03-09T20:00:00Z',
              ended_at: null,
              flow_id: null,
              flow_name: null,
              flow_execution_id: null,
              latest_model_alias: 'anthropic/claude-sonnet-4',
              latest_provider_name: 'Anthropic',
              is_active_now: true,
              activity_status: 'active_now',
              total_requests: 4,
              successful_requests: 3,
              failed_requests: 1,
              token_usage: {
                prompt_tokens: 1200,
                completion_tokens: 450,
                total_tokens: 1650,
              },
              estimated_cost: 0.42,
              last_request_at: '2026-03-09T20:00:00Z',
            },
            usage_by_model: [
              {
                ai_model_id: 'model-1',
                model_alias: 'anthropic/claude-sonnet-4',
                provider_name: 'Anthropic',
                request_count: 4,
                token_usage: {
                  prompt_tokens: 1200,
                  completion_tokens: 450,
                  total_tokens: 1650,
                },
                estimated_cost: 0.42,
              },
            ],
          }),
          {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          }
        );
      }

      if (url.startsWith('/api/v1/runtime-sessions/runtime-session-2')) {
        return new Response(
          JSON.stringify({
            period_start: '2026-02-08T00:00:00Z',
            period_end: '2026-03-09T23:59:59Z',
            session: {
              id: 'runtime-session-2',
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
              latest_model_alias: 'openai/gpt-5',
              latest_provider_name: 'OpenAI',
              is_active_now: false,
              activity_status: 'ended',
              total_requests: 2,
              successful_requests: 2,
              failed_requests: 0,
              token_usage: {
                prompt_tokens: 500,
                completion_tokens: 200,
                total_tokens: 700,
              },
              estimated_cost: 0.11,
              last_request_at: '2026-03-09T19:15:00Z',
            },
            usage_by_model: [
              {
                ai_model_id: 'model-2',
                model_alias: 'openai/gpt-5',
                provider_name: 'OpenAI',
                request_count: 2,
                token_usage: {
                  prompt_tokens: 500,
                  completion_tokens: 200,
                  total_tokens: 700,
                },
                estimated_cost: 0.11,
              },
            ],
            interactions: {
              period_start: '2026-02-08T00:00:00Z',
              period_end: '2026-03-09T23:59:59Z',
              query: null,
              total: 0,
              limit: 50,
              offset: 0,
              items: [],
            },
            activity_timeline: [
              {
                activity_type: 'session_started',
                timestamp: '2026-03-09T19:00:00Z',
                title: 'Session started',
                summary: 'session-abc123',
                status: 'info',
                api_usage_id: null,
                tool_name: null,
                server_name: null,
                auth_subject_type: null,
                api_key_id: null,
                api_key_name: null,
                estimated_cost: null,
                total_tokens: null,
              },
              {
                activity_type: 'tool_call',
                timestamp: '2026-03-09T19:10:00Z',
                title: 'search_issues',
                summary: 'Found similar issues',
                status: 'success',
                api_usage_id: null,
                tool_name: 'search_issues',
                server_name: 'preloop-mcp',
                auth_subject_type: null,
                api_key_id: null,
                api_key_name: null,
                estimated_cost: null,
                total_tokens: null,
              },
            ],
          }),
          {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          }
        );
      }

      if (
        url.startsWith('/api/v1/flows/executions/execution-1/gateway-events')
      ) {
        return new Response(
          JSON.stringify({
            source: 'database',
            logs: [
              {
                execution_id: 'execution-1',
                timestamp: '2026-03-09T19:15:00Z',
                type: 'model_gateway_call',
                payload: {
                  api_usage_id: 'usage-flow-1',
                  model_alias: 'openai/gpt-5',
                  provider_name: 'OpenAI',
                  outcome: 'success',
                  estimated_cost: 0.11,
                  total_tokens: 700,
                  prompt_tokens: 500,
                  completion_tokens: 200,
                  status_code: 200,
                  method: 'POST',
                  endpoint: '/openai/v1/responses',
                  endpoint_kind: 'responses',
                  conversation_preview: {
                    messages: [
                      {
                        source: 'request',
                        role: 'user',
                        text: 'Review the rollout plan',
                        redacted: false,
                        truncated: false,
                      },
                      {
                        source: 'response',
                        role: 'assistant',
                        text: 'Rollout plan reviewed.',
                        redacted: false,
                        truncated: false,
                      },
                    ],
                    metadata: {
                      message_count: 2,
                    },
                  },
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

      return new Response(
        JSON.stringify({ detail: `Unhandled request: ${url}` }),
        {
          status: 500,
          headers: { 'Content-Type': 'application/json' },
        }
      );
    });
  });

  afterEach(() => {
    wsStub.restore();
    fetchStub.restore();
    localStorage.clear();
    window.history.replaceState({}, '', '/console/runtime-sessions');
  });

  it('shows a first-run empty state when no sessions exist and no filters are active', async () => {
    fetchStub.callsFake(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString();
      if (url.startsWith('/api/v1/runtime-sessions?')) {
        return new Response(
          JSON.stringify({
            period_start: '2026-02-08T00:00:00Z',
            period_end: '2026-03-09T23:59:59Z',
            query: null,
            session_source_type: null,
            status: 'all',
            total: 0,
            limit: 50,
            offset: 0,
            items: [],
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        );
      }
      return new Response('{}', {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    });

    const element = (await fixture(
      html`<runtime-sessions-view></runtime-sessions-view>`
    )) as RuntimeSessionsView;

    await waitUntil(
      () => !(element as any).loading,
      'Runtime sessions view did not finish loading'
    );
    await element.updateComplete;

    const content = getDeepText(element).replace(/\s+/g, ' ');
    expect(content).to.contain('No sessions yet.');
    expect(content).to.contain(
      'Onboard an agent from the Agents page to see your first one.'
    );
    expect(content).to.not.contain('No sessions matched the current filters.');

    // With a non-default filter active, blame the filters instead.
    (element as any).searchQuery = 'nothing-matches-this';
    await element.updateComplete;

    await waitUntil(
      () =>
        getDeepText(element)
          .replace(/\s+/g, ' ')
          .includes('No sessions matched the current filters.'),
      'Filtered empty-state copy did not render'
    );
    expect(getDeepText(element).replace(/\s+/g, ' ')).to.not.contain(
      'No sessions yet.'
    );
  });

  it('renders runtime session list without blocking on session detail', async () => {
    const element = (await fixture(
      html`<runtime-sessions-view></runtime-sessions-view>`
    )) as RuntimeSessionsView;

    await waitUntil(
      () => !(element as any).loading,
      'Runtime sessions view did not finish loading'
    );
    await element.updateComplete;

    const content = getDeepText(element).replace(/\s+/g, ' ');
    expect(content).to.contain('Claude Workspace');

    const listCall = fetchStub
      .getCalls()
      .find((call) =>
        String(call.args[0]).startsWith('/api/v1/runtime-sessions?')
      );
    // Parent no longer fetches detail on list load — observer owns events.
    const detailCall = fetchStub.getCalls().find((call) => {
      const url = String(call.args[0]);
      return (
        url.startsWith('/api/v1/runtime-sessions/runtime-session-1') &&
        !url.includes('/gateway-events') &&
        !url.includes('/activity') &&
        !url.includes('/requests')
      );
    });

    expect(listCall).to.not.equal(undefined);
    expect(detailCall).to.equal(undefined);

    await waitUntil(() => {
      const obs = element.shadowRoot?.querySelector(
        'preloop-session-observer'
      ) as any;
      return (
        obs?.activeSessionId === 'runtime-session-1' && !obs?.loadingSessionId
      );
    }, 'Observer did not finish loading selected session events');

    expect(content).to.contain('anthropic/claude-sonnet-4');
  });

  it('shows flow-backed session content from execution gateway events', async () => {
    const element = (await fixture(
      html`<runtime-sessions-view></runtime-sessions-view>`
    )) as RuntimeSessionsView;

    await waitUntil(
      () => !(element as any).loading,
      'Runtime sessions view did not finish loading'
    );

    const observer = element.shadowRoot?.querySelector(
      'preloop-session-observer'
    );
    const listPanel = observer?.shadowRoot?.querySelector('session-list-panel');
    const sessionButtons =
      listPanel?.shadowRoot?.querySelectorAll('.session-card');
    (sessionButtons?.[1] as HTMLButtonElement).click();

    await waitUntil(() => {
      const obs = element.shadowRoot?.querySelector(
        'preloop-session-observer'
      ) as any;
      return (
        (element as any).selectedSessionId === 'runtime-session-2' &&
        obs?.activeSessionId === 'runtime-session-2' &&
        !obs?.loadingSessionId
      );
    }, 'Flow-backed session detail did not finish loading');
    await element.updateComplete;

    const content = getDeepText(element).replace(/\s+/g, ' ');
    expect(content).to.contain('openai/gpt-5');
  });

  describe('collection bar', () => {
    it('states the matching session count in one live region', async () => {
      const element = (await fixture(
        html`<runtime-sessions-view></runtime-sessions-view>`
      )) as RuntimeSessionsView;

      await waitUntil(
        () => !(element as any).loading,
        'Runtime sessions view did not finish loading'
      );
      await element.updateComplete;

      const toolbar = element.shadowRoot!.querySelector('list-toolbar')!;
      expect(toolbar).to.not.equal(null);
      const count = toolbar.querySelector('[slot="count"]')!;
      expect(count.textContent!.trim()).to.equal('2 sessions');
      const liveRegion = toolbar.shadowRoot!.querySelector('.results-count')!;
      expect(liveRegion.getAttribute('aria-live')).to.equal('polite');
    });

    it('drops the filter and observer card titles', async () => {
      const element = (await fixture(
        html`<runtime-sessions-view></runtime-sessions-view>`
      )) as RuntimeSessionsView;

      await waitUntil(
        () => !(element as any).loading,
        'Runtime sessions view did not finish loading'
      );
      await element.updateComplete;

      const text = element.shadowRoot!.textContent || '';
      expect(text).to.not.contain('Session Explorer Filters');
      expect(text).to.not.contain('Session Observer');
    });

    it('refetches on a debounced search, like the Agents bar', async () => {
      const element = (await fixture(
        html`<runtime-sessions-view></runtime-sessions-view>`
      )) as RuntimeSessionsView;

      await waitUntil(
        () => !(element as any).loading,
        'Runtime sessions view did not finish loading'
      );
      await element.updateComplete;

      const listCalls = () =>
        fetchStub
          .getCalls()
          .map((call) => String(call.args[0]))
          .filter((url) => url.startsWith('/api/v1/runtime-sessions?'));
      const before = listCalls().length;

      const toolbar = element.shadowRoot!.querySelector('list-toolbar')!;
      toolbar.dispatchEvent(
        new CustomEvent('search-change', {
          detail: { value: 'workspace-42' },
          bubbles: true,
          composed: true,
        })
      );

      // Nothing goes out on the keystroke itself: the query is a server
      // parameter, so it waits for the typing to stop.
      expect(listCalls().length).to.equal(before);

      await waitUntil(
        () => listCalls().length > before,
        'Search did not refetch after the debounce',
        { timeout: 3000 }
      );
      expect(listCalls().pop()).to.contain('query=workspace-42');
    });

    it('ignores a stale list when a later load already finished', async () => {
      const element = (await fixture(
        html`<runtime-sessions-view></runtime-sessions-view>`
      )) as RuntimeSessionsView;

      await waitUntil(
        () => !(element as any).loading,
        'Runtime sessions view did not finish loading'
      );
      await element.updateComplete;

      const listPayload = (id: string, name: string) => ({
        period_start: '2026-02-08T00:00:00Z',
        period_end: '2026-03-09T23:59:59Z',
        query: null,
        session_source_type: null,
        status: 'all',
        total: 1,
        limit: 50,
        offset: 0,
        items: [
          {
            id,
            session_source_type: 'claude_code',
            session_source_id: 'workspace-42',
            session_reference: name,
            runtime_principal_type: 'claude_code',
            runtime_principal_id: 'workspace-42',
            runtime_principal_name: name,
            started_at: '2026-03-09T18:00:00Z',
            last_activity_at: '2026-03-09T20:00:00Z',
            ended_at: null,
            flow_id: null,
            flow_name: null,
            flow_execution_id: null,
            latest_model_alias: 'anthropic/claude-sonnet-4',
            latest_provider_name: 'Anthropic',
            is_active_now: true,
            activity_status: 'active_now',
            total_requests: 4,
            successful_requests: 3,
            failed_requests: 1,
            token_usage: {
              prompt_tokens: 1200,
              completion_tokens: 450,
              total_tokens: 1650,
            },
            estimated_cost: 0.42,
            last_request_at: '2026-03-09T20:00:00Z',
          },
        ],
      });

      let releaseStale: () => void = () => undefined;
      const staleHold = new Promise<void>((resolve) => {
        releaseStale = resolve;
      });
      let listCalls = 0;
      fetchStub.callsFake(async (input: RequestInfo | URL) => {
        const url = typeof input === 'string' ? input : input.toString();
        if (url.startsWith('/api/v1/runtime-sessions?')) {
          listCalls += 1;
          if (listCalls === 1) {
            await staleHold;
            return new Response(
              JSON.stringify(listPayload('stale-session', 'Stale')),
              {
                status: 200,
                headers: { 'Content-Type': 'application/json' },
              }
            );
          }
          return new Response(
            JSON.stringify(listPayload('fresh-session', 'Fresh')),
            {
              status: 200,
              headers: { 'Content-Type': 'application/json' },
            }
          );
        }
        return new Response('{}', { status: 404 });
      });

      const first = (element as any).loadSessions();
      const second = (element as any).loadSessions();
      releaseStale();
      await Promise.all([first, second]);
      await element.updateComplete;

      expect((element as any).sessions.items[0].id).to.equal('fresh-session');
      expect((element as any).selectedSessionId).to.equal('fresh-session');
      expect((element as any).loading).to.equal(false);
    });

    it('keeps the hint about what the query matches', async () => {
      const element = (await fixture(
        html`<runtime-sessions-view></runtime-sessions-view>`
      )) as RuntimeSessionsView;

      await waitUntil(
        () => !(element as any).loading,
        'Runtime sessions view did not finish loading'
      );
      await element.updateComplete;

      const toolbar = element.shadowRoot!.querySelector('list-toolbar')!;
      await (toolbar as any).updateComplete;
      const input = toolbar.shadowRoot!.querySelector('sl-input.search-input')!;
      expect(input.getAttribute('placeholder')).to.equal(
        'Principal, session reference, or source id'
      );
      expect(input.getAttribute('label')).to.equal('Search sessions');
    });

    it('shows one search input on the page', async () => {
      const element = (await fixture(
        html`<runtime-sessions-view></runtime-sessions-view>`
      )) as RuntimeSessionsView;

      await waitUntil(
        () => !(element as any).loading,
        'Runtime sessions view did not finish loading'
      );
      await element.updateComplete;

      const observer = element.shadowRoot!.querySelector(
        'preloop-session-observer'
      )!;
      await (observer as any).updateComplete;

      const toolbarSearches = element
        .shadowRoot!.querySelector('list-toolbar')!
        .shadowRoot!.querySelectorAll('sl-input.search-input');
      const sidebarSearches =
        observer.shadowRoot!.querySelectorAll('.sidebar sl-input');
      expect(toolbarSearches.length).to.equal(1);
      expect(sidebarSearches.length).to.equal(0);
    });
  });

  describe('reader-facing copy', () => {
    it('uses no em dash in the page copy', async () => {
      const element = (await fixture(
        html`<runtime-sessions-view></runtime-sessions-view>`
      )) as RuntimeSessionsView;
      await waitUntil(
        () => !(element as any).loading,
        'Runtime sessions view did not finish loading'
      );

      // This view's own template only. Nested components own their copy and
      // are checked in their own suites.
      const text = element.shadowRoot!.textContent || '';
      expect(text).to.not.contain('\u2014');
      const header = element.shadowRoot!.querySelector('view-header')!;
      const description = header.getAttribute('description') || '';
      expect(description).to.not.contain('\u2014');
      expect(description).to.contain(
        'Everything your agents did, as it happened'
      );
    });
  });
});
