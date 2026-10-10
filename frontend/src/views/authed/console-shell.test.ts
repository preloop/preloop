import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import { invalidateApiCaches } from '../../api';
import { PENDING_APPROVALS_EVENT } from '../../components/console-header';
import { LOCATION_CHANGED, Router } from '../../router';
import { publishAttentionSummary } from '../../utils/attention-summary';
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
    sessionStorage.removeItem('preloop:attention-summary');
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
        expect(el.shadowRoot!.querySelector('usage-nudge-banner')).to.not.exist;
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
        // Usage sits with the other banners under the header. It paints
        // nothing of its own until the endpoint answers with a limit.
        expect(el.shadowRoot!.querySelector('usage-nudge-banner')).to.exist;
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
      () => el.shadowRoot?.querySelector('nav') !== null,
      'Navigation did not render'
    );

    const sidebar = el.shadowRoot?.querySelector('.sidebar');
    expect(sidebar).to.exist;
    expect(sidebar?.tagName).to.equal('NAV');
    expect(sidebar?.getAttribute('aria-label')).to.equal('Console navigation');
  });

  it('stacks the kill-switch banner above the bypass banner', async () => {
    // A halted account is the most severe governance state, so it reads
    // first when both are active.
    const el = (await fixture(
      html`<console-shell></console-shell>`
    )) as ConsoleShell;
    await el.updateComplete;
    const order = Array.from(
      el.shadowRoot!.querySelectorAll(
        'kill-switch-banner, approval-bypass-banner, usage-nudge-banner'
      )
    ).map((node) => node.localName);
    expect(order).to.eql([
      'kill-switch-banner',
      'approval-bypass-banner',
      'usage-nudge-banner',
    ]);
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
      '--console-link-color',
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
      document.documentElement.style.removeProperty('--console-link-color');
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
      () => el.shadowRoot?.querySelector('#console-nav > ul') !== null,
      'Sidebar menu did not render'
    );

    expect(el.shadowRoot?.querySelector('#console-nav')?.tagName).to.equal(
      'NAV'
    );
    expect(el.shadowRoot?.querySelector('sl-menu, sl-menu-item')).to.equal(
      null
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

  it('reaches the personal settings pages from the sidebar', async () => {
    const el = (await fixture(
      html`<console-shell></console-shell>`
    )) as ConsoleShell;

    await waitUntil(
      () => el.shadowRoot?.querySelector('a[href="/console/tools"]') !== null,
      'Sidebar did not render'
    );

    const hrefs = Array.from(
      el.shadowRoot?.querySelectorAll('a.sidebar-link') ?? []
    ).map((link) => link.getAttribute('href'));
    // These four had routes and a place in the avatar menu, but no way in
    // from the sidebar.
    expect(hrefs).to.include('/console/settings/profile');
    expect(hrefs).to.include('/console/settings/security');
    expect(hrefs).to.include('/console/settings/appearance');
    expect(hrefs).to.include('/console/settings/notification-preferences');
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

  it('keeps Approvals and Sessions under Audit, not top-level', async () => {
    const el = (await fixture(
      html`<console-shell></console-shell>`
    )) as ConsoleShell;

    await waitUntil(
      () =>
        el.shadowRoot?.querySelector('a[href="/console/runtime-sessions"]') !==
        null,
      'Sessions link did not render'
    );

    const approvals = el.shadowRoot?.querySelector(
      'a[href="/console/approvals"]'
    );
    const sessions = el.shadowRoot?.querySelector(
      'a[href="/console/runtime-sessions"]'
    );
    const audit = approvals?.closest('details.nav-section');
    expect(audit?.textContent).to.contain('Audit');
    expect(sessions?.closest('details.nav-section')).to.equal(audit);
    // Without audit_logs there is no All events entry.
    expect(el.shadowRoot?.querySelector('a[href="/console/audit"]')).to.not
      .exist;
  });

  it('orders the top level as Overview, the product pages, Audit, Settings, Emergency', async () => {
    fetchStub.callsFake(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString();
      if (url.endsWith('/api/v1/features')) {
        return new Response(
          JSON.stringify({
            plugins: [],
            features: { audit_logs: true, policies_console: true },
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        );
      }
      return new Response(JSON.stringify([]), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    });
    const el = (await fixture(
      html`<console-shell></console-shell>`
    )) as ConsoleShell;
    await waitUntil(
      () => el.shadowRoot?.querySelector('a[href="/console/policies"]'),
      'Policies link did not render'
    );

    const menu = el.shadowRoot!.querySelector('#console-nav > ul')!;
    const rows = Array.from(menu.children)
      .map((child) =>
        child.matches('details.nav-section')
          ? child.querySelector('summary .sidebar-label')?.textContent
          : child.querySelector('.sidebar-label')?.textContent
      )
      .filter((label): label is string => !!label)
      .map((label) => label.trim());
    expect(rows).to.deep.equal([
      'Overview',
      'Agents',
      'Flows',
      'Models',
      'Tools',
      'Policies',
      'Trackers',
      'Cost',
      'API usage',
      'Audit',
      'Settings',
      'Emergency',
    ]);
  });

  it('orders the Audit group Approvals, Sessions, All events, Artifacts, Records', async () => {
    fetchStub.callsFake(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString();
      if (url.endsWith('/api/v1/features')) {
        return new Response(
          JSON.stringify({ plugins: [], features: { audit_logs: true } }),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        );
      }
      return new Response(JSON.stringify([]), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    });
    const el = (await fixture(
      html`<console-shell></console-shell>`
    )) as ConsoleShell;
    await waitUntil(
      () =>
        el.shadowRoot?.querySelector('a[href="/console/audit"]') &&
        el.shadowRoot?.querySelector('a[href="/console/settings/records"]'),
      'Audit links did not render'
    );

    const audit = el
      .shadowRoot!.querySelector('a[href="/console/audit"]')!
      .closest('details.nav-section')!;
    const hrefs = Array.from(audit.querySelectorAll('a.sidebar-link')).map(
      (a) => a.getAttribute('href')
    );
    expect(hrefs).to.deep.equal([
      '/console/approvals',
      '/console/runtime-sessions',
      '/console/audit',
      '/console/artifacts',
      '/console/settings/records',
    ]);
  });

  it('has no Needs attention entry; the Overview banner links there', async () => {
    publishAttentionSummary([
      {
        id: 'flow:flow-1',
        kind: 'flow',
        severity: 'critical',
        title: 'Pull Request Reviewer',
        detail: '11 failed runs',
        href: '/console/flows',
        at: null,
        fingerprint: 'flow-1:11',
        dismissable: true,
      },
    ]);

    const el = (await fixture(
      html`<console-shell></console-shell>`
    )) as ConsoleShell;
    await waitUntil(
      () => el.shadowRoot?.querySelector('a[href="/console/cost"]') !== null,
      'nav did not render'
    );

    expect(el.shadowRoot?.querySelector('a[href="/console/attention"]')).to.not
      .exist;
    expect(
      el.shadowRoot?.querySelector('#console-nav')?.textContent
    ).to.not.contain('Needs attention');
  });

  it('badges Approvals with the pending count published by the header', async () => {
    const el = (await fixture(
      html`<console-shell></console-shell>`
    )) as ConsoleShell;

    await waitUntil(
      () =>
        el.shadowRoot?.querySelector('a[href="/console/approvals"]') !== null,
      'Approvals link did not render'
    );

    // The real header child also publishes its (empty) count; wait for its
    // initial load to settle so a later dispatch is not overwritten by it.
    const header = el.shadowRoot?.querySelector(
      'console-header'
    ) as unknown as { loadingPendingApprovals: boolean } & HTMLElement;
    await waitUntil(
      () => !header.loadingPendingApprovals,
      'header approval load never settled'
    );
    await header.updateComplete;

    // No pending approvals: no badge.
    expect(
      el.shadowRoot
        ?.querySelector('a[href="/console/approvals"]')
        ?.querySelector('sl-badge')
    ).to.not.exist;

    window.dispatchEvent(
      new CustomEvent<number>(PENDING_APPROVALS_EVENT, { detail: 3 })
    );
    await el.updateComplete;

    const badge = el.shadowRoot
      ?.querySelector('a[href="/console/approvals"]')
      ?.querySelector('sl-badge');
    expect(badge?.textContent).to.contain('3');

    // The closed Audit group repeats the count on its header, so it stays
    // visible without opening the group; an open group hides the copy.
    const audit = el
      .shadowRoot!.querySelector('a[href="/console/approvals"]')!
      .closest('details.nav-section') as HTMLElement & { open: boolean };
    const headerBadge = audit.querySelector(
      'summary sl-badge.nav-section-badge'
    ) as HTMLElement;
    expect(headerBadge?.textContent).to.contain('3');
    expect(audit.open).to.equal(false);
    expect(getComputedStyle(headerBadge).display).to.not.equal('none');
    audit.open = true;
    await el.updateComplete;
    expect(getComputedStyle(headerBadge).display).to.equal('none');
  });

  it('highlights Approvals for a single approval route', async () => {
    const originalPath = window.location.pathname;
    window.history.replaceState({}, '', '/console/approval/123');

    try {
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
    } finally {
      window.history.replaceState({}, '', originalPath);
    }
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
      el.shadowRoot?.querySelectorAll('details.nav-section') ?? []
    );
    const settingsSection = settingsSections.find((section) =>
      section.textContent?.includes('Settings')
    );
    expect(settingsSection).to.exist;
    expect(
      settingsSection?.querySelector('a[href="/console/settings/runners"]')
    ).to.exist;
  });

  it('offers the plan page only where something is sold', async () => {
    // Default stub: no billing plugin, so the console sells nothing and the
    // link would lead to a page with nothing to say.
    const el = (await fixture(
      html`<console-shell></console-shell>`
    )) as ConsoleShell;
    await waitUntil(
      () =>
        el.shadowRoot?.querySelector('a[href="/console/settings/api-keys"]') !==
        null,
      'Settings links did not render'
    );

    expect(el.shadowRoot?.querySelector('a[href="/console/settings/plan"]')).to
      .not.exist;
    // The kill switch is core, so its page is offered either way.
    expect(
      el.shadowRoot?.querySelector('a[href="/console/settings/emergency"]')
    ).to.exist;
  });

  it('shows the plan page when the billing plugin is present', async () => {
    invalidateApiCaches();
    fetchStub.callsFake(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString();
      if (url.endsWith('/api/v1/features')) {
        return new Response(
          JSON.stringify({
            plugins: ['billing'],
            features: { billing: true, user_management: true },
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
      () =>
        el.shadowRoot?.querySelector('a[href="/console/settings/plan"]') !==
        null,
      'Plan link did not render'
    );
    expect(el.shadowRoot?.querySelector('a[href="/console/settings/plan"]')).to
      .exist;
  });

  it('puts Plan directly under Account and above Users', async () => {
    // What the account pays for belongs with the account, not below the list
    // of people in it, where it read as a per-person setting.
    invalidateApiCaches();
    fetchStub.callsFake(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString();
      if (url.endsWith('/api/v1/features')) {
        return new Response(
          JSON.stringify({
            plugins: ['billing'],
            features: { billing: true, user_management: true },
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
      () =>
        el.shadowRoot?.querySelector('a[href="/console/settings/plan"]') !==
        null,
      'Plan link did not render'
    );

    const settingsPaths = Array.from(
      el.shadowRoot?.querySelectorAll<HTMLAnchorElement>(
        'a[href^="/console/settings/"]'
      ) ?? []
    ).map((link) => link.getAttribute('href'));
    const order = ['account', 'plan', 'users'].map((page) =>
      settingsPaths.indexOf(`/console/settings/${page}`)
    );
    expect(order[0]).to.be.greaterThan(-1);
    expect(order[1]).to.equal(order[0] + 1);
    expect(order[2]).to.equal(order[1] + 1);
  });

  describe('Records placement', () => {
    /** Re-stub fetch with a chosen permission set; audit_logs stays off. */
    function stubRecords(permissions: string[]) {
      invalidateApiCaches();
      fetchStub.callsFake(async (input: RequestInfo | URL) => {
        const url = typeof input === 'string' ? input : input.toString();
        if (url.endsWith('/api/v1/features')) {
          return new Response(
            JSON.stringify({
              plugins: [],
              features: { user_management: true },
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
              permissions,
            }),
            { status: 200, headers: { 'Content-Type': 'application/json' } }
          );
        }
        if (
          url.includes('approval-requests') ||
          url.endsWith('/api/v1/trackers')
        ) {
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
    }

    async function mountShell() {
      const el = (await fixture(
        html`<console-shell></console-shell>`
      )) as ConsoleShell;
      await waitUntil(
        () =>
          (el as unknown as { _featuresLoaded: boolean })._featuresLoaded &&
          (el as unknown as { _permissionsLoaded: boolean })._permissionsLoaded,
        'Features and permissions did not load'
      );
      await el.updateComplete;
      return el;
    }

    function recordsLink(el: ConsoleShell): Element | null | undefined {
      return el.shadowRoot?.querySelector(
        'a[href="/console/settings/records"]'
      );
    }

    it('hides Records unless the operator can read the audit or policies', async () => {
      stubRecords(['view_flows']);
      const el = await mountShell();

      expect(recordsLink(el)).to.not.exist;
      // Artifacts and Sessions share the view_runtime_sessions gate.
      expect(el.shadowRoot?.querySelector('a[href="/console/artifacts"]')).to
        .not.exist;
      expect(
        el.shadowRoot?.querySelector('a[href="/console/runtime-sessions"]')
      ).to.not.exist;
      // Approvals is gated on view_approvals, which this user does not have.
      expect(el.shadowRoot?.querySelector('a[href="/console/approvals"]')).to
        .not.exist;
    });

    it('shows Records under Audit, not Settings, with view_audit_logs', async () => {
      stubRecords(['view_audit_logs']);
      const el = await mountShell();

      const link = recordsLink(el);
      expect(link).to.exist;
      const group = link?.closest('details.nav-section');
      expect(group?.textContent).to.contain('Audit');
      expect(group?.textContent).to.not.contain('Settings');
    });

    it('shows Records under Audit with view_policies', async () => {
      stubRecords(['view_policies']);
      const el = await mountShell();

      const link = recordsLink(el);
      expect(link).to.exist;
      expect(link?.closest('details.nav-section')?.textContent).to.contain(
        'Audit'
      );
    });
  });

  it('still offers the plan page where there is no user management', async () => {
    // The two conditions are separate: a deployment that sells plans but does
    // not manage users keeps its way in.
    invalidateApiCaches();
    fetchStub.callsFake(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString();
      if (url.endsWith('/api/v1/features')) {
        return new Response(
          JSON.stringify({
            plugins: ['billing'],
            features: { billing: true, user_management: false },
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
      () =>
        el.shadowRoot?.querySelector('a[href="/console/settings/plan"]') !==
        null,
      'Plan link did not render'
    );
    // Account is core (account name, artifact storage), so it stays; the
    // people pages need user management and do not.
    expect(el.shadowRoot?.querySelector('a[href="/console/settings/account"]'))
      .to.exist;
    expect(el.shadowRoot?.querySelector('a[href="/console/settings/users"]')).to
      .not.exist;
  });

  it('offers Account on an open-source install without user management', async () => {
    // Default stub: no plugins. The account page holds the account name and
    // the session artifact storage card, which core pages deep-link to.
    const el = (await fixture(
      html`<console-shell></console-shell>`
    )) as ConsoleShell;
    await waitUntil(
      () =>
        el.shadowRoot?.querySelector('a[href="/console/settings/api-keys"]') !==
        null,
      'Settings links did not render'
    );
    expect(el.shadowRoot?.querySelector('a[href="/console/settings/account"]'))
      .to.exist;
  });

  it('groups Settings under labels and hides a label with nothing under it', async () => {
    const el = (await fixture(
      html`<console-shell></console-shell>`
    )) as ConsoleShell;
    await waitUntil(
      () =>
        el.shadowRoot?.querySelector('a[href="/console/settings/api-keys"]') !==
        null,
      'Settings links did not render'
    );
    const labels = Array.from(
      el.shadowRoot?.querySelectorAll('li.nav-group-label') ?? []
    ).map((label) => label.textContent?.trim());
    // No user or team management in the default stub: no people heading.
    expect(labels).to.deep.equal(['Account', 'Developers', 'Personal']);
    expect(
      el.shadowRoot?.querySelector('a[href="/console/settings/api-keys"]')
        ?.textContent
    ).to.contain('API keys');
  });

  it('puts the kill switch outside Settings, one click from any page', async () => {
    const el = (await fixture(
      html`<console-shell></console-shell>`
    )) as ConsoleShell;
    await waitUntil(
      () =>
        el.shadowRoot?.querySelector(
          'a[href="/console/settings/emergency"]'
        ) !== null,
      'Emergency link did not render'
    );
    const emergency = el.shadowRoot?.querySelector(
      'a[href="/console/settings/emergency"]'
    );
    expect(emergency?.closest('details')).to.equal(null);
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
    window.history.replaceState({}, '', '/console/artifacts');

    try {
      const el = (await fixture(
        html`<console-shell></console-shell>`
      )) as ConsoleShell;

      await waitUntil(
        () =>
          el.shadowRoot?.querySelector(
            'a.sidebar-link.active[href="/console/artifacts"]'
          ) !== null,
        'Active artifacts link did not render'
      );

      const auditSections = Array.from(
        el.shadowRoot?.querySelectorAll('details.nav-section') ?? []
      );
      const auditSection = auditSections.find((section) =>
        section.textContent?.includes('Audit')
      ) as HTMLElement | undefined;
      expect(auditSection?.hasAttribute('open')).to.be.true;
    } finally {
      window.history.replaceState({}, '', originalPath);
    }
  });

  describe('wayfinding on pages without a nav entry', () => {
    let originalPath: string;
    let originalSearch: string;

    beforeEach(() => {
      originalPath = window.location.pathname;
      originalSearch = window.location.search;
    });

    afterEach(() => {
      window.history.replaceState({}, '', originalPath + originalSearch);
    });

    function auditSection(el: ConsoleShell): HTMLElement | undefined {
      return Array.from(
        el.shadowRoot?.querySelectorAll('details.nav-section') ?? []
      ).find((section) => section.textContent?.includes('Audit')) as
        HTMLElement | undefined;
    }

    /** RBAC on, with the given permissions. */
    function withPermissions(permissions: string[]) {
      invalidateApiCaches();
      fetchStub.callsFake(async (input: RequestInfo | URL) => {
        const url = typeof input === 'string' ? input : input.toString();
        if (url.endsWith('/api/v1/features')) {
          return new Response(JSON.stringify({ plugins: [], features: {} }), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        }
        if (url.endsWith('/api/v1/auth/users/me')) {
          return new Response(
            JSON.stringify({
              username: 'test',
              email: 'test@example.com',
              email_verified: true,
              permissions,
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

    async function loaded(): Promise<ConsoleShell> {
      const el = (await fixture(
        html`<console-shell></console-shell>`
      )) as ConsoleShell;
      await waitUntil(
        () => (el as any)._featuresLoaded && (el as any)._permissionsLoaded,
        'features and permissions did not load'
      );
      await el.updateComplete;
      return el;
    }

    it('highlights Approvals and opens Audit on a single approval', async () => {
      // The deep-link target of every approval notification, Slack and email.
      // Approvals lives under Audit, so the group opens to show where you are.
      window.history.replaceState({}, '', '/console/approval/req-123');
      const el = await loaded();
      const approvals = el.shadowRoot!.querySelector(
        'a.sidebar-link.active[href="/console/approvals"]'
      );
      expect(approvals).to.exist;
      expect(approvals?.closest('details.nav-section')).to.equal(
        auditSection(el)
      );
      expect(auditSection(el)?.hasAttribute('open')).to.be.true;
    });

    it('gives API usage its own entry under Cost', async () => {
      window.history.replaceState({}, '', '/console/api-usage');
      const el = await loaded();
      const link = el.shadowRoot!.querySelector(
        'a.sidebar-link[href="/console/api-usage"]'
      );
      expect(link).to.exist;
      expect(link!.classList.contains('active')).to.be.true;
      expect(link!.getAttribute('aria-current')).to.equal('page');
      expect(link!.textContent).to.contain('Gateway traffic and rate limits');
      // It sits right after Cost, and Cost no longer claims the page.
      const cost = el.shadowRoot!.querySelector(
        'a.sidebar-link[href="/console/cost"]'
      )!;
      expect(cost.classList.contains('active')).to.be.false;
      expect(cost.closest('li')!.nextElementSibling).to.equal(
        link!.closest('li')
      );
    });

    it('does not mistake Approvals for the single-approval alias the other way', async () => {
      window.history.replaceState({}, '', '/console/approvals');
      const el = await loaded();
      expect(
        el.shadowRoot!.querySelectorAll('a.sidebar-link.active')
      ).to.have.length(1);
    });

    it('gates a single approval on the same permission as the list', async () => {
      window.history.replaceState({}, '', '/console/approval/req-123');
      withPermissions(['view_agents']);
      const el = await loaded();
      const denied = el.shadowRoot!.querySelector('permission-denied');
      expect(denied).to.exist;
      expect(denied!.getAttribute('required-permission')).to.equal(
        'view_approvals'
      );
    });

    it('lets a decision-token link through to the approval page', async () => {
      // An escalation recipient may hold no view_approvals at all; the page
      // falls back to the token, which authorizes exactly this request.
      window.history.replaceState(
        {},
        '',
        '/console/approval/req-123?token=example-token'
      );
      withPermissions(['view_agents']);
      const el = await loaded();
      expect(el.shadowRoot!.querySelector('permission-denied')).to.not.exist;
      expect(el.shadowRoot!.querySelector('.main-content slot')).to.exist;
    });
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
        '#console-nav-toggle'
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
        '#console-nav-toggle'
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
  describe('keyboard and landmarks', () => {
    function useMobile() {
      const mockMediaQuery = createMatchMediaStub(true);
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
    }

    async function mount(): Promise<ConsoleShell> {
      const el = (await fixture(
        html`<console-shell></console-shell>`
      )) as ConsoleShell;
      await waitUntil(
        () => el.shadowRoot?.querySelector('a[href="/console/tools"]') !== null,
        'Sidebar menu did not render'
      );
      return el;
    }

    const toggle = (el: ConsoleShell) =>
      el.shadowRoot!.querySelector('#console-nav-toggle') as HTMLButtonElement;
    const sidebar = (el: ConsoleShell) =>
      el.shadowRoot!.querySelector('#console-nav') as HTMLElement;

    it('offers "Skip to content" first, landing focus on the content area', async () => {
      const el = await mount();
      const first = el.shadowRoot!.querySelector('a, button') as HTMLElement;
      expect(first.textContent?.trim()).to.equal('Skip to content');
      expect(first.getAttribute('href')).to.equal('#console-main');

      first.click();
      const main = el.shadowRoot!.querySelector('#console-main') as HTMLElement;
      expect(main.getAttribute('tabindex')).to.equal('-1');
      expect(el.shadowRoot!.activeElement).to.equal(main);
      // The click is handled, not followed: no stray fragment in the URL.
      expect(window.location.hash).to.equal('');
    });

    it('never nests a second <main> inside the app outlet', async () => {
      const el = await mount();
      expect(el.shadowRoot!.querySelector('main')).to.not.exist;
    });

    it('states the sidebar state on the toggle and keeps a hidden sidebar inert', async () => {
      const el = await mount();
      expect(toggle(el).getAttribute('aria-controls')).to.equal('console-nav');
      expect(toggle(el).getAttribute('aria-expanded')).to.equal('true');
      expect(toggle(el).getAttribute('aria-label')).to.equal('Hide navigation');
      expect(sidebar(el).hasAttribute('inert')).to.be.false;

      toggle(el).click();
      await el.updateComplete;

      expect(toggle(el).getAttribute('aria-expanded')).to.equal('false');
      expect(toggle(el).getAttribute('aria-label')).to.equal('Show navigation');
      // Collapsed to zero width, so its links must leave the tab order.
      expect(sidebar(el).hasAttribute('inert')).to.be.true;
    });

    it('keeps the closed mobile drawer inert', async () => {
      useMobile();
      const el = await mount();
      expect(sidebar(el).hasAttribute('inert')).to.be.true;
      expect(toggle(el).getAttribute('aria-expanded')).to.equal('false');
    });

    it('moves focus into the mobile drawer, and Escape closes it back to the toggle', async () => {
      useMobile();
      const el = await mount();
      toggle(el).click();
      await waitUntil(
        () =>
          (el.shadowRoot!.activeElement as HTMLElement | null)?.closest?.(
            '#console-nav'
          ),
        'focus did not move into the drawer'
      );

      el.shadowRoot!.activeElement!.dispatchEvent(
        new KeyboardEvent('keydown', {
          key: 'Escape',
          bubbles: true,
          composed: true,
        })
      );
      await waitUntil(
        () => sidebar(el).classList.contains('closed'),
        'Escape did not close the drawer'
      );
      await el.updateComplete;
      expect(el.shadowRoot!.activeElement).to.equal(toggle(el));
    });

    it('ignores Escape on desktop, where the sidebar is not a drawer', async () => {
      const el = await mount();
      el.dispatchEvent(
        new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })
      );
      await el.updateComplete;
      expect(sidebar(el).classList.contains('open')).to.be.true;
    });

    it('starts a newly opened page at the top of the content area', async () => {
      const originalPath = window.location.pathname;
      const el = await mount();
      await waitUntil(
        () => el.shadowRoot!.querySelector('.main-content slot'),
        'outlet did not render'
      );
      const main = el.shadowRoot!.querySelector('.main-content') as HTMLElement;
      // A tall routed child, so the content area really scrolls.
      const filler = document.createElement('div');
      filler.style.minHeight = '5000px';
      el.appendChild(filler);
      await el.updateComplete;
      main.scrollTop = 500;
      expect(main.scrollTop).to.be.greaterThan(0);

      window.history.pushState({}, '', '/console/agents');
      window.dispatchEvent(new CustomEvent(LOCATION_CHANGED));
      expect(main.scrollTop).to.equal(0);

      // Back keeps the place it had, like the router does for the window.
      main.scrollTop = 400;
      window.history.pushState({}, '', '/console/tools');
      window.dispatchEvent(new PopStateEvent('popstate'));
      window.dispatchEvent(new CustomEvent(LOCATION_CHANGED));
      expect(main.scrollTop).to.equal(400);

      window.history.replaceState({}, '', originalPath);
    });

    it('shows the loading line, not a blank page, while features load', async () => {
      const el = (await fixture(
        html`<console-shell></console-shell>`
      )) as ConsoleShell;
      (el as unknown as { _featuresLoaded: boolean })._featuresLoaded = false;
      el.requestUpdate();
      await el.updateComplete;
      expect(el.shadowRoot!.querySelector('.main-content route-loading')).to
        .exist;
      expect(el.shadowRoot!.querySelector('.main-content slot')).to.not.exist;
    });
  });

  describe('toasts', () => {
    afterEach(() => {
      document.body.querySelectorAll('sl-alert').forEach((a) => a.remove());
    });

    it('turns a show-toast request into a visible toast', async () => {
      const el = (await fixture(
        html`<console-shell></console-shell>`
      )) as ConsoleShell;
      await el.updateComplete;
      window.dispatchEvent(
        new CustomEvent('show-toast', {
          detail: { message: 'Slow down a little.', variant: 'warning' },
        })
      );
      const alert = Array.from(document.querySelectorAll('sl-alert')).find(
        (a) => a.textContent?.includes('Slow down a little.')
      ) as (HTMLElement & { variant: string }) | undefined;
      expect(alert, 'toast rendered').to.exist;
      expect(alert!.variant).to.equal('warning');
    });

    it('accepts a request that bubbles up from a routed view', async () => {
      const el = (await fixture(
        html`<console-shell><div id="child"></div></console-shell>`
      )) as ConsoleShell;
      await el.updateComplete;
      el.querySelector('#child')!.dispatchEvent(
        new CustomEvent('show-toast', {
          bubbles: true,
          composed: true,
          detail: { message: 'Logs copied.', variant: 'bogus' },
        })
      );
      const alert = Array.from(document.querySelectorAll('sl-alert')).find(
        (a) => a.textContent?.includes('Logs copied.')
      ) as (HTMLElement & { variant: string }) | undefined;
      expect(alert).to.exist;
      // An unknown variant falls back rather than rendering unstyled.
      expect(alert!.variant).to.equal('primary');
    });

    it('ignores a request with nothing to say', async () => {
      const el = (await fixture(
        html`<console-shell></console-shell>`
      )) as ConsoleShell;
      await el.updateComplete;
      const before = document.querySelectorAll('sl-alert').length;
      window.dispatchEvent(
        new CustomEvent('show-toast', { detail: { message: '  ' } })
      );
      expect(document.querySelectorAll('sl-alert').length).to.equal(before);
    });

    it('stops listening once the shell is gone', async () => {
      const el = (await fixture(
        html`<console-shell></console-shell>`
      )) as ConsoleShell;
      await el.updateComplete;
      el.remove();
      const before = document.querySelectorAll('sl-alert').length;
      window.dispatchEvent(
        new CustomEvent('show-toast', { detail: { message: 'Gone.' } })
      );
      expect(document.querySelectorAll('sl-alert').length).to.equal(before);
    });
  });

  describe('upgrade modal', () => {
    /** Collapse Lit's template line breaks so assertions test copy, not layout. */
    function copy(el: ConsoleShell): string {
      return (el.shadowRoot?.textContent ?? '').replace(/\s+/g, ' ').trim();
    }

    /** Serve /features with the billing plugin on or off. */
    function withBilling(billing: boolean) {
      invalidateApiCaches();
      fetchStub.callsFake(async (input: RequestInfo | URL) => {
        const url = typeof input === 'string' ? input : input.toString();
        if (url.endsWith('/api/v1/features')) {
          return new Response(
            JSON.stringify({
              plugins: billing ? ['billing'] : [],
              features: billing ? { billing: true } : {},
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
    }

    async function mountShell(): Promise<ConsoleShell> {
      const el = (await fixture(
        html`<console-shell></console-shell>`
      )) as ConsoleShell;
      await waitUntil(
        () => (el as any)._featuresLoaded,
        'features did not load'
      );
      await el.updateComplete;
      return el;
    }

    beforeEach(() => withBilling(true));

    async function openGate(feature: string) {
      const el = await mountShell();
      // show() would open a real dialog and animate; the copy under test is
      // rendered from _upgradeFeature either way.
      sinon.stub((el as any)._upgradeModal, 'show');
      window.dispatchEvent(
        new CustomEvent('show-upgrade-modal', {
          detail: { code: 'upgrade_required', feature },
        })
      );
      await el.updateComplete;
      return el;
    }

    it('names the gated feature without naming a specific plan', async () => {
      const el = await openGate('session_optimization');
      const text = copy(el);
      expect(text).to.contain('AI session optimization is a paid feature');
      // "Teams" is the grandfathered legacy plan and is no longer sold, so
      // promising the feature under that name sends buyers to a dead plan.
      expect(text).to.not.contain('Teams feature');
    });

    it('names the gates a person reaches by doing something', async () => {
      // Price overrides, built-in model analysis and asking for older data
      // are the three user actions that hit a 402/403 gate. The modal has
      // to say what was refused, not print a capability key.
      expect(copy(await openGate('price_overrides'))).to.contain(
        'custom model prices is a paid feature'
      );
      expect(copy(await openGate('ai_optimization'))).to.contain(
        'analysis with built-in models is a paid feature'
      );
      expect(copy(await openGate('analytics_window_days'))).to.contain(
        'analytics history beyond your plan window is a paid feature'
      );
    });

    it('uses no em dash in the upgrade copy (founder ruling)', async () => {
      const el = await openGate('replay_verification');
      expect(copy(el)).to.not.contain('\u2014');
    });

    // Router.go is a static on a module singleton, so it outlives the
    // fixture and has to be put back by hand.
    afterEach(() => {
      (Router.go as sinon.SinonStub).restore?.();
    });

    /** The two footer actions, as a reader sees them. */
    function footer(el: ConsoleShell, id: string): HTMLElement {
      return el.shadowRoot!.querySelector(
        `[data-testid="${id}"]`
      ) as HTMLElement;
    }

    it('takes "Upgrade now" to the plan page naming what was refused', async () => {
      const go = sinon.stub(Router, 'go').returns(true);
      const el = await openGate('session_titles');
      footer(el, 'upgrade-now').click();
      await el.updateComplete;

      // The name travels exactly as this dialog showed it. The plan page maps
      // it to the capability a plan sells (session_titles is part of
      // ai_optimization) before matching, and keeps the name for the sentence
      // it prints, so the card answers in the words the reader was refused in.
      expect(go.lastCall.args[0]).to.equal(
        '/console/settings/plan?feature=session_titles'
      );
    });

    it('takes "View plans" to the same page with nothing chosen', async () => {
      const go = sinon.stub(Router, 'go').returns(true);
      const el = await openGate('session_titles');
      footer(el, 'upgrade-view-plans').click();
      await el.updateComplete;

      // Reading the list is not the same act as buying the thing that was
      // refused, so this one preselects nothing.
      expect(go.lastCall.args[0]).to.equal('/console/settings/plan');
    });

    it('starts no checkout of its own: the plan page owns the decision', async () => {
      sinon.stub(Router, 'go').returns(true);
      const el = await openGate('session_optimization');
      footer(el, 'upgrade-now').click();
      await el.updateComplete;
      expect(
        fetchStub
          .getCalls()
          .filter((c) => String(c.args[0]).includes('create-checkout-session'))
      ).to.have.length(0);
    });

    it('closes the dialog on the way out, leaving nothing over the page', async () => {
      sinon.stub(Router, 'go').returns(true);
      const el = await openGate('rbac');
      const hide = sinon.stub((el as any)._upgradeModal, 'hide');
      footer(el, 'upgrade-now').click();
      await el.updateComplete;
      expect(hide).to.have.been.calledOnce;
    });

    it('offers the plan page without a feature when the gate names none', async () => {
      const go = sinon.stub(Router, 'go').returns(true);
      const el = await mountShell();
      sinon.stub((el as any)._upgradeModal, 'show');
      window.dispatchEvent(
        new CustomEvent('show-upgrade-modal', { detail: {} })
      );
      await el.updateComplete;
      footer(el, 'upgrade-now').click();
      await el.updateComplete;
      expect(go.lastCall.args[0]).to.equal('/console/settings/plan');
    });

    it('never opens where nothing is sold (OSS)', async () => {
      // Without the billing plugin "Upgrade now" leads to a page that says
      // every feature is already available, so the dialog stays shut no
      // matter what raised it.
      withBilling(false);
      const el = await mountShell();
      const show = sinon.stub((el as any)._upgradeModal, 'show');
      window.dispatchEvent(
        new CustomEvent('show-upgrade-modal', {
          detail: { code: 'upgrade_required', feature: 'rbac' },
        })
      );
      await el.updateComplete;
      expect(show).to.not.have.been.called;
    });

    it('opens where the billing plugin sells plans', async () => {
      const el = await mountShell();
      const show = sinon.stub((el as any)._upgradeModal, 'show');
      window.dispatchEvent(
        new CustomEvent('show-upgrade-modal', {
          detail: { code: 'upgrade_required', feature: 'rbac' },
        })
      );
      expect(show).to.have.been.calledOnce;
    });

    it('falls back to a full page load where no router is mounted', async () => {
      sinon.stub(Router, 'go').returns(false);
      const el = await openGate('price_overrides');
      const assign = sinon.stub(el as any, '_navigate');
      footer(el, 'upgrade-now').click();
      await el.updateComplete;
      expect(assign).to.have.been.calledWith(
        '/console/settings/plan?feature=price_overrides'
      );
    });
  });

  /**
   * The first-login plan choice.
   *
   * The shell's whole job here is the decision: who gets asked, who is never
   * asked, and who is never even a request to the server about it. The
   * screen's own behaviour lives in plan-choice-screen.test.ts.
   */
  describe('first-login plan choice', () => {
    /**
     * Drive the shell with a chosen billing feature state and profile, and
     * record every URL it asks for so "no extra request" can be asserted
     * rather than assumed.
     */
    function drive(options: {
      billing: boolean;
      planChoiceMade?: boolean;
      show?: boolean;
    }): string[] {
      const seen: string[] = [];
      fetchStub.callsFake(async (input: RequestInfo | URL) => {
        const url = typeof input === 'string' ? input : input.toString();
        seen.push(url);
        if (url.endsWith('/api/v1/features')) {
          return new Response(
            JSON.stringify({
              plugins: [],
              features: { billing: options.billing },
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
              ...(options.planChoiceMade === undefined
                ? {}
                : { plan_choice_made: options.planChoiceMade }),
            }),
            { status: 200, headers: { 'Content-Type': 'application/json' } }
          );
        }
        if (url.includes('/api/v1/billing/plan-choice')) {
          return new Response(
            JSON.stringify({
              show: options.show === true,
              reason: options.show === true ? 'eligible' : 'answered',
              trial_days: 14,
            }),
            { status: 200, headers: { 'Content-Type': 'application/json' } }
          );
        }
        if (url.includes('/landing-content.json')) {
          return new Response(JSON.stringify({ pricing: { plans: [] } }), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        }
        return new Response(JSON.stringify({}), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      });
      return seen;
    }

    async function mount(): Promise<ConsoleShell> {
      const el = (await fixture(
        html`<console-shell></console-shell>`
      )) as ConsoleShell;
      await el.updateComplete;
      return el;
    }

    it('replaces the whole console for somebody who has not chosen', async () => {
      drive({ billing: true, planChoiceMade: false, show: true });
      const el = await mount();
      await waitUntil(
        () => el.shadowRoot?.querySelector('plan-choice-screen') !== null,
        'The plan choice never appeared'
      );

      // Full screen means full screen: no sidebar, no header, no banners and
      // no routed view underneath. A step you can walk around is not a step.
      expect(el.shadowRoot!.querySelector('.console-container')).to.not.exist;
      expect(el.shadowRoot!.querySelector('.sidebar-wrapper')).to.not.exist;
      expect(el.shadowRoot!.querySelector('console-header')).to.not.exist;
      expect(el.shadowRoot!.querySelector('slot')).to.not.exist;
      // And nothing to dismiss it with.
      expect(el.shadowRoot!.querySelector('sl-dialog#upgrade-modal')).to.not
        .exist;
    });

    it('hands the configured trial length to the screen', async () => {
      drive({ billing: true, planChoiceMade: false, show: true });
      const el = await mount();
      await waitUntil(
        () => el.shadowRoot?.querySelector('plan-choice-screen') !== null,
        'The plan choice never appeared'
      );
      const screen = el.shadowRoot!.querySelector('plan-choice-screen') as any;
      expect(screen.trialDays).to.equal(14);
    });

    it('shows the console again once the choice is made, on the same route', async () => {
      drive({ billing: true, planChoiceMade: false, show: true });
      const el = await mount();
      await waitUntil(
        () => el.shadowRoot?.querySelector('plan-choice-screen') !== null,
        'The plan choice never appeared'
      );

      el.shadowRoot!.querySelector('plan-choice-screen')!.dispatchEvent(
        new CustomEvent('plan-choice-made', { bubbles: true, composed: true })
      );
      await el.updateComplete;

      expect(el.shadowRoot!.querySelector('plan-choice-screen')).to.not.exist;
      expect(el.shadowRoot!.querySelector('.console-container')).to.exist;
    });

    it('never asks again once the profile says the choice was made', async () => {
      // The second login. The answer is on the user, so it holds on another
      // device and in another browser, and it costs no request at all.
      const seen = drive({ billing: true, planChoiceMade: true });
      const el = await mount();
      await waitUntil(
        () => el.shadowRoot?.querySelector('.console-container') !== null,
        'The console never rendered'
      );
      expect(el.shadowRoot!.querySelector('plan-choice-screen')).to.not.exist;
      expect(
        seen.filter((u) => u.includes('billing/plan-choice'))
      ).to.have.length(0);
    });

    it('never asks an older server that does not send the field', async () => {
      const seen = drive({ billing: true });
      const el = await mount();
      await waitUntil(
        () => el.shadowRoot?.querySelector('.console-container') !== null,
        'The console never rendered'
      );
      expect(el.shadowRoot!.querySelector('plan-choice-screen')).to.not.exist;
      expect(
        seen.filter((u) => u.includes('billing/plan-choice'))
      ).to.have.length(0);
    });

    it('takes the plugin word for it when the plugin says no', async () => {
      // An invited colleague: the profile has no stamp, but the plugin can
      // see that this member cannot buy for the account.
      drive({ billing: true, planChoiceMade: false, show: false });
      const el = await mount();
      await waitUntil(
        () => el.shadowRoot?.querySelector('.console-container') !== null,
        'The console never rendered'
      );
      expect(el.shadowRoot!.querySelector('plan-choice-screen')).to.not.exist;
    });

    it('does not exist without the billing plugin (OSS default)', async () => {
      // The OSS contract: no screen, and NO REQUEST. Even a profile that
      // says the choice is open changes nothing, because a deployment that
      // sells nothing has no plan to choose.
      const seen = drive({ billing: false, planChoiceMade: false, show: true });
      const el = await mount();
      await waitUntil(
        () => el.shadowRoot?.querySelector('.console-container') !== null,
        'The console never rendered'
      );
      expect(el.shadowRoot!.querySelector('plan-choice-screen')).to.not.exist;
      // Scoped to this feature's own route on purpose: other components in
      // the shell have their own OSS behaviour and their own tests, and a
      // blanket "no /billing/ request" here would fail for their reasons
      // rather than this one's.
      expect(
        seen.filter((u) => u.includes('billing/plan-choice'))
      ).to.have.length(0);
      expect(
        seen.filter((u) => u.includes('/landing-content.json'))
      ).to.have.length(0);
    });
  });
});
