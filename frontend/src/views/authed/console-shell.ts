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
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/icon-button/icon-button.js';
import '@shoelace-style/shoelace/dist/components/details/details.js';
import '@shoelace-style/shoelace/dist/components/dialog/dialog.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '../../components/logo-component';
import '../../components/global-notice';
import '../../components/console-header';
import '../../components/approval-bypass-banner';
import consoleStyles from '../../styles/console-styles.css?inline';
import {
  getFeatures,
  getUserProfile,
  hasAnyPermission,
  startCheckout,
  type FeaturesResponse,
  type UserPermissions,
} from '../../api';
import '../../components/permission-denied';
import { consoleDialogStyles } from '../../styles/console-dialog';

/** Nav items that require at least one of the listed permissions when RBAC is on. */
const NAV_PERMISSIONS: Record<string, string[]> = {
  '/console/agents': ['view_agents'],
  '/console/flows': ['view_flows'],
  '/console/settings/runners': ['view_flows'],
  '/console/tools': ['view_tools', 'view_policies'],
  '/console/policies': ['view_policies'],
  '/console/trackers': ['view_trackers'],
  '/console/ai-models': ['view_ai_models'],
  '/console/runtime-sessions': ['view_runtime_sessions'],
  '/console/cost': ['view_cost'],
  '/console/approvals': ['view_approvals'],
  '/console/audit': ['view_audit_logs'],
  '/console/settings/users': ['view_users'],
  '/console/settings/teams': ['view_teams'],
  '/console/settings/invitations': ['invite_users', 'view_users'],
  '/console/settings/account': ['manage_account', 'view_billing'],
};

const SIDEBAR_BREAKPOINT = 768;

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

// Human-readable names for gated features (402 upgrade contract).
const PREMIUM_FEATURE_LABELS: Record<string, string> = {
  session_optimization: 'AI session optimization',
  replay_verification: 'replay verification',
  session_titles: 'AI session titles',
};

@customElement('console-shell')
export class ConsoleShell extends LitElement {
  @query('#upgrade-modal')
  private _upgradeModal!: HTMLElement;

  @state()
  private _upgradeFeature = '';

  @state()
  private _upgradeStarting = false;

  @state()
  private features: FeaturesResponse['features'] = {};

  @state()
  private _featuresLoaded = false;

  @state()
  private _permissions: UserPermissions = undefined;

  @state()
  private _permissionsLoaded = false;

  @state()
  private _isSuperuser = false;

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
        grid-template-rows: auto auto 1fr; /* Header, bypass banner, content */
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
        padding: 1rem 2rem 2rem 2rem;
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

      .main-content > ::slotted(*) {
        width: 100%;
        max-width: 80rem;
      }

      .main-content.full-bleed > ::slotted(*) {
        max-width: none;
        height: 100%;
      }

