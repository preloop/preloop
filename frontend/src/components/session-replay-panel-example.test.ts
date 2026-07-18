/**
 * Tests for the bundled example session shown in the Optimize tab.
 *
 * The example only earns its place if it is unmistakably labelled as sample
 * data, so these tests pin the labelling and the "only when empty" gating
 * rather than the cosmetics.
 */
import { expect, fixture, html, waitUntil } from '@open-wc/testing';
import './session-replay-panel';
import type { SessionReplayPanel } from './session-replay-panel';
import type { FlowGatewayEvent } from '../types';

/** A minimal captured call, so the panel reaches the Optimize view at all. */
const EVENTS: FlowGatewayEvent[] = [
  {
    id: 'event-1',
    execution_id: 'exec-1',
    timestamp: '2026-06-07T12:01:00Z',
    type: 'model_gateway_call',
    payload: {
      model_alias: 'gpt-test',
      outcome: 'success',
      total_tokens: 200,
      prompt_tokens: 180,
      completion_tokens: 20,
      estimated_cost: 0.001,
      conversation_preview: {
        messages: [{ role: 'user', text: 'hello' }],
      },
    },
  },
];

const SESSION = {
  id: 'session-1',
  sourceId: 'hermes-1',
  sourceType: 'hermes',
  title: 'Hermes',
  subtitle: null,
  sessionReference: null,
  runtimePrincipalName: 'Hermes',
  flowName: null,
  flowExecutionId: null,
  status: 'active_now',
  startedAt: '2026-06-07T12:00:00Z',
  lastActivityAt: '2026-06-07T12:05:00Z',
  endedAt: null,
  totalRequests: 0,
  successfulRequests: 0,
  failedRequests: 0,
  tokenUsage: { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 },
  estimatedCost: 0,
  latestModelAlias: null,
  latestProviderName: null,
  canLoadEvents: true,
  raw: {},
};

const EXAMPLE_NOTICE =
  'This is a bundled example session, not your data. It is analyzed by the ' +
  'same engine that analyzes your own sessions.';

const EXAMPLE_RESPONSE = {
  generated_by: 'local',
  fast_model_name: null,
  suggestions: [
    {
      id: 'scope-tools',
      title: 'Scope down advertised tools',
      description: '6 of 9 advertised tools were never called.',
      expected_savings_tokens: 1962,
      expected_savings_usd: 0.006755,
      confidence: 'high',
      action_label: 'Scope tool access',
      evidence: ['6 of 9 advertised tools never invoked'],
      evidence_event_ids: [],
    },
  ],
  waste_score: 41,
  potential_savings_tokens: 7114,
  potential_savings_usd: 0.024491,
  analyzed_scope_total_tokens: 18161,
  analyzed_scope_estimated_cost: 0.062523,
  is_example: true,
  example_notice: EXAMPLE_NOTICE,
  example_provenance: 'Constructed example modelled on a CI-triage session.',
  example_title: 'Example: CI failure triage (bundled sample)',
  example_pricing_note: 'Priced at Claude Sonnet 4 public list rates.',
};

let originalFetch: typeof window.fetch;
let exampleRequestCount = 0;

function stubFetch(status = 200) {
  exampleRequestCount = 0;
  window.fetch = (async (input: RequestInfo | URL) => {
    const url = typeof input === 'string' ? input : input.toString();
    if (url.includes('/runtime-sessions/example/optimization')) {
      exampleRequestCount += 1;
      return new Response(JSON.stringify(EXAMPLE_RESPONSE), {
        status,
        headers: { 'Content-Type': 'application/json' },
      });
    }
    return new Response('{}', { status: 200 });
  }) as typeof window.fetch;
}

/**
 * Mount the panel in Optimize mode.
 *
 * Suggestions and result are bound in the template rather than assigned after
 * mount, so the first render already sees them — otherwise the panel would
 * briefly observe an empty result and fetch the example spuriously.
 */
async function mountOptimizePanel(
  options: {
    suggestions?: unknown[] | null;
    result?: Record<string, unknown> | null;
  } = {}
): Promise<SessionReplayPanel> {
  const element = await fixture<SessionReplayPanel>(html`
    <session-replay-panel
      .session=${SESSION}
      .events=${EVENTS}
      .replayMode=${'optimize'}
      .optimizationEnabled=${true}
      .optimizationSuggestions=${options.suggestions ?? null}
      .optimizationResult=${options.result ?? null}
    ></session-replay-panel>
  `);
  await element.updateComplete;
  return element;
}

