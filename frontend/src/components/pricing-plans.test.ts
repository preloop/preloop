import { html, fixture, expect } from '@open-wc/testing';
import './pricing-plans';
import type { PricingPlanCards, PricingPlanComparison } from './pricing-plans';

const PLANS = [
  {
    id: 'free',
    name: 'Free',
    price_monthly: 0,
    price_annually: 0,
    features: [] as string[],
  },
  {
    id: 'pro',
    name: 'Pro',
    price_monthly: 12,
    price_annually: 120,
    features: [] as string[],
  },
];

const COMPARISON = {
  title: 'Compare cloud plans',
  note: 'Governance never stops.',
  groups: [
    {
      title: 'Plan',
      rows: [
        { label: 'Agents', values: { free: '3', pro: '10' } },
        { label: 'Replay verification', values: { free: false, pro: true } },
      ],
    },
  ],
};

describe('pricing-plan-cards', () => {
  async function render(props: Record<string, unknown> = {}) {
    const el = (await fixture(html`
      <pricing-plan-cards
        .plans=${PLANS}
        .interval=${(props.interval as string) ?? 'year'}
        .ctaFor=${props.ctaFor as any}
        .highlightId=${(props.highlightId as string) ?? ''}
      ></pricing-plan-cards>
    `)) as PricingPlanCards;
    await el.updateComplete;
    return el;
  }

  it('renders into the light DOM so the host page keeps its styles', async () => {
    const el = await render();
    // A shadow root here would cut the host page's table and grid rules off
    // from this markup, and hide the cards from the page's own queries.
    expect(el.shadowRoot).to.equal(null);
    expect(el.querySelectorAll('pricing-card')).to.have.length(2);
  });

  it("leaves every card's own label alone when the page says nothing", async () => {
    const el = await render();
    el.querySelectorAll('pricing-card').forEach((card) => {
      expect((card as any).ctaLabel).to.equal('');
      expect((card as any).ctaDisabled).to.equal(false);
    });
  });

  it('asks the page what each button should say', async () => {
    const el = await render({
      ctaFor: (plan: { id: string }) =>
        plan.id === 'free'
          ? { label: 'Your plan', disabled: true, note: 'No card required.' }
          : { label: 'Choose Pro' },
    });
    const cards = Array.from(el.querySelectorAll('pricing-card')) as any[];
    expect(cards[0].ctaLabel).to.equal('Your plan');
    expect(cards[0].ctaDisabled).to.equal(true);
    expect(cards[0].ctaNote).to.equal('No card required.');
    expect(cards[1].ctaLabel).to.equal('Choose Pro');
    expect(cards[1].ctaDisabled).to.equal(false);
  });

  it('marks one card when the page arrived asking about it', async () => {
    const el = await render({ highlightId: 'pro' });
    const marked = el.querySelectorAll('pricing-card.requested');
    expect(marked).to.have.length(1);
    expect((marked[0] as any).plan.id).to.equal('pro');
  });
});

describe('pricing-plan-comparison', () => {
  async function render(currentPlanId = '') {
    const el = (await fixture(html`
      <pricing-plan-comparison
        .comparison=${COMPARISON}
        .plans=${PLANS}
        .fallbackTitle=${'Compare plans'}
        .currentPlanId=${currentPlanId}
      ></pricing-plan-comparison>
    `)) as PricingPlanComparison;
    await el.updateComplete;
    return el;
  }

  it('renders one column per plan and one row per stated limit', async () => {
    const el = await render();
    expect(el.querySelectorAll('thead th')).to.have.length(3);
    expect(el.querySelectorAll('tbody tr')).to.have.length(3);
    expect(el.textContent).to.contain('Compare cloud plans');
    expect(el.textContent).to.contain('Governance never stops.');
  });

  it('renders the optional note link next to the note', async () => {
    const el = (await fixture(html`
      <pricing-plan-comparison
        .comparison=${{
          ...COMPARISON,
          note_link: { label: 'Your data', url: '/docs/your-data' },
        }}
        .plans=${PLANS}
        .fallbackTitle=${'Compare plans'}
      ></pricing-plan-comparison>
    `)) as PricingPlanComparison;
    await el.updateComplete;
    const link = el.querySelector('.comparison-note a');
    expect(link?.getAttribute('href')).to.equal('/docs/your-data');
    expect(link?.textContent).to.equal('Your data');
  });

  it("names the account's own column, and only on a page that knows it", async () => {
    const anonymous = await render();
    expect(anonymous.querySelector('th.current')).to.not.exist;

    const signedIn = await render('pro');
    const current = signedIn.querySelector('th.current');
    expect(current?.textContent).to.contain('Pro');
    expect(current?.textContent).to.contain('Your plan');
  });

  it('renders an unstated value as empty rather than as a refusal', async () => {
    const el = (await fixture(html`
      <pricing-plan-comparison
        .comparison=${{
          groups: [
            {
              title: 'Plan',
              rows: [{ label: 'Agents', values: { pro: '10' } }],
            },
          ],
        }}
        .plans=${PLANS}
        .fallbackTitle=${'Compare plans'}
      ></pricing-plan-comparison>
    `)) as PricingPlanComparison;
    await el.updateComplete;
    const cells = el.querySelectorAll('tbody tr:last-child td');
    // Free states no number, which is not a claim that Free has no agents.
    expect(cells[0].textContent?.trim()).to.equal('');
    expect(cells[1].textContent?.trim()).to.equal('10');
  });
});
