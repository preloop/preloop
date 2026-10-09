import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import '../../../components/view-header.ts';
import { invalidateApiCaches } from '../../../api';
import {
  PLAN_PAGE_PATH,
  capabilityForFeature,
  planPageUrl,
  premiumFeatureLabel,
} from '../../../utils/premium-features';
import './plan-view';
import type { PlanView } from './plan-view';

/** The published cloud ladder, as the public pricing page reads it. */
const CONTENT = {
  pricing: {
    title: 'Pricing',
    lead: 'Start free with your own keys.',
    billing_toggle: true,
    plans: [
      {
        id: 'free',
        name: 'Free',
        price_monthly: 0,
        price_annually: 0,
        deployment: 'cloud',
        tagline: 'Try Preloop with your own keys.',
        features: [],
      },
      {
        id: 'pro',
        name: 'Pro',
        price_monthly: 12,
        price_annually: 120,
        deployment: 'cloud',
        tagline: 'One person, up to 10 agents.',
        features: [],
      },
      {
        id: 'team',
        name: 'Team',
        price_monthly: 120,
        price_annually: 1200,
        deployment: 'cloud',
        tagline: 'Up to 5 people.',
        features: [],
      },
      {
        id: 'enterprise',
        name: 'Enterprise',
        price_monthly: null,
        price_annually: null,
        deployment: 'cloud',
        tagline: 'Priced per deployment.',
        cta_text: 'Contact us',
        cta_url: '/request-demo',
        features: [],
      },
    ],
    comparison: {
      title: 'Compare cloud plans',
      groups: [
        {
          title: 'Plan',
          rows: [
            {
              label: 'Agents',
              values: { free: '3', pro: '10', team: '100', enterprise: 'All' },
            },
          ],
        },
      ],
    },
    faqs: [],
  },
};

/**
 * A published card with no price, whose catalog entry never says
 * `purchasable: false`. An older server sends no `plan_eligibility` at all,
 * so nothing but the missing price says this plan is quoted, not sold.
 */
const UNPRICED_CARD = {
  id: 'scale',
  name: 'Scale',
  price_monthly: null,
  price_annually: null,
  deployment: 'cloud',
  tagline: 'Priced with you.',
  cta_text: 'Talk to us',
  cta_url: '/request-demo',
  features: [],
};

const UNPRICED_CATALOG = {
  id: 'scale',
  name: 'Scale',
  price_monthly: null,
  price_annually: null,
  features: {},
  capabilities: [],
};

/** The billing catalog, as plan-change-options reports it. */
const CATALOG = [
  {
    id: 'free',
    name: 'Free',
    price_monthly: 0,
    price_annually: 0,
    features: {},
    capabilities: [],
  },
  {
    id: 'pro',
    name: 'Pro',
    price_monthly: 12,
    price_annually: 120,
    features: {},
    capabilities: ['ai_optimization'],
  },
  {
    id: 'team',
    name: 'Team',
    price_monthly: 120,
    price_annually: 1200,
    features: {},
    capabilities: ['ai_optimization', 'rbac'],
  },
  {
    id: 'enterprise',
    name: 'Enterprise',
    price_monthly: null,
    price_annually: null,
    features: {},
    capabilities: ['ai_optimization', 'rbac'],
    purchasable: false,
  },
];

