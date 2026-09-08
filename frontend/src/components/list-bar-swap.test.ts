import { expect, fixture, html } from '@open-wc/testing';

import './list-bar-swap.ts';
import type { ListBarSwap } from './list-bar-swap';

describe('list-bar-swap', () => {
  async function render(selecting = false): Promise<ListBarSwap> {
    const element = await fixture<ListBarSwap>(html`
      <list-bar-swap ?selecting=${selecting} style="width: 600px;">
        <div class="base-row" style="height: 48px;">
          <input class="search" />
        </div>
        <div slot="bulk" class="bulk-row" style="height: 32px;">
          <button class="action">Pause</button>
        </div>
      </list-bar-swap>
    `);
    await element.updateComplete;
    return element;
  }

  const layer = (element: ListBarSwap, name: 'base' | 'bulk') =>
    element.shadowRoot!.querySelector<HTMLElement>(`.layer.${name}`)!;

  it('keeps one height whether or not anything is selected', async () => {
    const element = await render(false);
    const idle = element.getBoundingClientRect().height;

    element.selecting = true;
    await element.updateComplete;
    const selecting = element.getBoundingClientRect().height;

    element.selecting = false;
    await element.updateComplete;
    const cleared = element.getBoundingClientRect().height;

    expect(selecting, 'height while selecting').to.equal(idle);
    expect(cleared, 'height after clearing').to.equal(idle);
    // The taller occupant sets the height: the row is the base row's 48px,
    // not the sum of the two.
    expect(idle).to.equal(48);
  });

  it('hides the idle layer without unmounting it, so its state survives', async () => {
    const element = await render(false);
    const search = element.querySelector<HTMLInputElement>('input.search')!;
    search.value = 'payments';

    element.selecting = true;
    await element.updateComplete;
    expect(getComputedStyle(search).visibility).to.equal('hidden');
    // Still measured, which is what holds the row's height.
    expect(search.getBoundingClientRect().width).to.be.greaterThan(0);

    element.selecting = false;
    await element.updateComplete;
    expect(getComputedStyle(search).visibility).to.equal('visible');
    expect(search.value, 'search text survived the swap').to.equal('payments');
  });

  it('makes the hidden layer inert so nothing behind the bar takes focus', async () => {
    const element = await render(false);
    expect(layer(element, 'base').hasAttribute('inert')).to.equal(false);
    expect(layer(element, 'bulk').hasAttribute('inert')).to.equal(true);

    element.selecting = true;
    await element.updateComplete;
    expect(layer(element, 'base').hasAttribute('inert')).to.equal(true);
    expect(layer(element, 'bulk').hasAttribute('inert')).to.equal(false);
  });

  it('stacks both layers in the same place', async () => {
    const element = await render(true);
    const base = layer(element, 'base').getBoundingClientRect();
    const bulk = layer(element, 'bulk').getBoundingClientRect();
    expect(Math.round(bulk.left)).to.equal(Math.round(base.left));
    expect(Math.round(bulk.width)).to.equal(Math.round(base.width));
  });

  it('swaps with opacity only, never with height', async () => {
    const element = await render(false);
    const transition = getComputedStyle(layer(element, 'bulk')).transition;
    expect(transition).to.contain('opacity');
    expect(transition).to.not.contain('height');
    // Motion budget: a short state change, not an animation.
    const duration = Number(
      (/opacity (\d*\.?\d+)s/.exec(transition) || [])[1] ?? '1'
    );
    expect(duration).to.be.at.most(0.25);
  });
});
