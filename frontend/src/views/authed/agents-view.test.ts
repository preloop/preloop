import { expect, fixture, html, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import './agents-view.ts';
import type { AgentListRow, AgentsView } from './agents-view';
import { sortAgentListRows } from './agents-view';
import { loadShoelaceTokens } from '../../utils/test-shoelace-theme';

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

/** The list view has no spinner, so wait for the loading placeholder to go. */
async function waitForAgents(el: AgentsView): Promise<void> {
  await waitUntil(
    () =>
      !el.shadowRoot?.querySelector('sl-spinner') &&
      !(el.shadowRoot?.textContent || '').includes('Loading agents...') &&
      !!el.shadowRoot?.querySelector(
        'table.agents-table tbody tr, sl-card.agent-card, .agent-node, .empty-state'
      ),
    'agents finished loading'
  );
  await el.updateComplete;
}

function makeRow(overrides: Partial<AgentListRow>): AgentListRow {
  return {
    id: 'row',
    isFlow: false,
    name: 'Agent',
    kindLabel: 'Claude Code',
    kind: 'claude_code',
    detailUrl: '/console/agents/row',
    statusLabel: 'Idle',
    statusVariant: 'neutral',
    statusOutline: false,
    owner: '',
    modelLabel: 'direct (not gated)',
    modelTitle: 'direct (not gated)',
    modelId: null,
    modelGated: false,
    requests: 0,
    spend: 0,
    lastSeen: null,
    source: {} as AgentListRow['source'],
    ...overrides,
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

  it('renders enrolled agents in the list and links to agent detail', async () => {
    const el = await fixture<AgentsView>(html`<agents-view></agents-view>`);

    await waitForAgents(el);

    const text = el.shadowRoot?.textContent || '';
    expect(text).to.contain('Claude Code Workspace');

    // The section description renders inside the shared view-header.
    const header = el.shadowRoot?.querySelector('view-header');
    expect(header?.getAttribute('description')).to.contain(
      'Onboard agents you already run with the CLI, or deploy new ones.'
    );

    // Nothing persisted means the table, not the canvas.
    const table = el.shadowRoot?.querySelector('table.agents-table');
    expect(table, 'list view is the default').to.exist;

    const nameLink = table?.querySelector<HTMLAnchorElement>(
      'tbody .agent-cell a.row-link'
    );
    expect(nameLink?.textContent?.trim()).to.equal('Claude Code Workspace');
    expect(nameLink?.getAttribute('href')).to.equal('/console/agents/agent-1');
    expect(
      table?.querySelector('tbody .row-subtitle')?.textContent?.trim()
    ).to.equal('Claude Code');
  });

  it('renders the three view options and remembers the chosen one', async () => {
    const el = await fixture<AgentsView>(html`<agents-view></agents-view>`);
    await waitForAgents(el);

    const toolbar = el.shadowRoot?.querySelector('list-toolbar');
    const buttons = Array.from(
      toolbar?.shadowRoot?.querySelectorAll(
        'sl-button-group sl-button[data-view]'
      ) || []
    );
    expect(buttons.map((b) => b.getAttribute('data-view'))).to.deep.equal([
      'list',
      'cards',
      'canvas',
    ]);
    expect(buttons.map((b) => b.textContent?.trim())).to.deep.equal([
      'List',
      'Cards',
      'Canvas',
    ]);
    expect(buttons[0].getAttribute('variant')).to.equal('primary');
    expect(buttons[0].getAttribute('aria-pressed')).to.equal('true');

    (buttons[2] as HTMLElement).click();
    await el.updateComplete;

    expect(localStorage.getItem('preloop.agents.view_mode')).to.equal('canvas');
    expect(el.shadowRoot?.querySelector('table.agents-table')).to.not.exist;
  });

  it('honours a persisted cards preference over the list default', async () => {
    localStorage.setItem('preloop.agents.view_mode', 'cards');

    const el = await fixture<AgentsView>(html`<agents-view></agents-view>`);
    await waitForAgents(el);

    expect(el.shadowRoot?.querySelector('table.agents-table')).to.not.exist;
    const cardLink = el.shadowRoot?.querySelector<HTMLAnchorElement>(
      '.cards a.agent-name'
    );
    expect(cardLink?.getAttribute('href')).to.equal('/console/agents/agent-1');
  });

  it('gives every column a sortable header with aria-sort', async () => {
    const el = await fixture<AgentsView>(html`<agents-view></agents-view>`);
    await waitForAgents(el);

    const headers = Array.from(
      el.shadowRoot?.querySelectorAll('table.agents-table thead th') || []
    );
    expect(headers.map((th) => th.textContent?.trim())).to.deep.equal([
      'Agent',
      'Status',
      'Owner',
      'Model',
      'Requests',
      'Spend (est.)',
      'Last seen',
      'Actions',
    ]);

    const lastSeen = headers[6];
    expect(lastSeen.getAttribute('aria-sort'), 'default sort').to.equal(
      'descending'
    );
    expect(headers[0].getAttribute('aria-sort')).to.equal('none');

    lastSeen.querySelector<HTMLButtonElement>('.sort-button')?.click();
    await el.updateComplete;
    expect(
      el.shadowRoot
        ?.querySelectorAll('table.agents-table thead th')[6]
        .getAttribute('aria-sort')
    ).to.equal('ascending');

    headers[0].querySelector<HTMLButtonElement>('.sort-button')?.click();
    await el.updateComplete;
    const after = el.shadowRoot?.querySelectorAll(
      'table.agents-table thead th'
    );
    expect(after?.[0].getAttribute('aria-sort')).to.equal('ascending');
    expect(after?.[6].getAttribute('aria-sort')).to.equal('none');
  });

  it('shows the status chip taxonomy and a right-aligned request count', async () => {
    agentItems = [
      {
        ...makeAgent('agent-1', 'Claude Code Workspace', 'claude_code'),
        total_requests: 1234,
      },
    ];

    const el = await fixture<AgentsView>(html`<agents-view></agents-view>`);
    await waitForAgents(el);

    const chip = el.shadowRoot?.querySelector('tbody sl-badge.status-chip');
    expect(chip?.textContent?.trim()).to.equal('Active now');
    expect(chip?.getAttribute('variant')).to.equal('success');
    // Wave 4: a state is a tint. The class carries the soft recipe; only a
    // header count or a failed run opts back into a solid pill.
    expect(chip?.classList.contains('solid'), 'row state is a solid pill').to.be
      .false;

    const numeric = el.shadowRoot?.querySelectorAll('tbody td.numeric');
    expect(numeric?.[0].textContent?.trim()).to.equal((1234).toLocaleString());
  });

  it('shows a relative last seen with the absolute time on hover', async () => {
    const el = await fixture<AgentsView>(html`<agents-view></agents-view>`);
    await waitForAgents(el);

    const cell = el.shadowRoot?.querySelectorAll('tbody td')[6];
    expect(cell?.textContent?.trim()).to.not.contain('2026-03-10T10:00:00Z');
    expect(cell?.getAttribute('title'))
      .to.be.a('string')
      .and.to.have.length.greaterThan(0);
  });

  it('falls back to cards on a narrow viewport without losing the preference', async () => {
    const el = await fixture<AgentsView>(html`<agents-view></agents-view>`);
    await waitForAgents(el);
    expect(el.shadowRoot?.querySelector('table.agents-table')).to.exist;

    // Simulate the matchMedia listener firing for a phone-width viewport.
    (el as unknown as { narrowViewport: boolean }).narrowViewport = true;
    await el.updateComplete;

    expect(el.shadowRoot?.querySelector('table.agents-table')).to.not.exist;
    expect(el.shadowRoot?.querySelector('.cards')).to.exist;
    // The switcher still reports List as the chosen view.
    expect(
      el.shadowRoot
        ?.querySelector('list-toolbar')
        ?.shadowRoot?.querySelector('sl-button[data-view="list"]')
        ?.getAttribute('aria-pressed')
    ).to.equal('true');
  });

  it('renders claude_desktop agents and agents of unknown kinds', async () => {
    localStorage.setItem('preloop.agents.view_mode', 'canvas');
    agentItems = [
      makeAgent('agent-desktop', 'My Claude Desktop', 'claude_desktop'),
      makeAgent('agent-unknown', 'Mystery Agent', 'some_future_kind'),
    ];

    const el = await fixture<AgentsView>(html`<agents-view></agents-view>`);

    await waitForAgents(el);

    const text = el.shadowRoot?.textContent || '';
    expect(text).to.contain('My Claude Desktop');
    expect(text).to.contain('Mystery Agent');

    const agentNodes = el.shadowRoot?.querySelectorAll('.agent-node');
    expect(agentNodes?.length).to.equal(2);
  });

  it('omits the agent kind allowlist by default so unknown kinds are fetched', async () => {
    const el = await fixture<AgentsView>(html`<agents-view></agents-view>`);

    await waitForAgents(el);

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

    await waitForAgents(el);

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
    localStorage.setItem('preloop.agents.view_mode', 'cards');
    agentItems = [
      {
        ...makeAgent('agent-1', 'Claude Code Workspace', 'claude_code'),
        live_validation_passed: null,
        live_validation_status: 'throttled',
      },
    ];

    const el = await fixture<AgentsView>(html`<agents-view></agents-view>`);
    await waitForAgents(el);

    const text = (el.shadowRoot?.textContent || '').replace(/\s+/g, ' ');
    expect(text).to.contain('Live check throttled, unverified');
    const badge = el.shadowRoot?.querySelector('sl-badge.validation-badge');
    expect(badge?.getAttribute('variant')).to.equal('warning');
  });

  it('surfaces a red badge on the list when validation failed', async () => {
    localStorage.setItem('preloop.agents.view_mode', 'cards');
    agentItems = [
      {
        ...makeAgent('agent-1', 'Claude Code Workspace', 'claude_code'),
        live_validation_passed: false,
        live_validation_status: 'failed',
      },
    ];

    const el = await fixture<AgentsView>(html`<agents-view></agents-view>`);
    await waitForAgents(el);

    // A failed live check IS the status, so it shows once as the status chip
    // rather than twice (status plus a second red badge saying the same).
    const text = (el.shadowRoot?.textContent || '').replace(/\s+/g, ' ');
    expect(text).to.contain('Live check failed');
    expect(el.shadowRoot?.querySelector('sl-badge.validation-badge')).to.not
      .exist;
    const chip = el.shadowRoot?.querySelector(
      '.identity-badges sl-badge.status-chip'
    );
    expect(chip?.textContent?.trim()).to.equal('Live check failed');
    expect(chip?.getAttribute('variant')).to.equal('warning');
    expect(chip?.classList.contains('solid')).to.be.false;
  });

  it('suppresses the validation badge when the live check passed', async () => {
    localStorage.setItem('preloop.agents.view_mode', 'cards');
    // Default makeAgent fixture has live_validation_status: 'passed'.
    const el = await fixture<AgentsView>(html`<agents-view></agents-view>`);
    await waitForAgents(el);

    expect(el.shadowRoot?.querySelector('sl-badge.validation-badge')).to.not
      .exist;
    expect(el.shadowRoot?.textContent).to.not.contain('Live validated');
  });

  it('shows the red model-traffic-failing strip when every request failed', async () => {
    localStorage.setItem('preloop.agents.view_mode', 'cards');
    agentItems = [
      {
        ...makeAgent('agent-1', 'Broken Claude', 'claude_code'),
        total_requests: 21,
        successful_requests: 0,
        failed_requests: 21,
      },
    ];

    const el = await fixture<AgentsView>(html`<agents-view></agents-view>`);
    await waitForAgents(el);

    const strip = el.shadowRoot?.querySelector('.model-traffic-failing');
    expect(strip, 'failing strip renders').to.exist;
    expect(strip?.textContent?.replace(/\s+/g, ' ')).to.contain(
      'Model traffic failing: see latest session'
    );
    const link = strip?.querySelector('a');
    expect(link?.getAttribute('href')).to.contain(
      '/console/runtime-sessions?sessionId=runtime-session-agent-1'
    );
  });

  it('keeps the strip off below the 5-request threshold and on mixed outcomes', async () => {
    localStorage.setItem('preloop.agents.view_mode', 'cards');
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
    await waitForAgents(el);

    expect(el.shadowRoot?.querySelector('.model-traffic-failing')).to.not.exist;
  });

  it('gives every column a fixed width so the kebab stays inside the card', async () => {
    const el = await fixture<AgentsView>(html`<agents-view></agents-view>`);
    await waitForAgents(el);

    const table = el.shadowRoot?.querySelector('table.agents-table');
    const cols = Array.from(table?.querySelectorAll('colgroup col') || []);
    expect(cols.map((col) => col.className)).to.deep.equal([
      'col-agent',
      'col-status',
      'col-owner',
      'col-model',
      'col-requests',
      'col-spend',
      'col-last-seen',
      'col-actions',
    ]);

    const actionsCell = table?.querySelector('tbody td.actions-cell');
    expect(actionsCell, 'the actions cell renders').to.exist;
    const tableRight = table!.getBoundingClientRect().right;
    const cellRight = actionsCell!.getBoundingClientRect().right;
    expect(cellRight, 'kebab column is not clipped').to.be.at.most(
      tableRight + 1
    );

    const name = table?.querySelector('tbody .agent-cell a.row-link');
    expect(getComputedStyle(name!).whiteSpace).to.equal('nowrap');
  });

  it('keeps the kebab button inside its own cell', async () => {
    // Measured against the real tokens: without them the button renders at
    // less than half its size and a column that clips it looks roomy.
    await loadShoelaceTokens();

    const el = await fixture<AgentsView>(html`<agents-view></agents-view>`);
    await waitForAgents(el);

    const cell = el.shadowRoot?.querySelector<HTMLElement>(
      'table.agents-table tbody td.actions-cell'
    );
    expect(cell, 'the actions cell renders').to.exist;

    const kebab = cell
      ?.querySelector('resource-actions')
      ?.shadowRoot?.querySelector<HTMLElement>('sl-dropdown > sl-button');
    expect(kebab, 'the kebab trigger renders').to.exist;

    const cellBox = cell!.getBoundingClientRect();
    const buttonBox = kebab!.getBoundingClientRect();

    expect(buttonBox.width, 'the kebab has its real width').to.be.greaterThan(
      30
    );
    expect(
      buttonBox.left,
      'the kebab is not cut off the left edge of its cell'
    ).to.be.at.least(cellBox.left);
    expect(
      buttonBox.right,
      'the kebab is not cut off the right edge of its cell'
    ).to.be.at.most(cellBox.right);
  });

  it('offers Talk in the list kebab only for agents with Agent Control', async () => {
    agentItems = [
      {
        ...makeAgent('agent-1', 'Mini', 'openclaw'),
        control_state: 'plugin_connected',
        control_enabled: true,
        control_online: true,
        control_capabilities: ['send_text_prompt'],
      },
      makeAgent('agent-2', 'Claude Desktop', 'claude_desktop'),
    ];

    const el = await fixture<AgentsView>(html`<agents-view></agents-view>`);
    await waitForAgents(el);

    const rows = Array.from(
      el.shadowRoot?.querySelectorAll('table.agents-table tbody tr') || []
    );
    const menuFor = (name: string) => {
      const row = rows.find((candidate) =>
        (candidate.textContent || '').includes(name)
      );
      return row?.querySelector('resource-actions') as HTMLElement & {
        actions: Array<Record<string, unknown>>;
      };
    };

    const connected = menuFor('Mini');
    const talk = connected.actions.find((action) => action.id === 'talk');
    expect(talk, 'the connected agent can be talked to').to.exist;
    expect(talk!.label).to.equal('Talk');
    expect(talk!.disabled).to.equal(false);
    expect(connected.actions[0].id, 'Talk leads the menu').to.equal('talk');

    expect(
      menuFor('Claude Desktop').actions.some((action) => action.id === 'talk'),
      'a runtime without Agent Control gets no Talk item'
    ).to.equal(false);
  });

  it('shows only the model alias, with the full model text in the title', async () => {
    agentItems = [
      {
        ...makeAgent('agent-1', 'Claude Code Workspace', 'claude_code'),
        ai_model_id: 'model-1',
        configured_model_alias: 'preloop/deepseek/deepseek-chat',
      },
    ];
    fetchStub.withArgs(sinon.match(/\/api\/v1\/ai-models/)).resolves(
      new Response(
        JSON.stringify([
          {
            id: 'model-1',
            name: 'OpenClaw preloop/deepseek/deepseek-chat',
            provider_name: 'deepseek',
            model_identifier: 'deepseek-chat',
            created_at: '2026-03-01T00:00:00Z',
          },
        ]),
        { status: 200, headers: { 'Content-Type': 'application/json' } }
      )
    );

    const el = await fixture<AgentsView>(html`<agents-view></agents-view>`);
    await waitForAgents(el);

    const cell = el.shadowRoot?.querySelector('tbody td.model-cell');
    expect(cell?.textContent?.trim()).to.equal(
      'preloop/deepseek/deepseek-chat'
    );
    expect(cell?.getAttribute('title')).to.contain(
      'OpenClaw preloop/deepseek/deepseek-chat'
    );
  });

  it('keeps last seen relative for a month and absolute after it', async () => {
    const now = Date.now();
    const tenDaysAgo = new Date(now - 10 * 86400000).toISOString();
    const twoHundredDaysAgo = new Date(now - 200 * 86400000).toISOString();
    agentItems = [
      {
        ...makeAgent('agent-1', 'Recent agent', 'claude_code'),
        last_seen_at: tenDaysAgo,
        last_activity_at: tenDaysAgo,
        is_active_now: false,
        activity_status: 'idle',
      },
      {
        ...makeAgent('agent-2', 'Stale agent', 'claude_code'),
        last_seen_at: twoHundredDaysAgo,
        last_activity_at: twoHundredDaysAgo,
        is_active_now: false,
        activity_status: 'idle',
      },
    ];

    const el = await fixture<AgentsView>(html`<agents-view></agents-view>`);
    await waitForAgents(el);

    const lastSeen = Array.from(
      el.shadowRoot?.querySelectorAll('tbody tr') || []
    ).map((row) => row.children[6].textContent?.trim());
    expect(lastSeen).to.contain('10d ago');
    expect(lastSeen).to.contain(
      new Date(twoHundredDaysAgo).toLocaleDateString()
    );
  });

  it('counts the rows the filters matched next to the view switcher', async () => {
    agentItems = [
      makeAgent('agent-1', 'One', 'claude_code'),
      makeAgent('agent-2', 'Two', 'claude_code'),
    ];

    const el = await fixture<AgentsView>(html`<agents-view></agents-view>`);
    await waitForAgents(el);

    expect(
      el.shadowRoot
        ?.querySelector('list-toolbar [slot="count"]')
        ?.textContent?.trim()
    ).to.equal('2 agents');
  });

  it('labels the header actions by what they do', async () => {
    const el = await fixture<AgentsView>(html`<agents-view></agents-view>`);
    await waitForAgents(el);

    const labels = Array.from(
      el.shadowRoot?.querySelectorAll('view-header sl-button') || []
    ).map((button) => button.textContent?.trim());
    expect(labels).to.deep.equal([
      'Deploy new agent',
      'Onboard existing agent',
    ]);
  });

  it('explains the dashed unmanaged nodes in the canvas legend', async () => {
    localStorage.setItem('preloop.agents.view_mode', 'canvas');

    const el = await fixture<AgentsView>(html`<agents-view></agents-view>`);
    await waitForAgents(el);

    const legend = el.shadowRoot?.querySelector('.canvas-legend');
    expect(legend, 'canvas legend renders').to.exist;
    const items = Array.from(
      legend?.querySelectorAll('.legend-item') || []
    ).map((item) => item.textContent?.replace(/\s+/g, ' ').trim());
    expect(items).to.have.length(3);
    expect(items[2]).to.contain('Unmanaged (dashed gray)');
  });
});

describe('sortAgentListRows', () => {
  const rows = [
    makeRow({
      id: 'b',
      name: 'Beta',
      requests: 10,
      spend: 1,
      lastSeen: '2026-03-10T09:00:00Z',
      owner: 'zoe',
    }),
    makeRow({
      id: 'a',
      name: 'Alpha',
      requests: 2,
      spend: 30,
      lastSeen: '2026-03-10T11:00:00Z',
      owner: 'adam',
    }),
    makeRow({
      id: 'c',
      name: 'Gamma',
      requests: 40,
      spend: 2,
      lastSeen: null,
      owner: '',
    }),
  ];

  const ids = (
    key: Parameters<typeof sortAgentListRows>[1],
    dir: 'asc' | 'desc'
  ) => sortAgentListRows(rows, key, dir).map((row) => row.id);

  it('does not mutate the rows it is given', () => {
    const before = rows.map((row) => row.id);
    sortAgentListRows(rows, 'agent', 'asc');
    expect(rows.map((row) => row.id)).to.deep.equal(before);
  });

  it('sorts by name in both directions', () => {
    expect(ids('agent', 'asc')).to.deep.equal(['a', 'b', 'c']);
    expect(ids('agent', 'desc')).to.deep.equal(['c', 'b', 'a']);
  });

  it('sorts numeric columns by value, not by their formatted text', () => {
    expect(ids('requests', 'desc')).to.deep.equal(['c', 'b', 'a']);
    expect(ids('spend', 'desc')).to.deep.equal(['a', 'c', 'b']);
  });

  it('sorts last seen newest first and keeps never-seen agents last', () => {
    expect(ids('last_seen', 'desc')).to.deep.equal(['a', 'b', 'c']);
  });

  it('sorts owners alphabetically with unassigned agents last', () => {
    expect(ids('owner', 'asc')).to.deep.equal(['a', 'b', 'c']);
  });
});
