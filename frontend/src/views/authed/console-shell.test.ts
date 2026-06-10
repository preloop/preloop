import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import './console-shell';
import type { ConsoleShell } from './console-shell';

const SIDEBAR_BREAKPOINT = 768;

function createMatchMediaStub(matches: boolean) {
  const listeners: Array<(e: MediaQueryListEvent) => void> = [];
  return {
    matches,
    addEventListener: sinon
      .stub()
      .callsFake((_type: string, fn: (e: MediaQueryListEvent) => void) => {
        listeners.push(fn);
      }),
    removeEventListener: sinon.stub(),
    dispatchChange: (m: boolean) => {
      listeners.forEach((fn) => fn({ matches: m } as MediaQueryListEvent));
    },
  };
}

const BRAND_CONFIG_STUB = {
  name: 'Test Brand',
  domain: 'test.example.com',
  company: { legal_name: 'Test Co', address: '123 Test', city: 'Test' },
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

describe('ConsoleShell', () => {
  let fetchStub: sinon.SinonStub;
  let matchMediaStub: sinon.SinonStub;

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
    const mockMediaQuery = createMatchMediaStub(false); // desktop by default
    matchMediaStub = sinon
      .stub(window, 'matchMedia')
      .callsFake((query: string) => {
        if (query.includes(`${SIDEBAR_BREAKPOINT}`)) {
          return mockMediaQuery as unknown as MediaQueryList;
        }
        return {
          matches: false,
          addEventListener: () => {},
          removeEventListener: () => {},
        } as unknown as MediaQueryList;
      });
    fetchStub = sinon.stub(window, 'fetch');
    // Stub getFeatures (fetchPublic) and _checkTrackers (fetch with auth)
    fetchStub.callsFake(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString();
      if (url === '/api/v1/features') {
        return new Response(
          JSON.stringify({
            plugins: [],
            features: { audit_logs: false },
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        );
      }
      if (url === '/api/v1/trackers') {
        return new Response(JSON.stringify([]), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      if (url.startsWith('/api/v1/flows/executions')) {
        return new Response(JSON.stringify([]), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      if (url.startsWith('/api/v1/approval-requests')) {
        return new Response(JSON.stringify([]), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      return new Response(JSON.stringify({}), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    });
  });

  afterEach(() => {
    fetchStub.restore();
    matchMediaStub?.restore();
    localStorage.clear();
    delete (window as any).BRAND_CONFIG;
  });

  it('renders the component', async () => {
    const el = (await fixture(
      html`<console-shell></console-shell>`
    )) as ConsoleShell;

    await waitUntil(
      () => el.shadowRoot?.querySelector('.console-container') !== null,
      'Console container did not render'
    );

    expect(el).to.exist;
    expect(el.shadowRoot).to.exist;
  });

  it('has navigation sidebar structure', async () => {
    const el = (await fixture(
      html`<console-shell></console-shell>`
    )) as ConsoleShell;

    await waitUntil(
      () => el.shadowRoot?.querySelector('[role="navigation"]') !== null,
      'Navigation did not render'
    );

    const sidebar = el.shadowRoot?.querySelector('.sidebar');
    expect(sidebar).to.exist;
    expect(sidebar?.getAttribute('role')).to.equal('navigation');
    expect(sidebar?.getAttribute('aria-label')).to.equal('Console navigation');
  });

  it('has main view with header and content area', async () => {
    const el = (await fixture(
      html`<console-shell></console-shell>`
    )) as ConsoleShell;

    await waitUntil(
      () => el.shadowRoot?.querySelector('.main-view') !== null,
      'Main view did not render'
    );

    const mainView = el.shadowRoot?.querySelector('.main-view');
    expect(mainView).to.exist;

    const header = el.shadowRoot?.querySelector('console-header');
    expect(header).to.exist;

    const mainContent = el.shadowRoot?.querySelector('.main-content');
    expect(mainContent).to.exist;
  });

  it('highlights the active sidebar section for the current route', async () => {
    const originalPath = window.location.pathname;
    window.history.replaceState({}, '', '/console/tools');

    const el = (await fixture(
      html`<console-shell></console-shell>`
    )) as ConsoleShell;

    await waitUntil(
      () =>
        el.shadowRoot?.querySelector(
          'a.sidebar-link.active[href="/console/tools"]'
        ) !== null,
      'Active tools link did not render'
    );

    const toolsLink = el.shadowRoot?.querySelector(
      'a.sidebar-link.active[href="/console/tools"]'
    );
    expect(toolsLink?.getAttribute('aria-current')).to.equal('page');

    const overviewLink = el.shadowRoot?.querySelector(
      'a.sidebar-link[href="/console"]'
    );
    expect(overviewLink?.classList.contains('active')).to.be.false;

    window.history.replaceState({}, '', originalPath);
  });

  it('has sidebar menu with Overview and Tools links', async () => {
    const el = (await fixture(
      html`<console-shell></console-shell>`
    )) as ConsoleShell;

    await waitUntil(
      () => el.shadowRoot?.querySelector('sl-menu') !== null,
      'Sidebar menu did not render'
    );

    const overviewLink = el.shadowRoot?.querySelector('a[href="/console"]');
    expect(overviewLink).to.exist;

    const toolsLink = el.shadowRoot?.querySelector('a[href="/console/tools"]');
    expect(toolsLink).to.exist;
  });

  describe('responsive sidebar', () => {
    it('shows sidebar as open on desktop by default', async () => {
      const el = (await fixture(
        html`<console-shell></console-shell>`
      )) as ConsoleShell;

      await waitUntil(
        () => el.shadowRoot?.querySelector('.sidebar') !== null,
        'Sidebar did not render'
      );

      const sidebar = el.shadowRoot?.querySelector('.sidebar');
      expect(sidebar?.classList.contains('open')).to.be.true;
      expect(sidebar?.classList.contains('closed')).to.be.false;
    });

    it('toggles sidebar when hamburger is clicked', async () => {
      const el = (await fixture(
        html`<console-shell></console-shell>`
      )) as ConsoleShell;

      await waitUntil(
        () => el.shadowRoot?.querySelector('.sidebar') !== null,
        'Sidebar did not render'
      );

      const hamburger = el.shadowRoot?.querySelector(
        'sl-icon-button[name="list"]'
      ) as HTMLElement;
      expect(hamburger).to.exist;

      const sidebar = el.shadowRoot?.querySelector('.sidebar');
      expect(sidebar?.classList.contains('open')).to.be.true;

      hamburger.click();
      await el.updateComplete;

      expect(sidebar?.classList.contains('closed')).to.be.true;
      expect(sidebar?.classList.contains('open')).to.be.false;

      hamburger.click();
      await el.updateComplete;

      expect(sidebar?.classList.contains('open')).to.be.true;
      expect(sidebar?.classList.contains('closed')).to.be.false;
    });

    it('does not close sidebar when nav link is clicked on desktop', async () => {
      const el = (await fixture(
        html`<console-shell></console-shell>`
      )) as ConsoleShell;

      await waitUntil(
        () => el.shadowRoot?.querySelector('a[href="/console/tools"]') !== null,
        'Sidebar menu did not render'
      );

      const sidebar = el.shadowRoot?.querySelector('.sidebar');
      const toolsLink = el.shadowRoot?.querySelector(
        'a[href="/console/tools"]'
      ) as HTMLAnchorElement;

      expect(sidebar?.classList.contains('open')).to.be.true;
      toolsLink.addEventListener('click', (e) => e.preventDefault(), {
        once: true,
      });
      toolsLink.click();
      await el.updateComplete;

      expect(sidebar?.classList.contains('open')).to.be.true;
    });

    it('closes sidebar when nav link is clicked on mobile', async () => {
      const mockMediaQuery = createMatchMediaStub(true); // mobile
      matchMediaStub.restore();
      matchMediaStub = sinon
        .stub(window, 'matchMedia')
        .callsFake((query: string) => {
          if (query.includes(`${SIDEBAR_BREAKPOINT}`)) {
            return mockMediaQuery as unknown as MediaQueryList;
          }
          return {
            matches: false,
            addEventListener: () => {},
            removeEventListener: () => {},
          } as unknown as MediaQueryList;
        });

      const el = (await fixture(
        html`<console-shell></console-shell>`
      )) as ConsoleShell;

      await waitUntil(
        () => el.shadowRoot?.querySelector('a[href="/console/tools"]') !== null,
        'Sidebar menu did not render'
      );

      const sidebar = el.shadowRoot?.querySelector('.sidebar');
      const hamburger = el.shadowRoot?.querySelector(
        'sl-icon-button[name="list"]'
      ) as HTMLElement;
      const toolsLink = el.shadowRoot?.querySelector(
        'a[href="/console/tools"]'
      ) as HTMLAnchorElement;

      hamburger.click();
      await el.updateComplete;
      expect(sidebar?.classList.contains('open')).to.be.true;

      toolsLink.addEventListener('click', (e) => e.preventDefault(), {
        once: true,
      });
      toolsLink.click();
      await el.updateComplete;

      expect(sidebar?.classList.contains('closed')).to.be.true;
    });
  });
});
