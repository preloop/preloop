import { html, fixture, expect, oneEvent } from '@open-wc/testing';
import sinon from 'sinon';

import './mcp-server-form';
import type { MCPServerForm } from './mcp-server-form';

describe('MCPServerForm', () => {
  let fetchStub: sinon.SinonStub;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
    fetchStub = sinon.stub(window, 'fetch');
    // Default stub so any unexpected fetch resolves; individual tests override via stubApi()
    fetchStub.callsFake(
      async () =>
        new Response(JSON.stringify({}), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
    );
  });

  afterEach(() => {
    fetchStub.restore();
    localStorage.clear();
  });

  function stubApi(opts?: { createId?: string; updateId?: string }) {
    fetchStub.callsFake(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();
        const method = (init?.method || 'GET').toUpperCase();

        if (url.includes('/api/v1/mcp-servers') && method === 'POST') {
          return new Response(
            JSON.stringify({
              id: opts?.createId || 'new-server-1',
              name: 'Test Server',
              url: 'http://localhost:8001',
            }),
            { status: 200, headers: { 'Content-Type': 'application/json' } }
          );
        }

        if (url.match(/\/api\/v1\/mcp-servers\/[^/]+$/) && method === 'PUT') {
          return new Response(
            JSON.stringify({
              id: opts?.updateId || 'server-1',
              name: 'Updated Server',
              url: 'http://localhost:8002',
            }),
            { status: 200, headers: { 'Content-Type': 'application/json' } }
          );
        }

        if (url.includes('/api/v1/auth/refresh') && method === 'POST') {
          return new Response(
            JSON.stringify({
              access_token: 'new-access-token',
              refresh_token: 'new-refresh-token',
            }),
            { status: 200, headers: { 'Content-Type': 'application/json' } }
          );
        }

        return new Response(JSON.stringify({}), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
    );
  }

  async function createForm(server: object | null = null) {
    const el = (await fixture(
      html`<mcp-server-form
        .server=${server}
        .opened=${true}
      ></mcp-server-form>`
    )) as MCPServerForm;
    await el.updateComplete;
    return el;
  }

  describe('Form rendering', () => {
    it('renders Add MCP Server dialog when server is null', async () => {
      const el = await createForm(null);
      const dialog = el.shadowRoot?.querySelector('sl-dialog');
      expect(dialog).to.exist;
      expect(dialog?.getAttribute('label')).to.equal('Add MCP Server');

      const nameInput = el.shadowRoot?.querySelector('sl-input[name="name"]');
      expect(nameInput).to.exist;
      expect((nameInput as any)?.value).to.equal('');

      const urlInput = el.shadowRoot?.querySelector('sl-input[name="url"]');
      expect(urlInput).to.exist;
      expect((urlInput as any)?.value).to.equal('');

      const addButton = el.shadowRoot?.querySelector(
        'sl-button[variant="primary"]'
      );
      expect(addButton?.textContent?.trim()).to.equal('Add');
    });

    it('renders Edit MCP Server dialog when server is provided', async () => {
      const server = {
        id: 'server-1',
        name: 'My Server',
        url: 'http://localhost:8001',
        transport: 'http-streaming',
        auth_type: 'none',
      };
      const el = await createForm(server);
      const dialog = el.shadowRoot?.querySelector('sl-dialog');
      expect(dialog).to.exist;
      expect(dialog?.getAttribute('label')).to.equal('Edit MCP Server');

      const nameInput = el.shadowRoot?.querySelector('sl-input[name="name"]');
      expect((nameInput as any)?.value).to.equal('My Server');

      const urlInput = el.shadowRoot?.querySelector('sl-input[name="url"]');
      expect((urlInput as any)?.value).to.equal('http://localhost:8001');

      const saveButton = el.shadowRoot?.querySelector(
        'sl-button[variant="primary"]'
      );
      expect(saveButton?.textContent?.trim()).to.equal('Save');
    });

    it('shows bearer token input when auth_type is bearer', async () => {
      const server = {
        id: 'server-1',
        name: 'My Server',
        url: 'http://localhost:8001',
        auth_type: 'bearer',
        auth_config: { token: 'secret-token' },
      };
      const el = await createForm(server);
      const bearerInput = el.shadowRoot?.querySelector(
        'sl-input[name="bearer_token"]'
      );
      expect(bearerInput).to.exist;
      expect((bearerInput as any)?.value).to.equal('secret-token');
    });
  });

  describe('Validation', () => {
    it('shows error when server name is empty on submit', async () => {
      const el = await createForm(null);
      (el as any).serverUrl = 'http://localhost:8001';
      (el as any).serverName = '';

      await (el as any).handleSave();
      await el.updateComplete;

      expect((el as any).errorMessage).to.equal('Server name is required');
      expect(fetchStub).not.to.have.been.called;
      // Announced to screen readers, not only painted red.
      const error = el.shadowRoot?.querySelector('.error');
      expect(error?.getAttribute('role')).to.equal('alert');
    });

    it('explains the transport in plain words, without roadmap jargon', async () => {
      const el = await createForm(null);
      const text = el.shadowRoot?.textContent ?? '';
      expect(text).to.not.contain('Phase 1B');
      expect(text).to.contain('Streamable HTTP');
      // No disabled field that cannot be changed.
      expect(el.shadowRoot?.querySelector('sl-input[name="transport"]')).to.not
        .exist;
    });

    it('shows error when server URL is empty on submit', async () => {
      const el = await createForm(null);
      (el as any).serverName = 'Test Server';
      (el as any).serverUrl = '';

      await (el as any).handleSave();
      await el.updateComplete;

      expect((el as any).errorMessage).to.equal('Server URL is required');
      expect(fetchStub).not.to.have.been.called;
    });

    it('shows error when bearer auth selected but token is empty', async () => {
      const el = await createForm(null);
      (el as any).serverName = 'Test Server';
      (el as any).serverUrl = 'http://localhost:8001';
      (el as any).authType = 'bearer';
      (el as any).bearerToken = '';

      await (el as any).handleSave();
      await el.updateComplete;

      expect((el as any).errorMessage).to.equal(
        'Bearer token is required when using bearer authentication'
      );
      expect(fetchStub).not.to.have.been.called;
    });
  });

  describe('Submit', () => {
    it('calls createMCPServer and dispatches server-added when adding', async () => {
      stubApi({ createId: 'new-123' });
      const el = await createForm(null);
      (el as any).serverName = 'New Server';
      (el as any).serverUrl = 'http://localhost:8001';

      const listener = oneEvent(el, 'server-added');
      await (el as any).handleSave();
      const { detail } = await listener;

      const createCall = fetchStub
        .getCalls()
        .find(
          (c) =>
            String(c.args[0]).includes('/api/v1/mcp-servers') &&
            (c.args[1] as RequestInit)?.method === 'POST'
        );
      expect(createCall).to.exist;
      const body = JSON.parse(
        (createCall!.args[1] as RequestInit).body as string
      );
      expect(body.name).to.equal('New Server');
      expect(body.url).to.equal('http://localhost:8001');
      expect(detail.server.id).to.equal('new-123');
    });

    it('calls updateMCPServer and dispatches server-updated when editing', async () => {
      stubApi({ updateId: 'server-1' });
      const server = {
        id: 'server-1',
        name: 'Old Name',
        url: 'http://localhost:8001',
        transport: 'http-streaming',
        auth_type: 'none',
      };
      const el = await createForm(server);
      (el as any).serverName = 'Updated Name';
      (el as any).serverUrl = 'http://localhost:8002';

      const listener = oneEvent(el, 'server-updated');
      await (el as any).handleSave();
      const { detail } = await listener;

      const updateCall = fetchStub
        .getCalls()
        .find(
          (c) =>
            String(c.args[0]).includes('/api/v1/mcp-servers/server-1') &&
            (c.args[1] as RequestInit)?.method === 'PUT'
        );
      expect(updateCall).to.exist;
      const body = JSON.parse(
        (updateCall!.args[1] as RequestInit).body as string
      );
      expect(body.name).to.equal('Updated Name');
      expect(detail.server.id).to.equal('server-1');
    });

    it('dispatches close-modal with success=true after successful save', async () => {
      stubApi();
      const el = await createForm(null);
      (el as any).serverName = 'New Server';
      (el as any).serverUrl = 'http://localhost:8001';

      const listener = oneEvent(el, 'close-modal');
      await (el as any).handleSave();
      const { detail } = await listener;

      expect(detail.success).to.be.true;
    });

    it('shows error message when API fails', async () => {
      fetchStub.callsFake(
        async (input: RequestInfo | URL, init?: RequestInit) => {
          const url = typeof input === 'string' ? input : input.toString();
          const method = (init?.method || 'GET').toUpperCase();
          if (url.includes('/api/v1/mcp-servers') && method === 'POST') {
            return new Response(
              JSON.stringify({ detail: 'Server already exists' }),
              { status: 400, headers: { 'Content-Type': 'application/json' } }
            );
          }
          return new Response(JSON.stringify({}), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        }
      );

      const el = await createForm(null);
      (el as any).serverName = 'New Server';
      (el as any).serverUrl = 'http://localhost:8001';

      await (el as any).handleSave();
      await el.updateComplete;

      expect((el as any).errorMessage).to.be.a('string').and.not.be.empty;
    });
  });

  describe('Close modal', () => {
    it('dispatches close-modal event on cancel', async () => {
      const el = await createForm(null);
      const cancelButton = el.shadowRoot?.querySelector(
        'sl-button:not([variant="primary"])'
      );
      const listener = oneEvent(el, 'close-modal');
      cancelButton?.click();
      await listener;
    });

    it('dispatches close-modal on sl-request-close', async () => {
      const el = await createForm(null);
      const dialog = el.shadowRoot?.querySelector('sl-dialog');
      const listener = oneEvent(el, 'close-modal');
      dialog?.dispatchEvent(new CustomEvent('sl-request-close'));
      await listener;
    });
  });
  describe('Tool prefix and collisions (#1135)', () => {
    it('sends an explicit prefix and null when left empty', async () => {
      stubApi();
      const el = await createForm(null);
      (el as any).serverName = 'crm';
      (el as any).serverUrl = 'http://localhost:8001';
      await (el as any).handleSave();
      const first = fetchStub
        .getCalls()
        .find((c) => String(c.args[0]).includes('/api/v1/mcp-servers'));
      expect(JSON.parse(first!.args[1].body).tool_prefix).to.equal(null);

      fetchStub.resetHistory();
      const second = await createForm(null);
      (second as any).serverName = 'crm';
      (second as any).serverUrl = 'http://localhost:8001';
      (second as any).toolPrefix = 'crm';
      await (second as any).handleSave();
      const call = fetchStub
        .getCalls()
        .find((c) => String(c.args[0]).includes('/api/v1/mcp-servers'));
      expect(JSON.parse(call!.args[1].body).tool_prefix).to.equal('crm');
    });

    it('rejects an invalid prefix before any request', async () => {
      const el = await createForm(null);
      (el as any).serverName = 'crm';
      (el as any).serverUrl = 'http://localhost:8001';
      (el as any).toolPrefix = 'CRM-1';
      await (el as any).handleSave();
      expect((el as any).errorMessage).to.contain('Tool prefix');
      expect(fetchStub).not.to.have.been.called;
    });

    it('shows the server warnings in the edit dialog', async () => {
      const warning =
        "Tool 'read_scope' on MCP server 'newer' is shadowed by MCP server 'older'.";
      const el = await createForm({
        id: 'server-2',
        name: 'newer',
        url: 'http://localhost:8002',
        auth_type: 'none',
        tool_prefix: null,
        warnings: [warning],
      });
      const box = el.shadowRoot?.querySelector(
        '[data-testid="mcp-server-warnings"]'
      );
      expect(box?.textContent).to.contain(warning);
      const prefix = el.shadowRoot?.querySelector(
        'sl-input[name="tool_prefix"]'
      );
      expect((prefix as any)?.value).to.equal('');
    });
  });
});
