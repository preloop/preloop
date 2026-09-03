import { expect, fixture, html } from '@open-wc/testing';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import shoelaceLight from '@shoelace-style/shoelace/dist/themes/light.css?inline';
import shoelaceDark from '@shoelace-style/shoelace/dist/themes/dark.css?inline';
import consoleSurfaces from './console-surfaces.css?inline';
import consoleStyles from './console-styles.css?inline';

/**
 * Wave 4: a state is a tint, not a paint. Solid pills were the loudest part
 * of the dark console, so every state chip is now 16% of one tone over the
 * surface it sits on, with no border. The two survivors (a section header
 * count, the danger pill on a failed run) opt back in with `.solid`.
 *
 * The assertions are computed, not textual: the recipe only works because
 * Shoelace's dark theme inverts the colour scales, and only a browser can
 * settle whether the result is still readable.
 */
describe('soft state chips', () => {
  let sheets: HTMLStyleElement[] = [];

  before(() => {
    for (const css of [
      shoelaceLight,
      shoelaceDark,
      consoleSurfaces,
      consoleStyles,
    ]) {
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

  /** Parses `rgb(...)`, `rgba(...)` and `color(srgb r g b / a)` alike. */
  function parse(value: string): [number, number, number, number] {
    const parts = value.match(/[\d.]+/g)!.map(Number);
    if (value.startsWith('color(')) {
      const [r, g, b, a = 1] = parts;
      return [r * 255, g * 255, b * 255, a];
    }
    const [r, g, b, a = 1] = parts;
    return [r, g, b, a];
  }

  function over(fg: string, bg: string): [number, number, number] {
    const [fr, fg_, fb, fa] = parse(fg);
    const [br, bg_, bb] = parse(bg);
    return [
      fr * fa + br * (1 - fa),
      fg_ * fa + bg_ * (1 - fa),
      fb * fa + bb * (1 - fa),
    ];
  }

  function relativeLuminance([r, g, b]: number[]) {
    const channel = (v: number) => {
      const c = v / 255;
      return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
    };
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
  }

  function contrast(a: number[], b: number[]) {
    const [hi, lo] = [relativeLuminance(a), relativeLuminance(b)].sort(
      (x, y) => y - x
    );
    return (hi + 0.05) / (lo + 0.05);
  }

  /** A chip on a card, in one theme. Returns the chip's `base` part. */
  async function mountChip(
    theme: 'light' | 'dark',
    variant: string,
    className = 'chip'
  ) {
    const wrapper = (await fixture(html`
      <div
        class="sl-theme-${theme}"
        style="background-color: var(--console-surface)"
      >
        <sl-badge class=${className} variant=${variant} pill>Active</sl-badge>
      </div>
    `)) as HTMLElement;
    const badge = wrapper.querySelector('sl-badge') as HTMLElement;
    await (badge as any).updateComplete;
    const base = badge.shadowRoot!.querySelector('[part~="base"]');
    return {
      surface: getComputedStyle(wrapper).backgroundColor,
      style: getComputedStyle(base as Element),
    };
  }

  for (const theme of ['light', 'dark'] as const) {
    for (const variant of [
      'neutral',
      'primary',
      'success',
      'warning',
      'danger',
    ]) {
      it(`tints a ${variant} chip instead of filling it (${theme})`, async () => {
        const { surface, style } = await mountChip(theme, variant);

        // A tint: translucent, so it reads as the same surface with a
        // colour cast rather than as a fourth box.
        const [, , , alpha] = parse(style.backgroundColor);
        expect(alpha, 'chip fill is opaque').to.be.lessThan(1);
        expect(alpha).to.be.closeTo(0.16, 0.01);
        expect(style.borderTopWidth).to.equal('0px');
        expect(style.fontSize).to.equal('12px');
        expect(style.fontWeight).to.equal('500');

        // And still readable where it lands: the ink is measured against
        // the tint composited over the card, not against the token.
        const painted = over(style.backgroundColor, surface);
        const ink = parse(style.color).slice(0, 3);
        expect(
          contrast(ink, painted),
          `${variant} chip text on its own tint (${theme})`
        ).to.be.greaterThan(4.5);
      });
    }
  }

  it('keeps a solid pill where one is still wanted', async () => {
    const { style } = await mountChip('dark', 'neutral', 'chip solid');

    // Section counts and the danger pill on a failed run opt out by name.
    const [, , , alpha] = parse(style.backgroundColor);
    expect(alpha).to.equal(1);
  });

  it('strips the box off tag chips entirely', async () => {
    const { style } = await mountChip('dark', 'neutral', 'tag-chip');

    const [, , , alpha] = parse(style.backgroundColor);
    expect(alpha, 'tag chips are text, not pills').to.equal(0);
    expect(style.borderTopWidth).to.equal('0px');
  });
});
