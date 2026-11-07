import { expect, fixture, html, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';
import { clearCostReadCache, invalidateApiCaches } from '../../api';
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

  it('renders the totals while metadata is still pending', async () => {
    const fallback = handler;
    handler = (url) => {
      if (url.pathname.endsWith('/ai-models')) {
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
    const summaries = urls.filter((url) =>
      url.pathname.endsWith('/cost/summary')
    );
    // The first read is the totals only: no breakdowns, no comparison.
    expect(summaries[0].searchParams.get('include_breakdown')).to.equal(
      'false'
    );
    expect(summaries[0].searchParams.getAll('breakdown')).to.deep.equal([]);
    expect(summaries[0].searchParams.has('compare_start_date')).to.equal(false);
    await waitUntil(() => element['loadedTabs'].has('agents'));
  });

  it('loads the default tab, shares session data, and defers tools until selected', async () => {
    const element = await fixture<CostView>(html`<cost-view></cost-view>`);
    await waitUntil(() => element['loadedTabs'].has('agents'));
    // The spend chart reads /cost/timeseries, so the summary never asks for
    // `days`; every breakdown waits for its tab.
    const tabBreakdowns = () =>
      urls.flatMap((u) => u.searchParams.getAll('breakdown'));
    expect(
      urls.filter((u) => u.searchParams.getAll('breakdown').includes('days'))
    ).to.have.length(0);
    // Imported detail is asked only when the totals report imported events.
    expect(tabBreakdowns()).to.deep.equal(['sessions', 'flows']);
    await element['handleTabShow'](
      new CustomEvent('sl-tab-show', { detail: { name: 'users' } })
    );
    expect(tabBreakdowns()).to.deep.equal(['sessions', 'flows']);
    await element['handleTabShow'](
      new CustomEvent('sl-tab-show', { detail: { name: 'tools' } })
    );
    expect(tabBreakdowns()).to.deep.equal(['sessions', 'flows', 'tools']);
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
    // Hold the headline totals (they end now; the comparison ends earlier).
    const isHeadline = (url: URL) =>
      url.pathname.endsWith('/cost/summary') &&
      url.searchParams.get('include_breakdown') === 'false' &&
      new Date(url.searchParams.get('end_date')!).getTime() >
        Date.now() - 24 * 60 * 60 * 1000;
    handler = (url) =>
      isHeadline(url) && pending.length < 2
        ? new Promise((resolve) => pending.push(resolve))
        : fallback(url);
    clearCostReadCache();
    const first = element['load']();
    element['selectedRange'] = 'last-7';
    const second = element['load']();
    element['selectedRange'] = 'last-30';
    await new Promise((resolve) => setTimeout(resolve, 20));
    // Back on the first range: the same window joins the read already in the
    // air, so two headline reads are pending, not three.
    const latest = element['load']();
    await waitUntil(() => pending.length === 2);
    pending[0](reply({ ...payload, estimated_cost: 70 }));
    await latest;
    // The last-7 answer lands late and must not replace the current totals.
    pending[1](reply({ ...payload, estimated_cost: 60 }));
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
      url.searchParams.getAll('breakdown').includes('tools')
        ? new Promise((resolve) => pending.push(resolve))
        : fallback(url);
    const first = element['handleTabShow'](
      new CustomEvent('sl-tab-show', { detail: { name: 'tools' } })
    );
    await waitUntil(() => pending.length === 1);
    element['selectedRange'] = 'last-7';
    const reload = element['load']();
    await waitUntil(() => pending.length === 2);
    pending[1](reply({ ...payload, usage_by_tool: [] }));
    await reload;
    await waitUntil(() => element['loadedTabs'].has('tools'));
    pending[0](reply({ ...payload, usage_by_tool: [{ name: 'stale' }] }));
    await first;
    expect(element['summary']!.usage_by_tool).to.deep.equal([]);
    expect(element['activeTab']).to.equal('tools');
    expect(element['sectionStates'].tools).to.equal('ready');
  });

  it('pages the Sessions tab after the shared recent-session window', async () => {
    const element = await fixture<CostView>(html`<cost-view></cost-view>`);
    await waitUntil(() => element['loadedTabs'].has('agents'));
    await element['handleTabShow'](
      new CustomEvent('sl-tab-show', { detail: { name: 'sessions' } })
    );
    const paged = urls.filter((u) => u.searchParams.has('session_limit'));
    expect(paged).to.have.length(1);
    expect(paged[0].searchParams.get('session_limit')).to.equal('25');
    expect(paged[0].searchParams.get('session_offset')).to.equal('0');
    expect(paged[0].searchParams.getAll('breakdown')).to.deep.equal([
      'sessions',
    ]);
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

  it('reads imported detail after the totals and keeps its spend separate', async () => {
    const fallback = handler;
    handler = async (url) => {
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
    await waitUntil(() => element['loadedTabs'].has('imported'));
    expect(
      urls.filter(
        (url) =>
          url.pathname.endsWith('/cost/summary') &&
          url.searchParams.getAll('breakdown').includes('imported')
      )
    ).to.have.length(1);
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

describe('Spend summary fan-out regression (#1583)', () => {
  it('reads totals, comparison and each detail once, and reuses them across tabs', async () => {
    localStorage.setItem('accessToken', 'test-token');
    const urls: URL[] = [];
    const fetchStub = sinon.stub(window, 'fetch').callsFake(async (input) => {
      const url = new URL(String(input), location.origin);
      urls.push(url);
      if (url.pathname.endsWith('/cost/summary'))
        return reply({
          ...payload,
          period_start: url.searchParams.get('start_date'),
          period_end: url.searchParams.get('end_date'),
          previous: { ...payload, estimated_cost: 21 },
          imported_usage: {
            event_count: 2,
            total_tokens: 100,
            imported_cost: 7,
            usage_by_model: [
              {
                model_alias: 'example',
                event_count: 2,
                total_tokens: 100,
                imported_cost: 7,
              },
            ],
            usage_by_conversation: [],
          },
        });
      if (url.pathname.endsWith('/features')) return reply({ features: {} });
      return reply([]);
    });
    try {
      const element = await fixture<CostView>(html`<cost-view></cost-view>`);
      await waitUntil(
        () =>
          element['loadedTabs'].has('agents') &&
          element['loadedTabs'].has('imported')
      );
      const summaries = urls.filter((url) =>
        url.pathname.endsWith('/cost/summary')
      );
      // Totals, comparison totals, Agents detail and imported detail: one
      // read each, never one per card.
      expect(summaries).to.have.length(4);
      expect(
        summaries.filter((url) => url.searchParams.has('compare_start_date'))
      ).to.have.length(0);
      await waitUntil(() => element['previousRangeSummary'] !== null);
      expect(element['summary']!.imported_usage!.usage_by_model).to.have.length(
        1
      );
      await element['handleTabShow'](
        new CustomEvent('sl-tab-show', { detail: { name: 'users' } })
      );
      // Users reuses the Agents session window: no new read.
      expect(
        urls.filter((url) => url.pathname.endsWith('/cost/summary'))
      ).to.have.length(4);
    } finally {
      fetchStub.restore();
      localStorage.clear();
      invalidateApiCaches();
    }
  });
});

describe('Spend selected-tab reload (#1583)', () => {
  it('pages sessions after the totals on a reload with Sessions open', async () => {
    localStorage.setItem('accessToken', 'test-token');
    const urls: URL[] = [];
    const fetchStub = sinon.stub(window, 'fetch').callsFake(async (input) => {
      const url = new URL(String(input), location.origin);
      urls.push(url);
      if (url.pathname.endsWith('/cost/summary'))
        return reply({
          ...payload,
          total_sessions: 100,
          period_start: url.searchParams.get('start_date'),
          period_end: url.searchParams.get('end_date'),
        });
      if (url.pathname.endsWith('/features')) return reply({ features: {} });
      return reply([]);
    });
    try {
      const element = await fixture<CostView>(html`<cost-view></cost-view>`);
      await waitUntil(() => element['loadedTabs'].has('agents'));
      element['activeTab'] = 'sessions';
      element['selectedRange'] = 'last-7';
      urls.length = 0;
      await element['load']();
      await waitUntil(() => element['loadedTabs'].has('sessions'));
      const summaries = () =>
        urls.filter((url) => url.pathname.endsWith('/cost/summary'));
      const paged = summaries().filter((url) =>
        url.searchParams.has('session_limit')
      );
      expect(paged).to.have.length(1);
      expect(paged[0].searchParams.get('session_limit')).to.equal('25');
      expect(element.sessionPages.total).to.equal(100);
      const before = summaries().length;
      await element['handleTabShow'](
        new CustomEvent('sl-tab-show', { detail: { name: 'users' } })
      );
      // The paged result cannot stand in for Users' recent-session window.
      expect(summaries()).to.have.length(before + 1);
    } finally {
      fetchStub.restore();
      localStorage.clear();
      invalidateApiCaches();
    }
  });
});
