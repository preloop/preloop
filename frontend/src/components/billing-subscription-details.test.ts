import { expect, fixture, html } from '@open-wc/testing';
import './billing-subscription-details';

describe('billing-subscription-details built-in model usage (B-21)', () => {
  it('lays the built-in usage figures out as label and value pairs', async () => {
    const summary = {
      plan: { name: 'Pro' },
      effective_plan_id: 'pro',
      hosted_models: {
        billing_period_end: '2099-11-01T00:00:00Z',
        included_limit_usd: 20,
        active_limit_usd: 20,
        used_usd: 4.5,
        remaining_usd: 15.5,
        models: [],
      },
    } as any;
    const el = await fixture(
      html`<billing-subscription-details
        .summary=${summary}
      ></billing-subscription-details>`
    );
    const grid = el.shadowRoot!.querySelector('.usage-grid') as HTMLElement;
    expect(grid, 'usage grid').to.exist;
    expect(getComputedStyle(grid).display).to.equal('grid');
    const metrics = Array.from(
      grid.querySelectorAll('.usage-metric')
    ) as HTMLElement[];
    expect(metrics.length).to.be.at.least(3);
    // Pairs sit side by side across the card, not stacked down it.
    const tops = metrics.map((metric) => metric.getBoundingClientRect().top);
    expect(tops[0]).to.equal(tops[1]);
    const label = metrics[0].querySelector('.usage-label') as HTMLElement;
    const value = metrics[0].querySelector('.usage-value') as HTMLElement;
    expect(getComputedStyle(label).fontSize).to.equal('13px');
    expect(getComputedStyle(value).fontWeight).to.equal('600');
  });
});
