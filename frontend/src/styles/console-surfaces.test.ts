import { expect, fixture, html } from '@open-wc/testing';
import shoelaceLight from '@shoelace-style/shoelace/dist/themes/light.css?inline';
import shoelaceDark from '@shoelace-style/shoelace/dist/themes/dark.css?inline';
import consoleSurfaces from './console-surfaces.css?inline';

/**
 * The surface ladder is the whole of wave 4's answer to "gray boxes within
 * gray boxes": one page rung, one surface rung, and a raised rung reserved
 * for things that float. Shoelace's dark theme inverts the neutral scale, so
 * the only way to be sure the ladder climbs the same direction in both themes
 * is to resolve it in a browser and compare luminance.
 */
describe('console surface ladder', () => {
  let sheets: HTMLStyleElement[] = [];

  before(() => {
    for (const css of [shoelaceLight, shoelaceDark, consoleSurfaces]) {
      const style = document.createElement('style');
      style.textContent = css;
      document.head.appendChild(style);
      sheets.push(style);
    }
  });

  after(() => {
    for (const style of sheets) style.remove();
    sheets = [];
  });

  /** Resolve a ladder token to an `rgb()` string by painting with it. */
  async function resolve(theme: 'light' | 'dark', token: string) {
    const wrapper = (await fixture(html`
      <div class="sl-theme-${theme}">
        <div style="background-color: var(${token})"></div>
      </div>
    `)) as HTMLElement;
    const probe = wrapper.firstElementChild as HTMLElement;
    return getComputedStyle(probe).backgroundColor;
  }

  /** Perceptual-ish lightness, enough to tell one rung from the next. */
  function luminance(rgb: string) {
    const [r, g, b] = rgb.match(/[\d.]+/g)!.map(Number);
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  }

  it('puts every surface one step above the page in light', async () => {
    const page = await resolve('light', '--console-page');
    const surface = await resolve('light', '--console-surface');
    const raised = await resolve('light', '--console-surface-raised');

    expect(page).to.equal('rgb(249, 249, 249)');
    expect(surface).to.equal('rgb(255, 255, 255)');
    // Light has nowhere lighter to go, so the raised rung is the card colour
    // plus a shadow rather than a fourth gray.
    expect(raised).to.equal(surface);
    expect(luminance(surface)).to.be.greaterThan(luminance(page));
  });

  it('puts every surface one step above the page in dark too', async () => {
    const page = await resolve('dark', '--console-page');
    const surface = await resolve('dark', '--console-surface');
    const raised = await resolve('dark', '--console-surface-raised');

    // neutral-0 is the DARKEST step in Shoelace's dark theme; a card painted
    // with it on a neutral-50 page is the inversion wave 4 removes.
    expect(page).to.equal('rgb(26, 26, 30)');
    expect(surface).to.equal('rgb(36, 36, 40)');
    expect(raised).to.equal('rgb(44, 44, 49)');
    expect(luminance(surface)).to.be.greaterThan(luminance(page));
    expect(luminance(raised)).to.be.greaterThan(luminance(surface));
  });

  it('states elevation with a border in light and with lightness in dark', async () => {
    const light = (await fixture(
      html`<div class="sl-theme-light"></div>`
    )) as HTMLElement;
    const dark = (await fixture(
      html`<div class="sl-theme-dark"></div>`
    )) as HTMLElement;

    expect(
      getComputedStyle(light).getPropertyValue('--console-card-border').trim()
    ).to.contain('1px solid');
    expect(
      getComputedStyle(light).getPropertyValue('--console-card-shadow').trim()
    ).to.not.equal('none');

    // Dark cards carry no border and no shadow: the lightness step is the
    // elevation, and a border around it reads as a box.
    expect(
      getComputedStyle(dark).getPropertyValue('--console-card-border').trim()
    ).to.equal('0 solid transparent');
    expect(
      getComputedStyle(dark).getPropertyValue('--console-card-shadow').trim()
    ).to.equal('none');
  });

  it('draws hairlines from the ink colour of the theme', async () => {
    const hairlineLight = await resolve('light', '--console-hairline');
    const hairlineDark = await resolve('dark', '--console-hairline');

    expect(hairlineLight).to.equal('rgb(228, 228, 231)');
    // 10% of white: a separator that works on both the card and the page
    // without naming a gray step that inverts between themes.
    expect(hairlineDark).to.equal('color(srgb 1 1 1 / 0.1)');
  });

  it('lifts meta text off the card in dark', async () => {
    const lightMeta = await resolve('light', '--console-meta-color');
    const darkMeta = await resolve('dark', '--console-meta-color');
    const darkSurface = await resolve('dark', '--console-surface');

    // neutral-500 on a dark card is gray on gray; neutral-600 is the step
    // that keeps a 13px timestamp scannable.
    expect(lightMeta).to.equal('rgb(113, 113, 122)');
    expect(darkMeta).to.equal('rgb(142, 142, 154)');
    expect(luminance(darkMeta)).to.be.greaterThan(luminance(darkSurface) * 2);
  });
});
