import { expect, fixture, html, oneEvent } from '@open-wc/testing';
import '@shoelace-style/shoelace/dist/components/select/select.js';

import './list-toolbar.ts';
import type { ListToolbar } from './list-toolbar';

describe('list-toolbar', () => {
  async function render(
    props: Partial<ListToolbar> = {}
  ): Promise<ListToolbar> {
    const element = await fixture<ListToolbar>(html`
      <list-toolbar
        search=${props.search ?? ''}
        searchPlaceholder=${props.searchPlaceholder ?? 'Search'}
        view=${props.view ?? 'list'}
        .views=${props.views ?? ['list', 'cards']}
      >
        <sl-select placeholder="All kinds"></sl-select>
        <span slot="count">2 trackers</span>
      </list-toolbar>
    `);
    await element.updateComplete;
    return element;
  }

  it('renders the search input with the search prefix icon', async () => {
    const element = await render({ searchPlaceholder: 'Search trackers' });
    const input = element.shadowRoot!.querySelector('sl-input.search-input')!;
    expect(input).to.exist;
    expect(input.getAttribute('placeholder')).to.equal('Search trackers');
    const icon = input.querySelector('sl-icon[name="search"]');
    expect(icon).to.exist;
  });

  it('projects page-specific filters into the default slot', async () => {
    const element = await render();
    const slotted = element.querySelector('sl-select');
    expect(slotted).to.exist;
    expect(slotted?.getAttribute('placeholder')).to.equal('All kinds');
  });

  it('renders list and cards toggle buttons with the Flows icons', async () => {
    const element = await render();
    const buttons = [
      ...element.shadowRoot!.querySelectorAll('sl-button[data-view]'),
    ];
    expect(
      buttons.map((button) => button.getAttribute('data-view'))
    ).to.deep.equal(['list', 'cards']);
    expect(buttons[0].querySelector('sl-icon')?.getAttribute('name')).to.equal(
      'list-ul'
    );
    expect(buttons[1].querySelector('sl-icon')?.getAttribute('name')).to.equal(
      'grid-3x3-gap'
    );
    expect(buttons[0].getAttribute('variant')).to.equal('primary');
    expect(buttons[1].getAttribute('variant')).to.equal('default');
  });

  it('emits search-change as the input value changes', async () => {
    const element = await render();
    const input = element.shadowRoot!.querySelector('sl-input.search-input')!;
    (input as unknown as { value: string }).value = 'github';

    setTimeout(() =>
      input.dispatchEvent(new CustomEvent('sl-input', { bubbles: true }))
    );
    const event = (await oneEvent(element, 'search-change')) as CustomEvent<{
      value: string;
    }>;

    expect(event.detail.value).to.equal('github');
    expect(element.search).to.equal('github');
  });

  it('emits search-change with an empty string on clear', async () => {
    const element = await render({ search: 'jira' });
    const input = element.shadowRoot!.querySelector('sl-input.search-input')!;

    setTimeout(() =>
      input.dispatchEvent(new CustomEvent('sl-clear', { bubbles: true }))
    );
    const event = (await oneEvent(element, 'search-change')) as CustomEvent<{
      value: string;
    }>;

    expect(event.detail.value).to.equal('');
    expect(element.search).to.equal('');
  });

  it('emits view-change when the other view is pressed', async () => {
    const element = await render({ view: 'list' });
    const cards = element.shadowRoot!.querySelector(
      'sl-button[data-view="cards"]'
    ) as HTMLElement;

    setTimeout(() => cards.click());
    const event = (await oneEvent(element, 'view-change')) as CustomEvent<{
      value: string;
    }>;

    expect(event.detail.value).to.equal('cards');
    expect(element.view).to.equal('cards');
  });

  it('does not emit view-change when the active view is pressed again', async () => {
    const element = await render({ view: 'list' });
    let emitted = 0;
    element.addEventListener('view-change', () => {
      emitted += 1;
    });
    const list = element.shadowRoot!.querySelector(
      'sl-button[data-view="list"]'
    ) as HTMLElement;
    list.click();
    await element.updateComplete;
    expect(emitted).to.equal(0);
  });

  it('hides the toggle when only one view is offered', async () => {
    const element = await render({ views: ['list'] });
    expect(element.shadowRoot!.querySelector('sl-button-group')).to.equal(null);
    expect(element.shadowRoot!.querySelector('.toolbar-divider')).to.equal(
      null
    );
    expect(element.shadowRoot!.querySelector('sl-input.search-input')).to.exist;
  });

  it('renders the count slot next to the switcher', async () => {
    const element = await render();
    const count = element.querySelector('[slot="count"]');
    expect(count?.textContent?.trim()).to.equal('2 trackers');
  });
});
