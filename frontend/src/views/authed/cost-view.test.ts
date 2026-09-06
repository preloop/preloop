import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import type { LitElement } from 'lit';
import sinon from 'sinon';
import '../../components/view-header.ts';
import './cost-view.ts';
import { CostView } from './cost-view';
import { invalidateApiCaches } from '../../api';

describe('CostView', () => {
  let fetchStub: sinon.SinonStub;
  // Per-test copy of the payload so a test can add fields (e.g. the imported
  // usage block) without leaking into the others.
  let summaryPayload: Record<string, unknown>;
  // Per-test feature flags; banner tests enable the override UI.
  let featuresPayload: Record<string, unknown>;
  // Per-test reprice POST response; set by banner tests.
  let repriceResult: Record<string, unknown>;
  // Optional per-test hooks: onReprice runs when the reprice POST arrives,
  // summaryResponder (when set) replaces the summary payload per fetch.
  let onReprice: (() => void) | null;
  let summaryResponder: (() => Record<string, unknown>) | null;

  const summary = {
    period_start: '2026-03-01T00:00:00Z',
    period_end: '2026-03-31T00:00:00Z',
    total_requests: 12,
    successful_requests: 11,
    failed_requests: 1,
    token_usage: {
      prompt_tokens: 200,
      completion_tokens: 100,
      total_tokens: 300,
    },
    estimated_cost: 8.5,
    budget: {
      monthly_limit_usd: 100,
      soft_limit_usd: 80,
      current_spend_usd: 8.5,
      soft_limit_exceeded: false,
      hard_limit_exceeded: false,
    },
    requests_by_day: [],
    usage_by_model: [
      {
        ai_model_id: 'model-1',
        model_alias: 'gpt-test',
        provider_name: 'openai',
        request_count: 5,
        token_usage: {
          prompt_tokens: 100,
          completion_tokens: 50,
          total_tokens: 150,
        },
        estimated_cost: 8.5,
      },
    ],
    usage_by_flow: [],
    usage_by_session: [
      {
        runtime_session_id: 'runtime-session-1',
        session_source_type: 'managed_agent',
        session_source_id: 'agent-1',
        agent_id: 'agent-1',
        agent_name: 'Ops Agent',
        title: 'Agent Session',
        flow_execution_id: null,
        flow_id: null,
        flow_name: null,
        session_reference: 'Agent Session',
        model_alias: 'gpt-test',
        provider_name: 'openai',
        request_count: 5,
        token_usage: {
          prompt_tokens: 100,
          completion_tokens: 50,
          total_tokens: 150,
        },
        estimated_cost: 8.5,
        last_request_at: '2026-03-07T10:00:00Z',
      },
    ],
  };

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
    summaryPayload = { ...summary };
    featuresPayload = { billing: true };
    onReprice = null;
    summaryResponder = null;
    repriceResult = {
      submitted_async: false,
      rows_examined: 0,
      rows_updated: 0,
      rows_skipped: 0,
      cost_before: null,
      cost_after: null,
      dry_run: false,
    };
    fetchStub = sinon.stub(window, 'fetch');
    fetchStub.callsFake(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString();

      if (url.includes('/api/v1/billing/cost/reprice')) {
        onReprice?.();
        return new Response(JSON.stringify(repriceResult), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      if (url.includes('/api/v1/billing/cost/pricing-overrides')) {
        return new Response(JSON.stringify([]), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      if (url.includes('/api/v1/cost/summary')) {
        const payload = summaryResponder ? summaryResponder() : summaryPayload;
        return new Response(JSON.stringify(payload), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      if (url.includes('/api/v1/ai-models')) {
        return new Response(JSON.stringify([]), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      if (url.includes('/api/v1/features')) {
        return new Response(JSON.stringify({ features: featuresPayload }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      if (url.includes('/api/v1/budget/policies')) {
        return new Response(JSON.stringify([]), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      return new Response('{}', { status: 200 });
    });
  });

  afterEach(() => {
    fetchStub.restore();
    localStorage.clear();
    sessionStorage.clear();
    invalidateApiCaches();
  });

  it('renders accessible cost metrics and tables after load', async () => {
    const element = (await fixture(html`<cost-view></cost-view>`)) as CostView;
    // `loading` is a private reactive state field; the cast keeps tsc happy
    // without widening the component's public API.
    await waitUntil(
      () => (element as unknown as { loading: boolean }).loading === false
    );

    const metrics = element.shadowRoot?.querySelector(
      '[aria-label="Cost summary metrics"]'
    );
    expect(metrics).to.exist;

    const agentTable = element.shadowRoot?.querySelector(
      'table[aria-label="Spend by agent"]'
    );
    expect(agentTable).to.exist;
    expect(agentTable?.querySelector('th[scope="col"]')).to.exist;
  });

  it('puts the token split ahead of the money in the agent table', async () => {
    summaryPayload = {
      ...summary,
      usage_by_session: [
        {
          ...summary.usage_by_session[0],
          token_usage: {
            prompt_tokens: 12400,
            completion_tokens: 3100,
            total_tokens: 15500,
            input_tokens: 12400,
            output_tokens: 3100,
            cache_read_tokens: 8200,
            cache_write_tokens: 0,
            uncached_input_tokens: 3900,
            cache_hit_ratio: 0.6777,
          },
        },
      ],
    };
    const element = (await fixture(html`<cost-view></cost-view>`)) as CostView;
    await waitUntil(
      () => (element as unknown as { loading: boolean }).loading === false
    );
    await element.updateComplete;

    const table = element.shadowRoot!.querySelector(
      'table[aria-label="Spend by agent"]'
    )!;
    const headers = Array.from(table.querySelectorAll('th')).map((th) =>
      (th.textContent || '').replace(/\s+/g, ' ').trim()
    );
    const tokenIndex = headers.findIndex((header) =>
      header.startsWith('Tokens')
    );
    const costIndex = headers.findIndex((header) => header.startsWith('Cost'));
    expect(tokenIndex).to.be.greaterThan(-1);
    expect(costIndex).to.be.greaterThan(-1);
    expect(tokenIndex).to.be.lessThan(costIndex);

    const figures = table.querySelector('token-figures') as HTMLElement & {
      updateComplete: Promise<unknown>;
    };
    expect(figures).to.exist;
    await figures.updateComplete;
    const text = (figures.shadowRoot?.textContent || '').replace(/\s+/g, ' ');
    expect(text).to.contain('12.4K in');
    expect(text).to.contain('3.1K out');
    // Expanded on this page: the hit and miss counts, not just the rate.
    expect(text).to.contain('8.2K hit');
    expect(text).to.contain('3.9K miss');
  });

  it('states tokens before cost per user, summed across their sessions', async () => {
    summaryPayload = {
      ...summary,
      usage_by_session: [
        {
          ...summary.usage_by_session[0],
          token_usage: {
            prompt_tokens: 1000,
            completion_tokens: 200,
            total_tokens: 1200,
            input_tokens: 1000,
            output_tokens: 200,
            cache_read_tokens: 600,
            cache_write_tokens: 0,
            uncached_input_tokens: 400,
            cache_hit_ratio: 0.6,
          },
        },
        {
          ...summary.usage_by_session[0],
          runtime_session_id: 'runtime-session-2',
          token_usage: {
            prompt_tokens: 1000,
            completion_tokens: 200,
            total_tokens: 1200,
            input_tokens: 1000,
            output_tokens: 200,
            cache_read_tokens: 400,
            cache_write_tokens: 0,
            uncached_input_tokens: 600,
            cache_hit_ratio: 0.4,
          },
        },
      ],
    };
    const element = (await fixture(html`<cost-view></cost-view>`)) as CostView;
    await waitUntil(
      () => (element as unknown as { loading: boolean }).loading === false
    );
    await element.updateComplete;

    const table = element.shadowRoot!.querySelector(
      'table[aria-label="Spend by user"]'
    )!;
    const headers = Array.from(table.querySelectorAll('th')).map((th) =>
      (th.textContent || '').replace(/\s+/g, ' ').trim()
    );
    const tokenIndex = headers.findIndex((header) =>
      header.startsWith('Tokens')
    );
    const costIndex = headers.findIndex((header) => header.startsWith('Cost'));
    expect(tokenIndex).to.be.greaterThan(-1);
    expect(tokenIndex).to.be.lessThan(costIndex);

    const figures = table.querySelector('token-figures') as HTMLElement & {
      updateComplete: Promise<unknown>;
    };
    await figures.updateComplete;
    const text = (figures.shadowRoot?.textContent || '').replace(/\s+/g, ' ');
    expect(text).to.contain('2K in');
    expect(text).to.contain('400 out');
    // Two sessions at 60% and 40%: the merged rate is read off the merged
    // counts, not averaged.
    expect(text).to.contain('1K hit');
    expect(text).to.contain('1K miss');
  });

  it('carries the shared range control, restates the window and drops Refresh', async () => {
    const element = (await fixture(html`<cost-view></cost-view>`)) as CostView;
    await waitUntil(
      () => (element as unknown as { loading: boolean }).loading === false
    );
    await element.updateComplete;

    // One range vocabulary per page: the shared control, not a bare select.
    const range = element.shadowRoot?.querySelector('time-range-select');
    expect(range).to.exist;
    expect(
      element.shadowRoot?.querySelector('sl-select[label="Date range"]')
    ).to.equal(null);

    // The window the numbers cover is restated beside the control, and the
    // page says how fresh it is instead of offering a manual poll.
    const window = element.shadowRoot
      ?.querySelector('.range-window')
      ?.textContent?.replace(/\s+/g, ' ')
      .trim();
    expect(window).to.contain(' to ');
    // The read time is a clock time, not "just now": the page neither polls
    // nor subscribes, so a relative phrase painted once goes stale in place.
    expect(window).to.match(/read \d/);
    expect(window).to.not.contain('just now');
    expect(
      element.shadowRoot?.querySelector('.range-window')?.getAttribute('title')
    ).to.contain('Loaded');

    const buttons = Array.from(
      element.shadowRoot?.querySelectorAll('sl-button') || []
    ).map((button) => (button.textContent || '').trim());
    expect(buttons).to.not.include('Refresh');
  });

  it('labels stats with the window and prints counts compact', async () => {
    summaryPayload = {
      ...summary,
      total_requests: 32969,
      successful_requests: 32519,
      token_usage: {
        prompt_tokens: 810_400_000,
        completion_tokens: 13_700_000,
        total_tokens: 824_100_000,
      },
    };
    const element = (await fixture(html`<cost-view></cost-view>`)) as CostView;
    await waitUntil(
      () => (element as unknown as { loading: boolean }).loading === false
    );
    await element.updateComplete;

    const metrics = element.shadowRoot
      ?.querySelector('[aria-label="Cost summary metrics"]')
      ?.textContent?.replace(/\s+/g, ' ');
    expect(metrics).to.contain('$ est. · 30d');
    expect(metrics).to.contain('Requests · 30d');
    expect(metrics).to.contain('Tokens · 30d');
    expect(metrics).to.contain('33K');
    expect(metrics).to.contain('824.1M');
    expect(metrics).to.not.contain('824,100,000');

    // A delta is an arrow and a percentage, as on the Overview.
    const delta = (
      element as unknown as {
        percentDelta: (current: number, previous: number) => string;
      }
    ).percentDelta(24, 10);
    expect(delta).to.equal('▲ 140% vs prior 30d');
  });

  it('states the catalog age and offers the action that fixes a price', async () => {
    summaryPayload = {
      ...summary,
      price_catalog: {
        fetched_at: new Date(
          Date.now() - 55 * 24 * 60 * 60 * 1000
        ).toISOString(),
        model_count: 868,
      },
    };
    featuresPayload = { billing: true, model_price_overrides: true };
    const element = (await fixture(html`<cost-view></cost-view>`)) as CostView;
    await waitUntil(
      () => (element as unknown as { loading: boolean }).loading === false
    );
    await element.updateComplete;

    const line = Array.from(
      element.shadowRoot?.querySelectorAll('.metric-detail') || []
    )
      .map((node) => (node.textContent || '').replace(/\s+/g, ' ').trim())
      .find((text) => text.startsWith('Price catalog from'));
    expect(line, 'catalog provenance line').to.exist;
    expect(line).to.contain('(868 models), 55 days old.');
    // "Update recommended" with nothing to click became something to click.
    expect(line).to.not.contain('update recommended');
    // It opens a dialog, so it is a button: an anchor to "#panel-pricing"
    // could never resolve inside a shadow root.
    expect(element.shadowRoot?.querySelector('a.catalog-action')).to.equal(
      null
    );
    const action = element.shadowRoot?.querySelector(
      'button.catalog-action'
    ) as HTMLButtonElement | null;
    expect(action?.textContent?.trim()).to.equal('Override a price');
    action?.click();
    await element.updateComplete;
    expect(
      (element as unknown as { priceDialogOpen: boolean }).priceDialogOpen
    ).to.equal(true);
  });

  it('exposes loading status while analytics are fetched', async () => {
    const element = (await fixture(html`<cost-view></cost-view>`)) as CostView;
    await element.updateComplete;

    const loading = element.shadowRoot?.querySelector(
      '[role="status"][aria-busy="true"]'
    );
    expect(loading).to.exist;
  });

  it('keeps the previous numbers on screen while a range change loads', async () => {
    const element = (await fixture(html`<cost-view></cost-view>`)) as CostView;
    await waitUntil(
      () => (element as unknown as { loading: boolean }).loading === false
    );
    await element.updateComplete;

    const spendBefore = element.shadowRoot?.querySelector(
      '[aria-label="Cost summary metrics"]'
    )?.textContent;
    expect(spendBefore).to.contain('$8.50');

    // The in-flight state of a range change: loading is true and an answer
    // from the previous range is already on screen.
    (element as unknown as { loading: boolean }).loading = true;
    await element.updateComplete;

    const metrics = element.shadowRoot?.querySelector(
      '[aria-label="Cost summary metrics"]'
    );
    expect(metrics, 'the metrics stay in the DOM while loading').to.exist;
    expect(metrics?.textContent).to.contain('$8.50');
    expect(
      element.shadowRoot?.querySelector('.results[aria-busy="true"]'),
      'the results region is marked busy'
    ).to.exist;
    expect(
      element.shadowRoot?.querySelector('.results.is-updating'),
      'the results region is dimmed rather than replaced'
    ).to.exist;
    expect(element.shadowRoot?.textContent).to.not.contain(
      'Loading cost analytics'
    );

    // The stale answers are inert, but the side column's budget and pricing
    // controls have nothing to do with the range, so they stay clickable.
    expect(getComputedStyle(metrics as Element).pointerEvents).to.equal('none');
    const sideColumn = element.shadowRoot?.querySelector(
      '.results.is-updating .side-column'
    );
    expect(sideColumn, 'the controls column is rendered').to.exist;
    expect(getComputedStyle(sideColumn as Element).pointerEvents).to.equal(
      'auto'
    );
  });

  it('describes the page with the tabs it actually has', async () => {
    const element = (await fixture(html`<cost-view></cost-view>`)) as CostView;
    await waitUntil(
      () => (element as unknown as { loading: boolean }).loading === false
    );
    await element.updateComplete;

    const description = element.shadowRoot
      ?.querySelector('view-header')
      ?.getAttribute('description');
    expect(description).to.equal(
      'Understand gateway spend by agent, tool, session and user.'
    );

    const tabs = Array.from(
      element.shadowRoot?.querySelectorAll('sl-tab[slot="nav"]') || []
    ).map((tab) => tab.textContent?.trim());
    expect(tabs).to.deep.equal(['Agents', 'Tools', 'Sessions', 'Users']);
    for (const promised of ['model', 'flow', 'API key']) {
      expect(description).to.not.contain(promised);
    }
  });

  describe('imported usage section', () => {
    const importedUsage = {
      event_count: 4,
      total_tokens: 9680,
      imported_cost: 2.09,
      usage_by_model: [
        {
          model_alias: 'claude-4.5-sonnet',
          source: 'cursor',
          request_count: 2,
          total_tokens: 7090,
          imported_cost: 1.67,
          last_event_at: '2026-07-31T10:05:00Z',
        },
        {
          model_alias: 'composer',
          source: 'cursor',
          request_count: 2,
          total_tokens: 2590,
          imported_cost: 0.42,
          last_event_at: '2026-07-31T10:00:00Z',
        },
      ],
    };

    async function loadView(imported?: unknown): Promise<CostView> {
      if (imported !== undefined) {
        summaryPayload = { ...summary, imported_usage: imported };
      }
      const element = (await fixture(
        html`<cost-view></cost-view>`
      )) as CostView;
      await waitUntil(
        () => (element as unknown as { loading: boolean }).loading === false
      );
      await element.updateComplete;
      return element;
    }

    it('renders totals and a per-model table when imported usage exists', async () => {
      const element = await loadView(importedUsage);

      const totals = element.shadowRoot?.querySelector(
        '[aria-label="Imported usage totals"]'
      );
      expect(totals).to.exist;
      expect(totals?.textContent).to.contain('4');
      expect(totals?.textContent).to.contain('9,680');
      expect(totals?.textContent).to.contain('$2.09');

      const table = element.shadowRoot?.querySelector(
        'table[aria-label="Imported usage by model"]'
      );
      expect(table).to.exist;
      expect(table?.querySelector('th[scope="col"]')).to.exist;
      expect(table?.querySelectorAll('tbody tr').length).to.equal(2);

      const body = table?.querySelector('tbody')?.textContent ?? '';
      expect(body).to.contain('claude-4.5-sonnet');
      expect(body).to.contain('cursor');
      expect(body).to.contain('$1.67');
    });

    it('labels the section so imported spend reads as separate from gateway spend', async () => {
      const element = await loadView(importedUsage);

      const text = element.shadowRoot?.textContent ?? '';
      expect(text).to.contain('Imported usage');
      expect(text).to.contain('Not gateway metered');
    });

    it('keeps imported cost out of the gateway spend metric', async () => {
      const element = await loadView(importedUsage);

      const metrics = element.shadowRoot?.querySelector(
        '[aria-label="Cost summary metrics"]'
      );
      // Gateway spend stays at the summary's estimated_cost (8.50); the
      // imported 2.09 must not be added to it.
      expect(metrics?.textContent).to.contain('$8.50');
      expect(metrics?.textContent).to.not.contain('$10.59');
    });

    it('hides the section when there is no imported usage', async () => {
      const element = await loadView({
        event_count: 0,
        total_tokens: 0,
        imported_cost: 0,
        usage_by_model: [],
      });

      expect(
        element.shadowRoot?.querySelector(
          '[aria-label="Imported usage totals"]'
        )
      ).to.not.exist;
      expect(element.shadowRoot?.textContent).to.not.contain('Imported usage');
    });

    it('hides the section when the response omits the imported block', async () => {
      const element = await loadView();

      expect(
        element.shadowRoot?.querySelector(
          '[aria-label="Imported usage totals"]'
        )
      ).to.not.exist;
      expect(element.shadowRoot?.textContent).to.not.contain('Imported usage');
    });

    describe('conversation rollup', () => {
      // A parent thread with an estimated and a reconciled record, one
      // subagent worker conversation, and one unrelated conversation whose
      // costs were never reported.
      const conversations = [
        {
          conversation_id: 'conv-parent',
          parent_conversation_id: null,
          source: 'cursor',
          event_count: 2,
          total_tokens: 1200,
          estimated_cost: 2.0,
          reconciled_cost: 1.8,
          last_event_at: '2026-07-31T10:05:00Z',
        },
        {
          conversation_id: 'conv-worker',
          parent_conversation_id: 'conv-parent',
          source: 'cursor',
          event_count: 1,
          total_tokens: 300,
          estimated_cost: 0.4,
          reconciled_cost: null,
          last_event_at: '2026-07-31T10:04:00Z',
        },
        {
          conversation_id: 'conv-lonely',
          parent_conversation_id: null,
          source: 'cursor',
          event_count: 1,
          total_tokens: null,
          estimated_cost: null,
          reconciled_cost: null,
          last_event_at: '2026-07-31T09:00:00Z',
        },
      ];

      function conversationTable(element: CostView) {
        return element.shadowRoot?.querySelector(
          'table[aria-label="Imported usage by conversation"]'
        );
      }

      it('nests subagent conversations under their parent thread', async () => {
        const element = await loadView({
          ...importedUsage,
          usage_by_conversation: conversations,
        });

        const table = conversationTable(element);
        expect(table).to.exist;

        const rows = [...(table?.querySelectorAll('tbody tr') ?? [])];
        // parent, nested worker, thread total, lonely conversation.
        expect(rows.length).to.equal(4);
        expect(rows[0]?.textContent).to.contain('conv-parent');
        expect(rows[1]?.textContent).to.contain('conv-worker');
        expect(
          rows[1]?.querySelector('.conversation-child-cell'),
          'worker row must be visually nested under its parent'
        ).to.exist;
        expect(rows[3]?.textContent).to.contain('conv-lonely');
      });

      it('shows per-thread totals with estimated and reconciled kept apart', async () => {
        const element = await loadView({
          ...importedUsage,
          usage_by_conversation: conversations,
        });

        const totalRow = conversationTable(element)?.querySelector(
          'tr.conversation-thread-total'
        );
        expect(totalRow).to.exist;
        const text = totalRow?.textContent ?? '';
        expect(text).to.contain('Thread total');
        expect(text).to.contain('1,500'); // 1200 + 300 tokens
        expect(text).to.contain('$2.40'); // estimated: 2.00 + 0.40
        expect(text).to.contain('$1.80'); // reconciled stays its own figure
        // The two bases must never be summed into one number.
        expect(text).to.not.contain('$4.20');
      });

      it('renders null quantities as "not reported", never as zero', async () => {
        const element = await loadView({
          ...importedUsage,
          usage_by_conversation: conversations,
        });

        const rows = [
          ...(conversationTable(element)?.querySelectorAll('tbody tr') ?? []),
        ];
        const lonely = rows.find((row) =>
          row.textContent?.includes('conv-lonely')
        );
        expect(lonely).to.exist;
        expect(lonely?.textContent).to.contain('not reported');
        expect(lonely?.textContent).to.not.contain('$0.00');

        // The worker's missing reconciled amount is also "not reported".
        const worker = rows.find((row) =>
          row.textContent?.includes('conv-worker')
        );
        expect(worker?.textContent).to.contain('not reported');
      });

      it('keeps the estimated and reconciled columns separate', async () => {
        const element = await loadView({
          ...importedUsage,
          usage_by_conversation: conversations,
        });

        const headers = [
          ...(conversationTable(element)?.querySelectorAll('th[scope="col"]') ??
            []),
        ].map((th) => th.textContent?.trim());
        expect(headers).to.include('Estimated cost');
        expect(headers).to.include('Reconciled cost');
      });

      it('hides the rollup when no conversations are reported', async () => {
        const element = await loadView({
          ...importedUsage,
          usage_by_conversation: [],
        });

        expect(conversationTable(element)).to.not.exist;
        expect(element.shadowRoot?.textContent).to.not.contain('Conversations');
      });

      it('hides the rollup when an older server omits the field', async () => {
        const element = await loadView(importedUsage);

        expect(conversationTable(element)).to.not.exist;
      });
    });
  });

  it('renders the page title and description in the view header', async () => {
    const element = (await fixture(html`<cost-view></cost-view>`)) as CostView;
    await element.updateComplete;

    const header = element.shadowRoot?.querySelector('view-header');
    expect(header).to.exist;
    await (header as LitElement).updateComplete;

    const h1 = header?.shadowRoot?.querySelector('h1');
    // One name for the page: the sidebar, the Overview link and the h1 all
    // say Cost.
    expect(h1?.textContent?.trim()).to.equal('Cost');

    const description = header?.shadowRoot?.querySelector('.description');
    expect(description?.textContent).to.contain(
      'Understand gateway spend by agent, tool, session and user.'
    );
  });

  describe('unpriced reprice banner', () => {
    const unpricedSummary = {
      unpriced_requests: 2,
      unpriced_tokens: 6000,
      unpriced_models: [
        {
          model: 'openai-compatible/muse-spark',
          requests: 2,
          tokens: 6000,
        },
      ],
    };

    let originalInterval: number;
    let originalAttempts: number;

    beforeEach(() => {
      // Shrink the async poll so tests do not wait real minutes. The
      // statics are readonly at compile time only; restore them after.
      const viewClass = CostView as unknown as {
        REPRICE_POLL_INTERVAL_MS: number;
        REPRICE_POLL_MAX_ATTEMPTS: number;
      };
      originalInterval = viewClass.REPRICE_POLL_INTERVAL_MS;
      originalAttempts = viewClass.REPRICE_POLL_MAX_ATTEMPTS;
      viewClass.REPRICE_POLL_INTERVAL_MS = 1;
      featuresPayload = { billing: true, model_price_overrides: true };
      summaryPayload = { ...summary, ...unpricedSummary };
    });

    afterEach(() => {
      const viewClass = CostView as unknown as {
        REPRICE_POLL_INTERVAL_MS: number;
        REPRICE_POLL_MAX_ATTEMPTS: number;
      };
      viewClass.REPRICE_POLL_INTERVAL_MS = originalInterval;
      viewClass.REPRICE_POLL_MAX_ATTEMPTS = originalAttempts;
    });

    async function loadView(): Promise<CostView> {
      const element = (await fixture(
        html`<cost-view></cost-view>`
      )) as CostView;
      await waitUntil(
        () => (element as unknown as { loading: boolean }).loading === false
      );
      await element.updateComplete;
      return element;
    }

    function banner(element: CostView) {
      return element.shadowRoot?.querySelector('#panel-pricing-catalog');
    }

    function bannerButton(element: CostView, label: string) {
      return [...(banner(element)?.querySelectorAll('sl-button') ?? [])].find(
        (button) => button.textContent?.trim() === label
      );
    }

    it('renders the unpriced count and names the affected models', async () => {
      const element = await loadView();

      const text = banner(element)?.textContent ?? '';
      expect(text).to.contain('2');
      expect(text).to.contain('6,000');
      expect(text).to.contain('openai-compatible/muse-spark');
      expect(text).to.contain('missing from the price catalog');
      expect(bannerButton(element, 'Reprice now')).to.exist;
    });

    it('wires the override CTA to the price override dialog, pre-filled', async () => {
      const element = await loadView();

      const cta = bannerButton(element, 'Set price override');
      expect(cta, 'override CTA must be present').to.exist;
      (cta as HTMLElement).click();
      await element.updateComplete;

      const state = element as unknown as {
        priceDialogOpen: boolean;
        priceModelAlias: string;
      };
      expect(state.priceDialogOpen).to.equal(true);
      expect(state.priceModelAlias).to.equal('openai-compatible/muse-spark');

      const dialog = element.shadowRoot?.querySelector(
        'sl-dialog[label="Add price override"]'
      );
      expect(dialog).to.exist;
      expect((dialog as unknown as { open: boolean }).open).to.equal(true);
    });

    it('sync reprice reports the actual counts and reloads the banner', async () => {
      repriceResult = {
        ...repriceResult,
        submitted_async: false,
        rows_examined: 2,
        rows_updated: 2,
      };
      // The reprice "works": after the POST the window is fully priced.
      onReprice = () => {
        summaryPayload = {
          ...summaryPayload,
          unpriced_requests: 0,
          unpriced_tokens: 0,
          unpriced_models: [],
        };
      };
      const element = await loadView();

      const reprice = bannerButton(element, 'Reprice now');
      expect(reprice).to.exist;
      (reprice as HTMLElement).click();

      await waitUntil(() => {
        const state = element as unknown as {
          repriceNotice: string | null;
          repricing: boolean;
        };
        return (
          !state.repricing && state.repriceNotice?.includes('Reprice finished')
        );
      });
      await element.updateComplete;

      const state = element as unknown as { repriceNotice: string | null };
      expect(state.repriceNotice).to.contain('2 of 2 requests priced');

      // The reloaded summary has nothing unpriced: the warning banner is
      // replaced by the success notice.
      expect(banner(element)).to.not.exist;
      const success = element.shadowRoot?.querySelector(
        'sl-alert[variant="success"]'
      );
      expect(success?.textContent).to.contain('2 of 2 requests priced');
    });

    it('async reprice polls the summary, then reloads and reports', async () => {
      repriceResult = {
        ...repriceResult,
        submitted_async: true,
        rows_examined: null,
        rows_updated: null,
        rows_skipped: null,
      };
      // The background worker finishes before the first poll: from the
      // second summary fetch on, the window comes back fully priced.
      let summaryFetches = 0;
      summaryResponder = () => {
        summaryFetches += 1;
        return summaryFetches > 1
          ? {
              ...summaryPayload,
              unpriced_requests: 0,
              unpriced_tokens: 0,
              unpriced_models: [],
            }
          : summaryPayload;
      };
      const element = await loadView();

      const reprice = bannerButton(element, 'Reprice now');
      expect(reprice).to.exist;
      (reprice as HTMLElement).click();

      await waitUntil(() => {
        const state = element as unknown as {
          repriceNotice: string | null;
          repricing: boolean;
        };
        return (
          !state.repricing && state.repriceNotice?.includes('Reprice finished')
        );
      });
      await element.updateComplete;

      const state = element as unknown as { repriceNotice: string | null };
      expect(state.repriceNotice).to.contain(
        'every request in this window now has a cost estimate'
      );
      // The summary was polled and then reloaded, not left stale.
      expect(summaryFetches).to.be.greaterThan(1);
      expect(banner(element)).to.not.exist;
    });

    it('async reprice with no change says so instead of claiming success', async () => {
      const viewClass = CostView as unknown as {
        REPRICE_POLL_MAX_ATTEMPTS: number;
      };
      viewClass.REPRICE_POLL_MAX_ATTEMPTS = 3;
      repriceResult = {
        ...repriceResult,
        submitted_async: true,
        rows_examined: null,
        rows_updated: null,
        rows_skipped: null,
      };
      const element = await loadView();

      const reprice = bannerButton(element, 'Reprice now');
      expect(reprice).to.exist;
      (reprice as HTMLElement).click();

      await waitUntil(() => {
        const state = element as unknown as {
          repriceNotice: string | null;
          repricing: boolean;
        };
        return !state.repricing && state.repriceNotice?.includes('No change');
      });
      await element.updateComplete;

      const state = element as unknown as { repriceNotice: string | null };
      expect(state.repriceNotice).to.contain('price override');
      // The banner stays, still naming the unpriceable model.
      expect(banner(element)?.textContent).to.contain(
        'openai-compatible/muse-spark'
      );
    });

    it('async reprice never reports a negative priced count', async () => {
      repriceResult = {
        ...repriceResult,
        submitted_async: true,
        rows_examined: null,
        rows_updated: null,
        rows_skipped: null,
      };
      // Live traffic adds unpriced rows during the poll window: the count
      // moves (2 -> 5), so the poll stops early, but nothing was priced.
      let summaryFetches = 0;
      summaryResponder = () => {
        summaryFetches += 1;
        return summaryFetches > 1
          ? { ...summaryPayload, unpriced_requests: 5, unpriced_tokens: 9000 }
          : summaryPayload;
      };
      const element = await loadView();

      const reprice = bannerButton(element, 'Reprice now');
      expect(reprice).to.exist;
      (reprice as HTMLElement).click();

      await waitUntil(() => {
        const state = element as unknown as {
          repriceNotice: string | null;
          repricing: boolean;
        };
        return !state.repricing && state.repriceNotice?.includes('No change');
      });
      await element.updateComplete;

      const state = element as unknown as { repriceNotice: string | null };
      expect(state.repriceNotice).to.not.match(/-\d+ requests priced/);
      expect(state.repriceNotice).to.contain('price override');
      // The banner reloads to the grown count, still naming the model.
      expect(banner(element)?.textContent).to.contain('5');
    });

    it('async reprice finishing after the last poll still reports the decrease', async () => {
      const viewClass = CostView as unknown as {
        REPRICE_POLL_MAX_ATTEMPTS: number;
      };
      viewClass.REPRICE_POLL_MAX_ATTEMPTS = 3;
      repriceResult = {
        ...repriceResult,
        submitted_async: true,
        rows_examined: null,
        rows_updated: null,
        rows_skipped: null,
      };
      // Every poll sees the stale count, so the poll times out; the worker
      // finishes before the final reload, which shows one of the two
      // requests got priced. The notice must come from the reloaded
      // summary, not from the poll's early-stop signal.
      let summaryFetches = 0;
      summaryResponder = () => {
        summaryFetches += 1;
        // Initial load, previous-range fetch and 3 polls see the stale
        // count; the final reload (fetch 6) sees the decrease.
        return summaryFetches > 5
          ? { ...summaryPayload, unpriced_requests: 1, unpriced_tokens: 2000 }
          : summaryPayload;
      };
      const element = await loadView();

      const reprice = bannerButton(element, 'Reprice now');
      expect(reprice).to.exist;
      (reprice as HTMLElement).click();

      await waitUntil(() => {
        const state = element as unknown as {
          repriceNotice: string | null;
          repricing: boolean;
        };
        return (
          !state.repricing && state.repriceNotice?.includes('Reprice finished')
        );
      });
      await element.updateComplete;

      const state = element as unknown as { repriceNotice: string | null };
      expect(state.repriceNotice).to.contain('1 requests priced');
      expect(state.repriceNotice).to.contain('1 still unpriced');
      // One row remains unpriced, so the banner stays up.
      expect(banner(element)).to.exist;
    });

    it('override CTA does not pre-fill the coalesced unknown model', async () => {
      summaryPayload = {
        ...summary,
        unpriced_requests: 3,
        unpriced_tokens: 4200,
        unpriced_models: [{ model: 'unknown', requests: 3, tokens: 4200 }],
      };
      const element = await loadView();

      const cta = bannerButton(element, 'Set price override');
      expect(cta, 'override CTA must be present').to.exist;
      (cta as HTMLElement).click();
      await element.updateComplete;

      const state = element as unknown as {
        priceDialogOpen: boolean;
        priceModelAlias: string;
      };
      expect(state.priceDialogOpen).to.equal(true);
      // "unknown" is the backend placeholder for rows with no model alias;
      // pre-filling it would create a no-op override, so the field is empty.
      expect(state.priceModelAlias).to.equal('');
    });
  });
});