describe('PlanView', () => {
  let fetchStub: sinon.SinonStub;
  const originalUrl = window.location.href;

  function json(data: unknown, status = 200) {
    return new Response(JSON.stringify(data), {
      status,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  /** Every card's button label, keyed by plan name. */
  function ctas(el: PlanView): Record<string, string> {
    const labels: Record<string, string> = {};
    el.shadowRoot?.querySelectorAll('pricing-card').forEach((card) => {
      const name = (
        card.shadowRoot?.querySelector('.plan-name')?.textContent ?? ''
      ).trim();
      labels[name] = (
        card.shadowRoot?.querySelector('sl-button.cta')?.textContent ?? ''
      )
        .replace(/\s+/g, ' ')
        .trim();
    });
    return labels;
  }

  /** One card's note under the button. */
  function note(el: PlanView, planName: string): string {
    const card = Array.from(
      el.shadowRoot?.querySelectorAll('pricing-card') ?? []
    ).find(
      (c) =>
        (
          c.shadowRoot?.querySelector('.plan-name')?.textContent ?? ''
        ).trim() === planName
    );
    return (card?.shadowRoot?.querySelector('.cta-note')?.textContent ?? '')
      .replace(/\s+/g, ' ')
      .trim();
  }

  function clickCard(el: PlanView, planName: string): void {
    const card = Array.from(
      el.shadowRoot?.querySelectorAll('pricing-card') ?? []
    ).find(
      (c) =>
        (
          c.shadowRoot?.querySelector('.plan-name')?.textContent ?? ''
        ).trim() === planName
    );
    (card?.shadowRoot?.querySelector('sl-button.cta') as HTMLElement)?.click();
  }

  function createFetchStub(
    opts: {
      billing?: boolean;
      currentPlan?: string;
      subscription?: Record<string, unknown> | null;
      canManageBilling?: boolean;
      legacy?: boolean;
      checkout?: Record<string, unknown>;
      blocked?: string[];
      unpricedPlan?: boolean;
    } = {}
  ) {
    const billing = opts.billing !== false;
    const currentPlan = opts.currentPlan ?? 'free';
    const subscription =
      opts.subscription === undefined
        ? currentPlan === 'free'
          ? null
          : {
              id: 'sub-1',
              plan_id: currentPlan,
              status: 'active',
              interval: 'month',
              quantity: 1,
              currency: 'usd',
              unit_amount_cents: 1200,
              total_amount_cents: 1200,
              current_period_end: '2030-03-01T00:00:00Z',
              cancel_at_period_end: false,
              legacy: opts.legacy === true,
              revision: 'a',
              pending_change: null,
            }
        : opts.subscription;

    return sinon
      .stub(window, 'fetch')
      .callsFake(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();
        const method = (init?.method || 'GET').toUpperCase();

        if (url.includes('/landing-content.json'))
          return json(
            opts.unpricedPlan
              ? {
                  ...CONTENT,
                  pricing: {
                    ...CONTENT.pricing,
                    plans: [...CONTENT.pricing.plans, UNPRICED_CARD],
                  },
                }
              : CONTENT
          );

        if (url.includes('/api/v1/features')) {
          return json({ plugins: [], features: { billing } });
        }

        if (url.includes('/api/v1/billing/summary')) {
          return json({
            subscription: null,
            plan: { id: 'free', name: 'Free', features: {} },
            trial: { is_trialing: false },
            hosted_models: {
              models: [],
              current_usage_usd: 0,
              remaining_limit_usd: 0,
            },
            seats: {
              active_users: 1,
              included_users: 1,
              over_included: false,
              seat_addon: null,
            },
          });
        }

        if (url.includes('/api/v1/billing/plan-change-options')) {
          const catalog = opts.unpricedPlan
            ? [...CATALOG, UNPRICED_CATALOG]
            : CATALOG;
          const plans = opts.legacy
            ? [
                ...catalog,
                {
                  id: 'teams',
                  name: 'Legacy Teams',
                  price_monthly: 29,
                  price_annually: 290,
                  pricing_model: 'per_seat',
                  is_legacy: true,
                  features: {},
                  capabilities: ['ai_optimization'],
                },
              ]
            : catalog;
          const blocked = opts.blocked ?? [];
          const eligibility = opts.blocked
            ? plans.map((p) => ({
                plan_id: p.id,
                name: p.name,
                eligible: !blocked.includes(p.id),
                purchasable:
                  (p as { purchasable?: boolean }).purchasable !== false,
                contact_url: null,
                requires_period: p.id !== 'free',
                is_current: p.id === currentPlan,
                blockers: blocked.includes(p.id)
                  ? [
                      {
                        kind: 'members',
                        current: 9,
                        limit: 1,
                        message: 'Too many users for this plan.',
                      },
                    ]
                  : [],
                warnings: [],
                retention: {
                  oldest_record_at: null,
                  oldest_record_class: null,
                  target_days: null,
                  cutoff_at: null,
                  affected: false,
                  protected_by_floor: false,
                  floor_days: null,
                  legal_hold: false,
                },
              }))
            : undefined;
          return json({
            can_manage_billing: opts.canManageBilling !== false,
            ...(eligibility ? { plan_eligibility: eligibility } : {}),
            switching_enabled: true,
            current_subscription: subscription,
            current_plan: plans.find((p) => p.id === currentPlan) ?? null,
            plans,
            monthly_usage: [],
            current_usage: {
              active_users: 1,
              pending_invitations: 0,
              active_agents: 1,
              historical_seat_peak: null,
              historical_agent_peak: null,
            },
            assessments: [],
            storage_retention: {
              source: 'account_policy',
              minimum_days: 183,
              legal_holds_override: true,
            },
            warnings: [],
          });
        }

        if (url.includes('/api/v1/billing/create-checkout-session')) {
          return json(
            opts.checkout ?? {
              action: 'refresh',
              code: 'test_stub',
              message: 'Checkout was not opened in this test.',
            }
          );
        }

        return json({ detail: `Unhandled: ${method} ${url}` }, 500);
      });
  }

  async function mount(
    opts: Parameters<typeof createFetchStub>[0] = {},
    query = ''
  ): Promise<PlanView> {
    fetchStub = createFetchStub(opts);
    if (query)
      window.history.replaceState({}, '', `/console/settings/plan${query}`);
    const element = await fixture<PlanView>(html`<plan-view></plan-view>`);
    await waitUntil(() => !(element as any)._loading, 'plan page did not load');
    await element.updateComplete;
    return element;
  }

  beforeEach(() => {
    invalidateApiCaches();
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
  });

  afterEach(() => {
    fetchStub?.restore();
    localStorage.clear();
    invalidateApiCaches();
    window.history.replaceState({}, '', originalUrl);
  });

  it('renders the published cards and the comparison table', async () => {
    const element = await mount();
    const cards = element.shadowRoot?.querySelectorAll('pricing-card');
    expect(cards).to.have.length(4);
    expect(element.shadowRoot?.querySelector('pricing-plan-comparison table'))
      .to.exist;
    expect(
      (element.shadowRoot?.textContent ?? '').replace(/\s+/g, ' ')
    ).to.contain('Compare cloud plans');
  });

  it('opens on annual, like the public page', async () => {
    const element = await mount();
    const toggle = element.shadowRoot?.querySelector('billing-toggle') as any;
    expect(toggle.interval).to.equal('year');
  });

  it('marks the current plan on a Free account and offers the rest', async () => {
    const element = await mount();
    const labels = ctas(element);
    expect(labels['Free']).to.equal('Your plan');
    expect(labels['Pro']).to.equal('Choose Pro');
    expect(labels['Enterprise']).to.equal('Contact us');
    expect(note(element, 'Free')).to.contain('No card required');
    // The current column is named in the table too.
    expect(
      element.shadowRoot?.querySelector('pricing-plan-comparison th.current')
        ?.textContent
    ).to.contain('Free');
  });

  it('names the upgrade and the downgrade for a paid account', async () => {
    const element = await mount({ currentPlan: 'pro' });
    const labels = ctas(element);
    expect(labels['Pro']).to.equal('Your plan');
    expect(labels['Team']).to.equal('Upgrade to Team');
    expect(labels['Free']).to.equal('Switch to Free');
    // Founder decisions, stated before anything is clicked.
    expect(note(element, 'Team')).to.contain('Applies immediately');
    expect(note(element, 'Team')).to.contain('credit for the unused time');
    expect(note(element, 'Free')).to.contain('Takes effect on');
    expect(note(element, 'Pro')).to.contain('Renews on');
  });

  it('sends a Free account straight to checkout, with no quote step', async () => {
    const element = await mount();
    clickCard(element, 'Pro');
    await waitUntil(
      () =>
        fetchStub
          .getCalls()
          .some((c) => String(c.args[0]).includes('create-checkout-session')),
      'expected a checkout request'
    );
    const call = fetchStub
      .getCalls()
      .find((c) => String(c.args[0]).includes('create-checkout-session'))!;
    expect(JSON.parse(call.args[1].body)).to.deep.equal({
      plan_id: 'pro',
      interval: 'year',
      return_to: '/console/settings/plan',
    });
    // Nothing to switch means nothing to preview.
    expect(
      fetchStub
        .getCalls()
        .some((c) => String(c.args[0]).includes('plan-change-preview'))
    ).to.equal(false);
  });

  it('hands a subscribed account to the quote step instead of checkout', async () => {
    const element = await mount({ currentPlan: 'pro' });
    const panel = element.shadowRoot?.querySelector(
      'billing-plan-comparison'
    ) as any;
    expect(panel, 'expected the plan change section').to.exist;
    const startChange = sinon.spy(panel, 'startChange');

    clickCard(element, 'Team');
    await element.updateComplete;

    expect(startChange).to.have.been.calledWith('team', 'year');
    expect(
      fetchStub
        .getCalls()
        .some((c) => String(c.args[0]).includes('create-checkout-session'))
    ).to.equal(false);
  });

  it('re-renders the cards after a change is confirmed below', async () => {
    const element = await mount({ currentPlan: 'pro' });
    expect(ctas(element)['Pro']).to.equal('Your plan');

    // The change goes through in the section below, which answers with this
    // event. Nothing else tells the cards that "Your plan" moved.
    fetchStub.restore();
    fetchStub = createFetchStub({ currentPlan: 'team' });
    element.shadowRoot?.querySelector('billing-plan-comparison')?.dispatchEvent(
      new CustomEvent('billing-subscription-changed', {
        bubbles: true,
        composed: true,
      })
    );

    await waitUntil(async () => {
      await element.updateComplete;
      return ctas(element)['Team'] === 'Your plan';
    }, 'the cards kept the plan the account no longer holds');
    expect(ctas(element)['Pro']).to.equal('Switch to Pro');
  });

  it('says so when the reload after a change fails', async () => {
    const element = await mount({ currentPlan: 'pro' });
    fetchStub.restore();
    fetchStub = sinon
      .stub(window, 'fetch')
      .callsFake(async () => json({ detail: 'nope' }, 500));

    // An un-awaited throw here used to be an unhandled rejection: stale cards
    // and nothing on screen admitting the refresh failed.
    element.shadowRoot?.querySelector('billing-plan-comparison')?.dispatchEvent(
      new CustomEvent('billing-subscription-changed', {
        bubbles: true,
        composed: true,
      })
    );

    await waitUntil(async () => {
      await element.updateComplete;
      return !!element.shadowRoot?.querySelector('[data-testid="plan-error"]');
    }, 'a failed reload said nothing');
    expect(
      element.shadowRoot?.querySelector('[data-testid="plan-error"]')
        ?.textContent
    ).to.contain('current plan');
  });

  it('speaks the refusal the dialog showed, end to end from a 402', async () => {
    // The whole path: a 402 names `session_titles`, the upgrade dialog builds
    // the URL, this page reads it. The card matches on the capability the
    // feature maps to (`ai_optimization`, which Pro sells) and the note says
    // it back in the words the reader was refused in.
    const element = await mount(
      {},
      planPageUrl('session_titles').slice(PLAN_PAGE_PATH.length)
    );
    const requested = element.shadowRoot?.querySelector(
      'pricing-card.requested'
    );
    expect(
      requested?.shadowRoot?.querySelector('.plan-name')?.textContent
    ).to.contain('Pro');
    expect(note(element, 'Pro')).to.contain('AI session titles');
    // Read the capability's label from the map rather than quoting it. #772
    // renamed this one, which silently turned a hardcoded "must not contain"
    // into an assertion about a string nothing produces any more.
    expect(note(element, 'Pro')).to.not.contain(
      premiumFeatureLabel(capabilityForFeature('session_titles'))
    );
  });

  it('opens on the cheapest plan that unlocks a refused feature', async () => {
    const element = await mount({}, '?feature=session_optimization');
    const requested = element.shadowRoot?.querySelector(
      'pricing-card.requested'
    );
    expect(requested, 'expected a highlighted card').to.exist;
    expect(
      requested?.shadowRoot?.querySelector('.plan-name')?.textContent
    ).to.contain('Pro');
    expect(note(element, 'Pro')).to.contain('AI session optimization');
  });

  it('highlights the same plan the quote panel opens on when one is blocked', async () => {
    // Pro is the cheapest plan that unlocks it, but the server says this
    // account cannot take it. Highlighting Pro while the panel below opened
    // on Team would put the refusal and its fix on two different plans.
    const element = await mount(
      { blocked: ['pro'] },
      '?feature=session_optimization'
    );
    expect(
      element.shadowRoot
        ?.querySelector('pricing-card.requested')
        ?.shadowRoot?.querySelector('.plan-name')?.textContent
    ).to.contain('Team');
    const panel = element.shadowRoot?.querySelector(
      'billing-plan-comparison'
    ) as any;
    await waitUntil(() => !panel.loading, 'the quote panel never loaded');
    expect(panel.selectedPlan).to.equal('team');
  });

  it('honours an explicit plan and interval in the query', async () => {
    const element = await mount({}, '?plan=team&interval=month');
    const toggle = element.shadowRoot?.querySelector('billing-toggle') as any;
    expect(toggle.interval).to.equal('month');
    expect(
      element.shadowRoot
        ?.querySelector('pricing-card.requested')
        ?.shadowRoot?.querySelector('.plan-name')?.textContent
    ).to.contain('Team');
  });

  it('sends a quote-only plan to the contact route', async () => {
    const element = await mount({ currentPlan: 'pro' });
    const navigate = sinon.stub(element as any, '_navigate');
    clickCard(element, 'Enterprise');
    await element.updateComplete;
    expect(navigate).to.have.been.calledWith('/request-demo');
  });

  it('refuses to offer a change the reader may not make', async () => {
    const element = await mount({
      currentPlan: 'pro',
      canManageBilling: false,
    });
    const card = Array.from(
      element.shadowRoot?.querySelectorAll('pricing-card') ?? []
    ).find(
      (c) =>
        (
          c.shadowRoot?.querySelector('.plan-name')?.textContent ?? ''
        ).trim() === 'Team'
    );
    expect(
      card?.shadowRoot?.querySelector('sl-button.cta')?.hasAttribute('disabled')
    ).to.equal(true);
    expect(note(element, 'Team')).to.contain('billing owner');
  });

  it('keeps a legacy per-seat account on its plan and offers the ladder', async () => {
    const element = await mount({ currentPlan: 'teams', legacy: true });
    const labels = ctas(element);
    // The legacy plan is not in the published ladder, so no card claims to be
    // the current plan, and every card offers a real move.
    expect(Object.values(labels)).to.not.contain('Your plan');
    expect(labels['Team']).to.equal('Upgrade to Team');
    expect(labels['Free']).to.equal('Switch to Free');
    expect(
      element.shadowRoot?.querySelector('pricing-plan-comparison th.current')
    ).to.not.exist;
    // But the page still says what the account is on, or a legacy customer
    // reads four plans and no answer.
    const line = element.shadowRoot?.querySelector(
      '[data-testid="current-plan-line"]'
    )?.textContent;
    expect(line).to.contain('Legacy Teams');
    expect(line).to.contain('renews on');
  });

  it('clicks the button it rendered on a server that sends no eligibility', async () => {
    // The old-server shape: no `plan_eligibility`, and a card the catalog
    // never marked unpurchasable. The page prints "Priced per deployment", so
    // the click has to go to the sales conversation rather than to a checkout
    // for an amount nobody quoted.
    const element = await mount({ currentPlan: 'pro', unpricedPlan: true });
    expect(element.shadowRoot?.querySelector('[data-testid="plan-error"]')).to
      .not.exist;
    expect(ctas(element)['Scale']).to.equal('Talk to us');
    expect(note(element, 'Scale')).to.contain('Priced per deployment');

    const navigate = sinon.stub(element as any, '_navigate');
    const panel = element.shadowRoot?.querySelector(
      'billing-plan-comparison'
    ) as any;
    const startChange = sinon.spy(panel, 'startChange');
    clickCard(element, 'Scale');
    await element.updateComplete;

    expect(navigate).to.have.been.calledWith('/request-demo');
    expect(startChange).to.not.have.been.called;
    expect(
      fetchStub
        .getCalls()
        .some((c) => String(c.args[0]).includes('create-checkout-session'))
    ).to.equal(false);
  });

  it('says when a subscription is ending rather than renewing', async () => {
    const element = await mount({
      currentPlan: 'pro',
      subscription: {
        id: 'sub-1',
        plan_id: 'pro',
        status: 'active',
        interval: 'month',
        quantity: 1,
        currency: 'usd',
        unit_amount_cents: 1200,
        total_amount_cents: 1200,
        current_period_end: '2030-03-01T00:00:00Z',
        cancel_at_period_end: true,
        revision: 'a',
        pending_change: null,
      },
    });

    // A cancelled subscription returns the account to Free at period end, so
    // its own card must not promise a renewal.
    expect(note(element, 'Pro')).to.contain('Ends on');
    expect(note(element, 'Pro')).to.not.contain('Renews on');
  });

  it('says nothing about plans where nothing is sold (open source default)', async () => {
    const element = await mount({ billing: false });
    expect(
      element.shadowRoot?.querySelector('[data-testid="billing-unavailable"]')
    ).to.exist;
    expect(element.shadowRoot?.querySelector('pricing-card')).to.not.exist;
    expect(element.shadowRoot?.querySelector('billing-plan-comparison')).to.not
      .exist;
    expect(
      fetchStub
        .getCalls()
        .some((c) => String(c.args[0]).includes('/api/v1/billing/'))
    ).to.equal(false);
  });
});
