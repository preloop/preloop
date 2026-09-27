import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';
import './copilot-usage-panel.ts';
import type { CopilotUsagePanel } from './copilot-usage-panel';
import type { CopilotUsageSummary } from '../types';

const jsonResponse = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });

const connection = {
  id: 'c-1',
  organization: 'example-org',
  enterprise: null,
  has_enterprise_token: false,
  seat_price_monthly: 19,
  currency: 'USD',
  is_active: true,
  last_synced_at: '2026-09-27T01:00:00Z',
  last_synced_day: '2026-09-24',
  last_error: null,
  per_user_billing_status: 'available',
  per_user_billing_reason: null,
  metrics_status: 'available',
  metrics_reason: null,
  last_warning: null,
};

const makeSummary = (
  overrides: Partial<CopilotUsageSummary> = {}
): CopilotUsageSummary => ({
  metered_by_gateway: false,
  marker: 'Not metered by the gateway',
  period_start: '2026-09-01T00:00:00Z',
  period_end: '2026-09-28T00:00:00Z',
  connection,
  seats: {
    total_seats: 2,
    plan_type: 'business',
    as_of: '2026-09-27T00:00:00Z',
    seat_price_monthly: 19,
    currency: 'USD',
    monthly_seat_estimate: 38,
    assigned: [
      {
        login: 'alice',
        last_activity_at: '2026-09-26T10:00:00Z',
        last_activity_editor: 'vscode/1.0',
      },
      { login: 'bob', last_activity_at: null, last_activity_editor: null },
    ],
  },
  premium_requests: {
    total_net_amount: 2.4,
    currency: 'USD',
    per_user_status: 'available',
    per_user_unavailable_reason: null,
    org_aggregate_net_amount: null,
    unattributed_net_amount: null,
    aggregate_days: 0,
    by_developer: [
      { login: 'alice', net_amount: 1.6, net_quantity: 40 },
      { login: 'bob', net_amount: 0.8, net_quantity: 20 },
    ],
    by_model: [
      { model: 'model-a', net_amount: 2, net_quantity: 50 },
      { model: 'model-b', net_amount: 0.4, net_quantity: 10 },
    ],
  },
  model_mix: [
    {
      login: 'alice',
      basis: 'net_amount',
      models: [
        { model: 'model-a', value: 1.2, share: 0.75 },
        { model: 'model-b', value: 0.4, share: 0.25 },
      ],
    },
    {
      login: 'bob',
      basis: 'requests',
      models: [{ model: 'model-a', value: 3, share: 1 }],
    },
  ],
  ...overrides,
});

