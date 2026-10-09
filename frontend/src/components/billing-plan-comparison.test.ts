import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';
import { invalidateApiCaches } from '../api';
import type {
  PlanChangeOptions,
  PlanChangePreview,
  PlanEligibility,
} from '../types/billing';
import './billing-plan-comparison';
import type { BillingPlanComparison } from './billing-plan-comparison';

const json = (value: unknown, status = 200) =>
  new Response(JSON.stringify(value), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
const plan = (id: string, extra = {}) => ({
  id,
  name: id === 'teams' ? 'Legacy Teams' : id === 'pro' ? 'Pro' : 'Enterprise',
  price_monthly: 10,
  price_annually: 100,
  pricing_model: 'bracket' as const,
  legacy: false,
  purchasable: true,
  capabilities: ['ai_optimization'],
  features: {
    max_users: 1,
    max_agents: -1,
    byok_ingest_tokens_monthly: 200000000,
    hosted_models_monthly_limit_usd: 2,
    retention_days: 365,
  },
  ...extra,
});
/**
 * One server verdict. The console composes no sentence of its own, so a test
 * that wants a refusal or a consequence on screen has to put the server's
 * words here, exactly as the backend builds them.
 */
const verdict = (
  id: string,
  extra: Partial<PlanEligibility> = {}
): PlanEligibility => ({
  plan_id: id,
  name: id === 'pro' ? 'Pro' : id === 'free' ? 'Free' : 'Enterprise',
  eligible: true,
  purchasable: true,
  contact_url: null,
  requires_period: true,
  is_current: false,
  blockers: [],
  warnings: [],
  retention: {
    oldest_record_at: null,
    oldest_record_class: null,
    target_days: 365,
    cutoff_at: null,
    affected: false,
    protected_by_floor: false,
    floor_days: null,
    legal_hold: false,
    message: null,
    benefit_message: null,
  },
  ...extra,
});
function options(): PlanChangeOptions {
  return {
    can_manage_billing: true,
    switching_enabled: true,
    current_subscription: {
      id: 'subscription-local',
      plan_id: 'teams',
      status: 'active',
      interval: 'month',
      quantity: 1,
      currency: 'usd',
      unit_amount_cents: 2900,
      total_amount_cents: 2900,
      current_period_end: '2030-09-18T11:00:00Z',
      cancel_at_period_end: false,
      legacy: true,
      revision: 'revision-a',
      pending_change: null,
    },
    current_plan: plan('teams', {
      legacy: true,
      pricing_model: 'per_seat',
      features: {
        max_users: -1,
        max_agents: -1,
        byok_ingest_tokens_monthly: -1,
        hosted_models_monthly_limit_usd: 10,
        retention_days: 365,
      },
      capabilities: ['ai_optimization', 'rbac'],
    }),
    plans: [
      plan('pro'),
      plan('enterprise', {
        price_monthly: null,
        price_annually: null,
        purchasable: false,
      }),
    ],
    monthly_usage: [6, 7, 8, 9].map((month) => ({
      period_start: `2030-${String(month).padStart(2, '0')}-01T00:00:00Z`,
      period_end: `2030-${String(month + 1).padStart(2, '0')}-01T00:00:00Z`,
      is_partial: month === 9,
      coverage: month === 9 ? 'partial' : 'complete',
      coverage_reasons: month === 9 ? ['current_partial_month'] : [],
      observed_byok_tokens: 5000,
      observed_hosted_cost_usd: 0.25,
      request_count: 10,
    })),
    current_usage: {
      active_users: 1,
      pending_invitations: 0,
      active_agents: 2,
      historical_seat_peak: null,
      historical_agent_peak: null,
    },
    assessments: [
      { plan_id: 'pro', fit: 'fits', blockers: [], advisories: [], months: [] },
    ],
    plan_eligibility: [
      verdict('pro'),
      verdict('enterprise', {
        purchasable: false,
        requires_period: false,
        contact_url: '/request-demo',
      }),
    ],
    storage_retention: {
      source: 'account_policy',
      minimum_days: 183,
      legal_holds_override: true,
    },
    warnings: [],
  };
}
function preview(): PlanChangePreview {
  return {
    preview_id: 'signed-quote-a',
    expires_at: '2035-01-01T00:00:00Z',
    current: {
      plan_id: 'teams',
      name: 'Legacy Teams',
      interval: 'month',
      quantity: 1,
      unit_amount_cents: 2900,
      total_amount_cents: 2900,
      currency: 'usd',
      features: {},
    },
    target: {
      plan_id: 'pro',
      name: 'Pro',
      interval: 'month',
      quantity: 1,
      unit_amount_cents: 1000,
      total_amount_cents: 1000,
      currency: 'usd',
      features: {},
      addon_quantity: 0,
    },
    timing: 'period_end',
    effective_at: '2030-09-18T11:00:00Z',
    proration_amount_cents: 0,
    amount_due_now_cents: 0,
    currency: 'usd',
    assessment: options().assessments[0],
    blockers: [],
    advisories: [],
    confirmation_required: true,
  };
}

describe('Billing plan comparison', () => {
  let stub: sinon.SinonStub;
  let data: PlanChangeOptions;
  let quote: PlanChangePreview;
  let previewResponse: (() => Promise<Response>) | undefined;
  let confirmResponse: (() => Promise<Response>) | undefined;
  const text = (el: BillingPlanComparison) =>
    (el.shadowRoot?.textContent ?? '').replace(/\s+/g, ' ');
  const calls = (suffix: string) =>
    stub.getCalls().filter((c) => String(c.args[0]).endsWith(suffix));
  const button = (el: BillingPlanComparison, id: string) =>
    el.shadowRoot!.querySelector(`[data-testid="${id}"]`) as HTMLButtonElement;
  async function mount() {
    const el = await fixture<BillingPlanComparison>(
      html`<billing-plan-comparison></billing-plan-comparison>`
    );
    await waitUntil(() => !(el as any).loading);
    await el.updateComplete;
    return el;
  }
  /** Step 2. The section is collapsed on load, so every action starts here. */
  async function open(el: BillingPlanComparison) {
    button(el, 'change-plan').click();
    await el.updateComplete;
  }
  async function showComparison(el: BillingPlanComparison) {
    button(el, 'show-comparison').click();
    await el.updateComplete;
  }
  async function showUsage(el: BillingPlanComparison) {
    button(el, 'show-usage').click();
    await el.updateComplete;
  }
  async function request(el: BillingPlanComparison) {
    if (!(el as any).changing) await open(el);
    button(el, 'preview').click();
    await waitUntil(() => !(el as any).busy);
    await el.updateComplete;
  }
  async function consent(el: BillingPlanComparison) {
    const checkbox = el.shadowRoot!.querySelector(
      '[data-testid="consent"]'
    ) as HTMLInputElement;
    checkbox.click();
    await el.updateComplete;
  }
  beforeEach(() => {
    invalidateApiCaches();
    localStorage.setItem('accessToken', 'test-access-token');
    data = options();
    quote = preview();
    previewResponse = undefined;
    confirmResponse = undefined;
    stub = sinon
      .stub(window, 'fetch')
      .callsFake(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes('/plan-change-options')) return json(data);
        if (url.endsWith('/plan-change-preview')) {
          if (previewResponse) return previewResponse();
          // The server quotes the period that was asked for. The console
          // refuses a quote for another period, so a fixture that always
          // answered "month" would refuse every annual quote.
          const asked = JSON.parse(String(init?.body ?? '{}')).interval;
          return json({
            ...quote,
            target: {
              ...quote.target,
              interval: asked ?? quote.target.interval,
            },
          });
        }
        if (url.endsWith('/plan-change-confirm'))
          return confirmResponse
            ? confirmResponse()
            : json({
                status: 'scheduled',
                plan_id: 'pro',
                effective_at: quote.effective_at,
                operation_id: 'change-a',
              });
        if (url.endsWith('/create-checkout-session'))
          return json({
            action: 'redirect',
            url: 'https://checkout.stripe.com/example',
          });
        return json({ detail: 'Unexpected request' }, 500);
      });
  });
  afterEach(() => {
    stub.restore();
    localStorage.clear();
    invalidateApiCaches();
  });

  it('opens collapsed: the current plan, one action, no tables and no warnings', async () => {
    data.warnings = [
      { code: 'subscription_not_reconciled', message: 'Not yet verified.' },
    ];
    const el = await mount();
    expect(text(el)).to.include('Legacy Teams').and.include('Unlimited users');
    expect(button(el, 'change-plan')).to.exist;
    expect(el.shadowRoot!.querySelector('[data-testid="plan"]')).to.not.exist;
    expect(el.shadowRoot!.querySelector('table')).to.not.exist;
    expect(text(el))
      .to.not.include('What changes')
      .and.not.include('Would this plan cover your usage?')
      .and.not.include('Not yet verified.');
  });
  it('reveals the picker, then the comparison, then the usage months, each on request', async () => {
    const el = await mount();
    await open(el);
    expect(el.shadowRoot!.querySelector('[data-testid="plan"]')).to.exist;
    expect(text(el)).to.not.include('What changes');
    expect(el.shadowRoot!.querySelector('table')).to.not.exist;
    await showComparison(el);
    expect(text(el)).to.include('What changes');
    expect(text(el)).to.not.include('Would this plan cover your usage?');
    await showUsage(el);
    expect(text(el))
      .to.include('Would this plan cover your usage?')
      .and.include('Missing records are not zero usage');
    await showUsage(el);
    expect(text(el)).to.not.include('Would this plan cover your usage?');
  });
  it('defaults the picker to the next tier up on annual billing, never to Free', async () => {
    data.current_subscription = null;
    data.current_plan = plan('free', {
      name: 'Free',
      price_monthly: 0,
      price_annually: 0,
    });
    data.plans.unshift(
      plan('free', { name: 'Free', price_monthly: 0, price_annually: 0 })
    );
    const el = await mount();
    expect((el as any).selectedPlan).to.equal('pro');
    expect((el as any).interval).to.equal('year');
    await open(el);
    expect(text(el)).to.include('Pro: $100.00 / year');
    const picker = el.shadowRoot!.querySelector(
      '[data-testid="plan"]'
    ) as HTMLSelectElement;
    expect([...picker.options].map((o) => o.value)).to.not.include('free');
  });
  /**
   * Arrive from somewhere that already asked the question.
   *
   * The upgrade dialog and the public pricing cards both send the reader here
   * with their answer in the query, so this section has to open on it instead
   * of making the reader choose again.
   */
  describe('arriving with a request in the query', () => {
    let original: string;
    beforeEach(() => {
      original = window.location.pathname + window.location.search;
    });
    afterEach(() => {
      window.history.replaceState({}, '', original);
    });
    const arriveWith = (query: string) =>
      window.history.replaceState(
        {},
        '',
        `${window.location.pathname}${query}`
      );

    it('opens the picker on the named plan and period', async () => {
      data.plans.push(
        plan('scale', {
          name: 'Scale',
          price_monthly: 50,
          price_annually: 500,
        })
      );
      data.plan_eligibility!.push(verdict('scale'));
      arriveWith('?plan=scale&interval=month');
      const el = await mount();
      expect((el as any).changing, 'picker open').to.be.true;
      expect((el as any).selectedPlan).to.equal('scale');
      expect((el as any).interval).to.equal('month');
    });

    it('opens on the cheapest plan that includes the refused capability', async () => {
      // Pro sells ai_optimization but not rbac. Sending a reader refused for
      // rbac to Pro sells them a plan that refuses them again.
      data.plans.push(
        plan('scale', {
          name: 'Scale',
          price_monthly: 50,
          price_annually: 500,
          capabilities: ['ai_optimization', 'rbac'],
        })
      );
      data.plan_eligibility!.push(verdict('scale'));
      arriveWith('?feature=rbac');
      const el = await mount();
      expect((el as any).selectedPlan).to.equal('scale');
      expect((el as any).changing).to.be.true;
    });

    it('keeps the ordinary default when no plan proves it includes the feature', async () => {
      arriveWith('?feature=reconciliation');
      const el = await mount();
      expect((el as any).selectedPlan).to.equal('pro');
    });

    it('ignores a request to change to the plan the account is already on', async () => {
      data.current_subscription = null;
      data.current_plan = plan('free', {
        name: 'Free',
        price_monthly: 0,
        price_annually: 0,
      });
      data.plans.unshift(
        plan('free', { name: 'Free', price_monthly: 0, price_annually: 0 })
      );
      arriveWith('?plan=free');
      const el = await mount();
      expect((el as any).selectedPlan).to.equal('pro');
    });

    it('stays collapsed when nothing was requested', async () => {
      const el = await mount();
      expect((el as any).changing).to.be.false;
    });
  });

  it('opens on the plan a card asked for', async () => {
    // The plan page's cards state the offer; the quote and the confirmation
    // stay here, so a card click selects a plan rather than buying one.
    data.plans.unshift(
      plan('free', { name: 'Free', price_monthly: 0, price_annually: 0 })
    );
    const el = await mount();
    el.startChange('pro', 'year');
    await el.updateComplete;

    expect((el as any).changing).to.be.true;
    expect((el as any).selectedPlan).to.equal('pro');
    expect((el as any).interval).to.equal('year');
    expect(calls('/plan-change-preview')).to.have.length(0);
  });

  it('waits for the options before applying a plan asked for during the load', async () => {
    const el = await fixture<BillingPlanComparison>(
      html`<billing-plan-comparison></billing-plan-comparison>`
    );
    // Asked while the first read is still in flight. Selecting now would
    // cancel that read and leave the section loading forever.
    el.startChange('pro', 'year');
    await waitUntil(() => !(el as any).loading, 'options never arrived');
    await el.updateComplete;

    expect((el as any).selectedPlan).to.equal('pro');
    expect((el as any).interval).to.equal('year');
    expect(el.shadowRoot!.querySelector('[data-testid="plan"]')).to.exist;
  });

  it('opens on a named plan for an account with no subscription', async () => {
    // The plan page's cards are the only caller now that the account page
    // links out instead of embedding this section, so the Free path in has
    // to work through the same entry point as every other card.
    data.current_subscription = null;
    data.current_plan = plan('free', {
      name: 'Free',
      price_monthly: 0,
      price_annually: 0,
    });
    const el = await mount();
    expect((el as any).changing).to.be.false;
    el.startChange('pro', 'year');
    await el.updateComplete;
    expect(el.shadowRoot!.querySelector('[data-testid="plan"]')).to.exist;
    expect((el as any).selectedPlan).to.equal('pro');
  });

  it('treats an expired trial as Free on the collapsed line and picker default', async () => {
    data.current_subscription = {
      ...data.current_subscription!,
      plan_id: 'pro',
      status: 'trialing',
      current_period_end: '2025-07-27T00:00:00Z',
      legacy: false,
    };
    data.current_plan = plan('pro');
    data.plans.unshift(
      plan('free', { name: 'Free', price_monthly: 0, price_annually: 0 })
    );
    const el = await mount();
    const current = el.shadowRoot!.querySelector(
      '[data-testid="current-plan"]'
    )?.textContent;
    expect(current).to.include('Free');
    expect(current).to.not.include('Pro');
    expect((el as any).selectedPlan).to.equal('pro');
  });
  it('does not default the picker to Free when it is the only other catalog plan', async () => {
    data.current_subscription!.plan_id = 'pro';
    data.current_subscription!.legacy = false;
    data.current_plan = plan('pro');
    data.plans = [
      plan('free', { name: 'Free', price_monthly: 0, price_annually: 0 }),
      plan('pro'),
    ];
    const el = await mount();
    expect((el as any).selectedPlan).to.not.equal('free');
  });
  it('shows the account warnings beside the action, not as the headline', async () => {
    data.warnings = [
      { code: 'subscription_not_reconciled', message: 'Not yet verified.' },
    ];
    const el = await mount();
    expect(el.shadowRoot!.querySelector('[data-testid="warnings"]')).to.not
      .exist;
    await open(el);
    expect(el.shadowRoot!.querySelector('[data-testid="warnings"]')).to.exist;
    expect(text(el)).to.include('Not yet verified.');
  });
  it('says nothing about fit when nothing is blocked and nothing is affected', async () => {
    const el = await mount();
    await open(el);
    for (const id of [
      'plan-blockers',
      'plan-warnings',
      'plan-benefit',
      'retention-protected',
      'unavailable-plans',
    ])
      expect(el.shadowRoot!.querySelector(`[data-testid="${id}"]`), id).to.not
        .exist;
    expect(text(el))
      .to.not.include('Not enough evidence')
      .and.not.include('historical peaks')
      .and.not.include('fits this plan');
    expect(el.shadowRoot!.querySelector('table')).to.not.exist;
    await showUsage(el);
    expect(text(el)).to.include('June 2030');
  });
  it('lists the reasons inline when the selected plan does not fit', async () => {
    data.assessments[0].fit = 'exceeds';
    data.assessments[0].advisories = [
      { code: 'hosted', message: 'Built-in model spend exceeds this plan.' },
    ];
    const el = await mount();
    await open(el);
    const warnings = el.shadowRoot!.querySelector(
      '[data-testid="plan-warnings"]'
    );
    expect(warnings?.textContent).to.include(
      'Built-in model spend exceeds this plan.'
    );
    expect(text(el)).to.not.include('Not enough evidence');
    expect(el.shadowRoot!.querySelector('table')).to.not.exist;
  });
  it('reconciles with the provider only when the reader asks for a refresh', async () => {
    const el = await mount();
    expect(String(stub.getCall(0).args[0])).to.not.include('reconcile');
    button(el, 'refresh').click();
    await waitUntil(() => !(el as any).loading);
    expect(calls('?reconcile=true')).to.have.length(1);
  });
  it('only reads on load and compares three completed months plus current partial', async () => {
    const el = await mount();
    await open(el);
    await showUsage(el);
    expect(calls('/plan-change-options')).to.have.length(1);
    expect(
      stub
        .getCalls()
        .every((c) => !c.args[1]?.method || c.args[1]?.method === 'GET')
    ).to.equal(true);
    expect(text(el))
      .to.include('June 2030')
      .and.include('August 2030')
      .and.include('Current partial month');
    expect(text(el)).to.not.include('Historical user peak');
  });
  it('states what the months show without turning an incomplete one into a warning', async () => {
    data.monthly_usage[0].coverage = 'unknown';
    data.monthly_usage[0].coverage_reasons = ['account_created_during_period'];
    data.monthly_usage[0].observed_byok_tokens = null;
    data.monthly_usage[0].observed_hosted_cost_usd = null;
    const el = await mount();
    await open(el);
    await showUsage(el);
    expect(text(el)).to.include('account was created during this month');
    expect(text(el)).to.include('Missing records are not zero usage');
    expect(text(el))
      .to.not.include('Not enough evidence to confirm a fit')
      .and.not.include('historical peaks are unavailable');
    expect(el.shadowRoot!.querySelector('[data-testid="plan-warnings"]')).to.not
      .exist;
  });
  it('shows observed overage even when the full month is incomplete', async () => {
    data.monthly_usage[0].coverage = 'unknown';
    data.monthly_usage[0].observed_byok_tokens = 201000000;
    data.monthly_usage[0].observed_hosted_cost_usd = 4;
    data.assessments[0].fit = 'exceeds';
    const el = await mount();
    await open(el);
    await showUsage(el);
    expect(text(el))
      .to.include('Above selected quota')
      .and.include('Above included allowance')
      .and.include('Observed only');
  });
  it('shows catalog history separately from stored evidence and feature loss', async () => {
    data.plans[0].features.retention_days = 730;
    const el = await mount();
    await open(el);
    await showComparison(el);
    expect(text(el))
      .to.include('2 years')
      .and.include('Older analytics are periodically removed')
      .and.include('longer grandfathered commitments remain protected')
      .and.include('minimum retention of 183 days')
      .and.include('Legal holds');
    expect(text(el))
      .to.include('Role-based access control')
      .and.include('Not included');
    expect(text(el)).to.include(
      'gateway, firewall, approvals and budgets continue'
    );
  });
  it('shows legacy unit rate but never offers the legacy plan as a candidate', async () => {
    data.plans.push(data.current_plan);
    const el = await mount();
    await open(el);
    expect(text(el)).to.include('Legacy Teams').and.include('$29.00 per user');
    const select = el.shadowRoot!.querySelector(
      '[data-testid="plan"]'
    ) as HTMLSelectElement;
    expect([...select.options].map((o) => o.value)).to.not.include('teams');
  });
  it('never prints placeholder text where a provider amount belongs', async () => {
    data.current_subscription!.total_amount_cents = null;
    data.current_subscription!.unit_amount_cents = null;
    const el = await mount();
    await open(el);
    expect(text(el))
      .to.not.include('Unavailable')
      .and.not.include('Unknown users')
      .and.not.include('unverified period');
    expect(text(el)).to.include(
      'Current subscription amount: not yet verified with the payment provider.'
    );
  });
  it('keeps member actions disabled including direct handler calls', async () => {
    data.can_manage_billing = false;
    const el = await mount();
    await open(el);
    expect(button(el, 'preview').disabled).to.equal(true);
    await (el as any).requestPreview();
    expect(calls('/plan-change-preview')).to.have.length(0);
    expect(text(el)).to.include(
      'Only a billing owner or account administrator'
    );
  });
  it('explains a disabled change quietly instead of heading the page with it', async () => {
    data.switching_enabled = false;
    const el = await mount();
    expect(button(el, 'change-plan').disabled).to.equal(true);
    expect(text(el)).to.include(
      'Plan changes from the console are not available yet. Manage in Stripe or contact support.'
    );
    expect(
      el
        .shadowRoot!.querySelector('[data-testid="switching-disabled"] a')
        ?.getAttribute('href')
    ).to.equal('mailto:sales@preloop.ai');
    expect(el.shadowRoot!.querySelector('.warning')).to.not.exist;
    expect(el.shadowRoot!.querySelector('[data-testid="preview"]')).to.not
      .exist;
  });
  it('keeps checkout for a free account behind the same flag', async () => {
    data.switching_enabled = false;
    data.current_subscription = null;
    data.current_plan = plan('free', { name: 'Free' });
    const el = await mount();
    expect(button(el, 'change-plan').disabled).to.equal(true);
    expect(text(el)).to.include(
      'Plan changes from the console are not available yet'
    );
  });
  it('does not silently replace an already scheduled cancellation', async () => {
    data.current_subscription!.cancel_at_period_end = true;
    const el = await mount();
    await open(el);
    expect(button(el, 'preview').disabled).to.equal(true);
    expect(text(el)).to.include('already scheduled');
  });
  it('requires a real preview then explicit consent before confirming once', async () => {
    const el = await mount();
    await request(el);
    expect(
      JSON.parse(calls('/plan-change-preview')[0].args[1].body)
    ).to.deep.equal({ target_plan_id: 'pro', interval: 'year' });
    expect(calls('/plan-change-confirm')).to.have.length(0);
    expect(button(el, 'confirm').disabled).to.equal(true);
    expect(text(el))
      .to.include('at the end of your current billing period')
      .and.include('Returning to this withdrawn plan');
    await consent(el);
    expect(button(el, 'confirm').disabled).to.equal(false);
    button(el, 'confirm').click();
    button(el, 'confirm').click();
    await waitUntil(() => !(el as any).busy);
    await el.updateComplete;
    expect(calls('/plan-change-confirm')).to.have.length(1);
    expect(
      JSON.parse(calls('/plan-change-confirm')[0].args[1].body)
    ).to.deep.equal({ preview_id: 'signed-quote-a' });
    expect(text(el)).to.include('Plan change scheduled');
    await waitUntil(() => calls('/plan-change-options').length >= 2);
    expect(text(el))
      .to.include('Pro')
      .and.not.to.include('Refresh to see your subscription');
    expect(button(el, 'preview').disabled).to.equal(false);
  });
  it('does not let an expired preview be confirmed', async () => {
    quote.expires_at = '2020-01-01T00:00:00Z';
    const el = await mount();
    await request(el);
    (el as any).accepted = true;
    await (el as any).confirm();
    expect(calls('/plan-change-confirm')).to.have.length(0);
    expect(text(el)).to.include('preview has expired');
  });
  it('does not permit confirmation if the provider price is unknown', async () => {
    quote.amount_due_now_cents = null;
    const el = await mount();
    await request(el);
    expect(button(el, 'confirm').disabled).to.equal(true);
    expect(text(el)).to.include(
      'complete price and effective date could not be verified'
    );
  });
  it('rejects a quote for a different target and clears consent on interval changes', async () => {
    const el = await mount();
    await request(el);
    await consent(el);
    const select = el.shadowRoot!.querySelector(
      '[data-testid="interval"]'
    ) as HTMLSelectElement;
    select.value = 'year';
    select.dispatchEvent(new Event('change'));
    await el.updateComplete;
    expect(el.shadowRoot!.querySelector('[data-testid="confirm"]')).not.to
      .exist;
    await request(el); // fixture still returns monthly quote
    expect(button(el, 'confirm').disabled).to.equal(true);
  });
  it('ignores a late quote after the target changes', async () => {
    let resolve!: (response: Response) => void;
    previewResponse = () =>
      new Promise((r) => {
        resolve = r;
      });
    const el = await mount();
    await open(el);
    button(el, 'preview').click();
    await waitUntil(() => calls('/plan-change-preview').length === 1);
    (el as any).choose('enterprise');
    resolve(json(quote));
    await new Promise((r) => setTimeout(r, 20));
    await el.updateComplete;
    expect(el.shadowRoot!.querySelector('[data-testid="confirm"]')).not.to
      .exist;
    expect(text(el)).to.include('Contact us about Enterprise');
  });
  it('requires refresh after a stale409 and never retries confirmation automatically', async () => {
    confirmResponse = async () =>
      json(
        {
          detail: {
            code: 'subscription_changed',
            operation_started: false,
            message: 'The subscription changed.',
          },
        },
        409
      );
    const el = await mount();
    await request(el);
    await consent(el);
    button(el, 'confirm').click();
    await waitUntil(() => !(el as any).busy);
    await el.updateComplete;
    expect(text(el))
      .to.include('subscription changed')
      .and.include('Refresh subscription status');
    expect(button(el, 'preview').disabled).to.equal(true);
    expect(calls('/plan-change-confirm')).to.have.length(1);
    await el.refresh();
    await el.updateComplete;
    expect(button(el, 'preview').disabled).to.equal(false);
  });
  it('reads on load but asks the server to reconcile when refresh is clicked', async () => {
    const el = await mount();
    expect(calls('/plan-change-options')).to.have.length(1);
    expect(calls('/plan-change-options?reconcile=true')).to.have.length(0);
    button(el, 'refresh').click();
    await waitUntil(() => !(el as any).loading);
    await el.updateComplete;
    expect(calls('/plan-change-options?reconcile=true')).to.have.length(1);
    expect(
      stub
        .getCalls()
        .every((c) => !c.args[1]?.method || c.args[1]?.method === 'GET')
    ).to.equal(true);
  });
  it('treats a lost confirmation response as uncertain, not a failed subscription', async () => {
    confirmResponse = async () => {
      throw new TypeError('Network unavailable');
    };
    const el = await mount();
    await request(el);
    await consent(el);
    button(el, 'confirm').click();
    await waitUntil(() => !(el as any).busy);
    await el.updateComplete;
    expect(button(el, 'preview').disabled).to.equal(true);
    expect(calls('/plan-change-confirm')).to.have.length(1);
    expect(text(el)).not.to.include('Plan changed:');
  });
  it('blocks quotes with capacity blockers even after manual consent', async () => {
    quote.blockers = [
      { code: 'seats', message: 'Reduce active users before switching.' },
    ];
    const el = await mount();
    await request(el);
    (el as any).accepted = true;
    await (el as any).confirm();
    expect(calls('/plan-change-confirm')).to.have.length(0);
    expect(text(el)).to.include('Reduce active users');
  });
  it('keeps Enterprise sales-led without requesting a checkout or quote', async () => {
    const el = await mount();
    await open(el);
    (el as any).choose('enterprise');
    await el.updateComplete;
    expect(
      el.shadowRoot!.querySelector('a.contact')?.getAttribute('href')
    ).to.equal('/request-demo');
    await (el as any).checkout();
    await (el as any).requestPreview();
    expect(calls('/create-checkout-session')).to.have.length(0);
    expect(calls('/plan-change-preview')).to.have.length(0);
  });
  it('uses checkout only for a free account and keeps subscription changes on preview', async () => {
    data.current_subscription = null;
    data.current_plan = plan('free');
    const el = await mount();
    await open(el);
    const navigate = sinon.stub(el as any, 'navigate');
    button(el, 'checkout').click();
    await waitUntil(() => !(el as any).busy);
    expect(calls('/create-checkout-session')).to.have.length(1);
    expect(calls('/plan-change-confirm')).to.have.length(0);
    expect(navigate).to.have.been.calledOnce;
    expect(text(el)).to.include('final amount and any taxes');
    expect(text(el)).to.include('self-hosted edition');
  });
  it('allows a current nonlegacy plan with a different billing interval', async () => {
    data.current_subscription!.plan_id = 'pro';
    data.current_subscription!.legacy = false;
    data.current_plan = plan('pro');
    const el = await mount();
    await open(el);
    const select = el.shadowRoot!.querySelector(
      '[data-testid="plan"]'
    ) as HTMLSelectElement;
    expect([...select.options].map((o) => o.value)).to.include('pro');
    select.value = 'pro';
    select.dispatchEvent(new Event('change'));
    await el.updateComplete;
    expect(button(el, 'preview').disabled).to.equal(true);
    const interval = el.shadowRoot!.querySelector(
      '[data-testid="interval"]'
    ) as HTMLSelectElement;
    interval.value = 'year';
    interval.dispatchEvent(new Event('change'));
    await el.updateComplete;
    expect(button(el, 'preview').disabled).to.equal(false);
    quote.target.interval = 'year';
    quote.target.total_amount_cents = 10000;
    await request(el);
    expect(
      JSON.parse(calls('/plan-change-preview')[0].args[1].body)
    ).to.deep.equal({ target_plan_id: 'pro', interval: 'year' });
  });
  it('explains held hosted credit and disabled extra spending', async () => {
    data.hosted_credit = {
      one_time_credit_usd: 0.5,
      lifetime_usage_usd: 0,
      lifetime_reserved_usd: 0.2,
      remaining_credit_usd: 0.3,
      coverage: 'known',
      extra_spending_enabled: false,
    };
    const el = await mount();
    await open(el);
    await showUsage(el);
    expect(text(el)).to.include('Available one-time credit: $0.30');
    expect(text(el)).to.include('$0.20 is reserved for calls in progress');
    expect(text(el)).to.include('Extra spending is off');
    expect(text(el)).to.include('Calls using your own provider keys continue');
  });

  it('does not invent a fresh hosted grant from unknown history', async () => {
    data.hosted_credit = {
      one_time_credit_usd: 0.5,
      lifetime_usage_usd: null,
      remaining_credit_usd: null,
      coverage: 'unknown',
    };
    const el = await mount();
    await open(el);
    await showUsage(el);
    expect(text(el)).to.include(
      'historical hosted balance has not been verified'
    );
    expect(text(el)).not.to.include('Available one-time credit: $0.50');
  });

  it('compares Free lifetime credit without granting it again each month', async () => {
    data.plans.unshift(
      plan('free', {
        name: 'Free',
        price_monthly: 0,
        price_annually: 0,
        features: {
          max_users: 1,
          max_agents: 3,
          byok_ingest_tokens_monthly: 10000000,
          hosted_models_monthly_limit_usd: null,
          hosted_credit_one_time_usd: 0.5,
          retention_days: 183,
        },
      })
    );
    data.hosted_credit = {
      one_time_credit_usd: 0.5,
      lifetime_usage_usd: 0.5,
      remaining_credit_usd: 0,
    };
    const el = await mount();
    await open(el);
    (el as any).choose('free');
    await el.updateComplete;
    await showUsage(el);
    expect(text(el))
      .to.include('$0.50 one-time credit')
      .and.include('Remaining lifetime credit: $0.00');
    expect(text(el)).to.include('Lifetime credit, not a monthly allowance');
    expect(text(el)).not.to.include('$0.50 / month');
    quote.target = {
      ...quote.target,
      plan_id: 'free',
      name: 'Free',
      total_amount_cents: 0,
      unit_amount_cents: 0,
    };
    await request(el);
    await consent(el);
    expect(button(el, 'confirm').disabled).to.equal(false);
    expect(text(el)).to.include('at the end of your current billing period');
  });
  it('labels recurring subtotals as excluding tax and discounts', async () => {
    const el = await mount();
    await request(el);
    expect(text(el)).to.include(
      'Recurring subtotals exclude discounts and tax'
    );
    expect(text(el)).to.include('Due now');
  });

  it('allows an exact immediate amount due without a separately itemized proration', async () => {
    quote.timing = 'immediate';
    quote.amount_due_now_cents = 1700;
    quote.proration_amount_cents = null;
    const el = await mount();
    await request(el);
    await consent(el);
    expect(button(el, 'confirm').disabled).to.equal(false);
    expect(text(el))
      .to.include('Not separately itemized')
      .and.include('$17.00');
  });
  it('allows paid cancellation to Free despite current overcapacity', async () => {
    data.plans.unshift(
      plan('free', { name: 'Free', price_monthly: 0, price_annually: 0 })
    );
    data.assessments.unshift({
      plan_id: 'free',
      fit: 'blocked',
      blockers: [
        { code: 'seats', message: 'Current users exceed Free capacity.' },
      ],
      advisories: [],
      months: [],
    });
    quote.target.plan_id = 'free';
    quote.target.name = 'Free';
    quote.target.total_amount_cents = 0;
    quote.advisories = [
      { code: 'seats', message: 'Current users exceed Free capacity.' },
    ];
    const el = await mount();
    await open(el);
    (el as any).choose('free');
    await el.updateComplete;
    expect(button(el, 'preview').disabled).to.equal(false);
    await request(el);
    await consent(el);
    expect(button(el, 'confirm').disabled).to.equal(false);
    expect(text(el)).to.include('Current users exceed Free capacity.');
  });
  it('retains and explicitly retries the original operation after a lost response and refresh', async () => {
    confirmResponse = async () => {
      throw new TypeError('Network unavailable');
    };
    const el = await mount();
    await request(el);
    await consent(el);
    button(el, 'confirm').click();
    await waitUntil(() => !(el as any).busy);
    await el.updateComplete;
    await el.refresh();
    await el.updateComplete;
    expect(button(el, 'preview').disabled).to.equal(true);
    expect(calls('/plan-change-confirm')).to.have.length(1);
    confirmResponse = undefined;
    button(el, 'recover').click();
    await waitUntil(() => !(el as any).busy);
    await el.updateComplete;
    expect(
      calls('/plan-change-confirm').map((c) => JSON.parse(c.args[1].body))
    ).to.deep.equal([
      { preview_id: 'signed-quote-a' },
      { preview_id: 'signed-quote-a' },
    ]);
    expect(text(el)).to.include('Plan change scheduled');
  });
  it('recovers the same actor and subscription command after component remount without an automatic write', async () => {
    localStorage.setItem(
      'accessToken',
      `header.${btoa(JSON.stringify({ sub: 'owner-a' }))}.signature`
    );
    confirmResponse = async () => {
      throw new TypeError('Network unavailable');
    };
    const first = await mount();
    await request(first);
    await consent(first);
    button(first, 'confirm').click();
    await waitUntil(() => !(first as any).busy);
    first.remove();
    const second = await mount();
    await open(second);
    expect(calls('/plan-change-confirm')).to.have.length(1);
    expect(button(second, 'recover')).to.exist;
    expect(button(second, 'preview').disabled).to.equal(true);
    sessionStorage.clear();
  });
  it('holds conflicting changes for operator recovery instead of discarding the operation', async () => {
    confirmResponse = async () =>
      json(
        {
          detail: {
            code: 'recovery_required',
            message: 'Provider state needs reconciliation.',
          },
        },
        409
      );
    const el = await mount();
    await request(el);
    await consent(el);
    button(el, 'confirm').click();
    await waitUntil(() => !(el as any).busy);
    await el.updateComplete;
    expect(text(el)).to.include('Billing reconciliation is required');
    expect(el.shadowRoot!.querySelector('[data-testid="recover"]')).not.to
      .exist;
    await el.refresh();
    await el.updateComplete;
    expect(button(el, 'preview').disabled).to.equal(true);
  });
  it('allows a fresh quote after a definitive catalog failure before any operation started', async () => {
    confirmResponse = async () =>
      json(
        {
          detail: {
            code: 'catalog_changed',
            operation_started: false,
            message: 'Plan terms changed.',
          },
        },
        409
      );
    const el = await mount();
    await request(el);
    await consent(el);
    button(el, 'confirm').click();
    await waitUntil(() => !(el as any).busy);
    await el.refresh();
    await el.updateComplete;
    expect(button(el, 'preview').disabled).to.equal(false);
    expect(el.shadowRoot!.querySelector('[data-testid="recover"]')).not.to
      .exist;
  });
  /**
   * The four states a reader can land in. Every sentence below is the
   * server's, rendered verbatim; the console decides only where it goes and
   * what stays selectable.
   */
  describe('what the server says about each candidate plan', () => {
    function optionFor(el: BillingPlanComparison, id: string) {
      return el.shadowRoot!.querySelector(
        `[data-testid="plan"] option[value="${id}"]`
      ) as HTMLOptionElement | null;
    }
    it('disables a plan the account cannot hold and repeats the reason with both numbers', async () => {
      data.plan_eligibility![0] = verdict('pro', {
        eligible: false,
        blockers: [
          {
            kind: 'members',
            current: 4,
            limit: 1,
            message:
              'You have 4 members; Pro includes 1. Remove 3 members to switch.',
          },
        ],
      });
      data.plans.unshift(
        plan('free', { name: 'Free', price_monthly: 0, price_annually: 0 })
      );
      data.plan_eligibility!.unshift(
        verdict('free', { requires_period: false })
      );
      const el = await mount();
      await open(el);
      expect(optionFor(el, 'pro')!.disabled).to.equal(true);
      expect(optionFor(el, 'pro')!.textContent).to.include('(not available)');
      expect(optionFor(el, 'free')!.disabled).to.equal(false);
      const listed = el.shadowRoot!.querySelector(
        '[data-testid="unavailable-plans"]'
      );
      expect(listed?.textContent).to.include(
        'You have 4 members; Pro includes 1. Remove 3 members to switch.'
      );
      expect((el as any).selectedPlan).to.equal('free');
    });
    it('refuses to act on a blocked plan even when it is selected directly', async () => {
      data.plan_eligibility![0] = verdict('pro', {
        eligible: false,
        blockers: [
          {
            kind: 'agents',
            current: 12,
            limit: 3,
            message:
              'You have 12 active agents; Pro includes 3. Deactivate 9 agents to switch. A plan change never deletes an agent.',
          },
        ],
      });
      const el = await mount();
      await open(el);
      (el as any).choose('pro');
      await el.updateComplete;
      expect(button(el, 'preview').disabled).to.equal(true);
      expect(
        el.shadowRoot!.querySelector('[data-testid="plan-blockers"]')
          ?.textContent
      ).to.include('Deactivate 9 agents to switch');
      await (el as any).requestPreview();
      expect(calls('/plan-change-preview')).to.have.length(0);
    });
    it('refuses a cancellation to Free while the server blocks Free', async () => {
      data.plans.unshift(
        plan('free', { name: 'Free', price_monthly: 0, price_annually: 0 })
      );
      data.plan_eligibility!.unshift(
        verdict('free', {
          requires_period: false,
          eligible: false,
          blockers: [
            {
              kind: 'members',
              current: 4,
              limit: 1,
              message:
                'You have 4 members; Free includes 1. Remove 3 members to switch.',
            },
          ],
        })
      );
      const el = await mount();
      await open(el);
      expect(data.current_subscription).to.not.equal(null);
      expect((el as any).selectedPlan).to.equal('pro');
      expect(optionFor(el, 'free')!.disabled).to.equal(true);
      (el as any).choose('free');
      await el.updateComplete;
      expect(button(el, 'preview').disabled).to.equal(true);
      await (el as any).requestPreview();
      expect(calls('/plan-change-preview')).to.have.length(0);
      expect(
        el.shadowRoot!.querySelector('[data-testid="unavailable-plans"]')
          ?.textContent
      ).to.include(
        'You have 4 members; Free includes 1. Remove 3 members to switch.'
      );
    });
    it('opens on no plan at all rather than on one it has just refused', async () => {
      data.plan_eligibility![0] = verdict('pro', {
        eligible: false,
        blockers: [
          {
            kind: 'members',
            current: 4,
            limit: 1,
            message:
              'You have 4 members; Pro includes 1. Remove 3 members to switch.',
          },
        ],
      });
      const el = await mount();
      await open(el);
      expect((el as any).selectedPlan).to.equal('');
      expect(el.shadowRoot!.querySelector('[data-testid="preview"]')).to.not
        .exist;
      expect(
        el.shadowRoot!.querySelector('[data-testid="unavailable-plans"]')
          ?.textContent
      ).to.include(
        'You have 4 members; Pro includes 1. Remove 3 members to switch.'
      );
    });
    it('sends a contact link nowhere but a path or an http(s) address', async () => {
      const el = await mount();
      await open(el);
      const href = () =>
        el
          .shadowRoot!.querySelector('[data-testid="contact-plans"] a')!
          .getAttribute('href');
      expect(href()).to.equal('/request-demo');
      for (const hostile of [
        'javascript:alert(1)',
        'jav\tascript:alert(1)',
        ' javascript:alert(1)',
        'data:text/html,<script></script>',
        '//evil.example.com/quote',
      ]) {
        (el as any).options.plan_eligibility[1].contact_url = hostile;
        (el as any).requestUpdate();
        await el.updateComplete;
        expect(href(), hostile).to.equal('/request-demo');
      }
      (el as any).options.plan_eligibility[1].contact_url =
        'https://sales.example.com/enterprise';
      (el as any).requestUpdate();
      await el.updateComplete;
      expect(href()).to.equal('https://sales.example.com/enterprise');
    });
    it('prefers the contact sentence the server composes', async () => {
      data.plan_eligibility![1] = verdict('enterprise', {
        purchasable: false,
        requires_period: false,
        contact_url: '/request-demo',
        contact_message:
          'Enterprise is scoped per deployment and installed with our team.',
      });
      const el = await mount();
      await open(el);
      const contact = el.shadowRoot!.querySelector(
        '[data-testid="contact-plans"]'
      );
      expect(contact?.textContent).to.include(
        'Enterprise is scoped per deployment and installed with our team.'
      );
      expect(contact?.textContent).to.not.include('priced per deployment');
    });
    it('keeps a quote-only plan out of the picker and offers contact instead', async () => {
      const el = await mount();
      await open(el);
      expect(optionFor(el, 'enterprise')).to.not.exist;
      expect(optionFor(el, 'pro')).to.exist;
      const contact = el.shadowRoot!.querySelector(
        '[data-testid="contact-plans"]'
      );
      expect(contact?.textContent).to.include(
        'Enterprise is priced per deployment.'
      );
      expect(contact?.querySelector('a')?.getAttribute('href')).to.equal(
        '/request-demo'
      );
    });
    it('offers no billing period for a plan with nothing to bill', async () => {
      data.plans.unshift(
        plan('free', { name: 'Free', price_monthly: 0, price_annually: 0 })
      );
      data.plan_eligibility!.unshift(
        verdict('free', { requires_period: false })
      );
      const el = await mount();
      await open(el);
      expect(el.shadowRoot!.querySelector('[data-testid="interval"]')).to.exist;
      (el as any).choose('free');
      await el.updateComplete;
      expect(el.shadowRoot!.querySelector('[data-testid="interval"]')).to.not
        .exist;
      expect((el as any).interval).to.equal('month');
      const price = (
        el.shadowRoot!.querySelector('[data-testid="price"]')?.textContent ?? ''
      ).replace(/\s+/g, ' ');
      expect(price).to.include('Free: $0.00.');
      expect(price).to.not.include('/ month');
    });
    it('says which records a shorter history deletes and when', async () => {
      data.plan_eligibility![0] = verdict('pro', {
        warnings: [
          {
            kind: 'retention',
            current: '2025-02-11T00:00:00+00:00',
            limit: 365,
            message:
              'Your usage records go back to 2025-02-11. Pro keeps 365 days, so records before 2025-09-16 will be deleted after the switch.',
          },
        ],
        retention: {
          ...verdict('pro').retention,
          oldest_record_at: '2025-02-11T00:00:00+00:00',
          oldest_record_class: 'usage',
          cutoff_at: '2025-09-16T00:00:00+00:00',
          affected: true,
          message:
            'Your usage records go back to 2025-02-11. Pro keeps 365 days, so records before 2025-09-16 will be deleted after the switch.',
        },
      });
      const el = await mount();
      await open(el);
      expect(
        el.shadowRoot!.querySelector('[data-testid="plan-warnings"]')
          ?.textContent
      ).to.include(
        'Your usage records go back to 2025-02-11. Pro keeps 365 days, so records before 2025-09-16 will be deleted after the switch.'
      );
      expect(optionFor(el, 'pro')!.disabled).to.equal(false);
      expect(button(el, 'preview').disabled).to.equal(false);
    });
    it('states a longer history as a benefit and a protected one as safe, never as a warning', async () => {
      data.plan_eligibility![0] = verdict('pro', {
        retention: {
          ...verdict('pro').retention,
          benefit_message:
            'Analytics history extends from 183 to 365 days. Records already deleted are not restored.',
        },
      });
      const el = await mount();
      await open(el);
      expect(
        el.shadowRoot!.querySelector('[data-testid="plan-benefit"]')
          ?.textContent
      ).to.include('Analytics history extends from 183 to 365 days.');
      expect(el.shadowRoot!.querySelector('[data-testid="plan-warnings"]')).to
        .not.exist;
    });
    it('renders nothing of its own when the server sends no verdicts', async () => {
      delete data.plan_eligibility;
      const el = await mount();
      await open(el);
      expect(el.shadowRoot!.querySelector('[data-testid="plan"]')).to.exist;
      expect(optionFor(el, 'enterprise')).to.not.exist;
      expect(el.shadowRoot!.querySelector('[data-testid="plan-blockers"]')).to
        .not.exist;
      expect(el.shadowRoot!.querySelector('[data-testid="unavailable-plans"]'))
        .to.not.exist;
    });
  });
});
