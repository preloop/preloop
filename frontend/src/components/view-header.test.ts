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

  it('renders no description element when description is not set', async () => {
    const el = (await fixture(
      html`<view-header headerText="Tools"></view-header>`
    )) as ViewHeader;

    expect(el.shadowRoot?.querySelector('.description')).to.equal(null);
  });

  it('supports compact metadata beside the page title', async () => {
    const el = (await fixture(html`
      <view-header headerText="Overview">
        <span slot="title-suffix">Last updated just now</span>
      </view-header>
    `)) as ViewHeader;

    const suffixSlot = el.shadowRoot?.querySelector<HTMLSlotElement>(
      'slot[name="title-suffix"]'
    );
    expect(suffixSlot).to.exist;
    expect(suffixSlot?.assignedElements()[0]?.textContent).to.contain(
      'Last updated just now'
    );
  });
});
