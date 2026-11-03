import { fixture, html, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import './flow-execution-view';
import type { FlowExecutionView } from './flow-execution-view';
import {
  TRANSCRIPT_HISTORY_WINDOW,
  TRANSCRIPT_PAGE_SIZE,
} from './flow-execution-view';

/**
 * The transcript of a large run is read and drawn a page at a time (#1208).
 *
 * A long review run replays its whole conversation in every model call. The
 * transcript used to read every call with its full payload in one response
 * and hand all of it to the conversation builder; on a run of a few million
 * tokens that killed the browser renderer seconds after the page painted.
 * The synthetic run here has 300 model calls; the server answers the way the
 * delta page read does (each call carries only what it adds).
 */
describe('FlowExecutionView transcript paging', () => {
  const CALLS = 300;
  const TEXT = 'y'.repeat(4000);
  let fetchStub: sinon.SinonStub;
  /** Delta page reads left to fail with a 500 before they succeed. */
  let failPages = 0;
  /** Model calls the run has made so far; a test grows it mid-run. */
  let runCalls = CALLS;
  /** When set, delta page reads wait for it before answering. */
  let pageGate: Promise<void> | null = null;

  const json = (body: unknown, status = 200) =>
    new Response(JSON.stringify(body), {
      status,
      headers: { 'Content-Type': 'application/json' },
    });

  const at = (index: number) =>
    new Date(Date.UTC(2026, 8, 1, 10, 0, 0) + index * 1000).toISOString();

  /** Call `index` of the run as the delta page read returns it. */
  const deltaCall = (index: number) => ({
    id: `call-${index}`,
    execution_id: 'exec-big',
    timestamp: at(index),
    type: 'model_gateway_call',
    payload: {
      api_usage_id: `usage-${index}`,
      model_alias: 'openai/gpt-5',
      outcome: 'success',
      status_code: 200,
      total_tokens: 15000,
      conversation_preview: {
        messages: [
          { position: 0, role: 'user', text: 'Review this pull request' },
          {
            position: 2 * index + 1,
            role: 'assistant',
            text: `Reading file ${index}`,
          },
          { position: 2 * index + 2, role: 'tool', text: `${index}:${TEXT}` },
        ],
        metadata: { history_elided_count: index ? 2 * index : 0 },
      },
      request: {
        messages: [{ role: 'tool', content: `${index}:${TEXT}` }],
      },
    },
  });

  const metadataCall = (index: number) => ({
    id: `call-${index}`,
    execution_id: 'exec-big',
    timestamp: at(index),
    type: 'model_gateway_call',
    payload: {
      api_usage_id: `usage-${index}`,
      model_alias: 'openai/gpt-5',
      outcome: 'success',
      status_code: 200,
      total_tokens: 15000,
    },
  });

  /** The newest `size` calls older than `before`, newest first. */
  const page = (url: URL) => {
    const size = Number(url.searchParams.get('tail'));
    const before = url.searchParams.get('before');
    const end = before ? Number(before.replace('call-', '')) : runCalls;
    const start = Math.max(0, end - size);
    const logs = [];
    for (let index = start; index < end; index += 1)
      logs.push(deltaCall(index));
    const hasMore = start > 0;
    const oldest = logs[0];
    logs.reverse();
    return {
      logs,
      source: 'database',
      has_more: hasMore,
      pagination: {
        limit: size,
        offset: null,
        next_offset: null,
        next_cursor: hasMore ? oldest.id : null,
        total: runCalls,
        has_more: hasMore,
      },
    };
  };

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
    window.history.replaceState(
      {},
      '',
      `${window.location.pathname}?tab=transcript`
    );
    fetchStub = sinon.stub(window, 'fetch');
    fetchStub.callsFake(async (input: RequestInfo | URL) => {
      const url = new URL(
        typeof input === 'string' ? input : input.toString(),
        window.location.origin
      );
      const path = url.pathname;
      if (path === '/api/v1/flows/executions/exec-big')
        return json({
          id: 'exec-big',
          flow_id: 'flow-big',
          flow_name: 'Pull request review',
          status: 'SUCCEEDED',
          start_time: at(0),
          end_time: at(CALLS),
          total_tokens: 4_390_000,
        });
      if (path === '/api/v1/flows/executions/exec-big/gateway-events') {
        if (url.searchParams.get('metadata_only') === 'true') {
          const logs = [];
          for (let index = CALLS - 200; index < CALLS; index += 1)
            logs.push(metadataCall(index));
          return json({ logs, source: 'database', has_more: true });
        }
        if (url.searchParams.get('history') === 'delta') {
          if (pageGate) await pageGate;
          if (failPages > 0) {
            failPages -= 1;
            return json({ detail: 'boom' }, 500);
          }
          return json(page(url));
        }
        // The old transcript read: never answered on this run.
        return json({ detail: 'unbounded read' }, 500);
      }
      if (path === '/api/v1/flows/executions/exec-big/logs')
        return json({ logs: [], source: 'database', has_more: false });
      if (path === '/api/v1/flows/executions/exec-big/metrics')
        return json({ tool_calls: 14, token_usage: { total_tokens: 1 } });
      if (path === '/api/v1/flows/flow-big')
        return json({ id: 'flow-big', name: 'Pull request review' });
      return json({ detail: 'Not found' }, 404);
    });
  });

  afterEach(() => {
    failPages = 0;
    runCalls = CALLS;
    pageGate = null;
    fetchStub.restore();
    localStorage.clear();
    window.history.replaceState({}, '', window.location.pathname);
  });

  const gatewayUrls = () =>
    fetchStub
      .getCalls()
      .map((c) => new URL(String(c.args[0]), window.location.origin))
      .filter((url) => url.pathname.endsWith('/gateway-events'));

  const transcriptUrls = () =>
    gatewayUrls().filter(
      (url) => url.searchParams.get('metadata_only') !== 'true'
    );

  /** Every node under `root`, through open shadow roots. */
  const countNodes = (root: Node): number => {
    let count = 0;
    const walk = (node: Node) => {
      count += 1;
      const shadow = (node as Element).shadowRoot;
      if (shadow) walk(shadow);
      node.childNodes.forEach(walk);
    };
    walk(root);
    return count;
  };

  async function openTranscript() {
    const element = (await fixture(
      html`<flow-execution-view></flow-execution-view>`
    )) as FlowExecutionView;
    element.executionId = 'exec-big';
    await waitUntil(
      () => (element as any).transcriptEvents.length > 0,
      'The transcript page did not load',
      { timeout: 5000 }
    );
    await element.updateComplete;
    const chat = element.shadowRoot!.querySelector('session-chat-view') as any;
    await chat.updateComplete;
    return { element, chat };
  }

  it('reads one bounded delta page of model calls, never the whole run', async () => {
    const { element } = await openTranscript();

    const urls = transcriptUrls();
    expect(urls).to.have.length(1);
    const [url] = urls;
    expect(url.searchParams.get('tail')).to.equal(String(TRANSCRIPT_PAGE_SIZE));
    expect(url.searchParams.get('history')).to.equal('delta');
    expect(url.searchParams.get('history_window')).to.equal(
      String(TRANSCRIPT_HISTORY_WINDOW)
    );
    expect(url.searchParams.get('model_calls_only')).to.equal('true');
    expect((element as any).transcriptEvents).to.have.length(
      TRANSCRIPT_PAGE_SIZE
    );
    // The timeline keeps its own metadata read; the transcript page does not
    // replace the events the timeline and strip are built from.
    await waitUntil(() => (element as any).gatewayEventsLoaded);
    expect((element as any).gatewayEvents).to.have.length(200);
  });

  it('says how much of the run it shows and loads earlier calls on request', async () => {
    const { element, chat } = await openTranscript();
    const note = () =>
      element
        .shadowRoot!.querySelector('[data-testid="transcript-page-note"]')
        ?.textContent?.replace(/\s+/g, ' ')
        .trim();
    expect(note()).to.equal(
      `Showing the latest ${TRANSCRIPT_PAGE_SIZE} of ${CALLS} model calls.`
    );
    expect(chat.hasMoreEvents).to.equal(true);

    const button = chat.shadowRoot!.querySelector(
      '[data-testid="load-earlier"]'
    ) as HTMLButtonElement;
    button.click();
    await waitUntil(
      () =>
        (element as any).transcriptEvents.length === 2 * TRANSCRIPT_PAGE_SIZE,
      'Earlier calls were not merged'
    );
    const urls = transcriptUrls();
    expect(urls).to.have.length(2);
    expect(urls[1].searchParams.get('before')).to.equal(
      `call-${CALLS - TRANSCRIPT_PAGE_SIZE}`
    );
    const ids = (element as any).transcriptEvents.map((e: any) => e.id);
    expect(new Set(ids).size).to.equal(ids.length);
    await element.updateComplete;
    expect(note()).to.equal(
      `Showing the latest ${2 * TRANSCRIPT_PAGE_SIZE} of ${CALLS} model calls.`
    );
  });

  it('renders the first page of a large run within a node and time budget', async () => {
    const started = performance.now();
    const { chat } = await openTranscript();
    const elapsed = performance.now() - started;
    const pageNodes = countNodes(chat.shadowRoot!);

    // The same conversation component holding every call of the run, which
    // is what the old single read handed it.
    const whole = (await fixture(
      html`<session-chat-view></session-chat-view>`
    )) as any;
    const all = [];
    for (let index = CALLS - 1; index >= 0; index -= 1)
      all.push(deltaCall(index));
    whole.events = all;
    await whole.updateComplete;
    const wholeNodes = countNodes(whole.shadowRoot!);

    console.info(
      `transcript nodes: page ${pageNodes}, whole run ${wholeNodes}; ` +
        `first page in ${Math.round(elapsed)} ms`
    );
    // A page is a bounded slice of the conversation, whatever the run's
    // length: under a fifth of the 300-call run, and a fixed ceiling.
    expect(pageNodes, `page rendered ${pageNodes} nodes`).to.be.below(6000);
    expect(pageNodes * 5).to.be.below(wholeNodes);
    expect(elapsed, `took ${Math.round(elapsed)} ms`).to.be.below(5000);
  });
  it('adds a live model call without a row id to the transcript', async () => {
    const { element } = await openTranscript();
    // The websocket event carries the usage id, not the stored row id.
    (element as any).handleWebSocketMessage({
      execution_id: 'exec-big',
      timestamp: at(CALLS),
      type: 'model_gateway_call',
      payload: {
        api_usage_id: 'usage-live',
        outcome: 'success',
        conversation_preview: {
          messages: [{ role: 'assistant', text: 'Live reply' }],
        },
      },
    });
    await element.updateComplete;

    const events = (element as any).transcriptEvents;
    expect(events).to.have.length(TRANSCRIPT_PAGE_SIZE + 1);
    expect(events[0].payload.api_usage_id).to.equal('usage-live');
    expect((element as any).transcriptTotal).to.equal(CALLS + 1);
  });

  it('re-reads the newest transcript page after a realtime gap', async () => {
    const { element } = await openTranscript();
    expect(transcriptUrls()).to.have.length(1);

    await (element as any).recoverRealtime();
    await waitUntil(
      () =>
        transcriptUrls().length === 2 && !(element as any).transcriptLoading,
      'Recovery did not re-read the transcript'
    );
    expect(transcriptUrls()[1].searchParams.get('before')).to.equal(null);
    const keys = (element as any).transcriptEvents.map(
      (e: any) => e.payload.api_usage_id
    );
    // The same calls read twice are kept once.
    expect(new Set(keys).size).to.equal(keys.length);
    expect(keys).to.have.length(TRANSCRIPT_PAGE_SIZE);
  });

  it('retries a failed first transcript page', async () => {
    failPages = 1;
    const element = (await fixture(
      html`<flow-execution-view></flow-execution-view>`
    )) as FlowExecutionView;
    element.executionId = 'exec-big';
    await waitUntil(
      () => (element as any).transcriptError !== null,
      'The failed page was not reported'
    );
    await element.updateComplete;
    const retry = element.shadowRoot!.querySelector(
      '[data-testid="transcript-retry"]'
    ) as HTMLElement;
    expect(retry, 'no retry action on the error').to.exist;

    retry.click();
    await waitUntil(
      () => (element as any).transcriptEvents.length === TRANSCRIPT_PAGE_SIZE,
      'Retry did not load the page'
    );
    expect((element as any).transcriptError).to.equal(null);

    // Reopening the tab after a failure also retries.
    failPages = 1;
    (element as any).transcriptEvents = [];
    (element as any).transcriptRequested = false;
    (element as any).transcriptPageLoaded = false;
    await (element as any).loadTranscript();
    expect((element as any).transcriptError).to.not.equal(null);
    (element as any).handleTabShow({ detail: { name: 'timeline' } });
    (element as any).handleTabShow({ detail: { name: 'transcript' } });
    await waitUntil(
      () => (element as any).transcriptEvents.length === TRANSCRIPT_PAGE_SIZE,
      'Reopening the tab did not retry'
    );
  });
  it('retries the first page when a live call arrived during its failure', async () => {
    failPages = 1;
    let open!: () => void;
    pageGate = new Promise<void>((resolve) => (open = resolve));
    const element = (await fixture(
      html`<flow-execution-view></flow-execution-view>`
    )) as FlowExecutionView;
    element.executionId = 'exec-big';
    await waitUntil(() => (element as any).transcriptLoading);
    (element as any).handleWebSocketMessage({
      execution_id: 'exec-big',
      timestamp: at(CALLS),
      type: 'model_gateway_call',
      payload: { api_usage_id: 'usage-live', outcome: 'success' },
    });
    pageGate = null;
    open();
    await waitUntil(() => (element as any).transcriptError !== null);
    expect((element as any).transcriptEvents).to.have.length(1);
    await element.updateComplete;

    (
      element.shadowRoot!.querySelector(
        '[data-testid="transcript-retry"]'
      ) as HTMLElement
    ).click();
    await waitUntil(
      () =>
        (element as any).transcriptEvents.length === TRANSCRIPT_PAGE_SIZE + 1,
      'Retry did not read the first page'
    );
    expect(transcriptUrls().at(-1)!.searchParams.get('before')).to.equal(null);
  });

  it('retries a failed newest-page refresh, not an older page', async () => {
    const { element, chat } = await openTranscript();
    (
      chat.shadowRoot!.querySelector('[data-testid="load-earlier"]') as any
    ).click();
    await waitUntil(
      () =>
        (element as any).transcriptEvents.length === 2 * TRANSCRIPT_PAGE_SIZE
    );
    failPages = 1;
    await (element as any).recoverRealtime();
    await waitUntil(() => (element as any).transcriptError !== null);
    await element.updateComplete;
    const before = transcriptUrls().length;

    (
      element.shadowRoot!.querySelector(
        '[data-testid="transcript-retry"]'
      ) as HTMLElement
    ).click();
    await waitUntil(() => transcriptUrls().length === before + 1);
    expect(transcriptUrls().at(-1)!.searchParams.get('before')).to.equal(null);
  });

  it('keeps the older-page cursor across a realtime refresh', async () => {
    const { element, chat } = await openTranscript();
    (
      chat.shadowRoot!.querySelector('[data-testid="load-earlier"]') as any
    ).click();
    await waitUntil(
      () =>
        (element as any).transcriptEvents.length === 2 * TRANSCRIPT_PAGE_SIZE
    );
    const cursor = (element as any).transcriptNextCursor;
    expect(cursor).to.equal(`call-${CALLS - 2 * TRANSCRIPT_PAGE_SIZE}`);

    await (element as any).recoverRealtime();
    await waitUntil(
      () => transcriptUrls().length === 3 && !(element as any).transcriptLoading
    );
    expect((element as any).transcriptNextCursor).to.equal(cursor);
  });
  it('reaches calls missed in a realtime gap longer than a page', async () => {
    const { element } = await openTranscript();
    // The socket drops while the run makes three more pages of calls.
    runCalls = CALLS + 3 * TRANSCRIPT_PAGE_SIZE;
    await (element as any).recoverRealtime();
    await waitUntil(
      () => transcriptUrls().length === 2 && !(element as any).transcriptLoading
    );
    // The refreshed page does not touch what was shown, so paging back
    // starts from it and walks through the gap.
    expect((element as any).transcriptNextCursor).to.equal(
      `call-${runCalls - TRANSCRIPT_PAGE_SIZE}`
    );
    for (let read = 0; read < 2; read += 1) {
      const count = (element as any).transcriptEvents.length;
      await (element as any).loadTranscript(true);
      expect((element as any).transcriptEvents.length).to.be.above(count);
    }
    const keys = new Set(
      (element as any).transcriptEvents.map((e: any) => e.payload.api_usage_id)
    );
    for (let index = CALLS; index < runCalls; index += 1)
      expect(keys.has(`usage-${index}`), `usage-${index}`).to.equal(true);
  });
});
