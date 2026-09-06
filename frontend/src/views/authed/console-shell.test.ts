import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import { invalidateApiCaches } from '../../api';
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

describe('ConsoleShell', () => {
  let fetchStub: sinon.SinonStub;
  let matchMediaStub: sinon.SinonStub;

  beforeEach(() => {
    invalidateApiCaches();
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
      if (url.endsWith('/api/v1/features')) {
        return new Response(
          JSON.stringify({
            plugins: [],
            features: { audit_logs: false },
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        );
      }
      if (url.endsWith('/api/v1/auth/users/me')) {
        // permissions: null => RBAC inactive, so nav stays unrestricted in tests
        return new Response(
          JSON.stringify({
            username: 'test',
            email: 'test@example.com',
            email_verified: true,
            permissions: null,
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        );
      }
      if (url.endsWith('/api/v1/trackers')) {
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
    invalidateApiCaches();
  });

  describe('window chrome', () => {
    it('drops the sidebar, the header and the bell for ?window=1', async () => {
      const originalUrl = window.location.pathname + window.location.search;
      window.history.replaceState(
        {},
        '',
        '/console/agents/agent-1/talk?window=1'
      );
      try {
        const el = (await fixture(
          html`<console-shell></console-shell>`
        )) as ConsoleShell;
        await waitUntil(
          () => el.shadowRoot?.querySelector('.console-container') !== null,
          'Console container did not render'
        );

        expect(el.shadowRoot!.querySelector('.sidebar-wrapper')).to.not.exist;
        expect(el.shadowRoot!.querySelector('console-header')).to.not.exist;
        expect(el.shadowRoot!.querySelector('approval-bypass-banner')).to.not
          .exist;
        // The popup content is the only row and it fills the window.
        expect(el.shadowRoot!.querySelector('.main-view.window-mode')).to.exist;
        expect(el.shadowRoot!.querySelector('.main-content.full-bleed')).to
          .exist;
        // Dialogs centre on the window, not on a sidebar that is not there.
        expect(el.style.getPropertyValue('--console-main-offset')).to.equal(
          '0px'
        );
      } finally {
        window.history.replaceState({}, '', originalUrl);
      }
    });

    it('keeps the full chrome on the same route without the flag', async () => {
      const originalUrl = window.location.pathname + window.location.search;
      window.history.replaceState({}, '', '/console/agents/agent-1/talk');
      try {
        const el = (await fixture(
          html`<console-shell></console-shell>`
        )) as ConsoleShell;
        await waitUntil(
          () => el.shadowRoot?.querySelector('console-header') !== null,
          'Header did not render'
        );
        expect(el.shadowRoot!.querySelector('.sidebar-wrapper')).to.exist;
      } finally {
        window.history.replaceState({}, '', originalUrl);
      }
    });
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

  it('publishes the content offset so dialogs centre on the page, not the window', async () => {
    const el = (await fixture(
      html`<console-shell></console-shell>`
    )) as ConsoleShell;

    await waitUntil(
      () => el.shadowRoot?.querySelector('.main-view') !== null,
      'Main view did not render'
    );

    // Desktop: the sidebar takes 250px out of the window, so a dialog that
    // centres on the window sits 125px left of the content it belongs to.
    expect(el.style.getPropertyValue('--console-main-offset')).to.equal(
      '250px'
    );

    // Closing the sidebar gives the content the full window back.
    (el as any)._sidebarOpen = false;
    await el.updateComplete;
    expect(el.style.getPropertyValue('--console-main-offset')).to.equal('0px');
  });

  it('stops offsetting dialogs when the sidebar overlays the page', async () => {
    const el = (await fixture(
      html`<console-shell></console-shell>`
    )) as ConsoleShell;

    await waitUntil(
      () => el.shadowRoot?.querySelector('.main-view') !== null,
      'Main view did not render'
    );

    (el as any)._isMobile = true;
    (el as any)._sidebarOpen = true;
    await el.updateComplete;

    // On a phone the sidebar floats above the page instead of beside it.
    expect(el.style.getPropertyValue('--console-main-offset')).to.equal('0px');
  });

  it('paints the page from the ladder and sets the compact type scale', async () => {
    // The ladder sheet is loaded by main.css, not by the test page, so pin
    // the rung to a sentinel colour: if the rule stops reading the token the
    // computed colour stops matching. Naming a neutral step here instead is
    // exactly the bug wave 4 removed, because that step inverts in dark.
    document.documentElement.style.setProperty(
      '--console-page',
      'rgb(1, 2, 3)'
    );

    try {
      const el = (await fixture(
        html`<console-shell></console-shell>`
      )) as ConsoleShell;

      await waitUntil(
        () => el.shadowRoot?.querySelector('.main-content') !== null,
        'Main content did not render'
      );

      const mainContent = el.shadowRoot?.querySelector(
        '.main-content'
      ) as HTMLElement;
      const styles = getComputedStyle(mainContent);

      expect(styles.backgroundColor).to.equal('rgb(1, 2, 3)');
      expect(styles.fontSize).to.equal('14px');
      expect(styles.fontVariantNumeric).to.contain('tabular-nums');
    } finally {
      document.documentElement.style.removeProperty('--console-page');
    }
  });

  it('puts the sidebar on the card rung, hairline away from the page', async () => {
    document.documentElement.style.setProperty(
      '--console-surface',
      'rgb(7, 8, 9)'
    );
    document.documentElement.style.setProperty(
      '--console-hairline',
      'rgb(10, 11, 12)'
    );

    try {
      const el = (await fixture(
        html`<console-shell></console-shell>`
      )) as ConsoleShell;

      await waitUntil(
        () => el.shadowRoot?.querySelector('.sidebar') !== null,
        'Sidebar did not render'
      );

      const sidebar = el.shadowRoot?.querySelector('.sidebar') as HTMLElement;
      const styles = getComputedStyle(sidebar);

      // Sidebar and cards share one rung; a hairline, not a second gray
      // step, is what separates it from the page.
      expect(styles.backgroundColor).to.equal('rgb(7, 8, 9)');
      expect(styles.borderRightColor).to.equal('rgb(10, 11, 12)');
      expect(styles.borderRightWidth).to.equal('1px');
    } finally {
      document.documentElement.style.removeProperty('--console-surface');
      document.documentElement.style.removeProperty('--console-hairline');
    }
  });

  it('marks the active nav item with a rule and colour, not bold', async () => {
    const originalPath = window.location.pathname;
    window.history.replaceState({}, '', '/console/tools');
    document.documentElement.style.setProperty(
      '--sl-color-primary-600',
      'rgb(4, 5, 6)'
    );

    try {
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

      const link = el.shadowRoot?.querySelector(
        'a.sidebar-link.active[href="/console/tools"]'
      ) as HTMLElement;
      const styles = getComputedStyle(link);
      // A 3px primary rule and a primary label carry "you are here"; weight
      // stays at 600 so the nav does not shout.
      expect(styles.borderLeftWidth).to.equal('3px');
      expect(styles.borderLeftColor).to.equal('rgb(4, 5, 6)');

      const label = link.querySelector('.sidebar-label') as HTMLElement;
      const labelStyles = getComputedStyle(label);
      expect(labelStyles.fontWeight).to.equal('600');
      expect(labelStyles.fontSize).to.equal('14px');
    } finally {
      document.documentElement.style.removeProperty('--sl-color-primary-600');
      window.history.replaceState({}, '', originalPath);
    }
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

  it('lists Models before Tools in the sidebar', async () => {
    const el = (await fixture(
      html`<console-shell></console-shell>`
    )) as ConsoleShell;

    await waitUntil(
      () =>
        el.shadowRoot?.querySelector('a[href="/console/ai-models"]') !== null,
      'Sidebar models link did not render'
    );

    const hrefs = Array.from(
      el.shadowRoot?.querySelectorAll('a.sidebar-link') ?? []
    ).map((link) => link.getAttribute('href'));
    const models = hrefs.indexOf('/console/ai-models');
    const tools = hrefs.indexOf('/console/tools');
    expect(models, 'models link is in the sidebar').to.be.greaterThan(-1);
    expect(tools, 'tools link is in the sidebar').to.be.greaterThan(-1);
    // Models is the everyday destination; Tools (and Policies under it) is
    // configuration, so it reads after Models.
    expect(models).to.be.lessThan(tools);
  });

  describe('Policies preview gate', () => {
    /** Re-stub fetch with a chosen policies_console flag and superuser bit. */
    function stubShell(
      opts: { policiesConsole?: boolean; isSuperuser?: boolean } = {}
    ) {
      invalidateApiCaches();
      fetchStub.callsFake(async (input: RequestInfo | URL) => {
        const url = typeof input === 'string' ? input : input.toString();
        if (url.endsWith('/api/v1/features')) {
          return new Response(
            JSON.stringify({
              plugins: [],
              features: {
                audit_logs: false,
                policies_console: opts.policiesConsole === true,
              },
            }),
            { status: 200, headers: { 'Content-Type': 'application/json' } }
          );
        }
        if (url.endsWith('/api/v1/auth/users/me')) {
          return new Response(
            JSON.stringify({
              username: 'test',
              email: 'test@example.com',
              email_verified: true,
              is_superuser: opts.isSuperuser === true,
              permissions: null,
            }),
            { status: 200, headers: { 'Content-Type': 'application/json' } }
          );
        }
        return new Response(JSON.stringify({}), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      });
    }

    async function mountShell() {
      const el = (await fixture(
        html`<console-shell></console-shell>`
      )) as ConsoleShell;
      // The nav renders before /features resolves, so wait for the load to
      // finish rather than for a link that is always present.
      await waitUntil(
        () =>
          (el as unknown as { _featuresLoaded: boolean })._featuresLoaded &&
          (el as unknown as { _permissionsLoaded: boolean })._permissionsLoaded,
        'Features and permissions did not load'
      );
      await el.updateComplete;
      return el;
    }

    it('hides the Policies link when the flag is off', async () => {
      stubShell();
      const el = await mountShell();

      expect(el.shadowRoot?.querySelector('a[href="/console/policies"]')).to.not
        .exist;
    });

    it('shows the Policies link when policies_console is on', async () => {
      stubShell({ policiesConsole: true });
      const el = await mountShell();

      const policiesLink = el.shadowRoot?.querySelector(
        'a[href="/console/policies"]'
      );
      expect(policiesLink).to.exist;
      expect(policiesLink?.textContent).to.contain('Policies');
    });

    it('shows the Policies link to an instance admin with the flag off', async () => {
      stubShell({ isSuperuser: true });
      const el = await mountShell();

      expect(el.shadowRoot?.querySelector('a[href="/console/policies"]')).to
        .exist;
    });

    it('renders permission-denied on a direct /console/policies URL when hidden', async () => {
      const originalPath = window.location.pathname;
      window.history.replaceState({}, '', '/console/policies');
      stubShell();

      const el = await mountShell();

      expect(el.shadowRoot?.querySelector('permission-denied')).to.exist;
      expect(el.shadowRoot?.querySelector('.main-content slot')).to.not.exist;

      window.history.replaceState({}, '', originalPath);
    });

    it('never assigns the routed child on a denied path', async () => {
      // B-P1: the shell used to keep the outlet slot while it renders
      // permission-denied, so the routed view painted behind the refusal.
      const originalPath = window.location.pathname;
      window.history.replaceState({}, '', '/console/policies');
      stubShell();

      const el = (await fixture(
        html`<console-shell><div id="routed-view">rules</div></console-shell>`
      )) as ConsoleShell;
      await waitUntil(
        () =>
          (el as unknown as { _featuresLoaded: boolean })._featuresLoaded &&
          (el as unknown as { _permissionsLoaded: boolean })._permissionsLoaded,
        'Features and permissions did not load'
      );
      await el.updateComplete;

      const child = el.querySelector('#routed-view') as HTMLElement;
      expect(child).to.exist;
      expect(child.assignedSlot).to.equal(null);
      expect(child.getClientRects().length).to.equal(0);
      expect(el.shadowRoot?.querySelector('permission-denied')).to.exist;

      window.history.replaceState({}, '', originalPath);
    });

    it('holds the outlet back until permissions have loaded', async () => {
      // Permissions in flight is not "allowed": the slot must not render
      // before the shell knows, or a gated view mounts and fetches first.
      const originalPath = window.location.pathname;
      window.history.replaceState({}, '', '/console/policies');
      stubShell();

      const el = (await fixture(
        html`<console-shell></console-shell>`
      )) as ConsoleShell;
      (el as unknown as { _featuresLoaded: boolean })._featuresLoaded = false;
      (el as unknown as { _permissionsLoaded: boolean })._permissionsLoaded =
        false;
      el.requestUpdate();
      await el.updateComplete;

      expect(el.shadowRoot?.querySelector('.main-content slot')).to.not.exist;
      expect(el.shadowRoot?.querySelector('permission-denied')).to.not.exist;

      window.history.replaceState({}, '', originalPath);
    });

    it('renders the page on a direct URL when the flag is on', async () => {
      const originalPath = window.location.pathname;
      window.history.replaceState({}, '', '/console/policies');
      stubShell({ policiesConsole: true });

      const el = await mountShell();

      expect(el.shadowRoot?.querySelector('permission-denied')).to.not.exist;

      window.history.replaceState({}, '', originalPath);
    });
  });

  it('nests Sessions and Approvals under Audit without All events when audit_logs is off', async () => {
    const el = (await fixture(
      html`<console-shell></console-shell>`
    )) as ConsoleShell;

    await waitUntil(
      () =>
        el.shadowRoot?.querySelector('a[href="/console/runtime-sessions"]') !==
        null,
      'Sessions link did not render'
    );

    const auditSections = Array.from(
      el.shadowRoot?.querySelectorAll('sl-details.nav-section') ?? []
    );
    const auditSection = auditSections.find((section) =>
      section.textContent?.includes('Audit')
    );
    expect(auditSection).to.exist;

    expect(el.shadowRoot?.querySelector('a[href="/console/runtime-sessions"]'))
      .to.exist;
    expect(el.shadowRoot?.querySelector('a[href="/console/approvals"]')).to
      .exist;
    expect(el.shadowRoot?.querySelector('a[href="/console/audit"]')).to.not
      .exist;
    expect(el.shadowRoot?.querySelector('a[href="/console/cost"]')).to.exist;
  });

  it('nests Runners under Settings instead of the top-level nav', async () => {
    const el = (await fixture(
      html`<console-shell></console-shell>`
    )) as ConsoleShell;

    await waitUntil(
      () =>
        el.shadowRoot?.querySelector('a[href="/console/settings/api-keys"]') !==
        null,
      'Settings links did not render'
    );

    expect(el.shadowRoot?.querySelector('a[href="/console/runners"]')).to.not
      .exist;

    const settingsSections = Array.from(
      el.shadowRoot?.querySelectorAll('sl-details.nav-section') ?? []
    );
    const settingsSection = settingsSections.find((section) =>
      section.textContent?.includes('Settings')
    );
    expect(settingsSection).to.exist;
    expect(
      settingsSection?.querySelector('a[href="/console/settings/runners"]')
    ).to.exist;
  });

  it('shows All events under Audit when audit_logs is enabled', async () => {
    invalidateApiCaches();
    fetchStub.callsFake(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString();
      if (url.endsWith('/api/v1/features')) {
        return new Response(
          JSON.stringify({
            plugins: [],
            features: { audit_logs: true },
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        );
      }
      if (url.endsWith('/api/v1/auth/users/me')) {
        return new Response(
          JSON.stringify({
            username: 'test',
            email: 'test@example.com',
            email_verified: true,
            permissions: null,
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        );
      }
      return new Response(JSON.stringify({}), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    });

    const el = (await fixture(
      html`<console-shell></console-shell>`
    )) as ConsoleShell;

    await waitUntil(
      () => el.shadowRoot?.querySelector('a[href="/console/audit"]') !== null,
      'All events link did not render'
    );

    const auditLink = el.shadowRoot?.querySelector('a[href="/console/audit"]');
    expect(auditLink?.textContent).to.contain('All events');
    expect(el.shadowRoot?.querySelector('a[href="/console/runtime-sessions"]'))
      .to.exist;
    expect(el.shadowRoot?.querySelector('a[href="/console/approvals"]')).to
      .exist;
  });

  it('opens the Audit section when a nested route is active', async () => {
    const originalPath = window.location.pathname;
    window.history.replaceState({}, '', '/console/approvals');

    const el = (await fixture(
      html`<console-shell></console-shell>`
    )) as ConsoleShell;

    await waitUntil(
      () =>
        el.shadowRoot?.querySelector(
          'a.sidebar-link.active[href="/console/approvals"]'
        ) !== null,
      'Active approvals link did not render'
    );

    const auditSections = Array.from(
      el.shadowRoot?.querySelectorAll('sl-details.nav-section') ?? []
    );
    const auditSection = auditSections.find((section) =>
      section.textContent?.includes('Audit')
    ) as HTMLElement | undefined;
    expect(auditSection?.hasAttribute('open')).to.be.true;

    window.history.replaceState({}, '', originalPath);
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
