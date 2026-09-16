import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import '../../../components/view-header.ts';
import {
  BILLING_SUBSCRIPTION_CHANGED,
  invalidateApiCaches,
} from '../../../api';
import './account-view';
import type { AccountView } from './account-view';

describe('AccountView', () => {
  let fetchStub: sinon.SinonStub;

  function copy(el: AccountView): string {
    return (el.shadowRoot?.textContent ?? '').replace(/\s+/g, ' ').trim();
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

  it('includes the operator recovery reason in the deactivation request', async () => {
    fetchStub = createFetchStub();
    const element = await fixture<AccountView>(
      html`<account-view></account-view>`
    );
    await waitUntil(() => !(element as any)._loading);
    (element as any)._haltStatus = {
      active: true,
      scopes: [{ scope: 'flows', reason: 'Inspect active runtimes' }],
    };
    await element.updateComplete;
    expect(
      element.shadowRoot?.querySelectorAll('sl-input[label="Recovery reason"]')
    ).to.have.length(1);
    (element as any)._haltReason = 'Runtime termination verified';
    await (element as any)._handleResume(['flows']);
    const request = fetchStub
      .getCalls()
      .find((call) => String(call.args[0]).includes('/kill-switch/deactivate'));
    expect(request).to.exist;
    expect(JSON.parse(request!.args[1].body)).to.deep.equal({
      scopes: ['flows'],
      reason: 'Runtime termination verified',
    });
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

  it('uses the explicit comparison panel instead of an immediate upgrade grid', async () => {
    fetchStub = createFetchStub({ billing: true });
    const element = (await fixture(
      html`<account-view></account-view>`
    )) as AccountView;

    await waitUntil(() => !(element as any)._loading, 'load');
    await element.updateComplete;

    expect(element.shadowRoot?.querySelector('billing-plan-comparison')).to
      .exist;
    expect(element.shadowRoot?.querySelector('pricing-card')).not.to.exist;
  });

  it('does not claim a dollar costs a dollar at a 1:1 credit rate (D13)', async () => {
    fetchStub = createFetchStub({ billing: true, extraCreditPricePerUsd: 1 });
    const element = (await fixture(
      html`<account-view></account-view>`
    )) as AccountView;

    await waitUntil(() => !(element as any)._loading, 'load');
    await element.updateComplete;

    const text = element.shadowRoot?.textContent ?? '';
    expect(text).to.contain(
      'Additional usage is billed at cost only when you opt in'
    );
    expect(text).to.not.contain('$1.00 per');
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
        quota_tokens: 100000000,
        used_tokens: 100000000,
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
    expect(text).to.contain('100M of 100M tokens');
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
  it('explains grandfathering without automatically changing a legacy plan', async () => {
    fetchStub = createFetchStub({
      billing: true,
      subscription: {
        plan_id: 'teams',
        status: 'active',
        current_period_end: '2026-12-31T00:00:00Z',
      },
    });
    const element = (await fixture(
      html`<account-view></account-view>`
    )) as AccountView;
    await waitUntil(() => !(element as any)._loading, 'load');
    await element.updateComplete;
    const panel = element.shadowRoot!.querySelector(
      'billing-plan-comparison'
    ) as any;
    await waitUntil(() => !panel.loading);
    await panel.updateComplete;
    const collapsed = (panel.shadowRoot.textContent ?? '').replace(/\s+/g, ' ');
    // Collapsed, the panel states the plan and offers one action.
    expect(collapsed).to.contain('Legacy Teams');
    expect(collapsed).to.not.contain('What changes');
    expect(collapsed).to.not.contain('Would this plan cover your usage?');
    panel.shadowRoot.querySelector('[data-testid="change-plan"]').click();
    await panel.updateComplete;
    const comparison = (panel.shadowRoot.textContent ?? '').replace(
      /\s+/g,
      ' '
    );
    expect(comparison).to.contain('Legacy Teams');
    expect(comparison).to.contain('$29.00 per user');
    expect(comparison).to.contain('grandfathered per-user rate stays');
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

  it('ignores a composed child event so the template binding is the only extra fetch', async () => {
    fetchStub = createFetchStub({ billing: true });
    const element = await fixture<AccountView>(
      html`<account-view></account-view>`
    );
    await waitUntil(() => !(element as any)._loading, 'load');
    await element.updateComplete;
    const comparison = element.shadowRoot!.querySelector(
      'billing-plan-comparison'
    );
    expect(comparison, 'expected the plan comparison').to.exist;
    const before = summaryGets();

    comparison!.dispatchEvent(
      new CustomEvent(BILLING_SUBSCRIPTION_CHANGED, {
        bubbles: true,
        composed: true,
      })
    );
    await waitUntil(
      () => summaryGets() === before + 1,
      'template binding should fetch summary once'
    );
    expect(summaryGets()).to.equal(before + 1);
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
