/**
 * Live desktop in the session view (#870): the "Live desktop" toolbar button
 * appears only when the server lists an online desktop endpoint for the live
 * session, and opens the viewer in a side panel next to the timeline. The
 * viewer socket is the mock in scripts/desktop-mock-server.mjs.
 */
import { fixture, html, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';
import { unifiedWebSocketManager } from '../services/unified-websocket-manager';
import { invalidateApiCaches } from '../api';
import './preloop-session-observer';
import type { PreloopSessionObserver } from './preloop-session-observer';
import type { DesktopViewElement } from './desktop-view';

const SESSION = {
  id: 'runtime-session-1',
  session_source_type: 'custom',
  session_source_id: 'browser-agent',
  session_reference: 'browser-session-1',
  runtime_principal_name: 'Browser agent',
  started_at: '2026-10-11T18:00:00Z',
  last_activity_at: '2026-10-11T18:05:00Z',
  ended_at: null,
  latest_model_alias: 'openai/gpt-5',
  latest_provider_name: 'OpenAI',
  is_active_now: true,
  activity_status: 'active_now',
  total_requests: 1,
  successful_requests: 1,
  failed_requests: 0,
  token_usage: { prompt_tokens: 10, completion_tokens: 5, total_tokens: 15 },
  estimated_cost: 0.01,
  last_request_at: '2026-10-11T18:05:00Z',
};

const ENDPOINT = {
  id: '00000000-0000-4000-8000-000000000001',
  endpoint_id: 'host-endpoint-1',
  hostname: 'agent-vm.example.com',
  display: ':99',
  managed_agent_id: null,
  runner_id: null,
  state: 'online',
  last_seen_at: '2026-10-11T18:05:00Z',
  active_view: null,
};

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('PreloopSessionObserver live desktop', () => {
  let fetchStub: sinon.SinonStub;
  let connectStub: sinon.SinonStub;
  let subscribeStub: sinon.SinonStub;
  let endpointsResponse: () => Response;
  let profileResponse: () => Response;
  let viewId: string;
  let featuresOff = false;
  const calls: Array<{ url: string; method: string; body: any }> = [];

  beforeEach(() => {
    calls.length = 0;
    viewId = `live-observer-${Date.now()}`;
    endpointsResponse = () => jsonResponse({ endpoints: [ENDPOINT] });
    profileResponse = () => jsonResponse({ id: 'user-1', permissions: null });
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
    connectStub = sinon.stub(unifiedWebSocketManager, 'connect').resolves();
    subscribeStub = sinon
      .stub(unifiedWebSocketManager, 'subscribe')
      .returns(() => undefined);
    fetchStub = sinon.stub(window, 'fetch');
    fetchStub.callsFake(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();
        const method = String(init?.method || 'GET').toUpperCase();
        const body = init?.body ? JSON.parse(String(init.body)) : null;
        calls.push({ url, method, body });
        if (url === '/api/v1/features')
          return jsonResponse({
            features: featuresOff ? { desktop_views: false } : {},
          });
        if (url.startsWith('/api/v1/desktop/endpoints?'))
          return endpointsResponse();
        if (method === 'POST' && url.endsWith('/views')) {
          return jsonResponse(
            {
              view_id: viewId,
              endpoint_id: ENDPOINT.endpoint_id,
              managed_agent_id: null,
              runtime_session_id: SESSION.id,
              user_id: 'user-1',
              state: 'pending',
              mode: 'view_only',
              ws_url: `/api/v1/ws/desktop/${viewId}`,
              created_at: new Date().toISOString(),
              live_at: null,
              expires_at: new Date(Date.now() + 900000).toISOString(),
              closed_at: null,
              closed_reason: null,
              control_expires_at: null,
            },
            201
          );
        }
        if (method === 'DELETE') return new Response(null, { status: 204 });
        if (url.includes('/auth/users/me')) return profileResponse();
        if (url.includes('/gateway-events')) {
          return jsonResponse({
            logs: [
              {
                id: 'event-1',
                timestamp: '2026-10-11T18:05:00Z',
                type: 'model_gateway_call',
                payload: {
                  outcome: 'success',
                  model_alias: 'openai/gpt-5',
                  total_tokens: 15,
                  conversation_preview: {
                    messages: [{ role: 'user', text: 'Open the dashboard' }],
                  },
                },
              },
            ],
          });
        }
        if (url.includes('/activity')) return jsonResponse({ items: [] });
        if (url.includes('/approval-requests')) return jsonResponse([]);
        if (url.includes('/ai-models')) return jsonResponse([]);
        return jsonResponse({});
      }
    );
  });

  afterEach(() => {
    fetchStub.restore();
    connectStub.restore();
    subscribeStub.restore();
    localStorage.clear();
  });

  async function mount(
    session: Record<string, unknown> = SESSION,
    features: Record<string, boolean> = { liveDesktop: true }
  ): Promise<PreloopSessionObserver> {
    const el = (await fixture(
      html`<preloop-session-observer
        .sessions=${[session]}
        .features=${features}
      ></preloop-session-observer>`
    )) as PreloopSessionObserver;
    await waitUntil(
      () => Boolean(el.shadowRoot?.querySelector('.toolbar .mode-row')),
      'toolbar did not render',
      { timeout: 3000 }
    );
    return el;
  }

  const liveDesktopButton = (el: PreloopSessionObserver) =>
    el.shadowRoot!.querySelector<HTMLElement>('sl-button.live-desktop');

  it('opens no session by itself when autoSelect is off (#1586)', async () => {
    const el = (await fixture(
      html`<preloop-session-observer
        .sessions=${[SESSION]}
        .autoSelect=${false}
      ></preloop-session-observer>`
    )) as PreloopSessionObserver;
    await new Promise((resolve) => setTimeout(resolve, 100));
    await el.updateComplete;
    expect((el as any).activeSessionId).to.equal(null);
    expect(
      calls.filter(
        (c) => c.url.includes('/activity') || c.url.includes('/gateway-events')
      )
    ).to.deep.equal([]);
  });

  it('does not ask for desktops when the server says desktop views are off', async () => {
    invalidateApiCaches();
    endpointsResponse = () => jsonResponse({ detail: 'disabled' }, 404);
    featuresOff = true;
    const el = await mount();
    await new Promise((resolve) => setTimeout(resolve, 100));
    expect(
      calls.filter((c) => c.url.startsWith('/api/v1/desktop/endpoints'))
    ).to.deep.equal([]);
    expect(liveDesktopButton(el)).to.equal(null);
    featuresOff = false;
    invalidateApiCaches();
  });

  it('opens the live desktop beside the timeline for a live session', async () => {
    const el = await mount();
    await waitUntil(() => liveDesktopButton(el), 'Live desktop button', {
      timeout: 3000,
    });
    const lookup = calls.find((c) =>
      c.url.startsWith('/api/v1/desktop/endpoints?')
    )!;
    expect(lookup.url).to.contain('runtime_session_id=runtime-session-1');
    expect(lookup.url).to.contain('state=online');
    expect(el.shadowRoot!.querySelector('.desktop-panel')).to.be.null;

    liveDesktopButton(el)!.click();
    await el.updateComplete;
    const split = el.shadowRoot!.querySelector('.desktop-split')!;
    expect(split, 'side-by-side layout').to.exist;
    expect(split.querySelector('.content'), 'timeline column').to.exist;
    const panel = split.querySelector('.desktop-panel')!;
    expect(panel.textContent).to.contain('agent-vm.example.com');

    const viewer = panel.querySelector('desktop-view') as DesktopViewElement;
    await waitUntil(() => viewer.viewerState === 'live', 'viewer live', {
      timeout: 10000,
    });
    expect(viewer.shadowRoot!.querySelector('.screen canvas')).to.exist;
    expect(
      viewer.shadowRoot!.querySelector('[data-testid="desktop-banner"]')!
        .textContent
    ).to.equal('View only. Nothing is recorded.');
    const create = calls.find((c) => c.method === 'POST')!;
    expect(create.url).to.equal(
      `/api/v1/desktop/endpoints/${ENDPOINT.id}/views`
    );
    expect(create.body).to.deep.equal({ runtime_session_id: SESSION.id });

    // Closing the panel ends the view.
    panel
      .querySelector<HTMLElement>('sl-icon-button')!
      .dispatchEvent(new MouseEvent('click'));
    await el.updateComplete;
    expect(el.shadowRoot!.querySelector('.desktop-panel')).to.be.null;
    await waitUntil(() => calls.some((c) => c.method === 'DELETE'));
  });

  it('keeps an online desktop available while the conversation is idle', async () => {
    const el = await mount({
      ...SESSION,
      turn_state: 'idle',
      activity_status: 'idle',
      is_active_now: false,
    });
    await waitUntil(() => liveDesktopButton(el), 'Idle session desktop');
    expect(el.shadowRoot!.textContent).not.to.contain('Following live');
    liveDesktopButton(el)!.click();
    await el.updateComplete;
    expect(el.shadowRoot!.querySelector('.desktop-panel')).to.exist;
  });

  it('re-checks the endpoint while the session stays selected', async () => {
    const el = await mount();
    await waitUntil(() => liveDesktopButton(el), 'Live desktop button', {
      timeout: 3000,
    });
    // The host goes offline: the next check hides the button.
    endpointsResponse = () => jsonResponse({ endpoints: [] });
    await (el as any).recheckDesktopEndpoint();
    await el.updateComplete;
    expect(liveDesktopButton(el)).to.be.null;
    // And it comes back.
    endpointsResponse = () => jsonResponse({ endpoints: [ENDPOINT] });
    await (el as any).recheckDesktopEndpoint();
    await el.updateComplete;
    expect(liveDesktopButton(el)).to.exist;
    expect((el as any).desktopRecheck, 'next check scheduled').to.not.equal(
      null
    );
  });

  it('hides Request control when the profile could not be loaded', async () => {
    profileResponse = () => jsonResponse({ detail: 'boom' }, 500);
    const el = await mount();
    await waitUntil(() => liveDesktopButton(el), 'Live desktop button', {
      timeout: 3000,
    });
    expect((el as any).canControlDesktop).to.equal(false);
  });

  it('stays inert when the server has no desktop views (404)', async () => {
    endpointsResponse = () =>
      jsonResponse(
        { detail: { code: 'desktop_views_disabled', message: 'off' } },
        404
      );
    const el = await mount();
    await waitUntil(() =>
      calls.some((c) => c.url.startsWith('/api/v1/desktop/endpoints?'))
    );
    await new Promise((resolve) => setTimeout(resolve, 50));
    await el.updateComplete;
    expect(liveDesktopButton(el)).to.be.null;
  });

  it('hides the button when no endpoint is online', async () => {
    endpointsResponse = () =>
      jsonResponse({ endpoints: [{ ...ENDPOINT, state: 'offline' }] });
    const el = await mount();
    await waitUntil(() =>
      calls.some((c) => c.url.startsWith('/api/v1/desktop/endpoints?'))
    );
    await new Promise((resolve) => setTimeout(resolve, 50));
    await el.updateComplete;
    expect(liveDesktopButton(el)).to.be.null;
  });

  it('does not offer a live desktop for an ended session', async () => {
    const el = await mount({
      ...SESSION,
      ended_at: '2026-10-11T18:10:00Z',
      is_active_now: false,
      activity_status: 'ended',
    });
    await new Promise((resolve) => setTimeout(resolve, 100));
    await el.updateComplete;
    expect(liveDesktopButton(el)).to.be.null;
  });

  it('does not query endpoints when the host does not enable the feature', async () => {
    const el = await mount(SESSION, {});
    await new Promise((resolve) => setTimeout(resolve, 100));
    await el.updateComplete;
    expect(calls.some((c) => c.url.includes('/desktop/'))).to.equal(false);
    expect(liveDesktopButton(el)).to.be.null;
  });
});
