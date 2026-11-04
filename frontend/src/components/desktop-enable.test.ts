/** Browser tests for the console "Enable desktop" action (#1566). */
import {
  html,
  fixture,
  fixtureCleanup,
  expect,
  waitUntil,
} from '@open-wc/testing';
import sinon from 'sinon';

import './desktop-enable';
import {
  DESKTOP_ENABLE_STRINGS,
  type DesktopEnableElement,
} from './desktop-enable';

describe('desktop-enable', () => {
  let fetchStub: sinon.SinonStub;
  let calls: Array<{ url: string; method: string }> = [];

  function install(post: () => Response, get?: () => unknown) {
    calls = [];
    localStorage.setItem('accessToken', 'test-token');
    fetchStub = sinon.stub(window, 'fetch').callsFake(async (input, init) => {
      const url = String(input);
      const method = String(init?.method || 'GET').toUpperCase();
      calls.push({ url, method });
      if (method === 'POST' && url.endsWith('/desktop/install')) return post();
      if (method === 'GET' && /\/desktop\/install\/[^/]+$/.test(url) && get) {
        return new Response(JSON.stringify(get()), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      return new Response('{}', { status: 404 });
    });
  }

  const accepted = () =>
    new Response(JSON.stringify({ request_id: 'req-1', state: 'pending' }), {
      status: 202,
      headers: { 'Content-Type': 'application/json' },
    });

  afterEach(() => {
    fixtureCleanup();
    fetchStub?.restore();
  });

  function text(el: DesktopEnableElement): string {
    return el.shadowRoot!.textContent || '';
  }

  it('shows the one-liner and sends nothing without a control channel', async () => {
    install(accepted);
    const el = await fixture<DesktopEnableElement>(
      html`<desktop-enable agentId="agent-1"></desktop-enable>`
    );
    const row = el.shadowRoot!.querySelector(
      '[data-testid="desktop-enable-command"]'
    )!;
    expect(row.querySelector('code')!.textContent).to.equal(
      'preloop desktop enable --agent agent-1'
    );
    expect((row.querySelector('sl-copy-button') as any).value).to.equal(
      'preloop desktop enable --agent agent-1'
    );
    expect(el.shadowRoot!.querySelector('sl-button.enable')).to.not.exist;
    expect(calls).to.deep.equal([]);
  });

  it('installs remotely and announces when the plugin is done', async () => {
    let state = 'pending';
    install(accepted, () => ({
      request_id: 'req-1',
      state,
      error: null,
      command: 'preloop desktop enable --agent agent-1',
    }));
    const el = await fixture<DesktopEnableElement>(
      html`<desktop-enable
        agentId="agent-1"
        controlOnline
        .pollMs=${30}
      ></desktop-enable>`
    );
    const installed = new Promise((resolve) =>
      el.addEventListener('desktop-installed', resolve)
    );
    el.shadowRoot!.querySelector<HTMLElement>('sl-button.enable')!.click();
    await waitUntil(() => text(el).includes(DESKTOP_ENABLE_STRINGS.installing));
    state = 'done';
    await installed;
    await waitUntil(() => text(el).includes(DESKTOP_ENABLE_STRINGS.done));
    expect(calls.filter((c) => c.method === 'POST')).to.have.length(1);
  });

  it('explains an unsupported distro with the docs link', async () => {
    install(accepted, () => ({
      request_id: 'req-1',
      state: 'failed',
      error: 'desktop_unsupported_distro',
      command: '',
    }));
    const el = await fixture<DesktopEnableElement>(
      html`<desktop-enable
        agentId="agent-1"
        controlOnline
        .pollMs=${30}
      ></desktop-enable>`
    );
    el.shadowRoot!.querySelector<HTMLElement>('sl-button.enable')!.click();
    await waitUntil(() =>
      text(el).includes(
        DESKTOP_ENABLE_STRINGS.failed.desktop_unsupported_distro
      )
    );
    expect(el.shadowRoot!.querySelector('a')!.getAttribute('href')).to.contain(
      'live-desktop-view'
    );
  });

  it('falls back to the one-liner when the plugin does not know the command', async () => {
    install(accepted, () => ({
      request_id: 'req-1',
      state: 'unsupported',
      error: null,
      command: '',
    }));
    const el = await fixture<DesktopEnableElement>(
      html`<desktop-enable
        agentId="agent-1"
        controlOnline
        .pollMs=${30}
      ></desktop-enable>`
    );
    el.shadowRoot!.querySelector<HTMLElement>('sl-button.enable')!.click();
    await waitUntil(() =>
      el.shadowRoot!.querySelector('[data-testid="desktop-enable-command"]')
    );
    expect(text(el)).to.include(DESKTOP_ENABLE_STRINGS.unsupportedPlugin);
  });

  it('falls back to the one-liner when the API reports no control channel', async () => {
    install(
      () =>
        new Response(
          JSON.stringify({
            detail: {
              code: 'desktop_install_unavailable',
              message: 'no channel',
              command: 'preloop desktop enable --agent agent-1',
            },
          }),
          { status: 409, headers: { 'Content-Type': 'application/json' } }
        )
    );
    const el = await fixture<DesktopEnableElement>(
      html`<desktop-enable agentId="agent-1" controlOnline></desktop-enable>`
    );
    el.shadowRoot!.querySelector<HTMLElement>('sl-button.enable')!.click();
    await waitUntil(() =>
      el.shadowRoot!.querySelector('[data-testid="desktop-enable-command"]')
    );
    expect(text(el)).to.include(DESKTOP_ENABLE_STRINGS.runOnHost);
  });
});

describe('desktop-enable follow limits', () => {
  let fetchStub: sinon.SinonStub;
  afterEach(() => {
    fixtureCleanup();
    fetchStub?.restore();
  });

  function stub(getStatus: number, body: unknown) {
    localStorage.setItem('accessToken', 'test-token');
    fetchStub = sinon.stub(window, 'fetch').callsFake(async (_input, init) => {
      const method = String(init?.method || 'GET').toUpperCase();
      if (method === 'POST')
        return new Response(
          JSON.stringify({ request_id: 'r', state: 'pending' }),
          {
            status: 202,
            headers: { 'Content-Type': 'application/json' },
          }
        );
      return new Response(JSON.stringify(body), {
        status: getStatus,
        headers: { 'Content-Type': 'application/json' },
      });
    });
  }

  it('stops following on 404', async () => {
    stub(404, { detail: 'Request not found' });
    const el = await fixture<DesktopEnableElement>(
      html`<desktop-enable
        agentId="a"
        controlOnline
        .pollMs=${20}
      ></desktop-enable>`
    );
    el.shadowRoot!.querySelector<HTMLElement>('sl-button.enable')!.click();
    await waitUntil(() =>
      el.shadowRoot!.textContent!.includes(
        DESKTOP_ENABLE_STRINGS.failed.desktop_failed
      )
    );
    const calls = fetchStub.callCount;
    await new Promise((r) => setTimeout(r, 100));
    expect(fetchStub.callCount).to.equal(calls);
  });

  it('gives up after the follow deadline', async () => {
    stub(200, { request_id: 'r', state: 'pending', error: null, command: '' });
    const el = await fixture<DesktopEnableElement>(
      html`<desktop-enable
        agentId="a"
        controlOnline
        .pollMs=${20}
        .followMs=${60}
      ></desktop-enable>`
    );
    el.shadowRoot!.querySelector<HTMLElement>('sl-button.enable')!.click();
    await waitUntil(() =>
      el.shadowRoot!.textContent!.includes(
        DESKTOP_ENABLE_STRINGS.failed.desktop_timed_out
      )
    );
  });
});
