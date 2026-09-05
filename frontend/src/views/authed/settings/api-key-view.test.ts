import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import './api-key-view';
import type { ApiKeyView } from './api-key-view';
import { unifiedWebSocketManager } from '../../../services/unified-websocket-manager';

describe('ApiKeyView', () => {
  let fetchStub: sinon.SinonStub;
  let wsSendStub: sinon.SinonStub;
  let wsSubscribeStub: sinon.SinonStub;
  let wsStateStub: sinon.SinonStub;

  function json(data: unknown, status = 200) {
    return new Response(JSON.stringify(data), {
      status,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  function createFetchStub(opts: { keyFails?: boolean } = {}) {
    return sinon
      .stub(window, 'fetch')
      .callsFake(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();
        const method = (init?.method || 'GET').toUpperCase();

        if (/\/api\/v1\/auth\/api-keys\/key-1$/.test(url) && method === 'GET') {
          if (opts.keyFails) {
            return json({ detail: 'boom' }, 500);
          }
          return json({
            id: 'key-1',
            name: 'Production Key',
            created_at: '2026-03-01T00:00:00Z',
            expires_at: null,
            last_used_at: '2026-03-02T00:00:00Z',
          });
        }

        if (url.includes('/api/v1/auth/api-keys/key-1/governance')) {
          return json({
            subject_type: 'api_keys',
            subject_id: 'key-1',
            config: { allowed_models: [], tool_rules: {} },
          });
        }

        if (url.includes('/api/v1/auth/api-keys/key-1/gateway-usage/summary')) {
          return json({
            estimated_cost: 1.23,
            total_requests: 4,
            usage_by_model: [],
            usage_by_session: [],
          });
        }

        if (url.endsWith('/api/v1/tools')) return json([]);
        if (url.endsWith('/api/v1/mcp-servers')) return json([]);
        if (url.endsWith('/api/v1/approval-workflows')) return json([]);
        if (url.endsWith('/api/v1/ai-models')) return json([]);
        if (url.includes('/api/v1/features')) return json({ features: {} });

        return json({ detail: `Unhandled: ${method} ${url}` }, 200);
      });
  }

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
    wsSendStub = sinon.stub(unifiedWebSocketManager, 'send').returns(true);
    wsSubscribeStub = sinon
      .stub(unifiedWebSocketManager, 'subscribe')
      .returns(() => {});
    wsStateStub = sinon
      .stub(unifiedWebSocketManager, 'onStateChange')
      .returns(() => {});
  });

  afterEach(() => {
    fetchStub?.restore();
    wsSendStub.restore();
    wsSubscribeStub.restore();
    wsStateStub.restore();
    localStorage.clear();
  });

  it('shows the loading spinner when no key id is provided', async () => {
    fetchStub = createFetchStub();
    const element = (await fixture(
      html`<api-key-view></api-key-view>`
    )) as ApiKeyView;
    await element.updateComplete;

    // Without a keyId, loadData is never called, so loading stays true.
    expect((element as any).loading).to.be.true;
    expect(element.shadowRoot?.querySelector('sl-spinner')).to.exist;
  });

  it('loads and renders API key details', async () => {
    fetchStub = createFetchStub();
    const element = (await fixture(
      html`<api-key-view
        .location=${{ params: { keyId: 'key-1' } }}
      ></api-key-view>`
    )) as ApiKeyView;

    await waitUntil(
      () => !(element as any).loading,
      'API key view did not finish loading'
    );
    await element.updateComplete;

    expect((element as any).apiKey?.name).to.equal('Production Key');
    const header = element.shadowRoot?.querySelector('view-header');
    expect((header as any)?.headerText).to.equal('Production Key');
    expect(header?.shadowRoot?.querySelector('h1')?.textContent).to.contain(
      'Production Key'
    );
    expect(element.shadowRoot?.textContent).to.contain('Active');
  });

  it('renders the back link and a Revoke button in slots view-header has', async () => {
    fetchStub = createFetchStub();
    const element = (await fixture(
      html`<api-key-view
        .location=${{ params: { keyId: 'key-1' } }}
      ></api-key-view>`
    )) as ApiKeyView;

    await waitUntil(
      () => !(element as any).loading,
      'API key view did not finish loading'
    );
    await element.updateComplete;

    const back = element.shadowRoot?.querySelector(
      'view-header [slot="top"] sl-button'
    );
    expect(back, 'back link is rendered').to.exist;
    expect(back?.getAttribute('href')).to.equal('/console/settings/api-keys');
    expect(back?.textContent?.trim()).to.contain('Back to API keys');

    const revoke = element.shadowRoot?.querySelector(
      'view-header [slot="main-column"] sl-button[variant="danger"]'
    );
    expect(revoke, 'Revoke button is rendered').to.exist;
    expect(revoke?.hasAttribute('outline')).to.be.true;
    expect(revoke?.textContent?.trim()).to.contain('Revoke key');
  });

  it('renders an error state when the key fails to load', async () => {
    fetchStub = createFetchStub({ keyFails: true });
    const element = (await fixture(
      html`<api-key-view
        .location=${{ params: { keyId: 'key-1' } }}
      ></api-key-view>`
    )) as ApiKeyView;

    await waitUntil(
      () => !(element as any).loading,
      'API key view did not finish loading'
    );
    await element.updateComplete;

    expect((element as any).error).to.be.a('string');
    expect(element.shadowRoot?.textContent).to.contain('Error loading API key');
  });
});
