import { html, fixture, expect } from '@open-wc/testing';
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
});
