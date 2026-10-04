import { html, fixture, expect } from '@open-wc/testing';

import './view-header';
import type { ViewHeader } from './view-header';
import { PHONE_WIDTH, renderInPhoneFrame } from '../test-helpers/phone-frame';

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

  // The test runner does not load the Shoelace theme, so its spacing tokens
  // are undefined and every gap collapses to zero. Define the ones the
  // header uses with the theme's values for the spacing tests.
  const SPACING_TOKENS: Record<string, string> = {
    '--sl-spacing-2x-small': '0.25rem',
    '--sl-spacing-x-small': '0.5rem',
    '--sl-spacing-small': '0.75rem',
    '--sl-spacing-medium': '1rem',
    '--sl-spacing-large': '1.25rem',
  };
  const withThemeSpacing = () => {
    for (const [name, value] of Object.entries(SPACING_TOKENS)) {
      document.documentElement.style.setProperty(name, value);
    }
  };
  afterEach(() => {
    for (const name of Object.keys(SPACING_TOKENS)) {
      document.documentElement.style.removeProperty(name);
    }
  });

  it('keeps the description right under the title, not a section away', async () => {
    withThemeSpacing();
    const el = (await fixture(
      html`<view-header
        headerText="Cost"
        description="Understand gateway spend by agent, tool and session."
      ></view-header>`
    )) as ViewHeader;

    const header = el.shadowRoot?.querySelector('.header') as HTMLElement;
    const description = el.shadowRoot?.querySelector(
      '.description'
    ) as HTMLElement;
    const gap =
      description.getBoundingClientRect().top -
      header.getBoundingClientRect().bottom;
    // The shared .header margin plus the column gap used to put ~44px
    // between a title and the line that explains it.
    expect(gap).to.be.at.most(12);
  });

  it('keeps room between a top-slot back link and the title', async () => {
    withThemeSpacing();
    const el = (await fixture(
      html`<view-header headerText="Flow">
        <a slot="top" href="/console/flows">Back to Flows</a>
      </view-header>`
    )) as ViewHeader;

    const link = el.querySelector('a') as HTMLElement;
    const header = el.shadowRoot?.querySelector('.header') as HTMLElement;
    const gap =
      header.getBoundingClientRect().top - link.getBoundingClientRect().bottom;
    expect(gap).to.be.at.least(8);
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

  it('wraps the actions onto their own row on a phone', async () => {
    // Agents slots two buttons in one nowrap flex row; at 390px they used to
    // run off the right edge with only the "+" of the primary visible.
    const { element, frameWindow, cleanup } =
      await renderInPhoneFrame<ViewHeader>({
        moduleUrl: new URL('./view-header.ts', import.meta.url).href,
        tagName: 'view-header',
        markup: `
        <view-header headerText="Agents">
          <div
            slot="main-column"
            style="display: flex; gap: 12px; align-items: center;"
          >
            <button style="width: 200px">Deploy new agent</button>
            <button id="primary" style="width: 220px">
              Onboard existing agent
            </button>
          </div>
        </view-header>
      `,
      });

    try {
      const header = element.shadowRoot?.querySelector(
        '.header'
      ) as HTMLElement;
      expect(frameWindow.getComputedStyle(header).flexWrap).to.equal('wrap');

      const actions = element.querySelector(
        '[slot="main-column"]'
      ) as HTMLElement;
      // The actions row owns the full phone width, so it can wrap inside.
      expect(Math.round(actions.getBoundingClientRect().width)).to.equal(
        PHONE_WIDTH
      );
      expect(frameWindow.getComputedStyle(actions).flexWrap).to.equal('wrap');

      const primary = element.querySelector('#primary') as HTMLElement;
      const box = primary.getBoundingClientRect();
      expect(box.left).to.be.at.least(0);
      expect(box.right).to.be.at.most(PHONE_WIDTH);
    } finally {
      cleanup();
    }
  });

  it('keeps the actions beside the title on a desktop viewport', async () => {
    const { element, frameWindow, cleanup } =
      await renderInPhoneFrame<ViewHeader>({
        moduleUrl: new URL('./view-header.ts', import.meta.url).href,
        tagName: 'view-header',
        width: 1024,
        markup: `
        <view-header headerText="Agents">
          <div slot="main-column" style="display: flex; gap: 12px;">
            <button style="width: 200px">Deploy new agent</button>
          </div>
        </view-header>
      `,
      });

    try {
      const header = element.shadowRoot?.querySelector(
        '.header'
      ) as HTMLElement;
      expect(frameWindow.getComputedStyle(header).flexWrap).to.equal('nowrap');
      const actions = element.querySelector(
        '[slot="main-column"]'
      ) as HTMLElement;
      expect(actions.getBoundingClientRect().width).to.be.below(1024);
    } finally {
      cleanup();
    }
  });

  it('renders no description element when description is not set', async () => {
    const el = (await fixture(
      html`<view-header headerText="Tools"></view-header>`
    )) as ViewHeader;

    expect(el.shadowRoot?.querySelector('.description')).to.equal(null);
  });

  describe('document title', () => {
    let originalTitle: string;

    beforeEach(() => {
      originalTitle = document.title;
    });

    afterEach(() => {
      document.title = originalTitle;
      delete (window as any).BRAND_CONFIG;
    });

    it('names the tab after the page and the brand', async () => {
      // White-label brands get their own name in the tab, not Preloop's.
      (window as any).BRAND_CONFIG = { name: 'Acme Console' };
      const el = (await fixture(
        html`<view-header headerText="Approvals"></view-header>`
      )) as ViewHeader;
      expect(document.title).to.equal('Approvals · Acme Console');

      // A page that learns its own name later (a detail view) retitles.
      el.headerText = 'Agent: build-bot';
      await el.updateComplete;
      expect(document.title).to.equal('Agent: build-bot · Acme Console');
    });

    it('uses the page name alone where no brand config is loaded', async () => {
      delete (window as any).BRAND_CONFIG;
      await fixture(html`<view-header headerText="Cost"></view-header>`);
      expect(document.title).to.equal('Cost');
    });

    it('leaves the title alone while the page has no name yet', async () => {
      document.title = 'Before';
      await fixture(html`<view-header headerText=""></view-header>`);
      expect(document.title).to.equal('Before');
    });
  });
});