describe('CopilotUsagePanel', () => {
  let fetchStub: sinon.SinonStub;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
    fetchStub = sinon.stub(window, 'fetch');
  });

  afterEach(() => {
    fetchStub.restore();
    localStorage.clear();
  });

  const mount = async (): Promise<CopilotUsagePanel> => {
    const element = (await fixture(
      html`<copilot-usage-panel
        .startDate=${'2026-09-01T00:00:00Z'}
        .endDate=${'2026-09-28T00:00:00Z'}
      ></copilot-usage-panel>`
    )) as CopilotUsagePanel;
    await waitUntil(
      () =>
        element.shadowRoot?.querySelector('.panel') ||
        element.shadowRoot?.querySelector('[role="alert"]'),
      'panel should render'
    );
    return element;
  };

  const text = (element: CopilotUsagePanel) =>
    (element.shadowRoot?.textContent ?? '').replace(/\s+/g, ' ');

  const q = (element: CopilotUsagePanel, id: string) =>
    element.shadowRoot?.querySelector(`[data-testid="${id}"]`);

  it('requests the page window and shows the not-metered marker', async () => {
    fetchStub.callsFake(async () => jsonResponse(makeSummary()));
    const element = await mount();
    const url = String(fetchStub.firstCall.args[0]);
    expect(url).to.contain('/api/v1/cost/copilot?');
    expect(url).to.contain('start_date=2026-09-01');
    expect(url).to.contain('end_date=2026-09-28');
    const markers = element.shadowRoot?.querySelectorAll(
      '[data-testid="copilot-marker"]'
    );
    // Header, seats, premium requests and model mix all carry the marker.
    expect(markers?.length).to.equal(4);
    expect(markers?.[0].textContent).to.contain('Not metered by the gateway');
    expect(text(element)).to.contain('never count toward gateway usage');
  });

  it('renders seats, seat price, estimate, developers, models and mix', async () => {
    fetchStub.callsFake(async () => jsonResponse(makeSummary()));
    const element = await mount();
    const body = text(element);
    expect(q(element, 'copilot-seat-estimate')?.textContent).to.contain(
      '38.00'
    );
    expect(body).to.contain('19.00');
    expect(q(element, 'copilot-premium-total')?.textContent).to.contain('2.40');
    const developers = q(element, 'copilot-by-developer')?.textContent ?? '';
    expect(developers).to.contain('alice');
    expect(developers).to.contain('1.60');
    const models = q(element, 'copilot-by-model')?.textContent ?? '';
    expect(models).to.contain('model-a');
    const mix = q(element, 'copilot-model-mix')?.textContent ?? '';
    expect(mix).to.contain('model-a 75.0%');
    expect(mix).to.contain('net spend');
    expect(mix).to.contain('request counts');
    expect(q(element, 'copilot-per-user-reason')).to.equal(null);
  });

  it('shows no dollar seat line when the seat price is cleared', async () => {
    const summary = makeSummary();
    summary.seats = {
      ...summary.seats,
      seat_price_monthly: null,
      monthly_seat_estimate: null,
    };
    fetchStub.callsFake(async () => jsonResponse(summary));
    const element = await mount();
    expect(q(element, 'copilot-seat-estimate')).to.equal(null);
    expect(q(element, 'copilot-no-seat-price')).to.not.equal(null);
    const seats = q(element, 'copilot-seats')?.textContent ?? '';
    expect(seats).to.not.contain('$0');
    expect(seats).to.contain('not set');
  });

  it('shows the organization total and reason when per-user is unavailable', async () => {
    const summary = makeSummary();
    summary.premium_requests = {
      ...summary.premium_requests,
      per_user_status: 'unavailable',
      per_user_unavailable_reason:
        'Per-user premium-request spend is unavailable: the organization route returned 403.',
      org_aggregate_net_amount: 5,
      aggregate_days: 1,
      by_developer: [],
    };
    fetchStub.callsFake(async () => jsonResponse(summary));
    const element = await mount();
    expect(q(element, 'copilot-org-total')?.textContent).to.contain('5.00');
    const reason = q(element, 'copilot-per-user-reason')?.textContent ?? '';
    expect(reason).to.contain('returned 403');
    expect(reason).to.contain('organization total');
  });

  it('shows the last import error and the metrics reason', async () => {
    fetchStub.callsFake(async () =>
      jsonResponse(
        makeSummary({
          connection: {
            ...connection,
            last_error: 'GitHub returned 403 for Copilot seat information.',
            metrics_status: 'unavailable',
            metrics_reason: 'GitHub returned 403 for the usage-metrics report.',
          },
        })
      )
    );
    const element = await mount();
    expect(q(element, 'copilot-error')?.textContent).to.contain(
      '403 for Copilot seat information'
    );
    expect(q(element, 'copilot-metrics')?.textContent).to.contain(
      'usage-metrics report'
    );
  });

  it('shows "no data imported" rather than a zero when nothing was imported', async () => {
    const summary = makeSummary();
    summary.premium_requests = {
      ...summary.premium_requests,
      total_net_amount: null,
      by_developer: [],
      by_model: [],
    };
    fetchStub.callsFake(async () => jsonResponse(summary));
    const element = await mount();
    const total = q(element, 'copilot-premium-total')?.textContent ?? '';
    expect(total).to.contain('no data imported');
    expect(total).to.not.contain('$0');
  });

  it('shows the connect form without a connection and saves it', async () => {
    let saved: Record<string, unknown> | null = null;
    fetchStub.callsFake(async (input: RequestInfo, init?: RequestInit) => {
      const url = String(input);
      if (url.includes('/connection') && init?.method === 'PUT') {
        saved = JSON.parse(String(init.body));
        return jsonResponse(connection);
      }
      return jsonResponse(makeSummary(saved ? {} : { connection: null }));
    });
    const element = await mount();
    expect(q(element, 'copilot-form')).to.not.equal(null);
    const inputs = element.shadowRoot!.querySelectorAll('sl-input');
    const byName = (name: string) =>
      Array.from(inputs).find((input) => input.getAttribute('name') === name)!;
    const setValue = (name: string, value: string) => {
      const input = byName(name) as unknown as { value: string };
      input.value = value;
      byName(name).dispatchEvent(new Event('sl-input'));
    };
    setValue('organization', 'example-org');
    setValue('token', 'secret-value');
    setValue('seat_price', '');
    await element.updateComplete;
    (q(element, 'copilot-save') as HTMLElement).click();
    await waitUntil(() => saved !== null, 'connection should be saved');
    expect(saved).to.deep.include({
      organization: 'example-org',
      token: 'secret-value',
      seat_price_monthly: null,
    });
    await waitUntil(() => q(element, 'copilot-status'), 'status should show');
  });

  it('queues a sync and reports a failure to queue', async () => {
    let syncStatus = 202;
    fetchStub.callsFake(async (input: RequestInfo, init?: RequestInit) => {
      if (String(input).endsWith('/connection/sync') && init?.method === 'POST')
        return syncStatus === 202
          ? jsonResponse({ status: 'queued' }, 202)
          : jsonResponse({ detail: 'Could not queue the Copilot import' }, 503);
      return jsonResponse(makeSummary());
    });
    const element = await mount();
    (q(element, 'copilot-sync') as HTMLElement).click();
    await waitUntil(() => text(element).includes('Import queued'));
    syncStatus = 503;
    (q(element, 'copilot-sync') as HTMLElement).click();
    await waitUntil(() =>
      text(element).includes('Could not queue the Copilot import')
    );
  });

  it('shows spend not matched to a current seat and the last warning', async () => {
    const summary = makeSummary({
      connection: {
        ...connection,
        last_warning: 'GitHub reported 5 seats but only 2 were listed.',
      },
    });
    summary.premium_requests = {
      ...summary.premium_requests,
      total_net_amount: 2.9,
      unattributed_net_amount: 0.5,
    };
    fetchStub.callsFake(async () => jsonResponse(summary));
    const element = await mount();
    expect(q(element, 'copilot-unattributed')?.textContent).to.contain('$0.50');
    expect(q(element, 'copilot-warning')?.textContent).to.contain(
      'only 2 were listed'
    );
    expect(q(element, 'copilot-org-total')).to.equal(null);
    expect(q(element, 'copilot-per-user-reason')).to.equal(null);
  });

  it('shows a paused connection with Resume instead of Sync now', async () => {
    let active = false;
    let saved: Record<string, unknown> | null = null;
    fetchStub.callsFake(async (input: RequestInfo, init?: RequestInit) => {
      if (String(input).includes('/connection') && init?.method === 'PUT') {
        saved = JSON.parse(String(init.body));
        active = true;
        return jsonResponse({ ...connection, is_active: true });
      }
      return jsonResponse(
        makeSummary({ connection: { ...connection, is_active: active } })
      );
    });
    const element = await mount();
    expect(q(element, 'copilot-paused')).to.not.equal(null);
    expect(q(element, 'copilot-sync')).to.equal(null);
    (q(element, 'copilot-resume') as HTMLElement).click();
    await waitUntil(() => saved !== null, 'resume should save');
    expect(saved).to.deep.include({
      organization: 'example-org',
      seat_price_monthly: 19,
      is_active: true,
    });
    expect(saved).to.not.have.property('token');
    await waitUntil(() => q(element, 'copilot-sync'), 'sync should return');
    expect(q(element, 'copilot-paused')).to.equal(null);
  });
});