      @media (max-width: 768px) {
        .main-content {
          padding: 1rem;
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
    `,
  ];

  private _handleShowUpgradeModal = (event: Event) => {
    const detail = (event as CustomEvent).detail;
    this._upgradeFeature =
      detail?.code === 'upgrade_required' ? String(detail.feature || '') : '';
    (this._upgradeModal as any).show();
  };

  private async _startUpgradeCheckout() {
    this._upgradeStarting = true;
    try {
      // Land back exactly where the gate was hit once checkout-success
      // reconciles the new subscription (webhook-independent).
      await startCheckout(
        'teams',
        'month',
        window.location.pathname + window.location.search
      );
    } catch (error) {
      console.error('Failed to start upgrade checkout', error);
      this._upgradeStarting = false;
    }
  }

  async connectedCallback() {
    super.connectedCallback();
    window.addEventListener('show-upgrade-modal', this._handleShowUpgradeModal);
    window.addEventListener(
      'vaadin-router-location-changed',
      this._handleLocationChanged
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

  private _canAccess(href: string): boolean {
    const required = NAV_PERMISSIONS[href];
    if (!required) {
      return true;
    }
    return hasAnyPermission(this._permissions, required);
  }

  /**
   * The Policies page ships as a preview: hidden unless the instance opts in
   * through the `policies_console` feature flag, or the viewer is an instance
   * admin. Normal `view_policies` permission still applies on top of that.
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
    for (const [href, required] of Object.entries(NAV_PERMISSIONS)) {
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

  private _handleSidebarToggle = () => {
    this._sidebarOpen = !this._sidebarOpen;
  };

  private _closeSidebar = () => {
    if (this._isMobile) {
      this._sidebarOpen = false;
    }
  };

  private _handleLocationChanged = () => {
    this._fullBleed = false;
    this._currentPath = window.location.pathname;
    this._windowMode = isWindowChromeRequested(window.location.search);
    this._publishMainOffset();
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
    return current === target || current.startsWith(`${target}/`);
  }

  private _isSettingsActive(): boolean {
    return this._isNavActive('/console/settings');
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
    window.removeEventListener(
      'vaadin-router-location-changed',
      this._handleLocationChanged
    );
    window.removeEventListener('popstate', this._handleLocationChanged);
    this._mediaQuery?.removeEventListener('change', this._mediaQueryHandler!);
    super.disconnectedCallback();
  }

  render() {
    return html`
      <sl-dialog id="upgrade-modal" label="Upgrade Your Plan">
        ${
          this._upgradeFeature
            ? html`${
                PREMIUM_FEATURE_LABELS[this._upgradeFeature] ||
                this._upgradeFeature
              }
              is a Teams feature. Upgrade to unlock it — you'll come right back
              here, already unlocked.`
            : html`You have exceeded the usage limits of your current plan.
              Please upgrade to continue using this feature.`
        }
        <sl-button slot="footer" href="/console/pricing">
          View Plans
        </sl-button>
        <sl-button
          slot="footer"
          variant="primary"
          ?loading=${this._upgradeStarting}
          @click=${this._startUpgradeCheckout}
        >
          Upgrade now
        </sl-button>
      </sl-dialog>

      <global-notice></global-notice>

      <div class="console-container">
        ${
          this._windowMode
            ? nothing
            : html`<div class="sidebar-wrapper">
                <div
                  class="sidebar-backdrop ${this._sidebarOpen ? 'visible' : ''}"
                  @click=${this._closeSidebar}
                  aria-hidden="true"
                ></div>
                <div
                  class="sidebar ${this._sidebarOpen ? 'open' : 'closed'}"
                  role="navigation"
                  aria-label="Console navigation"
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
                      '/console/ai-models',
                      html`
                        <sl-menu-item>
                          <sl-icon name="cpu" slot="prefix"></sl-icon>
                          <span class="sidebar-label">Models</span>
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
                        ${
                          this.features.user_management
                            ? html`${this._renderNavLink(
                                '/console/settings/account',
                                html`<sl-menu-item>Account</sl-menu-item>`
                              )}
                              ${this._renderNavLink(
                                '/console/settings/users',
                                html`<sl-menu-item>Users</sl-menu-item>`
                              )}`
                            : ''
                        }
                        ${
                          this.features.team_management
                            ? this._renderNavLink(
                                '/console/settings/teams',
                                html`<sl-menu-item>Teams</sl-menu-item>`
                              )
                            : ''
                        }
                        ${
                          this.features.user_management ||
                          this.features.team_management
                            ? this._renderNavLink(
                                '/console/settings/invitations',
                                html`<sl-menu-item>Invitations</sl-menu-item>`
                              )
                            : ''
                        }
                        ${this._renderNavLink(
                          '/console/settings/api-keys',
                          html`<sl-menu-item>API Keys</sl-menu-item>`
                        )}
                        ${this._renderNavLink(
                          '/console/settings/runners',
                          html`<sl-menu-item>Runners</sl-menu-item>`
                        )}
                      </sl-menu>
                    </sl-details>
                  </sl-menu>
                </div>
              </div>`
        }

        <div class="main-view ${this._windowMode ? 'window-mode' : ''}">
          ${
            this._windowMode
              ? nothing
              : html`<console-header>
                    <sl-icon-button
                      slot="nav-toggle"
                      name="list"
                      label="Open menu"
                      @click=${this._handleSidebarToggle}
                    ></sl-icon-button>
                  </console-header>
                  <!-- Sits directly under the header so a relaxed governance
                       state is visible on every console page, not just the
                       approvals view. -->
                  <approval-bypass-banner></approval-bypass-banner>`
          }
          <div
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
                return nothing;
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
