import { LitElement, html, css } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import { Router } from '@vaadin/router';
import { getUserProfile, getFeatures } from '../api';
import { getBrandConfig, isSaaS } from '../brand-config';
import { trackGoal } from '../services/web-analytics';

import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/icon-button/icon-button.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import './logo-component';

interface User {
  username: string;
  email: string;
}

@customElement('app-header')
export class AppHeader extends LitElement {
  @property({ type: Boolean })
  showDrawerToggle = false;

  @state()
  private isAuthenticated = false;

  @state()
  private user: User | null = null;

  @state()
  private isMenuOpen = false;

  @state()
  private registrationEnabled = true;

  static styles = css`
    :host {
      display: block;
      position: relative;
    }
    .header-container {
      display: flex;
      justify-content: space-between;
      align-items: center;
      max-width: 1200px;
      margin: 0 auto;
      padding: 1.5rem 1rem;
    }
    nav {
      display: flex;
      align-items: center;
      gap: 0.5rem;
    }
    .logo {
      display: flex;
      align-items: center;
    }
    .logo logo-component {
      height: 36px;
    }
    sl-icon-button::part(base) {
      font-size: 1.5rem;
    }

    .mobile-menu-button {
      display: none;
    }

    .mobile-menu-button sl-icon-button {
      color: var(--sl-color-primary-500);
    }

    @media (max-width: 768px) {
      nav {
        display: none;
        position: absolute;
        top: 100%;
        left: 0;
        right: 0;
        background-color: #161b24;
        flex-direction: column;
        align-items: stretch;
        padding: 1rem;
        border-top: 1px solid #30363d;
        z-index: 100;
      }

      nav.mobile-menu-open {
        display: flex;
      }

      .mobile-menu-button {
        display: block;
      }

      nav sl-button {
        width: 100%;
        justify-content: flex-start;
      }

      nav sl-button::part(base) {
        justify-content: flex-start;
      }
    }
  `;

  connectedCallback() {
    super.connectedCallback();
    this.checkAuth();
    this.checkBillingEnabled();
    window.addEventListener('auth-change', () => this.checkAuth());
    window.addEventListener('vaadin-router-location-changed', () =>
      this.requestUpdate()
    );
  }

  async checkBillingEnabled() {
    try {
      const features = await getFeatures();
      // Registration is enabled by default, unless explicitly disabled
      this.registrationEnabled = features.features['registration'] !== false;
    } catch (error) {
      console.error('Failed to check registration feature:', error);
      this.registrationEnabled = true;
    }
  }

  handleSignup(e: Event) {
    e.preventDefault();
    this.isMenuOpen = false; // Close mobile menu
    // Signup is card-free (T2 paywall move): every signup door leads to
    // /register. Pricing keeps checkout as the deliberate upgrade path.
    trackGoal('Signup Click', { location: 'header' });
    Router.go('/register');
  }

  disconnectedCallback() {
    window.removeEventListener('auth-change', () => this.checkAuth());
    window.removeEventListener('vaadin-router-location-changed', () =>
      this.requestUpdate()
    );
    super.disconnectedCallback();
  }

  async checkAuth() {
    const token = localStorage.getItem('accessToken');
    this.isAuthenticated = !!token;
    if (this.isAuthenticated) {
      try {
        this.user = await getUserProfile();
      } catch (error) {
        console.error('Failed to fetch user details', error);
        this.logout();
      }
    } else {
      this.user = null;
    }
  }

  logout() {
    localStorage.removeItem('accessToken');
    localStorage.removeItem('refreshToken');
    this.isAuthenticated = false;
    this.user = null;
    const event = new CustomEvent('auth-change', {
      bubbles: true,
      composed: true,
    });
    window.dispatchEvent(event);
    Router.go('/login');
  }

  render() {
    return html`
      <header>
        <div class="header-container">
          <div class="flex items-center">
            ${
              this.showDrawerToggle
                ? html`<sl-icon-button
                    name="menu"
                    @click=${() =>
                      this.dispatchEvent(
                        new CustomEvent('toggle-drawer', {
                          bubbles: true,
                          composed: true,
                        })
                      )}
                  ></sl-icon-button>`
                : html`<div class="logo">
                    <a href="/">
                      <logo-component
                        alt="${getBrandConfig().name} Logo"
                        override-theme="dark"
                      ></logo-component>
                    </a>
                  </div>`
            }
          </div>
          <nav class="${this.isMenuOpen ? 'mobile-menu-open' : ''}">
            <sl-icon-button
              name="github"
              href="https://github.com/preloop/preloop"
              target="_blank"
              label="GitHub Repository"
              style="font-size: 1.25rem;"
            ></sl-icon-button>
            <sl-button
              href="https://docs.preloop.ai"
              target="_blank"
              variant="text"
              >Docs</sl-button
            >
            ${
              isSaaS()
                ? html`
                    <sl-button href="/about" variant="text">About</sl-button>
                    <sl-button href="/pricing" variant="text"
                      >Pricing</sl-button
                    >
                  `
                : ''
            }
            ${
              this.isAuthenticated && this.user
                ? html`
                    ${
                      window.location.pathname.startsWith('/console')
                        ? html`<sl-button @click=${this.logout}
                            >Logout</sl-button
                          >`
                        : html`<sl-button variant="primary" href="/console"
                            >Console</sl-button
                          >`
                    }
                  `
                : html`
                    <sl-button href="/login" variant="text">Sign in</sl-button>
                    ${
                      this.registrationEnabled
                        ? html`
                            <sl-button
                              variant="primary"
                              @click=${this.handleSignup}
                              data-track="cta_get_started_header"
                              >Sign Up</sl-button
                            >
                          `
                        : ''
                    }
                  `
            }
          </nav>
          <div class="mobile-menu-button">
            <sl-icon-button
              name="list"
              label="Menu"
              @click=${() => {
                this.isMenuOpen = !this.isMenuOpen;
              }}
            ></sl-icon-button>
          </div>
        </div>
      </header>
    `;
  }
}
