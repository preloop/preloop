import { expect, fixture, html, nextFrame } from '@open-wc/testing';
import './logo-component';
import type { LogoComponent } from './logo-component';

describe('logo theme', () => {
  let classes: string;
  let brand: unknown;
  beforeEach(() => {
    classes = document.documentElement.className;
    brand = (window as any).BRAND_CONFIG;
    (window as any).BRAND_CONFIG = {
      name: 'Synthetic',
      branding: { logo_light: '/light.svg', logo_dark: '/dark.svg' },
    };
    document.documentElement.className = 'sl-theme-light';
  });
  afterEach(() => {
    document.documentElement.className = classes;
    (window as any).BRAND_CONFIG = brand;
  });

  it('follows resolved system theme changes without a settings event', async () => {
    const logo = await fixture<LogoComponent>(
      html`<logo-component></logo-component>`
    );
    for (const theme of ['light', 'dark', 'light']) {
      document.documentElement.className = `sl-theme-${theme}`;
      await nextFrame();
      await logo.updateComplete;
      expect(
        logo.shadowRoot!.querySelector('img')!.getAttribute('src')
      ).to.equal(`/${theme}.svg`);
    }
  });

  it('keeps an explicit theme override when the application theme changes', async () => {
    const logo = await fixture<LogoComponent>(
      html`<logo-component override-theme="dark"></logo-component>`
    );
    document.documentElement.className = 'sl-theme-light';
    window.dispatchEvent(
      new CustomEvent('theme-change', { detail: { theme: 'light' } })
    );
    await nextFrame();
    await logo.updateComplete;
    expect(logo.shadowRoot!.querySelector('img')!.getAttribute('src')).to.equal(
      '/dark.svg'
    );
  });
});
