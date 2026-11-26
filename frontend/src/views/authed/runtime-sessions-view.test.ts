import { unifiedWebSocketManager } from '../../services/unified-websocket-manager';
import { fixture, html, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';
import { setViewport, sendKeys } from '@web/test-runner-commands';
import { loadShoelaceTokens } from '../../utils/test-shoelace-theme';

import './runtime-sessions-view';
import type { RuntimeSessionsView } from './runtime-sessions-view';
import { SESSION_PAGE_SIZE, formatCueStart } from './runtime-sessions-view';
import { runtimeSessionPathRedirect } from '../../utils/runtime-session-route';

const SESSION_OPTIONS = {
  runner_id: 'r',
  online: true,
  sessions_available: true,
  harnesses: [
    {
      harness: 'copilot_cli',
      display_name: 'GitHub Copilot CLI',
      session_mode: 'resume',
      models: [{ id: 'auto', source: 'static' }],
      available: true,
    },
  ],
  authorized_directories: [
    { id: 'dir_api', label: 'api', mode: 'write', harnesses: 'all' },
  ],
  checkout_sources: [],
  limits: { max_concurrent: 2, active: 0, idle_timeout_seconds: 1800 },
};

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

  beforeEach(async () => {
    await setViewport({ width: 1440, height: 1000 });
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
        const hasArtifacts = new URL(
          url,
          window.location.origin
        ).searchParams.get('has_artifacts');
        const page = {
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
              artifact_counts: {
                screenshot: 3,
                trace: 2,
                transcript: 1,
                document: 1,
                recording: 1,
              },
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
        };
        if (hasArtifacts) {
          // The server filter, mirrored: only rows holding that kind.
          page.items = page.items.filter((item: any) => {
            const counts = item.artifact_counts ?? {};
            return hasArtifacts === 'any'
              ? Object.keys(counts).length > 0
              : (counts[hasArtifacts] ?? 0) > 0;
          });
          page.total = page.items.length;
        }
        return new Response(JSON.stringify(page), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
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

  it('restores and clears the terminal workspace filter through the paged API', async () => {
    window.history.replaceState(
      {},
      '',
      '/console/runtime-sessions?workspace_id=workspace-1'
    );
    const element = await fixture<RuntimeSessionsView>(
      html`<runtime-sessions-view></runtime-sessions-view>`
    );
    await waitUntil(() => !(element as any).loading);
    const filter = element.shadowRoot!.querySelector(
      'sl-select[label="Workspace"]'
    ) as HTMLSelectElement;
    expect(filter.value).to.equal('workspace-1');
    expect(
      fetchStub
        .getCalls()
        .some((call) =>
          String(call.args[0]).includes('workspace_id=workspace-1')
        )
    ).to.equal(true);
    await (element as any).clearFilters();
    expect(
      new URLSearchParams(window.location.search).has('workspace_id')
    ).to.equal(false);
    expect((element as any).buildListParams().workspaceId).to.equal(undefined);
  });

  it('opens the new session dialog over non-ephemeral runners', async () => {
    const runners = [
      { id: 'r-1', name: 'laptop', status: 'online', labels: [] },
      { id: 'r-2', name: 'ci', status: 'online', ephemeral: true, labels: [] },
    ];
    fetchStub.callsFake(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString();
      const body =
        url === '/api/v1/runners?purpose=sessions'
          ? runners
          : url.includes('/session-options')
            ? SESSION_OPTIONS
            : {};
      return new Response(JSON.stringify(body), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    });
    const element = (await fixture(
      html`<runtime-sessions-view></runtime-sessions-view>`
    )) as RuntimeSessionsView;
    await element.updateComplete;

    const button = element.shadowRoot!.querySelector(
      'sl-button.new-runner-session'
    ) as HTMLElement;
    expect(button).to.exist;
    await (element as any).openNewSession();
    await element.updateComplete;
    const dialog = element.shadowRoot!.querySelector(
      'new-runner-session-dialog'
    ) as any;
    expect(dialog.hasAttribute('open')).to.equal(true);
    expect(dialog.runners.map((r: { id: string }) => r.id)).to.deep.equal([
      'r-1',
    ]);
    await waitUntil(
      () => dialog.options !== null,
      'session options did not load'
    );
    await dialog.updateComplete;
    expect(dialog.selectedRunnerId).to.equal('r-1');
    expect(dialog.shadowRoot!.querySelector('sl-alert.error')).to.not.exist;
    expect(
      dialog
        .shadowRoot!.querySelector('sl-select.directory')!
        .getAttribute('value')
    ).to.equal('dir_api');

    dialog.dispatchEvent(
      new CustomEvent('runner-session-started', {
        detail: {
          session_id: 'rs-9',
          remote_session_id: 'x',
          state: 'requested',
        },
        bubbles: true,
        composed: true,
      })
    );
    await element.updateComplete;
    expect((element as any).newSessionOpen).to.equal(false);
    expect((element as any).selectedSessionId).to.equal('rs-9');
  });

  it('tells the user to install a runner when there is none', async () => {
    fetchStub.callsFake(
      async () =>
        new Response('[]', {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
    );
    const element = (await fixture(
      html`<runtime-sessions-view></runtime-sessions-view>`
    )) as RuntimeSessionsView;
    await (element as any).openNewSession();
    await element.updateComplete;
    expect((element as any).newSessionOpen).to.equal(false);
    expect(
      element.shadowRoot!.querySelector('sl-alert.new-session-error')!
        .textContent
    ).to.contain('preloop runner enable');
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
        !url.includes('/artifacts') &&
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

  it('coalesces a burst of live events into at most one list refresh (#1584)', async () => {
    const element = (await fixture(
      html`<runtime-sessions-view></runtime-sessions-view>`
    )) as RuntimeSessionsView;
    await waitUntil(
      () => !(element as any).loading,
      'Runtime sessions view did not finish loading'
    );
    const listCalls = () =>
      fetchStub
        .getCalls()
        .filter((call) =>
          String(call.args[0]).startsWith('/api/v1/runtime-sessions?')
        ).length;
    expect(listCalls()).to.equal(1);

    // Events spaced past the debounce: each one used to fetch the list.
    for (let i = 0; i < 4; i++) {
      (element as any).scheduleRefresh();
      await new Promise((resolve) => setTimeout(resolve, 300));
    }
    expect(listCalls()).to.equal(1);

    const view = element.constructor as any;
    const interval = view.LIVE_REFRESH_MIN_INTERVAL_MS;
    try {
      // Once the interval has passed, the pending refresh fires exactly once.
      view.LIVE_REFRESH_MIN_INTERVAL_MS = 0;
      const timer = (element as any).refreshTimer;
      window.clearTimeout(timer);
      (element as any).refreshTimer = null;
      for (let i = 0; i < 20; i++) (element as any).scheduleRefresh();
      await waitUntil(() => listCalls() === 2, 'No refresh after the interval');
      await new Promise((resolve) => setTimeout(resolve, 400));
      expect(listCalls()).to.equal(2);
    } finally {
      view.LIVE_REFRESH_MIN_INTERVAL_MS = interval;
      const timer = (element as any).refreshTimer;
      if (timer !== null) window.clearTimeout(timer);
      (element as any).refreshTimer = null;
    }
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

  describe('phone browser (#1483 wave 1)', () => {
    for (const [width, height] of [
      [390, 844],
      [360, 780],
    ]) {
      it(`fits ${width}x${height} with a tappable New session button`, async () => {
        await loadShoelaceTokens();
        await setViewport({ width, height });
        try {
          const element = (await fixture(
            html`<runtime-sessions-view></runtime-sessions-view>`
          )) as RuntimeSessionsView;
          await waitUntil(
            () => !(element as any).loading,
            'Runtime sessions view did not finish loading'
          );
          await element.updateComplete;
          await new Promise((resolve) => requestAnimationFrame(resolve));
          expect(document.documentElement.scrollWidth).to.be.at.most(
            window.innerWidth
          );
          const host = element.getBoundingClientRect();
          expect(host.right).to.be.at.most(window.innerWidth);
          const button = element.shadowRoot!.querySelector(
            'sl-button.new-runner-session'
          )!;
          const rect = button.getBoundingClientRect();
          expect(rect.right).to.be.at.most(window.innerWidth);
          expect(rect.height).to.be.at.least(44);
          expect(rect.width).to.be.at.least(44);
        } finally {
          await setViewport({ width: 1280, height: 800 });
        }
      });
    }
  });

  it('keeps filter widths and positions stable when Clear filters appears', async () => {
    await loadShoelaceTokens();
    const wrapper = await fixture<HTMLElement>(
      html`<div style="width: 1150px">
        <runtime-sessions-view></runtime-sessions-view>
      </div>`
    );
    const element = wrapper.querySelector<RuntimeSessionsView>(
      'runtime-sessions-view'
    )!;
    await waitUntil(() => !(element as any).loading);
    await element.updateComplete;
    const toolbar = element.shadowRoot!.querySelector('list-toolbar') as any;
    await toolbar.updateComplete;
    await new Promise((resolve) => requestAnimationFrame(resolve));
    const controls = [
      toolbar.shadowRoot!.querySelector('sl-input.search-input'),
      ...Array.from(toolbar.querySelectorAll('sl-select')),
    ] as HTMLElement[];
    expect(controls).to.have.length(7);
    expect(
      controls.slice(1).map((control) => control.getAttribute('label'))
    ).to.deep.equal([
      'Source type',
      'Workspace',
      'Status',
      'Date range',
      'Has artifacts',
      'Sort',
    ]);
    const before = controls.map((node) =>
      node.getBoundingClientRect().toJSON()
    );
    expect(
      getComputedStyle(toolbar.querySelector('.filter-actions')).visibility
    ).to.equal('hidden');
    (element as any).status = 'active';
    await element.updateComplete;
    expect(
      getComputedStyle(toolbar.querySelector('.filter-actions')).visibility
    ).to.equal('visible');
    controls.forEach((node, index) => {
      const after = node.getBoundingClientRect();
      expect(after.x).to.equal(before[index].x);
      expect(after.y).to.equal(before[index].y);
      expect(after.width).to.equal(before[index].width);
      expect(after.width).to.be.at.least(150);
    });
  });

  describe('artifact counts on the list (#1084)', () => {
    async function listPanelOf(element: RuntimeSessionsView) {
      await waitUntil(
        () => !(element as any).loading,
        'Runtime sessions view did not finish loading'
      );
      await element.updateComplete;
      const observer = element.shadowRoot!.querySelector(
        'preloop-session-observer'
      ) as any;
      await observer.updateComplete;
      const panel = observer.shadowRoot!.querySelector(
        'session-list-panel'
      ) as any;
      await panel.updateComplete;
      return { observer, panel };
    }

    it('renders kind icons with counts, three at most then +N, and a tooltip of every kind', async () => {
      const element = (await fixture(
        html`<runtime-sessions-view></runtime-sessions-view>`
      )) as RuntimeSessionsView;
      const { panel } = await listPanelOf(element);
      await waitUntil(() =>
        panel.shadowRoot!.querySelector(
          '[data-testid="session-artifacts-runtime-session-1"]'
        )
      );
      const cell = panel.shadowRoot!.querySelector(
        '[data-testid="session-artifacts-runtime-session-1"]'
      )!;
      const kinds = Array.from(cell.querySelectorAll('.artifact-kind')).map(
        (node) => [
          node.getAttribute('data-kind'),
          node.textContent!.replace(/\s+/g, ''),
        ]
      );
      expect(kinds).to.deep.equal([
        ['screenshot', '3'],
        ['other', '3'],
        ['transcript', '1'],
        ['more', '+1'],
      ]);
      const tooltip = cell.getAttribute('title')!;
      for (const part of [
        'screenshot: 3',
        'trace: 2',
        'transcript: 1',
        'document: 1',
        'recording: 1',
      ]) {
        expect(tooltip).to.contain(part);
      }
      // A session without artifacts carries no cell at all.
      expect(
        panel.shadowRoot!.querySelector(
          '[data-testid="session-artifacts-runtime-session-2"]'
        )
      ).to.equal(null);
    });

    it('narrows the list with the "Has artifacts: transcript" filter', async () => {
      const element = (await fixture(
        html`<runtime-sessions-view></runtime-sessions-view>`
      )) as RuntimeSessionsView;
      const { panel } = await listPanelOf(element);
      expect(panel.sessions).to.have.length(2);

      const select = element.shadowRoot!.querySelector(
        '[data-testid="has-artifacts-filter"]'
      ) as any;
      expect(select, 'filter is on the toolbar').to.exist;
      select.value = 'transcript';
      select.dispatchEvent(new Event('sl-change'));
      await (element as any).applyFilters();
      await element.updateComplete;

      const urls = fetchStub.getCalls().map((call) => String(call.args[0]));
      expect(
        urls.some(
          (url) =>
            url.startsWith('/api/v1/runtime-sessions?') &&
            url.includes('has_artifacts=transcript')
        )
      ).to.equal(true);
      const { panel: narrowed } = await listPanelOf(element);
      await waitUntil(() => narrowed.sessions.length === 1);
      expect(narrowed.sessions[0].id).to.equal('runtime-session-1');
    });

    it('says what to do next when no session holds that kind', async () => {
      const element = (await fixture(
        html`<runtime-sessions-view></runtime-sessions-view>`
      )) as RuntimeSessionsView;
      await listPanelOf(element);
      (element as any).hasArtifacts = 'audio';
      await (element as any).applyFilters();
      await waitUntil(
        () => getDeepText(element).includes('No sessions with audio matched.'),
        'Empty state for the artifact filter did not render'
      );
      const text = getDeepText(element);
      expect(text).to.contain('deposit_artifact');
    });

    it('opens the session filtered to the kind whose icon was clicked', async () => {
      const element = (await fixture(
        html`<runtime-sessions-view></runtime-sessions-view>`
      )) as RuntimeSessionsView;
      const { observer, panel } = await listPanelOf(element);
      await waitUntil(() =>
        panel.shadowRoot!.querySelector(
          '[data-testid="session-artifacts-runtime-session-1"] [data-kind="transcript"]'
        )
      );
      (
        panel.shadowRoot!.querySelector(
          '[data-testid="session-artifacts-runtime-session-1"] [data-kind="transcript"]'
        ) as HTMLButtonElement
      ).click();

      await waitUntil(
        () =>
          observer.activeSessionId === 'runtime-session-1' &&
          observer.artifactKindFilter === 'transcript',
        'Session did not open with the transcript filter'
      );
      await observer.updateComplete;
      const summary = observer.shadowRoot!.querySelector(
        'session-artifact-summary'
      ) as any;
      if (summary) expect(summary.activeKind).to.equal('transcript');
    });
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
      const count = toolbar.querySelector('.session-count')!;
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
      expect(input.getAttribute('placeholder')).to.equal('Search sessions');
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

  describe('list load failure and source filter (sessions UX audit)', () => {
    it('shows one error with Retry and no first-run empty state', async () => {
      let fail = true;
      const failing = sinon.match(
        (value: unknown) =>
          fail && String(value).startsWith('/api/v1/runtime-sessions?')
      );
      fetchStub.withArgs(failing, sinon.match.any).callsFake(
        async () =>
          new Response(JSON.stringify({ detail: 'boom' }), {
            status: 500,
            headers: { 'Content-Type': 'application/json' },
          })
      );
      const element = (await fixture(
        html`<runtime-sessions-view></runtime-sessions-view>`
      )) as RuntimeSessionsView;
      await waitUntil(() => !(element as any).loading);
      await element.updateComplete;
      const root = element.shadowRoot!;
      expect(root.querySelector('[data-testid="sessions-load-error"]')).to
        .exist;
      expect(getDeepText(root)).to.not.contain('No sessions yet');
      expect(root.querySelector('preloop-session-observer')).to.equal(null);

      fail = false;
      (
        root.querySelector('[data-testid="sessions-retry"]') as HTMLElement
      ).click();
      await waitUntil(() => root.querySelector('preloop-session-observer'));
      expect(
        root.querySelector('[data-testid="sessions-load-error"]')
      ).to.equal(null);
    });

    it('lists every known agent kind as a source type, Cursor included', async () => {
      const element = (await fixture(
        html`<runtime-sessions-view></runtime-sessions-view>`
      )) as RuntimeSessionsView;
      await waitUntil(() => !(element as any).loading);
      await element.updateComplete;
      const select = Array.from(
        element.shadowRoot!.querySelectorAll('sl-select')
      ).find((el) => el.getAttribute('label') === 'Source type')!;
      const values = Array.from(select.querySelectorAll('sl-option')).map(
        (option) => option.getAttribute('value')
      );
      expect(values).to.include.members([
        'all',
        'flow_execution',
        'claude_code',
        'cursor',
        'copilot_cli',
      ]);
      expect(values).to.not.include('geminicli');
    });

    it('filters a product option by agent kind, once, and finds its live session', async () => {
      const element = (await fixture(
        html`<runtime-sessions-view></runtime-sessions-view>`
      )) as RuntimeSessionsView;
      await waitUntil(() => !(element as any).loading);
      await element.updateComplete;
      // A live enrollment: recorded as desktop_agent, with the cursor kind.
      const live = {
        ...(element as any).sessions.items[0],
        id: 'runtime-session-cursor',
        session_source_type: 'desktop_agent',
        agent_kind: 'cursor',
      };
      const cursorList = sinon.match(
        (value: unknown) =>
          String(value).startsWith('/api/v1/runtime-sessions?') &&
          String(value).includes('agent_kind=cursor')
      );
      fetchStub.withArgs(cursorList).callsFake(
        async () =>
          new Response(
            JSON.stringify({
              ...(element as any).sessions,
              total: 1,
              items: [live],
            }),
            { status: 200, headers: { 'Content-Type': 'application/json' } }
          )
      );
      const listUrls = () =>
        fetchStub
          .getCalls()
          .map((call) => String(call.args[0]))
          .filter((url) => url.startsWith('/api/v1/runtime-sessions?'));
      const before = listUrls().length;
      const select = Array.from(
        element.shadowRoot!.querySelectorAll('sl-select')
      ).find((el) => el.getAttribute('label') === 'Source type')! as any;

      select.value = 'cursor';
      select.dispatchEvent(new Event('sl-change'));
      await waitUntil(
        () =>
          (element as any).sessions?.items?.[0]?.id ===
          'runtime-session-cursor',
        'The live Cursor session was not listed'
      );
      // The select re-reports its value when its options change.
      select.dispatchEvent(new Event('sl-change'));
      await element.updateComplete;

      const requests = listUrls().slice(before);
      expect(requests).to.have.length(1);
      const params = new URL(requests[0], window.location.origin).searchParams;
      expect(params.get('agent_kind')).to.equal('cursor');
      expect(params.get('session_source_type')).to.equal(null);
      expect(
        (element as any).sessions.items.map((item: any) => item.id)
      ).to.deep.equal(['runtime-session-cursor']);
    });

    it('filters a non-product source by source type', async () => {
      const element = (await fixture(
        html`<runtime-sessions-view></runtime-sessions-view>`
      )) as RuntimeSessionsView;
      await waitUntil(() => !(element as any).loading);
      (element as any).sessionSourceType = 'flow_execution';
      const built = (element as any).buildListParams();
      expect(built.sessionSourceType).to.equal('flow_execution');
      expect(built.agentKind).to.equal(undefined);
    });
  });
  describe('content search', () => {
    function snippetButtons(element: RuntimeSessionsView): HTMLElement[] {
      return Array.from(
        element.shadowRoot!.querySelectorAll('a.snippet')
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
      // The timestamp of the turn, not of the session: relative, with the
      // absolute value in the title (DESIGN.md).
      expect(first.textContent).to.contain('ago');
      expect(
        first
          .querySelector('.snippet-header span[title]')!
          .getAttribute('title')
      ).to.contain(
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

    it('renders each hit as a link that a modified click leaves to the browser', async () => {
      const element = await renderedSearch();
      const link = snippetButtons(element)[1] as HTMLAnchorElement;
      const href = new URL(link.href);
      expect(href.searchParams.get('sessionId')).to.equal('runtime-session-2');
      expect(href.searchParams.get('turn')).to.equal('tool-7');

      // A cmd-click must not be swallowed: the browser opens a tab. Record
      // what the view decided, then cancel the default here: on Linux a
      // meta-click navigates this page instead, which aborts the run.
      let leftToBrowser = false;
      const cancelNavigation = (event: Event) => {
        leftToBrowser = !event.defaultPrevented;
        event.preventDefault();
      };
      // Registered after the view's listener, so it runs after it.
      link.addEventListener('click', cancelNavigation);
      const modified = new MouseEvent('click', {
        bubbles: true,
        cancelable: true,
        metaKey: true,
      });
      link.dispatchEvent(modified);
      link.removeEventListener('click', cancelNavigation);
      expect(leftToBrowser).to.equal(true);
      expect((element as any).selectedSessionId).to.not.equal(
        'runtime-session-2'
      );

      const plain = new MouseEvent('click', {
        bubbles: true,
        cancelable: true,
      });
      link.dispatchEvent(plain);
      expect(plain.defaultPrevented).to.equal(true);
      expect((element as any).selectedSessionId).to.equal('runtime-session-2');
    });

    it('keeps a hyphenated term in one highlight', async () => {
      fetchStub.withArgs(SEARCH_URL, sinon.match.any).callsFake(async () => {
        const body = searchResponse();
        body.results[0].snippets = [
          {
            ...body.results[0].snippets[0],
            text: 'saw <mark>alpha</mark>-<mark>beta</mark> fail',
          },
        ];
        return new Response(JSON.stringify(body), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      });
      const element = await renderedSearch('alpha-beta');
      const marks = snippetButtons(element)[0].querySelectorAll('mark');
      expect(marks).to.have.length(1);
      expect(marks[0].textContent).to.equal('alpha-beta');
    });

    it('does not show the detail of a selected session that did not match', async () => {
      const element = await renderedSearch();
      // runtime-session-1 was selected from the list; only session 2 matched.
      expect((element as any).selectedSessionId).to.equal('runtime-session-1');
      expect(
        element.shadowRoot!.querySelector('preloop-session-observer')
      ).to.equal(null);
    });

    it('offers a friendly message and a retry when search fails', async () => {
      let fail = true;
      fetchStub.withArgs(SEARCH_URL, sinon.match.any).callsFake(async () =>
        fail
          ? new Response(JSON.stringify({ detail: 'search down' }), {
              status: 500,
              headers: { 'Content-Type': 'application/json' },
            })
          : new Response(JSON.stringify(searchResponse()), {
              status: 200,
              headers: { 'Content-Type': 'application/json' },
            })
      );
      const element = await renderedSearch();
      const alert = element.shadowRoot!.querySelector(
        '[data-testid="search-error"]'
      )!;
      expect(alert.textContent).to.contain('Search is unavailable');
      fail = false;
      (
        alert.querySelector('[data-testid="search-retry"]') as HTMLElement
      ).click();
      await waitUntil(() => snippetButtons(element).length === 2);
    });

    it('shows a session found only by its content, not its title', async () => {
      fetchStub.withArgs(SEARCH_URL, sinon.match.any).callsFake(async () => {
        const body = searchResponse({ query: 'quokkaflux' });
        body.results[0].snippets = [
          {
            document_id: 'doc-message',
            runtime_session_id: 'runtime-session-2',
            source_kind: 'transcript_message',
            source_id: 'message-9',
            chunk_index: 0,
            occurred_at: '2026-03-09T19:10:00Z',
            role: 'user',
            rank: 0.5,
            redaction_state: 'clear',
            text: 'check the <mark>quokkaflux</mark> migration',
          },
        ];
        return new Response(JSON.stringify(body), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      });

      const element = await renderedSearch('quokkaflux');

      // The title does not hold the word; the ranked content search does.
      const card = element.shadowRoot!.querySelector(
        '[data-testid="search-result-runtime-session-2"]'
      ) as HTMLElement;
      expect(card).to.exist;
      expect(card.textContent).to.contain('Triage Assistant');
      expect(card.textContent).to.not.contain('quokkaflux Triage');
      const buttons = snippetButtons(element);
      expect(buttons).to.have.length(1);
      expect(buttons[0].querySelector('mark')!.textContent).to.equal(
        'quokkaflux'
      );
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

    it('shows an artifact hit with its kind, name, labels and cue time, and opens the session with its deposit row in the location', async () => {
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
            text: 'nord-late.vtt\nlabels: site=nord tags=handover dock\nReceiving Lead: Reporting a <mark>damaged</mark> <mark>pallet</mark>',
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
      ).to.contain('from 1:05');
      // The timeline does not draw artifact rows yet, so no jump is claimed.
      expect(button.textContent).to.contain('Opens the session');
      // The header lines the badges already show are not repeated.
      const body = button.querySelector('.snippet-text')!.textContent!;
      expect(body.trim()).to.equal(
        'Receiving Lead: Reporting a damaged pallet'
      );

      button.click();
      await element.updateComplete;
      expect((element as any).selectedSessionId).to.equal('runtime-session-2');
      expect((element as any).focusTurnId).to.equal('activity-artifact-1');
      expect(new URLSearchParams(window.location.search).get('turn')).to.equal(
        'activity-artifact-1'
      );
    });

    it('does not repeat the tool_name and labels header lines of an artifact hit', async () => {
      fetchStub.withArgs(SEARCH_URL, sinon.match.any).callsFake(async () => {
        const body = searchResponse();
        body.results[0].snippets = [
          {
            document_id: 'doc-artifact-tool',
            runtime_session_id: 'runtime-session-2',
            source_kind: 'artifact',
            source_id: 'artifact-2',
            chunk_index: 0,
            occurred_at: '2026-03-09T19:00:00Z',
            role: 'artifact',
            rank: 0.5,
            redaction_state: 'clear',
            text: 'name: dock-call.vtt\ntool_name: record_call\nlabels: site=nord\nReceiving Lead: a <mark>damaged</mark> pallet',
            artifact: {
              artifact_id: 'artifact-2',
              activity_id: 'activity-artifact-2',
              kind: 'transcript',
              name: 'dock-call.vtt',
              content_type: 'text/vtt',
              tool_name: 'record_call',
              labels: { site: 'nord' },
              cue_start: null,
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
      const body =
        snippetButtons(element)[0].querySelector('.snippet-text')!.textContent!;
      expect(body.trim()).to.equal('Receiving Lead: a damaged pallet');
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

    it('drops a stale ?artifact= when a snippet opens another session', async () => {
      const element = await renderedSearch();
      // An earlier landing on an artifact of some other session.
      (element as any).focusArtifactId = 'artifact-of-session-1';
      (element as any).syncUrl();
      expect(
        new URLSearchParams(window.location.search).get('artifact')
      ).to.equal('artifact-of-session-1');

      snippetButtons(element)[1].click();
      await element.updateComplete;

      expect((element as any).selectedSessionId).to.equal('runtime-session-2');
      expect((element as any).focusArtifactId).to.equal(null);
      const params = new URLSearchParams(window.location.search);
      expect(params.get('artifact')).to.equal(null);
      expect(params.get('turn')).to.equal('tool-7');
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
        const meta = observer.shadowRoot?.querySelector('.session-facts');
        return Boolean(meta && meta.textContent?.includes('Tokens'));
      }, 'Observer toolbar never showed session ledger numbers');

      const meta = observer.shadowRoot!.querySelector('.session-facts')!;
      const text = (meta.textContent || '').replace(/\s+/g, ' ').trim();
      expect(text).to.not.equal('0 tokens · $0.00');
      expect(text).to.contain('Tokens 700');
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

  describe('paging', () => {
    function sessionItem(id: string) {
      return {
        id,
        session_source_type: 'flow_execution',
        session_source_id: id,
        session_reference: id,
        started_at: '2026-03-09T18:00:00Z',
        last_activity_at: '2026-03-09T20:00:00Z',
        ended_at: null,
        activity_status: 'ended',
      };
    }

    function listPage(total: number, offset: number, limit: number) {
      const items = Array.from({ length: Math.max(0, total - offset) })
        .slice(0, limit)
        .map((_unused, index) =>
          sessionItem(`runtime-session-${offset + index}`)
        );
      return new Response(
        JSON.stringify({
          period_start: '2026-02-08T00:00:00Z',
          period_end: '2026-03-09T23:59:59Z',
          query: null,
          session_source_type: null,
          status: 'all',
          total,
          limit,
          offset,
          items,
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } }
      );
    }

    function stubPagedSessions(total: number) {
      fetchStub.callsFake(async (input: RequestInfo | URL) => {
        const url = typeof input === 'string' ? input : input.toString();
        if (url.startsWith('/api/v1/runtime-sessions?')) {
          const params = new URL(url, window.location.origin).searchParams;
          const offset = Number(params.get('offset') ?? 0);
          const limit = Number(params.get('limit') ?? 50);
          return listPage(total, offset, limit);
        }
        return new Response('{}', {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      });
    }

    function countLabel(element: RuntimeSessionsView): string {
      return element
        .shadowRoot!.querySelector('list-toolbar')!
        .querySelector('.session-count')!
        .textContent!.trim();
    }

    function loadMoreButton(element: RuntimeSessionsView): HTMLElement | null {
      return element.shadowRoot!.querySelector(
        '[data-testid="load-more-sessions"]'
      );
    }

    it('pages the list with Load more and keeps the count label honest', async () => {
      stubPagedSessions(50);
      const element = (await fixture(
        html`<runtime-sessions-view></runtime-sessions-view>`
      )) as RuntimeSessionsView;

      await waitUntil(
        () => !(element as any).loading,
        'Runtime sessions view did not finish loading'
      );
      await element.updateComplete;

      // 20 per page (#1585), so the default list fits in about two viewports.
      const firstUrl = fetchStub
        .getCalls()
        .map((call) => String(call.args[0]))
        .find((url) => url.startsWith('/api/v1/runtime-sessions?'));
      expect(firstUrl).to.contain(`limit=${SESSION_PAGE_SIZE}`);
      expect(SESSION_PAGE_SIZE).to.equal(20);
      expect(countLabel(element)).to.equal('20 of 50 sessions');
      expect(loadMoreButton(element)).to.not.equal(null);

      loadMoreButton(element)!.click();
      await waitUntil(
        () => (element as any).sessions.items.length === 40,
        'Second page did not append'
      );
      await element.updateComplete;
      expect(countLabel(element)).to.equal('40 of 50 sessions');

      loadMoreButton(element)!.click();
      await waitUntil(
        () => (element as any).sessions.items.length === 50,
        'Final page did not append'
      );
      await element.updateComplete;
      expect(countLabel(element)).to.equal('50 sessions');
      expect(loadMoreButton(element)).to.equal(null);
    });

    it('keeps a linked session that is not on the first page (#1585)', async () => {
      const linked = 'runtime-session-35';
      window.history.replaceState(
        {},
        '',
        `/console/runtime-sessions?sessionId=${linked}`
      );
      fetchStub.callsFake(async (input: RequestInfo | URL) => {
        const url = typeof input === 'string' ? input : input.toString();
        if (url.startsWith('/api/v1/runtime-sessions?')) {
          const params = new URL(url, window.location.origin).searchParams;
          return listPage(
            50,
            Number(params.get('offset') ?? 0),
            Number(params.get('limit') ?? 50)
          );
        }
        if (url === `/api/v1/runtime-sessions/${linked}`) {
          return new Response(
            JSON.stringify({ session: sessionItem(linked) }),
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

      expect((element as any).selectedSessionId).to.equal(linked);
      expect(
        new URLSearchParams(window.location.search).get('sessionId')
      ).to.equal(linked);
      const observer = element.shadowRoot!.querySelector(
        'preloop-session-observer'
      ) as HTMLElement & { sessions: Array<{ id: string }> };
      expect(observer.sessions[0].id).to.equal(linked);
      await waitUntil(() =>
        observer.shadowRoot?.querySelector('sl-copy-button')
      );
      expect(
        (observer.shadowRoot!.querySelector('sl-copy-button') as any).value
      ).to.equal(linked);
      expect(
        observer
          .shadowRoot!.querySelector('#session-title')!
          .textContent?.trim()
      ).to.not.equal('');
      // The page and its count stay what the server said.
      expect((element as any).sessions.items).to.have.length(SESSION_PAGE_SIZE);
      expect(countLabel(element)).to.equal('20 of 50 sessions');

      // A live refresh must not swap the pinned link for the newest session.
      await (element as any).loadSessions(true);
      await element.updateComplete;
      expect((element as any).selectedSessionId).to.equal(linked);
      expect(
        new URLSearchParams(window.location.search).get('sessionId')
      ).to.equal(linked);

      // A filter change re-scopes the list: the pin goes, the new list's
      // first row is selected.
      (element as any).status = 'ended';
      await (element as any).applyFilters();
      await element.updateComplete;
      expect((element as any).selectedSessionId).to.equal('runtime-session-0');
      expect(observer.sessions[0].id).to.equal('runtime-session-0');
    });

    it('falls back to the newest session when a linked one cannot be read', async () => {
      window.history.replaceState(
        {},
        '',
        '/console/runtime-sessions?sessionId=gone'
      );
      stubPagedSessions(50);
      const element = (await fixture(
        html`<runtime-sessions-view></runtime-sessions-view>`
      )) as RuntimeSessionsView;
      await waitUntil(
        () => !(element as any).loading,
        'Runtime sessions view did not finish loading'
      );
      expect((element as any).selectedSessionId).to.equal('runtime-session-0');
    });

    it('keeps the loaded rows when loading more fails', async () => {
      let failMore = false;
      fetchStub.callsFake(async (input: RequestInfo | URL) => {
        const url = typeof input === 'string' ? input : input.toString();
        if (url.startsWith('/api/v1/runtime-sessions?')) {
          const params = new URL(url, window.location.origin).searchParams;
          const offset = Number(params.get('offset') ?? 0);
          if (offset > 0 && failMore) {
            return new Response(JSON.stringify({ detail: 'boom' }), {
              status: 500,
              headers: { 'Content-Type': 'application/json' },
            });
          }
          return listPage(60, offset, 50);
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

      failMore = true;
      loadMoreButton(element)!.click();
      await waitUntil(
        () => element.shadowRoot!.querySelector('[data-testid="more-error"]'),
        'No load-more error rendered'
      );
      await element.updateComplete;
      expect((element as any).sessions.items.length).to.equal(50);
      expect(loadMoreButton(element)).to.not.equal(null);

      failMore = false;
      loadMoreButton(element)!.click();
      await waitUntil(
        () => (element as any).sessions.items.length === 60,
        'Rows did not append after the error cleared'
      );
      await element.updateComplete;
      expect(loadMoreButton(element)).to.equal(null);
      expect(
        element.shadowRoot!.querySelector('[data-testid="more-error"]')
      ).to.equal(null);
    });

    it('does not append a session the next page repeats', async () => {
      fetchStub.callsFake(async (input: RequestInfo | URL) => {
        const url = typeof input === 'string' ? input : input.toString();
        if (url.startsWith('/api/v1/runtime-sessions?')) {
          const params = new URL(url, window.location.origin).searchParams;
          const offset = Number(params.get('offset') ?? 0);
          if (offset > 0) {
            const repeated = sessionItem('runtime-session-49');
            const fresh = sessionItem('runtime-session-50');
            return new Response(
              JSON.stringify({
                period_start: '2026-02-08T00:00:00Z',
                period_end: '2026-03-09T23:59:59Z',
                query: null,
                session_source_type: null,
                status: 'all',
                total: 51,
                limit: 50,
                offset,
                items: [repeated, fresh],
              }),
              { status: 200, headers: { 'Content-Type': 'application/json' } }
            );
          }
          return listPage(51, 0, 50);
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

      loadMoreButton(element)!.click();
      await waitUntil(
        () => (element as any).sessions.items.length === 51,
        'Load more did not settle'
      );
      await element.updateComplete;

      const ids = (element as any).sessions.items.map(
        (item: { id: string }) => item.id
      );
      expect(ids).to.have.length(51);
      expect(new Set(ids).size).to.equal(51);
      expect(ids[ids.length - 1]).to.equal('runtime-session-50');
    });

    it('clears the loading flag when a refresh supersedes a load-more', async () => {
      let resolveMore: ((response: Response) => void) | null = null;
      fetchStub.callsFake(async (input: RequestInfo | URL) => {
        const url = typeof input === 'string' ? input : input.toString();
        if (url.startsWith('/api/v1/runtime-sessions?')) {
          const params = new URL(url, window.location.origin).searchParams;
          const offset = Number(params.get('offset') ?? 0);
          if (offset > 0) {
            // Hold the "load more" page open so a refresh can supersede it
            // while it is still in flight.
            return new Promise<Response>((resolve) => {
              resolveMore = resolve;
            });
          }
          return listPage(120, 0, 50);
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

      const pending = (element as any).loadMoreSessions();
      await waitUntil(() => resolveMore !== null, 'Load more did not start');
      expect((element as any).loadingMore).to.equal(true);

      // A live refresh supersedes the in-flight page and bumps loadSequence.
      await (element as any).loadSessions(true);
      await element.updateComplete;

      resolveMore!(listPage(120, 50, 50));
      await pending;
      await element.updateComplete;

      expect((element as any).loadingMore).to.equal(false);
      const button = loadMoreButton(element);
      expect(button).to.not.equal(null);
      expect(button!.hasAttribute('loading')).to.equal(false);
    });
  });
  describe('sessions layout and accessibility (#2427–#2431)', () => {
    async function layoutView(): Promise<RuntimeSessionsView> {
      const element = await fixture<RuntimeSessionsView>(
        html`<runtime-sessions-view></runtime-sessions-view>`
      );
      await waitUntil(() => !(element as any).loading);
      await element.updateComplete;
      await (
        element.shadowRoot!.querySelector('preloop-session-observer') as any
      )?.updateComplete;
      return element;
    }

    function observerOf(element: RuntimeSessionsView): any {
      return element.shadowRoot!.querySelector('preloop-session-observer');
    }

    function rowsOf(element: RuntimeSessionsView): HTMLAnchorElement[] {
      return Array.from(
        observerOf(element)
          .shadowRoot.querySelector('session-list-panel')
          .shadowRoot.querySelectorAll('a.session-select')
      );
    }

    it('opens a phone selection immediately, returns to the list and restores browser history', async () => {
      await loadShoelaceTokens();
      await setViewport({ width: 390, height: 844 });
      const element = await layoutView();
      const observer = observerOf(element);
      const content = observer.shadowRoot.querySelector(
        '.content'
      ) as HTMLElement;
      const sidebar = observer.shadowRoot.querySelector(
        '.sidebar'
      ) as HTMLElement;
      expect((element as any).selectedSessionId).to.equal(null);
      expect(getComputedStyle(content).display).to.equal('none');
      rowsOf(element)[0].focus();
      await sendKeys({ press: 'ArrowDown' });
      await sendKeys({ press: 'Enter' });
      await waitUntil(
        () => (element as any).selectedSessionId === 'runtime-session-2'
      );
      await element.updateComplete;
      await observer.updateComplete;
      expect(getComputedStyle(sidebar).display).to.equal('none');
      expect(getComputedStyle(content).display).to.not.equal('none');
      expect(observer.shadowRoot.activeElement?.id).to.equal('session-title');
      expect(
        element
          .shadowRoot!.querySelector('.sessions-page')!
          .getBoundingClientRect().width
      ).to.be.at.most(390);
      expect(new URLSearchParams(location.search).get('sessionId')).to.equal(
        'runtime-session-2'
      );
      observer.shadowRoot.querySelector('.back-to-list').click();
      await element.updateComplete;
      await observer.updateComplete;
      expect(getComputedStyle(content).display).to.equal('none');
      expect(getComputedStyle(sidebar).display).to.not.equal('none');
      expect(rowsOf(element)[1].tabIndex).to.equal(0);
      window.history.back();
      await waitUntil(
        () => (element as any).selectedSessionId === 'runtime-session-2'
      );
      await element.updateComplete;
      await observer.updateComplete;
      expect(getComputedStyle(content).display).to.not.equal('none');
    });

    it('opens a phone deep link with its identity and copyable full id', async () => {
      await setViewport({ width: 390, height: 844 });
      history.replaceState(
        {},
        '',
        '/console/runtime-sessions?sessionId=runtime-session-2'
      );
      const element = await layoutView();
      const observer = observerOf(element);
      await waitUntil(() =>
        observer.shadowRoot.querySelector('#session-title')
      );
      expect(
        observer.shadowRoot.querySelector('#session-title').textContent.trim()
      ).to.not.equal('');
      expect(
        observer.shadowRoot.querySelector('sl-copy-button').value
      ).to.equal('runtime-session-2');
      const time = observer.shadowRoot.querySelector('.session-identity time');
      expect(time.textContent).to.contain('ago');
      expect(time.title).to.not.equal('');
      expect(observer.shadowRoot.querySelector('.session-heading .chip')).to
        .exist;
      expect(
        getComputedStyle(observer.shadowRoot.querySelector('.sidebar')).display
      ).to.equal('none');
    });

    it('takes a phone search hit to detail and back to the same results', async () => {
      await setViewport({ width: 390, height: 844 });
      const element = await renderedSearch();
      const searchList =
        element.shadowRoot!.querySelector<HTMLElement>('.search-list')!;
      expect(getComputedStyle(searchList).display).to.not.equal('none');
      element.shadowRoot!.querySelector<HTMLAnchorElement>('.snippet')!.click();
      await element.updateComplete;
      const observer = observerOf(element);
      await observer.updateComplete;
      expect(getComputedStyle(searchList).display).to.equal('none');
      expect(
        getComputedStyle(observer.shadowRoot.querySelector('.content')).display
      ).to.not.equal('none');
      observer.shadowRoot.querySelector('.back-to-list').click();
      await element.updateComplete;
      expect(getComputedStyle(searchList).display).to.not.equal('none');
      expect((element as any).searchQuery).to.equal('rollout plan');
      expect(element.shadowRoot!.querySelectorAll('.snippet')).to.have.length(
        2
      );
    });

    it('skips the list to its named detail and focuses search with slash', async () => {
      const element = await layoutView();
      const observer = observerOf(element);
      await waitUntil(() => observer.shadowRoot.querySelector('.skip-detail'));
      observer.shadowRoot.querySelector('.skip-detail').focus();
      await sendKeys({ press: 'Enter' });
      expect(observer.shadowRoot.activeElement?.id).to.equal('session-title');
      await sendKeys({ press: '/' });
      const toolbar = element.shadowRoot!.querySelector('list-toolbar')!;
      const input = toolbar.shadowRoot!.querySelector('sl-input') as any;
      expect(toolbar.shadowRoot!.activeElement).to.equal(input);
      await sendKeys({ type: '/' });
      expect(input.value).to.equal('/');
    });

    it('focuses the detail when Enter activates the already selected row', async () => {
      const element = await layoutView();
      const observer = observerOf(element);
      await waitUntil(() => observer.activeSessionId === 'runtime-session-1');
      rowsOf(element)[0].focus();
      await sendKeys({ press: 'Enter' });
      await waitUntil(
        () => observer.shadowRoot.activeElement?.id === 'session-title'
      );
      expect((element as any).selectedSessionId).to.equal('runtime-session-1');
    });

    it('does not page stale rows while a replacement list is pending', async () => {
      const element = await layoutView();
      const original = (element as any).sessions;
      (element as any).sessions = { ...original, total: 50 };
      let finish!: (response: Response) => void;
      fetchStub.callsFake(
        () =>
          new Promise<Response>((resolve) => {
            finish = resolve;
          })
      );
      const before = fetchStub.callCount;
      const replacement = (element as any).loadSessions(true);
      await element.updateComplete;
      const button = element.shadowRoot!.querySelector(
        '[data-testid="load-more-sessions"]'
      ) as any;
      expect(button.disabled).to.be.true;
      const page = (element as any).loadMoreSessions();
      expect(fetchStub.callCount - before).to.equal(1);
      finish(
        new Response(JSON.stringify(original), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      );
      await replacement;
      await page;
      expect((element as any).sessions.items).to.deep.equal(original.items);
    });

    it('renders cost order from the API across replacement and appended pages', async () => {
      const element = await layoutView();
      const response = (element as any).sessions;
      const [recent, older] = response.items;
      const third = {
        ...recent,
        id: 'third-session',
        last_activity_at: '2027-01-01T00:00:00Z',
      };
      fetchStub
        .withArgs(
          sinon.match(
            (input: unknown) =>
              String(input).startsWith('/api/v1/runtime-sessions?') &&
              String(input).includes('sort=cost')
          )
        )
        .callsFake(async (input: RequestInfo | URL) => {
          const offset = Number(
            new URL(String(input), location.origin).searchParams.get('offset')
          );
          return new Response(
            JSON.stringify({
              ...response,
              total: 3,
              has_more: offset === 0,
              items: offset ? [third] : [older, recent],
            }),
            { status: 200, headers: { 'Content-Type': 'application/json' } }
          );
        });
      const select = element.shadowRoot!.querySelector(
        'sl-select[label="Sort"]'
      ) as any;
      select.value = 'cost';
      select.dispatchEvent(new CustomEvent('sl-change', { bubbles: true }));
      const ids = () =>
        rowsOf(element).map((row) =>
          new URL(row.href).searchParams.get('sessionId')
        );
      await waitUntil(() => ids()[0] === older.id);
      expect(ids()).to.deep.equal([older.id, recent.id]);
      await (element as any).loadMoreSessions();
      await element.updateComplete;
      await waitUntil(() => rowsOf(element).length === 3);
      expect(ids()).to.deep.equal([older.id, recent.id, 'third-session']);
    });

    it('hides free-account list notices on phone detail and restores them on Back', async () => {
      await setViewport({ width: 390, height: 844 });
      const element = await layoutView();
      (element as any).isPremium = false;
      (element as any).newSessionError = 'Session could not start';
      (element as any).error = 'List refresh failed';
      await element.updateComplete;
      rowsOf(element)[0].click();
      await element.updateComplete;
      const notices = Array.from(
        element.shadowRoot!.querySelectorAll<HTMLElement>(
          '.titles-upsell-hint, .new-session-error, sl-alert[variant="danger"]'
        )
      );
      expect(notices).to.have.length(3);
      for (const notice of notices)
        expect(getComputedStyle(notice).display).to.equal('none');
      const observer = observerOf(element);
      await observer.updateComplete;
      observer.shadowRoot.querySelector('.back-to-list').click();
      await element.updateComplete;
      for (const notice of notices)
        expect(getComputedStyle(notice).display).to.not.equal('none');
    });

    it('keeps Clear filters outside the live count region', async () => {
      const element = await layoutView();
      (element as any).status = 'active';
      await element.updateComplete;
      const toolbar = element.shadowRoot!.querySelector('list-toolbar')!;
      const actions = toolbar.querySelector<HTMLElement>('.filter-actions')!;
      expect(actions.assignedSlot).to.exist;
      expect(actions.assignedSlot!.closest('[aria-live]')).to.equal(null);
      const count = toolbar.querySelector<HTMLElement>('.session-count')!;
      expect(
        count.assignedSlot!.closest('[aria-live]')?.getAttribute('aria-live')
      ).to.equal('polite');
    });

    it('sorts via the API, persists the choice and starts at the first page', async () => {
      const element = await layoutView();
      const select = element.shadowRoot!.querySelector(
        'sl-select[label="Sort"]'
      ) as any;
      select.value = 'cost';
      select.dispatchEvent(new CustomEvent('sl-change', { bubbles: true }));
      await waitUntil(() =>
        fetchStub
          .getCalls()
          .some((call) => String(call.args[0]).includes('sort=cost'))
      );
      const request = fetchStub
        .getCalls()
        .find((call) => String(call.args[0]).includes('sort=cost'))!;
      const params = new URL(String(request.args[0]), location.origin)
        .searchParams;
      expect(params.get('offset')).to.equal(null);
      expect(new URLSearchParams(location.search).get('sort')).to.equal('cost');
      await waitUntil(() => !(element as any).listLoadInFlight);
      (element as any).sessions = { ...(element as any).sessions, total: 372 };
      await element.updateComplete;
      expect(
        element.shadowRoot!.querySelector('.session-count')!.textContent
      ).to.equal('2 of 372 sessions');
      const observer = observerOf(element);
      const slot = observer.shadowRoot.querySelector(
        '.sidebar slot[name="list-footer"]'
      ) as HTMLSlotElement;
      expect(
        slot
          .assignedElements()[0]
          .querySelector('[data-testid="load-more-sessions"]')
      ).to.exist;
      (element as any).sessions = {
        ...(element as any).sessions,
        total: null,
        has_more: true,
      };
      await element.updateComplete;
      expect(
        element.shadowRoot!.querySelector('.session-count')!.textContent
      ).to.equal('2 sessions loaded');
      expect(
        element.shadowRoot!.querySelector('[data-testid="load-more-sessions"]')
      ).to.exist;
    });
  });
});

describe('runtime session path route', () => {
  it('path id route redirects to sessionId query', () => {
    expect(runtimeSessionPathRedirect('abc-123')).to.equal(
      '/console/runtime-sessions?sessionId=abc-123'
    );
    expect(
      runtimeSessionPathRedirect('abc-123', '?replay=replay', '#turn-4')
    ).to.equal(
      '/console/runtime-sessions?replay=replay&sessionId=abc-123#turn-4'
    );
  });
});
