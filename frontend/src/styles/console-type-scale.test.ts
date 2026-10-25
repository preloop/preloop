import { expect } from '@open-wc/testing';
import consoleStyles from './console-styles.css?inline';

/**
 * The console has six type sizes (DESIGN.md, Console > Type scale). Section
 * headings that a view leaves unstyled fall back to the browser defaults
 * (1.5em, 1.17em, 0.83em of whatever they sit in), which is how 21px,
 * 17.55px and 11.62px titles reached the settings and cost pages. The
 * shared sheet snaps them back onto the scale, computed in a real shadow
 * root, the way every console view adopts the sheet.
 */
describe('console heading scale', () => {
  let host: HTMLElement;

  beforeEach(() => {
    host = document.createElement('div');
    document.body.appendChild(host);
  });

  afterEach(() => host.remove());

  function render(markup: string): ShadowRoot {
    const root = host.attachShadow({ mode: 'open' });
    root.innerHTML = `<style>${consoleStyles}</style>${markup}`;
    return root;
  }

  const sizeOf = (el: Element | null) =>
    getComputedStyle(el as Element).fontSize;

  it('puts unstyled h2, h3 and h4 on the 15px card-title size', () => {
    const root = render(
      '<sl-card><h2 slot="header">Audit integrity</h2></sl-card>' +
        '<section><h3>Verify offline</h3><h4>Limits</h4></section>'
    );
    for (const tag of ['h2', 'h3', 'h4']) {
      expect(sizeOf(root.querySelector(tag)), tag).to.equal('15px');
      expect(getComputedStyle(root.querySelector(tag)!).fontWeight).to.equal(
        '600'
      );
    }
  });

  it('puts unstyled h5 and h6 on the 14px body size', () => {
    const root = render('<h5>Model routing rules</h5><h6>Detail</h6>');
    expect(sizeOf(root.querySelector('h5'))).to.equal('14px');
    expect(sizeOf(root.querySelector('h6'))).to.equal('14px');
  });

  it('leaves the page title at 26px', () => {
    const root = render('<div class="header"><h1>Records</h1></div>');
    expect(sizeOf(root.querySelector('h1'))).to.equal('26px');
  });

  it('lets a view rule after the sheet keep its own on-scale size', () => {
    const root = render(
      '<style>h3 { font-size: var(--console-text-meta); }</style><h3>Note</h3>'
    );
    expect(sizeOf(root.querySelector('h3'))).to.equal('13px');
  });
});
