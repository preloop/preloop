import { expect, fixture, html } from '@open-wc/testing';

import './not-found-view';
import type { NotFoundView } from './not-found-view';

describe('NotFoundView', () => {
  it('explains the miss and links back to the console', async () => {
    const el = (await fixture(
      html`<not-found-view></not-found-view>`
    )) as NotFoundView;

    expect(el.shadowRoot!.textContent).to.contain('Page not found');
    expect(
      el.shadowRoot!.querySelector('sl-button')!.getAttribute('href')
    ).to.equal('/console');
  });
});
