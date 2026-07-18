import { expect, fixture, html, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import './agents-view.ts';
import type { AgentsView } from './agents-view';

function makeAgent(
  id: string,
  displayName: string,
  sourceType: string
): Record<string, unknown> {
  return {
    id,
    runtime_session_id: `runtime-session-${id}`,
    owner_user_id: null,
    owner_username: null,
    owner_email: null,
    display_name: displayName,
    agent_kind: sourceType,
    session_source_type: sourceType,
    session_source_id: `workspace-${id}`,
    session_reference: `session-${id}`,
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
    total_requests: 3,
    estimated_cost: 0.42,
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
  };
}

describe('AgentsView', () => {
  let fetchStub: sinon.SinonStub;
  let agentItems: Array<Record<string, unknown>>;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');

    agentItems = [makeAgent('agent-1', 'Claude Code Workspace', 'claude_code')];

    fetchStub = sinon.stub(window, 'fetch');
    fetchStub.callsFake(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString();

      if (url.startsWith('/api/v1/agents')) {
        return new Response(
          JSON.stringify({
            query: null,
            session_source_type: null,
            status: 'all',
            total: agentItems.length,
            limit: 50,
            offset: 0,
            items: agentItems,
          }),
          {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          }
        );
      }

      if (url.startsWith('/api/v1/account/gateway-usage/summary')) {
        return new Response(
          JSON.stringify({
            start_date: '2026-02-10T00:00:00Z',
            end_date: '2026-03-10T00:00:00Z',
            token_usage: {
              total_tokens: 10000,
              input_tokens: 8000,
              output_tokens: 2000,
            },
            estimated_cost: 0.5,
            total_requests: 100,
            has_pricing: true,
            requests_by_day: [],
            top_models: [],
            top_agents: [],
            total_agents: 1,
            total_models: 1,
            top_flows: [],
            total_flows: 0,
          }),
          {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          }
        );
      }

      if (url.startsWith('/api/v1/flows')) {
        return new Response(
          JSON.stringify({
            items: [],
            total: 0,
            limit: 50,
            offset: 0,
          }),
          {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          }
        );
      }

      return new Response('Not found', { status: 404 });
    });
  });

  afterEach(() => {
    fetchStub.restore();
    localStorage.clear();
  });

  it('renders enrolled agents and links to agent detail', async () => {
    const el = await fixture<AgentsView>(html`<agents-view></agents-view>`);

    await waitUntil(() => !el.shadowRoot?.querySelector('sl-spinner'));

    const text = el.shadowRoot?.textContent || '';
    expect(text).to.contain('Claude Code Workspace');

    // The section description renders inside the shared view-header.
    const header = el.shadowRoot?.querySelector('view-header');
    expect(header?.getAttribute('description')).to.contain(
      'Onboard agents you already run with the CLI, or deploy new ones.'
    );

    const agentNode = el.shadowRoot?.querySelector('.agent-node');
    expect(agentNode).to.exist;
  });

  it('renders claude_desktop agents and agents of unknown kinds', async () => {
    agentItems = [
      makeAgent('agent-desktop', 'My Claude Desktop', 'claude_desktop'),
      makeAgent('agent-unknown', 'Mystery Agent', 'some_future_kind'),
    ];

    const el = await fixture<AgentsView>(html`<agents-view></agents-view>`);

    await waitUntil(() => !el.shadowRoot?.querySelector('sl-spinner'));

    const text = el.shadowRoot?.textContent || '';
    expect(text).to.contain('My Claude Desktop');
    expect(text).to.contain('Mystery Agent');

    const agentNodes = el.shadowRoot?.querySelectorAll('.agent-node');
    expect(agentNodes?.length).to.equal(2);
  });

  it('omits the agent kind allowlist by default so unknown kinds are fetched', async () => {
    const el = await fixture<AgentsView>(html`<agents-view></agents-view>`);

    await waitUntil(() => !el.shadowRoot?.querySelector('sl-spinner'));

    const agentUrls = fetchStub
      .getCalls()
      .map((call) =>
        typeof call.args[0] === 'string'
          ? call.args[0]
          : call.args[0].toString()
      )
      .filter((url: string) => url.startsWith('/api/v1/agents'));
    expect(agentUrls.length).to.be.greaterThan(0);
    for (const url of agentUrls) {
      expect(url).to.not.contain('agent_kind');
    }
  });

  it('skips the agents API call and renders empty when all kinds are hidden', async () => {
    // Hiding every kind means nothing can match — the view must not fall back
    // to a sentinel agent_kind value; it should not call the agents API at all.
    localStorage.setItem(
      'preloopAgentKindsHidden',
      JSON.stringify([
        'openclaw',
        'opencode',
        'claude_code',
        'claude_desktop',
        'codex',
        'gemini_cli',
        'hermes',
        'cursor',
        'windsurf',
        'desktop_agent',
        'custom',
        'flows',
      ])
    );
    localStorage.setItem('preloop.agents.view_mode', 'cards');

    const el = await fixture<AgentsView>(html`<agents-view></agents-view>`);

    // Cards mode has no spinner; the empty-state renders once loading is done.
    await waitUntil(() => !!el.shadowRoot?.querySelector('.empty-state'));

    const agentUrls = fetchStub
      .getCalls()
      .map((call) =>
        typeof call.args[0] === 'string'
          ? call.args[0]
          : call.args[0].toString()
      )
      .filter((url: string) => url.startsWith('/api/v1/agents'));
    expect(agentUrls).to.have.length(0);

    const agentNodes = el.shadowRoot?.querySelectorAll('.agent-node');
    expect(agentNodes?.length ?? 0).to.equal(0);
    const emptyState = el.shadowRoot?.querySelector('.empty-state');
    expect(emptyState).to.exist;
    expect(emptyState?.textContent).to.contain(
      'No agents or flows found matching your query.'
    );
  });

  it('keeps kinds added after a legacy saved filter visible (claude_desktop)', async () => {
    // A selected-list persisted before claude_desktop existed must not hide it.
    localStorage.setItem(
      'preloopAgentKinds',
      JSON.stringify(['claude_code', 'codex'])
    );
    agentItems = [
      makeAgent('agent-desktop', 'My Claude Desktop', 'claude_desktop'),
    ];

    const el = await fixture<AgentsView>(html`<agents-view></agents-view>`);

    await waitUntil(() => !el.shadowRoot?.querySelector('sl-spinner'));

    const agentUrls = fetchStub
      .getCalls()
      .map((call) =>
        typeof call.args[0] === 'string'
          ? call.args[0]
          : call.args[0].toString()
      )
      .filter((url: string) => url.startsWith('/api/v1/agents'));
    expect(agentUrls.length).to.be.greaterThan(0);
    for (const url of agentUrls) {
      expect(decodeURIComponent(url)).to.contain('claude_desktop');
    }

    const text = el.shadowRoot?.textContent || '';
    expect(text).to.contain('My Claude Desktop');
  });

  it('surfaces the unverified badge on the list when validation was throttled', async () => {
    agentItems = [
      {
        ...makeAgent('agent-1', 'Claude Code Workspace', 'claude_code'),
        live_validation_passed: null,
        live_validation_status: 'throttled',
      },
    ];

    const el = await fixture<AgentsView>(html`<agents-view></agents-view>`);
    await waitUntil(() => !el.shadowRoot?.querySelector('sl-spinner'));

    const text = (el.shadowRoot?.textContent || '').replace(/\s+/g, ' ');
    expect(text).to.contain('Live check throttled — unverified');
    const badge = el.shadowRoot?.querySelector('sl-badge.validation-badge');
    expect(badge?.getAttribute('variant')).to.equal('warning');
  });

  it('surfaces a red badge on the list when validation failed', async () => {
    agentItems = [
      {
        ...makeAgent('agent-1', 'Claude Code Workspace', 'claude_code'),
        live_validation_passed: false,
        live_validation_status: 'failed',
      },
    ];

    const el = await fixture<AgentsView>(html`<agents-view></agents-view>`);
    await waitUntil(() => !el.shadowRoot?.querySelector('sl-spinner'));

    const text = (el.shadowRoot?.textContent || '').replace(/\s+/g, ' ');
    expect(text).to.contain('Live check failed');
    const badge = el.shadowRoot?.querySelector('sl-badge.validation-badge');
    expect(badge?.getAttribute('variant')).to.equal('danger');
  });

  it('suppresses the validation badge when the live check passed', async () => {
    // Default makeAgent fixture has live_validation_status: 'passed'.
    const el = await fixture<AgentsView>(html`<agents-view></agents-view>`);
    await waitUntil(() => !el.shadowRoot?.querySelector('sl-spinner'));

    expect(el.shadowRoot?.querySelector('sl-badge.validation-badge')).to.not
      .exist;
    expect(el.shadowRoot?.textContent).to.not.contain('Live validated');
  });

  it('shows the red model-traffic-failing strip when every request failed', async () => {
    agentItems = [
      {
        ...makeAgent('agent-1', 'Broken Claude', 'claude_code'),
        total_requests: 21,
        successful_requests: 0,
        failed_requests: 21,
      },
    ];

    const el = await fixture<AgentsView>(html`<agents-view></agents-view>`);
    await waitUntil(() => !el.shadowRoot?.querySelector('sl-spinner'));

    const strip = el.shadowRoot?.querySelector('.model-traffic-failing');
    expect(strip, 'failing strip renders').to.exist;
    expect(strip?.textContent?.replace(/\s+/g, ' ')).to.contain(
      'Model traffic failing — see latest session'
    );
    const link = strip?.querySelector('a');
    expect(link?.getAttribute('href')).to.contain(
      '/console/runtime-sessions?sessionId=runtime-session-agent-1'
    );
  });

  it('keeps the strip off below the 5-request threshold and on mixed outcomes', async () => {
    agentItems = [
      {
        ...makeAgent('agent-few', 'New Agent', 'claude_code'),
        total_requests: 3,
        successful_requests: 0,
        failed_requests: 3,
      },
      {
        ...makeAgent('agent-mixed', 'Mixed Agent', 'codex'),
        total_requests: 10,
        successful_requests: 4,
        failed_requests: 6,
      },
    ];

    const el = await fixture<AgentsView>(html`<agents-view></agents-view>`);
    await waitUntil(() => !el.shadowRoot?.querySelector('sl-spinner'));

    expect(el.shadowRoot?.querySelector('.model-traffic-failing')).to.not.exist;
  });
});
