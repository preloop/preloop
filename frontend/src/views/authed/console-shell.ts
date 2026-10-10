import {
  LitElement,
  html,
  css,
  unsafeCSS,
  nothing,
  type TemplateResult,
} from 'lit';
import { customElement, query, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/icon-button/icon-button.js';
import '@shoelace-style/shoelace/dist/components/dialog/dialog.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '../../components/logo-component';
import '../../components/global-notice';
import { PENDING_APPROVALS_EVENT } from '../../components/console-header';
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
  '/console/api-usage': ['view_cost'],
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
 * `/console/approval/:id` is a single approval opened from a notification
 * or email; it highlights Approvals.
 */
const NAV_ALIASES: Record<string, string[]> = {
  '/console/approvals': ['/console/approval'],
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
  private _planChoiceEmail = '';

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

  /** Unexpired pending approvals, published by the header for the badge. */
  @state()
  private _pendingApprovalsCount = 0;

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

      #console-nav {
        --nav-row-inset: 1.25rem;
      }

      .nav-subitem {
        padding-left: var(--sl-spacing-medium);
      }

      .nav-text {
        display: flex;
        flex-direction: column;
        min-width: 0;
      }

      .nav-subtitle {
        margin-left: 0.5rem;
        font-size: var(--console-text-caption, 0.75rem);
        color: var(--sl-color-neutral-600);
        white-space: normal;
      }

      .nav-list {
        list-style: none;
        margin: 0;
        padding: 0;
        font-size: var(--console-text-body);
      }
      .sidebar-link {
        display: flex;
        align-items: center;
        padding: 0.5em 0.5em 0.5em var(--nav-row-inset);
        color: inherit;
        text-decoration: none;
        border-radius: var(--sl-border-radius-medium);
        border-left: 3px solid transparent;
      }
      .sidebar-link:hover {
        background-color: var(--console-hover-tint);
      }
      .sidebar-link.active {
        background-color: var(--console-selected-tint);
        border-left-color: var(--console-link-color);
        color: var(--console-link-color);
        font-weight: 600;
      }
      .nav-row {
        display: flex;
        align-items: center;
        gap: var(--sl-spacing-x-small);
        width: 100%;
      }
      .sidebar-link sl-icon,
      details.nav-section sl-icon {
        font-size: 18px;
        flex: 0 0 auto;
      }
      .sidebar-label {
        font-size: var(--console-text-body);
      }
      details.nav-section {
        width: 100%;
      }
      details.nav-section > summary {
        display: flex;
        align-items: center;
        cursor: pointer;
        padding: 0.5em 0.5em 0.5em var(--nav-row-inset);
        border-left: 3px solid transparent;
        min-height: calc(
          var(--sl-font-size-medium) * var(--sl-line-height-normal)
        );
      }
      details.nav-section > summary::before {
        content: '›';
        width: 0.9em;
        margin-left: -0.9em;
      }
      details.nav-section[open] > summary::before {
        content: '⌄';
      }
      details.nav-section > summary::marker {
        content: '';
      }
      details.nav-section > summary::-webkit-details-marker {
        display: none;
      }
      details.nav-section > summary .sidebar-label {
        margin-left: calc(0.5rem + var(--sl-spacing-x-small));
      }
      details.nav-section > .nav-list {
        padding-left: 1.5rem;
      }
      .nav-section-badge {
        margin-left: auto;
        margin-right: var(--sl-spacing-x-small);
      }
      details.nav-section[open] .nav-section-badge {
        display: none;
      }
      .nav-group-label {
        padding: var(--sl-spacing-small) 0.5em var(--sl-spacing-3x-small);
        font-size: var(--sl-font-size-x-small);
        letter-spacing: 0.04em;
        text-transform: uppercase;
        color: var(--console-meta-color);
      }
      .route-announcement {
        position: absolute;
        width: 1px;
        height: 1px;
        padding: 0;
        margin: -1px;
        overflow: hidden;
        clip-path: inset(50%);
        white-space: nowrap;
        border: 0;
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
    this.addEventListener(
      'console-view-heading-ready',
      this._handleHeadingReady
    );
    this.addEventListener('console-view-updated', this._handleHeadingReady);
    window.addEventListener(LOCATION_CHANGED, this._handleLocationChanged);
    window.addEventListener(
      PENDING_APPROVALS_EVENT,
      this._handlePendingApprovals
    );
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
    this._planChoiceEmail = profile.email;
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

  @state() private _navigationAnnouncement = '';
  private _lastNavigationPath = window.location.pathname;
  private _pendingFocusPath: string | null = null;

  private _focusedHeading: HTMLElement | null = null;
  private _focusedPath = '';
  private _handleHeadingReady = () => {
    if (
      !this._pendingFocusPath &&
      this._focusedHeading &&
      !this._focusedHeading.isConnected &&
      this._focusedPath === window.location.pathname
    ) {
      let active = document.activeElement;
      while (active?.shadowRoot?.activeElement)
        active = active.shadowRoot.activeElement;
      if (
        active === document.body ||
        active === this.firstElementChild ||
        active?.matches('lit-app, console-shell')
      )
        this._pendingFocusPath = this._focusedPath;
    }
    void this._focusNewView();
  };

  private async _focusNewView(): Promise<void> {
    if (
      !this._pendingFocusPath ||
      this._pendingFocusPath !== window.location.pathname
    )
      return;
    await this.updateComplete;
    if (!this.isConnected || !this._pendingFocusPath) return;
    const view = this.firstElementChild as
      (HTMLElement & { updateComplete?: Promise<unknown> }) | null;
    if (view?.updateComplete) await view.updateComplete;
    await new Promise<void>((resolve) =>
      requestAnimationFrame(() => resolve())
    );
    if (
      !this._pendingFocusPath ||
      this._pendingFocusPath !== window.location.pathname
    )
      return;
    const find = (root: Element | DocumentFragment): HTMLElement | null => {
      if (root instanceof Element && root.tagName === 'VIEW-HEADER') {
        return root.shadowRoot?.querySelector<HTMLElement>('h1') ?? null;
      }
      for (const child of root.children) {
        const heading =
          (child.shadowRoot && find(child.shadowRoot)) || find(child);
        if (heading) return heading;
      }
      return null;
    };
    const denied = this._deniedPermissionForPath(this._currentPath);
    const heading = find(denied ? this.renderRoot : this);
    if (!heading) return; // A loading view will emit heading-ready after it renders.
    heading.focus({ preventScroll: true });
    this._focusedHeading = heading;
    this._focusedPath = this._pendingFocusPath;
    this._navigationAnnouncement = `${heading.textContent?.trim() || 'Page'} page.`;
    this._pendingFocusPath = null;
  }

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
    if (path !== this._lastNavigationPath) {
      this._lastNavigationPath = path;
      this._pendingFocusPath = path;
      void this._focusNewView();
    }
  };

  private _handleNavClick = (e: Event) => {
    const anchor = e.currentTarget as HTMLAnchorElement;
    if (anchor.href) {
      this._currentPath = new URL(anchor.href).pathname;
    }
    this._closeSidebar();
  };

  private _handlePendingApprovals = (event: Event) => {
    this._pendingApprovalsCount = (event as CustomEvent<number>).detail ?? 0;
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
      !this._isNavActive('/console/settings/emergency') &&
      !this._isNavActive('/console/settings/records')
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
    return html`<li class="nav-group-label">${label}</li>
      ${visible}`;
  }

  /** True when any Audit child is visible for this user/edition. */
  private _hasAuditSection(): boolean {
    return (
      this._canAccess('/console/approvals') ||
      this._canAccess('/console/runtime-sessions') ||
      this._canShowAuditEvents() ||
      this._canAccess('/console/artifacts') ||
      this._canAccess('/console/settings/records')
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
      this._isNavActive('/console/approvals') ||
      this._isNavActive('/console/runtime-sessions') ||
      this._isNavActive('/console/audit') ||
      this._isNavActive('/console/artifacts') ||
      this._isNavActive('/console/settings/records')
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
      <li>
        <a
          href=${href}
          class="sidebar-link ${active ? 'active' : ''}"
          aria-current=${active ? 'page' : nothing}
          @click=${this._handleNavClick}
        >
          ${content}
        </a>
      </li>
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
    this.removeEventListener(
      'console-view-heading-ready',
      this._handleHeadingReady
    );
    this.removeEventListener('console-view-updated', this._handleHeadingReady);
    window.removeEventListener(LOCATION_CHANGED, this._handleLocationChanged);
    window.removeEventListener('popstate', this._handleLocationChanged);
    window.removeEventListener(
      PENDING_APPROVALS_EVENT,
      this._handlePendingApprovals
    );
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
          .email=${this._planChoiceEmail}
          .checking=${this._planChoice === 'checking'}
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

      <span
        class="route-announcement"
        role="status"
        aria-live="polite"
        aria-atomic="true"
        >${this._navigationAnnouncement}</span
      >
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
                <nav
                  id="console-nav"
                  class="sidebar ${this._sidebarOpen ? 'open' : 'closed'}"
                  aria-label="Console navigation"
                  ?inert=${!this._sidebarOpen}
                >
                  <div class="logo">
                    <a href="/console" @click=${this._closeSidebar}
                      ><logo-component></logo-component
                    ></a>
                  </div>
                  <ul class="nav-list">
                    ${this._renderNavLink(
                      '/console',
                      html`
                        <span class="nav-row">
                          <sl-icon name="house" slot="prefix"></sl-icon>
                          <span class="sidebar-label">Overview</span>
                        </span>
                      `,
                      true
                    )}
                    ${this._renderNavLink(
                      '/console/agents',
                      html`
                        <span class="nav-row">
                          <sl-icon name="robot" slot="prefix"></sl-icon>
                          <span class="sidebar-label">Agents</span>
                        </span>
                      `
                    )}
                    ${this._renderNavLink(
                      '/console/flows',
                      html`
                        <span class="nav-row">
                          <sl-icon
                            src="/images/flow.svg"
                            slot="prefix"
                          ></sl-icon>
                          <span class="sidebar-label">Flows</span>
                        </span>
                      `
                    )}
                    ${this._renderNavLink(
                      '/console/ai-models',
                      html`
                        <span class="nav-row">
                          <sl-icon name="cpu" slot="prefix"></sl-icon>
                          <span class="sidebar-label">Models</span>
                        </span>
                      `
                    )}
                    ${this._renderNavLink(
                      '/console/tools',
                      html`
                        <span class="nav-row">
                          <sl-icon name="tools" slot="prefix"></sl-icon>
                          <span class="sidebar-label">Tools</span>
                        </span>
                      `
                    )}
                    ${
                      this._canShowPolicies()
                        ? this._renderNavLink(
                            '/console/policies',
                            html`
                              <span class="nav-row">
                                <sl-icon
                                  name="shield-lock"
                                  slot="prefix"
                                ></sl-icon>
                                <span class="sidebar-label">Policies</span>
                              </span>
                            `
                          )
                        : nothing
                    }
                    ${this._renderNavLink(
                      '/console/trackers',
                      html`
                        <span class="nav-row">
                          <sl-icon
                            src="/images/git.svg"
                            slot="prefix"
                          ></sl-icon>
                          <span class="sidebar-label">Trackers</span>
                        </span>
                      `
                    )}
                    ${this._renderNavLink(
                      '/console/cost',
                      html`
                        <span class="nav-row">
                          <sl-icon name="cash-coin" slot="prefix"></sl-icon>
                          <span class="sidebar-label">Cost</span>
                        </span>
                      `
                    )}
                    ${this._renderNavLink(
                      '/console/api-usage',
                      html`
                        <span class="nav-row nav-subitem">
                          <sl-icon name="activity" slot="prefix"></sl-icon>
                          <span class="nav-text">
                            <span class="sidebar-label">API usage</span>
                            <span class="nav-subtitle"
                              >Gateway traffic and rate limits</span
                            >
                          </span>
                        </span>
                      `
                    )}
                    ${
                      this._hasAuditSection()
                        ? html`
                            <li class="nav-section-item">
                              <details
                                class="nav-section"
                                ?open=${this._isAuditActive()}
                              >
                                <summary>
                                  <sl-icon name="journal-text"></sl-icon>
                                  <span class="sidebar-label">Audit</span>
                                  ${
                                    // The Approvals badge sits inside the
                                    // group, so a closed group repeats the
                                    // count on its header.
                                    this._pendingApprovalsCount > 0 &&
                                    this._canAccess('/console/approvals')
                                      ? html`<sl-badge
                                          class="nav-section-badge"
                                          variant="primary"
                                          pill
                                          >${this._pendingApprovalsCount}</sl-badge
                                        >`
                                      : nothing
                                  }
                                </summary>
                                <ul class="nav-list">
                                  ${this._renderNavLink(
                                    '/console/approvals',
                                    html`<span class="nav-row"
                                      >Approvals${
                                        this._pendingApprovalsCount > 0
                                          ? html`<sl-badge
                                              slot="suffix"
                                              variant="primary"
                                              pill
                                              >${this._pendingApprovalsCount}</sl-badge
                                            >`
                                          : nothing
                                      }</span
                                    >`
                                  )}
                                  ${this._renderNavLink(
                                    '/console/runtime-sessions',
                                    html`<span class="nav-row">Sessions</span>`
                                  )}
                                  ${
                                    this._canShowAuditEvents()
                                      ? this._renderNavLink(
                                          '/console/audit',
                                          html`<span class="nav-row"
                                            >All events</span
                                          >`
                                        )
                                      : nothing
                                  }
                                  ${this._renderNavLink(
                                    '/console/artifacts',
                                    html`<span class="nav-row">Artifacts</span>`
                                  )}
                                  ${
                                    this._permissionsLoaded
                                      ? this._renderNavLink(
                                          '/console/settings/records',
                                          html`<span class="nav-row"
                                            >Records</span
                                          >`
                                        )
                                      : ''
                                  }
                                </ul>
                              </details>
                            </li>
                          `
                        : nothing
                    }
                    <li class="nav-section-item">
                      <details
                        class="nav-section"
                        ?open=${this._isSettingsActive()}
                      >
                        <summary>
                          <sl-icon name="gear"></sl-icon>
                          <span class="sidebar-label">Settings</span>
                        </summary>
                        <ul class="nav-list">
                          ${this._renderNavGroup('Account', [
                            // Account holds the account name and the session
                            // artifact storage card, which every edition has
                            // and core pages link to. It used to hang off
                            // user_management, a flag only plugins set, so an
                            // open-source install had no way in from the nav.
                            this._renderNavLink(
                              '/console/settings/account',
                              html`<span class="nav-row">Account</span>`
                            ),
                            // Plans exist only where something is sold. Without
                            // the billing plugin the deployment has no catalog,
                            // no subscription and nothing for this page to say.
                            // It sits under Account because that is what it is
                            // about: what this account pays for.
                            this.features.billing
                              ? this._renderNavLink(
                                  '/console/settings/plan',
                                  html`<span class="nav-row">Plan</span>`
                                )
                              : nothing,
                          ])}
                          ${this._renderNavGroup('People & access', [
                            this.features.user_management
                              ? this._renderNavLink(
                                  '/console/settings/users',
                                  html`<span class="nav-row">Users</span>`
                                )
                              : nothing,
                            this.features.team_management
                              ? this._renderNavLink(
                                  '/console/settings/teams',
                                  html`<span class="nav-row">Teams</span>`
                                )
                              : nothing,
                            this.features.user_management ||
                            this.features.team_management
                              ? this._renderNavLink(
                                  '/console/settings/invitations',
                                  html`<span class="nav-row">Invitations</span>`
                                )
                              : nothing,
                            // Served by an extension plugin; the capability in
                            // /features is the only switch. Subaccounts have no
                            // entry: they are created from the Account page,
                            // and people and teams get access to them from the
                            // Users and Teams pages. This page is the overview
                            // of every grant.
                            hasCapability(this.features, 'account_hierarchy')
                              ? this._renderNavLink(
                                  '/console/settings/access-grants',
                                  html`<span class="nav-row"
                                    >Access grants</span
                                  >`
                                )
                              : nothing,
                          ])}
                          ${this._renderNavGroup('Developers', [
                            this._ciSetupAvailable
                              ? this._renderNavLink(
                                  '/console/settings/ci-identities',
                                  html`<span class="nav-row"
                                    >Restricted CI</span
                                  >`
                                )
                              : nothing,
                            this._renderNavLink(
                              '/console/settings/api-keys',
                              html`<span class="nav-row">API keys</span>`
                            ),
                            this._renderNavLink(
                              '/console/settings/runners',
                              html`<span class="nav-row">Runners</span>`
                            ),
                            this._renderNavLink(
                              '/console/settings/webhooks',
                              html`<span class="nav-row">Webhooks</span>`
                            ),
                            hasCapability(this.features, 'chat_connections')
                              ? this._renderNavLink(
                                  '/console/settings/chat',
                                  html`<span class="nav-row"
                                    >Chat connections</span
                                  >`
                                )
                              : nothing,
                          ])}
                          ${this._renderNavGroup('Personal', [
                            this._renderNavLink(
                              '/console/settings/profile',
                              html`<span class="nav-row">Profile</span>`
                            ),
                            this._renderNavLink(
                              '/console/settings/security',
                              html`<span class="nav-row">Security</span>`
                            ),
                            this._renderNavLink(
                              '/console/settings/appearance',
                              html`<span class="nav-row">Appearance</span>`
                            ),
                            this._renderNavLink(
                              '/console/settings/notification-preferences',
                              html`<span class="nav-row">Notifications</span>`
                            ),
                          ])}
                        </ul>
                      </details>
                    </li>
                    <!-- The kill switch sits outside Settings, at the foot of
                         the nav: an operator in an incident reaches it in one
                         click instead of expanding Settings and scanning past
                         a dozen configuration pages. -->
                    ${this._renderNavLink(
                      '/console/settings/emergency',
                      html`<span class="nav-row nav-emergency">
                        <sl-icon
                          name="exclamation-octagon"
                          slot="prefix"
                        ></sl-icon>
                        <span class="sidebar-label">Emergency</span>
                      </span>`
                    )}
                  </ul>
                </nav>
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
