import { expect, fixture, html, nextFrame } from '@open-wc/testing';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import shoelaceLight from '@shoelace-style/shoelace/dist/themes/light.css?inline';
import shoelaceDark from '@shoelace-style/shoelace/dist/themes/dark.css?inline';
import consoleSurfaces from './console-surfaces.css?inline';
import consoleStyles from './console-styles.css?inline';

/** Slotted icons inherit through the button's shadow tree, not its host. */
describe('empty-state button themes', () => {
  const sheets: HTMLStyleElement[] = [];
  before(() => {
    for (const css of [
      shoelaceLight,
      shoelaceDark,
      consoleSurfaces,
      consoleStyles,
    ]) {
      const style = document.createElement('style');
      style.textContent = css;
      document.head.append(style);
      sheets.push(style);
    }
  });
  after(() => sheets.forEach((sheet) => sheet.remove()));

  it('keeps prefix and suffix icons on the button foreground through theme changes', async () => {
    const wrapper = await fixture<HTMLDivElement>(html`
      <div class="sl-theme-light">
        <div class="empty-state">
          <sl-icon name="diagram-3"></sl-icon>
          <sl-button variant="primary">
            <sl-icon slot="prefix" name="plus-lg"></sl-icon>
            Create item
            <sl-icon slot="suffix" name="arrow-right"></sl-icon>
          </sl-button>
        </div>
      </div>
    `);
    const button = wrapper.querySelector('sl-button')!;
    await button.updateComplete;
    for (const theme of ['light', 'dark', 'light']) {
      wrapper.className = `sl-theme-${theme}`;
      await nextFrame();
      const foreground = getComputedStyle(
        button.shadowRoot!.querySelector('[part~="base"]')!
      ).color;
      for (const icon of button.querySelectorAll('sl-icon')) {
        expect(getComputedStyle(icon).color, `${theme} ${icon.slot}`).to.equal(
          foreground
        );
        expect(getComputedStyle(icon).marginBottom).to.equal('0px');
      }
      expect(
        getComputedStyle(wrapper.querySelector('.empty-state > sl-icon')!)
          .fontSize
      ).to.equal('24px');
    }
  });
});
