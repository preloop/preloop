import { html, fixture, expect } from '@open-wc/testing';
import { setViewport } from '@web/test-runner-commands';
import sinon from 'sinon';

import './tool-list-item';
import type { ToolListItem } from './tool-list-item';

/**
 * Regression tests for per-tool justification settings in tool-list-item.
 *
 * Covers:
 *  - Opening the justification dialog populates current mode
 *  - Saving with an existing config_id calls updateToolConfiguration
 *  - Saving without config_id creates a new configuration first
 *  - Disabled mode sends null as justification_mode
 *  - tool-updated event fires after save
 */
describe('ToolListItem – justification settings', () => {
  let fetchStub: sinon.SinonStub;

  const baseTool = {
    name: 'bash',
    description: 'Execute shell commands',
    source: 'builtin' as const,
    source_id: null,
    source_name: 'Built-in',
    schema: {},
    is_enabled: true,
    is_supported: true,
    approval_workflow_id: null,
    has_approval_condition: false,
    config_id: null as string | null,
    justification_mode: null as string | null,
  };

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
    fetchStub = sinon.stub(window, 'fetch');
  });

  afterEach(() => {
    fetchStub.restore();
    localStorage.clear();
  });

  // ---------------------------------------------------------------------------
  // Helpers
  // ---------------------------------------------------------------------------

  function stubApi(opts?: { configId?: string }) {
    fetchStub.callsFake(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();
        const method = (init?.method || 'GET').toUpperCase();

        // Create tool configuration
        if (url.endsWith('/api/v1/tool-configurations') && method === 'POST') {
          return new Response(
            JSON.stringify({ id: opts?.configId || 'new-cfg-1' }),
            { status: 200, headers: { 'Content-Type': 'application/json' } }
          );
        }

        // Update tool configuration
        if (url.includes('/api/v1/tool-configurations/') && method === 'PUT') {
          return new Response(
            JSON.stringify({ id: opts?.configId || 'cfg-1' }),
            {
              status: 200,
              headers: { 'Content-Type': 'application/json' },
            }
          );
        }

        return new Response(JSON.stringify({}), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
    );
  }

  async function createItem(toolOverrides: Partial<typeof baseTool> = {}) {
    const tool = { ...baseTool, ...toolOverrides };
    const el = (await fixture(
      html`<tool-list-item
        .tool=${tool}
        .accessRules=${[]}
        .policies=${[]}
        .features=${{}}
      ></tool-list-item>`
    )) as ToolListItem;
    await el.updateComplete;
    return el;
  }

  // ---------------------------------------------------------------------------
  // Tests
  // ---------------------------------------------------------------------------

  it('opens justification dialog with current mode populated', async () => {
    stubApi();
    const el = await createItem({ justification_mode: 'required' });

    (el as any)._openJustificationDialog();
    await el.updateComplete;

    expect((el as any)._showJustificationDialog).to.be.true;
    expect((el as any)._justificationMode).to.equal('required');
  });

  it('defaults justification mode to disabled when tool has no mode', async () => {
    stubApi();
    const el = await createItem({ justification_mode: null });

    (el as any)._openJustificationDialog();
    await el.updateComplete;

    expect((el as any)._justificationMode).to.equal('disabled');
  });

  it('creates new config when saving justification on tool without config_id', async () => {
    stubApi({ configId: 'new-cfg-1' });
    const el = await createItem({ config_id: null });

    (el as any)._justificationMode = 'required';
    await (el as any)._saveJustificationMode();

    const createCall = fetchStub.getCalls().find((c) => {
      const url = String(c.args[0]);
      const method = String(
        (c.args[1] as RequestInit | undefined)?.method || 'GET'
      ).toUpperCase();
      return url.endsWith('/api/v1/tool-configurations') && method === 'POST';
    });

    expect(createCall).to.exist;
    const body = JSON.parse(
      (createCall!.args[1] as RequestInit).body as string
    );
    expect(body.justification_mode).to.equal('required');
    expect(body.tool_name).to.equal('bash');
  });

  it('updates existing config when config_id is present', async () => {
    stubApi({ configId: 'cfg-existing' });
    const el = await createItem({ config_id: 'cfg-existing' });

    (el as any)._justificationMode = 'optional';
    await (el as any)._saveJustificationMode();

    const updateCall = fetchStub.getCalls().find((c) => {
      const url = String(c.args[0]);
      const method = String(
        (c.args[1] as RequestInit | undefined)?.method || 'GET'
      ).toUpperCase();
      return (
        url.includes('/api/v1/tool-configurations/cfg-existing') &&
        method === 'PUT'
      );
    });

    expect(updateCall).to.exist;
    const body = JSON.parse(
      (updateCall!.args[1] as RequestInit).body as string
    );
    expect(body.justification_mode).to.equal('optional');
  });

  it('sends null justification_mode when mode is disabled', async () => {
    stubApi({ configId: 'cfg-1' });
    const el = await createItem({ config_id: 'cfg-1' });

    (el as any)._justificationMode = 'disabled';
    await (el as any)._saveJustificationMode();

    const updateCall = fetchStub.getCalls().find((c) => {
      const url = String(c.args[0]);
      const method = String(
        (c.args[1] as RequestInit | undefined)?.method || 'GET'
      ).toUpperCase();
      return url.includes('/api/v1/tool-configurations/') && method === 'PUT';
    });

    expect(updateCall).to.exist;
    const body = JSON.parse(
      (updateCall!.args[1] as RequestInit).body as string
    );
    expect(body.justification_mode).to.be.null;
  });

  it('dispatches tool-updated event after save', async () => {
    stubApi({ configId: 'cfg-1' });
    const el = await createItem({ config_id: 'cfg-1' });

    let eventFired = false;
    el.addEventListener('tool-updated', () => {
      eventFired = true;
    });

    (el as any)._justificationMode = 'required';
    await (el as any)._saveJustificationMode();

    expect(eventFired).to.be.true;
  });

  it('closes dialog after successful save', async () => {
    stubApi({ configId: 'cfg-1' });
    const el = await createItem({ config_id: 'cfg-1' });

    (el as any)._showJustificationDialog = true;
    (el as any)._justificationMode = 'optional';
    await (el as any)._saveJustificationMode();

    expect((el as any)._showJustificationDialog).to.be.false;
  });

  // ---------------------------------------------------------------------------
  // B-T1 / B-T4: a native row states the effective policy, and keeps its name
  // at phone width.
  // ---------------------------------------------------------------------------

  async function createNativeItem(
    toolOverrides: Partial<typeof baseTool> = {},
    accountAsksByDefault: boolean | null = false
  ) {
    const tool = {
      ...baseTool,
      name: 'Bash',
      source: 'agent',
      source_name: 'Claude Code',
      adapters: ['Claude Code', 'OpenCode'],
      ...toolOverrides,
    };
    const el = (await fixture(
      html`<tool-list-item
        .tool=${tool}
        .accessRules=${[]}
        .policies=${[]}
        .features=${{}}
        .accountAsksByDefault=${accountAsksByDefault}
      ></tool-list-item>`
    )) as ToolListItem;
    await el.updateComplete;
    return el;
  }

  function ruleSummaryText(el: ToolListItem) {
    return el.shadowRoot
      ?.querySelector('.no-rules')
      ?.textContent?.replace(/\s+/g, ' ')
      .trim();
  }

  it('says a ruleless native tool asks a human when the account default is on', async () => {
    stubApi();
    const el = await createNativeItem({ is_enabled: true }, true);

    expect(ruleSummaryText(el)).to.equal(
      'No rules · asks a human (account default)'
    );
    expect((el as any)._emptyRulesMessage()).to.contain(
      'ask a human first, from the account default'
    );
  });

  it('says a ruleless native tool is allowed when the account default is off', async () => {
    stubApi();
    const el = await createNativeItem({ is_enabled: true }, false);

    expect(ruleSummaryText(el)).to.equal('No rules · allowed');
    expect((el as any)._emptyRulesMessage()).to.contain(
      'All calls to this tool are allowed'
    );
  });

  it('says only No rules when the account default is unread', async () => {
    stubApi();
    const el = await createNativeItem({ is_enabled: true }, null);

    expect(ruleSummaryText(el)).to.equal('No rules');
    expect(ruleSummaryText(el)).to.not.include('allowed');
    expect(ruleSummaryText(el)).to.not.include('asks a human');
    expect((el as any)._emptyRulesMessage()).to.equal(
      'No access rules configured.'
    );
    expect((el as any)._emptyRulesMessage()).to.not.include('allowed');
    expect((el as any)._emptyRulesMessage()).to.not.include('ask a human');
  });

  it('says a ruleless native tool is blocked when the switch is on', async () => {
    stubApi();
    const el = await createNativeItem({ is_enabled: false }, true);

    expect(ruleSummaryText(el)).to.equal('No rules · blocked');
  });

  it('labels the native switch with the verb Block', async () => {
    stubApi();
    const el = await createNativeItem();

    const label = el.shadowRoot
      ?.querySelector('.tool-toggle sl-switch')
      ?.textContent?.trim();
    expect(label).to.equal('Block');
  });

  it('keeps the tool name in the row header at 390px', async () => {
    stubApi();
    await setViewport({ width: 390, height: 844 });
    const el = await createNativeItem({ is_enabled: true }, true);
    await el.updateComplete;

    const name = el.shadowRoot?.querySelector('.tool-name') as HTMLElement;
    const badges = el.shadowRoot?.querySelector('.tool-badges') as HTMLElement;
    expect(name.textContent?.trim()).to.equal('Bash');
    expect(name.getBoundingClientRect().width).to.be.greaterThan(20);
    // Tags sit on their own line beneath the name, not beside it.
    expect(badges.getBoundingClientRect().top).to.be.greaterThan(
      name.getBoundingClientRect().top
    );

    await setViewport({ width: 1280, height: 800 });
  });

  it('keeps the MCP tool settings menu right of the name at 390px', async () => {
    stubApi();
    await setViewport({ width: 390, height: 844 });
    const el = await createItem({
      source: 'mcp',
      source_name: 'GitHub',
    } as any);
    await el.updateComplete;

    const name = el.shadowRoot?.querySelector('.tool-name') as HTMLElement;
    const menu = el.shadowRoot?.querySelector('.tool-menu') as HTMLElement;
    expect(menu).to.exist;
    // The wrapper has no order of its own without .tool-menu, so it would
    // sort with the chevron and land left of the name.
    expect(menu.getBoundingClientRect().left).to.be.greaterThan(
      name.getBoundingClientRect().left
    );

    await setViewport({ width: 1280, height: 800 });
  });

  it('shows per-tool schema token estimate', async () => {
    stubApi();
    const el = await createItem({ schema_tokens_estimate: 245 } as any);
    await el.updateComplete;

    const badge = el.shadowRoot?.querySelector('.schema-tokens');
    expect(badge).to.exist;
    expect(badge!.textContent?.replace(/\s+/g, ' ').trim()).to.equal(
      '~245 tokens/request'
    );
  });
});
