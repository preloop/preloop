import { fixture, html, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import { unifiedWebSocketManager } from '../../../services/unified-websocket-manager';
import '../../../setup-tests';
import './ai-model-detail-view';
import type { AIModelDetailView } from './ai-model-detail-view';

describe('AIModelDetailView', () => {
  let fetchStub: sinon.SinonStub;
  let connectStub: sinon.SinonStub;
  let subscribeStub: sinon.SinonStub;
  let pricingResponse: any;
  let pricingQuote: any;
  let featureFlags: Record<string, boolean>;
  let overrideWrites: { url: string; method: string; body: any }[];
  let repriceCalls: any[];
  let repriceResponse: any;

  beforeEach(() => {
    featureFlags = {};
    overrideWrites = [];
    repriceCalls = [];
    repriceResponse = {
      submitted_async: false,
      rows_examined: 1284,
      rows_updated: 1120,
      rows_skipped: 164,
      cost_before: 0,
      cost_after: 42.5,
      dry_run: false,
    };
    pricingResponse = {
      ai_model_id: 'model-1',
      model_alias: 'anthropic/claude-sonnet-4',
      provider_name: 'Anthropic',
      source: 'catalog',
      price: {
        input_per_1m: 3,
        output_per_1m: 15,
        cached_input_per_1m: 0.3,
        blended_per_1m: null,
        request_price: null,
      },
      currency: 'USD',
      override_id: null,
      effective_from: null,
      effective_until: null,
      catalog_key: 'anthropic/claude-sonnet-4',
      fetch_supported: false,
      fetch_provider_label: 'Anthropic',
    };
    pricingQuote = {
      ai_model_id: 'model-1',
      provider_name: 'openrouter',
      source_url: 'https://openrouter.ai/api/v1/models',
      model_key: 'anthropic/claude-sonnet-4',
      price: {
        input_per_1m: 2.75,
        output_per_1m: 13.5,
        cached_input_per_1m: 0.28,
        blended_per_1m: null,
        request_price: null,
      },
      currency: 'USD',
      fetched_at: '2026-09-03T09:00:00Z',
    };
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');

    fetchStub = sinon.stub(window, 'fetch');
    fetchStub.callsFake(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();

        if (url.includes('/api/v1/ai-models/model-1/pricing/fetch')) {
          return new Response(JSON.stringify(pricingQuote), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        }

        if (url.includes('/api/v1/ai-models/model-1/pricing')) {
          return new Response(JSON.stringify(pricingResponse), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        }

        if (url.includes('/api/v1/billing/cost/reprice')) {
          repriceCalls.push(init?.body ? JSON.parse(String(init.body)) : null);
          return new Response(JSON.stringify(repriceResponse), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        }

        if (url.includes('/api/v1/billing/cost/pricing-overrides')) {
          overrideWrites.push({
            url,
            method: (init?.method || 'GET').toUpperCase(),
            body: init?.body ? JSON.parse(String(init.body)) : null,
          });
          return new Response(JSON.stringify({ id: 'override-1' }), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        }

        if (
          url.includes('/api/v1/ai-models/model-1') &&
          !url.includes('/pricing') &&
          !url.includes('/summary') &&
          !url.includes('/runtime-sessions') &&
          !url.includes('/interactions')
        ) {
          return new Response(
            JSON.stringify({
              id: 'model-1',
              name: 'Claude Sonnet Primary',
              provider_name: 'Anthropic',
              model_identifier: 'claude-sonnet-4',
              has_api_key: true,
              meta_data: {
                gateway: {
                  enabled: true,
                  url: 'https://gateway.example/openai/v1',
                  model_alias: 'preloop/anthropic/claude-sonnet-4',
                },
                managed_agent_id: 'agent-1',
                managed_agent_display_name: 'Mini Claw',
                managed_agent_runtime_principal_id: 'mini-claw-123',
              },
              is_default: true,
              created_at: '2026-03-01T10:00:00Z',
              updated_at: '2026-03-09T18:30:00Z',
            }),
            {
              status: 200,
              headers: { 'Content-Type': 'application/json' },
            }
          );
        }

        if (url.startsWith('/api/v1/ai-models/model-1/summary')) {
          return new Response(
            JSON.stringify({
              ai_model_id: 'model-1',
              model_name: 'Claude Sonnet Primary',
              provider_name: 'Anthropic',
              model_identifier: 'claude-sonnet-4',
              period_start: '2026-02-08T00:00:00Z',
              period_end: '2026-03-09T23:59:59Z',
              total_requests: 18,
              successful_requests: 16,
              failed_requests: 2,
              token_usage: {
                prompt_tokens: 6400,
                completion_tokens: 2100,
                total_tokens: 8500,
              },
              estimated_cost: 1.42,
              requests_by_day: [
                {
                  date: '2026-03-08',
                  request_count: 7,
                  estimated_cost: 0.51,
                  total_tokens: 3200,
                },
                {
                  date: '2026-03-09',
                  request_count: 11,
                  estimated_cost: 0.91,
                  total_tokens: 5300,
                },
              ],
              usage_by_session: [],
            }),
            {
              status: 200,
              headers: { 'Content-Type': 'application/json' },
            }
          );
        }

        if (url.startsWith('/api/v1/ai-models/model-1/runtime-sessions')) {
          return new Response(
            JSON.stringify({
              period_start: '2026-02-08T00:00:00Z',
              period_end: '2026-03-09T23:59:59Z',
              query: null,
              session_source_type: null,
              status: 'all',
              total: 1,
              limit: 10,
              offset: 0,
              items: [
                {
                  id: 'runtime-session-1',
                  session_source_type: 'flow_execution',
                  session_source_id: 'execution-1',
                  session_reference: 'session-abc123',
                  runtime_principal_type: 'flow_execution',
                  runtime_principal_id: 'execution-1',
                  runtime_principal_name: 'Triage Assistant',
                  started_at: '2026-03-09T19:00:00Z',
                  last_activity_at: '2026-03-09T19:15:00Z',
                  ended_at: '2026-03-09T19:20:00Z',
                  flow_id: 'flow-1',
                  flow_name: 'Triage Assistant',
                  flow_execution_id: 'execution-1',
                  latest_model_alias: 'anthropic/claude-sonnet-4',
                  latest_provider_name: 'Anthropic',
                  total_requests: 6,
                  successful_requests: 6,
                  failed_requests: 0,
                  token_usage: {
                    prompt_tokens: 2200,
                    completion_tokens: 700,
                    total_tokens: 2900,
                  },
                  estimated_cost: 0.48,
                  last_request_at: '2026-03-09T19:15:00Z',
                },
              ],
            }),
            {
              status: 200,
              headers: { 'Content-Type': 'application/json' },
            }
          );
        }

        if (url.startsWith('/api/v1/ai-models/model-1/interactions')) {
          return new Response(
            JSON.stringify({
              period_start: '2026-02-08T00:00:00Z',
              period_end: '2026-03-09T23:59:59Z',
              query: null,
              total: 1,
              limit: 10,
              offset: 0,
              items: [
                {
                  api_usage_id: 'usage-1',
                  timestamp: '2026-03-09T19:15:00Z',
                  status_code: 200,
                  outcome: 'success',
                  endpoint: '/anthropic/v1/messages',
                  method: 'POST',
                  provider_name: 'Anthropic',
                  model_alias: 'anthropic/claude-sonnet-4',
                  flow_id: 'flow-1',
                  flow_name: 'Triage Assistant',
                  flow_execution_id: 'execution-1',
                  runtime_session_id: 'runtime-session-1',
                  session_source_type: 'flow_execution',
                  session_source_id: 'execution-1',
                  session_reference: 'session-abc123',
                  runtime_principal_type: 'flow_execution',
                  runtime_principal_id: 'execution-1',
                  runtime_principal_name: 'Triage Assistant',
                  estimated_cost: 0.12,
                  token_usage: {
                    prompt_tokens: 300,
                    completion_tokens: 95,
                    total_tokens: 395,
                  },
                  excerpt:
                    'request.input: Summarize deployment risk response.output_text: Deployment risk summary completed',
                  meta_data: {
                    source: 'gateway_interaction',
                  },
                },
              ],
            }),
            {
              status: 200,
              headers: { 'Content-Type': 'application/json' },
            }
          );
        }

        if (url.includes('/openai/v1/responses')) {
          return new Response(
            JSON.stringify({
              output: [
                {
                  content: [
                    {
                      text: 'Welcome acknowledged.',
                    },
                  ],
                },
              ],
            }),
            {
              status: 200,
              headers: { 'Content-Type': 'application/json' },
            }
          );
        }

        if (url.endsWith('/api/v1/features')) {
          return new Response(JSON.stringify({ features: featureFlags }), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        }

        if (url.endsWith('/api/v1/ai-models')) {
          return new Response(JSON.stringify([]), {
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

        if (url.includes('/api/v1/users')) {
          return new Response(
            JSON.stringify({
              users: [],
              total: 0,
              skip: 0,
              limit: 100,
            }),
            {
              status: 200,
              headers: { 'Content-Type': 'application/json' },
            }
          );
        }

        if (url.includes('/api/v1/auth/users/me')) {
          return new Response(
            JSON.stringify({
              email: 'test@preloop.ai',
              username: 'test',
            }),
            {
              status: 200,
              headers: { 'Content-Type': 'application/json' },
            }
          );
        }

        if (url.includes('/api/v1/agents')) {
          return new Response(
            JSON.stringify({
              items: [],
              total: 0,
            }),
            {
              status: 200,
              headers: { 'Content-Type': 'application/json' },
            }
          );
        }

        if (
          url.includes(
            '/api/v1/runtime-sessions/runtime-session-1/gateway-events'
          )
        ) {
          return new Response(JSON.stringify({ logs: [] }), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        }

        if (
          url.includes('/api/v1/runtime-sessions/runtime-session-1/activity')
        ) {
          return new Response(JSON.stringify({ items: [] }), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        }

        return new Response(
          JSON.stringify({ detail: `Unhandled request: ${url}` }),
          {
            status: 500,
            headers: { 'Content-Type': 'application/json' },
          }
        );
      }
    );

    connectStub = sinon.stub(unifiedWebSocketManager, 'connect').resolves();
    subscribeStub = sinon
      .stub(unifiedWebSocketManager, 'subscribe')
      .callsFake(() => () => undefined);
  });

  afterEach(() => {
    fetchStub.restore();
    connectStub.restore();
    subscribeStub.restore();
    localStorage.clear();
  });

  it('renders model observability summary, sessions, and interactions', async () => {
    const element = (await fixture(
      html`<ai-model-detail-view .modelId=${'model-1'}></ai-model-detail-view>`
    )) as AIModelDetailView;

    await waitUntil(
      () => !(element as any).loading,
      'AI model detail view did not finish loading',
      { timeout: 5000 }
    );
    await element.updateComplete;

    const content = element.shadowRoot?.textContent || '';
    expect(content).to.contain('Claude Sonnet Primary');
    expect(content).to.contain('Model Observability');
    expect(content).to.contain('Anthropic');
    expect(content).to.contain('claude-sonnet-4');
    expect(content).to.contain('preloop/anthropic/claude-sonnet-4');
    expect(content).to.contain('Mini Claw');
    expect(content).to.contain('Try Through Gateway');
    expect(content).to.contain('18');
    expect(content).to.contain('$1.42');
    expect(content).to.contain('8,500');
    expect(content).to.contain('Session Observer');

    const observer = element.shadowRoot?.querySelector(
      'preloop-session-observer'
    );
    expect(observer).to.exist;

    const agentLink = element.shadowRoot?.querySelector(
      'a[href="/console/agents/agent-1"]'
    );
    expect(agentLink).to.not.equal(null);

    const summaryCall = fetchStub
      .getCalls()
      .find((call) =>
        String(call.args[0]).startsWith('/api/v1/ai-models/model-1/summary')
      );
    const sessionsCall = fetchStub
      .getCalls()
      .find((call) =>
        String(call.args[0]).startsWith(
          '/api/v1/ai-models/model-1/runtime-sessions'
        )
      );
    const interactionsCall = fetchStub
      .getCalls()
      .find((call) =>
        String(call.args[0]).startsWith(
          '/api/v1/ai-models/model-1/interactions'
        )
      );

    expect(summaryCall).to.not.equal(undefined);
    expect(String(summaryCall?.args[0])).to.contain('start_date=');
    expect(String(summaryCall?.args[0])).to.contain('end_date=');
    expect(sessionsCall).to.not.equal(undefined);
    expect(String(sessionsCall?.args[0])).to.contain('limit=10');
    expect(interactionsCall).to.not.equal(undefined);
    expect(String(interactionsCall?.args[0])).to.contain('limit=10');
    expect(connectStub.callCount).to.be.at.least(1);
    expect(subscribeStub.callCount).to.be.at.least(4);
  });

  it('sends a test request through the gateway', async () => {
    const element = (await fixture(
      html`<ai-model-detail-view .modelId=${'model-1'}></ai-model-detail-view>`
    )) as AIModelDetailView;

    await waitUntil(
      () => !(element as any).loading,
      'AI model detail view did not finish loading',
      { timeout: 5000 }
    );
    await element.updateComplete;

    localStorage.setItem('accessToken', 'test-access-token');
    await (element as any).runValidationPrompt();

    await waitUntil(
      () =>
        fetchStub
          .getCalls()
          .some((call) =>
            String(call.args[0]).includes('/openai/v1/responses')
          ),
      'Gateway request was not sent',
      { timeout: 5000 }
    );
    await element.updateComplete;

    const requestCall = fetchStub
      .getCalls()
      .find((call) => String(call.args[0]).includes('/openai/v1/responses'));
    expect(requestCall).to.not.equal(undefined);
    expect(requestCall?.args[1]).to.deep.include({ method: 'POST' });
    expect(String((requestCall?.args[1] as RequestInit)?.body)).to.contain(
      'preloop/anthropic/claude-sonnet-4'
    );
    expect(element.shadowRoot?.textContent || '').to.contain(
      'Welcome acknowledged.'
    );
  });

  it('surfaces the upstream provider error from an OpenAI-shaped error body', async () => {
    const element = (await fixture(
      html`<ai-model-detail-view .modelId=${'model-1'}></ai-model-detail-view>`
    )) as AIModelDetailView;

    await waitUntil(
      () => !(element as any).loading,
      'AI model detail view did not finish loading',
      { timeout: 5000 }
    );
    await element.updateComplete;

    // Real staging failure: the gateway relays the scrubbed upstream message
    // in an OpenAI-error-shaped body. The UI must show it, not the generic
    // "Failed to run model request".
    fetchStub
      .withArgs(
        sinon.match((value: any) =>
          String(value).includes('/openai/v1/responses')
        )
      )
      .callsFake(
        async () =>
          new Response(
            JSON.stringify({
              error: {
                message:
                  'litellm.NotFoundError: OpenrouterException - No allowed providers are available for the selected model.',
                type: 'not_found_error',
                code: '404',
              },
            }),
            { status: 404, headers: { 'Content-Type': 'application/json' } }
          )
      );

    localStorage.setItem('accessToken', 'test-access-token');
    await (element as any).runValidationPrompt();
    await element.updateComplete;

    const message = (element as any).validationError as string;
    expect(message).to.contain('No allowed providers');
    // No endpoint URLs or key-shaped material may leak into the UI message.
    expect(message).to.not.contain('http://');
    expect(message).to.not.contain('https://');
    expect(message).to.not.match(/sk-[A-Za-z0-9]/);
  });

  it('opens the shared edit dialog from the header Edit action', async () => {
    const element = (await fixture(
      html`<ai-model-detail-view .modelId=${'model-1'}></ai-model-detail-view>`
    )) as AIModelDetailView;

    await waitUntil(
      () => !(element as any).loading,
      'AI model detail view did not finish loading',
      { timeout: 5000 }
    );
    await element.updateComplete;

    const actions = element.shadowRoot?.querySelector('resource-actions');
    expect(actions).to.exist;
    const clickable = Array.from(
      actions!.shadowRoot?.querySelectorAll('sl-button, sl-menu-item') || []
    ).find((el) => (el.textContent || '').includes('Edit'));
    expect(clickable).to.exist;
    (clickable as HTMLElement).click();
    await element.updateComplete;

    const modal = element.shadowRoot?.querySelector(
      'add-ai-model-modal'
    ) as HTMLElement & { open: boolean; model: { id?: string } | null };
    expect(modal).to.exist;
    expect(modal.open).to.equal(true);
    expect(modal.model?.id).to.equal('model-1');
  });

  it('opens a delete confirmation from the header Delete action', async () => {
    const element = (await fixture(
      html`<ai-model-detail-view .modelId=${'model-1'}></ai-model-detail-view>`
    )) as AIModelDetailView;

    await waitUntil(
      () => !(element as any).loading,
      'AI model detail view did not finish loading',
      { timeout: 5000 }
    );
    await element.updateComplete;

    const actions = element.shadowRoot?.querySelector('resource-actions');
    const clickable = Array.from(
      actions!.shadowRoot?.querySelectorAll('sl-button, sl-menu-item') || []
    ).find((el) => (el.textContent || '').includes('Delete'));
    expect(clickable).to.exist;
    (clickable as HTMLElement).click();
    await element.updateComplete;

    expect((element as any).isDeleteConfirmOpen).to.equal(true);
    const dialog = element.shadowRoot?.querySelector('sl-dialog');
    expect(dialog).to.exist;
    expect(dialog?.getAttribute('label') || (dialog as any).label).to.contain(
      'Delete Model'
    );
  });
  const mountModel = async (): Promise<AIModelDetailView> => {
    const element = (await fixture(
      html`<ai-model-detail-view .modelId=${'model-1'}></ai-model-detail-view>`
    )) as AIModelDetailView;
    await waitUntil(
      () => !(element as any).loading && (element as any).pricing !== null,
      'AI model detail view did not finish loading',
      { timeout: 5000 }
    );
    await element.updateComplete;
    return element;
  };

  const pricingCard = (element: AIModelDetailView): HTMLElement =>
    element.shadowRoot!.querySelector('#pricing') as HTMLElement;

  it('says what the model costs and where the price came from', async () => {
    const element = await mountModel();

    const card = pricingCard(element);
    const text = card.textContent!.replace(/\s+/g, ' ');
    expect(text).to.contain('Provider catalog');
    expect(text).to.contain('$3.00');
    expect(text).to.contain('$15.00');
    expect(text).to.contain('$0.3');
    expect(text).to.contain('per 1M tokens');
    expect(text).to.contain('Catalog entry anthropic/claude-sonnet-4');
    // Per request has no price here, and saying "$0" would be a lie.
    expect(text).to.contain('Not priced');
  });

  it('names the provider that does not publish prices', async () => {
    const element = await mountModel();

    const fetchButton = pricingCard(element).querySelector(
      '[data-testid="fetch-price"]'
    ) as HTMLElement;
    expect(fetchButton.hasAttribute('disabled')).to.be.true;
    expect(fetchButton.textContent!.replace(/\s+/g, ' ')).to.contain(
      'Not offered by Anthropic'
    );
  });

  it('offers no price editor without the price override feature', async () => {
    const element = await mountModel();

    expect(pricingCard(element).querySelector('[data-testid="edit-price"]')).to
      .not.exist;
    expect(pricingCard(element).textContent!.replace(/\s+/g, ' ')).to.contain(
      'Preloop Cloud and Enterprise'
    );
  });

  it('opens the price editor when the attention link asks for it', async () => {
    featureFlags = { model_price_overrides: true };
    const original = window.location.search;
    window.history.replaceState({}, '', '?pricing=edit');
    try {
      const element = await mountModel();
      await waitUntil(
        () => Boolean((element as any).pricingEditOpen),
        'price editor did not open'
      );
      await element.updateComplete;
      expect(pricingCard(element).querySelector('[data-testid="price-form"]'))
        .to.exist;
    } finally {
      window.history.replaceState({}, '', original || window.location.pathname);
    }
  });

  it('fills the form from the provider without saving anything', async () => {
    featureFlags = { model_price_overrides: true };
    pricingResponse.provider_name = 'OpenRouter';
    pricingResponse.fetch_supported = true;
    pricingResponse.fetch_provider_label = 'OpenRouter';
    const element = await mountModel();

    (
      pricingCard(element).querySelector(
        '[data-testid="fetch-price"]'
      ) as HTMLElement
    ).click();
    await waitUntil(
      () => Boolean((element as any).pricingEditOpen),
      'price editor did not open'
    );
    await element.updateComplete;

    const input = pricingCard(element).querySelector(
      '[data-testid="price-input"]'
    ) as HTMLInputElement;
    expect(input.value).to.equal('2.75');
    expect(pricingCard(element).textContent!.replace(/\s+/g, ' ')).to.contain(
      'OpenRouter lists anthropic/claude-sonnet-4'
    );
    // A fetched price is a proposal. Nothing is written until somebody saves.
    expect(overrideWrites).to.have.length(0);
  });

  it('saves a typed price per million as an override per thousand', async () => {
    featureFlags = { model_price_overrides: true };
    const element = await mountModel();

    (
      pricingCard(element).querySelector(
        '[data-testid="edit-price"]'
      ) as HTMLElement
    ).click();
    await element.updateComplete;

    (element as any).setPriceField('input', '4');
    (element as any).setPriceField('output', '20');
    (element as any).setPriceField('cachedInput', '');
    (element as any).setPriceField('effectiveFrom', '2026-09-01');
    await element.updateComplete;

    (
      pricingCard(element).querySelector(
        '[data-testid="save-price"]'
      ) as HTMLElement
    ).click();
    await waitUntil(() => overrideWrites.length > 0, 'override written');

    expect(overrideWrites[0].method).to.equal('POST');
    expect(overrideWrites[0].body.input_price_per_1k).to.equal(0.004);
    expect(overrideWrites[0].body.output_price_per_1k).to.equal(0.02);
    // An empty field says nothing about cached input; it does not say free.
    expect(overrideWrites[0].body.cache_read_input_price_per_1k).to.equal(null);
    expect(overrideWrites[0].body.model_alias).to.equal(
      'anthropic/claude-sonnet-4'
    );
    // The date is read as local midnight, which is the day the operator meant.
    expect(overrideWrites[0].body.effective_from).to.equal(
      new Date('2026-09-01T00:00:00').toISOString()
    );
  });

  async function saveAPrice(element: AIModelDetailView) {
    (
      pricingCard(element).querySelector(
        '[data-testid="edit-price"]'
      ) as HTMLElement
    ).click();
    await element.updateComplete;
    (element as any).setPriceField('input', '4');
    (element as any).setPriceField('output', '20');
    (element as any).setPriceField('effectiveFrom', '2026-08-01');
    await element.updateComplete;
    (
      pricingCard(element).querySelector(
        '[data-testid="save-price"]'
      ) as HTMLElement
    ).click();
    await waitUntil(() => overrideWrites.length > 0, 'override written');
    // The save is not done when the request goes out: the reload of the
    // price follows it.
    await waitUntil(
      () => Boolean((element as any).repriceSince),
      'the save did not settle'
    );
    await element.updateComplete;
  }

  it('says a new price does not touch what is already recorded', async () => {
    const element = await mountModel();
    expect(
      pricingCard(element)
        .querySelector('[data-testid="pricing-history-note"]')!
        .textContent!.replace(/\s+/g, ' ')
    ).to.contain('keeps the cost it was given until it is repriced');
  });

  it('offers to apply a saved price to past usage, from the date it starts', async () => {
    featureFlags = { model_price_overrides: true };
    const element = await mountModel();
    expect(
      pricingCard(element).querySelector('[data-testid="reprice-offer"]'),
      'nothing offered before a save'
    ).to.not.exist;

    await saveAPrice(element);

    const offer = pricingCard(element).querySelector(
      '[data-testid="reprice-offer"]'
    ) as HTMLElement;
    expect(offer, 'the offer follows the save').to.exist;
    const button = offer.querySelector(
      '[data-testid="apply-past-usage"]'
    ) as HTMLElement;
    expect(button.textContent!.replace(/\s+/g, ' ').trim()).to.equal(
      'Apply to past usage since Aug 1, 2026'
    );
    // The button reprices the account window, which is worth saying before
    // somebody presses it.
    expect(offer.textContent!.replace(/\s+/g, ' ')).to.contain(
      'recosts every gateway row since Aug 1, 2026'
    );

    button.click();
    await waitUntil(() => repriceCalls.length > 0, 'reprice requested');
    await waitUntil(
      () => Boolean((element as any).repriceNotice),
      'the reprice never reported back'
    );
    await element.updateComplete;

    expect(repriceCalls[0].start_date).to.equal(
      new Date('2026-08-01T00:00:00').toISOString()
    );
    // A row costed with the old price has a cost, so unpriced-only would
    // leave the very rows the offer is about untouched.
    expect(repriceCalls[0].only_unpriced).to.equal(false);
    expect(
      pricingCard(element)
        .querySelector('[data-testid="reprice-result"]')!
        .textContent!.replace(/\s+/g, ' ')
        .trim()
    ).to.equal('Repriced 1,120 of 1,284 rows since Aug 1, 2026.');
  });

  it('says a backgrounded reprice is still running rather than counting rows', async () => {
    featureFlags = { model_price_overrides: true };
    repriceResponse = {
      submitted_async: true,
      rows_examined: null,
      rows_updated: null,
      rows_skipped: null,
      cost_before: null,
      cost_after: null,
      dry_run: false,
    };
    const element = await mountModel();
    await saveAPrice(element);

    (
      pricingCard(element).querySelector(
        '[data-testid="apply-past-usage"]'
      ) as HTMLElement
    ).click();
    await waitUntil(() => repriceCalls.length > 0, 'reprice requested');
    await waitUntil(
      () => Boolean((element as any).repriceNotice),
      'the reprice never reported back'
    );
    await element.updateComplete;

    const result = pricingCard(element)
      .querySelector('[data-testid="reprice-result"]')!
      .textContent!.replace(/\s+/g, ' ');
    expect(result).to.contain('running in the background');
    expect(result).to.not.contain('0 of 0');
  });

  it('refuses a negative price instead of sending it', async () => {
    featureFlags = { model_price_overrides: true };
    const element = await mountModel();

    (
      pricingCard(element).querySelector(
        '[data-testid="edit-price"]'
      ) as HTMLElement
    ).click();
    await element.updateComplete;
    (element as any).setPriceField('input', '-2');
    await element.updateComplete;

    (
      pricingCard(element).querySelector(
        '[data-testid="save-price"]'
      ) as HTMLElement
    ).click();
    await element.updateComplete;

    expect(overrideWrites).to.have.length(0);
    expect(pricingCard(element).textContent).to.contain(
      'Prices must be zero or more.'
    );
  });
});
