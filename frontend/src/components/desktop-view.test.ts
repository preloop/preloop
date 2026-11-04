/**
 * Browser tests for the live desktop viewer (#870) against the mock viewer
 * socket in scripts/desktop-mock-server.mjs, which speaks the contract's RFB
 * 3.8 None handshake and serves a fixed framebuffer.
 */
import {
  html,
  fixture,
  fixtureCleanup,
  expect,
  waitUntil,
} from '@open-wc/testing';
import sinon from 'sinon';

import './desktop-view';
import { canTakeDesktopControl, desktopViewerSocketUrl } from '../desktop-api';
import {
  DESKTOP_VIEW_STRINGS,
  formatRemaining,
  outcomeForCloseCode,
  type DesktopViewElement,
} from './desktop-view';

interface Call {
  url: string;
  method: string;
  body: any;
}

const realFetch = window.fetch.bind(window);

function viewRow(viewId: string, overrides: Record<string, unknown> = {}) {
  return {
    view_id: viewId,
    endpoint_id: 'ep-1',
    managed_agent_id: null,
    runtime_session_id: null,
    user_id: 'user-1',
    state: 'pending',
    mode: 'view_only',
    ws_url: `/api/v1/ws/desktop/${viewId}`,
    created_at: new Date().toISOString(),
    live_at: null,
    expires_at: new Date(Date.now() + 15 * 60 * 1000).toISOString(),
    closed_at: null,
    closed_reason: null,
    control_expires_at: null,
    ...overrides,
  };
}

