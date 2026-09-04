import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import './tools-view';
import type { ToolsView } from './tools-view';
import { invalidateApiCaches } from '../../api';

describe('ToolsView (approvals + conditions)', () => {
  let fetchStub: sinon.SinonStub;
  let overrideAgentIds: string[];
  let accountAgents: { id: string; display_name: string }[];

  beforeEach(() => {
    overrideAgentIds = ['agent-override-1'];
    accountAgents = [
      { id: 'agent-override-1', display_name: 'Claude Code Workspace' },
    ];
    // fetchWithAuth requires an access token to exist
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');

    fetchStub = sinon.stub(window, 'fetch');

    const tool = {
      name: 'example_tool',
      description: 'Example tool',
      source: 'builtin',
      source_id: null,
      source_name: 'Built-in',
      schema: {},
      is_enabled: true,
      is_supported: true,
      approval_workflow_id: null,
      has_approval_condition: false,
      config_id: null,
    };

    const defaultPolicy = {
      id: 'policy-1',
      name: 'Default',
      description: 'Default policy',
      approval_type: 'standard',
      is_default: true,
    };

    fetchStub.callsFake(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();
        const method = (init?.method || 'GET').toUpperCase();

        // Initial ToolsView.loadData() requests
        if (url.endsWith('/api/v1/tools') && method === 'GET') {
          return new Response(JSON.stringify([tool]), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        }

        if (url.endsWith('/api/v1/mcp-servers') && method === 'GET') {
          return new Response(JSON.stringify([]), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        }

        if (url.endsWith('/api/v1/approval-workflows') && method === 'GET') {
          return new Response(JSON.stringify([defaultPolicy]), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        }

        if (url.endsWith('/api/v1/features') && method === 'GET') {
          return new Response(JSON.stringify({ features: {} }), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        }

        if (url.endsWith('/api/v1/ai-models') && method === 'GET') {
          return new Response(JSON.stringify([]), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        }

        // ToolListItem / loadCurrentUser() request
        if (url.endsWith('/api/v1/auth/users/me') && method === 'GET') {
          return new Response(JSON.stringify({ id: 'user-1' }), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        }

        // Native tool approvals account-defaults card
        if (url.endsWith('/api/v1/account/governance-defaults')) {
          if (method === 'PUT') {
            return new Response(
              JSON.stringify({
                defaults: JSON.parse(String(init?.body)),
                override_agent_ids: [],
              }),
              { status: 200, headers: { 'Content-Type': 'application/json' } }
            );
          }
          return new Response(
            JSON.stringify({
              defaults: {
                native_tool_approvals: null,
                approval_workflow_id: null,
              },
              override_agent_ids: overrideAgentIds,
            }),
            { status: 200, headers: { 'Content-Type': 'application/json' } }
          );
        }

        if (url.includes('/api/v1/agents?') || url.endsWith('/api/v1/agents')) {
          return new Response(
            JSON.stringify({
              query: null,
              agent_kind: null,
              last_seen_after: null,
              status: 'all',
              total: accountAgents.length,
              limit: 100,
              offset: 0,
              items: accountAgents,
            }),
            { status: 200, headers: { 'Content-Type': 'application/json' } }
          );
        }

        // Regression path: enable approval => create tool configuration
        if (url.endsWith('/api/v1/tool-configurations') && method === 'POST') {
          return new Response(JSON.stringify({ id: 'cfg-1' }), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        }

        // Saving an access rule uses the dedicated endpoint
        if (
          url.endsWith('/api/v1/tool-configurations/cfg-1/access-rules') &&
          method === 'POST'
        ) {
          return new Response(
            JSON.stringify({ id: 'rule-1', action: 'require_approval' }),
            {
              status: 200,
              headers: { 'Content-Type': 'application/json' },
            }
          );
        }

        return new Response(
          JSON.stringify({ detail: `Unhandled request: ${method} ${url}` }),
          {
            status: 500,
            headers: { 'Content-Type': 'application/json' },
          }
        );
      }
    );
  });

  afterEach(() => {
    fetchStub.restore();
    localStorage.clear();
    invalidateApiCaches();
  });

  it('renders the summary stats with correct labels and an unavailable note', async () => {
    const availableTool = {
      name: 'example_tool',
      description: 'Example tool',
      source: 'builtin',
      source_id: null,
      source_name: 'Built-in',
      schema: {},
      is_enabled: true,
      is_supported: true,
      approval_workflow_id: null,
      has_approval_condition: false,
      config_id: null,
      schema_tokens_estimate: 1200,
    };
    const unavailableTool = {
      ...availableTool,
      name: 'tracker_tool',
      description: 'Needs a tracker',
      is_supported: false,
      unsupported_reason: 'Requires a connected tracker',
      schema_tokens_estimate: 800,
    };

    fetchStub.callsFake(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString();
      if (url.endsWith('/api/v1/tools')) {
        return new Response(JSON.stringify([availableTool, unavailableTool]), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      if (url.endsWith('/api/v1/features')) {
        return new Response(JSON.stringify({ features: {} }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      return new Response('[]', {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    });

    const el = (await fixture(html`<tools-view></tools-view>`)) as ToolsView;
    await waitUntil(
      () => el.shadowRoot?.querySelector('.summary-table') !== null,
      'Summary table did not render'
    );

    const summary = el.shadowRoot?.querySelector('.summary-table');
    expect(summary?.textContent).to.contain('Total tools');
    expect(summary?.textContent).to.not.contain('toolss');

    const note = el.shadowRoot?.querySelector('.unavailable-note');
    expect(note).to.exist;
    expect(note?.textContent).to.contain('Requires a connected tracker');

    const contextTax = el.shadowRoot?.querySelector('.context-tax');
    expect(contextTax).to.exist;
    // Only enabled+supported tools contribute (1200), not unavailable (800).
    expect(contextTax?.textContent?.replace(/\s+/g, ' ')).to.contain(
      '~1,200 tokens'
    );
    expect(contextTax?.textContent).to.contain('to every agent request');
  });

  it('renders the native tool approvals account-default card with override links', async () => {
    const element = await fixture<ToolsView>(html`<tools-view></tools-view>`);
    await waitUntil(
      () =>
        !(element as any).loading &&
        (element as any).governanceDefaults !== null,
      'Tools view did not load governance defaults'
    );
    await element.updateComplete;

    const card = element.shadowRoot?.querySelector(
      '#native-approvals-defaults-card'
    );
    expect(card).to.exist;
    expect(card?.textContent).to.not.include('\u2014');
    expect(card?.textContent?.replace(/\s+/g, ' ')).to.include(
      'Ask a human before running'
    );

    // Default (null) renders as enforce: switch on, no bypass notice.
    const switchEl = element.shadowRoot?.querySelector(
      '#native-approvals-default-switch'
    ) as any;
    expect(switchEl).to.exist;
    expect(switchEl.checked).to.be.true;

    // The override list resolves agent names and links to agent detail.
    const overrides = element.shadowRoot?.querySelector(
      '#native-approvals-override-list'
    );
    expect(overrides).to.exist;
    expect(overrides?.textContent).to.include('1 agent uses its own setting');
    const link = overrides?.querySelector('a');
    expect(link?.getAttribute('href')).to.equal(
      '/console/agents/agent-override-1'
    );
    expect(link?.textContent).to.contain('Claude Code Workspace');

    // Switching off persists via PUT with native_tool_approvals="off".
    await (element as any)._saveGovernanceDefaults({
      native_tool_approvals: 'off',
    });
    const putCall = fetchStub
      .getCalls()
      .find(
        (call) =>
          String(call.args[0]).endsWith(
            '/api/v1/account/governance-defaults'
          ) && call.args[1]?.method === 'PUT'
      );
    expect(putCall).to.exist;
    expect(
      JSON.parse(String(putCall!.args[1]!.body)).native_tool_approvals
    ).to.equal('off');
  });

  it('shows the resolved default workflow name in the workflow select', async () => {
    const element = await fixture<ToolsView>(html`<tools-view></tools-view>`);
    await waitUntil(
      () =>
        !(element as any).loading &&
        (element as any).governanceDefaults !== null,
      'Tools view did not load governance defaults'
    );
    await element.updateComplete;

    const select = element.shadowRoot?.querySelector(
      '#native-approvals-default-workflow'
    );
    expect(select).to.exist;
    const options = Array.from(select!.querySelectorAll('sl-option'));
    expect(options.length).to.be.greaterThan(0);
    expect(options[0]?.textContent).to.include('Default workflow (Default)');
    for (const option of options) {
      expect(option.textContent).to.not.include('(account default)');
    }
  });

  it('hides the workflow select and shows a notice when approvals are off', async () => {
    const element = await fixture<ToolsView>(html`<tools-view></tools-view>`);
    await waitUntil(
      () =>
        !(element as any).loading &&
        (element as any).governanceDefaults !== null,
      'Tools view did not load governance defaults'
    );
    await element.updateComplete;

    await (element as any)._saveGovernanceDefaults({
      native_tool_approvals: 'off',
    });
    await element.updateComplete;

    expect(
      element.shadowRoot?.querySelector('#native-approvals-default-workflow')
    ).to.be.null;
    const notice = element.shadowRoot?.querySelector('.governance-notice');
    expect(notice).to.exist;
    expect(notice?.textContent).to.include('run without asking');

    await (element as any)._saveGovernanceDefaults({
      native_tool_approvals: 'enforce',
    });
    await element.updateComplete;

    expect(
      element.shadowRoot?.querySelector('#native-approvals-default-workflow')
    ).to.exist;
    expect(element.shadowRoot?.querySelector('.governance-notice')).to.be.null;
  });

  it('caps the override list at 5 agents and links the remainder', async () => {
    overrideAgentIds = [
      'agent-1',
      'agent-2',
      'agent-3',
      'agent-4',
      'agent-5',
      'agent-6',
      'agent-7',
    ];
    accountAgents = overrideAgentIds.map((id, index) => ({
      id,
      display_name: `Agent ${index + 1}`,
    }));

    const element = await fixture<ToolsView>(html`<tools-view></tools-view>`);
    await waitUntil(
      () =>
        !(element as any).loading &&
        (element as any).governanceDefaults !== null,
      'Tools view did not load governance defaults'
    );
    await element.updateComplete;

    const overrides = element.shadowRoot?.querySelector(
      '#native-approvals-override-list'
    );
    expect(overrides).to.exist;
    expect(overrides?.textContent).to.include('7 agents use their own setting');

    const links = Array.from(overrides!.querySelectorAll('a'));
    expect(links.length).to.equal(6);
    const agentLinks = links.slice(0, 5);
    agentLinks.forEach((agentLink, index) => {
      expect(agentLink.getAttribute('href')).to.equal(
        `/console/agents/agent-${index + 1}`
      );
    });
    const moreLink = links[5];
    expect(moreLink.getAttribute('href')).to.equal('/console/agents');
    expect(moreLink.textContent).to.include('and 2 more');
  });

  it('does not create tool configuration twice when adding a rule immediately after toggling enabled', async () => {
    const element = (await fixture(
      html`<tools-view></tools-view>`
    )) as ToolsView;

    await waitUntil(
      () => (element as any).tools?.length === 1,
      'Tools did not load'
    );
    await element.updateComplete;

    const editor = element.shadowRoot?.querySelector(
      'tools-editor-component'
    ) as HTMLElement;
    const toolItem = editor.shadowRoot?.querySelector(
      'tool-list-item'
    ) as HTMLElement;
    expect(toolItem).to.exist;

    expect((element as any).tools[0].config_id).to.equal(null);

    // Step 1: toggle enabled (auto-creates config since config_id is null)
    toolItem.dispatchEvent(
      new CustomEvent('toggle-enabled', {
        detail: (element as any).tools[0],
        bubbles: true,
        composed: true,
      })
    );

    await waitUntil(
      () => (element as any).tools?.[0]?.config_id === 'cfg-1',
      'Tool config_id was not set after toggling enabled'
    );
    await element.updateComplete;

    const updatedTool = (element as any).tools[0];
    expect(updatedTool.config_id).to.equal('cfg-1');

    // Step 2: immediately save a rule - must NOT try to create config again
    toolItem.dispatchEvent(
      new CustomEvent('save-rule', {
        detail: {
          tool: updatedTool,
          existingRule: null,
          formData: {
            action: 'require_approval',
            condition_expression: null,
            condition_type: 'cel',
            description: null,
            is_enabled: true,
            approval_workflow_id: 'policy-1',
          },
        },
        bubbles: true,
        composed: true,
      })
    );

    await new Promise((r) => setTimeout(r, 0));
    await element.updateComplete;

    const toolConfigCreateCalls = fetchStub.getCalls().filter((c) => {
      const url = String(c.args[0]);
      const method = String(
        (c.args[1] as RequestInit | undefined)?.method || 'GET'
      ).toUpperCase();
      return url.endsWith('/api/v1/tool-configurations') && method === 'POST';
    });

    expect(toolConfigCreateCalls.length).to.equal(1);
    expect((element as any).error).to.equal(null);
  });
});

describe('ToolsView – filter persistence', () => {
  let fetchStub: sinon.SinonStub;

  const STORAGE_KEY = 'preloop:tools-filter';

  function stubLoadData() {
    fetchStub.callsFake(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();
        const method = (init?.method || 'GET').toUpperCase();

        if (url.endsWith('/api/v1/tools') && method === 'GET') {
          return new Response(JSON.stringify([]), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        }
        if (url.endsWith('/api/v1/mcp-servers') && method === 'GET') {
          return new Response(JSON.stringify([]), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        }
        if (url.endsWith('/api/v1/approval-workflows') && method === 'GET') {
          return new Response(JSON.stringify([]), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        }
        if (url.endsWith('/api/v1/features') && method === 'GET') {
          return new Response(JSON.stringify({ features: {} }), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        }
        if (url.endsWith('/api/v1/ai-models') && method === 'GET') {
          return new Response(JSON.stringify([]), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        }
        if (url.endsWith('/api/v1/auth/users/me') && method === 'GET') {
          return new Response(JSON.stringify({ id: 'user-1' }), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        }
        return new Response(JSON.stringify({}), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
    );
  }

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
    fetchStub = sinon.stub(window, 'fetch');
    stubLoadData();
  });

  afterEach(() => {
    fetchStub.restore();
    localStorage.clear();
    invalidateApiCaches();
  });

  it('defaults to "available" filter when no saved preference', async () => {
    const el = (await fixture(html`<tools-view></tools-view>`)) as ToolsView;
    await waitUntil(() => !(el as any).loading, 'Still loading');
    await el.updateComplete;

    expect((el as any).activeFilter).to.equal('available');
  });

  it('restores saved filter from localStorage on mount', async () => {
    localStorage.setItem(STORAGE_KEY, 'prelooped');

    const el = (await fixture(html`<tools-view></tools-view>`)) as ToolsView;
    await waitUntil(() => !(el as any).loading, 'Still loading');
    await el.updateComplete;

    expect((el as any).activeFilter).to.equal('prelooped');
  });

  it('persists filter to localStorage when changed', async () => {
    const el = (await fixture(html`<tools-view></tools-view>`)) as ToolsView;
    await waitUntil(() => !(el as any).loading, 'Still loading');
    await el.updateComplete;

    (el as any)._setFilter('prelooped');
    expect(localStorage.getItem(STORAGE_KEY)).to.equal('prelooped');
    expect((el as any).activeFilter).to.equal('prelooped');
  });

  it('survives round-trip: set filter → remount → filter restored', async () => {
    // Mount first instance, set filter
    const el1 = (await fixture(html`<tools-view></tools-view>`)) as ToolsView;
    await waitUntil(() => !(el1 as any).loading, 'Still loading');
    (el1 as any)._setFilter('mcp');
    el1.remove();

    // Mount second instance — should pick up the saved filter
    const el2 = (await fixture(html`<tools-view></tools-view>`)) as ToolsView;
    await waitUntil(() => !(el2 as any).loading, 'Still loading');
    await el2.updateComplete;

    expect((el2 as any).activeFilter).to.equal('mcp');
  });
});

describe('ToolsView – starter policy suggestions', () => {
  let fetchStub: sinon.SinonStub;
  let servers: any[];
  let tools: any[];
  let aiModels: any[];
  let generatedYaml: string;
  let generatedDiff: any;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');

    servers = [];
    tools = [];
    aiModels = [
      {
        id: 'model-1',
        name: 'Default Model',
        provider_name: 'openai',
        model_identifier: 'gpt-test',
        is_default: true,
        created_at: '2026-01-01T00:00:00Z',
        updated_at: '2026-01-01T00:00:00Z',
      },
    ];
    generatedYaml = 'version: "1.0"\nmetadata:\n  name: starter-policy';
    generatedDiff = {
      has_changes: true,
      summary: '2 change(s) detected',
      changes: [
        {
          path: '$.tools.github_search_issues',
          operation: 'modify',
          old_value: { action: 'require_approval' },
          new_value: { action: 'allow' },
        },
        {
          path: '$.approval_workflows.github-approval',
          operation: 'add',
          new_value: { type: 'human' },
        },
      ],
    };

    fetchStub = sinon.stub(window, 'fetch');
    fetchStub.callsFake(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();
        const method = (init?.method || 'GET').toUpperCase();

        if (url.endsWith('/api/v1/tools') && method === 'GET') {
          return new Response(JSON.stringify(tools), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        }
        if (url.endsWith('/api/v1/mcp-servers') && method === 'GET') {
          return new Response(JSON.stringify(servers), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        }
        if (url.endsWith('/api/v1/approval-workflows') && method === 'GET') {
          return new Response(JSON.stringify([]), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        }
        if (url.endsWith('/api/v1/features') && method === 'GET') {
          return new Response(JSON.stringify({ features: {} }), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        }
        if (url.endsWith('/api/v1/auth/users/me') && method === 'GET') {
          return new Response(JSON.stringify({ id: 'user-1' }), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        }
        if (url.endsWith('/api/v1/ai-models') && method === 'GET') {
          return new Response(JSON.stringify(aiModels), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        }
        if (url.endsWith('/api/v1/policies/generate') && method === 'POST') {
          return new Response(
            JSON.stringify({
              yaml: generatedYaml,
              warnings: ['Review mutating tools before applying.'],
            }),
            {
              status: 200,
              headers: { 'Content-Type': 'application/json' },
            }
          );
        }
        if (url.endsWith('/api/v1/policies/diff') && method === 'POST') {
          return new Response(JSON.stringify(generatedDiff), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        }
        if (url.endsWith('/api/v1/policies/upload') && method === 'POST') {
          return new Response(JSON.stringify({ ok: true }), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        }

        return new Response(
          JSON.stringify({ detail: `Unhandled request: ${method} ${url}` }),
          {
            status: 500,
            headers: { 'Content-Type': 'application/json' },
          }
        );
      }
    );
  });

  afterEach(() => {
    fetchStub.restore();
    localStorage.clear();
    window.history.replaceState({}, '', window.location.pathname);
    invalidateApiCaches();
  });

  function makeServer(
    id: string,
    updatedAt: string,
    name = 'GitHub MCP'
  ): Record<string, unknown> {
    return {
      id,
      name,
      url: `https://${id}.example.com/mcp`,
      transport: 'http-streaming',
      auth_type: 'oauth',
      status: 'active',
      created_at: '2026-01-01T00:00:00Z',
      updated_at: updatedAt,
    };
  }

  function makeTool(serverId: string, name = 'github_search_issues') {
    return {
      name,
      description: 'Search issues in GitHub',
      source: 'mcp',
      source_id: serverId,
      source_name: 'GitHub MCP',
      schema: {},
      is_enabled: true,
      is_supported: true,
      approval_workflow_id: null,
      has_approval_condition: false,
      config_id: null,
      access_rules: [],
    };
  }

  it('auto-opens a starter policy suggestion after server-added when tools are present', async () => {
    const el = (await fixture(html`<tools-view></tools-view>`)) as ToolsView;
    await waitUntil(() => !(el as any).loading, 'Initial load did not finish');

    const server = makeServer('srv-1', '2026-02-01T00:00:00Z');
    servers = [server];
    tools = [makeTool('srv-1')];

    await (el as any)._handleServerAdded(
      new CustomEvent('server-added', { detail: { server } })
    );

    await waitUntil(
      () => (el as any).showStarterPolicyDialog,
      'Starter policy dialog did not open'
    );
    await waitUntil(
      () => (el as any).starterPolicyYaml !== '',
      'Starter policy YAML was not generated'
    );

    expect((el as any).starterPolicyServer.id).to.equal('srv-1');
    const generateCall = fetchStub
      .getCalls()
      .find((call) =>
        String(call.args[0]).endsWith('/api/v1/policies/generate')
      );
    expect(generateCall).to.exist;

    const body = JSON.parse(
      (generateCall!.args[1] as RequestInit).body as string
    );
    expect(body.include_current_config).to.equal(true);
    expect(body.prompt).to.include('GitHub MCP');
    expect(body.prompt).to.include('github_search_issues');
    expect((el as any).starterPolicyDiff).to.deep.equal(generatedDiff);
  });

  it('requires review confirmation before applying the generated YAML', async () => {
    servers = [makeServer('srv-1', '2026-02-01T00:00:00Z')];
    tools = [makeTool('srv-1')];

    const el = (await fixture(html`<tools-view></tools-view>`)) as ToolsView;
    await waitUntil(() => !(el as any).loading, 'Initial load did not finish');
    await el.updateComplete;

    const editor = el.shadowRoot?.querySelector(
      'tools-editor-component'
    ) as HTMLElement;
    const trigger = editor.shadowRoot?.querySelector(
      'sl-icon-button[name="magic"]'
    ) as HTMLElement | null;
    expect(trigger).to.exist;
    trigger!.click();

    await waitUntil(
      () => (el as any).starterPolicyYaml !== '',
      'Starter policy YAML was not generated'
    );
    await waitUntil(
      () => (el as any).starterPolicyDiff !== null,
      'Starter policy diff was not generated'
    );
    await el.updateComplete;

    const applyButton = Array.from(
      el.shadowRoot!.querySelectorAll('sl-dialog sl-button')
    ).find((button) => button.textContent?.trim().includes('Apply')) as
      HTMLElement | undefined;
    expect(applyButton).to.exist;
    expect((applyButton as any).disabled).to.equal(true);

    const reviewCheckbox = el.shadowRoot?.querySelector(
      '.starter-policy-review-confirm'
    ) as any;
    expect(reviewCheckbox).to.exist;
    (el as any).starterPolicyReviewConfirmed = true;

    await el.updateComplete;
    expect((applyButton as any).disabled).to.equal(false);

    applyButton!.click();

    await waitUntil(
      () =>
        fetchStub
          .getCalls()
          .some(
            (call) =>
              String(call.args[0]).endsWith('/api/v1/policies/upload') &&
              String(
                (call.args[1] as RequestInit | undefined)?.method || 'GET'
              ).toUpperCase() === 'POST'
          ),
      'Policy upload request was not made'
    );

    await waitUntil(
      () => (el as any).showStarterPolicyDialog === false,
      'Starter policy dialog did not close'
    );
  });

  it('keeps apply disabled when the generated policy matches current state', async () => {
    servers = [makeServer('srv-1', '2026-02-01T00:00:00Z')];
    tools = [makeTool('srv-1')];
    generatedDiff = {
      has_changes: false,
      summary: 'No changes detected.',
      changes: [],
    };

    const el = (await fixture(html`<tools-view></tools-view>`)) as ToolsView;
    await waitUntil(() => !(el as any).loading, 'Initial load did not finish');

    const editor = el.shadowRoot?.querySelector(
      'tools-editor-component'
    ) as HTMLElement;
    const trigger = editor.shadowRoot?.querySelector(
      'sl-icon-button[name="magic"]'
    ) as HTMLElement | null;
    expect(trigger).to.exist;
    trigger!.click();

    await waitUntil(
      () => (el as any).starterPolicyDiff !== null,
      'Starter policy diff was not generated'
    );
    await el.updateComplete;

    const applyButton = Array.from(
      el.shadowRoot!.querySelectorAll('sl-dialog sl-button')
    ).find((button) => button.textContent?.trim().includes('Apply')) as
      HTMLElement | undefined;
    expect(applyButton).to.exist;
    expect((applyButton as any).disabled).to.equal(true);
    expect(
      el.shadowRoot?.textContent?.includes('No changes detected.')
    ).to.equal(true);
  });

  it('uses the most recently updated MCP server after OAuth success', async () => {
    servers = [
      makeServer('srv-older', '2026-02-01T00:00:00Z', 'Older MCP'),
      makeServer('srv-newer', '2026-02-03T00:00:00Z', 'Newer MCP'),
    ];
    tools = [makeTool('srv-newer', 'github_list_pull_requests')];
    window.location.hash = '#setup_mcp=success';

    const el = (await fixture(html`<tools-view></tools-view>`)) as ToolsView;

    await waitUntil(
      () => (el as any).showStarterPolicyDialog,
      'Starter policy dialog did not open after OAuth success'
    );
    await waitUntil(
      () => (el as any).starterPolicyYaml !== '',
      'Starter policy YAML was not generated'
    );

    expect((el as any).starterPolicyServer.id).to.equal('srv-newer');
    expect((el as any).oauthAlert).to.equal('success');
  });
});
