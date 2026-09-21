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
  // A refusal for the delete tests, and a hook that lets the account's price
  // actually change under a successful one.
  let overrideDeleteFailure: { status: number; detail: string } | null;
  let onOverrideDelete: (() => void) | null;
  let modelWrites: { method: string; body: any }[];
  let repriceCalls: any[];
  let repriceResponse: any;
  let modelPayload: any;

  beforeEach(() => {
    modelPayload = {
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
    };
    featureFlags = {};
    overrideWrites = [];
    overrideDeleteFailure = null;
    onOverrideDelete = null;
    modelWrites = [];
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

        if (url.includes('/api/v1/billing/cost/reprice/')) {
          return new Response(
            JSON.stringify({
              id: 'job-1',
              status: 'succeeded',
              rows_examined: 10,
              rows_updated: 8,
              rows_skipped: 2,
            })
          );
        }
        if (url.includes('/api/v1/billing/cost/reprice')) {
          repriceCalls.push(init?.body ? JSON.parse(String(init.body)) : null);
          return new Response(JSON.stringify(repriceResponse), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        }

        if (url.includes('/api/v1/billing/cost/pricing-overrides')) {
          const method = (init?.method || 'GET').toUpperCase();
          overrideWrites.push({
            url,
            method,
            body: init?.body ? JSON.parse(String(init.body)) : null,
          });
          if (method === 'DELETE') {
            if (overrideDeleteFailure) {
              return new Response(
                JSON.stringify({ detail: overrideDeleteFailure.detail }),
                {
                  status: overrideDeleteFailure.status,
                  headers: { 'Content-Type': 'application/json' },
                }
              );
            }
            onOverrideDelete?.();
            return new Response(null, { status: 204 });
          }
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
          const method = (init?.method || 'GET').toUpperCase();
          if (method !== 'GET') {
            modelWrites.push({
              method,
              body: init?.body ? JSON.parse(String(init.body)) : null,
            });
          }
          return new Response(JSON.stringify(modelPayload), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
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
    // The card is named for what it holds, not for a product feature.
    expect(content).to.contain('Details');
    expect(content).to.not.contain('Model Observability');
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

  it('keeps the gateway URL when it enables gateway routing', async () => {
    // A model onboarded by `preloop agents onboard` carries the gateway URL
    // its runtime can actually reach. Re-enabling routing from the console
    // used to rewrite the gateway block from scratch and drop that URL, after
    // which the server fell back to a default host and every model call from
    // that model 404'd.
    modelPayload.meta_data.gateway = {
      enabled: false,
      url: 'https://gateway.example/openai/v1',
      transport_mode: 'preloop_gateway',
    };

    const element = (await fixture(
      html`<ai-model-detail-view .modelId=${'model-1'}></ai-model-detail-view>`
    )) as AIModelDetailView;
    await waitUntil(
      () => !(element as any).loading,
      'AI model detail view did not finish loading',
      { timeout: 5000 }
    );

    await (element as any).enableGatewayRouting();

    const write = modelWrites.find((entry) => entry.method === 'PUT');
    expect(write).to.not.equal(undefined);
    expect(write?.body.meta_data.gateway).to.deep.equal({
      enabled: true,
      url: 'https://gateway.example/openai/v1',
      transport_mode: 'preloop_gateway',
      provider_adapter: 'preloop',
      model_alias: 'anthropic/claude-sonnet-4',
    });
  });

  it('leaves the gateway URL absent when the model never had one', async () => {
    // Nothing is invented for a model without a URL: the server picks the
    // right in-cluster or public gateway for the deployment it runs in.
    modelPayload.meta_data.gateway = { enabled: false };

    const element = (await fixture(
      html`<ai-model-detail-view .modelId=${'model-1'}></ai-model-detail-view>`
    )) as AIModelDetailView;
    await waitUntil(
      () => !(element as any).loading,
      'AI model detail view did not finish loading',
      { timeout: 5000 }
    );

    await (element as any).enableGatewayRouting();

    const write = modelWrites.find((entry) => entry.method === 'PUT');
    expect(write?.body.meta_data.gateway).to.not.have.property('url');
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
      'Delete model'
    );
  });

  it('sets the header Delete apart as an outline action', async () => {
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
    const deleteAction = (
      actions as unknown as {
        actions: {
          id: string;
          variant?: string;
          outline?: boolean;
          separated?: boolean;
        }[];
      }
    ).actions.find((action) => action.id === 'delete');
    expect(deleteAction?.variant).to.equal('danger');
    expect(deleteAction?.outline).to.equal(true);
    expect(deleteAction?.separated).to.equal(true);
  });

  it('shows Never for a model that was never updated and a hairline usage strip', async () => {
    modelPayload = {
      ...modelPayload,
      updated_at: null,
    };

    const element = (await fixture(
      html`<ai-model-detail-view .modelId=${'model-1'}></ai-model-detail-view>`
    )) as AIModelDetailView;
    await waitUntil(
      () => !(element as any).loading,
      'AI model detail view did not finish loading',
      { timeout: 5000 }
    );
    await element.updateComplete;

    const content = (element.shadowRoot?.textContent || '').replace(
      /\s+/g,
      ' '
    );
    expect(content).to.contain('Updated: Never');

    // The summary is a hairline row inside the card, not boxes inside a box.
    expect(element.shadowRoot?.querySelector('.summary-strip')).to.exist;
    expect(element.shadowRoot?.querySelector('.stat-card')).to.equal(null);
    // The period is stated once, in the card header.
    expect(content).to.contain('Usage summary');
    expect(content).to.contain('30 days ·');
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

  it('does not offer to reprice when the new price starts in the future', async () => {
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
    (element as any).setPriceField('effectiveFrom', '2027-01-01');
    await element.updateComplete;
    (
      pricingCard(element).querySelector(
        '[data-testid="save-price"]'
      ) as HTMLElement
    ).click();
    await waitUntil(() => overrideWrites.length > 0, 'override written');
    await waitUntil(
      () => Boolean((element as any).pricingNotice),
      'the save did not settle'
    );
    await element.updateComplete;

    expect((element as any).repriceSince).to.equal(null);
    expect(
      pricingCard(element).querySelector('[data-testid="reprice-offer"]'),
      'a future start date has no past window to reprice'
    ).to.not.exist;
  });

  describe('removing the override', () => {
    // The price this account set, which is the only kind that can be taken
    // back. Per 1M in the response; the stored row is per 1K.
    const overridePricing = () => ({
      ai_model_id: 'model-1',
      model_alias: 'anthropic/claude-sonnet-4',
      provider_name: 'Anthropic',
      source: 'override',
      price: {
        input_per_1m: 0,
        output_per_1m: 0,
        cached_input_per_1m: null,
        blended_per_1m: null,
        request_price: null,
      },
      currency: 'USD',
      override_id: 'override-1',
      effective_from: '2026-08-01T00:00:00Z',
      effective_until: null,
      catalog_key: null,
      fetch_supported: false,
      fetch_provider_label: 'Anthropic',
    });

    const removeButton = (element: AIModelDetailView) =>
      pricingCard(element).querySelector(
        '[data-testid="remove-override"]'
      ) as HTMLElement | null;

    const removeDialog = (element: AIModelDetailView) =>
      pricingCard(element).querySelector(
        '[data-testid="remove-override-dialog"]'
      ) as HTMLElement;

    const deletes = () =>
      overrideWrites.filter((write) => write.method === 'DELETE');

    beforeEach(() => {
      featureFlags = { model_price_overrides: true };
    });

    it('offers the removal only for a price this account set', async () => {
      const element = await mountModel();
      // The fixture is priced from the catalog: there is nothing to remove.
      expect(removeButton(element), 'catalog price has no override').to.not
        .exist;

      pricingResponse = {
        ...pricingResponse,
        source: 'none',
        override_id: null,
        price: {
          input_per_1m: null,
          output_per_1m: null,
          cached_input_per_1m: null,
          blended_per_1m: null,
          request_price: null,
        },
      };
      const unpriced = await mountModel();
      expect(removeButton(unpriced), 'an unpriced model has nothing to remove')
        .to.not.exist;

      pricingResponse = overridePricing();
      const overridden = await mountModel();
      expect(removeButton(overridden), 'an override can be taken back').to
        .exist;
    });

    it('confirms with the rates, deletes once and re-reads the price', async () => {
      pricingResponse = overridePricing();
      // The removal is real: the next read of the price is the catalog's.
      onOverrideDelete = () => {
        pricingResponse = {
          ...overridePricing(),
          source: 'catalog',
          override_id: null,
          effective_from: null,
          catalog_key: 'anthropic/claude-sonnet-4',
          price: {
            input_per_1m: 3,
            output_per_1m: 15,
            cached_input_per_1m: 0.3,
            blended_per_1m: null,
            request_price: null,
          },
        };
      };
      const element = await mountModel();

      removeButton(element)!.click();
      await element.updateComplete;
      const prompt = removeDialog(element).textContent!.replace(/\s+/g, ' ');
      expect(prompt).to.contain('anthropic/claude-sonnet-4');
      expect(prompt).to.contain('input $0 per 1M');
      expect(prompt).to.contain('output $0 per 1M');
      expect(prompt).to.contain('effective from Aug 1, 2026');
      expect(deletes(), 'the confirm alone sends nothing').to.have.length(0);

      (
        removeDialog(element).querySelector(
          '[data-testid="confirm-remove-override"]'
        ) as HTMLElement
      ).click();
      await waitUntil(() => deletes().length > 0, 'no delete was sent');
      await waitUntil(
        () => (element as any).pricing?.source === 'catalog',
        'the price was not re-read'
      );
      await element.updateComplete;

      expect(deletes()).to.have.length(1);
      expect(deletes()[0].url).to.contain(
        '/api/v1/billing/cost/pricing-overrides/override-1'
      );
      const card = pricingCard(element).textContent!.replace(/\s+/g, ' ');
      expect(card).to.contain('Provider catalog');
      expect(card).to.contain('Catalog entry anthropic/claude-sonnet-4');
      expect(card).to.not.contain('Account override');
      expect(removeButton(element), 'a catalog price has nothing to remove').to
        .not.exist;
    });

    it('offers the reprice after a removal when the model has usage', async () => {
      pricingResponse = overridePricing();
      const element = await mountModel();

      removeButton(element)!.click();
      await element.updateComplete;
      (
        removeDialog(element).querySelector(
          '[data-testid="confirm-remove-override"]'
        ) as HTMLElement
      ).click();
      await waitUntil(
        () => Boolean((element as any).repriceSince),
        'no reprice window after the removal'
      );
      await element.updateComplete;

      const card = pricingCard(element).textContent!.replace(/\s+/g, ' ');
      expect(card).to.contain(
        'Rows recorded under it keep the old cost until they are repriced'
      );
      const offer = pricingCard(element).querySelector(
        '[data-testid="reprice-offer"]'
      ) as HTMLElement;
      expect(offer, 'the offer follows the removal').to.exist;
      // Not run for the reader: the reprice is still theirs to ask for.
      expect(repriceCalls).to.have.length(0);

      (
        offer.querySelector('[data-testid="apply-past-usage"]') as HTMLElement
      ).click();
      await waitUntil(() => repriceCalls.length > 0, 'reprice requested');
      // The rows to fix already have a cost: the old one.
      expect(repriceCalls[0].only_unpriced).to.equal(false);
      expect(repriceCalls[0].start_date).to.equal('2026-08-01T00:00:00.000Z');
    });

    it('sends nothing when the confirm is cancelled', async () => {
      pricingResponse = overridePricing();
      const element = await mountModel();

      removeButton(element)!.click();
      await element.updateComplete;
      (
        removeDialog(element).querySelector(
          '[data-testid="cancel-remove-override"]'
        ) as HTMLElement
      ).click();
      await element.updateComplete;

      expect(deletes()).to.have.length(0);
      expect((element as any).overrideRemoveOpen).to.equal(false);
      expect((element as any).pricing.source).to.equal('override');
      expect(removeButton(element), 'the override is still there').to.exist;
    });

    it('keeps the override on screen when the delete is refused', async () => {
      pricingResponse = overridePricing();
      overrideDeleteFailure = {
        status: 403,
        detail: 'Only an account owner can remove a price override.',
      };
      const element = await mountModel();

      removeButton(element)!.click();
      await element.updateComplete;
      (
        removeDialog(element).querySelector(
          '[data-testid="confirm-remove-override"]'
        ) as HTMLElement
      ).click();
      await waitUntil(
        () => Boolean((element as any).pricingError),
        'the refusal was swallowed'
      );
      await element.updateComplete;

      expect(
        pricingCard(element).querySelector('.price-error')!.textContent!.trim()
      ).to.contain('Only an account owner can remove a price override.');
      expect((element as any).pricing.source).to.equal('override');
      expect(pricingCard(element).textContent).to.contain('Account override');
      expect(removeButton(element), 'nothing was removed').to.exist;
      expect((element as any).repriceSince).to.equal(null);
    });
  });

  it('leaves legacy async completion unconfirmed', async () => {
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
    expect(result).to.contain('completion cannot be confirmed');
    expect(result).to.not.contain('0 of 0');
  });

  it('tracks asynchronous past-usage repricing through its job status', async () => {
    featureFlags = { model_price_overrides: true };
    repriceResponse = { submitted_async: true, job_id: 'job-1' };
    const element = await mountModel();
    await saveAPrice(element);
    (
      pricingCard(element).querySelector(
        '[data-testid="apply-past-usage"]'
      ) as HTMLElement
    ).click();
    await waitUntil(() =>
      Boolean(
        pricingCard(element)
          .querySelector('reprice-job-status')
          ?.shadowRoot?.textContent?.includes('succeeded')
      )
    );
    const text =
      pricingCard(element).querySelector('reprice-job-status')!.shadowRoot!
        .textContent!;
    expect(text).to.contain('8 of 10 requests updated, 2 skipped');
    expect(text).to.contain('job-1');
    expect(repriceCalls).to.have.length(1);
    expect(repriceCalls[0].only_unpriced).to.equal(false);
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
  it('carries the shared range control instead of a Filters card', async () => {
    const element = (await fixture(
      html`<ai-model-detail-view .modelId=${'model-1'}></ai-model-detail-view>`
    )) as AIModelDetailView;

    await waitUntil(
      () => !(element as any).loading,
      'AI model detail view did not finish loading',
      { timeout: 5000 }
    );
    await element.updateComplete;

    // One range vocabulary, shared with the Overview, Cost and API usage.
    const range = element.shadowRoot?.querySelector('time-range-select');
    expect(range).to.exist;
    expect((range as any).value).to.equal('last-30');
    expect(
      element.shadowRoot?.querySelector('sl-select[label="Date range"]')
    ).to.equal(null);
    expect(element.shadowRoot?.querySelector('sl-input[type="date"]')).to.equal(
      null
    );

    const content = element.shadowRoot?.textContent || '';
    expect(content).to.not.contain('Filters');
    expect(content).to.not.contain('Showing model-scoped activity from');

    // The window is restated beside the control that chose it.
    const window = element.shadowRoot
      ?.querySelector('.range-window')
      ?.textContent?.trim();
    expect(window).to.contain(' to ');

    // The window on the wire is the shared 30 days, not 30 local calendar
    // days ending tonight.
    const summaryCall = fetchStub
      .getCalls()
      .find((call) =>
        String(call.args[0]).startsWith('/api/v1/ai-models/model-1/summary')
      );
    const query = new URLSearchParams(
      String(summaryCall?.args[0]).split('?')[1] || ''
    );
    const start = new Date(query.get('start_date') || '');
    const end = new Date(query.get('end_date') || '');
    const spanDays = (end.getTime() - start.getTime()) / (24 * 60 * 60 * 1000);
    expect(Math.abs(spanDays - 30)).to.be.below(0.01);
  });

  // A range change is a refinement, not a new page: the answers it is
  // replacing stay readable, the way API usage and Cost behave.
  it('keeps the previous numbers on screen while a new range loads', async () => {
    const element = (await fixture(
      html`<ai-model-detail-view .modelId=${'model-1'}></ai-model-detail-view>`
    )) as AIModelDetailView;

    await waitUntil(
      () => !(element as any).loading,
      'AI model detail view did not finish loading',
      { timeout: 5000 }
    );
    await element.updateComplete;

    const range = element.shadowRoot?.querySelector('time-range-select');
    range?.dispatchEvent(
      new CustomEvent('range-change', {
        detail: { value: 'last-7' },
        bubbles: true,
        composed: true,
      })
    );
    await element.updateComplete;

    // No spinner card: the usage summary and the session observer are still
    // there, dimmed and marked busy.
    expect((element as any).updating, 'the reload is marked as an update').to.be
      .true;
    expect(element.shadowRoot?.querySelector('.loading-state')).to.equal(null);
    const results = element.shadowRoot?.querySelector('.results');
    expect(results, 'the answers stay mounted').to.exist;
    expect(results?.classList.contains('is-updating')).to.be.true;
    expect(results?.getAttribute('aria-busy')).to.equal('true');
    expect(results?.textContent).to.contain('Usage summary');

    await waitUntil(
      () => !(element as any).updating,
      'the range reload never settled',
      { timeout: 5000 }
    );
    await element.updateComplete;
    expect(
      element.shadowRoot
        ?.querySelector('.results')
        ?.classList.contains('is-updating')
    ).to.be.false;
  });

  // A range change that lands during the 250 ms realtime refresh used to be
  // dropped, leaving the control naming a window nobody fetched.
  it('runs a range change that arrives while a refresh is in flight', async () => {
    const element = (await fixture(
      html`<ai-model-detail-view .modelId=${'model-1'}></ai-model-detail-view>`
    )) as AIModelDetailView;

    await waitUntil(
      () => !(element as any).loading,
      'AI model detail view did not finish loading',
      { timeout: 5000 }
    );
    await element.updateComplete;

    const summaryCalls = () =>
      fetchStub
        .getCalls()
        .map((call) => String(call.args[0]))
        .filter((url) => url.startsWith('/api/v1/ai-models/model-1/summary'));
    const before = summaryCalls().length;

    // A background refresh takes the lock, then the operator picks 24h.
    void (element as any).loadData({ preserveLoadingState: true });
    (element as any).selectedRange = 'last-24h';
    void (element as any).loadData({
      preserveLoadingState: true,
      markUpdating: true,
    });

    await waitUntil(
      () => summaryCalls().length >= before + 2,
      'the queued range change never ran',
      { timeout: 5000 }
    );
    await waitUntil(
      () => !(element as any).refreshInFlight,
      'the reloads never settled',
      { timeout: 5000 }
    );

    const last = summaryCalls()[summaryCalls().length - 1];
    const query = new URLSearchParams(last.split('?')[1] || '');
    const start = new Date(query.get('start_date') || '');
    const end = new Date(query.get('end_date') || '');
    const spanDays = (end.getTime() - start.getTime()) / (24 * 60 * 60 * 1000);
    expect(Math.abs(spanDays - 1)).to.be.below(0.01);
  });

  // "All time" survived the Filters card here too.
  it('offers All time and asks for it without date bounds', async () => {
    const element = (await fixture(
      html`<ai-model-detail-view .modelId=${'model-1'}></ai-model-detail-view>`
    )) as AIModelDetailView;

    await waitUntil(
      () => !(element as any).loading,
      'AI model detail view did not finish loading',
      { timeout: 5000 }
    );
    await element.updateComplete;

    const range = element.shadowRoot?.querySelector('time-range-select');
    expect(
      ((range as any).options as { value: string; label: string }[]).map(
        (option) => option.value
      )
    ).to.contain('all');

    const summaryCalls = () =>
      fetchStub
        .getCalls()
        .map((call) => String(call.args[0]))
        .filter((url) => url.startsWith('/api/v1/ai-models/model-1/summary'));
    const before = summaryCalls().length;

    range?.dispatchEvent(
      new CustomEvent('range-change', {
        detail: { value: 'all' },
        bubbles: true,
        composed: true,
      })
    );
    await waitUntil(
      () => summaryCalls().length > before,
      'the All time reload never reached the server',
      { timeout: 3000 }
    );

    const url = summaryCalls().slice(before)[0];
    expect(url).to.not.contain('start_date=');
    expect(url).to.not.contain('end_date=');
  });

  // Only the captured interactions depend on the query, so a pause in typing
  // costs one request, not the five the whole page costs.
  it('spends one request on an interaction search and shows the results', async () => {
    const element = (await fixture(
      html`<ai-model-detail-view .modelId=${'model-1'}></ai-model-detail-view>`
    )) as AIModelDetailView;

    await waitUntil(
      () => !(element as any).loading,
      'AI model detail view did not finish loading',
      { timeout: 5000 }
    );
    await element.updateComplete;

    // The search field promises a list, so the list is on the page.
    expect(element.shadowRoot?.textContent).to.contain('Captured interactions');

    // Icons are fetched too; only the API calls are counted here.
    const apiCalls = () =>
      fetchStub
        .getCalls()
        .map((call) => String(call.args[0]))
        .filter((url) => url.startsWith('/api/'));
    const callsAfterLoad = apiCalls().length;

    const search = element.shadowRoot?.querySelector(
      'sl-input.interaction-search'
    ) as HTMLInputElement;
    search.value = 'timeout';
    search.dispatchEvent(new CustomEvent('sl-input', { bubbles: true }));

    await waitUntil(
      () => apiCalls().length > callsAfterLoad,
      'the debounced search never reached the server',
      { timeout: 3000 }
    );
    await waitUntil(
      () => !(element as any).interactionsLoading,
      'the search never settled',
      { timeout: 3000 }
    );
    await element.updateComplete;

    const newCalls = apiCalls().slice(callsAfterLoad);
    expect(newCalls).to.have.length(1);
    expect(newCalls[0]).to.contain('/api/v1/ai-models/model-1/interactions');
    expect(newCalls[0]).to.contain('query=timeout');

    // The summary the search did not touch is still on screen.
    expect(element.shadowRoot?.textContent).to.contain('Usage summary');
  });
});

/**
 * The detail page reported the window's failure count and nothing else, so a
 * failure acknowledged on the Overview still read as an open problem here, and
 * there was nowhere to acknowledge one while looking at the evidence for it.
 */
describe('AIModelDetailView attention dismissals', () => {
  let fetchStub: sinon.SinonStub;
  let connectStub: sinon.SinonStub;
  let subscribeStub: sinon.SinonStub;
  let dismissalsResponse: any[];
  let dismissalWrites: { url: string; method: string; body: any }[];
  let summaryRequests: string[];
  let lastFailureAt: string;
  /** #848: what the price in force says, which is how this page reads unpriced. */
  let pricingSource: string;
  /** #848: cost recorded in the window, which is 0 until usage is repriced. */
  let summaryCost: number;
  /** #848: a price in force that is zero, which is the other pricing question. */
  let pricedAtZero: boolean;
  /** Off for the pages that are only unpriced, not failing. */
  let failuresEnabled: boolean;
  let extraAliasFailures: {
    alias: string;
    last_failure_at: string;
    failed_requests: number;
    failed_requests_since: number | null;
  }[];

  const json = (data: unknown) =>
    new Response(JSON.stringify(data), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    dismissalsResponse = [];
    dismissalWrites = [];
    summaryRequests = [];
    lastFailureAt = '2026-09-14T09:00:00Z';
    pricingSource = 'catalog';
    summaryCost = 1.5;
    pricedAtZero = false;
    failuresEnabled = true;
    extraAliasFailures = [];

    fetchStub = sinon
      .stub(window, 'fetch')
      .callsFake(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();

        if (url.startsWith('/api/v1/attention/dismissals')) {
          const method = (init?.method || 'GET').toUpperCase();
          if (method === 'GET') {
            return json({ items: dismissalsResponse });
          }
          if (method === 'DELETE') {
            const itemId = decodeURIComponent(url.split('/').pop()!);
            dismissalWrites.push({ url, method, body: null });
            dismissalsResponse = dismissalsResponse.filter(
              (existing) => existing.item_id !== itemId
            );
            return new Response(null, { status: 204 });
          }
          const body = JSON.parse(String(init!.body));
          dismissalWrites.push({ url, method, body });
          const record = {
            id: 'dismissal-1',
            item_id: decodeURIComponent(url.split('/').pop()!),
            fingerprint: body.fingerprint,
            reason: body.reason,
            snooze_until: null,
            dismissed_by_user_id: 'user-1',
            dismissed_by_username: 'Jane Doe',
            created_at: '2026-09-14T09:30:00Z',
          };
          // Upsert, as the API does: the failure marker and the unpriced
          // marker for one model are two rows.
          dismissalsResponse = [
            ...dismissalsResponse.filter(
              (existing) => existing.item_id !== record.item_id
            ),
            record,
          ];
          return json(record);
        }

        if (url.startsWith('/api/v1/ai-models/model-1/summary')) {
          summaryRequests.push(url);
          return json({
            ai_model_id: 'model-1',
            model_name: 'Reviewer model',
            provider_name: 'example-provider',
            model_identifier: 'example-model-1',
            period_start: '2026-08-15T00:00:00Z',
            period_end: '2026-09-14T23:59:59Z',
            total_requests: 20,
            successful_requests: failuresEnabled ? 11 : 20,
            failed_requests: failuresEnabled ? 9 : 0,
            last_failure_at: failuresEnabled ? lastFailureAt : null,
            last_failure_alias: failuresEnabled ? 'example/reviewer' : null,
            failed_requests_since: url.includes('failed_since') ? 2 : null,
            alias_failures: failuresEnabled
              ? [
                  {
                    alias: 'example/reviewer',
                    last_failure_at: lastFailureAt,
                    failed_requests: 9,
                    failed_requests_since: url.includes('failed_since')
                      ? 2
                      : null,
                  },
                  ...extraAliasFailures,
                ]
              : [],
            token_usage: {
              prompt_tokens: 100,
              completion_tokens: 100,
              total_tokens: 200,
            },
            estimated_cost: summaryCost,
            requests_by_day: [],
            usage_by_session: [],
          });
        }

        if (url.startsWith('/api/v1/ai-models/model-1/runtime-sessions')) {
          return json({ total: 0, limit: 10, offset: 0, items: [] });
        }

        if (url.startsWith('/api/v1/ai-models/model-1/interactions')) {
          return json({ total: 0, limit: 10, offset: 0, items: [] });
        }

        if (url.includes('/api/v1/ai-models/model-1/pricing')) {
          return json({
            ai_model_id: 'model-1',
            model_alias: 'example/reviewer',
            provider_name: 'example-provider',
            source: pricingSource,
            price: {
              input_per_1m: pricedAtZero ? 0 : 3,
              output_per_1m: pricedAtZero ? 0 : 15,
              cached_input_per_1m: null,
              blended_per_1m: null,
              request_price: null,
            },
            currency: 'USD',
            override_id: null,
            effective_from: null,
            effective_until: null,
            catalog_key: 'example/reviewer',
            fetch_supported: false,
            fetch_provider_label: 'Example provider',
          });
        }

        if (url.includes('/api/v1/ai-models/model-1')) {
          return json({
            id: 'model-1',
            name: 'Reviewer model',
            provider_name: 'example-provider',
            model_identifier: 'example-model-1',
            has_api_key: true,
            meta_data: {
              gateway: { enabled: true, model_alias: 'example/reviewer' },
            },
            is_default: false,
            created_at: '2026-09-01T10:00:00Z',
            updated_at: '2026-09-14T10:00:00Z',
          });
        }

        if (url.endsWith('/api/v1/features')) {
          return json({ features: {} });
        }

        // Anything the page loads around the summary (policies, agents, the
        // session observer) answers empty: this suite is about the failure
        // line, not about those panels.
        return json({ items: [], total: 0 });
      });

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

  const mount = async (): Promise<AIModelDetailView> => {
    const element = (await fixture(
      html`<ai-model-detail-view modelId="model-1"></ai-model-detail-view>`
    )) as AIModelDetailView;
    await waitUntil(
      () => !(element as any).loading,
      'the model detail view did not finish loading'
    );
    await element.updateComplete;
    return element;
  };

  const attentionBadge = (element: AIModelDetailView) =>
    element.shadowRoot!.querySelector(
      '[data-testid="model-attention"] sl-badge'
    ) as HTMLElement;

  it('dismisses this model with the item id and fingerprint the inbox uses', async () => {
    const element = await mount();

    expect(attentionBadge(element).textContent!.trim()).to.equal('Attention');
    const menu = element.shadowRoot!.querySelector(
      '[data-testid="model-attention"] sl-menu'
    )!;
    menu.dispatchEvent(
      new CustomEvent('sl-select', { detail: { item: { value: 'fixed' } } })
    );
    await waitUntil(
      () => dismissalWrites.length > 0,
      'the dismissal was never written'
    );

    expect(dismissalWrites[0].method).to.equal('PUT');
    expect(decodeURIComponent(dismissalWrites[0].url)).to.contain(
      'model:example/reviewer'
    );
    expect(dismissalWrites[0].body).to.deep.equal({
      fingerprint: `last:${lastFailureAt}`,
      reason: 'fixed',
    });

    await waitUntil(
      () => attentionBadge(element).textContent!.trim() === 'Healthy',
      'the page stayed flagged after the failure was marked fixed'
    );
    expect(attentionBadge(element).getAttribute('title')).to.contain(
      'Marked fixed'
    );
    expect(
      element.shadowRoot!.querySelector('[data-testid="dismiss-model"]')
    ).to.equal(null);
  });

  it('keeps a two-alias page flagged until every alias item is dismissed', async () => {
    extraAliasFailures = [
      {
        alias: 'example/reviewer-old',
        last_failure_at: '2026-09-13T08:00:00Z',
        failed_requests: 4,
        failed_requests_since: null,
      },
    ];
    dismissalsResponse = [
      {
        id: 'dismissal-1',
        item_id: 'model:example/reviewer',
        fingerprint: `last:${lastFailureAt}`,
        reason: 'fixed',
        snooze_until: null,
        dismissed_by_user_id: 'user-1',
        dismissed_by_username: 'Jane Doe',
        created_at: '2026-09-14T09:30:00Z',
      },
    ];

    let element = await mount();
    expect(attentionBadge(element).textContent!.trim()).to.equal('Attention');

    dismissalsResponse = [
      ...dismissalsResponse,
      {
        id: 'dismissal-2',
        item_id: 'model:example/reviewer-old',
        fingerprint: 'last:2026-09-13T08:00:00Z',
        reason: 'fixed',
        snooze_until: null,
        dismissed_by_user_id: 'user-1',
        dismissed_by_username: 'Jane Doe',
        created_at: '2026-09-14T09:35:00Z',
      },
    ];
    element = await mount();
    expect(attentionBadge(element).textContent!.trim()).to.equal('Healthy');
  });

  it('counts only the failures newer than an overtaken marker', async () => {
    dismissalsResponse = [
      {
        id: 'dismissal-1',
        item_id: 'model:example/reviewer',
        fingerprint: 'last:2026-09-13T08:00:00Z',
        reason: 'fixed',
        snooze_until: null,
        dismissed_by_user_id: 'user-1',
        dismissed_by_username: 'Jane Doe',
        created_at: '2026-09-13T08:30:00Z',
      },
    ];

    const element = await mount();
    await waitUntil(
      () =>
        Boolean(
          element.shadowRoot!.querySelector('[data-testid="since-marker"]')
        ),
      'the failures-since line never rendered'
    );

    expect(attentionBadge(element).textContent!.trim()).to.equal('Attention');
    const since = element.shadowRoot!.querySelector(
      '[data-testid="since-marker"]'
    )!;
    expect(since.textContent!.replace(/\s+/g, ' ')).to.contain(
      '2 failed since fix'
    );
    const splitRequest = summaryRequests.find((url) =>
      url.includes('failed_since')
    )!;
    expect(decodeURIComponent(splitRequest)).to.contain(
      'failed_since=2026-09-13T08:00:00Z'
    );
  });

  /**
   * #848: the same marker, offered where the price itself is edited, so an
   * operator who decides a model is unpriced on purpose can say so without
   * inventing a price of $0.
   */
  describe('unpriced requests marked expected', () => {
    const unpricedMarker = (overrides: Record<string, unknown> = {}) => ({
      id: 'dismissal-unpriced',
      item_id: 'model-unpriced:example/reviewer',
      fingerprint: 'unpriced:example/reviewer',
      reason: 'expected',
      snooze_until: null,
      dismissed_by_user_id: 'user-1',
      dismissed_by_username: 'Jane Doe',
      created_at: '2026-09-14T09:30:00Z',
      ...overrides,
    });

    const menuValues = (element: AIModelDetailView) =>
      [
        ...element.shadowRoot!.querySelectorAll(
          '[data-testid="model-attention"] sl-menu sl-menu-item'
        ),
      ].map((item) => item.getAttribute('value'));

    beforeEach(() => {
      pricingSource = 'none';
      failuresEnabled = false;
    });

    it('flags an unpriced model and says what clears it', async () => {
      const element = await mount();

      expect(attentionBadge(element).textContent!.trim()).to.equal('Attention');
      const line = element.shadowRoot!.querySelector(
        '[data-testid="unpriced-attention"]'
      )!;
      const text = line.textContent!.replace(/\s+/g, ' ');
      expect(text).to.contain('No price is in force');
      expect(text).to.contain('Set a price on the model');
      expect(text).to.contain('Apply to past usage');
      expect(menuValues(element)).to.eql([
        'unpriced-expected',
        'unpriced-snoozed',
      ]);
    });

    it('writes the item id and fingerprint the Models page and inbox read', async () => {
      const element = await mount();

      element
        .shadowRoot!.querySelector('[data-testid="model-attention"] sl-menu')!
        .dispatchEvent(
          new CustomEvent('sl-select', {
            detail: { item: { value: 'unpriced-expected' } },
          })
        );
      await waitUntil(
        () => dismissalWrites.length > 0,
        'the dismissal was never written'
      );

      expect(dismissalWrites[0].method).to.equal('PUT');
      expect(decodeURIComponent(dismissalWrites[0].url)).to.contain(
        'model-unpriced:example/reviewer'
      );
      expect(dismissalWrites[0].body).to.deep.equal({
        fingerprint: 'unpriced:example/reviewer',
        reason: 'expected',
      });

      await waitUntil(
        () => attentionBadge(element).textContent!.trim() === 'Healthy',
        'the page stayed flagged after the price question was answered'
      );
      expect(attentionBadge(element).getAttribute('title')).to.contain(
        'marked expected'
      );
    });

    // A price set today does not reprice yesterday's requests until somebody
    // runs "Apply to past usage", and until then the Models list and the inbox
    // keep counting them. This page says the same thing rather than falling
    // quiet on them.
    it('still flags a priced model whose window recorded no cost', async () => {
      pricingSource = 'override';
      summaryCost = 0;

      const element = await mount();

      expect(attentionBadge(element).textContent!.trim()).to.equal('Attention');
      expect(
        element
          .shadowRoot!.querySelector('[data-testid="unpriced-attention"]')!
          .textContent!.replace(/\s+/g, ' ')
      ).to.contain('20 requests');
    });

    // A price of zero is an answer, and the inbox asks about it separately.
    it('asks nothing when the price in force is zero', async () => {
      pricingSource = 'override';
      pricedAtZero = true;
      summaryCost = 0;

      const element = await mount();

      // Nothing to say at all: no failures either, so the line is absent.
      expect(
        element.shadowRoot!.querySelector('[data-testid="model-attention"]')
      ).to.equal(null);
      expect(
        element.shadowRoot!.querySelector('[data-testid="unpriced-attention"]')
      ).to.equal(null);
    });

    it('snoozes the price question for seven days', async () => {
      const element = await mount();

      element
        .shadowRoot!.querySelector('[data-testid="model-attention"] sl-menu')!
        .dispatchEvent(
          new CustomEvent('sl-select', {
            detail: { item: { value: 'unpriced-snoozed' } },
          })
        );
      await waitUntil(
        () => dismissalWrites.length > 0,
        'the dismissal was never written'
      );

      expect(dismissalWrites[0].body).to.deep.equal({
        fingerprint: 'unpriced:example/reviewer',
        reason: 'snoozed',
        snooze_days: 7,
      });
    });

    it('reads Healthy under an active marker and offers Restore', async () => {
      dismissalsResponse = [unpricedMarker()];

      const element = await mount();

      expect(attentionBadge(element).textContent!.trim()).to.equal('Healthy');
      const marker = element.shadowRoot!.querySelector(
        '[data-testid="unpriced-marker"]'
      )!;
      expect(marker.textContent!.replace(/\s+/g, ' ')).to.contain(
        'Apply to past usage'
      );
      const restore = element.shadowRoot!.querySelector(
        '[data-testid="restore-unpriced"]'
      ) as HTMLElement;
      expect(restore).to.exist;

      restore.click();
      await waitUntil(
        () => dismissalWrites.length > 0,
        'the restore was never written'
      );
      expect(dismissalWrites[0].method).to.equal('DELETE');
      expect(decodeURIComponent(dismissalWrites[0].url)).to.contain(
        'model-unpriced:example/reviewer'
      );
      await waitUntil(
        () => attentionBadge(element).textContent!.trim() === 'Attention',
        'the page stayed healthy after the marker was restored'
      );
    });

    // The stable fingerprint again: the page is not asked twice.
    it('stays quiet while the model keeps serving unpriced requests', async () => {
      dismissalsResponse = [unpricedMarker()];

      const element = await mount();

      expect(attentionBadge(element).textContent!.trim()).to.equal('Healthy');
      expect(
        element.shadowRoot!.querySelector('[data-testid="dismiss-model"]')
      ).to.equal(null);
    });

    it('needs both markers on a page that is failing and unpriced', async () => {
      failuresEnabled = true;
      dismissalsResponse = [unpricedMarker()];

      const element = await mount();

      expect(attentionBadge(element).textContent!.trim()).to.equal('Attention');
      // Only the open claim is offered.
      expect(menuValues(element)).to.eql(['expected', 'snoozed', 'fixed']);

      element
        .shadowRoot!.querySelector('[data-testid="model-attention"] sl-menu')!
        .dispatchEvent(
          new CustomEvent('sl-select', { detail: { item: { value: 'fixed' } } })
        );
      await waitUntil(
        () => attentionBadge(element).textContent!.trim() === 'Healthy',
        'the page stayed flagged after both markers were taken'
      );
      const title = attentionBadge(element).getAttribute('title')!;
      expect(title).to.contain('Marked fixed');
      expect(title).to.contain('marked expected');
    });

    it('says nothing about price when the model has one', async () => {
      pricingSource = 'catalog';

      const element = await mount();

      expect(
        element.shadowRoot!.querySelector('[data-testid="unpriced-attention"]')
      ).to.equal(null);
      expect(
        element.shadowRoot!.querySelector('[data-testid="model-attention"]')
      ).to.equal(null);
    });
  });
});
