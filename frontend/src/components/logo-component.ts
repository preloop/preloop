import { LitElement, html, css } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import { Theme } from '../theme';
import { getBrandConfig } from '../brand-config';

@customElement('logo-component')
export class LogoComponent extends LitElement {
  @property({ type: String })
  alt = getBrandConfig().name;

  @property({ type: String, attribute: 'override-theme' })
  themeOverride?: Theme;

  @state()
  private theme: 'light' | 'dark' = 'dark';

  static styles = css`
    :host {
      display: block;
      height: 35px;
    }

    img {
      height: 100%;
    }
  `;

  connectedCallback() {
    super.connectedCallback();
    if (this.themeOverride) {
      this.setTheme(this.themeOverride);
    } else {
      this.setThemeFromClass();
      window.addEventListener('theme-change', this.handleThemeChange);
    }
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    window.removeEventListener('theme-change', this.handleThemeChange);
  }

  private handleThemeChange = (event: Event) => {
    const detail = (event as CustomEvent<{ theme: Theme }>).detail;
    this.setTheme(detail.theme);
  };

  private setTheme(theme: Theme) {
    if (theme === 'system') {
      const prefersDark = window.matchMedia(
        '(prefers-color-scheme: dark)'
      ).matches;
      this.theme = prefersDark ? 'dark' : 'light';
    } else if (theme === 'dark' || theme === 'light') {
      this.theme = theme;
    }
  }

  private setThemeFromClass() {
    if (document.documentElement.classList.contains('sl-theme-dark')) {
      this.theme = 'dark';
    } else if (document.documentElement.classList.contains('sl-theme-light')) {
      this.theme = 'light';
    } else {
      // Fallback to system preference if no class is set
      this.setTheme('system');
    }
  }

  render() {
    const brandConfig = getBrandConfig();
    const logoSrc =
      this.theme === 'dark'
        ? brandConfig.branding.logo_dark
        : brandConfig.branding.logo_light;
    return html`<img src="${logoSrc}" alt=${this.alt} />`;
  }
}
