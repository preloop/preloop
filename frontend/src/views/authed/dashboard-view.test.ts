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
  let usersResponse: any;
  let budgetPoliciesResponse: any[];

  beforeEach(() => {
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
    flowExecutionsResponse = [
      {
        id: 'execution-1',
        flow_id: 'flow-1',
        flow_name: 'Refund Assistant',
        status: 'FAILED',
        start_time: '2026-03-07T10:00:00Z',
        end_time: '2026-03-07T10:03:00Z',
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
            permissions: null,
          });
        }

        if (url === '/api/v1/issue-count') {
          return json(issueCountResponse);
        }

        if (url.startsWith('/api/v1/mcp-servers')) {
          return json(mcpServersResponse);
        }

        if (url.startsWith('/api/v1/tools')) {
          return json(toolsResponse);
        }

        if (url.startsWith('/api/v1/flows/executions')) {
          return json(flowExecutionsResponse);
        }

        if (url.startsWith('/api/v1/flows')) {
          return json(flowsResponse);
        }

        if (url.startsWith('/api/v1/approval-requests')) {
          return json(
            url.includes('status=pending')
              ? pendingApprovalRequestsResponse
              : allApprovalRequestsResponse
          );
        }

        if (url === '/api/v1/ai-models') {
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
        !element['fetchingMCPAndTools'],
      'dashboard did not finish loading'
    );
    await element.updateComplete;

    const header = element.shadowRoot?.querySelector('view-header');
    expect(header?.getAttribute('headerText')).to.equal('Overview');
    expect(element.shadowRoot?.textContent).to.contain(
      'Recent Flow Executions'
    );
    expect(element.shadowRoot?.textContent).to.contain('Audit exceptions');
    expect(element.shadowRoot?.textContent).to.contain('Needs attention');
  });

  it('subscribes to realtime topics and fetches dashboard data', async () => {
    const element = await mountDashboard();
    await waitUntil(
      () =>
        !element['loading'] &&
        !element['fetchingActiveAgents'] &&
        !element['fetchingBudget'] &&
        !element['fetchingAudit'] &&
        !element['fetchingMCPAndTools'],
      'dashboard did not finish loading'
    );

    expect(connectStub).to.have.been.calledOnce;
    expect(subscribeStub.callCount).to.equal(8);

    const urls = fetchStub.getCalls().map((call) => String(call.args[0]));
    expect(
      urls.some((url) =>
        url.startsWith('/api/v1/account/gateway-usage/summary')
      )
    ).to.be.true;
    // Two for the cards (summary + breakdown upgrade) plus the fixed 30d one
    // the shared attention loader uses, and one agents call per source: the
    // cards' own list and the attention loader's, which must stay on the
    // parameters the Attention page uses.
    expect(
      urls.filter((url) =>
        url.startsWith('/api/v1/account/gateway-usage/summary')
      ).length
    ).to.be.at.most(3);
    expect(urls.some((url) => url.includes('include_breakdown=false'))).to.be
      .true;
    expect(
      urls.filter((url) => url.startsWith('/api/v1/agents')).length
    ).to.equal(2);
    expect(urls.some((url) => url === '/api/v1/auth/api-usage')).to.be.false;
    expect(urls.some((url) => url.startsWith('/api/v1/audit-logs/grouped'))).to
      .be.true;
    expect(urls).to.include('/api/v1/trackers');
    expect(urls).to.include('/api/v1/mcp-servers');
    expect(urls).to.include('/api/v1/tools');
    expect(urls).to.include('/api/v1/flows');
    expect(urls).to.include('/api/v1/flows/executions?limit=10');
    expect(urls.some((url) => url.startsWith('/api/v1/approval-requests'))).to
      .be.true;

    const updatedAt = element.shadowRoot?.querySelector('.updated-at');
    expect(updatedAt?.textContent || '').to.match(/Updated (just now|\d)/);
    expect(updatedAt?.textContent || '').to.not.contain('Never');
    expect(updatedAt?.textContent || '').to.not.contain('Loading');

    const failedRow = element.shadowRoot?.querySelector(
      '.item-card.failed-execution'
    );
    expect(failedRow).to.exist;
    expect(element.shadowRoot?.querySelector('.item-card.danger')).to.not.exist;
  });

  describe('Recent Flow Executions failed row (#174)', () => {
    // A real failing run: a git clone error long enough to overflow the row.
    // The URL has no break opportunity, which is what actually broke the
    // layout: it set the min-content width of the flex column.
    const LONG_ERROR =
      'Git clone failed! Could not clone repository ' +
      'https://github.com/example_org/mender_mcu_firmware_integration_service_repository_mirror.git ' +
      'into /workspace/src: authentication failed';

    async function mountWithFailedExecution(width: string) {
      flowExecutionsResponse = [
        {
          id: 'execution-failed',
          flow_id: 'flow-1',
          flow_name: 'Security Vulnerability Scanner',
          status: 'FAILED',
          start_time: '2026-08-04T14:37:00Z',
          end_time: '2026-08-04T14:38:00Z',
          error_message: LONG_ERROR,
        },
      ];

      const element = await mountDashboard();
      // Constrain to the main column width the card gets beside the sidebar.
      element.style.display = 'block';
      element.style.width = width;
      await waitUntil(
        () => !element['loading'],
        'dashboard did not finish loading'
      );
      await element.updateComplete;
      return element;
    }

    it('does not let the error text overflow the row horizontally', async () => {
      const element = await mountWithFailedExecution('520px');
      const row = element.shadowRoot?.querySelector(
        '.item-card.failed-execution'
      ) as HTMLElement;
      const error = row.querySelector('.item-error') as HTMLElement;

      expect(error).to.exist;
      // scrollWidth exceeding clientWidth means content spills out of the box.
      expect(
        error.scrollWidth,
        'error text is wider than its container'
      ).to.be.at.most(error.clientWidth + 1);
    });

    it('keeps the row after a reload, with no dismiss control', async () => {
      const element = await mountWithFailedExecution('520px');

      expect(element.shadowRoot?.querySelector('sl-icon-button.item-dismiss'))
        .to.not.exist;
      expect(
        element.shadowRoot?.querySelector('.item-card.failed-execution'),
        'failed execution row'
      ).to.exist;
      expect(localStorage.getItem('dashboard_dismissed_executions')).to.be.null;
    });
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
        !element['fetchingActiveAgents'] &&
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
        !element['fetchingActiveAgents'] &&
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

  it('renders exactly the five hero metrics with no expand toggle', async () => {
    const element = await mountDashboard();
    await waitUntil(
      () =>
        !element['loading'] &&
        !element['fetchingActiveAgents'] &&
        !element['fetchingBudget'] &&
        !element['fetchingAudit'] &&
        !element['fetchingMCPAndTools'],
      'dashboard did not finish loading'
    );
    await element.updateComplete;

    const metricsGrid = element.shadowRoot?.querySelector('.metrics-grid');
    expect(metricsGrid).to.exist;
    const metricText = metricsGrid?.textContent || '';
    ['agents', 'flows', 'models', 'tools', 'need attention'].forEach((label) =>
      expect(metricText).to.contain(label)
    );

    const metricLinks = metricsGrid?.querySelectorAll('a.tool-count') || [];
    expect(metricLinks.length).to.equal(5);
    expect(metricLinks[4].getAttribute('href')).to.equal('/console/attention');

    [
      'approved requests',
      'inactive agents',
      'flow executions',
      'model requests',
      'tool calls',
      'declined requests',
      'total runtime sessions',
      'failed executions',
      'failed requests',
      'failed tool calls',
      'timed out approval requests',
      'total tokens',
      'flow execution success rate',
      'model request success rate',
      'tool call success rate',
      'approval rate',
    ].forEach((label) => expect(metricText).to.not.contain(label));

    const toggle = Array.from(
      element.shadowRoot?.querySelectorAll('sl-button') || []
    ).find((button) => button.textContent?.includes('Show more metrics'));
    expect(toggle).to.not.exist;
    expect(localStorage.getItem('preloop_dashboard_metrics_expanded')).to.be
      .null;
  });

  it('surfaces pending approvals in the attention card', async () => {
    const element = await mountDashboard();
    await waitUntil(
      () =>
        !element['loading'] &&
        !element['fetchingApprovals'] &&
        !element['fetchingAudit'] &&
        !element['fetchingMCPAndTools'],
      'dashboard did not finish loading'
    );
    await element.updateComplete;

    const attentionLink = element.shadowRoot?.querySelector(
      'a.header-action-link[href="/console/attention"]'
    );
    expect(attentionLink, 'View all link').to.exist;

    const rows = element.shadowRoot?.querySelectorAll('.attention-row') || [];
    expect(rows.length).to.be.greaterThan(0);
    expect(element.shadowRoot?.textContent || '').to.not.contain(
      'Pending approvals'
    );
  });

  it('skips zero-request runtime sessions and displays ids instead of config paths', async () => {
    runtimeSessionsResponse = {
      ...runtimeSessionsResponse,
      items: [
        ...runtimeSessionsResponse.items,
        {
          ...runtimeSessionsResponse.items[0],
          id: 'runtime-session-empty',
          session_reference: '/Users/dimo/.openclaw/openclaw.json',
          total_requests: 0,
          successful_requests: 0,
          failed_requests: 0,
          estimated_cost: 0,
          last_request_at: null,
        },
      ],
    };

    const element = await mountDashboard();
    await waitUntil(
      () =>
        !element['loading'] &&
        !element['fetchingActiveAgents'] &&
        !element['fetchingBudget'] &&
        !element['fetchingAudit'] &&
        !element['fetchingMCPAndTools'],
      'dashboard did not finish loading'
    );
    await element.updateComplete;

    element['expandedOverviewGroups'] = new Set(['active-agent:agent-1']);
    await element.updateComplete;

    const content = element.shadowRoot?.textContent || '';
    expect(content).to.contain('Ops Agent');
    expect(content).to.contain('8 req');
    expect(content).to.not.contain('/Users/dimo/.openclaw/openclaw.json');
  });

  it('attributes top model usage to agents and flows instead of generic sessions', async () => {
    gatewaySummaryResponse = {
      ...gatewaySummaryResponse,
      usage_by_session: [
        ...gatewaySummaryResponse.usage_by_session,
        {
          runtime_session_id: 'flow-runtime-session-1',
          session_source_type: 'flow_execution',
          session_source_id: 'execution-1',
          flow_execution_id: 'execution-1',
          flow_id: 'flow-1',
          flow_name: 'Refund Assistant',
          session_reference: 'flow-session-ref',
          model_alias: 'gpt-5.4',
          provider_name: 'openai',
          request_count: 2,
          token_usage: {
            prompt_tokens: 10,
            completion_tokens: 5,
            total_tokens: 15,
          },
          estimated_cost: 1.2,
          last_request_at: '2026-03-07T10:00:00Z',
        },
      ],
    };

    const element = await mountDashboard();
    await waitUntil(
      () =>
        !element['loading'] &&
        !element['fetchingActiveAgents'] &&
        !element['fetchingBudget'] &&
        !element['fetchingAudit'] &&
        !element['fetchingMCPAndTools'],
      'dashboard did not finish loading'
    );
    await element.updateComplete;

    const content = element.shadowRoot?.textContent || '';
    expect(content).to.contain('Ops Agent');
    expect(content).to.contain('Refund Assistant');
    expect(content).to.not.contain('flow-session-ref');
  });

  it('links flow-backed top model sessions to flow execution pages', async () => {
    gatewaySummaryResponse = {
      ...gatewaySummaryResponse,
      usage_by_session: [
        {
          runtime_session_id: 'flow-runtime-session-1',
          session_source_type: 'flow_execution',
          session_source_id: 'execution-1',
          flow_execution_id: 'execution-1',
          flow_id: 'flow-1',
          flow_name: 'Pull Request Reviewer',
          title: 'PR #42 review',
          model_alias: 'gpt-5.4',
          provider_name: 'openai',
          ai_model_id: 'model-1',
          request_count: 2,
          token_usage: {
            prompt_tokens: 10,
            completion_tokens: 5,
            total_tokens: 15,
          },
          estimated_cost: 1.2,
          last_request_at: '2026-03-07T10:00:00Z',
        },
      ],
    };

    const element = await mountDashboard();
    await waitUntil(
      () =>
        !element['loading'] &&
        !element['fetchingActiveAgents'] &&
        !element['fetchingBudget'] &&
        !element['fetchingAudit'] &&
        !element['fetchingMCPAndTools'],
      'dashboard did not finish loading'
    );
    await element.updateComplete;

    element['expandedOverviewGroups'] = new Set([
      'top-model:model-1-gpt-5.4-openai:flow:flow-1',
    ]);
    await element.updateComplete;

    const flowLink = element.shadowRoot?.querySelector(
      'a[href="/console/flows/flow-1"]'
    );
    const executionLink = element.shadowRoot?.querySelector(
      'a[href="/console/flows/executions/execution-1"]'
    );

    expect(flowLink).to.exist;
    expect(executionLink).to.exist;
    expect(element.shadowRoot?.textContent || '').to.contain(
      'Pull Request Reviewer'
    );
    expect(element.shadowRoot?.textContent || '').to.contain('PR #42 review');
  });
});
