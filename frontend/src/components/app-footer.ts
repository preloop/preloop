import { LitElement, html, css } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import { router } from '../router';
import { getBrandConfig, isSaaS } from '../brand-config';
import { getFeatures } from '../api';
import './logo-component';

@customElement('app-footer')
export class AppFooter extends LitElement {
  @state() private _registrationEnabled = true;

  async connectedCallback() {
    super.connectedCallback();
    await this._checkRegistrationEnabled();
  }

  private async _checkRegistrationEnabled() {
    try {
      const features = await getFeatures();
      // Registration is enabled by default, unless explicitly disabled
      this._registrationEnabled = features.features['registration'] !== false;
    } catch (error) {
      // Default to enabled if we can't fetch features
      this._registrationEnabled = true;
    }
  }

  static styles = [
    css`
      :host {
        display: block;
        color: rgb(161, 161, 170);
        padding: 0 0 48px 0;
        flex-shrink: 0;
      }

      p {
        line-height: 1.6;
      }

      .footer-container {
        max-width: 1200px;
        margin: 0 auto;
        padding: 0 16px;
      }

      .footer-main {
        display: grid;
        grid-template-columns: minmax(0, 1fr) auto;
        align-items: flex-start;
        gap: 48px;
      }

      .footer-brand {
        max-width: 460px;
      }

      .footer-brand-header {
        display: flex;
        align-items: center;
        gap: 16px;
        margin-bottom: 16px;
      }

      .footer-description {
        margin: 0;
        color: rgba(203, 213, 225, 0.84);
        font-size: 0.95rem;
        line-height: 1.65;
      }

      .footer-nav {
        text-align: right;
        min-width: 150px;
      }

      .footer-nav ul {
        list-style: none;
        padding: 0;
        margin: 0;
      }

      .footer-nav li {
        margin-bottom: 10px;
      }

      .footer-nav a {
        font-size: 0.9rem;
        color: rgb(161, 161, 170);
        transition: color 0.2s ease;
        text-decoration: none;
        cursor: pointer;
      }

      .footer-nav a:hover {
        color: rgb(178, 178, 182);
      }

      .divider {
        margin: 34px 0;
        border-top: 1px solid rgba(255, 255, 255, 0.1);
      }

      .footer-bottom {
        display: flex;
        justify-content: space-between;
        align-items: center;
        margin-top: 30px;
      }

      .copyright-text {
        font-size: 0.85rem;
      }

      .copyright-text a {
        color: inherit;
        text-decoration: none;
      }

      .copyright-text a:hover {
        text-decoration: underline;
      }

      .social-links {
        display: flex;
        gap: 16px;
      }

      @media (max-width: 768px) {
        .footer-main {
          grid-template-columns: 1fr;
          gap: 32px;
        }

        .footer-brand-header {
          margin-bottom: 14px;
        }

        .footer-nav {
          text-align: left;
          margin-left: 0;
        }

        .footer-bottom {
          align-items: flex-start;
          flex-direction: column;
          gap: 18px;
        }
      }
    `,
  ];

  handleLinkClick(event: MouseEvent) {
    event.preventDefault();
    const target = event.target as HTMLAnchorElement;
    const path = target.getAttribute('href');
    if (path) {
      router.navigate(path);
    }
  }

  render() {
    const config = getBrandConfig();
    const company = config.company;
    const social = config.social;
    const hasCompanyInfo = company?.legal_name || company?.address;

    return html`
      <div class="footer-container">
        <div class="divider"></div>
        <div class="footer-main">
          <div class="footer-brand">
            <div class="footer-brand-header">
              <logo-component override-theme="dark"></logo-component>
            </div>
            <p class="footer-description">
              Preloop helps teams control tool access, require approvals, track
              spend, and keep audit trails before AI agents touch real systems.
            </p>
            ${hasCompanyInfo
              ? html`
                  <p style="margin-top: 1rem;">
                    ${company.legal_name
                      ? html`${company.legal_name}<br />`
                      : ''}
                    ${company.address ? html`${company.address}<br />` : ''}
                    ${company.city ? html`${company.city}` : ''}
                  </p>
                `
              : ''}
          </div>
          <nav class="footer-nav">
            <ul>
              ${this._registrationEnabled
                ? html`<li><a href="/register">Register</a></li>`
                : ''}
              <li><a href="/login">Sign in</a></li>
              <li><a href="/privacy">Privacy Policy</a></li>
              <li><a href="/terms">Terms of Service</a></li>
              <li><a href="/whatis-mcp">What is MCP?</a></li>
              <li><a href="https://docs.preloop.ai">Docs</a></li>
              ${isSaaS()
                ? html` <li><a href="/pricing">Pricing</a></li>
                    <li><a href="/about">About</a></li>`
                : ''}
            </ul>
          </nav>
        </div>
        <div class="divider"></div>
        <div class="footer-bottom">
          ${hasCompanyInfo
            ? html`
                <span class="copyright-text">
                  &copy; ${new Date().getFullYear()}
                  ${company.legal_name
                    ? html`<a href="/">${company.legal_name}</a>`
                    : config.name}.
                  All rights reserved.
                </span>
              `
            : html`
                <span class="copyright-text">
                  &copy; ${new Date().getFullYear()} ${config.name}
                </span>
              `}
          <div class="social-links">
            <sl-icon-button
              name="github"
              label="GitHub"
              href="https://github.com/preloop/preloop"
              target="_blank"
            ></sl-icon-button>
            ${social?.linkedin
              ? html`
                  <sl-icon-button
                    name="linkedin"
                    label="LinkedIn"
                    href="${social.linkedin}"
                    target="_blank"
                  ></sl-icon-button>
                `
              : ''}
            ${social?.instagram
              ? html`
                  <sl-icon-button
                    name="instagram"
                    label="Instagram"
                    href="${social.instagram}"
                    target="_blank"
                  ></sl-icon-button>
                `
              : ''}
            ${social?.twitter
              ? html`
                  <sl-icon-button
                    name="twitter-x"
                    label="Twitter/X"
                    href="https://twitter.com/${social.twitter.replace(
                      '@',
                      ''
                    )}"
                    target="_blank"
                  ></sl-icon-button>
                `
              : ''}
          </div>
        </div>
      </div>
    `;
  }
}
