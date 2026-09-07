import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';
import { Router } from '../router';

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
