import { expect, fixture, html, oneEvent } from '@open-wc/testing';
import './time-range-select.ts';
import type { TimeRangeSelect } from './time-range-select';

const OPTIONS = [
  { value: 'day', label: '24h' },
  { value: 'week', label: '7d' },
  { value: 'month', label: '30d' },
];

describe('time-range-select', () => {
  async function render(value = 'month'): Promise<TimeRangeSelect> {
    const element = await fixture<TimeRangeSelect>(html`
      <time-range-select
        ariaLabel="Usage time range"
        .value=${value}
        .options=${OPTIONS}
      ></time-range-select>
    `);
    await element.updateComplete;
    return element;
  }

  it('renders one option per configured range', async () => {
    const element = await render();
    const options = element.shadowRoot!.querySelectorAll('sl-option');
    expect(options.length).to.equal(3);
    expect(
      Array.from(options).map((option) => option.textContent?.trim())
    ).to.deep.equal(['24h', '7d', '30d']);
  });

  it('labels the control for assistive technology', async () => {
    const element = await render();
    const select = element.shadowRoot!.querySelector('sl-select')!;
    expect(select.getAttribute('aria-label')).to.equal('Usage time range');
  });

  it('reflects the current value on the select', async () => {
    const element = await render('week');
    const select = element.shadowRoot!.querySelector(
      'sl-select'
    )! as unknown as {
      value: string;
    };
    expect(select.value).to.equal('week');
  });

  it('emits range-change with the new value', async () => {
    const element = await render('month');
    const select = element.shadowRoot!.querySelector('sl-select')!;
    (select as unknown as { value: string }).value = 'day';

    setTimeout(() =>
      select.dispatchEvent(new CustomEvent('sl-change', { bubbles: true }))
    );
    const event = (await oneEvent(element, 'range-change')) as CustomEvent<{
      value: string;
    }>;

    expect(event.detail.value).to.equal('day');
    expect(element.value).to.equal('day');
  });

  it('does not emit when the value is unchanged', async () => {
    const element = await render('month');
    let emitted = 0;
    element.addEventListener('range-change', () => {
      emitted += 1;
    });
    const select = element.shadowRoot!.querySelector('sl-select')!;
    (select as unknown as { value: string }).value = 'month';
    select.dispatchEvent(new CustomEvent('sl-change', { bubbles: true }));
    await element.updateComplete;

    expect(emitted).to.equal(0);
  });
});
