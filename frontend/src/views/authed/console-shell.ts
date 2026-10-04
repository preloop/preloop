import {
  LitElement,
  html,
  css,
  unsafeCSS,
  nothing,
  type TemplateResult,
} from 'lit';
import { customElement, query, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/menu/menu.js';
import '@shoelace-style/shoelace/dist/components/menu-item/menu-item.js';
import '@shoelace-style/shoelace/dist/components/menu-label/menu-label.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/icon-button/icon-button.js';
import '@shoelace-style/shoelace/dist/components/details/details.js';
import '@shoelace-style/shoelace/dist/components/dialog/dialog.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '../../components/logo-component';
import '../../components/global-notice';
import '../../components/console-header';
import '../../components/approval-bypass-banner';
import '../../components/kill-switch-banner';
import '../../components/usage-nudge-banner';
import '../../components/capability-extension';
import {
  ciAdministration,
  completeCiCapabilities,
} from '../../ci-administration-api';
import { hasCapability } from '../../capabilities';
import consoleStyles from '../../styles/console-styles.css?inline';
import {
  getFeatures,
  getPlanChoice,
  getUserProfile,
  hasAnyPermission,
  invalidateUserProfileCache,
  type FeaturesResponse,
  type UserPermissions,
  type UserProfile,
} from '../../api';
import '../../components/permission-denied';
import '../../components/route-loading';
import { showToast } from '../../components/confirm-dialog';
import '../../components/plan-choice-screen';
import { consoleDialogStyles } from '../../styles/console-dialog';
import { LOCATION_CHANGED, Router } from '../../router';
import { planPageUrl, premiumFeatureLabel } from '../../utils/premium-features';

/** Nav items that require at least one of the listed permissions when RBAC is on. */
const NAV_PERMISSIONS: Record<string, string[]> = {
  '/console/agents': ['view_agents'],
  '/console/flows': ['view_flows'],
  '/console/settings/runners': ['view_flows'],
  '/console/settings/webhooks': ['view_policies'],
  '/console/tools': ['view_tools', 'view_policies'],
  '/console/policies': ['view_policies'],
  '/console/trackers': ['view_trackers'],
  '/console/ai-models': ['view_ai_models'],
  '/console/runtime-sessions': ['view_runtime_sessions'],
  '/console/artifacts': ['view_runtime_sessions'],
  '/console/cost': ['view_cost'],
  '/console/approvals': ['view_approvals'],
  // One request, opened from a notification, Slack or email. A link that
  // carries a decision token is exempt (see _deniedPermissionForPath).
  '/console/approval': ['view_approvals'],
  '/console/audit': ['view_audit_logs'],
  '/console/settings/users': ['view_users'],
  '/console/settings/teams': ['view_teams'],
  '/console/settings/invitations': ['invite_users', 'view_users'],
  '/console/settings/account': ['manage_account', 'view_billing'],
  '/console/settings/plan': ['manage_account', 'view_billing'],
  // Audit integrity and exports use view_audit_logs. Retention and holds
  // use view_policies. Either permission is enough to open the page.
  '/console/settings/records': ['view_audit_logs', 'view_policies'],
  // Halting an account is the kill switch permission, not the billing one.
  // The controls used to sit on the account page, where a reader who could
  // not use them still saw them.
  '/console/settings/emergency': ['manage_kill_switch'],
};

/**
 * Pages with no nav entry of their own, and the nav item that owns them.
 * Without this, a reader who opened a single approval from a notification
 * saw no item highlighted and the Audit group closed: no wayfinding at all.
 */
const NAV_ALIASES: Record<string, string[]> = {
  '/console/approvals': ['/console/approval'],
  '/console/cost': ['/console/api-usage'],
};

const SIDEBAR_BREAKPOINT = 768;

const TOAST_VARIANTS = [
  'primary',
  'success',
  'neutral',
  'warning',
  'danger',
] as const;

/**
 * A view asks for popup chrome with `?window=1` in the URL.
 *
 * The talk window is a real console route inside this shell rather than a
 * separate page: it keeps the session cookie, the theme, the permission gate
 * and the dialog offset without a second bootstrap. All the popup needs is for
 * the shell to stop drawing navigation it cannot use.
 */
export function isWindowChromeRequested(search: string): boolean {
  return new URLSearchParams(search).get('window') === '1';
}

/**
 * The sidebar's rendered width on desktop, published as
 * `--console-main-offset` so that dialogs centre over the content area
 * instead of over the window. Kept in step with the `.sidebar` rule below.
 */
const SIDEBAR_WIDTH_PX = 250;

// static styles = [formStyles, css`
//     h2 {}

//     `];

@customElement('console-shell')
export class ConsoleShell extends LitElement {
  @query('#upgrade-modal')
  private _upgradeModal!: HTMLElement;

  @state()
  private _upgradeFeature = '';

  @state()
  private features: FeaturesResponse['features'] = {};

  @state()
  private _featuresLoaded = false;

  @state()
  private _ciSetupAvailable = false;

  @state()
  private _permissions: UserPermissions = undefined;

  @state()
  private _permissionsLoaded = false;

  @state()
  private _isSuperuser = false;

  /**
   * The first-login plan choice, as a small state machine.
   *
   * `settled` is the answer for everybody except a brand new account, and it
   * is reached without a single extra request: the profile the shell already
   * fetches carries `plan_choice_made`, and the migration that shipped this
   * stamped every account that existed beforehand. `checking` and `required`
   * both take the console off the screen, because a console behind a
   * question the person has not answered is exactly the half-open state this
   * replaces.
   */
  @state()
  private _planChoice: 'settled' | 'checking' | 'required' = 'settled';

  /** Configured trial length, from the plugin, for the screen's own copy. */
  @state()
  private _planChoiceTrialDays = 0;

  @state()
  private _sidebarOpen = false;

  @state()
  private _isMobile = false;

  @state()
  private _fullBleed = false;

  @state()
  private _currentPath = window.location.pathname;

  @state()
  private _windowMode = isWindowChromeRequested(window.location.search);

  /** The path the content area was last scrolled for (see below). */
  private _scrolledPath = window.location.pathname;

  /** Set by a back/forward popstate until the router announces it. */
  private _historyTraversal = false;

  private _mediaQuery?: MediaQueryList;
  private _mediaQueryHandler?: (e: MediaQueryListEvent) => void;

  static styles = [
    consoleDialogStyles,
    unsafeCSS(consoleStyles),
    css`
      :host {
        display: block;
        height: 100vh;
      }

      a:hover {
        text-decoration: none;
      }

      .console-container {
        display: flex;
        flex-direction: row;
        height: 100%;
      }

      /* The sidebar is a card-level surface next to the page, separated by
         a hairline rather than by a second gray step. */
      .sidebar {
        width: 250px;
        flex-shrink: 0;
        display: flex;
        flex-direction: column;
        transition:
          width 0.2s ease,
          transform 0.25s ease;
        background-color: var(--console-surface);
        border-right: 1px solid var(--console-hairline);
        z-index: 100;
      }

      /* Desktop: when closed, sidebar is fully hidden (hamburger only) */
      .sidebar.closed {
        width: 0;
        min-width: 0;
        overflow: hidden;
        padding: 0;
        border-right-width: 0;
      }

      .sidebar-wrapper {
        position: relative;
        display: flex;
        flex-shrink: 0;
      }

      .sidebar-backdrop {
        display: none;
        position: fixed;
        inset: 0;
        background: rgba(0, 0, 0, 0.4);
        z-index: 99;
        opacity: 0;
        transition: opacity 0.25s ease;
      }

      @media (max-width: 768px) {
        .sidebar {
          position: fixed;
          left: 0;
          top: 0;
          bottom: 0;
          width: 260px;
          max-width: 85vw;
          transform: translateX(-100%);
          box-shadow: var(--sl-shadow-large);
        }

        .sidebar.open {
          transform: translateX(0);
        }

        .sidebar.closed {
          width: 260px;
          min-width: 260px;
        }

        .sidebar-backdrop.visible {
          display: block;
          opacity: 1;
        }
      }

      .sign-out-menu {
        flex-grow: 0;
      }

      .sign-out-menu sl-menu-item::part(base) {
        background-color: transparent;
        color: var(--console-link-color);
      }

      .sign-out-menu sl-menu-item:hover::part(base) {
        background-color: var(--console-hover-tint);
        color: var(--sl-color-primary-700);
      }

      .main-view {
        flex-grow: 1;
        display: grid;
        grid-template-rows: auto auto auto auto 1fr; /* Header, banners, content */
        overflow-y: hidden;
        background-color: var(--console-page);
      }

      /* A popup has no header and no banner, so the content is the only row. */
      .main-view.window-mode {
        grid-template-rows: 1fr;
      }

      /* The page is the bottom rung of the ladder and every card sits one
         step above it, in both themes (styles/console-surfaces.css). Slotted
         views inherit the console's compact type scale and tabular figures
         from here, so a new page matches its neighbours without opting in. */
      .main-content {
        overflow-y: auto;
        padding: var(--console-page-padding-top) var(--console-page-padding-x)
          var(--console-page-padding-bottom);
        display: flex;
        flex-direction: column;
        align-items: center;
        background-color: var(--console-page);
        color: var(--console-body-color);
        font-size: var(--console-text-body);
        font-variant-numeric: tabular-nums;
      }

      .main-content.full-bleed {
        padding: 0;
        overflow: hidden;
      }

      /* The skip target takes focus programmatically only; a ring around the
         whole page would be noise. */
      .main-content:focus {
        outline: none;
      }

      /* Visually hidden until focused, then pinned over the top-left corner
         as the first thing a keyboard user reaches. */
      .skip-link {
        position: absolute;
        left: var(--sl-spacing-small);
        top: var(--sl-spacing-small);
        z-index: 1000;
        padding: var(--sl-spacing-x-small) var(--sl-spacing-small);
        border-radius: var(--sl-border-radius-medium);
        background: var(--console-surface-raised, var(--sl-color-neutral-0));
        color: var(--console-link-color, var(--sl-color-primary-600));
        box-shadow: var(--sl-shadow-medium);
        font-weight: 600;
        transform: translateY(calc(-100% - var(--sl-spacing-large)));
      }

      .skip-link:focus {
        transform: none;
        outline: var(--sl-focus-ring);
        outline-offset: var(--sl-focus-ring-offset);
      }

      @media (prefers-reduced-motion: no-preference) {
        .skip-link {
          transition: transform 0.15s ease;
        }
      }

      /* A native button, not sl-icon-button, so aria-expanded and
         aria-controls sit on the element that actually takes focus. Matches
         sl-icon-button's look. */
      .nav-toggle-button {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        padding: var(--sl-spacing-x-small);
        border: 0;
        border-radius: var(--sl-border-radius-medium);
        background: none;
        color: var(--sl-color-neutral-600);
        font-size: 1.5rem;
        line-height: 1;
        cursor: pointer;
        transition: color var(--sl-transition-x-fast) ease;
      }

      .nav-toggle-button:hover {
        color: var(--sl-color-primary-600);
      }

      .nav-toggle-button:focus-visible {
        outline: var(--sl-focus-ring);
        outline-offset: var(--sl-focus-ring-offset);
      }

      .main-content > ::slotted(*) {
        width: 100%;
        max-width: var(--console-page-max-width);
      }

      .main-content.full-bleed > ::slotted(*) {
        max-width: none;
        height: 100%;
      }

      @media (max-width: 768px) {
        .main-content {
          padding: var(--console-page-padding-x-compact);
        }
        .main-content.full-bleed {
          padding: 0;
        }
      }

      .logo {
        margin-left: 2px;
        padding: 1rem;
        background-color: transparent;
        display: flex;
        align-items: center;
      }

      .logo img {
        max-width: 150px;
      }

      .sidebar-label {
        margin-left: 0.5rem;
      }

      sl-menu {
        flex-grow: 1;
        border-width: 0;
        background-color: transparent;
        padding: 0;
        margin-left: -2px;
      }

      sl-details::part(base) {
        width: 100%;
        border-width: 0;
        background-color: transparent;
      }

      sl-details::part(content) {
        padding-top: 0;
        padding-left: 1.5rem;
      }

      .sidebar-link {
        display: block;
        color: inherit;
        text-decoration: none;
        border-radius: var(--sl-border-radius-medium);
        /* Reserved so the active rule appears without shifting the label. */
        border-left: 3px solid transparent;
      }

      .sidebar-link:hover {
        background-color: var(--console-hover-tint);
      }

      /* Style the anchor, not ::part — Shoelace shadow styles override ::part rules */
      /* A translucent mix of one primary token is the same tint in both
         themes; a named step (primary-50) is the palest blue in light and the
         darkest navy in dark, which is how the active item became a block. */
      .sidebar-link.active {
        background-color: color-mix(
          in srgb,
          var(--sl-color-primary-500) 14%,
          transparent
        );
        border-left-color: var(--sl-color-primary-600);
      }

      /* The active item is stated once, in colour and weight; bold on top of
         a tinted rule was three signals for one fact. */
      .sidebar-link.active .sidebar-label,
      .sidebar-link.active sl-menu-item::part(label),
      .sidebar-link.active sl-icon {
        color: var(--sl-color-primary-700);
        font-weight: 600;
      }

      /* No dark-mode override here on purpose: Shoelace's dark theme inverts
         the palette scale, so primary-50 is already the dark tint and
         primary-700 already the light ink. Hard-coding primary-950/300 for
         dark inverted it twice and painted the active item near-white. */

      .sidebar-link sl-icon,
      sl-details.nav-section sl-icon {
        font-size: 18px;
      }

      .sidebar-label {
        font-size: var(--console-text-body);
      }

      sl-menu-item::part(base) {
        padding: 0;
        background-color: transparent;
        border-radius: inherit;
      }

      sl-menu-item {
        padding: 0.5em;
      }

      sl-details {
        padding-left: 1em;
      }

      sl-details.nav-section[open]::part(summary) {
        font-weight: var(--sl-font-weight-bold);
      }

      /* Child links read at the same size as top-level ones; Shoelace's
         default menu-item size made Audit and Settings children larger
         than the items they sit under. */
      sl-details sl-menu-item::part(label) {
        font-size: var(--console-text-body);
      }

      sl-menu-label.nav-group-label::part(base) {
        padding: var(--sl-spacing-small) 0.5em var(--sl-spacing-3x-small);
        font-size: var(--sl-font-size-x-small);
        letter-spacing: 0.04em;
        text-transform: uppercase;
        color: var(--console-meta-color);
      }

      .nav-emergency sl-icon,
      .nav-emergency .sidebar-label {
        color: var(--sl-color-danger-600);
      }
    `,
  ];

  private _handleShowUpgradeModal = (event: Event) => {
    // Plans exist only where something is sold. Without the billing plugin
    // the dialog would offer "Upgrade now" to a page that says there is
    // nothing to buy, so the shell drops the request whatever raised it.
    if (this.features['billing'] !== true) return;
    const detail = (event as CustomEvent).detail;
    this._upgradeFeature =
      detail?.code === 'upgrade_required' ? String(detail.feature || '') : '';
    (this._upgradeModal as any).show();
  };

  /**
   * Render a `show-toast` request as the console's toast.
   *
   * Views and the API layer ask for a toast with this event (`detail.message`
   * and an optional `detail.variant`) instead of each importing the toast
   * helper. The shell is the one place that turns it into a visible alert.
   */
  private _handleShowToast = (event: Event) => {
    const detail = (event as CustomEvent).detail as
      { message?: unknown; variant?: unknown } | undefined;
    const message =
      typeof detail?.message === 'string' ? detail.message.trim() : '';
    if (!message) return;
    const variant = TOAST_VARIANTS.find((v) => v === detail?.variant);
    showToast(message, variant ?? 'primary');
  };

  /**
   * Leave the dialog for the plan page.
   *
   * The dialog used to start a checkout for one hardcoded plan. That bought
   * the wrong thing twice over: too much plan for a feature the entry plan
   * already includes, and a refusal for a feature that needs a bigger one.
   * It also left the dialog open on top of whatever happened next. Both
   * actions now close it and hand the decision to the plan page, which knows
   * the catalog, the account's current plan and the price of each.
   */
  private _goToPlans(feature: string) {
    const url = planPageUrl(feature);
    (this._upgradeModal as any)?.hide?.();
    if (!Router.go(url)) this._navigate(url);
  }

  /** "View plans": the whole list, with nothing chosen for the reader. */
  private _viewPlans = () => this._goToPlans('');

  /**
   * "Upgrade now": the same page, with the refused feature named in the
   * query so it opens on the cheapest plan that unlocks it.
   */
  private _upgradeNow = () => this._goToPlans(this._upgradeFeature);

  /** A full page load, for the case where no router claimed the path. */
  private _navigate(url: string): void {
    window.location.assign(url);
  }

  async connectedCallback() {
    super.connectedCallback();
    window.addEventListener('show-upgrade-modal', this._handleShowUpgradeModal);
    window.addEventListener('show-toast', this._handleShowToast);
    this.addEventListener('keydown', this._handleKeydown);
    window.addEventListener(LOCATION_CHANGED, this._handleLocationChanged);
    this._mediaQuery = window.matchMedia(
      `(max-width: ${SIDEBAR_BREAKPOINT}px)`
    );
    this._isMobile = this._mediaQuery.matches;
    this._sidebarOpen = !this._mediaQuery.matches; // desktop: visible, mobile: hidden
    this._publishMainOffset();
    this._mediaQueryHandler = (e: MediaQueryListEvent) => {
      this._isMobile = e.matches;
      if (!e.matches) {
        this._sidebarOpen = true;
      } else {
        this._sidebarOpen = false;
      }
    };
    this._mediaQuery.addEventListener('change', this._mediaQueryHandler);
    window.addEventListener('popstate', this._handleLocationChanged);
    this._currentPath = window.location.pathname;

    void ciAdministration
      .capabilities()
      .then((capabilities) => {
        if (this.isConnected)
          this._ciSetupAvailable =
            completeCiCapabilities(capabilities) &&
            capabilities.can_view === true;
      })
      .catch(() => {
        this._ciSetupAvailable = false;
      });

    // Fetch enabled features and current-user permissions in parallel
    try {
      const [featuresResponse, profile] = await Promise.all([
        getFeatures(),
        getUserProfile().catch((error) => {
          console.error('Failed to fetch user profile for permissions:', error);
          return null;
        }),
      ]);
      this.features = featuresResponse.features;
      this._permissions = profile?.permissions ?? null;
      this._isSuperuser = profile?.is_superuser === true;
      this._startPlanChoiceCheck(profile);
    } catch (error) {
      console.error('Failed to fetch features:', error);
      // Default to empty features if fetch fails
      this.features = {};
      this._permissions = null;
      this._isSuperuser = false;
    } finally {
      this._featuresLoaded = true;
      this._permissionsLoaded = true;
    }
  }

  /**
   * Decide whether this person still owes the product a plan choice.
   *
   * Two gates, in this order, and both have to be open before anything is
   * asked of the server:
   *
   * 1. The `billing` feature. Without the billing plugin this deployment
   *    sells nothing, so there is no plan to choose, no screen, and NO
   *    REQUEST. That is the OSS contract and it is asserted in the tests.
   * 2. The profile's `plan_choice_made`. False only for an account created
   *    after this shipped by somebody who did not pick a plan on the way in.
   *    Everybody else is settled here, for free, on a response the shell had
   *    already fetched.
   *
   * Only then does the billing plugin get the last word, because only it can
   * see a subscription row or whether this member may buy for the account.
   * Its refusals that write nothing down (a member who cannot buy, an account
   * already subscribed) are remembered by `getPlanChoice` for the tab, so
   * those cohorts ask once per page load and not once per route change. A
   * plugin that is simply unreachable is not remembered, so the question
   * heals itself when it comes back.
   */
  private _startPlanChoiceCheck(profile: UserProfile | null): void {
    if (this.features['billing'] !== true) return;
    if (profile?.plan_choice_made !== false) return;
    this._planChoice = 'checking';
    void (async () => {
      const decision = await getPlanChoice();
      this._planChoiceTrialDays = decision.trial_days;
      this._planChoice = decision.show ? 'required' : 'settled';
    })();
  }

  /**
   * The choice was made. Drop the screen and let the route underneath render.
   *
   * The router never moved, so whatever the person was going to see (the
   * overview, or a deep link such as the CLI consent page) is what appears.
   * The cached profile is dropped so the next read of it agrees with the
   * server rather than re-triggering this from a stale `false`.
   */
  private _handlePlanChoiceMade = () => {
    this._planChoice = 'settled';
    invalidateUserProfileCache();
  };

  private _canAccess(href: string): boolean {
    const required = NAV_PERMISSIONS[href];
    if (!required) {
      return true;
    }
    return hasAnyPermission(this._permissions, required);
  }

  /**
   * The Policies page is enabled by default. Operators may hide it through
   * the `policies_console` feature flag; instance admins bypass that flag.
   * Normal `view_policies` permission still applies on top of that.
   */
  private _canShowPolicies(): boolean {
    if (!this._featuresLoaded || !this._permissionsLoaded) {
      return false;
    }
    return (
      (this.features['policies_console'] === true || this._isSuperuser) &&
      this._canAccess('/console/policies')
    );
  }

  private _deniedPermissionForPath(path: string): string | null {
    const normalized = this._normalizePath(path);
    // Flag-hidden Policies page: same permission-denied surface as an RBAC
    // block, so a direct URL never renders an empty shell.
    if (
      (normalized === '/console/policies' ||
        normalized.startsWith('/console/policies/')) &&
      !this._canShowPolicies()
    ) {
      return 'view_policies';
    }
    // A decision-token link authorizes exactly one request, for somebody
    // who may not hold view_approvals at all (an escalation recipient). The
    // approval page falls back to the token itself, so the shell must not
    // refuse it first.
    const tokenLink =
      normalized.startsWith('/console/approval/') &&
      new URLSearchParams(window.location.search).has('token');
    for (const [href, required] of Object.entries(NAV_PERMISSIONS)) {
      if (tokenLink && href === '/console/approval') continue;
      if (
        normalized === href ||
        normalized.startsWith(`${href}/`) ||
        (href !== '/console' && normalized === href)
      ) {
        if (!hasAnyPermission(this._permissions, required)) {
          return required[0];
        }
      }
    }
    return null;
  }

  private _handleSidebarToggle = async () => {
    this._sidebarOpen = !this._sidebarOpen;
    // The mobile drawer overlays the page, so opening it moves focus in;
    // otherwise the next Tab lands behind the backdrop.
    if (this._sidebarOpen && this._isMobile) {
      await this.updateComplete;
      this.renderRoot.querySelector<HTMLElement>('.sidebar a[href]')?.focus();
    }
  };

  private _closeSidebar = () => {
    if (this._isMobile) {
      this._sidebarOpen = false;
    }
  };

  /** Close the mobile drawer and hand focus back to the toggle. */
  private _dismissDrawer = async () => {
    if (!this._isMobile || !this._sidebarOpen) return;
    this._sidebarOpen = false;
    await this.updateComplete;
    this.renderRoot.querySelector<HTMLElement>('#console-nav-toggle')?.focus();
  };

  private _handleKeydown = (event: KeyboardEvent) => {
    if (event.key === 'Escape' && this._isMobile && this._sidebarOpen) {
      event.preventDefault();
      void this._dismissDrawer();
    }
  };

  /**
   * "Skip to content": move focus to the content area. The target lives in
   * this shadow root, which a fragment link cannot reach, so the click is
   * handled here instead of by the browser.
   */
  private _skipToContent = (event: Event) => {
    event.preventDefault();
    const main = this.renderRoot.querySelector<HTMLElement>('#console-main');
    main?.focus();
  };

  private _handleLocationChanged = (event?: Event) => {
    this._fullBleed = false;
    this._currentPath = window.location.pathname;
    this._windowMode = isWindowChromeRequested(window.location.search);
    this._publishMainOffset();
    if (event?.type === 'popstate') {
      this._historyTraversal = true;
      return;
    }
    // The shell is reused across child routes and `.main-content` is the
    // scroll port, so the router's window.scrollTo does nothing here: a new
    // page opened at the scroll offset of the last one. Back and forward
    // keep their place, like the router does for the window.
    const path = window.location.pathname;
    if (path !== this._scrolledPath) {
      if (!this._historyTraversal) {
        const main = this.renderRoot?.querySelector('.main-content');
        if (main) main.scrollTop = 0;
      }
      this._scrolledPath = path;
    }
    this._historyTraversal = false;
  };

  private _handleNavClick = (e: Event) => {
    const anchor = e.currentTarget as HTMLAnchorElement;
    if (anchor.href) {
      this._currentPath = new URL(anchor.href).pathname;
    }
    this._closeSidebar();
  };

  private _normalizePath(path: string): string {
    if (path.length > 1 && path.endsWith('/')) {
      return path.slice(0, -1);
    }
    return path;
  }

  private _isNavActive(href: string, exact = false): boolean {
    const current = this._normalizePath(this._currentPath);
    const target = this._normalizePath(href);
    if (exact) {
      return current === target;
    }
    return [target, ...(NAV_ALIASES[target] ?? [])].some(
      (prefix) => current === prefix || current.startsWith(`${prefix}/`)
    );
  }

  private _isSettingsActive(): boolean {
    return (
      this._isNavActive('/console/settings') &&
      !this._isNavActive('/console/settings/emergency')
    );
  }

  /**
   * One labelled block of the Settings menu. The label renders only when at
   * least one of its links survives the edition and permission gates, so a
   * deployment never shows a heading over nothing.
   */
  private _renderNavGroup(
    label: string,
    links: ReadonlyArray<unknown>
  ): TemplateResult | typeof nothing {
    const visible = links.filter((link) => link !== nothing);
    if (visible.length === 0) return nothing;
    return html`<sl-menu-label class="nav-group-label">${label}</sl-menu-label
      >${visible}`;
  }

  /** True when any Audit child is visible for this user/edition. */
  private _hasAuditSection(): boolean {
    return (
      this._canShowAuditEvents() ||
      this._canAccess('/console/runtime-sessions') ||
      this._canAccess('/console/approvals')
    );
  }

  private _canShowAuditEvents(): boolean {
    return (
      this._featuresLoaded &&
      !!this.features['audit_logs'] &&
      this._canAccess('/console/audit')
    );
  }

  private _isAuditActive(): boolean {
    return (
      this._isNavActive('/console/audit') ||
      this._isNavActive('/console/runtime-sessions') ||
      this._isNavActive('/console/artifacts') ||
      this._isNavActive('/console/approvals')
    );
  }

  private _renderNavLink(
    href: string,
    content: TemplateResult,
    exact = false
  ): TemplateResult | typeof nothing {
    if (!this._canAccess(href)) {
      return nothing;
    }
    const active = this._isNavActive(href, exact);
    return html`
      <a
        href=${href}
        class="sidebar-link ${active ? 'active' : ''}"
        aria-current=${active ? 'page' : nothing}
        @click=${this._handleNavClick}
      >
        ${content}
      </a>
    `;
  }

  updated(changedProperties: Map<string, unknown>) {
    super.updated?.(changedProperties);
    if (
      changedProperties.has('_sidebarOpen') ||
      changedProperties.has('_isMobile')
    ) {
      document.body.style.overflow =
        this._sidebarOpen && this._isMobile ? 'hidden' : '';
      this._publishMainOffset();
    }
  }

  /**
   * How far the content area starts from the left of the window: the
   * sidebar's width when it takes space (desktop, open), zero when it is
   * hidden or overlaid on top of the page (mobile). Custom properties
   * inherit through shadow roots, so every dialog in every view can centre
   * itself on the content area by reading this one value.
   */
  private _publishMainOffset() {
    const offset =
      this._sidebarOpen && !this._isMobile && !this._windowMode
        ? `${SIDEBAR_WIDTH_PX}px`
        : '0px';
    this.style.setProperty('--console-main-offset', offset);
  }

  disconnectedCallback() {
    document.body.style.overflow = '';
    window.removeEventListener(
      'show-upgrade-modal',
      this._handleShowUpgradeModal
    );
    window.removeEventListener('show-toast', this._handleShowToast);
    this.removeEventListener('keydown', this._handleKeydown);
    window.removeEventListener(LOCATION_CHANGED, this._handleLocationChanged);
    window.removeEventListener('popstate', this._handleLocationChanged);
    this._mediaQuery?.removeEventListener('change', this._mediaQueryHandler!);
    super.disconnectedCallback();
  }

  render() {
    // The plan choice replaces the console outright: no sidebar, no header,
    // no banners, no routed view underneath it. The route is untouched, so
    // answering reveals whatever the person was on their way to, including a
    // deep link such as the CLI consent page. `checking` renders it too (the
    // screen shows its own spinner), because painting the console and then
    // pulling it away one request later is worse than waiting.
    if (this._planChoice !== 'settled') {
      return html`
        <plan-choice-screen
          .trialDays=${this._planChoiceTrialDays}
          @plan-choice-made=${this._handlePlanChoiceMade}
        ></plan-choice-screen>
      `;
    }

    return html`
      <sl-dialog id="upgrade-modal" label="Upgrade Your Plan">
        ${
          this._upgradeFeature
            ? html`${premiumFeatureLabel(this._upgradeFeature)} is a paid
              feature. The plan page shows the cheapest plan that includes it,
              next to the plan you are on now.`
            : html`This feature is not included in your current plan. The plan
              page shows what each plan includes, next to the plan you are on
              now.`
        }
        <sl-button
          slot="footer"
          data-testid="upgrade-view-plans"
          @click=${this._viewPlans}
        >
          View plans
        </sl-button>
        <sl-button
          slot="footer"
          variant="primary"
          data-testid="upgrade-now"
          @click=${this._upgradeNow}
        >
          Upgrade now
        </sl-button>
      </sl-dialog>

      <global-notice></global-notice>

      ${
        this._windowMode
          ? nothing
          : html`<a
              class="skip-link"
              href="#console-main"
              router-ignore
              @click=${this._skipToContent}
              >Skip to content</a
            >`
      }

      <div class="console-container">
        ${
          this._windowMode
            ? nothing
            : html`<div class="sidebar-wrapper">
                <div
                  class="sidebar-backdrop ${this._sidebarOpen ? 'visible' : ''}"
                  @click=${this._dismissDrawer}
                  aria-hidden="true"
                ></div>
                <!-- Hidden means out of the tab order too: a collapsed
                     sidebar (desktop) or a closed drawer (mobile) is inert,
                     so focus never lands on a link nobody can see. -->
                <div
                  id="console-nav"
                  class="sidebar ${this._sidebarOpen ? 'open' : 'closed'}"
                  role="navigation"
                  aria-label="Console navigation"
                  ?inert=${!this._sidebarOpen}
                >
                  <div class="logo">
                    <a href="/console" @click=${this._closeSidebar}
                      ><logo-component></logo-component
                    ></a>
                  </div>
                  <sl-menu style="font-size: var(--console-text-body);">
                    ${this._renderNavLink(
                      '/console',
                      html`
                        <sl-menu-item>
                          <sl-icon name="house" slot="prefix"></sl-icon>
                          <span class="sidebar-label">Overview</span>
                        </sl-menu-item>
                      `,
                      true
                    )}
                    ${this._renderNavLink(
                      '/console/agents',
                      html`
                        <sl-menu-item>
                          <sl-icon name="robot" slot="prefix"></sl-icon>
                          <span class="sidebar-label">Agents</span>
                        </sl-menu-item>
                      `
                    )}
                    ${this._renderNavLink(
                      '/console/flows',
                      html`
                        <sl-menu-item>
                          <sl-icon
                            src="/images/flow.svg"
                            slot="prefix"
                          ></sl-icon>
                          <span class="sidebar-label">Flows</span>
                        </sl-menu-item>
                      `
                    )}
                    ${this._renderNavLink(
                      '/console/ai-models',
                      html`
                        <sl-menu-item>
                          <sl-icon name="cpu" slot="prefix"></sl-icon>
                          <span class="sidebar-label">Models</span>
                        </sl-menu-item>
                      `
                    )}
                    ${this._renderNavLink(
                      '/console/tools',
                      html`
                        <sl-menu-item>
                          <sl-icon name="tools" slot="prefix"></sl-icon>
                          <span class="sidebar-label">Tools</span>
                        </sl-menu-item>
                      `
                    )}
                    ${
                      this._canShowPolicies()
                        ? this._renderNavLink(
                            '/console/policies',
                            html`
                              <sl-menu-item>
                                <sl-icon
                                  name="shield-lock"
                                  slot="prefix"
                                ></sl-icon>
                                <span class="sidebar-label">Policies</span>
                              </sl-menu-item>
                            `
                          )
                        : nothing
                    }
                    ${this._renderNavLink(
                      '/console/trackers',
                      html`
                        <sl-menu-item>
                          <sl-icon
                            src="/images/git.svg"
                            slot="prefix"
                          ></sl-icon>
                          <span class="sidebar-label">Trackers</span>
                        </sl-menu-item>
                      `
                    )}
                    ${this._renderNavLink(
                      '/console/cost',
                      html`
                        <sl-menu-item>
                          <sl-icon name="cash-coin" slot="prefix"></sl-icon>
                          <span class="sidebar-label">Cost</span>
                        </sl-menu-item>
                      `
                    )}
                    ${
                      this._hasAuditSection()
                        ? html`
                            <sl-details
                              class="nav-section"
                              ?open=${this._isAuditActive()}
                            >
                              <span slot="summary">
                                <sl-icon
                                  name="journal-text"
                                  style="padding-right: 6px;"
                                ></sl-icon>
                                <span class="sidebar-label">Audit</span>
                              </span>
                              <sl-menu>
                                ${
                                  this._canShowAuditEvents()
                                    ? this._renderNavLink(
                                        '/console/audit',
                                        html`<sl-menu-item
                                          >All events</sl-menu-item
                                        >`
                                      )
                                    : nothing
                                }
                                ${this._renderNavLink(
                                  '/console/runtime-sessions',
                                  html`<sl-menu-item>Sessions</sl-menu-item>`
                                )}
                                ${this._renderNavLink(
                                  '/console/artifacts',
                                  html`<sl-menu-item>Artifacts</sl-menu-item>`
                                )}
                                ${this._renderNavLink(
                                  '/console/approvals',
                                  html`<sl-menu-item>Approvals</sl-menu-item>`
                                )}
                              </sl-menu>
                            </sl-details>
                          `
                        : nothing
                    }
                    <sl-details
                      class="nav-section"
                      ?open=${this._isSettingsActive()}
                    >
                      <span slot="summary">
                        <sl-icon
                          name="gear"
                          style="padding-right: 6px;"
                        ></sl-icon>
                        <span class="sidebar-label">Settings</span>
                      </span>
                      <sl-menu>
                        ${this._renderNavGroup('Account', [
                          // Account holds the account name and the session
                          // artifact storage card, which every edition has
                          // and core pages link to. It used to hang off
                          // user_management, a flag only plugins set, so an
                          // open-source install had no way in from the nav.
                          this._renderNavLink(
                            '/console/settings/account',
                            html`<sl-menu-item>Account</sl-menu-item>`
                          ),
                          // Plans exist only where something is sold. Without
                          // the billing plugin the deployment has no catalog,
                          // no subscription and nothing for this page to say.
                          // It sits under Account because that is what it is
                          // about: what this account pays for.
                          this.features.billing
                            ? this._renderNavLink(
                                '/console/settings/plan',
                                html`<sl-menu-item>Plan</sl-menu-item>`
                              )
                            : nothing,
                          this._permissionsLoaded
                            ? this._renderNavLink(
                                '/console/settings/records',
                                html`<sl-menu-item>Records</sl-menu-item>`
                              )
                            : nothing,
                        ])}
                        ${this._renderNavGroup('People & access', [
                          this.features.user_management
                            ? this._renderNavLink(
                                '/console/settings/users',
                                html`<sl-menu-item>Users</sl-menu-item>`
                              )
                            : nothing,
                          this.features.team_management
                            ? this._renderNavLink(
                                '/console/settings/teams',
                                html`<sl-menu-item>Teams</sl-menu-item>`
                              )
                            : nothing,
                          this.features.user_management ||
                          this.features.team_management
                            ? this._renderNavLink(
                                '/console/settings/invitations',
                                html`<sl-menu-item>Invitations</sl-menu-item>`
                              )
                            : nothing,
                          // Served by an extension plugin; the capability in
                          // /features is the only switch.
                          ...(hasCapability(this.features, 'account_hierarchy')
                            ? [
                                this._renderNavLink(
                                  '/console/settings/subaccounts',
                                  html`<sl-menu-item>Subaccounts</sl-menu-item>`
                                ),
                                this._renderNavLink(
                                  '/console/settings/access-grants',
                                  html`<sl-menu-item
                                    >Access grants</sl-menu-item
                                  >`
                                ),
                              ]
                            : []),
                        ])}
                        ${this._renderNavGroup('Developers', [
                          this._ciSetupAvailable
                            ? this._renderNavLink(
                                '/console/settings/ci-identities',
                                html`<sl-menu-item>Restricted CI</sl-menu-item>`
                              )
                            : nothing,
                          this._renderNavLink(
                            '/console/settings/api-keys',
                            html`<sl-menu-item>API keys</sl-menu-item>`
                          ),
                          this._renderNavLink(
                            '/console/settings/runners',
                            html`<sl-menu-item>Runners</sl-menu-item>`
                          ),
                          this._renderNavLink(
                            '/console/settings/webhooks',
                            html`<sl-menu-item>Webhooks</sl-menu-item>`
                          ),
                          hasCapability(this.features, 'chat_connections')
                            ? this._renderNavLink(
                                '/console/settings/chat',
                                html`<sl-menu-item
                                  >Chat connections</sl-menu-item
                                >`
                              )
                            : nothing,
                        ])}
                        ${this._renderNavGroup('Personal', [
                          this._renderNavLink(
                            '/console/settings/profile',
                            html`<sl-menu-item>Profile</sl-menu-item>`
                          ),
                          this._renderNavLink(
                            '/console/settings/security',
                            html`<sl-menu-item>Security</sl-menu-item>`
                          ),
                          this._renderNavLink(
                            '/console/settings/appearance',
                            html`<sl-menu-item>Appearance</sl-menu-item>`
                          ),
                          this._renderNavLink(
                            '/console/settings/notification-preferences',
                            html`<sl-menu-item>Notifications</sl-menu-item>`
                          ),
                        ])}
                      </sl-menu>
                    </sl-details>
                    <!-- The kill switch sits outside Settings, at the foot of
                         the nav: an operator in an incident reaches it in one
                         click instead of expanding Settings and scanning past
                         a dozen configuration pages. -->
                    ${this._renderNavLink(
                      '/console/settings/emergency',
                      html`<sl-menu-item class="nav-emergency">
                        <sl-icon
                          name="exclamation-octagon"
                          slot="prefix"
                        ></sl-icon>
                        <span class="sidebar-label">Emergency</span>
                      </sl-menu-item>`
                    )}
                  </sl-menu>
                </div>
              </div>`
        }

        <div class="main-view ${this._windowMode ? 'window-mode' : ''}">
          ${
            this._windowMode
              ? nothing
              : html`<console-header>
                    <button
                      id="console-nav-toggle"
                      class="nav-toggle-button"
                      slot="nav-toggle"
                      type="button"
                      aria-controls="console-nav"
                      aria-expanded=${this._sidebarOpen ? 'true' : 'false'}
                      aria-label=${
                        this._sidebarOpen
                          ? 'Hide navigation'
                          : 'Show navigation'
                      }
                      @click=${this._handleSidebarToggle}
                    >
                      <sl-icon name="list" aria-hidden="true"></sl-icon>
                    </button>
                    ${
                      hasCapability(this.features, 'multi_account')
                        ? html`<capability-extension
                            slot="account-switcher"
                            name="account-switcher"
                          ></capability-extension>`
                        : nothing
                    }
                  </console-header>
                  <!-- The kill-switch banner sits directly under the header,
                       above the bypass banner: a halted account is the most
                       severe state and must be impossible to miss on any
                       console page (#157). -->
                  <kill-switch-banner></kill-switch-banner>
                  <!-- A relaxed governance state is visible on every console
                       page, not just the approvals view. -->
                  <approval-bypass-banner></approval-bypass-banner>
                  <!-- Usage sits under both governance banners: it is
                       information, not a fault, and it renders nothing at
                       all on OSS, where the endpoint does not exist. -->
                  <usage-nudge-banner></usage-nudge-banner>`
          }
          <!-- The skip-link target. Not a <main>: lit-app's router outlet
               is already the document's <main>, and this shell renders
               inside it, so a second one would nest main in main. -->
          <div
            id="console-main"
            tabindex="-1"
            class="main-content ${
              this._fullBleed || this._windowMode ? 'full-bleed' : ''
            }"
            @request-full-bleed=${(e: CustomEvent) =>
              (this._fullBleed = !!e.detail)}
          >
            ${(() => {
              // The outlet renders only once the answer is known and it is
              // "yes". Rendering the slot while features and permissions are
              // still in flight let a routed view paint (and keep painting,
              // behind permission-denied) on a page the shell then refused.
              // A routed view is a light-DOM child, so dropping the slot only
              // hides it: views that carry data also check before fetching.
              if (!this._featuresLoaded || !this._permissionsLoaded) {
                return html`<route-loading></route-loading>`;
              }
              const denied = this._deniedPermissionForPath(this._currentPath);
              return denied
                ? html`<permission-denied
                    required-permission=${denied}
                  ></permission-denied>`
                : html`<slot></slot>`;
            })()}
          </div>
        </div>
      </div>
    `;
  }
}
