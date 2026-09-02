import { expect, fixture, html, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import '../../components/view-header.ts';
import { unifiedWebSocketManager } from '../../services/unified-websocket-manager';
import './attention-view';
import './dashboard-control-plane-view';
import type { AttentionView } from './attention-view';
import type { DashboardView } from './dashboard-control-plane-view';

/**
 * The Overview hero used to say "3 need attention" while /console/attention
 * listed nine items, because the two fetched their inputs differently: the
 * Overview dropped expired approvals and derived flows from its five most
 * recent executions, and the page asked for 200 agents, which the API rejects
 * with a 422. The fixture below reproduces all three traps.
 */
describe('attention count parity', () => {
  let fetchStub: sinon.SinonStub;
  let connectStub: sinon.SinonStub;
  let subscribeStub: sinon.SinonStub;
  let agentsRequests: string[] = [];

  const ago = (ms: number) => new Date(Date.now() - ms).toISOString();

  const json = (data: unknown) =>
    new Response(JSON.stringify(data), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });

  // Pending for seven weeks and past its expiry: the Overview used to hide it.
  const pendingApprovals = [
    {
      id: 'approval-1',
      tool_name: 'Bash',
      status: 'pending',
      requested_at: ago(49 * 24 * 3600_000),
      expires_at: ago(48 * 24 * 3600_000),
      managed_agent_name: 'Claude Code',
    },
  ];

  const agents = [
    {
      id: 'agent-1',
      display_name: 'Analyst',
      onboarding_state: 'incomplete',
      lifecycle_state: 'active',
      last_seen_at: ago(3600_000),
    },
  ];

  const failedExecutions = [
    {
      id: 'execution-1',
      flow_id: 'flow-1',
      flow_name: 'Pull Request Reviewer',
      status: 'FAILED',
      start_time: ago(2 * 24 * 3600_000),
      end_time: ago(2 * 24 * 3600_000 - 31_000),
    },
    {
      id: 'execution-2',
      flow_id: 'flow-1',
      flow_name: 'Pull Request Reviewer',
      status: 'FAILED',
      start_time: ago(3 * 24 * 3600_000),
      end_time: ago(3 * 24 * 3600_000 - 12_000),
    },
    {
      id: 'execution-3',
      flow_id: 'flow-2',
      flow_name: 'Merge Request Reviewer',
      status: 'FAILED',
      start_time: ago(24 * 3600_000),
      end_time: ago(24 * 3600_000 - 5_000),
    },
  ];

  // What the Overview's own "Recent Flow Executions" card asks for: the ten
  // most recent runs of any status, which here contain no failures at all.
  const recentExecutions = [
    {
      id: 'execution-9',
      flow_id: 'flow-1',
      flow_name: 'Pull Request Reviewer',
      status: 'SUCCEEDED',
      start_time: ago(1800_000),
      end_time: ago(1700_000),
    },
  ];

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
    agentsRequests = [];

    fetchStub = sinon
      .stub(window, 'fetch')
      .callsFake(async (input: RequestInfo | URL) => {
        const url = typeof input === 'string' ? input : input.toString();

        if (url.startsWith('/api/v1/agents')) {
          agentsRequests.push(url);
          const limit = Number(
            new URLSearchParams(url.split('?')[1] || '').get('limit') || '20'
          );
          // Mirrors the backend: limit is Query(20, ge=1, le=100).
          if (limit > 100) {
            return new Response('{"detail":"too large"}', { status: 422 });
          }
          return json({ items: agents, total: agents.length });
        }
        if (url.startsWith('/api/v1/approval-requests')) {
          return json(url.includes('status=pending') ? pendingApprovals : []);
        }
        if (url.startsWith('/api/v1/flows/executions')) {
          return json(
            url.includes('status=FAILED') ? failedExecutions : recentExecutions
          );
        }
        if (url.startsWith('/api/v1/flows')) {
          return json([]);
        }
        if (url.startsWith('/api/v1/runtime-sessions')) {
          return json({ items: [], total: 0 });
        }
        if (url.startsWith('/api/v1/account/gateway-usage/search')) {
          return json({ items: [] });
        }
        if (url.startsWith('/api/v1/account/gateway-usage/summary')) {
          return json({
            total_requests: 0,
            successful_requests: 0,
            failed_requests: 0,
            token_usage: {
              prompt_tokens: 0,
              completion_tokens: 0,
              total_tokens: 0,
            },
            estimated_cost: 0,
            requests_by_day: [],
            usage_by_model: [],
            usage_by_flow: [],
            usage_by_session: [],
          });
        }
        if (url.startsWith('/api/v1/budget/policies')) {
          return json([]);
        }
        if (url === '/api/v1/features') {
          return json({ features: { billing: true } });
        }
        if (url.startsWith('/api/v1/audit-logs/grouped')) {
          return json({ groups: [], total: 0 });
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
          return json({ count: 0 });
        }
        if (url.startsWith('/api/v1/trackers')) {
          return json([]);
        }
        if (url.startsWith('/api/v1/mcp-servers')) {
          return json([]);
        }
        if (url.startsWith('/api/v1/tools')) {
          return json([]);
        }
        if (url === '/api/v1/auth/api-keys') {
          return json([]);
        }
        if (url === '/api/v1/ai-models') {
          return json([]);
        }
        return json({});
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

  it('shows the same count on the Overview hero and the Attention page', async () => {
    const dashboard = (await fixture(
      html`<dashboard-view></dashboard-view>`
    )) as DashboardView;
    await waitUntil(
      () => !dashboard['loading'] && dashboard['attentionInputs'] !== null,
      'dashboard did not finish loading'
    );
    await dashboard.updateComplete;

    const heroValue = dashboard
      .shadowRoot!.querySelector('a.tool-count[href="/console/attention"]')!
      .querySelector('.tool-count-value')!
      .textContent!.trim();

    const page = (await fixture(
      html`<attention-view></attention-view>`
    )) as AttentionView;
    await waitUntil(
      () => !page.shadowRoot!.querySelector('sl-spinner'),
      'attention page did not finish loading'
    );
    await page.updateComplete;

    const pageRows = page.shadowRoot!.querySelectorAll('.attention-row').length;

    // 1 approval + 1 agent + 3 failed flow runs.
    expect(pageRows).to.equal(5);
    expect(heroValue).to.equal(String(pageRows));
    expect(dashboard['attentionItems'].map((item) => item.id)).to.eql(
      page['items'].map((item) => item.id)
    );
  });

  it('never asks the agents endpoint for more than it accepts', async () => {
    const page = (await fixture(
      html`<attention-view></attention-view>`
    )) as AttentionView;
    await waitUntil(
      () => !page.shadowRoot!.querySelector('sl-spinner'),
      'attention page did not finish loading'
    );

    expect(agentsRequests).to.have.length.greaterThan(0);
    for (const url of agentsRequests) {
      const limit = Number(
        new URLSearchParams(url.split('?')[1] || '').get('limit') || '20'
      );
      expect(limit, url).to.be.at.most(100);
    }
    expect(page.shadowRoot!.querySelector('#agents'), 'agents section').to
      .exist;
  });
});
