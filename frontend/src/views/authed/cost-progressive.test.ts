import { expect, fixture, html, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';
import { invalidateApiCaches } from '../../api';
import { CostView } from './cost-view';
import './cost-view.ts';

const payload = {
  period_start: '2026-01-01T00:00:00Z',
  period_end: '2026-01-31T00:00:00Z',
  total_requests: 10,
  successful_requests: 10,
  failed_requests: 0,
  estimated_cost: 42,
  token_usage: { prompt_tokens: 100, completion_tokens: 20, total_tokens: 120 },
  budget: { current_spend_usd: 42 },
  requests_by_day: [],
  usage_by_model: [],
  usage_by_session: [],
  usage_by_flow: [],
  usage_by_tool: [],
};

function reply(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('Cost progressive loading', () => {
  let fetchStub: sinon.SinonStub;
  let urls: URL[];
  let handler: (url: URL) => Promise<Response>;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-token');
    urls = [];
    handler = async (url) => {
      if (url.pathname.endsWith('/cost/summary'))
        return reply({
          ...payload,
          period_start: url.searchParams.get('start_date'),
          period_end: url.searchParams.get('end_date'),
        });
      if (url.pathname.endsWith('/features')) return reply({ features: {} });
      if (url.pathname.endsWith('/agents')) return reply({ items: [] });
      if (url.pathname.endsWith('/users')) return reply({ users: [] });
      return reply([]);
    };
    fetchStub = sinon
      .stub(window, 'fetch')
      .callsFake((input: RequestInfo | URL) => {
        const url = new URL(String(input), location.origin);
        urls.push(url);
        return handler(url);
      });
  });

  afterEach(() => {
    fetchStub.restore();
    localStorage.clear();
    sessionStorage.clear();
    invalidateApiCaches();
  });

  it('renders totals while metadata and the first breakdown are still pending', async () => {
    const fallback = handler;
    handler = (url) => {
      if (
        url.pathname.endsWith('/ai-models') ||
        url.searchParams.has('breakdown')
      ) {
        return new Promise(() => {});
      }
      return fallback(url);
    };
    const element = await fixture<CostView>(html`<cost-view></cost-view>`);
    await waitUntil(
      () => !element['loading'],
      'totals must not wait for other requests',
      { timeout: 2000 }
    );
    expect(
      element.shadowRoot!.querySelector('[aria-label="Cost summary metrics"]')
    ).to.exist;
    expect(
      urls
        .filter(
          (u) =>
            u.pathname.endsWith('/cost/summary') &&
            !u.searchParams.has('breakdown')
        )
        .every((u) => u.searchParams.get('include_breakdown') === 'false')
    ).to.equal(true);
    expect(
      element.shadowRoot!.querySelector('[data-section="agents"]')!.textContent
    ).to.include('Loading');
  });

  it('loads the default tab, shares session data, and defers tools until selected', async () => {
    const element = await fixture<CostView>(html`<cost-view></cost-view>`);
    await waitUntil(() => element['loadedTabs'].has('agents'));
    expect(
      urls.flatMap((u) => u.searchParams.getAll('breakdown'))
    ).to.deep.equal(['sessions', 'flows']);
    await element['handleTabShow'](
      new CustomEvent('sl-tab-show', { detail: { name: 'users' } })
    );
    expect(
      urls.flatMap((u) => u.searchParams.getAll('breakdown'))
    ).to.deep.equal(['sessions', 'flows']);
    await element['handleTabShow'](
      new CustomEvent('sl-tab-show', { detail: { name: 'tools' } })
    );
    expect(
      urls.flatMap((u) => u.searchParams.getAll('breakdown'))
    ).to.deep.equal(['sessions', 'flows', 'tools']);
  });

  it('keeps totals visible after a breakdown failure and retries only that section', async () => {
    const fallback = handler;
    let fail = true;
    handler = async (url) =>
      url.searchParams.get('breakdown') === 'tools' && fail
        ? reply({ detail: 'Unavailable' }, 503)
        : fallback(url);
    const element = await fixture<CostView>(html`<cost-view></cost-view>`);
    await waitUntil(() => element['loadedTabs'].has('agents'));
    await element['handleTabShow'](
      new CustomEvent('sl-tab-show', { detail: { name: 'tools' } })
    );
    await element.updateComplete;
    expect(element['sectionStates'].tools).to.equal('error');
    expect(element['summary']!.estimated_cost).to.equal(42);
    expect(
      element.shadowRoot!.querySelector('[aria-label="Cost summary metrics"]')
    ).to.exist;
    fail = false;
    (
      element.shadowRoot!.querySelector(
        '[data-section="tools"] sl-button'
      ) as HTMLElement
    ).click();
    await waitUntil(() => element['loadedTabs'].has('tools'));
    expect(
      urls.filter((u) => u.searchParams.get('breakdown') === 'tools')
    ).to.have.length(2);
    expect(
      urls.filter((u) =>
        u.searchParams.getAll('breakdown').includes('sessions')
      )
    ).to.have.length(1);
  });

  it('rejects late totals even when the selected range changes away and back', async () => {
    const element = await fixture<CostView>(html`<cost-view></cost-view>`);
    await waitUntil(() => element['loadedTabs'].has('agents'));
    const fallback = handler;
    const pending: Array<(response: Response) => void> = [];
    handler = (url) =>
      url.pathname.endsWith('/cost/summary') &&
      url.searchParams.get('include_breakdown') === 'false' &&
      pending.length < 3
        ? new Promise((resolve) => pending.push(resolve))
        : fallback(url);
    const first = element['load']();
    element['selectedRange'] = 'last-7';
    const second = element['load']();
    element['selectedRange'] = 'last-30';
    await new Promise((resolve) => setTimeout(resolve, 20));
    const latest = element['load']();
    await waitUntil(() => pending.length === 3);
    pending[2](reply({ ...payload, estimated_cost: 70 }));
    await latest;
    pending[1](reply({ ...payload, estimated_cost: 60 }));
    pending[0](reply({ ...payload, estimated_cost: 50 }));
    await Promise.all([first, second]);
    expect(element['summary']!.estimated_cost).to.equal(70);
    expect(element['loading']).to.equal(false);
  });

  it('ignores old breakdowns and automatically reloads the tab kept open on a range change', async () => {
    const element = await fixture<CostView>(html`<cost-view></cost-view>`);
    await waitUntil(() => element['loadedTabs'].has('agents'));
    const fallback = handler;
    const pending: Array<(response: Response) => void> = [];
    handler = (url) =>
      url.searchParams.get('breakdown') === 'tools'
        ? new Promise((resolve) => pending.push(resolve))
        : fallback(url);
    const first = element['handleTabShow'](
      new CustomEvent('sl-tab-show', { detail: { name: 'tools' } })
    );
    await waitUntil(() => pending.length === 1);
    element['selectedRange'] = 'last-7';
    await element['load']();
    await waitUntil(() => pending.length === 2);
    pending[1](reply({ ...payload, usage_by_tool: [] }));
    await waitUntil(() => element['loadedTabs'].has('tools'));
    pending[0](reply({ ...payload, usage_by_tool: [{ name: 'stale' }] }));
    await first;
    expect(element['summary']!.usage_by_tool).to.deep.equal([]);
    expect(element['activeTab']).to.equal('tools');
    expect(element['sectionStates'].tools).to.equal('ready');
  });

  it('deduplicates a pending session breakdown shared by Agents and Sessions', async () => {
    const fallback = handler;
    let resolveBreakdown!: (response: Response) => void;
    handler = (url) =>
      url.searchParams.has('breakdown')
        ? new Promise((resolve) => {
            resolveBreakdown = resolve;
          })
        : fallback(url);
    const element = await fixture<CostView>(html`<cost-view></cost-view>`);
    await waitUntil(() => !!resolveBreakdown);
    const selected = element['handleTabShow'](
      new CustomEvent('sl-tab-show', { detail: { name: 'sessions' } })
    );
    expect(urls.filter((u) => u.searchParams.has('breakdown'))).to.have.length(
      1
    );
    resolveBreakdown(reply(payload));
    await selected;
    expect(element['loadedTabs'].has('sessions')).to.equal(true);
  });

  it('does not publish totals after the view disconnects', async () => {
    const fallback = handler;
    let resolveSummary!: (response: Response) => void;
    handler = (url) =>
      url.pathname.endsWith('/cost/summary')
        ? new Promise((resolve) => {
            resolveSummary = resolve;
          })
        : fallback(url);
    const element = await fixture<CostView>(html`<cost-view></cost-view>`);
    await waitUntil(() => !!resolveSummary);
    element.remove();
    resolveSummary(reply(payload));
    await new Promise((resolve) => setTimeout(resolve, 30));
    expect(element['summary']).to.equal(null);
  });

  it('shows a retryable primary error without fabricating zero totals', async () => {
    const fallback = handler;
    handler = async (url) =>
      url.pathname.endsWith('/cost/summary')
        ? reply({ detail: 'Unavailable' }, 503)
        : fallback(url);
    const element = await fixture<CostView>(html`<cost-view></cost-view>`);
    await waitUntil(() => !!element['error']);
    await element.updateComplete;
    expect(
      element.shadowRoot!.querySelector('[aria-label="Cost summary metrics"]')
    ).not.to.exist;
    handler = fallback;
    (
      element.shadowRoot!.querySelector(
        'sl-alert[role="alert"] sl-button'
      ) as HTMLElement
    ).click();
    await waitUntil(() => element['summary']?.estimated_cost === 42);
    expect(element['error']).to.equal(null);
  });

  it('keeps imported totals separate and visible while imported detail is pending', async () => {
    const fallback = handler;
    handler = async (url) => {
      if (url.searchParams.get('breakdown') === 'imported')
        return new Promise(() => {});
      if (url.pathname.endsWith('/cost/summary')) {
        const response = await fallback(url);
        return reply({
          ...(await response.json()),
          imported_usage: {
            event_count: 2,
            total_tokens: 100,
            imported_cost: 7,
            usage_by_model: [],
            usage_by_conversation: [],
          },
        });
      }
      return fallback(url);
    };
    const element = await fixture<CostView>(html`<cost-view></cost-view>`);
    await waitUntil(() => !element['loading']);
    await element.updateComplete;
    expect(element['summary']!.estimated_cost).to.equal(42);
    expect(
      element.shadowRoot!.querySelector('[aria-label="Imported usage totals"]')!
        .textContent
    ).to.include('$7.00');
    expect(
      element.shadowRoot!.querySelector('[data-section="imported"]')!
        .textContent
    ).to.include('Loading');
    expect(
      element.shadowRoot!.querySelector(
        '[aria-label="Imported usage by model"]'
      )
    ).not.to.exist;
  });

  it('preserves a pricing deep link until delayed settings render its target', async () => {
    const originalUrl = location.href;
    const scroll = sinon.stub(Element.prototype, 'scrollIntoView');
    const fallback = handler;
    let resolveFeatures!: (response: Response) => void;
    handler = (url) =>
      url.pathname.endsWith('/features')
        ? new Promise((resolve) => {
            resolveFeatures = resolve;
          })
        : fallback(url);
    history.replaceState(null, '', `${location.pathname}?panel=pricing`);
    try {
      const element = await fixture<CostView>(html`<cost-view></cost-view>`);
      await waitUntil(() => !element['loading']);
      expect(element['requestedPanel']).to.equal('pricing');
      expect(scroll.called).to.equal(false);
      resolveFeatures(reply({ features: { model_price_overrides: true } }));
      await waitUntil(() => scroll.called);
      expect((scroll.thisValues[0] as HTMLElement).id).to.equal(
        'panel-pricing'
      );
      expect(element['requestedPanel']).to.equal(null);
    } finally {
      scroll.restore();
      history.replaceState(null, '', originalUrl);
      resolveFeatures?.(reply({ features: {} }));
    }
  });

  it('preserves loaded budget controls when an unrelated settings request fails', async () => {
    const fallback = handler;
    handler = async (url) =>
      url.pathname.endsWith('/ai-models')
        ? reply({ detail: 'Unavailable' }, 503)
        : fallback(url);
    const element = await fixture<CostView>(html`<cost-view></cost-view>`);
    await waitUntil(() => !element['loading'] && !element['contextLoading']);
    await element.updateComplete;
    expect(element.shadowRoot!.querySelector('budget-health-card')).to.exist;
    expect(
      element.shadowRoot!.querySelector('[aria-label="Cost summary metrics"]')
    ).to.exist;
    expect(element['contextError']).not.to.equal(null);
  });
});
