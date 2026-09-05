import { expect, fixture, html, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import '../../components/view-header.ts';
import { unifiedWebSocketManager } from '../../services/unified-websocket-manager';
import './dashboard-control-plane-view';
import type { DashboardView } from './dashboard-control-plane-view';

describe('DashboardView', () => {
  let fetchStub: sinon.SinonStub;
  let connectStub: sinon.SinonStub;
  let subscribeStub: sinon.SinonStub;
  let gatewaySummaryResponse: any;
  let runtimeSessionsResponse: any;
  let agentsResponse: any;
  let gatewaySearchResponse: any;
  let auditResponse: any;
  let trackersResponse: any;
  let apiKeysResponse: any;
  let issueCountResponse: any;
  let mcpServersResponse: any;
  let toolsResponse: any;
  let flowsResponse: any[];
  let flowExecutionsResponse: any[];
  let pendingApprovalRequestsResponse: any[];
  let allApprovalRequestsResponse: any[];
  let aiModelsResponse: any[];
  let aiModelsOverviewResponse: any;
  let usersResponse: any;
  let budgetPoliciesResponse: any[];
  /** Set by a test that wants to hold the secondary pass open. */
  let aiModelsGate: Promise<void> | null;
  /** Holds `GET /api/v1/tools` so Next steps and the Tools tab can wait. */
  let toolsGate: Promise<void> | null;
  /** Holds `GET /api/v1/flows` so the Flows tab can paint identity first. */
  let flowsGate: Promise<void> | null;
  /** Holds flow executions so Flows usage can arrive after the list. */
  let executionsGate: Promise<void> | null;
  /** Holds `GET /api/v1/ai-models/overview` so Models usage can wait. */
  let overviewGate: Promise<void> | null;
  /**
   * Holds the Overview's wave-2 breakdown call in flight. Attention also
   * asks for a 30-day copy after first paint.
   */
  let breakdownGate: Promise<void> | null;
  let breakdownCalls = 0;
  /** Holds every gateway summary call, for tests about a range change. */
  let summaryGate: Promise<void> | null;
  /** null means RBAC is off, so every permission check passes. */
  let mePermissions: string[] | null;

  beforeEach(() => {
    aiModelsGate = null;
    toolsGate = null;
    flowsGate = null;
    executionsGate = null;
    overviewGate = null;
    breakdownGate = null;
    breakdownCalls = 0;
    summaryGate = null;
    mePermissions = null;
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');

    gatewaySummaryResponse = {
      period_start: '2026-03-01T00:00:00Z',
      period_end: '2026-03-31T00:00:00Z',
      total_requests: 22,
      successful_requests: 19,
      failed_requests: 3,
      token_usage: {
        prompt_tokens: 1000,
        completion_tokens: 500,
        total_tokens: 1500,
      },
      estimated_cost: 12.34,
      budget: {
        monthly_limit_usd: 50,
        soft_limit_usd: 40,
        current_spend_usd: 12.34,
        soft_limit_exceeded: false,
        hard_limit_exceeded: false,
      },
      requests_by_day: [],
      usage_by_model: [
        {
          ai_model_id: 'model-1',
          model_alias: 'gpt-5.4',
          provider_name: 'openai',
          request_count: 10,
          token_usage: {
            prompt_tokens: 100,
            completion_tokens: 50,
            total_tokens: 150,
          },
          estimated_cost: 6.5,
        },
      ],
      usage_by_flow: [],
      usage_by_session: [
        {
          runtime_session_id: 'runtime-session-1',
          session_source_type: 'managed_agent',
          session_source_id: 'hermes-runtime-principal',
          agent_id: 'agent-1',
          agent_name: 'Ops Agent',
          title: 'Agent Session',
          flow_execution_id: null,
          flow_id: null,
          flow_name: null,
          session_reference: 'Agent Session',
          model_alias: 'gpt-5.4',
          provider_name: 'openai',
          request_count: 8,
          token_usage: {
            prompt_tokens: 10,
            completion_tokens: 5,
            total_tokens: 15,
          },
          estimated_cost: 4.2,
          last_request_at: '2026-03-07T10:00:00Z',
        },
      ],
    };

    runtimeSessionsResponse = {
      period_start: '2026-03-01T00:00:00Z',
      period_end: '2026-03-31T00:00:00Z',
      query: null,
      session_source_type: null,
      status: 'all',
      total: 1,
      limit: 12,
      offset: 0,
      items: [
        {
          id: 'runtime-session-1',
          session_source_type: 'managed_agent',
          session_source_id: 'hermes-runtime-principal',
          session_reference: 'Agent Session',
          runtime_principal_type: 'managed_agent',
          runtime_principal_id: 'hermes-runtime-principal',
          runtime_principal_name: 'Ops Agent',
          started_at: '2026-03-07T09:00:00Z',
          last_activity_at: '2026-03-07T10:05:00Z',
          ended_at: null,
          flow_id: null,
          flow_name: null,
          flow_execution_id: null,
          latest_model_alias: 'gpt-5.4',
          latest_provider_name: 'openai',
          total_requests: 8,
          successful_requests: 7,
          failed_requests: 1,
          token_usage: {
            prompt_tokens: 10,
            completion_tokens: 5,
            total_tokens: 15,
          },
          estimated_cost: 4.2,
          last_request_at: '2026-03-07T10:00:00Z',
          activity_status: 'active_now',
        },
      ],
    };

    agentsResponse = {
      query: null,
      session_source_type: null,
      status: 'all',
      total: 1,
      limit: 12,
      offset: 0,
      items: [
        {
          id: 'agent-1',
          runtime_session_id: 'runtime-session-1',
          display_name: 'Ops Agent',
          session_source_type: 'managed_agent',
          session_source_id: 'hermes-runtime-principal',
          session_reference: 'Agent Session',
          enrolled_via: 'runtime_session_token',
          managed_mcp_servers: ['github'],
          last_seen_at: '2026-03-07T10:05:00Z',
          started_at: '2026-03-07T09:00:00Z',
          last_activity_at: '2026-03-07T10:05:00Z',
          ended_at: null,
          total_requests: 8,
          estimated_cost: 4.2,
          latest_model_alias: 'gpt-5.4',
          latest_provider_name: 'openai',
          last_request_at: '2026-03-07T10:00:00Z',
          activity_status: 'active_now',
        },
      ],
    };

    gatewaySearchResponse = {
      period_start: '2026-03-01T00:00:00Z',
      period_end: '2026-03-31T00:00:00Z',
      query: null,
      total: 2,
      limit: 12,
      offset: 0,
      items: [
        {
          api_usage_id: 'usage-1',
          timestamp: '2026-03-07T10:00:00Z',
          status_code: 502,
          outcome: 'error',
          endpoint: '/openai/v1/responses',
          method: 'POST',
          provider_name: 'openai',
          model_alias: 'gpt-5.4',
          flow_id: null,
          flow_name: null,
          flow_execution_id: null,
          runtime_session_id: 'runtime-session-1',
          session_source_type: 'managed_agent',
          session_source_id: 'hermes-runtime-principal',
          session_reference: 'Agent Session',
          runtime_principal_type: 'managed_agent',
          runtime_principal_id: 'hermes-runtime-principal',
          runtime_principal_name: 'Ops Agent',
          auth_subject_type: 'api_key',
          api_key_id: 'api-key-1',
          api_key_name: 'Runtime Session managed_agent:agent-1',
          estimated_cost: 0,
          token_usage: {
            prompt_tokens: 0,
            completion_tokens: 0,
            total_tokens: 0,
          },
          excerpt: '',
          meta_data: {},
        },
      ],
    };

    auditResponse = {
      groups: [
        {
          correlation_id: null,
          outcome: 'failed',
          primary_event: {
            id: 'audit-1',
            action: 'model_gateway_request',
            status: 'failed',
            timestamp: '2026-03-07T10:00:00Z',
            details: { requested_model: 'gpt-5.4' },
          },
          sub_events: [],
        },
      ],
      total: 1,
    };

    trackersResponse = [
      { id: 'tracker-1', name: 'GitHub', type: 'github' },
      { id: 'tracker-2', name: 'Jira', type: 'jira' },
    ];
    apiKeysResponse = [];
    issueCountResponse = { total_issues: 27 };
    mcpServersResponse = [
      {
        id: 'mcp-1',
        name: 'Example MCP Server',
        url: 'http://localhost:8001/mcp',
        status: 'active',
      },
    ];
    toolsResponse = [
      {
        name: 'verify_refund_eligibility',
        source: 'builtin',
        source_id: null,
        source_name: 'builtin',
        schema: {},
        is_enabled: true,
        approval_workflow_id: null,
        has_approval_condition: false,
      },
      {
        name: 'refund_order',
        source: 'mcp',
        source_id: 'mcp-1',
        source_name: 'Example MCP Server',
        schema: {},
        is_enabled: true,
        approval_workflow_id: 'approval-1',
        has_approval_condition: true,
      },
    ];
    flowsResponse = [{ id: 'flow-1', name: 'Refund Assistant' }];
    // Relative, not a fixed date: the Inventory Flows tab counts the runs the
    // page range contains, so a run pinned to a calendar day leaves the range
    // as soon as the clock moves past it.
    flowExecutionsResponse = [
      {
        id: 'execution-1',
        flow_id: 'flow-1',
        flow_name: 'Refund Assistant',
        status: 'FAILED',
        start_time: new Date(Date.now() - 2 * 3600 * 1000).toISOString(),
        end_time: new Date(Date.now() - 2 * 3600 * 1000 + 180000).toISOString(),
        error_message: 'Provider timeout',
      },
    ];
    pendingApprovalRequestsResponse = [
      {
        id: 'approval-1',
        tool_name: 'refund_order',
        status: 'pending',
        requested_at: '2026-03-07T10:01:00Z',
      },
    ];
    allApprovalRequestsResponse = [
      ...pendingApprovalRequestsResponse,
      {
        id: 'approval-2',
        tool_name: 'send_email',
        status: 'approved',
        requested_at: '2026-03-07T09:00:00Z',
        resolved_at: '2026-03-07T09:02:00Z',
      },
      {
        id: 'approval-3',
        tool_name: 'rollback_deployment',
        status: 'declined',
        requested_at: '2026-03-07T08:00:00Z',
        resolved_at: '2026-03-07T08:10:00Z',
      },
    ];
    aiModelsResponse = [
      {
        id: 'model-1',
        name: 'OpenAI GPT-5.4',
        provider_name: 'openai',
        model_identifier: 'gpt-5.4',
      },
    ];
    aiModelsOverviewResponse = {
      period_start: '2026-03-01T00:00:00Z',
      period_end: '2026-03-31T00:00:00Z',
      models: [
        {
          ai_model_id: 'model-1',
          model_name: 'OpenAI GPT-5.4',
          provider_name: 'openai',
          model_identifier: 'gpt-5.4',
          model_alias: 'gpt-5.4',
          is_default: true,
          total_requests: 10,
          successful_requests: 9,
          failed_requests: 1,
          token_usage: {
            prompt_tokens: 700,
            completion_tokens: 300,
            total_tokens: 1000,
          },
          estimated_cost: 5.5,
          unpriced_request_count: 0,
          active_session_count: 1,
          last_request_at: '2026-03-09T18:30:00Z',
          pricing_source: 'catalog',
        },
      ],
    };
    usersResponse = {
      users: [{ id: 'user-1', is_active: true }],
      total: 1,
      skip: 0,
      limit: 100,
    };
    budgetPoliciesResponse = [
      {
        id: 'budget-global',
        subject_type: 'global',
        subject_id: null,
        model_alias: null,
        period: 'monthly',
        hard_limit_usd: 50,
        soft_limit_usd: 40,
        notify_on_soft: true,
        notify_on_hard: true,
        notification_emails: null,
      },
      {
        id: 'budget-global-daily',
        subject_type: 'global',
        subject_id: null,
        model_alias: null,
        period: 'daily',
        hard_limit_usd: 10,
        soft_limit_usd: 8,
        notify_on_soft: true,
        notify_on_hard: true,
        notification_emails: null,
      },
      {
        id: 'budget-agent',
        subject_type: 'managed_agent',
        subject_id: 'agent-1',
        model_alias: null,
        period: 'monthly',
        hard_limit_usd: 25,
        soft_limit_usd: 20,
        current_spend_usd: 4.2,
        notify_on_soft: true,
        notify_on_hard: true,
        notification_emails: null,
      },
    ];

    fetchStub = sinon
      .stub(window, 'fetch')
      .callsFake(async (input: RequestInfo | URL) => {
        const url = typeof input === 'string' ? input : input.toString();
        const json = (data: unknown) =>
          new Response(JSON.stringify(data), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });

        if (url.startsWith('/api/v1/account/gateway-usage/summary')) {
          if (summaryGate) {
            await summaryGate;
          }
          if (url.includes('include_breakdown=true')) {
            breakdownCalls += 1;
            if (breakdownGate) {
              await breakdownGate;
            }
          }
          if (url.includes('runtime_principal_id=hermes-runtime-principal')) {
            return json({
              ...gatewaySummaryResponse,
              estimated_cost: 4.2,
              budget: {
                ...gatewaySummaryResponse.budget,
                current_spend_usd: 4.2,
              },
            });
          }
          return json(gatewaySummaryResponse);
        }

        if (url.startsWith('/api/v1/runtime-sessions')) {
          return json(runtimeSessionsResponse);
        }

        if (url.startsWith('/api/v1/agents')) {
          return json(agentsResponse);
        }

        if (url.startsWith('/api/v1/account/gateway-usage/search')) {
          return json(gatewaySearchResponse);
        }

        if (url.startsWith('/api/v1/audit-logs/grouped')) {
          return json(auditResponse);
        }

        if (url.startsWith('/api/v1/trackers')) {
          return json(trackersResponse);
        }

        if (url === '/api/v1/auth/api-keys') {
          return json(apiKeysResponse);
        }

        if (url === '/api/v1/auth/users/me') {
          return json({
            username: 'tester',
            email: 'tester@example.com',
            email_verified: true,
            is_superuser: false,
            permissions: mePermissions,
          });
        }

        if (url === '/api/v1/issue-count') {
          return json(issueCountResponse);
        }

        if (url.startsWith('/api/v1/mcp-servers')) {
          return json(mcpServersResponse);
        }

        if (url.startsWith('/api/v1/tools')) {
          if (toolsGate) await toolsGate;
          return json(toolsResponse);
        }

        if (url.startsWith('/api/v1/flows/executions')) {
          if (executionsGate) await executionsGate;
          return json(flowExecutionsResponse);
        }

        if (url.startsWith('/api/v1/flows')) {
          if (flowsGate) await flowsGate;
          return json(flowsResponse);
        }

        if (url.startsWith('/api/v1/approval-requests')) {
          return json(
            url.includes('status=pending')
              ? pendingApprovalRequestsResponse
              : allApprovalRequestsResponse
          );
        }

        if (url.startsWith('/api/v1/ai-models/overview')) {
          if (overviewGate) await overviewGate;
          return json(aiModelsOverviewResponse);
        }

        if (url === '/api/v1/ai-models') {
          if (aiModelsGate) await aiModelsGate;
          return json(aiModelsResponse);
        }

        if (url === '/api/v1/users?skip=0&limit=100') {
          return json(usersResponse);
        }

        if (url.startsWith('/api/v1/budget/policies')) {
          return json(budgetPoliciesResponse);
        }

        if (url === '/api/v1/features') {
          return json({ features: { billing: true } });
        }

        return json({ detail: `Unhandled ${url}` });
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
    sessionStorage.clear();
  });

  async function mountDashboard(): Promise<DashboardView> {
    return fixture(html`<dashboard-view></dashboard-view>`);
  }

  it('renders the merged overview dashboard with legacy and control-plane cards', async () => {
    const element = await mountDashboard();
    await waitUntil(
      () =>
        !element['loading'] &&
        !element['fetchingAudit'] &&
        !element['fetchingMCPAndTools'] &&
        element['attentionInputs'] != null,
      'dashboard did not finish loading'
    );
    await element.updateComplete;

    const header = element.shadowRoot?.querySelector('view-header');
    expect(header?.getAttribute('headerText')).to.equal('Overview');
    // Both boxes own their shadow roots, so the page holds the elements and
    // their own tests hold their contents.
    expect(element.shadowRoot?.querySelector('inventory-card')).to.exist;
    expect(element.shadowRoot?.querySelector('activity-feed')).to.exist;
    expect(element.shadowRoot?.textContent).to.contain('Audit exceptions');
    expect(element.shadowRoot?.textContent).to.contain('need attention');
  });

  it('subscribes to realtime topics and fetches dashboard data', async () => {
    const element = await mountDashboard();
    await waitUntil(
      () =>
        !element['loading'] &&
        !element['fetchingAgents'] &&
        !element['fetchingBudget'] &&
        !element['fetchingAudit'] &&
        !element['fetchingMCPAndTools'] &&
        element['attentionInputs'] != null,
      'dashboard did not finish loading'
    );

    expect(connectStub).to.have.been.calledOnce;
    // Six for the page's own topic refreshers (audit and the authenticated
    // signal no longer cost a request), seven for the Activity feed's topics.
    expect(subscribeStub.callCount).to.equal(13);

    const urls = fetchStub.getCalls().map((call) => String(call.args[0]));
    expect(
      urls.some((url) =>
        url.startsWith('/api/v1/account/gateway-usage/summary')
      )
    ).to.be.true;
    // Two for the cards (summary + breakdown upgrade), one for the prior
    // window behind the Usage delta, and one rolling 30-day breakdown for
    // attention (a calendar month is not those 30 days).
    expect(
      urls.filter((url) =>
        url.startsWith('/api/v1/account/gateway-usage/summary')
      ).length
    ).to.equal(4);
    expect(urls.some((url) => url.includes('include_breakdown=false'))).to.be
      .true;
    expect(
      urls.filter((url) => url.includes('include_breakdown=true')).length
    ).to.equal(2);
    // One list of agents for the whole load: the attention rules are handed
    // the list the fold already fetched.
    expect(
      urls.filter((url) => url.startsWith('/api/v1/agents')).length
    ).to.equal(1);
    expect(urls.some((url) => url === '/api/v1/auth/api-usage')).to.be.false;
    expect(urls.some((url) => url.startsWith('/api/v1/audit-logs/grouped'))).to
      .be.true;
    expect(urls).to.include('/api/v1/trackers');
    expect(urls).to.include('/api/v1/mcp-servers');
    expect(urls).to.include('/api/v1/tools');
    expect(urls).to.include('/api/v1/flows');
    // Raised from 10 in wave 6: the Inventory Flows tab counts runs and
    // failures per flow across the range, not the last five runs.
    expect(urls).to.include('/api/v1/flows/executions?limit=100');
    expect(urls.some((url) => url.startsWith('/api/v1/approval-requests'))).to
      .be.true;

    const updatedAt = element.shadowRoot?.querySelector('.updated-at');
    expect(updatedAt?.textContent || '').to.match(/Updated (just now|\d)/);
    expect(updatedAt?.textContent || '').to.not.contain('Never');
    expect(updatedAt?.textContent || '').to.not.contain('Loading');
  });

  it('hides exception cards when there is nothing actionable to show', async () => {
    gatewaySearchResponse = {
      ...gatewaySearchResponse,
      items: [],
    };
    auditResponse = {
      groups: [],
      total: 0,
    };

    const element = await mountDashboard();
    await waitUntil(
      () =>
        !element['loading'] &&
        !element['fetchingAudit'] &&
        !element['fetchingMCPAndTools'],
      'dashboard did not finish loading'
    );
    await element.updateComplete;

    const content = element.shadowRoot?.textContent || '';
    expect(content).to.not.contain('Gateway failures needing attention');
    expect(content).to.not.contain('Audit exceptions');
  });

  it('shows usage first with the global budgets under it', async () => {
    const element = await mountDashboard();
    await waitUntil(
      () =>
        !element['loading'] &&
        !element['fetchingAgents'] &&
        !element['fetchingBudget'] &&
        !element['fetchingAudit'] &&
        !element['fetchingMCPAndTools'],
      'dashboard did not finish loading'
    );
    await element.updateComplete;

    expect(element.shadowRoot?.querySelector('budget-health-card')).to.not
      .exist;

    const usageCard = element.shadowRoot?.querySelector('usage-card');
    expect(usageCard).to.exist;
    await usageCard?.updateComplete;
    const usageContent = usageCard?.shadowRoot?.textContent || '';

    // Tokens lead, dollars are one toggle away.
    expect(usageContent).to.contain('1.5K');
    expect(usageContent).to.contain('tokens · 30d');
    expect(usageContent).to.contain('1K prompt');

    // Global policies only, ordered daily then monthly; the agent policy is
    // summarised on one line.
    expect(usageContent).to.contain('Daily budget');
    expect(usageContent).to.contain('Monthly budget');
    expect(usageContent).to.contain('$50.00');
    expect(usageContent).to.contain('+ 1 more limit (agents)');
    expect(usageContent).to.contain('Configure limits');
  });

  it('offers to set a budget when none is configured', async () => {
    gatewaySummaryResponse = {
      ...gatewaySummaryResponse,
      total_requests: 0,
      successful_requests: 0,
      failed_requests: 0,
      estimated_cost: 0,
      token_usage: {
        prompt_tokens: 0,
        completion_tokens: 0,
        total_tokens: 0,
      },
      budget: {
        monthly_limit_usd: null,
        soft_limit_usd: null,
        current_spend_usd: 0,
        soft_limit_exceeded: false,
        hard_limit_exceeded: false,
      },
      usage_by_model: [],
      usage_by_session: [],
    };
    budgetPoliciesResponse = [];

    const element = await mountDashboard();
    await waitUntil(
      () =>
        !element['loading'] &&
        !element['fetchingAgents'] &&
        !element['fetchingBudget'] &&
        !element['fetchingAudit'] &&
        !element['fetchingMCPAndTools'],
      'dashboard did not finish loading'
    );
    await element.updateComplete;

    const usageCard = element.shadowRoot?.querySelector('usage-card');
    await usageCard?.updateComplete;
    const usageContent = usageCard?.shadowRoot?.textContent || '';
    expect(usageContent).to.contain('No budget set.');
    expect(usageContent).to.contain('Configure limits');
    expect(usageContent).to.contain('Cost details');
  });

  /** A usage summary whose only oddity is a model that costs nothing. */
  const zeroPricedSummary = (): any => ({
    total_requests: 12,
    successful_requests: 12,
    failed_requests: 0,
    token_usage: { prompt_tokens: 4, completion_tokens: 4, total_tokens: 40 },
    estimated_cost: 0,
    unpriced_requests: 0,
    requests_by_day: [],
    price_catalog: { fetched_at: new Date().toISOString(), model_count: 120 },
    usage_by_model: [
      {
        ai_model_id: 'model-2',
        model_alias: 'local/qwen-3-coder',
        provider_name: 'ollama',
        request_count: 12,
        token_usage: {
          prompt_tokens: 4,
          completion_tokens: 4,
          total_tokens: 40,
        },
        estimated_cost: 0,
        unpriced_request_count: 0,
        zero_priced_request_count: 12,
        last_request_at: new Date(Date.now() - 60_000).toISOString(),
      },
    ],
    usage_by_flow: [],
    usage_by_session: [],
  });

  it('surfaces pending approvals in the attention strip', async () => {
    const element = await mountDashboard();
    await waitUntil(
      () =>
        !element['loading'] &&
        !element['fetchingApprovals'] &&
        !element['fetchingAudit'] &&
        !element['fetchingMCPAndTools'] &&
        element['attentionInputs'] != null,
      'dashboard did not finish loading'
    );
    await element.updateComplete;

    const strip = element.shadowRoot?.querySelector('.attention-strip');
    expect(strip, 'the attention strip').to.exist;
    expect(strip?.textContent).to.contain('need attention');
    expect(
      strip?.querySelector('a.attention-strip-all')?.getAttribute('href')
    ).to.equal('/console/attention');
    expect(element.shadowRoot?.textContent || '').to.not.contain(
      'Pending approvals'
    );
  });

  it('keeps the attention strip on one line at 1440 and shows at most three items', async () => {
    const element = await mountDashboard();
    await waitUntil(
      () =>
        !element['loading'] &&
        !element['fetchingApprovals'] &&
        !element['fetchingAudit'] &&
        !element['fetchingMCPAndTools'] &&
        element['attentionInputs'] != null,
      'dashboard did not finish loading'
    );
    await element.updateComplete;

    const strip = element.shadowRoot?.querySelector(
      '.attention-strip'
    ) as HTMLElement;
    expect(strip, 'the attention strip').to.exist;
    const chips = strip.querySelectorAll('a.attention-chip-link');
    expect(chips.length, 'chips shown inline').to.be.at.most(3);
    // The whole thing is a line, not a card: a side card cost 5 rows plus a
    // header for the same facts.
    expect(strip.offsetHeight, 'strip height').to.be.at.most(56);
  });

  it('points each strip chip at its own row on the attention page', async () => {
    const element = await mountDashboard();
    await waitUntil(
      () =>
        !element['loading'] &&
        !element['fetchingApprovals'] &&
        !element['fetchingAudit'] &&
        !element['fetchingMCPAndTools'] &&
        element['attentionInputs'] != null,
      'dashboard did not finish loading'
    );
    await element.updateComplete;

    const strip = element.shadowRoot?.querySelector(
      '.attention-strip'
    ) as HTMLElement;
    const chips = Array.from(
      strip.querySelectorAll('a.attention-chip-link')
    ) as HTMLAnchorElement[];
    expect(chips.length).to.be.greaterThan(0);
    // A chip used to open the entity itself, which lost the context that
    // explained why it was on the list. It now lands on the row.
    for (const chip of chips) {
      expect(chip.getAttribute('href')).to.match(/^\/console\/attention#item-/);
    }
    const first = element['attentionItems'][0];
    expect(chips[0].getAttribute('href')).to.equal(
      `/console/attention#item-${encodeURIComponent(first.id)}`
    );
  });

  // Wave 8: a model priced at $0 is a question. It never takes a slot from
  // something that is actually wrong.
  it('keeps zero-priced models out of the strip while warnings are open', async () => {
    const element = await mountDashboard();
    await waitUntil(
      () =>
        !element['loading'] &&
        !element['fetchingApprovals'] &&
        !element['fetchingAudit'] &&
        !element['fetchingMCPAndTools'] &&
        element['attentionInputs'] != null,
      'dashboard did not finish loading'
    );
    element['attentionInputs'] = {
      approvals: [
        {
          id: 'approval-1',
          tool_name: 'refund_order',
          status: 'pending',
          requested_at: new Date(Date.now() - 60_000).toISOString(),
        },
      ],
      usageSummary: zeroPricedSummary(),
    } as any;
    await element.updateComplete;

    const strip = element.shadowRoot?.querySelector(
      '.attention-strip'
    ) as HTMLElement;
    expect(strip.classList.contains('low-only')).to.be.false;
    expect(strip.textContent).to.contain('need attention');
    expect(strip.textContent).to.not.contain('priced at $0');
    // The count is what the strip shows, not what the page derived.
    expect(strip.textContent!.replace(/\s+/g, ' ')).to.contain(
      '1 need attention'
    );
  });

  it('shows a zero-priced model on its own, without the amber tone', async () => {
    const element = await mountDashboard();
    await waitUntil(
      () =>
        !element['loading'] &&
        !element['fetchingApprovals'] &&
        !element['fetchingAudit'] &&
        !element['fetchingMCPAndTools'] &&
        element['attentionInputs'] != null,
      'dashboard did not finish loading'
    );
    element['attentionInputs'] = {
      usageSummary: zeroPricedSummary(),
    } as any;
    await element.updateComplete;

    const strip = element.shadowRoot?.querySelector(
      '.attention-strip'
    ) as HTMLElement;
    expect(strip, 'the attention strip').to.exist;
    expect(strip.classList.contains('low-only')).to.be.true;
    expect(strip.textContent).to.contain('worth a look');
    expect(strip.textContent).to.contain('1 model priced at $0');
    expect(strip.querySelector('sl-badge')?.getAttribute('variant')).to.equal(
      'neutral'
    );
  });

  it('hides the attention strip when nothing needs attention', async () => {
    const element = await mountDashboard();
    await waitUntil(
      () =>
        !element['loading'] &&
        !element['fetchingApprovals'] &&
        !element['fetchingAudit'] &&
        !element['fetchingMCPAndTools'] &&
        element['attentionInputs'] != null,
      'dashboard did not finish loading'
    );
    // A quiet account: the same shape the loader returns, with nothing in it.
    element['attentionInputs'] = {};
    await element.updateComplete;

    expect(
      element['attentionItems'].length,
      'nothing needs attention'
    ).to.equal(0);
    expect(element.shadowRoot?.querySelector('.attention-strip')).to.not.exist;
    // Wave 6: at zero the page is simply quiet. There is no counter left to
    // state the fact, and the strip is the only thing that ever did.
    expect(element.shadowRoot?.textContent).to.not.contain('need attention');
  });

  it('shows Updated beside the page title, and Manage keys in the gateway header', async () => {
    const element = await mountDashboard();
    await waitUntil(() => !element['loading'], 'dashboard did not load');
    await element.updateComplete;

    const updated = element.shadowRoot?.querySelector(
      'view-header .updated-at'
    );
    expect(updated, 'the page-level updated line renders').to.exist;
    expect(updated?.getAttribute('slot')).to.equal('meta');
    expect(updated?.textContent).to.contain('Updated');

    const manageKeys = element.shadowRoot?.querySelector(
      'a.header-action-link[href="/console/settings/api-keys"]'
    );
    expect(manageKeys?.textContent?.trim()).to.equal('Manage keys');
  });

  describe('wave 2 Overview', () => {
    async function mountLoaded(): Promise<DashboardView> {
      const element = await mountDashboard();
      await waitUntil(
        () =>
          !element['loading'] &&
          !element['fetchingAgents'] &&
          !element['fetchingBudget'] &&
          !element['fetchingAudit'] &&
          !element['fetchingMCPAndTools'],
        'dashboard did not finish loading'
      );
      await element.updateComplete;
      return element;
    }

    it('shows both endpoints without a disclosure', async () => {
      const element = await mountLoaded();

      // Wave 2 hid the URLs behind "Show endpoints". They are reference
      // material, but they are one line each: the disclosure cost more than
      // it saved.
      expect(element.shadowRoot?.querySelector('.connect-toggle')).to.not.exist;
      const rows = element.shadowRoot?.querySelectorAll('.plane-row') || [];
      expect(rows.length, 'one row per plane').to.equal(2);
      expect(rows[0].textContent).to.contain('Model gateway');
      expect(rows[0].textContent).to.contain('/openai/v1');
      expect(rows[1].textContent).to.contain('Tool firewall');
      expect(rows[1].textContent).to.contain('/mcp');
    });

    it('spends the endpoint line on the host and the path, not the scheme', async () => {
      const element = await mountLoaded();

      const endpoint = element.shadowRoot?.querySelector(
        '.plane-row .server-endpoint'
      ) as HTMLElement;
      // The card is a third of the window wide: seven characters of "https://"
      // cost the reader the hostname. The copy button and the tooltip still
      // carry the exact URL.
      expect(endpoint.textContent).to.not.contain('http');
      expect(endpoint.querySelector('.endpoint-tail')!.textContent).to.equal(
        '/openai/v1'
      );
      expect(endpoint.getAttribute('title')).to.contain('://');
      expect(endpoint.getAttribute('title')).to.contain('/openai/v1');
    });

    it('states what each plane did, and never shows a zero it cannot measure', async () => {
      gatewaySummaryResponse = {
        ...gatewaySummaryResponse,
        total_requests: 13932,
        failed_requests: 12,
      };
      const element = await mountLoaded();

      const rows = element.shadowRoot?.querySelectorAll('.plane-row') || [];
      const modelStats = rows[0].querySelector('.plane-stats')!.textContent!;
      // Compact above a thousand, like every other count in the console.
      expect(modelStats).to.contain('13.9K requests');
      expect(modelStats).to.contain('12 failed');
      // The rate-limit report is empty, so no "0 rate limited" is invented.
      expect(modelStats).to.not.contain('rate limited');
      expect(rows[0].querySelector('.plane-dot')!.classList.contains('served'))
        .to.be.true;
    });

    it('names the card Gateway and drops the question mark', async () => {
      const element = await mountLoaded();

      const header = element.shadowRoot?.querySelector(
        '.gateway-card .chart-header'
      ) as HTMLElement;
      expect(header.textContent!.trim()).to.equal('Gateway');
      expect(header.querySelector('sl-tooltip')).to.not.exist;
      const meta = element.shadowRoot?.querySelector('.gateway-header-meta');
      expect(meta?.textContent?.replace(/\s+/g, ' ')).to.contain(
        'active API key'
      );
    });

    it('switches the model endpoint with the format select', async () => {
      const element = await mountLoaded();

      const select = element.shadowRoot?.querySelector(
        '.plane-row .format-select'
      ) as HTMLSelectElement;
      select.value = '/anthropic/v1';
      select.dispatchEvent(new Event('change'));
      await element.updateComplete;

      const row = element.shadowRoot?.querySelector('.plane-row')!;
      expect(row.querySelector('.endpoint-tail')!.textContent).to.equal(
        '/anthropic/v1'
      );
    });

    it('replaces the plane rows with one onboarding line on a new instance', async () => {
      agentsResponse = { ...agentsResponse, total: 0, items: [] };
      const element = await mountLoaded();

      // Nothing has ever called the gateway, so two endpoint rows would be
      // reference material for a reader with nothing to point at them.
      expect(
        element.shadowRoot?.querySelectorAll('.plane-row').length
      ).to.equal(0);
      const line = element.shadowRoot?.querySelector(
        '.connect-first'
      ) as HTMLElement;
      expect(line, 'the connect line').to.exist;
      expect(line.textContent).to.contain('Connect your first agent');
      expect(line.querySelector('code')?.textContent).to.equal(
        'preloop agents onboard'
      );
      expect(line.querySelector('a[href="/console/agents"]')).to.exist;
      // The Next steps card is still there to carry the rest of the setup.
      expect(element.shadowRoot?.querySelector('.next-steps-card')).to.exist;
    });

    it('wraps a plane row onto two lines on a phone', async () => {
      const element = await mountLoaded();
      const row = element.shadowRoot?.querySelector(
        '.plane-row'
      ) as HTMLElement;
      // The test window is a phone width, where the row is name + stats on
      // the first line and the endpoint underneath.
      const name = row.querySelector('.plane-name') as HTMLElement;
      const stats = row.querySelector('.plane-stats') as HTMLElement;
      const endpoint = row.querySelector('.plane-endpoint') as HTMLElement;
      expect(window.innerWidth, 'phone width').to.be.at.most(800);
      expect(stats.getBoundingClientRect().top).to.be.closeTo(
        name.getBoundingClientRect().top,
        6
      );
      expect(endpoint.getBoundingClientRect().top).to.be.greaterThan(
        name.getBoundingClientRect().top
      );
    });

    // Wave 8: spend reaches a person through the agents they own, which is
    // the only link the gateway records between a request and a human.
    it("attributes an owned agent's tokens and spend to its owner", async () => {
      agentsResponse = {
        ...agentsResponse,
        items: agentsResponse.items.map((agent: any) => ({
          ...agent,
          owner_user_id: 'user-1',
          owner_username: 'ada',
        })),
      };
      const element = await mountLoaded();
      element['accountUsers'] = [
        {
          id: 'user-1',
          username: 'ada',
          full_name: 'Ada Lovelace',
          is_active: true,
          last_login: '2026-03-07T09:00:00Z',
          roles: [{ name: 'Admin' }],
        },
        {
          id: 'user-2',
          username: 'grace',
          full_name: null,
          is_active: true,
          last_login: null,
          roles: [],
        },
      ] as any;
      await element.updateComplete;

      const rows = element['inventoryUserRows'];
      expect(rows.map((row: any) => row.name)).to.eql([
        'Ada Lovelace',
        'grace',
      ]);
      expect(rows[0].role).to.equal('Admin');
      expect(rows[0].agentsOwned).to.equal(1);
      expect(rows[0].tokens).to.equal(15);
      expect(rows[0].cost).to.equal(4.2);
      // Nobody's agent, nobody's spend: a zero here is true, not missing.
      expect(rows[1].agentsOwned).to.equal(0);
      expect(rows[1].cost).to.equal(0);
    });

    it('shows the Users tab only where user management is on', async () => {
      const element = await mountLoaded();
      const inventory = element.shadowRoot!.querySelector('inventory-card')!;
      expect((inventory as any).showUsers).to.be.false;

      element['userManagementEnabled'] = true;
      await element.updateComplete;
      await (inventory as any).updateComplete;
      expect((inventory as any).showUsers).to.be.true;
    });

    // Wave 8: the tab is a list of people, and the API behind it answers only
    // to view_users. Without that permission the tab would open on an error,
    // so it is not offered at all.
    it('hides the Users tab from a reader without view_users', async () => {
      mePermissions = ['view_agents', 'view_flows'];
      const element = await mountLoaded();
      element['userManagementEnabled'] = true;
      await element.updateComplete;
      const inventory = element.shadowRoot!.querySelector('inventory-card')!;
      await (inventory as any).updateComplete;
      expect((inventory as any).showUsers).to.be.false;
      expect(
        [...inventory.shadowRoot!.querySelectorAll('sl-tab')].map((tab) =>
          tab.getAttribute('panel')
        )
      ).to.not.contain('users');
    });

    it('shows the Users tab to a reader with view_users', async () => {
      mePermissions = ['view_agents', 'view_users'];
      const element = await mountLoaded();
      element['userManagementEnabled'] = true;
      await element.updateComplete;
      const inventory = element.shadowRoot!.querySelector('inventory-card')!;
      await (inventory as any).updateComplete;
      expect((inventory as any).showUsers).to.be.true;
    });

    it('hides next steps when every step is already done', async () => {
      const element = await mountLoaded();

      // The fixture has an agent, budget policies and a tool under approval.
      expect(element['nextSteps'].every((step: any) => step.done)).to.be.true;
      expect(element.shadowRoot?.querySelector('.next-steps-card')).to.not
        .exist;
    });

    // Wave 8: the checklist reads four separate fetches. Before they land
    // every step looks undone, so a finished account used to watch the card
    // appear and vanish on every refresh.
    it('does not flash next steps while the page is still loading', async () => {
      localStorage.setItem('dashboard_next_steps_all_done', 'true');
      const element = await mountDashboard();

      // Mid-load: agents, policies and tools have not answered yet.
      expect(element['loading'] || element['fetchingTools']).to.be.true;
      expect(
        element.shadowRoot?.querySelector('.next-steps-card'),
        'no card during loading'
      ).to.not.exist;

      await waitUntil(
        () =>
          !element['loading'] &&
          !element['fetchingAgents'] &&
          !element['fetchingBudget'] &&
          !element['fetchingTools'],
        'dashboard did not finish loading'
      );
      await element.updateComplete;
      expect(element.shadowRoot?.querySelector('.next-steps-card')).to.not
        .exist;
    });

    it('hides next steps while inventory lists are in flight, even for an unfinished account', async () => {
      // Last visit said the checklist was still open. Empty arrays before
      // the lists answer used to paint "Onboard an agent" over an account
      // that already had one.
      localStorage.setItem('dashboard_next_steps_all_done', 'false');
      let releaseTools = () => {};
      toolsGate = new Promise<void>((resolve) => {
        releaseTools = resolve;
      });

      const element = await mountDashboard();
      await waitUntil(() => !element['loading'], 'fold never finished');
      await element.updateComplete;

      expect(element['fetchingTools'], 'tools list still in flight').to.be.true;
      expect(
        element.shadowRoot?.querySelector('.next-steps-card'),
        'no checklist while the tools list is unanswered'
      ).to.not.exist;

      releaseTools();
      await waitUntil(
        () => element['nextStepsInputsResolved'],
        'list phase never resolved'
      );
      await element.updateComplete;
      // Fixture has an agent, budget policies and a gated tool: done.
      expect(element.shadowRoot?.querySelector('.next-steps-card')).to.not
        .exist;
    });

    it('remembers that the checklist is finished', async () => {
      localStorage.removeItem('dashboard_next_steps_all_done');
      await mountLoaded();

      expect(localStorage.getItem('dashboard_next_steps_all_done')).to.equal(
        'true'
      );
    });

    it('lists the open steps for a new account, with the done ones ticked', async () => {
      budgetPoliciesResponse = [];
      toolsResponse = toolsResponse.map((tool: any) => ({
        ...tool,
        is_enabled: true,
        approval_workflow_id: null,
      }));

      const element = await mountLoaded();

      const card = element.shadowRoot?.querySelector('.next-steps-card');
      expect(card, 'the next steps card').to.exist;
      const steps = card?.querySelectorAll('.next-step') || [];
      expect(steps.length).to.equal(3);
      expect(card?.textContent).to.contain('Onboard an agent');
      expect(card?.textContent).to.contain('Set a spending limit');
      expect(card?.textContent).to.contain('Restrict a tool');
      // The agent exists, so that step is ticked and the other two are not.
      expect(steps[0].classList.contains('done'), 'agent step done').to.be.true;
      expect(steps[1].classList.contains('done'), 'budget step done').to.be
        .false;
      expect(
        (steps[1].querySelector('.next-step-link') as HTMLElement).tagName
      ).to.equal('BUTTON');
      expect(
        steps[2].querySelector('a.next-step-link')?.getAttribute('href')
      ).to.equal('/console/policies');
    });

    it('adds the invite step only when user management is on', async () => {
      budgetPoliciesResponse = [];
      const element = await mountLoaded();
      expect(element['nextSteps'].map((step: any) => step.id)).to.not.include(
        'invite'
      );

      element['userManagementEnabled'] = true;
      await element.updateComplete;
      const invite = element['nextSteps'].find(
        (step: any) => step.id === 'invite'
      );
      expect(invite?.href).to.equal('/console/settings/invitations');
    });

    it('dismisses next steps and remembers it', async () => {
      budgetPoliciesResponse = [];
      const element = await mountLoaded();

      const dismiss = element.shadowRoot?.querySelector(
        '.next-steps-card sl-icon-button[label="Dismiss next steps"]'
      ) as HTMLElement;
      expect(dismiss, 'the dismiss control').to.exist;
      dismiss.click();
      await element.updateComplete;

      expect(element.shadowRoot?.querySelector('.next-steps-card')).to.not
        .exist;
      expect(localStorage.getItem('dashboard_next_steps_dismissed')).to.equal(
        'true'
      );
    });

    it('asks the gateway for the previous window and hands it to the Usage card', async () => {
      const element = await mountLoaded();

      const summaryCalls = fetchStub
        .getCalls()
        .map((call: any) => String(call.args[0]))
        .filter((url: string) =>
          url.startsWith('/api/v1/account/gateway-usage/summary')
        );
      const priorCall = summaryCalls.find((url: string) =>
        url.includes('end_date=')
      );
      expect(priorCall, 'a bounded prior-window request').to.exist;

      const usageCard = element.shadowRoot?.querySelector('usage-card') as any;
      expect(usageCard.priorSummary, 'prior summary reaches the card').to.exist;
    });
  });
  describe('wave 6 Overview', () => {
    async function mountLoaded(): Promise<DashboardView> {
      const element = await mountDashboard();
      await waitUntil(
        () =>
          !element['loading'] &&
          !element['fetchingAgents'] &&
          !element['fetchingBudget'] &&
          !element['fetchingAudit'] &&
          !element['fetchingMCPAndTools'],
        'dashboard did not finish loading'
      );
      await element.updateComplete;
      return element;
    }

    it('no longer renders the stat strip or the three folded cards', async () => {
      const element = await mountLoaded();

      expect(element.shadowRoot?.querySelector('.hero-stats')).to.not.exist;
      expect(element.shadowRoot?.querySelector('.stat-strip')).to.not.exist;
      const text = element.shadowRoot?.textContent || '';
      expect(text).to.not.contain('Active agents');
      expect(text).to.not.contain('Recent Flow Executions');
      expect(text).to.not.contain('Top Models');
      // The Active agents card had the page's second time range; there is
      // one range on the Overview now, and it lives on the Usage card.
      expect(
        element.shadowRoot?.querySelectorAll('time-range-select').length
      ).to.equal(0);
    });

    it('puts the Inventory under the Gateway card and the feed under Usage', async () => {
      const element = await mountLoaded();

      const inventory = element.shadowRoot?.querySelector(
        'inventory-card'
      ) as HTMLElement;
      const feed = element.shadowRoot?.querySelector(
        'activity-feed'
      ) as HTMLElement;
      const gateway = element.shadowRoot?.querySelector(
        '.gateway-card'
      ) as HTMLElement;
      const usage = element.shadowRoot?.querySelector(
        'usage-card'
      ) as HTMLElement;

      expect(inventory.closest('.main-column'), 'Inventory column').to.exist;
      expect(feed.closest('.side-column'), 'Activity column').to.exist;
      expect(gateway.getBoundingClientRect().top).to.be.lessThan(
        inventory.getBoundingClientRect().top
      );
      expect(usage.getBoundingClientRect().top).to.be.lessThan(
        feed.getBoundingClientRect().top
      );
    });

    it('labels the tabs with the counts the strip used to carry', async () => {
      const element = await mountLoaded();

      const inventory = element.shadowRoot?.querySelector(
        'inventory-card'
      ) as any;
      expect(inventory.agentsTotal).to.equal(1);
      expect(inventory.flowsTotal).to.equal(1);
      expect(inventory.modelsTotal).to.equal(1);
      expect(inventory.toolsTotal).to.equal(2);
      expect(inventory.rangeLabel).to.equal('30d');

      const tabs = [
        ...(inventory.shadowRoot?.querySelectorAll('sl-tab') || []),
      ].map((tab: Element) =>
        (tab.textContent || '').replace(/\s+/g, ' ').trim()
      );
      expect(tabs).to.eql(['Agents 1', 'Flows 1', 'Models 1', 'Tools 2']);
    });

    it('reads each tab from a fetch the page already made', async () => {
      const element = await mountLoaded();

      const agents = element['inventoryAgentRows'];
      expect(agents[0].name).to.equal('Ops Agent');
      // Range numbers, from the gateway breakdown, not the agent's lifetime.
      expect(agents[0].requests).to.equal(8);
      expect(agents[0].cost).to.equal(4.2);
      expect(agents[0].modelAlias).to.equal('gpt-5.4');

      const flows = element['inventoryFlowRows'];
      expect(flows[0].name).to.equal('Refund Assistant');
      expect(flows[0].runs).to.equal(1);
      expect(flows[0].failed).to.equal(1);
      expect(flows[0].lastRun.id).to.equal('execution-1');

      const models = element['inventoryModelRows'];
      expect(models[0].alias).to.equal('OpenAI GPT-5.4');
      expect(models[0].provider).to.equal('openai');
      // Numbers come from the one batch overview call, joined on the model
      // id, so a model whose gateway alias differs from its name is no
      // longer reported as idle beside a phantom row for the alias.
      expect(models[0].requests).to.equal(10);
      expect(models[0].tokens).to.equal(1000);
      expect(models[0].cost).to.equal(5.5);
      expect(models.length).to.equal(1);

      const tools = element['inventoryToolRows'].map(
        (row: { name: string; server: string }) => `${row.name} (${row.server})`
      );
      expect(tools).to.eql([
        'verify_refund_eligibility (builtin)',
        'refund_order (Example MCP Server)',
      ]);
    });

    it('counts flow runs in the range the $ est. column already covers', async () => {
      // Two runs of the same flow: one this morning, one last quarter. The
      // spend column is range-scoped, so the run counts beside it must be.
      flowExecutionsResponse = [
        {
          id: 'execution-1',
          flow_id: 'flow-1',
          flow_name: 'Refund Assistant',
          status: 'FAILED',
          start_time: new Date(Date.now() - 3600 * 1000).toISOString(),
          end_time: null,
          error_message: 'Provider timeout',
        },
        {
          id: 'execution-old',
          flow_id: 'flow-1',
          flow_name: 'Refund Assistant',
          status: 'FAILED',
          start_time: new Date(Date.now() - 120 * 86400000).toISOString(),
          end_time: null,
          error_message: 'Ancient history',
        },
      ];
      const element = await mountLoaded();

      const flows = element['inventoryFlowRows'];
      expect(flows[0].runs).to.equal(1);
      expect(flows[0].failed).to.equal(1);
      expect(flows[0].lastRun.id).to.equal('execution-1');
      expect(element['flowRunsCapped']).to.be.false;
      const inventory = element.shadowRoot?.querySelector(
        'inventory-card'
      ) as any;
      expect(inventory.flowRunsCapped).to.be.false;
    });

    it('leaves a flow whose runs all predate the range at zero', async () => {
      flowExecutionsResponse = [
        {
          id: 'execution-old',
          flow_id: 'flow-1',
          flow_name: 'Refund Assistant',
          status: 'SUCCEEDED',
          start_time: new Date(Date.now() - 120 * 86400000).toISOString(),
          end_time: null,
          error_message: null,
        },
      ];
      const element = await mountLoaded();

      const flows = element['inventoryFlowRows'];
      expect(flows.length).to.equal(1);
      expect(flows[0].runs).to.equal(0);
      expect(flows[0].lastRun).to.equal(null);
    });

    it('flags the 100-run cap when the page never reached the range floor', async () => {
      flowExecutionsResponse = Array.from({ length: 100 }, (_, index) => ({
        id: `execution-${index}`,
        flow_id: 'flow-1',
        flow_name: 'Refund Assistant',
        status: index === 0 ? 'FAILED' : 'SUCCEEDED',
        start_time: new Date(Date.now() - (index + 1) * 60000).toISOString(),
        end_time: null,
        error_message: null,
      }));
      const element = await mountLoaded();

      expect(element['flowRunsCapped']).to.be.true;
      const inventory = element.shadowRoot?.querySelector(
        'inventory-card'
      ) as any;
      expect(inventory.flowRunsCapped).to.be.true;
    });

    it('lists the teammates without waiting for the tools request', async () => {
      // Users used to share one Promise.all with MCP servers, tools, models
      // and API keys, so a slow models call held back names that had already
      // arrived. Hold the models and the Users tab must still fill.
      let releaseModels = () => {};
      aiModelsGate = new Promise<void>((resolve) => {
        releaseModels = resolve;
      });

      const element = await mountDashboard();
      element['userManagementEnabled'] = true;
      await waitUntil(
        () => element['accountUsers'].length > 0,
        'users did not arrive on their own'
      );

      expect(element['fetchingUsers'], 'users request finished').to.be.false;
      expect(element['fetchingModels'], 'models still in flight').to.be.true;
      expect(element['inventoryUserRows'][0].name).to.exist;

      releaseModels();
      await waitUntil(
        () => !element['fetchingModels'],
        'models list did not finish'
      );
    });

    it('skeletons the usage columns until the breakdown lands', async () => {
      let releaseBreakdown = () => {};
      breakdownGate = new Promise<void>((resolve) => {
        releaseBreakdown = resolve;
      });
      // No breakdown in hand from an earlier pass: the columns have nothing
      // true to show, which is what the skeleton says.
      gatewaySummaryResponse.usage_by_session = [];
      gatewaySummaryResponse.usage_by_model = [];

      const element = await mountDashboard();
      await waitUntil(() => !element['loading'], 'first pass did not finish');
      await element.updateComplete;

      const inventory = element.shadowRoot?.querySelector(
        'inventory-card'
      ) as any;
      expect(element['fetchingUsageBreakdown']).to.be.true;
      expect(inventory.usageLoading, 'card waiting on usage').to.be.true;

      releaseBreakdown();
      await waitUntil(
        () => !element['fetchingUsageBreakdown'],
        'breakdown did not land'
      );
      await element.updateComplete;
      await inventory.updateComplete;
      expect(inventory.usageLoading).to.be.false;
    });

    it('renders inventory rows before usage arrives and fills cells without re-skeletoning rows', async () => {
      let releaseBreakdown = () => {};
      breakdownGate = new Promise<void>((resolve) => {
        releaseBreakdown = resolve;
      });
      let releaseOverview = () => {};
      overviewGate = new Promise<void>((resolve) => {
        releaseOverview = resolve;
      });
      let releaseExecutions = () => {};
      executionsGate = new Promise<void>((resolve) => {
        releaseExecutions = resolve;
      });
      gatewaySummaryResponse.usage_by_session = [];
      gatewaySummaryResponse.usage_by_model = [];
      gatewaySummaryResponse.usage_by_tool = [];
      gatewaySummaryResponse.usage_by_flow = [];

      const element = await mountDashboard();
      await waitUntil(() => !element['loading'], 'fold never finished');
      await waitUntil(
        () =>
          !element['fetchingAgents'] &&
          !element['fetchingFlows'] &&
          !element['fetchingModels'] &&
          !element['fetchingTools'],
        'identity lists never landed'
      );
      await element.updateComplete;

      const inventory = element.shadowRoot?.querySelector(
        'inventory-card'
      ) as any;
      await inventory.updateComplete;

      expect(element['inventoryAgentRows'][0].name).to.equal('Ops Agent');
      expect(element['inventoryFlowRows'][0].name).to.equal('Refund Assistant');
      expect(element['inventoryModelRows'][0].alias).to.equal('OpenAI GPT-5.4');
      expect(
        element['inventoryToolRows'].map((row: any) => row.name)
      ).to.include('verify_refund_eligibility');

      expect(inventory.usageLoading, 'agents usage still pending').to.be.true;
      expect(inventory.usageLoadingFlows, 'flows usage still pending').to.be
        .true;
      expect(inventory.usageLoadingModels, 'models usage still pending').to.be
        .true;

      const agentRow = inventory.shadowRoot?.querySelector('tbody tr');
      expect(agentRow?.textContent).to.contain('Ops Agent');
      expect(agentRow?.querySelector('.skeleton-row')).to.not.exist;
      expect(
        agentRow?.querySelectorAll('sl-skeleton').length
      ).to.be.greaterThan(0);

      releaseExecutions();
      releaseOverview();
      releaseBreakdown();
      await waitUntil(
        () =>
          !element['fetchingUsageBreakdown'] &&
          !element['fetchingModelUsage'] &&
          !element['fetchingFlowUsage'],
        'usage never landed'
      );
      await element.updateComplete;
      await inventory.updateComplete;

      expect(inventory.usageLoading).to.be.false;
      expect(inventory.usageLoadingFlows).to.be.false;
      expect(inventory.usageLoadingModels).to.be.false;
      const filled = inventory.shadowRoot?.querySelector('tbody tr');
      expect(filled?.textContent).to.contain('Ops Agent');
      expect(filled?.querySelector('sl-skeleton')).to.not.exist;
      expect(
        inventory.shadowRoot?.querySelectorAll('.skeleton-row').length
      ).to.equal(0);
    });

    it('shows next steps only after a resolved empty inventory', async () => {
      agentsResponse = { ...agentsResponse, total: 0, items: [] };
      budgetPoliciesResponse = [];
      toolsResponse = toolsResponse.map((tool: any) => ({
        ...tool,
        is_enabled: true,
        approval_workflow_id: null,
      }));
      let releaseTools = () => {};
      toolsGate = new Promise<void>((resolve) => {
        releaseTools = resolve;
      });

      const element = await mountDashboard();
      await waitUntil(() => !element['loading'], 'fold never finished');
      await element.updateComplete;
      expect(
        element.shadowRoot?.querySelector('.next-steps-card'),
        'hidden while the tools list is unanswered'
      ).to.not.exist;

      releaseTools();
      await waitUntil(
        () => element['nextStepsInputsResolved'],
        'list phase never resolved'
      );
      await element.updateComplete;
      const card = element.shadowRoot?.querySelector('.next-steps-card');
      expect(card, 'checklist after a resolved empty inventory').to.exist;
      expect(card?.textContent).to.contain('Onboard an agent');
    });

    it('keeps the first-paint request count at or below today', async () => {
      const element = await mountDashboard();
      await waitUntil(
        () =>
          element['attentionInputs'] != null &&
          !element['fetchingMCPAndTools'] &&
          !element['fetchingTools'] &&
          !element['fetchingModels'] &&
          !element['fetchingFlows'],
        'cold load never finished'
      );

      // Same filter the report quotes next to the 30 from code reading.
      // Child widgets own: activity-feed /audit-logs and /users (the
      // feed also falls back after HOST_USERS_WAIT_MS), invite-dialog
      // /teams /roles. The 30 includes three page audit-logs this
      // filter drops, so the measured set is 27. /ai-models is the
      // Inventory list once; preloop-deploy-wizard does not mount on
      // an onboarded account.
      const pageUrls = fetchStub
        .getCalls()
        .map((call) => String(call.args[0]))
        .filter(
          (url) =>
            !url.startsWith('/api/v1/audit-logs') &&
            !url.startsWith('/api/v1/teams') &&
            !url.startsWith('/api/v1/roles') &&
            !url.startsWith('/api/v1/users')
        );
      expect(pageUrls.length, pageUrls.join('\n')).to.equal(27);
      expect(pageUrls.filter((url) => url === '/api/v1/flows').length).to.equal(
        1
      );
      expect(pageUrls.filter((url) => url === '/api/v1/tools').length).to.equal(
        1
      );
      expect(
        pageUrls.filter((url) => url === '/api/v1/ai-models').length
      ).to.equal(1);
      expect(
        pageUrls.filter((url) => url.startsWith('/api/v1/ai-models/overview'))
          .length
      ).to.equal(1);
      expect(
        pageUrls.filter((url) => url.startsWith('/api/v1/agents')).length
      ).to.equal(1);
      expect(
        element.shadowRoot?.querySelector('preloop-deploy-wizard'),
        'wizard stays unmounted on an onboarded account'
      ).to.not.exist;
    });

    it('does not clear inventory list flags when the fold fails', async () => {
      let releaseModels = () => {};
      aiModelsGate = new Promise<void>((resolve) => {
        releaseModels = resolve;
      });
      const proto = customElements.get('dashboard-view')!
        .prototype as unknown as Record<string, unknown>;
      const apply = sinon
        .stub(proto, 'applyAgentsList')
        .throws(new Error('fold boom'));
      try {
        const element = await mountDashboard();
        await waitUntil(() => element['error'] != null, 'fold never failed');
        expect(element['fetchingModels'], 'models list still in flight').to.be
          .true;
      } finally {
        apply.restore();
        releaseModels();
      }
    });

    it('dims the usage numbers over a range change rather than blanking them', async () => {
      const element = await mountDashboard();
      await waitUntil(() => !element['loading'], 'first pass did not finish');
      await element.updateComplete;

      const usage = element.shadowRoot?.querySelector('usage-card') as any;
      const shown = usage.summary;
      expect(shown, 'a summary on screen before the change').to.exist;

      let releaseSummary = () => {};
      summaryGate = new Promise<void>((resolve) => {
        releaseSummary = resolve;
      });
      usage.dispatchEvent(
        new CustomEvent('range-change', {
          detail: { value: 'year' },
          bubbles: true,
          composed: true,
        })
      );
      await element.updateComplete;
      await usage.updateComplete;

      expect(usage.updating, 'card told it is updating').to.be.true;
      expect(usage.summary, 'last range still on screen').to.equal(shown);
      expect(usage.shadowRoot.querySelector('.primary-value')).to.exist;
      expect(usage.shadowRoot.querySelector('sl-skeleton')).to.not.exist;

      summaryGate = null;
      releaseSummary();
      await waitUntil(
        () => !element['updatingUsage'],
        'the range change never settled'
      );
      await element.updateComplete;
      await usage.updateComplete;
      expect(usage.updating).to.be.false;
    });

    it('reloads what the range changes and nothing else', async () => {
      const element = await mountDashboard();
      await waitUntil(
        () => element['attentionInputs'] != null,
        'first pass did not finish'
      );

      fetchStub.resetHistory();
      const usage = element.shadowRoot?.querySelector('usage-card') as any;
      usage.dispatchEvent(
        new CustomEvent('range-change', {
          detail: { value: 'year' },
          bubbles: true,
          composed: true,
        })
      );
      await waitUntil(
        () => !element['updatingUsage'] && !element['fetchingUsageBreakdown'],
        'the range change never settled'
      );
      await new Promise((resolve) => setTimeout(resolve, 50));

      const urls = fetchStub.getCalls().map((call) => String(call.args[0]));
      // The flows, the people and the tool catalogue read the same at any
      // range, so a range change does not ask for them again.
      expect(urls.some((url) => url === '/api/v1/flows')).to.be.false;
      expect(urls.some((url) => url === '/api/v1/tools')).to.be.false;
      expect(urls.some((url) => url.startsWith('/api/v1/users'))).to.be.false;
      // The gateway call log is scoped to the range, so it does.
      expect(
        urls.some((url) =>
          url.startsWith('/api/v1/account/gateway-usage/search')
        )
      ).to.be.true;
      expect(urls.some((url) => url.includes('include_breakdown=true'))).to.be
        .true;
    });

    it('keeps the Models tab on a skeleton until the second pass lands', async () => {
      // The models list is its own request. Hold it open after the fold
      // has cleared and the tab must still be waiting, not telling a
      // stocked account it has no models.
      let releaseModels = () => {};
      aiModelsGate = new Promise<void>((resolve) => {
        releaseModels = resolve;
      });
      // Nothing in the gateway breakdown either, so the tab has only the
      // models list to draw from.
      gatewaySummaryResponse.usage_by_model = [];

      const element = await mountDashboard();
      await waitUntil(() => !element['loading'], 'first pass did not finish');
      await element.updateComplete;

      const inventory = element.shadowRoot?.querySelector(
        'inventory-card'
      ) as any;
      expect(inventory.loadingModels, 'models tab still loading').to.be.true;
      expect(element['inventoryModelRows'].length).to.equal(0);
      inventory.shadowRoot
        ?.querySelector('sl-tab[panel="models"]')
        ?.dispatchEvent(
          new CustomEvent('sl-tab-show', {
            detail: { name: 'models' },
            bubbles: true,
            composed: true,
          })
        );
      await inventory.updateComplete;
      const midFlight = (inventory.shadowRoot?.textContent || '').replace(
        /\s+/g,
        ' '
      );
      expect(midFlight).to.not.contain('No models yet');
      expect(
        inventory.shadowRoot?.querySelectorAll('sl-skeleton').length
      ).to.be.greaterThan(0);

      releaseModels();
      await waitUntil(
        () => !element['fetchingModels'],
        'models list did not finish'
      );
      await element.updateComplete;
      await inventory.updateComplete;
      expect(inventory.loadingModels).to.be.false;
      expect(element['inventoryModelRows'][0].alias).to.equal('OpenAI GPT-5.4');
    });

    it('restores the models from the cache so a reload paints rows', async () => {
      // The cache is keyed on the token subject, so this test needs a token
      // shaped like one.
      const payload = btoa(JSON.stringify({ sub: 'cache-tester' }));
      localStorage.setItem('accessToken', `header.${payload}.signature`);
      const key = 'preloop:dashboard:cache-tester';
      sessionStorage.removeItem(key);

      try {
        const first = await mountLoaded();
        expect(first['aiModels'].length).to.equal(1);

        // Writes are coalesced onto requestIdleCallback (2s timeout), so a
        // busy CI runner can finish the second pass before the cache lands.
        await waitUntil(
          () => JSON.parse(sessionStorage.getItem(key) || '{}').tools != null,
          'dashboard cache did not persist tools',
          { timeout: 4000 }
        );
        const cached = JSON.parse(sessionStorage.getItem(key) || '{}');
        expect(cached.tools, 'tools were already cached').to.exist;
        expect(cached.aiModels?.[0]?.name).to.equal('OpenAI GPT-5.4');

        // A reload restores them before the slow pass resolves, exactly the
        // way `tools` already was.
        const reloaded = await mountDashboard();
        expect(reloaded['aiModels'][0].name).to.equal('OpenAI GPT-5.4');
        expect(reloaded['inventoryModelRows'][0].alias).to.equal(
          'OpenAI GPT-5.4'
        );
        await waitUntil(
          () => !reloaded['fetchingMCPAndTools'],
          'second pass did not finish'
        );
      } finally {
        sessionStorage.removeItem(key);
      }
    });

    it('hands the feed the context it needs to name things', async () => {
      const element = await mountLoaded();

      const feed = element.shadowRoot?.querySelector('activity-feed') as any;
      expect(feed.flows[0].name).to.equal('Refund Assistant');
      expect(feed.executions[0].id).to.equal('execution-1');
      expect(feed.agents[0].display_name).to.equal('Ops Agent');
      expect(feed.budgetPolicies.length).to.equal(3);
    });

    it('makes the side column a sticky rail on a wide viewport', async () => {
      const element = await mountLoaded();
      const side = element.shadowRoot?.querySelector(
        '.side-column'
      ) as HTMLElement;
      const feed = element.shadowRoot?.querySelector(
        'activity-feed'
      ) as HTMLElement;

      // The rule is a media query, so it only holds on a wide test window.
      if (window.innerWidth < 1200) return;
      const column = getComputedStyle(side);
      expect(column.position).to.equal('sticky');
      // Exactly as tall as the viewport minus the shell header and the gap,
      // never taller: the column is the page's floor for its cards.
      expect(parseFloat(column.maxHeight)).to.be.at.most(window.innerHeight);
      expect(parseFloat(column.maxHeight)).to.be.greaterThan(0);
      const card = getComputedStyle(feed);
      expect(card.flexGrow).to.equal('1');
      expect(parseFloat(card.minHeight)).to.equal(240);
      expect(side.getBoundingClientRect().height).to.be.at.most(
        window.innerHeight
      );
    });

    it('ages the Updated label instead of freezing it at "just now"', async () => {
      const element = await mountLoaded();
      const label = () =>
        (
          element.shadowRoot?.querySelector('view-header .updated-at')
            ?.textContent || ''
        )
          .replace(/\s+/g, ' ')
          .trim();
      expect(label()).to.equal('Updated just now');

      // The page has been open for four minutes and nothing has changed.
      element['lastUpdatedAt'] = new Date(Date.now() - 4 * 60000).toISOString();
      await element.updateComplete;
      const stamp = element.shadowRoot?.querySelector('relative-time-label');
      await (stamp as unknown as { updateComplete: Promise<unknown> })
        .updateComplete;
      expect(label()).to.equal('Updated 4m ago');
      expect(
        element.shadowRoot
          ?.querySelector('view-header .updated-at')
          ?.getAttribute('title')
      ).to.not.equal('Not loaded yet');
    });

    it('takes its turn when an event lands during a refresh', async () => {
      const element = await mountLoaded();
      // A socket event arriving mid-refresh used to be dropped, leaving the
      // header claiming numbers older than the event that announced them.
      element['lastFetchStartedAt'] = Date.now() - 60000;
      element['lastUpdatedAt'] = new Date(Date.now() - 60000).toISOString();
      let ran = 0;
      element['scheduleTopicRefresh']('gateway', async () => {
        ran += 1;
      });
      expect(element['refreshTimers']['gateway']).to.not.equal(undefined);
      await waitUntil(() => ran === 1, 'queued refresh never ran');
    });

    it('loads the fold without asking for the same thing twice', async () => {
      // Hold the deferred pass so what is counted is exactly what the reader
      // waits for before the page is usable.
      const proto = customElements.get('dashboard-view')!
        .prototype as unknown as Record<string, unknown>;
      const deferred = sinon.stub(proto, 'fetchDeferredData').resolves();
      let element: DashboardView;
      try {
        element = await mountDashboard();
        await waitUntil(() => !element['loading'], 'fold never finished');
        await waitUntil(() => deferred.called, 'deferred pass never started');
      } finally {
        deferred.restore();
      }

      const foldUrls = fetchStub
        .getCalls()
        .map((call) => String(call.args[0]))
        // The feed and the dialogs in the template are separate elements that
        // fetch their own reference data on connect; this counts the page.
        .filter(
          (url) =>
            !url.startsWith('/api/v1/audit-logs') &&
            !url.startsWith('/api/v1/teams') &&
            !url.startsWith('/api/v1/roles') &&
            !url.startsWith('/api/v1/ai-models') &&
            // Identity lists start with the fold but do not block it.
            url !== '/api/v1/flows' &&
            url !== '/api/v1/tools'
        );
      const duplicates = foldUrls.filter(
        (url, index) => foldUrls.indexOf(url) !== index
      );
      expect(duplicates, 'a URL was fetched twice before the fold').to.eql([]);
      // Eight above the fold plus the two assignment reads behind the
      // approvals card. The wave before this one took twenty three.
      expect(foldUrls.length, foldUrls.join('\n')).to.be.at.most(12);
    });

    it('answers a gateway event with four requests, not the page', async () => {
      const element = await mountLoaded();
      await waitUntil(
        () => element['attentionInputs'] != null,
        'deferred pass never finished'
      );
      const handler = subscribeStub
        .getCalls()
        .find(
          (call) =>
            call.args[0] === 'gateway_activity' &&
            call.thisValue === unifiedWebSocketManager
        )?.args[1] as (message: unknown) => void;
      expect(handler, 'nothing listens to gateway_activity').to.be.a(
        'function'
      );

      // The load's own five second grace period is what the gate reads.
      element['lastFetchStartedAt'] = Date.now() - 30000;
      fetchStub.resetHistory();
      handler({ type: 'gateway_activity' });
      // A busy agent publishes one of these per model call.
      handler({ type: 'gateway_activity' });
      handler({ type: 'gateway_activity' });
      await waitUntil(
        () => fetchStub.callCount > 0,
        'the event was never served'
      );
      await new Promise((resolve) => setTimeout(resolve, 300));

      const urls = fetchStub
        .getCalls()
        .map((call) => String(call.args[0]))
        .filter((url) => !url.startsWith('/api/v1/users'));
      expect(urls.length, urls.join('\n')).to.be.at.most(4);
      expect(urls.some((url) => url.startsWith('/api/v1/flows'))).to.be.false;
      expect(urls.some((url) => url.includes('include_breakdown=true'))).to.be
        .false;
    });

    it('derives the attention list once per set of inputs', async () => {
      const element = await mountLoaded();
      await waitUntil(
        () => element['attentionInputs'] != null,
        'deferred pass never finished'
      );

      const first = element['attentionItems'];
      // Something unrelated changes and the page renders again.
      element['showBudgetDialog'] = true;
      await element.updateComplete;
      expect(element['attentionItems']).to.equal(first);

      element['attentionInputs'] = {
        ...element['attentionInputs']!,
      };
      await element.updateComplete;
      expect(element['attentionItems']).to.not.equal(first);
    });

    it('opens the budget dialog when the feed asks for it', async () => {
      const element = await mountLoaded();

      const feed = element.shadowRoot?.querySelector('activity-feed')!;
      feed.dispatchEvent(
        new CustomEvent('open-budget-limits', { bubbles: true, composed: true })
      );
      await element.updateComplete;
      expect(element['showBudgetDialog']).to.be.true;
    });
  });
});
