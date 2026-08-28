import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import type { LitElement } from 'lit';
import sinon from 'sinon';
import '../../components/view-header.ts';
import './cost-view.ts';
import type { CostView } from './cost-view';
import { invalidateApiCaches } from '../../api';

describe('CostView', () => {
  let fetchStub: sinon.SinonStub;
  // Per-test copy of the payload so a test can add fields (e.g. the imported
  // usage block) without leaking into the others.
  let summaryPayload: Record<string, unknown>;

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
    fetchStub = sinon.stub(window, 'fetch');
    fetchStub.callsFake(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString();

      if (url.includes('/api/v1/cost/summary')) {
        return new Response(JSON.stringify(summaryPayload), {
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
        return new Response(JSON.stringify({ features: { billing: true } }), {
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

  it('exposes loading status while analytics are fetched', async () => {
    const element = (await fixture(html`<cost-view></cost-view>`)) as CostView;
    await element.updateComplete;

    const loading = element.shadowRoot?.querySelector(
      '[role="status"][aria-busy="true"]'
    );
    expect(loading).to.exist;
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
    expect(h1?.textContent).to.contain('Cost Analytics');

    const description = header?.shadowRoot?.querySelector('.description');
    expect(description?.textContent).to.contain(
      'Understand gateway spend by model, agent, session, flow, and API key.'
    );
  });
});
