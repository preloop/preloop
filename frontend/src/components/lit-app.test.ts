import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';
import { router, Router, LOCATION_CHANGED } from '../router';
import { CapabilityRouteGate } from '../lazy-routes';

import type { LitApp } from './lit-app';
import './lit-app';

describe('LitApp routing', () => {
  let fetchStub: sinon.SinonStub;

  beforeEach(() => {
    (window as any).BRAND_CONFIG = {
      name: 'Preloop',
      domain: 'preloop.ai',
      company: { legal_name: 'Preloop', address: '', city: '' },
      branding: {
        logo_light: '/logo.svg',
        logo_dark: '/logo-dark.svg',
        favicon: '/favicon.ico',
        primary_color: '#000',
        gradient_product: '',
        gradient_ai: '',
      },
      social: { twitter: '', linkedin: '', instagram: '' },
    };
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');

    fetchStub = sinon.stub(window, 'fetch');
    fetchStub.callsFake(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString();
      if (url.includes('/api/v1/features')) {
        return new Response(JSON.stringify({ features: {}, permissions: [] }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      // Endpoints that expect list payloads
      if (
        url.includes('/api/v1/approval-requests') ||
        url.includes('/api/v1/tools') ||
        url.includes('/api/v1/trackers') ||
        url.includes('/api/v1/ai-models') ||
        url.includes('/api/v1/mcp-servers')
      ) {
        return new Response('[]', {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      return new Response('{}', {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    });
  });

  afterEach(() => {
    fetchStub.restore();
    localStorage.clear();
    delete (window as any).BRAND_CONFIG;
    window.history.replaceState({}, '', '/');
  });

  it('keeps console pages out of the initial public page registration', () => {
    expect(customElements.get('profile-view')).to.equal(undefined);
    expect(customElements.get('agent-detail-view')).to.equal(undefined);
    expect(customElements.get('flow-execution-view')).to.equal(undefined);
  });

  it('removes capability listeners on disconnect and restores one on reconnect', async () => {
    const sync = sinon.stub(CapabilityRouteGate.prototype, 'sync').resolves([]);
    try {
      const el = await fixture<LitApp>(html`<lit-app></lit-app>`);
      const parent = el.parentElement!;
      window.history.replaceState({}, '', '/console');
      sync.resetHistory();
      window.dispatchEvent(new CustomEvent(LOCATION_CHANGED));
      expect(sync.callCount).to.equal(1);

      el.remove();
      sync.resetHistory();
      window.dispatchEvent(new CustomEvent(LOCATION_CHANGED));
      expect(sync.callCount).to.equal(0);

      parent.appendChild(el);
      await el.updateComplete;
      sync.resetHistory();
      window.dispatchEvent(new CustomEvent(LOCATION_CHANGED));
      expect(sync.callCount).to.equal(1);
      el.remove();
    } finally {
      sync.restore();
    }
  });

  it('cancels the deferred websocket connection when removed before the frame', async () => {
    const schedule = sinon.stub(window, 'requestAnimationFrame').returns(12345);
    const cancel = sinon.spy(window, 'cancelAnimationFrame');
    try {
      const el = await fixture<LitApp>(html`<lit-app></lit-app>`);
      const connect = sinon.spy(el, 'connectWebSocket');
      el.remove();
      expect(cancel.calledWith(12345)).to.equal(true);
      // Even an already queued callback must not reconnect a removed app.
      const callback = schedule.firstCall.args[0] as FrameRequestCallback;
      callback(0);
      expect(connect.called).to.equal(false);
    } finally {
      schedule.restore();
      cancel.restore();
    }
  });

  it('reschedules websocket startup after an early disconnect, only once', async () => {
    const schedule = sinon.stub(window, 'requestAnimationFrame').returns(12345);
    try {
      const el = await fixture<LitApp>(html`<lit-app></lit-app>`);
      const parent = el.parentElement!;
      const connect = sinon.spy(el, 'connectWebSocket');
      const initialSchedules = schedule.callCount;
      el.remove();
      parent.appendChild(el);
      await el.updateComplete;

      expect(schedule.callCount).to.equal(initialSchedules + 1);
      const callback = schedule.lastCall.args[0] as FrameRequestCallback;
      callback(0);
      expect(connect.callCount).to.equal(1);

      el.remove();
      parent.appendChild(el);
      await el.updateComplete;
      expect(schedule.callCount).to.equal(initialSchedules + 1);
      expect(connect.callCount).to.equal(1);
    } finally {
      schedule.restore();
    }
  });

  it('finishes a gated deep-link initialization after reconnecting', async () => {
    let release!: (routes: []) => void;
    let releaseResumed!: (routes: []) => void;
    const pending = new Promise<[]>((resolve) => {
      release = resolve;
    });
    const resumed = new Promise<[]>((resolve) => {
      releaseResumed = resolve;
    });
    const sync = sinon.stub(CapabilityRouteGate.prototype, 'sync').resolves([]);
    sync.onFirstCall().returns(pending);
    sync.onSecondCall().returns(resumed);
    const install = sinon.spy(router, 'setRoutes');
    try {
      window.history.replaceState({}, '', '/console/settings/subaccounts');
      const el = await fixture<LitApp>(html`<lit-app></lit-app>`);
      const parent = el.parentElement!;
      el.remove();
      release([]);
      parent.appendChild(el);
      await el.updateComplete;
      await pending;
      await Promise.resolve();
      // The completion from before disconnect cannot install stale routes;
      // the fresh capability read made on reconnect must finish first.
      expect(install.callCount).to.equal(0);

      releaseResumed([]);
      await waitUntil(
        () => install.callCount === 1,
        'Route initialization was lost on reconnect'
      );
    } finally {
      sync.restore();
      install.restore();
    }
  });

  it('does not install stale gated routes into a newer app outlet', async () => {
    let release!: (routes: []) => void;
    const pending = new Promise<[]>((resolve) => {
      release = resolve;
    });
    const sync = sinon
      .stub(CapabilityRouteGate.prototype, 'sync')
      .returns(pending);
    const install = sinon.spy(router, 'setRoutes');
    try {
      window.history.replaceState({}, '', '/console/settings/subaccounts');
      await fixture<LitApp>(html`<lit-app></lit-app>`);
      const current = await fixture<LitApp>(html`<lit-app></lit-app>`);
      release([]);
      await pending;
      await Promise.resolve();
      expect(install.callCount).to.equal(1);
      expect(router.getOutlet()).to.equal(
        current.shadowRoot!.querySelector('main')
      );
    } finally {
      sync.restore();
      install.restore();
    }
  });

  it('renders the landing page and /login without touching a console chunk', async () => {
    const el = await fixture<HTMLElement>(html`<lit-app></lit-app>`);
    await waitUntil(
      () => Boolean(el.shadowRoot?.querySelector('landing-view')),
      'Expected the landing page to render'
    );

    Router.go('/login');
    await waitUntil(
      () => Boolean(el.shadowRoot?.querySelector('login-view')),
      'Expected /login to render'
    );

    // The two doors an anonymous visitor uses. Neither may drag the console
    // in behind it; that is the whole point of the split.
    expect(customElements.get('console-shell')).to.equal(undefined);
    expect(customElements.get('dashboard-view')).to.equal(undefined);
    expect(customElements.get('agents-view')).to.equal(undefined);
  });

  it('puts the served title back when the reader leaves the console', async () => {
    const original = document.title;
    try {
      const el = await fixture<HTMLElement>(html`<lit-app></lit-app>`);
      await waitUntil(() =>
        Boolean(el.shadowRoot?.querySelector('landing-view'))
      );
      // A console page retitled the tab (view-header does this).
      document.title = 'Agents · Preloop';
      Router.go('/login');
      await waitUntil(
        () => Boolean(el.shadowRoot?.querySelector('login-view')),
        'Expected /login to render'
      );
      expect(document.title).to.equal(original);
    } finally {
      document.title = original;
    }
  });

  it('registers a nested console view on navigation and handles OAuth tokens', async () => {
    window.history.replaceState(
      {},
      '',
      '/console/settings/profile#access_token=oauth-access&refresh_token=oauth-refresh'
    );
    const el = await fixture<HTMLElement>(html`<lit-app></lit-app>`);
    await waitUntil(
      () =>
        Boolean(el.shadowRoot?.querySelector('console-shell > profile-view')),
      'Expected the lazy profile route to render',
      { timeout: 5000 }
    );
    expect(customElements.get('profile-view')).to.exist;
    expect(localStorage.getItem('accessToken')).to.equal('oauth-access');
    expect(localStorage.getItem('refreshToken')).to.equal('oauth-refresh');
  });

  it('redirects the legacy /console/onboarding route to /console/agents', async () => {
    await fixture(html`<lit-app></lit-app>`);

    Router.go('/console/onboarding');

    await waitUntil(
      () => window.location.pathname === '/console/agents',
      'Expected /console/onboarding to redirect to /console/agents',
      { timeout: 5000 }
    );

    expect(window.location.pathname).to.equal('/console/agents');
    // The onboarding view was deleted; the redirect is the only thing left of
    // that route, so nothing must render the old element.
    expect(document.querySelector('onboarding-view')).to.equal(null);
    expect(customElements.get('onboarding-view')).to.equal(undefined);
  });

  it('resolves the plan page and the emergency page', async () => {
    const el = await fixture<HTMLElement>(html`<lit-app></lit-app>`);

    Router.go('/console/settings/plan');
    await waitUntil(
      () => Boolean(el.shadowRoot?.querySelector('console-shell > plan-view')),
      'Expected the plan route to render',
      { timeout: 5000 }
    );

    Router.go('/console/settings/emergency');
    await waitUntil(
      () =>
        Boolean(el.shadowRoot?.querySelector('console-shell > emergency-view')),
      'Expected the emergency route to render',
      { timeout: 5000 }
    );
  });

  it('renders an unknown console path as a 404 inside the shell', async () => {
    const el = await fixture<HTMLElement>(html`<lit-app></lit-app>`);

    Router.go('/console/does-not-exist');

    // Inside console-shell, so the sidebar and header stay on screen.
    await waitUntil(
      () =>
        Boolean(el.shadowRoot?.querySelector('console-shell > not-found-view')),
      'Expected the console 404 to render inside the shell',
      { timeout: 5000 }
    );
    expect(window.location.pathname).to.equal('/console/does-not-exist');
  });

  it('keeps real console routes ahead of the console 404', async () => {
    const el = await fixture<HTMLElement>(html`<lit-app></lit-app>`);

    Router.go('/console/settings/emergency');
    await waitUntil(
      () =>
        Boolean(el.shadowRoot?.querySelector('console-shell > emergency-view')),
      'Expected the emergency route to render',
      { timeout: 5000 }
    );
    expect(el.shadowRoot?.querySelector('not-found-view')).to.equal(null);
  });

  it('still renders the bare 404 for an unknown public path', async () => {
    const el = await fixture<HTMLElement>(html`<lit-app></lit-app>`);

    Router.go('/no-such-page');
    await waitUntil(
      () => Boolean(el.shadowRoot?.querySelector('main > not-found-view')),
      'Expected the top-level 404',
      { timeout: 5000 }
    );
    expect(el.shadowRoot?.querySelector('console-shell')).to.equal(null);
  });

  it('redirects the old bell link to the executions list', async () => {
    await fixture(html`<lit-app></lit-app>`);

    Router.go('/console/flow-executions');

    await waitUntil(
      () => window.location.pathname === '/console/flows/executions',
      'Expected /console/flow-executions to redirect',
      { timeout: 5000 }
    );
  });

  it('redirects the console pricing route to the plan page', async () => {
    await fixture(html`<lit-app></lit-app>`);

    Router.go('/console/pricing');

    await waitUntil(
      () => window.location.pathname === '/console/settings/plan',
      'Expected /console/pricing to redirect to the plan page',
      { timeout: 5000 }
    );
    expect(window.location.pathname).to.equal('/console/settings/plan');
  });

  it('registers markdown pages from BRAND_CONFIG.static_markdown_pages', async () => {
    (window as any).BRAND_CONFIG.static_markdown_pages = [
      { path: '/dora', src: '/content/dora.md' },
    ];
    const el = (await fixture(html`<lit-app></lit-app>`)) as HTMLElement;

    Router.go('/dora');

    await waitUntil(
      () => Boolean(el.shadowRoot?.querySelector('static-view')),
      'Expected /dora to render static-view from the injected page list',
      { timeout: 5000 }
    );

    const view = el.shadowRoot?.querySelector('static-view') as HTMLElement & {
      src?: string;
    };
    expect(view).to.exist;
    expect(view.src).to.equal('/content/dora.md');
  });
});