describe('desktop-view', () => {
  let fetchStub: sinon.SinonStub;
  const calls: Call[] = [];

  function install(options: {
    create: () =>
      | { status: number; body: unknown }
      | Promise<{ status: number; body: unknown }>;
    get?: (viewId: string) => unknown;
    control?: (viewId: string) => unknown;
    controlStatus?: number;
  }) {
    calls.length = 0;
    localStorage.setItem('accessToken', 'test-token');
    localStorage.setItem('refreshToken', 'refresh');
    fetchStub = sinon.stub(window, 'fetch').callsFake(async (input, init) => {
      const url = String(input);
      if (url.startsWith('/__desktop_mock/')) return realFetch(url);
      const method = String(init?.method || 'GET').toUpperCase();
      const body = init?.body ? JSON.parse(String(init.body)) : null;
      calls.push({ url, method, body });
      const json = (data: unknown, status = 200) =>
        new Response(JSON.stringify(data), {
          status,
          headers: { 'Content-Type': 'application/json' },
        });
      if (
        method === 'POST' &&
        /\/desktop\/endpoints\/[^/]+\/views$/.test(url)
      ) {
        const { status, body: payload } = await options.create();
        return json(payload, status);
      }
      const control = url.match(/\/desktop\/views\/([^/]+)\/control$/);
      if (method === 'POST' && control && options.control) {
        return json(options.control(control[1]), options.controlStatus ?? 200);
      }
      const view = url.match(/\/desktop\/views\/([^/?]+)$/);
      if (view && method === 'DELETE')
        return new Response(null, { status: 204 });
      if (view && method === 'GET' && options.get) {
        return json(options.get(view[1]));
      }
      return json({ detail: 'not stubbed' }, 404);
    });
  }

  afterEach(() => {
    // Unmount first: a mounted viewer ends its view on disconnect, and that
    // DELETE must hit the stub, not the test server.
    fixtureCleanup();
    fetchStub?.restore();
  });

  async function mockLog(viewId: string): Promise<number[] | null> {
    const response = await realFetch(
      `/__desktop_mock/log?view=${encodeURIComponent(viewId)}`
    );
    return (await response.json()).types;
  }

  function stateOf(el: DesktopViewElement): string {
    return el.viewerState;
  }

  it('opens a view, renders the remote framebuffer view-only with banner and countdown', async () => {
    const viewId = `live-${Date.now()}`;
    install({ create: () => ({ status: 201, body: viewRow(viewId) }) });
    const el = await fixture<DesktopViewElement>(
      html`<desktop-view endpointId="ep-1"></desktop-view>`
    );
    const open = el.shadowRoot!.querySelector<HTMLElement>('sl-button.open')!;
    expect(open.textContent).to.contain(DESKTOP_VIEW_STRINGS.open);
    open.click();

    await waitUntil(() => stateOf(el) === 'live', 'viewer went live', {
      timeout: 10000,
    });
    const canvas = el.shadowRoot!.querySelector(
      '.screen canvas'
    ) as HTMLCanvasElement | null;
    expect(canvas, 'noVNC canvas').to.exist;
    await waitUntil(() => canvas!.width === 64 && canvas!.height === 48);
    // The mock paints solid green; the canvas must show it.
    await waitUntil(() => {
      const px = canvas!.getContext('2d')!.getImageData(1, 1, 1, 1).data;
      return px[1] === 0xc0 && px[0] === 0 && px[2] === 0;
    }, 'framebuffer painted');

    const banner = el.shadowRoot!.querySelector(
      '[data-testid="desktop-banner"]'
    )!;
    expect(banner.textContent).to.equal('View only. Nothing is recorded.');
    const countdown = el.shadowRoot!.querySelector(
      '[data-testid="desktop-countdown"]'
    )!;
    expect(countdown.textContent).to.match(/^1[45]:\d\d left$/);

    // View-only: clicking and typing on the canvas sends no input upstream.
    canvas!.dispatchEvent(
      new MouseEvent('mousedown', { bubbles: true, clientX: 5, clientY: 5 })
    );
    canvas!.dispatchEvent(
      new KeyboardEvent('keydown', { bubbles: true, key: 'a', code: 'KeyA' })
    );
    await new Promise((resolve) => setTimeout(resolve, 200));
    const types = (await mockLog(viewId))!;
    expect(types).to.include(3);
    expect(types).to.not.include(4);
    expect(types).to.not.include(5);
    expect(types).to.not.include(6);

    // Request control is hidden unless the caller may take control.
    expect(el.shadowRoot!.querySelector('sl-button.request-control')).to.be
      .null;

    const create = calls.find((c) => c.method === 'POST')!;
    expect(create.url).to.equal('/api/v1/desktop/endpoints/ep-1/views');
  });

  it('sends the user token on the viewer socket and never renders it', async () => {
    const viewId = `live-token-${Date.now()}`;
    install({ create: () => ({ status: 201, body: viewRow(viewId) }) });
    const el = await fixture<DesktopViewElement>(
      html`<desktop-view endpointId="ep-1" autoStart></desktop-view>`
    );
    await waitUntil(() => stateOf(el) === 'live', 'live', { timeout: 10000 });
    expect(el.shadowRoot!.innerHTML).to.not.contain('test-token');
  });

  it('End closes the view and deletes it on the server', async () => {
    const viewId = `live-end-${Date.now()}`;
    install({ create: () => ({ status: 201, body: viewRow(viewId) }) });
    const el = await fixture<DesktopViewElement>(
      html`<desktop-view endpointId="ep-1" autoStart></desktop-view>`
    );
    await waitUntil(() => stateOf(el) === 'live', 'live', { timeout: 10000 });
    el.shadowRoot!.querySelector<HTMLElement>('sl-button.end')!.click();
    await waitUntil(() => stateOf(el) === 'ended');
    await waitUntil(() => calls.some((c) => c.method === 'DELETE'));
    const del = calls.find((c) => c.method === 'DELETE')!;
    expect(del.url).to.equal(`/api/v1/desktop/views/${viewId}`);
    expect(el.shadowRoot!.querySelector('.screen canvas')).to.be.null;
    expect(el.shadowRoot!.textContent).to.contain('View ended: closed.');
    expect(
      el.shadowRoot!.querySelector('sl-button.open')!.textContent
    ).to.contain(DESKTOP_VIEW_STRINGS.reopen);
  });

  it('shows "host disconnected" when the endpoint drops mid-view (4410)', async () => {
    const viewId = `gone-${Date.now()}`;
    install({ create: () => ({ status: 201, body: viewRow(viewId) }) });
    const el = await fixture<DesktopViewElement>(
      html`<desktop-view endpointId="ep-1" autoStart></desktop-view>`
    );
    await waitUntil(() => stateOf(el) === 'gone', 'gone', { timeout: 10000 });
    expect(
      el.shadowRoot!.querySelector('[data-state="gone"]')!.textContent
    ).to.contain(DESKTOP_VIEW_STRINGS.gone);
  });

  it('shows unreachable when the endpoint never connects (4415)', async () => {
    const viewId = `unreachable-${Date.now()}`;
    install({ create: () => ({ status: 201, body: viewRow(viewId) }) });
    const el = await fixture<DesktopViewElement>(
      html`<desktop-view endpointId="ep-1" autoStart></desktop-view>`
    );
    await waitUntil(() => stateOf(el) === 'unreachable', 'unreachable', {
      timeout: 10000,
    });
    expect(el.shadowRoot!.textContent).to.contain(
      'preloop-desktop-tunnel.service'
    );
  });

  it('shows the expiry reason when the view times out (4411)', async () => {
    const viewId = `expire-${Date.now()}`;
    install({ create: () => ({ status: 201, body: viewRow(viewId) }) });
    const el = await fixture<DesktopViewElement>(
      html`<desktop-view endpointId="ep-1" autoStart></desktop-view>`
    );
    await waitUntil(() => stateOf(el) === 'ended', 'ended', { timeout: 10000 });
    expect(el.shadowRoot!.textContent).to.contain(
      'View ended: time limit reached.'
    );
  });

  it('asks the view row for the reason on an unspecific close', async () => {
    const viewId = `drop-${Date.now()}`;
    install({
      create: () => ({ status: 201, body: viewRow(viewId) }),
      get: (id) =>
        viewRow(id, { state: 'closed', closed_reason: 'display_unavailable' }),
    });
    const el = await fixture<DesktopViewElement>(
      html`<desktop-view endpointId="ep-1" autoStart></desktop-view>`
    );
    await waitUntil(() => stateOf(el) === 'unreachable', 'unreachable', {
      timeout: 10000,
    });
    expect(el.shadowRoot!.textContent).to.contain(
      DESKTOP_VIEW_STRINGS.displayUnavailable
    );
  });

  it('names the holder when the desktop is busy (409 desktop_view_busy)', async () => {
    install({
      create: () => ({
        status: 409,
        body: {
          detail: {
            code: 'desktop_view_busy',
            message: 'busy',
            holder: {
              view_id: 'v',
              user_id: 'u2',
              username: 'Jane Doe',
              since: new Date().toISOString(),
            },
          },
        },
      }),
    });
    const el = await fixture<DesktopViewElement>(
      html`<desktop-view endpointId="ep-1" autoStart></desktop-view>`
    );
    await waitUntil(() => stateOf(el) === 'busy');
    expect(el.shadowRoot!.textContent).to.contain('Jane Doe is viewing');
  });

  it('shows the host as gone when no endpoint is available (409)', async () => {
    install({
      create: () => ({
        status: 409,
        body: { detail: { code: 'desktop_not_available', message: 'offline' } },
      }),
    });
    const el = await fixture<DesktopViewElement>(
      html`<desktop-view endpointId="ep-1" autoStart></desktop-view>`
    );
    await waitUntil(() => stateOf(el) === 'gone');
  });

  it('offers Request control only when permitted and switches to control mode', async () => {
    const viewId = `live-control-${Date.now()}`;
    install({
      create: () => ({ status: 201, body: viewRow(viewId) }),
      control: (id) =>
        viewRow(id, {
          state: 'live',
          mode: 'control',
          control_expires_at: new Date(Date.now() + 600000).toISOString(),
        }),
    });
    const el = await fixture<DesktopViewElement>(
      html`<desktop-view endpointId="ep-1" autoStart canControl></desktop-view>`
    );
    await waitUntil(() => stateOf(el) === 'live', 'live', { timeout: 10000 });
    el.shadowRoot!.querySelector<HTMLElement>(
      'sl-button.request-control'
    )!.click();
    await el.updateComplete;
    const input = el.shadowRoot!.querySelector<HTMLInputElement>(
      'sl-input.control-reason'
    )!;
    input.value = 'fix a stuck dialog';
    [...el.shadowRoot!.querySelectorAll<HTMLElement>('.reason sl-button')]
      .find((b) =>
        b.textContent!.includes(DESKTOP_VIEW_STRINGS.controlConfirm)
      )!
      .click();
    await waitUntil(() =>
      el
        .shadowRoot!.querySelector('[data-testid="desktop-banner"]')!
        .textContent!.includes('You have control')
    );
    const control = calls.find((c) => c.url.endsWith('/control'))!;
    expect(control.body).to.deep.equal({ reason: 'fix a stuck dialog' });
    expect(el.shadowRoot!.querySelector('sl-button.release-control')).to.exist;
  });

  async function askForControl(el: DesktopViewElement) {
    await waitUntil(() => stateOf(el) === 'live', 'live', { timeout: 10000 });
    el.shadowRoot!.querySelector<HTMLElement>(
      'sl-button.request-control'
    )!.click();
    await el.updateComplete;
    el.shadowRoot!.querySelector<HTMLInputElement>(
      'sl-input.control-reason'
    )!.value = 'fix a stuck dialog';
    [...el.shadowRoot!.querySelectorAll<HTMLElement>('.reason sl-button')]
      .find((b) =>
        b.textContent!.includes(DESKTOP_VIEW_STRINGS.controlConfirm)
      )!
      .click();
  }

  it('waits for another admin, then switches when the view enters control', async () => {
    const viewId = `live-approval-${Date.now()}`;
    const pending = { approval_id: 'appr-1', state: 'pending' };
    let approved = false;
    install({
      create: () => ({ status: 201, body: viewRow(viewId) }),
      controlStatus: 202,
      control: (id) => viewRow(id, { state: 'live', control_request: pending }),
      get: (id) =>
        approved
          ? viewRow(id, {
              state: 'live',
              mode: 'control',
              control_request: null,
              control_expires_at: new Date(Date.now() + 600000).toISOString(),
            })
          : viewRow(id, { state: 'live', control_request: pending }),
    });
    const el = await fixture<DesktopViewElement>(
      html`<desktop-view
        endpointId="ep-1"
        autoStart
        canControl
        .controlPollMs=${50}
      ></desktop-view>`
    );
    await askForControl(el);
    await waitUntil(() =>
      el
        .shadowRoot!.querySelector('[data-testid="desktop-control-waiting"]')
        ?.textContent!.includes(DESKTOP_VIEW_STRINGS.waitingForApproval)
    );
    expect(el.shadowRoot!.querySelector('sl-button.request-control')).to.not
      .exist;
    expect(
      el.shadowRoot!.querySelector('[data-testid="desktop-banner"]')!
        .textContent
    ).to.include('View only');
    approved = true;
    await waitUntil(() =>
      el
        .shadowRoot!.querySelector('[data-testid="desktop-banner"]')!
        .textContent!.includes('You have control')
    );
    expect(
      el.shadowRoot!.querySelector('[data-testid="desktop-control-waiting"]')
    ).to.not.exist;
    expect(el.shadowRoot!.querySelector('sl-button.release-control')).to.exist;
  });

  it('says so when the control request is not approved', async () => {
    const viewId = `live-denied-${Date.now()}`;
    const pending = { approval_id: 'appr-2', state: 'pending' };
    let decided = false;
    install({
      create: () => ({ status: 201, body: viewRow(viewId) }),
      controlStatus: 202,
      control: (id) => viewRow(id, { state: 'live', control_request: pending }),
      get: (id) =>
        viewRow(id, {
          state: 'live',
          control_request: decided ? null : pending,
        }),
    });
    const el = await fixture<DesktopViewElement>(
      html`<desktop-view
        endpointId="ep-1"
        autoStart
        canControl
        .controlPollMs=${50}
      ></desktop-view>`
    );
    await askForControl(el);
    await waitUntil(() =>
      el.shadowRoot!.querySelector('[data-testid="desktop-control-waiting"]')
    );
    decided = true;
    await waitUntil(() =>
      el.shadowRoot!.textContent!.includes(
        DESKTOP_VIEW_STRINGS.controlNotApproved
      )
    );
    expect(el.shadowRoot!.querySelector('sl-button.request-control')).to.exist;
  });
});

