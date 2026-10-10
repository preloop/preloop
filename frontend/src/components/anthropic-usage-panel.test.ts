import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';
import './anthropic-usage-panel.ts';
import type { AnthropicUsagePanel } from './anthropic-usage-panel';
import type { AnthropicUsageSummary } from '../types';

const jsonResponse = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });

const connection = {
  id: 'a-1',
  has_key: true,
  key_hint: 'AbCd',
  gateway_key_names: [],
  is_active: true,
  last_synced_at: '2026-10-09T01:00:00Z',
  last_synced_day: '2026-10-08',
  last_error: null,
  last_warning: null,
};

const makeSummary = (
  overrides: Partial<AnthropicUsageSummary> = {}
): AnthropicUsageSummary => ({
  metered_by_gateway: false,
  marker: 'Not metered by the gateway',
  period_start: '2026-09-09T00:00:00Z',
  period_end: '2026-10-09T00:00:00Z',
  connection,
  total_estimated_cost: 1.23,
  total_tokens: 163600,
  currency: 'USD',
  excluded_metered_by_gateway: {
    estimated_cost: 5,
    tokens: 60000,
    actors: ['key:preloop-gateway'],
  },
  by_actor: [
    {
      actor: 'dev@corp.example',
      actor_type: 'user_actor',
      estimated_cost: 1.2,
      tokens: 152500,
      num_sessions: 5,
      lines_added: 1543,
      lines_removed: 892,
      commits: 12,
      pull_requests: 2,
      days: 1,
      user_id: 'u-1',
      mapping_source: 'member_email',
      gateway_subject_id: null,
    },
  ],
  by_model: [],
  not_attributable: ['Claude Enterprise seats are not imported.'],
  ...overrides,
});

describe('AnthropicUsagePanel', () => {
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

  const mount = async (): Promise<AnthropicUsagePanel> => {
    const element = (await fixture(
      html`<anthropic-usage-panel
        .startDate=${'2026-09-09T00:00:00Z'}
        .endDate=${'2026-10-09T00:00:00Z'}
      ></anthropic-usage-panel>`
    )) as AnthropicUsagePanel;
    await waitUntil(
      () =>
        element.shadowRoot?.querySelector('.panel') ||
        element.shadowRoot?.querySelector('[role="alert"]'),
      'panel should render'
    );
    return element;
  };

  const text = (element: AnthropicUsagePanel) =>
    (element.shadowRoot?.textContent ?? '').replace(/\s+/g, ' ');

  const q = (element: AnthropicUsagePanel, id: string) =>
    element.shadowRoot?.querySelector(`[data-testid="${id}"]`);

  it('requests the window and labels figures as not metered', async () => {
    fetchStub.callsFake(async () => jsonResponse(makeSummary()));
    const element = await mount();
    const url = String(fetchStub.firstCall.args[0]);
    expect(url).to.contain('/api/v1/anthropic-usage?');
    expect(url).to.contain('start_date=2026-09-09');
    expect(q(element, 'anthropic-marker')?.textContent).to.contain(
      'Not metered by the gateway'
    );
    expect(text(element)).to.contain('dev@corp.example');
    expect(text(element)).to.contain('matched by email');
    expect(text(element)).to.contain('AbCd');
  });

  it('shows gateway-metered usage apart from the total', async () => {
    fetchStub.callsFake(async () => jsonResponse(makeSummary()));
    const element = await mount();
    const excluded = q(element, 'anthropic-excluded');
    expect(excluded).to.not.equal(null);
    expect(excluded?.textContent).to.contain('key:preloop-gateway');
    expect(q(element, 'anthropic-total')?.textContent).to.contain('1.23');
  });

  it('lists what cannot be attributed', async () => {
    fetchStub.callsFake(async () => jsonResponse(makeSummary()));
    const element = await mount();
    expect(q(element, 'anthropic-not-attributable')?.textContent).to.contain(
      'Claude Enterprise seats'
    );
  });

  it('shows the connect form without a connection', async () => {
    fetchStub.callsFake(async () =>
      jsonResponse(
        makeSummary({ connection: null, total_estimated_cost: null })
      )
    );
    const element = await mount();
    expect(q(element, 'anthropic-form')).to.not.equal(null);
    const save = q(element, 'anthropic-save') as HTMLElement & {
      disabled: boolean;
    };
    expect(save.disabled).to.equal(true);
  });

  it('queues a sync with POST to the sync endpoint', async () => {
    fetchStub.callsFake(async (input: RequestInfo | URL) => {
      if (String(input).includes('/sync')) {
        return jsonResponse({ status: 'queued' }, 202);
      }
      return jsonResponse(makeSummary());
    });
    const element = await mount();
    (q(element, 'anthropic-sync') as HTMLElement).click();
    await waitUntil(() => q(element, 'anthropic-notice'), 'notice shown');
    const syncCall = fetchStub
      .getCalls()
      .find((call) =>
        String(call.args[0]).includes('/api/v1/anthropic-usage/sync')
      );
    expect(syncCall).to.not.equal(undefined);
  });

  it('shows the last error recorded on the connection', async () => {
    fetchStub.callsFake(async () =>
      jsonResponse(
        makeSummary({
          connection: {
            ...connection,
            last_error: 'Anthropic rejected the Admin API key (401).',
          },
        })
      )
    );
    const element = await mount();
    expect(q(element, 'anthropic-error')?.textContent).to.contain('401');
  });
});
