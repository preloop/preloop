import { expect, fixture, html, nextFrame } from '@open-wc/testing';
import { AgentsView } from './agents-view';
import { DashboardView } from './dashboard-control-plane-view';
import shoelaceLight from '@shoelace-style/shoelace/dist/themes/light.css?inline';
import shoelaceDark from '@shoelace-style/shoelace/dist/themes/dark.css?inline';
import consoleSurfaces from '../../styles/console-surfaces.css?inline';

/** Resolve the actual view styles without starting their API polling. */
describe('console appearance details', () => {
  const sheets: HTMLStyleElement[] = [];
  before(() => {
    const viewStyles = [AgentsView.styles, DashboardView.styles]
      .flat(Infinity)
      .map((style) => style.cssText)
      .join('\n');
    for (const css of [
      shoelaceLight,
      shoelaceDark,
      consoleSurfaces,
      viewStyles,
    ]) {
      const sheet = document.createElement('style');
      sheet.textContent = css;
      document.head.append(sheet);
      sheets.push(sheet);
    }
  });
  after(() => sheets.forEach((sheet) => sheet.remove()));

  it('paints the welcome badge with theme ink on light and dark surfaces', async () => {
    const wrapper = await fixture<HTMLElement>(html`
      <div class="sl-theme-light" style="color: var(--console-body-color)">
        <span class="welcome-badge" aria-hidden="true"></span>
      </div>
    `);
    const badge = wrapper.querySelector('span')!;
    for (const theme of ['light', 'dark', 'light']) {
      wrapper.className = `sl-theme-${theme}`;
      await nextFrame();
      const style = getComputedStyle(badge);
      expect(style.backgroundColor).to.equal(getComputedStyle(wrapper).color);
      expect(style.maskImage).to.contain('/assets/preloop-badge.svg');
      expect(style.width).to.equal('56px');
      expect(style.height).to.equal('56px');
    }
  });

  it('changes flow artwork with the selected theme regardless of the OS default', async () => {
    const wrapper = await fixture<HTMLElement>(html`
      <div class="sl-theme-light"><img class="flow-icon" alt="Flow" /></div>
    `);
    const image = wrapper.querySelector('img')!;
    for (const theme of ['light', 'dark', 'light']) {
      wrapper.className = `sl-theme-${theme}`;
      await nextFrame();
      expect(getComputedStyle(image).filter).to.equal(
        theme === 'dark' ? 'invert(0.8) hue-rotate(180deg)' : 'none'
      );
    }
  });
});