describe('session-replay-panel bundled example', () => {
  beforeEach(() => {
    originalFetch = window.fetch;
    localStorage.setItem('accessToken', 'test-token');
  });

  afterEach(() => {
    window.fetch = originalFetch;
    localStorage.removeItem('accessToken');
  });

  it('shows the example with an unmistakable label when empty', async () => {
    stubFetch();
    const element = await mountOptimizePanel();

    await waitUntil(
      () => Boolean(element.shadowRoot?.querySelector('.example-banner')),
      'example banner should render'
    );

    const text = element.shadowRoot?.textContent || '';
    // The honesty contract: it must say it is an example AND not the user's data.
    expect(text).to.include('Example');
    expect(text).to.include('not your data');
    expect(text).to.include('Example: CI failure triage (bundled sample)');
    // Provenance is disclosed rather than implied.
    expect(text).to.include('Constructed example');
  });

  it('labels the example before showing its savings figure', async () => {
    stubFetch();
    const element = await mountOptimizePanel();
    await waitUntil(
      () => Boolean(element.shadowRoot?.querySelector('.example-banner')),
      'example banner should render'
    );

    const container = element.shadowRoot?.querySelector(
      '.example-optimization'
    ) as HTMLElement;
    const banner = container.querySelector('.example-banner');
    const results = container.querySelector('session-optimization-panel');

    expect(banner, 'banner present').to.exist;
    expect(results, 'results present').to.exist;
    // The label must precede the numbers in document order, so the figure
    // cannot be read without the disclaimer.
    expect(
      banner!.compareDocumentPosition(results!) &
        Node.DOCUMENT_POSITION_FOLLOWING
    ).to.be.greaterThan(0);
  });

  const realSuggestion = (savingsTokens: number) => ({
    id: 'trim-context',
    title: 'Trim prompt context',
    description: 'Real suggestion for the real session.',
    expectedSavingsTokens: savingsTokens,
    expectedSavingsUsd: 0.01,
    confidence: 'high' as const,
    actionLabel: 'Review context segments',
    evidence: ['900 prompt tokens'],
    evidenceEventIds: [],
    action: null,
  });

  it('does not show the example when the session has real savings', async () => {
    stubFetch();
    const element = await mountOptimizePanel({
      suggestions: [realSuggestion(500)],
      result: {
        generated_by: 'local',
        fast_model_name: null,
        suggestions: [],
        potential_savings_tokens: 500,
      },
    });
    await new Promise((resolve) => setTimeout(resolve, 50));

    expect(element.shadowRoot?.querySelector('.example-banner')).to.not.exist;
    expect(exampleRequestCount, 'no example fetch when not needed').to.equal(0);
  });

  it('shows the example when a short session yields zero savings', async () => {
    // The reported symptom: a tiny session still produces a fallback
    // suggestion, but with no savings figure — the tab looks broken.
    stubFetch();
    const element = await mountOptimizePanel({
      suggestions: [realSuggestion(0)],
      result: {
        generated_by: 'local',
        fast_model_name: null,
        suggestions: [],
        potential_savings_tokens: 0,
      },
    });

    await waitUntil(
      () => Boolean(element.shadowRoot?.querySelector('.example-banner')),
      'example should back-fill a zero-savings result'
    );
    const text = element.shadowRoot?.textContent || '';
    expect(text).to.include('not your data');
    // The session's own result panel is still rendered alongside the example,
    // so the user keeps their (zero-savings) analysis rather than losing it.
    const panels = element.shadowRoot?.querySelectorAll(
      'session-optimization-panel'
    );
    expect(panels?.length, 'real result and example both render').to.equal(2);
  });

  it('falls back to the normal empty state when the example is unavailable', async () => {
    stubFetch(404);
    const element = await mountOptimizePanel();
    await new Promise((resolve) => setTimeout(resolve, 50));
    await element.updateComplete;

    // A missing example must never break the tab.
    expect(element.shadowRoot?.querySelector('.example-banner')).to.not.exist;
  });

  it('fetches the example at most once', async () => {
    stubFetch();
    const element = await mountOptimizePanel();
    await waitUntil(
      () => Boolean(element.shadowRoot?.querySelector('.example-banner')),
      'example banner should render'
    );

    element.requestUpdate();
    await element.updateComplete;
    element.requestUpdate();
    await element.updateComplete;

    expect(exampleRequestCount).to.equal(1);
  });
});
