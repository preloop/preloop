import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import '../../../components/view-header.ts';
import { invalidateApiCaches } from '../../../api';
import './account-view';
import type { AccountView } from './account-view';

describe('AccountView', () => {
  let fetchStub: sinon.SinonStub;

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
            subscription:
              opts.subscription === undefined
                ? {
                    plan_id: 'plan-pro',
                    status: 'active',
                    current_period_end: '2026-12-31T00:00:00Z',
                  }
                : opts.subscription,
            plan: { id: 'plan-pro', name: 'Pro Plan' },
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
            },
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

  it('says a trial ended when the period end is in the past (D13)', async () => {
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
        hosted_model_hard_cap_usd: null,
      },
    });
    const element = (await fixture(
      html`<account-view></account-view>`
    )) as AccountView;

    await waitUntil(() => !(element as any)._loading, 'load');
    await element.updateComplete;

    const text = element.shadowRoot?.textContent ?? '';
    expect(text).to.contain('Trial ended');
    expect(text).to.contain('Jul 27');
    expect(text).to.not.contain('Renews on');
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

  it('shows the interval toggle when there is a plan to upgrade to', async () => {
    fetchStub = createFetchStub({ billing: true });
    const element = (await fixture(
      html`<account-view></account-view>`
    )) as AccountView;

    await waitUntil(() => !(element as any)._loading, 'load');
    await element.updateComplete;

    expect(element.shadowRoot?.querySelector('billing-toggle')).to.exist;
    expect(
      element.shadowRoot?.querySelectorAll('pricing-card').length
    ).to.equal(1);
  });

  it('does not claim a dollar costs a dollar at a 1:1 credit rate (D13)', async () => {
    fetchStub = createFetchStub({ billing: true, extraCreditPricePerUsd: 1 });
    const element = (await fixture(
      html`<account-view></account-view>`
    )) as AccountView;

    await waitUntil(() => !(element as any)._loading, 'load');
    await element.updateComplete;

    const text = element.shadowRoot?.textContent ?? '';
    expect(text).to.contain('Usage beyond the cap is billed at cost');
    expect(text).to.not.contain('$1.00 per');
  });
});
