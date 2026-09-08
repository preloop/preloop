import { html, fixture, expect } from '@open-wc/testing';

import './resource-actions';
import type { ResourceActions } from './resource-actions';
import { PHONE_WIDTH, renderInPhoneFrame } from '../test-helpers/phone-frame';

const ACTIONS = [
  { id: 'talk', label: 'Talk', icon: 'chat-dots' },
  { id: 'rename', label: 'Rename', icon: 'pencil' },
  { id: 'tags', label: 'Edit tags', icon: 'tags' },
  {
    id: 'remove',
    label: 'Remove',
    icon: 'trash',
    variant: 'danger' as const,
    outline: true,
    separated: true,
  },
];

describe('ResourceActions', () => {
  it('left-aligns the menu inside a right-aligned host cell', async () => {
    // Table action cells set `text-align: right`; the menu used to inherit it
    // through the slot, parking every label far away from its icon.
    const host = await fixture<HTMLElement>(html`
      <div style="text-align: right; width: 400px;">
        <resource-actions menu-only .actions=${ACTIONS}></resource-actions>
      </div>
    `);
    const el = host.querySelector('resource-actions') as ResourceActions;
    await el.updateComplete;

    const menu = el.shadowRoot?.querySelector('sl-menu') as HTMLElement;
    expect(menu, 'overflow menu').to.exist;
    expect(getComputedStyle(menu).textAlign).to.equal('left');
    const item = el.shadowRoot?.querySelector('sl-menu-item') as HTMLElement;
    expect(getComputedStyle(item).textAlign).to.equal('left');
  });

  it('keeps the separated destructive action out of the row on a phone', async () => {
    const { element, cleanup } = await renderInPhoneFrame<ResourceActions>({
      moduleUrl: new URL('./resource-actions.ts', import.meta.url).href,
      tagName: 'resource-actions',
      markup: `<div style="padding: 0 16px;"><resource-actions></resource-actions></div>`,
    });

    try {
      element.actions = ACTIONS;
      await element.updateComplete;
      await new Promise((resolve) => setTimeout(resolve, 50));
      await element.updateComplete;

      const buttons = Array.from(
        element.shadowRoot?.querySelectorAll('sl-button') ?? []
      ) as HTMLElement[];
      // Every rendered button, including the overflow trigger, is inside the
      // 390px viewport: nothing is clipped by the container's `overflow:
      // hidden` (DESIGN.md, "the button's bounding box lies inside its cell").
      expect(buttons.length).to.be.greaterThan(0);
      for (const button of buttons) {
        const box = button.getBoundingClientRect();
        expect(box.left, `${button.textContent?.trim()} left`).to.be.at.least(
          0
        );
        expect(box.right, `${button.textContent?.trim()} right`).to.be.at.most(
          PHONE_WIDTH
        );
      }

      // Remove moved into the overflow menu rather than off the edge.
      const rowLabels = buttons.map((b) => b.textContent?.trim());
      expect(rowLabels).to.not.include('Remove');
      const menuLabels = Array.from(
        element.shadowRoot?.querySelectorAll('sl-menu-item') ?? []
      ).map((item) => item.textContent?.trim());
      expect(menuLabels).to.include('Remove');
      expect(
        element.shadowRoot?.querySelector('sl-menu-item.danger-item'),
        'Remove keeps its danger styling in the menu'
      ).to.exist;
    } finally {
      cleanup();
    }
  });

  const talkRenderActions = () => [
    {
      id: 'talk',
      label: 'Talk',
      render: () => html`<button class="talk-button">Talk</button>`,
    },
    ...ACTIONS.slice(1),
    { id: 'pause', label: 'Pause', icon: 'pause' },
  ];

  async function renderTalkActionsAtWidth(widthPx: number) {
    const host = await fixture<HTMLElement>(html`
      <div style=${`width: ${widthPx}px;`}>
        <resource-actions></resource-actions>
      </div>
    `);
    const el = host.querySelector('resource-actions') as ResourceActions;
    el.actions = talkRenderActions();
    await el.updateComplete;
    await new Promise((resolve) => setTimeout(resolve, 50));
    await el.updateComplete;
    return el;
  }

  it('keeps an action that brings its own element out of the overflow menu', async () => {
    // The agent page passes Talk as a `render` action, and it is declared
    // first, which is the first thing the width heuristic folds. A menu item
    // built from a `render` action has neither href nor onClick, so folding it
    // left a row that did nothing at all. Talk stays on the row instead.
    const el = await renderTalkActionsAtWidth(420);

    const menuLabels = Array.from(
      el.shadowRoot?.querySelectorAll('sl-menu-item') ?? []
    ).map((item) => item.textContent?.trim());
    expect(menuLabels, 'something folded, so this is the case under test').to
      .not.be.empty;
    expect(menuLabels, 'no dead Talk row in the menu').to.not.include('Talk');
    expect(
      el.shadowRoot?.querySelector('.talk-button'),
      'Talk keeps its place on the row'
    ).to.exist;
  });

  it('does not clip a self-rendered action at an intermediate width', async () => {
    // Restoring Talk after the fold, without reserving its width, overflowed
    // `.actions-container` (overflow: hidden; flex-end) around 500-700px and
    // clipped Talk — the leftmost item, and the button this keeps clickable.
    const el = await renderTalkActionsAtWidth(560);

    expect(
      el.shadowRoot?.querySelector('.talk-button'),
      'Talk keeps its place on the row'
    ).to.exist;
    const menuLabels = Array.from(
      el.shadowRoot?.querySelectorAll('sl-menu-item') ?? []
    ).map((item) => item.textContent?.trim());
    expect(menuLabels, 'no dead Talk row in the menu').to.not.include('Talk');

    const container = el.shadowRoot?.querySelector(
      '.actions-container'
    ) as HTMLElement;
    expect(container, 'actions row').to.exist;
    expect(
      container.scrollWidth,
      'row is not clipped by overflow:hidden'
    ).to.be.at.most(container.clientWidth);
  });

  it('keeps every action on the row when there is room', async () => {
    const { element, cleanup } = await renderInPhoneFrame<ResourceActions>({
      moduleUrl: new URL('./resource-actions.ts', import.meta.url).href,
      tagName: 'resource-actions',
      width: 1200,
      markup: `<resource-actions></resource-actions>`,
    });

    try {
      element.actions = ACTIONS;
      await element.updateComplete;
      await new Promise((resolve) => setTimeout(resolve, 50));
      await element.updateComplete;

      const labels = Array.from(
        element.shadowRoot?.querySelectorAll('sl-button') ?? []
      ).map((button) => button.textContent?.trim());
      expect(labels).to.include('Remove');
      expect(element.shadowRoot?.querySelector('sl-menu-item')).to.not.exist;
    } finally {
      cleanup();
    }
  });
});