describe('desktop-view review fixes', () => {
  let fetchStub: sinon.SinonStub;
  const calls: Array<{ url: string; method: string }> = [];

  afterEach(() => {
    fixtureCleanup();
    fetchStub?.restore();
  });

  function stub(create: () => Promise<Response>) {
    calls.length = 0;
    localStorage.setItem('accessToken', 'test-token');
    fetchStub = sinon.stub(window, 'fetch').callsFake(async (input, init) => {
      const url = String(input);
      const method = String(init?.method || 'GET').toUpperCase();
      calls.push({ url, method });
      if (method === 'POST') return create();
      if (method === 'DELETE') return new Response(null, { status: 204 });
      return new Response('{}', { status: 404 });
    });
  }

  it('ends a view whose creation finished after the viewer unmounted', async () => {
    const viewId = `live-orphan-${Date.now()}`;
    let release!: () => void;
    const held = new Promise<void>((resolve) => (release = resolve));
    stub(async () => {
      await held;
      return new Response(JSON.stringify(viewRow(viewId)), {
        status: 201,
        headers: { 'Content-Type': 'application/json' },
      });
    });
    const el = await fixture<DesktopViewElement>(
      html`<desktop-view endpointId="ep-1" autoStart></desktop-view>`
    );
    await waitUntil(() => calls.some((c) => c.method === 'POST'));
    el.remove();
    release();
    await waitUntil(
      () =>
        calls.some(
          (c) =>
            c.method === 'DELETE' && c.url === `/api/v1/desktop/views/${viewId}`
        ),
      'orphaned view deleted'
    );
  });

  it('refuses a ws_url on another origin and never connects with the token', async () => {
    const viewId = `live-offorigin-${Date.now()}`;
    stub(
      async () =>
        new Response(
          JSON.stringify(
            viewRow(viewId, {
              ws_url: `wss://other.example.com/api/v1/ws/desktop/${viewId}`,
            })
          ),
          { status: 201, headers: { 'Content-Type': 'application/json' } }
        )
    );
    const socketSpy = sinon.spy(window, 'WebSocket');
    try {
      const el = await fixture<DesktopViewElement>(
        html`<desktop-view endpointId="ep-1" autoStart></desktop-view>`
      );
      await waitUntil(() => el.viewerState === 'error');
      expect(socketSpy.called).to.equal(false);
      expect(el.shadowRoot!.textContent).to.contain('another origin');
      await waitUntil(() => calls.some((c) => c.method === 'DELETE'));
    } finally {
      socketSpy.restore();
    }
  });
});

