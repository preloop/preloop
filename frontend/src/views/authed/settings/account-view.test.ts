import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import '../../../components/view-header.ts';
import {
  BILLING_SUBSCRIPTION_CHANGED,
  invalidateApiCaches,
} from '../../../api';
import { Router } from '../../../router';
import './account-view';
import type { AccountView } from './account-view';

describe('AccountView', () => {
  let fetchStub: sinon.SinonStub;

  function copy(el: AccountView): string {
    return (el.shadowRoot?.textContent ?? '').replace(/\s+/g, ' ').trim();
  }

  /** The built-in usage card's cells, read by their visible label. */
  function usageCells(el: AccountView): Record<string, string> {
    const cells: Record<string, string> = {};
    el.shadowRoot?.querySelectorAll('.usage-metric').forEach((metric) => {
      const read = (selector: string) =>
        (metric.querySelector(selector)?.textContent ?? '')
          .replace(/\s+/g, ' ')
          .trim();
      cells[read('.usage-label')] = read('.usage-value');
    });
    return cells;
  }

  function json(data: unknown, status = 200) {
    return new Response(JSON.stringify(data), {
      status,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  function createFetchStub(
    opts: {
      billing?: boolean;
      accountFails?: boolean;
      subscription?: Record<string, unknown> | null;
      trial?: Record<string, unknown>;
      plans?: Record<string, unknown>[];
      extraCreditPricePerUsd?: number;
      freeTier?: boolean;
      ingestionQuota?: Record<string, unknown> | null;
      seats?: Record<string, unknown> | null;
      hostedOverrides?: Record<string, unknown>;
      canManageBilling?: boolean;
      effectivePlanId?: string;
      effectivePlan?: Record<string, unknown> | null;
      summaryPlan?: Record<string, unknown> | null;
      sessionArtifactUsage?: Record<string, unknown> | null;
    } = {}
  ) {
    return sinon
      .stub(window, 'fetch')
      .callsFake(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();
        const method = (init?.method || 'GET').toUpperCase();

        if (url.includes('/api/v1/account/details') && method === 'GET') {
          if (opts.accountFails) {
            return json({ detail: 'boom' }, 500);
          }
          return json({
            id: 'acc-1',
            organization_name: 'Acme Corp',
            created_at: '2026-01-01T00:00:00Z',
            updated_at: '2026-01-01T00:00:00Z',
          });
        }

        if (url.includes('/api/v1/account/details') && method === 'PATCH') {
          return json({
            id: 'acc-1',
            organization_name: 'New Org Name',
            created_at: '2026-01-01T00:00:00Z',
            updated_at: '2026-01-02T00:00:00Z',
          });
        }

        if (url.includes('/api/v1/account/session-artifacts/usage')) {
          if (!opts.sessionArtifactUsage) {
            return json({ detail: 'no usage in this test' }, 404);
          }
          return json(opts.sessionArtifactUsage);
        }

        if (url.includes('/api/v1/features')) {
          return json({
            plugins: [],
            features: { billing: opts.billing === true },
          });
        }

        if (url.includes('/api/v1/billing/sync-subscription')) {
          return json({ ok: true });
        }

        if (url.includes('/api/v1/billing/summary')) {
          return json({
            subscription: opts.freeTier
              ? null
              : opts.subscription === undefined
                ? {
                    plan_id: 'plan-pro',
                    status: 'active',
                    current_period_end: '2026-12-31T00:00:00Z',
                  }
                : opts.subscription,
            plan:
              opts.summaryPlan !== undefined
                ? opts.summaryPlan
                : opts.freeTier
                  ? { id: 'free', name: 'Free', features: { max_agents: 3 } }
                  : {
                      id: 'plan-pro',
                      name: 'Pro Plan',
                      features: { max_agents: -1 },
                    },
            ...(opts.effectivePlanId === undefined
              ? {}
              : { effective_plan_id: opts.effectivePlanId }),
            ...(opts.effectivePlan === undefined
              ? {}
              : { effective_plan: opts.effectivePlan }),
            ingestion_quota: opts.ingestionQuota ?? null,
            seats: opts.seats ?? null,
            trial: opts.trial ?? {
              is_trialing: false,
              days: 0,
              requires_payment_method: false,
              hosted_model_hard_cap_usd: null,
            },
            hosted_models: {
              billing_period_start: '2026-06-01T00:00:00Z',
              billing_period_end: '2026-06-30T00:00:00Z',
              included_limit_usd: 100,
              active_limit_usd: 100,
              current_usage_usd: 25,
              remaining_limit_usd: 75,
              extra_credit_price_per_usd: opts.extraCreditPricePerUsd ?? 1.2,
              models: [],
              ...(opts.hostedOverrides ?? {}),
            },
          });
        }

        if (url.includes('/api/v1/billing/plan-change-options')) {
          const legacy = opts.subscription?.plan_id === 'teams';
          return json({
            can_manage_billing: opts.canManageBilling ?? true,
            switching_enabled: true,
            current_subscription: opts.freeTier
              ? null
              : {
                  id: 'subscription-local',
                  plan_id: legacy ? 'teams' : 'plan-pro',
                  status: 'active',
                  interval: 'month',
                  quantity: 1,
                  currency: 'usd',
                  unit_amount_cents: legacy ? 2900 : 1000,
                  total_amount_cents: legacy ? 2900 : 1000,
                  current_period_end: '2030-01-01T00:00:00Z',
                  legacy,
                  revision: 'a',
                  pending_change: null,
                  cancel_at_period_end: false,
                },
            current_plan: {
              id: legacy ? 'teams' : 'plan-pro',
              name: legacy ? 'Legacy Teams' : 'Pro',
              is_legacy: legacy,
              features: {},
            },
            plans: opts.plans ?? [
              {
                id: 'enterprise',
                name: 'Enterprise',
                price_monthly: null,
                price_annually: null,
                features: {},
                capabilities: [],
                purchasable: false,
              },
            ],
            monthly_usage: [],
            current_usage: {
              active_users: 1,
              active_agents: 1,
              pending_invitations: 0,
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

        if (url.includes('/api/v1/billing/plans')) {
          return json(
            opts.plans ?? [
              {
                id: 'plan-enterprise',
                name: 'Enterprise',
                price_monthly: 99,
                price_annually: 990,
                features: {},
              },
            ]
          );
        }

        if (url.includes('/api/v1/billing/custom-plans')) {
          return json([]);
        }

        return json({ detail: `Unhandled: ${method} ${url}` }, 500);
      });
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
  });

  it('leaves the emergency controls to the page that owns them', async () => {
    // The kill switch moved to /console/settings/emergency. A page that asks
    // about a subscription is not where an operator halts an account.
    fetchStub = createFetchStub({ billing: true });
    const element = await fixture<AccountView>(
      html`<account-view></account-view>`
    );
    await waitUntil(() => !(element as any)._loading, 'load');
    await element.updateComplete;

    expect(copy(element)).to.not.contain('Emergency Controls');
    expect(copy(element)).to.not.contain('Block new agent requests');
    expect(
      fetchStub
        .getCalls()
        .some((call) => String(call.args[0]).includes('/kill-switch'))
    ).to.equal(false);
  });

  it('renders organization details after load (non-billing edition)', async () => {
    fetchStub = createFetchStub({ billing: false });
    const element = (await fixture(
      html`<account-view></account-view>`
    )) as AccountView;

    await waitUntil(
      () => !(element as any)._loading,
      'Account view did not finish loading'
    );
    await element.updateComplete;

    expect(element.shadowRoot?.textContent).to.contain('Organization Details');
    expect((element as any).organizationName).to.equal('Acme Corp');
    // No billing/subscription section in the open-source edition.
    expect(element.shadowRoot?.textContent).to.not.contain('Manage in Stripe');
  });

  it('renders subscription information in the billing edition', async () => {
    fetchStub = createFetchStub({ billing: true });
    const element = (await fixture(
      html`<account-view></account-view>`
    )) as AccountView;

    await waitUntil(
      () => !(element as any)._loading,
      'Account view did not finish loading'
    );
    await element.updateComplete;

    expect(element.shadowRoot?.textContent).to.contain('Pro Plan');
    expect(element.shadowRoot?.textContent).to.contain('Manage in Stripe');
    expect((element as any)._billingSummary).to.not.be.null;
  });

  it('offers a plan to a Free account instead of a dead portal button', async () => {
    // The provider portal manages a subscription. Free has none, so the
    // button sat there disabled where the one useful action belongs.
    fetchStub = createFetchStub({ billing: true, freeTier: true });
    const element = (await fixture(
      html`<account-view></account-view>`
    )) as AccountView;

    await waitUntil(() => !(element as any)._loading, 'load');
    await element.updateComplete;

    expect(element.shadowRoot?.textContent).to.not.contain('Manage in Stripe');
    const choose = element.shadowRoot?.querySelector(
      '[data-testid="choose-a-plan"]'
    ) as HTMLElement | null;
    expect(choose, 'expected a plan action').to.exist;
    expect(choose?.hasAttribute('disabled')).to.be.false;
  });

  it('sends "Choose a plan" to the plan page', async () => {
    fetchStub = createFetchStub({ billing: true, freeTier: true });
    const element = (await fixture(
      html`<account-view></account-view>`
    )) as AccountView;

    await waitUntil(() => !(element as any)._loading, 'load');
    await element.updateComplete;

    const go = sinon.stub(Router, 'go').returns(true);
    try {
      (
        element.shadowRoot?.querySelector(
          '[data-testid="choose-a-plan"]'
        ) as HTMLElement
      ).click();
      await element.updateComplete;
      expect(go.lastCall.args[0]).to.equal('/console/settings/plan');
    } finally {
      go.restore();
    }
  });

  it('offers a subscribed account the plan page beside the portal', async () => {
    fetchStub = createFetchStub({ billing: true });
    const element = (await fixture(
      html`<account-view></account-view>`
    )) as AccountView;

    await waitUntil(() => !(element as any)._loading, 'load');
    await element.updateComplete;

    const go = sinon.stub(Router, 'go').returns(true);
    try {
      (
        element.shadowRoot?.querySelector(
          '[data-testid="view-plans"]'
        ) as HTMLElement
      ).click();
      await element.updateComplete;
      expect(go.lastCall.args[0]).to.equal('/console/settings/plan');
    } finally {
      go.restore();
    }
  });

  it('falls back to a full page load where no router claimed the path', async () => {
    fetchStub = createFetchStub({ billing: true, freeTier: true });
    const element = (await fixture(
      html`<account-view></account-view>`
    )) as AccountView;

    await waitUntil(() => !(element as any)._loading, 'load');
    await element.updateComplete;

    const go = sinon.stub(Router, 'go').returns(false);
    const navigate = sinon.stub(element as any, '_navigate');
    try {
      (
        element.shadowRoot?.querySelector(
          '[data-testid="choose-a-plan"]'
        ) as HTMLElement
      ).click();
      await element.updateComplete;
      expect(navigate).to.have.been.calledWith('/console/settings/plan');
    } finally {
      go.restore();
    }
  });

  it('keeps the portal button for a subscription, disabled only without the permission', async () => {
    fetchStub = createFetchStub({ billing: true, canManageBilling: false });
    const element = (await fixture(
      html`<account-view></account-view>`
    )) as AccountView;

    await waitUntil(() => !(element as any)._loading, 'load');
    await element.updateComplete;

    const manage = element.shadowRoot?.querySelector(
      '[data-testid="manage-in-stripe"]'
    ) as HTMLElement | null;
    expect(manage, 'expected the portal button').to.exist;
    expect(element.shadowRoot?.querySelector('[data-testid="choose-a-plan"]'))
      .to.not.exist;
  });

  it('shows an error alert when account details fail to load', async () => {
    fetchStub = createFetchStub({ accountFails: true });
    const element = (await fixture(
      html`<account-view></account-view>`
    )) as AccountView;

    await waitUntil(
      () => !(element as any)._loading,
      'Account view did not finish loading'
    );
    await element.updateComplete;

    expect((element as any)._error).to.be.a('string');
    const alert = element.shadowRoot?.querySelector(
      'sl-alert[variant="danger"]'
    );
    expect(alert).to.exist;
  });

  it('saves the organization name', async () => {
    fetchStub = createFetchStub({ billing: false });
    const element = (await fixture(
      html`<account-view></account-view>`
    )) as AccountView;

    await waitUntil(() => !(element as any)._loading, 'load');

    (element as any).organizationName = 'New Org Name';
    await (element as any)._handleSaveOrganization();
    await element.updateComplete;

    expect((element as any).orgSuccessMessage).to.contain('saved successfully');
    const patchCall = fetchStub
      .getCalls()
      .find((c) => (c.args[1]?.method || 'GET').toUpperCase() === 'PATCH');
    expect(patchCall, 'expected a PATCH request').to.exist;
  });

  // The persisted plan row keeps the name a grandfathered plan was sold
  // under, so a catalog sync can never rewrite a live contract's terms. The
  // catalog carries the current public name, and the server resolves it into
  // `effective_plan.name`. When the two disagree the catalog wins on screen,
  // which is what stops a withdrawn plan from announcing itself as "Teams"
  // after the catalog renamed it to "Legacy Teams".
  it('names a grandfathered legacy plan from the catalog, not the stored row', async () => {
    fetchStub = createFetchStub({
      billing: true,
      subscription: {
        plan_id: 'teams',
        status: 'active',
        current_period_end: '2030-01-01T00:00:00Z',
      },
      summaryPlan: { id: 'teams', name: 'Teams', features: { max_agents: -1 } },
      effectivePlanId: 'teams',
      effectivePlan: { id: 'teams', name: 'Legacy Teams' },
    });
    const element = (await fixture(
      html`<account-view></account-view>`
    )) as AccountView;

    await waitUntil(() => !(element as any)._loading, 'load');
    await element.updateComplete;

    const rendered = element.shadowRoot?.querySelector('.plan-name');
    expect(rendered?.textContent?.trim()).to.equal('Legacy Teams');
  });

  // The exact surface the report came from: the subscription card at the top
  // of the page, the one that read "Teams  active  Renews on Sep 27" while
  // the section below already said "Legacy Teams (grandfathered)". One name
  // on every surface means this card, the status chip and the renewal line
  // all have to be right at once, so they are asserted together.
  it('shows the catalog name on the top subscription card, beside the status and renewal date', async () => {
    fetchStub = createFetchStub({
      billing: true,
      subscription: {
        plan_id: 'teams',
        status: 'active',
        current_period_end: '2030-09-27T00:00:00Z',
      },
      summaryPlan: { id: 'teams', name: 'Teams', features: { max_agents: -1 } },
      effectivePlanId: 'teams',
      effectivePlan: { id: 'teams', name: 'Legacy Teams' },
    });
    const element = (await fixture(
      html`<account-view></account-view>`
    )) as AccountView;

    await waitUntil(() => !(element as any)._loading, 'load');
    await element.updateComplete;

    const card = element.shadowRoot?.querySelector('.card.current-plan');
    expect(card, 'expected the subscription card').to.exist;
    expect(card?.querySelector('.plan-name')?.textContent?.trim()).to.equal(
      'Legacy Teams'
    );
    expect(card?.querySelector('.status-chip')?.textContent?.trim()).to.equal(
      'active'
    );
    expect(card?.querySelector('.date')?.textContent).to.contain('Renews on');
    // The retired name must not survive anywhere on the card, and "Teams"
    // on its own only appears as the tail of "Legacy Teams".
    const cardText = card?.textContent ?? '';
    expect(cardText.split('Teams').length - 1).to.equal(
      cardText.split('Legacy Teams').length - 1
    );
  });

  it('falls back to the stored plan name when the server sends no effective plan', async () => {
    fetchStub = createFetchStub({
      billing: true,
      subscription: {
        plan_id: 'plan-pro',
        status: 'active',
        current_period_end: '2030-01-01T00:00:00Z',
      },
    });
    const element = (await fixture(
      html`<account-view></account-view>`
    )) as AccountView;

    await waitUntil(() => !(element as any)._loading, 'load');
    await element.updateComplete;

    const rendered = element.shadowRoot?.querySelector('.plan-name');
    expect(rendered?.textContent?.trim()).to.equal('Pro Plan');
  });

  it('reports an ended trial as Free from the effective plan fields (D13)', async () => {
    fetchStub = createFetchStub({
      billing: true,
      subscription: {
        plan_id: 'plan-pro',
        status: 'trialing',
        current_period_end: '2025-07-27T00:00:00Z',
      },
      trial: {
        is_trialing: false,
        is_expired: true,
        ended_at: '2025-07-27T00:00:00Z',
        days: 0,
        requires_payment_method: false,
        hosted_model_hard_cap_usd: 2,
      },
      effectivePlanId: 'free',
      effectivePlan: { id: 'free', name: 'Free' },
      summaryPlan: null,
      hostedOverrides: {
        included_limit_usd: null,
        active_limit_usd: 0.5,
        remaining_limit_usd: 0.5,
        one_time_credit_usd: 0.5,
      },
    });
    const element = (await fixture(
      html`<account-view></account-view>`
    )) as AccountView;

    await waitUntil(() => !(element as any)._loading, 'load');
    await element.updateComplete;

    const text = copy(element);
    expect(text).to.contain('trial ended on Jul 27');
    expect(text).to.contain('You are on the Free plan');
    // The trial cap and the monthly allowance describe a plan this account no
    // longer has. Printing either is the mis-sell the founder rejected.
    expect(text).to.not.contain('Trial cap');
    expect(text).to.not.contain('Monthly allowance');
    expect(text).to.not.contain('trialing');
    expect(text).to.not.contain('Renews on');
    expect(text).to.contain('One-time credit');
  });

  it('treats a past trial period end as expired without the new fields (D13)', async () => {
    fetchStub = createFetchStub({
      billing: true,
      subscription: {
        plan_id: 'plan-pro',
        status: 'trialing',
        current_period_end: '2025-07-27T00:00:00Z',
      },
      trial: {
        is_trialing: true,
        days: 0,
        requires_payment_method: false,
        hosted_model_hard_cap_usd: 2,
      },
    });
    const element = (await fixture(
      html`<account-view></account-view>`
    )) as AccountView;

    await waitUntil(() => !(element as any)._loading, 'load');
    await element.updateComplete;

    const text = copy(element);
    expect(text).to.contain('Your Pro Plan trial ended on Jul 27');
    expect(text).to.contain('You are on the Free plan');
    expect(text).to.not.contain('Trial cap');
    expect(text).to.not.contain('Renews on');
    // Nothing verified the Free credit here, so no allowance is printed at
    // all rather than reprinting the ended trial's figures.
    expect(text).to.not.contain('Monthly allowance');
    expect(text).to.not.contain('Current active cap');
    expect(text).to.contain('Allowances from the ended trial are not shown');
  });

  it('omits the date clause when an expired trial has no ended date', async () => {
    fetchStub = createFetchStub({
      billing: true,
      subscription: null,
      trial: {
        is_trialing: false,
        is_expired: true,
        days: 0,
        requires_payment_method: false,
        hosted_model_hard_cap_usd: 2,
      },
    });
    const element = (await fixture(
      html`<account-view></account-view>`
    )) as AccountView;

    await waitUntil(() => !(element as any)._loading, 'load');
    await element.updateComplete;

    const text = copy(element);
    expect(text).to.contain('trial ended. You are on the Free plan');
    expect(text).to.not.contain('Unknown');
    expect(text).to.not.contain('ended on');
  });

  it('still says "Renews on" for a future period end', async () => {
    fetchStub = createFetchStub({ billing: true });
    const element = (await fixture(
      html`<account-view></account-view>`
    )) as AccountView;

    await waitUntil(() => !(element as any)._loading, 'load');
    await element.updateComplete;

    expect(element.shadowRoot?.textContent).to.contain('Renews on');
  });

  it('says a trial ends, never renews, for a future period end (D13)', async () => {
    fetchStub = createFetchStub({
      billing: true,
      trial: {
        is_trialing: true,
        days: 14,
        requires_payment_method: false,
        hosted_model_hard_cap_usd: 2,
      },
    });
    const element = (await fixture(
      html`<account-view></account-view>`
    )) as AccountView;

    await waitUntil(() => !(element as any)._loading, 'load');
    await element.updateComplete;

    const text = copy(element);
    expect(text).to.contain('Trial ends on');
    expect(text).to.contain('Trial cap for built-in models: $2.00');
    expect(text).to.not.contain('Renews on');
  });

  it('hides the interval toggle and grid when no plans render (D13)', async () => {
    fetchStub = createFetchStub({ billing: true, plans: [] });
    const element = (await fixture(
      html`<account-view></account-view>`
    )) as AccountView;

    await waitUntil(() => !(element as any)._loading, 'load');
    await element.updateComplete;

    expect(element.shadowRoot?.querySelector('billing-toggle')).to.not.exist;
    expect(element.shadowRoot?.querySelector('.plans-grid')).to.not.exist;
  });

  it('keeps the plan picker off the account page', async () => {
    // The picker, the quote and the confirmation live on the plan page now.
    // This page states which plan is current and links to the rest.
    fetchStub = createFetchStub({ billing: true });
    const element = (await fixture(
      html`<account-view></account-view>`
    )) as AccountView;

    await waitUntil(() => !(element as any)._loading, 'load');
    await element.updateComplete;

    expect(element.shadowRoot?.querySelector('billing-plan-comparison')).to.not
      .exist;
    expect(element.shadowRoot?.querySelector('pricing-card')).not.to.exist;
    expect(element.shadowRoot?.querySelector('[data-testid="view-plans"]')).to
      .exist;
  });

  it('never offers an opt-in for extra usage that does not exist (D13)', async () => {
    fetchStub = createFetchStub({ billing: true, extraCreditPricePerUsd: 1 });
    const element = (await fixture(
      html`<account-view></account-view>`
    )) as AccountView;

    await waitUntil(() => !(element as any)._loading, 'load');
    await element.updateComplete;

    expect(usageCells(element)['Extra credits']).to.equal(
      'Usage stops at the allowance.'
    );
    const text = copy(element);
    expect(text).to.not.contain('opt in');
    expect(text).to.not.contain('$1.00 per');
  });

  it('quotes no price for extra usage even when the server sends one', async () => {
    // A marked-up rate is still a price for something nobody can buy.
    fetchStub = createFetchStub({ billing: true, extraCreditPricePerUsd: 1.2 });
    const element = (await fixture(
      html`<account-view></account-view>`
    )) as AccountView;

    await waitUntil(() => !(element as any)._loading, 'load');
    await element.updateComplete;

    expect(usageCells(element)['Extra credits']).to.equal(
      'Usage stops at the allowance.'
    );
    expect(copy(element)).to.not.contain('per $1.00 of additional usage');
  });

  it('shows $0.00 and the whole cap when a capped plan has no usage yet', async () => {
    // The founder's Legacy Teams account: $10 allowance, $10 cap, nothing
    // spent, so the server sends no usage figure at all. "Not configured"
    // there reads as a broken plan; the account simply has not spent.
    fetchStub = createFetchStub({
      billing: true,
      hostedOverrides: {
        included_limit_usd: 10,
        active_limit_usd: 10,
        current_usage_usd: null,
        remaining_limit_usd: null,
      },
    });
    const element = (await fixture(
      html`<account-view></account-view>`
    )) as AccountView;

    await waitUntil(() => !(element as any)._loading, 'load');
    await element.updateComplete;

    const cells = usageCells(element);
    expect(cells['Usage so far']).to.equal('$0.00');
    expect(cells['Remaining before cap']).to.equal('$10');
    expect(cells['Current active cap']).to.equal('$10');
    expect(copy(element)).to.not.contain('Not configured');
  });

  it('reports a literal zero usage as $0.00, not as an absent figure', async () => {
    fetchStub = createFetchStub({
      billing: true,
      hostedOverrides: {
        included_limit_usd: 10,
        active_limit_usd: 10,
        current_usage_usd: 0,
        remaining_limit_usd: 10,
      },
    });
    const element = (await fixture(
      html`<account-view></account-view>`
    )) as AccountView;

    await waitUntil(() => !(element as any)._loading, 'load');
    await element.updateComplete;

    const cells = usageCells(element);
    expect(cells['Usage so far']).to.equal('$0.00');
    expect(cells['Remaining before cap']).to.equal('$10');
  });

  it('still says "Not configured" when the plan has no allowance and no cap', async () => {
    fetchStub = createFetchStub({
      billing: true,
      hostedOverrides: {
        included_limit_usd: null,
        active_limit_usd: null,
        current_usage_usd: null,
        remaining_limit_usd: null,
      },
    });
    const element = (await fixture(
      html`<account-view></account-view>`
    )) as AccountView;

    await waitUntil(() => !(element as any)._loading, 'load');
    await element.updateComplete;

    const cells = usageCells(element);
    expect(cells['Usage so far']).to.equal('Not configured');
    expect(cells['Remaining before cap']).to.equal('Not configured');
    expect(cells['Monthly allowance']).to.equal('Not configured');
  });

  it('measures zero spend against the free tier one-time credit', async () => {
    // Free has a one-time credit instead of a monthly cap, so the cap-based
    // check alone would call an untouched grant "Not configured".
    fetchStub = createFetchStub({
      billing: true,
      freeTier: true,
      hostedOverrides: {
        included_limit_usd: null,
        active_limit_usd: null,
        current_usage_usd: null,
        remaining_limit_usd: null,
        one_time_credit_usd: 5,
      },
    });
    const element = (await fixture(
      html`<account-view></account-view>`
    )) as AccountView;

    await waitUntil(() => !(element as any)._loading, 'load');
    await element.updateComplete;

    const cells = usageCells(element);
    expect(cells['One-time credit']).to.equal('$5.00');
    expect(cells['Usage so far']).to.equal('$0.00');
    // No cap to subtract from, so nothing is invented for the cap cell.
    expect(cells['Remaining before cap']).to.equal('Not configured');
  });
  it('keeps the sales-led plan (null price) and drops only the $0 plan', async () => {
    fetchStub = createFetchStub({
      billing: true,
      plans: [
        {
          id: 'free',
          name: 'Free',
          price_monthly: 0,
          price_annually: 0,
          features: {},
        },
        {
          id: 'pro',
          name: 'Pro',
          price_monthly: 10,
          price_annually: 100,
          features: {},
        },
        {
          id: 'enterprise',
          name: 'Enterprise',
          price_monthly: null,
          price_annually: null,
          features: {},
        },
      ],
    });
    const element = (await fixture(
      html`<account-view></account-view>`
    )) as AccountView;
    await waitUntil(() => !(element as any)._loading, 'load');
    await element.updateComplete;

    const ids = (element as any)._publicPlans.map((p: any) => p.id);
    // Enterprise has no price, but it is still a plan you can move to. The
    // old filter dropped it along with Free and left no route to sales.
    expect(ids).to.deep.equal(['pro', 'enterprise']);
  });

  it('describes the free hosted credit as one-time, not monthly', async () => {
    fetchStub = createFetchStub({
      billing: true,
      freeTier: true,
      hostedOverrides: { one_time_credit_usd: 0.5, included_limit_usd: null },
    });
    const element = (await fixture(
      html`<account-view></account-view>`
    )) as AccountView;
    await waitUntil(() => !(element as any)._loading, 'load');
    await element.updateComplete;

    const text = copy(element);
    expect(text).to.contain('One-time credit');
    expect(text).to.contain('$0.50');
    expect(text).to.contain('does not reset');
    // Calling a one-time grant an allowance is the specific mis-sell here.
    expect(text).to.not.contain('Monthly allowance');
  });

  it('calls the paid hosted grant a monthly allowance', async () => {
    fetchStub = createFetchStub({ billing: true });
    const element = (await fixture(
      html`<account-view></account-view>`
    )) as AccountView;
    await waitUntil(() => !(element as any)._loading, 'load');
    await element.updateComplete;

    const text = copy(element);
    expect(text).to.contain('Monthly allowance');
    expect(text).to.not.contain('One-time credit');
  });

  it('renders the analysis quota meter and never implies agents stop', async () => {
    fetchStub = createFetchStub({
      billing: true,
      ingestionQuota: {
        plan_id: 'pro',
        quota_tokens: 200000000,
        used_tokens: 200000000,
        remaining_tokens: 0,
        is_unlimited: false,
        over_quota: true,
        degraded_analytics: true,
        usage_ratio: 1,
        approaching_limit: false,
        period_start: '2026-08-01T00:00:00Z',
        period_end: '2026-09-01T00:00:00Z',
      },
    });
    const element = (await fixture(
      html`<account-view></account-view>`
    )) as AccountView;
    await waitUntil(() => !(element as any)._loading, 'load');
    await element.updateComplete;

    const text = copy(element);
    expect(text).to.contain('Analysis quota');
    expect(text).to.contain('200M of 200M tokens');
    // Product-safety rule: exhausting the BYOK quota degrades analytics
    // detail and nothing else. Copy that says otherwise is a bug.
    expect(text).to.contain('Your agents keep running');
    expect(text).to.contain('every policy still applies');
    expect(text).to.not.match(/blocked|suspended|stopped|disabled/i);
  });

  it('omits the quota meter entirely when the quota is unlimited', async () => {
    fetchStub = createFetchStub({
      billing: true,
      ingestionQuota: {
        plan_id: 'enterprise',
        quota_tokens: -1,
        used_tokens: 5,
        remaining_tokens: null,
        is_unlimited: true,
        over_quota: false,
        degraded_analytics: false,
        usage_ratio: 0,
        approaching_limit: false,
        period_start: '2026-08-01T00:00:00Z',
        period_end: '2026-09-01T00:00:00Z',
      },
    });
    const element = (await fixture(
      html`<account-view></account-view>`
    )) as AccountView;
    await waitUntil(() => !(element as any)._loading, 'load');
    await element.updateComplete;

    expect(element.shadowRoot?.querySelector('.quota-bar')).to.not.exist;
  });

  it('shows seats as capacity and quotes the add-on only when over', async () => {
    fetchStub = createFetchStub({
      billing: true,
      seats: {
        active_users: 22,
        included_users: 20,
        max_users: 50,
        over_included: true,
        seat_addon: {
          price_per_user_monthly: 15,
          price_per_user_annually: 150,
          max_users: 50,
        },
      },
    });
    const element = (await fixture(
      html`<account-view></account-view>`
    )) as AccountView;
    await waitUntil(() => !(element as any)._loading, 'load');
    await element.updateComplete;

    const text = copy(element);
    expect(text).to.contain('22 of 20');
    expect(text).to.contain('Agents are unlimited');
    expect(text).to.contain('$15 each per month');
  });

  it('omits the seat line when the plan has no seat bracket', async () => {
    fetchStub = createFetchStub({
      billing: true,
      seats: {
        active_users: 3,
        included_users: null,
        max_users: null,
        over_included: false,
        seat_addon: null,
      },
    });
    const element = (await fixture(
      html`<account-view></account-view>`
    )) as AccountView;
    await waitUntil(() => !(element as any)._loading, 'load');
    await element.updateComplete;

    expect(copy(element)).to.not.contain('included users');
  });
  it('names the legacy plan a per-seat account is on', async () => {
    // The panel that explains grandfathering moved to the plan page; what
    // this page owes a legacy account is the name of the plan it holds.
    fetchStub = createFetchStub({
      billing: true,
      subscription: {
        plan_id: 'teams',
        status: 'active',
        current_period_end: '2026-12-31T00:00:00Z',
      },
      summaryPlan: { id: 'teams', name: 'Legacy Teams', features: {} },
      effectivePlan: { id: 'teams', name: 'Legacy Teams' },
    });
    const element = (await fixture(
      html`<account-view></account-view>`
    )) as AccountView;
    await waitUntil(() => !(element as any)._loading, 'load');
    await element.updateComplete;
    expect(copy(element)).to.contain('Legacy Teams');
    expect(
      fetchStub
        .getCalls()
        .some((call) =>
          String(call.args[0]).includes('create-checkout-session')
        )
    ).to.equal(false);
  });

  it('does not label a current plan as grandfathered', async () => {
    fetchStub = createFetchStub({ billing: true });
    const element = (await fixture(
      html`<account-view></account-view>`
    )) as AccountView;
    await waitUntil(() => !(element as any)._loading, 'load');
    await element.updateComplete;
    expect(element.shadowRoot?.querySelector('.legacy-plan-note')).to.not.exist;
  });
  it('does not synchronize or mutate subscriptions merely by opening Account', async () => {
    fetchStub = createFetchStub({ billing: true });
    const element = await fixture<AccountView>(
      html`<account-view></account-view>`
    );
    await waitUntil(() => !(element as any)._loading);
    await element.updateComplete;
    expect(
      fetchStub
        .getCalls()
        .some((c) => String(c.args[0]).includes('sync-subscription'))
    ).to.equal(false);
    expect(
      fetchStub.getCalls().some((c) => c.args[1]?.method === 'POST')
    ).to.equal(false);
  });
  it('keeps portal mutations disabled for a member without billing permission', async () => {
    fetchStub = createFetchStub({ billing: true, canManageBilling: false });
    const element = await fixture<AccountView>(
      html`<account-view></account-view>`
    );
    await waitUntil(() => !(element as any)._loading);
    await element.updateComplete;
    await (element as any)._handleManageSubscription();
    expect(
      fetchStub
        .getCalls()
        .some((c) => String(c.args[0]).includes('create-portal-session'))
    ).to.equal(false);
    expect(
      element
        .shadowRoot!.querySelector('.current-plan sl-button')
        ?.hasAttribute('disabled')
    ).to.equal(true);
  });

  function summaryGets(): number {
    return fetchStub.getCalls().filter((call) => {
      const url = String(call.args[0]);
      const method = (call.args[1]?.method || 'GET').toUpperCase();
      return url.includes('/api/v1/billing/summary') && method === 'GET';
    }).length;
  }

  it('re-reads the billing summary when the checkout refresh event is on window', async () => {
    fetchStub = createFetchStub({ billing: true });
    const element = await fixture<AccountView>(
      html`<account-view></account-view>`
    );
    await waitUntil(() => !(element as any)._loading, 'load');
    await element.updateComplete;
    const before = summaryGets();

    window.dispatchEvent(new Event(BILLING_SUBSCRIPTION_CHANGED));
    await waitUntil(
      () => summaryGets() === before + 1,
      'window dispatch should fetch summary once more'
    );
    expect(summaryGets()).to.equal(before + 1);
  });

  it('ignores a composed child event: only a window dispatch refreshes', async () => {
    fetchStub = createFetchStub({ billing: true });
    const element = await fixture<AccountView>(
      html`<account-view></account-view>`
    );
    await waitUntil(() => !(element as any)._loading, 'load');
    await element.updateComplete;
    const card = element.shadowRoot!.querySelector('.current-plan');
    expect(card, 'expected the current plan card').to.exist;
    const before = summaryGets();

    card!.dispatchEvent(
      new CustomEvent(BILLING_SUBSCRIPTION_CHANGED, {
        bubbles: true,
        composed: true,
      })
    );
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(summaryGets(), 'a child event fetches nothing').to.equal(before);

    window.dispatchEvent(new Event(BILLING_SUBSCRIPTION_CHANGED));
    await waitUntil(
      () => summaryGets() === before + 1,
      'the window dispatch should fetch summary once'
    );
    expect(summaryGets()).to.equal(before + 1);
  });

  it('renders session artifact usage as used, budget, and per kind', async () => {
    fetchStub = createFetchStub({
      billing: false,
      sessionArtifactUsage: {
        used_bytes: 409600,
        budget_bytes: 1048576,
        by_kind: { screenshot: 0, recording: 409600 },
        evicted_count_30d: 1,
      },
    });
    const element = await fixture<AccountView>(
      html`<account-view></account-view>`
    );
    await waitUntil(() => !(element as any)._loading, 'load');
    await element.updateComplete;

    const row = element.shadowRoot?.querySelector(
      '[data-testid="session-artifact-usage"]'
    );
    expect(row, 'expected the session artifact usage row').to.exist;
    const text = (row?.textContent ?? '').replace(/\s+/g, ' ');
    expect(text).to.contain('400 KiB');
    expect(text).to.contain('1 MiB');
    expect(text).to.contain('0 B');
    const cells = usageCells(element);
    expect(cells['Screenshots']).to.equal('0 B');
    expect(cells['Recordings']).to.equal('400 KiB');
    expect(cells['Used']).to.contain('400 KiB');
    expect(cells['Used']).to.contain('1 MiB');
  });

  it('stops listening after disconnect so a window dispatch fetches nothing', async () => {
    fetchStub = createFetchStub({ billing: true });
    const element = await fixture<AccountView>(
      html`<account-view></account-view>`
    );
    await waitUntil(() => !(element as any)._loading, 'load');
    await element.updateComplete;
    const before = summaryGets();

    element.remove();
    window.dispatchEvent(new Event(BILLING_SUBSCRIPTION_CHANGED));
    await new Promise((resolve) => {
      setTimeout(resolve, 50);
    });
    expect(summaryGets()).to.equal(before);
  });
});
