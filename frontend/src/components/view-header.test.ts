import { html, fixture, expect } from '@open-wc/testing';

import './view-header';
import type { ViewHeader } from './view-header';

describe('ViewHeader', () => {
  it('renders the headerText as the page title', async () => {
    const el = (await fixture(
      html`<view-header headerText="Cost Analytics"></view-header>`
    )) as ViewHeader;

    const h1 = el.shadowRoot?.querySelector('h1');
    expect(h1).to.exist;
    expect(h1?.textContent).to.contain('Cost Analytics');
  });

  it('renders the description as a muted line under the title', async () => {
    const el = (await fixture(
      html`<view-header
        headerText="Cost Analytics"
        description="Understand gateway spend by model, agent, session, flow, and API key."
      ></view-header>`
    )) as ViewHeader;

    const description = el.shadowRoot?.querySelector('.description');
    expect(description).to.exist;
    expect(description?.textContent).to.contain(
      'Understand gateway spend by model, agent, session, flow, and API key.'
    );
  });

  it('renders the title at the console H1 scale, not the marketing one', async () => {
    const el = (await fixture(
      html`<view-header headerText="Overview"></view-header>`
    )) as ViewHeader;

    const h1 = el.shadowRoot?.querySelector('h1') as HTMLElement;
    const styles = getComputedStyle(h1);
    // 26px/600: the title introduces the page, it does not compete with the
    // numbers on it (1.75rem/700 did).
    expect(styles.fontSize).to.equal('26px');
    expect(styles.fontWeight).to.equal('600');
  });

  it('offers a meta slot beside the title for "Updated ..." lines', async () => {
    const el = (await fixture(
      html`<view-header headerText="Overview"
        ><span slot="meta">Updated just now</span></view-header
      >`
    )) as ViewHeader;

    const slot = el.shadowRoot?.querySelector(
      'slot[name="meta"]'
    ) as HTMLSlotElement;
    expect(slot, 'meta slot').to.exist;
    expect(slot.assignedElements()[0]?.textContent).to.equal(
      'Updated just now'
    );
  });

  it('renders no description element when description is not set', async () => {
    const el = (await fixture(
      html`<view-header headerText="Tools"></view-header>`
    )) as ViewHeader;

    expect(el.shadowRoot?.querySelector('.description')).to.equal(null);
  });
});