describe('desktop-api helpers', () => {
  const location = { protocol: 'https:', host: 'console.example.com' };

  it('builds the viewer socket URL on this origin only', () => {
    expect(
      desktopViewerSocketUrl('/api/v1/ws/desktop/v1', 'jwt', location)
    ).to.equal('wss://console.example.com/api/v1/ws/desktop/v1?token=jwt');
    for (const bad of [
      'wss://other.example.com/api/v1/ws/desktop/v1',
      '//other.example.com/api/v1/ws/desktop/v1',
      '/\\other.example.com/x',
      'api/v1/ws/desktop/v1',
      null,
    ]) {
      expect(
        () => desktopViewerSocketUrl(bad, 'jwt', location),
        String(bad)
      ).to.throw(/another origin/);
    }
  });

  it('shows control only when the profile loaded and grants both permissions', () => {
    expect(canTakeDesktopControl(null)).to.equal(false);
    expect(canTakeDesktopControl(undefined)).to.equal(false);
    expect(canTakeDesktopControl({ permissions: null })).to.equal(true);
    expect(
      canTakeDesktopControl({
        permissions: ['control_managed_agent', 'decide_approvals'],
      })
    ).to.equal(true);
    expect(
      canTakeDesktopControl({ permissions: ['control_managed_agent'] })
    ).to.equal(false);
  });
});

describe('desktop-view helpers', () => {
  it('maps contract close codes to viewer states', () => {
    expect(outcomeForCloseCode(4403)!.state).to.equal('forbidden');
    expect(outcomeForCloseCode(4409)!.state).to.equal('busy');
    expect(outcomeForCloseCode(4410)).to.deep.equal({
      state: 'gone',
      reason: 'agent_disconnected',
    });
    expect(outcomeForCloseCode(4415)!.state).to.equal('unreachable');
    expect(outcomeForCloseCode(4413)!.state).to.equal('unreachable');
    expect(outcomeForCloseCode(4408)).to.deep.equal({
      state: 'ended',
      reason: 'idle',
    });
    expect(outcomeForCloseCode(4499)!.reason).to.equal('server_restart');
    expect(outcomeForCloseCode(1006)).to.be.null;
  });

  it('formats the countdown', () => {
    const now = Date.parse('2026-10-11T18:00:00Z');
    expect(formatRemaining('2026-10-11T18:14:05Z', now)).to.equal('14:05');
    expect(formatRemaining('2026-10-11T19:00:00Z', now)).to.equal('1:00:00');
    expect(formatRemaining('2026-10-11T17:00:00Z', now)).to.equal('0:00');
  });
});
