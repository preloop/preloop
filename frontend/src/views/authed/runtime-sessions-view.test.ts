import { unifiedWebSocketManager } from '../../services/unified-websocket-manager';
import { fixture, html, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import './runtime-sessions-view';
import type { RuntimeSessionsView } from './runtime-sessions-view';
import { formatCueStart } from './runtime-sessions-view';

describe('RuntimeSessionsView', () => {
  let fetchStub: sinon.SinonStub;
  let wsStub: sinon.SinonStub;

  const SEARCH_URL = '/api/v1/runtime-sessions/search';

  // One matching session with two snippets, one of each kind the corpus
  // publishes, so the tags and the deep link both have something to work with.
  function searchResponse(overrides: Record<string, unknown> = {}) {
    return {
      query: 'rollout plan',
      mode: 'keyword',
      effective_mode: 'keyword',
      degraded: { keyword: true, semantic: false, reasons: [], detail: null },
      indexed_through: null,
      indexed_from: null,
      backfill_complete: false,
      backfill_state: 'not_started',
      total: 1,
      limit: 25,
      offset: 0,
      elapsed_ms: 12.5,
      results: [
        {
          runtime_session_id: 'runtime-session-2',
          session_source_type: 'flow_execution',
          session_source_id: 'execution-1',
          session_reference: 'session-abc123',
          title: 'Triage Assistant',
          started_at: '2026-03-09T19:00:00Z',
          last_activity_at: '2026-03-09T19:15:00Z',
          score: 0.91,
          best_chunk_rank: 0.62,
          matched_chunk_count: 3,
          first_match_at: '2026-03-09T19:05:00Z',
          last_match_at: '2026-03-09T19:12:00Z',
          snippets: [
            {
              document_id: 'doc-1',
              runtime_session_id: 'runtime-session-2',
              source_kind: 'gateway_interaction',
              source_id: 'usage-flow-1',
              chunk_index: 0,
              occurred_at: '2026-03-09T19:05:00Z',
              role: 'user',
              rank: 0.62,
              redaction_state: 'clear',
              text: 'Review the <mark>rollout</mark> plan before shipping',
            },
            {
              document_id: 'doc-2',
              runtime_session_id: 'runtime-session-2',
              source_kind: 'tool_call',
              source_id: 'tool-7',
              chunk_index: 0,
              occurred_at: '2026-03-09T19:12:00Z',
              role: null,
              rank: 0.41,
              redaction_state: 'clear',
              text: 'search_issues: <mark>rollout</mark> owner',
            },
          ],
        },
      ],
      ...overrides,
    };
  }

  function searchCalls(): sinon.SinonSpyCall[] {
    return fetchStub
      .getCalls()
      .filter((call) => String(call.args[0]) === SEARCH_URL);
  }

  async function typeQuery(
    element: RuntimeSessionsView,
    value: string
  ): Promise<void> {
    const toolbar = element.shadowRoot!.querySelector('list-toolbar')!;
    toolbar.dispatchEvent(
      new CustomEvent('search-change', {
        detail: { value },
        bubbles: true,
        composed: true,
      })
    );
    await element.updateComplete;
  }

  async function renderedSearch(
    query = 'rollout plan'
  ): Promise<RuntimeSessionsView> {
    const element = (await fixture(
      html`<runtime-sessions-view></runtime-sessions-view>`
    )) as RuntimeSessionsView;
    await waitUntil(
      () => !(element as any).loading,
      'Runtime sessions view did not finish loading'
    );
    await typeQuery(element, query);
    await waitUntil(
      () => searchCalls().length > 0,
      'Search did not reach the content search endpoint',
      { timeout: 3000 }
    );
    await waitUntil(
      () => !(element as any).searchLoading,
      'Search did not settle'
    );
    await element.updateComplete;
    return element;
  }

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

      if (url === SEARCH_URL) {
        return new Response(JSON.stringify(searchResponse()), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }

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
                note_count: 2,
                latest_note_author_display: 'Reviewer',
                latest_note_author_auth_method: 'agent',
                latest_note_at: '2026-03-09T19:55:00Z',
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
    (element as any).sessionSourceType = 'flow_execution';
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

  it('shows the AI-titles upsell hint for free accounts and opens the upgrade modal', async () => {
    fetchStub.callsFake(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString();
      if (url.includes('/api/v1/billing/entitlements')) {
        return new Response(
          JSON.stringify({ premium: false, reason: 'none' }),
          {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          }
        );
      }
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
      return new Response(JSON.stringify({ features: {} }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    });

    const opened: string[] = [];
    const onUpgrade = (event: Event) => {
      opened.push(String((event as CustomEvent).detail?.feature || ''));
    };
    window.addEventListener('show-upgrade-modal', onUpgrade);

    const element = (await fixture(
      html`<runtime-sessions-view></runtime-sessions-view>`
    )) as RuntimeSessionsView;

    try {
      await waitUntil(
        () => Boolean(element.shadowRoot?.querySelector('.titles-upsell-hint')),
        'titles upsell hint did not render'
      );
      const hint = element.shadowRoot!.querySelector(
        '.titles-upsell-hint'
      ) as HTMLButtonElement;
      expect(hint.textContent).to.contain('AI titles');
      hint.click();
      expect(opened).to.deep.equal(['session_titles']);
    } finally {
      window.removeEventListener('show-upgrade-modal', onUpgrade);
    }
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

  it('shows who noted a session, and asks for nothing extra to do it', async () => {
    const element = (await fixture(
      html`<runtime-sessions-view></runtime-sessions-view>`
    )) as RuntimeSessionsView;

    await waitUntil(
      () => !(element as any).loading,
      'Runtime sessions view did not finish loading'
    );
    await element.updateComplete;

    const listPanel = element.shadowRoot
      ?.querySelector('preloop-session-observer')
      ?.shadowRoot?.querySelector('session-list-panel');
    await waitUntil(
      () =>
        Boolean(
          listPanel?.shadowRoot?.querySelector(
            '[data-testid="session-notes-runtime-session-1"]'
          )
        ),
      'Note indicator did not render on the noted session'
    );

    const noted = listPanel!.shadowRoot!.querySelector(
      '[data-testid="session-notes-runtime-session-1"]'
    )!;
    expect(noted.textContent).to.contain('2 notes');
    expect(noted.textContent).to.contain('Reviewer');
    expect(noted.getAttribute('title')).to.equal(
      'Most recent note from Reviewer (agent)'
    );
    // The other row was never noted, so it carries no indicator at all.
    expect(
      listPanel!.shadowRoot!.querySelector(
        '[data-testid="session-notes-runtime-session-2"]'
      )
    ).to.equal(null);

    // The fields ride the list row: no note request, and one list request for
    // the whole page rather than one per row.
    const urls = fetchStub.getCalls().map((call) => String(call.args[0]));
    expect(urls.filter((url) => url.includes('operator-notes'))).to.have.length(
      0
    );
    expect(
      urls.filter((url) => url.startsWith('/api/v1/runtime-sessions?'))
    ).to.have.length(1);
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

    it('sends a debounced query to the content search, not the list', async () => {
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
      const listsBefore = listCalls().length;

      await typeQuery(element, 'rollout plan');

      // Nothing goes out on the keystroke itself: the query is a server
      // round trip, so it waits for the typing to stop.
      expect(searchCalls().length).to.equal(0);

      await waitUntil(
        () => searchCalls().length > 0,
        'Search did not reach the endpoint after the debounce',
        { timeout: 3000 }
      );

      const request = searchCalls()[0];
      expect(String(request.args[0])).to.equal(SEARCH_URL);
      expect(String((request.args[1] as RequestInit).method)).to.equal('POST');
      expect(
        JSON.parse(String((request.args[1] as RequestInit).body))
      ).to.deep.include({ query: 'rollout plan', mode: 'keyword' });
      const searchBody = JSON.parse(
        String((request.args[1] as RequestInit).body)
      ) as Record<string, unknown>;
      expect(searchBody).to.not.have.property('session_source_type');
      expect(
        (searchBody.filters as Record<string, unknown> | undefined) || {}
      ).to.not.have.property('session_source_type');
      expect(
        (searchBody.filters as Record<string, unknown> | undefined) || {}
      ).to.not.have.property('source_kind');
      // The list endpoint never sees the query.
      expect(listCalls().length).to.equal(listsBefore);
      expect(
        listCalls().filter((url) => url.includes('query='))
      ).to.have.length(0);
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

      const pending: Array<(body: unknown) => void> = [];
      fetchStub.callsFake(async (input: RequestInfo | URL) => {
        const url = typeof input === 'string' ? input : input.toString();
        if (url.includes('/api/v1/runtime-sessions?')) {
          const body = await new Promise<unknown>((resolve) => {
            pending.push(resolve);
          });
          return new Response(JSON.stringify(body), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        }
        return new Response('{}', { status: 200 });
      });

      try {
        // Identical GETs share one in-flight request, so the two loads need
        // different parameters or there is only one fetch to resolve.
        (element as any).status = 'active';
        const first = (element as any).loadSessions();
        await waitUntil(
          () => pending.length >= 1,
          'First session list fetch did not start'
        );
        (element as any).status = 'ended';
        const second = (element as any).loadSessions();
        await waitUntil(
          () => pending.length >= 2,
          'Second session list fetch did not start'
        );
        pending[1](listPayload('fresh-session', 'Fresh'));
        pending[0](listPayload('stale-session', 'Stale'));
        await Promise.all([first, second]);
        await element.updateComplete;

        expect((element as any).sessions.items[0].id).to.equal('fresh-session');
        expect((element as any).selectedSessionId).to.equal('fresh-session');
        expect((element as any).loading).to.equal(false);
      } finally {
        for (const resolve of pending) {
          resolve(listPayload('fresh-session', 'Fresh'));
        }
      }
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
        'Search prompts, responses, tool calls, and artifacts'
      );
      expect(input.getAttribute('label')).to.equal('Search session content');
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

  describe('content search', () => {
    function snippetButtons(element: RuntimeSessionsView): HTMLElement[] {
      return Array.from(
        element.shadowRoot!.querySelectorAll('button.snippet')
      ) as HTMLElement[];
    }

    it('renders one snippet per match with its time and match tag', async () => {
      const element = await renderedSearch();

      const buttons = snippetButtons(element);
      expect(buttons).to.have.length(2);

      const first = buttons[0];
      expect(first.getAttribute('data-testid')).to.equal('snippet-doc-1');
      expect(first.textContent).to.contain('Model call');
      expect(first.textContent).to.contain('user');
      expect(first.querySelector('mark')!.textContent).to.equal('rollout');
      // The timestamp of the turn, not of the session.
      expect(first.textContent).to.contain(
        new Intl.DateTimeFormat(undefined, {
          month: 'short',
          day: 'numeric',
          year: 'numeric',
          hour: 'numeric',
          minute: '2-digit',
        }).format(new Date('2026-03-09T19:05:00Z'))
      );
      expect(buttons[1].textContent).to.contain('Tool call');
    });

    it('opens a session-summary hit without claiming a turn jump', async () => {
      fetchStub.withArgs(SEARCH_URL, sinon.match.any).callsFake(async () => {
        const body = searchResponse();
        body.results[0].snippets = [
          {
            document_id: 'doc-summary',
            runtime_session_id: 'runtime-session-2',
            source_kind: 'session_summary',
            source_id: 'runtime-session-2',
            chunk_index: 0,
            occurred_at: '2026-03-09T19:00:00Z',
            role: 'system',
            rank: 0.5,
            redaction_state: 'clear',
            text: 'Triage Assistant <mark>rollout</mark>',
          },
        ];
        return new Response(JSON.stringify(body), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      });

      const element = await renderedSearch();
      const button = snippetButtons(element)[0];
      expect(button.textContent).to.contain('Opens the session');
      button.click();
      await element.updateComplete;

      expect((element as any).selectedSessionId).to.equal('runtime-session-2');
      expect((element as any).focusTurnId).to.equal(null);
      expect(new URLSearchParams(window.location.search).get('turn')).to.equal(
        null
      );
    });

    it('shows an artifact hit with its kind, name, labels and cue time, and opens its timeline row', async () => {
      fetchStub.withArgs(SEARCH_URL, sinon.match.any).callsFake(async () => {
        const body = searchResponse();
        body.results[0].snippets = [
          {
            document_id: 'doc-artifact',
            runtime_session_id: 'runtime-session-2',
            source_kind: 'artifact',
            source_id: 'artifact-1',
            chunk_index: 0,
            occurred_at: '2026-03-09T19:00:00Z',
            role: 'artifact',
            rank: 0.5,
            redaction_state: 'clear',
            text: 'Receiving Lead: Reporting a <mark>damaged</mark> <mark>pallet</mark>',
            artifact: {
              artifact_id: 'artifact-1',
              activity_id: 'activity-artifact-1',
              kind: 'transcript',
              name: 'nord-late.vtt',
              content_type: 'text/vtt',
              labels: { site: 'nord', tags: ['handover', 'dock'] },
              cue_start: 65.5,
              text_truncated: false,
            },
          },
        ];
        return new Response(JSON.stringify(body), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      });

      const element = await renderedSearch();
      const button = snippetButtons(element)[0];
      expect(button.textContent).to.contain('Transcript · nord-late.vtt');
      const labels = Array.from(
        button.querySelectorAll('[data-testid="snippet-artifact-label"]')
      ).map((node) => node.textContent!.trim());
      expect(labels).to.deep.equal(['site: nord', 'tags: handover, dock']);
      expect(
        button.querySelector('[data-testid="snippet-cue-start"]')!.textContent
      ).to.contain('at 1:05');
      expect(button.textContent).to.not.contain('Opens the session');

      button.click();
      await element.updateComplete;
      expect((element as any).selectedSessionId).to.equal('runtime-session-2');
      expect((element as any).focusTurnId).to.equal('activity-artifact-1');
      expect(new URLSearchParams(window.location.search).get('turn')).to.equal(
        'activity-artifact-1'
      );
    });

    it('formats cue starts past an hour', () => {
      expect(formatCueStart(0)).to.equal('0:00');
      expect(formatCueStart(3725.9)).to.equal('1:02:05');
    });

    it('does not claim nothing matched before a search has run', async () => {
      const element = (await fixture(
        html`<runtime-sessions-view></runtime-sessions-view>`
      )) as RuntimeSessionsView;
      await waitUntil(
        () => !(element as any).loading,
        'Runtime sessions view did not finish loading'
      );
      await element.updateComplete;

      await typeQuery(element, 'r');
      await element.updateComplete;

      expect((element as any).searchQuery).to.equal('r');
      expect((element as any).searchResults).to.equal(null);
      expect((element as any).searchLoading).to.equal(false);
      expect(
        element.shadowRoot!.querySelector('[data-testid="search-empty"]')
      ).to.equal(null);
      const loadingState = element.shadowRoot!.querySelector(
        '[data-testid="search-loading"]'
      );
      expect(loadingState).to.not.equal(null);
      expect(loadingState!.textContent).to.contain('Searching session content');
    });

    it('opens the session at the matching turn, and the location reproduces it', async () => {
      const element = await renderedSearch();

      snippetButtons(element)[1].click();
      await element.updateComplete;

      expect((element as any).selectedSessionId).to.equal('runtime-session-2');
      expect((element as any).focusTurnId).to.equal('tool-7');

      const params = new URLSearchParams(window.location.search);
      expect(params.get('sessionId')).to.equal('runtime-session-2');
      expect(params.get('turn')).to.equal('tool-7');
      expect(params.get('q')).to.equal('rollout plan');

      const observer = element.shadowRoot!.querySelector(
        'preloop-session-observer'
      ) as any;
      expect(observer.focusTurnId).to.equal('tool-7');

      // Reloading that location reproduces the same view.
      const reloaded = (await fixture(
        html`<runtime-sessions-view></runtime-sessions-view>`
      )) as RuntimeSessionsView;
      await waitUntil(
        () => (reloaded as any).searchResults !== null,
        'Reloaded view did not restore the search'
      );
      await reloaded.updateComplete;

      expect((reloaded as any).searchQuery).to.equal('rollout plan');
      expect((reloaded as any).selectedSessionId).to.equal('runtime-session-2');
      expect((reloaded as any).focusTurnId).to.equal('tool-7');
      expect(snippetButtons(reloaded)).to.have.length(2);
    });

    it('keeps list ledger numbers when a snippet opens a listed session', async () => {
      const element = await renderedSearch();
      snippetButtons(element)[0].click();
      await element.updateComplete;

      const observer = element.shadowRoot!.querySelector(
        'preloop-session-observer'
      ) as HTMLElement & { updateComplete: Promise<unknown> };
      await observer.updateComplete;
      await waitUntil(() => {
        const meta = observer.shadowRoot?.querySelector('.toolbar .meta');
        return Boolean(meta && meta.textContent?.includes('tokens'));
      }, 'Observer toolbar never showed session ledger numbers');

      const meta = observer.shadowRoot!.querySelector('.toolbar .meta')!;
      const text = (meta.textContent || '').replace(/\s+/g, ' ').trim();
      expect(text).to.not.equal('0 tokens · $0.00');
      expect(text).to.contain('700 tokens');
      expect(text).to.contain('$0.11');
    });

    it('restores the list behaviour when the query is emptied', async () => {
      const element = await renderedSearch();
      expect(snippetButtons(element)).to.have.length(2);

      const listCallsBefore = fetchStub
        .getCalls()
        .filter((call) =>
          String(call.args[0]).startsWith('/api/v1/runtime-sessions?')
        ).length;
      const searchCallsBefore = searchCalls().length;

      await typeQuery(element, '');
      await waitUntil(
        () =>
          fetchStub
            .getCalls()
            .filter((call) =>
              String(call.args[0]).startsWith('/api/v1/runtime-sessions?')
            ).length > listCallsBefore &&
          element.shadowRoot!.querySelector('preloop-session-observer') !==
            null,
        'Emptying the query did not go back to the list',
        { timeout: 3000 }
      );
      await element.updateComplete;

      expect(searchCalls().length).to.equal(searchCallsBefore);
      expect((element as any).searchResults).to.equal(null);
      expect(snippetButtons(element)).to.have.length(0);
      expect(
        element.shadowRoot!.querySelector('preloop-session-observer')
      ).to.not.equal(null);
      expect(new URLSearchParams(window.location.search).get('q')).to.equal(
        null
      );
    });

    it('states partial coverage when the corpus stops inside the range', async () => {
      const insideRange = new Date(Date.now() - 3_600_000).toISOString();
      fetchStub
        .withArgs(SEARCH_URL, sinon.match.any)
        .callsFake(
          async () =>
            new Response(
              JSON.stringify(searchResponse({ indexed_through: insideRange })),
              { status: 200, headers: { 'Content-Type': 'application/json' } }
            )
        );

      const element = await renderedSearch();
      const notice = element.shadowRoot!.querySelector(
        '[data-testid="coverage-notice"]'
      );
      expect(notice).to.not.equal(null);
      expect(notice!.textContent).to.contain('indexed through');
    });

    it('states nothing about coverage when the corpus covers the range', async () => {
      const pastRangeEnd = new Date(
        Date.now() + 7 * 24 * 3_600_000
      ).toISOString();
      fetchStub
        .withArgs(SEARCH_URL, sinon.match.any)
        .callsFake(
          async () =>
            new Response(
              JSON.stringify(searchResponse({ indexed_through: pastRangeEnd })),
              { status: 200, headers: { 'Content-Type': 'application/json' } }
            )
        );

      const element = await renderedSearch();
      expect(
        element.shadowRoot!.querySelector('[data-testid="coverage-notice"]')
      ).to.equal(null);
    });

    it('states how far back the corpus reaches when it stops inside the range', async () => {
      // The shape of a deployment whose backfill has never run: the corpus
      // starts a few days ago and everything older is absent, not unmatched.
      const daysAgo = (days: number) =>
        new Date(Date.now() - days * 24 * 3_600_000).toISOString();
      fetchStub.withArgs(SEARCH_URL, sinon.match.any).callsFake(
        async () =>
          new Response(
            JSON.stringify(
              searchResponse({
                indexed_from: daysAgo(2),
                indexed_through: daysAgo(0),
                backfill_complete: false,
                backfill_state: 'not_started',
              })
            ),
            { status: 200, headers: { 'Content-Type': 'application/json' } }
          )
      );

      const element = await renderedSearch();
      const notice = element.shadowRoot!.querySelector(
        '[data-testid="coverage-floor-notice"]'
      );
      expect(notice).to.not.equal(null);
      expect(notice!.textContent).to.contain('reaches back to');
    });

    it('states nothing about the floor once the backfill walked the history', async () => {
      // A complete backfill makes an empty answer honest on its own: there is
      // no older history left to index, so a warning would be noise.
      const daysAgo = (days: number) =>
        new Date(Date.now() - days * 24 * 3_600_000).toISOString();
      fetchStub.withArgs(SEARCH_URL, sinon.match.any).callsFake(
        async () =>
          new Response(
            JSON.stringify(
              searchResponse({
                indexed_from: daysAgo(2),
                indexed_through: daysAgo(0),
                backfill_complete: true,
                backfill_state: 'complete',
              })
            ),
            { status: 200, headers: { 'Content-Type': 'application/json' } }
          )
      );

      const element = await renderedSearch();
      expect(
        element.shadowRoot!.querySelector(
          '[data-testid="coverage-floor-notice"]'
        )
      ).to.equal(null);
    });

    it('states nothing about the floor when it predates the range searched', async () => {
      // The default range is the last 30 days; a corpus reaching back a year
      // covers all of it, so there is nothing to warn about.
      const daysAgo = (days: number) =>
        new Date(Date.now() - days * 24 * 3_600_000).toISOString();
      fetchStub.withArgs(SEARCH_URL, sinon.match.any).callsFake(
        async () =>
          new Response(
            JSON.stringify(
              searchResponse({
                indexed_from: daysAgo(365),
                indexed_through: daysAgo(0),
                backfill_complete: false,
                backfill_state: 'in_progress',
              })
            ),
            { status: 200, headers: { 'Content-Type': 'application/json' } }
          )
      );

      const element = await renderedSearch();
      expect(
        element.shadowRoot!.querySelector(
          '[data-testid="coverage-floor-notice"]'
        )
      ).to.equal(null);
    });

    it('warns hardest when the whole range searched predates the corpus', async () => {
      // A custom range that ends before the corpus starts: every session in it
      // is unindexed, so an empty answer says nothing about what happened.
      const daysAgo = (days: number) =>
        new Date(Date.now() - days * 24 * 3_600_000).toISOString();
      const isoDay = (days: number) => daysAgo(days).slice(0, 10);
      fetchStub.withArgs(SEARCH_URL, sinon.match.any).callsFake(
        async () =>
          new Response(
            JSON.stringify(
              searchResponse({
                indexed_from: daysAgo(2),
                indexed_through: daysAgo(0),
                backfill_complete: false,
                backfill_state: 'not_started',
              })
            ),
            { status: 200, headers: { 'Content-Type': 'application/json' } }
          )
      );

      const element = (await fixture(
        html`<runtime-sessions-view></runtime-sessions-view>`
      )) as RuntimeSessionsView;
      await waitUntil(
        () => !(element as any).loading,
        'Runtime sessions view did not finish loading'
      );
      (element as any).startDate = isoDay(30);
      (element as any).endDate = isoDay(10);
      await element.updateComplete;
      await typeQuery(element, 'rollout plan');
      await waitUntil(
        () => searchCalls().length > 0,
        'Search did not reach the content search endpoint',
        { timeout: 3000 }
      );
      await waitUntil(
        () => !(element as any).searchLoading,
        'Search did not settle'
      );
      await element.updateComplete;

      const notice = element.shadowRoot!.querySelector(
        '[data-testid="coverage-floor-notice"]'
      );
      expect(notice).to.not.equal(null);
      expect(notice!.textContent).to.contain('Nothing in this date range');
      expect(notice!.getAttribute('variant')).to.equal('warning');
    });

    it('surfaces a degraded semantic half rather than implying completeness', async () => {
      fetchStub.withArgs(SEARCH_URL, sinon.match.any).callsFake(
        async () =>
          new Response(
            JSON.stringify(
              searchResponse({
                mode: 'hybrid',
                degraded: {
                  keyword: true,
                  semantic: false,
                  reasons: ['semantic_not_enabled'],
                  detail:
                    'Semantic ranking is not enabled on this deployment; these are keyword results ranked by relevance.',
                },
              })
            ),
            { status: 200, headers: { 'Content-Type': 'application/json' } }
          )
      );

      const element = await renderedSearch();
      const notice = element.shadowRoot!.querySelector(
        '[data-testid="degraded-notice"]'
      );
      expect(notice).to.not.equal(null);
      expect(notice!.textContent).to.contain('Semantic ranking is not enabled');
    });

    it('keeps the semantic search settings closed and unread until asked', async () => {
      const element = await renderedSearch();

      expect(
        element.shadowRoot!.querySelector('session-embedding-settings')
      ).to.equal(null);
      expect(
        fetchStub
          .getCalls()
          .some((call) => String(call.args[0]).includes('/settings/embedding'))
      ).to.equal(false);
      expect(
        element.shadowRoot!.querySelector(
          '[data-testid="embedding-settings-toggle"]'
        )
      ).to.not.equal(null);
    });

    it('offers the opt in from the semantic_not_enabled notice', async () => {
      fetchStub.withArgs(SEARCH_URL, sinon.match.any).callsFake(
        async () =>
          new Response(
            JSON.stringify(
              searchResponse({
                mode: 'hybrid',
                degraded: {
                  keyword: true,
                  semantic: false,
                  reasons: ['semantic_not_enabled'],
                  detail:
                    'This account has not opted in to embedding its session content, so these are keyword results.',
                },
              })
            ),
            { status: 200, headers: { 'Content-Type': 'application/json' } }
          )
      );

      const element = await renderedSearch();
      const open = element.shadowRoot!.querySelector(
        '[data-testid="degraded-notice"] [data-testid="open-embedding-settings"]'
      ) as HTMLElement | null;
      expect(open).to.not.equal(null);

      open!.click();
      await element.updateComplete;

      expect(
        element.shadowRoot!.querySelector('session-embedding-settings')
      ).to.not.equal(null);
    });

    it('does not offer the opt in for a degraded reason it cannot fix', async () => {
      fetchStub.withArgs(SEARCH_URL, sinon.match.any).callsFake(
        async () =>
          new Response(
            JSON.stringify(
              searchResponse({
                mode: 'hybrid',
                degraded: {
                  keyword: true,
                  semantic: false,
                  reasons: ['semantic_disabled'],
                  detail:
                    'Semantic ranking is switched off on this deployment.',
                },
              })
            ),
            { status: 200, headers: { 'Content-Type': 'application/json' } }
          )
      );

      const element = await renderedSearch();

      expect(
        element.shadowRoot!.querySelector(
          '[data-testid="open-embedding-settings"]'
        )
      ).to.equal(null);
    });

    it('re-runs the current search after the embedding setting is saved', async () => {
      const element = await renderedSearch();
      const before = searchCalls().length;

      element
        .shadowRoot!.querySelector('[data-testid="embedding-settings-toggle"]')!
        .dispatchEvent(new Event('click'));
      await element.updateComplete;
      const card = element.shadowRoot!.querySelector(
        'session-embedding-settings'
      )!;
      card.dispatchEvent(
        new CustomEvent('session-embedding-changed', {
          bubbles: true,
          composed: true,
          detail: { setting: { enabled: true } },
        })
      );

      await waitUntil(
        () => searchCalls().length > before,
        'the search was not re-run after the opt in',
        { timeout: 3000 }
      );
    });

    it('issues one request after the debounce, not one per keystroke', async () => {
      const element = (await fixture(
        html`<runtime-sessions-view></runtime-sessions-view>`
      )) as RuntimeSessionsView;
      await waitUntil(
        () => !(element as any).loading,
        'Runtime sessions view did not finish loading'
      );
      await element.updateComplete;

      await typeQuery(element, 'r');
      await typeQuery(element, 'ro');
      await typeQuery(element, 'rollout plan');
      expect(searchCalls().length).to.equal(0);

      await waitUntil(() => searchCalls().length > 0, 'Search never went out', {
        timeout: 3000,
      });
      // Let a second debounce window pass: nothing further goes out.
      await new Promise((resolve) => setTimeout(resolve, 600));
      expect(searchCalls().length).to.equal(1);
      expect(
        JSON.parse(String((searchCalls()[0].args[1] as RequestInit).body)).query
      ).to.equal('rollout plan');
    });

    it('renders a loading state while the search is in flight', async () => {
      let release: ((value: unknown) => void) | null = null;
      fetchStub.withArgs(SEARCH_URL, sinon.match.any).callsFake(async () => {
        await new Promise((resolve) => {
          release = resolve;
        });
        return new Response(JSON.stringify(searchResponse()), {
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
      await typeQuery(element, 'rollout plan');
      await waitUntil(
        () => (element as any).searchLoading === true,
        'Search never entered its loading state',
        { timeout: 3000 }
      );
      await element.updateComplete;

      const loadingState = element.shadowRoot!.querySelector(
        '[data-testid="search-loading"]'
      );
      expect(loadingState).to.not.equal(null);
      expect(loadingState!.textContent).to.contain('Searching session content');
      release?.(null);
    });

    it('renders an empty state when nothing matched', async () => {
      fetchStub
        .withArgs(SEARCH_URL, sinon.match.any)
        .callsFake(
          async () =>
            new Response(
              JSON.stringify(searchResponse({ total: 0, results: [] })),
              { status: 200, headers: { 'Content-Type': 'application/json' } }
            )
        );

      const element = await renderedSearch();
      const empty = element.shadowRoot!.querySelector(
        '[data-testid="search-empty"]'
      );
      expect(empty).to.not.equal(null);
      expect(empty!.textContent).to.contain('No session content matched');
    });

    it('renders an error state when the search fails', async () => {
      fetchStub
        .withArgs(SEARCH_URL, sinon.match.any)
        .callsFake(
          async () =>
            new Response(
              JSON.stringify({ detail: 'Search is unavailable right now' }),
              { status: 500, headers: { 'Content-Type': 'application/json' } }
            )
        );

      const element = await renderedSearch();
      const error = element.shadowRoot!.querySelector(
        '[data-testid="search-error"]'
      );
      expect(error).to.not.equal(null);
      expect(error!.textContent).to.contain('Search is unavailable right now');
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
