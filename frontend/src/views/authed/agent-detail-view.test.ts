import { expect, fixture, html, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import './agent-detail-view.ts';
import type { AgentDetailView } from './agent-detail-view';

describe('AgentDetailView', () => {
  let fetchStub: sinon.SinonStub;

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
    fetchStub.callsFake(
      async (input: RequestInfo | URL, init?: RequestInit) => {
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
                  search_issues: [
                    { action: 'allow', condition_type: 'simple' },
                  ],
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

        if (
          url.includes('/api/v1/runtime-sessions/runtime-session-2/activity')
        ) {
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
              },
              {
                id: 'model-anthropic-claude',
                name: 'anthropic/claude-sonnet-4',
                provider_name: 'anthropic',
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
      }
    );
  });

  afterEach(() => {
    fetchStub.restore();
    localStorage.clear();
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
    expect(content).to.contain('Live validated');
    expect(content).to.contain('openai/gpt-5');
    expect(
      element.shadowRoot?.querySelector('sl-tooltip')?.getAttribute('content')
    ).to.contain('Tool calls and model traffic both flow through Preloop.');

    (element as any).activeTab = 'models';
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
    // proven but model traffic is unverified. The badge must say so — the
    // old fallthrough rendered an eternal "Live check pending".
    (element as any).agent = {
      ...(element as any).agent,
      live_validation_passed: false,
      live_validation_status: 'throttled',
    };
    await element.updateComplete;

    const content = getDeepText(element).replace(/\s+/g, ' ');
    expect(content).to.contain('Live check throttled — unverified');
    expect(content).to.not.contain('Live check pending');
    expect((element as any).getLiveValidationVariant()).to.equal('warning');

    (element as any).agent = {
      ...(element as any).agent,
      live_validation_status: 'upstream_unavailable',
    };
    await element.updateComplete;
    expect(getDeepText(element)).to.contain('Upstream refused — unverified');
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
});
