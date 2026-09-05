import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import type { LitElement } from 'lit';
import sinon from 'sinon';

import './tools-view';
import type { ToolsView } from './tools-view';
import { invalidateApiCaches } from '../../api';

describe('ToolsView (approvals + conditions)', () => {
  let fetchStub: sinon.SinonStub;
  let overrideAgentIds: string[];
  let accountAgents: { id: string; display_name: string }[];
  let failGovernanceGet: boolean;
  let failGovernancePut: boolean;

  beforeEach(() => {
    overrideAgentIds = ['agent-override-1'];
    accountAgents = [
      { id: 'agent-override-1', display_name: 'Claude Code Workspace' },
    ];
    failGovernanceGet = false;
    failGovernancePut = false;
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
    const extraPolicy = {
      id: 'policy-2',
      name: 'Security',
      description: 'Security policy',
      approval_type: 'standard',
      is_default: false,
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
          return new Response(JSON.stringify([defaultPolicy, extraPolicy]), {
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
            if (failGovernancePut) {
              failGovernancePut = false;
              return new Response(
                JSON.stringify({ detail: 'Simulated PUT failure' }),
                { status: 500, headers: { 'Content-Type': 'application/json' } }
              );
            }
            return new Response(
              JSON.stringify({
                defaults: JSON.parse(String(init?.body)),
                override_agent_ids: [],
              }),
              { status: 200, headers: { 'Content-Type': 'application/json' } }
            );
          }
          if (failGovernanceGet) {
            failGovernanceGet = false;
            return new Response(
              JSON.stringify({ detail: 'Simulated GET failure' }),
              { status: 500, headers: { 'Content-Type': 'application/json' } }
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
    window.history.replaceState({}, '', window.location.pathname);
    invalidateApiCaches();
  });

  it('renders the summary strip counts and the unavailable count', async () => {
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
    const agentTool = {
      name: 'Bash',
      description: 'Run a shell command',
      source: 'agent',
      source_id: null,
      source_name: 'Agent',
      adapters: ['Claude Code'],
      parameters: {
        command: { type: 'string', description: 'Shell command' },
      },
      schema: {
        type: 'object',
        properties: {
          command: { type: 'string', description: 'Shell command' },
        },
      },
      is_enabled: true,
      is_supported: true,
      approval_workflow_id: null,
      has_approval_condition: false,
      config_id: null,
      access_rules: [],
      schema_tokens_estimate: 400,
    };

    fetchStub.callsFake(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString();
      if (url.endsWith('/api/v1/tools')) {
        return new Response(
          JSON.stringify([availableTool, unavailableTool, agentTool]),
          {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          }
        );
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
      () => el.shadowRoot?.querySelector('.summary-strip') !== null,
      'Summary strip did not render'
    );

    const summary = el.shadowRoot?.querySelector('.summary-strip');
    const stripText = summary?.textContent?.replace(/\s+/g, ' ') || '';
    expect(stripText).to.contain('2 tools');
    expect(stripText).to.contain('1 enabled');
    expect(stripText).to.contain('1 unavailable');
    expect(stripText).to.not.contain('toolss');
    expect(stripText).to.not.contain('Total tools');

    const tooltip = el.shadowRoot?.querySelector(
      '.summary-strip sl-tooltip'
    ) as HTMLElement | null;
    expect(tooltip).to.exist;
    expect(tooltip?.getAttribute('content')).to.contain(
      'Requires a connected tracker'
    );
    expect(el.shadowRoot?.querySelector('.unavailable-note')).to.equal(null);

    const unavailableButton = tooltip?.querySelector(
      '.strip-count'
    ) as HTMLButtonElement | null;
    expect(unavailableButton?.getAttribute('aria-pressed')).to.equal('false');

    const contextTax = el.shadowRoot?.querySelector('.context-tax');
    expect(contextTax).to.exist;
    // Only enabled+supported tools contribute (1200), not unavailable (800).
    expect(contextTax?.textContent?.replace(/\s+/g, ' ')).to.contain(
      '~1,200 tokens'
    );
    expect(contextTax?.textContent).to.contain('to every request');
    expect(
      el.shadowRoot?.querySelector('sl-tab[panel="native"]')?.textContent
    ).to.contain('Native tools 1');
    const toolbarCount = el.shadowRoot
      ?.querySelector('list-toolbar [slot="count"]')
      ?.textContent?.replace(/\s+/g, ' ');
    expect(toolbarCount).to.contain('2 tools');
  });

  it('renders the native tool approvals account-default card with override links', async () => {
    const element = await fixture<ToolsView>(html`<tools-view></tools-view>`);
    await waitUntil(
      () =>
        element.shadowRoot?.querySelector('#native-approvals-override-list') !=
        null,
      'override list did not render'
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
    expect(options.map((option) => option.getAttribute('value'))).to.deep.equal(
      ['', 'policy-2']
    );
    expect(options[0]?.textContent).to.include('Default workflow (Default)');
    expect(options[1]?.textContent).to.include('Security');
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
        element.shadowRoot?.querySelector('#native-approvals-override-list') !=
        null,
      'override list did not render'
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

  it('shows an inline error and reverts the switch when saving fails', async () => {
    const element = await fixture<ToolsView>(html`<tools-view></tools-view>`);
    await waitUntil(
      () =>
        !(element as any).loading &&
        (element as any).governanceDefaults !== null,
      'Tools view did not load governance defaults'
    );
    await element.updateComplete;

    failGovernancePut = true;

    const switchEl = element.shadowRoot?.querySelector(
      '#native-approvals-default-switch'
    ) as any;
    expect(switchEl).to.exist;
    expect(switchEl.checked).to.be.true;

    switchEl.checked = false;
    switchEl.dispatchEvent(new CustomEvent('sl-change'));
    await waitUntil(
      () => !(element as any).savingGovernanceDefaults,
      'Save did not finish'
    );
    await element.updateComplete;

    const saveError = element.shadowRoot?.querySelector('.governance-error');
    expect(saveError).to.exist;
    expect(saveError?.textContent).to.include('Could not save');
    expect(switchEl.checked).to.be.true;
    expect((element as any).error).to.equal(null);
  });

  it('reverts the workflow select when saving fails', async () => {
    const element = await fixture<ToolsView>(html`<tools-view></tools-view>`);
    await waitUntil(
      () =>
        !(element as any).loading &&
        (element as any).governanceDefaults !== null,
      'Tools view did not load governance defaults'
    );
    await element.updateComplete;

    failGovernancePut = true;

    const select = element.shadowRoot?.querySelector(
      '#native-approvals-default-workflow'
    ) as HTMLSelectElement;
    expect(select).to.exist;
    expect(select.value || '').to.equal('');

    select.value = 'policy-2';
    select.dispatchEvent(new CustomEvent('sl-change'));
    await waitUntil(
      () => !(element as any).savingGovernanceDefaults,
      'Save did not finish'
    );
    await element.updateComplete;

    expect(select.value || '').to.equal('');
    const saveError = element.shadowRoot?.querySelector('.governance-error');
    expect(saveError?.textContent).to.include('Could not save');
  });

  it('renders a retry fallback when governance defaults fail to load', async () => {
    failGovernanceGet = true;

    const element = await fixture<ToolsView>(html`<tools-view></tools-view>`);
    await waitUntil(
      () => !(element as any).loading && (element as any).governanceLoadFailed,
      'Governance load failure was not recorded'
    );
    await element.updateComplete;

    const card = element.shadowRoot?.querySelector(
      '#native-approvals-defaults-card'
    );
    expect(card).to.exist;
    expect(card?.textContent).to.include('Could not load this setting');

    const governanceGetCalls = () =>
      fetchStub
        .getCalls()
        .filter(
          (call) =>
            String(call.args[0]).endsWith(
              '/api/v1/account/governance-defaults'
            ) &&
            String(
              (call.args[1] as RequestInit | undefined)?.method || 'GET'
            ).toUpperCase() === 'GET'
        );
    expect(governanceGetCalls().length).to.equal(1);

    const retry = card?.querySelector('a') as HTMLElement;
    expect(retry).to.exist;
    expect(retry.textContent).to.include('Retry');
    retry.click();

    await waitUntil(
      () => (element as any).governanceDefaults !== null,
      'Retry did not reload governance defaults'
    );
    await element.updateComplete;

    expect(governanceGetCalls().length).to.equal(2);
    expect(
      element.shadowRoot?.querySelector('#native-approvals-default-switch')
    ).to.exist;
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

describe('ToolsView – tabs and toolbar', () => {
  let fetchStub: sinon.SinonStub;
  let tools: Record<string, unknown>[];
  let workflows: Record<string, unknown>[];

  function makeTool(
    overrides: Record<string, unknown> = {}
  ): Record<string, unknown> {
    return {
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
      access_rules: [],
      ...overrides,
    };
  }

  function stubLoadData() {
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
          return new Response(JSON.stringify([]), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        }
        if (url.endsWith('/api/v1/approval-workflows') && method === 'GET') {
          return new Response(JSON.stringify(workflows), {
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
        if (url.endsWith('/api/v1/account/governance-defaults')) {
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
        if (url.endsWith('/api/v1/tool-configurations') && method === 'POST') {
          return new Response(JSON.stringify({ id: 'cfg-native-1' }), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        }
        if (
          url.includes('/api/v1/tool-configurations/') &&
          url.endsWith('/access-rules') &&
          method === 'POST'
        ) {
          return new Response(
            JSON.stringify({ id: 'rule-native-1', action: 'require_approval' }),
            { status: 200, headers: { 'Content-Type': 'application/json' } }
          );
        }
        if (url.includes('/api/v1/tool-configurations/') && method === 'PUT') {
          return new Response(JSON.stringify({ id: 'cfg-native' }), {
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
    localStorage.removeItem('preloop.tools.view_mode');
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
    tools = [makeTool()];
    workflows = [
      {
        id: 'policy-1',
        name: 'Default',
        description: 'Default policy',
        approval_type: 'standard',
        is_default: true,
      },
      {
        id: 'policy-2',
        name: 'Security',
        description: 'Security policy',
        approval_type: 'standard',
        is_default: false,
      },
    ];
    fetchStub = sinon.stub(window, 'fetch');
    stubLoadData();
  });

  afterEach(() => {
    fetchStub.restore();
    localStorage.removeItem('preloop.tools.view_mode');
    localStorage.clear();
    window.history.replaceState({}, '', window.location.pathname);
    invalidateApiCaches();
  });

  it('defaults to the MCP tab and restores the native tab from localStorage', async () => {
    const el = (await fixture(html`<tools-view></tools-view>`)) as ToolsView;
    await waitUntil(() => !(el as any).loading, 'Still loading');
    await el.updateComplete;

    expect((el as any).activeTab).to.equal('mcp');
    const mcpTab = el.shadowRoot?.querySelector('sl-tab[panel="mcp"]');
    expect(mcpTab?.textContent).to.contain('MCP tools');
    el.remove();

    window.history.replaceState({}, '', window.location.pathname);
    localStorage.setItem('preloop.tools.tab', 'native');

    const restored = (await fixture(
      html`<tools-view></tools-view>`
    )) as ToolsView;
    await waitUntil(() => !(restored as any).loading, 'Still loading');
    await restored.updateComplete;

    expect((restored as any).activeTab).to.equal('native');
    expect(localStorage.getItem('preloop.tools.tab')).to.equal('native');
  });

  it('?tab=native opens the native tab', async () => {
    window.history.replaceState(
      {},
      '',
      `${window.location.pathname}?tab=native`
    );

    const el = (await fixture(html`<tools-view></tools-view>`)) as ToolsView;
    await waitUntil(() => !(el as any).loading, 'Still loading');
    await el.updateComplete;

    expect((el as any).activeTab).to.equal('native');
    expect(localStorage.getItem('preloop.tools.tab')).to.equal('native');
  });

  it('native tab renders the defaults strip with the three stable ids', async () => {
    window.history.replaceState(
      {},
      '',
      `${window.location.pathname}?tab=native`
    );

    const el = (await fixture(html`<tools-view></tools-view>`)) as ToolsView;
    await waitUntil(
      () => !(el as any).loading && (el as any).governanceDefaults !== null,
      'Governance defaults did not load'
    );
    await el.updateComplete;

    expect(el.shadowRoot?.querySelector('#native-approvals-defaults-card')).to
      .exist;
    expect(el.shadowRoot?.querySelector('#native-approvals-default-switch')).to
      .exist;
    expect(el.shadowRoot?.querySelector('#native-approvals-default-workflow'))
      .to.exist;
    expect((el as any).activeTab).to.equal('native');
    expect(el.shadowRoot?.textContent).to.contain(
      'No native tool calls have reached Preloop yet'
    );
    expect(
      el.shadowRoot?.querySelector('sl-tab[panel="native"]')?.textContent
    ).to.contain('Native tools 0');
  });

  it('toolbar search narrows the tool list', async () => {
    tools = [
      makeTool({ name: 'alpha_search', description: 'First' }),
      makeTool({ name: 'beta_other', description: 'Second' }),
    ];

    const el = (await fixture(html`<tools-view></tools-view>`)) as ToolsView;
    await waitUntil(
      () => (el as any).tools?.length === 2,
      'Tools did not load'
    );
    await el.updateComplete;

    const toolbar = el.shadowRoot?.querySelector('list-toolbar') as HTMLElement;
    expect(toolbar).to.exist;
    expect(
      el.shadowRoot
        ?.querySelector('sl-select.status-filter')
        ?.getAttribute('label')
    ).to.equal('Status');
    expect(
      el.shadowRoot
        ?.querySelector('sl-select.server-filter')
        ?.getAttribute('label')
    ).to.equal('Server');
    expect(
      el.shadowRoot
        ?.querySelector('sl-select.rules-filter')
        ?.getAttribute('label')
    ).to.equal('Rules');
    expect(
      el.shadowRoot
        ?.querySelector('sl-select.workflow-filter')
        ?.getAttribute('label')
    ).to.equal('Workflow');
    toolbar.dispatchEvent(
      new CustomEvent('search-change', {
        detail: { value: 'alpha' },
        bubbles: true,
        composed: true,
      })
    );
    await el.updateComplete;

    const names = ((el as any)._getFilteredTools() as { name: string }[]).map(
      (tool) => tool.name
    );
    expect(names).to.deep.equal(['alpha_search']);
  });

  it('workflow filter shows only tools whose rules use that workflow', async () => {
    tools = [
      makeTool({
        name: 'secured_tool',
        access_rules: [
          {
            id: 'rule-1',
            action: 'require_approval',
            is_enabled: true,
            approval_workflow_id: 'policy-2',
          },
        ],
      }),
      makeTool({ name: 'open_tool', access_rules: [] }),
    ];

    const el = (await fixture(html`<tools-view></tools-view>`)) as ToolsView;
    await waitUntil(
      () => (el as any).tools?.length === 2,
      'Tools did not load'
    );
    await el.updateComplete;

    const select = el.shadowRoot?.querySelector(
      'sl-select.workflow-filter'
    ) as HTMLSelectElement;
    expect(select).to.exist;
    select.value = 'policy-2';
    select.dispatchEvent(new CustomEvent('sl-change'));
    await el.updateComplete;

    const names = ((el as any)._getFilteredTools() as { name: string }[]).map(
      (tool) => tool.name
    );
    expect(names).to.deep.equal(['secured_tool']);
  });

  it('connect an agent opens the setup dialog', async () => {
    const el = (await fixture(html`<tools-view></tools-view>`)) as ToolsView;
    await waitUntil(() => !(el as any).loading, 'Still loading');
    await el.updateComplete;

    const button = Array.from(
      el.shadowRoot!.querySelectorAll('sl-button')
    ).find((item) => item.textContent?.includes('Connect an agent')) as
      HTMLElement | undefined;
    expect(button).to.exist;
    button!.click();
    await el.updateComplete;

    expect((el as any).showSetupDialog).to.equal(true);
    const dialog = el.shadowRoot?.querySelector('mcp-setup-dialog');
    expect(dialog).to.exist;
    expect((dialog as any).open).to.equal(true);
  });

  it('add MCP server opens the server form', async () => {
    const el = (await fixture(html`<tools-view></tools-view>`)) as ToolsView;
    await waitUntil(() => !(el as any).loading, 'Still loading');
    await el.updateComplete;

    const button = Array.from(
      el.shadowRoot!.querySelectorAll('sl-button')
    ).find((item) => item.textContent?.includes('Add MCP server')) as
      HTMLElement | undefined;
    expect(button).to.exist;
    button!.click();
    await el.updateComplete;

    expect((el as any).isAddingMCPServer).to.equal(true);
    expect(el.shadowRoot?.querySelector('mcp-server-form')).to.exist;
  });

  it('workflows menu opens the workflow dialog for edit and for new', async () => {
    const el = (await fixture(html`<tools-view></tools-view>`)) as ToolsView;
    await waitUntil(() => !(el as any).loading, 'Still loading');
    await el.updateComplete;

    const menu = el.shadowRoot?.querySelector('.workflows-menu sl-menu');
    expect(menu).to.exist;
    const editItem = menu?.querySelector(
      'sl-menu-item[data-workflow-edit="policy-2"]'
    ) as HTMLElement;
    expect(editItem).to.exist;
    editItem.click();
    await el.updateComplete;

    expect((el as any).showPolicyDialog).to.equal(true);
    expect((el as any).editingPolicy?.id).to.equal('policy-2');

    (el as any)._closePolicyDialog();
    await el.updateComplete;

    const newItem = menu?.querySelector(
      'sl-menu-item[data-workflow-new]'
    ) as HTMLElement;
    expect(newItem).to.exist;
    newItem.click();
    await el.updateComplete;

    expect((el as any).showPolicyDialog).to.equal(true);
    expect((el as any).editingPolicy).to.equal(null);
  });

  it('view toggle persists to preloop.tools.view_mode', async () => {
    const el = (await fixture(html`<tools-view></tools-view>`)) as ToolsView;
    await waitUntil(() => !(el as any).loading, 'Still loading');
    await el.updateComplete;

    const toolbar = el.shadowRoot?.querySelector(
      'list-toolbar'
    ) as HTMLElement & {
      view: string;
      views: string[];
    };
    expect(toolbar).to.exist;
    expect(toolbar.views).to.deep.equal(['cards']);
    expect(toolbar.view).to.equal('cards');
    expect(toolbar.shadowRoot?.querySelector('sl-button-group')).to.equal(null);

    toolbar.dispatchEvent(
      new CustomEvent('view-change', {
        detail: { value: 'list' },
        bubbles: true,
        composed: true,
      })
    );
    await el.updateComplete;

    expect(localStorage.getItem('preloop.tools.view_mode')).to.equal('list');
    expect((el as any).viewMode).to.equal('list');
  });

  it('strip counts set and clear the matching filter', async () => {
    const el = (await fixture(html`<tools-view></tools-view>`)) as ToolsView;
    await waitUntil(() => !(el as any).loading, 'Still loading');
    await el.updateComplete;

    const enabled = [
      ...(el.shadowRoot?.querySelectorAll('.strip-count') || []),
    ].find((button) => button.textContent?.includes('enabled')) as
      HTMLButtonElement | undefined;
    expect(enabled).to.exist;
    enabled!.click();
    await el.updateComplete;

    expect((el as any).filters.statuses).to.deep.equal(['enabled']);
    expect(enabled!.getAttribute('aria-pressed')).to.equal('true');

    const toolsCount = [
      ...(el.shadowRoot?.querySelectorAll('.strip-count') || []),
    ].find((button) => /^\s*\d+\s+tools\s*$/.test(button.textContent || '')) as
      HTMLButtonElement | undefined;
    expect(toolsCount).to.exist;
    toolsCount!.click();
    await el.updateComplete;

    expect((el as any).filters.statuses).to.deep.equal([]);
    expect(toolsCount!.getAttribute('aria-pressed')).to.equal('true');
    expect(enabled!.getAttribute('aria-pressed')).to.equal('false');
  });

  function makeNativeTool(
    overrides: Record<string, unknown> = {}
  ): Record<string, unknown> {
    return {
      name: 'Bash',
      description: 'Run a shell command inside the Claude Code agent.',
      source: 'agent',
      source_id: null,
      source_name: 'Agent',
      adapters: ['Claude Code'],
      parameters: {
        command: { type: 'string', description: 'Shell command to execute' },
        description: { type: 'string', description: 'Short explanation' },
        timeout: { type: 'integer', description: 'Timeout in milliseconds' },
      },
      schema: {
        type: 'object',
        properties: {
          command: { type: 'string', description: 'Shell command to execute' },
          description: { type: 'string', description: 'Short explanation' },
          timeout: { type: 'integer', description: 'Timeout in milliseconds' },
        },
      },
      is_enabled: true,
      is_supported: true,
      approval_workflow_id: null,
      has_approval_condition: false,
      config_id: null,
      access_rules: [],
      ...overrides,
    };
  }

  it('native tab lists agent-source tools from the API and opens the rule editor with the tool parameters', async () => {
    tools = [
      makeTool(),
      makeNativeTool(),
      makeNativeTool({
        name: 'Edit',
        adapters: ['Claude Code', 'Cursor'],
        parameters: {
          file_path: {
            type: 'string',
            description: 'Path of the file to edit',
          },
        },
        schema: {
          type: 'object',
          properties: {
            file_path: {
              type: 'string',
              description: 'Path of the file to edit',
            },
          },
        },
      }),
    ];
    window.history.replaceState(
      {},
      '',
      `${window.location.pathname}?tab=native`
    );

    const el = (await fixture(html`<tools-view></tools-view>`)) as ToolsView;
    await waitUntil(
      () => (el as any).tools?.length === 3,
      'Tools did not load'
    );
    await el.updateComplete;

    expect(
      el.shadowRoot?.querySelector('sl-tab[panel="native"]')?.textContent
    ).to.contain('Native tools 2');
    expect(
      el.shadowRoot?.querySelector('sl-tab[panel="mcp"]')?.textContent
    ).to.contain('MCP tools 1');
    expect(el.shadowRoot?.querySelector('.native-empty')).to.equal(null);
    expect(el.shadowRoot?.querySelector('.native-toolbar')).to.exist;
    const nativeToolbar = el.shadowRoot?.querySelector(
      '.native-toolbar list-toolbar'
    ) as HTMLElement & { views: string[]; view: string };
    expect(nativeToolbar.views).to.deep.equal(['cards']);
    expect(nativeToolbar.view).to.equal('cards');
    expect(nativeToolbar.shadowRoot?.querySelector('sl-button-group')).to.equal(
      null
    );

    const strip = el.shadowRoot?.querySelector('.summary-strip');
    expect(strip?.textContent?.replace(/\s+/g, ' ')).to.contain('1 tools');

    const nativeEditor = el.shadowRoot?.querySelector(
      'sl-tab-panel[name="native"] tools-editor-component'
    ) as LitElement & { family: string; tools: { name: string }[] };
    expect(nativeEditor).to.exist;
    expect(nativeEditor.family).to.equal('native');
    expect(nativeEditor.tools.map((tool) => tool.name)).to.deep.equal([
      'Bash',
      'Edit',
    ]);
    expect(nativeEditor.shadowRoot?.textContent).to.contain('Claude Code');

    const bashItem = [
      ...(nativeEditor.shadowRoot?.querySelectorAll('tool-list-item') || []),
    ].find(
      (item) =>
        (item as LitElement & { tool?: { name: string } }).tool?.name === 'Bash'
    ) as (LitElement & { tool?: { name: string } }) | undefined;
    expect(bashItem).to.exist;
    (
      bashItem!.shadowRoot?.querySelector('.tool-header') as HTMLElement
    ).click();
    await el.updateComplete;
    await nativeEditor.updateComplete;
    await bashItem!.updateComplete;

    const ruleSet = bashItem!.shadowRoot?.querySelector(
      'governance-rule-set-editor'
    ) as LitElement & {
      toolSchema?: { properties?: Record<string, unknown> };
    };
    expect(ruleSet).to.exist;
    expect(Object.keys(ruleSet.toolSchema?.properties || {})).to.deep.equal([
      'command',
      'description',
      'timeout',
    ]);

    const addRule = [
      ...(ruleSet.shadowRoot?.querySelectorAll('sl-button') || []),
    ].find((button) => button.textContent?.includes('Add Rule')) as
      (HTMLElement & { click: () => void }) | undefined;
    expect(addRule).to.exist;
    await waitUntil(
      () => Boolean(addRule!.shadowRoot?.querySelector('button')),
      'Add Rule button did not upgrade'
    );
    addRule!.click();
    await waitUntil(
      () =>
        (
          ruleSet.shadowRoot?.querySelector('tool-rule-editor') as {
            open?: boolean;
          } | null
        )?.open === true,
      'Rule editor did not open'
    );
    await ruleSet.updateComplete;

    const ruleEditor = ruleSet.shadowRoot?.querySelector('tool-rule-editor') as
      | (HTMLElement & {
          open?: boolean;
          toolSchema?: { properties?: Record<string, unknown> };
        })
      | null;
    expect(ruleEditor?.open).to.equal(true);
    expect(Object.keys(ruleEditor?.toolSchema?.properties || {})).to.include(
      'command'
    );

    await waitUntil(() => {
      const select = ruleEditor?.shadowRoot?.querySelector(
        'sl-select.param-select'
      );
      return Boolean(select);
    }, 'Parameter select did not render');
    const options = [
      ...(ruleEditor!.shadowRoot?.querySelectorAll(
        'sl-select.param-select sl-option'
      ) || []),
    ].map((option) => option.getAttribute('value'));
    expect(options).to.deep.equal(['command', 'description', 'timeout']);
  });

  it('saving a native rule creates an agent-source configuration without a server id', async () => {
    tools = [makeTool(), makeNativeTool()];
    window.history.replaceState(
      {},
      '',
      `${window.location.pathname}?tab=native`
    );

    const el = (await fixture(html`<tools-view></tools-view>`)) as ToolsView;
    await waitUntil(
      () => (el as any).tools?.length === 2,
      'Tools did not load'
    );
    await el.updateComplete;

    const nativeEditor = el.shadowRoot?.querySelector(
      'sl-tab-panel[name="native"] tools-editor-component'
    ) as LitElement;
    const bashItem = [
      ...(nativeEditor.shadowRoot?.querySelectorAll('tool-list-item') || []),
    ].find(
      (item) =>
        (item as LitElement & { tool?: { name: string } }).tool?.name === 'Bash'
    ) as LitElement;
    expect(bashItem).to.exist;

    bashItem.dispatchEvent(
      new CustomEvent('save-rule', {
        detail: {
          tool: (el as any).tools.find(
            (tool: { name: string }) => tool.name === 'Bash'
          ),
          existingRule: null,
          formData: {
            action: 'require_approval',
            condition_expression: 'args.command.matches("rm -rf")',
            condition_type: 'simple',
            description: 'Block recursive deletes',
            is_enabled: true,
            approval_workflow_id: 'policy-1',
          },
        },
        bubbles: true,
        composed: true,
      })
    );

    await waitUntil(() => {
      return fetchStub.getCalls().some((call) => {
        const url = String(call.args[0]);
        const method = String(
          (call.args[1] as RequestInit | undefined)?.method || 'GET'
        ).toUpperCase();
        return url.endsWith('/api/v1/tool-configurations') && method === 'POST';
      });
    }, 'Native tool configuration was not created');

    const createCall = fetchStub.getCalls().find((call) => {
      const url = String(call.args[0]);
      const method = String(
        (call.args[1] as RequestInit | undefined)?.method || 'GET'
      ).toUpperCase();
      return url.endsWith('/api/v1/tool-configurations') && method === 'POST';
    });
    const body = JSON.parse(
      String((createCall?.args[1] as RequestInit | undefined)?.body || '{}')
    );
    expect(body.tool_source).to.equal('agent');
    expect(body.mcp_server_id).to.equal(null);
    expect(body.tool_name).to.equal('Bash');
  });

  it('blocked switch sends is_enabled=false', async () => {
    tools = [
      makeTool(),
      makeNativeTool({
        config_id: 'cfg-native',
        is_enabled: true,
      }),
    ];
    window.history.replaceState(
      {},
      '',
      `${window.location.pathname}?tab=native`
    );

    const el = (await fixture(html`<tools-view></tools-view>`)) as ToolsView;
    await waitUntil(
      () => (el as any).tools?.length === 2,
      'Tools did not load'
    );
    await el.updateComplete;

    const nativeEditor = el.shadowRoot?.querySelector(
      'sl-tab-panel[name="native"] tools-editor-component'
    ) as LitElement;
    const bashItem = [
      ...(nativeEditor.shadowRoot?.querySelectorAll('tool-list-item') || []),
    ].find(
      (item) =>
        (item as LitElement & { tool?: { name: string } }).tool?.name === 'Bash'
    ) as LitElement;
    expect(bashItem).to.exist;
    expect(bashItem.shadowRoot?.querySelector('sl-menu-item')).to.equal(null);

    const blockedSwitch = bashItem.shadowRoot?.querySelector('sl-switch') as
      (HTMLElement & { checked: boolean }) | null;
    expect(blockedSwitch).to.exist;
    expect(blockedSwitch!.textContent).to.contain('Blocked');
    expect(blockedSwitch!.checked).to.equal(false);

    blockedSwitch!.checked = true;
    blockedSwitch!.dispatchEvent(
      new CustomEvent('sl-change', { bubbles: true })
    );

    await waitUntil(() => {
      return fetchStub.getCalls().some((call) => {
        const url = String(call.args[0]);
        const method = String(
          (call.args[1] as RequestInit | undefined)?.method || 'GET'
        ).toUpperCase();
        return (
          url.includes('/api/v1/tool-configurations/cfg-native') &&
          method === 'PUT'
        );
      });
    }, 'Blocked switch did not update the configuration');

    const updateCall = fetchStub.getCalls().find((call) => {
      const url = String(call.args[0]);
      const method = String(
        (call.args[1] as RequestInit | undefined)?.method || 'GET'
      ).toUpperCase();
      return (
        url.includes('/api/v1/tool-configurations/cfg-native') &&
        method === 'PUT'
      );
    });
    const body = JSON.parse(
      String((updateCall?.args[1] as RequestInit | undefined)?.body || '{}')
    );
    expect(body.is_enabled).to.equal(false);
  });

  it('blocked switch on a native tool without config_id creates an agent config', async () => {
    tools = [makeTool(), makeNativeTool({ is_enabled: true })];
    window.history.replaceState(
      {},
      '',
      `${window.location.pathname}?tab=native`
    );

    const el = (await fixture(html`<tools-view></tools-view>`)) as ToolsView;
    await waitUntil(
      () => (el as any).tools?.length === 2,
      'Tools did not load'
    );
    await el.updateComplete;

    const nativeEditor = el.shadowRoot?.querySelector(
      'sl-tab-panel[name="native"] tools-editor-component'
    ) as LitElement;
    const bashItem = [
      ...(nativeEditor.shadowRoot?.querySelectorAll('tool-list-item') || []),
    ].find(
      (item) =>
        (item as LitElement & { tool?: { name: string } }).tool?.name === 'Bash'
    ) as LitElement;
    expect(bashItem).to.exist;

    const blockedSwitch = bashItem.shadowRoot?.querySelector('sl-switch') as
      (HTMLElement & { checked: boolean }) | null;
    expect(blockedSwitch).to.exist;
    blockedSwitch!.checked = true;
    blockedSwitch!.dispatchEvent(
      new CustomEvent('sl-change', { bubbles: true })
    );

    await waitUntil(() => {
      return fetchStub.getCalls().some((call) => {
        const url = String(call.args[0]);
        const method = String(
          (call.args[1] as RequestInit | undefined)?.method || 'GET'
        ).toUpperCase();
        return url.endsWith('/api/v1/tool-configurations') && method === 'POST';
      });
    }, 'Blocked switch did not create a configuration');

    const createCall = fetchStub.getCalls().find((call) => {
      const url = String(call.args[0]);
      const method = String(
        (call.args[1] as RequestInit | undefined)?.method || 'GET'
      ).toUpperCase();
      return url.endsWith('/api/v1/tool-configurations') && method === 'POST';
    });
    const body = JSON.parse(
      String((createCall?.args[1] as RequestInit | undefined)?.body || '{}')
    );
    expect(body.tool_source).to.equal('agent');
    expect(body.mcp_server_id).to.equal(null);
    expect(body.tool_name).to.equal('Bash');
    expect(body.is_enabled).to.equal(false);
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
        if (
          url.includes('/api/v1/mcp-servers/') &&
          url.endsWith('/scan') &&
          method === 'POST'
        ) {
          return new Response(JSON.stringify({ scanned: true }), {
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
    expect(body.scope_mcp_server_name).to.equal('GitHub MCP');
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

  it('refresh scans the server and does not open the starter policy dialog', async () => {
    servers = [makeServer('srv-1', '2026-02-01T00:00:00Z')];
    tools = [makeTool('srv-1')];

    const el = (await fixture(html`<tools-view></tools-view>`)) as ToolsView;
    await waitUntil(() => !(el as any).loading, 'Initial load did not finish');
    await el.updateComplete;

    const editor = el.shadowRoot?.querySelector(
      'tools-editor-component'
    ) as HTMLElement;
    const refresh = editor.shadowRoot?.querySelector(
      'sl-icon-button[name="arrow-clockwise"]'
    ) as HTMLElement | null;
    expect(refresh).to.exist;
    refresh!.click();

    await waitUntil(
      () =>
        fetchStub
          .getCalls()
          .some(
            (call) =>
              String(call.args[0]).includes('/api/v1/mcp-servers/srv-1/scan') &&
              String(
                (call.args[1] as RequestInit | undefined)?.method || 'GET'
              ).toUpperCase() === 'POST'
          ),
      'Scan request was not made'
    );
    await waitUntil(
      () => !(el as any).loading,
      'Reload after scan did not finish'
    );
    await el.updateComplete;

    expect((el as any).showStarterPolicyDialog).to.equal(false);
    expect((el as any)._pendingStarterPolicyServerId).to.equal(null);
    expect((el as any)._pendingStarterPolicyFallbackToLatest).to.equal(false);
    const generateCall = fetchStub
      .getCalls()
      .find((call) =>
        String(call.args[0]).endsWith('/api/v1/policies/generate')
      );
    expect(generateCall).to.equal(undefined);
  });
});
