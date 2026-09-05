import { fixture, html, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import { unifiedWebSocketManager } from '../../../services/unified-websocket-manager';
import './ai-models-view';
import {
  filterModels,
  isGatewayEnabled,
  type AIModelsView,
} from './ai-models-view';
import type { AIModel } from '../../../types';

describe('AIModelsView', () => {
  let fetchStub: sinon.SinonStub;
  let connectStub: sinon.SinonStub;
  let subscribeStub: sinon.SinonStub;

  beforeEach(() => {
    localStorage.removeItem('preloop.models.view_mode');
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');

    fetchStub = sinon.stub(window, 'fetch');
    fetchStub.callsFake(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString();

      if (url === '/api/v1/ai-models') {
        return new Response(
          JSON.stringify([
            {
              id: 'model-1',
              name: 'Claude Sonnet Primary',
              provider_name: 'Anthropic',
              model_identifier: 'claude-sonnet-4',
              meta_data: {
                gateway: {
                  enabled: true,
                  model_alias: 'preloop/anthropic/claude-sonnet-4',
                },
                managed_agent_display_name: 'Mini Claw',
              },
              is_default: true,
              created_at: '2026-03-01T10:00:00Z',
              updated_at: '2026-03-09T18:30:00Z',
            },
          ]),
          {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          }
        );
      }

      if (url.startsWith('/api/v1/ai-models/overview')) {
        return new Response(
          JSON.stringify({
            period_start: '2026-02-09T00:00:00Z',
            period_end: '2026-03-09T23:59:59Z',
            models: [
              {
                ai_model_id: 'model-1',
                model_name: 'Claude Sonnet Primary',
                provider_name: 'Anthropic',
                model_identifier: 'claude-sonnet-4',
                model_alias: 'preloop/anthropic/claude-sonnet-4',
                is_default: true,
                total_requests: 42,
                successful_requests: 40,
                failed_requests: 2,
                token_usage: {
                  prompt_tokens: 1200,
                  completion_tokens: 800,
                  total_tokens: 2000,
                },
                estimated_cost: 12.34,
                unpriced_request_count: 0,
                active_session_count: 3,
                last_request_at: '2026-03-09T18:30:00Z',
                pricing_source: 'override',
              },
            ],
          }),
          {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          }
        );
      }

      return new Response(
        JSON.stringify({ detail: `Unhandled request: ${url}` }),
        {
          status: 500,
          headers: { 'Content-Type': 'application/json' },
        }
      );
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
    localStorage.removeItem('preloop.models.view_mode');
    localStorage.clear();
  });

  it('links each configured model to its observability detail page', async () => {
    const element = (await fixture(
      html`<ai-models-view></ai-models-view>`
    )) as AIModelsView;

    await waitUntil(
      () => !(element as any).isLoading,
      'AI models view did not finish loading'
    );
    await element.updateComplete;

    const content = (element.shadowRoot?.textContent || '').replace(
      /\s+/g,
      ' '
    );
    expect(content).to.contain('Claude Sonnet Primary');
    expect(content).to.contain('View');
    expect(content).to.contain('Fleet spend');
    expect(content).to.contain('$12.34');
    expect(content).to.contain('42 requests');
    expect(content).to.contain('3 active sessions');
    expect(content).to.contain('Attention');
    expect(content).to.contain('preloop/anthropic/claude-sonnet-4');
    expect(content).to.contain('Mini Claw');
    expect(content).to.contain('Priced by account override');
    // One batch request for the page, plus the models list itself: the page
    // must never go back to one call per model.
    const modelScopedCalls = fetchStub
      .getCalls()
      .map((call: sinon.SinonSpyCall) => String(call.args[0]))
      .filter((url: string) => url.startsWith('/api/v1/ai-models/model-1'));
    expect(modelScopedCalls).to.eql([]);

    const nameLink = element.shadowRoot?.querySelector(
      'a.model-link[href="/console/ai-models/model-1"]'
    );
    const viewButton = element.shadowRoot?.querySelector(
      'sl-button[href="/console/ai-models/model-1"]'
    );

    expect(nameLink).to.not.equal(null);
    expect(viewButton).to.not.equal(null);
    expect(connectStub).to.have.been.calledOnce;
    expect(subscribeStub.callCount).to.equal(5);

    const providerSelect = element.shadowRoot?.querySelector(
      'sl-select.provider-filter'
    );
    const statusSelect = element.shadowRoot?.querySelector(
      'sl-select.status-filter'
    );
    expect(providerSelect?.getAttribute('label')).to.equal('Provider');
    expect(statusSelect?.getAttribute('label')).to.equal('Status');
  });

  it('hides the toolbar when a refresh fails with models still loaded', async () => {
    const element = (await fixture(
      html`<ai-models-view></ai-models-view>`
    )) as AIModelsView;

    await waitUntil(
      () => !(element as any).isLoading,
      'AI models view did not finish loading'
    );
    await element.updateComplete;
    expect(element.shadowRoot?.querySelector('list-toolbar')).to.exist;

    (element as any).error = 'Failed to refresh models';
    await element.updateComplete;

    expect(element.shadowRoot?.querySelector('list-toolbar')).to.equal(null);
    expect(element.shadowRoot?.querySelector('sl-alert')).to.exist;
  });

  const secondModel = {
    id: 'model-2',
    name: 'GPT-4o Mini',
    provider_name: 'OpenAI',
    model_identifier: 'gpt-4o-mini',
    meta_data: {
      gateway: { enabled: false, model_alias: 'preloop/openai/gpt-4o-mini' },
    },
    is_default: false,
    created_at: '2026-03-01T10:00:00Z',
    updated_at: '2026-03-09T18:30:00Z',
  };

  it('filters models by name and provider', () => {
    const models = [
      {
        id: 'model-1',
        name: 'Claude Sonnet Primary',
        provider_name: 'Anthropic',
        model_identifier: 'claude-sonnet-4',
        created_at: '2026-03-01T10:00:00Z',
        updated_at: '2026-03-09T18:30:00Z',
      },
      secondModel,
    ] as AIModel[];
    expect(
      filterModels(models, 'claude', '', '').map((m) => m.id)
    ).to.deep.equal(['model-1']);
    expect(
      filterModels(models, '', 'OpenAI', '').map((m) => m.id)
    ).to.deep.equal(['model-2']);
    expect(isGatewayEnabled(models[0])).to.equal(false);
    expect(isGatewayEnabled(secondModel as AIModel)).to.equal(false);
    expect(
      filterModels(models, '', '', 'disabled').map((m) => m.id)
    ).to.deep.equal(['model-1', 'model-2']);
    expect(filterModels(models, '', '', 'enabled')).to.deep.equal([]);
  });

  it('narrows list rows when search matches one model', async () => {
    fetchStub.restore();
    fetchStub = sinon.stub(window, 'fetch');
    fetchStub.callsFake(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString();
      if (url === '/api/v1/ai-models') {
        return new Response(
          JSON.stringify([
            {
              id: 'model-1',
              name: 'Claude Sonnet Primary',
              provider_name: 'Anthropic',
              model_identifier: 'claude-sonnet-4',
              meta_data: { gateway: { enabled: true } },
              is_default: true,
              created_at: '2026-03-01T10:00:00Z',
              updated_at: '2026-03-09T18:30:00Z',
            },
            secondModel,
          ]),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        );
      }
      if (url.startsWith('/api/v1/ai-models/overview')) {
        return new Response(
          JSON.stringify({
            period_start: '2026-02-09T00:00:00Z',
            period_end: '2026-03-09T23:59:59Z',
            models: [],
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        );
      }
      return new Response(JSON.stringify({ detail: url }), {
        status: 500,
        headers: { 'Content-Type': 'application/json' },
      });
    });

    const element = (await fixture(
      html`<ai-models-view></ai-models-view>`
    )) as AIModelsView;
    await waitUntil(
      () => !(element as any).isLoading,
      'AI models view did not finish loading'
    );
    await element.updateComplete;

    expect(element.shadowRoot?.querySelectorAll('.model-row')).to.have.lengthOf(
      2
    );

    const toolbar = element.shadowRoot?.querySelector('list-toolbar');
    expect(toolbar).to.exist;
    toolbar!.dispatchEvent(
      new CustomEvent('search-change', {
        detail: { value: 'openai' },
        bubbles: true,
        composed: true,
      })
    );
    await element.updateComplete;

    const rows = element.shadowRoot?.querySelectorAll('.model-row');
    expect(rows).to.have.lengthOf(1);
    expect(rows?.[0].textContent).to.include('GPT-4o Mini');
  });

  it('switches from list rows to cards', async () => {
    const element = (await fixture(
      html`<ai-models-view></ai-models-view>`
    )) as AIModelsView;
    await waitUntil(
      () => !(element as any).isLoading,
      'AI models view did not finish loading'
    );
    await element.updateComplete;

    expect(element.shadowRoot?.querySelector('.model-row')).to.exist;
    expect(element.shadowRoot?.querySelector('.models-grid')).to.equal(null);

    const toolbar = element.shadowRoot?.querySelector('list-toolbar');
    toolbar!.dispatchEvent(
      new CustomEvent('view-change', {
        detail: { value: 'cards' },
        bubbles: true,
        composed: true,
      })
    );
    await element.updateComplete;

    expect(element.shadowRoot?.querySelector('.models-grid')).to.exist;
    expect(element.shadowRoot?.querySelector('.model-card')).to.exist;
    expect(element.shadowRoot?.querySelector('.model-row')).to.equal(null);
    expect(element.shadowRoot?.textContent).to.contain('Default');
    expect(element.shadowRoot?.textContent).to.contain('View');
  });
});
