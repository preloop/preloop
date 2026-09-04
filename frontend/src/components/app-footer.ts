import { LitElement, html, css } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import { router } from '../router';
import { getBrandConfig, hasBrandConfig, isSaaS } from '../brand-config';
import { getFeatures } from '../api';
import './logo-component';

@customElement('app-footer')
export class AppFooter extends LitElement {
  @state() private _registrationEnabled = true;
  /** Same `legal_disclaimer` knob as landing content. Empty falls back to brand config. */
  @property({ type: String }) legalDisclaimer = '';

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
        display: flex;
        justify-content: space-between;
        align-items: flex-start;
        flex-wrap: wrap;
        gap: 24px;
      }

      .footer-nav {
        text-align: right;
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
        margin: 30px 0;
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

      .legal-disclaimer {
        margin: 0 0 16px;
        font-size: 13px;
        line-height: 1.5;
        color: rgb(161, 161, 170);
        text-align: left;
      }

      .legal-disclaimer + .footer-bottom {
        margin-top: 16px;
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
        .footer-nav {
          margin-left: auto;
        }
      }
    `,
  ];

  private _disclaimerText(): string {
    const fromProp = (this.legalDisclaimer || '').trim();
    if (fromProp) {
      return fromProp;
    }
    if (!hasBrandConfig()) {
      return '';
    }
    return (getBrandConfig().legal_disclaimer || '').trim();
  }

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
    // Injected by the brand plugin from build-time markdown discovery. A brand
    // without the markdown file gets no link, because nginx would serve the
    // homepage for that route with a 200 and the SPA fetch would then 404.
    const regulationPages = config.regulation_pages ?? [];
    const hasAboutPage = (config.static_markdown_pages ?? []).some(
      (page) => page.path === '/about'
    );

    return html`
      <div class="footer-container">
        <div class="divider"></div>
        <div class="footer-main">
          <div>
            <logo-component override-theme="dark"></logo-component>
            ${
              hasCompanyInfo
                ? html`
                    <p style="margin-top: 1rem;">
                      ${
                        company.legal_name
                          ? html`${company.legal_name}<br />`
                          : ''
                      }
                      ${company.address ? html`${company.address}<br />` : ''}
                      ${company.city ? html`${company.city}` : ''}
                    </p>
                  `
                : ''
            }
          </div>
          <nav class="footer-nav">
            <ul>
              ${
                this._registrationEnabled
                  ? html`<li><a href="/register">Register</a></li>`
                  : ''
              }
              <li><a href="/login">Sign in</a></li>
              <li><a href="/privacy">Privacy Policy</a></li>
              <li><a href="/terms">Terms of Service</a></li>
              <li><a href="/whatis-mcp">What is MCP?</a></li>
              <li><a href="https://docs.preloop.ai">Docs</a></li>
              ${
                isSaaS()
                  ? html` <li><a href="/pricing">Pricing</a></li>
                      <li><a href="/blog">Blog</a></li>
                      ${
                        hasAboutPage
                          ? html`<li><a href="/about">About</a></li>`
                          : ''
                      }
                      ${regulationPages.map(
                        (page) =>
                          html`<li>
                            <a href="${page.href}">${page.label}</a>
                          </li>`
                      )}`
                  : ''
              }
            </ul>
          </nav>
        </div>
        <div class="divider"></div>
        ${
          this._disclaimerText()
            ? html`<p class="legal-disclaimer">${this._disclaimerText()}</p>`
            : ''
        }
        <div class="footer-bottom">
          ${
            hasCompanyInfo
              ? html`
                  <span class="copyright-text">
                    &copy; ${new Date().getFullYear()}
                    ${
                      company.legal_name
                        ? html`<a href="/">${company.legal_name}</a>`
                        : config.name
                    }.
                    All rights reserved.
                  </span>
                `
              : html`
                  <span class="copyright-text">
                    &copy; ${new Date().getFullYear()} ${config.name}
                  </span>
                `
          }
          <div class="social-links">
            <sl-icon-button
              name="github"
              label="GitHub"
              href="https://github.com/preloop/preloop"
              target="_blank"
            ></sl-icon-button>
            ${
              social?.linkedin
                ? html`
                    <sl-icon-button
                      name="linkedin"
                      label="LinkedIn"
                      href="${social.linkedin}"
                      target="_blank"
                    ></sl-icon-button>
                  `
                : ''
            }
            ${
              social?.instagram
                ? html`
                    <sl-icon-button
                      name="instagram"
                      label="Instagram"
                      href="${social.instagram}"
                      target="_blank"
                    ></sl-icon-button>
                  `
                : ''
            }
            ${
              social?.twitter
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
                : ''
            }
          </div>
        </div>
      </div>
    `;
  }
}
