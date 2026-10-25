import { expect, fixture, html } from '@open-wc/testing';
import './time-range-select';

describe('time-range-select size (U-15)', () => {
  const options = [
    { value: 'day', label: '24h' },
    { value: 'week', label: '7d' },
  ];

  it('stays a small pill by default, for card headers', async () => {
    const el = await fixture(
      html`<time-range-select
        .options=${options}
        value="day"
      ></time-range-select>`
    );
    const select = el.shadowRoot!.querySelector('sl-select')!;
    expect(select.getAttribute('size')).to.equal('small');
    expect(select.hasAttribute('pill')).to.be.true;
  });

  it('matches the filters beside it in a filter bar', async () => {
    const el = await fixture(
      html`<time-range-select
        size="medium"
        .options=${options}
        value="day"
      ></time-range-select>`
    );
    const select = el.shadowRoot!.querySelector('sl-select')!;
    expect(select.getAttribute('size')).to.equal('medium');
    expect(select.hasAttribute('pill')).to.be.false;
  });
});
