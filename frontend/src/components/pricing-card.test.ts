import { html, fixture, expect } from '@open-wc/testing';
import './pricing-card';
import { PricingCard } from './pricing-card';

const BASE = {
  id: 'pro',
  name: 'Pro',
  price_monthly: 12,
  price_annually: 120,
  features: [] as string[],
};

async function renderCard(plan: any, interval: 'month' | 'year' = 'month') {
  const el = (await fixture(
    html`<pricing-card .plan=${plan} .interval=${interval}></pricing-card>`
  )) as PricingCard;
  await el.updateComplete;
  return el;
}

describe('PricingCard', () => {
  it('prices per bracket, never per seat', async () => {
    const el = await renderCard(BASE);
    const text = el.shadowRoot?.textContent || '';
    expect(text).to.contain('$12');
    expect(text).to.contain('/mo');
    expect(text).to.contain('billed monthly');
    // Bracket pricing: a plan costs the same for everyone inside the bracket,
    // so a "/user" unit would misstate what the customer is buying.
    expect(text).to.not.contain('/user');
  });

  it('leads with the effective monthly rate when the year divides exactly', async () => {
    const el = await renderCard(BASE, 'year');
    const text = el.shadowRoot?.textContent || '';
    // $120/yr is exactly $10/mo, which is the number a buyer compares
    // against the $12 monthly price.
    expect(text).to.contain('$10');
    expect(text).to.contain('/mo');
    expect(text).to.contain('billed annually ($120/yr)');
    expect(text).to.not.contain('$120 /yr');
  });

  it('formats thousands with separators on the annual note', async () => {
    const el = await renderCard(
      {
        ...BASE,
        id: 'team',
        name: 'Team',
        price_monthly: 120,
        price_annually: 1200,
      },
      'year'
    );
    const text = el.shadowRoot?.textContent || '';
    expect(text).to.contain('$100');
    expect(text).to.contain('billed annually ($1,200/yr)');
  });

  it('leads with the annual total when the monthly rate is not a round number', async () => {
    const el = await renderCard(
      {
        ...BASE,
        id: 'business',
        name: 'Business',
        price_monthly: 350,
        price_annually: 3500,
      },
      'year'
    );
    const text = el.shadowRoot?.textContent || '';
    // $3,500 / 12 is $291.67: an ugly headline, and rounding it silently
    // would misstate what is charged. The annual total leads instead.
    expect(text).to.contain('$3,500');
    expect(text).to.contain('/yr');
    expect(text).to.contain('about $292/mo, billed annually');
    expect(text).to.not.contain('291.67');
  });

  it('lets a configured note override the derived one', async () => {
    const el = await renderCard(
      { ...BASE, price_note_annual: 'Billed yearly, 2 months free' },
      'year'
    );
    const text = el.shadowRoot?.textContent || '';
    expect(text).to.contain('Billed yearly, 2 months free');
    expect(text).to.not.contain('billed annually ($120/yr)');
  });

  it('honours price_label so a floor price is not shown as an exact one', async () => {
    const el = await renderCard({
      id: 'enterprise',
      name: 'Enterprise',
      price_monthly: null,
      price_annually: null,
      price_label: 'from $30k/yr',
      features: [],
    });
    const text = el.shadowRoot?.textContent || '';
    expect(text).to.contain('from $30k/yr');
    expect(text).to.not.contain('Custom');
  });

  it('renders a tagline instead of a feature list when one is set', async () => {
    const el = await renderCard({
      ...BASE,
      tagline: 'You and all your agents.',
      features: ['Should not render'],
    });
    const text = el.shadowRoot?.textContent || '';
    expect(text).to.contain('You and all your agents.');
    // One number plus one line: quota and feature rows belong in the table.
    expect(text).to.not.contain('Should not render');
    expect(el.shadowRoot?.querySelector('.features')).to.not.exist;
  });

  it('still renders a feature list for plans that have no tagline', async () => {
    const el = await renderCard({ ...BASE, features: ['Feature A'] });
    expect(el.shadowRoot?.textContent).to.contain('Feature A');
    expect(el.shadowRoot?.querySelector('.features')).to.exist;
  });

  it('shows $0 rather than the word Free so the ladder reads consistently', async () => {
    const free = {
      id: 'free',
      name: 'Free',
      price_monthly: 0,
      price_annually: 0,
      tagline: 'Try Preloop with your own keys.',
      features: [],
    };
    for (const interval of ['month', 'year'] as const) {
      const el = await renderCard(free, interval);
      const text = el.shadowRoot?.textContent || '';
      expect(text).to.contain('$0');
      // The free plan keeps its name heading like every other card.
      expect(text).to.contain('Free');
      // Nothing is billed, so neither period nor a billing note applies.
      expect(text, interval).to.not.contain('/mo');
      expect(text, interval).to.not.contain('billed');
    }
  });

  it('shows Custom for a quoted plan that carries no label', async () => {
    const el = await renderCard({
      id: 'enterprise',
      name: 'Enterprise',
      price_monthly: null,
      price_annually: null,
      features: [],
    });
    expect(el.shadowRoot?.textContent).to.contain('Custom');
  });

  it('emits the plan id it was given when the CTA is pressed', async () => {
    const el = await renderCard({ ...BASE, id: 'business', name: 'Business' });
    let planId = '';
    el.addEventListener('signup-requested', (e) => {
      planId = (e as CustomEvent).detail.planId;
    });
    (el.shadowRoot?.querySelector('.cta') as HTMLElement).click();
    expect(planId).to.equal('business');
  });

  it('keeps the annual note readable on the highlighted gradient card', async () => {
    const el = await renderCard({ ...BASE, highlight: true }, 'year');
    const note = el.shadowRoot?.querySelector('.price-sub');
    expect(note, 'annual note renders').to.exist;
    expect(note?.textContent).to.contain('billed annually ($120/yr)');
    // Grey-on-purple was the review finding: the note now carries the class
    // that paints it in the card's own foreground colour.
    expect(note?.classList.contains('on-highlight')).to.equal(true);
  });

  it('leaves the note in the secondary colour on a plain card', async () => {
    const el = await renderCard(
      { ...BASE, id: 'team', price_monthly: 120, price_annually: 1200 },
      'year'
    );
    const note = el.shadowRoot?.querySelector('.price-sub');
    expect(note?.classList.contains('on-highlight')).to.equal(false);
  });

  it('uses the configured CTA label when one is supplied', async () => {
    const el = await renderCard({ ...BASE, cta_text: 'Contact us' });
    expect(el.shadowRoot?.querySelector('.cta')?.textContent).to.contain(
      'Contact us'
    );
  });
});
