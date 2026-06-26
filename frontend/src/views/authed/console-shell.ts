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
import consoleStyles from '../../styles/console-styles.css?inline';
import { getFeatures, type FeaturesResponse } from '../../api';

const SIDEBAR_BREAKPOINT = 768;

// static styles = [formStyles, css`
//     h2 {}

//     `];

@customElement('console-shell')
export class ConsoleShell extends LitElement {
  @query('#upgrade-modal')
  private _upgradeModal!: HTMLElement;

  @state()
  private features: FeaturesResponse['features'] = {};

  @state()
  private _featuresLoaded = false;

  @state()
  private _sidebarOpen = false;

  @state()
  private _isMobile = false;

  @state()
  private _fullBleed = false;

  @state()
  private _currentPath = window.location.pathname;

  private _mediaQuery?: MediaQueryList;
  private _mediaQueryHandler?: (e: MediaQueryListEvent) => void;

  static styles = [
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

      .sidebar {
        width: 250px;
        flex-shrink: 0;
        display: flex;
        flex-direction: column;
        transition:
          width 0.2s ease,
          transform 0.25s ease;
        background-color: var(--sl-color-neutral-100);
        z-index: 100;
      }

      /* Desktop: when closed, sidebar is fully hidden (hamburger only) */
      .sidebar.closed {
        width: 0;
        min-width: 0;
        overflow: hidden;
        padding: 0;
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
        background-color: var(--sl-color-neutral-100);
        color: var(--sl-color-primary-500);
      }

      .sign-out-menu sl-menu-item:hover::part(base) {
        background-color: var(--sl-color-neutral-100);
        color: var(--sl-color-primary-700);
      }

      .main-view {
        flex-grow: 1;
        display: grid;
        grid-template-rows: auto 1fr; /* Header row, Content row */
        overflow-y: hidden;
        background-color: var(--sl-color-neutral-0);
      }

      .main-content {
        overflow-y: auto;
        padding: 1rem 2rem 2rem 2rem;
        display: flex;
        flex-direction: column;
        align-items: center;
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
        background-color: var(--sl-color-neutral-100);
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
        background-color: var(--sl-color-neutral-100);
        padding: 0;
        margin-left: -2px;
      }

      sl-details::part(base) {
        width: 100%;
        border-width: 0;
        background-color: var(--sl-color-neutral-100);
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
      }

      /* Style the anchor, not ::part — Shoelace shadow styles override ::part rules */
      .sidebar-link.active {
        background-color: var(--sl-color-neutral-200);
      }

      .sidebar-link.active .sidebar-label,
      .sidebar-link.active sl-menu-item::part(label) {
        font-weight: var(--sl-font-weight-bold);
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

      sl-details.settings-section[open]::part(summary) {
        font-weight: var(--sl-font-weight-bold);
      }
    `,
  ];

  async connectedCallback() {
    super.connectedCallback();
    window.addEventListener('show-upgrade-modal', () => {
      (this._upgradeModal as any).show();
    });
    window.addEventListener(
      'vaadin-router-location-changed',
      this._handleLocationChanged
    );
    this._mediaQuery = window.matchMedia(
      `(max-width: ${SIDEBAR_BREAKPOINT}px)`
    );
    this._isMobile = this._mediaQuery.matches;
    this._sidebarOpen = !this._mediaQuery.matches; // desktop: visible, mobile: hidden
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

    // Fetch enabled features
    try {
      const response = await getFeatures();
      this.features = response.features;
    } catch (error) {
      console.error('Failed to fetch features:', error);
      // Default to empty features if fetch fails
      this.features = {};
    } finally {
      this._featuresLoaded = true;
    }
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

  private _renderNavLink(
    href: string,
    content: TemplateResult,
    exact = false
  ): TemplateResult {
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
    }
  }

  disconnectedCallback() {
    document.body.style.overflow = '';
    window.removeEventListener('show-upgrade-modal', () => {
      (this._upgradeModal as any).show();
    });
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
        You have exceeded the usage limits of your current plan. Please upgrade
        to continue using this feature.
        <sl-button slot="footer" href="/console/pricing">
          View Plans
        </sl-button>
      </sl-dialog>

      <global-notice></global-notice>

      <div class="console-container">
        <div class="sidebar-wrapper">
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
            <sl-menu style="font-size: 16px;">
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
                    <sl-icon src="/images/flow.svg" slot="prefix"></sl-icon>
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
              ${this._renderNavLink(
                '/console/trackers',
                html`
                  <sl-menu-item>
                    <sl-icon src="/images/git.svg" slot="prefix"></sl-icon>
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
                '/console/runtime-sessions',
                html`
                  <sl-menu-item>
                    <sl-icon name="collection" slot="prefix"></sl-icon>
                    <span class="sidebar-label">Sessions</span>
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
              ${this._renderNavLink(
                '/console/approvals',
                html`
                  <sl-menu-item>
                    <sl-icon name="shield-check" slot="prefix"></sl-icon>
                    <span class="sidebar-label">Approvals</span>
                  </sl-menu-item>
                `
              )}
              ${this._featuresLoaded && this.features['audit_logs']
                ? this._renderNavLink(
                    '/console/audit',
                    html`
                      <sl-menu-item>
                        <sl-icon name="journal-text" slot="prefix"></sl-icon>
                        <span class="sidebar-label">Audit</span>
                      </sl-menu-item>
                    `
                  )
                : ''}
              <sl-details
                class="settings-section"
                ?open=${this._isSettingsActive()}
              >
                <span slot="summary">
                  <sl-icon name="gear" style="padding-right: 6px;"></sl-icon>
                  <span class="sidebar-label">Settings</span>
                </span>
                <sl-menu>
                  ${this.features.user_management
                    ? html`${this._renderNavLink(
                        '/console/settings/account',
                        html`<sl-menu-item>Account</sl-menu-item>`
                      )}
                      ${this._renderNavLink(
                        '/console/settings/users',
                        html`<sl-menu-item>Users</sl-menu-item>`
                      )}`
                    : ''}
                  ${this.features.team_management
                    ? this._renderNavLink(
                        '/console/settings/teams',
                        html`<sl-menu-item>Teams</sl-menu-item>`
                      )
                    : ''}
                  ${this.features.user_management ||
                  this.features.team_management
                    ? this._renderNavLink(
                        '/console/settings/invitations',
                        html`<sl-menu-item>Invitations</sl-menu-item>`
                      )
                    : ''}
                  ${this._renderNavLink(
                    '/console/settings/api-keys',
                    html`<sl-menu-item>API Keys</sl-menu-item>`
                  )}
                </sl-menu>
              </sl-details>
            </sl-menu>
          </div>
        </div>

        <div class="main-view">
          <console-header>
            <sl-icon-button
              slot="nav-toggle"
              name="list"
              label="Open menu"
              @click=${this._handleSidebarToggle}
            ></sl-icon-button>
          </console-header>
          <div
            class="main-content ${this._fullBleed ? 'full-bleed' : ''}"
            @request-full-bleed=${(e: CustomEvent) =>
              (this._fullBleed = !!e.detail)}
          >
            <slot></slot>
          </div>
        </div>
      </div>
    `;
  }
}
