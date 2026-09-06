import { fixture, html, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import '../../setup-tests';
import './cost-view.ts';
import './api-usage-view.ts';
import './settings/ai-model-detail-view.ts';
import { invalidateApiCaches } from '../../api';

/**
 * Cost, API usage and the model detail page each report the same account's
 * gateway usage. They used to compute "the last 30 days" three different ways
 * (a rolling instant, 30 local calendar days ending tonight, a calendar month
 * back), so the same data read 18,504 on one page and 18,419 on another. They
 * now all resolve the window through `utils/time-range`: one fixture, one
 * total, one window.
 */
describe('usage range agreement', () => {
  let fetchStub: sinon.SinonStub;
  let requestedWindows: Record<string, { start: string; end: string }>;

  const TOTAL_REQUESTS = 18419;
  const TOTAL_TOKENS = 821295121;

  const tokenUsage = {
    prompt_tokens: 807700000,
    completion_tokens: 13595121,
    total_tokens: TOTAL_TOKENS,
  };

  const usageSummary = {
    period_start: '2026-08-07T00:00:00Z',
    period_end: '2026-09-06T00:00:00Z',
    total_requests: TOTAL_REQUESTS,
    successful_requests: 18358,
    failed_requests: 61,
    token_usage: tokenUsage,
    estimated_cost: 38.21,
    budget: {
      monthly_limit_usd: 100,
      soft_limit_usd: 80,
      current_spend_usd: 38.21,
      soft_limit_exceeded: false,
      hard_limit_exceeded: false,
    },
    requests_by_day: [],
    usage_by_model: [],
    usage_by_flow: [],
    usage_by_session: [],
  };

  const modelSummary = {
    ai_model_id: 'model-1',
    model_name: 'Claude Sonnet Primary',
    provider_name: 'Anthropic',
    model_identifier: 'claude-sonnet-4',
    period_start: usageSummary.period_start,
    period_end: usageSummary.period_end,
    total_requests: TOTAL_REQUESTS,
    successful_requests: 18358,
    failed_requests: 61,
    token_usage: tokenUsage,
    estimated_cost: 38.21,
    requests_by_day: [],
    usage_by_session: [],
  };

  const json = (payload: unknown) =>
    new Response(JSON.stringify(payload), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });

  const recordWindow = (page: string, url: string) => {
    const query = new URLSearchParams(url.split('?')[1] || '');
    const start = query.get('start_date');
    const end = query.get('end_date');
    if (start && end && !requestedWindows[page]) {
      requestedWindows[page] = { start, end };
    }
  };

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
    requestedWindows = {};

    fetchStub = sinon.stub(window, 'fetch');
    fetchStub.callsFake(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString();

      if (url.startsWith('/api/v1/cost/summary')) {
        recordWindow('cost', url);
        return json(usageSummary);
      }
      if (url.startsWith('/api/v1/account/gateway-usage/summary')) {
        recordWindow('api-usage', url);
        return json(usageSummary);
      }
      if (url.startsWith('/api/v1/ai-models/model-1/summary')) {
        recordWindow('models', url);
        return json(modelSummary);
      }
      if (url.startsWith('/api/v1/account/gateway-usage/search')) {
        return json({ total: 0, limit: 10, offset: 0, items: [] });
      }
      if (url.startsWith('/api/v1/ai-models/model-1/interactions')) {
        return json({ total: 0, limit: 10, offset: 0, items: [] });
      }
      if (url.startsWith('/api/v1/ai-models/model-1/runtime-sessions')) {
        return json({ total: 0, limit: 10, offset: 0, items: [] });
      }
      if (url.startsWith('/api/v1/ai-models/model-1/pricing')) {
        return json({
          ai_model_id: 'model-1',
          source: 'none',
          price: null,
          currency: 'USD',
          fetch_supported: false,
        });
      }
      if (url.startsWith('/api/v1/ai-models/model-1')) {
        return json({
          id: 'model-1',
          name: 'Claude Sonnet Primary',
          provider_name: 'Anthropic',
          model_identifier: 'claude-sonnet-4',
          has_api_key: true,
          meta_data: {},
          is_default: false,
          created_at: '2026-03-01T10:00:00Z',
          updated_at: '2026-03-09T18:30:00Z',
        });
      }
      if (url.startsWith('/api/v1/account/gateway-usage/rate-limits')) {
        // Rate-limit telemetry is not what this test is about, and the view
        // treats a failure here as "no report".
        return new Response(JSON.stringify({ detail: 'not available' }), {
          status: 500,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      if (url.includes('/api/v1/budget/policies')) {
        return json([]);
      }
      if (url.includes('/api/v1/billing/cost/pricing-overrides')) {
        return json([]);
      }
      if (url.includes('/api/v1/features')) {
        return json({ features: { billing: true } });
      }
      if (url.includes('/api/v1/ai-models')) {
        return json([]);
      }
      return json({});
    });
  });

  afterEach(() => {
    fetchStub.restore();
    localStorage.clear();
    sessionStorage.clear();
    invalidateApiCaches();
  });

  const untilLoaded = async (element: HTMLElement) => {
    await waitUntil(
      () => (element as unknown as { loading: boolean }).loading === false,
      `${element.tagName} did not finish loading`
    );
    await (element as unknown as { updateComplete: Promise<unknown> })
      .updateComplete;
  };

  it('asks for one window and prints one total on Cost, API usage and Models', async () => {
    const cost = (await fixture(html`<cost-view></cost-view>`)) as HTMLElement;
    await untilLoaded(cost);

    const apiUsage = (await fixture(
      html`<api-usage-view></api-usage-view>`
    )) as HTMLElement;
    await untilLoaded(apiUsage);

    const models = (await fixture(
      html`<ai-model-detail-view modelId="model-1"></ai-model-detail-view>`
    )) as HTMLElement;
    await untilLoaded(models);

    // 1. The same nominal 30 days is the same 30 days on the wire. The three
    // pages mount milliseconds apart, so the bound is a second, not equality.
    const pages = ['cost', 'api-usage', 'models'];
    for (const page of pages) {
      expect(requestedWindows[page], `${page} sent no window`).to.not.equal(
        undefined
      );
    }
    const spanDays = (window: { start: string; end: string }) =>
      (new Date(window.end).getTime() - new Date(window.start).getTime()) /
      (24 * 60 * 60 * 1000);
    for (const page of pages) {
      const window = requestedWindows[page];
      expect(Math.abs(spanDays(window) - 30), `${page} span`).to.be.below(0.01);
      expect(
        Math.abs(
          new Date(window.start).getTime() -
            new Date(requestedWindows.cost.start).getTime()
        ),
        `${page} start`
      ).to.be.below(2000);
      expect(
        Math.abs(
          new Date(window.end).getTime() -
            new Date(requestedWindows.cost.end).getTime()
        ),
        `${page} end`
      ).to.be.below(2000);
    }

    // 2. One fixture, one total. Cost and API usage print the count compact
    // and keep the exact figure in a title; the model page prints it in full.
    const exact = TOTAL_REQUESTS.toLocaleString();
    const compact = new Intl.NumberFormat(undefined, {
      notation: 'compact',
      maximumFractionDigits: 1,
    }).format(TOTAL_REQUESTS);

    const readsTotal = (element: HTMLElement) => {
      const root = element.shadowRoot!;
      const text = root.textContent || '';
      const titles = Array.from(root.querySelectorAll('[title]'))
        .map((node) => node.getAttribute('title') || '')
        .join(' ');
      return {
        shown: text.includes(compact) || text.includes(exact),
        exact: titles.includes(exact) || text.includes(exact),
      };
    };

    for (const [name, element] of [
      ['cost', cost],
      ['api usage', apiUsage],
      ['models', models],
    ] as Array<[string, HTMLElement]>) {
      const reading = readsTotal(element);
      expect(reading.shown, `${name} does not show the total`).to.equal(true);
      expect(reading.exact, `${name} hides the exact total`).to.equal(true);
    }
  });
});
