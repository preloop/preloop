import { expect, fixture, html, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import './agent-detail-view.ts';
import type { AgentDetailView } from './agent-detail-view';
import { invalidateApiCaches } from '../../api';
import { historyStateForNavigation } from '../../utils/in-app-history';

describe('AgentDetailView', () => {
  let fetchStub: sinon.SinonStub;
  let defaultFetch: (
    input: RequestInfo | URL,
    init?: RequestInit
  ) => Promise<Response>;

  function getDeepText(el: Element | null | undefined): string {
    if (!el) return '';
    let text = el.textContent || '';
    if (el.shadowRoot) {
      text += ' ' + getDeepText(el.shadowRoot);
    }
    const children = Array.from(el.children);
    for (const child of children) {
      text += ' ' + getDeepText(child);
    }
    if (el.shadowRoot) {
      const shadowChildren = Array.from(el.shadowRoot.children);
      for (const child of shadowChildren) {
        text += ' ' + getDeepText(child);
      }
    }
    return text;
  }

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');

    fetchStub = sinon.stub(window, 'fetch');
    defaultFetch = async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.toString();

      if (
        url.startsWith('/api/v1/agents/agent-1') &&
        !url.includes('/governance')
      ) {
        return new Response(
          JSON.stringify({
            agent: {
              id: 'agent-1',
              runtime_session_id: 'runtime-session-2',
              display_name: 'Claude Code Workspace',
              session_source_type: 'claude_code',
              session_source_id: 'claude-code-agent-1',
              session_reference: 'claude-session-2',
              enrolled_via: 'runtime_session_token',
              managed_mcp_servers: ['github', 'jira'],
              lifecycle_state: 'active',
              lifecycle_reason: null,
              lifecycle_updated_at: '2026-03-10T10:00:00Z',
              is_active_now: true,
              activity_status: 'active_now',
              turn_state: 'your_turn',
              last_seen_at: '2026-03-10T10:00:00Z',
              started_at: '2026-03-10T09:00:00Z',
              last_activity_at: '2026-03-10T10:00:00Z',
              ended_at: null,
              total_requests: 1,
              estimated_cost: 0.12,
              configured_model_alias: 'openai/gpt-5',
              configured_model_id: 'configured-model-1',
              latest_model_alias: 'openai/gpt-5',
              latest_provider_name: 'openai',
              last_request_at: '2026-03-10T09:58:00Z',
              mcp_proxy_configured: true,
              model_gateway_configured: true,
              onboarding_state: 'fully_onboarded',
              live_validation_supported: true,
              live_validation_passed: true,
              live_validation_status: 'passed',
              last_validated_at: '2026-03-10T10:01:00Z',
              owner_user_id: null,
              owner_username: null,
              owner_email: null,
            },
            aggregate: {
              session_count: 2,
              total_requests: 4,
              successful_requests: 3,
              failed_requests: 1,
              token_usage: {
                prompt_tokens: 300,
                completion_tokens: 120,
                total_tokens: 420,
              },
              estimated_cost: 0.57,
              latest_model_alias: 'openai/gpt-5',
              latest_provider_name: 'openai',
              last_request_at: '2026-03-10T09:58:00Z',
            },
            usage_by_model: [
              {
                ai_model_id: 'model-1',
                model_alias: 'openai/gpt-5',
                provider_name: 'openai',
                request_count: 4,
                token_usage: {
                  prompt_tokens: 300,
                  completion_tokens: 120,
                  total_tokens: 420,
                },
                estimated_cost: 0.57,
              },
            ],
            activity_by_server: [
              {
                server_name: 'github',
                call_count: 2,
                successful_calls: 2,
                failed_calls: 0,
                last_activity_at: '2026-03-10T10:00:00Z',
              },
            ],
            activity_by_tool: [
              {
                server_name: 'github',
                tool_name: 'search_issues',
                call_count: 2,
                successful_calls: 2,
                failed_calls: 0,
                last_activity_at: '2026-03-10T10:00:00Z',
              },
            ],
            sessions: [
              {
                id: 'runtime-session-2',
                session_source_type: 'claude_code',
                session_source_id: 'workspace-2',
                session_reference: null,
                runtime_principal_type: 'claude_code',
                runtime_principal_id: 'claude-code-agent-1',
                runtime_principal_name: 'Claude Code Workspace',
                started_at: '2026-03-10T09:30:00Z',
                last_activity_at: '2026-03-10T09:59:00Z',
                ended_at: null,
                flow_id: null,
                flow_name: null,
                flow_execution_id: null,
                latest_model_alias: 'openai/gpt-5',
                latest_provider_name: 'openai',
                is_active_now: true,
                activity_status: 'active_now',
                total_requests: 1,
                successful_requests: 1,
                failed_requests: 0,
                token_usage: {
                  prompt_tokens: 100,
                  completion_tokens: 20,
                  total_tokens: 120,
                },
                estimated_cost: 0.12,
                last_request_at: '2026-03-10T09:59:00Z',
              },
            ],
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        );
      }

      if (url === '/api/v1/account/governance-defaults') {
        return new Response(
          JSON.stringify({
            defaults: {
              native_tool_approvals: null,
              approval_workflow_id: null,
            },
            override_agent_ids: [],
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        );
      }

      if (url === '/api/v1/agents/agent-1/governance') {
        if (init?.method === 'PUT') {
          // Echo the submitted config back, as the real endpoint does.
          const submitted = JSON.parse(String(init.body));
          return new Response(
            JSON.stringify({
              subject_type: 'managed_agents',
              subject_id: 'agent-1',
              config: {
                allowed_models: [],
                model_budgets: {},
                tool_rules: {},
                ...submitted,
              },
            }),
            { status: 200, headers: { 'Content-Type': 'application/json' } }
          );
        }
        return new Response(
          JSON.stringify({
            subject_type: 'managed_agents',
            subject_id: 'agent-1',
            config: {
              allowed_models: ['openai/gpt-5'],
              model_budgets: {
                'openai/gpt-5': { monthly_usd_limit: 25 },
              },
              tool_rules: {
                search_issues: [{ action: 'allow', condition_type: 'simple' }],
              },
            },
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        );
      }

      if (url === '/api/v1/runtime-sessions/runtime-session-2') {
        return new Response(
          JSON.stringify({
            period_start: '2026-03-01T00:00:00Z',
            period_end: '2026-03-10T23:59:59Z',
            session: {
              id: 'runtime-session-2',
              session_source_type: 'claude_code',
              session_source_id: 'workspace-2',
              session_reference: null,
              runtime_principal_type: 'claude_code',
              runtime_principal_id: 'claude-code-agent-1',
              runtime_principal_name: 'Claude Code Workspace',
              started_at: '2026-03-10T09:30:00Z',
              last_activity_at: '2026-03-10T09:59:00Z',
              ended_at: null,
              flow_id: null,
              flow_name: null,
              flow_execution_id: null,
              latest_model_alias: 'openai/gpt-5',
              latest_provider_name: 'openai',
              is_active_now: true,
              activity_status: 'active_now',
              total_requests: 1,
              successful_requests: 1,
              failed_requests: 0,
              token_usage: {
                prompt_tokens: 100,
                completion_tokens: 20,
                total_tokens: 120,
              },
              estimated_cost: 0.12,
              last_request_at: '2026-03-10T09:59:00Z',
            },
            usage_by_model: [],
            interactions: {
              period_start: '2026-03-01T00:00:00Z',
              period_end: '2026-03-10T23:59:59Z',
              query: null,
              total: 0,
              limit: 10,
              offset: 0,
              items: [],
            },
            activity_timeline: [
              {
                activity_type: 'tool_call',
                timestamp: '2026-03-10T09:59:00Z',
                title: 'github / search_issues',
                summary: 'Completed successfully',
                status: 'success',
                api_usage_id: null,
                tool_name: 'search_issues',
                server_name: 'github',
                auth_subject_type: null,
                api_key_id: null,
                api_key_name: null,
                estimated_cost: null,
                total_tokens: null,
              },
            ],
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        );
      }

      if (url === '/api/v1/users') {
        return new Response(JSON.stringify({ users: [] }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }

      if (url === '/api/v1/tools') {
        return new Response(
          JSON.stringify([
            {
              name: 'search_issues',
              description: 'Search GitHub issues',
              schema: {
                properties: {
                  query: { type: 'string' },
                },
              },
            },
          ]),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        );
      }

      if (url === '/api/v1/approval-workflows') {
        return new Response(
          JSON.stringify([
            {
              id: 'wf-1',
              name: 'Default Approval',
              approval_type: 'standard',
            },
          ]),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        );
      }

      if (url === '/api/v1/features') {
        return new Response(JSON.stringify({ features: {} }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }

      if (url === '/api/v1/mcp-servers') {
        return new Response(JSON.stringify([]), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }

      if (url === '/api/v1/flows') {
        return new Response(JSON.stringify([]), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }

      if (
        url.includes(
          '/api/v1/runtime-sessions/runtime-session-2/gateway-events'
        )
      ) {
        return new Response(
          JSON.stringify({
            logs: [
              {
                id: 'event-1',
                timestamp: '2026-03-10T09:58:00Z',
                type: 'model_gateway_call',
                payload: {
                  api_usage_id: 'usage-1',
                  model_alias: 'openai/gpt-5',
                  provider_name: 'openai',
                  outcome: 'success',
                  estimated_cost: 0.12,
                  total_tokens: 120,
                  prompt_tokens: 100,
                  completion_tokens: 20,
                  status_code: 200,
                  method: 'POST',
                  endpoint: '/openai/v1/responses',
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

      if (url.includes('/api/v1/runtime-sessions/runtime-session-2/activity')) {
        return new Response(
          JSON.stringify({
            items: [
              {
                activity_type: 'tool_call',
                timestamp: '2026-03-10T09:59:00Z',
                title: 'github / search_issues',
                summary: 'Completed successfully',
                status: 'success',
                api_usage_id: null,
                tool_name: 'search_issues',
                server_name: 'github',
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

      if (url === '/api/v1/ai-models') {
        return new Response(
          JSON.stringify([
            {
              id: 'model-openai-gpt-5',
              name: 'openai/gpt-5',
              provider_name: 'openai',
              model_identifier: 'gpt-5',
            },
            {
              id: 'model-anthropic-claude',
              name: 'anthropic/claude-sonnet-4',
              provider_name: 'anthropic',
              model_identifier: 'claude-sonnet-4',
            },
          ]),
          {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          }
        );
      }

      return new Response(
        JSON.stringify({ detail: `Unhandled request: ${url}` }),
        { status: 500, headers: { 'Content-Type': 'application/json' } }
      );
    };
    fetchStub.callsFake(defaultFetch);
  });

  afterEach(() => {
    fetchStub.restore();
    localStorage.clear();
  });

  describe('live desktop (#870)', () => {
    const endpoint = {
      id: '00000000-0000-4000-8000-000000000001',
      endpoint_id: 'host-endpoint-1',
      hostname: 'agent-vm.example.com',
      display: ':99',
      managed_agent_id: 'agent-1',
      runner_id: null,
      state: 'online',
      last_seen_at: '2026-10-11T18:00:00Z',
      active_view: null,
    };

    function withEndpoints(respond: () => Response) {
      fetchStub.callsFake(
        async (input: RequestInfo | URL, init?: RequestInit) => {
          const url = input.toString();
          if (url.startsWith('/api/v1/desktop/endpoints?')) return respond();
          return defaultFetch(input, init);
        }
      );
    }

    const desktopTab = (el: AgentDetailView) =>
      el.shadowRoot!.querySelector('sl-tab[panel="desktop"]');

    it('shows a Desktop tab with the viewer when an online endpoint is linked', async () => {
      withEndpoints(
        () =>
          new Response(JSON.stringify({ endpoints: [endpoint] }), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          })
      );
      const el = await fixture<AgentDetailView>(
        html`<agent-detail-view agentId="agent-1"></agent-detail-view>`
      );
      await waitUntil(() => desktopTab(el), 'Desktop tab', { timeout: 5000 });
      const lookup = fetchStub
        .getCalls()
        .map((call) => call.args[0].toString())
        .find((url) => url.startsWith('/api/v1/desktop/endpoints?'))!;
      expect(lookup).to.contain('managed_agent_id=agent-1');
      expect(lookup).to.contain('state=online');
      expect(desktopTab(el)!.textContent).to.contain('Desktop');

      (el as any).activeTab = 'desktop';
      await el.updateComplete;
      const card = el.shadowRoot!.querySelector('.desktop-tab')!;
      expect(card.textContent).to.contain('agent-vm.example.com');
      const viewer = card.querySelector('desktop-view') as any;
      expect(viewer).to.exist;
      expect(viewer.endpointId).to.equal(endpoint.id);
      await viewer.updateComplete;
      // Nothing opens until the operator asks.
      expect(viewer.viewerState).to.equal('idle');
      expect(
        viewer.shadowRoot.querySelector('sl-button.open').textContent
      ).to.contain('Open live view');
    });

    it('has no Desktop tab when the server has desktop views off (404)', async () => {
      withEndpoints(
        () =>
          new Response(
            JSON.stringify({
              detail: { code: 'desktop_views_disabled', message: 'off' },
            }),
            { status: 404, headers: { 'Content-Type': 'application/json' } }
          )
      );
      const el = await fixture<AgentDetailView>(
        html`<agent-detail-view agentId="agent-1"></agent-detail-view>`
      );
      await waitUntil(() => !(el as any).loading);
      await waitUntil(() =>
        fetchStub
          .getCalls()
          .some((call) =>
            call.args[0].toString().startsWith('/api/v1/desktop/endpoints?')
          )
      );
      await new Promise((resolve) => setTimeout(resolve, 50));
      await el.updateComplete;
      expect(desktopTab(el)).to.be.null;
    });

    it('has no Desktop tab when no linked endpoint is online', async () => {
      withEndpoints(
        () =>
          new Response(
            JSON.stringify({ endpoints: [{ ...endpoint, state: 'offline' }] }),
            { status: 200, headers: { 'Content-Type': 'application/json' } }
          )
      );
      const el = await fixture<AgentDetailView>(
        html`<agent-detail-view agentId="agent-1"></agent-detail-view>`
      );
      await waitUntil(() => !(el as any).loading);
      await new Promise((resolve) => setTimeout(resolve, 100));
      await el.updateComplete;
      expect(desktopTab(el)).to.be.null;
    });
  });

  // D-05: the strip states the without-subscriptions estimate beside spend.
  it('states what the spend would have been without subscriptions', async () => {
    const el = await fixture<any>(
      html`<agent-detail-view agentId="agent-1"></agent-detail-view>`
    );
    await waitUntil(() => el.agent, 'agent loaded');
    el.aggregate = {
      ...el.aggregate,
      estimated_cost: 0.16,
      subscription_usage: {
        request_count: 3,
        prompt_tokens: 3000,
        completion_tokens: 300,
        total_tokens: 3300,
        api_equivalent_cost: 1.75,
        api_equivalent_cost_is_estimate: true,
        covered_requests: 3,
        coverage: 1,
        billed: null,
        billed_available: false,
      },
    };
    el.requestUpdate();
    await el.updateComplete;
    const line = el.shadowRoot.querySelector(
      '[data-testid="agent-without-subscriptions"]'
    );
    expect(line.textContent.replace(/\s+/g, ' ').trim()).to.equal(
      'Without subscriptions: $1.91 (estimate)'
    );
    expect(line.getAttribute('title')).to.contain('Estimate, not a bill');
  });

  it('defers all editor catalogs on the sessions tab and shares a single load across editors', async () => {
    const el = await fixture<AgentDetailView>(
      html`<agent-detail-view agentId="agent-1"></agent-detail-view>`
    );
    await waitUntil(() => !(el as any).loading);
    await (el as any).loadInFlight;
    const catalogs = [
      '/api/v1/tools',
      '/api/v1/mcp-servers',
      '/api/v1/approval-workflows',
      '/api/v1/agents/agent-1/governance',
      '/api/v1/ai-models',
    ];
    const calls = () =>
      fetchStub
        .getCalls()
        .filter((call) =>
          catalogs.includes(call.args[0].toString().split('?')[0])
        );
    expect(calls()).to.have.length(0);
    (el as any).activeTab = 'tools';
    await waitUntil(() => (el as any).editorContextReady);
    expect(calls()).to.have.length(5);
    (el as any).activeTab = 'models';
    await el.updateComplete;
    expect(calls()).to.have.length(5);
  });

  it('ignores an old primary response after changing the agent route', async () => {
    let release!: () => void;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    fetchStub.callsFake(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = input.toString();
        if (/\/agents\/agent-1(?:\?|$)/.test(url)) await held;
        if (/\/agents\/agent-2(?:\?|$)/.test(url)) {
          const response = await defaultFetch(
            url.replace('agent-2', 'agent-1'),
            init
          );
          const body = await response.json();
          body.agent.id = 'agent-2';
          body.agent.display_name = 'New agent';
          return new Response(JSON.stringify(body), {
            headers: { 'Content-Type': 'application/json' },
          });
        }
        return defaultFetch(input, init);
      }
    );
    const el = await fixture<AgentDetailView>(
      html`<agent-detail-view agentId="agent-1"></agent-detail-view>`
    );
    try {
      (el as any).onBeforeEnter({ params: { agentId: 'agent-2' } });
      expect((el as any).agent).to.equal(null);
    } finally {
      release();
    }
    await waitUntil(() => (el as any).agent?.id === 'agent-2');
    await el.updateComplete;
    expect(
      el.shadowRoot!.querySelector('view-header')?.getAttribute('headerText')
    ).to.equal('New agent');
  });

  it('blocks governance writes immediately when a route changes during supplemental loading', async () => {
    let releaseUsers!: () => void;
    let releaseGovernance!: () => void;
    const users = new Promise<void>((resolve) => {
      releaseUsers = resolve;
    });
    const governance = new Promise<void>((resolve) => {
      releaseGovernance = resolve;
    });
    let releaseNextGovernance!: () => void;
    const nextGovernance = new Promise<void>((resolve) => {
      releaseNextGovernance = resolve;
    });
    let holdGovernance = false;
    fetchStub.callsFake(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        let url = input.toString();
        if (url === '/api/v1/users') await users;
        if (url.includes('agent-1/governance') && holdGovernance)
          await governance;
        if (url.includes('agent-2/governance')) await nextGovernance;
        const isNext = url.includes('agent-2');
        if (isNext) url = url.replace('agent-2', 'agent-1');
        const response = await defaultFetch(url, init);
        if (isNext && /\/agents\/agent-1(?:\?|$)/.test(url)) {
          const body = await response.json();
          body.agent.id = 'agent-2';
          return new Response(JSON.stringify(body), {
            headers: { 'Content-Type': 'application/json' },
          });
        }
        return response;
      }
    );
    const el = await fixture<AgentDetailView>(
      html`<agent-detail-view agentId="agent-1"></agent-detail-view>`
    );
    await waitUntil(() => !(el as any).loading);
    (el as any).activeTab = 'tools';
    await waitUntil(() => (el as any).editorContextReady);
    holdGovernance = true;
    const staleContext = (el as any).ensureEditorContext(true);
    try {
      (el as any).onBeforeEnter({
        params: { agentId: 'agent-2' },
        search: '?tab=tools',
      });
      expect((el as any).editorContextReady).to.equal(false);
      await (el as any).saveGovernance();
      await (el as any).saveAllowedModels(['old-model']);
      expect(
        fetchStub.getCalls().filter((call) => call.args[1]?.method === 'PUT')
      ).to.have.length(0);
      releaseGovernance();
      await staleContext;
      expect((el as any).editorContextReady).to.equal(false);
    } finally {
      releaseUsers();
      releaseGovernance();
      releaseNextGovernance();
    }
    await waitUntil(() => (el as any).agent?.id === 'agent-2');
    await waitUntil(() => (el as any).editorContextReady);
    await (el as any).saveGovernance();
    expect(
      fetchStub
        .getCalls()
        .some(
          (call) =>
            call.args[0].toString().includes('agent-2/governance') &&
            call.args[1]?.method === 'PUT'
        )
    ).to.equal(true);
  });

  it('renders agent identity while governance is pending and keeps its editor closed', async () => {
    let release!: () => void;
    const pending = new Promise<void>((resolve) => {
      release = resolve;
    });
    fetchStub.callsFake(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        if (input.toString().includes('/governance')) await pending;
        return defaultFetch(input, init);
      }
    );
    const el = await fixture<AgentDetailView>(
      html`<agent-detail-view agentId="agent-1"></agent-detail-view>`
    );
    try {
      await waitUntil(() => !(el as any).loading && !!(el as any).agent);
      await el.updateComplete;
      expect(
        el.shadowRoot!.querySelector('view-header')?.getAttribute('headerText')
      ).to.equal('Claude Code Workspace');
      (el as any).activeTab = 'tools';
      await el.updateComplete;
      expect(el.shadowRoot!.querySelector('tools-editor-component')).not.to
        .exist;
      expect(el.shadowRoot!.textContent).to.include('Loading governance');
    } finally {
      release();
    }
    await waitUntil(() => (el as any).editorContextReady);
    await el.updateComplete;
    expect(el.shadowRoot!.querySelector('tools-editor-component')).to.exist;
  });

  it('lets only the agent owner change who may start sessions', async () => {
    let patched: unknown = null;
    fetchStub.callsFake(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        if (init?.method === 'PATCH' && input.toString().endsWith('/agent-1')) {
          patched = JSON.parse(String(init.body));
        }
        return defaultFetch(input, init);
      }
    );
    const el = await fixture<AgentDetailView>(
      html`<agent-detail-view agentId="agent-1"></agent-detail-view>`
    );
    await waitUntil(() => !(el as any).loading && !!(el as any).agent);
    const control = () =>
      el.shadowRoot!.querySelector('owner-consent-control')!;
    (el as any).currentUserId = 'someone-else';
    (el as any).agent = { ...(el as any).agent, owner_user_id: 'owner-1' };
    await el.updateComplete;
    expect(control().editable).to.equal(false);
    expect(control().ownerOnly).to.contain('agent owner');

    (el as any).currentUserId = 'owner-1';
    await el.updateComplete;
    expect(control().editable).to.equal(true);
    control().dispatchEvent(
      new CustomEvent('consent-change', { detail: { value: 'deny' } })
    );
    await waitUntil(() => patched !== null);
    expect(patched).to.deep.equal({ others_may_start_sessions: 'deny' });
  });

  it('names every device the agent runs on, linked to its device page', async () => {
    const devices = [
      {
        device_key: 'fp:' + 'a'.repeat(64),
        hostname: 'example-workstation',
        os: 'darwin',
        runner_state: 'online',
        last_seen_at: '2026-10-17T12:00:00Z',
      },
      {
        device_key: 'runner:00000000-0000-4000-8000-000000000001',
        hostname: null,
        os: null,
        runner_state: 'offline',
        last_seen_at: null,
      },
    ];
    fetchStub.callsFake(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const response = await defaultFetch(input, init);
        const url = input.toString();
        if (
          !url.startsWith('/api/v1/agents/agent-1?') &&
          url !== '/api/v1/agents/agent-1'
        )
          return response;
        const body = await response.json();
        return new Response(JSON.stringify({ ...body, devices }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
    );
    const el = await fixture<AgentDetailView>(
      html`<agent-detail-view agentId="agent-1"></agent-detail-view>`
    );
    await waitUntil(() => !!(el as any).agent && (el as any).devices.length);
    await el.updateComplete;
    const strip = el.shadowRoot!.querySelector(
      '[data-testid="agent-devices"]'
    )!;
    const text = strip.textContent!.replace(/\s+/g, ' ');
    expect(text).to.include('Devices');
    expect(text).to.include('macOS');
    expect(text).to.include('Runner online');
    expect(text).to.include('Runner offline');
    expect(text).to.include('Runner 00000000');
    const links = [...strip.querySelectorAll('a')].map((a) =>
      a.getAttribute('href')
    );
    expect(links).to.eql([
      '/console/settings/device-fleet?device=' +
        encodeURIComponent(devices[0].device_key),
      '/console/settings/device-fleet?device=' +
        encodeURIComponent(devices[1].device_key),
    ]);
  });

  it('names the owner in the summary strip, linked to their page', async () => {
    const el = await fixture<AgentDetailView>(
      html`<agent-detail-view agentId="agent-1"></agent-detail-view>`
    );
    await waitUntil(() => !(el as any).loading && !!(el as any).agent);
    expect(el.shadowRoot!.querySelector('[data-testid="agent-owner"]')).to.not
      .exist;
    (el as any).agent = {
      ...(el as any).agent,
      owner_user_id: 'user-7',
      owner_username: 'jane',
      owner_email: 'jane@example.com',
    };
    await el.updateComplete;
    const owner = el.shadowRoot!.querySelector('[data-testid="agent-owner"]')!;
    expect(owner.textContent!.replace(/\s+/g, ' ')).to.include('Owner');
    const link = owner.querySelector('a')!;
    expect(link.getAttribute('href')).to.equal(
      '/console/settings/users/user-7'
    );
    expect(link.textContent!.trim()).to.equal('jane');
  });

  it('shows no device when none is known', async () => {
    const el = await fixture<AgentDetailView>(
      html`<agent-detail-view agentId="agent-1"></agent-detail-view>`
    );
    await waitUntil(() => !(el as any).loading && !!(el as any).agent);
    await el.updateComplete;
    expect(el.shadowRoot!.querySelector('[data-testid="agent-devices"]')).to.not
      .exist;
  });

  it('keeps agent identity when the governance request fails', async () => {
    fetchStub.callsFake(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        if (input.toString().includes('/governance'))
          return new Response('{}', { status: 500 });
        return defaultFetch(input, init);
      }
    );
    const el = await fixture<AgentDetailView>(
      html`<agent-detail-view agentId="agent-1"></agent-detail-view>`
    );
    await waitUntil(() => !(el as any).loading);
    (el as any).activeTab = 'tools';
    await waitUntil(() => !!(el as any).editorContextError);
    await el.updateComplete;
    expect(
      el.shadowRoot!.querySelector('view-header')?.getAttribute('headerText')
    ).to.equal('Claude Code Workspace');
    expect((el as any).error).to.equal(null);
  });

  it('loads associated flows only when that tab is opened', async () => {
    const el = await fixture<AgentDetailView>(
      html`<agent-detail-view agentId="agent-1"></agent-detail-view>`
    );
    await waitUntil(() => !(el as any).loading);
    const flowCalls = () =>
      fetchStub
        .getCalls()
        .filter((call) =>
          /^\/api\/v1\/flows(?:\?|$)/.test(call.args[0].toString())
        );
    expect(flowCalls()).to.have.length(0);
    el.shadowRoot!.querySelector('sl-tab-group')!.dispatchEvent(
      new CustomEvent('sl-tab-show', {
        detail: { name: 'associated-flows' },
        bubbles: true,
      })
    );
    await waitUntil(() => (el as any).associatedFlowsLoaded);
    expect(flowCalls()).to.have.length(1);
    el.shadowRoot!.querySelector('sl-tab-group')!.dispatchEvent(
      new CustomEvent('sl-tab-show', {
        detail: { name: 'associated-flows' },
        bubbles: true,
      })
    );
    expect(flowCalls()).to.have.length(1);
  });

  it('paints its cards from the theme surface, never a fixed white', async () => {
    // Inline white backgrounds under theme-coloured text read at 1.5:1 in
    // the dark theme: two whole cards became bright slabs with pale text.
    const el = await fixture<AgentDetailView>(
      html`<agent-detail-view agentId="agent-1"></agent-detail-view>`
    );
    await waitUntil(() => !(el as any).loading);
    el.shadowRoot!.querySelector('sl-tab-group')!.dispatchEvent(
      new CustomEvent('sl-tab-show', {
        detail: { name: 'associated-flows' },
        bubbles: true,
      })
    );
    await waitUntil(() => (el as any).associatedFlowsLoaded);
    await el.updateComplete;
    const whiteCards = Array.from(
      el.shadowRoot!.querySelectorAll<HTMLElement>('[style]')
    ).filter((node) =>
      /background:\s*#fff(?:fff)?\b/i.test(node.getAttribute('style') ?? '')
    );
    expect(whiteCards).to.have.length(0);
  });

  it('allows the new agent associated-flows request while an old failure is pending', async () => {
    let releaseOld!: () => void;
    let releaseNew!: () => void;
    const oldRead = new Promise<void>((resolve) => {
      releaseOld = resolve;
    });
    const newRead = new Promise<void>((resolve) => {
      releaseNew = resolve;
    });
    let flowCalls = 0;
    fetchStub.callsFake(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = input.toString();
        if (url === '/api/v1/flows') {
          const request = ++flowCalls;
          if (request === 1) {
            await oldRead;
            return new Response('{}', { status: 500 });
          }
          await newRead;
          return new Response(
            JSON.stringify([
              {
                id: 'flow-B',
                name: 'B flow',
                agent_config: {
                  execution_path: 'persistent',
                  target_agent_id: 'agent-2',
                },
              },
            ]),
            { headers: { 'Content-Type': 'application/json' } }
          );
        }
        if (/\/agents\/agent-2(?:\?|$)/.test(url)) {
          const response = await defaultFetch(
            url.replace('agent-2', 'agent-1'),
            init
          );
          const body = await response.json();
          body.agent.id = 'agent-2';
          return new Response(JSON.stringify(body), {
            headers: { 'Content-Type': 'application/json' },
          });
        }
        return defaultFetch(input, init);
      }
    );
    const el = await fixture<AgentDetailView>(
      html`<agent-detail-view agentId="agent-1"></agent-detail-view>`
    );
    await (el as any).loadInFlight;
    (el as any).activeTab = 'associated-flows';
    const stale = (el as any).loadAssociatedFlows();
    await waitUntil(() => flowCalls === 1);
    try {
      invalidateApiCaches();
      (el as any).onBeforeEnter({ params: { agentId: 'agent-2' } });
      await waitUntil(() => flowCalls === 2);
      releaseOld();
      await stale;
      expect((el as any).associatedFlowsLoading).to.equal(true);
      expect((el as any).associatedFlowsError).to.equal(null);
      releaseNew();
      await waitUntil(() => (el as any).associatedFlowsLoaded);
      expect(
        (el as any).associatedFlows.map((flow: any) => flow.id)
      ).to.deep.equal(['flow-B']);
      expect((el as any).associatedFlowsLoading).to.equal(false);
    } finally {
      releaseOld();
      releaseNew();
    }
  });

  it('does not reload static editor catalogs during a realtime refresh', async () => {
    const el = await fixture<AgentDetailView>(
      html`<agent-detail-view agentId="agent-1"></agent-detail-view>`
    );
    await waitUntil(() => !(el as any).loading);
    (el as any).activeTab = 'tools';
    await waitUntil(() => (el as any).editorContextReady);
    await (el as any).loadInFlight;
    fetchStub.resetHistory();
    await (el as any).loadData(true);
    expect(
      fetchStub
        .getCalls()
        .filter((call) =>
          [
            '/api/v1/tools',
            '/api/v1/mcp-servers',
            '/api/v1/approval-workflows',
            '/api/v1/users',
          ].includes(call.args[0].toString())
        )
    ).to.have.length(0);
  });

  it('coalesces live refreshes while agent data is still loading', async () => {
    const element = document.createElement(
      'agent-detail-view'
    ) as AgentDetailView;
    let release!: () => void;
    const read = sinon.stub(element as any, 'performLoadData').callsFake(
      () =>
        new Promise<void>((resolve) => {
          release = resolve;
        })
    );
    const initial = element['loadData']();
    const live = element['loadData'](true);
    const anotherLive = element['loadData'](true);
    expect(read.calledOnce).to.equal(true);
    release();
    await Promise.all([initial, live, anotherLive]);
    const next = element['loadData'](true);
    expect(read.calledTwice).to.equal(true);
    release();
    await next;
    read.restore();
  });

  it('delivers one trailing live refresh after a slow read completes', async () => {
    const element = document.createElement(
      'agent-detail-view'
    ) as AgentDetailView;
    const connected = sinon.stub(element, 'isConnected').get(() => true);
    let release!: () => void;
    const read = sinon.stub(element as any, 'performLoadData').callsFake(
      () =>
        new Promise<void>((resolve) => {
          release = resolve;
        })
    );
    const initial = element['loadData']();
    const refreshes = [element['loadData'](true), element['loadData'](true)];
    expect(read.calledOnce).to.equal(true);
    release();
    await Promise.all([initial, ...refreshes]);
    expect(read.calledTwice).to.equal(true);
    release();
    await element['loadInFlight'];
    expect(read.calledTwice).to.equal(true);
    connected.restore();
    read.restore();
  });

  it('prioritizes an explicit reload over queued live updates', async () => {
    const element = document.createElement(
      'agent-detail-view'
    ) as AgentDetailView;
    const connected = sinon.stub(element, 'isConnected').get(() => true);
    let release!: () => void;
    const read = sinon.stub(element as any, 'performLoadData').callsFake(
      () =>
        new Promise<void>((resolve) => {
          release = resolve;
        })
    );
    const initial = element['loadData']();
    const live = element['loadData'](true);
    const explicit = element['loadData']();
    release();
    await Promise.all([initial, live]);
    expect(read.calledTwice).to.equal(true);
    expect(read.secondCall.args).to.deep.equal([false]);
    release();
    await explicit;
    expect(read.calledTwice).to.equal(true);
    connected.restore();
    read.restore();
  });

  it('renders without fetching unused runtime session detail', async () => {
    fetchStub.callsFake(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();
        if (/\/api\/v1\/runtime-sessions\/[^/?]+$/.test(url)) {
          return new Response(JSON.stringify({ detail: 'Unavailable' }), {
            status: 503,
            headers: { 'Content-Type': 'application/json' },
          });
        }
        return defaultFetch(input, init);
      }
    );
    const element = await fixture<AgentDetailView>(
      html`<agent-detail-view agentId="agent-1"></agent-detail-view>`
    );
    await waitUntil(() => !(element as any).loading);
    expect(element.shadowRoot?.querySelector('view-header')).to.exist;
    expect((element as any).error).to.equal(null);
    const detailRequests = fetchStub
      .getCalls()
      .filter((call) =>
        /\/api\/v1\/runtime-sessions\/[^/?]+$/.test(String(call.args[0]))
      );
    expect(detailRequests).to.have.length(0);
  });

  it('renders live validation and scoped governance', async () => {
    const element = await fixture<AgentDetailView>(
      html`<agent-detail-view agentId="agent-1"></agent-detail-view>`
    );

    await waitUntil(() => {
      const obs = element.shadowRoot?.querySelector(
        'preloop-session-observer'
      ) as any;
      return (
        !(element as any).loading &&
        (element as any).agent !== null &&
        obs &&
        !obs.loading &&
        obs.activeSessionId === 'runtime-session-2' &&
        !obs.loadingSessionId
      );
    }, 'Agent detail view did not finish loading');
    await element.updateComplete;

    const viewHeader = element.shadowRoot?.querySelector('view-header');
    expect(viewHeader?.getAttribute('headertext')).to.equal(
      'Claude Code Workspace'
    );

    const content = getDeepText(element).replace(/\s+/g, ' ');
    expect(content).to.contain('openai/gpt-5');

    // A fully onboarded agent whose live check passed has nothing to warn
    // about, so the header carries the status chip and nothing else.
    const chips = Array.from(
      element.shadowRoot?.querySelectorAll('.badge-row sl-badge') || []
    ).map((chip) => chip.textContent?.replace(/\s+/g, ' ').trim());
    expect(chips).to.deep.equal(['Your turn']);
    expect(content).to.not.contain('Live check: off');
    expect(content).to.not.contain('Live validated');
    // Enrolment method is plumbing, not a feature: no blue chip for it.
    expect(content).to.not.contain('runtime_session_token');

    (element as any).activeTab = 'models';
    await waitUntil(() => (element as any).editorContextReady);
    await element.updateComplete;

    const modelLink = element.shadowRoot?.querySelector(
      'a.session-link[href="/console/ai-models/configured-model-1"]'
    );
    expect(modelLink).to.exist;
    const wrongModelLink = element.shadowRoot?.querySelector(
      'a.session-link[href="/console/ai-models/model-1"]'
    );
    expect(wrongModelLink).to.not.exist;
  });

  it('labels a throttled live check as unverified instead of pending', async () => {
    const element = await fixture<AgentDetailView>(
      html`<agent-detail-view agentId="agent-1"></agent-detail-view>`
    );

    await waitUntil(
      () => !(element as any).loading && (element as any).agent !== null,
      'Agent detail view did not finish loading'
    );

    // The CLI persisted an upstream-refused probe: the gateway plumbing is
    // proven but model traffic is unverified. The header chip says "Live
    // check: off" and the detail is on its tooltip; the old fallthrough
    // rendered an eternal "Live check pending".
    (element as any).agent = {
      ...(element as any).agent,
      live_validation_passed: false,
      live_validation_status: 'throttled',
    };
    await element.updateComplete;

    const chip = element.shadowRoot?.querySelector(
      '.badge-row sl-badge.capability-chip'
    );
    expect(chip?.textContent?.trim()).to.equal('Live check: off');
    expect(chip?.getAttribute('variant')).to.equal('warning');
    expect(chip?.closest('sl-tooltip')?.getAttribute('content')).to.equal(
      'Live check throttled, unverified'
    );
    expect(getDeepText(element)).to.not.contain('Live check pending');
    expect((element as any).getLiveValidationVariant()).to.equal('warning');

    (element as any).agent = {
      ...(element as any).agent,
      live_validation_status: 'upstream_unavailable',
    };
    await element.updateComplete;
    expect(
      element.shadowRoot
        ?.querySelector('.badge-row sl-badge.capability-chip')
        ?.closest('sl-tooltip')
        ?.getAttribute('content')
    ).to.equal('Upstream refused, unverified');
  });

  it('labels a timed out live check as unverified with a warning', async () => {
    const element = await fixture<AgentDetailView>(
      html`<agent-detail-view agentId="agent-1"></agent-detail-view>`
    );
    await waitUntil(
      () => !(element as any).loading && (element as any).agent !== null,
      'Agent detail view did not finish loading'
    );
    (element as any).agent = {
      ...(element as any).agent,
      live_validation_passed: false,
      live_validation_status: 'timeout',
    };
    await element.updateComplete;
    const chip = element.shadowRoot?.querySelector(
      '.badge-row sl-badge.capability-chip'
    );
    expect(chip?.getAttribute('variant')).to.equal('warning');
    expect(chip?.closest('sl-tooltip')?.getAttribute('content')).to.equal(
      'Live check timed out, unverified'
    );
  });

  describe('progressive load', () => {
    function agentCalls(): URL[] {
      return fetchStub
        .getCalls()
        .map((call) => String(call.args[0]))
        .filter(
          (url) =>
            url.startsWith('/api/v1/agents/agent-1') &&
            !url.includes('/governance') &&
            !url.includes('/model-bindings')
        )
        .map((url) => new URL(url, window.location.origin));
    }

    async function mount(): Promise<AgentDetailView> {
      const element = await fixture<AgentDetailView>(
        html`<agent-detail-view agentId="agent-1"></agent-detail-view>`
      );
      await waitUntil(
        () => !(element as any).loading && (element as any).agent !== null,
        'header did not load'
      );
      return element;
    }

    it("reads the header alone, then only the open tab's section", async () => {
      const element = await mount();
      await waitUntil(() => (element as any).loadedSections.has('sessions'));
      const calls = agentCalls();
      expect(
        calls.map((url) => url.searchParams.get('include')).sort()
      ).to.deep.equal(['', 'aggregate', 'sessions']);
      expect(
        calls.every((url) => url.searchParams.get('lightweight') === 'true')
      ).to.equal(true);
      expect(
        calls
          .find((url) => url.searchParams.get('include') === 'sessions')!
          .searchParams.has('start_date')
      ).to.equal(false);

      (element as any).activeTab = 'models';
      await (element as any).loadActiveTabData();
      const models = agentCalls().at(-1)!;
      expect(models.searchParams.get('include')).to.equal('usage_by_model');
      expect(models.searchParams.has('start_date')).to.equal(true);
      // Opening it again does not read it again.
      await (element as any).loadActiveTabData();
      expect(agentCalls()).to.have.length(4);
    });

    it('keeps identity and the selected transcript mounted during a slow range read', async () => {
      const element = await mount();
      await waitUntil(
        () =>
          (element as any).loadedSections.has('sessions') &&
          !(element as any).spendLoading
      );
      await element.updateComplete;
      const heading = element.shadowRoot!.querySelector('view-header');
      expect(heading).to.exist;
      const observer = element.shadowRoot!.querySelector(
        'preloop-session-observer'
      );
      expect(observer).to.exist;
      const previous = (element as any).aggregate;
      let release!: () => void;
      const gate = new Promise<void>((resolve) => {
        release = resolve;
      });
      fetchStub.callsFake(
        async (input: RequestInfo | URL, init?: RequestInit) => {
          if (String(input).includes('include=aggregate')) await gate;
          return defaultFetch(input, init);
        }
      );
      const before = agentCalls().length;
      (element as any).budgetTimeRange = 'week';
      (element as any).refreshSpendRange();
      await element.updateComplete;
      expect(element.shadowRoot!.querySelector('view-header')).to.equal(
        heading
      );
      expect(
        element.shadowRoot!.querySelector('preloop-session-observer')
      ).to.equal(observer);
      expect((element as any).aggregate).to.equal(previous);
      expect((element as any).loading).to.equal(false);
      expect(
        element
          .shadowRoot!.querySelector('.strip-spend')!
          .getAttribute('aria-busy')
      ).to.equal('true');
      expect(element.shadowRoot!.textContent).to.contain('Updating spend');
      expect(
        agentCalls()
          .slice(before)
          .map((url) => url.searchParams.get('include'))
      ).to.deep.equal(['aggregate']);
      release();
      await waitUntil(() => !(element as any).spendLoading);
    });

    it('ignores unrelated realtime bursts and refreshes only matching sessions', async () => {
      const element = await mount();
      await waitUntil(() => (element as any).loadedSections.has('sessions'));
      const clock = sinon.useFakeTimers({
        now: Date.now(),
        toFake: ['setTimeout', 'clearTimeout', 'Date'],
      });
      const full = sinon.stub(element as any, 'loadData').resolves();
      const sessions = sinon.stub(element as any, 'loadSessions').resolves();
      const spend = sinon.stub(element as any, 'loadSpend').resolves();
      try {
        for (let i = 0; i < 100; i++)
          (element as any).handleResourceEvent(
            {
              payload: {
                managed_agent_id: 'another-agent',
                runtime_session_id: 'another-session',
              },
            },
            true
          );
        await clock.tickAsync(1000);
        expect(full.callCount + sessions.callCount + spend.callCount).to.equal(
          0
        );
        for (let i = 0; i < 100; i++)
          (element as any).handleResourceEvent(
            { payload: { managed_agent_id: 'agent-1' } },
            true
          );
        await clock.tickAsync(300);
        expect(sessions.callCount).to.equal(1);
        expect(full.callCount + spend.callCount).to.equal(0);
      } finally {
        clock.restore();
      }
    });

    it('matches agent summaries and merges without a bound session, and top-level session ids', async () => {
      const element = await mount();
      await waitUntil(() => (element as any).loadedSections.has('sessions'));
      (element as any).agent = {
        ...(element as any).agent,
        runtime_session_id: null,
      };
      const refresh = sinon.stub(element as any, 'scheduleRefresh');
      for (const message of [
        {
          topic: 'managed_agents',
          type: 'managed_agent_updated',
          payload: { id: 'agent-1', runtime_session_id: null },
        },
        {
          topic: 'managed_agents',
          type: 'managed_agent_merged',
          payload: { survivor_id: 'other', duplicate_id: 'agent-1' },
        },
        {
          topic: 'managed_agents',
          type: 'managed_agent_merged',
          payload: { survivor_id: 'agent-1', duplicate_id: 'other' },
        },
        { runtime_session_id: (element as any).sessions[0].id, payload: {} },
      ])
        (element as any).handleResourceEvent(message, false);
      expect(refresh.callCount).to.equal(4);
      (element as any).handleResourceEvent(
        { type: 'managed_agent_updated', payload: { id: 'other' } },
        false
      );
      (element as any).handleResourceEvent(
        { type: 'unrelated', payload: { id: 'agent-1' } },
        false
      );
      expect(refresh.callCount).to.equal(4);
    });

    it('shows unavailable figures instead of zero while initial spend is pending or fails', async () => {
      let release!: () => void;
      const gate = new Promise<void>((resolve) => {
        release = resolve;
      });
      fetchStub.callsFake(
        async (input: RequestInfo | URL, init?: RequestInit) => {
          if (String(input).includes('include=aggregate')) {
            await gate;
            return new Response('{}', { status: 500 });
          }
          return defaultFetch(input, init);
        }
      );
      const element = await mount();
      const assertUnavailable = () => {
        expect(
          element.shadowRoot!.querySelector('.strip-value')?.textContent?.trim()
        ).to.equal('—');
        expect(
          element.shadowRoot!.querySelector('.strip-requests')?.textContent
        ).to.not.contain('0 requests');
        expect(element.shadowRoot!.querySelector('.strip-tokens token-figures'))
          .to.not.exist;
      };
      try {
        await element.updateComplete;
        expect(element.shadowRoot!.textContent).to.contain('Loading spend');
        assertUnavailable();
      } finally {
        release();
      }
      await waitUntil(() => !(element as any).spendLoading);
      await element.updateComplete;
      expect(element.shadowRoot!.textContent).to.contain('Spend unavailable');
      assertUnavailable();
    });

    it('defers hidden-view invalidations and reconciles once when visible', async () => {
      const element = await mount();
      await waitUntil(() => (element as any).loadedSections.has('sessions'));
      let hidden = true;
      Object.defineProperty(document, 'hidden', {
        configurable: true,
        get: () => hidden,
      });
      const clock = sinon.useFakeTimers({
        now: Date.now(),
        toFake: ['setTimeout', 'clearTimeout', 'Date'],
      });
      const load = sinon.stub(element as any, 'loadData').resolves();
      try {
        for (let i = 0; i < 100; i++)
          (element as any).handleResourceEvent(
            { payload: { managed_agent_id: 'agent-1' } },
            false
          );
        await clock.tickAsync(30000);
        expect(load.callCount).to.equal(0);
        hidden = false;
        document.dispatchEvent(new Event('visibilitychange'));
        await clock.tickAsync(300);
        expect(load.callCount).to.equal(1);
      } finally {
        delete (document as unknown as { hidden?: boolean }).hidden;
        clock.restore();
      }
    });

    it('shows skeleton rows until the first session page arrives', async () => {
      let release!: () => void;
      const gate = new Promise<void>((resolve) => (release = resolve));
      fetchStub.callsFake(
        async (input: RequestInfo | URL, init?: RequestInit) => {
          const url = String(input);
          if (url.includes('include=sessions')) await gate;
          return defaultFetch(input, init);
        }
      );
      const element = await mount();
      await waitUntil(() =>
        element.shadowRoot!.querySelector('[data-testid="sessions-skeleton"]')
      );
      expect(element.shadowRoot!.querySelector('preloop-session-observer')).to
        .be.null;
      release();
      await waitUntil(() =>
        element.shadowRoot!.querySelector('preloop-session-observer')
      );
      const observer = element.shadowRoot!.querySelector(
        'preloop-session-observer'
      )!;
      expect(observer.getAttribute('layout')).to.equal('full');
    });

    it('pages sessions with Load more and keeps the rows on refresh', async () => {
      fetchStub.callsFake(
        async (input: RequestInfo | URL, init?: RequestInit) => {
          const url = new URL(String(input), window.location.origin);
          if (url.searchParams.get('include') === 'sessions') {
            const offset = Number(url.searchParams.get('session_offset') || 0);
            const limit = Number(url.searchParams.get('session_limit') || 20);
            const sessions = Array.from(
              { length: Math.min(limit, 45 - offset) },
              (_, i) => ({
                id: `s-${offset + i}`,
                session_source_type: 'claude_code',
                session_source_id: `src-${offset + i}`,
                started_at: '2026-10-01T00:00:00Z',
              })
            );
            return new Response(
              JSON.stringify({
                agent: { id: 'agent-1' },
                sessions,
                session_total: 45,
              }),
              { status: 200, headers: { 'Content-Type': 'application/json' } }
            );
          }
          return defaultFetch(input, init);
        }
      );
      const element = await mount();
      await waitUntil(() => (element as any).sessions.length === 20);
      await element.updateComplete;
      const more = element.shadowRoot!.querySelector(
        '[data-testid="load-more-sessions"]'
      ) as HTMLElement;
      expect(more.textContent?.replace(/\s+/g, ' ')).to.contain('20 of 45');
      more.click();
      await waitUntil(() => (element as any).sessions.length === 40);
      expect(agentCalls().at(-1)!.searchParams.get('session_offset')).to.equal(
        '20'
      );

      await (element as any).loadSessions({ refresh: true });
      const refresh = agentCalls().at(-1)!;
      expect(refresh.searchParams.get('session_limit')).to.equal('40');
      expect(refresh.searchParams.has('session_offset')).to.equal(false);
      expect((element as any).sessions).to.have.length(40);
    });

    function sessionsServer(total: number | null, fail = () => false) {
      fetchStub.callsFake(
        async (input: RequestInfo | URL, init?: RequestInit) => {
          const url = new URL(String(input), window.location.origin);
          const include = url.searchParams.get('include');
          if (include === 'sessions' || include === 'usage_by_model') {
            if (fail()) return new Response('{}', { status: 500 });
          }
          if (include === 'sessions' && total !== null) {
            const offset = Number(url.searchParams.get('session_offset') || 0);
            const limit = Number(url.searchParams.get('session_limit') || 20);
            const sessions = Array.from(
              { length: Math.max(0, Math.min(limit, total - offset)) },
              (_, i) => ({
                id: `s-${offset + i}`,
                session_source_type: 'claude_code',
                session_source_id: `src-${offset + i}`,
                started_at: '2026-10-01T00:00:00Z',
              })
            );
            return new Response(
              JSON.stringify({
                agent: { id: 'agent-1' },
                sessions,
                session_total: total,
              }),
              { status: 200, headers: { 'Content-Type': 'application/json' } }
            );
          }
          return defaultFetch(input, init);
        }
      );
    }

    it('shows the list without Load more on a server that does not page', async () => {
      sessionsServer(null);
      const element = await mount();
      await waitUntil(() => (element as any).loadedSections.has('sessions'));
      await element.updateComplete;
      expect((element as any).sessions.length).to.be.greaterThan(0);
      expect((element as any).sessionTotal).to.equal(null);
      expect(
        Boolean(
          element.shadowRoot!.querySelector(
            '[data-testid="load-more-sessions"]'
          )
        )
      ).to.equal(false);
    });

    it('keeps rows past the first 100 when a refresh lands', async () => {
      sessionsServer(150);
      const element = await mount();
      await waitUntil(() => (element as any).sessions.length === 20);
      for (let i = 0; i < 5; i++)
        await (element as any).loadSessions({ more: true });
      expect((element as any).sessions).to.have.length(120);
      await (element as any).loadSessions({ refresh: true });
      const ids = ((element as any).sessions as Array<{ id: string }>).map(
        (r) => r.id
      );
      expect(ids).to.have.length(120);
      expect(new Set(ids).size).to.equal(120);
      expect(ids[119]).to.equal('s-119');
    });

    it('keeps rows that new sessions push past the re-read', async () => {
      sessionsServer(150);
      const element = await mount();
      await waitUntil(() => (element as any).sessions.length === 20);
      for (let i = 0; i < 5; i++)
        await (element as any).loadSessions({ more: true });
      // Two new sessions arrive at the top: s-98 and s-99 fall past row 100.
      const shifted = (element as any).sessions.slice();
      fetchStub.callsFake(
        async () =>
          new Response(
            JSON.stringify({
              agent: { id: 'agent-1' },
              sessions: [
                {
                  id: 'new-1',
                  session_source_type: 'claude_code',
                  session_source_id: 'n1',
                },
                {
                  id: 'new-2',
                  session_source_type: 'claude_code',
                  session_source_id: 'n2',
                },
                ...shifted.slice(0, 98),
              ],
              session_total: 152,
            }),
            { status: 200, headers: { 'Content-Type': 'application/json' } }
          )
      );
      await (element as any).loadSessions({ refresh: true });
      const ids = ((element as any).sessions as Array<{ id: string }>).map(
        (r) => r.id
      );
      expect(ids).to.have.length(122);
      expect(ids).to.include.members(['s-98', 's-99', 's-119']);
      expect(new Set(ids).size).to.equal(122);
    });

    it('never lets a refresh overwrite a page loaded after it', async () => {
      sessionsServer(60);
      const element = await mount();
      await waitUntil(() => (element as any).sessions.length === 20);
      const refresh = (element as any).loadSessions({ refresh: true });
      const more = (element as any).loadSessions({ more: true });
      await Promise.all([refresh, more]);
      expect((element as any).sessions).to.have.length(40);
    });

    it('keeps the list and says so when a session read fails', async () => {
      let failing = false;
      sessionsServer(45, () => failing);
      const element = await mount();
      await waitUntil(() => (element as any).sessions.length === 20);
      failing = true;
      await (element as any).loadSessions({ refresh: true });
      expect((element as any).sessions).to.have.length(20);
      expect((element as any).sessionsError).to.be.a('string');
    });

    it('reports a failed usage read instead of zero spend', async () => {
      sessionsServer(5, () => true);
      const element = await mount();
      await (element as any).loadUsageByModel(true);
      expect((element as any).usageError).to.be.a('string');
      expect((element as any).loadedSections.has('usage_by_model')).to.equal(
        false
      );
    });

    it('hides spend until the usage read resolves', async () => {
      let release!: () => void;
      const gate = new Promise<void>((resolve) => {
        release = resolve;
      });
      fetchStub.callsFake(
        async (input: RequestInfo | URL, init?: RequestInit) => {
          const url = String(input);
          if (url.includes('include=usage_by_model')) await gate;
          return defaultFetch(input, init);
        }
      );
      const element = await mount();
      (element as any).activeTab = 'models';
      await element.updateComplete;
      const reading = (element as any).loadActiveTabData();
      await waitUntil(() => (element as any).editorContextReady);
      await element.updateComplete;
      const modelsCard = [
        ...element.shadowRoot!.querySelectorAll('sl-card'),
      ].find((card) => getDeepText(card).includes('Models & spend'));
      expect(modelsCard, 'models card while usage is in flight').to.exist;
      const pending = getDeepText(modelsCard);
      expect(pending).to.contain('openai/gpt-5');
      expect(pending).to.not.contain('spent');
      expect(pending).to.not.contain('$0.00');
      release();
      await reading;
      await element.updateComplete;
      expect(getDeepText(modelsCard)).to.contain('spent');
    });

    it('clears a usage error when the next read starts', async () => {
      let fail = true;
      let release!: () => void;
      const gate = new Promise<void>((resolve) => {
        release = resolve;
      });
      fetchStub.callsFake(
        async (input: RequestInfo | URL, init?: RequestInit) => {
          const url = String(input);
          if (url.includes('include=usage_by_model')) {
            if (fail) return new Response('{}', { status: 500 });
            await gate;
          }
          return defaultFetch(input, init);
        }
      );
      const element = await mount();
      await (element as any).loadUsageByModel(true);
      expect((element as any).usageError).to.be.a('string');
      fail = false;
      const retry = (element as any).loadUsageByModel(true);
      expect((element as any).usageError).to.equal(null);
      release();
      await retry;
      expect((element as any).usageError).to.equal(null);
      expect((element as any).loadedSections.has('usage_by_model')).to.equal(
        true
      );
    });

    it('spaces live refreshes out on a busy agent', async () => {
      const element = await mount();
      const clock = sinon.useFakeTimers({
        now: Date.now(),
        toFake: ['setTimeout', 'clearTimeout', 'Date'],
      });
      const load = sinon.stub(element as any, 'loadData').resolves();
      try {
        for (let i = 0; i < 10; i++) (element as any).scheduleRefresh();
        await clock.tickAsync(300);
        expect(load.callCount, 'one refresh for a burst').to.equal(1);
        expect(load.firstCall.args[0]).to.equal(true);
        (element as any).scheduleRefresh();
        await clock.tickAsync(5_000);
        expect(load.callCount, 'not again within the interval').to.equal(1);
        await clock.tickAsync(10_100);
        expect(load.callCount, 'the trailing refresh lands').to.equal(2);
      } finally {
        load.restore();
        clock.restore();
      }
    });

    it('keeps Decommission and Remove in the More menu', async () => {
      const element = await mount();
      const actions = (element as any).agentActions as Array<{
        id: string;
        menu?: boolean;
      }>;
      const pinned = actions.filter((action) => action.menu).map((a) => a.id);
      expect(pinned).to.deep.equal(['decommission', 'remove']);
    });
  });

  describe('notes', () => {
    function withNotes(
      total: number,
      agentPatch: Record<string, unknown> = {}
    ): void {
      const posted: Array<Record<string, unknown>> = [];
      fetchStub.callsFake(
        async (input: RequestInfo | URL, init?: RequestInit) => {
          const url = typeof input === 'string' ? input : input.toString();
          if (url.startsWith('/api/v1/operator-notes')) {
            const method = (init?.method || 'GET').toUpperCase();
            if (method === 'POST') {
              const body = init?.body
                ? (JSON.parse(String(init.body)) as Record<string, unknown>)
                : {};
              const note = {
                note_id: String(body.note_id || `posted-${posted.length}`),
                state: 'pending',
                text: String(body.text || ''),
                author: body.author || {
                  display: 'dimitris',
                  auth_method: 'jwt',
                },
              };
              posted.unshift(note);
              return new Response(JSON.stringify(note), {
                status: 201,
                headers: { 'Content-Type': 'application/json' },
              });
            }
            const generated = Array.from(
              { length: Math.min(3, total) },
              (_, i) => ({
                note_id: `note-${i}`,
                state: 'delivered',
                text: `Conductor note ${i} ${'x'.repeat(600)}`,
                author: { display: 'dimitris', auth_method: 'jwt' },
              })
            );
            const notes = [...posted, ...generated].slice(0, 3);
            return new Response(
              JSON.stringify({ notes, total: total + posted.length }),
              {
                status: 200,
                headers: { 'Content-Type': 'application/json' },
              }
            );
          }
          const response = await defaultFetch(input, init);
          if (
            url.startsWith('/api/v1/agents/agent-1') &&
            !url.includes('/governance') &&
            response.ok
          ) {
            const body = await response.json();
            if (body.agent) Object.assign(body.agent, agentPatch);
            return new Response(JSON.stringify(body), {
              status: 200,
              headers: { 'Content-Type': 'application/json' },
            });
          }
          return response;
        }
      );
    }

    async function mount(): Promise<AgentDetailView> {
      const element = await fixture<AgentDetailView>(
        html`<agent-detail-view agentId="agent-1"></agent-detail-view>`
      );
      await waitUntil(
        () => !(element as any).loading && (element as any).agent !== null,
        'Agent detail view did not finish loading'
      );
      await waitUntil(
        () => (element as any).notesPage !== null,
        'notes count did not load'
      );
      await element.updateComplete;
      return element;
    }

    it('keeps notes out of the page above the tabs, behind a counted tab', async () => {
      withNotes(48);
      const element = await mount();
      const root = element.shadowRoot!;
      // Nothing note-shaped renders until the tab is opened.
      expect(root.querySelector('operator-note-composer')).to.be.null;
      expect(root.querySelector('agent-notes-tab')).to.be.null;
      const tab = root.querySelector('[data-testid="notes-tab"]');
      expect(tab?.textContent?.replace(/\s+/g, ' ').trim()).to.equal(
        'Notes (48)'
      );
      const ids = ((element as any).agentActions as Array<{ id: string }>).map(
        (action) => action.id
      );
      expect(ids).to.include('note');
    });

    it('opens the Notes tab from the header Note action', async () => {
      withNotes(2);
      const element = await mount();
      const note = ((element as any).agentActions as Array<any>).find(
        (action) => action.id === 'note'
      );
      note.onClick();
      await waitUntil(() =>
        element.shadowRoot!.querySelector('agent-notes-tab')
      );
      expect((element as any).activeTab).to.equal('notes');
      expect(new URL(window.location.href).searchParams.get('tab')).to.equal(
        'notes'
      );
    });

    it('keeps a sent note in the tab and counts it', async () => {
      withNotes(5);
      const element = await mount();
      (element as any).activeTab = 'notes';
      await element.updateComplete;
      const tab = element.shadowRoot!.querySelector('agent-notes-tab')!;
      await (tab as any).updateComplete;
      expect(tab.shadowRoot!.querySelectorAll('li')).to.have.length(3);
      const sent = {
        note_id: 'note-new',
        state: 'pending',
        text: 'Rebase first.',
        author: { display: 'dimitris', auth_method: 'jwt' },
      };
      const composer = tab.shadowRoot!.querySelector('operator-note-composer')!;
      composer.dispatchEvent(
        new CustomEvent('operator-note-sent', {
          detail: sent,
          bubbles: true,
          composed: true,
        })
      );
      await element.updateComplete;
      await (tab as any).updateComplete;
      const rows = Array.from(tab.shadowRoot!.querySelectorAll('li'));
      expect(rows).to.have.length(4);
      expect(rows[0].getAttribute('data-testid')).to.equal(
        'agent-note-note-new'
      );
      const label = element.shadowRoot!.querySelector(
        '[data-testid="notes-tab"]'
      );
      expect(label?.textContent?.replace(/\s+/g, ' ').trim()).to.equal(
        'Notes (6)'
      );
      await fetch('/api/v1/operator-notes', {
        method: 'POST',
        body: JSON.stringify(sent),
      });
      // Leaving the tab and coming back reads a fresh page, not the stale one.
      (element as any).activeTab = 'sessions';
      await element.updateComplete;
      (element as any).activeTab = 'notes';
      await element.updateComplete;
      const remounted = element.shadowRoot!.querySelector('agent-notes-tab')!;
      expect((remounted as any).initial).to.equal(null);
      expect((element as any).notesPage).to.equal(null);
      expect(label?.textContent?.replace(/\s+/g, ' ').trim()).to.equal(
        'Notes (6)'
      );
      await waitUntil(
        () =>
          remounted.shadowRoot!.querySelector(
            '[data-testid="agent-note-note-new"]'
          ) !== null
      );
      const freshRows = Array.from(
        remounted.shadowRoot!.querySelectorAll('li')
      );
      expect(freshRows[0].getAttribute('data-testid')).to.equal(
        'agent-note-note-new'
      );
      // A fresh first page is three rows of six, so three stay behind Older.
      expect(
        remounted.shadowRoot!.querySelector('[data-testid="notes-older"]')
          ?.textContent
      ).to.contain('(3)');
    });

    it('drops a notes page that arrives after a send', async () => {
      withNotes(5);
      const element = await mount();
      let release!: (response: Response) => void;
      const pending = new Promise<Response>((resolve) => {
        release = resolve;
      });
      fetchStub.callsFake(async (input: RequestInfo | URL) => {
        const url = typeof input === 'string' ? input : input.toString();
        if (url.startsWith('/api/v1/operator-notes')) return pending;
        return defaultFetch(input);
      });
      const loading = (element as any).loadNotesPage();
      (element as any).activeTab = 'notes';
      await element.updateComplete;
      const tab = element.shadowRoot!.querySelector('agent-notes-tab')!;
      tab.dispatchEvent(
        new CustomEvent('notes-count-changed', {
          detail: { total: 6 },
          bubbles: true,
          composed: true,
        })
      );
      await element.updateComplete;
      release(
        new Response(
          JSON.stringify({
            notes: [
              {
                note_id: 'note-stale',
                state: 'delivered',
                text: 'stale',
                author: { display: 'dimitris', auth_method: 'jwt' },
              },
            ],
            total: 5,
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        )
      );
      await loading;
      expect((element as any).notesPage).to.equal(null);
      expect((element as any).notesTotal).to.equal(6);
    });

    it('offers no note box or tab to an agent no note can reach', async () => {
      withNotes(0, { supports_operator_notes: false });
      const element = await mount();
      const root = element.shadowRoot!;
      expect(root.querySelector('[data-testid="notes-tab"]')).to.be.null;
      const ids = ((element as any).agentActions as Array<{ id: string }>).map(
        (action) => action.id
      );
      expect(ids).not.to.include('note');
    });

    it('still lists earlier notes for an agent that can no longer receive one', async () => {
      withNotes(4, { supports_operator_notes: false });
      const element = await mount();
      const tab = element.shadowRoot!.querySelector(
        '[data-testid="notes-tab"]'
      );
      expect(tab, 'history stays reachable').to.exist;
    });
  });

  it('orders the action bar and keeps Remove last, outlined and set apart', async () => {
    const element = await fixture<AgentDetailView>(
      html`<agent-detail-view agentId="agent-1"></agent-detail-view>`
    );

    await waitUntil(
      () => !(element as any).loading && (element as any).agent !== null,
      'Agent detail view did not finish loading'
    );

    const actions = (element as any).agentActions as Array<{
      id: string;
      icon?: string;
      variant?: string;
      outline?: boolean;
      separated?: boolean;
    }>;
    const ids = actions.map((action) => action.id);
    expect(ids.indexOf('rename')).to.be.lessThan(ids.indexOf('edit-tags'));
    expect(ids.indexOf('edit-tags')).to.be.lessThan(ids.indexOf('pause'));
    expect(ids[ids.length - 1], 'Remove is last').to.equal('remove');

    // Pause is an everyday control, not a warning: amber competed with Talk.
    const pause = actions.find((action) => action.id === 'pause');
    expect(pause?.variant, 'Pause is neutral').to.equal('default');
    expect((pause as { icon?: string }).icon).to.equal('pause-fill');

    const remove = actions[actions.length - 1];
    expect(remove.variant).to.equal('danger');
    expect(remove.outline, 'Remove is outlined, never solid red').to.equal(
      true
    );
    expect(remove.separated, 'Remove is set apart').to.equal(true);
  });

  // The page kept its own action array and quietly lost Decommission, which
  // the list row has always offered for the same agent.
  it('offers the same lifecycle moves the agents list offers', async () => {
    const element = await fixture<AgentDetailView>(
      html`<agent-detail-view agentId="agent-1"></agent-detail-view>`
    );

    await waitUntil(
      () => !(element as any).loading && (element as any).agent !== null,
      'Agent detail view did not finish loading'
    );

    const ids = ((element as any).agentActions as Array<{ id: string }>).map(
      (action) => action.id
    );
    expect(ids).to.contain('decommission');
    expect(ids).to.contain('pause');
    expect(ids).to.not.contain('resume');
  });

  it('shows a desktop badge only for a loopback vnc or rdp agent', async () => {
    const element = await fixture<AgentDetailView>(
      html`<agent-detail-view agentId="agent-1"></agent-detail-view>`
    );

    await waitUntil(
      () => !(element as any).loading && (element as any).agent !== null,
      'Agent detail view did not finish loading'
    );

    const badge = () =>
      element.shadowRoot?.querySelector('sl-badge.desktop-badge');

    for (const desktop of [undefined, 'none'] as const) {
      (element as any).agent = { ...(element as any).agent, desktop };
      await element.updateComplete;
      expect(badge(), `no badge when desktop is ${String(desktop)}`).to.not
        .exist;
    }

    (element as any).agent = { ...(element as any).agent, desktop: 'vnc' };
    await element.updateComplete;
    expect(badge()?.textContent).to.contain('Desktop: VNC (loopback)');

    (element as any).agent = { ...(element as any).agent, desktop: 'rdp' };
    await element.updateComplete;
    expect(badge()?.textContent).to.contain('Desktop: RDP (loopback)');

    // An online desktop endpoint (#870) makes the live view reachable.
    (element as any).desktopEndpoints = [{ id: 'endpoint-1' }];
    await element.updateComplete;
    expect(badge()?.textContent).to.contain(
      'Desktop: RDP (live view available)'
    );
  });

  // Wave 4: tags are labels, not states, so they lost the pill and took a
  // leading tag icon instead of the hash.
  it('shows operator tags as text with a leading tag icon', async () => {
    const element = await fixture<AgentDetailView>(
      html`<agent-detail-view agentId="agent-1"></agent-detail-view>`
    );

    await waitUntil(
      () => !(element as any).loading && (element as any).agent !== null,
      'Agent detail view did not finish loading'
    );

    (element as any).agent = {
      ...(element as any).agent,
      tags: { team: 'platform', pilot: 'true' },
    };
    await element.updateComplete;

    const tagChips = Array.from(
      element.shadowRoot?.querySelectorAll('.badge-row sl-badge.tag-chip') || []
    );
    expect(tagChips.map((chip) => chip.getAttribute('variant'))).to.deep.equal([
      'neutral',
      'neutral',
    ]);
    expect(
      tagChips.map((chip) => chip.textContent?.replace(/\s+/g, '').trim())
    ).to.deep.equal(['team=platform', 'pilot']);
    expect(
      tagChips.every((chip) => chip.querySelector('sl-icon[name="tag"]')),
      'every tag chip has a leading tag icon'
    ).to.be.true;
  });

  // Wave 3 (A5): the facts about the agent are one hairline row between the
  // header and the tabs, not a 190px card holding one number.
  it('states the agent facts and its spend on one summary strip', async () => {
    const element = await fixture<AgentDetailView>(
      html`<agent-detail-view agentId="agent-1"></agent-detail-view>`
    );

    await waitUntil(
      () => !(element as any).loading && (element as any).agent !== null,
      'Agent detail view did not finish loading'
    );

    const strip = element.shadowRoot?.querySelector('.summary-strip');
    expect(strip, 'the summary strip is rendered').to.exist;
    expect(element.shadowRoot?.querySelector('.agent-overview'), 'no card').to
      .not.exist;

    const stripText = getDeepText(strip).replace(/\s+/g, ' ').trim();
    // Kind, the id the agent reports itself as, the reference it enrolled
    // with, and its status, in that order.
    expect(stripText).to.contain('Claude Code');
    expect(stripText).to.contain('claude-code-agent-1');
    expect(stripText).to.contain('claude-session-2');
    expect(
      strip?.querySelector('.badge-row sl-badge.status-chip')?.textContent
    ).to.contain('Your turn');

    // The spend is stated with the window it covers and what it is based on.
    expect(stripText).to.contain('Estimated spend · 30d');
    expect(stripText).to.contain('$0.57');
    expect(stripText).to.contain('4 requests');
    expect(
      strip?.querySelector('.strip-requests')?.getAttribute('title')
    ).to.equal('4 requests');

    // Volume leads the money on the strip as it does in the lists, so the
    // card this replaced did not take the token split down with it.
    const figures = strip?.querySelector(
      '[data-testid="agent-token-figures"] token-figures'
    ) as (HTMLElement & { updateComplete: Promise<unknown> }) | null;
    expect(figures, 'the strip states the token split').to.exist;
    await figures!.updateComplete;
    const figuresText = (figures!.shadowRoot?.textContent || '').replace(
      /\s+/g,
      ' '
    );
    expect(figuresText).to.contain('300 in');
    expect(figuresText).to.contain('120 out');
    // This aggregate reported no cache fields, so no hit rate is claimed.
    expect(figuresText).to.not.contain('hit');

    // One range control, the shared one, holding the selected window.
    const range = strip?.querySelector('time-range-select');
    expect(range, 'the strip carries the shared range control').to.exist;
    expect((range as unknown as { value: string }).value).to.equal('month');
    expect(strip?.querySelector('select'), 'no bare select').to.not.exist;
  });

  // DESIGN.md: the strip never wraps a UUID. Identifiers are shortened to
  // eight characters, the whole value stays in the title and on the
  // clipboard.
  it('shortens the uuids on the strip and keeps the full value to copy', async () => {
    const uuid = '2f1c8d9a-4b7e-4c21-9f3a-6d0e5b8c1a77';
    fetchStub.callsFake(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();
        if (
          url.startsWith('/api/v1/agents/agent-1') &&
          !url.includes('/governance')
        ) {
          const response = await defaultFetch(input, init);
          const payload = await response.json();
          payload.agent.session_source_id = uuid;
          payload.agent.session_reference = `agent-${uuid}`;
          return new Response(JSON.stringify(payload), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        }
        return defaultFetch(input, init);
      }
    );

    const element = await fixture<AgentDetailView>(
      html`<agent-detail-view agentId="agent-1"></agent-detail-view>`
    );
    await waitUntil(
      () => !(element as any).loading && (element as any).agent !== null,
      'Agent detail view did not finish loading'
    );

    const strip = element.shadowRoot?.querySelector('.summary-strip');
    const ids = Array.from(strip?.querySelectorAll('.strip-id') || []);
    expect(ids.map((id) => id.textContent?.trim())).to.deep.equal([
      '2f1c8d9a\u2026',
      'agent-2f1c8d9a\u2026',
    ]);
    expect(ids.map((id) => id.getAttribute('title'))).to.deep.equal([
      uuid,
      `agent-${uuid}`,
    ]);

    // The full value goes to the clipboard, not to the screen.
    const copyButtons = Array.from(
      strip?.querySelectorAll('sl-copy-button') || []
    );
    expect(
      copyButtons.map((button) => button.getAttribute('value'))
    ).to.deep.equal([uuid, `agent-${uuid}`]);
    expect(strip?.textContent).to.not.contain('4b7e-4c21');
  });

  it('calls the session history panel in sentence case (U-13)', async () => {
    const element = await fixture<AgentDetailView>(
      html`<agent-detail-view agentId="agent-1"></agent-detail-view>`
    );

    await waitUntil(
      () => !(element as any).loading && (element as any).agent !== null,
      'Agent detail view did not finish loading'
    );

    const text = getDeepText(element).replace(/\s+/g, ' ');
    expect(text).to.not.contain('Sessions History');
    expect(text).to.contain('Session history');
    await element.updateComplete;
    const refresh = element.shadowRoot!.querySelector(
      'sl-icon-button[name="arrow-clockwise"]'
    );
    expect(refresh?.getAttribute('label')).to.equal('Refresh sessions');
  });

  it('lets the user pick the approval workflow for native tool approvals', async () => {
    const element = await fixture<AgentDetailView>(
      html`<agent-detail-view agentId="agent-1"></agent-detail-view>`
    );

    await waitUntil(
      () => !(element as any).loading && (element as any).agent !== null,
      'Agent detail view did not finish loading'
    );

    (element as any).activeTab = 'tools';
    await waitUntil(() => (element as any).editorContextReady);
    await element.updateComplete;

    const select = element.shadowRoot?.querySelector(
      '#agent-approval-workflow-select'
    ) as any;
    expect(select).to.exist;

    // Options: the account-default fallback plus every workflow.
    const optionValues = Array.from(select.querySelectorAll('sl-option')).map(
      (option: any) => option.value
    );
    expect(optionValues).to.deep.equal(['', 'wf-1']);

    // Selecting a workflow persists it through the governance PUT.
    (element as any).saveApprovalWorkflowSelection('wf-1');
    await new Promise((resolve) => setTimeout(resolve, 100));

    const putCall = fetchStub
      .getCalls()
      .find(
        (call) =>
          String(call.args[0]) === '/api/v1/agents/agent-1/governance' &&
          call.args[1]?.method === 'PUT'
      );
    expect(putCall).to.exist;
    const body = JSON.parse(putCall!.args[1].body);
    expect(body.approval_workflow_id).to.equal('wf-1');
  });

  it('lets the user switch native tool approvals off', async () => {
    const element = await fixture<AgentDetailView>(
      html`<agent-detail-view agentId="agent-1"></agent-detail-view>`
    );

    await waitUntil(
      () => !(element as any).loading && (element as any).agent !== null,
      'Agent detail view did not finish loading'
    );

    (element as any).activeTab = 'tools';
    await waitUntil(() => (element as any).editorContextReady);
    await element.updateComplete;

    // Default (field absent) renders as inherit; with no account default of
    // "off", the effective mode is enforce: no bypass warning.
    const modeSelect = element.shadowRoot?.querySelector(
      '#agent-native-tool-approvals-mode'
    ) as any;
    expect(modeSelect).to.exist;
    expect(modeSelect.value).to.equal('');
    expect(
      element.shadowRoot?.querySelector('#agent-native-tool-approvals-off-note')
    ).to.not.exist;

    // Selecting "off" persists native_tool_approvals="off" via the PUT.
    (element as any).saveNativeToolApprovalsMode('off');
    await new Promise((resolve) => setTimeout(resolve, 100));

    const putCall = fetchStub
      .getCalls()
      .find(
        (call) =>
          String(call.args[0]) === '/api/v1/agents/agent-1/governance' &&
          call.args[1]?.method === 'PUT'
      );
    expect(putCall).to.exist;
    const body = JSON.parse(putCall!.args[1].body);
    expect(body.native_tool_approvals).to.equal('off');

    // The workflow select mutes and the bypass warning appears.
    await element.updateComplete;
    const select = element.shadowRoot?.querySelector(
      '#agent-approval-workflow-select'
    ) as any;
    expect(select.disabled).to.be.true;
    const note = element.shadowRoot?.querySelector(
      '#agent-native-tool-approvals-off-note'
    );
    expect(note).to.exist;
    const noteText = getDeepText(note).replace(/\s+/g, ' ');
    expect(noteText).to.contain('Approvals are bypassed server-side');
    expect(noteText).to.contain('How to fully disable the hook locally');

    // Selecting inherit clears the field (absent = inherit account default).
    (element as any).saveNativeToolApprovalsMode('');
    await new Promise((resolve) => setTimeout(resolve, 100));

    const enforcePut = fetchStub
      .getCalls()
      .filter(
        (call) =>
          String(call.args[0]) === '/api/v1/agents/agent-1/governance' &&
          call.args[1]?.method === 'PUT'
      )
      .pop();
    expect(enforcePut).to.exist;
    const enforceBody = JSON.parse(enforcePut!.args[1].body);
    expect(enforceBody.native_tool_approvals).to.equal(null);

    await element.updateComplete;
    expect(
      element.shadowRoot?.querySelector('#agent-native-tool-approvals-off-note')
    ).to.not.exist;
  });

  it('lets the user update the available models from the Models & Spend tab', async () => {
    const element = await fixture<AgentDetailView>(
      html`<agent-detail-view agentId="agent-1"></agent-detail-view>`
    );

    await waitUntil(
      () => !(element as any).loading && (element as any).agent !== null,
      'Agent detail view did not finish loading'
    );

    (element as any).activeTab = 'models';
    await waitUntil(() => (element as any).editorContextReady);
    await element.updateComplete;

    // Every configured AI model renders as an allow toggle.
    const toggles = Array.from(
      element.shadowRoot!.querySelectorAll(
        'sl-checkbox[data-model-allow-toggle]'
      )
    );
    expect(
      toggles.map((t: any) => t.getAttribute('data-model-allow-toggle'))
    ).to.deep.equal(['openai/gpt-5', 'anthropic/claude-sonnet-4']);

    // Governance already allows openai/gpt-5, so only that one is checked.
    const gptToggle = element.shadowRoot?.querySelector(
      'sl-checkbox[data-model-allow-toggle="openai/gpt-5"]'
    ) as any;
    expect(gptToggle.checked).to.be.true;
    const claudeToggle = element.shadowRoot?.querySelector(
      'sl-checkbox[data-model-allow-toggle="anthropic/claude-sonnet-4"]'
    ) as any;
    expect(claudeToggle.checked).to.be.false;

    // Checking a model persists it through the governance PUT.
    claudeToggle.checked = true;
    claudeToggle.dispatchEvent(new Event('sl-change'));
    await new Promise((resolve) => setTimeout(resolve, 100));

    const putCall = fetchStub
      .getCalls()
      .filter(
        (call) =>
          String(call.args[0]) === '/api/v1/agents/agent-1/governance' &&
          call.args[1]?.method === 'PUT'
      )
      .pop();
    expect(putCall).to.exist;
    const body = JSON.parse(putCall!.args[1].body);
    expect(body.allowed_models).to.deep.equal([
      'anthropic/claude-sonnet-4',
      'openai/gpt-5',
    ]);
    // Budgets ride along unchanged; the model list is not rewritten from them.
    expect(body.model_budgets).to.deep.equal({
      'openai/gpt-5': { monthly_usd_limit: 25 },
    });

    await element.updateComplete;
    expect((element as any).governance.allowed_models).to.deep.equal([
      'anthropic/claude-sonnet-4',
      'openai/gpt-5',
    ]);
    // The status line reflects restricted mode.
    const status = element.shadowRoot?.querySelector(
      '#available-models-status'
    );
    expect(getDeepText(status).replace(/\s+/g, ' ')).to.contain(
      'may only use the checked models'
    );
  });

  it('unchecking the only allowed model clears the restriction via the manual override', async () => {
    const element = await fixture<AgentDetailView>(
      html`<agent-detail-view agentId="agent-1"></agent-detail-view>`
    );

    await waitUntil(
      () => !(element as any).loading && (element as any).agent !== null,
      'Agent detail view did not finish loading'
    );

    (element as any).activeTab = 'models';
    await waitUntil(() => (element as any).editorContextReady);
    await element.updateComplete;

    // Governance allows exactly openai/gpt-5. Unchecking it empties the list,
    // which the backend reads as "every model allowed".
    const gptToggle = element.shadowRoot?.querySelector(
      'sl-checkbox[data-model-allow-toggle="openai/gpt-5"]'
    ) as any;
    gptToggle.checked = false;
    gptToggle.dispatchEvent(new Event('sl-change'));
    await new Promise((resolve) => setTimeout(resolve, 100));

    const putCall = fetchStub
      .getCalls()
      .filter(
        (call) =>
          String(call.args[0]) === '/api/v1/agents/agent-1/governance' &&
          call.args[1]?.method === 'PUT'
      )
      .pop();
    expect(putCall).to.exist;
    const body = JSON.parse(putCall!.args[1].body);
    expect(body.allowed_models).to.deep.equal([]);

    await element.updateComplete;
    // The toggle must stay unchecked after the re-render instead of bouncing
    // back to checked: an empty list is "all allowed", rendered as such.
    expect(gptToggle.checked).to.be.false;
    // This fixture agent has no configured bindings, so an empty allowlist
    // really is "every model allowed".
    const status = element.shadowRoot?.querySelector(
      '#available-models-status'
    );
    expect(getDeepText(status).replace(/\s+/g, ' ')).to.contain(
      'Every model is currently allowed'
    );

    const overrideInput = element.shadowRoot?.querySelector(
      'sl-input[label="Allowed models"]'
    ) as any;
    expect(overrideInput.value).to.equal('');
  });

  it('describes the configured-models default when bindings exist and no allowlist is set', async () => {
    const element = await fixture<AgentDetailView>(
      html`<agent-detail-view agentId="agent-1"></agent-detail-view>`
    );

    await waitUntil(
      () => !(element as any).loading && (element as any).agent !== null,
      'Agent detail view did not finish loading'
    );

    // Two onboarding bindings and an empty allowlist: the gateway offers the
    // agent exactly those two models, and the status line must say so
    // instead of claiming every model is allowed.
    (element as any).agent = {
      ...(element as any).agent,
      configured_models: [
        {
          ai_model_id: 'model-astra',
          gateway_alias: 'openai/gpt-6-astra',
          config_key: 'model',
          binding_type: 'configured',
          is_primary: true,
          status: 'gateway_ready',
        },
        {
          ai_model_id: 'model-sol',
          gateway_alias: 'openai/gpt-6.1-sol',
          config_key: 'gateway.autoregister.gpt-6.1-sol',
          binding_type: 'configured',
          is_primary: false,
          status: 'gateway_ready',
        },
      ],
    };
    (element as any).activeTab = 'models';
    await waitUntil(() => (element as any).editorContextReady);
    await element.updateComplete;
    // The governance fixture allows openai/gpt-5; clear it the way the
    // console does when the last checkbox is unchecked.
    (element as any).governance = {
      ...(element as any).governance,
      allowed_models: [],
    };
    (element as any).allowedModelsText = '';
    await element.updateComplete;

    const status = element.shadowRoot?.querySelector(
      '#available-models-status'
    );
    const text = getDeepText(status).replace(/\s+/g, ' ');
    expect(text).to.contain('uses its configured models');
    expect(text).to.contain('2');
    expect(text).to.not.contain('Every model is currently allowed');
  });

  it('saving budgets no longer derives allowed_models from the budget keys', async () => {
    const element = await fixture<AgentDetailView>(
      html`<agent-detail-view agentId="agent-1"></agent-detail-view>`
    );

    await waitUntil(
      () => !(element as any).loading && (element as any).agent !== null,
      'Agent detail view did not finish loading'
    );

    // Governance loads with allowed_models=['openai/gpt-5']. Editing the
    // budgets to add a different model must not silently allow that model.
    (element as any).activeTab = 'models';
    await waitUntil(() => (element as any).editorContextReady);
    (element as any).modelBudgetsText = JSON.stringify({
      'openai/gpt-5': { monthly_usd_limit: 25 },
      'anthropic/claude-sonnet-4': { monthly_usd_limit: 10 },
    });
    await (element as any).saveGovernance();

    const putCall = fetchStub
      .getCalls()
      .filter(
        (call) =>
          String(call.args[0]) === '/api/v1/agents/agent-1/governance' &&
          call.args[1]?.method === 'PUT'
      )
      .pop();
    expect(putCall).to.exist;
    const body = JSON.parse(putCall!.args[1].body);
    expect(body.model_budgets['anthropic/claude-sonnet-4']).to.deep.equal({
      monthly_usd_limit: 10,
    });
    expect(body.allowed_models).to.deep.equal(['openai/gpt-5']);
  });

  /**
   * Re-stub fetch so the account models carry display names distinct from
   * their gateway aliases and the stored allowlist uses the legacy keys
   * (display name, model id) the console used to persist.
   */
  function stubDisplayNamedModels(allowedModels: unknown[]): void {
    fetchStub.callsFake(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();
        if (url === '/api/v1/ai-models') {
          return new Response(
            JSON.stringify([
              {
                id: 'model-alpha',
                name: 'Alpha Chat',
                provider_name: 'acme',
                model_identifier: 'alpha-chat',
                meta_data: {
                  gateway: { enabled: true, model_alias: 'acme/alpha-chat' },
                },
              },
              {
                id: 'model-beta',
                name: 'Beta Flash',
                provider_name: 'other',
                model_identifier: 'beta-flash',
                meta_data: { gateway: { enabled: true } },
              },
            ]),
            { status: 200, headers: { 'Content-Type': 'application/json' } }
          );
        }
        if (
          url === '/api/v1/agents/agent-1/governance' &&
          (!init?.method || init.method === 'GET')
        ) {
          return new Response(
            JSON.stringify({
              subject_type: 'managed_agents',
              subject_id: 'agent-1',
              config: {
                allowed_models: allowedModels,
                model_budgets: {},
                tool_rules: {},
              },
            }),
            { status: 200, headers: { 'Content-Type': 'application/json' } }
          );
        }
        return defaultFetch(input, init);
      }
    );
  }

  /**
   * Re-stub fetch so two imports of the same upstream model share one bare
   * tail (acme/alpha-chat and vendor/alpha-chat).
   */
  function stubSameTailModels(allowedModels: string[]): void {
    fetchStub.callsFake(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();
        if (url === '/api/v1/ai-models') {
          return new Response(
            JSON.stringify([
              {
                id: 'model-alpha',
                name: 'Alpha Chat',
                provider_name: 'acme',
                model_identifier: 'alpha-chat',
                meta_data: {
                  gateway: { enabled: true, model_alias: 'acme/alpha-chat' },
                },
              },
              {
                id: 'model-alpha-import',
                name: 'Imported alpha-chat',
                provider_name: 'vendor',
                model_identifier: 'alpha-chat',
                meta_data: {
                  gateway: {
                    enabled: true,
                    model_alias: 'vendor/alpha-chat',
                  },
                },
              },
              {
                id: 'model-beta',
                name: 'Beta Flash',
                provider_name: 'other',
                model_identifier: 'beta-flash',
                meta_data: { gateway: { enabled: true } },
              },
            ]),
            { status: 200, headers: { 'Content-Type': 'application/json' } }
          );
        }
        if (
          url === '/api/v1/agents/agent-1/governance' &&
          (!init?.method || init.method === 'GET')
        ) {
          return new Response(
            JSON.stringify({
              subject_type: 'managed_agents',
              subject_id: 'agent-1',
              config: {
                allowed_models: allowedModels,
                model_budgets: {},
                tool_rules: {},
              },
            }),
            { status: 200, headers: { 'Content-Type': 'application/json' } }
          );
        }
        return defaultFetch(input, init);
      }
    );
  }

  /** Re-stub fetch with one model whose configured alias differs from its identifier. */
  function stubCustomAliasModel(allowedModels: string[]): void {
    fetchStub.callsFake(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();
        if (url === '/api/v1/ai-models') {
          return new Response(
            JSON.stringify([
              {
                id: 'model-gamma',
                name: 'Gamma Chat',
                provider_name: 'acme',
                model_identifier: 'gamma-upstream-v2',
                meta_data: {
                  gateway: { enabled: true, model_alias: 'team/gamma' },
                },
              },
            ]),
            { status: 200, headers: { 'Content-Type': 'application/json' } }
          );
        }
        if (
          url === '/api/v1/agents/agent-1/governance' &&
          (!init?.method || init.method === 'GET')
        ) {
          return new Response(
            JSON.stringify({
              subject_type: 'managed_agents',
              subject_id: 'agent-1',
              config: {
                allowed_models: allowedModels,
                model_budgets: {},
                tool_rules: {},
              },
            }),
            { status: 200, headers: { 'Content-Type': 'application/json' } }
          );
        }
        return defaultFetch(input, init);
      }
    );
  }

  async function loadModelsTab(): Promise<AgentDetailView> {
    const element = await fixture<AgentDetailView>(
      html`<agent-detail-view agentId="agent-1"></agent-detail-view>`
    );
    await waitUntil(
      () => !(element as any).loading && (element as any).agent !== null,
      'Agent detail view did not finish loading'
    );
    (element as any).activeTab = 'models';
    await waitUntil(() => (element as any).editorContextReady);
    await element.updateComplete;
    return element;
  }

  function lastGovernancePutBody(): any {
    const putCall = fetchStub
      .getCalls()
      .filter(
        (call) =>
          String(call.args[0]) === '/api/v1/agents/agent-1/governance' &&
          call.args[1]?.method === 'PUT'
      )
      .pop();
    expect(putCall).to.exist;
    return JSON.parse(putCall!.args[1].body);
  }

  it('renders legacy display-name allowlist entries as checked models and persists aliases', async () => {
    stubDisplayNamedModels(['Alpha Chat']);
    const element = await loadModelsTab();

    // Toggles are keyed by gateway alias, labelled by display name.
    const toggles = Array.from(
      element.shadowRoot!.querySelectorAll(
        'sl-checkbox[data-model-allow-toggle]'
      )
    );
    expect(
      toggles.map((t: any) => t.getAttribute('data-model-allow-toggle'))
    ).to.deep.equal(['acme/alpha-chat', 'other/beta-flash']);
    const alphaToggle = toggles[0] as any;
    expect(alphaToggle.checked).to.be.true;
    expect(getDeepText(alphaToggle)).to.contain('Alpha Chat');
    const betaToggle = toggles[1] as any;
    expect(betaToggle.checked).to.be.false;

    // The manual override shows the policy as aliases, not display names.
    const overrideInput = element.shadowRoot?.querySelector(
      'sl-input[label="Allowed models"]'
    ) as any;
    expect(overrideInput.value).to.equal('acme/alpha-chat');

    // Checking the second model rewrites the whole list to aliases.
    betaToggle.checked = true;
    betaToggle.dispatchEvent(new Event('sl-change'));
    await new Promise((resolve) => setTimeout(resolve, 100));
    expect(lastGovernancePutBody().allowed_models).to.deep.equal([
      'acme/alpha-chat',
      'other/beta-flash',
    ]);
  });

  it('renders model-id allowlist entries as checked and converts typed names to aliases', async () => {
    stubDisplayNamedModels(['model-beta']);
    const element = await loadModelsTab();

    const betaToggle = element.shadowRoot?.querySelector(
      'sl-checkbox[data-model-allow-toggle="other/beta-flash"]'
    ) as any;
    expect(betaToggle.checked).to.be.true;
    const alphaToggle = element.shadowRoot?.querySelector(
      'sl-checkbox[data-model-allow-toggle="acme/alpha-chat"]'
    ) as any;
    expect(alphaToggle.checked).to.be.false;

    // Typing display names, ids, bare tails, and unknown aliases into the
    // override persists aliases where a model is known and keeps the rest.
    const overrideInput = element.shadowRoot?.querySelector(
      'sl-input[label="Allowed models"]'
    ) as any;
    overrideInput.value = 'alpha chat, model-beta, beta-flash, other/unknown';
    overrideInput.dispatchEvent(new Event('sl-change'));
    await new Promise((resolve) => setTimeout(resolve, 100));
    expect(lastGovernancePutBody().allowed_models).to.deep.equal([
      'acme/alpha-chat',
      'other/beta-flash',
      'other/unknown',
    ]);
  });

  it('drops non-string allowlist entries instead of stringifying them', async () => {
    stubDisplayNamedModels(['Alpha Chat', null, 3]);
    const element = await loadModelsTab();

    const overrideInput = element.shadowRoot?.querySelector(
      'sl-input[label="Allowed models"]'
    ) as any;
    expect(overrideInput.value).to.equal('acme/alpha-chat');
  });

  it('keeps an ambiguous bare-tail entry as typed instead of narrowing it to one import', async () => {
    stubSameTailModels(['alpha-chat']);
    const element = await loadModelsTab();

    // The bare tail matches two rows, so it resolves to neither alias: the
    // override shows it unchanged and both same-tail toggles render
    // unchecked (the backend honours the tail for both rows).
    const overrideInput = element.shadowRoot?.querySelector(
      'sl-input[label="Allowed models"]'
    ) as any;
    expect(overrideInput.value).to.equal('alpha-chat');
    for (const alias of ['acme/alpha-chat', 'vendor/alpha-chat']) {
      const toggle = element.shadowRoot?.querySelector(
        `sl-checkbox[data-model-allow-toggle="${alias}"]`
      ) as any;
      expect(toggle.checked).to.be.false;
    }

    // Toggling an unrelated model must not rewrite the bare tail to one
    // import's alias.
    const betaToggle = element.shadowRoot?.querySelector(
      'sl-checkbox[data-model-allow-toggle="other/beta-flash"]'
    ) as any;
    betaToggle.checked = true;
    betaToggle.dispatchEvent(new Event('sl-change'));
    await new Promise((resolve) => setTimeout(resolve, 100));
    expect(lastGovernancePutBody().allowed_models).to.deep.equal([
      'alpha-chat',
      'other/beta-flash',
    ]);
  });

  it('checks a model whose allowlist entry is its bare model_identifier under a custom alias', async () => {
    stubCustomAliasModel(['gamma-upstream-v2']);
    const element = await loadModelsTab();

    // The gateway accepts the bare identifier even though the model answers
    // to team/gamma, so the console must show it as allowed and write it
    // back under the alias.
    const gammaToggle = element.shadowRoot?.querySelector(
      'sl-checkbox[data-model-allow-toggle="team/gamma"]'
    ) as any;
    expect(gammaToggle.checked).to.be.true;
    const overrideInput = element.shadowRoot?.querySelector(
      'sl-input[label="Allowed models"]'
    ) as any;
    expect(overrideInput.value).to.equal('team/gamma');

    gammaToggle.checked = false;
    gammaToggle.dispatchEvent(new Event('sl-change'));
    await new Promise((resolve) => setTimeout(resolve, 100));
    expect(lastGovernancePutBody().allowed_models).to.deep.equal([]);
  });

  describe('navigation and failure states', () => {
    afterEach(() => {
      window.history.replaceState({}, '', '/');
    });

    it('writes the open tab to ?tab= so a reload keeps it', async () => {
      window.history.replaceState({}, '', '/console/agents/agent-1');
      const element = await fixture<AgentDetailView>(
        html`<agent-detail-view agentId="agent-1"></agent-detail-view>`
      );
      await waitUntil(() => !(element as any).loading);
      await element.updateComplete;

      const group = element.shadowRoot!.querySelector('sl-tab-group')!;
      group.dispatchEvent(
        new CustomEvent('sl-tab-show', { detail: { name: 'tools' } })
      );
      expect(new URL(window.location.href).searchParams.get('tab')).to.equal(
        'tools'
      );
      expect(window.location.pathname).to.equal('/console/agents/agent-1');
    });

    it('sends Back to the Agents list on a page loaded directly', async () => {
      window.history.replaceState(null, '', '/console/agents/agent-1');
      const element = await fixture<AgentDetailView>(
        html`<agent-detail-view agentId="agent-1"></agent-detail-view>`
      );
      await waitUntil(() => !(element as any).loading);
      await element.updateComplete;
      const back = sinon.stub(window.history, 'back');
      try {
        const click = new MouseEvent('click', { cancelable: true });
        (element as any).handleBack(click);
        // The click is left alone, so the button's href (/console/agents)
        // is followed instead of leaving the console.
        expect(click.defaultPrevented).to.equal(false);
        expect(back.called).to.equal(false);
      } finally {
        back.restore();
      }
    });

    it('goes back in history after an in-app navigation', async () => {
      // The router writes in-app entries with pushState, which never updates
      // document.referrer; the entry's own in-app depth is what counts.
      window.history.replaceState(null, '', '/console/agents');
      window.history.pushState(
        historyStateForNavigation('push'),
        '',
        '/console/agents/agent-1'
      );
      const element = await fixture<AgentDetailView>(
        html`<agent-detail-view agentId="agent-1"></agent-detail-view>`
      );
      await waitUntil(() => !(element as any).loading);
      await element.updateComplete;
      const back = sinon.stub(window.history, 'back');
      try {
        const click = new MouseEvent('click', { cancelable: true });
        (element as any).handleBack(click);
        expect(click.defaultPrevented).to.equal(true);
        expect(back.calledOnce).to.equal(true);
      } finally {
        back.restore();
      }
    });

    it('links Back to the Agents list for a reader with no history here', async () => {
      const element = await fixture<AgentDetailView>(
        html`<agent-detail-view agentId="agent-1"></agent-detail-view>`
      );
      await waitUntil(() => !(element as any).loading);
      await element.updateComplete;
      const back = element.shadowRoot!.querySelector('sl-button.back-button');
      expect(back?.getAttribute('href')).to.equal('/console/agents');
    });

    it('offers Back to Agents and Try again when the agent fails to load', async () => {
      let fail = true;
      fetchStub.callsFake(
        async (input: RequestInfo | URL, init?: RequestInit) => {
          const url = typeof input === 'string' ? input : input.toString();
          if (fail && url.startsWith('/api/v1/agents/agent-1')) {
            return new Response(JSON.stringify({ detail: 'Unavailable' }), {
              status: 500,
            });
          }
          return defaultFetch(input, init);
        }
      );
      const element = await fixture<AgentDetailView>(
        html`<agent-detail-view agentId="agent-1"></agent-detail-view>`
      );
      await waitUntil(() => !(element as any).loading);
      await element.updateComplete;

      const root = element.shadowRoot!;
      const alert = root.querySelector('[data-agent-error]');
      expect(alert?.textContent).to.contain('Could not load this agent');
      const back = root.querySelector('view-header sl-button');
      expect(back?.getAttribute('href')).to.equal('/console/agents');
      expect(back?.textContent).to.contain('Back to Agents');

      fail = false;
      const retry = [...alert!.querySelectorAll('sl-button')].find((button) =>
        button.textContent?.includes('Try again')
      ) as HTMLElement;
      retry.click();
      await waitUntil(() =>
        getDeepText(element).includes('Claude Code Workspace')
      );
    });

    it('says "Agent not found" without internal jargon', async () => {
      const element = document.createElement(
        'agent-detail-view'
      ) as AgentDetailView & Record<string, any>;
      element.initialized = true;
      element.loading = false;
      element.error = null;
      element.agent = null;
      element.loadData = async () => undefined;
      document.body.appendChild(element);
      try {
        element.loading = false;
        await element.updateComplete;
        const text = element.shadowRoot!.textContent || '';
        expect(text).to.contain('Agent not found');
        expect(text).to.not.contain('Managed agent');
      } finally {
        element.remove();
      }
    });
  });
});

describe('agent scoped rule save settlement', () => {
  afterEach(() => sinon.restore());
  it('settles the editor only after governance persistence and restores rules on error', async () => {
    const el = await fixture<AgentDetailView>(
      html`<agent-detail-view></agent-detail-view>`
    );
    const view = el as any;
    const previous = { read_file: [] };
    view.scopedToolRules = previous;
    const save = sinon
      .stub(view, 'saveGovernance')
      .rejects(new Error('Invalid scoped condition'));
    const resolve = sinon.spy();
    const reject = sinon.spy();
    await view.saveScopedToolRule(
      'read_file',
      null,
      { action: 'deny' },
      { resolve, reject }
    );
    expect(resolve.called).to.equal(false);
    expect(reject.firstCall.args[0]).to.equal('Invalid scoped condition');
    expect(view.scopedToolRules).to.equal(previous);
    save.resolves();
    await view.saveScopedToolRule(
      'read_file',
      null,
      { action: 'deny' },
      { resolve, reject }
    );
    expect(resolve.calledOnce).to.equal(true);
    expect(view.scopedToolRules.read_file).to.have.length(1);
  });
});
